"""Lightweight in-memory runtime stats for the monitoring dashboard.

A single process-wide :data:`STATS` instance records request counters and a
bounded ring buffer of recent requests. It is intentionally dependency-free and
thread-safe so both the async request path and the dashboard endpoint can touch
it without ceremony. Nothing here is persisted.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional


class Stats:
    def __init__(self, history: int = 50) -> None:
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.requests_total = 0
        self.requests_error = 0
        self.streams_active = 0
        self.tokens_prompt = 0
        self.tokens_completion = 0
        self._recent: Deque[Dict[str, Any]] = deque(maxlen=history)

    def request_started(self, request_id: str, model: str, stream: bool) -> Dict[str, Any]:
        entry = {
            "id": request_id,
            "model": model,
            "stream": stream,
            "ts": time.time(),
            "status": "running",
            "latency_ms": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "session_key": None,
            "thread_id": None,
            "reused_thread": None,
            "error": None,
        }
        with self._lock:
            self.requests_total += 1
            if stream:
                self.streams_active += 1
            self._recent.appendleft(entry)
        return entry

    def request_finished(
        self,
        entry: Dict[str, Any],
        *,
        status: str = "ok",
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        session_key: Optional[str] = None,
        thread_id: Optional[str] = None,
        reused_thread: Optional[bool] = None,
        error: Optional[str] = None,
    ) -> None:
        with self._lock:
            if entry.get("stream") and self.streams_active > 0:
                self.streams_active -= 1
            entry["status"] = status
            entry["latency_ms"] = round((time.time() - entry["ts"]) * 1000)
            entry["prompt_tokens"] = prompt_tokens
            entry["completion_tokens"] = completion_tokens
            entry["session_key"] = session_key
            entry["thread_id"] = thread_id
            entry["reused_thread"] = reused_thread
            entry["error"] = error
            if status == "error":
                self.requests_error += 1
            if prompt_tokens:
                self.tokens_prompt += prompt_tokens
            if completion_tokens:
                self.tokens_completion += completion_tokens

    def recent(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._recent)

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "uptime_seconds": round(time.time() - self.started_at, 1),
                "requests_total": self.requests_total,
                "requests_error": self.requests_error,
                "streams_active": self.streams_active,
                "tokens_prompt": self.tokens_prompt,
                "tokens_completion": self.tokens_completion,
            }


STATS = Stats()
