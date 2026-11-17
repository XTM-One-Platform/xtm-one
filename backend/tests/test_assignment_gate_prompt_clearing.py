"""The assignment gate prompt must be clearable.

``activation_prompt`` is the optional "should I run?" gate; ``NULL`` means "no
gate, always run". Two bugs made it write-once from the UI:

* the frontend dropped an emptied field from the payload, so the
  "omitted means unchanged" update loop left the stale gate in place, and
* a whitespace-only value was truthy, so ``"   "`` was persisted and the engine
  ran a real gate LLM call with a blank question.

The API now normalizes any blank / whitespace-only gate to ``NULL`` on both the
create and the update path, and the engine ignores legacy whitespace rows.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from app.api.v1 import assignments as assignments_api
from app.api.v1.schemas.assignment import (
    CreateAssignmentRequest,
    UpdateAssignmentRequest,
)

# ---------------------------------------------------------------------------
# Schema-level normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", " ", "   ", "\n", " \t\n "])
def test_update_request_normalizes_blank_gate_to_none(blank):
    body = UpdateAssignmentRequest(activation_prompt=blank)
    assert body.activation_prompt is None
    # Still explicitly provided: the update loop must write the NULL through
    # instead of treating the field as "not provided".
    assert "activation_prompt" in body.model_fields_set


def test_update_request_keeps_a_real_gate_verbatim():
    gate = "Is this relevant? Answer YES or NO."
    assert UpdateAssignmentRequest(activation_prompt=gate).activation_prompt == gate


def test_update_request_omitting_the_gate_leaves_it_unset():
    # The normalization must not add the field to ``model_fields_set``, or every
    # unrelated update (a rename) would clear an existing gate.
    body = UpdateAssignmentRequest(name="Renamed")
    assert body.activation_prompt is None
    assert "activation_prompt" not in body.model_fields_set


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_create_request_normalizes_blank_gate_to_none(blank):
    body = CreateAssignmentRequest(
        agent_id=str(uuid.uuid4()),
        name="Assignment",
        prompt="do the thing",
        trigger_type="manual",
        activation_prompt=blank,
    )
    assert body.activation_prompt is None


# ---------------------------------------------------------------------------
# Update endpoint: the column actually lands on NULL
# ---------------------------------------------------------------------------


class _AgentResult:
    def __init__(self, agent):
        self._agent = agent

    def scalar_one_or_none(self):
        return self._agent


class _FakeDB:
    """Minimal async session — the parent-agent lookup is the only query
    ``update_assignment`` runs for these payloads."""

    def __init__(self, agent):
        self._agent = agent

    async def execute(self, _stmt):
        return _AgentResult(self._agent)

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def refresh(self, _obj):
        return None


def _fake_assignment(activation_prompt):
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="Assignment",
        activation_prompt=activation_prompt,
        max_iterations=None,
        trigger_type="manual",
        trigger_config={},
        is_company_managed=False,
        enabled=True,
        delay=None,
        ai_label=False,
        human_in_the_loop=False,
        # Read by the run-as handling of the update route (#9188).
        run_as_user_id=None,
    )


async def _invoke_update_assignment(monkeypatch, assignment, body):
    agent = SimpleNamespace(
        id=assignment.agent_id, is_company_managed=False, name="Agent"
    )
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, role="admin")

    async def _groups(_db, _uid):
        return []

    async def _get_assignment(_db, _uid, _aid, _gids, **_kwargs):
        return assignment

    def _assert_writable(*_args, **_kwargs):
        return None

    async def _audit(*_args, **_kwargs):
        return None

    def _to_response(w, **_kwargs):
        return {"id": str(w.id), "activation_prompt": w.activation_prompt}

    monkeypatch.setattr(assignments_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(assignments_api, "get_user_assignment", _get_assignment)
    monkeypatch.setattr(
        assignments_api, "assert_writable_assignment", _assert_writable
    )
    monkeypatch.setattr(assignments_api, "log_audit", _audit)
    monkeypatch.setattr(assignments_api, "_to_response", _to_response)
    monkeypatch.setattr(assignments_api, "_owner_paused", AsyncMock(return_value=False))

    request = Request({"type": "http", "method": "PUT", "path": "/x", "headers": []})
    return await assignments_api.update_assignment(
        str(assignment.id), body, request, user, _FakeDB(agent)
    )


@pytest.mark.asyncio
async def test_explicit_null_clears_the_gate(monkeypatch):
    assignment = _fake_assignment("Is this relevant? YES or NO.")
    await _invoke_update_assignment(
        monkeypatch, assignment, UpdateAssignmentRequest(activation_prompt=None)
    )
    assert assignment.activation_prompt is None


@pytest.mark.parametrize("blank", ["", "  "])
@pytest.mark.asyncio
async def test_blank_gate_clears_the_column(monkeypatch, blank):
    assignment = _fake_assignment("Is this relevant? YES or NO.")
    await _invoke_update_assignment(
        monkeypatch, assignment, UpdateAssignmentRequest(activation_prompt=blank)
    )
    assert assignment.activation_prompt is None


@pytest.mark.asyncio
async def test_unrelated_update_keeps_the_existing_gate(monkeypatch):
    gate = "Is this relevant? YES or NO."
    assignment = _fake_assignment(gate)
    await _invoke_update_assignment(
        monkeypatch, assignment, UpdateAssignmentRequest(name="Renamed")
    )
    assert assignment.activation_prompt == gate


# ---------------------------------------------------------------------------
# Engine read side: legacy whitespace-only rows must not gate
#
# Pinned via source inspection (same convention as
# ``test_activation_gate_skip_prompt.py``) because the full execution path is
# too heavy to drive in a unit test.
# ---------------------------------------------------------------------------


def test_engine_gate_check_ignores_whitespace_only_prompt():
    import inspect

    from app.services import assignment_engine

    source = inspect.getsource(assignment_engine.execute_assignment_run)
    assert 'if (assignment.activation_prompt or "").strip():' in source, (
        "The activation gate must be entered on a non-blank prompt only, so a "
        "legacy whitespace-only row doesn't trigger a gate LLM call with a "
        "blank question."
    )
    assert "if assignment.activation_prompt:" not in source, (
        "The plain-truthiness gate check would treat ' ' as a real gate."
    )
