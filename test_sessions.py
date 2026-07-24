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
                self.assertEqual(config.load_sessions(), [])


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
