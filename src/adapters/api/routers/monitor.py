"""The live monitoring dashboard and the JSON feed behind it.

Both are unauthenticated on purpose — the proxy binds to localhost by default
and the payload carries no secrets (session tokens appear only as last-4
fingerprints).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from src.core import config
from src.core.stats import STATS
from src.adapters.api.deps import cookie_provider
from src.services import accounts

router = APIRouter()

_DASHBOARD = Path(__file__).resolve().parent.parent / "static" / "dashboard.html"

# Recent-request rows shown in the dashboard's activity log.
_RECENT_LIMIT = 15


@router.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    """Serve the real-time mini monitoring panel."""
    return _DASHBOARD.read_text(encoding="utf-8")


@router.get("/api/live-status")
async def live_status() -> Dict[str, Any]:
    """Runtime activity, session health and the most recent requests."""
    summary = STATS.summary()
    sessions = await _sessions_with_accounts()
    return {
        "status": "online",
        **summary,
        "sessions": sessions,
        "anti_detection": {
            "tls_impersonate": config.TLS_IMPERSONATE,
            "ua_rotation": config.ENABLE_UA_ROTATION,
            "human_jitter": config.ENABLE_HUMAN_JITTER,
            "session_cooldown": config.ENABLE_SESSION_COOLDOWN,
        },
        "recent_requests": STATS.recent()[:_RECENT_LIMIT],
    }


async def _sessions_with_accounts() -> List[Dict[str, Any]]:
    """Session status rows, annotated with the account each token belongs to.

    The account lookup is cached in :mod:`src.services.accounts`; the dashboard
    polls every second and must not turn that into upstream traffic.
    """
    sessions = cookie_provider.list_status() if hasattr(cookie_provider, "list_status") else []
    tokens = config.load_sessions()
    if not sessions or not tokens:
        return sessions

    verified = await accounts.verify_all(tokens)
    by_mask = {v.get("session"): v for v in verified if v.get("valid")}
    for entry in sessions:
        match = by_mask.get(entry.get("token"))
        if match:
            entry["email"] = match.get("email")
            entry["name"] = match.get("name")
    return sessions
