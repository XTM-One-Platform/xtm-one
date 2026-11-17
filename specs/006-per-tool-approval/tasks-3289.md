---

description: "Task list for answering an approval from the executions list (FR-026–FR-037b)"

---

# Tasks: Answering an approval from the executions list

**Input**: `specs/006-per-tool-approval/` — `spec.md` (User Story 5, FR-026–FR-037b, SC-018–SC-024), `plan.md` (Increment), `research.md` (D11–D15), `contracts/executions-approvals.md`, `data-model.md` §10, `quickstart.md` Scenarios 10–14

**Why this is a separate file**: these tasks satisfy requirements of the `006-per-tool-approval`
spec, so they are not a new feature and get no new spec — but keeping them in that feature's
`tasks.md` would put them in the path of `/speckit-implement`. Task IDs continue that file's
sequence (last used: **T181**, in `tasks-3282.md`).

**Notes that are not reconstructable from the diff**:

* **The selectable unit is the run.** The loop refuses a partial set within one, so per-proposal selection would let a reviewer build something the backend must reject (FR-031).
* **`RunCard` already had an `onApprove` prop** and the executions page never passed it, so it told whoever could approve that it was *"waiting for approval from someone with access"*.
* **The refusal cannot name who decided**: the resume path clears every pause field (FR-030).
* **No frontend test framework exists** — no vitest config, no test script. Every frontend guarantee here is held from the backend or not at all.

## Format: `[ID] [P?] [Story?] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[US5]**: User Story 5 — answering a pause without opening the run

---

## Phase 1: Foundational

- [X] T182 Distinguish the reasons a run can no longer be answered in `backend/app/api/v1/assignments.py` (`_refuse_not_awaiting`): one `400` covered already-answered, ended and never-paused alike. Resumable statuses now answer `409` naming the status reached, hedged because no pause field survives a resume, per FR-030
- [X] T183 [P] Cover those reasons in `backend/tests/test_notification_approval_visibility.py`, including that the wording stays hedged — "answered by X at Y" is the detail a later edit would be tempted to invent

**Checkpoint**: answering a stale approval says why it failed.

---

## Phase 2: Answer one approval from the list (US5, FR-026–FR-030, FR-037)

**Independent Test**: [quickstart.md Scenario 10](./quickstart.md).

- [X] T184 [US5] Pass `onApprove` to `RunCard` in `frontend/src/pages/TaskHistory.tsx`, wired to `useSubmitApprovalDecisions`. Without it the card renders "Waiting for approval from someone with access to this run" to the person with access, per FR-026, FR-029
- [X] T185 [US5] Surface T182's refusal reasons in the page's toast so a stale approval says which reason applies, per FR-030
- [X] T186 [P] [US5] Assert the listing model still carries `pending_approvals` in `backend/tests/test_notification_approval_visibility.py` — drop it and the page silently renders nothing while the run page keeps working, per FR-027
- [X] T187 [P] [US5] Assert there is exactly one `_grant_standing_approvals`, so an exception cannot mean different things per surface, per FR-029, FR-037b

**Checkpoint**: one approval is answerable from the list. Shippable alone.

---

## Phase 3: Answer several at once (US5, FR-031–FR-034)

**Independent Test**: [quickstart.md Scenarios 11–13](./quickstart.md).

