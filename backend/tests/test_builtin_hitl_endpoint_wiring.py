"""The agent endpoint consults the built-in approval carve-out.

``test_builtin_hitl_admin_writable.py`` tests the decision function alone;
nothing there fails if ``update_agent`` stops calling it. Driven directly with a
fake session: the authorisation decision is made before any I/O.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.v1 import agents as agents_api
from app.api.v1 import assignments as assignments_api
from app.api.v1.schemas.agent import UpdateAgentRequest
from app.api.v1.schemas.assignment import UpdateAssignmentRequest
from tests.test_agent_update_partial_payload import (
    _DummyAgent,
    _FakeDB,
    _response_payload,
    _setup_update_agent_mocks,
)


@pytest.fixture(autouse=True)
def _xtm_one_mode(monkeypatch):
    """The built-in read-only rule, and its carve-out, are XTM One only."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "platform_mode", "xtm_one", raising=False)


def _admin() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), is_admin=True, role="admin")


def _member() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), is_admin=False, role="user")


def _request() -> Request:
    return Request(
        {"type": "http", "method": "PUT", "path": "/api/v1/agents/x", "headers": []}
    )


# ---------------------------------------------------------------------------
# PUT /agents/{id} on a built-in agent
# ---------------------------------------------------------------------------


def _builtin_agent() -> _DummyAgent:
    agent = _DummyAgent()
    agent.is_builtin = True
    # Built-in agents are always company-managed; the carve-out leans on that
    # to resolve to administrators without inventing a permission.
    agent.is_company_managed = True
    agent.approve_tool_calls = True
    return agent


async def _invoke_agent_update(monkeypatch, body, agent, user):
    _setup_update_agent_mocks(monkeypatch, agent)
    audits: list[dict] = []

    async def _capture_audit(*_args, **kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(agents_api, "log_audit", _capture_audit)
    monkeypatch.setattr(agents_api, "_to_response", _response_payload)

    await agents_api.update_agent(str(agent.id), body, _request(), user, _FakeDB())
    return audits


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [False, True, None], ids=["off", "on", "install-default"])
async def test_admin_can_set_the_gate_on_a_builtin_agent(monkeypatch, value):
    """The endpoint writes the field, null included (follow the install)."""
    agent = _builtin_agent()
    await _invoke_agent_update(
        monkeypatch, UpdateAgentRequest(approve_tool_calls=value), agent, _admin()
    )
    assert agent.approve_tool_calls is value


@pytest.mark.asyncio
async def test_setting_the_gate_on_a_builtin_agent_is_audited(monkeypatch):
    """The audit record carries the value, not just "agent updated"."""
    agent = _builtin_agent()
    audits = await _invoke_agent_update(
        monkeypatch, UpdateAgentRequest(approve_tool_calls=False), agent, _admin()
    )
    assert audits, "no audit record was written"
    assert (audits[-1].get("details") or {}).get("approve_tool_calls") is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(UpdateAgentRequest(human_in_the_loop=False), id="legacy-hitl"),
        pytest.param(
            UpdateAgentRequest(human_in_the_loop_prompt="Ask me first"),
            id="prompt-alone",
        ),
        pytest.param(
            UpdateAgentRequest(
                approve_tool_calls=False, human_in_the_loop_prompt="Ask me first"
            ),
            id="prompt-smuggled-with-an-allowed-field",
        ),
        pytest.param(UpdateAgentRequest(persona="rewritten"), id="unrelated-field"),
    ],
)
async def test_other_fields_on_a_builtin_agent_are_still_refused(monkeypatch, body):
    agent = _builtin_agent()
    with pytest.raises(HTTPException) as exc:
        await _invoke_agent_update(monkeypatch, body, agent, _admin())
    assert exc.value.status_code == 403
    # The refusal names the locked field when one moved (#914).
    assert exc.value.detail.startswith("Built-in agents cannot be modified")
    assert agent.approve_tool_calls is True, "refused edit must not have applied"


