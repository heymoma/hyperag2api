"""Rendering backend events as an OpenAI-compatible delta stream.

This is the plain (non tool-calling) path: every frame Hyperagent sends becomes
zero or more ``chat.completion.chunk`` events. Three things make it more than a
straight mapping:

* **Reasoning** can be surfaced as the native ``reasoning_content`` field, as
  inline ``<think>`` tags, or both — clients differ in what they understand.
  When think-tags are in play the renderer has to track whether a block is open
  so it can be closed before ordinary text resumes.
* **Backend tool activity** is either hidden, shown as readable prose, or
  re-emitted as real ``tool_calls`` deltas.
* **Silence** is answered with heartbeats so long cold starts do not time out.

The renderer also accumulates the assistant-visible text, which the caller needs
both for usage accounting and to compute the next turn's session key.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncGenerator, AsyncIterator, Dict, List

from src.core import config
from src.core.sse import format_openai_chunk, format_openai_keepalive_chunk, sse_comment
from src.services import stream_events as events
from src.services.streaming import KEEPALIVE, iter_with_keepalive

_THINK_OPEN = "<think>\n"
_THINK_CLOSE = "\n</think>\n\n"


class PlainStreamRenderer:
    """Turns one backend stream into OpenAI SSE chunks.

    Single-use: construct, iterate :meth:`render` once, then read
    :attr:`completion_text`.
    """

    def __init__(self, chat_id: str, model: str) -> None:
        self.chat_id = chat_id
        self.model = model
        self._parts: List[str] = []
        self._in_think = False
        self._tool_index = 0

    @property
    def completion_text(self) -> str:
        """The assistant-visible text emitted so far."""
        return "".join(self._parts)

    # ------------------------------------------------------------------ #
    def _emit(self, text: str) -> str:
        """Record text as part of the completion and format it as a chunk."""
        self._parts.append(text)
        return format_openai_chunk(self.chat_id, self.model, text)

    def _close_think(self) -> List[str]:
        """Close an open ``<think>`` block, if any."""
        if not self._in_think:
            return []
        self._in_think = False
        return [self._emit(_THINK_CLOSE)]

    # ------------------------------------------------------------------ #
    async def render(
        self, source: AsyncIterator[Dict[str, Any]]
    ) -> AsyncGenerator[str, None]:
        async for item in iter_with_keepalive(source, config.KEEPALIVE_INTERVAL):
            if item is KEEPALIVE:
                yield sse_comment("keepalive")
                yield format_openai_keepalive_chunk(self.chat_id, self.model)
                continue

            data_type = events.event_type(item)
            if events.is_ignorable(data_type):
                continue
            if events.is_terminal(data_type):
                break

            for chunk in self._render_frame(data_type, item):
                yield chunk

        for chunk in self._close_think():
            yield chunk

    def _render_frame(self, data_type: str, data: Dict[str, Any]) -> List[str]:
        if events.is_reasoning(data_type):
            return self._render_reasoning(data)
        if events.is_ask_question(data_type, data):
            return self._render_ask_question(data)
        if events.is_tool(data_type, data):
            return self._render_tool(data)
        if events.is_text(data_type, data):
            return self._render_text(data)
        return []

    # ------------------------------------------------------------------ #
    def _render_reasoning(self, data: Dict[str, Any]) -> List[str]:
        text = events.event_text(data)
        if not text:
            return []
        style = config.REASONING_STYLE
        out: List[str] = []
        if style in ("think_tags", "both") and not self._in_think:
            self._in_think = True
            out.append(self._emit(_THINK_OPEN))
        if style in ("reasoning_content", "both"):
            out.append(format_openai_chunk(self.chat_id, self.model, "", reasoning=text))
        if style in ("think_tags", "both"):
            out.append(self._emit(text))
        return out

    def _render_ask_question(self, data: Dict[str, Any]) -> List[str]:
        # Steering prompts are always shown as prose — they are a question for
        # the user, not a tool the client is expected to run.
        out = self._close_think()
        out.append(self._emit(events.render_ask_question(events.tool_input(data))))
        return out

    def _render_tool(self, data: Dict[str, Any]) -> List[str]:
        mode = config.TOOLCALL_MODE
        if mode == "off":
            return []

        name = events.tool_name(data)
        if mode != "openai":
            return [self._emit(f"\n\n> 🔧 `{name}`\n")]

        call = {
            "index": self._tool_index,
            "id": f"call_{self._tool_index}_{uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(events.tool_input(data), ensure_ascii=False),
            },
        }
        self._tool_index += 1
        return [format_openai_chunk(self.chat_id, self.model, "", tool_calls=[call])]

    def _render_text(self, data: Dict[str, Any]) -> List[str]:
        text = events.event_text(data)
        if not text:
            return []
        return self._close_think() + [self._emit(text)]
