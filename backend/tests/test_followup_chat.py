"""Tests for follow-up behaviour on assignment runs.

Covers two things:

1. A follow-up must NOT be blocked by other active runs on the same assignment
   (``followup_run`` no longer has an active-run guard).
2. The interactive follow-up chat endpoint (``followup_run_chat``) creates a
   conversation routed to the run's agent and seeds it with the run's task,
   result and a compact execution trace.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.api.v1 import assignments as assignments_module
from app.api.v1.assignments import (
    _build_run_execution_trace,
    _extract_run_task,
    followup_run_chat,
)
from app.api.v1.schemas.assignment import FollowupChatRequest, FollowupRunRequest

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _OneResult:
    """Mimics a SQLAlchemy Result for ``.scalar_one_or_none()`` / ``.scalar_one()``."""

    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj

    def scalar_one(self):
        return self._obj


class _FakeDB:
    """Returns queued execute() results in order; assigns ids on flush()."""

    def __init__(self, results):
        self._results = list(results)
        self.added: list = []
        self.execute_calls = 0

    async def execute(self, _stmt):
        self.execute_calls += 1
        if not self._results:
            raise AssertionError("Unexpected extra execute() call")
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        # The real DB assigns ``id`` defaults on INSERT flush; mirror that so
        # downstream ``conv.id`` references resolve to a real UUID in tests.
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self):
        # A commit flushes first.
        await self.flush()


def _make_user(is_admin: bool = False):
    return SimpleNamespace(
        id=uuid.uuid4(),
        role="admin" if is_admin else "user",
        is_admin=is_admin,
        email="user@example.com",
        full_name="Test User",
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_extract_run_task_prefers_first_user_message():
    run = SimpleNamespace(
        messages=[
            {"role": "system", "content": "you are a bot"},
            {"role": "user", "content": "Validate issue #1234"},
            {"role": "assistant", "content": "done"},
        ]
    )
    assignment = SimpleNamespace(prompt="fallback prompt")
    assert _extract_run_task(run, assignment) == "Validate issue #1234"


def test_extract_run_task_falls_back_to_assignment_prompt():
    run = SimpleNamespace(messages=[{"role": "assistant", "content": "hi"}])
    assignment = SimpleNamespace(prompt="the configured prompt")
    assert _extract_run_task(run, assignment) == "the configured prompt"


def test_extract_run_task_empty_when_nothing_available():
    run = SimpleNamespace(messages=None)
    assert _extract_run_task(run, None) == ""


def test_build_execution_trace_lists_tools_and_results():
    run = SimpleNamespace(
        messages=[
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "name": "search_memory", "arguments": {}},
                    {"id": "c2", "name": "search_knowledge", "arguments": {}},
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "no memories found"},
            {"role": "tool", "tool_call_id": "c2", "content": "3 docs"},
            {"role": "assistant", "content": "final answer"},
        ]
    )
    trace = _build_run_execution_trace(run)
    assert "2 tool call(s)" in trace
    assert "`search_memory`" in trace
    assert "no memories found" in trace
    assert "`search_knowledge`" in trace


def test_build_execution_trace_empty_when_no_tools():
    run = SimpleNamespace(
        messages=[
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "just a reply"},
        ]
    )
    assert _build_run_execution_trace(run) == ""

    assert _build_run_execution_trace(SimpleNamespace(messages=None)) == ""


def test_build_execution_trace_truncates_long_results():
    long_result = "x" * 5000
    run = SimpleNamespace(
        messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "name": "fetch", "arguments": {}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": long_result},
        ]
    )
    trace = _build_run_execution_trace(run)
    assert "…" in trace
    # The whole 5000-char blob must not be inlined verbatim.
    assert long_result not in trace


# ---------------------------------------------------------------------------
# Interactive follow-up chat endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_followup_chat_creates_seeded_conversation(monkeypatch):
    user = _make_user()
    agent = SimpleNamespace(id=uuid.uuid4(), enabled=True, name="Issue Validator")
    assignment = SimpleNamespace(
        id=uuid.uuid4(), name="Validate new issues", prompt="cfg prompt"
    )
    parent_run = SimpleNamespace(
        id=uuid.uuid4(),
        status="completed",
        is_company_managed=False,
        user_id=user.id,
        agent_id=agent.id,
        assignment_id=assignment.id,
        result_summary="Issue #1234 is fully compliant.",
        error=None,
        messages=[
            {"role": "user", "content": "Validate issue #1234"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "name": "search_memory", "arguments": {}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "nothing relevant"},
            {"role": "assistant", "content": "Issue #1234 is fully compliant."},
        ],
    )

    db = _FakeDB(
        results=[
            _OneResult(parent_run),  # load parent run
            _OneResult(agent),  # load agent
            _OneResult(assignment),  # load assignment
        ]
    )

    audited: dict = {}

    async def _fake_audit(*_a, **kwargs):
        audited.update(kwargs)

    monkeypatch.setattr(assignments_module, "log_audit", _fake_audit)

    resp = await followup_run_chat(
        run_id=str(parent_run.id),
        body=FollowupChatRequest(prompt="Why did you only use search_memory?"),
        request=None,
        user=user,
        db=db,
    )

    # Conversation routed to the run's agent and owned by the requesting user.
    convs = [o for o in db.added if type(o).__name__ == "Conversation"]
    msgs = [o for o in db.added if type(o).__name__ == "Message"]
    assert len(convs) == 1
    conv = convs[0]
    assert conv.agent_id == agent.id
    assert conv.user_id == user.id
    assert conv.channel == "web"

    # Two seeded messages: the original task (user) + result/trace (assistant).
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert "Validate issue #1234" in msgs[0].content
    assert msgs[1].role == "assistant"
    assert "fully compliant" in msgs[1].content
    assert "`search_memory`" in msgs[1].content  # execution trace seeded

    # Response carries the new conversation + agent ids.
    assert resp.conversation_id == str(conv.id)
    assert resp.agent_id == str(agent.id)
    assert audited.get("action") == "followup_chat"


@pytest.mark.asyncio
async def test_followup_chat_strips_null_bytes_from_seeded_messages(monkeypatch):
    """Seeded content is derived from arbitrary run output, which can contain
    null bytes (0x00). PostgreSQL ``Text`` columns reject them, so the endpoint
    must strip them before insert (mirroring ``store_message``)."""
    user = _make_user()
    agent = SimpleNamespace(id=uuid.uuid4(), enabled=True, name="Agent")
    assignment = SimpleNamespace(id=uuid.uuid4(), name="Job", prompt="cfg")
    parent_run = SimpleNamespace(
        id=uuid.uuid4(),
        status="completed",
        is_company_managed=False,
        user_id=user.id,
        agent_id=agent.id,
        assignment_id=assignment.id,
        result_summary="result with a null\x00byte",
        error=None,
        messages=[
            {"role": "user", "content": "task with a null\x00byte"},
            {"role": "assistant", "content": "done"},
        ],
    )

    db = _FakeDB(
        results=[
            _OneResult(parent_run),  # load parent run
            _OneResult(agent),  # load agent
            _OneResult(assignment),  # load assignment
        ]
    )

    async def _fake_audit(*_a, **_k):
        return None

    monkeypatch.setattr(assignments_module, "log_audit", _fake_audit)

    await followup_run_chat(
        run_id=str(parent_run.id),
        body=FollowupChatRequest(prompt=None),
        request=None,
        user=user,
        db=db,
    )

    msgs = [o for o in db.added if type(o).__name__ == "Message"]
    assert msgs, "expected seeded messages"
    for msg in msgs:
        assert "\x00" not in msg.content
    # The surrounding text is preserved — only the null byte is removed.
    assert "task with a nullbyte" in msgs[0].content
    assert "result with a nullbyte" in msgs[1].content


@pytest.mark.asyncio
async def test_followup_chat_rejects_non_terminal_run(monkeypatch):
    from fastapi import HTTPException

    user = _make_user()
    parent_run = SimpleNamespace(
        id=uuid.uuid4(),
        status="running",
        is_company_managed=False,
        user_id=user.id,
        agent_id=uuid.uuid4(),
        assignment_id=uuid.uuid4(),
    )
    db = _FakeDB(results=[_OneResult(parent_run)])

    with pytest.raises(HTTPException) as exc:
        await followup_run_chat(
            run_id=str(parent_run.id),
            body=FollowupChatRequest(prompt="hi"),
            request=None,
            user=user,
            db=db,
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_followup_run_not_blocked_by_active_runs(monkeypatch):
    """A follow-up succeeds even though the assignment has other active runs.

    The active-run guard was removed for the follow-up path. The fake
    DB queues exactly the four queries the handler now makes — parent run,
    assignment, agent, and the final run fetch — with NO active-run check in
    between. If the guard were still present it would consume one of these
    results out of order and raise ``409``.
    """
    user = _make_user()
    agent = SimpleNamespace(id=uuid.uuid4(), enabled=True, name="Validator")
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        name="Validate",
        enabled=True,
        user_id=None,
        is_company_managed=False,
    )
    parent_run = SimpleNamespace(
        id=uuid.uuid4(),
        status="completed",
        is_company_managed=False,
        user_id=user.id,
        agent_id=agent.id,
        assignment_id=assignment.id,
        result_summary="ok",
        error=None,
    )
    new_run = SimpleNamespace(id=uuid.uuid4())

    db = _FakeDB(
        results=[
            _OneResult(parent_run),  # load parent run
            _OneResult(assignment),  # load assignment
            _OneResult(agent),  # load agent
            _OneResult(new_run),  # final run fetch (.scalar_one)
        ]
    )

    trigger_calls: list = []

    async def _fake_trigger(**kwargs):
        trigger_calls.append(kwargs)
        return new_run.id

    sentinel = object()

    monkeypatch.setattr(
        "app.services.assignment_engine.trigger_assignment", _fake_trigger
    )
    monkeypatch.setattr(
        assignments_module, "_run_to_response", lambda r, **_k: sentinel
    )

    async def _fake_audit(*_a, **_k):
        return None

    monkeypatch.setattr(assignments_module, "log_audit", _fake_audit)

    result = await assignments_module.followup_run(
        run_id=str(parent_run.id),
        body=FollowupRunRequest(prompt="please refine the result"),
        request=None,
        user=user,
        db=db,
    )

    assert result is sentinel
    assert len(trigger_calls) == 1
    # Every queued query was consumed — no leftover active-run check.
    assert db.execute_calls == 4
