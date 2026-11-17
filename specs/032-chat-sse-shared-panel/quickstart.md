# Quickstart: validating chat over SSE on the shared panel

## Prerequisites

- `@filigran/chatbot` 3.15.0 (#455, gaps G1-G5, `contracts/panel-host-integration.md`), pinned at 3.15.2 in `frontend/package.json` and `apps/microsoft-office/package.json` (T006). To try an unreleased build, resolve the package to a local filigran-ui checkout (a `portal:` resolution in `frontend/package.json`, not committed; a linked build for the add-in).
- Dev stack up: `docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build`. A worktree also exports `SECRET_KEY`.

## Automated checks

```bash
cd backend && uv run pytest tests/ -m "not integration" -k "chat or turn or relay or approval or steer or session"
```

```bash
cd backend && uv run pytest tests/test_chat_web_sse_transport.py tests/test_chat_web_routes.py tests/test_chat_web_sse_fanout.py tests/test_chat_web_agents.py tests/test_chat_panel_contracts.py tests/test_agent_chats_panel_session.py tests/test_chat_ws_removed.py
```

```bash
cd frontend && yarn check
```

```bash
cd frontend && yarn test:e2e --project=core
```

The add-in's `src/lib` unit tests run with the frontend's (`frontend/tests_unit/office-addin-*.test.ts`); `cd apps/microsoft-office && npm run check` type-checks it and runs its component test (`src/components/OfficeChatPanel.test.tsx`, vitest + jsdom). The cross-replica Stop and Continue now run against a real Redis: `REDIS_URL=redis://… uv run pytest -m integration tests/test_chat_web_cancel_skip.py`.

Expected: green. `rg -n "chat/conversations/.*/ws|new WebSocket" frontend/src apps/microsoft-office/src` returns nothing (SC-001).

## Manual scenarios

| # | Do | Expect | Covers |
|---|---|---|---|
| 1 | Open `/chat`, send a message to an agent with a tool | Status, reasoning, tool status, streamed answer, then `done`; same as before the change | US1, SC-002 |
| 2 | `curl -N -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"content":"hi","conversation_id":"<id>"}' http://localhost:8100/api/v1/web/chat/messages` | `data:` frames, `: keepalive` during long tool calls, a final `done` | FR-001, FR-009 |
| 3 | Start a long turn (a `wait` tool), reload at once | Text so far reappears and keeps streaming | US2, FR-006 |
| 4 | Same conversation in two tabs, send from one | The other shows the user message, the answer and the title; the sender sees no duplicate | US2 |
| 5 | Stop mid-turn; then "Continue now" on an announced wait in a new turn | `cancelled` in both tabs; the wait ends within ~1 s | US2, FR-003 |
| 6 | Two backend replicas behind one port; send on one, Stop from a tab routed to the other | The turn stops | R5 |
| 7 | Revoke the session (sign out elsewhere) during a turn | `session_expired`, the tab signs out | FR-008 |
| 8 | A tool needing approval: approve, decline, approve always | Same outcomes as today | US1 |
| 9 | Presentation Studio rail: ask for a slide change | The stage follows each save | US3 |
| 10 | Agent detail → Chats → a conversation | Transcript and cards, no composer | US3 |
| 11 | Voice call: speak, then talk over the answer | The agent answers aloud; barge-in stops speech | US3 |
| 12 | Word add-in: select text, "Improve", Replace | Selection replaced; the bubble shows the request, not the document context | US3 |
| 13 | A tab from the previous build, then focus it | The reload notice appears | R12 |

SC-003 (first-token latency) and SC-004 (10-minute turn with reload and network cut) are measured against the previous release with the same agent and scripted model (`tests_e2e/scripted-llm`).

## Results (2026-10-22, `feat/chat-page-panel` dev stack, US3 session)

Run on the dev stack of the branch (`docker-compose.yml` + `docker-compose.dev.yml`, vite on `:3130`, platform on `:8130`), with agents pinned to the scripted model (`custom_openai` / `scripted-llm`).

| Measure | Result |
|---|---|
| SC-003, first token | Against the previous release on one rig (below, 2026-10-27): median **89.9 ms** over the WebSocket and **101.5 ms** over SSE, 80 runs each, SSE +12.8%. **Not met, by a small margin**: SC-003 allows 10%. The 35 ms SSE median measured here (20 runs, p90 43 ms) was on this dev stack, which has no WebSocket to compare against, so it is not compared. |
| SC-004, 10-minute turn | One turn of two scripted `wait` calls (300 s + 290 s) and a final answer; the page reloaded at minute 5 (the user message and the running turn came back) and the network was cut for 10 s at minute 7 (the events stream refused meanwhile). The answer appeared at minute **9.84**, once (1 user message, 1 answer), and a final reload read the same complete answer back; the model served 3 agent turns, as scripted. **Pass.** |
| SC-001 | `git grep -nE "chat/conversations/.*/ws\|new WebSocket" -- frontend/src apps/microsoft-office/src` finds nothing; `GET /api/v1/chat/conversations/{id}/ws` answers 404. |

Scenarios run (the rest are covered as noted):

| # | Outcome |
|---|---|
| 2 | `curl -N` on `/api/v1/web/chat/messages` with a 35 s scripted `wait`: `text/event-stream`, `X-Accel-Buffering: no`, `: keepalive` at 15 s and 30 s, then `stream` and the final `done`. |
| 3, 4, 5 (Stop), 8 | `tests_e2e/chat-resume.spec.ts` (reload mid-turn, second tab, Stop from the second tab, a send during a reconnect shown once, reload during an approval): passed. |
| 7 | `tests/test_chat_web_sse_transport.py`: `test_a_session_that_ends_closes_the_stream_and_not_the_turn` (the send stream) and `test_a_session_that_ends_ends_the_events_stream` (the events stream, `idle` and `lifetime`): passing; not repeated by hand. |
| 9 | `tests_e2e/presentation-studio-rail.spec.ts`: passed (studio agent list, deck-bound session, `studio_slide`, upload, history). |
| 10 | Agent detail → Chats → a conversation: transcript in the panel, no composer (checked on the stack). |
| 11 | `tests_e2e/chat-voice-call.spec.ts`: passed (spoken send, `/chat/tts`, barge-in pauses playback). |
| 6 | Two replicas of the branch's backend (:8201, :8202) on one Postgres and Redis, a scripted 90 s `wait`, the send on :8201 and the control on :8202 under another client id, 3 runs each. Stop: `{"turn":"remote"}`, `cancelled` on :8201 within 0.57-0.98 s. Continue now: `{"turn":"remote"}`, the wait skipped and `done` within 0.98 s. The :8202 events stream relayed every frame of the :8201 turn within ~50 ms. With no turn running both answer `{"turn":"none"}`. Passed. |
| 12 | `apps/microsoft-office/src/components/OfficeChatPanel.test.tsx` (`npm run check`): the real pane and panel with Office.js and fetch faked. Improve on a Word selection sends the document context with the action's prompt, the bubble shows "Improve the writing quality" (live and read back), the action block is stripped, Replace previews and Apply writes the agent's text over the selection: passed. Not run in an Office host. |
| 13 | `tests_e2e/stale-build-notice.spec.ts` on the dev stack (the page's build injected into the document, `/auth/config` routed, focus on the page clock): the reload notice is offered on focus once per server build, never for the page's own build, and Reload loads the new build: passed. `frontend/src/hooks/useStaleBuildNotice.test.tsx` covers the same rule in `yarn check`. |

Local e2e runs use the stored session (`--no-deps`): this stack's admin speaks French, so `auth.setup.ts` and English role names fail there; with `/auth/me`'s `language` set to `en` for the run (not committed) where a spec reads an English label as CI does.

`./scripts/ci-local.sh` (the run stops at the first failing group, so the later groups ran with `--only`):

| Group | Result |
|---|---|
| Backend lint + unit | ruff clean; 55,487 passed, 3 failed: `test_ai_disabled_read_surface` (the new read-only `POST /agents/{id}/chats/sessions` lacked its read exemption: fixed and passing), `test_ci_local_lock::test_without_flock_the_run_goes_on_unqueued` and `test_model_catalog_announcements::test_the_speech_email_names_the_video_studio_only_while_it_is_on` (both fail the same way at `HEAD` on this machine: `flock` installed, `VIDEO_STUDIO` on in the checkout's `.env`). |
| Frontend | typecheck, eslint, manifests, i18n, unit (1,375 node + 996 vitest) and design-system guards pass. |
| Office add-in | `npm ci` installs the pinned published `@filigran/chatbot` 3.10.0, which has no inline mode, handle types or `displayText`: the typecheck fails until T006 pins the release (it passes against the linked package build, as the frontend does through its local `portal:`). |
| Migrations, integration, docs | pass (integration: 2,684 passed). |
| E2E | the platform image build stops at `yarn install --immutable`: the local `portal:` resolution of `@filigran/chatbot` is outside the Docker context. Blocked until T006; the chat specs were run against the dev stack instead (above). |

## SC-003 against the previous release (2026-10-27)

Both builds on one rig, one backend up at a time: each backend from source on the host (`uvicorn`, no `--reload`, its own database), with Postgres, Redis, MinIO and `xtmone/scripted-llm:1.0.0` in Podman. The previous release is `1.261023.0`, over `WS /api/v1/chat/conversations/{id}/ws`; `main` is `3abd8b4e8`, over `POST /api/v1/web/chat/messages`. Same agent (`custom_openai` / `scripted-llm`, no tools), a new conversation per run and the same scripted one-sentence answer. Each run is timed from the send to the first `stream` frame; over the WebSocket, the send goes on a socket that is already open. Each round is one warm-up run and then 20 measured runs, with the quota counters cleared before the round.

| Round | WebSocket: median / p90 / min-max, whole turn (ms) | SSE: median / p90 / min-max, whole turn (ms) | SSE vs WebSocket |
|---|---|---|---|
| 1 | 103.2 / 134.7 / 77.6-141.8, 126.8 | 102.7 / 113.0 / 52.5-115.7, 118.2 | -0.5% |
| 2 | 84.8 / 108.8 / 57.5-121.3, 101.2 | 103.0 / 122.2 / 66.9-161.7, 118.8 | +21.5% |
| 3 | 91.5 / 134.2 / 75.2-142.3, 106.2 | 96.0 / 129.5 / 62.9-130.4, 111.8 | +4.9% |
| 4 | 85.2 / 116.3 / 72.8-128.7, 102.5 | 99.5 / 124.0 / 79.2-127.4, 117.7 | +16.8% |
| All 80 | **89.9** / 123.1 / 57.5-142.3, 106.1 | **101.5** / 122.0 / 52.5-161.7, 117.8 | **+12.8%** |

Round 1 alone, the 20-run comparison SC-003 names, is a tie. The rounds spread by about 20 ms, though, and over all 80 runs SSE is 11.6 ms slower at the median (95% bootstrap interval +5.4 to +17.4 ms; Mann-Whitney p = 0.002). The p90s are level.

Where the extra time goes was not profiled. Two observations from round 4:

- The first frame (`thinking`) reaches the client at 29 ms over SSE and at 1.5 ms over the WebSocket. The SSE send holds its pre-turn frames until the turn is accepted, so it can still refuse with an HTTP status; the WebSocket wrote `thinking` before its checks.
- Opening the socket took a median of 10 ms. The WebSocket paid that once per connection, not per send. Counting it in, the WebSocket's first token is at 100.5 ms, level with SSE.
