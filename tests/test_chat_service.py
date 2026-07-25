"""Tests for ChatService orchestration: thread reuse, rendering, error paths."""

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.core import config
from src.core.config import MODEL_MAPPING
from src.core.schemas import ChatCompletionRequest, Message
from src.core.session_store import SessionStore
from src.services.chat_service import ChatService


async def drain(agen):
    return [chunk async for chunk in agen]


def make_backend(stream_fn=None, thread_id="thread-1"):
    backend = MagicMock()
    backend.create_thread = AsyncMock(return_value=thread_id)
    backend.warm_thread = AsyncMock(return_value=None)
    backend.upload_file = AsyncMock(return_value=None)
    backend.interrupt = AsyncMock(return_value=None)
    if stream_fn is not None:
        backend.stream_chat = MagicMock(side_effect=stream_fn)
    return backend


def make_service(backend, cookies=None):
    provider = AsyncMock()
    provider.get_cookies = AsyncMock(return_value=cookies or {"s": "1"})
    return ChatService(provider, backend, session_store=SessionStore(persist=False)), provider


class TestModelListing(unittest.TestCase):
    def test_lists_every_mapped_model(self):
        service, _ = make_service(make_backend())
        models = service.get_available_models()
        self.assertEqual(len(models), len(MODEL_MAPPING))
        for model in models:
            self.assertEqual(model["object"], "model")
            self.assertIn("id", model)


