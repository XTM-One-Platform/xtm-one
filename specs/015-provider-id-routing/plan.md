# Implementation Plan: Route by provider config, not by model name

**Branch**: `fix/3337-provider-id-routing` | **Date**: 2026-09-07 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/015-provider-id-routing/spec.md`

## Summary

Seven administered model choices persist a model id; four of them also persist the
provider config that offered it, three do not, and none of the pickers let the
admin say which config they meant. Give the three missing surfaces a reference
column, make every picker carry `<provider_id>::<model_id>` so the saved reference
is the admin's actual selection, prefer that reference in resolution, and leave the
recency path as the fallback for references that are null. Then warn when two
configs advertise one model id.

Design decisions and their grounding: [research.md](./research.md).

## Technical Context

**Language/Version**: Python 3.14 (backend), TypeScript strict / React 19 (frontend)

**Primary Dependencies**: FastAPI + Pydantic, SQLAlchemy 2.0 async, Alembic, Radix UI Select, TanStack Query

**Storage**: PostgreSQL 17 — three additive nullable UUID columns

**Testing**: pytest (`asyncio_mode=auto`), the `migrations` CI job, `yarn check`

**Target Platform**: Linux server, unified Docker image

**Project Type**: Web application (`backend/` + `frontend/`)

**Performance Goals**: no new query on any chat turn — the reference is a column on
a row already loaded, and the overlap computation runs only on settings reads

**Constraints**: no behaviour change for deployments with no duplicated model id
(SC-003); no backfill (research.md D6); migration must be lock-light (Principle V)

**Scale/Scope**: 3 columns, 1 migration, 12 backend source files (the routing
tuple change fans out to three `routing_model()` callers), 11 frontend source
files carrying 7 pickers plus 4 locale files, 1 new API field

## Constitution Check

*GATE: passed before Phase 0; re-checked after Phase 1 — still passing.*

| Principle | Verdict |
|---|---|
| I. Assignment-Driven Automation | N/A — no new automation or execution path. |
| II. Fail-Closed by Default | Evaluated, see below. |
| III. Security by Default | No new secret. A provider reference is not a credential and is not logged as one; the dangling-reference log prints a UUID only. |
| IV. Concurrency-Safe | N/A — no scheduled or startup work added. |
| V. Alembic-Only, Lock-Safe | One migration, additive nullable columns, `IF NOT EXISTS`, working `downgrade()`, single head off `k5p3358studio` (research.md D7). |
| VI. CI-Enforced Quality Gates | `ruff check app/`, `yarn check`, pytest, and the migrations job — commands in [quickstart.md](./quickstart.md). |
| VII. Rationale-Documented Complexity | Two "why"s must land in code: why the reference columns carry no FK (D2), and why picker values are composite (D4). |
| VIII. Shared Utilities Over Duplication | One composite encode/decode helper for all seven pickers; one name formatter, `providerConfigName` (`lib/utils.ts`, research.md D10), for the providers list and every picker; `_resolve_model_owner_config` reused unchanged. |
| IX. TanStack Query | Unchanged — the pickers already read through it. |
| X. Design System Compliance | Overlap warning uses tokens and the existing `Tip`; no native `title`, no hardcoded colour. |
| XI. Minimal, Reviewable Writing | These artifacts state each decision once and cite by identifier. |

**Principle II, in detail**: FR-007 makes a dangling reference fall back to the
stored provider name and log, rather than refuse the call. Principle II enumerates
its scope — quota and rate-limit enforcement, `Settings` validators, migration
state, credentials, webhook signatures, tool allow-lists — and LLM route selection
is not in it. Refusing would take chat down platform-wide when an admin deletes one
provider config, and the established behaviour for the agent-level override
(`router.py:1310-1336`) is already log-and-fall-back. This feature matches it
rather than introducing a second policy for the same condition. No Complexity
Tracking entry: no gate is violated.

## Project Structure

### Documentation (this feature)

```text
specs/015-provider-id-routing/
├── plan.md              # This file
├── research.md          # Phase 0: D1-D11
├── data-model.md        # Phase 1: schema, invariants, wire shapes
├── quickstart.md        # Phase 1: validation guide
├── contracts/README.md  # Phase 1: the three changed contracts
├── checklists/          # requirements.md (from /speckit-specify)
└── tasks.md             # /speckit-tasks output — not created here
```

### Source Code (repository root)

```text
backend/
├── alembic/versions/            # + 1 migration (3 columns, off k5p3358studio)
├── app/models/
│   ├── general_assistant_config.py   # + llm_provider_id            (FR-002)
│   └── platform_settings.py          # + routing/fallback_provider_id (FR-003)
├── app/api/v1/
│   ├── schemas/settings.py           # + model_overlaps, + 3 id fields
│   └── settings.py                   # persist the 3 ids; overlap warning (FR-010)
├── app/llm/router.py                 # resolve_configured_fallback prefers the id (FR-005)
├── app/services/
│   ├── general_chat.py               # pass provider_id_override      (FR-005)
│   ├── flow_llm.py                   # routing_model() -> (model, id)
│   ├── flow_condition_llm.py         # follow the tuple signature
│   ├── flow_tool_selector.py         # follow the tuple signature
│   ├── workset_processing.py         # follow the tuple signature
│   └── routing_evaluator.py          # pass the routing provider id
└── tests/                            # unit + migration coverage

frontend/
├── src/components/ui/model-select-items.tsx  # one branch per config (D11)
├── src/components/ui/model-select-value.tsx  # the closed trigger's value (D11)
├── src/lib/utils.ts                  # composite encode/decode helper (D4)
├── src/types/index.ts                # + the 3 id fields
├── src/hooks/api/useSettingsApi.ts   # + the 3 id fields on the payloads
├── src/pages/Settings.tsx            # 5 pickers: default, routing, fallback, embeddings, images
├── src/pages/settings/GeneralAssistantSection.tsx  # GA chat model picker
├── src/pages/settings/ProviderCard.tsx  # the overlap warning            (FR-010)
└── src/pages/agent-detail/
    ├── config/GeneralSection.tsx     # the agent model picker itself
    ├── ConfigTab.tsx                 # drop the find()-derived id memo   (FR-004)
    └── OverviewTab.tsx               # read-back resolves by model name only
```

**Structure Decision**: the repository's existing `backend/` + `frontend/` split.
No new module — every file above already exists except the migration.

## Complexity Tracking

No constitutional gate is violated; nothing to justify.
