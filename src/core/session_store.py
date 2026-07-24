"""Session → thread mapping with optional SQLite persistence.

OpenAI's ``/v1/chat/completions`` is stateless: the client resends the whole
message history every turn. Hyperagent, by contrast, is thread-stateful. This
store bridges the two so that one logical conversation maps to exactly one
Hyperagent thread instead of spawning a fresh thread on every request.

Keys are opaque strings produced by :mod:`chat_service`:
- ``sid:<id>``  — an explicit/named session (client header, OpenAI ``user``
  field, or ``model@suffix``).
- ``px:<hash>`` — a hash of the conversation prefix (all messages except the
  newest), letting us recognise the next turn of the same conversation.

An in-memory LRU cache fronts the database; SQLite gives durability across
restarts. All access goes through an ``asyncio.Lock`` and DB calls are guarded
by a threading lock, so it is safe under FastAPI's concurrency.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.core.logging_config import get_logger

logger = get_logger("sessions")


@dataclass
class SessionRecord:
    key: str
    thread_id: str
    model: str = ""
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    message_count: int = 0

    def as_dict(self) -> Dict[str, object]:
        return {
            "key": self.key,
            "thread_id": self.thread_id,
            "model": self.model,
            "created_at": self.created_at,
            "last_used": self.last_used,
            "message_count": self.message_count,
            "age_seconds": round(time.time() - self.created_at, 1),
            "idle_seconds": round(time.time() - self.last_used, 1),
        }


class SessionStore:
    """LRU + TTL store mapping conversation keys to Hyperagent thread ids."""

    def __init__(
        self,
        persist: bool = False,
        db_path: str = "sessions.db",
        ttl: int = 6 * 60 * 60,
        max_size: int = 1000,
    ) -> None:
        self.ttl = ttl
        self.max_size = max_size
        self._cache: "OrderedDict[str, SessionRecord]" = OrderedDict()
        # Lazily created inside a running loop (Python 3.9 can't build a Lock
        # outside one). Access only via _get_lock().
        self._lock: Optional[asyncio.Lock] = None
        self._db_lock = threading.Lock()
        self._db: Optional[sqlite3.Connection] = None

        self.persist = persist and db_path not in ("", ":memory:")
        self.db_path = db_path
        if self.persist:
            try:
                self._db = sqlite3.connect(db_path, check_same_thread=False)
                self._init_db()
                logger.info("Session persistence enabled at %s", db_path)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Could not open session DB %s (%s); using memory only.", db_path, exc)
                self._db = None
                self.persist = False

    # ------------------------------------------------------------------ #
    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @classmethod
    def from_config(cls) -> "SessionStore":
        from src.core import config

        return cls(
            persist=config.SESSION_PERSIST,
            db_path=config.SESSION_DB_PATH,
            ttl=config.SESSION_TTL_SECONDS,
            max_size=config.SESSION_MAX,
        )

    # ------------------------------------------------------------------ #
    def _init_db(self) -> None:
        assert self._db is not None
        with self._db_lock:
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    key TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    model TEXT,
                    created_at REAL,
                    last_used REAL,
                    message_count INTEGER
                )
                """
            )
            self._db.commit()

    def _db_write(self, rec: SessionRecord) -> None:
        if not self._db:
            return
        try:
            with self._db_lock:
                self._db.execute(
                    "REPLACE INTO sessions (key, thread_id, model, created_at, last_used, message_count) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (rec.key, rec.thread_id, rec.model, rec.created_at, rec.last_used, rec.message_count),
                )
                self._db.commit()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Session DB write failed: %s", exc)

    def _db_delete(self, key: str) -> None:
        if not self._db:
            return
        try:
            with self._db_lock:
                self._db.execute("DELETE FROM sessions WHERE key = ?", (key,))
                self._db.commit()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Session DB delete failed: %s", exc)

    def _db_read(self, key: str) -> Optional[SessionRecord]:
        if not self._db:
            return None
        try:
            with self._db_lock:
                cur = self._db.execute(
                    "SELECT key, thread_id, model, created_at, last_used, message_count "
                    "FROM sessions WHERE key = ?",
                    (key,),
                )
                row = cur.fetchone()
            if not row:
                return None
            return SessionRecord(
                key=row[0], thread_id=row[1], model=row[2] or "",
                created_at=row[3] or time.time(), last_used=row[4] or time.time(),
                message_count=row[5] or 0,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Session DB read failed: %s", exc)
            return None

    # ------------------------------------------------------------------ #
    def _expired(self, rec: SessionRecord) -> bool:
        return self.ttl > 0 and (time.time() - rec.last_used) > self.ttl

    async def get(self, key: str) -> Optional[str]:
        """Return the thread id for ``key`` if present and not expired."""
        async with self._get_lock():
            rec = self._cache.get(key)
            if rec is None:
                rec = self._db_read(key)
                if rec is not None:
                    self._cache[key] = rec
            if rec is None:
                return None
            if self._expired(rec):
                self._cache.pop(key, None)
                self._db_delete(key)
                logger.debug("Session %s expired; dropping thread %s", key, rec.thread_id)
                return None
            rec.last_used = time.time()
            self._cache.move_to_end(key)
            return rec.thread_id

    async def get_record(self, key: str) -> Optional[Dict[str, object]]:
        """Like :meth:`get` but returns {thread_id, message_count, model}."""
        async with self._get_lock():
            rec = self._cache.get(key)
            if rec is None:
                rec = self._db_read(key)
                if rec is not None:
                    self._cache[key] = rec
            if rec is None or self._expired(rec):
                if rec is not None:
                    self._cache.pop(key, None)
                    self._db_delete(key)
                return None
            rec.last_used = time.time()
            self._cache.move_to_end(key)
            return {"thread_id": rec.thread_id, "message_count": rec.message_count, "model": rec.model}

    async def put(self, key: str, thread_id: str, model: str = "", message_count: int = 0) -> None:
        """Create or update the mapping ``key`` → ``thread_id``."""
        async with self._get_lock():
            existing = self._cache.get(key)
            now = time.time()
            if existing:
                existing.thread_id = thread_id
                existing.model = model or existing.model
                existing.last_used = now
                existing.message_count = message_count or existing.message_count
                rec = existing
                self._cache.move_to_end(key)
            else:
                rec = SessionRecord(
                    key=key, thread_id=thread_id, model=model,
                    created_at=now, last_used=now, message_count=message_count,
                )
                self._cache[key] = rec
            self._db_write(rec)
            self._evict_locked()

    def _evict_locked(self) -> None:
        while self.max_size > 0 and len(self._cache) > self.max_size:
            old_key, old_rec = self._cache.popitem(last=False)
            self._db_delete(old_key)
            logger.debug("Evicting LRU session %s (thread %s)", old_key, old_rec.thread_id)

    async def forget(self, key: str) -> bool:
        async with self._get_lock():
            existed = self._cache.pop(key, None) is not None
            self._db_delete(key)
            return existed

    async def snapshot(self) -> List[Dict[str, object]]:
        """Return a view of active (non-expired) sessions, most-recent first."""
        async with self._get_lock():
            out: List[Dict[str, object]] = []
            for rec in reversed(self._cache.values()):
                if not self._expired(rec):
                    out.append(rec.as_dict())
            return out

    async def count(self) -> int:
        async with self._get_lock():
            return sum(1 for rec in self._cache.values() if not self._expired(rec))
