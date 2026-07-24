"""System utilities: platform, network, CDP and browser detection.

Browser detection covers three sources:
  * system-installed browsers (Windows / macOS / Linux),
  * Playwright-managed Chromium (queried from Playwright itself), and
  * a filesystem fallback scan of the Playwright browser cache.
"""

from __future__ import annotations

import glob
import os
import random
import shutil
import socket
import sys
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional

from src.core.logging_config import get_logger

logger = get_logger("system")


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------
def get_platform() -> str:
    """Return the normalised platform name: 'windows', 'macos' or 'linux'."""
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def is_windows() -> bool:
    return get_platform() == "windows"


def is_macos() -> bool:
    return get_platform() == "macos"


def is_linux() -> bool:
    return get_platform() == "linux"


def platform_label() -> str:
    """Human-friendly platform label for the launcher banner."""
    return {"windows": "Windows", "macos": "macOS", "linux": "Linux"}[get_platform()]


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------
def check_command(cmd: str) -> bool:
    """Check if a command is available on the system PATH."""
    return shutil.which(cmd) is not None


def get_local_ip() -> str:
    """Retrieve the machine's local IP on the active network interface."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_global_ip() -> str:
    """Retrieve the machine's public IP using an external lookup provider."""
    try:
        req = urllib.request.Request(
            "https://api.ipify.org",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        return urllib.request.urlopen(req, timeout=3.0).read().decode("utf-8").strip()
    except Exception:
        return "Unknown / Offline"


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Check if a TCP port is in use on the given host."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) == 0


def get_random_port(start: int = 39100, end: int = 48900) -> int:
    """Find a random unused TCP port in the specified range."""
    for _ in range(50):
        port = random.randint(start, end)
        if not is_port_in_use(port):
            return port
    return end  # Fallback


def is_cdp_endpoint_ready(host: str = "127.0.0.1", port: int = 9222, timeout: float = 1.0) -> bool:
    """Verify a real Chrome DevTools Protocol endpoint is answering.

    A listening TCP port is not proof that DevTools is live, so we query the
    ``/json/version`` metadata endpoint that every Chromium debugger exposes.
    """
    url = f"http://{host}:{port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Browser detection
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BrowserInfo:
    """A discovered browser the launcher can drive over CDP."""

    name: str
    command: str          # absolute executable path or a PATH command
    family: str           # "chromium" | "firefox"
    source: str = "system"  # "system" | "playwright"

    @property
    def is_chromium(self) -> bool:
        return self.family == "chromium"


def _system_browser_candidates() -> "list[tuple[str, str, list[str]]]":
    """Return (name, family, candidate_paths_or_commands) per platform.

    Chromium-based browsers are listed first; Firefox last (experimental CDP).
    """
    home = os.path.expanduser("~")

    if is_windows():
        pf = os.environ.get("ProgramFiles", "C:\\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        return [
            ("Microsoft Edge", "chromium", [
                os.path.join(pf86, "Microsoft\\Edge\\Application\\msedge.exe"),
                os.path.join(pf, "Microsoft\\Edge\\Application\\msedge.exe"),
                shutil.which("msedge"),
            ]),
            ("Google Chrome", "chromium", [
                os.path.join(pf, "Google\\Chrome\\Application\\chrome.exe"),
                os.path.join(pf86, "Google\\Chrome\\Application\\chrome.exe"),
                os.path.join(local, "Google\\Chrome\\Application\\chrome.exe"),
                shutil.which("chrome"),
                shutil.which("google-chrome"),
            ]),
            ("Brave", "chromium", [
                os.path.join(pf, "BraveSoftware\\Brave-Browser\\Application\\brave.exe"),
                os.path.join(pf86, "BraveSoftware\\Brave-Browser\\Application\\brave.exe"),
                shutil.which("brave"),
                shutil.which("brave-browser"),
            ]),
            ("Chromium", "chromium", [
                os.path.join(local, "Chromium\\Application\\chrome.exe"),
                shutil.which("chromium"),
            ]),
            ("Firefox", "firefox", [
                os.path.join(pf, "Mozilla Firefox\\firefox.exe"),
                os.path.join(pf86, "Mozilla Firefox\\firefox.exe"),
                shutil.which("firefox"),
            ]),
        ]

    if is_macos():
        def _apps(rel: str) -> "list[str]":
            return [f"/Applications/{rel}", os.path.join(home, "Applications", rel)]

        return [
            ("Microsoft Edge", "chromium",
             _apps("Microsoft Edge.app/Contents/MacOS/Microsoft Edge")),
            ("Google Chrome", "chromium",
             _apps("Google Chrome.app/Contents/MacOS/Google Chrome")),
            ("Brave", "chromium",
             _apps("Brave Browser.app/Contents/MacOS/Brave Browser")),
            ("Chromium", "chromium",
             _apps("Chromium.app/Contents/MacOS/Chromium")),
            ("Firefox", "firefox",
             _apps("Firefox.app/Contents/MacOS/firefox")),
        ]

    # Linux
    return [
        ("Microsoft Edge", "chromium", [
            "microsoft-edge-stable", "microsoft-edge", "microsoft-edge-beta",
        ]),
        ("Google Chrome", "chromium", [
            "google-chrome-stable", "google-chrome",
        ]),
        ("Brave", "chromium", [
            "brave-browser-stable", "brave-browser", "brave",
            "/opt/brave.com/brave/brave-browser",
            "/opt/brave.com/brave/brave",
        ]),
        ("Chromium", "chromium", [
            "chromium-browser", "chromium",
            "/snap/bin/chromium",
        ]),
        ("Firefox", "firefox", [
            "firefox", "firefox-esr", "firefox-bin",
        ]),
    ]


def discover_system_browsers() -> List[BrowserInfo]:
    """Scan the OS for supported system-installed browsers."""
    found: List[BrowserInfo] = []
    for name, family, candidates in _system_browser_candidates():
        for candidate in candidates:
            if not candidate:
                continue
            if os.path.sep in candidate or (is_windows() and ":" in candidate):
                # Absolute path candidate.
                if os.path.exists(candidate):
                    found.append(BrowserInfo(name, candidate, family, "system"))
                    break
            else:
                # PATH command candidate.
                if check_command(candidate):
                    found.append(BrowserInfo(name, candidate, family, "system"))
                    break
    return found


def _playwright_cache_dirs() -> List[str]:
    """Locations Playwright uses to store downloaded browser builds."""
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env_path and env_path != "0":
        return [env_path]
    home = os.path.expanduser("~")
    if is_windows():
        base = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        return [os.path.join(base, "ms-playwright")]
    if is_macos():
        return [os.path.join(home, "Library", "Caches", "ms-playwright")]
    return [os.path.join(home, ".cache", "ms-playwright")]


def _scan_playwright_cache() -> List[BrowserInfo]:
    """Filesystem fallback: look for Chromium builds in the Playwright cache."""
    results: List[BrowserInfo] = []
    if is_windows():
        rel_globs = ["chromium-*/chrome-win*/chrome.exe"]
    elif is_macos():
        rel_globs = [
            "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:
        rel_globs = [
            "chromium-*/chrome-linux*/chrome",
            "chromium_headless_shell-*/chrome-linux*/headless_shell",
        ]

    for cache_dir in _playwright_cache_dirs():
        for rel in rel_globs:
            for match in sorted(glob.glob(os.path.join(cache_dir, rel))):
                if os.path.exists(match):
                    results.append(
                        BrowserInfo("Playwright Chromium", match, "chromium", "playwright")
                    )
    return results


def discover_playwright_browsers() -> List[BrowserInfo]:
    """Discover Playwright-managed Chromium.

    Primary path asks Playwright directly for its bundled Chromium executable
    (reliable, respects channels and custom install paths). If that fails we
    fall back to scanning the on-disk browser cache.
    """
    results: List[BrowserInfo] = []
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            path = p.chromium.executable_path
            if path and os.path.exists(path):
                results.append(
                    BrowserInfo("Playwright Chromium", path, "chromium", "playwright")
                )
    except Exception as exc:
        logger.debug("Playwright API browser discovery unavailable: %s", exc)

    if not results:
        results = _scan_playwright_cache()
    return results


def discover_browsers() -> List[BrowserInfo]:
    """Return all drivable browsers, de-duplicated, chromium first.

    Ordering: system chromium browsers, Playwright Chromium, then Firefox last.
    """
    system = discover_system_browsers()
    playwright = discover_playwright_browsers()

    chromium_system = [b for b in system if b.is_chromium]
    firefox_system = [b for b in system if not b.is_chromium]

    ordered = chromium_system + playwright + firefox_system

    deduped: List[BrowserInfo] = []
    seen = set()
    for b in ordered:
        key = os.path.realpath(b.command) if (os.path.sep in b.command) else b.command
        if key in seen:
            continue
        seen.add(key)
        deduped.append(b)
    return deduped


def get_installed_browsers() -> Dict[str, str]:
    """Backwards-compatible view: ``{display_name: command}``.

    Prefer :func:`discover_browsers` for full metadata (family / source).
    """
    result: "dict[str, str]" = {}
    for b in discover_browsers():
        name = b.name
        # Avoid clobbering if two sources share a display name.
        if name in result:
            name = f"{b.name} ({b.source})"
        result[name] = b.command
    return result
