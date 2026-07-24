"""Process management: launching the debug browser and the proxy server."""

from __future__ import annotations

import os
import sys
import time
import socket
import subprocess
from typing import Dict, List, Optional

from src.core.logging_config import get_logger
from src.core.config import HYPERAGENT_BASE_URL, CDP_HOST
from src.adapters.launcher.system import (
    get_platform,
    is_windows,
    is_macos,
    is_cdp_endpoint_ready,
)

logger = get_logger("processes")


def _app_data_dir() -> str:
    """Return a per-user, app-specific data directory for the CDP profile."""
    home = os.path.expanduser("~")
    if is_windows():
        base = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
    elif is_macos():
        base = os.path.join(home, "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME", os.path.join(home, ".local", "share"))
    return os.path.join(base, "hyperagent-proxy")


class ProcessManager:
    """Manages spawning, tracking, and stopping external background processes
    (the debug browser and the FastAPI proxy server)."""

    def __init__(self, browser_cdp_port: int = 9222, cdp_host: str = CDP_HOST):
        self.browser_cdp_port = browser_cdp_port
        self.cdp_host = cdp_host
        self.browser_proc: Optional[subprocess.Popen] = None
        self.proxy_proc: Optional[subprocess.Popen] = None
        self._user_data_dir: Optional[str] = None

    # -- CDP readiness --------------------------------------------------------
    def check_cdp_port_active(self) -> bool:
        """Check if the remote debugging port is already open and listening."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            return s.connect_ex(("127.0.0.1", self.browser_cdp_port)) == 0

    def wait_for_cdp(self, timeout: float = 20.0, interval: float = 0.5) -> bool:
        """Poll the DevTools endpoint until it answers or the timeout elapses."""
        attempts = max(1, int(timeout / interval))
        for _ in range(attempts):
            if is_cdp_endpoint_ready(host="127.0.0.1", port=self.browser_cdp_port, timeout=interval):
                return True
            time.sleep(interval)
        return False

    # -- Browser --------------------------------------------------------------
    def user_data_dir(self) -> str:
        """Persistent, dedicated CDP profile directory (created on first use)."""
        if self._user_data_dir is None:
            path = os.path.join(_app_data_dir(), "cdp-profile")
            try:
                os.makedirs(path, exist_ok=True)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Could not create profile dir %s: %s", path, exc)
            self._user_data_dir = path
        return self._user_data_dir

    def _build_browser_args(self, browser_cmd: str, family: str) -> List[str]:
        """Build launch args. A dedicated user-data-dir is essential: without
        it, launching Chrome/Edge while an instance is already running just
        opens a tab in the existing process and the debug port never opens."""
        if family == "firefox":
            # Firefox CDP is experimental; best-effort flags only.
            return [browser_cmd, f"--remote-debugging-port={self.browser_cdp_port}"]

        return [
            browser_cmd,
            f"--remote-debugging-port={self.browser_cdp_port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={self.user_data_dir()}",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-sync",
            "--disable-session-crashed-bubble",
            "--disable-infobars",
            f"{HYPERAGENT_BASE_URL}/",
        ]

    def launch_browser(self, browser_cmd: str, family: str = "chromium") -> bool:
        """Launch the specified browser with remote debugging if not already running."""
        try:
            if self.check_cdp_port_active():
                logger.info(
                    "Port %s is already active. Skipping browser launch.",
                    self.browser_cdp_port,
                )
                return True

            popen_kwargs: Dict[str, object] = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
            if is_windows():
                # CREATE_NEW_CONSOLE = 0x00000010
                popen_kwargs["creationflags"] = 0x00000010
            else:
                popen_kwargs["preexec_fn"] = os.setpgrp

            args = self._build_browser_args(browser_cmd, family)
            logger.info(
                "Spawning browser (%s) on CDP port %s with dedicated profile.",
                browser_cmd,
                self.browser_cdp_port,
            )
            self.browser_proc = subprocess.Popen(args, **popen_kwargs)

            # Wait for DevTools to actually come up rather than a blind sleep.
            if family == "chromium":
                if self.wait_for_cdp(timeout=20.0):
                    logger.info("CDP endpoint is ready on port %s.", self.browser_cdp_port)
                else:
                    logger.warning(
                        "CDP endpoint did not respond within timeout on port %s; "
                        "continuing anyway.",
                        self.browser_cdp_port,
                    )
            else:
                time.sleep(2.0)
            return True
        except Exception as e:
            logger.error("Error launching browser: %s", e)
            return False

    # -- Proxy server ---------------------------------------------------------
    def start_proxy_server(
        self,
        port: int,
        custom_key: str,
        app_dir: str,
        env_vars: Dict[str, str],
        host: str = "0.0.0.0",
    ) -> subprocess.Popen:
        """Start the FastAPI proxy server as an unbuffered subprocess.

        Runs ``proxy_server.py`` directly (rather than ``-m uvicorn``) so the
        application's own logging configuration governs uvicorn's loggers too.
        """
        self.terminate_proxy_server()

        env = os.environ.copy()
        env.update(env_vars or {})
        env["PORT"] = str(port)
        env["HOST"] = env.get("HOST", host)
        env["PYTHONUNBUFFERED"] = "1"  # live log tailing in the launcher UI
        if custom_key:
            env["PROXY_API_KEY"] = custom_key

        runner = os.path.join(app_dir, "proxy_server.py")
        cmd = [sys.executable, "-u", runner]

        logger.info("Spawning proxy server on %s:%s (dir=%s)", env["HOST"], port, app_dir)
        self.proxy_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # line-buffered
            cwd=app_dir,
            env=env,
        )
        return self.proxy_proc

    def terminate_proxy_server(self):
        """Terminate the active proxy server process gracefully."""
        if self.proxy_proc:
            try:
                self.proxy_proc.terminate()
                self.proxy_proc.wait(timeout=3.0)
            except Exception:
                try:
                    self.proxy_proc.kill()
                except Exception:
                    pass
            self.proxy_proc = None

    def terminate_all(self):
        """Clean up both browser and proxy subprocesses."""
        self.terminate_proxy_server()
        if self.browser_proc:
            try:
                self.browser_proc.terminate()
            except Exception:
                pass
            self.browser_proc = None
