"""Tests for streaming SSE helpers, dual keepalives, and stream chunk formatting."""

import json
import unittest

from src.core.sse import (
    format_openai_chunk,
    format_openai_keepalive_chunk,
    sse_comment,
    DONE,
)


class TestStreamingHelpers(unittest.TestCase):
    def test_sse_comment(self):
        comment = sse_comment("keepalive")
        self.assertEqual(comment, ": keepalive\n\n")

    def test_format_openai_chunk(self):
        chunk_str = format_openai_chunk("chat-1", "opus-latest", "hello world", role="assistant")
        self.assertTrue(chunk_str.startswith("data: "))
        self.assertTrue(chunk_str.endswith("\n\n"))
        data = json.loads(chunk_str[6:].strip())
        self.assertEqual(data["id"], "chat-1")
        self.assertEqual(data["model"], "opus-latest")
        self.assertEqual(data["choices"][0]["delta"]["content"], "hello world")
        self.assertEqual(data["choices"][0]["delta"]["role"], "assistant")

    def test_format_openai_keepalive_chunk(self):
        ka_str = format_openai_keepalive_chunk("chat-1", "opus-latest")
        self.assertTrue(ka_str.startswith("data: "))
        data = json.loads(ka_str[6:].strip())
        self.assertEqual(data["id"], "chat-1")
        self.assertEqual(data["choices"][0]["delta"], {})

    def test_done_constant(self):
        self.assertEqual(DONE, "data: [DONE]\n\n")


if __name__ == "__main__":
    unittest.main()
