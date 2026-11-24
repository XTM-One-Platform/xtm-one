"""A supervisor lists and disposes of the frictions of the agents it may supervise (#1928).

The grant is the trace supervision one: the agent's
``trace_supervision_target_agent_ids``, or every agent for an admin whose agent
carries ``inspect_agent_traces``. The store is the in-memory fake of
``test_frictions``.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.dialects import postgresql

from app.services import frictions
from app.services.builtin_tools import build_friction_tools
from app.services.builtin_tools import frictions as friction_tools
from app.tools.base import ToolCall
from tests.test_frictions import published, store  # noqa: F401  (fixtures)

SUPERVISOR = uuid.uuid4()
MEMBER = uuid.uuid4()
USER = uuid.uuid4()
MEMBER_USER = uuid.uuid4()
MEMBER_RUN = uuid.uuid4()


def _sql(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_the_clause_keeps_own_alone_without_a_grant():
    own = frictions.AgentFriction.user_id == USER
    assert frictions.supervision_clause(own, ()) is own
    assert frictions.supervision_clause(None, ()) is None


def test_the_clause_adds_the_supervised_agents():
    own = frictions.AgentFriction.user_id == USER
    sql = _sql(frictions.supervision_clause(own, [MEMBER]))
    assert " OR " in sql and str(MEMBER) in sql and str(USER) in sql
    assert str(MEMBER) in _sql(frictions.supervision_clause(None, [MEMBER]))


def test_the_admin_surface_sees_every_friction():
    assert _sql(frictions.supervision_clause(None, None)) == "true"


def _member_friction():
    scope = frictions.make_scope(MEMBER, user_id=MEMBER_USER, run_id=MEMBER_RUN)
    friction, _ = asyncio.run(frictions.record_friction(scope, kind="tool_error", tool="t", evidence="Error: 403"))
    return friction


def test_a_supervised_agents_friction_can_be_disposed(store):  # noqa: F811
    friction = _member_friction()
    with pytest.raises(frictions.FrictionNotFoundError):
        asyncio.run(
            frictions.dispose_friction(
                friction["id"], agent_id=SUPERVISOR, user_id=USER, friction_class="harness", disposition="accepted"
            )
        )
    done = asyncio.run(
        frictions.dispose_friction(
            friction["id"],
            agent_id=SUPERVISOR,
            user_id=USER,
            friction_class="platform_acceptable",
            disposition="accepted",
            supervised_agent_ids=[MEMBER],
        )
    )
    assert done["id"] == friction["id"] and store.rows[0].disposed_at is not None


def test_the_admin_surface_disposes_any_friction(store):  # noqa: F811
    friction = _member_friction()
    done = asyncio.run(
        frictions.dispose_friction(
            friction["id"],
            agent_id=SUPERVISOR,
            user_id=USER,
            friction_class="platform_acceptable",
            disposition="accepted",
            supervised_agent_ids=None,
        )
    )
    assert done["id"] == friction["id"]


def _call(executor, name, **arguments):
    return asyncio.run(executor.execute_tool(ToolCall(id=name, name=name, arguments=arguments))).content


def test_a_run_outside_the_grant_answers_not_visible_rather_than_none(store):  # noqa: F811
    _member_friction()
    _, executor = build_friction_tools(str(USER), str(SUPERVISOR))
    answer = _call(executor, "list_frictions", run_id=str(MEMBER_RUN))
    assert "not visible to you" in answer and not answer.startswith("No ")
    # A run with no friction at all still answers none.
    empty = _call(executor, "list_frictions", run_id=str(uuid.uuid4()))
    assert empty.startswith("No friction recorded")


def test_the_tools_pass_the_grant_to_the_store(store, monkeypatch):  # noqa: F811
    friction = _member_friction()
    seen: list = []

    async def fake_list(**kwargs):
        seen.append(kwargs["supervised_agent_ids"])
        return [], 0

    monkeypatch.setattr(frictions, "list_frictions", fake_list)
    _, executor = build_friction_tools(str(USER), str(SUPERVISOR), supervision_target_agent_ids=[str(MEMBER)])
    _call(executor, "list_frictions", agent_id=str(MEMBER))
    done = _call(
        executor,
        "dispose_friction",
        id=str(friction["id"]),
        friction_class="platform_acceptable",
        disposition="accepted",
    )
    assert seen == [[MEMBER]]
    assert done.startswith("Disposed")


def test_no_grant_reaches_no_other_agent(monkeypatch):
    async def never(*_args, **_kwargs):  # pragma: no cover - must not be called
        raise AssertionError("no grant, no lookup")

    monkeypatch.setattr(friction_tools, "_supervised_agents", never)
    _, executor = build_friction_tools(str(USER), str(SUPERVISOR))
    assert executor is not None


def test_platform_health_never_supervises(monkeypatch):
    import app.services.builtin_tools.traces as traces

    monkeypatch.setattr(traces, "_health_refuses_other_agents", lambda: True)
    reach = asyncio.run(friction_tools._supervised_agents(str(USER), str(SUPERVISOR), [str(MEMBER)], True))
    assert reach == []


def test_the_allow_list_drops_the_agent_itself_and_bad_ids(monkeypatch):
    import app.services.builtin_tools.traces as traces

    monkeypatch.setattr(traces, "_health_refuses_other_agents", lambda: False)
    reach = asyncio.run(
        friction_tools._supervised_agents(str(USER), str(SUPERVISOR), [str(MEMBER), str(SUPERVISOR), "nope"], False)
    )
    assert reach == [MEMBER]
