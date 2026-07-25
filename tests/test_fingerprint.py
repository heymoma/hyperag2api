"""Tests for anti-detection suite: browser headers, human jitter, and TLS impersonation."""

import time
import unittest
from unittest.mock import patch, AsyncMock

from src.core import config
from src.infra.fingerprint import (
    BROWSER_PROFILES,
    apply_human_jitter,
    get_endpoint_headers,
    get_random_browser_headers,
)
from src.infra.http_client import get_async_session


class TestAntiDetection(unittest.IsolatedAsyncioTestCase):
    def test_random_browser_headers_enabled(self):
        with patch.object(config, "ENABLE_UA_ROTATION", True):
            headers = get_random_browser_headers({"Custom-Header": "val"})
            self.assertIn("User-Agent", headers)
            self.assertIn("Custom-Header", headers)
            self.assertEqual(headers["Custom-Header"], "val")
            # Verify it picked one of the browser profiles
            user_agents = [p["User-Agent"] for p in BROWSER_PROFILES]
            self.assertIn(headers["User-Agent"], user_agents)

    def test_random_browser_headers_disabled(self):
        with patch.object(config, "ENABLE_UA_ROTATION", False):
            headers = get_random_browser_headers({"User-Agent": "Custom-UA"})
            self.assertEqual(headers["User-Agent"], "Custom-UA")

    def test_endpoint_headers(self):
        h_new = get_endpoint_headers("new_thread")
        self.assertIn("/threads/new", h_new["Referer"])
        self.assertEqual(h_new["Sec-Fetch-Site"], "same-origin")

        h_chat = get_endpoint_headers("chat", thread_id="t-123")
        self.assertIn("/threads/t-123", h_chat["Referer"])

        h_auth = get_endpoint_headers("auth_me")
        self.assertEqual(h_auth["Referer"], f"{config.HYPERAGENT_BASE_URL}/")

    async def test_apply_human_jitter_enabled(self):
        with patch.object(config, "ENABLE_HUMAN_JITTER", True):
            t0 = time.time()
            await apply_human_jitter(min_ms=50, max_ms=100)
            elapsed = time.time() - t0
            self.assertGreaterEqual(elapsed, 0.04)  # at least ~50ms

    async def test_apply_human_jitter_disabled(self):
        with patch.object(config, "ENABLE_HUMAN_JITTER", False):
            t0 = time.time()
            await apply_human_jitter(min_ms=500, max_ms=1000)
            elapsed = time.time() - t0
            self.assertLess(elapsed, 0.1)  # instant

    async def test_async_session_httpx_mocked(self):
        # When httpx is mocked (as in unit tests), get_async_session yields UnifiedSession with httpx
        with patch("httpx.AsyncClient") as mock_client:
            mock_inst = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_inst
            mock_inst.get.return_value = AsyncMock(status_code=200, json=lambda: {"ok": True})

            async with get_async_session() as session:
                resp = await session.get("https://example.com")
                self.assertEqual(resp.status_code, 200)
                self.assertFalse(session.is_curl)


if __name__ == "__main__":
    unittest.main()
