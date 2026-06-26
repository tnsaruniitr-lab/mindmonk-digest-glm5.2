# MindMonk — Architecture

> Multi-tenant YouTube→podcast-digest platform. Telegram-native. Target: 1,000 users. Self-funded, Whisper-primary.

## Design principles

1. **Transcripts and brief-sections 1–3 are video-scoped** (identical for every subscriber). Only section 4 (tailored learnings) is user-scoped. This collapses cost from O(users × videos) to O(unique videos + users × section-4).
2. **Idempotent jobs.** Every job carries a composite key; retries never double-process or double-deliver.
3. **Webhook, not polling.** Telegram webhooks (not getUpdates) for command intake at scale. Fixes the race condition we hit in dev.
4. **Singleton poller, parallel workers.** One poller globally deduplicates channel polling; N workers fan out.
5. **Rate-limit delivery.** 1 msg/sec/chat, 30/sec global — enforced via a Redis-backed limiter.
6. **Captions are a fallback, Whisper is primary** (per product decision). Cost accepted.

## System topology

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
   │        │                                │              │
   │        ▼                                ▼              │
   │  ┌──────────────────────────────────────────────────┐ │
   │  │                   Postgres                        │ │
   │  │  users · channels · subscriptions · videos        │ │
   │  │  transcripts · summaries · digests · usage_ledger │ │
   │  └──────────────────────────────────────────────────┘ │
   │        ▲                                               │
   │        │                                               │
   │  ┌─────┴──────┐  yt-dlp via residential proxy         │
   │  │  (all yt   │  (proxy shared across poller+workers)  │
   │  │   workers) │                                       │
   │  └────────────┘                                       │
   └───────────────────────────────────────────────────────┘
```

## Services (Railway)

| Service | Replicas | Role | Scales on |
|---|---|---|---|
| `api` | N | Telegram webhook receiver + landing page (HTTP) | request rate |
| `poller` | **1 (singleton)** | Polls each unique channel once, enqueues video jobs | n/a (fixed) |
| `worker` | N | Transcript/summary/digest/deliver job consumer | queue depth |
| `postgres` | 1 | Source of truth | storage |
| `redis` | 1 | Job queue (RQ) + cache + rate-limit counters | memory |

## Job taxonomy (Redis queue)

| Queue | Job | Key (idempotency) | Produced by | Consumed by |
|---|---|---|---|---|
| `transcripts` | Fetch + cache transcript for video | `video_id` | poller | worker |
| `summaries` | Gen sections 1–3 (video-scoped) | `video_id` | poller / fetch | worker |
| `digests` | Gen section 4 + assemble + deliver | `(user_id, video_id)` | summary-done fan-out | worker |

Each job is idempotent: re-enqueuing the same key is a no-op (checked against `status` in DB before work).

## Cost model (self-funded, 1k users, Whisper-primary)

| Component | Per month | Notes |
|---|---|---|
| Whisper transcripts | ~$1,500 | 3,000 unique videos/wk × 15% no-caption × $0.72 avg |
| Global summaries (sec 1–3) | ~$480 | 3,000 videos × ~$0.04 each |
| Per-user section 4 | ~$640 | 1,000 users × ~3 digests/wk × $0.05 |
| Proxy (Whisper audio) | ~$620 | bandwidth per GB |
| Postgres + Redis + compute | ~$120 | Railway |
| **Total** | **~$3,360/mo** | all you — see risks §6 |

**Levers you control**: per-user quotas (cap free-tier usage), captions-as-fallback (would cut ~$2k), BYOK up-sell (future revenue).

## Failure handling

- **Per-job retries** with exponential backoff (3 attempts) on transient errors (Whisper 429, Telegram throttle, proxy hiccup)
- **Dead-letter queue** for jobs that exhaust retries — surfaced in `/admin status`
- **Soft vs hard failures**: bot-wall → retry with client rotation; deleted video → mark skipped (terminal)
- **Self-healing scheduler**: poller backfills missed videos on next poll

## Security & isolation

- **Per-user encryption at rest** for `llm_api_key` (if BYOK later) — AES-GCM with a master key in Railway env
- **Telegram chat_id is the auth token** — no separate auth needed for Telegram-native product
- **Row-level isolation in app layer** — every query scoped by `user_id`; integration tests assert no cross-user leakage
- **No secrets in code** — kill hardcoded project IDs (current `bot.py` has one)

## Risks

1. **Cost runaway** — the #1 risk at 1k users self-funded. Mitigation: hard per-user quotas, usage dashboards, and the option to flip to captions-primary if needed.
2. **YouTube ToS at scale** — scraping 1,500 channels risks IP/account actions. Proxy mitigates, doesn't eliminate.
3. **Telegram delivery bursts** — one popular video → 1,000 near-simultaneous sends → throttling. Mitigation: stagger + rate-limit.
4. **Whisper quota** — OpenAI rate limits per key; need key rotation pool or BYOK.
5. **Multi-tenant data leak** — tests must assert isolation rigorously.
