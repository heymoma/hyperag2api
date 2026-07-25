"""Tests for the proxy enhancements: session→thread reuse, persistence,
reasoning styles, multimodal parsing, tool-call passthrough, named sessions."""

import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.core import config
from src.core.config import resolve_model
from src.core.session_store import SessionStore
from src.services.chat_service import ChatService
from src.adapters.api.schemas import ChatCompletionRequest, Message


# --------------------------------------------------------------------------- #
# Session store                                                                #
# --------------------------------------------------------------------------- #
class TestSessionStore(unittest.IsolatedAsyncioTestCase):
    async def test_put_get_forget(self):
        s = SessionStore(persist=False)
        await s.put("k1", "thread-1", model="opus")
        self.assertEqual(await s.get("k1"), "thread-1")
        self.assertEqual(await s.count(), 1)
        self.assertTrue(await s.forget("k1"))
        self.assertIsNone(await s.get("k1"))

    async def test_ttl_expiry(self):
        s = SessionStore(persist=False, ttl=100)
        await s.put("k", "t")
        s._cache["k"].last_used = time.time() - 1000  # force stale
        self.assertIsNone(await s.get("k"))

    async def test_lru_eviction(self):
        s = SessionStore(persist=False, max_size=2)
        await s.put("k1", "t1")
        await s.put("k2", "t2")
        await s.put("k3", "t3")
        self.assertIsNone(await s.get("k1"))  # oldest evicted
        self.assertEqual(await s.get("k2"), "t2")
        self.assertEqual(await s.get("k3"), "t3")

    async def test_persistence_roundtrip(self):
        path = os.path.join(tempfile.mkdtemp(), "sessions.db")
        s1 = SessionStore(persist=True, db_path=path)
        await s1.put("conv", "thread-xyz", model="sonnet-5")
        # A brand new store pointed at the same file recovers the mapping.
        s2 = SessionStore(persist=True, db_path=path)
        self.assertEqual(await s2.get("conv"), "thread-xyz")

    async def test_snapshot(self):
        s = SessionStore(persist=False)
        await s.put("a", "t1")
        await s.put("b", "t2")
        snap = await s.snapshot()
        self.assertEqual(len(snap), 2)
        self.assertEqual(snap[0]["key"], "b")  # most recent first


# --------------------------------------------------------------------------- #
# Model / named session resolution                                             #
# --------------------------------------------------------------------------- #
class TestResolveModel(unittest.TestCase):
    def test_plain_mapping(self):
        model, sid = resolve_model("opus-4.8")
        self.assertEqual(model, "opus-latest")
        self.assertIsNone(sid)

    def test_named_session_suffix(self):
        model, sid = resolve_model("opus-4.8@my-project")
        self.assertEqual(model, "opus-latest")
        self.assertEqual(sid, "my-project")

    def test_unknown_falls_back(self):
        model, _ = resolve_model("totally-unknown")
        self.assertEqual(model, config.DEFAULT_MODEL)


