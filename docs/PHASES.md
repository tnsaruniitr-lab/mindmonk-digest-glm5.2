# MindMonk — Phased Implementation Plan

> Target: single-user prototype → multi-tenant platform for 1,000 users.
> Self-funded. Whisper-primary. Telegram-native.

Each phase is **independently shippable**: code compiles, tests pass, deploy works, and a defined UAT passes before the next phase begins.

## Phase conventions

- **Checklist items** (`[ ]`) are discrete engineering tasks.
- **UAT** (User Acceptance Test) is the human-verifiable proof the phase works.
- **Exit gate**: all `[ ]` checked AND UAT passes AND pushed to GitHub AND Railway deploy succeeds.

---

## Phase 0 — Engineering foundations (no behavior change)

**Goal**: bring the codebase from prototype hygiene to production-ready baseline. Zero new features.

### Checklist
- [ ] Add `pytest` + a test Postgres DB (testcontainers or docker-compose fixture)
- [ ] Port the ad-hoc `/tmp` smoke tests into `tests/` (store round-trip, splitter, url detection)
- [ ] Add `ruff` (lint+format) and `mypy` (types) configs; fix existing violations
- [ ] Introduce Alembic for migrations; convert `CREATE TABLE IF NOT EXISTS` → initial migration
- [ ] Replace ad-hoc logging with `structlog` (JSON, job-id + user-id context)
- [ ] Remove all hardcoded IDs from code (Railway project id in `bot.py`; move to env)
- [ ] GitHub Actions CI: on push → ruff + mypy + pytest (fail the build on any red)
- [ ] Add `docker-compose.yml` for local dev (postgres + redis + the app)
- [ ] Document the local-dev setup in README

### UAT
- `pytest` passes locally and in CI (green check on a commit)
- `alembic upgrade head` creates the schema from scratch on an empty DB
- A push to `main` triggers CI and Railway auto-deploy; both green

### Exit gate
- CI badge green; `alembic upgrade head` works on empty DB; deploy succeeds with no behavior change.

---

## Phase 1 — Multi-tenant data model

**Goal**: schema supports multiple users, channels, subscriptions, and per-user digests. Still single-worker, still just you using it.

### Checklist
- [ ] Design + write Alembic migration for the full schema (see ARCHITECTURE.md):
  `users`, `channels`, `subscriptions`, `videos`, `transcripts`, `summaries`, `digests`, `usage_ledger`
- [ ] Backfill: insert the current single-user state (you + DOAC + your profile) as the first rows
- [ ] Refactor `Pipeline` to be user-aware: every operation takes a `user_id` / resolves channels from `subscriptions`
- [ ] Rewrite `Store` queries to be user-scoped (no more global `processed_videos`; use `digests` per user)
- [ ] Migration test: run on a copy of the prod DB, assert no data loss
- [ ] Integration tests: two synthetic users, same channel — assert no cross-user data leak

### UAT
- `/list` shows only YOUR channels; a second user's `/list` shows only theirs
- `/fetch` stores the digest under YOUR user record, not globally
- `alembic upgrade` runs cleanly on the live Railway Postgres with zero downtime

### Exit gate
- Multi-user data model live; you can onboard a second test user without affecting your data.

---

## Phase 2 — User onboarding + Telegram webhooks

**Goal**: new users can `/start`, set up a profile, add channels — fully self-serve. Switch from getUpdates long-polling to webhooks (fixes the race, scales to 1k users).

### Checklist
- [ ] Telegram webhook handler in `api` (`POST /tg/{secret}`) replacing the long-poll loop
- [ ] Set webhook on deploy (`setWebhook`); store secret in env
- [ ] `/start` onboarding flow: welcome → prompt for profession/goals/interests → store `profile_yaml`
- [ ] `/profile` command: show + edit the user's profile
- [ ] `/add` writes to `subscriptions` (user-scoped), dedupes per user
- [ ] `/list`, `/remove` operate on the user's subscriptions only
- [ ] Auth: every command resolves `telegram_user_id` → `users.id`; reject unknown users with a friendly onboarding nudge
- [ ] Webhook secret rotation docs

### UAT
- A brand-new user messages the bot → gets onboarding → sets profile → adds a channel → `/list` shows it
- Two users in parallel don't see each other's channels or profiles
- Webhook receives + handles 10 commands in 5 seconds without dropping any (the old getUpdates race is gone)

### Exit gate
- Self-serve onboarding works for a stranger with no help. Webhook is the command transport.

---

## Phase 3 — Global deduplication + job queue

**Goal**: the cost-collapse. Poll each channel once globally; transcript + sections 1–3 computed once per video; only section 4 is per-user. Introduce Redis + RQ worker pool.

