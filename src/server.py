"""Process entry point: configure logging, then serve the app.

Logging is set up *before* the app is imported so that import-time messages are
captured and uvicorn's own loggers inherit our handlers. The thin ``start.py``
and ``proxy_server.py`` scripts at the repository root both delegate here.
"""

from __future__ import annotations

import os
import sys

from src.core.logging_config import setup_logging

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logger = setup_logging()

from src.core import config  # noqa: E402  (import after logging setup)
from src.adapters.api.app import app  # noqa: E402

__all__ = ["app", "main"]


def main() -> None:
    """Run the proxy with uvicorn using the configured host/port."""
    import uvicorn

    if not config.load_sessions():
        logger.warning(
            "No sessions configured. Copy config.example.yaml to config.yaml and add "
            "your '__Host-hyperagent_session' token(s), or set HYPERAGENT_SESSION."
        )

    logger.info(
        "Starting hyperag2api on http://%s:%s  (dashboard: / · docs: /docs · health: /health)",
        config.HOST, config.PORT,
    )
    # log_config=None keeps our unified logging; the app's lifespan verifies sessions.
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_config=None, access_log=True)


if __name__ == "__main__":
    main()
