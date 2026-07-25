"""OpenAI-compatible endpoints: ``/v1/models`` and ``/v1/chat/completions``."""

from __future__ import annotations

import uuid
from typing import Any, AsyncGenerator, Dict, Optional

from fastapi import APIRouter, Depends, Header
from fastapi.responses import StreamingResponse

from src.core.config import SESSION_HEADER
from src.core.logging_config import get_logger
from src.core.schemas import ChatCompletionRequest, ChatCompletionResponse
from src.core.sse import estimate_tokens
from src.core.stats import STATS
from src.adapters.api.deps import chat_service, verify_api_key

logger = get_logger("api")

router = APIRouter(dependencies=[Depends(verify_api_key)])


@router.get("/v1/models")
async def list_models() -> Dict[str, Any]:
    """List available models in OpenAI format."""
    return {"object": "list", "data": chat_service.get_available_models()}


@router.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    x_session_id: Optional[str] = Header(None, alias=SESSION_HEADER),
):
    """Handle a chat completion, streaming or not."""
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    logger.info(
        "[%s] completions model=%s stream=%s session_hdr=%s",
        request_id, req.model, req.stream, bool(x_session_id),
    )

    if req.stream:
        return StreamingResponse(
            _tracked_stream(request_id, req, x_session_id), media_type="text/event-stream"
        )
    return await _tracked_completion(request_id, req, x_session_id)


async def _tracked_stream(
    request_id: str, req: ChatCompletionRequest, session_id: Optional[str]
) -> AsyncGenerator[str, None]:
    """Stream the completion, recording stats even if the client disconnects."""
    entry = STATS.request_started(request_id, req.model, stream=True)
    meta: Dict[str, Any] = {}
    error: Optional[str] = None
    try:
        async for chunk in chat_service.execute_chat_stream(req, session_id=session_id, meta=meta):
            yield chunk
    except Exception as exc:  # noqa: BLE001 - recorded below, then re-raised
        error = str(exc)
        raise
    finally:
        # Streamed turns have no usage block, so fall back to an estimate.
        estimated = estimate_tokens(meta.get("completion_text", "")) or None
        _finish(entry, meta, error=error, completion_fallback=estimated)


async def _tracked_completion(
    request_id: str, req: ChatCompletionRequest, session_id: Optional[str]
) -> ChatCompletionResponse:
    entry = STATS.request_started(request_id, req.model, stream=False)
    meta: Dict[str, Any] = {}
    try:
        response = await chat_service.execute_chat_non_stream(req, session_id=session_id, meta=meta)
    except Exception as exc:  # noqa: BLE001 - recorded below, then re-raised
        STATS.request_finished(entry, status="error", error=str(exc))
        raise
    _finish(entry, meta)
    return response


def _finish(
    entry: Dict[str, Any],
    meta: Dict[str, Any],
    error: Optional[str] = None,
    completion_fallback: Optional[int] = None,
) -> None:
    failure = error or meta.get("error")
    STATS.request_finished(
        entry,
        status="error" if failure else "ok",
        prompt_tokens=meta.get("prompt_tokens"),
        completion_tokens=meta.get("completion_tokens") or completion_fallback,
        session_key=meta.get("session_key"),
        thread_id=meta.get("thread_id"),
        reused_thread=meta.get("reused_thread"),
        error=failure,
    )
