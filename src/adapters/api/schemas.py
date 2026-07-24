"""OpenAI-compatible request/response schemas.

Kept deliberately lenient (``extra="ignore"``) so the wide variety of fields
modern clients send — ``temperature``, ``tools``, ``reasoning_effort``, etc. —
never trigger a 422. ``Message.content`` accepts either a plain string or an
OpenAI "content parts" array (text + image_url), with helpers to normalise it.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str = "user"
    content: Optional[Union[str, List[Any]]] = ""
    reasoning_content: Optional[str] = None
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List[Any]] = None

    def text(self) -> str:
        """Flatten content into plain text (joining any text parts)."""
        c = self.content
        if c is None:
            return ""
        if isinstance(c, str):
            return c
        parts: List[str] = []
        for part in c:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                ptype = part.get("type")
                if ptype in (None, "text", "input_text"):
                    val = part.get("text") or part.get("content") or ""
                    if isinstance(val, str):
                        parts.append(val)
        return "\n".join(p for p in parts if p)

    def image_urls(self) -> List[str]:
        """Extract any image_url references from content parts (data URIs or URLs)."""
        c = self.content
        if not isinstance(c, list):
            return []
        urls: List[str] = []
        for part in c:
            if not isinstance(part, dict):
                continue
            if part.get("type") in ("image_url", "input_image"):
                img = part.get("image_url") or part.get("image") or part.get("url")
                if isinstance(img, dict):
                    img = img.get("url")
                if isinstance(img, str) and img:
                    urls.append(img)
        return urls


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str = "opus-latest"
    messages: List[Message]
    stream: bool = False
    # Accepted for compatibility (currently advisory only).
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    user: Optional[str] = None
    stop: Optional[Union[str, List[str]]] = None


class Choice(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int = 0
    message: Message
    finish_reason: Optional[str] = None


class ChatCompletionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[Choice]
    usage: Optional[Dict[str, int]] = None
