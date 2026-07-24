#!/usr/bin/env python3
"""Automated installer for the Hyperagent Local Proxy.

Installs the Python dependencies and (optionally) the Playwright Chromium
browser, then verifies the environment and reports which browsers were found.

Usage:
    python3 install.py                    # install deps + Playwright Chromium
    python3 install.py --skip-browser     # deps only (use a system browser)
    python3 install.py --with-deps        # also install OS libs (Linux, needs sudo)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def _run(cmd, desc: str) -> bool:
    print(f"\n\033[94m▶ {desc}\033[0m")
    print(f"  $ {' '.join(cmd)}")
    try:
        subprocess.check_call(cmd)
        return True
    except subprocess.CalledProcessError as exc:
        print(f"\033[91m  ✗ Command failed (exit {exc.returncode}).\033[0m")
        return False
    except FileNotFoundError as exc:
        print(f"\033[91m  ✗ Command not found: {exc}\033[0m")
        return False


def install_dependencies() -> bool:
    req = os.path.join(ROOT, "requirements.txt")
    return _run(
        [sys.executable, "-m", "pip", "install", "-r", req],
        "Installing Python dependencies (requirements.txt)",
    )


def install_playwright_browser(with_deps: bool) -> bool:
    cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
    if with_deps:
        cmd.append("--with-deps")
    return _run(cmd, "Installing Playwright Chromium browser")


def verify() -> bool:
    print("\n\033[94m▶ Verifying installation\033[0m")
    ok = True

    for mod in ("fastapi", "uvicorn", "httpx", "playwright", "pydantic"):
        try:
            __import__(mod)
            print(f"  ✓ import {mod}")
        except Exception as exc:  # pragma: no cover - defensive
            print(f"\033[91m  ✗ import {mod}: {exc}\033[0m")
            ok = False

    # Report detected browsers (system + Playwright) using our own detector.
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    try:
        from src.adapters.launcher.system import discover_browsers

        browsers = discover_browsers()
        if browsers:
            print("\n  Detected browsers:")
            for b in browsers:
                print(f"    • {b.name}  ({b.source}, {b.family})  ->  {b.command}")
        else:
            print("\033[93m  ⚠ No browsers detected yet. Install a Chromium browser "
                  "or run: playwright install chromium\033[0m")
    except Exception as exc:  # pragma: no cover - defensive
        print(f"\033[91m  ✗ Browser detection failed: {exc}\033[0m")
        ok = False

    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the Hyperagent Local Proxy.")
    parser.add_argument("--skip-browser", action="store_true",
                        help="Do not download Playwright Chromium (use a system browser).")
    parser.add_argument("--with-deps", action="store_true",
                        help="Also install OS-level browser dependencies (Linux; may need sudo).")
    args = parser.parse_args()

    print("=" * 60)
    print("  Hyperagent Local Proxy — Installer".center(60))
    print("=" * 60)

    if not install_dependencies():
        print("\n\033[91mDependency installation failed. Aborting.\033[0m")
        return 1

    if not args.skip_browser:
        if not install_playwright_browser(args.with_deps):
            print("\033[93m\nPlaywright browser install failed. You can still use a "
                  "system browser (Edge/Chrome/Brave), or retry later.\033[0m")

    ok = verify()

    print("\n" + "=" * 60)
    if ok:
        print("\033[92m✓ Installation complete. Start the launcher with:  python3 start.py\033[0m")
    else:
        print("\033[93m⚠ Installation finished with warnings. See messages above.\033[0m")
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
