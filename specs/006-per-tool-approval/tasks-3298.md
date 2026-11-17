---

description: "Task list for releasing paused runs already covered by a standing approval (FR-039)"

---

# Tasks: Release paused runs already covered by a standing approval

**Input**: `specs/006-per-tool-approval/` — `spec.md` (FR-039–FR-040h, FR-015, FR-031–FR-033), `contracts/executions-approvals.md` §5

**Why this is a separate file**: these tasks satisfy requirements of the `006-per-tool-approval`
spec, so they are not a new feature and get no new spec. Task IDs continue the sequence (last used:
**T208**, in `tasks-3289.md`).

**Notes that are not reconstructable from the diff**:

* **`tool_key` is the proposal's `tool_name`** — `_grant_standing_approvals` writes it straight from `proposal["tool_name"]`, and the gate looks it up by the same string. That is what lets the coverage test be a plain set membership.
* **No new endpoint, no new state.** The page already holds both halves: `pending_approvals` on each run and the caller's active preferences from `useToolApprovals`. Releasing reuses the batch endpoint.
* **Nothing sweeps.** `resume_assignment_run` is called only from the respond and batch endpoints; `tool_approval_service.py` never touches `awaiting_human`. That is the defect, not an oversight to route around.

## Format: `[ID] [P?] Description`

- **[P]**: Can run in parallel (different files, no dependencies)

---

## Phase 1: Offer the release

**Independent Test**: grant "always" on a tool, then pause three runs on it. The executions list
offers to release all three; one click resumes them, each with its own decision recorded.

- [X] T209 Derive the covered set in `frontend/src/pages/TaskHistory.tsx` from `useToolApprovals().preferences` against each run's `pending_approvals`, requiring **every** pending call to be covered, per FR-039, FR-039b
- [X] T210 Offer it rather than applying it: a banner stating how many paused runs are covered, with a single release action, per FR-039a
- [X] T211 Release through the batch endpoint, reporting per-member results and never granting anything, per FR-039c
- [X] T212 [P] Add the strings to `frontend/src/i18n/lang/{en,fr,es,ja}.json` and run `validate-i18n.mjs`

**Checkpoint**: "always" stops meaning "except for what is already waiting".

---

## Phase 2: Still open

- [X] T213 Cover FR-039b from the backend: a run with one covered and one uncovered call must not be releasable. The rule lives in the page today and the frontend has no test framework, so the guarantee is currently unenforced — the batch itself cannot check it, since it is told which calls to answer rather than deciding — so the release moved to `POST /assignments/runs/approvals/release-covered`, which takes run ids and no decisions: the server resolves the caller's standing approvals and refuses any run whose pending calls are not all covered (`not_fully_covered`). The page still decides what to *offer*; it no longer decides what is allowed
- [X] T214 [P] Document the release offer in `docs/docs/administration/ai-governance.md`, under "Answering several at once"
- [X] T215 Decide whether the same offer belongs on the run page. A reviewer who grants "always" there is told nothing about the other runs it now covers, which is the same gap on a surface this increment did not touch — **decided: no** (Fred, 2026-09-04). The run page answers one run, and the reviewer arrived there for that run; the executions list and the profile are where somebody is looking at the backlog and so where the offer belongs. Keeping it off the run page also keeps a page about one run from acting on others. Reopen only if reviewers are seen granting "always" on a run page and then answering the same tool by hand elsewhere

---

## Dependencies

Phase 1 is self-contained. T213 is the one that matters: without it, FR-039b holds only by the
page's arithmetic being right.

---

## Phase 3: Answering by tool, across pages (FR-040)

**Why this is here and not in its own issue**: it is the same question — how a reviewer gets
approvals done beyond the page in front of them — and it subsumes Phase 1. Releasing covered runs
needs a prior grant; naming a tool does not.

**Independent Test**: pause runs on two tools across more pages than fit on screen. Open *Approve by
tool*, tick one: the count covers every page, and runs mixing both tools are reported as held back.
Tick the second and they become answerable.

