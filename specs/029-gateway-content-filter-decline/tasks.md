---

description: "Tasks for reading the gateway's content_filter refusal as a model decline"
---

# Tasks: Read the gateway's content_filter refusal as a model decline

**Input**: [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/gateway-refusal.md](contracts/gateway-refusal.md)

**Tests**: required (backend change, constitution VI). Written before the code they cover; they must fail first.

## Format: `[ID] [P?] [Story] Description`

Paths are from the repo root. Tests go in `backend/tests/`. Run Python from `backend/` with `.venv/bin/python`. Keep each file's line endings as they are.

## Phase 1: Setup

- [X] T001 Record the baseline: run `.venv/bin/python -m pytest tests/test_model_declined.py tests/test_gateway_provider.py tests/test_decline_fallback_models.py -q` in `backend/` and note any failures that exist before the change.

---

## Phase 2: Foundational (blocks every story)

**Purpose**: the one decline builder both providers use (R1, R2).

- [X] T002 Add `declined_error(model, stop_details, usage)` next to `ModelDeclinedError` in `backend/app/llm/base.py`. It reads `category` / `explanation` from a dict or an attribute object, keeps only non-empty strings, logs one warning naming the model and category, and returns the error. No provider argument (plan.md Design 1).
- [X] T003 Replace `_declined` in `backend/app/llm/anthropic_provider.py` (definition at the `_DECLINED_STOP_REASON` block, calls in `chat`, `chat_stream`, `chat_stream_with_tools`) with `declined_error(...)`, and the two calls in `backend/app/api/v1/llm_proxy.py` that imported it. Behaviour unchanged; the log line no longer says "Anthropic" (no test asserts it).
- [X] T004 Add unit tests for `declined_error` to `backend/tests/test_model_declined.py`: a dict, an object, `None`, and empty or non-string fields give `category` / `explanation` of `None` where absent (data-model.md).
- [X] T005 Run the existing Anthropic decline cases in `backend/tests/test_model_declined.py`: all still pass.

**Checkpoint**: T001's baseline holds and T004 passes.

---

## Phase 3: User Story 1 - A refusal through the gateway is a decline (P1) 🎯 MVP

**Goal**: a `content_filter` finish with a category raises a decline, streaming or not, on every OpenAI-format provider.

**Independent Test**: gateway-shaped refusal in, `ModelDeclinedError` out with category, explanation and usage; a `stop` finish returns the answer unchanged.

### Tests for User Story 1

Shared fixtures first, in `backend/tests/test_model_declined.py`:

- [X] T006 [US1] Add fixtures building the gateway shape of [the contract](contracts/gateway-refusal.md) with the OpenAI SDK types (`ChatCompletion` for `chat`, `ChatCompletionChunk` streams for the other paths), `stop_details` as a dict, and a trailing usage chunk. The stream fixture can also send content chunks before the final `content_filter` chunk.
- [X] T007 [US1] Tests in `backend/tests/test_model_declined.py` that raise on a categorised `content_filter`, each asserting `model`, `category == "cyber"`, `explanation`, `usage`, and one create call (no retry), for both `OpenAICompatibleProvider` and `OpenAIProvider`: `chat`; `chat_stream` (usage also reported to the sink); `chat_stream_with_tools` (the shared consumer).
- [X] T008 [US1] Tests in the same file that a `stop`, `length` and `tool_calls` finish return as today on the same six paths (FR-005).
- [X] T009 [US1] Test in the same file for the partial-text edge case: a `chat_stream` that sends content chunks and then `content_filter` delivers those chunks, then raises the decline with its usage reported, and returns no answer.

### Implementation for User Story 1

