"""A chained assignment is triggered as its own agent.

``trigger_assignment`` decides the new run's enabled gate, visibility and
deadline from the agent it is handed. The chain dispatch in
``execute_assignment_run`` used to hand it the agent whose run had just
finished, which is wrong whenever ``next_assignment_id`` points at another
agent's assignment. No database.
"""

import ast
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.services.assignment_engine as engine


def _agent(**over) -> SimpleNamespace:
    base = dict(id=uuid.uuid4(), name="Agent", enabled=True, hide_runs=False)
    base.update(over)
    return SimpleNamespace(**base)


def _assignment(agent_id, **over) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="Next step",
        enabled=True,
        is_company_managed=True,
        hide_runs=None,
        run_as_all_users=False,
        run_as_user_id=None,
        user_id=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _AgentLookup:
    """Session double answering the one agent lookup, counting queries."""

    def __init__(self, agent=None):
        self._agent = agent
        self.queries = 0

    async def execute(self, statement):
        self.queries += 1
        return _Result(self._agent)


@pytest.mark.asyncio
async def test_a_same_agent_chain_keeps_the_agent_and_asks_nothing():
    """The common case is untouched: the finished run's own agent object is
    handed on and no query is made."""
    agent = _agent()
    db = _AgentLookup()
    assert await engine._resolve_chain_agent(db, agent, _assignment(agent.id)) is agent
    assert db.queries == 0


@pytest.mark.asyncio
async def test_a_cross_agent_chain_runs_as_the_next_assignments_agent():
    finished = _agent(name="Collector")
    target = _agent(name="Reporter")
    db = _AgentLookup(target)
    assert await engine._resolve_chain_agent(db, finished, _assignment(target.id)) is target
    assert db.queries == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("target", "reason"), [(None, "missing"), (_agent(enabled=False), "disabled")])
async def test_a_chain_into_a_missing_or_disabled_agent_is_not_dispatched(caplog, target, reason):
    """Skipped with an INFO line, like a disabled next assignment - never run
    under the finished run's agent."""
    db = _AgentLookup(target)
    with caplog.at_level(logging.INFO, logger=engine.logger.name):
        assert await engine._resolve_chain_agent(db, _agent(), _assignment(uuid.uuid4())) is None
    assert f"its agent is {reason}" in caplog.text


class _TriggerDB:
    def __init__(self):
        self.added: list = []

    async def execute(self, statement):
        raise AssertionError(f"unexpected query: {statement}")

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()


@pytest.mark.asyncio
async def test_the_chained_run_takes_its_visibility_from_its_own_agent(monkeypatch):
    """End to end through the real ``trigger_assignment``: the next agent hides
    its runs, the finished one does not. The chained run is born hidden, as
    every other run of that agent is."""

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(engine, "_refuse_if_ai_disabled", _noop)
    monkeypatch.setattr(engine, "_schedule_run_execution", _noop)
    finished = _agent(name="Collector", hide_runs=False)
    target = _agent(name="Reporter", hide_runs=True)
    next_assign = _assignment(target.id)

    run_as = await engine._resolve_chain_agent(_AgentLookup(target), finished, next_assign)
    db = _TriggerDB()
    await engine.trigger_assignment(next_assign, run_as, db=db, trigger_context={"type": "chain"}, skip_if_active=False)
    (run,) = db.added
    assert run.agent_id == target.id
    assert run.is_company_managed is False


def test_the_chain_dispatch_hands_trigger_assignment_the_resolved_agent():
    """``execute_assignment_run`` triggers the next assignment with the agent
    ``_resolve_chain_agent`` returned, never the finished run's ``agent``."""
    tree = ast.parse(Path(engine.__file__).read_text(encoding="utf-8"))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "execute_assignment_run")
    chains = [
        c
        for c in ast.walk(node)
        if isinstance(c, ast.Call)
        and ast.unparse(c.func) == "trigger_assignment"
        and any(k.arg == "payload" and ast.unparse(k.value) == "chain_payload" for k in c.keywords)
    ]
    assert len(chains) == 1, "the chain no longer calls trigger_assignment"
    passed = {k.arg: ast.unparse(k.value) for k in chains[0].keywords}
    assert passed.get("agent") == "next_agent"
    built = [
        ast.unparse(a.value)
        for a in ast.walk(node)
        if isinstance(a, ast.Assign) and any(ast.unparse(t) == "next_agent" for t in a.targets)
    ]
    assert built and all("_resolve_chain_agent(" in value for value in built)
