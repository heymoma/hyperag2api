"""Entry point for the FastAPI proxy server.

Run standalone with ``python proxy_server.py`` (honours HOST/PORT/LOG_LEVEL env
vars) or let the interactive launcher (``start.py``) spawn it for you.
"""

import os

# Configure logging BEFORE importing the app so import-time logs are captured
# and uvicorn's loggers inherit our handlers.
from src.core.logging_config import setup_logging

logger = setup_logging()

from src.adapters.api.routes import app  # noqa: E402  (import after logging setup)


def main() -> None:
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("PORT", "8000"))
    except ValueError:
        port = 8000

    logger.info("Starting local Hyperagent proxy on http://%s:%s", host, port)
    # log_config=None => uvicorn does not override our unified logging config.
    uvicorn.run(app, host=host, port=port, log_config=None, access_log=True)


if __name__ == "__main__":
    main()
