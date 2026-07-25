"""Tests for the OpenAI-compatible request/response schemas.

They are deliberately lenient: modern clients send a long tail of fields we do
not act on, and rejecting them with a 422 would break the integration outright.
"""

import unittest

from src.core.schemas import ChatCompletionRequest, Message


class TestMessage(unittest.TestCase):
    def test_plain_string_content(self):
        self.assertEqual(Message(role="user", content="hi").text(), "hi")

    def test_none_content(self):
        self.assertEqual(Message(role="assistant", content=None).text(), "")

    def test_content_parts_text_and_images(self):
        msg = Message(role="user", content=[
            {"type": "text", "text": "describe this"},
            {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
        ])
        self.assertEqual(msg.text(), "describe this")
        self.assertEqual(msg.image_urls(), ["https://x/y.png"])

    def test_responses_api_part_names(self):
        msg = Message(role="user", content=[
            {"type": "input_text", "text": "look"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
        ])
        self.assertEqual(msg.text(), "look")
        self.assertEqual(msg.image_urls(), ["data:image/png;base64,AAA"])

    def test_image_urls_on_string_content(self):
        self.assertEqual(Message(role="user", content="no images").image_urls(), [])


class TestChatCompletionRequest(unittest.TestCase):
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

    def test_defaults(self):
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(req.model, "opus-latest")
        self.assertFalse(req.stream)
        self.assertIsNone(req.tools)


if __name__ == "__main__":
    unittest.main()
