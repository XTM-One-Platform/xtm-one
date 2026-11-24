"""Trace tools read through a shared agent never reach further than its Chats tab.

A trace holds the conversation it records, and an agent's trace tools read
every user's conversations with it. So a chat turn wires them, whatever
carries them, only for someone who may read the agent's chats
(``traces.may_read_agent_traces``), and a supervision allow-list reads a
target only for a caller who may read that target's chats. Every caller is a
non-admin (rule 19).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.services.builtin_tools.traces as traces_module
from app.tools.base import ToolCall

pytestmark = pytest.mark.asyncio


def _user():
    return SimpleNamespace(id=uuid.uuid4(), role="user", is_admin=False, email="u@example.com")


def _agent(*, owner=None, company_managed=False, carries=(), tier=None, grantee=None):
    shared = [str(grantee.id)] if grantee is not None and tier else None
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=owner,
        is_company_managed=company_managed,
        builtin_tool_ids=list(carries),
        builtin_allowed_tools=None,
        shared_user_ids=shared,
        shared_user_permissions={str(grantee.id): tier} if shared else None,
        shared_group_ids=None,
        shared_group_permissions=None,
        trace_supervision_target_agent_ids=None,
    )


# ---------------------------------------------------------------------------
# A suite the agent carries itself
# ---------------------------------------------------------------------------


async def _turn(user, agent, turn):
    from app.services.agent_chat import _turn_trace_tool_ids

    return await _turn_trace_tool_ids(object(), user, agent, turn, user_group_ids=[])


@pytest.mark.parametrize("suite", ["inspect_my_traces", "inspect_agent_traces"])
async def test_a_carried_suite_is_not_wired_for_a_company_agent_s_chatter(suite):
    agent = _agent(company_managed=True, owner=uuid.uuid4(), carries=[suite])
    assert await _turn(_user(), agent, [suite]) == set()


@pytest.mark.parametrize("tier", ["read_only", "config"])
async def test_a_carried_suite_is_not_wired_for_a_grantee_without_chat_access(tier):
    user = _user()
    agent = _agent(owner=uuid.uuid4(), carries=["inspect_my_traces"], tier=tier, grantee=user)
    assert await _turn(user, agent, ["inspect_my_traces"]) == set()


async def test_a_carried_suite_is_wired_for_whoever_may_read_the_agent_s_chats():
    owner = _user()
    owned = _agent(owner=owner.id, carries=["inspect_my_traces"])
    assert await _turn(owner, owned, ["inspect_my_traces"]) == {"inspect_my_traces"}
    grantee = _user()
    shared = _agent(owner=uuid.uuid4(), carries=["inspect_my_traces"], tier="config_with_chats", grantee=grantee)
    assert await _turn(grantee, shared, ["inspect_my_traces"]) == {"inspect_my_traces"}


async def test_an_anonymous_caller_gets_no_carried_suite():
    agent = _agent(owner=uuid.uuid4(), carries=["inspect_my_traces"])
    assert await _turn(None, agent, ["inspect_my_traces"]) == set()


# ---------------------------------------------------------------------------
# A supervision allow-list, read
# ---------------------------------------------------------------------------


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _DB:
    """Answers the targets' load with the given agents."""

    def __init__(self, agents):
        self.agents = list(agents)

    async def execute(self, _stmt):
        return _Rows(self.agents)


class _SessionCtx:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *_exc):
        return False


def _trace(agent_id):
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        assignment_run_id=None,
        source="chat",
        name="chat",
        model="m",
        status="ok",
        started_at=now - timedelta(minutes=5),
        ended_at=now - timedelta(minutes=4),
        duration_ms=60_000.0,
        input_tokens=1,
        output_tokens=1,
        total_tokens=2,
        total_cost=None,
        error_message=None,
        input="someone else's question",
        output="someone else's answer",
        metadata_json=None,
    )


class _Store:
    def __init__(self, traces):
        self.by_id = {t.id: t for t in traces}

    async def get_trace_detail(self, _db, **kwargs):
        trace = self.by_id.get(kwargs["trace_id"])
        if trace is None:
            raise HTTPException(status_code=404, detail="Trace not found")
        return trace, None, None, None

    async def get_trace_spans(self, **_kwargs):
        return []


