---

description: "Task list for gateway model types and decision models"
---

# Tasks: Gateway Model Types and Decision Models

**Input**: Design documents from `specs/031-gateway-model-types/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/](contracts/)

**Tests**: included (backend change, constitution VI). Each story's tests are written first.

**Organization**: by user story. Phases 1 to 3 are the types; Phase 4 is the decision picker, built after spec 028 merged.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: different file, no dependency on an unfinished task
- Paths are from the repository root; backend commands run from `backend/` with `uv run`

---

## Phase 1: Foundational (behaviour-preserving)

**Purpose**: the record type and membership tests every story builds on, with `types` still the single `type`, so nothing observable changes (SC-002).

- [X] T001 In `backend/app/llm/gateway_provider.py` add `GatewayModel(NamedTuple)` with `id`, `owner`, `types: tuple[str, ...]`; make `_models_with_meta()` return it with `types=(type or _classify_model(id),)`; switch `list_models`, `list_models_and_offered`, `list_models_grouped`, `list_embedding_models`, `list_image_models`, `list_video_models`, `list_speech_models` and `_owner_of` to attribute access and `"<capability>" in model.types` (keep the video and speech `or is_*_model(id)` clause) (R2, [data-model](data-model.md))
- [X] T002 In `backend/tests/test_settings_llm_providers.py` make the `meta` fake of `_list_models_of` (around line 968) return `GatewayModel(...)` instead of 3-tuples (depends on T001)
- [X] T003 Run `cd backend && uv run pytest tests/test_gateway_provider.py tests/test_settings_llm_providers.py tests/test_video_studio_music.py tests/test_speech_generation.py -q`: green with no other test edited

**Checkpoint**: the gateway behaves exactly as before; `types` is a one-element tuple.

---

## Phase 2: User Story 1 - A multi-type model appears in every picker of its types (Priority: P1) 🎯 MVP

**Goal**: a model typed chat and embedding (or chat and decision) is offered by each of its types' pickers (FR-001, FR-002).

**Independent Test**: stub `/v1/models` with `types` and list each capability ([quickstart](quickstart.md), Types).

### Tests for User Story 1

- [X] T004 [US1] In `backend/tests/test_gateway_provider.py` add a test class stubbing `/v1/models` through `_mock_provider` with the examples of [the list contract](contracts/gateway-model-list.md) (`[chat, embedding]`, `[chat, decision]`, `[chat, image]`, `type: decision` alone): assert each picker lists exactly the models of its capability, a `[chat, decision]` model stays in `list_models` and `list_models_grouped`, and `list_models_and_offered` still returns every id (US1 scenarios 1 to 3, SC-001)
- [X] T005 [US1] Same class: a model typed `[chat]` whose id the platform classes as video (`sora-2`) or speech is still listed by the video or speech picker, as today (Assumptions)

### Implementation for User Story 1

- [X] T006 [US1] In `backend/app/llm/gateway_provider.py` add `_types_of(entry)` resolving `types`, else `type`, else `_classify_model(id)` as the [contract](contracts/gateway-model-list.md) states (text entries only, duplicates dropped, order kept), call it from `_models_with_meta()`, and comment why `types` outranks `type` (R1, constitution VII). Makes T004 pass
- [X] T007 [P] [US1] State in `docs/docs/administration/settings.md` (the paragraph on the models of each section, near line 73) that a gateway's models are offered in the section of every type they declare (FR-010, types part)

**Checkpoint**: User Story 1 works on its own.

---

## Phase 3: User Story 2 - Older gateways and unknown types behave as today (Priority: P1)

**Goal**: no `types`, no type, an unknown type or a malformed `types` changes no picker (FR-003, FR-004, FR-005).

**Independent Test**: stub the listings below and compare with the pre-change output.

### Tests for User Story 2

- [X] T008 [US2] In the T004 class add the cases T006's resolution must hold: `type` only (the existing `test_list_models_sends_jwt_and_filters_chat` and `test_typed_listing_and_grouping` stay unedited and green), neither field (`text-embedding-3-small` lands in the embedding picker, `gpt-4o` in chat), `types: []` falls back to `type`, `types: ["rerank"]` is listed nowhere, `types: ["chat", "rerank"]` still lists in chat, `types: [1, "chat"]` reads as chat, `types: [123]` falls back to `type`, `["chat", "chat"]` lists once (US2 scenarios 1 to 3, SC-002, SC-003)

### Gate for the types

- [X] T009 [US2] Run `cd backend && uv run ruff check app/` and the whole `uv run pytest tests/ -q`, list the failing files against the baseline, then run `/code-review` while the diff is small; open the PR (`feat(llm): gateway provider reads the model list's types`)

