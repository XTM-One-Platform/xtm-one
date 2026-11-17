"""Answering every paused run of a named tool, across pages (FR-040).

This is the only approval path that reaches past the rows a client loaded, and
it is safe for the same reason "Yes, always" is (FR-015): the reviewer judges a
**tool**, not each call's arguments. It is the weaker form of that decision —
it answers what is waiting and grants nothing durable.

The rule that keeps it honest is the mixed-run exclusion: a run cannot be
half-answered, so a run calling two tools is answerable only once both are
named. Approving it on one alone would sweep in a call nobody chose.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.api.v1 import assignments as api
from app.api.v1.schemas.assignment import ApproveByToolRequest

USER = SimpleNamespace(
    id=uuid.uuid4(), role="user", email="r@example.com", full_name="Reviewer"
)


def _run(*tools, owner=None, company=False):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status="awaiting_human",
        user_id=owner or USER.id,
        is_company_managed=company,
        triggered_by_user_id=owner or USER.id,
        human_input_question="Approval required",
        human_input_history=None,
        approval_decisions=None,
        pending_approvals=[
            {"tool_call_id": f"toolu_{i}", "tool_name": t, "input_schema": {}}
            for i, t in enumerate(tools)
        ],
    )


class _DB:
    def __init__(self, runs):
        self._runs = runs

    async def execute(self, stmt):
        rows = self._runs
        # Two shapes now: the unlocked selection, and the by-id re-read the
        # sweep does under ``FOR UPDATE`` before it answers a candidate.
        by_id = {str(r.id): r for r in rows}
        wanted = None
        try:
            for param in stmt.compile().params.values():
                if str(param) in by_id:
                    wanted = by_id[str(param)]
        except Exception:
            wanted = None
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: rows),
            scalar_one_or_none=lambda: wanted,
        )

    async def commit(self):
        pass


@pytest.fixture(autouse=True)
def _stub_pipeline(monkeypatch):
    resumed: list[str] = []

    monkeypatch.setattr(api, "_apply_decisions_to_run", lambda *a, **k: None)

    async def _grant(*_a, **_k):
        return None

    async def _resume(_db, run):
        resumed.append(str(run.id))

    monkeypatch.setattr(api, "_grant_standing_approvals", _grant)
    import app.services.assignment_engine as engine

    monkeypatch.setattr(engine, "resume_assignment_run", _resume)
    return resumed


# --- the aggregate ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_summary_groups_by_tool_and_counts_runs_and_calls():
    db = _DB([_run("generate_file"), _run("generate_file"), _run("web_fetch", "web_fetch")])

    out = await api.pending_approvals_by_tool(user=USER, db=db)

    by_name = {t.tool_name: t for t in out.tools}
    assert by_name["generate_file"].run_count == 2
    assert by_name["generate_file"].call_count == 2
    # One run, two calls of the same tool — runs and calls are not the same
    # number, and a control that says "approve N" must know which it means.
    assert by_name["web_fetch"].run_count == 1
    assert by_name["web_fetch"].call_count == 2


@pytest.mark.asyncio
async def test_a_run_mixing_two_tools_is_returned_as_its_own_combination():
    """Its calls belong to no single tool, so counting it under either would
    promise an approval that cannot happen. It is returned as the combination
    it is, because that IS what makes it answerable — reporting only how many
    such runs existed left the caller unable to reach them.
    """
    db = _DB([_run("generate_file"), _run("generate_file", "web_fetch")])

    out = await api.pending_approvals_by_tool(user=USER, db=db)

    assert {t.tool_name for t in out.tools} == {"generate_file"}
    assert next(t for t in out.tools).run_count == 1
    assert out.mixed_run_count == 1
    assert [g.tool_names for g in out.mixed] == [["generate_file", "web_fetch"]]
    assert out.mixed[0].run_count == 1
    assert out.mixed[0].call_count == 2


@pytest.mark.asyncio
async def test_the_summary_accounts_for_every_run_the_sweep_would_answer():
    """The arithmetic the confirmation is built from.

    With A-only, B-only and A+B paused, naming both answers three runs and four
    calls. A summary that reported the mixed run as a bare count let the dialog
    disclose two runs — understating the reach of the very decision it was
    asking consent for.
    """
    db = _DB(
        [_run("generate_file"), _run("web_fetch"), _run("generate_file", "web_fetch")]
    )

    out = await api.pending_approvals_by_tool(user=USER, db=db)

    singles = {t.tool_name: t for t in out.tools}
    covered = [
        g for g in out.mixed if set(g.tool_names) <= {"generate_file", "web_fetch"}
    ]
    runs = sum(t.run_count for t in singles.values()) + sum(
        g.run_count for g in covered
    )
    calls = sum(t.call_count for t in singles.values()) + sum(
        g.call_count for g in covered
    )
    assert (runs, calls) == (3, 4)


@pytest.mark.asyncio
async def test_a_tool_seen_only_in_mixed_runs_is_still_reachable():
    """It has no single-tool run, so it never reaches ``tools`` — and while the
    combinations were reported as a count alone there was nowhere else for it
    to appear either, leaving it impossible to select at all.
    """
    db = _DB([_run("generate_file"), _run("generate_file", "web_fetch")])

    out = await api.pending_approvals_by_tool(user=USER, db=db)

    assert "web_fetch" not in {t.tool_name for t in out.tools}
    assert "web_fetch" in {n for g in out.mixed for n in g.tool_names}


@pytest.mark.asyncio
async def test_runs_of_one_combination_are_grouped_whatever_the_call_order():
    """Grouped by combination rather than per run, so the payload is bounded by
    how many distinct combinations are paused, not by how many runs are."""
    db = _DB(
        [
            _run("generate_file", "web_fetch"),
            _run("web_fetch", "generate_file"),
            _run("store_memory", "web_fetch"),
        ]
    )

    out = await api.pending_approvals_by_tool(user=USER, db=db)

    assert [(g.tool_names, g.run_count) for g in out.mixed] == [
        (["generate_file", "web_fetch"], 2),
        (["store_memory", "web_fetch"], 1),
    ]
    assert out.mixed_run_count == 3


@pytest.mark.asyncio
async def test_a_proposal_with_no_tool_name_is_in_neither_bucket():
    """``approve_by_tool`` compares the whole set, so such a run is answerable
    at no selection. Listing it as a combination would promise a release that
    cannot happen."""
    run = _run("generate_file", "web_fetch")
    run.pending_approvals[1]["tool_name"] = None

    out = await api.pending_approvals_by_tool(user=USER, db=_DB([run]))

    assert out.tools == []
    assert out.mixed == []
    assert out.mixed_run_count == 0


@pytest.mark.asyncio
async def test_a_run_the_caller_cannot_answer_is_not_counted():
    """A company-managed run is visible to everyone but answerable only by an
    administrator. Counting it would offer a reviewer work it would refuse."""
    db = _DB([_run("generate_file"), _run("generate_file", owner=uuid.uuid4(), company=True)])

    out = await api.pending_approvals_by_tool(user=USER, db=db)

    assert next(t for t in out.tools).run_count == 1


# --- answering -------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_runs_whose_calls_are_all_named_are_answered(_stub_pipeline):
    only_a, mixed, other = _run("generate_file"), _run("generate_file", "web_fetch"), _run("store_memory")
    db = _DB([only_a, mixed, other])

    out = await api.approve_by_tool(
        body=ApproveByToolRequest(tool_names=["generate_file"], decision="approve"),
        user=USER,
        db=db,
    )

    assert {r.run_id for r in out.results} == {str(only_a.id)}
    assert _stub_pipeline == [str(only_a.id)]


@pytest.mark.asyncio
async def test_naming_both_tools_makes_the_mixed_run_answerable(_stub_pipeline):
    """The reason selection is multiple: a run is covered once every tool it
    calls has been named, and stays paused until then."""
    mixed = _run("generate_file", "web_fetch")
    db = _DB([mixed])

    out = await api.approve_by_tool(
        body=ApproveByToolRequest(
            tool_names=["generate_file", "web_fetch"], decision="approve"
        ),
        user=USER,
        db=db,
    )

    assert _stub_pipeline == [str(mixed.id)]
    # One decision per call, not per run.
    assert len(out.results) == 2


@pytest.mark.asyncio
async def test_a_company_managed_run_is_not_swept_in_for_a_non_admin(_stub_pipeline):
    theirs = _run("generate_file", owner=uuid.uuid4(), company=True)
    db = _DB([_run("generate_file"), theirs])

    out = await api.approve_by_tool(
        body=ApproveByToolRequest(tool_names=["generate_file"], decision="approve"),
        user=USER,
        db=db,
    )

    assert str(theirs.id) not in {r.run_id for r in out.results}
    assert str(theirs.id) not in _stub_pipeline


@pytest.mark.asyncio
async def test_naming_no_tool_is_refused(_stub_pipeline):
    """An empty list would otherwise mean "every run whose tools are a subset of
    nothing" — vacuously nothing today, and one refactor from meaning all."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await api.approve_by_tool(
            body=ApproveByToolRequest(tool_names=[], decision="approve"),
            user=USER,
            db=_DB([_run("generate_file")]),
        )
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_a_rejection_carries_its_reason_to_every_call(_stub_pipeline, monkeypatch):
    seen: list = []
    monkeypatch.setattr(api, "_apply_decisions_to_run", lambda run, ds, u: seen.extend(ds))

    await api.approve_by_tool(
        body=ApproveByToolRequest(
            tool_names=["generate_file"], decision="reject", rejection_reason="wrong scope"
        ),
        user=USER,
        db=_DB([_run("generate_file", "generate_file")]),
    )

    assert len(seen) == 2
    assert all(d.decision == "reject" and d.rejection_reason == "wrong scope" for d in seen)


