"""LLM-backed summarization with a provider abstraction.

Supports OpenAI and Anthropic, selected via LLM_PROVIDER. The same interface
(``generate``) wraps both, so the pipeline never branches on provider.

The brief is two calls:
  - main call (LLM_MODEL): sections 1, 2, 4
  - grading call (GRADER_MODEL or LLM_MODEL): section 3

Both are retried with exponential backoff on transient errors.
"""
from __future__ import annotations

import logging
import time
from typing import Protocol

from anthropic import Anthropic
from openai import OpenAI

from config.settings import LLMConfig, Profile
from .models import Transcript
from . import prompts

log = logging.getLogger(__name__)

MAX_RETRIES = 3
INITIAL_BACKOFF = 2.0  # seconds


class LLMError(Exception):
    """Raised when the LLM call fails after retries."""


# --------------------------------------------------------------------------- #
# Provider abstraction
# --------------------------------------------------------------------------- #
class LLMClient(Protocol):
    """Minimal interface both provider wrappers implement."""

    def generate(self, system: str, user: str, model: str) -> str: ...


class OpenAIClient:
    def __init__(self, api_key: str):
        self._client = OpenAI(api_key=api_key)

    def generate(self, system: str, user: str, model: str) -> str:
        resp = self._client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or ""


class AnthropicClient:
    def __init__(self, api_key: str):
        self._client = Anthropic(api_key=api_key)

    def generate(self, system: str, user: str, model: str) -> str:
        resp = self._client.messages.create(
            model=model,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        # Concatenate text blocks.
        return "".join(
            block.text for block in resp.content if hasattr(block, "text")
        )


def _build_client(config: LLMConfig) -> LLMClient:
    if not config.api_key or config.api_key.startswith("your-"):
        raise LLMError(
            "LLM_API_KEY is not set. Edit .env and add your real API key "
            "(see .env.example)."
        )
    if config.provider == "openai":
        return OpenAIClient(config.api_key)
    if config.provider == "anthropic":
        return AnthropicClient(config.api_key)
    raise LLMError(f"Unknown LLM provider: {config.provider!r}")


# --------------------------------------------------------------------------- #
# Summarizer
# --------------------------------------------------------------------------- #
class Summarizer:
    """Produces the full four-section brief from a transcript."""

    def __init__(self, config: LLMConfig):
        self._config = config
        self._client = _build_client(config)
        self._grader_model = config.grader_model or config.model

    def summarize(self, transcript: Transcript, profile: Profile) -> str:
        """Run both calls and assemble the final brief in section order.

        Section order: insights (1) → patterns (2) → grading (3) → learnings (4).
        Sections 1/2/4 come from the main call as a single block; we splice
        the grading block (section 3) between patterns and learnings.
        """
        # Main call: sections 1, 2, 4.
        main_text = self._call_with_retry(
            prompts.build_main_prompt(transcript, profile),
            self._config.model,
            "main",
        )
        # Grading call: section 3 (possibly a different model).
        grading_text = self._call_with_retry(
            prompts.build_grading_prompt(transcript),
            self._grader_model,
            "grading",
        )

        return _assemble_brief(transcript, main_text, grading_text)

    # ------------------------------------------------------------------ #
    def _call_with_retry(
        self, user_prompt: str, model: str, label: str
    ) -> str:
        system = prompts.system_prompt()
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return self._client.generate(system, user_prompt, model)
            except Exception as exc:  # noqa: BLE001 - retry any provider error
                last_exc = exc
                if attempt == MAX_RETRIES:
                    break
                backoff = INITIAL_BACKOFF * (2 ** (attempt - 1))
                log.warning(
                    "LLM %s call failed (attempt %d/%d): %s — retrying in %.1fs",
                    label,
                    attempt,
                    MAX_RETRIES,
                    exc,
                    backoff,
                )
                time.sleep(backoff)
        raise LLMError(f"LLM {label} call failed after {MAX_RETRIES} attempts") from last_exc


# --------------------------------------------------------------------------- #
# Brief assembly
# --------------------------------------------------------------------------- #
def _assemble_brief(
    transcript: Transcript, main_text: str, grading_text: str
) -> str:
    """Combine header + sections in the final order (1,2,3,4).

    The main call emits sections 1, 2, 4 in order; we split section 4
    (Tailored Learnings) off, insert grading, then re-append section 4 so the
    final order is insights → patterns → grading → learnings.
    """
    header = (
        f"🎙 *{transcript.video.title}*\n"
        f"📺 {transcript.video.channel.name}\n"
        f"🔗 {transcript.video.url}\n"
    )

    s4_marker = "### 🎯 Tailored Learnings"
    if s4_marker in main_text:
        before_s4, _, s4 = main_text.partition(s4_marker)
        s4_block = s4_marker + s4
    else:
        # Fallback: if markers drift, keep main as-is and append grading.
        before_s4, s4_block = main_text, ""

    parts = [header.strip(), "", before_s4.strip(), "", grading_text.strip()]
    if s4_block.strip():
        parts.extend(["", s4_block.strip()])
    return "\n".join(p for p in parts if p is not None).strip() + "\n"
