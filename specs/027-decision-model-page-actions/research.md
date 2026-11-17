# Research: Decision Model and Host-Page Snapshot Tool

Paths are relative to the repository root (`backend/app/...`) or to `filigran-ui/packages/filigran-chatbot` (`W/...`).

## R1. Action frame transport

- **Decision**: the tool handler sends each action with `emit_tool_event("page_action_required", ...)` (`tools/tool_events.py`). It reaches the platform stream as a `status` frame with `event: "page_action_required"`.
- **Rationale**: this is the existing channel for a tool to speak to the client mid-call, and the runner's inline approval (`builtin_tools/computer_use.py`) uses it the same way. It needs no change to the agent loop or to `run_agent_chat`'s stream plumbing. An action frame is far below the 8000-character payload cap.
- **Alternatives**: a new top-level frame from a `request_approval`-style callback was rejected, because it adds a loop-level hook for something a tool can do alone.

## R2. Answer route and wait

- **Decision**: `PlatformChatApprovalRequest` gets an optional `page_action` field, and `decisions` becomes optional. The handler requires exactly one of the two and answers 400 otherwise, replacing today's empty-decisions check, so the status stays 400 rather than the 422 a model validator would give.
- **Storage**: `chat_turn_guard.py` gets `set_pending_page_action` / `push_page_action_result` / `await_page_action_result` / `clear_pending_page_action`. The keys are `chat:page_action:pending:{conv}` (the waiting action id) and `chat:page_action:{conv}` (the inbox).
- **Delivery**: the push succeeds only when the pending id matches the answer's `action_id`. Otherwise the route answers 409.
- **Wait**: same poll shape as `await_approval_decision`, bounded by the per-step timeout.
- **Rationale**: the OpenCTI proxy already forwards this route's body whole (FR-009). Redis makes an answer that lands on another replica work. Matching on `action_id` refuses an answer meant for an older step.
- **Alternatives**: reusing the approval keys was rejected, because `pending-approvals` would hand page actions to the widget's approval prompt on reload.

## R3. Offering the tool on some turns only

- **Decision**: `PlatformChatRequest` gets `supports_page_actions: bool = False`, which `platform.py` passes to `run_agent_chat(page_actions=...)`. When it is true and a decision model is set (FR-003), `run_agent_chat` adds the tool with `composite.add_tools(...)` and no builtin id, as `read_conversation` is added.
- **Rationale**: FR-005 says every agent and no per-agent setting, so the tool must not live in `agent_builtin_tool_ids`. The flag is per message for the same reason as `supports_tool_approval`: a client that cannot answer must never receive a frame.

## R4. Tool name

- **Decision**: `act_on_page`.
- **Rationale**: the agent picks tools by name. The tool acts as well as reads, and the `browser` tool already has a `snapshot` action, which a tool named "snapshot" would be confused with. The spec's "snapshot tool" is this tool.

## R5. Decision model resolution and call

- **Decision**:
  - Columns: `platform_settings.decision_model` and `decision_provider_id`. `flow_llm.decision_model(db)` is the twin of `routing_model(db)`; with no model the tool is not built (FR-003).
  - Each decision is one `flow_classifier.classify(..., trace_name="page_decision")` call: the Classify node's provider resolution (pinned config, access check, "serves decisions" check), `BaseLLMProvider.decide_choice()`, the label check and the trace.
  - Ollama declares `decisions_api = DECISIONS_SYSTEM_ONE`, so `decide_choice()` reaches `/v1/systemone`. `list_decision_models()` (base default `[]`) is new: Ollama keeps the `/api/tags` models whose `capabilities` contain `decision`, for the settings picker. OpenAI lists the models of its listing that its Decisions API recognises, and the Filigran AI Gateway and custom OpenAI-compatible providers the models their listing types as `decision` (`specs/028-openai-decision-models`).
- **Rationale**: `clef-flash` has no chat or generate endpoint and only answers System One questions. The decisions module, its providers and the classifier already exist; one line makes Ollama one of them.
- **Alternatives**: a schema-constrained chat call was rejected, because a decision model cannot answer one and the user chose to have no fallback.

## R6. One step's question (FR-007)

