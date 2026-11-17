# Feature Specification: Chat over SSE on the shared Filigran chat panel

**Feature Branch**: `feat/chat-page-panel`

**Issue**: XTM-One-Platform/xtm-one#6291

**Created**: 2026-10-21

**Status**: Draft

**Input**: User description: "Switch web socket to SSE and use https://github.com/FiligranHQ/filigran-ui/pull/455 instead of chat panel (ignore current work done XTM One wise)."

## Context

XTM One's chat reaches the browser over a per-conversation WebSocket, and each of its chat surfaces is its own UI, separate from the `@filigran/chatbot` panel that OpenCTI, OpenAEV and OpenCRQ embed. FiligranHQ/filigran-ui#455 extends that panel with what a full chat page needs (inline mode, host control, slots, marker cards, turns that outlive their stream) and speaks only HTTP + Server-Sent Events. This feature retires the chat WebSocket in favour of SSE and serves every XTM One chat surface on that panel. Prior XTM One work on this (the XTM-One-Platform/xtm-one#6207 plan, which kept the WebSocket and moved only `/chat` behind a flag) is not a constraint.

## Clarifications

### Session 2026-10-21

- Q: Cut over in one release, or behind a feature flag? → A: One release, no flag: a flag would make the old page move to SSE too, or keep the WebSocket alive until it goes.
- Q: Which surfaces move onto the panel? → A: Every chat surface: `/chat`, Studio rails, the agent-detail conversation view, the voice-call overlay and the Office add-in.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Chat on the shared panel with nothing lost (Priority: P1)

A user opens `/chat`, picks an agent, sends a message, watches the answer stream, and uses every capability the page offers today: workspaces and the conversation list, attachments and drop, tool picker, `@` mentions, Deep Investigation, quota and context usage, tool approvals, desktop/runner activity and approvals, deck/report/video/file/work-item/investigation cards, file revisions, masked secrets, sharing, continue-in-channel, background-task and awaited-work banners, interrupted-turn notice, feedback, arcade while waiting.

**Why this priority**: The page is the product's main surface; any lost capability is a regression for every user.

**Independent Test**: Run the existing `/chat` acceptance checks (unit, component, e2e) against the new page; each capability above is exercised once and behaves as before.

**Acceptance Scenarios**:

1. **Given** a user on `/chat`, **When** they send a message, **Then** the answer streams token by token with the same status, reasoning and tool indications as today.
2. **Given** an answer that produced a deck, report, video, file or work item, **When** it completes, **Then** its card renders where its marker stands, with the same actions as today.
3. **Given** a turn that stops at a tool needing approval, **When** the user approves, declines or approves always, **Then** the turn resumes or ends exactly as today.
4. **Given** a running turn, **When** the user sends another message, **Then** it steers the running turn (or is held until it ends) as today.

---

### User Story 2 - A turn survives its connection (Priority: P1)

A user reloads mid-answer, opens the same conversation in a second tab, or loses the network briefly; the turn keeps running server-side and every open view catches up and continues streaming.

**Why this priority**: Long agentic turns (minutes) are the norm; losing one to a dropped connection is the main reason the turn runner is detached today, and SSE must keep that guarantee.

**Independent Test**: Start a long turn, reload the tab, open a second tab, and cut the network for 10 s; all three views end showing the same complete answer.

**Acceptance Scenarios**:

1. **Given** a running turn, **When** the page reloads, **Then** the view shows what was written so far and keeps streaming the rest.
2. **Given** two tabs on one conversation, **When** one sends a message, **Then** the other shows the user message, the streamed answer and any title change, without duplication in the sender's tab.
3. **Given** a running turn, **When** the user presses Stop or "Continue now" on an announced wait, **Then** the server ends the turn or skips the wait, and every open view reflects it.

---

### User Story 3 - Every other chat surface on the panel (Priority: P2)

Each other chat surface renders the panel and keeps what it does today:

- **Studio rails** (Presentation Studio, Video Studio): a compact chat bound to the deck or video on stage, offering only agents that carry the studio's tool, with saves landing on the stage as they commit.
- **Agent-detail conversation view**: a read-only transcript of a conversation with an agent.
- **Voice-call overlay**: a hands-free loop of listen, answer, speak, with barge-in and cancel.
- **Office add-in** (Word, Excel, PowerPoint): document context sent with each question, quick actions, and replace / insert / comment actions applying an answer to the document. It also gains Stop, Continue now and tool approvals, which every panel host has.

**Why this priority**: The WebSocket can only be retired once nothing depends on it, and a surface left on its own UI keeps the duplication this feature removes.

**Independent Test**: With the WebSocket route removed, on each surface: Studio rails and the Office add-in run a full turn (send, stream, Stop, approval) plus their capabilities above; the voice call runs a spoken turn with barge-in and cancel; the agent-detail view shows a transcript and offers no way to send.

**Acceptance Scenarios**:

1. **Given** a Presentation Studio rail, **When** the agent edits the deck, **Then** the stage shows each save as it commits, as today.
2. **Given** a conversation opened from an agent's detail page, **When** it renders, **Then** the transcript, files and cards show as today and no message can be sent.
3. **Given** a voice call, **When** the user speaks over the agent's answer, **Then** speech stops and the agent listens, as today.
4. **Given** the Word add-in with text selected, **When** the user runs "Improve" and picks Replace on the answer, **Then** the selection is replaced, as today.

---

### Edge Cases

