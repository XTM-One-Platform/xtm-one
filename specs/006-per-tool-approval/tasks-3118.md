---

description: "Task list for the chat channels collecting an approval"

---

# Tasks: The chat channels collect an approval

**Input**: `specs/006-per-tool-approval/` — `spec.md` (FR-002b1, FR-002c1/c2, FR-008a, SC-016, SC-017), `plan.md`, `contracts/chat-approval-api.md`

**Split out**: 2026-08-26

**Why this is a separate file**: these tasks satisfy requirements of the
`006-per-tool-approval` spec, so they are not a new feature and get no new spec — but they are
deliberately out of the browser-socket change, and keeping them in that feature's `tasks.md` put them in the path
of `/speckit-implement`. Task IDs continue that file's sequence so nothing is renumbered and
its references stay valid.

**Blocked on**: **T154** in `tasks.md` (Phase 17) — the shared propose-and-resume path. A channel
turn is a webhook and cannot be held open for a person, so these tasks consume that path and must
not grow a channel-specific copy of it.

## Format: `[ID] [P?] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- Include exact file paths in descriptions

---

## Phase 1: The chat channels collect an approval

**Purpose**: Deliver FR-002b1 for Slack, Teams and email, for **agents as well as the general assistant** — `channels/router.py` calls `run_agent_chat` (`:2868`) and `run_general_chat` (`:2923`) with neither `request_approval` nor `approvals_out`, so no assistant can complete a gated call there. Split out on 2026-08-26 and **out of the browser-socket change**, which ships the browser socket and the explanatory message T151 left in place.

**Depends on T154** (Phase 17). The channels cannot use a mid-turn callback — a webhook turn cannot be held open for a person — so they need the propose-and-resume path, and they must consume the shared one rather than growing a channel-specific copy.

**Independent Test**: A Slack user asks for something gated, is told approval is pending, answers, and the call executes on a later inbound message — with the process that ran the paused turn restarted in between. A different member of the same channel cannot answer it. The proposal's arguments never appear in the shared channel.

- [ ] T156 Consume it from the chat channels in `backend/app/channels/router.py`, for **agents as well as the general assistant** — `run_agent_chat` is called there without `request_approval` or `approvals_out` either, so a Slack user cannot complete a gated call with any assistant. A webhook turn cannot be held open, so this is propose-and-resume by construction, replacing T151's interim message, per FR-002b1, FR-002c2
- [ ] T157 Enforce that only the identified user whose call is paused may answer, refusing and recording any other user's decision rather than ignoring it, in `backend/app/services/chat_turn_guard.py` and both decision endpoints (`backend/app/api/v1/chat.py`, `backend/app/api/v1/platform.py`). Not hypothetical once T156 lands: a proposal reaching a shared channel is visible to everyone in it, per FR-008a, FR-020
- [ ] T158 Keep a proposal's arguments and its decision out of shared view on the chat channels, in `backend/app/channels/` and the surface-specific senders: a channel with other people in it may carry a notice that approval is pending, while the proposal and the decision go where only that user can act. FR-006 requires the full arguments be shown to the reviewer, which is precisely why they must not be broadcast, per FR-008a, FR-006

**Checkpoint**: The explanatory message from T151 is gone, replaced by a question the reviewer can actually answer. T157 must not ship later than T156: the moment a proposal reaches a shared channel, "only the paused user may answer" stops being theoretical.
