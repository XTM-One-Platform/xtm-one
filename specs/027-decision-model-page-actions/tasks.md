---
description: "Tasks for the decision model and the act_on_page tool"
---

# Tasks: Decision Model and Host-Page Snapshot Tool

**Input**: `specs/027-decision-model-page-actions/` ([plan](plan.md), [spec](spec.md), [research](research.md), [data model](data-model.md), [contracts](contracts/))

**Tests**: required for every backend change (constitution VI). Write each test task first and confirm it fails.

**Paths**: XTM One paths are relative to the repository root. `W/` = `filigran-ui/packages/filigran-chatbot/` (separate repository and PR; worktree `filigran-ui/.claude/worktrees/act-on-page-5559`).

## Phase 1: Setup

- [X] T001 [P] Add `happy-dom` as a devDependency in `W/package.json`, and refresh `filigran-ui/yarn.lock` with `yarn install` (research R11)
- [X] T002 [P] Confirm `ollama list` shows `clef-flash` and `/v1/systemone` answers (it does: capabilities `decision`, `vision`)

---

## Phase 2: Foundational

- [X] T003 Add `decision_model()` tests (configured and unset) in `backend/tests/test_provider_reference_call_sites.py`
- [X] T004 Add `decision_model` / `decision_provider_id` to `backend/app/models/platform_settings.py`
- [X] T005 Create `backend/alembic/versions/d1dec5559001_add_decision_model.py` (down_revision `c2cti5696002`)
- [X] T006 Add `decision_model(db)` next to `routing_model` in `backend/app/services/flow_llm.py`
- [X] T007 Run `tests/test_migration_integrity.py` and `tests/test_provider_reference_call_sites.py` (30 passed)
- [X] T008 Create `backend/tests/test_ollama_decisions.py`: a choice through `OllamaProvider.decide_choice()` reaches `<base>/systemone`; `list_decision_models()` keeps the `/api/tags` models whose `capabilities` contain `decision`, and returns `[]` on a transport error; the base default is `[]`
- [X] T009 Add `list_decision_models()` (default `[]`) beside `list_image_models` in `backend/app/llm/base.py`. Decision calls reuse the existing `decisions_api` / `decide_choice()` (`app/llm/decisions.py`).
- [X] T010 Declare `decisions_api = DECISIONS_SYSTEM_ONE` and implement `list_decision_models()` in `backend/app/llm/ollama_provider.py`; extend the `decisions_api` tripwire in `backend/tests/test_llm_decisions.py` and the provider wording of `backend/app/services/flow_classifier.py` / `flow_catalog.py`. T008 then passes.

**Checkpoint**: decision models resolve and can be called. US1 and US2 can proceed in parallel.

---

## Phase 3: User Story 1 - Ask Ariane to do something on the current page (P1) 🎯 MVP

**Goal**: an embedded-chat agent calls `act_on_page(goal, text?, path?)`, the widget performs the decided actions, and the agent answers from the outcome.

**Independent Test**: quickstart §4 step 1. In the backend alone: `test_act_on_page.py` drives a full run against stubbed page answers.

### Tests for User Story 1

- [X] T011 [P] [US1] Create `backend/tests/test_page_action_answer.py` for `POST /api/v1/platform/chat/messages/approve` with `page_action` ([frame contract](contracts/page-action-frame.md) §3). Follow the fixtures of `backend/tests/test_chat_approval_reconnect.py`. Cases:
  - 400 when both `decisions` and `page_action` are present
  - 400 when neither is present
  - 422 when the snapshot is over 20000 characters
  - 404 on another user's conversation
  - 409 when nothing is waiting
  - 409 on a stale `action_id`
  - 200 on the matching id
  - a decisions-only body still behaves as before
- [X] T012 [P] [US1] Create `backend/tests/test_chat_turn_guard_page_action.py`, marked `integration` (real Redis). Cases:
  - pending set, then a push from a second client, then `await_page_action_result` returns the answer
  - a push with another id returns False
  - a wait with no answer returns None at its timeout
  - `clear_pending_page_action` makes a later push return False
