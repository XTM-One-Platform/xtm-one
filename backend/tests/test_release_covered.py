"""Coverage is decided by the server, not claimed by the caller (FR-039b).

A run qualifies for release only when **every** one of its pending calls is
covered by a standing approval. One covered and one uncovered call means the run
is answered by hand — releasing it would approve a call the reviewer never
consented to, which is FR-006 defeated by arithmetic.

The rule used to live in the page, which sent the resulting per-call decisions
to the batch endpoint. The batch cannot check it: it is *told* which calls to
answer, so a decision naming a partially covered run looks like any other
approval. These tests exercise the endpoint that decides instead of being told.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.api.v1 import assignments as api
from app.api.v1.schemas.assignment import ReleaseCoveredRequest
from app.tools.approval import fingerprint

#: The shape the proposals below are made with. Coverage compares fingerprints,
#: not names, so a test that wants a real standing approval has to consent to
#: this exact schema.
SCHEMA = {"type": "object", "properties": {"to": {"type": "string"}}}

USER = SimpleNamespace(
    id=uuid.uuid4(), role="user", email="r@example.com", full_name="Reviewer"
)


def _run(
    *tools, owner=None, company=False, answered=False, source=None, entity_id=None
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status="awaiting_human",
        user_id=owner or USER.id,
        is_company_managed=company,
        triggered_by_user_id=owner or USER.id,
        human_input_question="Approval required",
        human_input_history=None,
        approval_decisions={"x": "y"} if answered else None,
        pending_approvals=[
            {
                "tool_call_id": f"call_{i}",
                "tool_name": t,
                "input_schema": SCHEMA,
                **({"source": source} if source else {}),
                **({"tool_entity_id": entity_id} if entity_id else {}),
            }
            for i, t in enumerate(tools)
        ],
    )


class _DB:
    """Returns whichever run the endpoint asks for, by id."""

    def __init__(self, runs):
        self._by_id = {str(r.id): r for r in runs}

    async def execute(self, stmt):
        wanted = None
        for param in stmt.compile().params.values():
            if str(param) in self._by_id:
                wanted = self._by_id[str(param)]
        return SimpleNamespace(scalar_one_or_none=lambda: wanted)

    async def commit(self):
        pass


@pytest.fixture(autouse=True)
def _stub_pipeline(monkeypatch):
    applied: list = []
    monkeypatch.setattr(
        api, "_apply_decisions_to_run", lambda run, ds, user: applied.append((run, ds))
    )

    async def _resume(_db, run):
        return None

    import app.services.assignment_engine as engine

    monkeypatch.setattr(engine, "resume_assignment_run", _resume)
    return applied


def _cover(
    monkeypatch,
    *tools,
    schema=SCHEMA,
    for_user=None,
    granted_to=None,
    granted_on=None,
):
    """Give ``for_user`` (default: the caller) a standing approval per tool.

    ``schema`` is what was consented to — pass a different one to model a tool
    that has changed shape since. ``granted_to`` is the tool source it was
    granted for, ``granted_on`` the entity id (an MCP tool's server).
    """
    seen: list = []

    async def _active(_db, user_id, *, sources=None, entity_ids=None):
        seen.append(user_id)
        if for_user is not None and user_id != for_user:
            return {}
        if sources is not None and granted_to:
            sources.update({t: granted_to for t in tools})
        if entity_ids is not None and granted_on:
            entity_ids.update({t: granted_on for t in tools})
        return {t: fingerprint(schema) for t in tools}

    import app.services.tool_approval_service as svc

    monkeypatch.setattr(svc, "list_active_exceptions", _active)
    return seen


@pytest.mark.asyncio
async def test_an_approval_for_another_tool_of_the_same_name_releases_nothing(
    monkeypatch, _stub_pipeline
):
    """A custom tool that copied an integration tool's name and schema."""
    run = _run("send_email", source="custom:t9")
    _cover(monkeypatch, "send_email", granted_to="integration:gmail")

    res = await api.release_covered_runs(
        ReleaseCoveredRequest(run_ids=[str(run.id)]), user=USER, db=_DB([run])
    )

    assert {r.reason for r in res.results} == {"not_fully_covered"}
    assert not _stub_pipeline


@pytest.mark.asyncio
async def test_an_approval_granted_to_the_custom_tool_itself_releases_its_run(
    monkeypatch, _stub_pipeline
):
    run = _run("send_email", source="custom:t9")
    _cover(monkeypatch, "send_email", granted_to="custom:t9")

    res = await api.release_covered_runs(
        ReleaseCoveredRequest(run_ids=[str(run.id)]), user=USER, db=_DB([run])
    )

    assert {r.status for r in res.results} == {"applied"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("granted_to", "granted_on", "status"),
    [
        ("mcp:Wiki", "srv-1", "applied"),
        # Another server took the same name: its id is not this one.
        ("mcp:Wiki", "srv-2", "refused"),
        # Granted before the server id was recorded.
        ("mcp:Wiki", None, "refused"),
        # The built-in the MCP tool copied its name and schema from.
        ("builtin", None, "refused"),
    ],
)
async def test_an_mcp_call_is_released_only_by_an_approval_on_its_server(
    monkeypatch, _stub_pipeline, granted_to, granted_on, status
):
    run = _run("search_wiki", source="mcp:Wiki", entity_id="srv-1")
    _cover(
        monkeypatch, "search_wiki", granted_to=granted_to, granted_on=granted_on
    )

    res = await api.release_covered_runs(
        ReleaseCoveredRequest(run_ids=[str(run.id)]), user=USER, db=_DB([run])
    )

    assert [r.status for r in res.results] == [status]
    assert len(_stub_pipeline) == (1 if status == "applied" else 0)


@pytest.mark.asyncio
async def test_a_fully_covered_run_is_released(monkeypatch, _stub_pipeline):
    run = _run("send_email", "send_email")
    _cover(monkeypatch, "send_email")

    res = await api.release_covered_runs(
        ReleaseCoveredRequest(run_ids=[str(run.id)]), user=USER, db=_DB([run])
    )

    assert [r.status for r in res.results] == ["applied", "applied"]
    assert _stub_pipeline, "the decisions must actually be applied"


@pytest.mark.asyncio
async def test_one_uncovered_call_refuses_the_whole_run(monkeypatch, _stub_pipeline):
    """The rule the page could assert but not enforce."""
    run = _run("send_email", "delete_everything")
    _cover(monkeypatch, "send_email")

    res = await api.release_covered_runs(
        ReleaseCoveredRequest(run_ids=[str(run.id)]), user=USER, db=_DB([run])
    )

    assert {r.status for r in res.results} == {"refused"}
    assert {r.reason for r in res.results} == {"not_fully_covered"}
    assert "delete_everything" in res.results[0].detail
    assert not _stub_pipeline, "nothing may be applied to a partially covered run"


@pytest.mark.asyncio
async def test_the_refusal_names_every_call_that_did_not_move(
    monkeypatch, _stub_pipeline
):
    run = _run("a", "b", "c")
    _cover(monkeypatch, "a")

    res = await api.release_covered_runs(
        ReleaseCoveredRequest(run_ids=[str(run.id)]), user=USER, db=_DB([run])
    )

    assert {r.tool_call_id for r in res.results} == {"call_0", "call_1", "call_2"}


@pytest.mark.asyncio
async def test_a_run_with_no_approvals_at_all_is_refused(monkeypatch, _stub_pipeline):
    """An empty ``covered`` set must not vacuously satisfy "every call"."""
    run = _run("send_email")
    _cover(monkeypatch)

    res = await api.release_covered_runs(
        ReleaseCoveredRequest(run_ids=[str(run.id)]), user=USER, db=_DB([run])
    )

    assert {r.reason for r in res.results} == {"not_fully_covered"}


@pytest.mark.asyncio
async def test_a_run_the_caller_cannot_answer_refuses_on_its_own(
    monkeypatch, _stub_pipeline
):
    """Per member, never per batch: bundling a run with ones the caller owns
    must not grant reach over it (FR-028)."""
    theirs = _run("send_email", owner=uuid.uuid4(), company=True)
    mine = _run("send_email")
    _cover(monkeypatch, "send_email")

    res = await api.release_covered_runs(
        ReleaseCoveredRequest(run_ids=[str(theirs.id), str(mine.id)]),
        user=USER,
        db=_DB([theirs, mine]),
    )

    by_run = {r.run_id: r for r in res.results}
    assert by_run[str(theirs.id)].reason == "forbidden"
    assert by_run[str(mine.id)].status == "applied"


@pytest.mark.asyncio
async def test_an_already_answered_run_is_not_answered_again(
    monkeypatch, _stub_pipeline
):
    run = _run("send_email", answered=True)
    _cover(monkeypatch, "send_email")

    res = await api.release_covered_runs(
        ReleaseCoveredRequest(run_ids=[str(run.id)]), user=USER, db=_DB([run])
    )

    assert {r.reason for r in res.results} == {"already_answered"}
    assert not _stub_pipeline


@pytest.mark.asyncio
async def test_one_run_failing_does_not_lose_the_ones_that_landed(
    monkeypatch, _stub_pipeline
):
    """Partial success is a success (FR-033), and resume commits per run.

    An escape here would abort the request *after* earlier runs had already
    resumed, so the caller would be told nothing about the ones that went
    through — the same reasoning the batch endpoint records.
    """
    first, boom, last = _run("send_email"), _run("send_email"), _run("send_email")
    _cover(monkeypatch, "send_email")

    async def _resume(_db, run):
        if run.id == boom.id:
            raise RuntimeError("orchestration is down")

    import app.services.assignment_engine as engine

    monkeypatch.setattr(engine, "resume_assignment_run", _resume)

    res = await api.release_covered_runs(
        ReleaseCoveredRequest(
            run_ids=[str(first.id), str(boom.id), str(last.id)]
        ),
        user=USER,
        db=_DB([first, boom, last]),
    )

    by_run = {r.run_id: r for r in res.results}
    assert by_run[str(first.id)].status == "applied"
    assert by_run[str(last.id)].status == "applied"
    assert by_run[str(boom.id)].reason == "rejected_by_applier"


@pytest.mark.asyncio
async def test_a_malformed_proposal_refuses_that_run_only(monkeypatch, _stub_pipeline):
    """A missing or non-string tool name must not raise out of the loop."""
    bad = _run("send_email")
    bad.pending_approvals = [
        {"tool_call_id": "call_0", "tool_name": None},
        {"tool_call_id": "call_1"},
    ]
    _cover(monkeypatch, "send_email")

    res = await api.release_covered_runs(
        ReleaseCoveredRequest(run_ids=[str(bad.id)]), user=USER, db=_DB([bad])
    )

    assert {r.reason for r in res.results} == {"not_fully_covered"}


@pytest.mark.asyncio
async def test_a_standing_approval_for_an_older_shape_does_not_release(
    monkeypatch, _stub_pipeline
):
    """The gate compares fingerprints; so must the endpoint that pre-empts it.

    Releasing on the tool name alone reported "applied" for a run the gate then
    paused again, because consent no longer described the tool.
    """
    run = _run("send_email")
    _cover(monkeypatch, "send_email", schema={"type": "object", "properties": {}})

    res = await api.release_covered_runs(
        ReleaseCoveredRequest(run_ids=[str(run.id)]), user=USER, db=_DB([run])
    )

    assert [r.status for r in res.results] == ["refused"]
    assert res.results[0].reason == "not_fully_covered"
    assert not _stub_pipeline


@pytest.mark.asyncio
async def test_coverage_is_read_for_the_run_s_owner_not_the_caller(
    monkeypatch, _stub_pipeline
):
    """An admin answers a company-managed run they do not own. The gate will
    use the run's own user, so the caller's approvals cannot stand in."""
    admin = SimpleNamespace(
        id=uuid.uuid4(), role="admin", email="a@example.com", full_name="Admin"
    )
    owner = uuid.uuid4()
    run = _run("send_email", owner=owner, company=True)
    seen = _cover(monkeypatch, "send_email", for_user=owner)

    res = await api.release_covered_runs(
        ReleaseCoveredRequest(run_ids=[str(run.id)]), user=admin, db=_DB([run])
    )

    assert [r.status for r in res.results] == ["applied"]
    assert seen == [owner], "approvals were resolved for the wrong user"


@pytest.mark.asyncio
async def test_a_failed_approvals_lookup_does_not_lose_the_runs_that_landed(
    monkeypatch, _stub_pipeline
):
    """Partial success is a success (FR-033), including for this lookup.

    Resolving approvals moved inside the loop so each run is checked against
    its own owner's — which put a database read after earlier runs had already
    resumed and committed. An escape there returns a bare 500 and the caller
    never learns run 1 landed.
    """
    admin = SimpleNamespace(
        id=uuid.uuid4(), role="admin", email="a@example.com", full_name="Admin"
    )
    ok_owner, bad_owner = uuid.uuid4(), uuid.uuid4()
    # Two owners, because the lookup is memoised per owner — one owner would
    # resolve once and never reach the second run's read.
    first = _run("send_email", owner=ok_owner, company=True)
    boom = _run("send_email", owner=bad_owner, company=True)

    async def _active(_db, user_id, **_k):
        if user_id == bad_owner:
            raise RuntimeError("standing approvals unavailable")
        return {"send_email": fingerprint(SCHEMA)}

    import app.services.tool_approval_service as svc

    monkeypatch.setattr(svc, "list_active_exceptions", _active)

    res = await api.release_covered_runs(
        ReleaseCoveredRequest(run_ids=[str(first.id), str(boom.id)]),
        user=admin,
        db=_DB([first, boom]),
    )

    by_run = {r.run_id: r for r in res.results}
    assert by_run[str(first.id)].status == "applied"
    assert by_run[str(boom.id)].status == "refused"
    assert by_run[str(boom.id)].reason == "rejected_by_applier"