- [X] T188 [US5] Add `POST /api/v1/assignments/runs/approvals/batch` in `backend/app/api/v1/assignments.py`, composing the existing `_apply_decisions_to_run` / `_grant_standing_approvals` / `resume_assignment_run` per member, per [contracts/executions-approvals.md §5](./contracts/executions-approvals.md), FR-031
- [X] T189 [US5] Authorise every member individually through `require_run_access`: bundling a run with ones the caller owns must not grant reach over it, per FR-028
- [X] T190 [US5] Refuse a run with an undecided call rather than letting it fail the batch, per FR-031, FR-033
- [X] T191 [US5] Return per-member `applied`/`refused` results and never roll back an applied member, per FR-033
- [X] T192 [P] [US5] Cover the batch in `backend/tests/test_batch_approval_api.py`: partial success, per-member audit, no batch-level record, the two-tab race, per FR-032, FR-033, SC-023
- [X] T193 [US5] Add bulk approve/reject to `frontend/src/components/ui/list-control-bar.tsx`, reusing the page's existing selection rather than a second one, per FR-031
- [X] T194 [US5] Add a shortcut selecting exactly the rows awaiting a decision in `frontend/src/pages/TaskHistory.tsx` — approvals sit interleaved with notices that cannot be answered at all, per FR-031a
- [X] T195 [US5] Name what the selection spans on the action buttons (counts) and in the confirm, per FR-031b
- [X] T196 [US5] Collect **one** rejection reason for a group in `frontend/src/pages/TaskHistory.tsx` and apply it to every member; an empty reason stays permitted, per FR-031c
- [X] T197 [US5] Offer "approve always" over a group with a confirm **enumerating every** tool it would un-gate, as a control distinct from the one-off approve, per FR-032a, FR-032b
- [X] T198 [P] [US5] Assert the batch grants through the shared helper rather than a batch-local copy, per FR-032c

**Checkpoint**: a reviewer resolves ten approvals in one action, having seen each one.

---

## Phase 4: Finding what is waiting (US5, FR-035, FR-036)

- [X] T199 [US5] Confirm the page's `status=awaiting_human` filter narrows to pauses, per FR-035
- [X] T200 [US5] Verify a pending approval cannot be dismissed from the executions list as though it were a notice, per FR-036

---

## Phase 5: Polish & cross-cutting

- [X] T201 [P] Add every new string to `frontend/src/i18n/lang/{en,fr,es,ja}.json`, merged **by key** and re-sorted, and run `node frontend/scripts/validate-i18n.mjs`
- [X] T202 [P] Document the surface in `docs/docs/administration/ai-governance.md`: a reviewer can answer from the executions list, including "approve always", with the same effect as the run page
- [X] T203 Run the full gate — `ruff check .`, `pytest -m "not integration"`, `yarn check`, `validate-i18n.mjs`
- [X] T204 Settle FR-028 against `require_run_access`. Resolved in the spec, not the code: FR-008a was written against a different hazard — a bystander answering someone else's pause on a shared channel (`tasks-3118.md` T157) — and reading it as "only `run.user_id`" would strand company-managed automation whose triggering user has left. FR-008b now states the rule the code implements, and FR-008c pins why the standing exception is granted to the run's identity rather than the clicker

---

## Dependencies

```
Phase 1 (T182–T183) ──blocks──> Phase 2 (T184–T187) ──> Phase 3 (T188–T198)
                                       │                        │
                                       └──> Phase 4 (T199–T200) ─┴──> Phase 5
```

**T204 is not a code task** — it is a decision the spec owes, surfaced rather than resolved
unilaterally.

## Implementation strategy

**MVP is Phase 1 + Phase 2.** That answers the complaint — a reviewer stops having to open each run
— and every later phase is volume relief on top of it. Phase 3 carries the real security surface (a
selection spanning tools the reviewer did not individually consider), which is why FR-032b's
enumerating disclosure is a task in its own right.

---

## Phase 6: Convergence

- [X] T205 ~~Duplicate of T204~~ — converge re-filed a task already open in Phase 5. Closed with T204
- [X] T206 Record an explicit gate outcome for every proposal on a run dismissed while awaiting approval, in `backend/app/api/v1/assignments.py` (`dismiss_run`): it sets `status="completed"` and writes a run-level `resolution="dismissed"` but leaves `pending_approvals` untouched, so the gated calls end with no verdict at all — a blank is reserved for a gate that failed to install, per FR-020, FR-036 (partial)
- [X] T207 [P] Document the executions-list surface in `docs/docs/administration/ai-governance.md`, including group approval and "approve always" over a group with its enumerating disclosure, per plan: Principle VI (missing)
- [X] T208 [P] Assert a pending approval cannot be hidden from the executions list without resolving the run, in `backend/tests/`, per FR-036 (partial)
