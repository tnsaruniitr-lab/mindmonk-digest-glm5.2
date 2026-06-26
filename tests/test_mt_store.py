"""Integration tests for the multi-tenant store.

These verify the core multi-tenancy guarantee: per-user isolation. Two users
subscribe to the same channel; their digests, channels, and stats must not leak.

Requires TEST_DATABASE_URL pointing at a Postgres with the Phase 1 schema.
Skipped otherwise (the SQLite-backed legacy tests still cover the old path).
"""

from __future__ import annotations

import os

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL", "")


@pytest.fixture
def mt_store():
    if not TEST_DB:
        pytest.skip("TEST_DATABASE_URL not set; skipping multi-tenant integration test")
    from src.mt_store import MultiTenantStore

    store = MultiTenantStore(TEST_DB, schema="test_mt")
    yield store
    store.close()


@pytest.fixture
def clean_db(mt_store):
    """Wipe test data before each test (isolates tests from each other)."""
    for table in [
        "usage_ledger",
        "digests",
        "subscriptions",
        "videos",
        "channels",
        "users",
    ]:
        mt_store._conn.execute(f"DELETE FROM {table}")  # noqa: SLF001
    yield


@pytest.mark.integration
class TestMultiTenantStore:
    def test_get_or_create_user_idempotent(self, mt_store, clean_db):
        uid1 = mt_store.get_or_create_user("111", "user111")
        uid2 = mt_store.get_or_create_user("111", "user111")
        assert uid1 == uid2  # same chat_id → same user

    def test_distinct_users_distinct_ids(self, mt_store, clean_db):
        uid1 = mt_store.get_or_create_user("111", "u1")
        uid2 = mt_store.get_or_create_user("222", "u2")
        assert uid1 != uid2

    def test_add_and_list_channels_per_user(self, mt_store, clean_db):
        u1 = mt_store.get_or_create_user("111", "u1")
        u2 = mt_store.get_or_create_user("222", "u2")
        mt_store.add_channel(u1, "DOAC", "https://youtube.com/@doac")
        mt_store.add_channel(u1, "Lex", "https://youtube.com/@lex")

        u1_channels = mt_store.list_channels(u1)
        assert len(u1_channels) == 2

        u2_channels = mt_store.list_channels(u2)
        assert u2_channels == []  # u2 has no subscriptions

    def test_isolation_same_channel_two_users(self, mt_store, clean_db):
        """The critical test: two users, same channel, no leak."""
        u1 = mt_store.get_or_create_user("111", "u1")
        u2 = mt_store.get_or_create_user("222", "u2")
        mt_store.add_channel(u1, "DOAC", "https://youtube.com/@doac")
        mt_store.add_channel(u2, "DOAC", "https://youtube.com/@doac")

        u1_channels = mt_store.list_channels(u1)
        u2_channels = mt_store.list_channels(u2)
        assert len(u1_channels) == 1
        assert len(u2_channels) == 1
        # Both see the same channel name, but their lists are independent
        assert u1_channels[0]["name"] == "DOAC"
        assert u2_channels[0]["name"] == "DOAC"

    def test_remove_channel_only_affects_one_user(self, mt_store, clean_db):
        u1 = mt_store.get_or_create_user("111", "u1")
        u2 = mt_store.get_or_create_user("222", "u2")
        mt_store.add_channel(u1, "DOAC", "https://youtube.com/@doac")
        mt_store.add_channel(u2, "DOAC", "https://youtube.com/@doac")

        removed = mt_store.remove_channel(u1, 0)
        assert removed is not None
        assert removed["name"] == "DOAC"

        # u1 no longer sees it, u2 still does
        assert len(mt_store.list_channels(u1)) == 0
        assert len(mt_store.list_channels(u2)) == 1

    def test_digest_isolation(self, mt_store, clean_db):
        """Digests are per-user: u1's digest shouldn't show as done for u2."""
        u1 = mt_store.get_or_create_user("111", "u1")
        u2 = mt_store.get_or_create_user("222", "u2")
        vid = mt_store.get_or_create_video("abc123", "Test Video")

        # u1 has a done digest; u2 does not
        mt_store.mark_digest_done(u1, vid, "u1's brief")
        assert mt_store.is_digested(u1, "abc123") is True
        assert mt_store.is_digested(u2, "abc123") is False

        assert mt_store.get_digest(u1, "abc123") == "u1's brief"
        assert mt_store.get_digest(u2, "abc123") is None

    def test_get_or_create_video_idempotent(self, mt_store, clean_db):
        v1 = mt_store.get_or_create_video("abc", "Title")
        v2 = mt_store.get_or_create_video("abc", "Updated Title")
        assert v1 == v2  # same youtube_id → same video row

    def test_latest_digest_per_user(self, mt_store, clean_db):
        u1 = mt_store.get_or_create_user("111", "u1")
        v1 = mt_store.get_or_create_video("vid1", "Video 1")
        mt_store.mark_digest_done(u1, v1, "brief 1")
        v2 = mt_store.get_or_create_video("vid2", "Video 2")
        mt_store.mark_digest_done(u1, v2, "brief 2")

        latest = mt_store.latest_digest(u1)
        assert latest is not None

    def test_user_stats(self, mt_store, clean_db):
        u1 = mt_store.get_or_create_user("111", "u1")
        mt_store.add_channel(u1, "DOAC", "https://youtube.com/@doac")
        v1 = mt_store.get_or_create_video("v1", "V1")
        v2 = mt_store.get_or_create_video("v2", "V2")
        mt_store.mark_digest_done(u1, v1, "b1")
        mt_store.mark_digest_skipped(u1, v2)

        stats = mt_store.user_stats(u1)
        assert stats["channels"] == 1
        assert stats["done"] == 1
        assert stats["skipped"] == 1

    def test_add_channel_returns_true_only_on_new_subscription(
        self, mt_store, clean_db
    ):
        u1 = mt_store.get_or_create_user("111", "u1")
        assert mt_store.add_channel(u1, "DOAC", "https://youtube.com/@doac") is True
        assert mt_store.add_channel(u1, "DOAC", "https://youtube.com/@doac") is False