# --------------------------------------------------------------------------- #
# Schema leniency / multimodal parsing                                         #
# --------------------------------------------------------------------------- #
class TestSchemas(unittest.TestCase):
    def test_ignores_unknown_fields(self):
        # Modern clients send lots of extra fields; none should 422.
        req = ChatCompletionRequest(
            model="opus-4.8",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.7,
            tools=[{"type": "function"}],
            reasoning_effort="high",
        )
        self.assertEqual(req.messages[0].text(), "hi")

    def test_content_parts_text_and_images(self):
        m = Message(role="user", content=[
            {"type": "text", "text": "describe this"},
            {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
        ])
        self.assertEqual(m.text(), "describe this")
        self.assertEqual(m.image_urls(), ["https://x/y.png"])


# --------------------------------------------------------------------------- #
# ChatService: session reuse + rendering                                       #
# --------------------------------------------------------------------------- #
def _text_stream(*parts):
    async def gen(*args, **kwargs):
        for p in parts:
            yield {"type": "text", "content": p}
    return gen


async def _drain(agen):
    return [c async for c in agen]


class TestChatServiceSessions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cookies = AsyncMock()
        self.cookies.get_cookies = AsyncMock(return_value={"s": "1"})
        self.backend = MagicMock()
        self.backend.create_thread = AsyncMock(return_value="thread-1")
        self.backend.warm_thread = AsyncMock(return_value=None)
        self.backend.upload_file = AsyncMock(return_value=None)
        self.backend.interrupt = AsyncMock(return_value=None)
        self.sent_prompts = []

        def mk_stream(thread_id, prompt, cookies, **kw):
            self.sent_prompts.append(prompt)
            async def gen():
                yield {"type": "text", "content": "A1"}
            return gen()

        self.backend.stream_chat = MagicMock(side_effect=mk_stream)
        self.svc = ChatService(self.cookies, self.backend, session_store=SessionStore(persist=False))

    async def test_thread_reused_across_turns(self):
        # Turn 1: single user message → creates a thread.
        req1 = ChatCompletionRequest(model="opus-4.8", messages=[Message(role="user", content="Q1")], stream=True)
        await _drain(self.svc.execute_chat_stream(req1))
        self.assertEqual(self.backend.create_thread.await_count, 1)
        self.assertEqual(self.sent_prompts[-1], "Q1")

        # Turn 2: history echoes back the assistant reply "A1" → same conversation.
        req2 = ChatCompletionRequest(model="opus-4.8", messages=[
            Message(role="user", content="Q1"),
            Message(role="assistant", content="A1"),
            Message(role="user", content="Q2"),
        ], stream=True)
        await _drain(self.svc.execute_chat_stream(req2))

        # No new thread was created, and only the newest message was sent.
        self.assertEqual(self.backend.create_thread.await_count, 1)
        self.assertEqual(self.sent_prompts[-1], "Q2")

    async def test_named_session_pins_thread(self):
        req1 = ChatCompletionRequest(model="opus-4.8@proj", messages=[Message(role="user", content="A")], stream=True)
        await _drain(self.svc.execute_chat_stream(req1))
        # A completely different history but same @proj suffix → reuse.
        req2 = ChatCompletionRequest(model="opus-4.8@proj", messages=[Message(role="user", content="totally new")], stream=True)
        await _drain(self.svc.execute_chat_stream(req2))
        self.assertEqual(self.backend.create_thread.await_count, 1)
        self.assertEqual(self.sent_prompts[-1], "totally new")

    async def test_reasoning_think_tags(self):
        orig = config.REASONING_STYLE
        config.REASONING_STYLE = "think_tags"
        try:
            def mk_stream(thread_id, prompt, cookies, **kw):
                async def gen():
                    yield {"type": "thinking", "content": "let me think"}
                    yield {"type": "text", "content": "final answer"}
                return gen()
            self.backend.stream_chat = MagicMock(side_effect=mk_stream)
            req = ChatCompletionRequest(model="opus-4.8", messages=[Message(role="user", content="hi")], stream=True)
            chunks = await _drain(self.svc.execute_chat_stream(req))
            blob = "".join(chunks)
            self.assertIn("<think>", blob)
            self.assertIn("</think>", blob)
            self.assertIn("let me think", blob)
            self.assertIn("final answer", blob)
        finally:
            config.REASONING_STYLE = orig

    async def test_toolcall_openai_passthrough(self):
        orig = config.TOOLCALL_MODE
        config.TOOLCALL_MODE = "openai"
        try:
            def mk_stream(thread_id, prompt, cookies, **kw):
                async def gen():
                    yield {"type": "tool_call", "toolName": "web_search", "toolInput": {"q": "x"}}
                    yield {"type": "text", "content": "done"}
                return gen()
            self.backend.stream_chat = MagicMock(side_effect=mk_stream)
            req = ChatCompletionRequest(model="opus-4.8", messages=[Message(role="user", content="hi")], stream=True)
            blob = "".join(await _drain(self.svc.execute_chat_stream(req)))
            self.assertIn("tool_calls", blob)
            self.assertIn("web_search", blob)
        finally:
            config.TOOLCALL_MODE = orig


TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the weather for a city",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}]


class TestToolBridge(unittest.TestCase):
    def test_parse_tool_calls(self):
        from src.services import tool_bridge
        text = 'Sure.<tool_call>{"name": "get_weather", "arguments": {"city": "Paris"}}</tool_call>'
        calls = tool_bridge.parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "get_weather")
        self.assertIn("Paris", calls[0]["function"]["arguments"])

    def test_parse_multiple(self):
        from src.services import tool_bridge
        text = ('<tool_call>{"name":"a","arguments":{}}</tool_call>'
                '<tool_call>{"name":"b","arguments":{"x":1}}</tool_call>')
        calls = tool_bridge.parse_tool_calls(text)
        self.assertEqual([c["function"]["name"] for c in calls], ["a", "b"])

    def test_preamble_and_disabled(self):
        from src.services import tool_bridge
        self.assertIn("get_weather", tool_bridge.build_tool_preamble(TOOLS))
        self.assertTrue(tool_bridge.tools_disabled("none"))
        self.assertFalse(tool_bridge.tools_disabled("auto"))

    def test_parse_fenced_fallback(self):
        from src.services import tool_bridge
        text = '```json\n{"name": "get_weather", "arguments": {"city": "Rome"}}\n```'
        calls = tool_bridge.parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "get_weather")

    def test_parse_dedup(self):
        from src.services import tool_bridge
        text = ('<tool_call>{"name":"a","arguments":{"x":1}}</tool_call>'
                '<tool_call>{"name":"a","arguments":{"x":1}}</tool_call>')
        self.assertEqual(len(tool_bridge.parse_tool_calls(text)), 1)

    def test_reminder_is_compact(self):
        from src.services import tool_bridge
        rem = tool_bridge.build_tool_reminder()
        self.assertIn("<tool_call>", rem)
        self.assertLess(len(rem), 260)

    def test_server_tools_off_flags(self):
        flags = config.server_tools_off_flags()
        self.assertFalse(flags["enableWebSearch"])
        self.assertFalse(flags["enableBrowser"])
        self.assertFalse(flags["injectPlanMode"])

    def test_low_latency_mode_forces_lean(self):
        orig = config.LOW_LATENCY_MODE
        try:
            config.LOW_LATENCY_MODE = True
            flags = config.get_chat_feature_flags()
            self.assertFalse(flags["enableWebSearch"])
            self.assertFalse(flags["enableBrowser"])
            self.assertFalse(flags["injectPlanMode"])
        finally:
            config.LOW_LATENCY_MODE = orig

    def test_sentinel_holdback(self):
        from src.services import tool_bridge
        # a trailing partial sentinel must be held back
        self.assertEqual(tool_bridge.sentinel_holdback("hi <tool"), 5)
        self.assertEqual(tool_bridge.sentinel_holdback("nothing here"), 0)

    def test_tools_signature_changes(self):
        from src.services import tool_bridge
        sig1 = tool_bridge.tools_signature(TOOLS)
        sig2 = tool_bridge.tools_signature(TOOLS + [{"type": "function", "function": {"name": "extra", "parameters": {}}}])
        self.assertNotEqual(sig1, sig2)


