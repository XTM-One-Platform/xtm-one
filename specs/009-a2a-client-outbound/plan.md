# Implementation Plan: Minimal A2A Client (Outbound)

**Branch**: `feat/implement-A2A-client` | **Date**: 2026-08-27 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/009-a2a-client-outbound/spec.md`

**Supersedes** the 2026-08-26 plan, which predates the direction clarification (spec Clarifications, Session 2026-08-27 — bidirectional integrations). That plan added two outbound-only provider keys and left the inbound surface untouched; this one gives the existing mechanisms a direction and therefore modifies shipped inbound code.

## Summary

An XTM One agent discovers a partner's A2A agent and hands it one unit of work. A partner is an **integration carrying exactly one direction**: the two shipped inbound mechanisms become direction-switchable, and a third catalog entry is added for Google Agent Platform outbound, whose credential flow neither existing mechanism reaches. Binding an integration to an agent applies only that integration's direction — outbound confers a delegation tool, inbound confers exposure, never both.

Most of the outbound half is reuse: `a2a-sdk==1.1.2` already ships `A2ACardResolver`, `client_factory`, auth interceptors and `utils/signing.create_signature_verifier`, whose `NoSignatureError` / `InvalidSignaturesError` split maps onto FR-016 / FR-015. `app/a2a/protocol.py` already shapes v1.0 ↔ v0.3 in both directions, so FR-051's two-version support needs a caller, not a shaper. `app/integrations/` supplies the tool seam (`PROVIDER_TOOLS`), the health verdict (`register_connectivity`), the connect hook (`register_lifecycle`) and retry/token helpers (`http.py`).

What is new, and what makes this larger than the superseded plan: a **direction concept on shipped inbound integrations**, and a change to the exposure hook that currently treats any A2A binding as consent to expose. Without that change, binding an integration for outbound purposes would silently expose the agent.

## Technical Context

**Language/Version**: Python 3.14 (backend), TypeScript / React (frontend)

**Primary Dependencies**: FastAPI, SQLAlchemy 2 (async), Alembic, `a2a-sdk==1.1.2`, `httpx==0.28.1`, `authlib`, `pyjwt`, `cryptography`; TanStack Query

**Storage**: PostgreSQL. Three new tables (`a2a_integration_settings`, `a2a_client_targets`, `a2a_delegations`) and one Alembic revision chained on main's current head — `a2a2intmodel1` when this plan was written, re-pointed on each rebase — including a backfill that stamps every existing A2A integration as inbound. Credentials stay encrypted on the `integrations` row. *(Updated 2026-08-31: this plan said two tables. The per-card state moved to its own table under FR-059 — see [data-model.md](./data-model.md).)*

**Testing**: pytest; `integration` marker for real Postgres/Redis. The partner side is stubbed, and the OAuth target is additionally exercisable against this platform's own inbound surface.

**Target Platform**: Linux server, multi-replica

**Project Type**: Web application — `backend/` + `frontend/`

**Performance Goals**: None introduced. The bounds are correctness bounds: 5-minute per-delegation ceiling (FR-024), 15-minute verification staleness threshold (FR-050), hourly sweep (FR-019), 1 MiB response body (FR-027).

**Constraints**: Deployment switch off by default (FR-007). Exactly one direction per integration, refused while bound (FR-055, FR-057). Fail-closed on any verification, credential, destination or authority decision (FR-038). No run history or platform credential outbound (FR-032). Origin-pinned, no off-origin redirects (FR-034). Partner output through `prompt_guard` (FR-047).

**Scale/Scope**: Registrations in the tens. 58 functional requirements, 19 success criteria, three outbound targets, two protocol versions, one shipped surface modified.

## Constitution Check

*Evaluated against constitution v2.1.0 before Phase 0 and re-evaluated after Phase 1. Result: **PASS**, with four items justified in Complexity Tracking.*

| Principle | Verdict | Basis |
|---|---|---|
| **I. Assignment-Driven Automation** | PASS | Delegation is a tool call in the agentic loop. The verification sweep is a background job, so the pass rests on the principle's own scope — "the single execution model for all **agent work**" — and re-verifying a card is platform maintenance with no agent in it, like `a2a.tasks.reap_stale_tasks`, which it follows. |
| **II. Fail-Closed by Default** | PASS | Three places carry it. An integration with no settings row is **not exposed and contributes no tool** — a missing row can never fail open. A stale verdict that cannot be re-verified refuses the delegation (FR-050). Unverifiable card, unevaluable credential, destination or authority all refuse (FR-013, FR-038). The circuit breaker is deliberately in Postgres, not Redis, so there is no fail-open-on-Redis-loss case to reason about. |
| **III. Security by Default** | PASS | Credentials Fernet-encrypted on the `integrations` row, never returned, logged or echoed into a health message (FR-004). Cleared on a direction change (FR-058). JWT on management endpoints. Partner output — the `input-required` question included — through the existing `prompt_guard` (FR-047), no prompt inflation. |
| **IV. Concurrency-Safe, Multi-Replica** | PASS | The sweep claims rows with `FOR UPDATE SKIP LOCKED`, mirroring `reap_stale_tasks`; a skipped cycle is safe because FR-050's delegation-time threshold is the binding control. Delegation-time re-verification writes a verdict, not a transition, so it is idempotent. |
| **V. Alembic-Only, Lock-Safe** | PASS | One revision on `a2a2intmodel1` (single head): two `CREATE TABLE IF NOT EXISTS`, plus a bounded backfill inserting one settings row per existing A2A integration — tens of rows, no rewrite, no hot-table lock. Working `downgrade()`. `a2a1srv0001` and `a2a2intmodel1` are merged history and are not edited. |
| **VI. CI-Enforced Quality Gates** | PASS | `ruff` (+bandit), `tsc --noEmit`, `eslint`, backend tests, migration idempotency and downgrade round-trip. The stub makes every FR unit-testable. |
| **VII. Rationale-Documented Complexity** | PASS | Required at the fail-closed staleness path, the transient/durable classifier two surfaces read, the widened `a2a_sdk` boundary, the destination guard, and — new here — the direction check in the exposure hook, where the "why" is a security regression it prevents. |
| **VIII. Shared Utilities Over Duplication** | PASS | No new HTTP, retry, credential, audit or metric machinery. Direction lives in `app/a2a/providers.py`, which already owns "which integrations are A2A", rather than in a second module answering the same question. |
| **IX. TanStack Query** | PASS | One new hook; intervals from `lib/constants.ts`; no `useState` for server data. |
| **X. Design System Compliance** | PASS | Catalog-driven forms, CSS tokens, `Switch`, `Tip`, strings in `en`/`fr`/`ja`. |
| **XI. Minimal, Reviewable Writing** | PASS | This plan supersedes rather than appends to its predecessor; the superseded design is not restated beside the current one. Requirements are cited by identifier, not paraphrased. |

## Project Structure

### Documentation (this feature)

```text
specs/009-a2a-client-outbound/
├── plan.md · research.md · data-model.md · quickstart.md · spec.md · tasks.md
├── contracts/{delegation-tool,partner-invocation,internal-management}.md
└── checklists/requirements.md
```

### Source Code (repository root)

```text
backend/
├── app/
│   ├── a2a/
│   │   ├── providers.py           # EXTEND: direction lookup — the one place "is this inbound?" is answered
│   │   ├── hooks.py               # CHANGE (shipped): expose only for INBOUND bindings
│   │   ├── protocol.py            # EXTEND: expose the shaper to the client side
│   │   ├── metrics.py             # EXTEND: outbound counters
│   │   └── client/                # NEW — the outbound half
│   │       ├── sdk.py             # the only new module importing a2a_sdk
│   │       ├── verification.py    # fetch, verify, negotiate version, fingerprint
│   │       ├── delegation.py      # submit, follow, continue, cancel
│   │       ├── tool.py            # the shared delegation tool builder
│   │       ├── destinations.py    # origin pinning + destination guard
│   │       ├── failures.py        # the single transient/durable classifier
│   │       ├── breaker.py         # consecutive-failure counting + cooldown
│   │       └── sweep.py           # periodic re-verification
│   ├── integrations/providers/
│   │   ├── oauth_a2a.py           # CHANGE (shipped): + outbound mode
│   │   ├── entra_a2a.py           # CHANGE (shipped): + outbound mode (Foundry)
│   │   └── google_a2a.py          # NEW: Google Agent Platform, both directions
│   ├── api/v1/
│   │   ├── integration_catalog.py # EXTEND: per-direction field sets
│   │   ├── integrations.py        # EXTEND: refuse a direction change while bound
│   │   └── a2a_client.py          # NEW: verification state, verify-now, accept-card
│   ├── services/integration_tools.py  # EXTEND: register the three keys in PROVIDER_TOOLS
│   ├── models/a2a.py              # EXTEND: A2AIntegrationSettings, A2AClientTarget, A2ADelegation
│   ├── core/config.py             # EXTEND: switch, sweep, staleness, ceiling, size, breaker
│   └── main.py                    # EXTEND: schedule the sweep beside the reaper
├── alembic/versions/              # NEW revision on a2a2intmodel1, with the inbound backfill
└── tests/                         # NEW test_a2a_client_*.py, plus inbound regression tests

