"""Classification of the raw SSE frames Hyperagent streams back.

The backend emits a loose, untyped event soup: assistant text, thinking tokens,
tool activity and lifecycle chatter all arrive as ``{"type": ..., ...}`` dicts
with inconsistent key names. Everything that decides *what a frame means* lives
here, so the renderers can stay pure presentation.

All predicates are total: an unrecognised frame is simply not text, not
reasoning and not a tool event, and gets dropped.
"""

from __future__ import annotations

from typing import Any, Dict

# Explicit reasoning/thinking frames. Anything starting with "reasoning"/
# "thinking" is also treated as reasoning (the backend varies the suffix).
REASONING_TYPES = {"thinking", "reasoning", "redacted_thinking", "reasoning_content"}

# Frames carrying assistant-visible text.
TEXT_TYPES = {"text", "content", "message", "answer"}

# Backend lifecycle/status frames (verified live) that carry no assistant output.
IGNORE_TYPES = {
    "thread_runtime_latched", "sandbox_status", "session_start", "session_end",
    "thread_status", "ping", "heartbeat", "keepalive",
}

TOOL_TYPES = {"tool_input_ready", "tool_call", "tool-call", "tool_use", "tool"}

# The backend's interactive-steering tool. Its payload is a question with
# options, which is useful to the user as prose rather than as a tool call.
ASK_QUESTION_TOOL = "mcp__t__AskQuestion"

# Keys that may hold a frame's payload text, in priority order.
_TEXT_KEYS = ("content", "text", "thinking", "delta")


def event_type(data: Dict[str, Any]) -> str:
    return str(data.get("type", ""))


def is_ignorable(data_type: str) -> bool:
    return data_type in IGNORE_TYPES


def is_terminal(data_type: str) -> bool:
    return data_type == "done"


def is_reasoning(data_type: str) -> bool:
    if data_type in REASONING_TYPES:
        return True
    normalized = data_type.replace("-", "_")
    return normalized.startswith("reasoning") or normalized.startswith("thinking")


def is_tool(data_type: str, data: Dict[str, Any]) -> bool:
    return "toolName" in data or "tool_name" in data or data_type in TOOL_TYPES


def is_text(data_type: str, data: Dict[str, Any]) -> bool:
    # An untyped frame carrying "content" is text — some backends omit "type".
    return data_type in TEXT_TYPES or ("content" in data and not data_type)


def is_ask_question(data_type: str, data: Dict[str, Any]) -> bool:
    return is_tool(data_type, data) and data.get("toolName") == ASK_QUESTION_TOOL


def event_text(data: Dict[str, Any]) -> str:
    """The frame's payload text, or ``""`` when it carries none."""
    for key in _TEXT_KEYS:
        val = data.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def tool_name(data: Dict[str, Any]) -> str:
    return str(data.get("toolName") or data.get("tool_name") or "tool")


def tool_input(data: Dict[str, Any]) -> Dict[str, Any]:
    value = data.get("toolInput") or data.get("tool_input") or {}
    return value if isinstance(value, dict) else {}


def render_ask_question(payload: Dict[str, Any]) -> str:
    """Render a steering prompt as readable markdown for the chat client."""
    questions = payload.get("questions", []) if isinstance(payload, dict) else []
    lines = ["\n\n### 📋 Action Required (Steering Options):"]
    for q in questions:
        lines.append(f"**Question:** {q.get('question')}")
        for idx, opt in enumerate(q.get("options", []) or []):
            lines.append(f"- **Option {idx + 1}:** {opt.get('label')} (Value: `{opt.get('value')}`)")
    return "\n".join(lines) + "\n"