- [X] T013 [P] [US1] Create `backend/tests/test_act_on_page.py` with a stubbed provider whose `decide()` is scripted, a stubbed page that answers through `push_page_action_result`, and a captured `emit_tool_event`. Cases:
  - the first frame is `snapshot`, or `navigate` when `path` is given
  - frames carry `event="page_action_required"`, `action_id` and `conversation_id`
  - `done` ends as `finished`, `impossible` as `cannot_reach`
  - a `click:eN` answer sends `{"type": "click", "ref": "eN"}`
  - a `type:eN` answer sends `{"type": "type", "ref": "eN", "text": <the agent's text>}`
  - 8 actions end as `budget_reached`
  - a monkeypatched clock past 240 s ends as `budget_reached`
  - a page wait timing out ends as `failed`
  - a `classify()` failure ends as `failed`, and the result names the decision model
  - each call is traced as `page_decision`
  - `act_on_page` is not in `SHIPPED_DEFAULT_WHITELIST`, and `evaluate_gate` with the shipped defaults pauses on it (FR-011)
- [X] T014 [P] [US1] Add `test_act_on_page_offered_only_when_declared_and_configured` to `backend/tests/test_act_on_page_offering_postgres.py`, marked `integration`. Using the `run_agent_chat` Postgres harness (model stubbed), the tool list holds `act_on_page` only with `page_actions=True` and a decision model set (FR-003, FR-005).

### Implementation for User Story 1 (XTM One)

- [X] T015 [US1] Add `set_pending_page_action(conv, action_id)`, `push_page_action_result(conv, result) -> bool`, `await_page_action_result(conv, action_id, timeout) -> dict | None` and `clear_pending_page_action(conv)` in `backend/app/services/chat_turn_guard.py`. Use the keys and TTLs in [data-model](data-model.md). The push RPUSHes only when the pending id equals `result["action_id"]`, with a why-comment (stale-step refusal, R2). The wait polls like `await_approval_decision`.
- [X] T016 [US1] In `backend/app/api/v1/platform.py`:
  - add `supports_page_actions: bool = False` to `PlatformChatRequest` after `supports_tool_approval`
  - add `PlatformChatPageActionAnswer`, with the fields and caps of contract §3
  - make `PlatformChatApprovalRequest.decisions` default to `[]` and add `page_action: PlatformChatPageActionAnswer | None = None`, with no model validator (the exactly-one check belongs to T017, so it answers 400)
- [X] T017 [US1] In `approve_platform_chat_tools` (`backend/app/api/v1/platform.py`):
  - replace the empty-decisions check with one requiring exactly one of `decisions` and `page_action` (400 otherwise)
  - after the ownership check, when `page_action` is set, call `push_page_action_result`; False gives 409 `"No page action is waiting for this answer"`, otherwise return `{"status": "accepted"}`