frontend/src/
├── hooks/api/useA2AClientApi.ts   # NEW
├── pages/integration-detail/      # EXTEND: direction control + verification panel
└── i18n/lang/{en,fr,ja}.json      # EXTEND
```

**Structure Decision**: Outbound logic lives in a new `app/a2a/client/` package rather than beside the inbound modules, which all answer "who is calling us". Three modules stay shared because both directions must agree on them: `protocol.py` (one shaper, per its own docstring), `audit.py`, and `metrics.py`. **Direction itself lives in `providers.py`**, not in `client/`: it governs the inbound hook as much as the outbound tool, and a second module answering "is this integration A2A, and which way" is the two-sources-of-truth bug the spec's FR-053 exists to prevent, in another guise.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| **Modifying shipped inbound code** (`hooks.py`, `providers.py`, both inbound providers), which the superseded plan's scope excluded | The exposure hook decides exposure from provider membership alone (`binds_a2a` → `bound_integration_ids`). Once a mechanism can be outbound, that test is wrong: binding an integration to get a delegation tool would silently expose the agent to a partner nobody approved | Keeping outbound on separate provider keys avoids touching inbound entirely — the superseded plan's approach — but yields four catalog cards for two partners, which the spec rejected on operator-experience grounds. Leaving the hook alone is not an option in any variant that lets a shipped key go outbound: it is a fail-open security regression, not a cosmetic gap |
| **A second module may import `a2a_sdk`** (`client/sdk.py`), widening a boundary `protocol.py` states as "the ONLY module" | The SDK's client half — card resolution, JWS verification, the JSON-RPC client — is precisely what must not be reimplemented, and is a different concern from type/version shaping | Routing client calls through `protocol.py` makes a version shaper own an HTTP client and both directions. Hand-rolling JWS verification against a JCS-canonicalised document is the wrong wheel to reinvent for a fail-closed security control. The invariant that matters — `a2a_sdk` types never leaving `app/a2a/` — is preserved, and `protocol.py`'s docstring is amended rather than left contradicted |
| **One side table with mode-dependent NULLs** (`a2a_integration_settings`: direction for every A2A integration, outbound columns NULL for inbound rows) | The exposure hook needs direction for a set of integration ids on every relevant flush, so it must be queryable — which rules out `extra_config_enc` (encrypted) — and one lookup is better than two | A column on `integrations` puts an A2A-specific concept on the table all ~130 providers share. Two side tables (direction, plus outbound state) means three new A2A tables and two lookups where the hook needs one. The cost accepted is ~14 columns that are NULL on an inbound row, documented in data-model.md |
| **A destination guard** (`client/destinations.py`), which the spec originally said this feature would not add | FR-035 now requires it. The repository's only egress allow-list is sandbox-scoped and nothing constrains backend `httpx` calls, so an operator-supplied card URL fetched with stored credentials is a live request-forgery surface | Consulting a deployment-wide egress policy — the original wording — was found to name an authority that does not exist (spec Clarifications, 2026-08-27). Building one for all integrations is out of proportion and changes ~130 providers' behaviour |
