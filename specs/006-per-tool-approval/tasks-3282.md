---

description: "Task list for the wholesale approval switches"

---

# Tasks: Platform kill switch and per-user allow-all

**Input**: `specs/006-per-tool-approval/` — `spec.md` (FR-023, FR-024, FR-025), `data-model.md` §5a, `contracts/rest-api.md` §3/§4a/§9a, `contracts/events.md` §3

**Filed**: 2026-09-02

**Why this is a separate file**: these switches sit above the `006-per-tool-approval` gate rather
than changing it, so they are not a new feature and get no new spec. Task IDs continue `tasks.md`'s
sequence so nothing is renumbered.

**Provenance**: the implementation landed as one commit authored by Samuel Hassine, cherry-picked
onto `feat/173-per-tool-approval` after its pull request was left without a base by a revert.
It carried no tests and no spec updates; T174–T176 close that, and the boxes below are ticked
against what is actually in the tree rather than against the pull request description.

## Format: `[ID] [P?] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- Include exact file paths in descriptions

---

## Phase 1: The switches

**Purpose**: Give an administrator a way to stop the gate entirely, and a user the wholesale form
of "Yes, always", without either one destroying the per-tool preferences it makes moot.

**Independent Test**: With the platform switch off, a scheduled run calling a non-whitelisted tool
completes without pausing and its action log names `auto_run_approvals_disabled`. Turning it back
on restores the exact set of standing exceptions the users had.

- [x] T174 Add `platform_settings.tool_approvals_enabled` and `users.approve_all_tools` in `backend/app/models/platform_settings.py` and `backend/app/models/user.py`, with one idempotent, lock-safe migration (`backend/alembic/versions/d4a2all3282_add_hitl_allow_all_switches.py`), per FR-023, FR-024
- [x] T175 Resolve both at a single point in `backend/app/services/hitl_whitelist.py` (`resolve_gate_suppression`), consumed by `build_approval_gate` and by the assignment engine's inline gate (`backend/app/services/assignment_engine.py`) so no entry point can honour one and miss the other, per FR-025
- [x] T176 Install a recording-only gate rather than no gate (`make_recording_gate`), stamping `auto_run_approvals_disabled` or `auto_run_user_allows_all` so a suppressed call stays distinguishable from an ungated one and from the `null` that means a gate failed to install, per FR-020, FR-025
- [x] T177 Expose them: `PUT /api/v1/tool-approvals/mode` for the user's own flag and `tool_approvals_enabled` on `PUT /api/v1/settings/platform`, both audited, the latter refusing a non-boolean rather than coercing it, in `backend/app/api/v1/tool_approvals.py` and `backend/app/api/v1/settings.py`, per FR-023, FR-024, FR-025
- [x] T178 Surface them: the admin card in `frontend/src/pages/settings/ParametersSection.tsx` between Artificial Intelligence and Platform Language, and the user's toggle in `frontend/src/pages/profile/ToolApprovalsRow.tsx`, which collapses the now-moot per-tool list to a count and explains a platform switch the user cannot change, per FR-023, FR-024

**Checkpoint**: Both switches work end to end, and neither deletes a `ToolApprovalPreference` row.

---

## Phase 2: What the implementation commit did not carry

**Purpose**: The commit shipped the behaviour with no tests and a spec that had gone stale rather
than merely silent — FR-013 still asserted that every `requires_approval` call pauses.

- [x] T179 Cover the switches in `backend/tests/test_hitl_allow_all_switches.py`: both fail-closed reads, the missing-settings-row default, the string/UUID identity split between chat and runs, `setdefault` on the recording gate, entry-point parity, and the endpoints. Verified by mutating the source, not by going green, per FR-023, FR-024, FR-025
- [x] T180 [P] Sync the spec artefacts — FR-023/024/025 and the two corrections to FR-013 and FR-020 in `spec.md`, §5a in `data-model.md`, §3/§4a/§9a in `contracts/rest-api.md`, the suppression subsection in `contracts/events.md`
- [x] T181 [P] Finish the design pass the issue asked for on `frontend/src/pages/settings/HitlWhitelistSection.tsx`: the flat chip dump grouped by provenance with counts, an empty state, and the remove control reachable by keyboard

**Checkpoint**: the acceptance criteria are all met and each is covered by a test or a
contract document.

---

## Deliberately not done

- **`request_human_input` is not suppressed** by either switch (FR-023). It needs no exemption code: the tool ships on the default whitelist and was never gated. `T179` asserts that, so a future change to the shipped default cannot silently gate it.
- **The column is `tool_approvals_enabled`, not the issue's `hitl_enabled`.** It governs the tool approval gate only, and the broader name would have claimed otherwise. Recorded in `data-model.md` §5a so it is not "corrected" back by someone reading the issue.
