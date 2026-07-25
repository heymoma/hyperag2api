"""HTTP client for hyperagent.com's thread/chat API.

Hardened over the original: per-call timeouts are split (short for one-shot
create/warm calls, long read for streaming), transient failures are retried with
exponential backoff, SSE parsing tolerates keepalives and non-JSON noise, and the
client can carry a stable ``sessionId`` plus best-effort attachment uploads.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from src.core.anti_detection import get_async_session, apply_human_jitter, get_endpoint_headers
from src.core.interfaces import ChatBackend, AuthError
from src.core import config
from src.core.config import (
    HYPERAGENT_THREADS_API,
    HYPERAGENT_WARM_API_TEMPLATE,
    HYPERAGENT_CHAT_API_TEMPLATE,
    HYPERAGENT_INTERRUPT_API_TEMPLATE,
    HYPERAGENT_UPLOADS_API,
    DEFAULT_HEADERS,
)
from src.core.logging_config import get_logger

logger = get_logger("backend")

_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class HyperagentClient(ChatBackend):
    """Implementation of ChatBackend for communicating with hyperagent.com."""

    def __init__(
        self,
        headers: Optional[Dict[str, str]] = None,
        max_retries: Optional[int] = None,
        retry_base_delay: Optional[float] = None,
    ):
        self.headers = dict(headers or DEFAULT_HEADERS)
        self.max_retries = config.MAX_RETRIES if max_retries is None else max_retries
        self.retry_base_delay = config.RETRY_BASE_DELAY if retry_base_delay is None else retry_base_delay

    # ------------------------------------------------------------------ #
    # Timeouts / retry helpers                                           #
    # ------------------------------------------------------------------ #
    def _timeout(self, read: float) -> httpx.Timeout:
        return httpx.Timeout(
            connect=config.HTTP_CONNECT_TIMEOUT,
            read=read,
            write=config.HTTP_WRITE_TIMEOUT,
            pool=config.HTTP_POOL_TIMEOUT,
        )

    async def _backoff(self, attempt: int) -> None:
        delay = min(self.retry_base_delay * (2 ** attempt), config.RETRY_MAX_DELAY)
        delay += random.uniform(0, delay * 0.1)  # small jitter
        await asyncio.sleep(delay)

    # ------------------------------------------------------------------ #
    async def create_thread(
        self,
        model: str,
        system_prompt: str,
        cookies: Dict[str, str],
        session_id: Optional[str] = None,
    ) -> str:
        """Initialize a new chat thread on Hyperagent (with retries)."""
        await apply_human_jitter()
        payload = {
            "modelId": model,
            "defaultSubagentModel": None,
            "source": "web.new_thread",
            "systemPrompt": (system_prompt or "").strip(),
        }

        last_error: Optional[str] = None
        for attempt in range(self.max_retries + 1):
            try:
                headers = get_endpoint_headers("new_thread", base_headers=self.headers)
                async with get_async_session(
                    cookies=cookies, headers=headers, timeout=self._timeout(config.REQUEST_READ_TIMEOUT)
                ) as client:
                    response = await client.post(HYPERAGENT_THREADS_API, json=payload)
                    if response.status_code == 200:
                        return response.json()["id"]
                    if response.status_code in (401, 403):
                        raise AuthError(
                            f"Authentication failed creating thread (status {response.status_code}). "
                            "The Hyperagent session may have expired."
                        )
                    last_error = f"status {response.status_code}"
                    if response.status_code not in _RETRYABLE_STATUS:
                        logger.error("Thread creation failed: %s - %s", response.status_code, response.text[:300])
                        raise RuntimeError(f"Failed to initialize thread on Hyperagent: {last_error}")
                    logger.warning("Thread creation transient failure (%s), attempt %d.", last_error, attempt + 1)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = f"network error: {exc}"
                logger.warning("Thread creation network error, attempt %d: %s", attempt + 1, exc)
            if attempt < self.max_retries:
                await self._backoff(attempt)

        raise RuntimeError(f"Failed to initialize thread on Hyperagent ({last_error}).")

    # ------------------------------------------------------------------ #
    async def warm_thread(self, thread_id: str, cookies: Dict[str, str]) -> None:
        """Warm the created thread (best-effort, with light retries)."""
        warm_url = HYPERAGENT_WARM_API_TEMPLATE.format(thread_id=thread_id)
        for attempt in range(self.max_retries + 1):
            try:
                headers = get_endpoint_headers("warm", thread_id=thread_id, base_headers=self.headers)
                async with get_async_session(
                    cookies=cookies, headers=headers, timeout=self._timeout(config.REQUEST_READ_TIMEOUT)
                ) as client:
                    response = await client.post(warm_url)
                    if response.status_code == 200:
                        return
                    if response.status_code not in _RETRYABLE_STATUS:
                        logger.warning("Thread warming failed: %s - %s", response.status_code, response.text[:200])
                        return
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                logger.debug("Warm thread network error, attempt %d: %s", attempt + 1, exc)
            if attempt < self.max_retries:
                await self._backoff(attempt)
        logger.warning("Thread warming did not succeed after retries (non-fatal).")

    # ------------------------------------------------------------------ #
    def _build_chat_payload(
        self,
        prompt: str,
        session_id: Optional[str],
        attachments: Optional[List[Dict[str, Any]]],
        feature_flags: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "sessionId": session_id,
            "content": (prompt or "").strip(),
            "debug": False,
            "enabledIntegrations": [],
        }
        payload.update(config.get_chat_feature_flags())
        if feature_flags:
            payload.update(feature_flags)
        if attachments:
            ids = [a.get("id") for a in attachments if a.get("id")]
            if ids:
                payload["attachmentIds"] = ids
        return payload

    async def stream_chat(
        self,
        thread_id: str,
        prompt: str,
        cookies: Dict[str, str],
        session_id: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        feature_flags: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Send a message to the thread and stream response events."""
        await apply_human_jitter()
        chat_url = HYPERAGENT_CHAT_API_TEMPLATE.format(thread_id=thread_id)
        chat_payload = self._build_chat_payload(prompt, session_id, attachments, feature_flags)

        headers = get_endpoint_headers("chat", thread_id=thread_id, base_headers=self.headers)
        async with get_async_session(
            cookies=cookies, headers=headers, timeout=self._timeout(config.STREAM_READ_TIMEOUT)
        ) as client:
            async with client.stream("POST", chat_url, json=chat_payload) as response:
                if response.status_code != 200:
                    if response.status_code in (401, 403):
                        raise AuthError(
                            f"Authentication failed on chat (status {response.status_code})."
                        )
                    logger.error("Chat request failed with status: %s", response.status_code)
                    raise RuntimeError(f"Chat request failed with status {response.status_code}")

                async for line in response.aiter_lines():
                    if not line:
                        continue
                    stripped = line.strip()
                    if stripped.startswith(":") or stripped.startswith("event:"):
                        continue
                    if not stripped.startswith("data:"):
                        continue
                    data_str = stripped[len("data:"):].strip()
                    if not data_str:
                        continue
                    if data_str == "[DONE]":
                        break
                    try:
                        yield json.loads(data_str)
                    except json.JSONDecodeError:
                        logger.debug("Skipping non-JSON SSE frame: %s", data_str[:120])
                        continue

    # ------------------------------------------------------------------ #
    async def upload_file(
        self,
        thread_id: str,
        cookies: Dict[str, str],
        filename: str,
        data: bytes,
        content_type: str,
    ) -> Optional[Dict[str, Any]]:
        """Upload an attachment via the presigned-S3 flow (verified from web traffic)."""
        size = len(data)
        try:
            headers = get_endpoint_headers("upload", thread_id=thread_id, base_headers=self.headers)
            async with get_async_session(
                cookies=cookies, headers=headers, timeout=self._timeout(config.REQUEST_READ_TIMEOUT)
            ) as client:
                resp = await client.post(
                    HYPERAGENT_UPLOADS_API,
                    json={"filename": filename, "mimeType": content_type, "sizeBytes": size},
                )
                if resp.status_code not in (200, 201):
                    logger.warning("Upload init failed (%s): %s", resp.status_code, resp.text[:160])
                    return None
                body = resp.json()
                file_id = body.get("fileId") or body.get("id")
                upload_url = body.get("uploadUrl") or body.get("url")
                if not file_id or not upload_url:
                    logger.warning("Upload init returned no fileId/uploadUrl: %s", str(body)[:160])
                    return None

            async with get_async_session(timeout=self._timeout(config.REQUEST_READ_TIMEOUT)) as s3:
                put = await s3.put(upload_url, content=data, headers={"If-None-Match": "*"})
                if put.status_code not in (200, 201, 204):
                    logger.warning("Presigned PUT failed (%s): %s", put.status_code, put.text[:120])
                    return None

            logger.info("Uploaded attachment %s -> fileId=%s", filename, file_id)
            return {"id": file_id, "name": filename, "mimeType": content_type, "size": size}
        except Exception as exc:  # pragma: no cover - network/best-effort
            logger.warning("Attachment upload error for %s: %s", filename, exc)
            return None

    async def interrupt(self, thread_id: str, cookies: Dict[str, str]) -> None:
        """Best-effort backend cancel (used on client disconnect)."""
        url = HYPERAGENT_INTERRUPT_API_TEMPLATE.format(thread_id=thread_id)
        try:
            headers = get_endpoint_headers("interrupt", thread_id=thread_id, base_headers=self.headers)
            async with get_async_session(
                cookies=cookies, headers=headers, timeout=self._timeout(10.0)
            ) as client:
                await client.post(url)
        except Exception as exc:  # pragma: no cover - best-effort
            logger.debug("Interrupt for %s failed (non-fatal): %s", thread_id, exc)
