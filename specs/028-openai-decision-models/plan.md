# Implementation Plan: Decision Models on OpenAI and the Filigran AI Gateway

**Branch**: `feat/openai-decision-models` | **Date**: 2026-10-16 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/028-openai-decision-models/spec.md`

## Summary

`OpenAIProvider.list_decision_models()` probes each model of OpenAI's model listing with a choice question OpenAI refuses before billing, and keeps the models it recognises (research R1). Each round first checks a made-up model id as a control, and goes on only when OpenAI refuses it as unknown (R1). Results are remembered in Redis per config and key (R2). Probes are bounded per provider, and providers are listed concurrently (R4). The settings listing builds providers through `provider_for_config()`, so the memo can key on the config (R3). The provider card's chat model selection is untouched (FR-017). The frontend is unchanged: it already offers a decision provider that lists nothing for a typed model id (R5) and keeps a set model visible (R8). The decision call itself already speaks OpenAI's format and is covered by tests (R6). The Filigran AI Gateway and custom OpenAI-compatible providers both speak OpenAI Decisions on the gateway's `/v1/decisions`; both list the models their listing types as `decision`, and a gateway decision names its upstream through a header hook when the listing or the credential gives one (R9, R10).

## Technical Context

**Language/Version**: Python 3.14 (backend); the frontend changes one message

**Primary Dependencies**: FastAPI, `openai` SDK (`AsyncOpenAI.post`), redis-py async, TanStack Query

**Storage**: Redis (probe results, 24 h TTL for a decision model, 1 h otherwise); no schema change, no migration

**Testing**: pytest with the OpenAI client and Redis stubbed

**Target Platform**: XTM One backend replicas and web frontend

**Project Type**: web service + web app

**Performance Goals**: first listing of an OpenAI provider's ~120 models within ~3 s; an unresponsive OpenAI within 8 s (SC-008); a repeat within the hour sends nothing about checked models (SC-004)

**Constraints**: zero billed requests for listing (SC-003); no model names in code (SC-002, XII); restricted providers never contacted (FR-007)

**Scale/Scope**: three provider classes, two memo helpers, one header hook, one listing seam, one message per language, docs

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status |
|---|---|
| I Assignment-driven | Pass: no automation added; listing is a settings read. |
| II Fail-closed | Pass: an unclear probe answer leaves the model unlisted (FR-004), and a control id not refused as unknown stops the round (FR-016); Redis down means re-probing, never listing unchecked models. |
| III Security | Pass: the key is never stored or logged; the Redis key carries a 12-hex digest of it (`log_once.fingerprint`). Endpoint auth unchanged. |
| IV Multi-replica | Pass: results live in Redis, shared by replicas; a race only probes twice, which is free and idempotent. |
| V Migrations | Pass: none. |
| VI CI gates | Pass: backend tests for probe, cache, listing, access. |
| VII Rationale comments | Required on the probe: it rests on OpenAI's observed, undocumented check order. |
| VIII Shared utilities | Pass: reuses `provider_for_config`, `log_once.fingerprint`, `redis_client`, `decision_provider_types`. |
| IX TanStack Query | Pass: no server-state change. |
| X Design system | Pass: one message string changes. |
| XI Minimal writing | Pass: artifacts cite R/FR ids. |
| XII No workarounds | Pass: vendor logic stays in the OpenAI adapter; models are classified by OpenAI's answer, never by name. |

## Project Structure

### Documentation (this feature)

```text
specs/028-openai-decision-models/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── openai-decision-probe.md
│   ├── gateway-decisions.md
│   └── settings-api.md
└── tasks.md             # /speckit-tasks
```

### Source Code (repository root)

```text
backend/
├── app/llm/decisions.py             # recall_decision_support() / remember_decision_support(): Redis memo (R2); decide_choice headers (R10)
├── app/llm/openai_provider.py       # list_decision_models(): probe (R1, R4)
├── app/llm/gateway_provider.py      # decisions_api, list_decision_models, _decision_headers (R9, R10)
├── app/llm/custom_openai_provider.py # decisions_api, list_decision_models (R9)
├── app/llm/base.py                  # _decision_headers hook (R10)
├── app/services/flow_classifier.py  # provider kinds named in its messages (FR-023)
├── app/llm/AGENTS.md                # list_decision_models() line
├── app/api/v1/settings.py           # _list_models_by_capability via provider_for_config (R3)
└── tests/
    ├── test_openai_decision_models.py   # new: probe classification, memo, concurrency
    ├── test_gateway_decision_models.py  # new: gateway and custom OpenAI-compatible listing and calls
    ├── test_llm_decisions.py            # decision providers map, headers
    ├── test_decision_model_settings.py  # picker lists OpenAI models; restricted provider not built
    └── test_settings_llm_providers.py   # listing seam moved to provider_for_config
docs/docs/administration/settings.md # Decision model section (FR-014)
frontend/src/pages/Settings.tsx       # decision provider kinds in the empty message (FR-023)
frontend/src/i18n/lang/{en,fr,es,ja}.json
specs/027-decision-model-page-actions/{research.md,contracts/settings-api.md}  # who lists decision models
```

**Structure Decision**: existing backend/frontend layout; no new module, the probe belongs to the OpenAI adapter and the memo beside `decide_choice`.

## Complexity Tracking

None.
