"""Tests for Telegram message splitting.

Verifies: no split when small, section-aware splitting, hard splits,
never exceeding the limit, and the best-split-point priority chain.
"""

from __future__ import annotations

from src.telegram import _split_message, _best_split_point


class TestSplitMessage:
    def test_short_message_single_chunk(self):
        assert _split_message("hello", 4000) == ["hello"]

    def test_exact_limit_not_split(self):
        text = "x" * 4000
        assert len(_split_message(text, 4000)) == 1

    def test_over_limit_splits(self):
        text = "x" * 10000
        chunks = _split_message(text, 4000)
        assert len(chunks) >= 2
        assert all(len(c) <= 4000 for c in chunks)

    def test_no_content_lost(self):
        text = "word " * 3000  # ~15000 chars
        chunks = _split_message(text, 4000)
        rejoined = "".join(chunks)
        # all original words preserved (whitespace may shift at boundaries)
        assert rejoined.replace(" ", "").count("word") == 3000

    def test_section_header_split_preferred(self):
        text = (
            "### Header 1\n" + ("line\n" * 1500) + "\n### Header 2\n" + ("y\n" * 1500)
        )
        chunks = _split_message(text, 4000)
        assert len(chunks) >= 2
        assert all(len(c) <= 4000 for c in chunks)


class TestBestSplitPoint:
    def test_returns_limit_for_tiny_input(self):
        # window shorter than max_len
        assert _best_split_point("abc", 4000) == 4000  # falls through to max_len

    def test_prefers_section_header(self):
        # Place the section header near the 4000 boundary so it's the best cut.
        text = "para " * 700 + "\n### Section\n" + "more " * 200
        cut = _best_split_point(text, 4000)
        # the cut should land at or before a ### marker if one is in window
        assert cut <= 4000
        # verify a section marker exists at or near the cut point
        assert "###" in text[max(0, cut - 10) : cut + 20]

    def test_falls_back_to_newline(self):
        text = "line\n" * 1000
        cut = _best_split_point(text, 4000)
        assert text[cut] == "\n" or text[cut - 1] == "\n"

    def test_hard_split_on_space(self):
        text = "a" * 3990 + " b" + "c" * 100
        cut = _best_split_point(text, 4000)
        assert cut <= 4000
