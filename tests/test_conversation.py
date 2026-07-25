"""Tests for conversation resolution: prompts, session keys, thread routing."""

import unittest

from src.core import config
from src.core.schemas import Message
from src.services import conversation


class TestPrompts(unittest.TestCase):
    def test_system_and_dialog_split(self):
        system, dialog = conversation.split_prompts([
            Message(role="system", content="System instruction"),
            Message(role="user", content="Hello"),
            Message(role="assistant", content="Hi user"),
            Message(role="user", content="How are you?"),
        ])
        self.assertEqual(system, "System instruction")
        self.assertEqual(dialog, "User: Hello\n\nAssistant: Hi user\n\nUser: How are you?")

    def test_developer_role_counts_as_system(self):
        # Cursor / OpenAI o1+ / OpenCode send "developer" instead of "system".
        system, dialog = conversation.split_prompts([
            Message(role="developer", content="Be terse"),
            Message(role="user", content="hi"),
        ])
        self.assertEqual(system, "Be terse")
        self.assertEqual(dialog, "User: hi")

    def test_missing_system_prompt_gets_proxy_default(self):
        system, _ = conversation.split_prompts([Message(role="user", content="hi")])
        self.assertEqual(system, conversation.DEFAULT_PROXY_SYSTEM_PROMPT)

    def test_system_only_falls_back_to_last_message(self):
        system, dialog = conversation.split_prompts([
            Message(role="system", content="Only system message")
        ])
        self.assertEqual(system, "Only system message")
        self.assertEqual(dialog, "Only system message")

    def test_accepts_plain_dicts(self):
        self.assertEqual(conversation.role_and_text({"role": "user", "content": "x"}), ("user", "x"))
        self.assertEqual(conversation.role_and_text("garbage"), ("user", ""))

    def test_latest_and_first_user_text(self):
        messages = [
            Message(role="user", content="first"),
            Message(role="assistant", content="mid"),
            Message(role="user", content="last"),
        ]
        self.assertEqual(conversation.latest_text(messages), "last")
        self.assertEqual(conversation.first_user_text(messages), "first")
        self.assertEqual(conversation.latest_text([]), "")


class TestSessionKeys(unittest.TestCase):
    def test_history_key_is_stable_and_content_sensitive(self):
        a = conversation.history_key("sys", "opus", [("user", "hi")])
        b = conversation.history_key("sys", "opus", [("user", "hi")])
        c = conversation.history_key("sys", "opus", [("user", "ho")])
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertTrue(a.startswith("px:"))

    def test_model_and_system_are_part_of_the_key(self):
        pairs = [("user", "hi")]
        self.assertNotEqual(
            conversation.history_key("sys", "opus", pairs),
            conversation.history_key("sys", "sonnet", pairs),
        )
        self.assertNotEqual(
            conversation.history_key("a", "opus", pairs),
            conversation.history_key("b", "opus", pairs),
        )

    def test_delimiters_prevent_pair_collisions(self):
        # ("ab","c") and ("a","bc") must not hash alike.
        self.assertNotEqual(
            conversation.history_key("s", "m", [("ab", "c")]),
            conversation.history_key("s", "m", [("a", "bc")]),
        )

    def test_conversation_key_ignores_the_tail(self):
        opening = [Message(role="user", content="start")]
        later = opening + [
            Message(role="assistant", content="..."),
            Message(role="tool", content="result"),
        ]
        self.assertEqual(
            conversation.conversation_key("s", "m", opening),
            conversation.conversation_key("s", "m", later),
        )


class TestResolveSession(unittest.TestCase):
    def test_history_hash_by_default(self):
        session = conversation.resolve_session(
            "opus-4.8", [Message(role="user", content="hi")]
        )
        self.assertEqual(session.model, "opus-latest")
        self.assertIsNone(session.explicit_id)
        self.assertTrue(session.lookup_key.startswith("px:"))
        self.assertTrue(session.is_first_turn)

    def test_explicit_id_precedence(self):
        # header beats model suffix, which beats the OpenAI `user` field.
        by_header = conversation.resolve_session(
            "opus-4.8@suffix", [], session_id="hdr", user="usr"
        )
        self.assertEqual(by_header.lookup_key, "sid:hdr")

        by_suffix = conversation.resolve_session("opus-4.8@suffix", [], user="usr")
        self.assertEqual(by_suffix.lookup_key, "sid:suffix")

        by_user = conversation.resolve_session("opus-4.8", [], user="usr")
        self.assertEqual(by_user.lookup_key, "sid:usr")

    def test_tool_mode_uses_conversation_key(self):
        session = conversation.resolve_session(
            "opus-4.8", [Message(role="user", content="hi")], tool_mode=True
        )
        self.assertTrue(session.lookup_key.startswith("cv:"))

    def test_forward_key_matches_the_next_turn(self):
        """The key stored after turn 1 is the key turn 2 will look up."""
        turn1 = [Message(role="user", content="Q1")]
        first = conversation.resolve_session("opus-4.8", turn1)
        stored = first.forward_key("A1")

        turn2 = turn1 + [
            Message(role="assistant", content="A1"),
            Message(role="user", content="Q2"),
        ]
        second = conversation.resolve_session("opus-4.8", turn2)
        self.assertEqual(stored, second.lookup_key)

    def test_unknown_model_falls_back(self):
        session = conversation.resolve_session("no-such-model", [])
        self.assertEqual(session.model, config.DEFAULT_MODEL)


if __name__ == "__main__":
    unittest.main()