# --- "always", named by the reviewer ---------------------------------------


@pytest.mark.asyncio
async def test_approve_always_by_tool_grants_through_the_shared_helper(monkeypatch):
    """FR-040e. The clearest-consented form of "always" in the product: FR-032b
    asks the disclosure to enumerate every tool being un-gated, and here the
    reviewer named the list themselves.

    What must not happen is a per-run grant: forty runs of one tool are one
    exception, not forty. `grant_exception` reuses an active row, and this test
    pins that the by-tool path goes through it rather than growing its own.
    """
    granted: list[str] = []

    async def _grant(_db, run, decisions, _user):
        granted.extend(d.decision for d in decisions)

    monkeypatch.setattr(api, "_grant_standing_approvals", _grant)

    out = await api.approve_by_tool(
        body=ApproveByToolRequest(
            tool_names=["generate_file"], decision="approve_always"
        ),
        user=USER,
        db=_DB([_run("generate_file"), _run("generate_file")]),
    )

    assert [r.status for r in out.results] == ["applied", "applied"]
    assert granted == ["approve_always", "approve_always"], (
        "the verdict must reach the shared grant helper unchanged — silently "
        "downgrading it would tell the reviewer they got the durable thing"
    )


@pytest.mark.asyncio
async def test_always_still_respects_the_mixed_run_rule(_stub_pipeline):
    """Widening the verdict must not widen what it reaches."""
    mixed = _run("generate_file", "web_fetch")
    out = await api.approve_by_tool(
        body=ApproveByToolRequest(
            tool_names=["generate_file"], decision="approve_always"
        ),
        user=USER,
        db=_DB([mixed]),
    )
    assert out.results == []
    assert _stub_pipeline == []


