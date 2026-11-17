# Implementation Plan: Chat over SSE on the shared Filigran chat panel

**Branch**: `feat/chat-page-panel` | **Date**: 2026-10-21 | **Spec**: [spec.md](spec.md) | **Issue**: XTM-One-Platform/xtm-one#6291

**Input**: Feature specification from `specs/032-chat-sse-shared-panel/spec.md`

## Summary

Retire the chat WebSocket and serve every XTM One chat surface on `@filigran/chatbot`. The turn moves out of the WebSocket handler into a transport-free runner, `run_chat_connection` behind the `ChatConnection` protocol (research R1), served one send per SSE `POST /api/v1/web/chat/messages` whose turn outlives its stream, and followed by a per-conversation SSE event stream built on the existing Redis relay (R6). Control actions are POST routes that reach the turn directly on its replica and through Redis from any other (R5). The frontend has one host module that configures the panel (`frontend/src/components/chat-panel/`), and each surface (`/chat`, Studio rails, agent-detail view, voice overlay, Office add-in) renders it with XTM One's pieces in its slots (`contracts/panel-host-integration.md`). Five package gaps (G1-G5) ship in `@filigran/chatbot` 3.15.0 (FiligranHQ/filigran-ui#455).

## Technical Context

**Language/Version**: Python 3.14 (backend), TypeScript strict / React 19 (frontend, Office add-in)

**Primary Dependencies**: FastAPI + Starlette (a raw ASGI SSE response for the send, `StreamingResponse` for the events stream), Redis (turn lock, journal, pub/sub), `@filigran/chatbot` ≥ 3.15.0 (G1-G5)

**Storage**: PostgreSQL unchanged (no migration); Redis keys in `data-model.md`, two new

**Testing**: pytest (`asyncio_mode=auto`), vitest + `node --test`, Playwright (`tests_e2e/`)

**Target Platform**: Linux server behind a reverse proxy speaking HTTP/2 to browsers (R8); Office add-in in Office's webview

**Project Type**: Web application (backend + frontend + `apps/microsoft-office`) and an external package (`FiligranHQ/filigran-ui`)

**Performance Goals**: First token no later than the WebSocket within 10 % (SC-003); stream text coalesced at 50 ms as today

**Constraints**: Turns outlive their stream (FR-005); any replica can serve any control route (Principle IV); keep-alive ≤ 15 s (R7); at most two streams per panel (R8)

**Scale/Scope**: ~3,400-line WebSocket handler extracted; ~204 backend and ~16 frontend/e2e tests ported; ~10,000 lines of frontend chat UI replaced; 5 surfaces

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design: unchanged.*

| Principle | Status | How |
|---|---|---|
| I. Assignment-driven automation | Pass | Chat turns are interactive, not automation; the agentic loop is untouched. |
| II. Fail-closed | Pass | Control routes answer 503 when Redis is down and the turn is elsewhere, pending approvals when Redis cannot say (R5); a stream whose session ended closes on `session_expired` (R4). |
| III. Security by default | Pass | Every route behind `get_current_user` (confined tokens refused, ToU, account), the web chat's own routes on a web session only (`web_chat_user`); the token leaves the URL (R4); marker args treated as untrusted by the panel. |
| IV. Multi-replica | Pass | Control actions through Redis from any replica, fan-out through the existing pub/sub, turns run in their request's task under the graceful drain (`data-model.md`). |
| V. Migrations | N/A | No schema change. |
| VI. CI gates | Pass | Backend tests ported to `run_chat_connection` and the SSE routes (R2); frontend checks and e2e on the new surfaces. |
| VII. Rationale comments | Pass | On the stream's detachment (`DetachedTurnStreamResponse`), the origin/echo rule and the skip-wait bridge. |
| VIII. Shared utilities | Pass | `services/chat_web_transport.py` is the one SSE framing and keep-alive of both web chat streams (R7), recorded in `dev-docs/reference/backend-shared-utilities.md`; the platform's builders are shared from `platform.py` (R3). |
| IX. TanStack Query | Justified | See Complexity Tracking. |
| X. Design system | Pass | Accent and fonts passed as props; G1 keeps the panel's CSS from restyling the app (R9). |
| XI. Minimal writing | Pass | Artifacts cite each other by id. |
| XII. No workarounds | Pass | Every capability gap goes into the package as a generic opt-in (R10); no XTM One logic in the package. |

## Project Structure

