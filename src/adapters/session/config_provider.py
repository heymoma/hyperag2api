"""A browserless CookieProvider backed by config-provided session token(s).

The token(s) are supplied via config (YAML/env/file) — no browser. With multiple
tokens it rotates on auth failures (:meth:`invalidate`) to advance to the next session.
"""

import time
import threading
from typing import Dict, List, Optional

from src.core.interfaces import CookieProvider
from src.core import config
from src.core.config import HYPERAGENT_BASE_URL
from src.core.logging_config import get_logger

logger = get_logger("session")

COOKIE_NAME = "__Host-hyperagent_session"


class StaticSessionCookieProvider(CookieProvider):
    def __init__(self, sessions: List[str]):
        self._sessions = [s for s in (sessions or []) if s]
        self._idx = 0
        self._lock = threading.Lock()
        self._cooldowns: Dict[str, float] = {}
        if not self._sessions:
            logger.warning(
                "No Hyperagent session configured. Add 'sessions:' to config.yaml, "
                "set HYPERAGENT_SESSION, or use SESSIONS_FILE / sessions.txt."
            )
        else:
            logger.info("Config-based auth: %d session(s) loaded (browserless).", len(self._sessions))

    def _get_healthy_idx(self) -> int:
        """Find the index of the first session not currently in cooldown."""
        if not self._sessions:
            return 0
        now = time.time()
        n = len(self._sessions)
        for i in range(n):
            cand_idx = (self._idx + i) % n
            tok = self._sessions[cand_idx]
            if self._cooldowns.get(tok, 0.0) <= now:
                return cand_idx
        # All sessions in cooldown: fallback to current index
        return self._idx

    @property
    def active(self) -> Optional[str]:
        with self._lock:
            if not self._sessions:
                return None
            idx = self._get_healthy_idx()
            return self._sessions[idx]

    def count(self) -> int:
        return len(self._sessions)

    def mark_cooldown(self, token: Optional[str] = None, duration_seconds: Optional[float] = None) -> None:
        """Put a session token into cooldown (circuit breaker for 429/401/403 errors)."""
        if not config.ENABLE_SESSION_COOLDOWN:
            return
        with self._lock:
            tok = token or (self._sessions[self._idx] if self._sessions else None)
            if not tok:
                return
            dur = duration_seconds if duration_seconds is not None else config.COOLDOWN_SECONDS
            self._cooldowns[tok] = time.time() + dur
            logger.warning("Session %s put in cooldown for %.0fs (circuit breaker).",
                           "…" + tok[-4:] if len(tok) > 4 else "…", dur)
            if len(self._sessions) > 1:
                self._idx = (self._idx + 1) % len(self._sessions)

    async def get_cookies(self, url: str = HYPERAGENT_BASE_URL) -> Dict[str, str]:
        token = self.active
        if not token:
            raise ConnectionError(
                "No Hyperagent session configured. Add one to config.yaml "
                "('sessions:'), HYPERAGENT_SESSION, or sessions.txt."
            )
        return {COOKIE_NAME: token}

    def invalidate(self) -> None:
        """Rotate to the next configured session and trigger cooldown on current."""
        with self._lock:
            if self._sessions:
                curr_tok = self._sessions[self._idx]
                if config.ENABLE_SESSION_COOLDOWN:
                    dur = config.COOLDOWN_SECONDS
                    self._cooldowns[curr_tok] = time.time() + dur
                    logger.warning("Session %s put in cooldown for %.0fs on invalidate.",
                                   "…" + curr_tok[-4:] if len(curr_tok) > 4 else "…", dur)
            if len(self._sessions) > 1:
                self._idx = (self._idx + 1) % len(self._sessions)
                logger.info("Rotated to configured session #%d.", self._idx + 1)

    def clear_cookies(self) -> bool:
        """No-op for config sessions (there is no browser context to clear)."""
        return False
