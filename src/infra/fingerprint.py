"""Browser fingerprint emulation: headers and human-like timing.

Hyperagent's API is a browser-facing endpoint, so requests that do not look like
they came from a real tab stand out. This module supplies the *presentation*
half of the disguise — User-Agent + Client Hints profiles, per-endpoint
``Referer``/``Sec-Fetch-*`` headers, and randomized inter-request delays. The
transport half (TLS/JA3 impersonation) lives in :mod:`src.infra.http_client`.
"""

from __future__ import annotations

import asyncio
import random
from typing import Dict, List, Optional

from src.core import config
from src.core.logging_config import get_logger

logger = get_logger("fingerprint")


# --------------------------------------------------------------------------- #
# Realistic browser profiles (User-Agent + Client Hints)                       #
# --------------------------------------------------------------------------- #
# Each profile is internally consistent: the UA string, the Sec-Ch-Ua brand list
# and the platform/arch hints all describe the SAME browser. Mixing them (a
# Windows UA with a macOS platform hint) is itself a detection signal.
BROWSER_PROFILES: List[Dict[str, str]] = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Arch": '"x86"',
        "Sec-Ch-Ua-Bitness": '"64"',
        "Sec-Ch-Ua-Full-Version-List": '"Chromium";v="124.0.6367.201", "Google Chrome";v="124.0.6367.201", "Not-A.Brand";v="99.0.0.0"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Ch-Ua-Platform-Version": '"15.0.0"',
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "application/json, text/plain, */*",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Arch": '"arm"',
        "Sec-Ch-Ua-Bitness": '"64"',
        "Sec-Ch-Ua-Full-Version-List": '"Chromium";v="124.0.6367.201", "Google Chrome";v="124.0.6367.201", "Not-A.Brand";v="99.0.0.0"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Ch-Ua-Platform-Version": '"14.4.1"',
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "application/json, text/plain, */*",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    },
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
        "Sec-Ch-Ua": '"Chromium";v="124", "Microsoft Edge";v="124", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Arch": '"x86"',
        "Sec-Ch-Ua-Bitness": '"64"',
        "Sec-Ch-Ua-Full-Version-List": '"Chromium";v="124.0.6367.201", "Microsoft Edge";v="124.0.2478.109", "Not-A.Brand";v="99.0.0.0"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Ch-Ua-Platform-Version": '"15.0.0"',
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "application/json, text/plain, */*",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    },
    {
        # Safari sends no Client Hints at all — omitting them here is correct.
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "application/json, text/plain, */*",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    },
]

# Referer path each endpoint would have been called from in a real browser tab.
# ``{thread_id}`` is substituted when the caller knows it.
_ENDPOINT_REFERERS = {
    "new_thread": "/threads/new",
    "chat": "/threads/{thread_id}",
    "warm": "/threads/{thread_id}",
    "interrupt": "/threads/{thread_id}",
    "upload": "/threads/{thread_id}",
    "auth_me": "/",
}


def get_random_browser_headers(base_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Return headers merged with a randomly selected modern browser profile."""
    headers = dict(base_headers or config.DEFAULT_HEADERS)
    if config.ENABLE_UA_ROTATION:
        headers.update(random.choice(BROWSER_PROFILES))
    return headers


def get_endpoint_headers(
    endpoint_type: str,
    thread_id: Optional[str] = None,
    base_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Headers for one endpoint, with the ``Referer``/``Origin``/``Sec-Fetch-*``
    a browser would have sent from the corresponding page.

    Known ``endpoint_type`` values are the keys of :data:`_ENDPOINT_REFERERS`;
    anything else falls back to the site root.
    """
    headers = get_random_browser_headers(base_headers)
    base = config.HYPERAGENT_BASE_URL

    path = _ENDPOINT_REFERERS.get(endpoint_type, "/")
    headers["Origin"] = base
    headers["Referer"] = base + path.format(thread_id=thread_id or "new")
    headers["Sec-Fetch-Site"] = "same-origin"
    headers["Sec-Fetch-Mode"] = "cors"
    headers["Sec-Fetch-Dest"] = "empty"
    return headers


# --------------------------------------------------------------------------- #
# Human timing jitter                                                          #
# --------------------------------------------------------------------------- #
async def apply_human_jitter(
    min_ms: Optional[float] = None, max_ms: Optional[float] = None
) -> None:
    """Sleep for a randomized human-like delay before/between requests."""
    if not config.ENABLE_HUMAN_JITTER:
        return
    low = (min_ms if min_ms is not None else config.JITTER_MIN_MS) / 1000.0
    high = (max_ms if max_ms is not None else config.JITTER_MAX_MS) / 1000.0
    if high > low > 0:
        delay = random.uniform(low, high)
        logger.debug("Applying human jitter delay: %.3fs", delay)
        await asyncio.sleep(delay)