- [X] T010 [US1] `OpenAICompatibleProvider.chat` in `backend/app/llm/openai_compat.py`: on `choice.finish_reason == "content_filter"` raise `declined_error(model, getattr(choice, "stop_details", None), extract_openai_response_usage(response.usage))` instead of building the response. Add the two-line comment on why a bare `content_filter` carries no category.
- [X] T011 [US1] `OpenAICompatibleProvider.chat_stream` in `backend/app/llm/openai_compat.py`: remember a `content_filter` finish and its `stop_details`; after the loop, with `last_usage` set, raise the decline (R4). Chunks already yielded stay yielded.
- [X] T012 [US1] `consume_openai_stream_with_tools` in `backend/app/llm/openai_compat.py`: same, raising after the loop with `extract_openai_usage(usage_obj)`; the decline must not be retried (`is_retryable_openai` already excludes it).
- [X] T013 [US1] `OpenAIProvider.chat` in `backend/app/llm/openai_provider.py`: the same as T010.
- [X] T014 [US1] `OpenAIProvider.chat_stream` in `backend/app/llm/openai_provider.py`: the same as T011.

**Checkpoint**: T007–T009 pass; a categorised refusal is a decline on every OpenAI-format path.

---

## Phase 4: User Story 2 - A declined category fails over like any other (P2)

**Goal**: failover and failure causes treat a gateway decline as an Anthropic one. No production code expected (R3); the tests prove it.

**Independent Test**: route the gateway decline through the router.

- [X] T015 [US2] Test in `backend/tests/test_model_declined.py`: a `FailoverLLMProvider` whose primary raises the gateway decline in category `cyber` is answered by the next declared fallback model (data-model.md).
- [X] T016 [US2] Test in the same file: a `bio` decline is not handed on, and `classify_run_failure` records it as `model_declined` with its category.
- [X] T017 [US2] Test in `backend/tests/test_gateway_provider.py`: a `FiligranGatewayProvider` fed the gateway refusal over its mocked HTTP layer raises the decline (the provider that motivated the issue). Skip this task if the file's fixtures cannot serve a chat response without new scaffolding, and say so in the PR.

**Checkpoint**: if T015 or T016 fails, fix the cause in the router or `failure_causes` and say so in the PR.

---

## Phase 5: User Story 3 - A bare content filter is a decline with no category (P3)

**Goal**: a `content_filter` with no category declines and never fails over.

**Independent Test**: `content_filter` with no `stop_details` on the three compat paths and native OpenAI.

- [X] T018 [US3] Parametrized tests in `backend/tests/test_model_declined.py` over `stop_details` absent, `{}`, `{"category": None}`, `{"category": ""}`, on `chat`, `chat_stream` and `chat_stream_with_tools`, for `OpenAICompatibleProvider` and `OpenAIProvider`: each raises with `category is None` and `failover_allowed is False`.
- [X] T019 [US3] Test in the same file: a bare `content_filter` through the failover wrapper is not handed to another model.

**Checkpoint**: all three stories pass independently.

---

## Phase 6: Polish

- [X] T020 Grep every test directory (`backend/tests`, and any other that exists) for tests that expect `content_filter` to end as `end_turn` and update them; list what changed.
- [X] T021 [P] Add one line to `backend/app/llm/AGENTS.md`, where the decline mechanism is recorded: the OpenAI-format providers raise the decline on `content_filter`.
- [X] T022 Run the full backend suite and `.venv/bin/ruff check app tests` in `backend/`; compare with T001's baseline, naming every failing file.
- [X] T023 Walk [quickstart.md](quickstart.md); run `/code-review` on the diff before opening the PR.

---

## Dependencies & Order

- T001 → Phase 2 → stories. T002 → T003, T004, T010–T014. T005 after T003.
- US1 tests (T006–T009) before US1 code (T010–T014); T010–T012 share `openai_compat.py` and T013–T014 share `openai_provider.py`, so each file's tasks run in sequence (the two files can proceed in parallel).
- US2 and US3 need US1's code (they reuse its fixtures); they do not depend on each other.
- Polish after all stories.

No task is marked [P] in the stories: nearly all touch `openai_compat.py`, `openai_provider.py` or `test_model_declined.py`. Only T021 is independent; the two provider files could be split between two people.

## Strategy

MVP is US1 on top of Phase 2: the gateway refusal stops being an empty answer. Then US2 (proof that failover works), then US3.