- [X] T018 [US1] Create `backend/app/services/builtin_tools/page_actions.py`:
  - `ACT_ON_PAGE_TOOL = "act_on_page"` and the `ToolDefinition` of the [tool contract](contracts/act-on-page-tool.md), with `source="builtin"`
  - `parse_snapshot(text) -> Snapshot`, which reads `[eN] role "name"` lines into refs, roles and names
  - `build_options(snapshot, text=...) -> dict`, the `click:eN` / `type:eN` options of research R6
  - `build_page_action_tools(db, *, user_id, conversation_id, model, provider_id)`, shaped like `conversation_reader.build_conversation_reader_tools`
  - a handler running the [data-model state diagram](data-model.md#state-transitions-of-a-run). For each step, in order:
    1. `set_pending_page_action`
    2. `emit_tool_event("page_action_required", conversation_id=..., action_id=uuid4().hex, action=...)`
    3. `await_page_action_result(timeout=20)`
    4. one choice through `flow_classifier.classify(..., trace_name="page_decision")` (provider resolution, access check and tracing reused), then a second round over the groups' picks when the page has more than 24 options
  - constants `MAX_ACTIONS = 8`, `PAGE_WAIT_SECONDS = 20` and `RUN_DEADLINE_SECONDS = 240`, with a why-comment tying the deadline to `TOOL_EXECUTION_TIMEOUT_SECONDS`
  - on `CancelledError` (a real task cancellation; a widget Stop ends at the next page timeout), `asyncio.shield(clear_pending_page_action(...))` and then re-raise, with a why-comment
- [X] T019 [US1] Add a `page_actions: bool = False` kwarg to `run_agent_chat` in `backend/app/services/agent_chat.py`. Next to the `read_conversation` registration, when it is true and `decision_model(db)` returns a model, call `composite.add_tools(*build_page_action_tools(...))`.
- [X] T020 [US1] Pass `page_actions=body.supports_page_actions` to `run_agent_chat` in the platform message handler (`backend/app/api/v1/platform.py`, beside `request_approval=`). T011–T014 then pass.

### Implementation for User Story 1 (widget, `W/`)

- [X] T021 [P] [US1] Create `W/src/utils/pageSnapshot.test.ts` (`node:test` + `happy-dom` `Window`). Cases:
  - the line format of the [snapshot text](contracts/page-action-frame.md#snapshot-text)
  - `.filigran-chatbot` subtrees, `hidden`, `aria-hidden="true"` and `inert` are skipped
  - a password value is never written
  - the name order: `aria-label`, then `aria-labelledby`, `<label>`, `alt`, `placeholder`, text cut to 80
  - output at most 20000 characters
  - refs resolve to their elements
- [X] T022 [P] [US1] Create `W/src/utils/pageActions.test.ts`. Cases:
  - `click` calls `click()`
  - `type` fires `input` with the new value through the native setter
  - `navigate` calls the callback, and without one fails with `navigation_unavailable`
  - an unknown ref gives `unknown_ref`
  - the settle wait resolves after 300 ms of DOM quiet and by 3 s at most
- [X] T023 [P] [US1] Extend `W/src/hooks/protocols/parseRestEvent.test.ts`:
  - a `status` frame with `event: "page_action_required"` parses to `{ action: 'page_action_required', actionId, pageAction, conversationId }`
  - a frame with no `action_id` parses to `noop`
- [X] T024 [P] [US1] Create `W/src/utils/pageSnapshot.ts` exporting `takePageSnapshot(doc: Document): { text: string; url: string; title: string; refs: Map<string, Element> }` (research R11). T021 then passes.
- [X] T025 [P] [US1] Create `W/src/utils/pageActions.ts` exporting `performPageAction(action, refs, onNavigate?): Promise<{ ok: boolean; error?: string }>` and `waitForDomSettle(doc)`. T022 then passes.
- [X] T026 [US1] Add the `page_action_required` variant to `ParsedAction` and a `PageAction` type in `W/src/hooks/protocols/types.ts`, and parse it in `W/src/hooks/protocols/parseRestEvent.ts` beside `approval_required`. T023 then passes.
- [X] T027 [US1] In `W/src/hooks/useChat.ts`:
  - set `body.supports_page_actions = true` under the same condition as `supports_tool_approval` (approve URL present)
  - on `page_action_required`, run these in order:
    1. `performPageAction(..., onRelativeLinkClick)` (skipped for `snapshot`)
    2. `waitForDomSettle`
    3. `takePageSnapshot(document)`
    4. POST `{ conversation_id, page_action: { action_id, ok, error, url, title, snapshot } }` to `getApproveUrl()`, with the same headers as `submitApprovalDecisions`
  - drop the action silently on 409
  - show the agent status "Acting on the page…" while it runs
  - `handleStopGenerating` discards the current refs

**Checkpoint**: US1 runs once a decision model is set (through the API or US2).

---

## Phase 4: User Story 2 - Choose the decision model in the AI provider panel (P1)

**Goal**: an admin picks a decision model and its provider in Settings → AI.

**Independent Test**: quickstart §1.

### Tests for User Story 2

- [X] T028 [P] [US2] Extend `backend/tests/test_llm_provider_deletion.py` and `backend/tests/test_llm_provider_deletion_postgres.py` wherever `routing_model` is asserted:
  - `decision_model` appears in the impact, in `routes()`, in the fingerprint and in the cleanup
  - it is never `env_managed`
- [X] T029 [P] [US2] Add GA config tests next to the existing `routing_model` PUT tests (`rg -l "general-assistant" backend/tests`):
  - a `decision_model` without `decision_provider_id` gets 422
  - a provider id naming no config gets 422
  - a provider whose class has no `decisions_api` gets 422
  - `""` clears both columns
  - a non-admin gets 403
  - GET returns both fields
  - `GET /settings/available-decision-models` lists the stubbed provider's decision models
- [X] T030 [P] [US2] Extend `frontend/src/pages/settings/provider-deletion.test.ts`. A `decision_model: cleared` impact counts as a change and renders its info message.

### Implementation for User Story 2

- [X] T031 [US2] Add `decision_model` / `decision_provider_id` to `GeneralAssistantConfigRequest` and `GeneralAssistantConfigResponse`, and add `decision_model: ProviderDeletionRoute` to `LLMProviderDeletionImpact`, in `backend/app/api/v1/schemas/settings.py` ([settings contract](contracts/settings-api.md)).
- [X] T032 [US2] In `backend/app/api/v1/settings.py`, add the pair next to each `routing_*` occurrence:
  - `_build_ga_response`, and the no-config branch of `get_general_assistant_config`
  - `_GA_PROVIDER_REFERENCE_FIELDS`
  - `_GA_PROVIDER_ID_FIELDS` as `("decision_provider_id", "platform", "the decision model")`
  - the PUT write block, with the contract's rules
  - `_cleanup_orphaned_model_references`
  - the deletion preview response
  - `GET /available-decision-models` via `_models_for_capability(db, "decision", user)`

  Do not add the pair to `_env_managed_ga_fields`.
- [X] T033 [US2] Add `decision_model: RouteImpact` to `DeletionImpact`, its `routes()` entry, and `reference_route("decision_model", "decision_provider_id")` in `backend/app/services/llm_provider_deletion.py`. T028 and T029 then pass.
- [X] T034 [P] [US2] Add `decision_model?: string | null` and `decision_provider_id?: string | null` to the GA config types, and `decision_model` to the deletion-impact type, in `frontend/src/types/index.ts`. Add a `useAvailableDecisionModels` hook beside `useAvailableEmbeddingModels` in `frontend/src/hooks/api/useSettingsApi.ts`.
- [X] T035 [US2] Create `frontend/src/pages/settings/PlatformModelSetting.tsx` by extracting the Routing Model section of `frontend/src/pages/Settings.tsx`. Its props cover the field names, icon, labels, the empty-choice label, `envLocked`, the model list and `onSave`. Keep `EnvLockBadge`, `ModelSelectValue` / `ModelSelectItems`, `modelChoiceValue` / `parseModelChoice` and the `"__none__"` item.
- [X] T036 [US2] In `frontend/src/pages/Settings.tsx`:
  - replace the Routing section with `<PlatformModelSetting>` (same strings, same `envLocked`)
  - add a Decision Model section under it, fed by `useAvailableDecisionModels`, with "Not configured" as the empty choice, `envLocked={false}` and a Lucide `MousePointerClick` icon
  - remove the now-unused routing draft state and effect
- [X] T037 [US2] Add the `decision` route to the `changesSomething` list and the info messages in `frontend/src/pages/settings/provider-deletion.ts`. Leave it out of the `envManaged` list. T030 then passes.
- [X] T038 [P] [US2] Add the new strings to `frontend/src/i18n/lang/en.json`, `fr.json`, `es.json` and `ja.json`, then run `node frontend/scripts/validate-i18n.mjs`.
- [X] T039 [US2] Add a `['Decision Model', 'combobox', ...]` row beside the Routing row, and `decision_model: unchanged` to the deletion-impact mock, in `frontend/tests_e2e/settings-llm.spec.ts`.

**Checkpoint**: US2 is testable alone (quickstart §1), and US1 now runs on the chosen model.

---

## Phase 5: User Story 3 - Page content stays out of the agent's context (P2)

**Goal**: only snapshot elements can be acted on, and only quarantined element names reach the agent.

**Independent Test**: quickstart §4 step 3. In the backend alone: the tests below.

### Tests for User Story 3

- [X] T040 [P] [US3] Add the following cases to `backend/tests/test_act_on_page.py`:
  - an answer naming a key it was not offered sends no frame and ends as `failed`
  - `path` values `https://x`, `//x`, `javascript:x` and `x` are refused before any frame
  - `text` over 1000 characters is refused before any frame
  - the same action on an unchanged snapshot hash ends as `cannot_reach`
  - a snapshot over 12000 characters is cut in the `state` and the outcome has `truncated: true`
  - the tool result has only `outcome`, `steps` and `truncated` outside the quarantine block, with url, title and step targets inside it

### Implementation for User Story 3

- [X] T041 [US3] Add `_validate_inputs(text, path) -> str | None`, the offered-key check and the no-progress check to `backend/app/services/builtin_tools/page_actions.py`, wired into the T018 loop (research R6, FR-013).
- [X] T042 [US3] Add `_build_result(outcome, steps, snapshot, truncated)` to `backend/app/services/builtin_tools/page_actions.py`. It emits the platform-owned JSON line followed by one `wrap_untrusted(..., source="host page")` block, with a why-comment on the split (R7). Apply the 12000-character cut to the state. T040 then passes.

**Checkpoint**: all three stories pass their independent tests.

---

## Phase 6: Polish & Cross-Cutting

- [X] T043 [P] Document the Decision Model section (decision models only, the tool off while it is unset, cleared on provider deletion) in `docs/docs/administration/settings.md`.
- [X] T044 [P] Document `supports_page_actions`, the `page_action_required` frame and the `page_action` answer in `docs/docs/deployment/chat-approvals-in-clients.md`, and `act_on_page` in the embedded chat section of `docs/docs/deployment/xtm-suite-integration.md`.
- [X] T045 [P] Add the decision model to model resolution in `dev-docs/reference/entities.md`; `decide` / `list_decision_models` to the provider interface in `backend/app/llm/AGENTS.md`; and `page_action_required` as a consumer of `emit_tool_event` in `backend/app/tools/AGENTS.md`.
- [X] T046 Run `cd backend && ruff check app/`, the pytest files of T003, T008, T011–T014, T028, T029 and T040, and `cd frontend && yarn check`. Then start `./scripts/ci-local.sh` in the background and let it queue.
- [X] T047 Sweep all 63 OpenCTI menu targets on `clef-flash` (goals named by the French menu path, the tool's loop replicated in the page with the real widget snapshot and actions): 53 / 63 reached (84%), 0 runs reporting `finished` on a wrong page, 13 s per target. Before the `open` action: 15 / 63. Misses: a section whose menu the model clicked instead of opening (3), "Données > …" after a dialog opened by a wrong click stayed open (6), "Données > Entités" taken for the "Entités" section (1). Full Ariane turns on the local qwen3.6 agent remain limited by its goal wording.
- [X] T048 Bump `W/package.json` to `3.13.0` in the widget PR (`chore(chatbot): bump version to 3.13.0`), then run `yarn test:filigran-chatbot`.
- [ ] T049 After the widget is published, bump `@filigran/chatbot` to `3.13.0` in `opencti/opencti-platform/opencti-front/package.json` (a separate OpenCTI PR, with no code change). The local `portal:` resolution used for live testing is reverted, never committed.

---

## Dependencies & Execution Order

- **Phase 2** blocks everything. T008 comes before T009–T010.
- **US1 (Phase 3)** and **US2 (Phase 4)** depend only on Phase 2.
- **US3 (Phase 5)** depends on T018 (same module).
- **Within US1**: tests T011–T014 first. Then T015, then T016 → T017, then T018 → T019 → T020. The widget track (T021–T027) depends only on the [frame contract](contracts/page-action-frame.md).
- **Within US2**: T031 → T032 → T033, and T034 → T035 → T036, with T037 and T038 in parallel with T035.
- **Polish**: after the stories. T049 after the widget release.

## Parallel Example: User Story 1

```text
Task: "T011 route tests in backend/tests/test_page_action_answer.py"
Task: "T012 Redis wait tests in backend/tests/test_chat_turn_guard_page_action.py"
Task: "T013 loop tests in backend/tests/test_act_on_page.py"
Task: "T021 W/src/utils/pageSnapshot.test.ts"
Task: "T022 W/src/utils/pageActions.test.ts"
Task: "T023 W/src/hooks/protocols/parseRestEvent.test.ts"
```

## Implementation Strategy

1. Phase 2 and US2 make a shippable XTM One PR: the setting and the provider capability.
2. US1 and US3 come together in a second PR. It is inert until a widget sends `supports_page_actions` and an admin sets a decision model.
3. The widget PR (T021–T027, T048), then its release, then T049.
4. Each checkpoint is a stop to validate.
