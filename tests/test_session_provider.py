"""Tests for the browserless session provider: rotation, cooldown, thread retry."""

import unittest
from unittest.mock import MagicMock

from src.adapters.session.config_provider import StaticSessionCookieProvider
from src.core.config import SESSION_COOKIE_NAME
from src.core.interfaces import AuthError
from src.services.threads import ThreadFactory


class TestStaticProvider(unittest.IsolatedAsyncioTestCase):
    async def test_get_cookies_returns_active(self):
        provider = StaticSessionCookieProvider(["tok1", "tok2"])
        self.assertEqual(await provider.get_cookies(), {SESSION_COOKIE_NAME: "tok1"})

    async def test_invalidate_rotates_and_wraps(self):
        provider = StaticSessionCookieProvider(["tok1", "tok2"])
        provider.invalidate()
        self.assertEqual((await provider.get_cookies())[SESSION_COOKIE_NAME], "tok2")
        provider.invalidate()
        self.assertEqual((await provider.get_cookies())[SESSION_COOKIE_NAME], "tok1")

    async def test_no_sessions_raises(self):
        with self.assertRaises(ConnectionError):
            await StaticSessionCookieProvider([]).get_cookies()

    def test_clear_is_a_noop(self):
        self.assertFalse(StaticSessionCookieProvider(["t"]).clear_cookies())

    def test_count(self):
        self.assertEqual(StaticSessionCookieProvider(["a", "b", "c"]).count(), 3)

    async def test_cooldown_skips_quarantined_session(self):
        provider = StaticSessionCookieProvider(["tok1", "tok2"])
        provider.mark_cooldown("tok1", duration_seconds=100)
        self.assertEqual((await provider.get_cookies())[SESSION_COOKIE_NAME], "tok2")

    async def test_all_quarantined_still_serves_a_session(self):
        # Better to try a cooling-down account than to fail the request outright.
        provider = StaticSessionCookieProvider(["tok1", "tok2"])
        provider.mark_cooldown("tok1", duration_seconds=100)
        provider.mark_cooldown("tok2", duration_seconds=100)
        self.assertIn("tok", (await provider.get_cookies())[SESSION_COOKIE_NAME])

    def test_status_masks_tokens(self):
        provider = StaticSessionCookieProvider(["secret-token-1234"])
        status = provider.list_status()[0]
        self.assertEqual(status["token"], "…1234")
        self.assertNotIn("secret", status["token"])
        self.assertEqual(status["status"], "active")
        self.assertTrue(status["is_current"])


class TestThreadFactoryRotation(unittest.IsolatedAsyncioTestCase):
    async def test_rotates_to_the_next_account_on_auth_error(self):
        provider = StaticSessionCookieProvider(["accA", "accB"])
        backend = MagicMock()
        calls = {"n": 0}

        async def create(model, system_prompt, cookies, session_id=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise AuthError("session dead")
            return "T2"

        backend.create_thread = create
        factory = ThreadFactory(provider, backend)
        cookies = await provider.get_cookies()
        thread_id, used = await factory.create("opus-latest", "", None, cookies)

        self.assertEqual(thread_id, "T2")
        self.assertEqual(calls["n"], 2)  # retried after rotation
        self.assertEqual(used[SESSION_COOKIE_NAME], "accB")  # rotated account

    async def test_auth_error_propagates_once_accounts_run_out(self):
        provider = StaticSessionCookieProvider(["only"])
        backend = MagicMock()

        async def create(model, system_prompt, cookies, session_id=None):
            raise AuthError("session dead")

        backend.create_thread = create
        factory = ThreadFactory(provider, backend)

        with self.assertRaises(AuthError):
            await factory.create("opus-latest", "", None, await provider.get_cookies())

    async def test_account_count_defaults_to_one_without_a_provider_count(self):
        provider = MagicMock(spec=[])  # no `count` attribute
        self.assertEqual(ThreadFactory(provider, MagicMock()).account_count(), 1)


if __name__ == "__main__":
    unittest.main()
