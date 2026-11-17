"""Tests for durable HITL resolution persistence.

The assignment engine clears the live ``human_input_question`` /
``human_input_response`` fields as soon as a HITL-paused run resumes, so
without a durable entry the UI had no persistent record that the question
was ever answered — the prompt block stayed orange/pending and the user's
reply vanished on the next panel load.

The respond/dismiss endpoints now append a durable entry to
``AssignmentRun.human_input_history`` ({question, response, resolution,
responded_at, responded_by, responded_by_id}) which the engine never
clears. These tests pin:

1. ``record_human_input_resolution`` — entry shape, timestamp, user
   attribution, and list reassignment (SQLAlchemy JSON change tracking).
2. ``respond_to_human_input`` — persists the response + an "answered"
   history entry and returns it in the API response.
3. ``dismiss_run`` — persists a "dismissed" history entry and leaves the
   pending state (terminal status).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1.assignments import (
    dismiss_run,
    record_human_input_resolution,
    respond_to_human_input,
)
from app.api.v1.schemas.assignment import RespondToHumanInputRequest
from app.models.assignment import AssignmentRun
from app.services import assignment_engine as ae

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _OneResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _FakeDB:
    """Returns queued execute() results in order; counts commits/refreshes."""

    def __init__(self, results):
        self._results = list(results)
        self.commits = 0
        self.refreshes = 0

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected extra execute() call")
        return self._results.pop(0)

    async def get(self, _model, _pk):
        # The run's assignment, read for the owner-deactivation refusal:
        # absent here, as for a run whose assignment was deleted.
        return None

    async def commit(self):
        self.commits += 1

    async def refresh(self, _obj):
        self.refreshes += 1


@pytest.fixture(autouse=True)
def _no_action_requests(monkeypatch):
    """Dismissing closes a run's open action requests: an UPDATE this
    fake session has no result queued for, and nothing to close."""
    import app.api.v1.assignments as api

    monkeypatch.setattr(api, "close_run_action_requests", AsyncMock(return_value=0))


def _make_user():
    return SimpleNamespace(
        id=uuid.uuid4(),
        role="user",
        is_admin=False,
        email="responder@example.com",
        full_name="Responder User",
    )


def _make_awaiting_run(user_id: uuid.UUID) -> AssignmentRun:
    return AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=user_id,
        is_company_managed=False,
        notification_sent=False,
        status="awaiting_human",
        human_input_question="Should I proceed with the deployment?",
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# record_human_input_resolution — pure helper
# ---------------------------------------------------------------------------


def test_record_resolution_appends_attributed_timestamped_entry():
    user = _make_user()
    run = _make_awaiting_run(user.id)
    before = datetime.now(UTC)

    entry = record_human_input_resolution(
        run, user, resolution="answered", response="Yes, go ahead"
    )

    after = datetime.now(UTC)
    assert run.human_input_history == [entry]
    assert entry["question"] == "Should I proceed with the deployment?"
    assert entry["response"] == "Yes, go ahead"
    assert entry["resolution"] == "answered"
    assert entry["responded_by"] == "Responder User"
    assert entry["responded_by_id"] == str(user.id)
    responded_at = datetime.fromisoformat(entry["responded_at"])
    assert before <= responded_at <= after


def test_record_resolution_reassigns_list_and_preserves_prior_rounds():
    """Reassignment (not in-place mutation) is what makes SQLAlchemy's JSON
    change tracking persist the update; prior HITL rounds must survive."""
    user = _make_user()
    run = _make_awaiting_run(user.id)
    first = record_human_input_resolution(
        run, user, resolution="answered", response="round one"
    )
    first_list = run.human_input_history

    run.human_input_question = "Second question?"
    second = record_human_input_resolution(
        run, user, resolution="answered", response="round two"
    )

    assert run.human_input_history == [first, second]
    assert run.human_input_history is not first_list


def test_record_resolution_falls_back_to_email_when_no_full_name():
    user = _make_user()
    user.full_name = None
    run = _make_awaiting_run(user.id)

    entry = record_human_input_resolution(
        run, user, resolution="dismissed", response=None
    )

    assert entry["responded_by"] == "responder@example.com"
    assert entry["response"] is None


# ---------------------------------------------------------------------------
# respond endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_respond_persists_response_and_history(monkeypatch):
    user = _make_user()
    run = _make_awaiting_run(user.id)
    db = _FakeDB([_OneResult(run)])
    resumed: list = []

    async def _fake_resume(_db, _run):
        resumed.append(_run)

    monkeypatch.setattr(ae, "resume_assignment_run", _fake_resume)

    result = await respond_to_human_input(
        str(run.id),
        RespondToHumanInputRequest(response="Yes, deploy it"),
        user=user,
        db=db,
    )

    # Live field set (drives the engine's resume injection) ...
    assert run.human_input_response == "Yes, deploy it"
    # ... and the durable history entry recorded before the resume.
    assert resumed == [run]
    assert len(run.human_input_history) == 1
    entry = run.human_input_history[0]
    assert entry["resolution"] == "answered"
    assert entry["response"] == "Yes, deploy it"
    assert entry["question"] == "Should I proceed with the deployment?"
    assert entry["responded_by"] == "Responder User"
    assert entry["responded_by_id"] == str(user.id)
    assert entry["responded_at"]
    # The API response surfaces the history for immediate UI reconciliation.
    assert result.human_input_history == run.human_input_history


@pytest.mark.asyncio
async def test_respond_rejects_blank_response():
    """Empty/whitespace responses must be rejected: the UI keys the pending
    -> answered transition off response presence, so persisting a blank
    answer would leave the run in an ambiguous 'answered but empty' state."""
    from fastapi import HTTPException

    user = _make_user()
    run = _make_awaiting_run(user.id)
    db = _FakeDB([_OneResult(run)])

    with pytest.raises(HTTPException) as exc:
        await respond_to_human_input(
            str(run.id),
            RespondToHumanInputRequest(response="   \n\t "),
            user=user,
            db=db,
        )

    assert exc.value.status_code == 400
    assert run.human_input_response is None
    assert run.human_input_history is None


@pytest.mark.asyncio
async def test_respond_strips_surrounding_whitespace(monkeypatch):
    user = _make_user()
    run = _make_awaiting_run(user.id)
    db = _FakeDB([_OneResult(run)])

    async def _fake_resume(_db, _run):
        return None

    monkeypatch.setattr(ae, "resume_assignment_run", _fake_resume)

    await respond_to_human_input(
        str(run.id),
        RespondToHumanInputRequest(response="  Yes, deploy it  \n"),
        user=user,
        db=db,
    )

    assert run.human_input_response == "Yes, deploy it"
    assert run.human_input_history[0]["response"] == "Yes, deploy it"


@pytest.mark.asyncio
async def test_respond_rejects_run_not_awaiting_human():
    from fastapi import HTTPException

    user = _make_user()
    run = _make_awaiting_run(user.id)
    run.status = "running"
    db = _FakeDB([_OneResult(run)])

    with pytest.raises(HTTPException) as exc:
        await respond_to_human_input(
            str(run.id),
            RespondToHumanInputRequest(response="too late"),
            user=user,
            db=db,
        )

    # 409: a run that reached `running` left `awaiting_human` by
    # being resumed, so this request was well formed and would have been
    # accepted a moment earlier. Only a run that never awaited input is the
    # caller's own mistake, and that stays 400 (FR-030).
    assert exc.value.status_code == 409
    assert "no longer awaiting approval" in exc.value.detail
    assert run.human_input_history is None


# ---------------------------------------------------------------------------
# dismiss endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dismiss_persists_dismissed_history_and_completes_run():
    user = _make_user()
    run = _make_awaiting_run(user.id)
    # Second execute() loads the assignment for the notification check.
    db = _FakeDB([_OneResult(run), _OneResult(None)])

    result = await dismiss_run(str(run.id), user=user, db=db)

    assert run.status == "completed"
    assert run.completed_at is not None
    assert len(run.human_input_history) == 1
    entry = run.human_input_history[0]
    assert entry["resolution"] == "dismissed"
    assert entry["response"] is None
    assert entry["question"] == "Should I proceed with the deployment?"
    assert entry["responded_by"] == "Responder User"
    assert db.commits == 1
    assert result.human_input_history == run.human_input_history
