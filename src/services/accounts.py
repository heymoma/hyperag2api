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
        async with httpx.AsyncClient(
            cookies=cookies, headers=config.DEFAULT_HEADERS, timeout=15.0
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


async def get_balance(token: str) -> Dict[str, Any]:
    """Fetch credit/consumption for a session (from /api/settings/billing/*)."""
    cookies = {"__Host-hyperagent_session": token}
    out: Dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(
            cookies=cookies, headers=config.DEFAULT_HEADERS, timeout=15.0
        ) as client:
            r = await client.get(config.HYPERAGENT_BILLING_CONSUMPTION_API)
            if r.status_code == 200:
                j = r.json()
                init = j.get("bonusCreditInitialUsd")
                used = j.get("bonusCreditUsedUsd")
                out["plan"] = j.get("planName")
                out["credit_initial_usd"] = init
                out["credit_used_usd"] = used
                if isinstance(init, (int, float)) and isinstance(used, (int, float)):
                    out["credit_remaining_usd"] = round(init - used, 2)
                out["total_cost_usd"] = j.get("totalCostUsd")
                out["cost_limit_usd"] = j.get("costLimitUsd")
                out["components"] = j.get("components")
            else:
                out["error"] = f"status {r.status_code}"
    except Exception as exc:
        out["error"] = str(exc)[:100]
    return out


async def account_summary(token: str) -> Dict[str, Any]:
    """Verify a session and, if valid, attach its credit balance."""
    info = await verify_session(token)
    if info.get("valid"):
        info["balance"] = await get_balance(token)
    return info


async def summaries(tokens: List[str]) -> List[Dict[str, Any]]:
    if not tokens:
        return []
    return list(await asyncio.gather(*[account_summary(t) for t in tokens]))


BALANCE_NOTE = (
    "Balance is the pay-as-you-go credit consumption from /api/settings/billing "
    "(initial bonus credit minus used). Figures are Hyperagent's own; the proxy adds nothing."
)
