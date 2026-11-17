# Implementation Plan: Decision Model and Host-Page Snapshot Tool

**Branch**: `claude/decision-model-ai-config-8169ed` | **Date**: 2026-10-12 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/027-decision-model-page-actions/spec.md`

## Summary

There are two parts:

- An admin-set **decision model**: a model that answers typed questions instead of writing text (`clef-flash` on Ollama's System One API), with the provider that owns it, stored on `platform_settings` and edited in the AI panel beside the routing model.
- A built-in **`act_on_page`** tool, which is the spec's snapshot tool. It is offered on every embedded-chat turn whose widget declares `supports_page_actions`.

The tool's loop:

1. It sends action frames to the widget through `emit_tool_event`.
2. It waits for the widget's answer, which arrives on the existing `/chat/messages/approve` route and is relayed through Redis.
3. It asks the decision model which element to act on, or whether the goal is reached, as a choice over options code built from the snapshot, through the flow Classify node's `classify()`.
4. It returns a quarantined outcome to the agent.

The widget builds the page snapshot and performs actions in the same document, navigating through the host's existing `onRelativeLinkClick`. OpenCTI's code is unchanged; it only bumps the widget version.

## Technical Context

**Language/Version**: Python 3.14 (backend), TypeScript / React 19 (frontend, widget)

**Primary Dependencies**: FastAPI, SQLAlchemy 2 async, Redis. The widget adds `happy-dom` as a dev dependency only (research R11).

**Storage**: PostgreSQL, two nullable columns ([data-model](data-model.md)). Redis holds the transient wait keys.

**Testing**: pytest (unit, plus `integration` for Redis/Postgres). Widget: `node --test`. Frontend: `yarn check`, and a Playwright spec.

**Target Platform**: Linux server with N replicas; the widget runs in host browsers.

**Project Type**: web service + SPA + embeddable widget package

**Performance Goals**: a two-click goal finishes within 8 steps on `clef-flash` (SC-002).

**Constraints**: 20 s per page wait and 240 s per run, under the 300 s tool timeout (R8). Snapshot caps are 20000 characters on the wire and 12000 to the model (R7).

**Scale/Scope**: one tool, one setting, Ollama joining the decision providers (`decisions_api`, `list_decision_models`), one route extension, two widget modules.

## Constitution Check

| Principle | Status |
|---|---|
| I Assignment-driven | Pass: the tool runs inside the agentic loop. There is no new automation path. |
| II Fail-closed | Pass: the tool is offered only when the client declares it. An unknown ref, an off-origin path or an unparsable answer is rejected (R6). A wait always ends (R8). |
| III Security | Pass: the snapshot goes only to the decision model, which can only pick among options code built (R6). The element names returned to the agent pass through `wrap_untrusted` (R7). No prompt text is added anywhere. The route keeps its JWT and ownership checks. |
| IV Multi-replica | Pass: the answer is relayed through Redis keys with TTLs, and the pending `action_id` is matched (R2). |
| V Migrations | Pass: one idempotent ADD COLUMN pair with `terminate_lock_holders` + `set_lock_timeout`, a single head, and a downgrade. |
| VI Quality gates | Pass: see Testing above. |
| VII Rationale comments | Required in four places: the action-id match, the run deadline vs the tool timeout, the shielded clear on cancel, and the quarantine split. |
| VIII Shared utilities | Pass: it reuses `emit_tool_event`, `resolve_llm`, `traced_generation`, `structured_output`, `wrap_untrusted` and `validate_selectable_chat_model`. The routing UI is extracted into `PlatformModelSetting` rather than copied (R10). |
| IX TanStack Query | Pass: the existing `useUpdateGeneralAssistantConfig`. |
| X Design system | Pass: the extracted component keeps the existing tokens and `Select`. |
| XI Minimal writing | Pass |
| XII No workarounds | Pass: no tool-name, vendor or keyword logic. Decision support is the existing provider capability (`decisions_api`), and listing is `list_decision_models()`, as embeddings and images are listed. Code checks the decision against data (offered labels, path shape). |
| Invariant 16 | Pass: behaviour is enforced in code (R6), not in prompt text. |

Post-design re-check: no change, and there are no violations, so Complexity Tracking is empty.

## Project Structure

### Documentation (this feature)

```text
specs/027-decision-model-page-actions/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── page-action-frame.md
│   ├── decision-output.md
│   ├── act-on-page-tool.md
│   └── settings-api.md
└── tasks.md            # /speckit-tasks
```

### Source Code

```text
backend/
├── alembic/versions/<rev>_add_decision_model.py          # new
├── app/models/platform_settings.py                       # +2 columns
├── app/api/v1/schemas/settings.py                        # GA request/response, deletion impact
├── app/api/v1/settings.py                                # PUT validation, provider-ref lists, orphan cleanup
├── app/services/llm_provider_deletion.py                 # DeletionImpact.decision_model
├── app/services/flow_llm.py                              # decision_model(db)
├── app/llm/base.py                                       # list_decision_models()
├── app/llm/ollama_provider.py                            # decisions_api, /api/tags capabilities
├── app/services/flow_classifier.py                       # classify(trace_name=)
├── app/services/chat_turn_guard.py                       # page-action pending/inbox helpers
├── app/services/builtin_tools/page_actions.py            # new: act_on_page builder + loop
├── app/services/agent_chat.py                            # page_actions kwarg → add tool
├── app/api/v1/platform.py                                # supports_page_actions; page_action answer
└── tests/
    ├── test_act_on_page.py                               # new: loop, validation, budgets, quarantine
    ├── test_page_action_answer.py                        # new: route 400/404/409/200, cross-replica
    ├── test_ollama_decisions.py                          # new: Ollama decide_choice() route, list_decision_models()
    └── (extend) test_llm_provider_deletion*.py, test_provider_reference_call_sites.py

frontend/
├── src/pages/settings/PlatformModelSetting.tsx           # new: extracted from the routing section
├── src/pages/Settings.tsx                                # Routing + Decision use it
├── src/types/index.ts                                    # GA config + deletion impact fields
├── src/pages/settings/provider-deletion.ts (+ .test.ts)  # decision route
├── src/i18n/lang/{en,fr,es,ja}.json
└── tests_e2e/settings-llm.spec.ts                        # Decision Model row

docs/docs/administration/settings.md                      # Decision Model + deletion clearing
docs/docs/deployment/xtm-suite-integration.md             # act_on_page in the embedded chat
dev-docs/reference/entities.md                            # model resolution: decision model

filigran-ui/packages/filigran-chatbot/
├── src/utils/pageSnapshot.ts (+ .test.ts)                # new
├── src/utils/pageActions.ts (+ .test.ts)                 # new
├── src/hooks/protocols/{types,parseRestEvent}.ts (+ test)# page_action_required
├── src/hooks/useChat.ts                                  # flag, handler, answer POST
└── package.json                                          # 3.13.0, happy-dom devDependency

opencti-front/package.json                                # @filigran/chatbot 3.13.0 (after widget release)
```

**Structure Decision**: the existing backend/frontend layout, plus the widget package in `filigran-ui`. The tool is one module under `services/builtin_tools/`, next to `conversation_reader.py`, whose turn-scoped registration it copies (R3).

## Delivery Order

1. XTM One: setting, migration and UI (US2). This can ship alone.
2. XTM One: route extension and `act_on_page` (US1, US3). This is inert until a widget declares the flag.
3. Widget 3.13.0 release.
4. OpenCTI version bump.

## Complexity Tracking

No violations.