- **Options**: code builds them from the latest snapshot.
  - `click:eN` for each element.
  - `open:eN` for each element whose menu is closed (state `menu`): the widget hovers it, and the next snapshot lists the menu's items. Found by sweeping OpenCTI's 63 menu targets: every section's sub-page but the first lives in a menu that opens on hover, so without it only 15 of 63 were reachable.
  - `type:eN` for each text field (role `textbox`, `searchbox` or `combobox`) while the agent's `text` is untyped. Typing presses Enter.
- **Per step**: first whether the goal is reached (`done` / `not_yet`), then one choice over the options plus `impossible` ("no action helps"). `done` was first an option beside the actions; on XTM One's agent wizard ("click Step 4") it lost to the next step button on Steps 5 and 6, while asked alone it was answered right after the requested step (`tev1:4b` 0.23, `clef-flash` 0.55).
- **Steps taken**: each question's state lists the run's steps (`Done so far`). Without it, "type the persona, then click Next" read the same on every following page, and the run clicked Next until its budget ran out.
- **Commit check**: unless the agent passes `submit`, a chosen action (a click, an `open`, which clicks a menu that does not open on hover, or a `type`, which presses Enter in a form of that one field) is first asked "would this save, create, send, submit or delete something?" with the page in the state, and a `commits` answer ends the run as `stopped_before_submit`. Form semantics (`type="submit"`) would not do: XTM One's "Create Agent" is a plain button outside any form, and the run asked to stop before validating created the agent. Without the page, both decision models judged that button `moves`; with it, `commits` (0.77, 0.61). "Add an assignment" was also judged `commits`, at near-zero confidence: a wrong stop costs the agent a call, a wrong click cannot be undone.
- **More than 15 options**: one choice per group of 15 plus `none`, sequentially, then the same over the groups' picks until they fit one choice of at most 15 plus `impossible`. Sequential because each call resolves its provider on the turn's database session.
- **Answer handling**: `done` gives `finished` when its confidence is at least 0.5, else `unsure`: a `done` at 0.31 came on the very dialog the goal named ("Générer un export" behind OpenCTI's Quick export) and one at 0.46 on a page that only resembled the goal, so a low `done` is reported as possible, never as unreachable; `impossible` gives `cannot_reach`. A navigation to the agent's `path` counts as arrived only when the page's URL then starts with it. That misses an unknown route the host renders as another page under the new URL (OpenCTI's `/dashboard/analysis/reports` shows the dashboard); comparing the elements before and after was tried and dropped, because a live page's elements change anyway (a nav link's `current` state). A label that was not offered is refused by `parse_choice_response`, giving `failed`. The same action on an unchanged snapshot hash gives `unverified` when the page performed it, `cannot_reach` otherwise: found live, where a toggle styled by a class alone made a successful click read as unreachable.
- **Goal wording**: on OpenCTI's French dashboard, `clef-flash` picked "Analyses" at 0.90 for "Open Analyses > Reports", but answered `none` (0.12) for "Navigate to the reports list page": the reports live behind a section link no label names. The agent holds that product knowledge, so the `goal` parameter's description asks it to name the menu path; a group where `none` wins is not overruled by its best option, because a wrong click is worse than `cannot_reach`.
- **Depth**: on OpenCTI's settings (5–9 steps from the dashboard), single-destination trips of 5 steps reached 3 of 5, each in the minimum number of steps; a goal naming several destinations ("A, then B") failed in one call and succeeded as one call per leg (4 of 5 legs). Two deep trips claimed `done` one or two steps short, at a confidence (0.67) inside the range of correct finishes (0.66–0.76): no `done` floor separates them, so the agent splits trips per destination instead.
- **Rationale**: the APIs allow 255 candidates, but Ollama refuses more than 26 for every decision model, and `tev1:4b` more than 24, neither reported anywhere. A first version asked groups of 24 plus two closing options (26), which `tev1:4b` refused on every page. Questions of at most 16 stay well under both; a decision takes about 15 ms warm, so the extra round costs little.

## R7. Containment of page content (FR-010, US3)

- **Decision**: the snapshot is the `state` of the decision request only.
  - The agent's result has `outcome`, the step count and `truncated` outside the quarantine. Step targets (element names) and the final url and title are inside one `wrap_untrusted(source="host page")` block.
  - The snapshot is cut to 12000 characters before it is sent, with `truncated: true` in the outcome.
  - When the provider knows the decision model's context (`decision_context_window()`: the administrator's per-model window, else Ollama's `num_ctx`), each question carries as much of the page as fits beside its own options, at 2 characters per token (OpenCTI snapshots measured 2.25-2.45 on `clef-flash` and `tev1:4b`), cut at a line. Ollama refuses a question over the context rather than cutting it: `tev1:4b` runs with 2048 tokens, about 5000 characters.
