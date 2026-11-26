"""A supervisor reads a supervised agent's run by its run id (dev:supervised-run-id-trace-lookup).

``get_trace_payload`` and ``inspect_agent_traces(trace_id=...)`` resolve a run id
(full or 8+ hex prefix) among the root traces of the agents the caller may
supervise; a caller without the grant gets the single "not found" answer and
no audit row.
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


@pytest.fixture(autouse=True)
def readable_targets(monkeypatch):
    """Every allow-listed target is one whose chats the caller may read: the
    narrowing to readable targets is pinned by ``test_shared_agent_trace_reach.py``."""

    async def _readable(_db, _user, agent_ids, _group_ids=None):
        return {str(uuid.UUID(str(i))) for i in agent_ids}

    monkeypatch.setattr(traces_module, "agents_whose_traces_user_may_read", _readable)


def _trace(agent_id: str, *, run_id: uuid.UUID | None = None, minutes_ago: int = 60, **overrides):
    now = datetime.now(UTC)
    base = dict(
        id=uuid.uuid4(),
        agent_id=uuid.UUID(agent_id),
        assignment_run_id=run_id,
        source="assignment",
        name="assignment:run",
        model="m",
        status="ok",
        started_at=now - timedelta(minutes=minutes_ago),
        ended_at=now - timedelta(minutes=minutes_ago - 1),
        duration_ms=60_000.0,
        input_tokens=100,
        output_tokens=10,
        total_tokens=110,
        total_cost=None,
        error_message=None,
        input=None,
        output="the supervised run's answer",
        metadata_json=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _SessionCtx:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_exc):
        return False


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

    async def list_traces(self, **_kwargs):
        return [], 0


def _install(monkeypatch, traces, *, is_admin: bool = False):
    """Every trace of *traces* with a run id is a root trace of that run."""
    store = _Store(traces)
    searched: list[dict] = []
    audits: list[dict] = []
    monkeypatch.setattr(traces_module, "async_session", lambda: _SessionCtx())
    monkeypatch.setattr(traces_module, "build_trace_read_store", lambda: store)
    monkeypatch.setattr(traces_module, "_health_refuses_other_agents", lambda: False)
    monkeypatch.setattr(traces_module, "_own_live_run", lambda _agent: None)

    async def _user(_db, _uid):
        return SimpleNamespace(id=uuid.uuid4(), email="sup@example.com", is_admin=is_admin), []

    async def _none_gone(_db, _ids):
        return set()

    async def _no_own_run(_db, _value, *, own_agent_id):
        return None, None

    async def _no_prefix(_db, _prefix, _value, **_kwargs):
        return None, None

    async def _run_roots(_db, value, *, viewer, group_ids, agent_ids):
        searched.append({"value": value, "agent_ids": agent_ids})
        raw = (value or "").strip().lower()
        found = [
            t
            for t in sorted(traces, key=lambda t: t.started_at)
            if t.assignment_run_id is not None
            and str(t.assignment_run_id).startswith(raw)
            and (agent_ids is None or str(t.agent_id) in agent_ids)
        ]
        return [(t.id, t.name) for t in found]

    async def _audit(_db, _user, **kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(traces_module, "_load_user_context", _user)
    monkeypatch.setattr(traces_module, "_agents_gone", _none_gone)
    monkeypatch.setattr(traces_module, "_own_trace_of_run", _no_own_run)
    monkeypatch.setattr(traces_module, "_resolve_trace_prefix", _no_prefix)
    monkeypatch.setattr(traces_module, "_supervised_run_root_traces", _run_roots)
    monkeypatch.setattr(traces_module, "_audit_trace_read", _audit)
    return searched, audits


def _executor(*, supervised: list[str], allow_cross_agent: bool = True):
    _defs, executor = traces_module.build_trace_tools(
        db=object(),
        user_id=str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()),
        supervision_target_agent_ids=supervised,
        allow_cross_agent=allow_cross_agent,
    )
    return executor


async def _call(executor, name, **arguments):
    return await executor.execute_tool(ToolCall(id="c", name=name, arguments=arguments))


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", [False, True])
async def test_a_supervisor_reads_a_supervised_runs_payload_by_its_run_id(monkeypatch, prefix):
    target = str(uuid.uuid4())
    run_id = uuid.uuid4()
    trace = _trace(target, run_id=run_id)
    _searched, audits = _install(monkeypatch, [trace])
    executor = _executor(supervised=[target])

    value = str(run_id)[:8] if prefix else str(run_id)
    result = await _call(executor, traces_module.TRACE_PAYLOAD_TOOL_NAME, trace_id=value)

    assert result.is_error is False, result.content
    assert "the supervised run's answer" in json.loads(result.content)["content"]
    assert [a["details"]["trace_id"] for a in audits] == [str(trace.id)]


@pytest.mark.asyncio
async def test_a_resumed_supervised_run_is_refused_naming_each_trace(monkeypatch):
    target = str(uuid.uuid4())
    run_id = uuid.uuid4()
    first = _trace(target, run_id=run_id, minutes_ago=90)
    second = _trace(target, run_id=run_id, minutes_ago=30)
    _searched, audits = _install(monkeypatch, [first, second])
    executor = _executor(supervised=[target])

    result = await _call(executor, traces_module.TRACE_PAYLOAD_TOOL_NAME, trace_id=str(run_id))

    assert str(first.id) in result.content and str(second.id) in result.content
    assert audits == []


@pytest.mark.asyncio
async def test_without_the_grant_another_agents_run_id_is_not_found_and_not_audited(monkeypatch):
    target = str(uuid.uuid4())
    run_id = uuid.uuid4()
    searched, audits = _install(monkeypatch, [_trace(target, run_id=run_id)])
    executor = _executor(supervised=[], allow_cross_agent=False)

    for value in (str(run_id), str(run_id)[:8]):
        result = await _call(executor, traces_module.TRACE_PAYLOAD_TOOL_NAME, trace_id=value)
        assert result.is_error is True
        assert "trace not found, or not visible to you" in result.content
    assert searched == [] and audits == []


@pytest.mark.asyncio
async def test_a_run_of_an_agent_outside_the_allow_list_is_not_found(monkeypatch):
    target, outsider = str(uuid.uuid4()), str(uuid.uuid4())
    run_id = uuid.uuid4()
    searched, audits = _install(monkeypatch, [_trace(outsider, run_id=run_id)])
    executor = _executor(supervised=[target])

    result = await _call(executor, traces_module.TRACE_PAYLOAD_TOOL_NAME, trace_id=str(run_id))

    assert "trace not found, or not visible to you" in result.content
    assert searched and searched[0]["agent_ids"] == [target]
    assert audits == []


@pytest.mark.asyncio
async def test_an_admin_with_the_supervision_surface_searches_every_agent(monkeypatch):
    target = str(uuid.uuid4())
    run_id = uuid.uuid4()
    searched, _audits = _install(monkeypatch, [_trace(target, run_id=run_id)], is_admin=True)
    monkeypatch.setattr(traces_module, "agent_tool_admin_bypass", lambda user: bool(user.is_admin))
    executor = _executor(supervised=[])

    result = await _call(executor, traces_module.TRACE_PAYLOAD_TOOL_NAME, trace_id=str(run_id))

    assert result.is_error is False, result.content
    assert searched[0]["agent_ids"] is None


@pytest.mark.asyncio
async def test_inspect_agent_traces_lists_every_root_trace_of_a_run_oldest_first(monkeypatch):
    target = str(uuid.uuid4())
    run_id = uuid.uuid4()
    first = _trace(target, run_id=run_id, minutes_ago=90)
    second = _trace(target, run_id=run_id, minutes_ago=30)
    _searched, audits = _install(monkeypatch, [second, first])
    executor = _executor(supervised=[target])

    result = await _call(
        executor, "inspect_agent_traces", target_agent_id=target, trace_id=str(run_id)[:8], rollup_only=True
    )

    assert result.is_error is False, result.content
    listing = json.loads(result.content)
    assert [t["id"] for t in listing["traces"]] == [str(first.id), str(second.id)]
    assert {t["run_id"] for t in listing["traces"]} == {str(run_id)}
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_inspect_agent_traces_opens_one_trace_by_its_id(monkeypatch):
    target = str(uuid.uuid4())
    trace = _trace(target, run_id=uuid.uuid4())
    _install(monkeypatch, [trace])
    executor = _executor(supervised=[target])

    result = await _call(
        executor, "inspect_agent_traces", target_agent_id=target, trace_id=str(trace.id), rollup_only=True
    )

    assert result.is_error is False, result.content
    assert [t["id"] for t in json.loads(result.content)["traces"]] == [str(trace.id)]


@pytest.mark.asyncio
async def test_inspect_agent_traces_trace_id_outside_the_scope_is_not_found_without_audit(monkeypatch):
    target, outsider = str(uuid.uuid4()), str(uuid.uuid4())
    trace = _trace(outsider, run_id=uuid.uuid4())
    _searched, audits = _install(monkeypatch, [trace])
    executor = _executor(supervised=[target])

    for value in (str(trace.id), str(trace.assignment_run_id)):
        result = await _call(executor, "inspect_agent_traces", target_agent_id=target, trace_id=value)
        assert "trace not found, or not visible to you" in result.content
    assert audits == []


@pytest.mark.asyncio
async def test_a_run_with_eight_root_traces_is_listed_whole_oldest_first(monkeypatch):
    """A run resumed through a series of rollouts has more root traces than the
    candidate cap of an id prefix (5): every one is listed, the newest (the
    attempt that holds the outcome) included (dev:supervised-run-id-trace-lookup-retest)."""
    target = str(uuid.uuid4())
    run_id = uuid.uuid4()
    attempts = [_trace(target, run_id=run_id, minutes_ago=200 - 10 * i) for i in range(8)]
    _searched, audits = _install(monkeypatch, list(reversed(attempts)))
    executor = _executor(supervised=[target])

    result = await _call(
        executor, "inspect_agent_traces", target_agent_id=target, trace_id=str(run_id), rollup_only=True
    )

    assert result.is_error is False, result.content
    listing = json.loads(result.content)
    assert [t["id"] for t in listing["traces"]] == [str(t.id) for t in attempts]
    assert listing["total"] == 8 and "run_note" not in listing
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_the_refusal_of_a_run_with_eight_traces_names_every_one_and_the_listing(monkeypatch):
    target = str(uuid.uuid4())
    run_id = uuid.uuid4()
    attempts = [_trace(target, run_id=run_id, minutes_ago=200 - 10 * i) for i in range(8)]
    _searched, audits = _install(monkeypatch, attempts)
    executor = _executor(supervised=[target])

    result = await _call(executor, traces_module.TRACE_PAYLOAD_TOOL_NAME, trace_id=str(run_id))

    assert all(str(t.id) in result.content for t in attempts)
    assert "8 root traces" in result.content and "and more" not in result.content
    assert f"The newest, `{attempts[-1].id}`" in result.content
    assert f"inspect_agent_traces(trace_id={run_id})" in result.content
    assert audits == []


def test_a_run_past_the_bound_keeps_the_newest_and_says_it_was_cut():
    bound = traces_module.RUN_ROOT_TRACES_MAX
    roots = [(uuid.uuid4(), f"a{i}") for i in range(bound + 1)]
    shown, note = traces_module._run_page(roots)
    assert len(shown) == bound and shown[-1] == roots[-1] and shown[0] == roots[0]
    assert f"more than {bound} root traces" in note
    assert traces_module._run_page(roots[:bound]) == (roots[:bound], "")
    refusal = traces_module._run_traces_refusal("abcdef12", roots, listing_tool="inspect_my_traces")
    assert f"more than {bound}" in refusal and str(roots[-1][0]) in refusal
    assert "inspect_my_traces(trace_id=abcdef12)" in refusal


def test_the_run_query_reads_past_the_prefix_candidate_cap():
    import asyncio

    from sqlalchemy.dialects import postgresql

    captured: list = []

    class _Db:
        async def execute(self, stmt):
            captured.append(stmt)
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []), all=lambda: [])

    viewer = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    asyncio.run(
        traces_module._supervised_run_root_traces(
            _Db(), str(uuid.uuid4()), viewer=viewer, group_ids=[], agent_ids=[str(uuid.uuid4())]
        )
    )
    asyncio.run(traces_module._own_run_root_traces(_Db(), str(uuid.uuid4()), own_agent_id=str(uuid.uuid4())))
    for stmt in captured:
        compiled = stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        assert f"LIMIT {traces_module.RUN_ROOT_TRACES_MAX + 1}" in str(compiled)
    assert len(captured) == 2


def test_a_listed_trace_carries_its_run_id():
    run_id = uuid.uuid4()
    assert traces_module._trace_summary(_trace(str(uuid.uuid4()), run_id=run_id))["run_id"] == str(run_id)
    assert "run_id" not in traces_module._trace_summary(_trace(str(uuid.uuid4())))


def test_the_supervised_run_query_reads_visible_root_traces_of_the_named_agents():
    import asyncio

    from sqlalchemy.dialects import postgresql

    captured: list = []

    class _Db:
        async def execute(self, stmt):
            captured.append(stmt)
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    viewer = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    target = str(uuid.uuid4())
    found = asyncio.run(
        traces_module._supervised_run_root_traces(
            _Db(), str(uuid.uuid4()), viewer=viewer, group_ids=[], agent_ids=[target]
        )
    )
    assert found == []
    sql = str(captured[0].compile(dialect=postgresql.dialect()))
    assert "assignment_run_id" in sql and "parent_trace_id IS NULL" in sql and "agent_id IN" in sql

    no_id = traces_module._supervised_run_root_traces(_Db(), "not-an-id", viewer=viewer, group_ids=[], agent_ids=None)
    assert asyncio.run(no_id) == []
    no_agent = traces_module._supervised_run_root_traces(
        _Db(), str(uuid.uuid4()), viewer=viewer, group_ids=[], agent_ids=[]
    )
    assert asyncio.run(no_agent) == []
    assert len(captured) == 1