def _install(monkeypatch, user, target, traces=()):
    fetched: list[dict] = []
    searched: list[dict] = []
    db = _DB([target])
    monkeypatch.setattr(traces_module, "async_session", lambda: _SessionCtx(db))
    monkeypatch.setattr(traces_module, "build_trace_read_store", lambda: _Store(traces))
    monkeypatch.setattr(traces_module, "_health_refuses_other_agents", lambda: False)
    monkeypatch.setattr(traces_module, "_own_live_run", lambda _agent: None)

    async def _context(_db, _uid):
        return user, []

    async def _fetch(_db, **kwargs):
        fetched.append(kwargs)
        return json.dumps({"traces": []})

    async def _audit(_db, _user, **_kwargs):
        return None

    async def _none_gone(_db, _ids):
        return set()

    async def _no_own_run(_db, _value, *, own_agent_id):
        return None, None

    async def _run_roots(_db, value, *, viewer, group_ids, agent_ids):
        searched.append({"agent_ids": agent_ids})
        return []

    monkeypatch.setattr(traces_module, "_load_user_context", _context)
    monkeypatch.setattr(traces_module, "_fetch_traces", _fetch)
    monkeypatch.setattr(traces_module, "_audit_trace_read", _audit)
    monkeypatch.setattr(traces_module, "_agents_gone", _none_gone)
    monkeypatch.setattr(traces_module, "_own_trace_of_run", _no_own_run)
    monkeypatch.setattr(traces_module, "_supervised_run_root_traces", _run_roots)
    return fetched, searched


def _executor(user, target):
    _defs, executor = traces_module.build_trace_tools(
        db=object(),
        user_id=str(user.id),
        agent_id=str(uuid.uuid4()),
        supervision_target_agent_ids=[str(target.id)],
        allow_cross_agent=True,
    )
    return executor


async def _call(executor, name, **arguments):
    return await executor.execute_tool(ToolCall(id="c", name=name, arguments=arguments))


@pytest.mark.parametrize(
    "target_of",
    [
        # A company-managed agent everyone may chat with: its chats are admins'.
        lambda user: _agent(owner=uuid.uuid4(), company_managed=True),
        # A personal agent shared without chat access.
        lambda user: _agent(owner=uuid.uuid4(), tier="config", grantee=user),
    ],
)
async def test_a_supervision_target_whose_chats_the_caller_may_not_read_stays_closed(monkeypatch, target_of):
    user = _user()
    target = target_of(user)
    trace = _trace(target.id)
    fetched, searched = _install(monkeypatch, user, target, [trace])
    executor = _executor(user, target)

    listing = await _call(executor, "inspect_agent_traces", target_agent_id=str(target.id))
    across = await _call(executor, "inspect_agent_traces")
    payload = await _call(executor, "get_trace_payload", trace_id=str(trace.id))
    by_run = await _call(executor, "get_trace_payload", trace_id=str(uuid.uuid4()))

    assert "not allowed to inspect traces for this agent" in str(listing.content)
    assert "requires admin privileges" in str(across.content)
    assert "not allowed to read payloads for this trace" in str(payload.content)
    assert "someone else's" not in str(payload.content)
    assert "trace not found" in str(by_run.content)
    assert fetched == [] and searched == []


async def test_a_supervision_target_whose_chats_the_caller_may_read_is_inspected(monkeypatch):
    user = _user()
    target = _agent(owner=uuid.uuid4(), tier="config_with_chats", grantee=user)
    trace = _trace(target.id)
    fetched, _searched = _install(monkeypatch, user, target, [trace])
    executor = _executor(user, target)

    await _call(executor, "inspect_agent_traces", target_agent_id=str(target.id))
    payload = await _call(executor, "get_trace_payload", trace_id=str(trace.id))

    assert [f["agent_ids"] for f in fetched] == [[str(target.id)]]
    assert "someone else's answer" in str(payload.content)


async def test_the_check_reads_the_rule_of_the_chats_tab():
    user = _user()
    readable = _agent(owner=user.id)
    company = _agent(owner=uuid.uuid4(), company_managed=True)
    db = _DB([readable, company])
    ids = [str(readable.id), str(company.id), "not-a-uuid"]
    assert await traces_module.agents_whose_traces_user_may_read(db, user, ids, []) == {str(readable.id)}
    assert await traces_module.agents_whose_traces_user_may_read(db, None, ids, []) == set()
