# MindMonk — Architecture

> Living document. Covers the **current state** (what we have), the **target architecture** (what we're building toward for 1,000 users), and the **engineering best practices** that govern both.

---

## Table of contents
1. [Current state — what we have today](#1-current-state--what-we-have-today)
2. [Target architecture — what we're building](#2-target-architecture--what-were-building)
3. [The core optimization: video-scoped vs user-scoped](#3-the-core-optimization-video-scoped-vs-user-scoped)
4. [Data model](#4-data-model)
5. [Job taxonomy & queues](#5-job-taxonomy--queues)
6. [Cost model (1k users, self-funded)](#6-cost-model-1k-users-self-funded)
7. [Failure handling & resilience](#7-failure-handling--resilience)
8. [Security & multi-tenant isolation](#8-security--multi-tenant-isolation)
9. [Risks & mitigations](#9-risks--mitigations)
10. [Engineering best practices](#10-engineering-best-practices)

---

## 1. Current state — what we have today

MindMonk is a **single-user prototype** that's **live in production** and working. It successfully turns YouTube podcasts into 4-section AI briefs delivered via Telegram. Here's exactly what exists and what it does well — and where it falls short of multi-tenant.

### What works (production-verified)
- ✅ **Polling**: yt-dlp polls subscribed channels every 30 min via residential proxy
- ✅ **Transcript waterfall**: Whisper-primary (proxy → audio download → OpenAI whisper-1) → captions fallback, with explicit per-step logging
- ✅ **Brief generation**: Anthropic Sonnet 4.5 produces 4 sections (insights, patterns/anti-patterns, grading, tailored learnings)
- ✅ **Delivery**: Telegram bot with `/fetch`, `/channel`, `/add`, `/list`, `/status`, `/latest`; long messages auto-split at section boundaries
- ✅ **Dedup**: SQLite/Postgres stores processed videos (idempotent across restarts)
- ✅ **Resilience**: bot-wall retry across 5 player clients; failed rows are retryable
- ✅ **Web presence**: landing page served publicly at `mindmonk-digest-glm52-production.up.railway.app`
- ✅ **Cost cascade**: cached videos return instantly (no re-spend on LLM)

### Current codebase (14 Python files, ~2,400 LOC)
```
podcast-digest/
├── main.py              # entrypoint: scheduler + bot + web in one process
├── config/settings.py   # env + YAML → typed settings (pydantic)
├── src/
│   ├── models.py        # Channel, Video, Transcript dataclasses
│   ├── store.py         # SQLite (local) + Postgres (prod) backends, same interface
│   ├── youtube.py       # yt-dlp polling, get_video, get_latest_video, bot-wall retry
│   ├── transcripts.py   # waterfall: Whisper-primary → captions fallback
│   ├── transcribe.py    # OpenAI Whisper (audio download + chunking + API)
│   ├── prompts.py       # the 4-section prompt templates
│   ├── summarizer.py    # Anthropic/OpenAI provider abstraction + brief assembly
│   ├── pipeline.py      # orchestration + on-demand methods (/fetch, /channel)
│   ├── telegram.py      # scheduled-digest sender + message splitter
│   ├── bot.py           # interactive Telegram command handler
│   └── web.py           # static landing page + /debug endpoints
├── landing/             # cinematic hero video landing page (HTML/CSS/JS)
├── systemd/             # systemd unit (legacy, pre-Railway)
├── Dockerfile           # python:3.12-slim worker image
└── railway.toml         # Railway worker config
```

### Where it falls short of multi-tenant (the gap)
| Limitation | Why it blocks 1,000 users |
|---|---|
| **Single hardcoded user** | One `TELEGRAM_CHAT_ID`, one profile, flat channel list — no user concept |
| **Global dedup, not per-user** | `processed_videos` marks a video done globally; a second user would never get it |
| **No transcript/summary caching** | Each fetch re-transcribes/re-summarizes; costs scale O(users × videos) |
| **getUpdates long-polling** | Doesn't scale past one consumer; race conditions (the bug we hit) |
| **Single process, global lock** | `threading.Lock` around DB; no horizontal scaling |
| **No job queue** | Sync processing; a burst of fetches blocks the bot |
| **No tests, no CI** | Zero automated safety net; changes are risky |
| **No migrations** | `CREATE TABLE IF NOT EXISTS` — no schema evolution |
| **Hardcoded project ID** | Railway project ID in `bot.py` (security/maintainability) |
| **No quotas** | One user could drain the LLM budget |

---

## 2. Target architecture — what we're building

```
                      ┌──────────────────────┐
                      │   Telegram Bot API    │
                      └───────┬──────────┬────┘
                   webhook    │          │  sendMessage
                              ▼          ▼
   ┌───────────────────────────────────────────────────────┐
   │                   Railway project                      │
   │                                                        │
   │  ┌────────────┐   enqueue    ┌──────────────────────┐  │
   │  │  api (N)   │─────────────▶│       Redis           │  │
   │  │ webhook +  │              │  (queue: RQ + cache   │  │
   │  │  landing   │◀──reply──── │   + rate-limit tokens)│  │
   │  └────────────┘              └──────────┬───────────┘  │
   │                                          │ dispatch     │
   │  ┌────────────┐              ┌──────────▼───────────┐  │
   │  │ poller (1) │──enqueue────▶│   worker pool (N)    │  │
   │  │ singleton  │   video jobs │  transcript/summary/  │  │
   │  │ polls each │              │  digest/deliver jobs  │  │
   │  │  channel   │              └──────────┬───────────┘  │
   │  │   once     │                         │              │
   │  └─────┬──────┘                         │              │
   │        │                                ▼              │
   │        ▼                          ┌──────────┐         │
   │  ┌──────────────────────────┐     │ Postgres │         │
   │  │ users·channels·subs·vids │◀────┤ (source  │         │
   │  │ transcripts·summaries·   │     │  of truth)│        │
   │  │ digests·usage_ledger     │     └──────────┘         │
   │  └──────────────────────────┘                          │
   │        ▲                                               │
   │        │ yt-dlp via residential proxy (shared)          │
   │  ┌─────┴──────┐                                       │
   │  │ (poller +  │                                       │
   │  │  workers)  │                                       │
   │  └────────────┘                                       │
   └────────────────────────────────────────────────────────┘
```

### Services (Railway)
| Service | Replicas | Role | Scales on |
|---|---|---|---|
| `api` | N | Telegram webhook receiver + landing page (HTTP) | request rate |
| `poller` | **1 (singleton)** | Polls each unique channel once; enqueues video jobs | n/a (fixed) |
| `worker` | N | Transcript/summary/digest/deliver job consumer | queue depth |
| `postgres` | 1 | Source of truth | storage |
| `redis` | 1 | Job queue (RQ) + transcript/summary cache + rate-limit counters | memory |

### Why each choice
- **Webhooks, not long-polling**: getUpdates doesn't scale past one consumer and caused the race bug in dev. Webhooks are stateless and horizontally scalable.
- **Singleton poller**: avoids double-polling channels. One replica, or leader-elected.
- **Redis + RQ**: simple, standard job queue; also serves as cache + rate-limit token bucket. Alternative (Postgres `SKIP LOCKED`) viable through a few hundred users.
- **Worker pool**: real parallelism for transcript/summary/delivery; autoscaled on queue depth.

---

## 3. The core optimization: video-scoped vs user-scoped

> **This is the single most important architectural decision.** It's the difference between ~$15k/mo and ~$3.4k/mo at 1,000 users.

Transcripts and brief sections 1–3 are **video-scoped** (identical for every subscriber — cached globally). Only section 4 (tailored learnings) is **user-scoped** (depends on the user's profile).

```
For a new video with 200 subscribers:
  WITHOUT optimization:  200 transcripts + 200 full summaries   = O(users × videos)
  WITH optimization:     1 transcript + 1 summary (1-3) + 200 section-4 calls
```

| Operation | Scope | Cost |
|---|---|---|
| Transcript (Whisper) | video | ~$0.72/video (2h podcast) — computed ONCE |
| Sections 1–3 (insights/patterns/grading) | video | ~$0.04/video — computed ONCE |
| Section 4 (tailored learnings) | user | ~$0.05/digest — per user |

Implemented in **Phase 3**.

---

## 4. Data model

```sql
users          (id, telegram_chat_id, telegram_user_id, created_at, tier,
                llm_provider, llm_api_key_enc, profile_yaml, preferences_json,
                usage_reset_at, deleted_at)

channels       (id, youtube_handle, name, url, last_polled_at, poll_error_count)

subscriptions  (user_id, channel_id, added_at)           -- many-to-many

videos         (id, channel_id, youtube_id, title, duration_s, published_at,
                transcript_status)                        -- global

transcripts    (video_id, text, source, language, fetched_at)   -- GLOBAL cache

summaries      (video_id, section, content, model, generated_at) -- GLOBAL (sections 1-3)

digests        (user_id, video_id, tailored_section, full_brief,                -- PER-USER
                status, delivered_at, tokens_used, cost_usd)    -- section 4 + assembled brief

usage_ledger   (user_id, date, videos_processed, tokens_in, tokens_out, cost_usd)
```

Key: `transcripts` + `summaries` are **video-scoped** (shared). `digests` is **user-scoped**. `subscriptions` is the fan-out link.

---

## 5. Job taxonomy & queues

| Queue | Job | Key (idempotency) | Produced by | Consumed by |
|---|---|---|---|---|
| `transcripts` | Fetch + cache transcript | `video_id` | poller | worker |
| `summaries` | Gen sections 1–3 (video-scoped) | `video_id` | transcript-done | worker |
| `digests` | Gen section 4 + assemble + deliver | `(user_id, video_id)` | summary-done fan-out | worker |

Every job is **idempotent**: re-enqueuing the same key checks DB status before working. Retries never double-process or double-deliver.

---

## 6. Cost model (1k users, self-funded)

Steady state: 1,000 users, ~1,500 unique channels, ~3,000 unique videos/week.

| Component | /month | Notes |
|---|---|---|
| Whisper transcripts | ~$1,500 | 3,000 videos/wk × 15% no-caption × $0.72 |
| Global summaries (sec 1–3) | ~$480 | ~$0.04/video |
| Per-user section 4 | ~$640 | 1,000 users × ~3/wk × $0.05 |
| Proxy (Whisper audio) | ~$620 | bandwidth |
| Postgres + Redis + compute | ~$120 | Railway |
| **Total** | **~$3,360/mo** | self-funded |

**Levers** (without changing architecture): per-user quotas, captions-as-fallback (cuts ~$2k), BYOK up-sell (future).

---

## 7. Failure handling & resilience

- **Per-job retries** with exponential backoff (3 attempts) on transient errors (Whisper 429, Telegram throttle, proxy hiccup)
- **Dead-letter queue** for exhausted retries — surfaced in `/admin status`
- **Soft vs hard failures**: bot-wall → retry with client rotation; deleted video → mark skipped (terminal, no retry)
- **Self-healing poller**: backfills missed videos on next poll after downtime
- **Whisper key rotation pool**: multiple OpenAI keys round-robin to dodge per-key rate limits
- **Proxy failover**: primary 429s → rotate to backup proxy

---

## 8. Security & multi-tenant isolation

- **Telegram chat_id is the auth token** — no separate auth needed for a Telegram-native product
- **Row-level isolation in app layer** — every query scoped by `user_id`; integration tests assert no cross-user leakage
- **Per-user encryption at rest** for `llm_api_key` (future BYOK) — AES-GCM with master key in Railway env
- **No secrets in code** — remove hardcoded project IDs (current `bot.py` has one)
- **Input validation** on all user-supplied URLs; `/fetch` rate-limited per user
- **Data retention policy**: prune old transcripts/summaries after N days (cost)

---

## 9. Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| **Cost runaway** (self-funded) | High | Hard per-user quotas, daily cost cap + circuit-breaker, usage dashboard |
| **YouTube ToS at scale** | Medium | Proxy mitigates IP bans; consider official Data API for metadata at volume |
| **Telegram delivery bursts** (1 popular video → 1k sends) | High | Stagger + rate-limit (1/sec/chat, 30/sec global) |
| **Whisper per-key quota** | Medium | Key rotation pool |
| **Multi-tenant data leak** | Medium | Rigorous isolation tests |
| **Whisper cost dominates** | High | Reversible to captions-primary via config if needed |

---

## 10. Engineering best practices

These govern all work on MindMonk — current and future. They're not optional; they're how we avoid rebuilding the prototype's mess at scale.

### Testing
- **pytest** as the test runner; tests live in `tests/`
- **Unit tests** for pure logic (splitter, URL detection, prompt assembly, brief ordering)
- **Integration tests** for DB-bound logic (store round-trips, multi-user isolation)
- **Test DB** via testcontainers or a docker-compose fixture (never test against prod)
- **Target coverage**: critical paths (isolation, cost accounting, dedup) 100%; rest pragmatic

### Migrations
- **Alembic** for all schema changes — no more `CREATE TABLE IF NOT EXISTS`
- Migrations are **forward-only** in prod; tested locally first
- `alembic upgrade head` must build the schema from scratch on an empty DB (CI verifies this)
- Backfill migrations for data, separate from schema migrations

### CI/CD
- **GitHub Actions** on every push: `ruff` → `mypy` → `pytest`
- **Fail-fast**: any red step blocks the deploy
- Railway **auto-deploys from `main`** (already wired); CI gate before deploy
- **Migration step** runs as part of deploy (pre-release hook)

### Code quality
- **ruff** for lint + formatting (replaces black + isort + flake8)
- **mypy** for type checking (strict on `src/`, gradual elsewhere)
- **No hardcoded secrets/IDs** anywhere — all via env or config
- **Small, focused commits** with clear messages

### Observability
- **structlog** for structured (JSON) logging with job-id + user-id context
- **Sentry** for errors (stack + context + breadcrumbs)
- **Health endpoints**: `/health` (api), `/health/worker` (queue depth), `/health/poller` (last poll)
- **Metrics**: queue depth, jobs/min, cost/day, error rate — exported to Grafana/Loki via Railway log drain

### Dependency management
- Pinned versions in `requirements.txt` with lower bounds (`>=`)
- **`pip-audit`** in CI for known vulnerabilities
- Quarterly dependency review
- Justify each new dep in the commit that adds it

### Configuration & secrets
- **pydantic-settings** for typed config (already in place)
- 12-factor: all config via env vars; no files in the image
- **Secrets never committed** (`.gitignore` covers `.env`, `*.yaml`, `*.db`)
- Railway variables are the single source of runtime config

### Local development
- **docker-compose.yml** spins up: postgres + redis + the app (hot-reload)
- `.env.example` documents every required var
- A single `make dev` / script to start everything
- README documents the setup

### Documentation
- **docs/** holds: SPEC, ARCHITECTURE (this file), PHASES, plus an INDEX linking them
- **Architecture Decision Records (ADRs)** in `docs/adr/` for significant choices (why Redis+RQ over alternatives, why webhooks, etc.)
- README stays the quickstart; deep design lives in docs

### Operational readiness
- **Runbook** in `docs/` for common failures (bot-wall, Whisper down, cost spike)
- **Backups**: Postgres automated backups (Railway-managed); test restore quarterly
- **Graceful shutdown**: SIGTERM drains the queue before exit (no lost work)

---

*This document evolves with the system. When architecture changes, update it in the same PR — docs are code.*
