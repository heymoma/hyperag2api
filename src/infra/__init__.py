"""Technical infrastructure: HTTP transport and browser-fingerprint emulation.

Everything here is about *how* bytes reach hyperagent.com, never about what the
proxy does with them. Import the two public entry points from this package:

    from src.infra import get_async_session, get_endpoint_headers
"""

from src.infra.fingerprint import (
    BROWSER_PROFILES,
    apply_human_jitter,
    get_endpoint_headers,
    get_random_browser_headers,
)
from src.infra.http_client import (
    CURL_CFFI_AVAILABLE,
    UnifiedResponse,
    UnifiedSession,
    get_async_session,
    use_curl_backend,
)

__all__ = [
    "BROWSER_PROFILES",
    "CURL_CFFI_AVAILABLE",
    "UnifiedResponse",
    "UnifiedSession",
    "apply_human_jitter",
    "get_async_session",
    "get_endpoint_headers",
    "get_random_browser_headers",
    "use_curl_backend",
]
