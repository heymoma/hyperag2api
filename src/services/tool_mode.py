"""The client-side tool-calling turn.

Hyperagent's agent only streams text — it neither accepts client tool schemas
nor speaks the OpenAI tool-call handshake. :mod:`src.services.tool_bridge`
defines a prompt contract that emulates it; this module runs one turn of that
contract end to end:

1. Prime the thread with the tool definitions (once per thread, re-sent only if
   the client's tool set changes).
2. Stream the answer *hybrid*: ordinary prose flows through token by token,
   and buffering starts only once the ``<tool_call>`` sentinel appears — so a
   partial sentinel split across tokens is never leaked as content.
3. Re-expose whatever was buffered as OpenAI ``tool_calls`` with
   ``finish_reason: "tool_calls"``.

Kept apart from the plain path deliberately: the two differ in session keying,
in what they send upstream, and in how they treat text, and interleaving them
was the main source of complexity in the original implementation.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

from src.core import config
from src.core.interfaces import ChatBackend, CookieProvider
from src.core.logging_config import get_logger
from src.core.schemas import ChatCompletionRequest
from src.core.session_store import SessionStore
from src.core.sse import DONE, format_openai_chunk, sse_comment
from src.services import conversation, stream_events as events, tool_bridge
from src.services.attachments import AttachmentUploader
from src.services.streaming import KEEPALIVE, iter_with_keepalive
from src.services.threads import ThreadFactory, fire_interrupt

logger = get_logger("tools")


class ToolModeExecutor:
    """Runs one client-tool-calling completion against a Hyperagent thread."""

    def __init__(
        self,
        cookie_provider: CookieProvider,
        backend: ChatBackend,
        session_store: SessionStore,
        threads: ThreadFactory,
        uploader: AttachmentUploader,
    ) -> None:
        self.cookie_provider = cookie_provider
        self.backend = backend
        self.session_store = session_store
        self.threads = threads
        self.uploader = uploader
        # thread_id -> tool-set signature already delivered in this process.
        # A changed signature (client added/removed MCP tools) re-sends the
        # full preamble; an unchanged one sends nothing extra, because
        # re-injecting a reminder every turn risks the model echoing it back.
        self._primed: Dict[str, str] = {}

    # ------------------------------------------------------------------ #
    def _continuation_text(self, tail: List[Any], preamble: str) -> str:
        """Compose what to send for a turn that carries tool results."""
        users = tool_bridge.user_messages(tail)
        result_block = tool_bridge.format_tool_results(tail)
        user_text = "\n\n".join(
            text for text in (conversation.role_and_text(u)[1] for u in users) if text
        )

        if result_block and user_text:
            content = result_block + "\n\n" + user_text
        else:
            content = result_block or user_text

        # A NEW user message may arrive on a thread that predates the tools, so
        # restate the contract — it also keeps the model from refusing.
        if user_text and preamble:
            content = preamble + "\n\n---\n\n" + content
        return content.strip()

    async def _prepare_turn(
        self,
        req: ChatCompletionRequest,
        session: conversation.ResolvedSession,
        cookies: Dict[str, str],
        preamble: str,
        tools_sig: str,
    ) -> "tuple[str, str, Dict[str, str], bool]":
        """Resolve the thread and the text to send. Returns
        (thread_id, content, cookies, reused)."""
        record = await self.session_store.get_record(session.lookup_key)
        if record:
            thread_id = str(record["thread_id"])
            count = int(record.get("message_count") or 0)
            primed = self._primed.get(thread_id) == tools_sig
            tail = req.messages[count:] if count < len(req.messages) else []
            content = self._continuation_text(tail, "" if primed else preamble)
            content = content or conversation.latest_text(req.messages)
            logger.info(
                "Tool-mode: reusing thread %s (count=%d, primed=%s)", thread_id, count, primed
            )
            return thread_id, content, cookies, True

        thread_id, cookies = await self.threads.create(
            session.model, session.system_prompt, session.explicit_id, cookies
        )
        await self.backend.warm_thread(thread_id, cookies)
        _, combined = conversation.split_prompts(req.messages)
        user_text = conversation.latest_text(req.messages) or combined
        # Deliver the tool contract in the USER turn — the platform's own agent
        # system prompt otherwise overrides an injected systemPrompt.
        content = f"{preamble}\n\n---\n\nUser request:\n{user_text}" if preamble else user_text
        logger.info("Tool-mode: created thread %s", thread_id)
        return thread_id, content, cookies, False

    # ------------------------------------------------------------------ #
    async def execute(
        self,
        req: ChatCompletionRequest,
        session_id: Optional[str],
        meta: Dict[str, Any],
    ) -> AsyncGenerator[str, None]:
        chat_id = f"chatcmpl-{uuid.uuid4()}"
        model = req.model
        session = conversation.resolve_session(
            req.model, req.messages, session_id=session_id, user=req.user, tool_mode=True
        )
        meta["session_key"] = session.lookup_key

        try:
            cookies = await self.cookie_provider.get_cookies()
        except Exception as exc:
            logger.error("Failed to retrieve browser cookies: %s", exc)
            meta["error"] = str(exc)
            for chunk in _cookie_error_chunks(chat_id, model, exc):
                yield chunk
            return

        yield format_openai_chunk(chat_id, model, "", role="assistant")

        preamble = tool_bridge.build_tool_preamble(req.tools, req.tool_choice)
        tools_sig = tool_bridge.tools_signature(req.tools)
        # Server-side tools/MCP are forced off during tool turns so the server
        # agent delegates everything to the client instead of acting on its own.
        flags = config.server_tools_off_flags() if config.DISABLE_SERVER_MCP else None

        thread_id: Optional[str] = None
        finish_reason = "stop"
        streamed: List[str] = []
        try:
            thread_id, content, cookies, reused = await self._prepare_turn(
                req, session, cookies, preamble, tools_sig
            )
            self._primed[thread_id] = tools_sig
            meta["thread_id"] = thread_id
            meta["reused_thread"] = reused

            attachments = await self.uploader.collect(req.messages, thread_id, cookies)
            source = self.backend.stream_chat(
                thread_id, content, cookies, session_id=session.explicit_id,
                attachments=attachments, feature_flags=flags,
            )

            buffer = _SentinelBuffer()
            async for item in iter_with_keepalive(source, config.KEEPALIVE_INTERVAL):
                if item is KEEPALIVE:
                    yield sse_comment("keepalive")
                    continue

                data_type = events.event_type(item)
                if events.is_ignorable(data_type):
                    continue
                if events.is_terminal(data_type):
                    break

                if events.is_reasoning(data_type):
                    reasoning = events.event_text(item)
                    if reasoning:
                        yield format_openai_chunk(chat_id, model, "", reasoning=reasoning)
                    continue

                if not events.is_text(data_type, item):
                    continue
                token = events.event_text(item)
                if not token:
                    continue

                emit = buffer.feed(token)
                if emit:
                    streamed.append(emit)
                    yield format_openai_chunk(chat_id, model, emit)

            tail = buffer.flush()
            if tail:
                streamed.append(tail)
                yield format_openai_chunk(chat_id, model, tail)

            await self.session_store.put(
                session.lookup_key, thread_id, session.model, len(req.messages)
            )

            calls = tool_bridge.parse_tool_calls(buffer.tool_region)
            if calls:
                for call in calls:
                    yield format_openai_chunk(chat_id, model, "", tool_calls=[call])
                finish_reason = "tool_calls"
                logger.info("Tool-mode: emitted %d tool_call(s)", len(calls))
            elif buffer.tool_region:
                # Sentinel opened but nothing parseable — surface the raw text
                # so nothing is silently dropped.
                streamed.append(buffer.tool_region)
                yield format_openai_chunk(chat_id, model, buffer.tool_region)

            meta["completion_text"] = "".join(streamed)

        except asyncio.CancelledError:
            logger.info("Client disconnected (tool mode); interrupting thread %s", thread_id)
            fire_interrupt(self.backend, thread_id, cookies)
            raise
        except Exception as exc:
            logger.error("Tool-mode stream error: %s", exc)
            meta["error"] = str(exc)
            yield format_openai_chunk(chat_id, model, f"\n[Stream Error: {exc}]")

        meta["finish_reason"] = finish_reason
        yield format_openai_chunk(chat_id, model, "", finish_reason=finish_reason)
        yield DONE


class _SentinelBuffer:
    """Splits a token stream into streamable prose and a buffered tool region.

    Before the ``<tool_call>`` sentinel everything is passed through, except a
    trailing suffix that could still turn into the sentinel once more tokens
    arrive. After it, every token is captured for parsing.
    """

    def __init__(self, sentinel: str = tool_bridge.TOOL_CALL_SENTINEL) -> None:
        self.sentinel = sentinel
        self.tool_region = ""
        self._pending = ""
        self._in_tool = False

    def feed(self, token: str) -> str:
        """Consume a token; return the text safe to stream right now."""
        if self._in_tool:
            self.tool_region += token
            return ""

        self._pending += token
        index = self._pending.find(self.sentinel)
        if index != -1:
            before = self._pending[:index]
            self._in_tool = True
            self.tool_region = self._pending[index:]
            self._pending = ""
            return before

        holdback = tool_bridge.sentinel_holdback(self._pending, self.sentinel)
        split = len(self._pending) - holdback
        emit, self._pending = self._pending[:split], self._pending[split:]
        return emit

    def flush(self) -> str:
        """Any held-back text that turned out not to be a sentinel."""
        if self._in_tool:
            return ""
        emit, self._pending = self._pending, ""
        return emit


def _cookie_error_chunks(chat_id: str, model: str, exc: Exception) -> List[str]:
    """A complete, well-formed SSE response reporting a session failure."""
    return [
        format_openai_chunk(chat_id, model, "", role="assistant"),
        format_openai_chunk(chat_id, model, f"\n[Cookie Error: {exc}]"),
        format_openai_chunk(chat_id, model, "", finish_reason="stop"),
        DONE,
    ]
