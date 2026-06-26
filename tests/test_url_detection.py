"""Tests for URL classification (video vs channel) in the bot.

Drives the paste-routing: a video URL → /fetch, a channel URL → /add.
"""

from __future__ import annotations

from src.bot import _is_video_url


class TestIsVideoUrl:
    def test_youtu_be_is_video(self):
        assert _is_video_url("https://youtu.be/abc123") is True

    def test_watch_v_is_video(self):
        assert _is_video_url("https://www.youtube.com/watch?v=abc") is True
        assert _is_video_url("https://youtube.com/watch?v=abc&t=10") is True

    def test_embed_is_video(self):
        assert _is_video_url("https://www.youtube.com/embed/abc") is True

    def test_shorts_is_video(self):
        assert _is_video_url("https://www.youtube.com/shorts/abc") is True

    def test_handle_is_not_video(self):
        assert _is_video_url("https://www.youtube.com/@TheDiaryOfACEO") is False
        assert _is_video_url("https://www.youtube.com/@TheDiaryOfACEO/videos") is False

    def test_channel_path_is_not_video(self):
        assert _is_video_url("https://www.youtube.com/channel/UCxxxx") is False

    def test_user_path_is_not_video(self):
        assert _is_video_url("https://www.youtube.com/user/someuser") is False

    def test_non_youtube_is_false(self):
        # _is_video_url is only called on URLs already known to be youtube,
        # so it pattern-matches broadly. A non-youtube URL with watch?v= is
        # an edge case we don't route through this function.
        assert _is_video_url("https://example.com/some/page") is False

    def test_plain_text_is_false(self):
        assert _is_video_url("hello there") is False
