---

description: "Task list for 015-provider-id-routing"
---

# Tasks: Route by provider config, not by model name

**Input**: Design documents from `/specs/015-provider-id-routing/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/](./contracts/README.md)

**Tests**: backend changes carry test tasks — a blocking CI gate here (Principle VI). The frontend has no unit runner (Playwright E2E only), so frontend tasks are verified by `yarn check` plus [quickstart.md](./quickstart.md).

**Organization**: grouped by user story. US1 is shippable alone.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallelizable — different file, no incomplete dependency
- Paths are repo-relative: `backend/`, `frontend/`

---

## Phase 1: Setup

- [X] T001 Add `modelChoiceValue(model, providerId)` / `parseModelChoice(value)` to `frontend/src/lib/utils.ts`, encoding `<provider_id>::<model_id>` per research.md D4, with a two-line comment on why the value is composite (Principle VII); mirror the existing inline encoding at `frontend/src/pages/NewAgentWizard.tsx:319`

---

## Phase 2: Foundational (Blocking Prerequisites)

**⚠️ No user story can start until T005 is applied.**

- [X] T002 [P] Add `llm_provider_id` (nullable `UUID`, no FK) to `backend/app/models/general_assistant_config.py:133`, beside `llm_provider`/`llm_model`, with a comment on why there is no FK (FR-002, research.md D2)
- [X] T003 [P] Add `routing_provider_id` and `fallback_provider_id` (nullable `UUID`, no FK) to `backend/app/models/platform_settings.py:73,80`, beside the models they qualify (FR-003)
- [X] T004 Create one Alembic migration in `backend/alembic/versions/` off head `e9m0dl3349` adding the three columns, `IF NOT EXISTS`-guarded with a working `downgrade()` (Principle V, research.md D7)
- [X] T005 Apply the migration locally and confirm a single head (`cd backend && alembic upgrade head && alembic heads`)
- [X] T006 Add the three nullable id fields to the platform-settings and GA request/response schemas in `backend/app/api/v1/schemas/settings.py` (contracts/README.md §1, §2)
- [X] T007 Add the three id fields to `frontend/src/types/index.ts` and the settings payload types in `frontend/src/hooks/api/useSettingsApi.ts:158`

**Checkpoint**: columns and wire fields exist; nothing reads them yet.

---

## Phase 3: User Story 1 — A saved model choice keeps routing where it was saved (P1) 🎯 MVP

**Goal**: a stored provider reference decides the provider config; a null one changes nothing (FR-005, FR-006).

**Independent Test**: two configs advertising one model id, a choice saved against the older, a third config created — the call still reaches the older config (SC-001). Full steps in quickstart.md §US1.

### Tests for User Story 1

- [X] T008 [P] [US1] Test that `resolve_configured_fallback` prefers `ps.fallback_provider_id` over the recency winner, in `backend/tests/test_llm_router_fallback_model.py`
- [X] T009 [P] [US1] Test that a null reference resolves exactly as today, in `backend/tests/test_llm_router.py` — the SC-003 / FR-006 regression guard
- [X] T010 [P] [US1] Test that a dangling reference falls back to the stored provider name and logs, in `backend/tests/test_llm_router.py` (FR-007)
- [X] T011 [P] [US1] Test that GA chat forwards its stored reference as `provider_id_override`, and that the `prebuilt_llm` reuse branch no longer short-circuits when only the reference is set, in `backend/tests/test_general_chat_provider_reference.py` — `backend/tests/test_general_chat_provider_reference.py` drives the real function to the resolution call and stops there
- [X] T012 [P] [US1] Test that routing resolution forwards `routing_provider_id`, in `backend/tests/test_routing_evaluator_provider_reference.py`
- [X] T013 [P] [US1] Test that the settings response carries all three new ids, and reports none for a row that has none, in `backend/tests/test_provider_reference_call_sites.py` (read path)
- [X] T013b [P] [US1] Test the settings **PUT** write path — each id persisted, and omitting one leaving the stored value untouched — in `backend/tests/test_settings_llm_providers.py` — covers persist, omission leaving the stored value, and explicit null clearing it
- [X] T014 [P] [US1] Test the reference-vs-model-owner precedence in `backend/tests/test_llm_router.py` and `backend/tests/test_llm_router_fallback_model.py`: a reference that advertises the model outranks the recency guess even across differently-named providers, and one that does not still loses to the model owner (data-model.md I2, research.md D9). The original premise — reference always wins — was wrong; see D9

### Implementation for User Story 1

- [X] T015 [US1] Narrow the mismatch auto-correct in `backend/app/llm/router.py:1320` so a reference that advertises the model is not treated as a mismatch, for both the `provider_id_override` and `agent_default_provider_id` channels (research.md D9) — without this, the gateway-beside-vendor case still routes by recency
- [X] T015b [US1] Prefer `ps.fallback_provider_id` in `resolve_configured_fallback` in `backend/app/llm/router.py:718`, falling through to `_resolve_model_owner_config` when it is null
- [X] T016 [US1] Change `routing_model()` in `backend/app/services/flow_llm.py:166` to return `(model, provider_id)` and thread it through `resolve_llm`'s `fallback_model` path at `flow_llm.py:150`
- [X] T017 [US1] Update the three `routing_model()` callers to the tuple: `backend/app/services/flow_condition_llm.py:60`, `backend/app/services/flow_tool_selector.py:172`, `backend/app/services/workset_processing.py:611`
- [X] T018 [US1] Pass `provider_id_override=ps.routing_provider_id` in `backend/app/services/routing_evaluator.py:98`
- [X] T019 [US1] Pass `provider_id_override=ga_provider_id` in `backend/app/services/general_chat.py:258`, keeping the prebuilt-LLM reuse branch correct when only the id differs
- [X] T020 [US1] Persist `routing_provider_id` and `fallback_provider_id` in the platform-settings PUT in `backend/app/api/v1/settings.py:2135`, following the `image_provider_id` pattern at `settings.py:2091`, and add each id to its env-lock group at `settings.py:254,256` — the file's stated convention is that a provider name and its id lock together (`settings.py:210`), and without it an env-pinned routing model still accepts an admin-set provider id (spec.md edge case, FR-003)
- [X] T021 [US1] Persist and serialize GA `llm_provider_id` in `backend/app/api/v1/settings.py:294,497`, and add it to the env-locked field group at `settings.py:239-242`

**Checkpoint**: US1 complete. Routing honours saved references; deployments without duplicates are unchanged.

---

## Phase 4: User Story 2 — The picker can express which provider you meant (P2)

**Goal**: one option per offering config, each named, single read-back (FR-008, FR-009), and every picker persists the admin's actual selection (FR-001, FR-004).

**Independent Test**: two configs advertising one id — every picker shows two distinct labelled options and the trigger reads one, so the provider is readable without consulting creation order (SC-002). quickstart.md §US2.

- [X] T022 [P] [US2] Convert the five pickers in `frontend/src/pages/Settings.tsx:788,966,1056,1158,1504` to `modelChoiceValue()` for both `key` and `value`, label via the existing `getProviderDisplayName(m.provider, m.provider_id)` (`Settings.tsx:302`), and persist the parsed `provider_id` instead of the `find()`-derived one at `Settings.tsx:731,1212,1270,1428`
- [X] T023 [P] [US2] Convert the GA chat model picker in `frontend/src/pages/settings/GeneralAssistantSection.tsx:490-507` and send `llm_provider_id` from the parsed choice at `GeneralAssistantSection.tsx:357-367`, replacing the `find(m => m.model === gaModel)` provider derivation — and keep it out of the `gaModelEnvLocked` branch at `GeneralAssistantSection.tsx:365`, since the backend raises when a locked field appears in `model_fields_set` (`backend/app/api/v1/settings.py:1973-1974`)
- [X] T024 [P] [US2] Convert the agent picker in `frontend/src/pages/agent-detail/config/GeneralSection.tsx:544` and replace the `find()`-derived `llmProviderId` memo in `frontend/src/pages/agent-detail/ConfigTab.tsx:103-106` with the parsed selection
- [X] T025 [US2] Show the saved provider on read-back where a model is displayed without a picker: `frontend/src/pages/agent-detail/OverviewTab.tsx:164` resolves the provider by model name only
- [X] T026 [US2] Run `cd frontend && yarn check`, then confirm in the browser that no duplicate-key warning appears and each trigger reads one option (US2 acceptance scenarios 1-2). Verified in the running UI 2026-09-07; the design was arrived at against it, which is where the grouped branches and the `Anthropic (2)` naming came from (research.md D10, D11)

**Checkpoint**: US1 and US2 both work; a duplicated model id is selectable and readable.

---

## Phase 5: User Story 3 — Overlap is visible when it is created (P3)

**Goal**: saving an overlapping config warns and names the other config, without blocking (FR-010).

**Independent Test**: save a config advertising a model id another already advertises; the overlap is learned at save time (SC-004). quickstart.md §US3.

- [X] T027 [P] [US3] Test `model_overlaps` — empty with no duplicate, and naming both config ids for a shared model — in `backend/tests/test_settings_llm_providers.py`
- [X] T028 [US3] Add computed read-only `model_overlaps` to `LLMProviderResponse` in `backend/app/api/v1/schemas/settings.py:26` and populate it in `_serialize_llm_provider_response` in `backend/app/api/v1/settings.py:877` (research.md D5, contracts/README.md §3)
- [X] T029 [US3] Surface the overlap in the AI Models section of `frontend/src/pages/Settings.tsx` using design tokens and the `Tip` component (Principle X) — never a native `title`

**Checkpoint**: all three stories independently functional.

---

## Phase 6: Polish & Cross-Cutting

- [X] T030 Run the blocking gates: `cd backend && ruff check app/ && pytest tests/` and `cd frontend && yarn check` (Principle VI)
- [X] T031 Run `cd backend && pytest tests/test_migration_integrity.py` for the idempotent-re-run and downgrade round-trip (Principle V)
- [X] T032 Walk [quickstart.md](./quickstart.md) on the dev stack. Done 2026-09-07: the pickers and provider naming were driven in the real UI (T026), and a provider delete was exercised — which surfaced a separate defect. The delete step was mis-specified and has been corrected in quickstart.md. Steps 1-5 exercised against four real configs — two `anthropic` and two `openai`, 82 model ids offered by more than one config: all four raise the overlap badge, and all 165 picker options carry a unique `(provider_id, model)` pair, so nothing collides
- [ ] T033 [P] Dedupe the option lists in the model pickers outside this feature's scope — `frontend/src/components/ui/quota-editor.tsx:216`, `frontend/src/pages/agent-detail/BudgetTab.tsx:389`, `frontend/src/pages/Profile.tsx`, the Evaluation pages, `frontend/src/pages/integration-detail/NovaRulesTab.tsx`, `frontend/src/pages/custom-tool-detail/BuiltinToolDetail.tsx` — one entry per model id naming every offering provider. This is deliberately **not** FR-008's one-option-per-provider shape: these select a model for pricing, limits or a user preference rather than an administered route, so there is no reference to persist and a second option would be a choice the caller cannot record. Removes only the duplicate-key warning and the doubled trigger text. NOT done: the source-level dedupe in `get_available_models` removes repeats *within* one config, which is a different defect — these pickers still render one identical-looking entry per config offering a shared model id
- [X] T034 Confirm the two required rationale comments landed — the composite-value note in `frontend/src/lib/utils.ts:786` (T001) and the no-FK note in `backend/app/models/general_assistant_config.py:150-153` (T002) — and that neither restates what the code does (Principles VII, XI)

---

## Dependencies & Execution Order

### Phase dependencies

- Setup (T001) — no dependencies, and independent of the backend phases
- Foundational (T002-T007) — T004 depends on T002, T003; T005 depends on T004; **blocks every story**
- US1 (T008-T021) — after T006
- US2 (T022-T026) — after T007 and T001; needs US1 for the reference to *mean* anything, but is independently testable as UI behaviour
- US3 (T027-T029) — after T005; independent of US1 and US2
- Polish (T030-T034) — after the stories being shipped

### Within US1

Tests T008-T014 before implementation. T016 before T017 (the callers follow the signature). T015, T018, T019 are independent of each other. T020 and T021 both touch `settings.py`, so run them in sequence.

### Parallel opportunities

- T002 ‖ T003 (different model files)
- T008 ‖ T009 ‖ T010 ‖ T011 ‖ T012 ‖ T013 ‖ T014 — but T009, T010 and T014 share `test_llm_router.py`, so land them together
- T022 ‖ T023 ‖ T024 (different frontend files)
- US3 (T027-T029) can run alongside US1 or US2 entirely

```bash
# Foundational models, in parallel:
Task: "Add llm_provider_id to backend/app/models/general_assistant_config.py"
Task: "Add routing_provider_id and fallback_provider_id to backend/app/models/platform_settings.py"

# US2 pickers, in parallel:
Task: "Convert the five Settings.tsx pickers to composite values"
Task: "Convert the GA chat model picker in GeneralAssistantSection.tsx"
Task: "Convert the agent picker in config/GeneralSection.tsx and ConfigTab.tsx"
```

---

## Implementation Strategy

**MVP = Phase 1 + Phase 2 + US1.** It fixes the defect for every choice already carrying a reference and for the three gaining one, and the FR-006 guard (T009) means no deployment changes behaviour without a duplicate. It ships without any frontend change.

US1 alone leaves the three new references unreachable through the UI, so US2 is the natural second increment rather than an optional extra. US3 is genuinely optional and can ship separately.

---

## Notes

- Task count: 34 — foundational 6, US1 14, US2 5, US3 3, setup 1, polish 5.
- `_resolve_model_owner_config` and `_config_recency_key` (`router.py:649,669`) are deliberately **not** modified: they are the no-reference path (FR-006).
- No backfill task exists, by research.md D6. A payload that sends a provider name with no id keeps today's save-time pin, by research.md D8 — so no task touches `settings.py:2129`.