# --- answered, but not yet resumed ----------------------------------------


@pytest.mark.asyncio
async def test_a_run_already_answered_is_not_counted_again():
    """`resume_assignment_run` emits an outbox event and commits; it does not
    change the status. So a run stays `awaiting_human` until the worker picks
    it up, and counting it would tell the reviewer something needs them when
    what it needs is a queue drain — the count settling only whenever the
    worker got round to it."""
    answered = _run("generate_file")
    answered.approval_decisions = [{"tool_call_id": "toolu_0", "decision": "approve"}]
    db = _DB([_run("generate_file"), answered])

    out = await api.pending_approvals_by_tool(user=USER, db=db)

    assert sum(t.run_count for t in out.tools) == 1


@pytest.mark.asyncio
async def test_an_answered_run_is_not_answered_a_second_time(_stub_pipeline):
    answered = _run("generate_file")
    answered.approval_decisions = [{"tool_call_id": "toolu_0", "decision": "approve"}]

    out = await api.approve_by_tool(
        body=ApproveByToolRequest(tool_names=["generate_file"], decision="approve"),
        user=USER,
        db=_DB([answered]),
    )

    assert out.results == []
    assert _stub_pipeline == []



# --- agent scoping ---------------------------------------------------------


class _CapturingDB(_DB):
    """Keeps the statement so the WHERE clause can be inspected."""

    def __init__(self, runs):
        super().__init__(runs)
        self.statements: list = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return await super().execute(stmt)


