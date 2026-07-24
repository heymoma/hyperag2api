"""A browserless CookieProvider backed by config-provided session token(s).

The token(s) are supplied via config (YAML/env/file) — no browser. With multiple
tokens it rotates:
- on an auth failure (:meth:`invalidate`) it advances to the next session, and
- optionally it orders sessions by **remaining credit** (balance rotation), so
  the account with the most budget is used and exhausted ones fall to the back.

Balance lookups are injected as ``balance_fn`` (token -> remaining USD | None) to
keep this adapter free of any service-layer import, and are throttled by a TTL.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Awaitable, Callable, Dict, List, Optional

from src.core.interfaces import CookieProvider
from src.core.config import HYPERAGENT_BASE_URL
from src.core.logging_config import get_logger

logger = get_logger("session")

COOKIE_NAME = "__Host-hyperagent_session"
BalanceFn = Callable[[str], Awaitable[Optional[float]]]


class StaticSessionCookieProvider(CookieProvider):
    def __init__(
        self,
        sessions: List[str],
        balance_fn: Optional[BalanceFn] = None,
        rotate_by_balance: bool = False,
        refresh_interval: float = 300.0,
    ):
        self._sessions = [s for s in (sessions or []) if s]
        self._idx = 0
        self._lock = threading.Lock()
        self._alock: Optional[asyncio.Lock] = None
        self._balance_fn = balance_fn
        self._rotate_by_balance = bool(rotate_by_balance and balance_fn)
        self._refresh_interval = refresh_interval
        self._last_refresh = 0.0
        self._balances: Dict[str, Optional[float]] = {}
        if not self._sessions:
            logger.warning(
                "No Hyperagent session configured. Add 'sessions:' to config.yaml, "
                "set HYPERAGENT_SESSION, or use SESSIONS_FILE / sessions.txt."
            )
        else:
            logger.info(
                "Config-based auth: %d session(s) loaded (browserless%s).",
                len(self._sessions), ", balance-rotation on" if self._rotate_by_balance else "",
            )

    def _get_alock(self) -> asyncio.Lock:
        if self._alock is None:
            self._alock = asyncio.Lock()
        return self._alock

    @property
    def active(self) -> Optional[str]:
        with self._lock:
            return self._sessions[self._idx] if self._sessions else None

    def count(self) -> int:
        return len(self._sessions)

    async def _maybe_reorder(self) -> None:
        if not self._rotate_by_balance or len(self._sessions) < 2:
            return
        if (time.time() - self._last_refresh) < self._refresh_interval and self._last_refresh:
            return
        async with self._get_alock():
            if (time.time() - self._last_refresh) < self._refresh_interval and self._last_refresh:
                return
            ranked = []
            for tok in self._sessions:
                try:
                    rem = await self._balance_fn(tok)  # type: ignore[misc]
                except Exception:
                    rem = None
                self._balances[tok] = rem
                ranked.append((rem if isinstance(rem, (int, float)) else -1.0, tok))
            # Highest remaining credit first; unknowns/errors sink to the back.
            ranked.sort(key=lambda x: x[0], reverse=True)
            with self._lock:
                self._sessions = [tok for _, tok in ranked]
                self._idx = 0
            self._last_refresh = time.time()
            logger.info("Ranked %d session(s) by remaining credit (top≈$%.2f).",
                        len(ranked), max(r[0] for r in ranked))

    async def get_cookies(self, url: str = HYPERAGENT_BASE_URL) -> Dict[str, str]:
        if self._rotate_by_balance:
            await self._maybe_reorder()
        token = self.active
        if not token:
            raise ConnectionError(
                "No Hyperagent session configured. Add one to config.yaml "
                "('sessions:'), HYPERAGENT_SESSION, or sessions.txt."
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
        return False

    def balances(self) -> Dict[str, Optional[float]]:
        return dict(self._balances)
