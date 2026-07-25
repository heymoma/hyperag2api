#!/usr/bin/env python3
"""Entry point for the Hyperagent Local Proxy (browserless).

Loads config (config.yaml / env) and runs the OpenAI-compatible server; sessions
are verified on startup. All the work lives in :mod:`src.server`.

    python3 start.py
"""

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.server import app, main  # noqa: E402,F401  (app re-exported for uvicorn)

if __name__ == "__main__":
    main()
