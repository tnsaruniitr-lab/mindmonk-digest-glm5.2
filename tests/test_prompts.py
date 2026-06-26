"""Tests for prompt building and brief assembly ordering.

Verifies: all 4 sections present, profile injected, correct section
order (1→2→3→4) after assembly, header included.
"""

from __future__ import annotations

from src.models import LegacyChannel as Channel, LegacyVideo as Video, Transcript
from src.prompts import build_main_prompt, build_grading_prompt, system_prompt
from src.summarizer import _assemble_brief
from config.settings import Profile


def _make_transcript(text: str = "hello world transcript") -> Transcript:
    ch = Channel(name="Test", url="https://youtube.com/@t")
    vid = Video(
        video_id="abc",
        title="T",
        url="https://youtu.be/abc",
        duration_seconds=3600,
        channel=ch,
    )
    return Transcript(video=vid, text=text, language="en")


def _make_profile() -> Profile:
    return Profile(
        profession="Engineer",
        skill_level="Senior",
        goals=["learn ML"],
        interests=["systems"],
        current_focus="building things",
    )


class TestPrompts:
    def test_main_prompt_has_all_three_sections(self):
        t = _make_transcript()
        p = _make_profile()
        prompt = build_main_prompt(t, p)
        assert "### 💡 Key Insights" in prompt
        assert "### 🔁 Patterns & Anti-Patterns" in prompt
        assert "### 🎯 Tailored Learnings" in prompt

    def test_main_prompt_injects_profile(self):
        t = _make_transcript()
        p = _make_profile()
        prompt = build_main_prompt(t, p)
        assert "Engineer" in prompt
        assert "learn ML" in prompt
        assert "building things" in prompt

    def test_grading_prompt_has_grading_structure(self):
        t = _make_transcript()
        prompt = build_grading_prompt(t)
        assert "### 🔍 Unbiased Grading" in prompt
        assert "letter grade" in prompt.lower()
        assert "Soundness" in prompt

    def test_system_prompt_is_nonempty(self):
        assert len(system_prompt()) > 50


class TestBriefAssembly:
    def test_sections_in_correct_order(self):
        """After assembly: insights → patterns → grading → learnings."""
        t = _make_transcript()
        main = (
            "### 💡 Key Insights\n- a\n\n"
            "### 🔁 Patterns & Anti-Patterns\n- b\n\n"
            "### 🎯 Tailored Learnings\n- c"
        )
        grading = "### 🔍 Unbiased Grading\n**Overall grade:** B"
        brief = _assemble_brief(t, main, grading)

        positions = [
            brief.find("Key Insights"),
            brief.find("Patterns & Anti-Patterns"),
            brief.find("Unbiased Grading"),
            brief.find("Tailored Learnings"),
        ]
        # all present and in ascending order
        assert all(p >= 0 for p in positions), f"missing section: {positions}"
        assert positions == sorted(positions), f"wrong order: {positions}"

    def test_brief_includes_header(self):
        t = _make_transcript()
        main = "### 💡 Key Insights\n- a\n\n### 🎯 Tailored Learnings\n- c"
        grading = "### 🔍 Unbiased Grading\ngrade: B"
        brief = _assemble_brief(t, main, grading)
        assert t.video.title in brief
        assert t.video.url in brief
        assert t.video.channel.name in brief

    def test_assembly_handles_missing_section4(self):
        """If the LLM omits section 4, assembly should still produce output."""
        t = _make_transcript()
        main = "### 💡 Key Insights\n- a\n\n### 🔁 Patterns & Anti-Patterns\n- b"
        grading = "### 🔍 Unbiased Grading\ngrade: B"
        brief = _assemble_brief(t, main, grading)
        assert "Key Insights" in brief
        assert "Unbiased Grading" in brief
