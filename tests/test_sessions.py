"""Tests for browserless config sessions, the static provider, and account verify."""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.core import config
from src.adapters.session.config_provider import StaticSessionCookieProvider
from src.services import accounts


class TestLoadSessions(unittest.TestCase):
    def test_env_single_and_plural_dedup(self):
        env = {"HYPERAGENT_SESSION": "tokA", "HYPERAGENT_SESSIONS": "tokB, tokA ,tokC"}
        with patch.dict("os.environ", env, clear=False):
            with patch.object(config, "SESSIONS_FILE", ""):
                toks = config.load_sessions()
        self.assertEqual(toks, ["tokA", "tokB", "tokC"])  # order preserved, deduped

    def test_empty(self):
        with patch.dict("os.environ", {"HYPERAGENT_SESSION": "", "HYPERAGENT_SESSIONS": ""}, clear=False):
            with patch.object(config, "SESSIONS_FILE", ""):
                # avoid picking up a stray sessions.txt in the cwd
                import os
                if not os.path.exists("sessions.txt"):
                    self.assertEqual(config.load_sessions(), [])

    def test_sessions_file(self):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), "sessions.txt")
        with open(path, "w") as f:
            f.write("# comment\ntokX\ntokY\n\n")
        with patch.dict("os.environ", {"HYPERAGENT_SESSION": "", "HYPERAGENT_SESSIONS": ""}, clear=False):
            with patch.object(config, "SESSIONS_FILE", path):
                self.assertEqual(config.load_sessions(), ["tokX", "tokY"])


class TestConfigFile(unittest.TestCase):
    def test_file_values_used(self):
        with patch.dict("os.environ", {"PORT": "", "LOW_LATENCY_MODE": "", "REASONING_STYLE": ""}, clear=False):
            with patch.object(config, "_FILE", {"port": 9999, "low_latency_mode": False, "reasoning_style": "both"}):
                self.assertEqual(config.env_int("PORT", 8000), 9999)
                self.assertFalse(config.env_flag("LOW_LATENCY_MODE", True))
                self.assertEqual(config.env_str("REASONING_STYLE", "reasoning_content"), "both")

    def test_env_overrides_file(self):
        with patch.dict("os.environ", {"PORT": "7000"}, clear=False):
            with patch.object(config, "_FILE", {"port": 9999}):
                self.assertEqual(config.env_int("PORT", 8000), 7000)

    def test_sessions_from_config_file(self):
        with patch.dict("os.environ", {"HYPERAGENT_SESSION": "", "HYPERAGENT_SESSIONS": ""}, clear=False):
            with patch.object(config, "SESSIONS_FILE", ""), patch.object(config, "_FILE", {"sessions": ["fileTokA", "fileTokB"]}):
                import os
                if not os.path.exists("sessions.txt"):
                    self.assertEqual(config.load_sessions(), ["fileTokA", "fileTokB"])


class TestRotation(unittest.IsolatedAsyncioTestCase):
    async def test_create_thread_rotates_on_auth_error(self):
        from src.core.interfaces import AuthError
        from src.services.chat_service import ChatService
        from src.core.session_store import SessionStore

        provider = StaticSessionCookieProvider(["accA", "accB"])
        backend = MagicMock()
        calls = {"n": 0}

        async def create(model, system_prompt, cookies, session_id=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise AuthError("session dead")
            return "T2"

        backend.create_thread = create
        svc = ChatService(provider, backend, session_store=SessionStore(persist=False))
        cookies = await provider.get_cookies()
        tid, used = await svc._create_thread_rotating("opus-latest", "", None, cookies)
        self.assertEqual(tid, "T2")
        self.assertEqual(calls["n"], 2)  # retried after rotation
        self.assertEqual(used["__Host-hyperagent_session"], "accB")  # rotated account


class TestStaticProvider(unittest.IsolatedAsyncioTestCase):
    async def test_get_cookies_returns_active(self):
        p = StaticSessionCookieProvider(["tok1", "tok2"])
        self.assertEqual(await p.get_cookies(), {"__Host-hyperagent_session": "tok1"})

    async def test_invalidate_rotates(self):
        p = StaticSessionCookieProvider(["tok1", "tok2"])
        p.invalidate()
        self.assertEqual((await p.get_cookies())["__Host-hyperagent_session"], "tok2")
        p.invalidate()  # wraps around
        self.assertEqual((await p.get_cookies())["__Host-hyperagent_session"], "tok1")

    async def test_empty_raises(self):
        p = StaticSessionCookieProvider([])
        with self.assertRaises(ConnectionError):
            await p.get_cookies()

    def test_clear_is_noop(self):
        self.assertFalse(StaticSessionCookieProvider(["t"]).clear_cookies())

    async def test_session_cooldown_skips_quarantined(self):
        p = StaticSessionCookieProvider(["tok1", "tok2"])
        p.mark_cooldown("tok1", duration_seconds=100)
        self.assertEqual((await p.get_cookies())["__Host-hyperagent_session"], "tok2")

    async def test_session_cooldown_all_quarantined_fallback(self):
        p = StaticSessionCookieProvider(["tok1", "tok2"])
        p.mark_cooldown("tok1", duration_seconds=100)
        p.mark_cooldown("tok2", duration_seconds=100)
        self.assertIn("tok", (await p.get_cookies())["__Host-hyperagent_session"])


class TestAccountVerify(unittest.IsolatedAsyncioTestCase):
    def _client(self, status, payload=None):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = payload or {}
        client = AsyncMock()
        client.get.return_value = resp
        cm = MagicMock()
        cm.__aenter__.return_value = client
        return cm

    async def test_valid(self):
        cm = self._client(200, {"email": "a@b.com", "name": "Neo", "userId": "u1", "timezone": "UTC"})
        with patch("src.services.accounts.httpx.AsyncClient", return_value=cm):
            info = await accounts.verify_session("secrettok9999")
        self.assertTrue(info["valid"])
        self.assertEqual(info["email"], "a@b.com")
        self.assertEqual(info["session"], "…9999")   # masked, never full token

    async def test_invalid_status(self):
        cm = self._client(401)
        with patch("src.services.accounts.httpx.AsyncClient", return_value=cm):
            info = await accounts.verify_session("tok")
        self.assertFalse(info["valid"])
        self.assertEqual(info["status"], 401)

    async def test_verify_all_empty(self):
        self.assertEqual(await accounts.verify_all([]), [])


if __name__ == "__main__":
    unittest.main()