### Checklist
- [ ] Add Redis service to Railway; add `redis` + `rq` to deps
- [ ] Singleton `poller` service: iterates `channels` by `last_polled_at`, enqueues `transcript:{video_id}` jobs
- [ ] Worker job: transcript fetch → cache in `transcripts` (global) → enqueue `summary:{video_id}`
- [ ] Worker job: summary sections 1–3 → cache in `summaries` (global) → fan out `digest:{user_id, video_id}` per subscriber
- [ ] Worker job: digest → section 4 per-user → assemble → enqueue delivery
- [ ] Idempotency: every job checks DB status before doing work; re-enqueue is safe
- [ ] Rate-limited sender: Redis token-bucket (1 msg/sec/chat, 30/sec global)
- [ ] Dead-letter queue + `/admin status` to view failed jobs
- [ ] Autoscale workers on queue depth (Railway metric)

### UAT
- Add a popular channel → 1 new video → exactly **1** transcript fetch + **1** summary (sections 1–3) in logs, regardless of subscriber count
- Two users subscribed to the same channel both get the same sections 1–3 but **different** section 4 (their profiles differ)
- A Whisper failure on one video doesn't block others (isolated)
- Sending 50 digests in a burst doesn't trigger Telegram's 30/sec throttle

### Exit gate
- Costs scale with unique videos, not users × videos. Fan-out delivery is rate-limited and safe.

---

## Phase 4 — Quotas, usage, and cost guardrails

**Goal**: since you're funding it, prevent runaway cost. Per-user quotas, usage visibility, hard caps.

### Checklist
- [ ] `usage_ledger` populated on every digest (tokens, cost, video count)
- [ ] Per-user daily/monthly quota (e.g., free tier: 20 videos/mo) — enforced before enqueuing
- [ ] `/usage` command: shows videos processed this period, tokens, est. cost
- [ ] `/upgrade` placeholder (for future paid tier — just info for now)
- [ ] Global cost dashboard: daily burn across all users (admin-only)
- [ ] Hard circuit-breaker: if daily spend > $X, pause non-essential jobs + alert
- [ ] Quota-exceeded messaging: graceful, not a crash

### UAT
- Hit the free quota → next `/fetch` returns a friendly "quota exceeded" instead of processing
- `/usage` shows accurate numbers matching the `usage_ledger`
- Circuit-breaker trips on a simulated over-spend and pauses jobs

### Exit gate
- Runaway cost is mechanically impossible past the cap. Users see their usage.

---

## Phase 5 — Observability, resilience, hardening

**Goal**: production-grade operations. You can see what's happening, failures self-heal, and the system degrades gracefully.

### Checklist
- [ ] Sentry integration (errors with stack + context)
- [ ] Structured logging everywhere (from Phase 0) — add Grafana/Loki or Railway log drain
- [ ] Health endpoints: `/health` (api), `/health/worker` (queue depth), `/health/poller` (last poll)
- [ ] Per-job retry with exponential backoff (3 attempts) + dead-letter on exhaustion
- [ ] Whisper key rotation pool (multiple keys, round-robin) to dodge per-key rate limits
- [ ] Proxy failover: if primary proxy 429s, rotate to a backup proxy
- [ ] Backfill on poller restart: any videos missed during downtime get picked up
- [ ] Load test: simulate 100 concurrent users, measure latency + cost
- [ ] Runbook: common failures + how to fix (in `docs/`)

- [ ] Security review: input validation on all user-supplied URLs; rate-limit `/fetch` per user
- [ ] Data retention policy: prune old transcripts/summaries after N days (cost)

### UAT
- Kill a worker mid-job → job retries and completes (no lost work)
- Trigger a fake error → appears in Sentry with full context
- Load test passes at 100 simulated users without errors or cost spike
- `/health` endpoints all green after a deploy

### Exit gate
- System is observable, self-healing, and survives a 100-user load test.

---

## Phase 6 — Scale validation (the 1,000-user gate)

**Goal**: prove the architecture holds at target scale before opening the floodgates.

### Checklist
- [ ] Provision for scale: enough worker replicas, Redis size, Postgres plan
- [ ] Load test at 1,000 simulated users (synthetic subscriptions + commands)
- [ ] Measure: end-to-end latency p50/p95, cost/video, cost/user, queue depth over time
- [ ] Tune: worker count, poll interval, batch sizes based on measurements
- [ ] Cost projection: extrapolate measured cost to steady-state; confirm affordable
- [ ] Documentation: final architecture diagram, ops runbook, cost dashboard

### UAT
- 1,000-user load test passes: p95 digest latency < 10 min from video publish, no errors
- Daily cost projection is within your budget
- All `/health` endpoints green under load

### Exit gate
- The system demonstrably handles 1,000 users. Ship it / open signups.

---

## Summary

| Phase | What it delivers | Risk it removes |
|---|---|---|
| 0 | Engineering baseline (tests, CI, migrations) | "prototype hygiene" |
| 1 | Multi-tenant schema | single-user limitation |
| 2 | Self-serve onboarding + webhooks | manual setup + scaling ceiling |
| 3 | Global dedup + job queue | cost explosion, fan-out failures |
| 4 | Quotas + cost guardrails | runaway spend |
| 5 | Observability + resilience | silent failures |
| 6 | Scale proof | "will it actually hold?" |

**Recommended order is strictly sequential** — each phase's exit gate is the next phase's prerequisite. Skipping ahead (e.g., scaling before multi-tenancy) multiplies rework.
