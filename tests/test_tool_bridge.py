"""Tests for the tool-calling prompt contract: preamble, parsing, result blocks."""

import json
import unittest

from src.core.schemas import Message
from src.services import tool_bridge

TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the weather for a city",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}]


class TestPreamble(unittest.TestCase):
    def test_describes_every_tool(self):
        preamble = tool_bridge.build_tool_preamble(TOOLS)
        self.assertIn("get_weather", preamble)
        self.assertIn("Get the weather for a city", preamble)
        self.assertIn("<tool_call>", preamble)

    def test_empty_tools_produce_no_preamble(self):
        self.assertEqual(tool_bridge.build_tool_preamble([]), "")
        self.assertEqual(tool_bridge.build_tool_preamble([{"no": "name"}]), "")

    def test_forced_tool_choice_is_stated(self):
        choice = {"type": "function", "function": {"name": "get_weather"}}
        self.assertIn("MUST call the `get_weather`", tool_bridge.build_tool_preamble(TOOLS, choice))

    def test_required_tool_choice_is_stated(self):
        self.assertIn(
            "MUST call at least one", tool_bridge.build_tool_preamble(TOOLS, "required")
        )

    def test_tools_disabled_only_for_none(self):
        self.assertTrue(tool_bridge.tools_disabled("none"))
        self.assertFalse(tool_bridge.tools_disabled("auto"))
        self.assertFalse(tool_bridge.tools_disabled(None))

    def test_signature_changes_with_the_tool_set(self):
        extra = TOOLS + [{"type": "function", "function": {"name": "extra", "parameters": {}}}]
        self.assertNotEqual(tool_bridge.tools_signature(TOOLS), tool_bridge.tools_signature(extra))
        self.assertEqual(tool_bridge.tools_signature(TOOLS), tool_bridge.tools_signature(TOOLS))


class TestParseToolCalls(unittest.TestCase):
    def test_single_call(self):
        text = 'Sure.<tool_call>{"name": "get_weather", "arguments": {"city": "Paris"}}</tool_call>'
        calls = tool_bridge.parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "get_weather")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"city": "Paris"})

    def test_multiple_calls_keep_order(self):
        text = ('<tool_call>{"name":"a","arguments":{}}</tool_call>'
                '<tool_call>{"name":"b","arguments":{"x":1}}</tool_call>')
        self.assertEqual(
            [c["function"]["name"] for c in tool_bridge.parse_tool_calls(text)], ["a", "b"]
        )

    def test_fenced_block_fallback(self):
        text = '```json\n{"name": "get_weather", "arguments": {"city": "Rome"}}\n```'
        calls = tool_bridge.parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "get_weather")

    def test_identical_calls_are_deduped(self):
        text = ('<tool_call>{"name":"a","arguments":{"x":1}}</tool_call>'
                '<tool_call>{"name":"a","arguments":{"x":1}}</tool_call>')
        self.assertEqual(len(tool_bridge.parse_tool_calls(text)), 1)

    def test_malformed_json_is_skipped(self):
        self.assertEqual(tool_bridge.parse_tool_calls("<tool_call>{oops}</tool_call>"), [])
        self.assertEqual(tool_bridge.parse_tool_calls(""), [])

    def test_nameless_object_is_skipped(self):
        self.assertEqual(tool_bridge.parse_tool_calls('<tool_call>{"args":{}}</tool_call>'), [])

    def test_alternate_argument_keys(self):
        text = '<tool_call>{"name":"a","parameters":{"k":1}}</tool_call>'
        calls = tool_bridge.parse_tool_calls(text)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"k": 1})


class TestSentinelHoldback(unittest.TestCase):
    def test_trailing_partial_sentinel_is_held(self):
        self.assertEqual(tool_bridge.sentinel_holdback("hi <tool"), 5)

    def test_no_partial_means_nothing_held(self):
        self.assertEqual(tool_bridge.sentinel_holdback("nothing here"), 0)

    def test_single_character_prefix(self):
        self.assertEqual(tool_bridge.sentinel_holdback("done <"), 1)


class TestToolResults(unittest.TestCase):
    def test_results_are_labelled_with_the_tool_name(self):
        tail = [
            Message(role="assistant", content=None, tool_calls=[
                {"id": "call_1", "type": "function",
                 "function": {"name": "get_weather", "arguments": "{}"}}
            ]),
            Message(role="tool", tool_call_id="call_1", content="sunny 25C"),
        ]
        block = tool_bridge.format_tool_results(tail)
        self.assertIn("Result from `get_weather`", block)
        self.assertIn("sunny 25C", block)

    def test_unknown_call_id_falls_back(self):
        tail = [Message(role="tool", tool_call_id="orphan", content="x")]
        self.assertIn("Result from `orphan`", tool_bridge.format_tool_results(tail))

    def test_no_tool_messages_means_no_block(self):
        self.assertEqual(tool_bridge.format_tool_results([Message(role="user", content="hi")]), "")

    def test_user_messages_are_extracted_from_a_tail(self):
        tail = [
            Message(role="tool", content="result"),
            Message(role="user", content="and also this"),
        ]
        users = tool_bridge.user_messages(tail)
        self.assertEqual([m.content for m in users], ["and also this"])


if __name__ == "__main__":
    unittest.main()
