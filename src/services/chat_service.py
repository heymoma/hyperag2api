"""Chat orchestration: the use case that turns an OpenAI request into an answer.

This module owns the *sequence* — resolve the session, get cookies, find or
create a thread, stream, persist the mapping — and delegates every specialised
concern to a collaborator:

* :mod:`src.services.conversation` — which thread this request belongs to, and
  what text to send upstream.
* :mod:`src.services.threads` — thread creation with account rotation.
* :mod:`src.services.attachments` — multimodal image uploads.
* :mod:`src.services.render` — backend events → OpenAI delta chunks.
* :mod:`src.services.tool_mode` — the client tool-calling turn, which follows a
  different protocol end to end.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

from src.core import config
from src.core.config import MODEL_MAPPING
from src.core.interfaces import AuthError, ChatBackend, CookieProvider
from src.core.logging_config import get_logger
from src.core.schemas import ChatCompletionRequest, ChatCompletionResponse, Choice, Message
from src.core.session_store import SessionStore
from src.core.sse import DONE, build_usage, format_openai_chunk
from src.services import conversation, tool_bridge
from src.services.attachments import AttachmentUploader
from src.services.conversation import ResolvedSession
from src.services.render import PlainStreamRenderer
from src.services.threads import ThreadFactory, fire_interrupt
from src.services.tool_mode import ToolModeExecutor

logger = get_logger("chat")


class ChatService:
    """Orchestrates the chat completion use case (business logic layer)."""

    def __init__(
        self,
        cookie_provider: CookieProvider,
        chat_backend: ChatBackend,
        session_store: Optional[SessionStore] = None,
    ) -> None:
        self.cookie_provider = cookie_provider
        self.chat_backend = chat_backend
        self.session_store = session_store or SessionStore.from_config()
        self.threads = ThreadFactory(cookie_provider, chat_backend)
        self.uploader = AttachmentUploader(chat_backend)
        self.tool_mode = ToolModeExecutor(
            cookie_provider, chat_backend, self.session_store, self.threads, self.uploader
        )

    # ------------------------------------------------------------------ #
    # Model listing                                                       #
    # ------------------------------------------------------------------ #
    def get_available_models(self) -> List[Dict[str, str]]:
        return [{"id": name, "object": "model", "owned_by": "hyperagent"} for name in MODEL_MAPPING]

    # ------------------------------------------------------------------ #
    # Streaming                                                           #
    # ------------------------------------------------------------------ #
    async def execute_chat_stream(
        self,
        req: ChatCompletionRequest,
        session_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[str, None]:
        """Stream one completion as OpenAI-compatible SSE chunks.

        ``meta`` is an out-parameter the API layer reads for request statistics
        (thread id, session key, token counts, error).
        """
        meta = meta if meta is not None else {}

        # Client-side tool calling is a separate, self-contained protocol — only
        # engaged when the request actually carries tools.
        if req.tools and not tool_bridge.tools_disabled(req.tool_choice):
            async for chunk in self.tool_mode.execute(req, session_id, meta):
                yield chunk
            return

        chat_id = f"chatcmpl-{uuid.uuid4()}"
        model = req.model
        session = conversation.resolve_session(
            req.model, req.messages, session_id=session_id, user=req.user
        )
        meta["session_key"] = session.lookup_key

        try:
            cookies = await self.cookie_provider.get_cookies()
        except Exception as exc:
            logger.error("Failed to retrieve browser cookies: %s", exc)
            meta["error"] = str(exc)
            yield format_openai_chunk(chat_id, model, "", role="assistant")
            yield format_openai_chunk(chat_id, model, f"\n[Cookie Error: {exc}]")
            yield format_openai_chunk(chat_id, model, "", finish_reason="stop")
            yield DONE
            return

        yield format_openai_chunk(chat_id, model, "", role="assistant")

        thread_id: Optional[str] = None
        renderer = PlainStreamRenderer(chat_id, model)
        try:
            thread_id, content, cookies, reused = await self._prepare_turn(req, session, cookies)
            meta["thread_id"] = thread_id
            meta["reused_thread"] = reused

            attachments = await self.uploader.collect(req.messages, thread_id, cookies)
            source = self.chat_backend.stream_chat(
                thread_id, content, cookies,
                session_id=session.explicit_id, attachments=attachments,
            )
            async for chunk in renderer.render(source):
                yield chunk

            await self._remember(session, thread_id, renderer.completion_text, len(req.messages))
            meta["completion_text"] = renderer.completion_text

        except asyncio.CancelledError:
            logger.info("Client disconnected; interrupting thread %s", thread_id)
            fire_interrupt(self.chat_backend, thread_id, cookies)
            raise
        except AuthError as exc:
            logger.warning("Session auth error during stream (%s); invalidating session.", exc)
            self.cookie_provider.invalidate()
            meta["error"] = str(exc)
            yield format_openai_chunk(
                chat_id, model,
                f"\n[Stream Error: {exc} (Session invalidated, rotating on next request)]",
            )
        except Exception as exc:
            logger.error("Stream error in execute_chat_stream: %s", exc)
            meta["error"] = str(exc)
            yield format_openai_chunk(chat_id, model, f"\n[Stream Error: {exc}]")

        yield format_openai_chunk(chat_id, model, "", finish_reason="stop")
        yield DONE

    # ------------------------------------------------------------------ #
    async def _prepare_turn(
        self,
        req: ChatCompletionRequest,
        session: ResolvedSession,
        cookies: Dict[str, str],
    ) -> "tuple[str, str, Dict[str, str], bool]":
        """Find or create this request's thread and decide what to send.

        Returns (thread_id, content, cookies, reused). A reused thread already
        holds the history, so only the newest message goes upstream; a fresh one
        needs the full dialog replayed unless this is the opening turn.
        """
        thread_id = await self.session_store.get(session.lookup_key)
        if thread_id:
            logger.info("Reusing thread %s for session %s", thread_id, session.lookup_key)
            return thread_id, conversation.latest_text(req.messages), cookies, True

        _, combined = conversation.split_prompts(req.messages)
        thread_id, cookies = await self.threads.create(
            session.model, session.system_prompt, session.explicit_id, cookies
        )
        await self.chat_backend.warm_thread(thread_id, cookies)
        first_turn = session.is_first_turn
        content = conversation.latest_text(req.messages) if first_turn else combined
        logger.info(
            "Created thread %s for session %s (first_turn=%s)",
            thread_id, session.lookup_key, first_turn,
        )
        return thread_id, content, cookies, False

    async def _remember(
        self,
        session: ResolvedSession,
        thread_id: str,
        completion_text: str,
        message_count: int,
    ) -> None:
        """Persist the mappings the next turn will look this thread up by."""
        try:
            if session.explicit_id:
                await self.session_store.put(
                    conversation.explicit_key(session.explicit_id),
                    thread_id, session.model, message_count,
                )
            # Always store the forward prefix key so the next turn matches by history.
            await self.session_store.put(
                session.forward_key(completion_text), thread_id, session.model, message_count
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Failed to persist session mapping: %s", exc)

    # ------------------------------------------------------------------ #
    # Non-streaming                                                       #
    # ------------------------------------------------------------------ #
    async def execute_chat_non_stream(
        self,
        req: ChatCompletionRequest,
        session_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> ChatCompletionResponse:
        """Accumulate the streamed chunks into a standard ChatCompletionResponse."""
        meta = meta if meta is not None else {}
        chat_id = f"chatcmpl-{uuid.uuid4()}"

        text, reasoning, tool_calls, finish_reason = await self._accumulate(
            self.execute_chat_stream(req, session_id=session_id, meta=meta)
        )

        usage = None
        if config.ENABLE_USAGE:
            system_prompt, combined = conversation.split_prompts(req.messages)
            usage = build_usage(f"{system_prompt}\n{combined}", text + reasoning)
            meta["prompt_tokens"] = usage["prompt_tokens"]
            meta["completion_tokens"] = usage["completion_tokens"]

        message = Message(
            role="assistant",
            # A tool-call-only turn must report content: null, not "".
            content=(text if text or not tool_calls else None),
            reasoning_content=reasoning or None,
            tool_calls=tool_calls or None,
        )
        return ChatCompletionResponse(
            id=chat_id,
            created=int(time.time()),
            model=req.model,
            choices=[Choice(index=0, message=message, finish_reason=finish_reason)],
            usage=usage,
        )

    @staticmethod
    async def _accumulate(
        stream: AsyncGenerator[str, None]
    ) -> "tuple[str, str, List[Dict[str, Any]], str]":
        """Fold an SSE stream back into (text, reasoning, tool_calls, finish)."""
        text = ""
        reasoning = ""
        tool_calls: List[Dict[str, Any]] = []
        finish_reason = "stop"

        async for chunk in stream:
            if not chunk.startswith("data: "):
                continue
            payload = chunk[len("data: "):].strip()
            if payload == "[DONE]":
                break
            try:
                data = json.loads(payload)
            except Exception:
                continue

            choices = data.get("choices") or []
            if not choices:
                continue
            if choices[0].get("finish_reason"):
                finish_reason = choices[0]["finish_reason"]

            delta = choices[0].get("delta", {})
            text += delta.get("content") or ""
            reasoning += delta.get("reasoning_content") or ""
            for call in delta.get("tool_calls") or []:
                tool_calls.append({
                    "id": call.get("id"),
                    "type": call.get("type", "function"),
                    "function": call.get("function", {}),
                })

        return text, reasoning, tool_calls, finish_reason
