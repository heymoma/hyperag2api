#!/usr/bin/env python3
"""Entry point for the Hyperagent Local Proxy (browserless).

Loads config (config.yaml / env) and runs the OpenAI-compatible server. Sessions
are verified on startup and each account's remaining balance is logged.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.core.logging_config import setup_logging  # noqa: E402

logger = setup_logging()

from src.core import config  # noqa: E402
from src.adapters.api.routes import app  # noqa: E402  (import after logging setup)


def main() -> None:
    import uvicorn

    if not config.load_sessions():
        logger.warning(
            "No sessions configured. Copy config.example.yaml to config.yaml and add "
            "your '__Host-hyperagent_session' token(s), or set HYPERAGENT_SESSION."
        )
    logger.info(
        "Starting hyperag2api on http://%s:%s  (docs: /docs · dashboard: /dashboard · accounts: /accounts)",
        config.HOST, config.PORT,
    )
    # log_config=None keeps our unified logging; the app startup hook verifies sessions.
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_config=None, access_log=True)


if __name__ == "__main__":
    main()
