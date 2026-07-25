"""Session verification + account lookup via Hyperagent's /api/auth/me.

Used to validate config-provided session tokens and surface which account each
one belongs to (email/name). Balance/credits are intentionally NOT reported —
Hyperagent does not expose them through a public JSON endpoint (verified), so we
never fabricate a number.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import httpx

from src.core.anti_detection import get_async_session, get_endpoint_headers
from src.core import config
from src.core.logging_config import get_logger

logger = get_logger("accounts")


def mask(token: str) -> str:
    """Last-4 fingerprint of a token — never log/return the full secret."""
    if not token:
        return "(empty)"
    return "…" + token[-4:] if len(token) > 4 else "…"


async def verify_session(token: str) -> Dict[str, Any]:
    """Return {valid, email, name, userId, session} for one session token."""
    cookies = {"__Host-hyperagent_session": token}
    try:
        headers = get_endpoint_headers("auth_me")
        async with get_async_session(
            cookies=cookies, headers=headers, timeout=15.0
        ) as client:
            r = await client.get(config.HYPERAGENT_AUTH_ME_API)
            if r.status_code == 200:
                j = r.json()
                return {
                    "valid": True,
                    "session": mask(token),
                    "email": j.get("email"),
                    "name": j.get("name"),
                    "userId": j.get("userId"),
                    "timezone": j.get("timezone"),
                }
            return {"valid": False, "session": mask(token), "status": r.status_code}
    except Exception as exc:
        return {"valid": False, "session": mask(token), "error": str(exc)[:100]}


async def verify_all(tokens: List[str]) -> List[Dict[str, Any]]:
    if not tokens:
        return []
    return list(await asyncio.gather(*[verify_session(t) for t in tokens]))


