---

description: "Task list for closing the decide-and-resume race on every approval path (FR-030, FR-039c, SC-023)"

---

# Tasks: Close the decide-and-resume race

**Input**: `specs/006-per-tool-approval/` — `spec.md` (FR-030, FR-039c, SC-023, and FR-031–FR-033 they build on)

**Why this is a separate file**: these tasks satisfy requirements of the `006-per-tool-approval`
spec, so they are not a new feature and get no new spec. Task IDs continue the sequence (last used:
**T282**, in `tasks-3311.md`).

**Notes that are not reconstructable from the diff**:

* **Half of this already landed.** `respond_to_human_input` and `dismiss_run` are locked.
  The issue's claim that `with_for_update` "appears nowhere in the file" is stale. What remains is
  the bulk half — and, as T294 found, the half of the *single* path that the lock alone never fixed.
* **The visibility of pending approvals is done and is not in scope here.** `_visible_pending_approvals` plus `redact_proposals`
  cover all four serializers and the `activity:company` feed. Named only because a reader will look
  for it.
* **The lock does not close the race on its own, and this is the finding that shaped the file.**
  Answering a run does not change its `status`: `resume_assignment_run` emits an outbox event and
  commits, so the run stays `awaiting_human` until the worker drains it. A second reviewer therefore
  takes the lock, reads exactly the status the first one read, and overwrites their verdict. The
  field that changes is `approval_decisions`, so every deciding path has to read *that* under the
  lock. The lock serialises; the guard decides.
* **The selection query must not lock.** Locking `_answerable_pending_runs` was tried and reverted.
  It cannot work: the first run's resume commits, which drops every lock the query took, so runs
  2..N are decided from a snapshot taken before that commit (`expire_on_commit=False`). It is also
  actively harmful — the summary path counts through the same helper, and a sweep naming a tool
  nobody waits on would hold `FOR UPDATE` over the caller's whole queue, blocking every single-run
  decision until the 30s `statement_timeout`. The sweep locks one row at a time instead.
* **The refusal paths end no transaction, and must not.** Releasing each refused run's lock looked
  like cheap insurance against two crossing requests deadlocking. It is not: `rollback` expires the
  entire identity map — `expire_on_commit=False` governs commit, not rollback — including the
  `user` the request was authenticated with, so the next member's `require_run_access` touches an
  expired instance and the batch dies with `MissingGreenlet`. That turned a rare, self-resolving
  deadlock into a deterministic 500 on the ordinary "one bad member, then a good one" path, which
  is FR-033 exactly inverted. `commit` is no better: the applier paths reach the refusal with
  decisions already written onto the run. Locks are therefore held to the loop's next commit or the
  request's end, and the deadlock is accepted: it needs two bulk requests naming overlapping runs in
  *different* orders, while every surface lists them in one order, and PostgreSQL breaks it itself.
  T294 is the test that holds this line.
* **The by-tool sweep reports no member for a run it loses.** FR-033 names members the *caller*
  named; this endpoint's members are chosen by the server from a filter, so a run answered
  elsewhere was never in the sweep. Reporting it refused would invent a member.
* **Source-grep tests cannot see any of this.** The existing lock test greps for
  `.with_for_update()`, which passes on code that locks the wrong row, locks too much, or never
  re-reads. T296 puts the load-bearing half against real Postgres.

## Format: `[ID] [P?] Description`

## Phase 1: the two helpers

- [x] **T283** Add `_locked_run(db, run_id)` to `backend/app/api/v1/assignments.py`: one
  `select(...).with_for_update()`, with `populate_existing=True` so the re-read is a real re-read
  and not the identity map's pre-lock copy — which a body naming the same run twice would hit.
- [x] **T284** Add `_refuse_already_answered(run)` beside it: 409 when `run.approval_decisions` is
  set. One helper rather than the check inlined four times, so the four cannot word it differently.

## Phase 2: the deciding paths

- [x] **T285** `release_covered_runs`: load through `_locked_run`. It already refuses an answered
  run, so the lock is all it was missing.
- [x] **T286** `batch_respond_to_approvals`: load through `_locked_run` and add
  `_refuse_already_answered` — it checked only the status, which cannot tell a fresh pause from an
  answered one.
- [x] **T287** `approve_by_tool`: keep the selection unlocked, then re-read each candidate through
  `_locked_run` and re-check status, decisions and the tool set before acting.
- [x] **T288** `respond_to_human_input`: add `_refuse_already_answered` after the status check. Its
  lock is already in place and has never been sufficient on its own.
  **Not `dismiss_run`.** Guarding it too was tried and reverted: a decision whose resume never
  lands (worker down, event dropped) leaves a run that cannot be answered *or* dismissed, and
  dismissal is the only way out. This is about not recording a second decision; dismissing is not
  a second decision.

## Phase 3: tests

- [x] **T291** Extend `test_the_pause_is_read_under_a_row_lock` to the bulk endpoints, accepting a
  lock taken through `_locked_run` so factoring the call out does not break the test protecting it.
- [x] **T292** Assert every deciding path consults `_refuse_already_answered`, and that
  `_answerable_pending_runs` never locks.
- [x] **T293** Drive `batch_respond_to_approvals` through a real session with a refused member
  followed by a good one, and assert both results come back. This is the regression test for the
  rollback described above — it fails with `MissingGreenlet` the moment a refusal ends the
  transaction, which no fake-session test can see.
- [x] **T294** New Postgres test (`integration`-marked, `tests/test_bulk_approval_race_postgres.py`):
  a second reader blocks until the first commits; an answered run still reads `awaiting_human` and
  is refused 409 by the guard; a refused run is immediately lockable again; the duplicate-id re-read
  sees the committed value. Verified by reverting the lock and watching them fail — run with
  `DATABASE_URL` exported, and read "skipped" as unverified.
- [x] **T295** Teach the fakes what the deciding paths now read: `scalar_one_or_none` on
  `test_approve_by_tool.py`'s `_DB` (the sweep re-reads by id), and `status` /
  `approval_decisions` on `test_approval_exposure_and_locking.py`'s `_Run`.

## Dependencies

- T283–T284 before T285–T288.
- T291–T295 last. T293 is the one that constrains Phase 2's implementation rather than checking it.
