# Implementation Plan: Gateway Model Types and Decision Models

**Branch**: `feat/5990-gateway-model-types` | **Date**: 2026-10-19 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/031-gateway-model-types/spec.md`

## Summary

`FiligranGatewayProvider._models_with_meta()` returns a `GatewayModel(id, owner, types)` instead of a `(id, owner, type)` tuple, `types` resolved as the list contract states (R1). Every picker asks `capability in types` (R2). The gateway and a custom OpenAI-compatible provider list decision models by the same membership, with no probe (R3). The decision itself stays spec 028's: one OpenAI Decisions route, no wire per model (R4). 028 merged first, so the branch carries the merge fix (R5). One function resolves types for both providers (R6). It ships as one PR.

## Technical Context

**Language/Version**: Python 3.14 (backend); no frontend change

**Primary Dependencies**: `openai` SDK (`AsyncOpenAI.models.list`), httpx

**Storage**: none, no migration

**Testing**: pytest (`asyncio_mode=auto`), the gateway's listing stubbed with `httpx.MockTransport`

**Target Platform**: XTM One backend replicas

**Project Type**: web service (backend change only)

**Performance Goals**: no request added: each picker still reads `/v1/models` once, and the decision picker sends nothing else (SC-005)

**Constraints**: a gateway without `types` lists what it listed (SC-002); no model id decides a type (XII)

**Scale/Scope**: two provider classes, one shared function, one record type, tests, docs

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design: unchanged.*

| Principle | Status |
|---|---|
| I Assignment-driven | Pass: no automation; settings reads. |
| II Fail-closed | Pass: an unknown type matches no picker (FR-004); a model is never offered for a capability its listing does not state. |
| III Security | Pass: no credential handling changes; the listing error path keeps `redact_token`. |
| IV Multi-replica | Pass: nothing stored or shared. |
| V Migrations | Pass: none. |
| VI CI gates | Pass: unit tests, `ruff check app/`, the whole suite. |
| VII Rationale comments | Pass: `declared_model_types` says why `types` outranks `type` (R6). |
| VIII Shared utilities | Pass: one resolution for both providers (R6). |
| IX TanStack Query | Pass: no frontend. |
| X Design system | Pass: no frontend. |
| XI Minimal writing | Pass: artifacts cite R and FR ids. |
| XII No workarounds | Pass: the adapters read what the gateway declares; the classifier never yields `decision`. |

## Project Structure

### Documentation (this feature)

```text
specs/031-gateway-model-types/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── gateway-model-list.md
├── checklists/requirements.md
└── tasks.md             # /speckit-tasks
```

### Source Code (repository root)

```text
backend/
├── app/llm/openai_compat.py          # declared_model_types(): types, else type (R6)
├── app/llm/gateway_provider.py       # GatewayModel, _types_of, pickers by membership, list_decision_models
├── app/llm/custom_openai_provider.py # list_decision_models by the same rule
├── app/llm/AGENTS.md                 # the multi-type listing, the decision listing
└── tests/
    ├── test_gateway_provider.py          # types, fallbacks, unknown types
    ├── test_gateway_decision_models.py   # chat-and-decision models, both provider kinds
    └── test_settings_llm_providers.py    # the listing fake returns GatewayModel
docs/docs/administration/settings.md       # Where each model is chosen, Decision model (FR-010)
specs/027-decision-model-page-actions/{research.md,contracts/settings-api.md}  # FR-010
specs/028-openai-decision-models/{spec.md,research.md,contracts/gateway-decisions.md}  # `types`, by replacement
```

**Structure Decision**: existing backend layout; the resolution lives in `openai_compat.py` beside the class both providers subclass, the record in `gateway_provider.py` beside the only code that reads the listing.

## Complexity Tracking

None.
