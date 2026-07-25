"""Shared plumbing for consuming the backend's event stream.

A cold Hyperagent thread can stay silent for a long time before the first token
arrives — long enough for clients (and intermediate proxies) to time the request
out. :func:`iter_with_keepalive` turns that silence into an explicit signal the
renderers can act on, so both the plain and the tool-calling paths get heartbeats
from one implementation.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator, AsyncIterator, Dict, Optional, Union


class _Keepalive:
    """Sentinel yielded when the backend has been silent for one interval."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<keepalive>"


KEEPALIVE = _Keepalive()

StreamItem = Union[Dict[str, Any], _Keepalive]


async def iter_with_keepalive(
    source: AsyncIterator[Dict[str, Any]], interval: float
) -> AsyncGenerator[StreamItem, None]:
    """Yield backend frames, plus :data:`KEEPALIVE` after each silent interval.

    The pending ``__anext__`` is held in a task that *survives* the timeout.
    Awaiting it through ``asyncio.wait_for`` instead would cancel it, and
    cancelling an async generator mid-suspension finishes it for good — every
    frame after the first heartbeat would be lost, silently truncating the
    answer exactly when the backend is slowest.

    A non-positive ``interval`` disables heartbeats and simply forwards the
    source. Non-dict frames are dropped — the backend occasionally emits bare
    strings that carry nothing we can render.
    """
    if not interval or interval <= 0:
        async for item in source:
            if isinstance(item, dict):
                yield item
        return

    iterator = source.__aiter__()
    pending: Optional[asyncio.Task] = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())

            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield KEEPALIVE
                continue

            task, pending = pending, None
            try:
                item = task.result()
            except StopAsyncIteration:
                return
            if isinstance(item, dict):
                yield item
    finally:
        # The consumer stopped early (client disconnect, `break`): drop the
        # in-flight fetch rather than leaking a task that outlives the request.
        if pending is not None:
            pending.cancel()