- **Rationale**: a decision model can only pick among options code built, so injected text can at most bias which real element is clicked, and never adds an action. The page writes nothing the agent reads except element names, which are quarantined.

## R8. Budgets (FR-008)

- **Decision**: at most 8 actions, plus the initial snapshot and the agent's optional `path`. Each page wait lasts at most 20 s. A run deadline of 240 s, checked before each step, keeps the handler under `TOOL_EXECUTION_TIMEOUT_SECONDS` (300 s). An ended deadline gives `budget_reached`.
- **Cancellation**: the platform path passes no `cancel_event`, so a widget Stop ends the run at the next page timeout. A real task cancellation (`CancelledError`) clears the pending action under `asyncio.shield`, as `computer_use.py` does.
- **Rationale**: a handler wait does not extend the loop deadline the way the approval pause does, so the tool owns its own bound.

## R9. Approval gate (FR-011)

- **Decision**: no whitelist entry. The tool is gated by default like every non-getter built-in tool (`hitl_whitelist_default.py`), so an agent with approvals on asks once per `act_on_page` call.
- **Rationale**: this follows Q1 = A. Each call can click, and the user approves it with its goal visible. A test pins the tool outside `SHIPPED_DEFAULT_WHITELIST`.

## R10. Settings surface

- **Decision**: the pair is added everywhere `routing_model` / `routing_provider_id` are handled, except the env seeding (FR-004):
  - GA config schemas, and `_GA_PROVIDER_REFERENCE_FIELDS` / `_GA_PROVIDER_ID_FIELDS`
  - on write, the provider id is required with the model and must name a config whose provider class declares a `decisions_api` (no network call)
  - `GET /settings/available-decision-models` through `_models_for_capability(db, "decision", user)`
  - `_cleanup_orphaned_model_references`
  - `DeletionImpact` and `reference_route`
  - frontend types and `provider-deletion.ts`
- **Frontend**: the routing section of `Settings.tsx` is extracted into one `PlatformModelSetting` component, used by both Routing (chat models, "Platform default") and Decision (decision models, "Not configured").
- **Rationale**: principle VIII. A second copy of the 100-line section would drift.

## R11. Widget snapshot and actions

- **Decision**:
  - `W/src/utils/pageSnapshot.ts` walks `document.body` and skips `.filigran-chatbot` subtrees (the panel and its portals), `hidden`, `aria-hidden="true"`, `inert` and zero-size elements. It keeps links, buttons, form fields, tabs, menu items, headings and elements with an explicit `role`.
  - Each kept element becomes one line: `[eN] role "name" (state)`. The name comes from `aria-label`, then `aria-labelledby`, `<label>`, `alt`, `placeholder`, `textContent`, in that order and cut to 80 characters. Password values are never read.
  - The output is capped at 20000 characters, and `refs: Map<string, Element>` is kept until the next snapshot.
  - `W/src/utils/pageActions.ts` handles three actions. `click` calls `el.click()`. `type` sets the value through the native setter and dispatches `input`. `navigate` calls the existing `onRelativeLinkClick(path)`; without it the action fails with `navigation_unavailable`.
  - After each action, the widget waits until the DOM has been quiet for 300 ms (at most 3 s), then snapshots.
- **Rationale**: OpenCTI already passes `onRelativeLinkClick` (react-router `navigate`), so there is no change in OpenCTI and no router internals. Same-document mounting means no iframe bridge.
- **Alternatives**:
  - `history.pushState` + a synthetic `popstate` was rejected, because it bypasses the router's history index.
  - `dom-accessibility-api` was rejected, because one hand-written name lookup covers the roles kept, and the package would be the widget's first DOM dependency.
- **Tests**: `happy-dom` as a dev dependency under the existing `node --test` runner.

## R12. The gateway's models in the capability pickers

- **Decision**: a Filigran AI Gateway model is offered by the picker of every type the gateway lists for it, as `specs/031-gateway-model-types/contracts/gateway-model-list.md` states (031, FR-013).
- **Rationale**: the gateway lets one model id carry several types; reading `type` alone showed such a model in one picker.
