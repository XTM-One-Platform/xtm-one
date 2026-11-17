---

description: "Task list for Decision Models on OpenAI"
---

# Tasks: Decision Models on OpenAI and the Filigran AI Gateway

**Input**: Design documents from `specs/028-openai-decision-models/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/](contracts/)

**Tests**: required (constitution VI); every backend task has its test task, written first.

**Run backend tests** from `backend/` with `CUSTOM_TOOL_SANDBOX=auto SECRET_KEY=<32+ bytes> uv run pytest …` (a worktree has no `.env`).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an unfinished task)
- **[Story]**: the user story of spec.md the task serves

## Phase 1: Setup

None: no dependency, no migration, no configuration (plan, Technical Context).

---

## Phase 2: Foundational (blocks US1 and US3)

**Purpose**: providers built for the listing carry their config's identity (R3), and providers are listed concurrently (R4).

- [X] T001 In `_list_models_by_capability` in `backend/app/api/v1/settings.py`: build each provider with `await provider_for_config(cfg)` (imported from `app.llm.router`) instead of `reveal_field` + `provider_cls(api_key=...)`, keeping the `PROVIDER_MAP.get(cfg.provider)` skip of unknown types and the TEI fallback; list the configs concurrently with `asyncio.gather`, one coroutine per config holding today's per-config `try` / `except` / `logger.warning`, results kept in config order; the coroutines read nothing from `db` and only `cfg` attributes already loaded.
- [X] T002 In `backend/tests/test_decision_model_settings.py` and `backend/tests/test_settings_llm_providers.py` (the tests calling `_list_models_by_capability`), replace the `reveal_field` / `decrypt_value` patches with a patch of `app.api.v1.settings.provider_for_config` returning the fake provider; keep patching `app.api.v1.settings.PROVIDER_MAP`, which T001 still reads; keep each test's assertion (depends on T001)
- [X] T003 Test in `backend/tests/test_settings_llm_providers.py` that two configs whose listing coroutines each sleep 0.2 s are listed in under 0.4 s, in config order (R4) (depends on T002, same file)

**Checkpoint**: `uv run pytest tests/test_decision_model_settings.py tests/test_settings_llm_providers.py` passes.

---

## Phase 3: User Story 1 - Pick an OpenAI decision model (Priority: P1) 🎯 MVP

**Goal**: the Decision Model picker offers the models of OpenAI's model listing that OpenAI recognises on `/decisions`; the provider card is untouched. The frontend needs no change: it offers a decision provider that lists nothing for a typed model id and keeps a set model shown (research R5, R8).

**Independent Test**: spec US1 (an OpenAI config whose listing holds a decision and a chat model; only the decision model is listed).

### Tests for User Story 1

- [X] T004 [P] [US1] Create `backend/tests/test_openai_decision_models.py`: with `OpenAIProvider.client.models.list` and `client.post` stubbed, `list_decision_models()` first probes a control id matching `xtm-probe-[0-9a-f]{32}` and, once it is refused as unknown, one probe per listed model id, each with the body, path and options of [the probe contract](contracts/openai-decision-probe.md); it sends nothing and returns `[]` when the listing is empty or raises
- [X] T005 [US1] In the same file, one test per row of the probe contract's Outcome table, each error built from an `httpx.Response` carrying the observed body the contract quotes; the 429 row also asserts that, with 20 listed models and the first probe rate-limited, fewer than 20 probes are sent and none is remembered (FR-018)
- [X] T006 [US1] In the same file: when the control id gets anything but `model_not_found` (the duplicate-value refusal, a 2xx, 401, 403, 429, a timeout), no other probe is sent, nothing is listed and nothing is remembered (FR-016)
- [X] T007 [US1] In the same file, with `app.llm.decisions.redis_client` faked (dict-backed `mget` and pipelined `set(ex=)`): a second listing sends no probe, control included; a model newly in the listing is probed with the control, alone; an unknown outcome stores nothing; the key follows [the data model](data-model.md) and changes with the API key; TTL is 86 400 for a decision model and 3 600 for a model that is not; a Redis error on get or set still returns the probed result
- [X] T008 [US1] In the same file: with 20 listed models and a stub recording in-flight calls, never more than 8 probes run at once; with `openai_provider.DECISION_PROBE_DEADLINE_SECONDS` patched to 0.2, `list_decision_models()` returns `[]` within 1 s and remembers nothing both when `post` never answers and when `models.list` never answers; `models.list` is called with `timeout=5`, `max_retries=0` (FR-006, SC-008)
- [X] T009 [P] [US1] In `backend/tests/test_decision_model_settings.py`: `_list_models_by_capability(db, "decision", user, usable_only=True)` lists `("gpt-dec", provider_id)` for an OpenAI config whose stubbed provider recognises `gpt-dec` and not `gpt-chat`, whatever the config's `available_models` holds (FR-017)
- [X] T010 [US1] In `backend/tests/test_decision_model_settings.py`: `_write_decision_model` with a `decision_provider_id` whose config is `azure_openai` raises a 422 whose detail says the provider serves no decision models (FR-010)

### Implementation for User Story 1

- [X] T011 [US1] Add to `backend/app/llm/decisions.py` `recall_decision_support(config_id, scope, models) -> dict[str, bool]` (the remembered results) and `remember_decision_support(config_id, scope, results: dict[str, bool])` (`ex=86_400` for `True`, `ex=3_600` for `False`), on Redis key `llm:decision-model:{config_id}:{scope}:{model}` with value `1` / `0`; a Redis error is logged at debug and reads as nothing remembered / writes nothing; a `None` config id recalls and remembers nothing (data model; R2). Makes T007 pass
- [X] T012 [US1] Implement `OpenAIProvider.list_decision_models()` in `backend/app/llm/openai_provider.py` as [the probe contract](contracts/openai-decision-probe.md) and R4 specify, under the module constant `DECISION_PROBE_DEADLINE_SECONDS = 8`: candidates, the control awaited before any other probe, classification and rate-limit stop; one `logger.debug` line per round naming the provider config, how many models were probed and recognised, and why the round stopped if it did; recalled and remembered through T011 with scope `log_once.fingerprint(self.client.api_key)`; recognised models returned sorted, as the other `list_*_models` do. A comment states why the duplicate value and the control id (constitution VII). Makes T004–T008 pass (depends on T011)

**Checkpoint**: T004–T010 pass; quickstart Live steps 1–4 behave as written.

---

## Phase 4: User Story 2 - Decisions on OpenAI (Priority: P1)

**Goal**: a decision on an OpenAI model is sent, parsed and fitted like any other (R6).

**Independent Test**: spec US2, stubbed.

- [X] T013 [P] [US2] In `backend/tests/test_llm_decisions.py`, test that `OpenAIProvider.decision_context_window(model)` returns the config's per-model window from `context_windows` and `None` for a model without one (FR-013)
- [X] T014 [US2] Run `uv run pytest tests/test_llm_decisions.py`: the existing OpenAI request-shape, answer, refusal, unknown-label and 255-label tests pass unchanged (FR-011, FR-012)

**Checkpoint**: US2 needs no production change.

---

## Phase 5: User Story 3 - Provider access and listing cost (Priority: P2)

**Goal**: a restricted provider is neither built nor contacted; listing beside Ollama is unchanged.

**Independent Test**: spec US3.

- [X] T015 [P] [US3] In `backend/tests/test_decision_model_settings.py`, extend `test_the_decision_picker_asks_only_the_providers_the_caller_may_use` to an OpenAI pair: `provider_for_config` is called only for the usable config, so the restricted one is never probed (FR-007, SC-005) (depends on T002)
- [X] T016 [US3] In `backend/tests/test_decision_model_settings.py`, an Ollama config and an OpenAI config list together, each model with its own `provider_id` (US3-3, SC-007) (depends on T002)

**Checkpoint**: T015–T016 pass; T007 covers US3-2.

---

## Phase 6: User Story 4 - Decision models through the Filigran AI Gateway (Priority: P2)

**Goal**: the gateway serves decision models whichever way it is configured, both on its OpenAI-standard `/v1/decisions` route ([gateway contract](contracts/gateway-decisions.md), research R9, R10).

**Independent Test**: spec US4, with the listing and the client stubbed.

### Tests for User Story 4

- [X] T017 [P] [US4] Create `backend/tests/test_gateway_decision_models.py`: a `FiligranGatewayProvider` with `client.models.list` stubbed (entries with `type` and `owned_by`) lists only the `type == "decision"` ids, sorted, and `[]` when no entry carries a type; its `decide_choice` posts `/decisions` with the OpenAI Decisions body and the header `X-Target-Provider: <owned_by>`, no such header when the owner is `gateway`, and the credential's pinned upstream when one is pinned (a static-token credential `<url>||ollama||<token>`, so `StaticTokenAuth` sets it, on an `httpx.MockTransport`); a failing listing raises (FR-019, FR-021, FR-022)
- [X] T018 [US4] In the same file: a `CustomOpenAIProvider` with `_list_model_entries` stubbed lists only the entries with `type == "decision"`, `[]` when none has a type, a failing listing raises, and its `decide_choice` posts `/decisions` with the OpenAI Decisions body and no extra header (FR-020, FR-021)
- [X] T019 [P] [US4] In `backend/tests/test_llm_decisions.py`, turn `test_only_openai_openrouter_and_ollama_declare_a_decisions_api` into the map of every decision provider and its format (`openai`, `custom_openai` and `filigran_gateway`: OpenAI Decisions; `openrouter` and `ollama`: System One; every other `PROVIDER_MAP` entry: none) (FR-002, FR-019, FR-020)

### Implementation for User Story 4

- [X] T020 [US4] Give `decide_choice` in `backend/app/llm/decisions.py` a `headers` keyword sent as the SDK's `options["headers"]`, and `BaseLLMProvider` in `backend/app/llm/base.py` a `_decision_headers(model)` hook returning `{}` that its `decide_choice` passes (R10)
- [X] T021 [US4] In `backend/app/llm/gateway_provider.py`: `decisions_api = DECISIONS_OPENAI`, `list_decision_models()` from `_models_with_meta()` (`type == "decision"`), `_decision_headers()` returning `_target_headers(model)`. Makes T017 pass (depends on T020)
- [X] T022 [P] [US4] In `backend/app/llm/custom_openai_provider.py`: `decisions_api = DECISIONS_OPENAI`, `list_decision_models()` from `_list_model_entries()` (`type == "decision"`; a failed listing raises, as the other listings do). Makes T018 and T019 pass
- [X] T023 [P] [US4] Name the Filigran AI Gateway and custom OpenAI-compatible endpoints wherever the kinds of decision provider are listed: `NO_DECISION_PROVIDER_MESSAGE` and the pinned-provider refusal in `backend/app/services/flow_classifier.py`, the Decision Model `emptyMessage` in `frontend/src/pages/Settings.tsx` and its key in `frontend/src/i18n/lang/{en,fr,es,ja}.json` (FR-023)
- [X] T024 [P] [US4] Document both ways of configuring the gateway, their shared `/v1/decisions` route, the credential pin for a decision model whose id names no upstream (on a gateway provider used only for decisions, since the pin applies to every call), and the gateway prerequisites, in the Decision Model paragraphs of `docs/docs/administration/settings.md`, in the `list_decision_models()` line of `backend/app/llm/AGENTS.md`, and in `specs/027-decision-model-page-actions/research.md` and `contracts/settings-api.md` (FR-014)

**Checkpoint**: T017–T019 pass; quickstart's gateway steps behave as written.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T025 [P] Rewrite the Decision Model paragraphs of `docs/docs/administration/settings.md`: decision models offered are those providers report (Ollama's decision models; the models of an OpenAI provider's listing that OpenAI's Decisions API recognises, checked for free and remembered a day (an hour for a model that does not serve decisions), with nothing to enable on the provider card); no OpenAI default; a decision model's context window comes from the provider card only when the same model is enabled there as a chat model, else the page action reads a fixed page size (FR-013); a decision provider listing none is offered for a typed model id, and a set model stays shown (FR-014)
- [X] T026 [P] In `specs/027-decision-model-page-actions/research.md` and `contracts/settings-api.md`, state that OpenAI lists the models its Decisions API recognises among its model listing, citing spec 028 (FR-014)
- [X] T027 [P] Update the `list_decision_models()` line of `backend/app/llm/AGENTS.md` to name OpenAI's probe of its model listing, its control id and the memo in `decisions.py`
- [X] T028 Run `cd backend && uv run ruff check app/` and the quickstart pytest set, including `tests/test_no_workarounds.py`
- [X] T029 Run `cd frontend && yarn check` (tsc, eslint, i18n validation, unit tests)
- [X] T030 Grep the changed files under `backend/app/`, `backend/tests/`, `frontend/src/`, `docs/docs/` and `specs/` for `used to|no longer|previously|formerly|was removed` and rewrite any hit in current-state terms

---

## Phase 8: Review round

- [X] T031 [US4] Resolve an unpinned Classify node among several decision providers through their decision listings (R11) in `backend/app/services/flow_classifier.py`; tests in `backend/tests/test_flow_classifier_resolution.py` and `backend/tests/test_flow_classifier_postgres.py`
- [X] T032 [US4] Remember an unlisted gateway model and send the decision without the header when the gateway listing fails, in `backend/app/llm/gateway_provider.py`; tests in `backend/tests/test_gateway_decision_models.py`
- [X] T033 [US3] Run the decision listing on the config's instance within 10 s, the other capabilities on a fresh instance, in `backend/app/api/v1/settings.py` (FR-025, R3); tests in `backend/tests/test_settings_llm_providers.py`
- [X] T034 [US1] Budgets for the listing and the control, cancellation of unfinished probes, one round at a time, the endpoint in the memo scope, in `backend/app/llm/openai_provider.py` and `backend/app/llm/decisions.py` (FR-005, FR-006, FR-024); tests in `backend/tests/test_openai_decision_models.py`

---

## Dependencies & Execution Order

- **Phase 2** blocks US1 (T012 keys its memo on the config id; T009 uses the T002 seam) and US3 (T015, T016 use the T002 seam).
- **US2** depends on nothing; it can run first or alongside Phase 2.
- **US3** stubs the provider, so it can run after Phase 2 without T012.
- **US4** depends on nothing but T020 before T021; it can run alongside the other stories.
- **Polish** after the stories it documents (T025–T027 after US1, T024 for US4); T028–T030 last.

Within US1: T004–T010 first (failing), then T011 → T012.

## Parallel Example: User Story 1

```text
T004 → T005 → T006 → T007 → T008 (one new test file, in sequence)
T009 → T010 (test_decision_model_settings.py)
```

## Implementation Strategy

1. Phase 2 (T001–T003), checkpoint.
2. US1 (T004–T012): the MVP; an OpenAI decision model can be picked.
3. US2 (T013–T014) and US3 (T015–T016): confirmations, mostly tests.
4. US4 (T017–T024): the gateway, both ways of configuring it.
5. Polish (T025–T030), then commit per phase.
