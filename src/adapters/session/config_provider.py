"""A browserless CookieProvider backed by config-provided session token(s).

Instead of driving a browser over CDP to capture the ``__Host-hyperagent_session``
cookie, the token(s) are supplied directly via config (env or file). No Playwright
required. With multiple tokens, :meth:`invalidate` rotates to the next one (useful
for spreading load / recovering from a dead session).
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional

from src.core.interfaces import CookieProvider
from src.core.config import HYPERAGENT_BASE_URL
from src.core.logging_config import get_logger

logger = get_logger("session")

COOKIE_NAME = "__Host-hyperagent_session"


class StaticSessionCookieProvider(CookieProvider):
    def __init__(self, sessions: List[str]):
        self._sessions = [s for s in (sessions or []) if s]
        self._idx = 0
        self._lock = threading.Lock()
        if not self._sessions:
            logger.warning(
                "No Hyperagent session configured. Set HYPERAGENT_SESSION / "
                "HYPERAGENT_SESSIONS or SESSIONS_FILE."
            )
        else:
            logger.info("Config-based auth: %d session(s) loaded (browserless).", len(self._sessions))

    @property
    def active(self) -> Optional[str]:
        with self._lock:
            return self._sessions[self._idx] if self._sessions else None

    def count(self) -> int:
        return len(self._sessions)

    async def get_cookies(self, url: str = HYPERAGENT_BASE_URL) -> Dict[str, str]:
        token = self.active
        if not token:
            raise ConnectionError(
                "No Hyperagent session configured. Set HYPERAGENT_SESSION (the "
                "'__Host-hyperagent_session' cookie value) or SESSIONS_FILE."
            )
        return {COOKIE_NAME: token}

    def invalidate(self) -> None:
        """Rotate to the next configured session (e.g. after an auth failure)."""
        with self._lock:
            if len(self._sessions) > 1:
                self._idx = (self._idx + 1) % len(self._sessions)
                logger.info("Rotated to configured session #%d.", self._idx + 1)

    def clear_cookies(self) -> bool:
        """No-op for config sessions (there is no browser context to clear)."""
        logger.info("clear_cookies is a no-op in config-session mode.")
        return False
