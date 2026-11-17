"""Answering approvals across several runs in one action (FR-031–FR-034).

The batch is a convenience over N independent decisions and must stay exactly
that: same authorisation per member, same applier, same resume, no batch-level
record, and no rollback when one member fails. Partial success is the expected
outcome, not an error state — these tests exist mostly to keep it that way,
because "make the whole thing atomic" is the obvious-looking change that would
break FR-033.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.api.v1 import assignments as api
from app.api.v1.schemas.assignment import BatchApprovalDecision, BatchApprovalRequest

USER = SimpleNamespace(
    id=uuid.uuid4(), role="user", email="r@example.com", full_name="Reviewer"
)

#: The real applier, captured before the autouse fixture stubs it.
_real_applier = api._apply_decisions_to_run


def _run(run_id, *, status="awaiting_human", calls=("toolu_1",)):
    return SimpleNamespace(
        id=run_id,
        status=status,
        user_id=USER.id,
        # `require_run_access` reads these: a private run answered by its owner
        # is the shape under test, so the real gate runs rather than a stub.
        is_company_managed=False,
        triggered_by_user_id=USER.id,
        human_input_question="Approval required",
        # The real applier writes the durable resolution trail.
        human_input_history=None,
        approval_decisions=None,
        pending_approvals=[
            {"tool_call_id": c, "tool_name": "generate_file"} for c in calls
        ],
    )


class _DB:
    """Returns whichever run the query asked for, by id."""

    def __init__(self, runs):
        self._runs = {str(r.id): r for r in runs}
        self.committed = False

    async def execute(self, stmt):
        wanted = str(stmt.whereclause.right.value)
        run = self._runs.get(wanted)
        return SimpleNamespace(scalar_one_or_none=lambda: run)

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        pass


@pytest.fixture(autouse=True)
def _stub_pipeline(monkeypatch):
    """Compose the real sequence, observed rather than executed."""
    calls = {"applied": [], "granted": [], "resumed": []}

    def _apply(run, decisions, user):
        calls["applied"].append((str(run.id), [d.tool_call_id for d in decisions]))

    async def _grant(db, run, decisions, user):
        calls["granted"].append(str(run.id))

    async def _resume(db, run):
        calls["resumed"].append(str(run.id))

    monkeypatch.setattr(api, "_apply_decisions_to_run", _apply)
    monkeypatch.setattr(api, "_grant_standing_approvals", _grant)
    import app.services.assignment_engine as engine

    monkeypatch.setattr(engine, "resume_assignment_run", _resume)
    return calls


def _decide(run_id, call_id="toolu_1", decision="approve"):
    return BatchApprovalDecision(
        run_id=str(run_id), tool_call_id=call_id, decision=decision
    )


@pytest.mark.asyncio
async def test_every_member_is_applied_and_resumed_independently(_stub_pipeline):
    a, b = uuid.uuid4(), uuid.uuid4()
    db = _DB([_run(a), _run(b)])

    out = await api.batch_respond_to_approvals(
        body=BatchApprovalRequest(decisions=[_decide(a), _decide(b)]),
        user=USER,
        db=db,
    )

    assert [r.status for r in out.results] == ["applied", "applied"]
    assert sorted(_stub_pipeline["resumed"]) == sorted([str(a), str(b)])
    # One decision per member, never one blanket decision (FR-032).
    assert len(_stub_pipeline["applied"]) == 2


@pytest.mark.asyncio
async def test_a_member_answered_elsewhere_does_not_roll_back_the_others(
    _stub_pipeline,
):
    """FR-033: partial success is a success. Rolling back the ones that landed
    would punish the reviewer for a race they did not cause."""
    a, gone = uuid.uuid4(), uuid.uuid4()
    db = _DB([_run(a), _run(gone, status="completed")])

    out = await api.batch_respond_to_approvals(
        body=BatchApprovalRequest(decisions=[_decide(a), _decide(gone)]),
        user=USER,
        db=db,
    )

    by_run = {r.run_id: r for r in out.results}
    assert by_run[str(a)].status == "applied"
    assert by_run[str(gone)].status == "refused"
    assert by_run[str(gone)].reason == "already_answered"
    assert _stub_pipeline["resumed"] == [str(a)], "the survivor still resumed"


@pytest.mark.asyncio
async def test_a_missing_run_is_reported_not_raised(_stub_pipeline):
    a = uuid.uuid4()
    db = _DB([_run(a)])
    out = await api.batch_respond_to_approvals(
        body=BatchApprovalRequest(decisions=[_decide(a), _decide(uuid.uuid4())]),
        user=USER,
        db=db,
    )
    assert {r.status for r in out.results} == {"applied", "refused"}
    assert any(r.reason == "run_not_found" for r in out.results)


@pytest.mark.asyncio
async def test_a_run_the_caller_cannot_reach_is_refused_alone(_stub_pipeline, monkeypatch):
    """FR-028: bundling a run with ones you own must not grant reach over it."""
    from fastapi import HTTPException

    mine, theirs = uuid.uuid4(), uuid.uuid4()

    def _access(run, user, action):
        if str(run.id) == str(theirs):
            raise HTTPException(status_code=403, detail="Not yours")

    monkeypatch.setattr(api, "require_run_access", _access)
    db = _DB([_run(mine), _run(theirs)])

    out = await api.batch_respond_to_approvals(
        body=BatchApprovalRequest(decisions=[_decide(mine), _decide(theirs)]),
        user=USER,
        db=db,
    )

    by_run = {r.run_id: r for r in out.results}
    assert by_run[str(mine)].status == "applied"
    assert by_run[str(theirs)].reason == "forbidden"
    assert _stub_pipeline["resumed"] == [str(mine)]


@pytest.mark.asyncio
async def test_a_run_with_an_undecided_call_is_refused_whole(_stub_pipeline):
    """The engine refuses a partial set — an undecided call leaves a `tool_use`
    block with no `tool_result`, which every provider rejects. Caught here so
    one under-specified run does not fail the batch."""
    a = uuid.uuid4()
    db = _DB([_run(a, calls=("toolu_1", "toolu_2"))])

    out = await api.batch_respond_to_approvals(
        body=BatchApprovalRequest(decisions=[_decide(a, "toolu_1")]),
        user=USER,
        db=db,
    )

    assert out.results[0].reason == "incomplete_decisions"
    assert _stub_pipeline["resumed"] == []


@pytest.mark.asyncio
async def test_approve_always_grants_through_the_one_shared_helper(_stub_pipeline):
    """FR-032 as revised on 2026-09-02: the durable choice is reachable from a
    batch, and grants through the same helper the run page uses.

    What must NOT happen is a batch growing its own grant: an exception that
    meant something different depending on which surface created it would break
    FR-038b's "same act performed elsewhere". The disclosure obligation moved to
    the caller (it must enumerate the tools first) rather than being dropped —
    that half is a UI guarantee and is asserted where the UI lives."""
    a = uuid.uuid4()
    db = _DB([_run(a)])

    out = await api.batch_respond_to_approvals(
        body=BatchApprovalRequest(
            decisions=[_decide(a, decision="approve_always")]
        ),
        user=USER,
        db=db,
    )

    assert out.results[0].status == "applied"
    assert _stub_pipeline["granted"] == [str(a)], (
        "the standing approval must come from the shared helper, not a "
        "batch-local copy"
    )


@pytest.mark.asyncio
async def test_a_batch_still_records_one_decision_per_member(_stub_pipeline):
    """Widening the verdict must not widen the accounting: three members remain
    three decisions and three audit entries, never one blanket grant."""
    a, b = uuid.uuid4(), uuid.uuid4()
    db = _DB([_run(a), _run(b)])
    out = await api.batch_respond_to_approvals(
        body=BatchApprovalRequest(
            decisions=[_decide(a, decision="approve_always"), _decide(b)]
        ),
        user=USER,
        db=db,
    )
    assert len(out.results) == 2
    assert len(_stub_pipeline["applied"]) == 2


@pytest.mark.asyncio
async def test_the_batch_composes_the_single_run_path(_stub_pipeline):
    """FR-029/FR-038b: the grant helper is called even though it is a no-op
    here, so the batch stays the same sequence as the single-run endpoint
    rather than a parallel one that can drift."""
    a = uuid.uuid4()
    db = _DB([_run(a)])
    await api.batch_respond_to_approvals(
        body=BatchApprovalRequest(decisions=[_decide(a)]), user=USER, db=db
    )
    assert _stub_pipeline["granted"] == [str(a)]


# --- The applier's own objections are reported, not fatal (FR-033) --------


@pytest.mark.asyncio
async def test_an_applier_rejection_refuses_that_member_and_spares_the_rest(monkeypatch):
    """Every other test here stubs `_apply_decisions_to_run`, which is exactly
    how an unguarded call escaped review: the applier raises on sets the
    pre-screen does not check — unknown ids, duplicates, a free-text pause with
    no proposals. Unguarded, that aborted the request with a bare 4xx *after*
    earlier members had already resumed and committed.

    This test therefore uses the REAL applier for the bad member.
    """
    good, bad = uuid.uuid4(), uuid.uuid4()
    # `bad` is a free-text pause: awaiting_human, but nothing to decide.
    bad_run = _run(bad)
    bad_run.pending_approvals = []
    db = _DB([_run(good), bad_run])

    resumed: list = []

    async def _resume(_db, run):
        resumed.append(str(run.id))

    import app.services.assignment_engine as engine

    monkeypatch.setattr(engine, "resume_assignment_run", _resume)
    monkeypatch.setattr(api, "_apply_decisions_to_run", _real_applier)

    out = await api.batch_respond_to_approvals(
        body=BatchApprovalRequest(decisions=[_decide(good), _decide(bad)]),
        user=USER,
        db=db,
    )

    by_run = {r.run_id: r for r in out.results}
    assert by_run[str(good)].status == "applied"
    assert by_run[str(bad)].status == "refused"
    assert by_run[str(bad)].reason == "rejected_by_applier"
    assert resumed == [str(good)], "the healthy member must still have resumed"


@pytest.mark.asyncio
async def test_a_stale_client_sending_an_unknown_call_id_is_reported(monkeypatch):
    """The pre-screen only catches *missing* ids. A run that re-paused with a
    different set leaves a stale client sending one the run does not have."""
    run_id = uuid.uuid4()
    db = _DB([_run(run_id, calls=("toolu_1",))])

    async def _resume(_db, _run):
        return None

    import app.services.assignment_engine as engine

    monkeypatch.setattr(engine, "resume_assignment_run", _resume)
    monkeypatch.setattr(api, "_apply_decisions_to_run", _real_applier)

    out = await api.batch_respond_to_approvals(
        body=BatchApprovalRequest(
            decisions=[_decide(run_id, "toolu_1"), _decide(run_id, "toolu_stale")]
        ),
        user=USER,
        db=db,
    )
    assert {r.status for r in out.results} == {"refused"}
    assert {r.reason for r in out.results} == {"rejected_by_applier"}


# --- Two runs, one tool, one durable consequence --------------------------


@pytest.mark.asyncio
async def test_approve_always_on_two_runs_of_the_same_tool_grants_once(monkeypatch):
    """A batch can hold several runs paused on the SAME tool, and each one
    calls `grant_exception` for the same `(user, tool_key)`.

    Exactly one preference row must result. It does today because
    `grant_exception` flushes the insert before auditing it — the audit needs
    the row's id — and that flush is what lets the second lookup see the first.
    Without it the second insert collides with the partial unique index
    `uq_tool_approval_preferences_active`, which raises `IntegrityError`, not
    `HTTPException`: the batch guard would not catch it and the request would
    500 *after* the first run had already resumed.

    So the correctness of this case rests on a flush added for an unrelated
    reason. That is worth a test of its own — reorder those two lines and
    nothing else in the suite notices.
    """
    from app.services import tool_approval_service as svc

    granted: list[tuple] = []
    flushed: list[str] = []

    class _Session:
        """Mimics the visibility rule that matters: a lookup sees a pending row
        only once it has been flushed."""

        def __init__(self):
            self._pending: list = []
            self._visible: list = []

        async def scalar(self, _stmt):
            return self._visible[0] if self._visible else None

        def add(self, obj):
            self._pending.append(obj)

        async def flush(self):
            flushed.append("flush")
            self._visible.extend(self._pending)
            self._pending.clear()

    session = _Session()

    async def _audit(*_a, **_k):
        return None

    monkeypatch.setattr(svc, "_audit_preference_change", _audit)

    user_id, tool = uuid.uuid4(), "generate_file"
    for _ in range(2):
        pref = await svc.grant_exception(
            session,
            user_id=user_id,
            tool_key=tool,
            tool_source="builtin",
            schema_fingerprint="fp-1",
        )
        granted.append(pref)

    assert flushed, "the insert must be flushed, or the second lookup is blind"
    assert granted[0] is granted[1], (
        "the second grant created a second row for the same (user, tool) — "
        "that collides with uq_tool_approval_preferences_active at commit"
    )

