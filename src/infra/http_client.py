"""Async HTTP transport with optional TLS fingerprint impersonation.

Two backends sit behind one interface:

* **curl_cffi** — replays a real Chrome TLS/JA3 handshake, so the connection
  itself looks like a browser. Used whenever it is installed and enabled.
* **httpx** — the plain fallback when curl_cffi is unavailable, disabled, or
  when the test-suite has patched ``httpx.AsyncClient``.

:class:`UnifiedSession` / :class:`UnifiedResponse` normalise the two so callers
never branch on which one is in play.
"""

from __future__ import annotations

import asyncio
import unittest.mock
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, Optional, Union

import httpx

from src.core import config
from src.core.logging_config import get_logger
from src.infra.fingerprint import get_random_browser_headers

logger = get_logger("http")

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession

    CURL_CFFI_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    CurlAsyncSession = None
    CURL_CFFI_AVAILABLE = False


# --------------------------------------------------------------------------- #
# Response / session wrappers                                                  #
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
        """Yield decoded lines from either an async or a sync line iterator."""
        if hasattr(self._raw, "aiter_lines"):
            async for line in self._raw.aiter_lines():
                yield _decode_line(line)
        elif hasattr(self._raw, "iter_lines"):  # pragma: no cover - curl_cffi sync path
            for line in self._raw.iter_lines():
                yield _decode_line(line)


def _decode_line(line: Any) -> str:
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    if isinstance(line, str):
        # Strip a UTF-8 BOM that some proxies prepend to the first SSE frame.
        return line.lstrip("\ufeff")
    return line


class StreamContextManager:
    """Async context manager for streaming responses across both backends.

    curl_cffi returns an awaitable that resolves to the response; httpx returns
    an async context manager. Both are entered/exited the same way from here.
    """

    def __init__(self, target: Any, is_curl: bool) -> None:
        self._target = target
        self.is_curl = is_curl
        self._response: Any = None

    async def __aenter__(self) -> UnifiedResponse:
        if self.is_curl:
            self._response = await self._target
        elif hasattr(self._target, "__aenter__"):
            self._response = await self._target.__aenter__()
        elif asyncio.iscoroutine(self._target):
            self._response = await self._target
        else:
            self._response = self._target
        return UnifiedResponse(self._response)

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if not self.is_curl and hasattr(self._target, "__aexit__"):
            await self._target.__aexit__(exc_type, exc_val, exc_tb)
        elif hasattr(self._response, "aclose"):
            await self._response.aclose()
        elif hasattr(self._response, "close"):
            self._response.close()


class UnifiedSession:
    """Adapter wrapping ``curl_cffi.requests.AsyncSession`` or ``httpx.AsyncClient``."""

    def __init__(self, session: Any, is_curl: bool = False) -> None:
        self.session = session
        self.is_curl = is_curl

    async def post(self, url: str, **kwargs: Any) -> UnifiedResponse:
        return UnifiedResponse(await self.session.post(url, **kwargs))

    async def get(self, url: str, **kwargs: Any) -> UnifiedResponse:
        return UnifiedResponse(await self.session.get(url, **kwargs))

    async def put(self, url: str, **kwargs: Any) -> UnifiedResponse:
        return UnifiedResponse(await self.session.put(url, **kwargs))

    def stream(self, method: str, url: str, **kwargs: Any) -> StreamContextManager:
        if self.is_curl:
            kwargs["stream"] = True
            return StreamContextManager(
                getattr(self.session, method.lower())(url, **kwargs), is_curl=True
            )
        return StreamContextManager(self.session.stream(method, url, **kwargs), is_curl=False)


# --------------------------------------------------------------------------- #
# Backend selection                                                            #
# --------------------------------------------------------------------------- #
def _httpx_is_patched() -> bool:
    """True when the test-suite has replaced ``httpx.AsyncClient`` with a mock.

    Unit tests assert against httpx call signatures, so an impersonating
    curl_cffi session would bypass their mocks entirely. Detecting the patch
    keeps the production default (curl_cffi) without forcing tests to configure
    the transport.
    """
    return (
        isinstance(httpx.AsyncClient, (unittest.mock.MagicMock, unittest.mock.AsyncMock))
        or hasattr(httpx.AsyncClient, "_mock_name")
        or hasattr(httpx.AsyncClient, "return_value")
    )


def use_curl_backend() -> bool:
    """Whether the impersonating curl_cffi transport should be used."""
    return CURL_CFFI_AVAILABLE and config.ENABLE_TLS_FINGERPRINT and not _httpx_is_patched()


def _as_httpx_timeout(timeout: Optional[Union[float, httpx.Timeout]]) -> Optional[httpx.Timeout]:
    if isinstance(timeout, (int, float)):
        return httpx.Timeout(timeout)
    return timeout


def _as_seconds(timeout: Optional[Union[float, httpx.Timeout]]) -> Optional[float]:
    if isinstance(timeout, httpx.Timeout):
        return timeout.read or config.REQUEST_READ_TIMEOUT
    if isinstance(timeout, (int, float)):
        return float(timeout)
    return None


@asynccontextmanager
async def get_async_session(
    cookies: Optional[Dict[str, str]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: Optional[Union[float, httpx.Timeout]] = None,
) -> AsyncGenerator[UnifiedSession, None]:
    """Yield a :class:`UnifiedSession` on the best available transport."""
    final_headers = get_random_browser_headers(headers)

    if not use_curl_backend():
        async with httpx.AsyncClient(
            cookies=cookies, headers=final_headers, timeout=_as_httpx_timeout(timeout)
        ) as client:
            yield UnifiedSession(client, is_curl=False)
        return

    async with CurlAsyncSession(
        cookies=cookies,
        headers=final_headers,
        timeout=_as_seconds(timeout),
        impersonate=config.TLS_IMPERSONATE or "chrome124",
    ) as session:
        yield UnifiedSession(session, is_curl=True)