- A session that expires or is signed out elsewhere mid-stream ends the stream and signs the user out, as the WebSocket's session-ended close does today; an account deactivated, a password changed or terms of use not accepted refuses the next request, as the WebSocket refuses the next turn today.
- Chat disabled for the edition or the user (CE lockdown, AI-disabled mode) refuses every request that starts or feeds a turn (send, steer, approve, a new conversation, an upload) as "chat unavailable" or "AI disabled"; reading past conversations (opening one in the panel included) and Stop stay open, as AI-disabled mode keeps them (specs/008-ai-disabled-mode). `/chat` keeps sending the user away as today, and the other surfaces show the refusal instead of a conversation.
- A proxy or load balancer that buffers responses or closes idle connections must not stall or cut a stream; keep-alives flow during long tool calls and waits.
- A stream attempt with a confined token (MCP resource, MFA challenge, A2A partner) is refused.
- A message sent while the event stream is reconnecting is neither lost nor shown twice.
- On a deployment meeting the HTTP/2 requirement, any number of open tabs keeps working; on HTTP/1.1, tabs hidden for a minute release their streams so the visible ones keep working.
- The Office add-in shows turns started elsewhere in its conversation, as every panel does; nothing reaches the document without the user's Apply.
- A tab open across the upgrade, still on the old UI, cannot reach the removed WebSocket; it must prompt a reload rather than fail silently.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The web chat MUST stream turns to the browser over Server-Sent Events.
- **FR-002**: The chat WebSocket route (`/chat/conversations/{id}/ws`) MUST be removed in the release that ships this feature.
- **FR-003**: The web chat MUST serve every route the panel's "turns that outlive their stream" contract names: send, steer, approve, cancel, skip-wait, per-conversation event stream, older-message pages, clear all, and text-to-speech (the existing one).
- **FR-004**: Every frame type the WebSocket sends today MUST have an SSE equivalent with the same meaning.
- **FR-005**: A turn MUST keep running when the stream that started it drops.
- **FR-006**: The per-conversation event stream MUST deliver turns a view did not start, re-attach a running turn with its text so far, and never relay a view's own turn back to it.
- **FR-007**: Each SSE request MUST pass the same checks the WebSocket handshake does: valid unconfined token, live session, active account, accepted terms; a request that starts or feeds a turn MUST also find chat enabled (Edge Cases).
- **FR-008**: An open stream MUST end when its session ends, with a signal the client reads as sign-out.
- **FR-009**: An idle stream MUST send a keep-alive at least every 15 seconds.
- **FR-010**: The `/chat` page MUST render the `@filigran/chatbot` panel in inline mode in place of its current UI.
- **FR-011**: Every capability listed in User Story 1 MUST remain available on `/chat`, through the panel's own features or its host extension points.
- **FR-012**: A capability the panel cannot host MUST be added to `@filigran/chatbot` as a generic, opt-in extension, never as XTM One-specific code in the package.
- **FR-013**: The chat links in use today MUST open what they open now: `/chat?c=` (conversation), `/chat?agent=` (new conversation with an agent), `/chat?investigate=&pack=` (Deep Investigation), and the studios' `?c=`.
- **FR-014**: The platform chat routes used by embedded hosts (`/api/v1/platform/chat/*`) MUST keep their current contract.
- **FR-015**: The new `/chat` page MUST replace the current one in a single release, with no feature flag.
- **FR-016**: Studio rails, the agent-detail conversation view, the voice-call overlay and the Office add-in MUST each render the panel.
- **FR-017**: Every capability User Story 3 lists for a surface MUST remain available on that surface.
- **FR-018**: The XTM One chat UI code each surface used before MUST be deleted once that surface renders the panel.
- **FR-019**: The user and developer documentation describing the chat transport and chat surfaces MUST describe SSE and the panel.

### Key Entities

- **Turn**: one agent answer to one or more user messages; runs server-side independent of any connection; has an id, frames, and an end state (done, error, cancelled).
- **Conversation event stream**: a per-conversation, per-view subscription that carries turns and conversation changes to every open view.
- **Client id**: identifies one open view so its own turns are not relayed back to it.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: No code path in the product opens a chat WebSocket; the route answers 404.
- **SC-002**: 100% of the capabilities in User Stories 1 and 3 pass their acceptance checks on the panel.
- **SC-003**: First streamed token appears no later than with the WebSocket (same agent, same prompt, median of 20 runs, within 10%).
- **SC-004**: A turn of 10 minutes completes and is fully shown after a reload at minute 5 and a 10 s network cut at minute 7.
- **SC-005**: XTM One keeps no chat transcript or composer UI of its own; one fix to the panel reaches every XTM One chat surface and OpenCTI, OpenAEV and OpenCRQ.

## Assumptions

- FiligranHQ/filigran-ui#455 merges and `@filigran/chatbot` 3.15.0 (or later) is published before this feature ships; XTM One pins it. The package changes FR-012 calls for (G1-G5, `contracts/panel-host-integration.md`) are part of that release.
- The Office add-in is served by the same deployment as the backend, so it moves in the same release with no version-skew window.
- The chat WebSocket is part of the documented public chat API (`docs/docs/usage/chat-api.md`). Users' own scripts on it break on upgrade, which `UPGRADING.md` and the release notes announce with the SSE replacement. The browser extension (separate repository) does not use it.
- The new-agent wizard and investigation-pack assistants are guided builders on their own endpoints, not chat conversations, and are out of scope.
- The runner (`/runners/connect`) and relay (`/relays/connect`) WebSockets are machine tunnels, not chat, and are out of scope.
- Channel bots (Teams, Slack, Discord, …) do not use the chat WebSocket and are unaffected.
- The product ships no reverse proxy; the deployment documentation states what a proxy in front of it must do for SSE (no buffering, HTTP/2, read timeout above the keep-alive).
