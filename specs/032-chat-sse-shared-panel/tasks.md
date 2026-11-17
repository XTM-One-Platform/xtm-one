---

description: "Task list for chat over SSE on the shared Filigran chat panel"
---

# Tasks: Chat over SSE on the shared Filigran chat panel

**Input**: Design documents from `specs/032-chat-sse-shared-panel/`

**Issue**: XTM-One-Platform/xtm-one#6291 (every PR title ends with `(#6291)`)

**Prerequisites**: plan.md, spec.md, research.md (R1-R14), data-model.md, contracts/web-chat-api.md, contracts/panel-host-integration.md (G1-G5)

**Tests**: Backend changes carry tests (Principle VI). Frontend surfaces carry vitest and Playwright coverage where the chat already had it.

**Organization**: By user story. Code is named by module and symbol.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: User story from spec.md (US1, US2, US3)

---

## Phase 1: Setup (package prerequisites)

**Purpose**: The `@filigran/chatbot` release every surface needs (research R9, R10). Work in `FiligranHQ/filigran-ui`, on #455.

- [X] T001 [P] G1: isolate the package stylesheet from the host in `packages/filigran-chatbot/postcss/isolate-from-host.js` (run by `postcss/pipeline.js` from `rollup.config.js`; FiligranHQ/filigran-ui): every utility scoped to `.filigran-chatbot` and its descendants, the theme variables on the panel root instead of `:root, :host`; `postcss/isolate-from-host.test.js`, and `postcss/stylesheet.test.js` compiling the stylesheet as the build does and failing on any rule outside the panel or on the host document (#3927)
- [X] T002 [P] G2: `hideConversationList` hides the conversation list in inline mode (`packages/filigran-chatbot/src/types.ts`, `src/components/ChatPanel.tsx`; FiligranHQ/filigran-ui); the thread, header and composer stay usable at 320 px wide; component test in `src/components/ChatPanelSlots.test.tsx`
- [X] T003 [P] G3: voice-call mode in `packages/filigran-chatbot/src/components/ChatVoiceCall.tsx`, `src/hooks/useVoiceCall.ts`, `src/hooks/voiceCallController.ts` and `src/hooks/voiceTurn.ts` (FiligranHQ/filigran-ui), started from the handle (`startVoiceCall()`, `voiceCallSupported()`): browser speech recognition, send after 1 s of silence (`VOICE_SILENCE_MS`), speak via `apiEndpoints.tts` in 200-character sentence chunks with `speechSynthesis` fallback, barge-in, cancel through `stop()`; speak only the final `done` of the panel's own turn (or the turn a `folded_into_active_turn` named) whose `answered_by` is the addressed agent, show others as text with their agent name and "Did not answer" on `mention_failed`; idle probe after 120 s (`VOICE_ANSWER_IDLE_MS`). Rules ported from XTM One's `voice-turn.ts`; tests `ChatPanelVoiceCall.test.tsx`, `voiceTurn.test.ts`, `voiceCallController.test.ts`
- [X] T004 [P] G4: `onBeforeSend` returns `{ text, displayText }` and `handle.send(text, { displayText })` (`packages/filigran-chatbot/src/types.ts`, `src/hooks/useChat.ts`; FiligranHQ/filigran-ui): the request body carries `text`, the user bubble shows `displayText`; test in `src/hooks/requestBody.test.ts`
- [X] T005 [P] G5: close the event stream once the page has been hidden for 60 s (`EVENTS_HIDDEN_CLOSE_MS`) and reopen it on `visibilitychange` to visible, resyncing through the event stream's snapshot, in `packages/filigran-chatbot/src/hooks/useChat.ts` (FiligranHQ/filigran-ui); test in `src/components/ChatPanelContracts.test.tsx`
- [X] T006 Release `@filigran/chatbot` 3.15.0 with #455 (T001-T005), then pin it in `frontend/package.json` + `frontend/yarn.lock` and `apps/microsoft-office/package.json` + `apps/microsoft-office/package-lock.json`
- [X] T007 [P] Check the browser extension (`FiligranHQ/xtm-browser-extension`) for the chat WebSocket before this feature's release (R13): it never opened it, so no issue is needed. It calls `POST /api/v1/extension/execute-task`, `GET /api/v1/auth/me` and `GET /api/v1/platform/config` with an `fcp-` key, all kept; checked on its `main` (e12efb3) and its branches. `plan.md` "Delivery order" says so

---

## Phase 2: Foundational (turn runner and SSE plumbing)

**Purpose**: The turn runner behind `ChatConnection` (R1, R2), the SSE transport (R7), the skip-wait key (R5), the shared builders (R3) and the router. Blocks every story.

**⚠️ CRITICAL**: T012-T016 are one extraction; land them together with the existing WebSocket tests green.

- [X] T008 [P] SSE framing and keep-alive in `backend/app/services/chat_web_transport.py`: `sse_data()` (`data: <json>\n\n`), `SSE_KEEPALIVE` (`: keepalive\n\n`), `SSE_KEEPALIVE_SECONDS` (15 s) and `keepalive_delay()` (a keep-alive at most 15 s after the last write, both streams), `SSE_HEADERS` (`text/event-stream; charset=utf-8`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`); `recheck_session` every `SESSION_RECHECK_SECONDS` in `run_chat_connection` and the events stream, ending with `{"type":"session_expired","reason":<lifetime|idle>}` (`HeadlessTurnConnection.end_session`); frames redacted where the runner sends them (`redact_frame(display=True)`); tests in `backend/tests/test_chat_web_routes.py` (keep-alive timing) and `backend/tests/test_chat_web_sse_transport.py` (session end)
- [X] T009 [P] `web_chat_user` in `backend/app/api/v1/chat_web_stream.py`: the caller `get_current_user` authenticated, refused with 403 unless it holds a web session (`auth_credential_kind == "session_jwt"`); its `web_session` is the one the streams recheck
- [X] T010 [P] `skip_wait_request_key` (`chat:skip_wait:{id}`), `request_skip_wait(conversation_id) -> bool` (addressed to the running turn's token, `SKIP_WAIT_REQUEST_TTL_SECONDS`) and `consume_skip_wait(conversation_id, token) -> bool` (GETDEL) in `backend/app/services/chat_turn_guard.py`; Redis unavailable raises, never returns False silently (Principle II); test `test_a_skip_wait_request_names_the_turn_it_is_for` in `backend/tests/test_chat_web_sse_transport.py`
- [X] T011 [P] Share the platform chat builders with the web routes from `backend/app/api/v1/platform.py`: `restore_chat_session_window`, `restored_message_entries`, `apply_approval_decisions`; the web router reuses the platform and chat handlers for quota, prompts, suggestions, conversation references, artifacts and workspaces (`add_api_route`); the platform routes keep their contract (FR-014)
- [X] T012 Create `backend/app/services/chat_web_transport.py`: `HeadlessTurnConnection` (the `ChatConnection` that serves one send: inbox, `inject()`, `steer()`, `refuse()`, `end_session()`, `client_present()`), the per-process registry (`register` / `unregister` / `live_connection`, entered on the turn's own `turn_started`), and `DetachedTurnStreamResponse` (raw ASGI: a disconnect only detaches, the producer runs in the request task); the module docstring states why the turn outlives its stream (R1). Redaction, `turn_id` stamping, the `TurnPublisher` offer and `_StreamCoalescer` batching stay in `run_chat_connection`'s `_safe_send_json`
- [X] T013 Move the WebSocket's pre-turn send path (empty check, rate limit, `recheck_session`, account recheck, owner conversation lookup, `web_send_refusal`, `refused_chat_selection`, pack options, file resolution, `resolve_referenced_conversations`, `hold_off_channel_turns`, the user message stored, `publish_user_message`) into `run_chat_connection` (`backend/app/api/v1/chat.py`), reading the send frame `WebChatSendRequest.frame()` builds (contracts/web-chat-api.md "Host extras", plus `content`, `referenced_conversation_ids`, `supports_tool_approval`); a refusal goes through `_refuse_send` → `HeadlessTurnConnection.refuse()` with its HTTP status, `detail` and `Retry-After` (R11)
- [X] T014 The turn body in `run_chat_connection`, run in the request's task by `DetachedTurnStreamResponse` and opening its own sessions with `async_session` (the request's released by `release_db_connection_before_loop`): `acquire_or_fold` (fold → `folded_into_active_turn`, then the turn relayed to its end), `turn_started`, `TurnPublisher` with `origin`, the steering-inbox bridge consuming `consume_turn_cancel`, `consume_skip_wait` and inbox steers (with their files, ending an announced wait as a direct steer does), the run branches with the fold-in loop and mention dispatch, terminals, the title, teardown; `worker_shutdown.turn_started` / `turn_ended` bracket it (`chat_turn_guard`); the approval wait reads `client_present()` and `mark_client_seen` freshness
- [X] T015 Control in the routes of `backend/app/api/v1/chat_web_stream.py`: steer (`chat.persist_web_steer`, then `live_connection().steer()` on this replica, else `chat_turn_guard.fold_into_inflight_turn`; `withdraw_web_steer` when no turn takes it), approve (`platform.apply_approval_decisions`, through Redis), cancel (`inject({"type": "cancel"})` on this replica, else `chat.stop_conversation_work` + `request_turn_cancel`), skip-wait (`inject({"type": "skip_wait"})`, else `request_skip_wait`); Redis down with the turn elsewhere → 503
- [X] T016 Replace `chat_websocket` (`backend/app/api/v1/chat.py`) by the SSE transport on T012-T015: the route and its helpers removed (`15f5c16f6`) rather than rewired; the WebSocket-driven suite passes on `run_chat_connection` (T018-T022)
- [X] T017 Keep running web turns under the graceful drain: they run in the request task, so uvicorn's drain cancels them and `_store_cut_turn` (`chat.py`) stores the cut; `worker_shutdown.turn_started` / `turn_ended` in `chat_turn_guard.py` let the drain and the recycle wait see them; `backend/tests/test_chat_shutdown_cut_turn.py` covers an SSE turn
- [X] T018 Turn test harness in `backend/tests/test_chat_turn_steering.py`: `_FakeConnection` (a `HeadlessTurnConnection`) and `_run_connection` drive `run_chat_connection` with the model stubbed and collect its frames
- [X] T019 [P] Port turn-core tests from `chat_websocket(fake_ws)` to the harness: `backend/tests/test_chat_turn_singleflight.py`, `test_segment_delivery.py`, `test_chat_stream_coalescing.py`, `test_chat_turn_interruption.py`, `test_midloop_connection_release.py`, `test_request_too_large_non_failoverable.py`, `test_malformed_conversation_id.py`
- [X] T020 [P] Port control tests to the harness: `backend/tests/test_chat_turn_steering.py`, `test_chat_approval_gate.py`, `test_chat_approval_resume.py`, `test_chat_tool_selection_scope.py`, `test_rate_limit_expansion.py`
- [X] T021 [P] Port feature tests to the harness: `backend/tests/test_deep_investigation_turn_route.py`, `test_mention_turn.py`, `test_mention_dispatch.py`, `test_conversation_reference_turns.py`, `test_investigation_pack_options.py`, `test_parent_wake.py`, `test_channel_continuation.py`, `test_runner_presence_postgres.py`
- [X] T022 [P] Port relay tests to the harness with `origin` from the turn: `backend/tests/test_chat_relay_reliability.py`, `test_chat_turn_fanout.py`, `test_relay_frames.py`
- [X] T023 Create `backend/app/api/v1/chat_web_stream.py` (`APIRouter`; every route on `web_chat_user`, `require_chat_enabled` on the routes that start or feed a turn; `_client_id` reading `X-Chat-Client-Id`) and register it at `/api/v1/web` (`WEB_CHAT_PREFIX`) in `backend/app/main.py`

**Checkpoint**: The send runs on the extracted runner; the router exists; ported tests green.

---

## Phase 3: User Story 1 - Chat on the shared panel with nothing lost (Priority: P1) 🎯 MVP

**Goal**: `/chat` renders the panel over SSE with every capability of spec US1.

**Independent Test**: Quickstart scenarios 1, 2 and 8; the ported `/chat` e2e specs pass.

### Tests for User Story 1

- [X] T024 [P] [US1] Route tests for `POST /api/v1/web/chat/messages` in `backend/tests/test_chat_web_sse_transport.py` and `backend/tests/test_chat_web_routes.py`: frames stream to the final `done`; refusals answer an HTTP error with `detail` and 429 + `Retry-After` (R11), a failure once the turn runs stays a frame; a fold relays the named turn to its end; a client that goes away leaves the turn running and storing its reply; chat disabled → 403 `chat_unavailable`; ToU in `backend/tests/test_terms_of_use_gate.py`, account and session in `test_deactivated_account_auth.py` and `test_session_timeout.py`
- [X] T025 [P] [US1] Shape tests for the conversation routes in `backend/tests/test_chat_web_routes.py` and `backend/tests/test_chat_web_sse_transport.py` against the panel README contract: sessions create/restore (with `pending_approvals`, `is_processing`, `interrupted_turn`, `has_more`, a conversation shared with the caller), list, rename, archive one and all, messages page (unknown cursor 404), upload (multipart → `{file_ids}`, 413 over the size limit, another user's conversation 404, no file 422); only the routes that start or feed a turn answer 403 `chat_unavailable`
- [X] T026 [P] [US1] Tests for steer and approve in `backend/tests/test_chat_web_routes.py`: steer → `steered` / `stored` / 409 / 429 with `Retry-After` / 503; a steer on the turn's own replica reaches it without Redis or a poll; a steer handed over that the bridge never read is answered by a follow-up; approve → `accepted` / 400 / 404 / 409 (recorded) / 422 for an unknown verdict

### Implementation for User Story 1

- [X] T027 [US1] `POST /chat/messages` in `backend/app/api/v1/chat_web_stream.py` (`send_web_chat_message`): the owner check, `ChatConnectionAuth` with `origin` from `X-Chat-Client-Id`, the request's database connection released, `run_chat_connection` over a `HeadlessTurnConnection` served by `DetachedTurnStreamResponse` (a refusal answers its HTTP error, a fold relays the turn to its end, a disconnect only detaches)
- [X] T028 [US1] The conversation routes in `backend/app/api/v1/chat_web_stream.py` per contracts/web-chat-api.md: agents (`list_web_chat_agents`), sessions (`POST` create/restore, `GET` list, `PATCH`, `DELETE` one, `DELETE` all), messages page (`read_message_page`), `POST /chat/upload` (`chat.upload_file_to_conversation(create_message=False)`, answering `{file_ids:[id]}`), and prompts, suggestions, quota, workspaces, conversation references and artifacts on their existing handlers; tts and file download stay on `/api/v1/chat/tts` and `/api/v1/chat/files/{id}/download`
- [X] T029 [US1] `POST /chat/messages/steer`, `POST /chat/messages/approve` and `GET /chat/conversations/{id}/pending-approvals` in `backend/app/api/v1/chat_web_stream.py` on T015
- [X] T030 [P] [US1] Move `FileChip` and `WorkingFileChip` out of `frontend/src/pages/chat/ChatMessages.tsx` into `frontend/src/components/chat-panel/FileChips.tsx`, and `InlineFileContent` into `components/chat-panel/InlineFileContent.tsx`; `frontend/src/pages/RunDetail.tsx` imports them
- [X] T031 [P] [US1] `frontend/src/components/chat-panel/markers.tsx`: `useMarkerRenderers` for `FILE` (FileChips + file revisions), `PRESENTATION`, `REPORT`, `VIDEO`, `WORK_ITEM`, `INVESTIGATION` (`markerCard`, each in `MarkerCardBoundary`) and `CHART`, `useTranscriptCards`; `transformMessageText` through `messageTextForPage` (`message-text.ts`) resolving `[[SECRET_VARIABLE:…]]` (`secretMarkers.ts`); vitest in `markers.test.tsx`, `message-text.test.ts`
- [X] T032 [P] [US1] `frontend/src/pages/chat-panel/useTurnFrames.ts`, the `onStreamEvent` + `onTurnRunningChange` handler: `tool_done` query invalidations, `tool_progress` → `handleToolProgress` (consult-wrapped frames included), runner activity and runner approval events, `agentic_action` → turn charges, wait items, the stored end refetching the conversation, and `working` from the panel's `onTurnRunningChange` for the arcade wait source; the session end is the panel's `onSessionEnded` (T033); vitest in `useTurnFrames.test.tsx`
- [X] T033 [US1] `frontend/src/components/chat-panel/XtmChatPanel.tsx` per contracts/panel-host-integration.md "Shared host module": `apiBaseUrl=API_BASE`, `backendType="rest"`, `mode="inline"`, `apiEndpoints=CHAT_PANEL_ENDPOINTS` (`endpoints.ts`), `requestHeaders` with the session token, `t` + `locale` from the app i18n returning the key unchanged when missing, `accentColor` / `fontFamily` from the theme (`panel-host.ts`), T031, `renderMessageExtras` / `messageFooterActions` (`MessageParts.tsx`: `MessageFeedbackButtons`, `apiEndpoints.feedback` unset), `renderWaitingPanel` (`ChatWaitingGame`) on `useArcadeWaitSignal` fed by `working`, `onSessionEnded` → `endSession()`; a 403 `chat_unavailable` shows the panel's own refusal (no host handling)
- [X] T034 [P] [US1] The composer's controls in `frontend/src/pages/chat-panel/ComposerParts.tsx` (`ComposerToolbar`: session tool picker toggle, Deep Investigation, `QuotaIndicator`, voice-call button; `ComposerHeader`: `SessionToolPicker` from `pages/chat/SessionToolPicker.tsx`, `RunnerApprovalCard`, `DesktopMachineBar`), the tool selection in `pages/chat-panel/useSessionTools.ts` (`requestFields`, the extras of contracts/web-chat-api.md); context usage is the panel's own gauge (`contextUsageEnabled`)
- [X] T035 [P] [US1] The conversation menu of `frontend/src/pages/ChatPanelPage.tsx` (`OverflowMenu`): files panel (`ConversationFilesPanel`), share (`ShareConversationDialog`), continue in channel (`ContinueInChannelDialog`), export, file in workspace (`FileConversationDialog`)
- [X] T036 [US1] `frontend/src/pages/ChatPanelPage.tsx` on `XtmChatPanel` (replacing `pages/Chat.tsx`): `conversationId` ↔ `?c=`, `initialAgent` from `?agent=` and a later `?agent=` through `newConversation`, `?investigate=&pack=` through the handle and `SourcePackPicker` from `onBeforeSend`, a primed draft / autosend via `setDraft` / `useHostSender`; slots: `composerToolbar` and `composerHeader` (T034), `getRequestExtras` (T034 + `desktop_runner_id`), `banner` (`ConversationBanner`), `transcriptFooter` (live `RunnerActivityBlock` and `InvestigationBlock`, `WaitItemList`, `InterruptedTurnNotice`), `headerActions` (T035), `sidebarSections` (shared conversations), `agentMentions`, `readOnly` for read-only shares; the away notice is the panel's (`notifyOnComplete`, `onTaskComplete`)
- [X] T037 [US1] The chat page object `frontend/tests_e2e/model/chat.page.ts` on the panel's `filigran-chat-*` test ids; `frontend/tests_e2e/chat-scroll-reasoning.spec.ts` and `frontend/tests_e2e/waiting-panel-contrast.spec.ts` on `page.route` SSE mocks of `/api/v1/web/chat/messages`; `frontend/tests_e2e/chat-turn.spec.ts` against the real backend

**Checkpoint**: `/chat` works on the panel.

---

## Phase 4: User Story 2 - A turn survives its connection (Priority: P1)

**Goal**: Reloads, second tabs and network cuts catch up with a running turn; Stop and Continue now act server-side from any replica.

**Independent Test**: Quickstart scenarios 3-7.

### Tests for User Story 2

- [X] T038 [P] [US2] Events stream tests in `backend/tests/test_chat_web_sse_fanout.py` and `backend/tests/test_chat_web_sse_transport.py`: a tab attaching mid-turn is resumed into it; other tabs see the turn, the tab whose id started it does not, nor when its own events stream re-attaches mid-turn; the client id read from the header too; keep-alive on the same rule as the message stream (`test_chat_web_routes.py`)
- [X] T039 [P] [US2] Tests in `backend/tests/test_chat_web_cancel_skip.py` (`integration` marker, real Redis): cancel and skip-wait requested with no live connection on this replica (`live_connection` is `None`: another replica) are consumed by the running turn's bridge within 2 s; Redis down → 503

### Implementation for User Story 2

- [X] T040 [US2] `GET /chat/conversations/{id}/events?client_id=` in `backend/app/api/v1/chat_web_stream.py` (`stream_web_chat_events`): owner check (`owned_conversation`, `backend/app/api/v1/helpers.py`), `relay_turn_frames` with a queue-backed `send` and `origin=client_id`, keep-alive by `keepalive_delay`, `recheck_session` every 15 s, `mark_client_seen` every `CLIENT_SEEN_REFRESH_SECONDS`, ending after `EVENTS_MAX_LIFETIME_SECONDS`
- [X] T041 [US2] `POST /chat/messages/cancel` and `POST /chat/messages/skip-wait` in `backend/app/api/v1/chat_web_stream.py` (202 with `turn` `local` / `remote` / `none`; 503 when Redis is down and the turn elsewhere; `awaiting_send` keeps a Stop for a send on its way)
- [X] T042 [US2] Name `cancel`, `skipWait` and `events` in `CHAT_PANEL_ENDPOINTS` (`frontend/src/components/chat-panel/endpoints.ts`), which `XtmChatPanel` passes
- [X] T043 [P] [US2] `frontend/tests_e2e/chat-resume.spec.ts` with the scripted model (`tests_e2e/scripted-llm`): reload mid-turn, second tab on the same conversation, Stop from the second tab, a message sent while the event stream is reconnecting shown once and answered once, and a reload while a turn waits on a tool approval brings the approval prompt back and the decision resumes the turn; passed on the dev stack
- [X] T044 [P] [US2] Port the raw-socket cancels in `frontend/tests_e2e/a2a/06-wait-cancel.spec.ts` and `frontend/tests_e2e/a2a/08-families.spec.ts` to `POST /api/v1/web/chat/messages/cancel`
- [X] T045 [US2] Measure SC-003 against the release before this feature: first-token latency over the WebSocket there and over `POST /api/v1/web/chat/messages` here, same agent and scripted model (`tests_e2e/scripted-llm`), median of 20 runs each; record the comparison in `specs/032-chat-sse-shared-panel/quickstart.md` "Results", which holds the SSE half (median 35 ms)

**Checkpoint**: US1 and US2 complete; `/chat` is at parity.

---

## Phase 5: User Story 3 - Every other chat surface on the panel (Priority: P2)

**Goal**: Studio rails, the agent-detail view, voice calls and the Office add-in render the panel.

**Independent Test**: Quickstart scenarios 9-12.

### Tests for User Story 3

- [X] T046 [P] [US3] Tests in `backend/tests/test_chat_web_agents.py` for `GET /chat/agents?studio=presentation|video`: only the chat agents carrying the studio's tool, the General Assistant when its tools do and no agent stands in for it; a studio session created bound to its subject
- [X] T047 [P] [US3] Tests in `backend/tests/test_agent_chats_panel_session.py`: the read-only session and messages-page routes answer in panel shape for a reader `GET /agents/{id}/chats/{cid}` admits and 403/404 for one it refuses; a restore without a conversation never starts one

### Implementation for User Story 3

- [X] T048 [US3] The `studio` filter on the agent list, `list_web_chat_agents(studio=)` in `backend/app/api/v1/chat_web_stream.py` (`STUDIO_PICKER_TOOL`: the chat agents that carry the subject's tool, plus the General Assistant when its tools do); a studio session is bound by `POST /web/chat/sessions?presentation_id=|video_project_id=`
- [X] T049 [US3] `POST /{agent_id}/chats/sessions` and `GET /{agent_id}/chats/conversations/{conversation_id}/messages` in `backend/app/api/v1/agent_chats.py`, on the access check of `GET /{agent_id}/chats/{conversation_id}` (`_viewable_agent_conversation`), `platform.restore_chat_session_window` and `chat_web_stream.read_message_page`
- [X] T050 [US3] `frontend/src/components/studio/StudioRail.tsx` on `LazyXtmChatPanel` with G2: `studioPanelEndpoints(kind, subjectId)` (agents `?studio=`, a new conversation bound by the session `POST`), origin-conversation offer kept, `getRequestExtras` → `subject.sendContext` (`studio_slide` / `studio_scene`), `banner` → `AwaitedWorkBanner`, `headerActions` → the subject's conversations and "Open in chat", `?c=` fallback toast kept; `subject.onToolProgress` fed through `useTurnFrames`; `XtmChatPanel.studioCards`; `frontend/src/components/presentation/PresentationStudioRail.tsx` and `frontend/src/components/video-studio/VideoStudioRail.tsx` build the subject
- [X] T051 [US3] Replace `frontend/tests_e2e/presentation-studio-attachments.spec.ts` (`page.routeWebSocket`) with `frontend/tests_e2e/presentation-studio-rail.spec.ts` on SSE route mocks
- [X] T052 [US3] `frontend/src/pages/AgentChatConversation.tsx` as a read-only `XtmChatPanel` (`readOnly`, G2, `apiEndpoints=agentChatPanelEndpoints(agentId)`: sessions / messagesPage → T049 routes, no events); route not prefetched
- [X] T053 [US3] Replace the `VoiceConversation` overlay with the panel's voice-call mode (G3), started by `ChatPanelHandle.startVoiceCall()` from `ComposerToolbar` (`frontend/src/pages/chat-panel/ComposerParts.tsx`) when `voiceCallSupported()`
- [X] T054 [US3] `apps/microsoft-office/src/lib/panelConfig.ts` (`OFFICE_PANEL_ENDPOINTS`, `panelApiBaseUrl` = `${origin}/api/v1`, the same `/web/chat/…` routes as `CHAT_PANEL_ENDPOINTS`, `OFFICE_PANEL_STORAGE_PREFIX`, `OFFICE_PANEL_MAX_UPLOAD_BYTES`) and `apps/microsoft-office/src/components/OfficeChatPanel.tsx` rendering the panel (inline, G2) after `LoginScreen` with that config: `requestHeaders` from the stored token, `onBeforeSend` → `buildDocumentPrompt` on the context `useOffice()` holds with `displayText` = what the user typed (G4), a whole-document Word question taken over and re-sent through the handle (`lib/sends.ts`), quick actions from `getAvailableActions` in `welcome` and the composer toolbar sent through the handle, Word's `comment_full` one section per turn (`lib/turns.ts`), `renderMessageExtras` → Replace (`ApplyPreview`) / Insert / Comment / Discard on any answer carrying an `action` block, stored or live, never while it streams (`lib/actions.ts`, `applyAgentAction`), `transformMessageText` stripping that block; mounted in `apps/microsoft-office/src/components/App.tsx`, the panel CSS imported
- [X] T055 [US3] Check the `/office` static mount's CSP in `backend/app/main.py` allows the panel (same-origin fetch streams, `data:` images, its fonts): the base policy already does; pinned by `backend/tests/test_office_addin_csp.py`
- [X] T056 [P] [US3] Node tests for the action-block parsing in `frontend/tests_unit/office-addin-actions.test.ts` (`apps/microsoft-office/src/lib/actions.ts`)
- [X] T057 [P] [US3] Node tests in `frontend/tests_unit/office-addin-panel-config.test.ts`: `panelConfig.ts` names the routes `endpoints.ts` names, and the upload limit
- [X] T058 [P] [US3] `frontend/tests_e2e/chat-voice-call.spec.ts`: with `SpeechRecognition` and `/api/v1/chat/tts` mocked, start a voice call from `/chat`, a recognised phrase is sent, the answer is requested from tts, speaking over it stops playback; passed on the dev stack

**Checkpoint**: Every surface on the panel; nothing opens the WebSocket.

---

## Phase 6: Polish & Cross-Cutting (removal, docs, validation)

**Purpose**: FR-002, FR-018, FR-019, SC-001, SC-003, SC-004. Ships in the same release as Phase 5 (FR-015).

- [X] T059 Delete the chat WebSocket from `backend/app/api/v1/chat.py` (`chat_websocket` and its helpers: `_accept_websocket`, `_refuse_ended_session`, `resolve_account_ws_rejection`, `resolve_terms_ws_rejection`), and `WS_SESSION_ENDED_CLOSE_CODE` / `ws_close_reason` from `backend/app/core/sessions.py`; delete WebSocket-only tests (`test_chat_websocket_accept.py`, every `chat_websocket(` caller), the assertions that still apply (session timeout, deactivated account, ToU, known secrets) kept on the web routes (`test_session_timeout.py`, `test_deactivated_account_auth.py`, `test_terms_of_use_gate.py`, `test_chat_web_sse_transport.py`)
- [X] T060 `backend/tests/test_chat_ws_removed.py`: `/api/v1/chat/conversations/{id}/ws` answers 404 (SC-001)
- [X] T061 [P] Delete from the frontend what contracts/panel-host-integration.md "Deleted" lists: `useChatWebSocket` / `useChatStream` and its frame code in `frontend/src/hooks/api/useChatApi.ts` (REST hooks stay) and their tests, `hooks/api/chatStream.ts`, `pages/Chat.tsx`, `pages/chat/ChatMessages.tsx`, `ChatComposer.tsx`, `ChatDropOverlay.tsx`, `ContextUsageIndicator.tsx`, `components/chat/VoiceConversation.tsx`, `voice-turn.ts` + test, `lib/turn-end.ts` / `turn-ack.ts`, and the orphans the removal left (`MentionMenu`, `ConversationRefChips`, `useChatAttachments`, `useMentionMenu`, `useReaderScrollUp`, `transcript-follow`, `context-usage`); `ConversationSidebar.tsx` and `WorkspaceGroups.tsx` stay (`dev-docs/chat-panel.md`)
- [X] T062 [P] Delete from the Office add-in: the hand-written SSE client in `apps/microsoft-office/src/lib/api.ts` (client id, `streamMessage`, `followConversation`), `createWebSocket`, `src/components/ChatView.tsx`, `Markdown.tsx`, `AgentPicker.tsx`, `AgentIcon.tsx`, `src/lib/markdown.ts`, `src/lib/turn.ts`, `src/lib/replies.ts`, `src/hooks/useChat.ts`, and their tests
- [X] T063 [P] `docs/docs/usage/chat-api.md` for the SSE API (contracts/web-chat-api.md): authenticate, open a conversation, send and read the stream, the event stream and `X-Chat-Client-Id`, Stop / Continue now / steer / approve and pending approvals, end reasons, `session_expired`
- [X] T064 [P] `docs/docs/deployment/installation.md`: HTTP/2 between browsers and the proxy (R8), no response buffering, the WebSocket upgrade kept only for XTM One Desktop, runners and relays
- [X] T065 [P] `dev-docs/chat-turn-fanout.md` (wire protocol, clients table) and `dev-docs/chat-turn-resilience.md` for `run_chat_connection`, the SSE routes and the client id
- [X] T066 [P] `dev-docs/reference/api-routes.md` (`/api/v1/web`), `dev-docs/reference/backend-shared-utilities.md` (`services/chat_web_transport.py`), `dev-docs/reference/frontend-shared-utilities.md` (`components/chat-panel/`), `backend/app/api/AGENTS.md`, `backend/AGENTS.md`
- [X] T067 [P] `frontend/AGENTS.md`: the package root and stylesheet imported only by `components/chat-panel/XtmChatPanel.tsx` (and `ChatPanelPage.tsx` for `voiceCallSupported`), on lazy, never-prefetched pages or through `LazyXtmChatPanel`; the #3927 warning stays until T006 pins the isolated stylesheet (G1); the WebSocket pitfalls are now the panel host's; `dev-docs/chat-panel.md` says what XTM One keeps and why
- [X] T068 [P] The breaking change in `UPGRADING.md` (WebSocket removed, SSE replacement, HTTP/2 requirement) and `apps/microsoft-office/README.md` (route table, the pane on the shared panel)
- [X] T069 Run SC-004 (10-minute turn, reload at 5 min, 10 s network cut at 7 min) with the scripted model; record the result in `specs/032-chat-sse-shared-panel/quickstart.md` — done: pass: answer at minute 9.84 after a reload at 5 and a 10 s cut at 7, shown once and read back after a reload (quickstart.md "Results")
- [X] T070 Run `specs/032-chat-sse-shared-panel/quickstart.md` end to end and `./scripts/ci-local.sh` (in the background) — done: quickstart scenarios and `ci-local.sh` recorded in quickstart.md "Results"; green but for two failures that reproduce at HEAD on this machine, and the add-in typecheck and e2e image build, which need the released package (T006)

---

## Dependencies & Execution Order

### Phase dependencies

- **Phase 1**: T001-T005 parallel; T006 needs them; T007 independent.
- **Phase 2**: T008-T011 parallel; T012 → T013 → T014 → T015 → T016 → T017; T018 after T016; T019-T022 parallel after T018; T023 after T008, T009.
- **Phase 3 (US1)**: needs Phase 2 and T006. Tests T024-T026 parallel; T027-T029 sequential (same file); frontend T030-T032, T034, T035 parallel; T033 after T031, T032; T036 after T033-T035; T037 after T036.
- **Phase 4 (US2)**: needs US1's T027 and T033. T040 → T041 (same file); T042 after T033; T045 needs a stack of the previous release.
- **Phase 5 (US3)**: needs US1 (host module) and G2-G5; the studio, agent-detail, voice and Office tracks are independent of each other.
- **Phase 6**: T059-T062 need Phases 3-5 complete; docs T063-T068 parallel any time after Phase 4.

### Parallel example: User Story 1

```text
Task: "T024 Route tests for POST /api/v1/web/chat/messages in backend/tests/test_chat_web_sse_transport.py"
Task: "T025 Shape tests for the conversation routes in backend/tests/test_chat_web_routes.py"
Task: "T030 Move FileChip... into frontend/src/components/chat-panel/FileChips.tsx"
Task: "T031 Create frontend/src/components/chat-panel/markers.tsx"
Task: "T032 Create frontend/src/pages/chat-panel/useTurnFrames.ts"
Task: "T034 Create frontend/src/pages/chat-panel/ComposerParts.tsx"
```

## Implementation Strategy

1. Phase 1 package work runs in filigran-ui while Phase 2 runs here.
2. Phases 2 to 6 land in one XTM One pull request (#6208, branch `feat/chat-page-panel`), merged to `main` once T006 pins the released package (plan "Delivery order"), so no release ships a switched surface beside the WebSocket (FR-002, FR-015).

---

## Phase 7: Convergence

- [X] T071 CRITICAL: make `POST /api/v1/web/chat/messages/cancel` and `/messages/skip-wait` fail closed: with the turn not on this replica and Redis erroring, answer 503, and make `GET /chat/conversations/{id}/pending-approvals` answer 503 instead of reporting `idle` (`backend/app/api/v1/chat_web_stream.py`); answer 202 on success as contracts/web-chat-api.md states; tests in `backend/tests/test_chat_web_routes.py` per Constitution II (contradicts)
- [X] T072 CRITICAL: add the backend tests the web chat routes lacked, in `backend/tests/test_chat_web_sse_transport.py` and `backend/tests/test_chat_web_routes.py`: approve (accepted, 409, another user's conversation), pending-approvals, upload (multipart → `{file_ids}`, size limit, another user's conversation), sessions POST / GET / PATCH / DELETE one / DELETE all response shapes, the chat gate on every web route, Redis down on steer / cancel / skip-wait, a steer on the turn's own replica, and one client's events stream re-attaching mid-turn (`test_chat_web_sse_fanout.py`) per Constitution VI (missing)
- [X] T073 CRITICAL: merge FiligranHQ/filigran-ui#455 and publish `@filigran/chatbot` 3.15.0, then bump `frontend/package.json` and `frontend/yarn.lock` from 3.10.0 (which has no `inline` mode, slots, handle or the outliving-turn endpoints the page uses) and run `yarn check` and `yarn test:e2e --project=core` against the published package per FR-010, T006 (missing)
- [X] T074 Tick the tasks PR #6208 satisfies with its own design and name on each the module that does it (`run_chat_connection`, `HeadlessTurnConnection`, `chat_web_stream.py`, `pages/ChatPanelPage.tsx`, `pages/chat-panel/`, `components/chat-panel/`); leave the others open per plan: Delivery order (partial)
- [X] T075 Scope the package's theme variables to `.filigran-chatbot` instead of `:root, :host` (filigran-ui `packages/filigran-chatbot/postcss/isolate-from-host.js` and its test) and fail on any rule outside the panel in the stylesheet compiled as the build compiles it (`postcss/stylesheet.test.js`); until T006 pins 3.15.0, keep the #3927 warning in `frontend/AGENTS.md` accurate about where the stylesheet is imported (`XtmChatPanel.tsx`) per R9, G1, T001 (contradicts)
- [X] T076 Answer the pre-turn refusals of `POST /api/v1/web/chat/messages` with an HTTP error and `detail` before the stream starts instead of `error` frames on a 200 stream: pack options, empty message, rate limit (429 with `Retry-After`), unknown conversation, `web_send_refusal`, refused tool selection, channel hold, account rejection (`run_chat_connection` `_refuse_send`, `HeadlessTurnConnection.refuse`); `Retry-After` on the steer 429 (`SteerRefusedError`); tests per R11, US1/AC1 (contradicts)
- [X] T077 In filigran-ui `packages/filigran-chatbot/src/hooks/useChat.ts` show a refused send's response `detail` instead of "Unable to connect. Please check the configuration." (`src/utils/refusal.ts`), keep the conversation id when a restore answers 401 (`src/components/ChatPanel.tsx`, so `/chat?c=` is not lost), and reopen the event stream when `requestHeaders` change after a refusal; tests in `src/components/ChatPanelContracts.test.tsx` per R11, FR-013, FR-008 (missing)
- [X] T078 Sign out on a session end: the panel's `onSessionEnded(reason)` (a `session_expired` frame, or a 401 whose `detail.code` is `session_expired`) → `endSession()` in `frontend/src/components/chat-panel/XtmChatPanel.tsx` (`namedSessionEndReason`, `lib/session.ts`, tested in `session.test.ts`); one frame name everywhere, `session_expired` (`chat_web_transport.py`, `docs/docs/usage/chat-api.md`, the contracts) per FR-008 (partial)
- [X] T079 Gate with `require_chat_enabled` exactly the web chat routes that start or feed a turn (send, steer, approve, a new conversation from `POST /chat/sessions`, upload) in `backend/app/api/v1/chat_web_stream.py`; reads (a restore included), a session's own `PATCH` / `DELETE`, events, pending approvals, Stop and Continue now stay open with AI off (`cancel_web_chat_turn`, `skip_web_chat_wait` on the allowlist of `backend/tests/test_ai_disabled_read_surface.py`), as contracts/web-chat-api.md records; the 403s carry a `code` (`CodedHTTPException`: `chat_unavailable`, `terms_required`); test `test_only_the_routes_that_start_or_feed_a_turn_answer_chat_unavailable` per FR-007, spec edge case "chat disabled" (partial)
- [X] T080 Filter the resume snapshot by origin in `_resync` (`backend/app/services/chat_turn_stream.py`) so a client whose own events stream reconnects mid-turn (a network blip, or `EVENTS_MAX_LIFETIME_SECONDS`) does not get its own turn replayed; test the same client re-attaching in `backend/tests/test_chat_web_sse_fanout.py` per FR-006 (partial)
- [X] T081 Rebase PR #6208 onto `main` past #6213, keeping `worker_shutdown.turn_started` / `turn_ended` in `backend/app/services/chat_turn_guard.py` and the stop-signal, recycle-watch and grace-deadline hooks in `backend/app/main.py`; the drain and the recycle wait see web turns that run in the request task; `backend/tests/test_chat_shutdown_cut_turn.py` per plan: graceful shutdown, T017 (partial)
- [X] T082 When the turn runs in this process, hand a steer to it directly (`HeadlessTurnConnection.steer`) instead of `fold_into_inflight_turn` and the 1 s bridge poll (`steer_web_chat_message`); `FoldRefusedError` → 409, a Redis error inside the hand-off → `stored` (nothing to resend), a Redis error before anything is stored → 503; tests per R5, US1/AC4 (contradicts)
- [X] T083 Add `pending_approvals`, `is_processing` and `interrupted_turn` to the `POST /api/v1/web/chat/sessions` restore (`WebChatSessionResponse`, from `chat.read_conversation_turn_state`); test per contracts/web-chat-api.md (partial)
- [X] T084 Send the message stream's keep-alive at most 15 s after the last write and drive both streams by one rule (`keepalive_delay`, `DetachedTurnStreamResponse._keepalive` and the events loop) per FR-009, Constitution VIII (partial)
- [X] T085 Update `specs/032-chat-sse-shared-panel/contracts/web-chat-api.md` and `contracts/panel-host-integration.md` to the routes served: `apiBaseUrl=/api/v1` with `/web` in each path, the messages page at `/web/chat/sessions/{id}/messages`, tts and file download on the existing `/api/v1/chat/tts` and `/api/v1/chat/files/{id}/download`, approval presence through `client_present` on the message stream, so contract and code state one thing (partial)
- [X] T086 Configure the panel for every surface in `frontend/src/components/chat-panel/XtmChatPanel.tsx` (`panel-host.ts`: `t` with `locale`, headers, look); wrap the marker cards in `MarkerCardBoundary` (`components/chat-panel/markers.tsx`); build the file URL through the base-path helper (`chatFileDownloadPath`, `endpoints.ts`); move `FileChip` out of `pages/chat/ChatMessages.tsx`; the panel's context gauge reads the web `done` and restore payloads (`contextUsageEnabled`) per FR-011, T030, T031, T033 (partial)
- [X] T087 Vitest coverage for `frontend/src/pages/chat-panel/useTurnFrames.ts` (`tool_done`, `presentation_saved` / `video_saved`, runner approval frames, `agentic_action`), `components/chat-panel/markers.tsx` and `pages/chat-panel/useSessionTools.ts`, and `frontend/tests_e2e/chat-scroll-reasoning.spec.ts` on SSE route mocks per T031, T032, T037 (partial)
- [X] T088 Add the interim SSE ports PR #6208 added to the deletion lists of T061 / T062 and remove them: `frontend/src/hooks/api/chatStream.ts` + `chatStream.test.ts`, the `useChatStream` frame code and its tests, and the Office add-in's hand-written SSE client (`apps/microsoft-office/src/lib/api.ts`, `src/hooks/useChat.ts`) per FR-018, SC-005 (unrequested)
- [X] T089 Review the web chat's backend additions the artifacts did not call for and record each in `specs/032-chat-sse-shared-panel/research.md` (R14) or remove it: the agent picker with the General Assistant, flows and a "Not recommended for chat" group (`list_web_chat_agents`, `tests/test_chat_web_agents.py`) with `?studio=` (T048); restore and paging of conversations shared with the caller; `scope=shared` and channel threads in `GET /sessions`; the web feedback routes (removed); the 15-minute events-stream lifetime (`EVENTS_MAX_LIFETIME_SECONDS`); and the `ConcurrencyLimitMiddleware` exemption for both streams (`is_long_lived_stream`, `backend/app/main.py`) per plan: Constraints (unrequested)
- [X] T090 Close the documentation gaps under T063-T068: `docs/docs/usage/chat-api.md` (approve, pending-approvals, skip-wait, the session routes, the Office add-in); `docs/docs/deployment/installation.md` (HTTP/2); `UPGRADING.md` (the removal entry); `dev-docs/reference/backend-shared-utilities.md` (`chat_web_transport.py`); the stale WebSocket lines of `backend/app/api/AGENTS.md`, `backend/app/services/AGENTS.md`, `backend/AGENTS.md`, `frontend/AGENTS.md` per FR-019 (partial)
- [X] T091 The panel's away notice (`notifyOnComplete`, on by default, and the `onTaskComplete` toast in `XtmChatPanel.tsx`) notifies like the deleted `useAwayCompletionNotice.ts` (a turn ending while the tab is hidden) per T036, US1 (unrequested)
- [X] T092 Open a new conversation for a later `?agent=` equal to the agent the page mounted with instead of ignoring it (`frontend/src/pages/ChatPanelPage.tsx`, `newConversation({ agent })`) per FR-013 (partial)
- [X] T093 Port the conversation export (#6258) to the panel page: "Export as Markdown" and "Export as PDF" in the header menu of `frontend/src/pages/ChatPanelPage.tsx`, through `useExportConversation` and `saveBlobAs`, per FR-011 (missing)
- [X] T094 Show what an announced wait watches (#6169) on the panel page: the `wait_start` / `wait_status` `items` (`readWaitItems`) kept by `frontend/src/pages/chat-panel/useTurnFrames.ts` (`waitItems`) and rendered by `WaitItemList` (`frontend/src/components/chat/WaitItemList.tsx`) in the panel's `transcriptFooter` (`components/chat-panel/transcript-footer.tsx`) while the turn waits, per FR-011 (missing)
- [X] T095 Render inline charts (#6258) on the panel: a `CHART` entry in `markerRenderers` (`frontend/src/components/chat-panel/markers.tsx`) hides the streamed `[[CHART:…]]` marker, and the stored answer's chart fence (`CHART_FENCE_LANGUAGE`, `components/chat/chat-chart.ts`) renders `ChatChart` through the package's `codeBlockRenderers` (`components/chat-panel/chart-code-block.ts`; filigran-ui `packages/filigran-chatbot/src/types.ts`: renderers keyed by fence language, opt-in), per FR-011, FR-012 (missing)
