"""Chat orchestration: bridges stateless OpenAI requests to Hyperagent threads.

Key behaviours added over the original:
- One Hyperagent thread per logical conversation (session), reused across turns,
  sending only the newest message instead of re-flattening the whole history.
- Reasoning/thinking surfaced as ``reasoning_content`` and/or inline ``<think>``
  tags, configurable via ``REASONING_STYLE``.
- Backend tool activity optionally surfaced as OpenAI ``tool_calls`` deltas.
- Multimodal image parts uploaded and attached (best-effort).
- SSE keepalive heartbeat during long silences; graceful cancel on disconnect.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
import uuid
import json
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx

from src.core.interfaces import CookieProvider, ChatBackend, AuthError
from src.core import config
from src.core.config import MODEL_MAPPING, resolve_model
from src.core.models import format_openai_chunk, format_openai_keepalive_chunk, sse_comment, build_usage, DONE
from src.core.session_store import SessionStore
from src.core.logging_config import get_logger
from src.services import tool_bridge
from src.adapters.api.schemas import ChatCompletionRequest, ChatCompletionResponse, Choice, Message

logger = get_logger("chat")

_REASONING_TYPES = {"thinking", "reasoning", "redacted_thinking", "reasoning_content"}
_TEXT_TYPES = {"text", "content", "message", "answer"}
# Backend lifecycle/status frames (verified live) that carry no assistant output.
_IGNORE_TYPES = {
    "thread_runtime_latched", "sandbox_status", "session_start", "session_end",
    "thread_status", "ping", "heartbeat", "keepalive",
}


class ChatService:
    """Orchestrates the chat completion use case (business logic layer)."""

    def __init__(
        self,
        cookie_provider: CookieProvider,
        chat_backend: ChatBackend,
        session_store: Optional[SessionStore] = None,
    ):
        self.cookie_provider = cookie_provider
        self.chat_backend = chat_backend
        self.session_store = session_store or SessionStore.from_config()
        # thread_id -> tools signature already delivered this process. Later turns
        # send only a one-line reminder (token optimization); a changed signature
        # (client added/removed MCP tools) re-sends the full preamble.
        self._tools_primed: Dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # Model listing                                                       #
    # ------------------------------------------------------------------ #
    def get_available_models(self) -> List[Dict[str, str]]:
        return [{"id": k, "object": "model", "owned_by": "hyperagent"} for k in MODEL_MAPPING.keys()]

    # ------------------------------------------------------------------ #
    # Account rotation                                                    #
    # ------------------------------------------------------------------ #
    def _session_count(self) -> int:
        import inspect
        fn = getattr(self.cookie_provider, "count", None)
        if callable(fn) and not inspect.iscoroutinefunction(fn):
            try:
                return max(1, int(fn()))
            except Exception:
                return 1
        return 1

    async def _create_thread_rotating(self, model, system_prompt, explicit, cookies):
        """Create a thread; on an auth failure rotate to the next configured
        account and retry. Returns (thread_id, cookies_used)."""
        attempts = self._session_count()
        last: Optional[Exception] = None
        for i in range(attempts):
            try:
                tid = await self.chat_backend.create_thread(model, system_prompt, cookies, session_id=explicit)
                return tid, cookies
            except AuthError as exc:
                last = exc
                if i < attempts - 1:
                    logger.warning("Session auth failed; rotating account (%d/%d).", i + 1, attempts)
                    self.cookie_provider.invalidate()
                    cookies = await self.cookie_provider.get_cookies()
                else:
                    raise
        raise last if last else RuntimeError("thread creation failed")

    # ------------------------------------------------------------------ #
    # Prompt helpers                                                      #
    # ------------------------------------------------------------------ #
    def _msg_role_content(self, msg: Any) -> Tuple[str, str]:
        if isinstance(msg, Message):
            return msg.role, msg.text()
        if hasattr(msg, "role"):
            content = msg.content
            if isinstance(content, list):
                content = " ".join(str(x) for x in content)
            return msg.role, content or ""
        if isinstance(msg, dict):
            return msg.get("role", "user"), msg.get("content", "") or ""
        return "user", ""

    def _prepare_prompts(self, messages: List[Any]) -> Tuple[str, str]:
        """Extract system prompt and format the full user/assistant history."""
        system_prompt = ""
        combined_prompt = ""
        for msg in messages:
            role, content = self._msg_role_content(msg)
            if role == "system":
                system_prompt += f"{content}\n"
            else:
                combined_prompt += f"{role.capitalize()}: {content}\n\n"
        if not combined_prompt and messages:
            _, last = self._msg_role_content(messages[-1])
            combined_prompt = last
        return system_prompt.strip(), combined_prompt.strip()

    def _dialog_pairs(self, messages: List[Any]) -> List[Tuple[str, str]]:
        pairs = []
        for msg in messages:
            role, content = self._msg_role_content(msg)
            if role != "system":
                pairs.append((role, content))
        return pairs

    def _latest_user_text(self, messages: List[Any]) -> str:
        if not messages:
            return ""
        _, content = self._msg_role_content(messages[-1])
        return content

    @staticmethod
    def _hash_key(system: str, model: str, pairs: List[Tuple[str, str]]) -> str:
        h = hashlib.sha256()
        h.update(model.encode("utf-8", "ignore"))
        h.update(b"\x00")
        h.update(system.encode("utf-8", "ignore"))
        for role, content in pairs:
            h.update(b"\x01")
            h.update(role.encode("utf-8", "ignore"))
            h.update(b"\x02")
            h.update(content.encode("utf-8", "ignore"))
        return "px:" + h.hexdigest()[:40]

    def _resolve_session(
        self, req: ChatCompletionRequest, explicit_session_id: Optional[str]
    ) -> Tuple[str, Optional[str], str, str, List[Tuple[str, str]]]:
        """Return (mapped_model, explicit_id, lookup_key, system_prompt, pairs)."""
        mapped_model, suffix_session = resolve_model(req.model)
        explicit = explicit_session_id or suffix_session or (req.user or None)

        system_prompt, _ = self._prepare_prompts(req.messages)
        pairs = self._dialog_pairs(req.messages)
        prefix_pairs = pairs[:-1] if pairs else []

        if explicit:
            lookup_key = f"sid:{explicit}"
        else:
            lookup_key = self._hash_key(system_prompt, mapped_model, prefix_pairs)

        return mapped_model, explicit, lookup_key, system_prompt, pairs

    # ------------------------------------------------------------------ #
    # Attachments (multimodal)                                            #
    # ------------------------------------------------------------------ #
    async def _fetch_image_bytes(self, ref: str) -> Optional[Tuple[bytes, str, str]]:
        try:
            if ref.startswith("data:"):
                header, _, b64 = ref.partition(",")
                mime = "image/png"
                if ";" in header and ":" in header:
                    mime = header[header.index(":") + 1 : header.index(";")] or mime
                ext = (mime.split("/")[-1] or "png").split("+")[0]
                return base64.b64decode(b64), mime, f"image.{ext}"
            if ref.startswith("http://") or ref.startswith("https://"):
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(ref)
                    if resp.status_code == 200:
                        mime = resp.headers.get("content-type", "image/png").split(";")[0]
                        ext = (mime.split("/")[-1] or "png").split("+")[0]
                        return resp.content, mime, f"image.{ext}"
        except Exception as exc:
            logger.debug("Failed to load image %s: %s", ref[:60], exc)
        return None

    async def _collect_attachments(
        self, req: ChatCompletionRequest, thread_id: str, cookies: Dict[str, str]
    ) -> List[Dict[str, Any]]:
        if not config.ENABLE_MULTIMODAL or not req.messages:
            return []
        last = req.messages[-1]
        image_refs = last.image_urls() if isinstance(last, Message) else []
        if not image_refs:
            return []
        attachments: List[Dict[str, Any]] = []
        for ref in image_refs[:8]:
            loaded = await self._fetch_image_bytes(ref)
            if not loaded:
                continue
            data, mime, filename = loaded
            try:
                descriptor = await self.chat_backend.upload_file(thread_id, cookies, filename, data, mime)
            except Exception as exc:
                logger.debug("upload_file raised (non-fatal): %s", exc)
                descriptor = None
            if descriptor:
                attachments.append(descriptor)
        if attachments:
            logger.info("Attached %d image(s) to thread %s", len(attachments), thread_id)
        return attachments

    # ------------------------------------------------------------------ #
    # Tool-call rendering                                                 #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_event_text(data: Dict[str, Any]) -> str:
        for key in ("content", "text", "thinking", "delta"):
            val = data.get(key)
            if isinstance(val, str) and val:
                return val
        return ""

    @staticmethod
    def _is_reasoning(data_type: str, data: Dict[str, Any]) -> bool:
        if data_type in _REASONING_TYPES:
            return True
        return data_type.replace("-", "_").startswith("reasoning") or data_type.replace("-", "_").startswith("thinking")

    @staticmethod
    def _is_tool_event(data_type: str, data: Dict[str, Any]) -> bool:
        if "toolName" in data or "tool_name" in data:
            return True
        return data_type in ("tool_input_ready", "tool_call", "tool-call", "tool_use", "tool")

    def _render_askquestion(self, tool_input: Dict[str, Any]) -> str:
        questions = tool_input.get("questions", []) if isinstance(tool_input, dict) else []
        steering_text = "\n\n### 📋 Action Required (Steering Options):\n"
        for q in questions:
            steering_text += f"**Question:** {q.get('question')}\n"
            for idx, opt in enumerate(q.get("options", []) or []):
                steering_text += f"- **Option {idx+1}:** {opt.get('label')} (Value: `{opt.get('value')}`)\n"
        return steering_text

    # ------------------------------------------------------------------ #
    # Streaming                                                           #
    # ------------------------------------------------------------------ #
    async def execute_chat_stream(
        self,
        req: ChatCompletionRequest,
        session_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[str, None]:
        """Orchestrate the streaming chat completion, yielding SSE chunks."""
        meta = meta if meta is not None else {}

        # Client-side tool calling (function calling / MCP bridge) is a separate,
        # self-contained path — only engaged when the request carries tools.
        if req.tools and not tool_bridge.tools_disabled(req.tool_choice):
            async for out in self._execute_tool_mode(req, session_id, meta):
                yield out
            return

        chat_id = f"chatcmpl-{uuid.uuid4()}"
        model = req.model

        mapped_model, explicit, lookup_key, system_prompt_key, pairs = self._resolve_session(req, session_id)
        meta["session_key"] = lookup_key

        # 1. Cookies (with one re-auth retry on failure).
        try:
            cookies = await self.cookie_provider.get_cookies()
        except Exception as e:
            logger.error("Failed to retrieve browser cookies: %s", e)
            meta["error"] = str(e)
            yield format_openai_chunk(chat_id, model, "", role="assistant")
            yield format_openai_chunk(chat_id, model, f"\n[Cookie Error: {e}]")
            yield format_openai_chunk(chat_id, model, "", finish_reason="stop")
            yield DONE
            return

        # 2. Initial role block.
        yield format_openai_chunk(chat_id, model, "", role="assistant")

        thread_id: Optional[str] = None
        reused = False
        try:
            # 3. Resolve or create the thread for this session.
            thread_id = await self.session_store.get(lookup_key)
            if thread_id:
                reused = True
                content_to_send = self._latest_user_text(req.messages)
                logger.info("Reusing thread %s for session %s", thread_id, lookup_key)
            else:
                system_prompt, combined_prompt = self._prepare_prompts(req.messages)
                pairs = self._dialog_pairs(req.messages)
                first_turn = len(pairs) <= 1
                thread_id, cookies = await self._create_thread_rotating(
                    mapped_model, system_prompt, explicit, cookies
                )
                await self.chat_backend.warm_thread(thread_id, cookies)
                # First turn (or explicit session with no history) → send just the
                # newest message; otherwise replay full history so context is kept.
                content_to_send = self._latest_user_text(req.messages) if first_turn else combined_prompt
                logger.info("Created thread %s for session %s (first_turn=%s)", thread_id, lookup_key, first_turn)

            meta["thread_id"] = thread_id
            meta["reused_thread"] = reused

            # 4. Attachments (multimodal, best-effort).
            attachments = await self._collect_attachments(req, thread_id, cookies)

            # 5. Stream, with keepalive + reasoning/tool rendering.
            completion_parts: List[str] = []
            async for out in self._stream_and_render(
                chat_id, model, thread_id, content_to_send, cookies, explicit, attachments, completion_parts
            ):
                yield out

            # 6. Persist session mapping for the next turn. The forward key is the
            # prefix hash the *next* request will present: this turn's full dialog
            # plus the assistant reply we just streamed (what the client echoes back).
            completion_text = "".join(completion_parts)
            forward_pairs = pairs + [("assistant", completion_text)]
            forward_key = self._hash_key(system_prompt_key, mapped_model, forward_pairs)
            await self._store_session(explicit, forward_key, thread_id, mapped_model, len(req.messages))
            meta["completion_text"] = completion_text

        except asyncio.CancelledError:
            logger.info("Client disconnected; interrupting thread %s", thread_id)
            self._fire_interrupt(thread_id, cookies)
            raise
        except Exception as e:
            logger.error("Stream error in execute_chat_stream: %s", e)
            meta["error"] = str(e)
            yield format_openai_chunk(chat_id, model, f"\n[Stream Error: {e}]")

        yield format_openai_chunk(chat_id, model, "", finish_reason="stop")
        yield DONE

    async def _stream_and_render(
        self,
        chat_id: str,
        model: str,
        thread_id: str,
        content_to_send: str,
        cookies: Dict[str, str],
        explicit: Optional[str],
        attachments: List[Dict[str, Any]],
        completion_parts: List[str],
    ) -> AsyncGenerator[str, None]:
        style = config.REASONING_STYLE
        in_think = False
        tool_index = 0

        source = self.chat_backend.stream_chat(
            thread_id, content_to_send, cookies, session_id=explicit, attachments=attachments
        )
        aiter = source.__aiter__()
        keepalive = config.KEEPALIVE_INTERVAL

        while True:
            try:
                if keepalive and keepalive > 0:
                    try:
                        data = await asyncio.wait_for(aiter.__anext__(), timeout=keepalive)
                    except asyncio.TimeoutError:
                        yield sse_comment("keepalive")
                        yield format_openai_keepalive_chunk(chat_id, model)
                        continue
                else:
                    data = await aiter.__anext__()
            except StopAsyncIteration:
                break

            if not isinstance(data, dict):
                continue
            data_type = str(data.get("type", ""))

            # --- backend lifecycle noise / terminal ---
            if data_type in _IGNORE_TYPES:
                continue
            if data_type == "done":
                break

            # --- reasoning / thinking ---
            if self._is_reasoning(data_type, data):
                text = self._extract_event_text(data)
                if not text:
                    continue
                if style in ("think_tags", "both") and not in_think:
                    in_think = True
                    completion_parts.append("<think>\n")
                    yield format_openai_chunk(chat_id, model, "<think>\n")
                if style in ("reasoning_content", "both"):
                    yield format_openai_chunk(chat_id, model, "", reasoning=text)
                if style in ("think_tags", "both"):
                    completion_parts.append(text)
                    yield format_openai_chunk(chat_id, model, text)
                continue

            # --- AskQuestion steering (always rendered as readable content) ---
            if self._is_tool_event(data_type, data) and data.get("toolName") == "mcp__t__AskQuestion":
                if in_think:
                    in_think = False
                    completion_parts.append("\n</think>\n\n")
                    yield format_openai_chunk(chat_id, model, "\n</think>\n\n")
                steering = self._render_askquestion(data.get("toolInput", {}))
                completion_parts.append(steering)
                yield format_openai_chunk(chat_id, model, steering)
                continue

            # --- other backend tool activity ---
            if self._is_tool_event(data_type, data):
                if config.TOOLCALL_MODE == "off":
                    continue
                tool_name = data.get("toolName") or data.get("tool_name") or "tool"
                tool_input = data.get("toolInput") or data.get("tool_input") or {}
                if config.TOOLCALL_MODE == "openai":
                    tc = [{
                        "index": tool_index,
                        "id": f"call_{tool_index}_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {"name": str(tool_name), "arguments": json.dumps(tool_input, ensure_ascii=False)},
                    }]
                    tool_index += 1
                    yield format_openai_chunk(chat_id, model, "", tool_calls=tc)
                else:  # content
                    note = f"\n\n> 🔧 `{tool_name}`\n"
                    completion_parts.append(note)
                    yield format_openai_chunk(chat_id, model, note)
                continue

            # --- normal text ---
            if data_type in _TEXT_TYPES or ("content" in data and not data_type):
                text = self._extract_event_text(data)
                if not text:
                    continue
                if in_think:
                    in_think = False
                    completion_parts.append("\n</think>\n\n")
                    yield format_openai_chunk(chat_id, model, "\n</think>\n\n")
                completion_parts.append(text)
                yield format_openai_chunk(chat_id, model, text)
                continue

        if in_think:
            completion_parts.append("\n</think>\n\n")
            yield format_openai_chunk(chat_id, model, "\n</think>\n\n")

    async def _store_session(
        self,
        explicit: Optional[str],
        forward_key: str,
        thread_id: str,
        model: str,
        message_count: int,
    ) -> None:
        try:
            if explicit:
                await self.session_store.put(f"sid:{explicit}", thread_id, model, message_count)
            # Always store the forward prefix key so the next turn matches by history.
            await self.session_store.put(forward_key, thread_id, model, message_count)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Failed to persist session mapping: %s", exc)

    def _fire_interrupt(self, thread_id: Optional[str], cookies: Dict[str, str]) -> None:
        if not thread_id:
            return
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(self.chat_backend.interrupt(thread_id, cookies))
        except Exception:  # pragma: no cover - best-effort
            pass

    # ------------------------------------------------------------------ #
    # Client-side tool calling (OpenAI function-calling bridge)            #
    # ------------------------------------------------------------------ #
    def _conv_key(self, system_prompt: str, model: str, messages: List[Any]) -> str:
        """Stable key for a tool conversation: system + model + first user msg."""
        first_user = ""
        for m in messages:
            role, content = self._msg_role_content(m)
            if role == "user":
                first_user = content
                break
        h = self._hash_key(system_prompt, model, [("user", first_user)])
        return "cv:" + h.split(":", 1)[-1]

    def _build_tool_delta(self, tail: List[Any], preamble: str) -> str:
        """Compose the text to send for a tool continuation turn."""
        users, tool_msgs, _ = tool_bridge.split_new_messages(tail)
        result_block = tool_bridge.format_tool_results(tail)
        user_text = "\n\n".join(
            self._msg_role_content(u)[1] for u in users if self._msg_role_content(u)[1]
        )
        if result_block and user_text:
            content = result_block + "\n\n" + user_text
        elif result_block:
            content = result_block
        else:
            content = user_text
        # If this turn introduces a NEW user message, restate the tool contract
        # (the thread may predate tools, and it keeps the model from refusing).
        if user_text and preamble:
            content = preamble + "\n\n---\n\n" + content
        return content.strip()

    async def _execute_tool_mode(
        self, req: ChatCompletionRequest, session_id: Optional[str], meta: Dict[str, Any]
    ) -> AsyncGenerator[str, None]:
        chat_id = f"chatcmpl-{uuid.uuid4()}"
        model = req.model
        mapped_model, suffix_session = resolve_model(req.model)
        explicit = session_id or suffix_session or (req.user or None)
        system_prompt, combined_prompt = self._prepare_prompts(req.messages)
        key = f"sid:{explicit}" if explicit else self._conv_key(system_prompt, mapped_model, req.messages)
        meta["session_key"] = key

        try:
            cookies = await self.cookie_provider.get_cookies()
        except Exception as e:
            logger.error("Failed to retrieve browser cookies: %s", e)
            meta["error"] = str(e)
            yield format_openai_chunk(chat_id, model, "", role="assistant")
            yield format_openai_chunk(chat_id, model, f"\n[Cookie Error: {e}]")
            yield format_openai_chunk(chat_id, model, "", finish_reason="stop")
            yield DONE
            return

        yield format_openai_chunk(chat_id, model, "", role="assistant")
        full_preamble = tool_bridge.build_tool_preamble(req.tools, req.tool_choice)
        tools_sig = tool_bridge.tools_signature(req.tools)
        # Server-side tools/MCP are forced off during tool turns so the server
        # agent delegates everything to the client instead of acting on its own.
        tool_flags = config.server_tools_off_flags() if config.DISABLE_SERVER_MCP else None
        thread_id: Optional[str] = None
        finish_reason = "stop"
        try:
            rec = await self.session_store.get_record(key)
            if rec:
                thread_id = rec["thread_id"]
                count = int(rec.get("message_count") or 0)
                reused = True
                # Re-send full schemas only if not primed OR the tool set changed
                # (client added/removed MCP). Once primed, inject NOTHING extra —
                # the thread already holds the contract, and re-injecting a reminder
                # every turn risks the model echoing it back to the user.
                primed = self._tools_primed.get(thread_id) == tools_sig
                pre = "" if primed else full_preamble
                tail = req.messages[count:] if count < len(req.messages) else []
                content = self._build_tool_delta(tail, pre) or self._latest_user_text(req.messages)
                logger.info("Tool-mode: reusing thread %s (count=%d, primed=%s)", thread_id, count, primed)
            else:
                thread_id, cookies = await self._create_thread_rotating(
                    mapped_model, system_prompt, explicit, cookies
                )
                await self.chat_backend.warm_thread(thread_id, cookies)
                reused = False
                user_text = self._latest_user_text(req.messages) or combined_prompt
                # Deliver the tool contract in the USER turn — the platform's own
                # agent system prompt otherwise overrides an injected systemPrompt.
                content = (full_preamble + "\n\n---\n\nUser request:\n" + user_text) if full_preamble else user_text
                logger.info("Tool-mode: created thread %s", thread_id)

            if thread_id:
                self._tools_primed[thread_id] = tools_sig
            meta["thread_id"] = thread_id
            meta["reused_thread"] = reused
            attachments = await self._collect_attachments(req, thread_id, cookies)

            # Hybrid streaming: stream ordinary text token-by-token, and only switch
            # to buffering once the <tool_call> sentinel appears. A partial sentinel
            # at a token boundary is held back so it is never leaked as content.
            sentinel = tool_bridge.TOOL_CALL_SENTINEL
            pending = ""
            streamed: List[str] = []
            tool_region = ""
            in_tool = False
            _src = self.chat_backend.stream_chat(
                thread_id, content, cookies, session_id=explicit,
                attachments=attachments, feature_flags=tool_flags,
            )
            _ait = _src.__aiter__()
            _ka = config.KEEPALIVE_INTERVAL
            while True:
                # Heartbeat during long/cold waits so big cold-starts don't time out the client.
                try:
                    if _ka and _ka > 0:
                        try:
                            data = await asyncio.wait_for(_ait.__anext__(), timeout=_ka)
                        except asyncio.TimeoutError:
                            yield sse_comment("keepalive")
                            continue
                    else:
                        data = await _ait.__anext__()
                except StopAsyncIteration:
                    break
                if not isinstance(data, dict):
                    continue
                dtype = str(data.get("type", ""))
                if dtype in _IGNORE_TYPES:
                    continue
                if dtype == "done":
                    break
                if self._is_reasoning(dtype, data):
                    rtext = self._extract_event_text(data)
                    if rtext:
                        yield format_openai_chunk(chat_id, model, "", reasoning=rtext)
                    continue
                if not (dtype in _TEXT_TYPES or ("content" in data and not dtype)):
                    continue
                tok = self._extract_event_text(data)
                if not tok:
                    continue
                if in_tool:
                    tool_region += tok
                    continue
                pending += tok
                idx = pending.find(sentinel)
                if idx != -1:
                    before = pending[:idx]
                    if before:
                        streamed.append(before)
                        yield format_openai_chunk(chat_id, model, before)
                    in_tool = True
                    tool_region = pending[idx:]
                    pending = ""
                else:
                    hb = tool_bridge.sentinel_holdback(pending, sentinel)
                    emit = pending[: len(pending) - hb]
                    if emit:
                        streamed.append(emit)
                        yield format_openai_chunk(chat_id, model, emit)
                    pending = pending[len(pending) - hb:]

            if pending and not in_tool:
                streamed.append(pending)
                yield format_openai_chunk(chat_id, model, pending)
                pending = ""

            await self.session_store.put(key, thread_id, mapped_model, len(req.messages))

            calls = tool_bridge.parse_tool_calls(tool_region) if in_tool else []
            if calls:
                for c in calls:
                    yield format_openai_chunk(chat_id, model, "", tool_calls=[c])
                finish_reason = "tool_calls"
                logger.info("Tool-mode: emitted %d tool_call(s)", len(calls))
            elif in_tool and tool_region:
                # Sentinel opened but nothing parseable — surface raw text so
                # nothing is silently dropped.
                yield format_openai_chunk(chat_id, model, tool_region)
                streamed.append(tool_region)
            meta["completion_text"] = "".join(streamed)

        except asyncio.CancelledError:
            logger.info("Client disconnected (tool mode); interrupting thread %s", thread_id)
            self._fire_interrupt(thread_id, cookies)
            raise
        except Exception as e:
            logger.error("Tool-mode stream error: %s", e)
            meta["error"] = str(e)
            yield format_openai_chunk(chat_id, model, f"\n[Stream Error: {e}]")

        meta["finish_reason"] = finish_reason
        yield format_openai_chunk(chat_id, model, "", finish_reason=finish_reason)
        yield DONE

    # ------------------------------------------------------------------ #
    # Non-streaming                                                       #
    # ------------------------------------------------------------------ #
    async def execute_chat_non_stream(
        self,
        req: ChatCompletionRequest,
        session_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> ChatCompletionResponse:
        """Accumulate streamed chunks into a standard ChatCompletionResponse."""
        chat_id = f"chatcmpl-{uuid.uuid4()}"
        meta = meta if meta is not None else {}
        text_content = ""
        reasoning_content = ""
        tool_calls: List[Dict[str, Any]] = []
        finish_reason = "stop"

        async for chunk in self.execute_chat_stream(req, session_id=session_id, meta=meta):
            if not chunk.startswith("data: "):
                continue
            data_str = chunk[6:].strip()
            if data_str == "[DONE]":
                break
            try:
                data = json.loads(data_str)
            except Exception:
                continue
            choices = data.get("choices", [])
            if not choices:
                continue
            if choices[0].get("finish_reason"):
                finish_reason = choices[0]["finish_reason"]
            delta = choices[0].get("delta", {})
            if delta.get("content"):
                text_content += delta["content"]
            if delta.get("reasoning_content"):
                reasoning_content += delta["reasoning_content"]
            for tc in delta.get("tool_calls", []) or []:
                tool_calls.append({
                    "id": tc.get("id"),
                    "type": tc.get("type", "function"),
                    "function": tc.get("function", {}),
                })

        usage = None
        if config.ENABLE_USAGE:
            system_prompt, combined = self._prepare_prompts(req.messages)
            usage = build_usage(f"{system_prompt}\n{combined}", text_content + reasoning_content)
            meta["prompt_tokens"] = usage["prompt_tokens"]
            meta["completion_tokens"] = usage["completion_tokens"]

        message = Message(
            role="assistant",
            content=(text_content if text_content or not tool_calls else None),
            reasoning_content=reasoning_content if reasoning_content else None,
            tool_calls=tool_calls or None,
        )
        return ChatCompletionResponse(
            id=chat_id,
            created=int(time.time()),
            model=req.model,
            choices=[Choice(index=0, message=message, finish_reason=finish_reason)],
            usage=usage,
        )
