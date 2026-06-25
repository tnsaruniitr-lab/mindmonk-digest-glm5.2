"""Prompt templates for the podcast brief.

Two prompts back the four-section format:
  - MAIN_PROMPT produces sections 1, 2, and 4 (insights, patterns, learnings).
  - GRADING_PROMPT produces section 3 (unbiased grading) and can be routed to
    a different model via the optional GRADER_MODEL env var.

Keeping grading in its own call means it can run with a different (stronger,
or differently-aligned) model, and keeps each prompt focused.
"""
from __future__ import annotations

from .models import Transcript
from config.settings import Profile


# --------------------------------------------------------------------------- #
# Shared system instruction
# --------------------------------------------------------------------------- #
_SYSTEM = (
    "You are a sharp, even-handed podcast analyst. You think critically, "
    "separate substance from hype, and write in clean Markdown. "
    "You never pad with filler — every line must earn its place."
)


# --------------------------------------------------------------------------- #
# Section 3: unbiased grading (separate call, optionally a different model)
# --------------------------------------------------------------------------- #
def build_grading_prompt(transcript: Transcript) -> str:
    """Prompt for an unbiased, critical evaluation of the podcast's ideas.

    The "specified LLM" framing is the grader itself: it grades the ideas as
    an independent evaluator, not as a fan of the podcast.
    """
    return f"""You are acting as an independent, unbiased grader evaluating the ideas and claims in a podcast transcript. You are NOT summarizing for the listener — you are judging the intellectual quality of what was said.

Grade the podcast on these dimensions:
- **Soundness of claims**: Are the arguments logical and evidence-based, or hand-wavy?
- **Originality**: Clichés/restatements vs. genuinely novel ideas.
- **Intellectual honesty**: Nuance and acknowledgment of trade-offs vs. overconfidence/cherry-picking.
- **Practical value**: Are the ideas actionable, or just entertaining talk?

Then give an overall **letter grade (A+ to F)** with a one-line justification.

Be rigorous and fair — neither harsh-for-its-own-sake nor sycophantic. If the podcast mixes strong and weak material, say so and grade the blend.

Use this exact structure:

### 🔍 Unbiased Grading

**Soundness:** <verdict + 1-2 sentences>
**Originality:** <verdict + 1-2 sentences>
**Intellectual honesty:** <verdict + 1-2 sentences>
**Practical value:** <verdict + 1-2 sentences>

**Overall grade:** <letter> — <one-line justification>

Transcript:
\"\"\"
{transcript.text}
\"\"\""""


# --------------------------------------------------------------------------- #
# Sections 1, 2, 4: insights, patterns, tailored learnings
# --------------------------------------------------------------------------- #
def build_main_prompt(transcript: Transcript, profile: Profile) -> str:
    """Prompt for key insights, patterns/anti-patterns, and tailored learnings.

    The profile (section 4) is injected so the LLM can tie takeaways to the
    user's profession, goals, and current focus.
    """
    return f"""Analyze the following podcast transcript and produce three sections in clean Markdown.

### 💡 Key Insights
The 5-8 most important ideas from the podcast. Each as a short bullet: one bold headline phrase, then 1 sentence of explanation. Skip filler and restatements — only ideas with real substance.

### 🔁 Patterns & Anti-Patterns
- **Patterns (good):** recurring modes of good thinking the speaker demonstrates — mental models, reasoning habits, or approaches worth copying. 2-4 bullets.
- **Anti-patterns (watch out):** flawed reasoning, logical gaps, cognitive biases, or bad advice that recurs. Be specific and fair — name what's weak and why. 2-4 bullets.

### 🎯 Tailored Learnings
Below is the listener's profile. Match the podcast's ideas to THIS person's goals, skills, and current focus, and give 3-6 concrete, personalized takeaways or action items. Each must be specific and actionable for them — not generic advice.

Listener profile:
{profile.as_prompt_block()}

Rules:
- Write only the three sections above, with the exact headers shown.
- Be concise and specific. No throat-clearing, no "in conclusion".
- Quote sparingly and only when a phrase is genuinely memorable.

Transcript:
\"\"\"
{transcript.text}
\"\"\""""


def system_prompt() -> str:
    """The shared system instruction for both calls."""
    return _SYSTEM
