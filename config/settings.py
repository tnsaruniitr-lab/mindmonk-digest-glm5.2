"""Configuration loading: environment vars (.env) + YAML files -> typed settings.

Environment variables hold secrets (Telegram token, LLM API key) and path
overrides. YAML files (config.yaml, profile.yaml) hold user-editable content
like the channel list and your profile.

Run ``load_settings()`` once at startup; it returns a fully validated
``Settings`` object.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# YAML-backed config models
# --------------------------------------------------------------------------- #
class ChannelConfig(BaseModel):
    name: str
    url: str


class AppConfig(BaseModel):
    """Contents of config.yaml."""

    poll_interval_minutes: int = 30
    min_duration_seconds: int = 1200
    max_per_cycle: int = 3  # cap LLM calls per cycle (cost + rate limits)
    languages: list[str] = Field(default_factory=lambda: ["en"])
    notify_on_no_transcript: bool = False
    channels: list[ChannelConfig] = Field(default_factory=list)


class Profile(BaseModel):
    """Contents of profile.yaml — drives section 4 (tailored learnings)."""

    profession: str = ""
    skill_level: str = ""
    goals: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    current_focus: str = ""

    def as_prompt_block(self) -> str:
        """Render the profile as a compact text block for the LLM prompt."""
        lines: list[str] = []
        if self.profession:
            lines.append(f"- Profession: {self.profession}")
        if self.skill_level:
            lines.append(f"- Skill level: {self.skill_level}")
        if self.goals:
            lines.append("- Goals:")
            lines.extend(f"  - {g}" for g in self.goals)
        if self.interests:
            lines.append("- Interests:")
            lines.extend(f"  - {i}" for i in self.interests)
        if self.current_focus:
            lines.append(f"- Current focus: {self.current_focus}")
        return "\n".join(lines) if lines else "- (no profile provided)"


# --------------------------------------------------------------------------- #
# Environment-backed settings (secrets + provider config + paths)
# --------------------------------------------------------------------------- #
class LLMConfig(BaseModel):
    provider: str = "openai"  # "openai" | "anthropic"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    grader_model: str = ""  # optional: routes section 3 to another model

    @field_validator("provider")
    @classmethod
    def _validate_provider(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("openai", "anthropic"):
            raise ValueError(
                f"LLM_PROVIDER must be 'openai' or 'anthropic', got {v!r}"
            )
        return v


class TelegramConfig(BaseModel):
    bot_token: str = ""
    chat_id: str = ""


class Settings(BaseModel):
    app: AppConfig
    profile: Profile
    telegram: TelegramConfig
    llm: LLMConfig
    groq_api_key: str = ""  # optional: enables Groq Whisper transcription fallback
    config_path: Path
    profile_path: Path
    db_path: Path


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #
def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _load_yaml_preferring_env(env_name: str, path: Path) -> dict:
    """Prefer an inline YAML env var (e.g. CONFIG_YAML); fall back to a file.

    On Railway we ship config/profile as variables (no files in the image),
    so CONFIG_YAML / PROFILE_YAML take precedence when present. Locally, the
    file path is used instead.
    """
    inline = os.getenv(env_name, "").strip()
    if inline:
        return yaml.safe_load(inline) or {}
    return _load_yaml(path)


def load_settings() -> Settings:
    """Load .env, config.yaml and profile.yaml into a validated Settings."""
    load_dotenv(PROJECT_ROOT / ".env")

    config_path = Path(os.getenv("CONFIG_PATH", PROJECT_ROOT / "config.yaml"))
    profile_path = Path(os.getenv("PROFILE_PATH", PROJECT_ROOT / "profile.yaml"))
    db_path = Path(os.getenv("DB_PATH", PROJECT_ROOT / "podcast-digest.db"))

    app = AppConfig(**_load_yaml_preferring_env("CONFIG_YAML", config_path))
    profile = Profile(**_load_yaml_preferring_env("PROFILE_YAML", profile_path))

    return Settings(
        app=app,
        profile=profile,
        telegram=TelegramConfig(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        ),
        llm=LLMConfig(
            provider=os.getenv("LLM_PROVIDER", "openai"),
            api_key=os.getenv("LLM_API_KEY", ""),
            model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
            grader_model=os.getenv("GRADER_MODEL", ""),
        ),
        groq_api_key=os.getenv("GROQ_API_KEY", ""),
        config_path=config_path,
        profile_path=profile_path,
        db_path=db_path,
    )