def _tok_stream(*parts):
    def f(thread_id, prompt, cookies, **kw):
        async def g():
            for p in parts:
                yield {"type": "text", "content": p}
        return g()
    return f


async def _collect_parts(agen):
    contents, calls, finish = [], [], "stop"
    async for chunk in agen:
        if not chunk.startswith("data: "):
            continue
        ds = chunk[6:].strip()
        if ds == "[DONE]":
            break
        try:
            d = json.loads(ds)
        except Exception:
            continue
        ch = (d.get("choices") or [{}])[0]
        if ch.get("finish_reason"):
            finish = ch["finish_reason"]
        delta = ch.get("delta", {})
        if delta.get("content"):
            contents.append(delta["content"])
        for tc in delta.get("tool_calls", []) or []:
            calls.append(tc.get("function", {}))
    return contents, calls, finish


import json  # noqa: E402  (used by _collect_parts)


class TestHybridStreaming(unittest.IsolatedAsyncioTestCase):
    def _svc(self, stream_fn):
        cookies = AsyncMock()
        cookies.get_cookies = AsyncMock(return_value={"s": "1"})
        backend = MagicMock()
        backend.create_thread = AsyncMock(return_value="T1")
        backend.warm_thread = AsyncMock(return_value=None)
        backend.upload_file = AsyncMock(return_value=None)
        backend.interrupt = AsyncMock(return_value=None)
        backend.stream_chat = MagicMock(side_effect=stream_fn)
        return ChatService(cookies, backend, session_store=SessionStore(persist=False))

    async def test_plain_text_streams_in_chunks(self):
        svc = self._svc(_tok_stream("Hel", "lo ", "world"))
        req = ChatCompletionRequest(model="opus-4.8", stream=True, tools=TOOLS,
                                    messages=[Message(role="user", content="hi")])
        contents, calls, finish = await _collect_parts(svc.execute_chat_stream(req))
        self.assertEqual("".join(contents), "Hello world")
        self.assertGreater(len(contents), 1)   # actually streamed, not buffered
        self.assertEqual(finish, "stop")
        self.assertFalse(calls)

    async def test_tool_call_split_across_tokens(self):
        svc = self._svc(_tok_stream(
            "Sure ", "<tool", "_call>",
            '{"name":"get_weather","arguments":{"city":"Rome"}}', "</tool_call>",
        ))
        req = ChatCompletionRequest(model="opus-4.8", stream=True, tools=TOOLS,
                                    messages=[Message(role="user", content="weather in Rome")])
        contents, calls, finish = await _collect_parts(svc.execute_chat_stream(req))
        self.assertEqual(finish, "tool_calls")
        self.assertTrue(calls and calls[0]["name"] == "get_weather")
        # the partial sentinel must never leak into streamed content
        self.assertTrue(all("<tool" not in c for c in contents))
        self.assertEqual("".join(contents), "Sure ")


