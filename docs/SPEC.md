# MindMonk — Product Specification

> YouTube podcasts → structured AI briefs → Telegram.
> Multi-tenant. Telegram-native. Self-funded. Whisper-primary.

## Vision

A user subscribes to YouTube channels they care about. Whenever a new long-form episode drops, MindMonk sends them a sharp, structured brief — not a summary, an *analysis*. The brief is personalized: section 4 ties the episode's ideas to the user's goals.

The product is **Telegram-native**: onboarding, channel management, and delivery all happen in one chat. No web app required (a landing page exists for discovery).

## Personas

- **Reader** (the user): busy, values time, listens to long-form podcasts but can't watch them all. Wants signal, not noise.
- **Operator** (you): funds the platform, manages cost, onboards users, monitors health.

## Core value loop

```
subscribe → (MindMonk polls) → new episode → transcript → brief → deliver
                                                              ↑
                                              section 4 personalized to user
```

## The brief (product output)

Every brief has four sections:

1. **💡 Key insights** — 5–8 substantive ideas, bold headline + one sentence.
2. **🔁 Patterns & anti-patterns** — good thinking vs. flawed reasoning exhibited in the episode.
3. **🔍 Unbiased grading** — soundness / originality / honesty / practical value + a letter grade. Even-handed, not sycophantic.
4. **🎯 Tailored learnings** — ideas matched to *this user's* profile (profession, goals, current focus) → concrete action items.

**Sections 1–3 are video-scoped** (identical for every subscriber — cached globally).
**Section 4 is user-scoped** (depends on the user's profile — computed per user).

## Commands (Telegram)

| Command | Purpose | Tier |
|---|---|---|
| `/start` | Onboarding + profile setup | all |
| `/profile` | View / edit profile (drives section 4) | all |
| `/add <url>` | Subscribe to a channel | all |
| `/list` | Show subscribed channels | all |
| `/remove <n>` | Unsubscribe | all |
| `/fetch <url>` | On-demand brief for one video | all |
| `/channel <url>` | Brief a channel's latest video | all |
| `/status` | Worker + DB health (operator) | admin |
| `/usage` | This user's quota + cost | all |
| `/upgrade` | (Placeholder for paid tier) | all |
| `/help` | Command list | all |

## User profile (drives section 4)

Structured fields, set during onboarding, editable anytime:
- `profession`, `skill_level`
- `goals` (list)
- `interests` (list)
- `current_focus` (free text)

Richer profile → sharper tailored learnings.

## Business model

- **Self-funded by the operator** (your decision).
- Free for users. Costs absorbed by the platform.
- Guardrails: per-user monthly quota, global daily cost cap, usage visibility.
- **Future option**: BYOK up-sell or paid tiers (out of scope for v1 multi-tenant).

## Constraints (non-functional)

| Constraint | Target |
|---|---|
| Users | 1,000 (validation gate in Phase 6) |
| Digest latency | p95 < 10 min from video publish |
| Telegram send rate | ≤ 1 msg/sec/chat, ≤ 30/sec global |
| Availability | self-healing; failed jobs retry, never silently lost |
| Cost | hard cap via circuit-breaker; daily burn visible |
| Data isolation | zero cross-user leakage (tested) |

## Transcript strategy

- **Whisper-primary** (product decision): audio download via residential proxy → OpenAI whisper-1. Universal coverage, highest quality.
- **Captions fallback**: yt-dlp captions if Whisper unavailable or fails.
- Reason accepted cost: reliability + coverage over frugality. Reversible via config.

## Scope boundaries

**In scope (multi-tenant v1):**
- Multi-user onboarding + isolation
- Per-user channels, profiles, digests
- Global transcript/summary dedup
- Job queue + worker pool
- Quotas + cost guardrails

**Out of scope (future):**
- Web dashboard (beyond landing page)
- Paid tiers / billing (Stripe)
- BYOK (users supply their own LLM key)
- Audio-based podcast sources (non-YouTube)
- Multi-language briefs (English only for now)
- Mobile app

## Success metrics

- Onboarding completion rate (start → first channel added)
- Active users / week
- Digests delivered / week
- Cost per active user / month
- p95 end-to-end latency
- Error rate (failed jobs / total)
