#!/usr/bin/env python3
"""Installer for the Hyperagent Local Proxy (browserless).

Installs the Python dependencies and verifies the environment. No browser or
Playwright required — authentication uses config-provided session token(s).

Usage:
    python3 install.py
"""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def _run(cmd, desc: str) -> bool:
    print(f"\n\033[94m▶ {desc}\033[0m\n  $ {' '.join(cmd)}")
    try:
        subprocess.check_call(cmd)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"\033[91m  ✗ {exc}\033[0m")
        return False


def install_dependencies() -> bool:
    req = os.path.join(ROOT, "requirements.txt")
    return _run([sys.executable, "-m", "pip", "install", "-r", req],
                "Installing Python dependencies (requirements.txt)")


def verify() -> bool:
    print("\n\033[94m▶ Verifying installation\033[0m")
    ok = True
    for mod in ("fastapi", "uvicorn", "httpx", "pydantic", "yaml"):
        try:
            __import__(mod)
            print(f"  ✓ import {mod}")
        except Exception as exc:  # pragma: no cover - defensive
            print(f"\033[91m  ✗ import {mod}: {exc}\033[0m")
            ok = False
    return ok


def main() -> int:
    print("=" * 60)
    print("  Hyperagent Local Proxy — Installer".center(60))
    print("=" * 60)

    if not install_dependencies():
        print("\n\033[91mDependency installation failed. Aborting.\033[0m")
        return 1

    ok = verify()
    print("\n" + "=" * 60)
    if ok:
        print("\033[92m✓ Done. Next:\033[0m")
        print("    1. cp config.example.yaml config.yaml")
        print("    2. add your '__Host-hyperagent_session' token(s) under 'sessions:'")
        print("    3. python3 start.py")
    else:
        print("\033[93m⚠ Finished with warnings (see above).\033[0m")
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
