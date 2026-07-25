"""Tests for the client-side tool-calling turn.

Covers the sentinel buffer that keeps a ``<tool_call>`` marker from leaking into
visible content, and the end-to-end handshake: emit tool_calls, take the results
back, continue in the same thread.
"""

import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.core.schemas import ChatCompletionRequest, Message
from src.core.session_store import SessionStore
from src.services.chat_service import ChatService
from src.services.tool_mode import _SentinelBuffer

TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the weather for a city",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}]


def _service(stream_fn):
    provider = AsyncMock()
    provider.get_cookies = AsyncMock(return_value={"s": "1"})
    backend = MagicMock()
    backend.create_thread = AsyncMock(return_value="T1")
    backend.warm_thread = AsyncMock(return_value=None)
    backend.upload_file = AsyncMock(return_value=None)
    backend.interrupt = AsyncMock(return_value=None)
    backend.stream_chat = MagicMock(side_effect=stream_fn)
    return ChatService(provider, backend, session_store=SessionStore(persist=False)), backend


def _token_stream(*tokens):
    def fn(thread_id, prompt, cookies, **kwargs):
        async def gen():
            for token in tokens:
                yield {"type": "text", "content": token}
        return gen()
    return fn


async def _collect(agen):
    """Fold an SSE stream into (content_parts, tool_calls, finish_reason)."""
    contents, calls, finish = [], [], "stop"
    async for chunk in agen:
        if not chunk.startswith("data: "):
            continue
        payload = chunk[len("data: "):].strip()
        if payload == "[DONE]":
            break
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        choice = (data.get("choices") or [{}])[0]
        if choice.get("finish_reason"):
            finish = choice["finish_reason"]
        delta = choice.get("delta", {})
        if delta.get("content"):
            contents.append(delta["content"])
        for call in delta.get("tool_calls") or []:
            calls.append(call.get("function", {}))
    return contents, calls, finish


class TestSentinelBuffer(unittest.TestCase):
    def test_plain_text_passes_straight_through(self):
        buf = _SentinelBuffer()
        self.assertEqual(buf.feed("Hello "), "Hello ")
        self.assertEqual(buf.feed("world"), "world")
        self.assertEqual(buf.flush(), "")
        self.assertEqual(buf.tool_region, "")

    def test_partial_sentinel_is_held_back(self):
        buf = _SentinelBuffer()
        # "<tool" could still become "<tool_call>", so it must not be emitted.
        self.assertEqual(buf.feed("hi <tool"), "hi ")
        self.assertEqual(buf.feed("_call>{}"), "")
        self.assertEqual(buf.tool_region, "<tool_call>{}")

    def test_held_back_text_is_flushed_when_it_was_not_a_sentinel(self):
        buf = _SentinelBuffer()
        self.assertEqual(buf.feed("done <to"), "done ")
        self.assertEqual(buf.flush(), "<to")

    def test_everything_after_the_sentinel_is_captured(self):
        buf = _SentinelBuffer()
        buf.feed('Sure <tool_call>{"name":"a",')
        buf.feed('"arguments":{}}</tool_call>')
        self.assertEqual(
            buf.tool_region, '<tool_call>{"name":"a","arguments":{}}</tool_call>'
        )
        self.assertEqual(buf.flush(), "")

    def test_sentinel_split_across_single_characters(self):
        buf = _SentinelBuffer()
        emitted = "".join(buf.feed(ch) for ch in "ok <tool_call>{}")
        self.assertEqual(emitted, "ok ")
        self.assertNotIn("<tool", emitted)
        self.assertEqual(buf.tool_region, "<tool_call>{}")

