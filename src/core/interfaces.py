from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator, Dict, List, Optional


class CookieProvider(ABC):
    """Abstraction for retrieving and managing browser session cookies."""

    @abstractmethod
    async def get_cookies(self, url: str) -> Dict[str, str]:
        """Fetch cookies for a given URL dynamically."""
        raise NotImplementedError

    @abstractmethod
    def clear_cookies(self) -> bool:
        """Clear cookies in the active browser context."""
        raise NotImplementedError

    def invalidate(self) -> None:
        """Drop any cached cookies so the next fetch re-reads from the browser.

        Concrete providers with a cache should override this. Default is a no-op
        so lightweight/mocked providers need not implement it.
        """
        return None


class ChatBackend(ABC):
    """Abstraction for interacting with the AI chat thread backend."""

    @abstractmethod
    async def create_thread(
        self,
        model: str,
        system_prompt: str,
        cookies: Dict[str, str],
        session_id: Optional[str] = None,
    ) -> str:
        """Initialize a new chat thread and return its thread ID."""
        raise NotImplementedError

    @abstractmethod
    async def warm_thread(self, thread_id: str, cookies: Dict[str, str]) -> None:
        """Warm up the created thread to prepare it for incoming messages."""
        raise NotImplementedError

    @abstractmethod
    async def stream_chat(
        self,
        thread_id: str,
        prompt: str,
        cookies: Dict[str, str],
        session_id: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        feature_flags: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Send a message to the thread and stream the response events."""
        raise NotImplementedError

    async def upload_file(
        self,
        thread_id: str,
        cookies: Dict[str, str],
        filename: str,
        data: bytes,
        content_type: str,
    ) -> Optional[Dict[str, Any]]:
        """Upload an attachment and return a descriptor (or None if unsupported)."""
        return None

    async def interrupt(self, thread_id: str, cookies: Dict[str, str]) -> None:
        """Best-effort cancel of an in-flight generation on the backend."""
        return None
