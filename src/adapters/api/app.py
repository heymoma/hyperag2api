"""FastAPI application assembly.

:func:`create_app` builds a fresh instance from the routers; the module-level
``app`` is the one uvicorn serves. Startup work lives in a lifespan handler so
that failures surface before the first request is accepted.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from src.core import config
from src.core.logging_config import get_logger
from src.adapters.api.routers import chat, health, monitor
from src.services import accounts

logger = get_logger("api")

APP_TITLE = "Hyperagent Local API Proxy"


async def verify_configured_sessions() -> None:
    """Log which configured sessions are usable, and which are not."""
    sessions = config.load_sessions()
    if not sessions:
        logger.warning(
            "No sessions configured — add 'sessions:' to config.yaml or set HYPERAGENT_SESSION."
        )
        return

    # force=True: startup is the one moment a cached answer would be misleading.
    for info in await accounts.verify_all(sessions, force=True):
        if info.get("valid"):
            logger.info(
                "Session %s OK — %s <%s>", info["session"], info.get("name"), info.get("email")
            )
        else:
            logger.warning(
                "Session %s INVALID (%s)",
                info.get("session"), info.get("status") or info.get("error"),
            )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await verify_configured_sessions()
    yield


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    app = FastAPI(title=APP_TITLE, lifespan=lifespan)
    app.include_router(monitor.router)
    app.include_router(health.router)
    app.include_router(chat.router)
    return app


app = create_app()
