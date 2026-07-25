"""Thread creation with account rotation.

A configured Hyperagent session can die at any moment (expired cookie, revoked
account). When that happens mid-create the request is still salvageable: rotate
to the next configured account and try again. Both the plain and the
tool-calling paths need that, so it lives in one place.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Dict, Optional, Tuple

from src.core.interfaces import AuthError, ChatBackend, CookieProvider
from src.core.logging_config import get_logger

logger = get_logger("threads")


def fire_interrupt(backend: ChatBackend, thread_id: Optional[str], cookies: Dict[str, str]) -> None:
    """Ask the backend to stop generating, without waiting for the answer.

    Called when the client disconnects — we are already unwinding, so the cancel
    is scheduled as a background task and its outcome ignored.
    """
    if not thread_id:
        return
    try:
        asyncio.get_event_loop().create_task(backend.interrupt(thread_id, cookies))
    except Exception:  # pragma: no cover - best-effort
        pass


class ThreadFactory:
    """Creates backend threads, rotating accounts on authentication failure."""

    def __init__(self, cookie_provider: CookieProvider, backend: ChatBackend) -> None:
        self.cookie_provider = cookie_provider
        self.backend = backend

    def account_count(self) -> int:
        """How many accounts are available to rotate through (at least 1).

        Providers are free not to implement ``count``; mocked ones may expose it
        as a coroutine, which we cannot await synchronously — treat both as one.
        """
        fn = getattr(self.cookie_provider, "count", None)
        if callable(fn) and not inspect.iscoroutinefunction(fn):
            try:
                return max(1, int(fn()))
            except Exception:
                return 1
        return 1

    async def create(
        self,
        model: str,
        system_prompt: str,
        session_id: Optional[str],
        cookies: Dict[str, str],
    ) -> Tuple[str, Dict[str, str]]:
        """Create a thread, returning (thread_id, cookies_that_worked).

        Retries once per configured account; the final failure propagates so the
        caller can surface it to the client.
        """
        attempts = self.account_count()
        last_error: Optional[Exception] = None

        for attempt in range(attempts):
            try:
                thread_id = await self.backend.create_thread(
                    model, system_prompt, cookies, session_id=session_id
                )
                return thread_id, cookies
            except AuthError as exc:
                last_error = exc
                if attempt >= attempts - 1:
                    raise
                logger.warning(
                    "Session auth failed; rotating account (%d/%d).", attempt + 1, attempts
                )
                self.cookie_provider.invalidate()
                cookies = await self.cookie_provider.get_cookies()

        raise last_error if last_error else RuntimeError("thread creation failed")
