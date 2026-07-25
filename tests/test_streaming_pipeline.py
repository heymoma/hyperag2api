"""Tests for the stream plumbing: keepalives, event classification, rendering."""

import asyncio
import json
import unittest
from unittest.mock import patch

from src.core import config
from src.services import stream_events as events
from src.services.render import PlainStreamRenderer
from src.services.streaming import KEEPALIVE, iter_with_keepalive


async def _frames(*items, delay=0.0):
    for item in items:
        if delay:
            await asyncio.sleep(delay)
        yield item


def _deltas(chunks):
    """Extract the delta dicts from a list of SSE chunk strings."""
    out = []
    for chunk in chunks:
        if not chunk.startswith("data: "):
            continue
        payload = chunk[len("data: "):].strip()
        if payload == "[DONE]":
            continue
        out.append(json.loads(payload)["choices"][0]["delta"])
    return out


class TestIterWithKeepalive(unittest.IsolatedAsyncioTestCase):
    async def test_frames_survive_a_silent_gap(self):
        """Regression: a heartbeat must not cancel the in-flight fetch.

        Awaiting __anext__ through wait_for cancels the async generator on
        timeout, which ends it for good — every frame after the first keepalive
        was silently dropped, truncating answers during slow cold starts.
        """
        source = _frames({"type": "text", "content": "a"}, {"type": "text", "content": "b"},
                         delay=0.2)
        items = [i async for i in iter_with_keepalive(source, 0.05)]

        self.assertGreaterEqual(sum(1 for i in items if i is KEEPALIVE), 1)
        self.assertEqual(
            [i for i in items if i is not KEEPALIVE],
            [{"type": "text", "content": "a"}, {"type": "text", "content": "b"}],
        )

    async def test_zero_interval_disables_heartbeats(self):
        items = [i async for i in iter_with_keepalive(_frames({"type": "text"}), 0)]
        self.assertEqual(items, [{"type": "text"}])

    async def test_non_dict_frames_are_dropped(self):
        items = [i async for i in iter_with_keepalive(_frames("junk", {"ok": 1}), 5)]
        self.assertEqual(items, [{"ok": 1}])

    async def test_early_exit_leaves_no_pending_task(self):
        agen = iter_with_keepalive(_frames({"a": 1}, {"b": 2}, delay=0.2), 0.05)
        async for _ in agen:
            break
        await agen.aclose()
        await asyncio.sleep(0.05)
        leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        self.assertEqual(leftover, [])


class TestEventClassification(unittest.TestCase):
    def test_reasoning_variants(self):
        for kind in ("thinking", "reasoning", "reasoning_delta", "thinking-block"):
            self.assertTrue(events.is_reasoning(kind), kind)
        self.assertFalse(events.is_reasoning("text"))

    def test_text_including_untyped_frames(self):
        self.assertTrue(events.is_text("text", {}))
        self.assertTrue(events.is_text("", {"content": "x"}))
        self.assertFalse(events.is_text("", {}))

    def test_tool_detection_by_type_or_field(self):
        self.assertTrue(events.is_tool("tool_call", {}))
        self.assertTrue(events.is_tool("anything", {"toolName": "x"}))
        self.assertTrue(events.is_tool("anything", {"tool_name": "x"}))
        self.assertFalse(events.is_tool("text", {}))

    def test_ask_question_is_a_distinct_tool(self):
        frame = {"type": "tool_input_ready", "toolName": events.ASK_QUESTION_TOOL}
        self.assertTrue(events.is_ask_question("tool_input_ready", frame))
        self.assertFalse(events.is_ask_question("tool_input_ready", {"toolName": "other"}))

    def test_event_text_key_priority(self):
        self.assertEqual(events.event_text({"content": "c", "text": "t"}), "c")
        self.assertEqual(events.event_text({"thinking": "th"}), "th")
        self.assertEqual(events.event_text({"content": 42}), "")

    def test_tool_input_coerces_non_dicts(self):
        self.assertEqual(events.tool_input({"toolInput": ["a"]}), {})
        self.assertEqual(events.tool_input({"tool_input": {"a": 1}}), {"a": 1})

    def test_lifecycle_frames_are_ignorable(self):
        self.assertTrue(events.is_ignorable("sandbox_status"))
        self.assertTrue(events.is_terminal("done"))
        self.assertFalse(events.is_ignorable("text"))


class TestPlainStreamRenderer(unittest.IsolatedAsyncioTestCase):
    async def _render(self, *frames):
        renderer = PlainStreamRenderer("chat-1", "opus-latest")
        chunks = [c async for c in renderer.render(_frames(*frames))]
        return renderer, chunks

    async def test_think_tags_are_opened_and_closed_around_text(self):
        with patch.object(config, "REASONING_STYLE", "think_tags"):
            renderer, chunks = await self._render(
                {"type": "thinking", "content": "hmm"},
                {"type": "text", "content": "answer"},
            )
        self.assertEqual(renderer.completion_text, "<think>\nhmm\n</think>\n\nanswer")
        self.assertNotIn("reasoning_content", "".join(chunks))

    async def test_unterminated_think_block_is_closed_at_the_end(self):
        with patch.object(config, "REASONING_STYLE", "think_tags"):
            renderer, _ = await self._render({"type": "thinking", "content": "hmm"})
        self.assertTrue(renderer.completion_text.endswith("</think>\n\n"))

    async def test_reasoning_content_style_keeps_it_out_of_the_completion(self):
        with patch.object(config, "REASONING_STYLE", "reasoning_content"):
            renderer, chunks = await self._render(
                {"type": "thinking", "content": "hmm"},
                {"type": "text", "content": "answer"},
            )
        self.assertEqual(renderer.completion_text, "answer")
        self.assertEqual(_deltas(chunks)[0]["reasoning_content"], "hmm")

    async def test_tool_mode_openai_emits_tool_calls(self):
        with patch.object(config, "TOOLCALL_MODE", "openai"):
            renderer, chunks = await self._render(
                {"type": "tool_call", "toolName": "web_search", "toolInput": {"q": "x"}}
            )
        call = _deltas(chunks)[0]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "web_search")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"q": "x"})
        self.assertEqual(renderer.completion_text, "")

    async def test_tool_mode_off_hides_backend_tools(self):
        with patch.object(config, "TOOLCALL_MODE", "off"):
            renderer, chunks = await self._render(
                {"type": "tool_call", "toolName": "web_search"},
                {"type": "text", "content": "done"},
            )
        self.assertEqual(renderer.completion_text, "done")

    async def test_ask_question_renders_as_prose(self):
        _, chunks = await self._render({
            "type": "tool_input_ready",
            "toolName": events.ASK_QUESTION_TOOL,
            "toolInput": {"questions": [
                {"question": "Which model?", "options": [{"label": "Fast", "value": "f"}]}
            ]},
        })
        blob = "".join(chunks)
        self.assertIn("Action Required", blob)
        self.assertIn("Which model?", blob)
        self.assertIn("Fast", blob)

    async def test_done_frame_stops_the_stream(self):
        renderer, _ = await self._render(
            {"type": "text", "content": "kept"},
            {"type": "done"},
            {"type": "text", "content": "dropped"},
        )
        self.assertEqual(renderer.completion_text, "kept")

    async def test_lifecycle_noise_is_not_rendered(self):
        renderer, _ = await self._render(
            {"type": "sandbox_status", "content": "booting"},
            {"type": "text", "content": "real"},
        )
        self.assertEqual(renderer.completion_text, "real")


if __name__ == "__main__":
    unittest.main()
