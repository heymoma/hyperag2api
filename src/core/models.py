"""SSE formatting helpers for OpenAI-compatible streaming output.

The request/response Pydantic models live in ``src.adapters.api.schemas``; this
module holds only the wire-formatting helpers so they can be reused without
pulling in FastAPI/pydantic request types.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional


def sse(payload: Dict[str, Any]) -> str:
    """Serialize a dict as one SSE ``data:`` event."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def sse_comment(text: str = "") -> str:
    """An SSE comment line — used as a keepalive; ignored by OpenAI clients."""
    return f": {text}\n\n"


DONE = "data: [DONE]\n\n"


def format_openai_chunk(
    id_str: str,
    model: str,
    content: str,
    role: Optional[str] = None,
    reasoning: Optional[str] = None,
    finish_reason: Optional[str] = None,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Format a single ``chat.completion.chunk`` SSE event."""
    delta: Dict[str, Any] = {}
    if role:
        delta["role"] = role
    if content:
        delta["content"] = content
    if reasoning:
        # Emit both spellings for maximum client compatibility.
        delta["reasoning_content"] = reasoning
        delta["reasoning"] = reasoning
    if tool_calls:
        delta["tool_calls"] = tool_calls

    chunk = {
        "id": id_str,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return sse(chunk)


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) for usage accounting."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def build_usage(prompt_text: str, completion_text: str) -> Dict[str, int]:
    p = estimate_tokens(prompt_text)
    c = estimate_tokens(completion_text)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}
