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


if __name__ == "__main__":
    unittest.main()