**Checkpoint**: the types are complete and shippable alone.

---

## Phase 4: User Story 3 - Pick a gateway decision model that is also a chat model (Priority: P2)

**Goal**: the Decision Model section offers the models of the gateway, and of a custom OpenAI-compatible endpoint, whose types include decision (FR-006 to FR-009).

**Independent Test**: stub the listing ([quickstart](quickstart.md), decision picker).

Withdrawn with the wire per model (R4): the former T012 (the picker asks `list_decision_models()` generically, which T011 and the existing picker tests pin), T016 to T022. T014 already holds on `main`.

- [X] T010 [US3] Confirm spec 028 is merged, merge `origin/main`, and check the base has `decisions_api` on `FiligranGatewayProvider`, the `_decision_headers` hook and `backend/tests/test_gateway_decision_models.py`; if not, stop and tell the user (R5)
- [X] T011 [US3] In `backend/tests/test_gateway_decision_models.py` (the file 028 created) pin: `list_decision_models()` lists a model typed chat and decision beside the decision-only ones, and `list_models()` still lists it; a custom endpoint lists the same from its raw entries and ignores a malformed `types`; the gateway listing sends one request (FR-006 to FR-008, SC-005)
- [X] T013 [US3] In `backend/app/llm/gateway_provider.py` make `list_decision_models()` the sorted ids with `"decision" in model.types`, replacing 028's `type == "decision"` test; this also fixes the silent break the merge carried (R3, R5)
- [X] T014 [P] [US3] `backend/tests/test_llm_decisions.py` already names `filigran_gateway` and `custom_openai` as OpenAI Decisions decision providers (028; FR-009)
- [X] T015 [P] [US3] Document that the gateway's decision models are those whose `types` include decision: the Decision model paragraph of `docs/docs/administration/settings.md`, the `list_decision_models()` line of `backend/app/llm/AGENTS.md` (FR-010)
- [X] T027 [US3] In `backend/app/llm/openai_compat.py` add `declared_model_types(types, type)`; call it from `gateway_provider._types_of` and from `CustomOpenAIProvider.list_decision_models` in `backend/app/llm/custom_openai_provider.py` (FR-007, R6)
- [X] T028 [US3] Final gate on the merged tree: `cd backend && uv run ruff check app/`, the whole `uv run pytest tests/ -q -n 8` with its failing ids compared with `origin/main`, `/code-review`

**Checkpoint**: all three stories work; the pull request carries them.

---

## Dependencies & Execution Order

- Phase 1 first: T001 → T002 → T003. US1 and US2 depend on Phase 1; T008 depends on T006.
- US3 depends on US1 (T013 reads `types`) and on spec 028 merged (T010). T027 precedes T011's custom cases.
- T028 closes the branch.

## Parallel Opportunities

- T007 runs beside T004 to T006 (docs, other files); T014 and T015 run beside T011 and T013.

## Implementation Strategy

1. **MVP = Phases 1 to 3** (T001 to T009): US1 and US2, which needed only the field.
2. Phase 4 followed spec 028's merge: T010 first, then the membership fix and the custom endpoint (T013, T027) with their tests.
3. One signed commit per step; PR title `feat(llm): gateway provider reads the model list's types`.

---

## Phase 5: Convergence

- [X] T023 In `specs/031-gateway-model-types/research.md` R1 and the `plan.md` Summary, replace the restated `types` → `type` → classification chain with a citation of `contracts/gateway-model-list.md`, per Constitution XI
- [X] T024 Revise `specs/028-openai-decision-models/spec.md` (FR-021), `research.md` R9 and `contracts/gateway-decisions.md` by replacement, so the gateway and custom endpoints list decision models whose `types` include decision, per spec 031 Assumptions and R5
- [X] T025 State in `specs/027-decision-model-page-actions/research.md` and `contracts/settings-api.md` that the gateway's models are offered in every picker of their types, per FR-010
- [X] T026 Add one line to the "Chat catalogues hold chat models" paragraph of `backend/app/llm/AGENTS.md` (near line 63): a gateway model typed with several types is also offered by each `list_<capability>_models`, per plan: source tree