@pytest.mark.asyncio
async def test_a_non_admin_cannot_set_the_gate_on_a_builtin_agent(monkeypatch):
    """The carve-out clears the built-in gate, not the company-managed one."""
    agent = _builtin_agent()
    with pytest.raises(HTTPException) as exc:
        await _invoke_agent_update(
            monkeypatch, UpdateAgentRequest(approve_tool_calls=False), agent, _member()
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == "Only admins can edit company-managed agents"
    assert agent.approve_tool_calls is True


# ---------------------------------------------------------------------------
# PUT /assignments/{id} on a built-in agent's assignment
# ---------------------------------------------------------------------------


class _AgentResult:
    def __init__(self, agent):
        self._agent = agent

    def scalar_one_or_none(self):
        return self._agent


class _AssignmentDB:
    """Answers the single parent-agent lookup `update_assignment` performs."""

    def __init__(self, agent):
        self._agent = agent

    async def execute(self, _stmt):
        return _AgentResult(self._agent)

    async def commit(self):
        return None

    async def flush(self):
        return None

    async def refresh(self, *_args, **_kwargs):
        return None


def _assignment_agent() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=None,
        is_builtin=True,
        is_company_managed=True,
        shared_group_ids=None,
        shared_group_permissions=None,
        human_in_the_loop=True,
    )


class _Assignment:
    """A permissive assignment stand-in.

    ``update_assignment`` reads a long tail of unrelated columns after the gate
    (max_iterations, KB selections, run-as identity, …). Enumerating them would
    make this file break every time an unrelated field is added, so unknown
    attributes read as ``None`` — the columns under test are set explicitly.
    """

    def __init__(self, agent_id):
        self.id = uuid.uuid4()
        self.agent_id = agent_id
        self.name = "Weekly discovery assessment"
        self.user_id = None
        self.is_company_managed = True
        self.human_in_the_loop = None
        self.human_in_the_loop_prompt = None
        self.trigger_type = "schedule"
        self.enabled = True

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return None


def _assignment(agent_id) -> _Assignment:
    return _Assignment(agent_id)


async def _invoke_assignment_update(monkeypatch, body, agent, assignment, user):
    """Drive `update_assignment`, capturing what the guard was told."""
    seen: dict = {}

    async def _groups(_db, _user_id):
        return []

    async def _get_user_assignment(_db, _user_id, _aid, _groups=None, **_kwargs):
        return assignment

    def _spy_guard(_assignment, _agent, _user, _group_ids=None, **kwargs):
        seen["fields"] = kwargs.get("fields")
        seen["action"] = kwargs.get("action")
        # Delegate to the real rule so behaviour is unchanged by the spy.
        from app.api.v1.helpers import assert_writable_assignment as real

        return real(_assignment, _agent, _user, _group_ids, **kwargs)

    async def _log_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(assignments_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(assignments_api, "get_user_assignment", _get_user_assignment)
    monkeypatch.setattr(assignments_api, "assert_writable_assignment", _spy_guard)
    monkeypatch.setattr(assignments_api, "log_audit", _log_audit)
    monkeypatch.setattr(
        assignments_api, "_to_response", lambda a, **_kwargs: {"id": str(a.id)}
    )

    await assignments_api.update_assignment(
        str(assignment.id), body, _request(), user, _AssignmentDB(agent)
    )
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(UpdateAssignmentRequest(human_in_the_loop=False), id="hitl-alone"),
        pytest.param(
            UpdateAssignmentRequest(human_in_the_loop=False, prompt="rewritten"),
            id="wider",
        ),
    ],
)
async def test_a_builtin_agents_assignment_stays_read_only(monkeypatch, body):
    """The gate is per agent now, so no assignment field is opened."""
    agent = _assignment_agent()
    assignment = _assignment(agent.id)
    with pytest.raises(HTTPException) as exc:
        await _invoke_assignment_update(
            monkeypatch,
            body,
            agent,
            assignment,
            _admin(),
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == "Cannot edit assignments on built-in agents."
    assert assignment.human_in_the_loop is None, "refused edit must not have applied"


def test_no_endpoint_passes_a_field_set_to_the_guard():
    """The narrow assignment exception is gone; nothing may re-open it."""
    import inspect

    assert "fields=" not in inspect.getsource(assignments_api).split(
        "assert_writable_assignment("
    )[1][:200]
    assert "fields" not in inspect.signature(
        __import__("app.api.v1.helpers", fromlist=["x"]).writable_assignment_error
    ).parameters