class TestHybridStreaming(unittest.IsolatedAsyncioTestCase):
    async def test_plain_text_streams_incrementally(self):
        service, _ = _service(_token_stream("Hel", "lo ", "world"))
        req = ChatCompletionRequest(
            model="opus-4.8", stream=True, tools=TOOLS,
            messages=[Message(role="user", content="hi")],
        )
        contents, calls, finish = await _collect(service.execute_chat_stream(req))

        self.assertEqual("".join(contents), "Hello world")
        self.assertGreater(len(contents), 1)   # actually streamed, not buffered
        self.assertEqual(finish, "stop")
        self.assertFalse(calls)

    async def test_sentinel_split_across_tokens_never_leaks(self):
        service, _ = _service(_token_stream(
            "Sure ", "<tool", "_call>",
            '{"name":"get_weather","arguments":{"city":"Rome"}}', "</tool_call>",
        ))
        req = ChatCompletionRequest(
            model="opus-4.8", stream=True, tools=TOOLS,
            messages=[Message(role="user", content="weather in Rome")],
        )
        contents, calls, finish = await _collect(service.execute_chat_stream(req))

        self.assertEqual(finish, "tool_calls")
        self.assertEqual(calls[0]["name"], "get_weather")
        self.assertTrue(all("<tool" not in c for c in contents))
        self.assertEqual("".join(contents), "Sure ")

    async def test_unparseable_tool_region_is_surfaced_not_dropped(self):
        service, _ = _service(_token_stream("<tool_call>", "{broken", "</tool_call>"))
        req = ChatCompletionRequest(
            model="opus-4.8", stream=True, tools=TOOLS,
            messages=[Message(role="user", content="hi")],
        )
        contents, calls, finish = await _collect(service.execute_chat_stream(req))

        self.assertFalse(calls)
        self.assertEqual(finish, "stop")
        self.assertIn("{broken", "".join(contents))

    async def test_tool_choice_none_uses_the_plain_path(self):
        service, _ = _service(_token_stream("<tool_call>", '{"name":"get_weather"}', "</tool_call>"))
        req = ChatCompletionRequest(
            model="opus-4.8", stream=True, tools=TOOLS, tool_choice="none",
            messages=[Message(role="user", content="hi")],
        )
        _, calls, finish = await _collect(service.execute_chat_stream(req))

        self.assertFalse(calls)          # no tool-call handshake
        self.assertEqual(finish, "stop")


class TestToolCallRoundTrip(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sent = []

        def stream(thread_id, prompt, cookies, **kwargs):
            self.sent.append(prompt)

            async def gen():
                if "Result from" in prompt:
                    yield {"type": "text", "content": "The weather in Paris is sunny, 25C."}
                else:
                    yield {"type": "text", "content":
                           '<tool_call>{"name": "get_weather", "arguments": {"city": "Paris"}}</tool_call>'}
            return gen()

        self.service, self.backend = _service(stream)

    async def test_tool_call_is_emitted(self):
        req = ChatCompletionRequest(
            model="opus-4.8", stream=True, tools=TOOLS,
            messages=[Message(role="user", content="Weather in Paris?")],
        )
        blob = "".join([c async for c in self.service.execute_chat_stream(req)])

        self.assertIn("tool_calls", blob)
        self.assertIn("get_weather", blob)
        self.assertIn('"finish_reason": "tool_calls"', blob)

    async def test_preamble_is_sent_once_per_thread(self):
        first = ChatCompletionRequest(
            model="opus-4.8", stream=True, tools=TOOLS,
            messages=[Message(role="user", content="Weather in Paris?")],
        )
        await _collect(self.service.execute_chat_stream(first))
        self.assertIn("get_weather", self.sent[0])   # full tool schema delivered

    async def test_results_continue_in_the_same_thread(self):
        first = ChatCompletionRequest(
            model="opus-4.8", stream=True, tools=TOOLS,
            messages=[Message(role="user", content="Weather in Paris?")],
        )
        await _collect(self.service.execute_chat_stream(first))

        second = ChatCompletionRequest(model="opus-4.8", stream=True, tools=TOOLS, messages=[
            Message(role="user", content="Weather in Paris?"),
            Message(role="assistant", content=None, tool_calls=[
                {"id": "call_1", "type": "function",
                 "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}]),
            Message(role="tool", tool_call_id="call_1", content="sunny 25C"),
        ])
        blob = "".join([c async for c in self.service.execute_chat_stream(second)])

        self.assertIn("sunny", blob)                                  # final answer streamed
        self.assertEqual(self.backend.create_thread.await_count, 1)   # thread reused
        self.assertTrue(any("Result from" in p and "sunny 25C" in p for p in self.sent))

    async def test_non_streaming_reports_tool_calls(self):
        req = ChatCompletionRequest(
            model="opus-4.8", stream=False, tools=TOOLS,
            messages=[Message(role="user", content="Weather?")],
        )
        response = await self.service.execute_chat_non_stream(req)

        self.assertEqual(response.choices[0].finish_reason, "tool_calls")
        self.assertIsNone(response.choices[0].message.content)
        self.assertEqual(
            response.choices[0].message.tool_calls[0]["function"]["name"], "get_weather"
        )


if __name__ == "__main__":
    unittest.main()
