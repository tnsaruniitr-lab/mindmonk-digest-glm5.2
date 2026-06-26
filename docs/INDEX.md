# MindMonk — Documentation Index

> Start here. This index links every doc in `/docs` and tells you what each is for.

## Documents

### 📋 [SPEC.md](./SPEC.md) — *What we're building*
The product spec: vision, personas, the brief format (4 sections), all Telegram commands, the user-profile model, business model (self-funded), scope boundaries, and success metrics. **Read this first** to understand the product.

### 🏗️ [ARCHITECTURE.md](./ARCHITECTURE.md) — *How it's built (current + target)*
The engineering bible. Three parts:
1. **Current state** — what's live in production today (single-user prototype), what works, what falls short of multi-tenant
2. **Target architecture** — the multi-tenant topology (api + poller + worker pool + Postgres + Redis), the core video-scoped-vs-user-scoped optimization, data model, job queues, cost model, failure handling, security
3. **Engineering best practices** — testing, migrations, CI/CD, code quality, observability, config — the rules that govern all work

**Read this** to understand both where we are and where we're going.

### 🚀 [PHASES.md](./PHASES.md) — *How we get there, step by step*
The execution roadmap: 7 phases (0–6) from engineering foundations → multi-tenant schema → onboarding → global dedup + queue → quotas → observability → 1,000-user validation. Each phase has a checklist (`[ ]`), a UAT (human-verifiable proof), and an exit gate. Phases are strictly sequential.

**Read this** to see the build order and what "done" means for each step.

---

## Quick navigation

| If you want to... | Read |
|---|---|
| Understand the product | [SPEC.md](./SPEC.md) |
| Understand the current system | [ARCHITECTURE.md §1](./ARCHITECTURE.md#1-current-state--what-we-have-today) |
| Understand the target system | [ARCHITECTURE.md §2](./ARCHITECTURE.md#2-target-architecture--what-were-building) |
| Understand the cost optimization | [ARCHITECTURE.md §3](./ARCHITECTURE.md#3-the-core-optimization-video-scoped-vs-user-scoped) |
| See the engineering rules | [ARCHITECTURE.md §10](./ARCHITECTURE.md#10-engineering-best-practices) |
| See the build plan | [PHASES.md](./PHASES.md) |
| Start building Phase 0 | [PHASES.md §Phase 0](./PHASES.md#phase-0--engineering-foundations-no-behavior-change) |

---

## Document conventions

- **Living documents**: these evolve with the system. When code or architecture changes, update the relevant doc **in the same commit** — docs are code.
- **Checklists** (`[ ]`) in PHASES.md track discrete tasks; check them off as work completes.
- **UATs** (User Acceptance Tests) are the human-verifiable proof a phase works before moving on.
- **ADRs** (Architecture Decision Records) will live in `docs/adr/` for significant choices once we start building.

## Repo structure (for reference)
```
podcast-digest/
├── docs/                 ← you are here
│   ├── INDEX.md          ← this file
│   ├── SPEC.md
│   ├── ARCHITECTURE.md
│   └── PHASES.md
├── src/                  ← application code (14 modules)
├── config/               ← typed settings
├── landing/              ← marketing landing page
├── tests/                ← (coming in Phase 0)
├── main.py               ← entrypoint
├── Dockerfile
├── railway.toml
└── requirements.txt
```
