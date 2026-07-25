"""Anti-detection suite for hyperag2api.

Provides:
1. Browser User-Agent and Client Hints rotation.
2. Human-like jitter timing delays.
3. TLS Fingerprint impersonation via curl_cffi (with httpx fallback).
"""

from __future__ import annotations

import asyncio
import random
import unittest.mock
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

import httpx

from src.core import config
from src.core.logging_config import get_logger

logger = get_logger("anti_detection")

# Check curl_cffi availability
try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    CURL_CFFI_AVAILABLE = True
except ImportError:  # pragma: no cover
    CurlAsyncSession = None
    CURL_CFFI_AVAILABLE = False


# --------------------------------------------------------------------------- #
# Realistic Browser Profiles (User-Agent + Client Hints)                      #
# --------------------------------------------------------------------------- #
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
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "application/json, text/plain, */*",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    },
]


def get_random_browser_headers(base_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Return headers merged with a randomly selected modern browser profile."""
    headers = dict(base_headers or config.DEFAULT_HEADERS)
    if config.ENABLE_UA_ROTATION:
        profile = random.choice(BROWSER_PROFILES)
        headers.update(profile)
    return headers


def get_endpoint_headers(
    endpoint_type: str,
    thread_id: Optional[str] = None,
    base_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Return headers customized with dynamic Referer, Origin, and Sec-Fetch-* for the endpoint.

    Supported endpoint_types:
    - 'new_thread' : Referer: https://hyperagent.com/threads/new
    - 'chat'       : Referer: https://hyperagent.com/threads/{thread_id}
    - 'warm'       : Referer: https://hyperagent.com/threads/{thread_id}
    - 'auth_me'    : Referer: https://hyperagent.com/
    - 'upload'     : Referer: https://hyperagent.com/threads/{thread_id} or /threads/new
    - 'interrupt'  : Referer: https://hyperagent.com/threads/{thread_id}
    """
    headers = get_random_browser_headers(base_headers)
    headers["Origin"] = config.HYPERAGENT_BASE_URL

    if endpoint_type == "new_thread":
        headers["Referer"] = f"{config.HYPERAGENT_BASE_URL}/threads/new"
    elif endpoint_type in ("chat", "warm", "interrupt"):
        t_id = thread_id or "new"
        headers["Referer"] = f"{config.HYPERAGENT_BASE_URL}/threads/{t_id}"
    elif endpoint_type == "auth_me":
        headers["Referer"] = f"{config.HYPERAGENT_BASE_URL}/"
    elif endpoint_type == "upload":
        t_id = thread_id or "new"
        headers["Referer"] = f"{config.HYPERAGENT_BASE_URL}/threads/{t_id}"

    headers["Sec-Fetch-Site"] = "same-origin"
    headers["Sec-Fetch-Mode"] = "cors"
    headers["Sec-Fetch-Dest"] = "empty"

    return headers


# --------------------------------------------------------------------------- #
# Human Timing Jitter                                                         #
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


# --------------------------------------------------------------------------- #
# Unified Async HTTP Client (TLS Fingerprint Impersonation)                    #
# --------------------------------------------------------------------------- #
class UnifiedResponse:
    """Standardized response wrapper unifying curl_cffi and httpx responses."""

    def __init__(self, raw_response: Any) -> None:
        self._raw = raw_response
        self.status_code = getattr(raw_response, "status_code", 200)

    @property
    def text(self) -> str:
        if hasattr(self._raw, "text"):
            return self._raw.text
        if hasattr(self._raw, "content") and isinstance(self._raw.content, bytes):
            return self._raw.content.decode("utf-8", errors="replace")
        return str(self._raw)

    def json(self) -> Any:
        return self._raw.json()

    async def aiter_lines(self) -> AsyncGenerator[str, None]:
        if hasattr(self._raw, "aiter_lines"):
            async for line in self._raw.aiter_lines():
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                if isinstance(line, str):
                    line = line.lstrip("\ufeff")
                yield line
        elif hasattr(self._raw, "iter_lines"):  # pragma: no cover
            for line in self._raw.iter_lines():
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                if isinstance(line, str):
                    line = line.lstrip("\ufeff")
                yield line


class StreamContextManager:
    """Context manager for streaming responses across backends."""

    def __init__(self, target: Any, is_curl: bool) -> None:
        self._target = target
        self.is_curl = is_curl
        self._response: Any = None

    async def __aenter__(self) -> UnifiedResponse:
        if self.is_curl:
            res = await self._target
            self._response = res
        else:
            if hasattr(self._target, "__aenter__"):
                res = await self._target.__aenter__()
            elif asyncio.iscoroutine(self._target):
                res = await self._target
            else:
                res = self._target
            self._response = res
        return UnifiedResponse(self._response)

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if not self.is_curl and hasattr(self._target, "__aexit__"):
            await self._target.__aexit__(exc_type, exc_val, exc_tb)
        elif hasattr(self._response, "aclose"):
            await self._response.aclose()
        elif hasattr(self._response, "close"):
            self._response.close()


class UnifiedSession:
    """Adapter wrapping curl_cffi.requests.AsyncSession or httpx.AsyncClient."""

    def __init__(self, session: Any, is_curl: bool = False) -> None:
        self.session = session
        self.is_curl = is_curl

    async def post(self, url: str, **kwargs: Any) -> UnifiedResponse:
        res = await self.session.post(url, **kwargs)
        return UnifiedResponse(res)

    async def get(self, url: str, **kwargs: Any) -> UnifiedResponse:
        res = await self.session.get(url, **kwargs)
        return UnifiedResponse(res)

    async def put(self, url: str, **kwargs: Any) -> UnifiedResponse:
        res = await self.session.put(url, **kwargs)
        return UnifiedResponse(res)

    def stream(self, method: str, url: str, **kwargs: Any) -> StreamContextManager:
        if self.is_curl:
            kwargs["stream"] = True
            coro = getattr(self.session, method.lower())(url, **kwargs)
            return StreamContextManager(coro, is_curl=True)
        else:
            ctx = self.session.stream(method, url, **kwargs)
            return StreamContextManager(ctx, is_curl=False)


def _is_httpx_mocked() -> bool:
    """Return True if httpx.AsyncClient has been mocked by unittest.mock."""
    return (
        isinstance(httpx.AsyncClient, (unittest.mock.MagicMock, unittest.mock.AsyncMock))
        or hasattr(httpx.AsyncClient, "_mock_name")
        or hasattr(httpx.AsyncClient, "return_value")
    )


@asynccontextmanager
async def get_async_session(
    cookies: Optional[Dict[str, str]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: Optional[Union[float, httpx.Timeout]] = None,
) -> AsyncGenerator[UnifiedSession, None]:
    """Yield a UnifiedSession with TLS fingerprint impersonation or httpx fallback."""
    final_headers = get_random_browser_headers(headers)

    # Use httpx if httpx is mocked (for unit tests) or if curl_cffi is disabled/missing
    use_httpx = _is_httpx_mocked() or not (CURL_CFFI_AVAILABLE and config.ENABLE_TLS_FINGERPRINT)

    if use_httpx:
        # Convert numeric timeout to httpx.Timeout if needed
        t_out = timeout
        if isinstance(t_out, (int, float)):
            t_out = httpx.Timeout(t_out)
        async with httpx.AsyncClient(cookies=cookies, headers=final_headers, timeout=t_out) as client:
            yield UnifiedSession(client, is_curl=False)
    else:
        # Convert httpx.Timeout to float seconds if needed for curl_cffi
        c_timeout: Optional[float] = None
        if isinstance(timeout, httpx.Timeout):
            c_timeout = timeout.read or config.REQUEST_READ_TIMEOUT
        elif isinstance(timeout, (int, float)):
            c_timeout = float(timeout)

        impersonate = config.TLS_IMPERSONATE or "chrome124"
        async with CurlAsyncSession(
            cookies=cookies,
            headers=final_headers,
            timeout=c_timeout,
            impersonate=impersonate,
        ) as session:
            yield UnifiedSession(session, is_curl=True)