class TestThreadReuse(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sent = []

        def stream(thread_id, prompt, cookies, **kwargs):
            self.sent.append(prompt)

            async def gen():
                yield {"type": "text", "content": "A1"}
            return gen()

        self.backend = make_backend(stream)
        self.service, _ = make_service(self.backend)

    async def test_thread_reused_across_turns(self):
        # Turn 1: single user message → creates a thread.
        turn1 = ChatCompletionRequest(
            model="opus-4.8", stream=True, messages=[Message(role="user", content="Q1")]
        )
        await drain(self.service.execute_chat_stream(turn1))
        self.assertEqual(self.backend.create_thread.await_count, 1)
        self.assertEqual(self.sent[-1], "Q1")

        # Turn 2: history echoes back the assistant reply "A1" → same conversation.
        turn2 = ChatCompletionRequest(model="opus-4.8", stream=True, messages=[
            Message(role="user", content="Q1"),
            Message(role="assistant", content="A1"),
            Message(role="user", content="Q2"),
        ])
        await drain(self.service.execute_chat_stream(turn2))

        # No new thread, and only the newest message went upstream.
        self.assertEqual(self.backend.create_thread.await_count, 1)
        self.assertEqual(self.sent[-1], "Q2")

    async def test_named_session_pins_thread(self):
        first = ChatCompletionRequest(
            model="opus-4.8@proj", stream=True, messages=[Message(role="user", content="A")]
        )
        await drain(self.service.execute_chat_stream(first))

        # A completely different history but the same @proj suffix → reuse.
        second = ChatCompletionRequest(
            model="opus-4.8@proj", stream=True,
            messages=[Message(role="user", content="totally new")],
        )
        await drain(self.service.execute_chat_stream(second))
        self.assertEqual(self.backend.create_thread.await_count, 1)
        self.assertEqual(self.sent[-1], "totally new")

    async def test_unrecognised_history_replays_the_full_dialog(self):
        # A multi-turn history the store has never seen must not lose context.
        req = ChatCompletionRequest(model="opus-4.8", stream=True, messages=[
            Message(role="user", content="Q1"),
            Message(role="assistant", content="A1"),
            Message(role="user", content="Q2"),
        ])
        await drain(self.service.execute_chat_stream(req))
        self.assertIn("Q1", self.sent[-1])
        self.assertIn("A1", self.sent[-1])
        self.assertIn("Q2", self.sent[-1])

    async def test_meta_reports_thread_and_reuse(self):
        req = ChatCompletionRequest(
            model="opus-4.8", stream=True, messages=[Message(role="user", content="hi")]
        )
        meta = {}
        await drain(self.service.execute_chat_stream(req, meta=meta))
        self.assertEqual(meta["thread_id"], "thread-1")
        self.assertFalse(meta["reused_thread"])
        self.assertTrue(meta["session_key"].startswith("px:"))
        self.assertEqual(meta["completion_text"], "A1")


class TestRendering(unittest.IsolatedAsyncioTestCase):
    async def test_reasoning_think_tags(self):
        def stream(thread_id, prompt, cookies, **kwargs):
            async def gen():
                yield {"type": "thinking", "content": "let me think"}
                yield {"type": "text", "content": "final answer"}
            return gen()

        service, _ = make_service(make_backend(stream))
        req = ChatCompletionRequest(
            model="opus-4.8", stream=True, messages=[Message(role="user", content="hi")]
        )
        with patch.object(config, "REASONING_STYLE", "think_tags"):
            blob = "".join(await drain(service.execute_chat_stream(req)))

        self.assertIn("<think>", blob)
        self.assertIn("</think>", blob)
        self.assertIn("let me think", blob)
        self.assertIn("final answer", blob)

    async def test_backend_tool_calls_passthrough(self):
        def stream(thread_id, prompt, cookies, **kwargs):
            async def gen():
                yield {"type": "tool_call", "toolName": "web_search", "toolInput": {"q": "x"}}
                yield {"type": "text", "content": "done"}
            return gen()

        service, _ = make_service(make_backend(stream))
        req = ChatCompletionRequest(
            model="opus-4.8", stream=True, messages=[Message(role="user", content="hi")]
        )
        with patch.object(config, "TOOLCALL_MODE", "openai"):
            blob = "".join(await drain(service.execute_chat_stream(req)))

        self.assertIn("tool_calls", blob)
        self.assertIn("web_search", blob)

    async def test_steering_question_is_rendered(self):
        def stream(thread_id, prompt, cookies, **kwargs):
            async def gen():
                yield {"type": "thinking", "content": "Thinking about it..."}
                yield {"type": "text", "content": "Hello world!"}
                yield {
                    "type": "tool_input_ready",
                    "toolName": "mcp__t__AskQuestion",
                    "toolInput": {"questions": [
                        {"question": "What model?", "options": [{"label": "Standard", "value": "std"}]}
                    ]},
                }
            return gen()

        service, _ = make_service(make_backend(stream))
        req = ChatCompletionRequest(
            model="opus-4.8", stream=True, messages=[Message(role="user", content="Hello")]
        )
        chunks = await drain(service.execute_chat_stream(req))

        self.assertIn("assistant", chunks[0])
        self.assertTrue(any("Thinking about it..." in c for c in chunks))
        self.assertTrue(any("Hello world!" in c for c in chunks))
        self.assertTrue(any("Action Required" in c for c in chunks))
        self.assertEqual(chunks[-1], "data: [DONE]\n\n")


class TestErrorPaths(unittest.IsolatedAsyncioTestCase):
    async def test_cookie_failure_is_reported_as_a_complete_stream(self):
        service, provider = make_service(make_backend())
        provider.get_cookies.side_effect = Exception("CDP connection failed")

        req = ChatCompletionRequest(
            model="opus-4.8", stream=True, messages=[Message(role="user", content="Hello")]
        )
        meta = {}
        chunks = await drain(service.execute_chat_stream(req, meta=meta))

        self.assertTrue(any("CDP connection failed" in c for c in chunks))
        self.assertEqual(chunks[-1], "data: [DONE]\n\n")
        self.assertEqual(meta["error"], "CDP connection failed")

    async def test_backend_failure_is_reported_as_a_complete_stream(self):
        backend = make_backend()
        backend.create_thread = AsyncMock(side_effect=Exception("Hyperagent offline"))
        service, _ = make_service(backend)

        req = ChatCompletionRequest(
            model="opus-4.8", stream=True, messages=[Message(role="user", content="Hello")]
        )
        chunks = await drain(service.execute_chat_stream(req))

        self.assertTrue(any("Hyperagent offline" in c for c in chunks))
        self.assertEqual(chunks[-1], "data: [DONE]\n\n")

    async def test_auth_error_invalidates_the_session(self):
        from src.core.interfaces import AuthError

        def stream(thread_id, prompt, cookies, **kwargs):
            async def gen():
                raise AuthError("session dead")
                yield  # pragma: no cover - unreachable, makes this a generator
            return gen()

        backend = make_backend(stream)
        service, provider = make_service(backend)
        provider.invalidate = MagicMock()

        req = ChatCompletionRequest(
            model="opus-4.8", stream=True, messages=[Message(role="user", content="hi")]
        )
        blob = "".join(await drain(service.execute_chat_stream(req)))

        provider.invalidate.assert_called_once()
        self.assertIn("Session invalidated", blob)


class TestNonStreaming(unittest.IsolatedAsyncioTestCase):
    async def test_accumulates_text_and_reasoning(self):
        def stream(thread_id, prompt, cookies, **kwargs):
            async def gen():
                yield {"type": "thinking", "content": "Thought process"}
                yield {"type": "text", "content": "Response content"}
            return gen()

        service, _ = make_service(make_backend(stream))
        req = ChatCompletionRequest(
            model="opus-4.8", stream=False, messages=[Message(role="user", content="Hello")]
        )
        response = await service.execute_chat_non_stream(req)

        self.assertEqual(response.model, "opus-4.8")
        self.assertEqual(response.choices[0].message.content, "Response content")
        self.assertEqual(response.choices[0].message.reasoning_content, "Thought process")
        self.assertEqual(response.choices[0].finish_reason, "stop")

    async def test_usage_is_reported_when_enabled(self):
        def stream(thread_id, prompt, cookies, **kwargs):
            async def gen():
                yield {"type": "text", "content": "hello there"}
            return gen()

        service, _ = make_service(make_backend(stream))
        req = ChatCompletionRequest(
            model="opus-4.8", stream=False, messages=[Message(role="user", content="Hi")]
        )
        meta = {}
        with patch.object(config, "ENABLE_USAGE", True):
            response = await service.execute_chat_non_stream(req, meta=meta)

        self.assertGreater(response.usage["prompt_tokens"], 0)
        self.assertGreater(response.usage["completion_tokens"], 0)
        self.assertEqual(meta["completion_tokens"], response.usage["completion_tokens"])

    async def test_malformed_chunks_are_skipped(self):
        service, _ = make_service(make_backend())

        async def noisy():
            yield "event: ping\n\n"
            yield "data: not-json\n\n"
            yield 'data: {"choices": []}\n\n'
            yield 'data: {"choices": [{"delta": {"content": "ok"}}]}\n\n'
            yield "data: [DONE]\n\n"

        text, reasoning, calls, finish = await service._accumulate(noisy())
        self.assertEqual(text, "ok")
        self.assertEqual(reasoning, "")
        self.assertEqual(calls, [])
        self.assertEqual(finish, "stop")


if __name__ == "__main__":
    unittest.main()