- [X] T216 Add `run_answerable_filter` in `backend/app/api/v1/helpers.py`, beside `require_run_access` and narrower than `run_visible_filter` — visibility shows every company-managed run to everyone while only an admin may answer one, so a query built on the visible filter offers work it would then refuse
- [X] T217 Add `GET /assignments/runs/approvals/pending-by-tool`, counting runs and calls separately and reporting mixed runs rather than dropping them, per FR-040b, FR-040c
- [X] T218 Add `POST /assignments/runs/approvals/by-tool`, answering only runs whose calls are all named, through the same per-member path as the batch, per FR-040, FR-040a
- [X] T219 [P] Cover both in `backend/tests/test_approve_by_tool.py`: grouping, the mixed-run exclusion, a company-managed run not swept in for a non-admin, naming both tools releasing a mixed run, and an empty tool list refused
- [X] T220 Build the picker in `frontend/src/pages/ApproveByToolDialog.tsx`: multi-select with per-tool counts, a live held-back number, and the FR-040d disclosure before the action
- [X] T221 [P] Add the strings to `frontend/src/i18n/lang/{en,fr,es,ja}.json`
- [X] T225 Exclude runs whose decision is already recorded, in `_answerable_pending_runs` and on the page's approvable lists. `resume_assignment_run` emits an outbox event without changing the status, so an answered run stays `awaiting_human` until the worker drains it — counting it blamed the reviewer for a queue, and offering it again would overwrite the recorded decision and emit a second resume. Exposed as a derived `approval_answered` rather than the decisions themselves, per FR-040g
- [X] T226 Poll the cross-page total on the same interval as the runs list: invalidation covers work the reviewer finishes but not work that arrives, so a new pause appeared in the list while the total stayed put, per FR-040f
- [X] T223 Refresh the cross-page total from `invalidateRunCaches` in `frontend/src/hooks/api/useAssignmentApi.ts` rather than from the by-tool mutation alone — every path that changes a run stales it, and the banner otherwise claimed work already finished from the card or the selection bar, per FR-040f
- [X] T224 Promote the by-tool entry from a ghost link to its own bar carrying the cross-page total, so the contrast with the selection bar's "selected" wording is visible rather than inferred, per FR-040c
- [X] T222 Accept `approve_always` in the by-tool path, granting through the shared helper so N runs of one tool yield one exception, with a confirmation stating the durable reach and a control kept distinct from the one-off approve, per FR-040e, FR-032b, FR-032c

**Checkpoint**: a reviewer clears a backlog spanning pages without approving anything they did not
choose a tool for.

---

## Phase 4: One agent's backlog (FR-040h)

**Why this is here**: FR-040 answers everything a reviewer may answer, which is the right scope from
the executions list and the wrong one from an agent's page — the page shows one agent and the sweep
reached the whole queue.

**Independent Test**: pause runs of two agents on the same tool. From agent A's activity list, tick
that tool: the count covers only A's runs across every page of its activity, and B's stay paused.

- [X] T227 Scope both by-tool endpoints with an optional `agent_id`, reaching `_answerable_pending_runs` and not only the summary, and offer the picker from `frontend/src/pages/agent-detail/ActivityTab.tsx` with the agent in the query key, per FR-040h
- [X] T228 [P] Add the strings to `frontend/src/i18n/lang/{en,fr,es,ja}.json` — the description, the pre-fire disclosure and the banner each gain an agent-scoped form, and "always" gains the line saying other agents' paused runs are not released
- [X] T229 Re-land the schema in `alembic/versions/e5rel3298_...py` rather than by editing the original three: every platform that upgraded through main is stamped past `d4und0173`, so those bodies never run again
- [X] T230 Fail the runs the revert stranded, in the same migration: a platform stamped before the revert release drops and re-creates `pending_approvals` in one startup, leaving a gated pause with no proposals that `respond_to_human_input` refuses free text for. Matched on the gate's own question prefix so a classic `request_human_input` wait, still answerable, survives — covered in `backend/tests/test_reland_approval_schema_migration.py`

**Checkpoint**: the by-tool control acts on exactly the runs the page it sits on shows.

