# Implementation Plan: AI-disabled platform mode

**Branch**: `feat/3095-ai-disabled-mode` (spec dir `008-ai-disabled-mode`) | **Date**: 2026-08-26 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/008-ai-disabled-mode/spec.md`

## Summary

Run the platform with AI off by construction: nothing seeded, no AI scheduler dispatch, no provider traffic — while the platform boots clean, stays administrable, keeps deterministic automation running, and says on every screen that AI is deliberately disabled rather than broken. An administrator can flip it either way at runtime without a restart.

Six findings shape the approach.

1. **The gating seam already exists, and it is two dependencies deep.** `require_agentic_ai` and `require_chat_enabled` (`api/deps.py:399`, `:420`) already refuse writes and chat when a licence does not permit them, and `platform.py:320-345` documents a map of feature keys to those gates that is maintained deliberately. This feature is the deterministic end of the same axis. Adding a third parallel mechanism would double the number of places a route can be gated, and the existing map is the artefact that would silently go stale.

2. **What those gates do *not* have is a machine-readable reason.** `require_agentic_ai` raises a 403 whose body is prose about contacting sales, so FR-009 needs a structured code added to it. Checked, and it is lower risk than it first appears: the frontend does not parse these bodies at all — it reads `agentic_ai_enabled` from the platform config query (`hooks/usePlatformConfig.tsx:198,247,528`). The established pattern is already *state comes from config, errors are just messages*, which is also what FR-011 needs for the sign-in screen, where there is no request to refuse. So the code is additive and the **config flag is the primary signal**; see [research.md](./research.md) §2.

3. **Seeding is one function, and it is not reusable yet.** All of it lives inside `_bootstrap_platform_data()` (`main.py:1044-1200`) — templates, agents, assignments, skills, integrations, knowledge bases, the example flow, template containers, intent bindings, model prices. FR-012 requires the same content to be creatable when AI is switched on at runtime, so this must be extracted into one idempotent operation called from both boot and the toggle. The extraction is the largest structural change in the feature and it touches boot, which is the highest-blast-radius code in the repo.

4. **Every AI scheduler already converges on one tick.** `_process_scheduled_assignments`, `process_scheduled_flows`, `process_scheduled_kb_syncs`, `process_scheduled_nova_syncs` and `_process_scheduled_evaluations` are all dispatched from `services/autonomous.py:1197-1290,1651`. One place reads the resolved state per tick; there is no need to touch each scheduler.

5. **The two obvious frontend hooks are both the wrong ones.** `NavLeaf.visible` *removes* an entry when false (`nav-model.ts:30-38`), and FR-017 requires AI entries to stay listed and reachable — so it needs a new marker, not that predicate. Likewise `NodeKind` (`flow_catalog.py:295`) is a frozen dataclass whose fields all default to the permissive value; FR-030 requires the *opposite* default for `requires_ai`, so that a node type nobody classified is held back rather than run.

6. **`ai_disabled` was silently read as "Copilot".** `is_xtm_one` is `platform_mode == "xtm_one"`, and nine call sites used `not is_xtm_one` to mean Copilot. A fourth value flipped all of them: the OpenAI-compatible `/v1` proxy was **mounted**, the code interpreter and custom tools defaulted **on**, self-registration turned **on**, `LicenseGateMiddleware` demanded a Copilot licence, and `is_agentic_ai_enabled` returned `True` unconditionally — which would have made the licence half of both new gates a no-op. Found by `/code-review`, not by the plan or by any test. Fixed with an explicit `is_copilot_like` property and a guard test. **The general lesson: adding a value to an enum requires auditing everything that reads it as a closed set, and `not X` is where that reading hides.**

## Technical Context

**Language/Version**: Python 3.11+ (backend), TypeScript 5.x / React 18 (frontend)

**Primary Dependencies**: FastAPI, SQLAlchemy 2.0 (async), Alembic, Pydantic v2, Redis, saq (worker), React Query, shadcn/ui

**Storage**: PostgreSQL (primary), Redis (scheduler coordination, pub/sub)

**Testing**: pytest (unit by default; `integration` marker for real Postgres/Redis/MinIO), Vitest + `tsc --noEmit` + eslint + ruff, dedicated Alembic migrations CI job

**Target Platform**: Linux server, multi-replica deployment

**Performance Goals**: **Decided (2026-08-26): cached, five seconds, process-local.** The resolved state is consulted on every gated request and every scheduler tick, so a per-request read is paid forever to make a setting changed a handful of times feel instant. It is primed at boot and invalidated by the toggle, so the steady-state request path does no database work at all — which `require_chat_enabled`'s test also requires, its fake session raising on any access. Staleness is bounded per replica by the TTL (FR-003a); no cross-replica invalidation. Failures are not cached, so a blip cannot hold AI off after recovery.

**Constraints**:

- **The resolved state has exactly three inputs and two outcomes.** Stored `true`, stored `false`, or stored `NULL` meaning "follow `PLATFORM_MODE`". `NULL` is load-bearing: without it, flipping the deployment default on an untouched install is shadowed by a row written at first boot. `platform_name` already uses this convention (`platform_settings_helpers.py:66`).
- **Turning AI off must not corrupt in-flight work.** FR-013 requires running work to finish under its existing timeout. A hard cancel would leave half-written runs, which is the failure mode the flow gating is also designed to avoid.
- **The 403 body must stay backward compatible.** The prose remains; the code is added beside it. No in-repo consumer parses it, but external ones may display it.
- **Boot ordering.** Provisioning currently runs inside the lifespan before the worker starts. Extracting it must not change that ordering for the boot path.
- **The seeders gate on `platform_mode`, so provisioning must pass a content mode explicitly.** Every `seed_builtin_*` checks `platform_mode in ("xtm_one", "dev")`; handing them `ai_disabled` makes all of them no-ops, so enabling AI at runtime would provision nothing. `content_mode()` resolves `ai_disabled` to `xtm_one` and leaves other modes alone, keeping the seeders ignorant of this feature.
- **No licence is required (FR-031).** `LicenseGateMiddleware` short-circuits on `not is_copilot_like` rather than `is_xtm_one`; the earlier reading made a Copilot licence mandatory in the one mode documented as needing none.

**Scale/Scope**: 4 `PLATFORM_MODE` values after this change; ~50 flow node types to classify; 6 top-level navigation groups; the seeding block is ~160 lines across 9 seed calls; **83 route call sites** already attached to the two gates being extended, which is the reach gained without touching them.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Constitution **v2.0.0** (ratified 2026-07-23, last amended 2026-08-24) — all ten principles gated.

- [x] **Assignment-Driven Automation** (Principle I): No new background job, script or ad-hoc step. This feature *suppresses* dispatch inside the existing scheduler tick and extracts existing seeding into a callable operation. The agentic loop remains the single execution model for the work that still runs.
- [x] **Fail-Closed by Default** (Principle II): The whole feature is an application of this principle, and it decides two defaults. A flow node type whose AI dependence is undeclared is treated as requiring AI (FR-030), so the `requires_ai` field defaults to `True` and each non-AI node opts out explicitly. If the resolved state cannot be read, the platform MUST treat AI as **disabled** — refusing is recoverable, whereas guessing "enabled" runs a model on a deployment that may forbid it by policy.
- [x] **Security by Default** (Principle III): No new secret. FR-024 requires administrator authorisation on the toggle, reusing the existing `can_manage` check rather than a new permission. FR-010 requires the refusal to cover API tokens and integration-driven calls, so the gate belongs in the shared dependency rather than in route bodies. FR-029 turns the MCP endpoint off entirely — a machine-facing surface is the one place a partial remainder would not be visible in the interface.
- [x] **Concurrency-Safe, Multi-Replica by Design** (Principle IV): The setting is a single column on an existing singleton whose uniqueness is already enforced by a constant-expression unique index (`models/platform_settings.py:29`), so concurrent writes converge (FR-026). Every replica resolves the same state from that row; scheduler loops re-read per tick, so a flip propagates without coordination. Rolling deploys are safe because the deployment default is code and the override is state.
- [x] **Alembic-Only, Lock-Safe Migrations** (Principle V): One migration adding one nullable boolean to `platform_settings`. Nullable with no default and no backfill, so it takes no long lock and the `NULL`-means-follow-the-mode semantics come free. Downgrade drops the column.
- [x] **CI-Enforced Quality Gates** (Principle VI): Unit tests for state resolution and node classification; `integration`-marked tests for the boot-with-nothing-seeded assertion, the runtime toggle round trip and the flow skip, because all three turn on real Postgres and the real scheduler rather than on any one function. Frontend must pass `tsc --noEmit` and eslint; backend must pass ruff. **Counting** is required by SC-001, so the tests assert seeded row counts and dispatched job counts, not the absence of an exception.
- [x] **Rationale-Documented Complexity** (Principle VII): Four decisions need "why" comments where they live: the `NULL` tri-state, the inverted `requires_ai` default, letting in-flight work finish, and the extraction of provisioning out of the lifespan.
- [x] **Shared Utilities Over Duplication** (Principle VIII): The refusal goes through the existing `api/deps.py` gates rather than a parallel one, and the feature-key map in `platform.py:320-345` is updated alongside. Provisioning becomes one shared function used by two callers — the opposite of duplication, and the reason finding 3 is structural rather than additive.
- [x] **TanStack Query for Server State** (Principle IX): The resolved state reaches the frontend through the existing platform-config query; the toggle is a mutation that invalidates it. No new store, no context holding server state.
- [x] **Design System Compliance** (Principle X): The lock affordance, the header badge and the disabled panel are token-based and reuse shared primitives. The landing screen generalises `CtemTrialSplash` (`pages/xtm-one/CtemTrialSplash.tsx`), whose `VALUE_PROPS` copy already exists, rather than introducing a second splash pattern. All four locales.

### Post-Design Re-Check (after Phase 1)

All ten gates still pass. Phase 1 sharpened three things and softened one.

- **Principle II resolved, with a carve-out.** An unresolvable state gates AI as disabled, but must not prevent the process from starting — the database is legitimately unavailable for part of boot, and refusing to boot would turn a transient condition into an outage. The resolver therefore distinguishes "gate a request" from "start the process" ([data-model.md](./data-model.md) §2). Without that distinction the fail-closed reading is a boot loop.
- **Principle VI is heavier than the first pass implied.** Three of the seven quickstart scenarios cannot be asserted without a real scheduler and a real database — the zero-seeding count, the toggle round trip, and the flow skip — so they are `integration`-marked rather than unit tests. SC-001 explicitly requires *counting* seeded rows and dispatched jobs, so "no exception was raised" is not evidence. SC-011 is a judgement call no assertion covers and is marked as such in the quickstart rather than pretended away.
- **Principle VIII is stronger than claimed.** The refusal reaches **83 route call sites** through two existing dependencies. That is the concrete measure of not duplicating the gate, and it is also why the feature-key map in `platform.py:320-345` must be updated in the same change rather than after.
- **Principle IX simplified.** No new query is needed: the state rides on the platform config the frontend already fetches, and the toggle is a mutation invalidating it. Research §2 also found the frontend never parses the 403 prose, so the structured code is additive and the config flag is the primary path — which removes the contract risk the first draft called the riskiest part of the backend work.

## Project Structure

### Documentation (this feature)

```text
specs/008-ai-disabled-mode/
├── plan.md              # This file
├── spec.md
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── ai-state-api.md
│   └── refusal-payload.md
└── checklists/requirements.md
```

### Source Code (repository root)

```text
backend/
├── app/
│   ├── core/
│   │   └── config.py                   # ai_disabled as a fourth platform_mode value
│   ├── models/
│   │   └── platform_settings.py        # + nullable ai_disabled column
│   ├── services/
│   │   ├── ai_state.py                 # NEW: resolve the one boolean; the only reader of the mode
│   │   ├── provisioning.py             # NEW: idempotent provision_builtin_content(), extracted from main.py
│   │   ├── autonomous.py               # suppress AI dispatch in the existing tick
│   │   ├── flow_catalog.py             # + requires_ai on NodeKind, defaulting to True
│   │   └── flow_triggers.py            # skip-with-reason for AI-bearing flows
│   ├── api/
│   │   ├── deps.py                     # existing gates gain the resolved state + a structured refusal
│   │   └── v1/
│   │       ├── platform.py             # /config carries the state; feature-key map updated
│   │       ├── settings.py             # the admin toggle, audit-logged
│       └── mcp_endpoint.py         # refused entirely while AI is disabled
│   └── main.py                         # calls provisioning instead of inlining it
└── alembic/versions/                   # ONE migration: one nullable boolean

