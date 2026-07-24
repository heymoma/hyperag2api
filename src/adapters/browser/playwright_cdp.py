"""Cookie provider backed by a running browser via Chrome DevTools Protocol.

Cookies are cached for a short TTL (``COOKIE_TTL_SECONDS``) so a burst of
requests doesn't spin up a fresh Playwright/CDP connection every time — the
single biggest per-request cost and failure point in the original proxy. The
cache is dropped automatically on auth failures via :meth:`invalidate`.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional, Tuple

from playwright.async_api import async_playwright
from playwright.sync_api import sync_playwright

from src.core.interfaces import CookieProvider
from src.core.config import CDP_URL, HYPERAGENT_BASE_URL, COOKIE_TTL_SECONDS
from src.core.logging_config import get_logger

logger = get_logger("cdp")


class PlaywrightCDPCookieProvider(CookieProvider):
    """Retrieves and clears browser cookies using Playwright over CDP.

    Note: connections are torn down by exiting the Playwright context manager.
    We deliberately never call ``browser.close()`` on a CDP connection, since
    that could close the user's debug browser out from under them.
    """

    def __init__(self, cdp_url: str = CDP_URL, cookie_ttl: float = COOKIE_TTL_SECONDS):
        self.cdp_url = cdp_url
        self.cookie_ttl = cookie_ttl
        self._cache_lock = threading.Lock()
        self._cache: Dict[str, Tuple[float, Dict[str, str]]] = {}

    def _connection_error(self, exc: Exception) -> ConnectionError:
        logger.error("Error connecting to browser CDP at %s: %s", self.cdp_url, exc)
        return ConnectionError(
            f"Could not connect to the active browser session at {self.cdp_url}. "
            "Please ensure the launcher's debug browser is running with remote "
            "debugging enabled (and that you are logged in to Hyperagent)."
        )

    def _cache_get(self, url: str) -> Optional[Dict[str, str]]:
        if self.cookie_ttl <= 0:
            return None
        with self._cache_lock:
            hit = self._cache.get(url)
            if not hit:
                return None
            ts, cookies = hit
            if (time.time() - ts) > self.cookie_ttl:
                self._cache.pop(url, None)
                return None
            return dict(cookies)

    def _cache_put(self, url: str, cookies: Dict[str, str]) -> None:
        if self.cookie_ttl <= 0 or not cookies:
            return
        with self._cache_lock:
            self._cache[url] = (time.time(), dict(cookies))

    def invalidate(self) -> None:
        """Forget cached cookies (e.g. after a 401) so the next fetch re-reads."""
        with self._cache_lock:
            self._cache.clear()
        logger.debug("Cookie cache invalidated.")

    async def get_cookies(self, url: str = HYPERAGENT_BASE_URL) -> Dict[str, str]:
        """Fetch cookies from the running browser instance via CDP (cached)."""
        cached = self._cache_get(url)
        if cached is not None:
            logger.debug("Using cached cookies for %s (%d).", url, len(cached))
            return cached
        try:
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(self.cdp_url)
                if not browser.contexts:
                    raise ConnectionError(
                        "The debug browser has no open context. Open a tab and "
                        "log in to Hyperagent, then retry."
                    )
                context = browser.contexts[0]
                cookies = await context.cookies(url)
                cookie_dict = {c["name"]: c["value"] for c in cookies}
                if not cookie_dict:
                    logger.warning(
                        "Connected to the browser but found no cookies for %s. "
                        "Are you logged in to Hyperagent?",
                        url,
                    )
                else:
                    logger.debug("Fetched %d cookies from CDP session.", len(cookie_dict))
                    self._cache_put(url, cookie_dict)
                return cookie_dict
        except ConnectionError:
            raise
        except Exception as exc:
            raise self._connection_error(exc)

    def clear_cookies(self) -> bool:
        """Clear cookies in the active browser context synchronously."""
        self.invalidate()
        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(self.cdp_url)
                if not browser.contexts:
                    logger.warning("No browser context available to clear cookies.")
                    return False
                context = browser.contexts[0]
                context.clear_cookies()
                # Refresh open pages (or open one) at the login page.
                if context.pages:
                    for page in context.pages:
                        try:
                            page.goto(f"{HYPERAGENT_BASE_URL}/")
                        except Exception as nav_exc:
                            logger.debug("Page navigation after clear failed: %s", nav_exc)
                else:
                    page = context.new_page()
                    page.goto(f"{HYPERAGENT_BASE_URL}/")
                logger.info("Successfully cleared cookies and redirected to Hyperagent login.")
                return True
        except Exception as exc:
            logger.error("CDP Cookie Clear Error: %s", exc)
            return False
