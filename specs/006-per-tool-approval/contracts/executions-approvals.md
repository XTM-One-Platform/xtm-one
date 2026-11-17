# Contract: answering an approval from the executions list

Covers FR-026–FR-037b. Companion to [rest-api.md](./rest-api.md), which owns the run-page surface
these reuse.


**Mostly "nothing new".** The read path and the single-decision write path already exist and are
reused verbatim (FR-029). One endpoint is added, for the group action only.

---

## 1. Listing — **existing, unchanged**

`GET /api/v1/assignments/runs?notifications=true`

Already returns `awaiting_human` runs, and `AssignmentRunDetailResponse` already carries
`pending_approvals` and `human_input_question`. The executions list receives the proposals in a
payload it already fetches; FR-027's disclosure needs no second fetch.

`RunCard` already accepts an `onApprove` callback; the page passes it to render and answer
proposals inline.

---

## 2. `GET /api/v1/assignments/runs/{run_id}/pending-approvals` — **existing, unchanged**

The authoritative read for one run, used when the reviewer expands an item and the listed payload
is stale. Already 409s when the run is no longer `awaiting_human`, which is FR-030's refusal for
the single case.

---

## 3. `POST /api/v1/assignments/runs/{run_id}/respond` — **existing, unchanged**

The single decision, including "approve always". `body.decisions` already routes through
`_apply_decisions_to_run` → `_grant_standing_approvals` → `resume_assignment_run`, so FR-037's
standing exception is the same act with the same scope, invalidation and revocation (FR-037b) — no
new field, no variant.

**FR-030 obligation on the existing endpoint**: it currently answers one `400` for every
not-awaiting reason. It MUST distinguish them, because "someone already answered this" and "the run
was cancelled" call for different things from the reviewer:

| Condition | Status | `detail` must identify |
|---|---|---|
| Status is `running`, `completed` or `failed` | `409` | that the run is no longer awaiting, the status it reached, and that an answer elsewhere is the likely cause |
| Any other status | `400` | unchanged — a client error, not a race |

**Narrower than it may look.** The refusal cannot name the decision that won: the resume path clears
`pending_approvals`, `approval_decisions`, `human_input_question` and `human_input_response`, so an
answered run keeps no trace of having been paused. The only available signal is that the status is
one reachable from `awaiting_human` by resuming, which supports "most likely answered elsewhere"
and nothing stronger. Naming a decider would require new state, which
[data-model.md §10](../data-model.md) says this increment adds none of.

There is no `cancelled` run status — runs reach only `awaiting_human`, `running`, `completed`,
`failed`, `delayed` and `skipped`, and no cancel endpoint exists.

---

## 4. Filtering to what awaits a decision — **existing, unchanged**

FR-035 as revised is served by the executions list's own `status=awaiting_human` filter plus a
client-side shortcut selecting exactly the rows carrying proposals. No count endpoint is involved:
a badge would answer "is anything waiting?" but still leave the reviewer to find it.

---

## 5. `POST /api/v1/assignments/runs/approvals/batch` — **NEW** (FR-031, FR-033)

The only new endpoint. One action over an arbitrary selection spanning several runs.

**Request**

```json
{
  "decisions": [
    { "run_id": "uuid", "call_id": "toolu_01…", "decision": "approve" },
    { "run_id": "uuid", "call_id": "toolu_02…", "decision": "reject", "reason": "wrong scope" },
    { "run_id": "uuid", "call_id": "toolu_03…", "decision": "approve" }
  ]
}
```

`decision` is `approve`, `approve_always` or `reject`.

**`approve_always` carries a client obligation** (FR-032b): a client offering it over a group MUST
first name every distinct tool it would un-gate, say the effect reaches unattended runs, and say it
is revocable. This endpoint cannot enforce that, which is precisely why it is written down here.

The grant itself goes through the same helper the single-run endpoint uses, to the same identity,
with the same fingerprint (FR-032c) — a batch never grows a grant of its own.

**Several runs paused on the same tool grant once, not once per run.** The helper reuses an existing
active preference rather than inserting beside it; `uq_tool_approval_preferences_active`
(`user_id, tool_key` where `revoked_at IS NULL`) is the backstop. That backstop must never be
reached from inside a batch: it raises `IntegrityError`, not `HTTPException`, so the per-member
guard would not catch it and the request would fail after earlier members had already resumed.

**200** — partial success is a success (FR-033).

```json
{
  "results": [
    { "run_id": "uuid", "call_id": "toolu_01…", "status": "applied" },
    { "run_id": "uuid", "call_id": "toolu_02…", "status": "applied" },
    { "run_id": "uuid", "call_id": "toolu_03…", "status": "refused",
      "reason": "already_answered",
      "detail": "This run is no longer awaiting approval (now: completed)." }
  ]
}
```

`reason` is one of `already_answered`, `run_not_awaiting`, `run_not_found`, `forbidden`,
`incomplete_decisions` (the run has pending calls this batch did not decide) or
`rejected_by_applier` (the decision set was refused — an unknown or duplicate call id, or a
free-text pause with nothing to decide).

`rejected_by_applier` exists because the applier raises rather than returns. Letting that escape
would abort the request **after** earlier members had already resumed and committed, leaving the
caller a bare 4xx and no `results` — the partial-success contract broken for exactly the failures
this endpoint did not anticipate.

**Contract obligations**

- **Per member, not per batch.** Each decision is applied and audited exactly as the single-run
  endpoint would (FR-029, FR-032). There is no batch-level audit record, because the group is not a
  thing that happened — the decisions are.
- **No rollback.** A refused member MUST NOT undo an applied one (FR-033). The batch is a
  convenience over N independent decisions, and they were independent before it existed.
- **Every member is authorised individually** through `require_run_access` (FR-028), with the
  discrepancy recorded in [research.md §D14](../research.md). A caller MUST NOT gain reach over a
  run by bundling it with runs they do own.
- **Ordering is not promised.** Runs resume independently; the response is not a sequence.
- **Idempotent under retry.** A replayed batch answers `already_answered` for what landed rather
  than recording a second decision (SC-023).

---

**Scope is what the client sends.** There is no filter-based variant: a batch answers the runs it
names, and nothing else. A surface offering a "select all N matching the filters" mode therefore
MUST NOT present approval actions while that mode is armed — its selection spans runs the client
has not loaded and cannot enumerate, so the buttons would silently act on a subset of what the
reviewer believes is selected.

---

## 6. Not in this contract

- **Read-tracking (FR-034)** is client-side and per sitting — no field, no endpoint. See
  [research.md §D15](../research.md).
- **The Approval Group** is transient and never stored, so it has no representation here beyond the
  request body above.
- **Notification delivery** is unchanged; FR-015d already decides who is told and when.
