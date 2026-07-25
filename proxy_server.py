"""Alias entry point kept for backwards compatibility.

``python proxy_server.py`` and ``uvicorn proxy_server:app`` both still work;
:mod:`src.server` is the real implementation and ``start.py`` the documented
entry point.
"""

from src.server import app, main  # noqa: F401  (app re-exported for uvicorn)

if __name__ == "__main__":
    main()
