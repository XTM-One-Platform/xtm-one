"""Tests for /api/v1/improvement-proposals (#8256, the #1044 routes generalized).

The rules are the service's (``test_persona_improvement.py``,
``test_improvement_proposals.py`` and the PostgreSQL suites); here: who reaches
which route, that a refusal keeps its status, that a malformed query is a 422,
and that the reconciler is woken only once an approval committed.
"""

from __future__ import annotations

import typing
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import deps
from app.api.v1 import improvement_proposals as api
from app.core.database import get_db
from app.services import improvement_proposals as proposals
from app.services.evaluation.availability import require_evaluation


class _Db:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def execute(self, _query):
        return SimpleNamespace(scalars=lambda: [], all=lambda: [])


def _user(*, admin=False):
    return SimpleNamespace(id=uuid.uuid4(), role="admin" if admin else "user", is_admin=admin)


def _response(status: str) -> api.ImprovementProposalResponse:
    return api.ImprovementProposalResponse(
        id=str(uuid.uuid4()),
        proposal_type="persona",
        target_type="agent",
        target_id=str(uuid.uuid4()),
        status=status,
        base_content="Before.",
        proposed_content="After.",
        diff=[],
        added=1,
        removed=1,
        experiment={},
        evidence_refs=[],
        experiment_refs=[],
        quality_metric="score",
        can_decide=False,
        created_at="2026-11-03T12:00:00+00:00",
    )


@pytest.fixture
def harness(monkeypatch):
    state = {"db": _Db(), "wakes": []}

    async def _load(_db, _user, _proposal_id):
        return SimpleNamespace(status="pending_approval"), object(), SimpleNamespace(id=uuid.uuid4())

    async def _responses(_db, _user, _target, _handler, rows):
        return [_response(row.status) for row in rows]

    async def _wake():
        state["wakes"].append(state["db"].commits)

    async def _allow():
        return None

    monkeypatch.setattr(api, "_load", _load)
    monkeypatch.setattr(api, "_responses", _responses)
    monkeypatch.setattr("app.services.orchestration_engine.notify_orchestration_wake", _wake)

    def _client(user):
        app = FastAPI()
        app.include_router(api.router, prefix=URL)
        app.dependency_overrides[deps.get_current_user] = lambda: user
        app.dependency_overrides[deps.require_ai_enabled] = _allow
        app.dependency_overrides[require_evaluation] = _allow
        app.dependency_overrides[api.require_surface] = lambda: None

        async def _get_db():
            yield state["db"]

        app.dependency_overrides[get_db] = _get_db
        return TestClient(app)

    state["client"] = _client
    return state


URL = "/api/v1/improvement-proposals"


def test_the_query_names_every_target_type():
    from app.models.improvement_proposal import TARGET_TYPES

    assert set(typing.get_args(api.TargetType)) == set(TARGET_TYPES)


# ---------------------------------------------------------------------------
# Who reads a target's proposals
# ---------------------------------------------------------------------------


class _VisibilityDb:
    def __init__(self, visible: bool):
        self.visible = visible

    async def scalar(self, _query):
        return uuid.uuid4() if self.visible else None


def _handler(target):
    from app.models.agent import Agent

    async def _load_target(_db, _target_id, *, lock):
        return target

    return SimpleNamespace(
        load_target=_load_target,
        describe=lambda row: SimpleNamespace(managed=Agent(id=uuid.uuid4()), name="Agent"),
    )


