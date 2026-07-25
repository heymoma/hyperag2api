"""Readiness probe.

Reports ``degraded`` (503) rather than ``ok`` whenever the currently active
Hyperagent session cannot be verified — a proxy with dead cookies is running but
cannot serve completions, and orchestrators need to see that difference.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src.core import config
from src.core.config import SESSION_COOKIE_NAME
from src.core.stats import STATS
from src.adapters.api.deps import cookie_provider
from src.services import accounts

router = APIRouter()


@router.get("/health")
async def health() -> JSONResponse:
    """Sessions configured + current account reachability."""
    session_ok = False
    detail = ""
    account = None

    try:
        cookies = await cookie_provider.get_cookies()
        token = cookies.get(SESSION_COOKIE_NAME, "")
        if token:
            info = await accounts.verify_session(token)
            session_ok = bool(info.get("valid"))
            if session_ok:
                account = {"email": info.get("email"), "name": info.get("name")}
            else:
                detail = f"Session invalid ({info.get('status') or info.get('error')})."
    except Exception as exc:
        detail = str(exc)

    status = "ok" if session_ok else "degraded"
    return JSONResponse(
        status_code=200 if session_ok else 503,
        content={
            "status": status,
            "sessions_configured": len(config.load_sessions()),
            "session_valid": session_ok,
            "account": account,
            "detail": detail,
            **STATS.summary(),
        },
    )