### Documentation (this feature)

```text
specs/032-chat-sse-shared-panel/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── web-chat-api.md
│   └── panel-host-integration.md
└── tasks.md             # /speckit-tasks
```

### Source Code (repository root)

```text
backend/app/
├── services/chat_web_transport.py       # new: SSE framing and keep-alive, HeadlessTurnConnection, DetachedTurnStreamResponse, same-replica registry (R1, R7)
├── services/chat_turn_guard.py          # + skip-wait and kept-Stop keys (R5)
├── services/chat_turn_stream.py         # resync skips the stream's own origin (R6)
├── api/v1/chat_web_stream.py            # new: /api/v1/web/chat/* (contracts/web-chat-api.md)
├── api/v1/chat.py                       # WebSocket removed; run_chat_connection behind ChatConnection, persist_web_steer, read_conversation_turn_state
├── api/v1/platform.py                   # restore_chat_session_window, restored_message_entries, apply_approval_decisions shared; contract unchanged
├── api/v1/agent_chats.py                # + read-only session and messages-page routes
├── api/v1/helpers.py                    # owned_conversation
└── main.py                              # router registration, admission-limiter exemption of the two streams
backend/tests/                           # WS tests ported to run_chat_connection; test_chat_web_sse_transport.py, test_chat_web_routes.py, test_chat_web_sse_fanout.py, test_chat_web_agents.py, test_chat_panel_contracts.py, test_agent_chats_panel_session.py, test_chat_ws_removed.py

frontend/src/
├── components/chat-panel/               # new: XtmChatPanel, LazyXtmChatPanel, endpoints, panel-host, markers, FileChips, MessageParts, transcript-footer, host-send
├── pages/ChatPanelPage.tsx              # /chat, the panel host (replaces pages/Chat.tsx)
├── pages/chat-panel/                    # new: the page's slots (ComposerParts, ConversationBanner), useTurnFrames, useSessionTools
├── components/studio/StudioRail.tsx     # panel host
├── pages/AgentChatConversation.tsx      # read-only panel
├── hooks/api/useChatApi.ts              # useChatWebSocket removed; REST hooks stay
└── pages/chat/, components/chat/        # deletions per contracts/panel-host-integration.md; stream-frames.contract.json
frontend/tests_e2e/                      # routeWebSocket specs → SSE route mocks; chat-resume, chat-voice-call, presentation-studio-rail
frontend/tests_unit/office-addin-*.test.ts

apps/microsoft-office/src/               # OfficeChatPanel, lib/panelConfig.ts, lib/sends.ts, lib/turns.ts; ChatView, Markdown, turn.ts removed

docs/docs/usage/chat-api.md              # SSE protocol (R13)
docs/docs/deployment/installation.md     # HTTP/2 + SSE proxy requirements (R8)
dev-docs/chat-panel.md, dev-docs/chat-turn-fanout.md, dev-docs/reference/api-routes.md, frontend/AGENTS.md, UPGRADING.md
```

**Structure Decision**: Existing web-application layout; the package work happens in `FiligranHQ/filigran-ui` and lands here as a version bump.

## Delivery order

1. **Package**: #455 (G1-G5) merged and released as `@filigran/chatbot` 3.15.0, then pinned here (T006).
2. **Runner extraction** (R2): the send path behind `ChatConnection` (`run_chat_connection`), the WebSocket tests ported to it.
3. **SSE routes**: `chat_web_stream.py`, `chat_web_transport.py`, the skip-wait and kept-Stop keys, the read-only session routes.
4. **Surfaces**: host module, then `/chat`, Studio rails, agent-detail view, voice, Office add-in.
5. **Removal and docs**: WebSocket route and client code, docs in the tree above.

Steps 2 to 5 land together in one XTM One pull request (#6208, branch `feat/chat-page-panel`), so no release ever ships a switched surface beside the WebSocket (FR-002, FR-015). It merges once step 1 is done. Every PR title ends with `(#6291)`. The browser extension needs no release first: it never used the WebSocket (R13, T007).

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| IX: the panel keeps conversations and messages in its own state, not TanStack Query | It is a shared package used by four products; its state is internal to it | Driving the panel from XTM One's TanStack cache needs a controlled-data API in the package, which would put XTM One's state model into the other hosts' panel. XTM One's own data around the panel (tools, quota, shares, workspaces) stays on TanStack and is invalidated from `onStreamEvent`. |
