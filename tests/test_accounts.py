"""Tests for session verification and its cache.

The dashboard polls /api/live-status once a second. Without caching that becomes
one upstream /api/auth/me request per session per second — wasteful, and exactly
the traffic pattern the anti-detection layer exists to avoid.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.services import accounts


class TestVerifySession(unittest.IsolatedAsyncioTestCase):
    def _client(self, status, payload=None):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = payload or {}
        client = AsyncMock()
        client.get.return_value = resp
        cm = MagicMock()
        cm.__aenter__.return_value = client
        return cm

    async def test_valid_session_returns_the_account(self):
        cm = self._client(200, {"email": "a@b.com", "name": "Neo", "userId": "u1", "timezone": "UTC"})
        with patch("httpx.AsyncClient", return_value=cm):
            info = await accounts.verify_session("secrettok9999")
        self.assertTrue(info["valid"])
        self.assertEqual(info["email"], "a@b.com")
        self.assertEqual(info["session"], "…9999")   # masked, never the full token

    async def test_rejected_session_reports_the_status(self):
        with patch("httpx.AsyncClient", return_value=self._client(401)):
            info = await accounts.verify_session("tok")
        self.assertFalse(info["valid"])
        self.assertEqual(info["status"], 401)

    async def test_network_failure_is_not_fatal(self):
        with patch("httpx.AsyncClient", side_effect=OSError("boom")):
            info = await accounts.verify_session("tok")
        self.assertFalse(info["valid"])
        self.assertIn("boom", info["error"])

    def test_mask_never_leaks_a_short_token(self):
        self.assertEqual(accounts.mask(""), "(empty)")
        self.assertEqual(accounts.mask("ab"), "…")
        self.assertEqual(accounts.mask("abcdef"), "…cdef")


class TestVerifyAllCaching(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        accounts.invalidate_cache()

    def tearDown(self):
        accounts.invalidate_cache()

    def _stub(self):
        return AsyncMock(side_effect=lambda token: {"valid": True, "session": accounts.mask(token)})

    async def test_repeat_calls_hit_the_cache(self):
        stub = self._stub()
        with patch.object(accounts, "verify_session", stub):
            first = await accounts.verify_all(["tokAAAA"], ttl=60)
            second = await accounts.verify_all(["tokAAAA"], ttl=60)

        self.assertEqual(stub.await_count, 1)
        self.assertEqual(first, second)
        self.assertEqual(first[0]["session"], "…AAAA")

    async def test_expired_entries_are_refetched(self):
        stub = self._stub()
        with patch.object(accounts, "verify_session", stub):
            await accounts.verify_all(["tok1"], ttl=0)
            await accounts.verify_all(["tok1"], ttl=0)
        self.assertEqual(stub.await_count, 2)

    async def test_force_bypasses_the_cache(self):
        stub = self._stub()
        with patch.object(accounts, "verify_session", stub):
            await accounts.verify_all(["tok1"], ttl=60)
            await accounts.verify_all(["tok1"], ttl=60, force=True)
        self.assertEqual(stub.await_count, 2)

    async def test_only_new_tokens_are_fetched(self):
        stub = self._stub()
        with patch.object(accounts, "verify_session", stub):
            await accounts.verify_all(["tok1"], ttl=60)
            result = await accounts.verify_all(["tok1", "tok2"], ttl=60)

        self.assertEqual(stub.await_count, 2)  # tok1 once, tok2 once
        self.assertEqual(len(result), 2)

    async def test_removed_tokens_are_forgotten(self):
        stub = self._stub()
        with patch.object(accounts, "verify_session", stub):
            await accounts.verify_all(["tok1", "tok2"], ttl=60)
            result = await accounts.verify_all(["tok2"], ttl=60)
            # tok1 is no longer configured, so it must not linger in the cache.
            self.assertEqual(len(result), 1)
            self.assertNotIn("tok1", accounts._cache)

    async def test_empty_token_list_makes_no_requests(self):
        stub = self._stub()
        with patch.object(accounts, "verify_session", stub):
            self.assertEqual(await accounts.verify_all([]), [])
        self.assertEqual(stub.await_count, 0)


if __name__ == "__main__":
    unittest.main()
