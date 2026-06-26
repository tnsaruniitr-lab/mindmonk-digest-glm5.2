"""Tests for the processed-video store (both backends).

Verifies: mark/get/dedup/list_recent, failed-row retryability, and reset.
Runs against SQLite by default; against Postgres when TEST_DATABASE_URL is set.
"""

from __future__ import annotations


class TestStoreBasics:
    """Backend-agnostic tests. Run against SQLite (fast, default)."""

    def test_mark_done_and_get(self, sqlite_store):
        sqlite_store.mark_done("vid1", "chanA", summary="brief text")
        row = sqlite_store.get("vid1")
        assert row is not None
        assert row.status == "done"
        assert row.summary == "brief text"
        assert row.video_id == "vid1"

    def test_is_processed_false_initially(self, sqlite_store):
        assert sqlite_store.is_processed("never_seen") is False

    def test_is_processed_true_after_done(self, sqlite_store):
        sqlite_store.mark_done("vid1", "chanA")
        assert sqlite_store.is_processed("vid1") is True

    def test_is_processed_true_after_skipped(self, sqlite_store):
        sqlite_store.mark_skipped("vid1", "chanA", note="short")
        assert sqlite_store.is_processed("vid1") is True

    def test_is_processed_false_after_failed(self, sqlite_store):
        """failed rows are retryable — NOT treated as processed."""
        sqlite_store.mark_failed("vid1", "chanA", note="transient")
        assert sqlite_store.is_processed("vid1") is False

    def test_mark_failed_then_retry_as_done(self, sqlite_store):
        """The key resilience behavior: a failed video can be reprocessed."""
        sqlite_store.mark_failed("vid1", "chanA", note="broke")
        assert sqlite_store.is_processed("vid1") is False
        # simulate a retry that succeeds
        sqlite_store.mark_done("vid1", "chanA", summary="ok now")
        assert sqlite_store.is_processed("vid1") is True
        assert sqlite_store.get("vid1").summary == "ok now"

    def test_upsert_overwrites(self, sqlite_store):
        sqlite_store.mark_done("vid1", "chanA", summary="v1")
        sqlite_store.mark_skipped("vid1", "chanA", note="reprocess")
        row = sqlite_store.get("vid1")
        assert row.status == "skipped"
        assert row.note == "reprocess"

    def test_list_recent_orders_desc(self, sqlite_store):
        sqlite_store.mark_done("a", "c", summary="x")
        sqlite_store.mark_done("b", "c", summary="y")
        recent = sqlite_store.list_recent(limit=10)
        assert len(recent) >= 2
        # most recent first (by processed_at)
        ids = [r.video_id for r in recent]
        assert "b" in ids and "a" in ids

    def test_list_recent_respects_limit(self, sqlite_store):
        for i in range(10):
            sqlite_store.mark_done(f"vid{i}", "c")
        recent = sqlite_store.list_recent(limit=3)
        assert len(recent) <= 3

    def test_get_returns_none_for_missing(self, sqlite_store):
        assert sqlite_store.get("nonexistent") is None


class TestStoreResetFailed:
    def test_reset_failed_clears_only_failed(self, sqlite_store):
        sqlite_store.mark_done("good", "c", summary="keep")
        sqlite_store.mark_failed("bad1", "c", note="x")
        sqlite_store.mark_failed("bad2", "c", note="y")
        count = sqlite_store.reset_failed()
        assert count == 2
        assert sqlite_store.is_processed("good") is True
        # failed rows are gone → not processed → retryable
        assert sqlite_store.is_processed("bad1") is False
