"""Tests for configuration resolution: env vs file precedence, sessions, models."""

import os
import tempfile
import unittest
from unittest.mock import patch

from src.core import config
from src.core.config import resolve_model


class TestTypedLookups(unittest.TestCase):
    def test_config_file_values_are_used(self):
        with patch.dict("os.environ", {"PORT": "", "LOW_LATENCY_MODE": "", "REASONING_STYLE": ""}, clear=False):
            with patch.object(config, "_FILE", {"port": 9999, "low_latency_mode": False, "reasoning_style": "both"}):
                self.assertEqual(config.env_int("PORT", 8000), 9999)
                self.assertFalse(config.env_flag("LOW_LATENCY_MODE", True))
                self.assertEqual(config.env_str("REASONING_STYLE", "reasoning_content"), "both")

    def test_env_overrides_file(self):
        with patch.dict("os.environ", {"PORT": "7000"}, clear=False):
            with patch.object(config, "_FILE", {"port": 9999}):
                self.assertEqual(config.env_int("PORT", 8000), 7000)

    def test_defaults_when_unset(self):
        with patch.dict("os.environ", {"NOPE_UNSET": ""}, clear=False):
            with patch.object(config, "_FILE", {}):
                self.assertEqual(config.env_int("NOPE_UNSET", 42), 42)
                self.assertEqual(config.env_float("NOPE_UNSET", 1.5), 1.5)
                self.assertEqual(config.env_str("NOPE_UNSET", "d"), "d")
                self.assertTrue(config.env_flag("NOPE_UNSET", True))

    def test_malformed_numbers_fall_back(self):
        with patch.dict("os.environ", {"PORT": "not-a-number"}, clear=False):
            self.assertEqual(config.env_int("PORT", 8000), 8000)


class TestLoadSessions(unittest.TestCase):
    def test_env_single_and_plural_dedup(self):
        env = {"HYPERAGENT_SESSION": "tokA", "HYPERAGENT_SESSIONS": "tokB, tokA ,tokC"}
        with patch.dict("os.environ", env, clear=False):
            with patch.object(config, "SESSIONS_FILE", ""), patch.object(config, "_FILE", {}):
                tokens = config.load_sessions()
        self.assertEqual(tokens, ["tokA", "tokB", "tokC"])  # order preserved, deduped

    def test_empty(self):
        with patch.dict("os.environ", {"HYPERAGENT_SESSION": "", "HYPERAGENT_SESSIONS": ""}, clear=False):
            with patch.object(config, "SESSIONS_FILE", ""), patch.object(config, "_FILE", {}):
                if not os.path.exists("sessions.txt"):
                    self.assertEqual(config.load_sessions(), [])

    def test_sessions_file_lines_with_comments(self):
        path = os.path.join(tempfile.mkdtemp(), "sessions.txt")
        with open(path, "w") as f:
            f.write("# comment\ntokX\ntokY\n\n")
        with patch.dict("os.environ", {"HYPERAGENT_SESSION": "", "HYPERAGENT_SESSIONS": ""}, clear=False):
            with patch.object(config, "SESSIONS_FILE", path), patch.object(config, "_FILE", {}):
                self.assertEqual(config.load_sessions(), ["tokX", "tokY"])

    def test_sessions_file_json_array(self):
        path = os.path.join(tempfile.mkdtemp(), "sessions.json")
        with open(path, "w") as f:
            f.write('["tokJ1", "tokJ2"]')
        with patch.dict("os.environ", {"HYPERAGENT_SESSION": "", "HYPERAGENT_SESSIONS": ""}, clear=False):
            with patch.object(config, "SESSIONS_FILE", path), patch.object(config, "_FILE", {}):
                self.assertEqual(config.load_sessions(), ["tokJ1", "tokJ2"])

    def test_sessions_from_config_file(self):
        with patch.dict("os.environ", {"HYPERAGENT_SESSION": "", "HYPERAGENT_SESSIONS": ""}, clear=False):
            with patch.object(config, "SESSIONS_FILE", ""), \
                 patch.object(config, "_FILE", {"sessions": ["fileTokA", "fileTokB"]}):
                if not os.path.exists("sessions.txt"):
                    self.assertEqual(config.load_sessions(), ["fileTokA", "fileTokB"])


class TestResolveModel(unittest.TestCase):
    def test_plain_mapping(self):
        model, sid = resolve_model("opus-4.8")
        self.assertEqual(model, "opus-latest")
        self.assertIsNone(sid)

    def test_named_session_suffix(self):
        model, sid = resolve_model("opus-4.8@my-project")
        self.assertEqual(model, "opus-latest")
        self.assertEqual(sid, "my-project")

    def test_empty_suffix_is_not_a_session(self):
        model, sid = resolve_model("opus-4.8@")
        self.assertEqual(model, "opus-latest")
        self.assertIsNone(sid)

    def test_unknown_falls_back(self):
        model, _ = resolve_model("totally-unknown")
        self.assertEqual(model, config.DEFAULT_MODEL)

    def test_none_and_blank(self):
        self.assertEqual(resolve_model("")[0], config.DEFAULT_MODEL)
        self.assertEqual(resolve_model(None)[0], config.DEFAULT_MODEL)


class TestChatFeatureFlags(unittest.TestCase):
    def test_server_tools_off_flags(self):
        flags = config.server_tools_off_flags()
        self.assertFalse(flags["enableWebSearch"])
        self.assertFalse(flags["enableBrowser"])
        self.assertFalse(flags["injectPlanMode"])
        self.assertIn("searchMode", flags)

    def test_low_latency_mode_forces_lean(self):
        with patch.object(config, "LOW_LATENCY_MODE", True):
            flags = config.get_chat_feature_flags()
        self.assertFalse(flags["enableWebSearch"])
        self.assertFalse(flags["enableBrowser"])
        self.assertFalse(flags["injectPlanMode"])

    def test_flags_are_configurable_when_low_latency_is_off(self):
        with patch.object(config, "LOW_LATENCY_MODE", False), \
             patch.dict("os.environ", {"ENABLE_WEB_SEARCH": "1"}, clear=False):
            flags = config.get_chat_feature_flags()
        self.assertTrue(flags["enableWebSearch"])
        self.assertFalse(flags["enableBrowser"])


if __name__ == "__main__":
    unittest.main()
