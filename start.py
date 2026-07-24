#!/usr/bin/env python3
"""Interactive launcher entry point for the Hyperagent Local Proxy."""

import os
import sys

# Ensure the project root is importable no matter the current working directory.
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.adapters.browser.playwright_cdp import PlaywrightCDPCookieProvider
from src.services.auth_service import AuthService
from src.adapters.launcher.processes import ProcessManager
from src.launcher_orchestrator import LauncherOrchestrator
from src.core.logging_config import setup_logging


def main():
    setup_logging()
    # Instantiate clean-architecture adapters and services.
    cookie_provider = PlaywrightCDPCookieProvider()
    auth_service = AuthService(cookie_provider)
    process_manager = ProcessManager()

    orchestrator = LauncherOrchestrator(auth_service, process_manager)
    orchestrator.run()


if __name__ == "__main__":
    main()