frontend/
└── src/
    ├── components/layout/nav-model.ts  # + requiresAi marker (NOT the `visible` predicate)
    ├── components/layout/              # header badge
    ├── pages/AiDisabledLanding.tsx     # NEW: generalised from CtemTrialSplash
    ├── hooks/api/usePlatformConfig     # existing query carries the state
    └── i18n/lang/{en,fr,es,ja}.json    # all four locales
```

**Structure Decision**: Existing web-application split. Two new backend service modules and no new API surface beyond the toggle: the resolved state rides on the platform config that every client already fetches, and the refusal rides on the 403 that every gated route already returns. `ai_state.py` exists so that FR-004's "one resolved boolean, never the mode directly" is enforceable by grep rather than by discipline.

## Complexity Tracking

| Cost | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| Extracting provisioning out of the FastAPI lifespan | FR-012: turning AI on at runtime must create the same content boot would. Today that logic exists only as ~160 inline lines in `_bootstrap_platform_data()`. | Duplicating the seed calls in the toggle handler: two copies of a nine-step sequence that must stay identical, which is the drift Principle VIII exists to prevent — and the copy nobody exercises at boot is the one that rots. |
| A nullable tri-state column rather than a boolean | `NULL` is the only way "no administrator has chosen" differs from "an administrator chose off". Without it, the first boot writes a value and flipping `PLATFORM_MODE` afterwards is silently shadowed. | A non-null boolean defaulted from the mode at first boot: makes the deployment default unchangeable in practice, which contradicts FR-003 and would be discovered only by an operator whose env change appeared to do nothing. |
| `requires_ai` defaulting to `True` on every node kind | FR-030. A node type added later with no classification must be held back, not run. | Defaulting to `False` like every other field on `NodeKind`: consistent-looking and wrong, because the failure is invisible — a new AI-bearing node would run on an AI-disabled platform and nothing in the diff would say so. |
| A structured code added to existing 403 bodies | FR-009 needs one machine-readable reason so the frontend renders the state instead of pattern-matching prose. | A new dedicated endpoint the client polls: adds a round trip to every refusal and leaves the 403s ambiguous anyway, so both would have to be maintained. |
| Letting in-flight work finish after AI is turned off | FR-013. A hard cancel mid-run leaves half-written runs and traces, and the delay is already bounded by each run's existing timeout. | Cancelling immediately: sounds safer, produces exactly the partial state the flow gating is separately designed to avoid. |
| Keeping AI navigation and data readable | FR-008, FR-017, and settled decision 3 in the issue. Visibility is the point — an install that hides its missing half is the problem being solved. | Hiding AI areas: less work, and it reproduces the "looks broken / looks incomplete" outcome the feature exists to fix. |

**Accepted operational consequence**: a platform started with AI disabled may later have AI enabled, so provider-dependent infrastructure (worker, embedding server, sandbox) must remain *addable* without a rebuild even if it is not started initially. "Optional" therefore cannot mean "absent from the deployment manifest".

**Sign-off status**: three product decisions were arbitrated in the spec's clarification pass on 2026-08-26 — the MCP endpoint is off entirely, an unclassified flow node counts as requiring AI, and the mode requires no licence while still being reported in telemetry. All three carry engineering sign-off from the branch owner and have **not** been to the product owner. FR-031 in particular is a commercial decision; confirm before implementation if licensing is contested.
