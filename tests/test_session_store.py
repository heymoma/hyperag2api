"""Tests for the session → thread store: TTL, LRU eviction, SQLite persistence."""

import os
import tempfile
import time
import unittest

from src.core.session_store import SessionStore


class TestSessionStore(unittest.IsolatedAsyncioTestCase):
    async def test_put_get_forget(self):
        store = SessionStore(persist=False)
        await store.put("k1", "thread-1", model="opus")
        self.assertEqual(await store.get("k1"), "thread-1")
        self.assertEqual(await store.count(), 1)
        self.assertTrue(await store.forget("k1"))
        self.assertIsNone(await store.get("k1"))

    async def test_get_record_returns_message_count(self):
        store = SessionStore(persist=False)
        await store.put("k", "t", model="opus", message_count=7)
        record = await store.get_record("k")
        self.assertEqual(record["thread_id"], "t")
        self.assertEqual(record["message_count"], 7)
        self.assertEqual(record["model"], "opus")

    async def test_ttl_expiry(self):
        store = SessionStore(persist=False, ttl=100)
        await store.put("k", "t")
        store._cache["k"].last_used = time.time() - 1000  # force stale
        self.assertIsNone(await store.get("k"))

    async def test_lru_eviction(self):
        store = SessionStore(persist=False, max_size=2)
        await store.put("k1", "t1")
        await store.put("k2", "t2")
        await store.put("k3", "t3")
        self.assertIsNone(await store.get("k1"))  # oldest evicted
        self.assertEqual(await store.get("k2"), "t2")
        self.assertEqual(await store.get("k3"), "t3")

    async def test_persistence_roundtrip(self):
        path = os.path.join(tempfile.mkdtemp(), "sessions.db")
        first = SessionStore(persist=True, db_path=path)
        await first.put("conv", "thread-xyz", model="sonnet-5")
        # A brand new store pointed at the same file recovers the mapping.
        second = SessionStore(persist=True, db_path=path)
        self.assertEqual(await second.get("conv"), "thread-xyz")

    async def test_snapshot_is_most_recent_first(self):
        store = SessionStore(persist=False)
        await store.put("a", "t1")
        await store.put("b", "t2")
        snapshot = await store.snapshot()
        self.assertEqual(len(snapshot), 2)
        self.assertEqual(snapshot[0]["key"], "b")


if __name__ == "__main__":
    unittest.main()