@pytest.mark.parametrize(
    ("exists", "manages", "visible", "expected"),
    [
        (True, True, True, 200),
        # Seeing the agent is not enough: a proposal carries evidence for its managers.
        (True, False, True, 403),
        # A target the caller cannot see answers like no target.
        (True, False, False, 404),
        (False, False, False, 404),
    ],
)
async def test_a_targets_proposals_are_for_its_managers(monkeypatch, exists, manages, visible, expected):
    from app.models.agent import Agent

    target = SimpleNamespace(id=uuid.uuid4()) if exists else None

    async def _group_ids(_db, _user_id):
        return []

    monkeypatch.setattr(api, "get_user_group_ids", _group_ids)
    monkeypatch.setattr(proposals, "can_read", lambda *_args: manages)
    monkeypatch.setattr(api, "user_visible_query", lambda *_args, **_kwargs: select(Agent.id))
    handler = _handler(target)
    try:
        found = await api._readable_target(_VisibilityDb(visible), _user(), handler, uuid.uuid4(), not_found="nf")
    except HTTPException as refused:
        assert refused.status_code == expected
        return
    assert (expected, found) == (200, target)


_SOME_ID = "00000000-0000-4000-8000-000000008256"


@pytest.mark.parametrize(
    "query",
    [
        "",
        f"target_type=agent&agent_id={_SOME_ID}",
        f"target_id={_SOME_ID}",
        "target_type=agent",
        f"target_type=workspace&target_id={_SOME_ID}",
        "agent_id=not-a-uuid",
    ],
)
def test_a_list_names_one_target_or_is_a_422(harness, query):
    resp = harness["client"](_user()).get(f"{URL}?{query}")

    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize(
    ("proposal", "surface", "readable", "expected"),
    [
        (False, True, True, 404),
        # A proposal on a target the caller may not read is answered like no proposal.
        (True, True, False, 404),
        # Behind a flag that is off (EVALUATION for a person's, CONCIERGE for an agent's).
        (True, False, True, 404),
        (True, True, True, 200),
    ],
)
async def test_a_proposal_is_reached_through_its_target(monkeypatch, proposal, surface, readable, expected):
    row = SimpleNamespace(proposal_type="persona", target_id=uuid.uuid4()) if proposal else None
    target = SimpleNamespace(id=uuid.uuid4())

    class _GetDb:
        async def get(self, _model, _id):
            return row

    async def _readable(_db, _user, _handler, _target_id, *, not_found):
        if not readable:
            raise HTTPException(status_code=403, detail="Managers only.")
        return target

    monkeypatch.setattr(proposals, "surface_enabled", lambda _row: surface)
    monkeypatch.setattr(api, "_readable_target", _readable)
    try:
        loaded = await api._load(_GetDb(), _user(), uuid.uuid4())
    except HTTPException as refused:
        assert (refused.status_code, refused.detail) == (expected, "Improvement proposal not found.")
        return
    assert (expected, loaded[0], loaded[2]) == (200, row, target)


async def test_the_diffs_of_a_list_are_computed_off_the_event_loop(monkeypatch):
    offloaded = []
    real_run_parse = api.run_parse

    async def _run_parse(fn, *args):
        offloaded.append(fn)
        return await real_run_parse(fn, *args)

    async def _group_ids(_db, _user_id):
        return []

    class _NoNamesDb:
        async def execute(self, _query):
            return SimpleNamespace(all=lambda: [])

    monkeypatch.setattr(api, "run_parse", _run_parse)
    monkeypatch.setattr(api, "get_user_group_ids", _group_ids)
    monkeypatch.setattr(proposals, "approval_error", lambda *_args: None)
    target = SimpleNamespace(id=uuid.uuid4())
    row = SimpleNamespace(
        id=uuid.uuid4(),
        proposal_type="persona",
        target_type="agent",
        target_id=target.id,
        agent_id=target.id,
        status="pending_approval",
        summary=None,
        base_content="You are terse.",
        proposed_content="You are terse and kind.",
        base_version_number=1,
        applied_version_number=None,
        rollback_version_number=None,
        experiment_verdict="unmeasured",
        experiment={},
        evidence_refs=[{"kind": "trace", "id": "t-1"}],
        experiment_refs=[],
        evaluation_id=None,
        baseline_run_id=None,
        candidate_run_id=None,
        quality_metric="score",
        baseline_score=None,
        candidate_score=None,
        delta=None,
        verdict=None,
        outcome_note=None,
        author_agent_id=None,
        requested_by_user_id=None,
        decided_by_user_id=None,
        created_at=datetime.now(UTC),
        decided_at=None,
        applied_at=None,
        watch_until=None,
        settled_at=None,
    )
    handler = SimpleNamespace(describe=lambda _target: SimpleNamespace(name="Agent"))

    [response] = await api._responses(_NoNamesDb(), _user(), target, handler, [row])

    assert offloaded == [api._diffs]
    assert (response.added, response.removed, response.can_decide) == (1, 1, True)
    assert (response.experiment_verdict, response.target_name) == ("unmeasured", "Agent")
    assert [ref.model_dump() for ref in response.evidence_refs] == [{"kind": "trace", "id": "t-1"}]


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


