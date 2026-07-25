"""Session verification and account lookup via Hyperagent's ``/api/auth/me``.

Used to validate config-provided session tokens and surface which account each
one belongs to (email/name). Balance/credits are intentionally NOT reported —
Hyperagent does not expose them through a public JSON endpoint (verified), so we
never fabricate a number.

:func:`verify_all` caches its results. The live dashboard polls once a second,
and an uncached lookup would mean one upstream auth request per session per
second — both wasteful and exactly the traffic pattern the anti-detection layer
exists to avoid.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

from src.core import config
from src.core.config import SESSION_COOKIE_NAME as COOKIE_NAME
from src.core.logging_config import get_logger
from src.infra import get_async_session, get_endpoint_headers

logger = get_logger("accounts")

_VERIFY_TIMEOUT = 15.0

# token -> (expires_at, result)
_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
# Serialises refreshes so concurrent dashboard polls issue one upstream request.
# Created lazily: Python 3.9 cannot build a Lock outside a running loop.
_refresh_lock: Optional[asyncio.Lock] = None


def _lock() -> asyncio.Lock:
    global _refresh_lock
    if _refresh_lock is None:
        _refresh_lock = asyncio.Lock()
    return _refresh_lock


def mask(token: str) -> str:
    """Last-4 fingerprint of a token — never log/return the full secret."""
    if not token:
        return "(empty)"
    return "…" + token[-4:] if len(token) > 4 else "…"


async def verify_session(token: str) -> Dict[str, Any]:
    """Return {valid, email, name, userId, session} for one session token.

    Always hits the network; use :func:`verify_all` for the cached path.
    """
    try:
        async with get_async_session(
            cookies={COOKIE_NAME: token},
            headers=get_endpoint_headers("auth_me"),
            timeout=_VERIFY_TIMEOUT,
        ) as client:
            resp = await client.get(config.HYPERAGENT_AUTH_ME_API)
            if resp.status_code != 200:
                return {"valid": False, "session": mask(token), "status": resp.status_code}
            body = resp.json()
            return {
                "valid": True,
                "session": mask(token),
                "email": body.get("email"),
                "name": body.get("name"),
                "userId": body.get("userId"),
                "timezone": body.get("timezone"),
            }
    except Exception as exc:
        return {"valid": False, "session": mask(token), "error": str(exc)[:100]}


async def verify_all(
    tokens: List[str], ttl: Optional[float] = None, force: bool = False
) -> List[Dict[str, Any]]:
    """Verify every token, reusing results younger than ``ttl`` seconds.

    ``force`` bypasses the cache (used at startup, where a fresh answer matters
    more than the extra requests).
    """
    if not tokens:
        return []

    ttl = config.ACCOUNT_CACHE_TTL if ttl is None else ttl
    configured = set(tokens)

    async with _lock():
        # Forget tokens that are no longer configured, so the cache cannot grow
        # without bound as sessions are rotated in and out.
        for token in [t for t in _cache if t not in configured]:
            _cache.pop(token, None)

        now = time.time()
        stale = [t for t in tokens if force or _cache.get(t, (0.0, None))[0] <= now]
        if stale:
            fresh = await asyncio.gather(*(verify_session(t) for t in stale))
            for token, result in zip(stale, fresh):
                _cache[token] = (now + ttl, result)

        return [_cache[t][1] for t in tokens if t in _cache]


def invalidate_cache() -> None:
    """Forget all cached verifications (e.g. after a session rotation)."""
    _cache.clear()
