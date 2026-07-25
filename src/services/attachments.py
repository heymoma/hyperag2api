"""Multimodal attachments: fetching images and handing them to the backend.

OpenAI clients send images inline as ``image_url`` content parts — either a
``data:`` URI or an HTTP(S) link. Hyperagent instead wants files uploaded up
front and referenced by id. This module does that translation.

The whole path is best-effort: an image that cannot be fetched or uploaded is
skipped rather than failing the completion, because a text-only answer beats no
answer at all.
"""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional, Tuple

import httpx

from src.core import config
from src.core.interfaces import ChatBackend
from src.core.logging_config import get_logger

logger = get_logger("attachments")

# Cap per turn: enough for real multimodal prompts, low enough that a hostile or
# buggy client cannot make us fetch an unbounded number of remote URLs.
MAX_IMAGES_PER_TURN = 8

_FETCH_TIMEOUT = 30.0
_DEFAULT_MIME = "image/png"


def _filename_for(mime: str) -> str:
    ext = (mime.split("/")[-1] or "png").split("+")[0]
    return f"image.{ext}"


async def fetch_image(ref: str) -> Optional[Tuple[bytes, str, str]]:
    """Load an image reference into (bytes, mime, filename), or None on failure."""
    try:
        if ref.startswith("data:"):
            header, _, b64 = ref.partition(",")
            mime = _DEFAULT_MIME
            if ";" in header and ":" in header:
                mime = header[header.index(":") + 1 : header.index(";")] or mime
            return base64.b64decode(b64), mime, _filename_for(mime)

        if ref.startswith("http://") or ref.startswith("https://"):
            async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT) as client:
                resp = await client.get(ref)
            if resp.status_code == 200:
                mime = resp.headers.get("content-type", _DEFAULT_MIME).split(";")[0]
                return resp.content, mime, _filename_for(mime)
    except Exception as exc:
        logger.debug("Failed to load image %s: %s", ref[:60], exc)
    return None


class AttachmentUploader:
    """Uploads the newest message's images and returns backend descriptors."""

    def __init__(self, backend: ChatBackend) -> None:
        self.backend = backend

    async def collect(
        self, messages: List[Any], thread_id: str, cookies: Dict[str, str]
    ) -> List[Dict[str, Any]]:
        """Upload images attached to the last message; never raises."""
        if not config.ENABLE_MULTIMODAL or not messages:
            return []

        last = messages[-1]
        image_urls = getattr(last, "image_urls", None)
        refs = image_urls() if callable(image_urls) else []
        if not refs:
            return []

        attachments: List[Dict[str, Any]] = []
        for ref in refs[:MAX_IMAGES_PER_TURN]:
            loaded = await fetch_image(ref)
            if not loaded:
                continue
            data, mime, filename = loaded
            try:
                descriptor = await self.backend.upload_file(thread_id, cookies, filename, data, mime)
            except Exception as exc:
                logger.debug("upload_file raised (non-fatal): %s", exc)
                descriptor = None
            if descriptor:
                attachments.append(descriptor)

        if attachments:
            logger.info("Attached %d image(s) to thread %s", len(attachments), thread_id)
        return attachments