def test_only_an_admin_asks_for_a_persona_rewrite(harness):
    body = {"agent_id": str(uuid.uuid4()), "evaluation_id": str(uuid.uuid4())}
    resp = harness["client"](_user()).post(f"{URL}/persona", json=body)

    assert resp.status_code == 403
    assert harness["db"].commits == 0


def test_an_approval_wakes_the_reconciler_once_it_committed(harness, monkeypatch):
    """What applying queues (a candidate run's execution) is part of the
    approval's transaction, so the route commits once and only then wakes the
    reconciler."""

    async def _approve(db, *, user, proposal_id):
        assert db.commits == 0
        return SimpleNamespace(status="watching")

    monkeypatch.setattr(proposals, "approve", _approve)

    resp = harness["client"](_user()).post(f"{URL}/{uuid.uuid4()}/approve")

    assert resp.status_code == 200, resp.text
    assert harness["db"].commits == 1
    assert harness["wakes"] == [1]


@pytest.mark.parametrize("status_code", [403, 404, 409])
def test_a_refused_decision_keeps_its_status_and_wakes_nothing(harness, monkeypatch, status_code):
    async def _refuse(db, **_kwargs):
        raise proposals.ProposalError(status_code, "No.")

    monkeypatch.setattr(proposals, "approve", _refuse)
    monkeypatch.setattr(proposals, "reject", _refuse)
    client = harness["client"](_user())

    approve = client.post(f"{URL}/{uuid.uuid4()}/approve")
    reject = client.post(f"{URL}/{uuid.uuid4()}/reject", json={})

    assert (approve.status_code, reject.status_code) == (status_code, status_code)
    assert approve.json()["detail"] == "No."
    assert harness["db"].commits == 0
    assert harness["db"].rollbacks == 2
    assert harness["wakes"] == []


def test_a_rejection_carries_its_reason(harness, monkeypatch):
    seen = {}

    async def _reject(db, *, user, proposal_id, reason):
        seen["reason"] = reason
        return SimpleNamespace(status="rejected")

    monkeypatch.setattr(proposals, "reject", _reject)

    resp = harness["client"](_user()).post(f"{URL}/{uuid.uuid4()}/reject", json={"reason": "Too long."})

    assert resp.status_code == 200, resp.text
    assert seen == {"reason": "Too long."}
    assert harness["wakes"] == []


def test_an_over_long_rejection_reason_is_422(harness):
    resp = harness["client"](_user()).post(f"{URL}/{uuid.uuid4()}/reject", json={"reason": "x" * 2001})

    assert resp.status_code == 422


@pytest.mark.parametrize(("method", "path", "body"), [("post", "approve", None), ("post", "reject", {}), ("get", "", None)])
def test_a_malformed_proposal_id_answers_like_an_unknown_one(harness, monkeypatch, method, path, body):
    async def _unreached(*_args, **_kwargs):  # pragma: no cover - the id never gets this far
        raise AssertionError("a malformed id reached the lifecycle")

    monkeypatch.setattr(api, "_load", _unreached)
    client = harness["client"](_user())
    url = f"{URL}/not-a-uuid/{path}".rstrip("/")

    resp = client.post(url, json=body) if method == "post" else client.get(url)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Improvement proposal not found."
    assert harness["db"].commits == 0
