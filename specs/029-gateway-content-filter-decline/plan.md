# Implementation Plan: Read the gateway's content_filter refusal as a model decline

**Branch**: `029-gateway-content-filter-decline` | **Date**: 2026-10-15 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/029-gateway-content-filter-decline/spec.md`

## Summary

The OpenAI-compatible provider maps `finish_reason: "content_filter"` to `end_turn`, so a gateway refusal is an empty successful answer. It will raise the existing `ModelDeclinedError` instead, at every place the finish reason is read on an OpenAI-format path (the compat provider, the shared tool-stream consumer, and native `OpenAIProvider`), through one decline builder shared with the Anthropic provider. Router failover, failure causes and the proxy already act on that error ([research.md](research.md) R3).

## Technical Context

**Language/Version**: Python 3.14 (backend)

**Primary Dependencies**: `openai` SDK 2.38 (`AsyncOpenAI`), existing `ModelDeclinedError` in `app/llm/base.py`

**Storage**: N/A

**Testing**: pytest (`backend/tests/test_model_declined.py` for the decline suite, `backend/tests/test_gateway_provider.py` for the gateway provider)

**Target Platform**: Linux server

**Project Type**: web-service (backend only)

**Performance Goals**: N/A; one string comparison per finish.

**Constraints**: `stop_details` is an extra field on the SDK's choice and arrives as a plain `dict`, not an object (R1).

**Scale/Scope**: two provider modules changed (`openai_compat.py`, `openai_provider.py`), one helper moved (`anthropic_provider.py` → `base.py`), tests.

## Constitution Check

*GATE: passed before research, re-checked after design: no change.*

| Principle | Verdict |
|---|---|
| II Fail-Closed | Pass. A filter refusal is refused, not passed on as an answer. |
| VI CI gates | Pass. Tests for every path (data-model.md table); `ruff check` run. |
| VII Rationale comments | Pass. One short comment where `content_filter` is read: why a bare one has no category. |
| VIII Shared utilities | Pass. One builder in `base.py` for both providers, no second copy of the category/explanation reading. |
| XI Minimal writing | Pass. |
| XII Generic mechanisms | Pass. Keyed on the protocol's `finish_reason`, not a vendor or model name; applies to every OpenAI-compatible provider. |
| I, III, IV, V, IX, X | Not touched. |

## Project Structure

### Documentation (this feature)

```text
specs/029-gateway-content-filter-decline/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── gateway-refusal.md
└── tasks.md             # /speckit-tasks, not created here
```

### Source Code (repository root)

```text
backend/
├── app/llm/
│   ├── base.py                # + declined_error(): builds ModelDeclinedError from stop_details (dict or object)
│   ├── anthropic_provider.py  # _declined() replaced by base.declined_error()
│   ├── openai_compat.py       # raise the decline at the 3 finish-reason sites (chat, chat_stream, shared tool-stream consumer)
│   └── openai_provider.py     # native OpenAI: the same at its chat and chat_stream (its tool stream is the shared consumer)
└── tests/
    ├── test_model_declined.py          # + gateway-shaped decline cases
    └── test_gateway_provider.py        # + one end-to-end case through FiligranGatewayProvider (only if it fits its fixtures)
```

**Structure Decision**: backend only; no frontend, schema or migration change.

## Design

1. `declined_error(model, stop_details, usage)` in `base.py`: reads `category` / `explanation` from a dict or an attribute object, keeps only non-empty strings (the rule `_declined` already applies), logs one warning naming the model and category, returns `ModelDeclinedError`. No provider argument: the shared consumer has no provider to name, and the model is enough. Anthropic's `_declined` and its three call sites move to it; its log line loses the word "Anthropic".
2. A `content_filter` finish in `OpenAICompatibleProvider.chat` / `chat_stream`, `OpenAIProvider.chat` / `chat_stream` and `consume_openai_stream_with_tools` (used by both) is turned into the decline by one helper, `declined_by_filter` in `openai_compat.py`, which reads the choice's `stop_details`; it is raised once usage is known: after the response, or after the stream loop, since the usage chunk trails the finish chunk (same order as Anthropic's streams).
3. `stop_reason` mapping is otherwise untouched (FR-005).

## Complexity Tracking

None.
