"""Composition root and shared FastAPI dependencies.

The concrete adapters are wired together here — once, at import time — and the
routers reach them through this module rather than constructing their own. That
keeps the object graph in one readable place and lets tests swap a collaborator
by patching a single attribute.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Header, HTTPException

from src.core import config
from src.core.logging_config import get_logger
from src.adapters.backend.hyperagent_client import HyperagentClient
from src.adapters.session.config_provider import StaticSessionCookieProvider
from src.services.chat_service import ChatService

logger = get_logger("api")


def build_cookie_provider() -> StaticSessionCookieProvider:
    """Build the browserless session provider from config."""
    sessions = config.load_sessions()
    if not sessions:
        logger.warning(
            "No sessions configured — add 'sessions:' to config.yaml or set HYPERAGENT_SESSION."
        )
    return StaticSessionCookieProvider(sessions)


# --- the application's object graph ---------------------------------------- #
cookie_provider = build_cookie_provider()
chat_backend = HyperagentClient()
chat_service = ChatService(cookie_provider, chat_backend)


async def verify_api_key(authorization: Optional[str] = Header(None)) -> None:
    """Enforce the proxy API key, when one is configured.

    Reads ``config.PROXY_API_KEY`` at call time so the key can be changed (or
    patched in tests) without rebuilding the routes.
    """
    expected = config.PROXY_API_KEY
    if not expected:
        return
    if not authorization:
        raise HTTPException(status_code=401, detail="Unauthorized: Missing API Key")
    if authorization.replace("Bearer ", "").strip() != expected:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid API Key")