def _compiled(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


@pytest.mark.asyncio
async def test_the_summary_counts_only_the_named_agent():
    agent_id = uuid.uuid4()
    db = _CapturingDB([_run("generate_file")])

    await api.pending_approvals_by_tool(agent_id=agent_id, user=USER, db=db)

    sql = _compiled(db.statements[0])
    assert f"assignment_runs.agent_id = '{agent_id.hex}'" in sql


@pytest.mark.asyncio
async def test_the_summary_stays_global_when_no_agent_is_named():
    db = _CapturingDB([_run("generate_file")])

    await api.pending_approvals_by_tool(user=USER, db=db)

    # ``agent_id`` is a selected column, so only the predicate answers this.
    assert "assignment_runs.agent_id = " not in _compiled(db.statements[0])


@pytest.mark.asyncio
async def test_the_sweep_is_scoped_too_not_only_the_count(_stub_pipeline):
    """The filter has to reach the decision, not just the total: a control
    offered on one agent's page that resumed another agent's runs would act
    outside the scope it displays."""
    agent_id = uuid.uuid4()
    db = _CapturingDB([_run("generate_file")])

    await api.approve_by_tool(
        ApproveByToolRequest(
            tool_names=["generate_file"], decision="approve", agent_id=agent_id
        ),
        user=USER,
        db=db,
    )

    sql = _compiled(db.statements[0])
    assert f"assignment_runs.agent_id = '{agent_id.hex}'" in sql


# --- own-runs scoping (FR-043) --------------------------------------

ADMIN = SimpleNamespace(
    id=uuid.uuid4(), role="admin", email="a@example.com", full_name="Admin"
)


@pytest.mark.asyncio
async def test_the_own_runs_scope_narrows_an_admins_answerable_set():
    """An admin may answer company-managed runs belonging to other people, and
    their allow-all switch reaches none of them. The offer beside that switch
    must not go further than the switch does."""
    db = _CapturingDB([_run("generate_file")])

    await api.pending_approvals_by_tool(own_only=True, user=ADMIN, db=db)

    sql = _compiled(db.statements[0])
    # Twice: once in the answerable disjunction, once as the added predicate
    # that makes somebody else's company-managed run insufficient.
    assert sql.count(f"assignment_runs.user_id = '{ADMIN.id.hex}'") == 2


@pytest.mark.asyncio
async def test_without_the_own_runs_scope_an_admin_still_sees_company_runs():
    db = _CapturingDB([_run("generate_file")])

    await api.pending_approvals_by_tool(user=ADMIN, db=db)

    sql = _compiled(db.statements[0])
    assert sql.count(f"assignment_runs.user_id = '{ADMIN.id.hex}'") == 1
    assert "is_company_managed" in sql


@pytest.mark.asyncio
async def test_the_own_runs_scope_reaches_the_sweep_too(_stub_pipeline):
    """Same reason as the agent scope: the count is not what acts."""
    db = _CapturingDB([_run("generate_file")])

    await api.approve_by_tool(
        ApproveByToolRequest(
            tool_names=["generate_file"], decision="approve", own_only=True
        ),
        user=ADMIN,
        db=db,
    )

    sql = _compiled(db.statements[0])
    assert sql.count(f"assignment_runs.user_id = '{ADMIN.id.hex}'") == 2
