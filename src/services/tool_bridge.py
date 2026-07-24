"""Client-side tool-calling bridge (OpenAI function-calling over a text agent).

Hyperagent's `/chat` endpoint drives a server-side agent that only streams
text/thinking — it does not accept client tool schemas nor speak the OpenAI
tool-call handshake. To let clients like OpenCode use their OWN tools/MCP, we
emulate the protocol with a prompt contract:

1. When a request carries `tools`, we inject a preamble describing them and ask
   the model to emit ``<tool_call>{json}</tool_call>`` blocks.
2. We parse those blocks out of the streamed text and re-expose them as OpenAI
   ``tool_calls`` with ``finish_reason:"tool_calls"``.
3. The client executes the tools and posts ``role:"tool"`` results back; we
   forward them into the same thread so the agent can continue.

This is best-effort and inherently less reliable than native function calling,
but works for the common single/parallel tool-call loop.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

TOOL_CALL_SENTINEL = "<tool_call>"

TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
# Fallback: fenced ```tool_call / ```json blocks holding a {"name": ...} object.
FENCED_RE = re.compile(r"```(?:tool_call|json)?\s*(\{.*?\})\s*```", re.DOTALL)


def tools_signature(tools: List[Any]) -> str:
    """Stable short hash of the tool set — changes when tools are added/removed,
    so a primed thread re-sends the full preamble if the client's MCP set changes."""
    defs = _tool_defs(tools)
    payload = json.dumps([[d["name"], d["parameters"]] for d in defs], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8", "ignore")).hexdigest()[:16]


def sentinel_holdback(pending: str, sentinel: str = TOOL_CALL_SENTINEL) -> int:
    """Length of the trailing suffix of ``pending`` that is a partial prefix of
    ``sentinel`` — those chars must be held back (not streamed) in case the next
    tokens complete the sentinel."""
    maxk = min(len(pending), len(sentinel) - 1)
    for k in range(maxk, 0, -1):
        if pending.endswith(sentinel[:k]):
            return k
    return 0


def build_tool_reminder() -> str:
    """One-line contract reminder for threads that already saw the full preamble."""
    return (
        "(Client tools available — to call one, output "
        '<tool_call>{"name":"<fn>","arguments":{...}}</tool_call> and nothing else; '
        "the client runs it and returns the result next turn.)"
    )


def _tool_defs(tools: List[Any]) -> List[Dict[str, Any]]:
    """Normalise OpenAI tool entries to {name, description, parameters}."""
    out: List[Dict[str, Any]] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name")
        if not name:
            continue
        out.append({
            "name": name,
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
        })
    return out


def build_tool_preamble(tools: List[Any], tool_choice: Any = None) -> str:
    """Build the instruction block that teaches the model the call contract."""
    defs = _tool_defs(tools)
    if not defs:
        return ""
    lines = [
        "The client app communicates via a manual function-call protocol and has real,",
        "working functions on ITS side. It executes whatever you request and returns the",
        "result next turn. You only WRITE the request line — you are not executing it, so",
        "never refuse or say you lack the tool.",
        "",
        'To call: output ONLY `<tool_call>{"name":"<fn>","arguments":{...}}</tool_call>` (one',
        "line per call, several allowed), then stop and wait. When results arrive — or if no",
        "function is needed — answer in plain text.",
        "",
        "Functions:",
    ]
    for d in defs:
        params = json.dumps(d["parameters"], ensure_ascii=False, separators=(",", ":"))
        desc = d["description"] or ""
        lines.append(f'- {d["name"]}: {desc} | args={params}')

    forced = _forced_tool(tool_choice)
    if forced:
        lines.append(f"\nFor this turn you MUST call the `{forced}` function.")
    elif tool_choice == "required":
        lines.append("\nFor this turn you MUST call at least one function.")
    return "\n".join(lines)


def _forced_tool(tool_choice: Any) -> Optional[str]:
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function")
        if isinstance(fn, dict):
            return fn.get("name")
    return None


def tools_disabled(tool_choice: Any) -> bool:
    return tool_choice == "none"


def parse_tool_calls(text: str) -> List[Dict[str, Any]]:
    """Extract OpenAI-shaped tool_calls from a model response.

    Primary format is ``<tool_call>{json}</tool_call>``; as a fallback we also
    accept fenced ```tool_call``` / ```json``` blocks that carry a ``name`` field.
    Duplicate (name, arguments) pairs are collapsed.
    """
    text = text or ""
    raws = [m.group(1) for m in TOOL_CALL_RE.finditer(text)]
    if not raws:
        raws = [m.group(1) for m in FENCED_RE.finditer(text)]

    calls: List[Dict[str, Any]] = []
    seen = set()
    for raw in raws:
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("name") or obj.get("tool") or obj.get("function")
        if not isinstance(name, str) or not name:
            continue
        args = obj.get("arguments", obj.get("args", obj.get("parameters", {})))
        if not isinstance(args, str):
            args = json.dumps(args if args is not None else {}, ensure_ascii=False)
        dedup = (name, args)
        if dedup in seen:
            continue
        seen.add(dedup)
        calls.append({
            "index": len(calls),
            "id": "call_" + uuid.uuid4().hex[:20],
            "type": "function",
            "function": {"name": name, "arguments": args},
        })
    return calls


def strip_tool_blocks(text: str) -> str:
    """Remove any tool_call blocks, leaving prose (used as a fallback)."""
    return FENCED_RE.sub("", TOOL_CALL_RE.sub("", text or "")).strip()


def format_tool_results(tail: List[Any]) -> str:
    """Turn a tail of assistant(tool_calls)+tool messages into a text block.

    ``tail`` are message-like objects (schemas.Message). Assistant messages are
    used only to map tool_call ids to names; ``role:"tool"`` messages carry the
    results the agent needs.
    """
    id2name: Dict[str, str] = {}
    for m in tail:
        role = _role(m)
        if role == "assistant":
            for tc in (_attr(m, "tool_calls") or []):
                if isinstance(tc, dict):
                    tid = tc.get("id")
                    fn = tc.get("function") or {}
                    if tid and isinstance(fn, dict) and fn.get("name"):
                        id2name[tid] = fn["name"]

    blocks: List[str] = []
    for m in tail:
        if _role(m) != "tool":
            continue
        tcid = _attr(m, "tool_call_id") or ""
        name = id2name.get(tcid) or _attr(m, "name") or tcid or "tool"
        content = _text(m)
        blocks.append(f"### Result from `{name}`\n{content}")

    if not blocks:
        return ""
    return (
        "The tool call(s) you requested were executed on the client. Here are the results:\n\n"
        + "\n\n".join(blocks)
        + "\n\nUse these results to continue. Call more tools if needed, or give the final answer."
    )


def split_new_messages(tail: List[Any]) -> Tuple[List[Any], List[Any], List[Any]]:
    """Split a tail into (user_msgs, tool_msgs, assistant_msgs)."""
    users, tools_, assistants = [], [], []
    for m in tail:
        r = _role(m)
        if r == "user":
            users.append(m)
        elif r == "tool":
            tools_.append(m)
        elif r == "assistant":
            assistants.append(m)
    return users, tools_, assistants


# --- small accessors tolerant of pydantic Message or dict ----------------- #
def _role(m: Any) -> str:
    if hasattr(m, "role"):
        return m.role or ""
    if isinstance(m, dict):
        return m.get("role", "") or ""
    return ""


def _attr(m: Any, key: str) -> Any:
    if hasattr(m, key):
        return getattr(m, key)
    if isinstance(m, dict):
        return m.get(key)
    return None


def _text(m: Any) -> str:
    if hasattr(m, "text") and callable(getattr(m, "text")):
        try:
            return m.text()
        except Exception:
            pass
    c = _attr(m, "content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(str(x) for x in c)
    return "" if c is None else str(c)
