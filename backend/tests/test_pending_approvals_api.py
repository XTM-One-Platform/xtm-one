"""Contract for ``GET /runs/{id}/pending-approvals``.

The endpoint serves both pause shapes, so ``kind`` is what tells a client which
UI to render — a structured approval form or the legacy free-text box. Getting
that wrong shows a reviewer a textarea for a proposed ``delete_entity`` call.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.v1.assignments as assignments_mod


class _FakeResult:
    def __init__(self, run):
        self._run = run

    def scalar_one_or_none(self):
        return self._run


class _FakeDB:
    def __init__(self, run):
        self._run = run

    async def execute(self, _stmt):
        return _FakeResult(self._run)


def _run(**overrides):
    base = dict(
        id=uuid.uuid4(),
        status="awaiting_human",
        pending_approvals=None,
        human_input_question=None,
        is_company_managed=False,
        user_id=uuid.uuid4(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


_USER = SimpleNamespace(id=uuid.uuid4(), role="user")


@pytest.fixture(autouse=True)
def _allow_access(monkeypatch):
    """Access control is covered by the /respond suite; bypass it here."""
    monkeypatch.setattr(assignments_mod, "require_run_access", lambda *a, **k: None)


async def _get(run):
    return await assignments_mod.get_pending_approvals(
        run_id=str(run.id), user=_USER, db=_FakeDB(run)
    )


_PROPOSAL = {
    "tool_call_id": "call_abc",
    "tool_name": "delete_entity",
    "tool_description": "Permanently delete an entity.",
    "arguments": {"entity_id": "e-1", "cascade": True},
    "input_schema": {"type": "object", "properties": {"entity_id": {"type": "string"}}},
    "source": "integration:opencti",
}


async def test_gated_pause_is_reported_as_an_action_proposal():
    payload = await _get(_run(pending_approvals=[_PROPOSAL]))

    assert payload["kind"] == "action_proposal"
    assert payload["proposals"] == [_PROPOSAL]


async def test_free_text_pause_is_reported_as_free_text():
    """No proposals means the legacy prompt — clients must not render a form."""
    payload = await _get(
        _run(pending_approvals=None, human_input_question="Shall I continue?")
    )

    assert payload["kind"] == "free_text"
    assert payload["proposals"] == []
    assert payload["question"] == "Shall I continue?"


async def test_proposal_carries_everything_the_reviewer_needs():
    payload = await _get(_run(pending_approvals=[_PROPOSAL]))
    proposal = payload["proposals"][0]

    assert proposal["tool_name"] == "delete_entity"
    assert proposal["tool_description"]
    assert proposal["arguments"] == {"entity_id": "e-1", "cascade": True}
    # The schema travels with the values so the UI can label each argument with
    # its own description rather than a bare field name.
    assert "properties" in proposal["input_schema"]


async def test_run_not_awaiting_input_is_rejected():
    with pytest.raises(HTTPException) as exc:
        await _get(_run(status="running"))

    assert exc.value.status_code == 409


async def test_missing_run_is_404():
    with pytest.raises(HTTPException) as exc:
        await assignments_mod.get_pending_approvals(
            run_id=str(uuid.uuid4()), user=_USER, db=_FakeDB(None)
        )

    assert exc.value.status_code == 404


async def test_several_proposals_preserve_call_order():
    """Order matches the model's tool_calls, which the decision set mirrors."""
    second = {**_PROPOSAL, "tool_call_id": "call_def", "tool_name": "send_report"}
    payload = await _get(_run(pending_approvals=[_PROPOSAL, second]))

    assert [p["tool_call_id"] for p in payload["proposals"]] == ["call_abc", "call_def"]