class TestToolCalling(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cookies = AsyncMock()
        self.cookies.get_cookies = AsyncMock(return_value={"s": "1"})
        self.backend = MagicMock()
        self.backend.create_thread = AsyncMock(return_value="T1")
        self.backend.warm_thread = AsyncMock(return_value=None)
        self.backend.upload_file = AsyncMock(return_value=None)
        self.backend.interrupt = AsyncMock(return_value=None)
        self.sent = []

        def mk(thread_id, prompt, cookies, **kw):
            self.sent.append(prompt)

            async def gen():
                if "Result from" in prompt:
                    yield {"type": "text", "content": "The weather in Paris is sunny, 25C."}
                else:
                    yield {"type": "text",
                           "content": '<tool_call>{"name": "get_weather", "arguments": {"city": "Paris"}}</tool_call>'}
            return gen()

        self.backend.stream_chat = MagicMock(side_effect=mk)
        self.svc = ChatService(self.cookies, self.backend, session_store=SessionStore(persist=False))

    async def test_tool_call_emitted(self):
        req = ChatCompletionRequest(model="opus-4.8", stream=True, tools=TOOLS,
                                    messages=[Message(role="user", content="Weather in Paris?")])
        blob = "".join(await _drain(self.svc.execute_chat_stream(req)))
        self.assertIn("tool_calls", blob)
        self.assertIn("get_weather", blob)
        self.assertIn('"finish_reason": "tool_calls"', blob)

    async def test_tool_result_continuation_reuses_thread(self):
        req1 = ChatCompletionRequest(model="opus-4.8", stream=True, tools=TOOLS,
                                     messages=[Message(role="user", content="Weather in Paris?")])
        await _drain(self.svc.execute_chat_stream(req1))

        req2 = ChatCompletionRequest(model="opus-4.8", stream=True, tools=TOOLS, messages=[
            Message(role="user", content="Weather in Paris?"),
            Message(role="assistant", content=None, tool_calls=[
                {"id": "call_1", "type": "function",
                 "function": {"name": "get_weather", "arguments": "{\"city\":\"Paris\"}"}}]),
            Message(role="tool", tool_call_id="call_1", content="sunny 25C"),
        ])
        blob = "".join(await _drain(self.svc.execute_chat_stream(req2)))
        self.assertIn("sunny", blob)                       # final answer streamed
        self.assertEqual(self.backend.create_thread.await_count, 1)  # thread reused
        self.assertTrue(any("Result from" in p and "sunny 25C" in p for p in self.sent))

    async def test_non_stream_tool_calls(self):
        req = ChatCompletionRequest(model="opus-4.8", stream=False, tools=TOOLS,
                                    messages=[Message(role="user", content="Weather?")])
        resp = await self.svc.execute_chat_non_stream(req)
        self.assertEqual(resp.choices[0].finish_reason, "tool_calls")
        self.assertTrue(resp.choices[0].message.tool_calls)
        self.assertEqual(resp.choices[0].message.tool_calls[0]["function"]["name"], "get_weather")


if __name__ == "__main__":
    unittest.main()
