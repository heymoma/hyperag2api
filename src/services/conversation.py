"""Mapping stateless OpenAI message arrays onto stateful Hyperagent threads.

``/v1/chat/completions`` is stateless — the client resends the entire history
every turn. Hyperagent is thread-stateful. Bridging the two comes down to two
questions, both answered here:

* **Which thread does this request belong to?** Either an explicit id (session
  header, ``user`` field, ``model@suffix``) or a hash of the conversation
  *prefix* — every message except the newest. The next turn presents that same
  prefix plus the reply we streamed, so hashing it forward recognises the
  continuation.
* **What text do we actually send?** Just the newest message when the thread
  already holds the context; the flattened history when we had to start fresh.

Pure functions only — no I/O, no config mutation — so the rules are testable in
isolation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from src.core.config import resolve_model

# Hyperagent's default persona assumes a chatbot sandbox and refuses to reason
# about code it "cannot access". Coding clients routinely send no system prompt
# at all, so we supply one that frames the proxy correctly.
DEFAULT_PROXY_SYSTEM_PROMPT = (
    "You are an expert AI coding assistant connected to the user's IDE via an OpenAI-compatible proxy API. "
    "Analyze, debug, and assist with the user's code directly. "
    "Do not state that you lack local workspace access or ask the user to upload zip files or GitHub links."
)

# ``developer`` is the modern spelling of ``system`` (OpenAI o1/o3, Cursor,
# OpenCode). Both must land in the system prompt, never in the dialog.
SYSTEM_ROLES = ("system", "developer")


# --------------------------------------------------------------------------- #
# Message accessors                                                            #
# --------------------------------------------------------------------------- #
def role_and_text(msg: Any) -> Tuple[str, str]:
    """Normalise a message (pydantic ``Message`` or plain dict) to (role, text)."""
    text = getattr(msg, "text", None)
    if callable(text):
        return msg.role, msg.text()
    if hasattr(msg, "role"):
        content = msg.content
        if isinstance(content, list):
            content = " ".join(str(x) for x in content)
        return msg.role, content or ""
    if isinstance(msg, dict):
        return msg.get("role", "user"), msg.get("content", "") or ""
    return "user", ""


def split_prompts(messages: List[Any]) -> Tuple[str, str]:
    """Split messages into (system_prompt, flattened_dialog).

    Falls back to :data:`DEFAULT_PROXY_SYSTEM_PROMPT` when the client sent no
    system prompt, and to the last message when there is no dialog at all.
    """
    system_parts: List[str] = []
    dialog_parts: List[str] = []
    for msg in messages:
        role, content = role_and_text(msg)
        if role in SYSTEM_ROLES:
            system_parts.append(content)
        else:
            dialog_parts.append(f"{role.capitalize()}: {content}\n")

    combined = "\n".join(dialog_parts)
    if not combined and messages:
        combined = role_and_text(messages[-1])[1]

    system = "\n".join(system_parts)
    if not system.strip():
        system = DEFAULT_PROXY_SYSTEM_PROMPT
    return system.strip(), combined.strip()


def dialog_pairs(messages: List[Any]) -> List[Tuple[str, str]]:
    """The (role, text) pairs that make up the visible conversation."""
    pairs = []
    for msg in messages:
        role, content = role_and_text(msg)
        if role not in SYSTEM_ROLES:
            pairs.append((role, content))
    return pairs


def latest_text(messages: List[Any]) -> str:
    """Text of the newest message, or ``""`` when there is none."""
    return role_and_text(messages[-1])[1] if messages else ""


def first_user_text(messages: List[Any]) -> str:
    for msg in messages:
        role, content = role_and_text(msg)
        if role == "user":
            return content
    return ""


# --------------------------------------------------------------------------- #
# Session keys                                                                 #
# --------------------------------------------------------------------------- #
def _digest(system: str, model: str, pairs: List[Tuple[str, str]]) -> str:
    h = hashlib.sha256()
    h.update(model.encode("utf-8", "ignore"))
    h.update(b"\x00")
    h.update(system.encode("utf-8", "ignore"))
    for role, content in pairs:
        h.update(b"\x01")
        h.update(role.encode("utf-8", "ignore"))
        h.update(b"\x02")
        h.update(content.encode("utf-8", "ignore"))
    return h.hexdigest()[:40]


def explicit_key(session_id: str) -> str:
    """Key for a client-pinned session (header / ``user`` / ``model@suffix``)."""
    return f"sid:{session_id}"


def history_key(system: str, model: str, pairs: List[Tuple[str, str]]) -> str:
    """Key derived from a conversation prefix — matches the next turn's request."""
    return "px:" + _digest(system, model, pairs)


def conversation_key(system: str, model: str, messages: List[Any]) -> str:
    """Stable key for a tool-calling conversation.

    Tool turns rewrite the tail of the history on every round-trip, so hashing
    the prefix would never match. The opening user message is the one part that
    stays put.
    """
    return "cv:" + _digest(system, model, [("user", first_user_text(messages))])


# --------------------------------------------------------------------------- #
# Request resolution                                                           #
# --------------------------------------------------------------------------- #
@dataclass
class ResolvedSession:
    """Everything the orchestrator needs to route one request to a thread."""

    model: str
    """The Hyperagent model id (already mapped from the OpenAI-style name)."""

    explicit_id: Optional[str]
    """Client-pinned session id, if any."""

    lookup_key: str
    """Store key to look up an existing thread with."""

    system_prompt: str
    dialog: List[Tuple[str, str]]

    @property
    def is_first_turn(self) -> bool:
        return len(self.dialog) <= 1

    def forward_key(self, completion_text: str) -> str:
        """Key the *next* turn of this conversation will present.

        That is this turn's dialog plus the assistant reply we just streamed —
        exactly what the client echoes back.
        """
        return history_key(
            self.system_prompt, self.model, self.dialog + [("assistant", completion_text)]
        )


def resolve_session(
    model: str,
    messages: List[Any],
    session_id: Optional[str] = None,
    user: Optional[str] = None,
    tool_mode: bool = False,
) -> ResolvedSession:
    """Work out which thread a request belongs to."""
    mapped_model, suffix_session = resolve_model(model)
    explicit = session_id or suffix_session or (user or None)
    system_prompt, _ = split_prompts(messages)
    pairs = dialog_pairs(messages)

    if explicit:
        lookup_key = explicit_key(explicit)
    elif tool_mode:
        lookup_key = conversation_key(system_prompt, mapped_model, messages)
    else:
        # Hash the prefix — everything but the newest message.
        lookup_key = history_key(system_prompt, mapped_model, pairs[:-1] if pairs else [])

    return ResolvedSession(
        model=mapped_model,
        explicit_id=explicit,
        lookup_key=lookup_key,
        system_prompt=system_prompt,
        dialog=pairs,
    )
