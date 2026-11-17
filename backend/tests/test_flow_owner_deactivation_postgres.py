"""A deactivated owner's flows are paused unless company-managed, against
PostgreSQL.

The same rule as the assignments', generalised: a SQL form the flow
sweeps and listener queries filter on, an instance form the run paths ask, and
nothing written when it applies. Only a real database shows that the forms
agree, that the sweeps' own queries apply it, that a queued run ends before it
starts, and that reactivating the account resumes everything as it was.

The cast, seeded fresh in each test:

* ``owner``: a non-admin whose PRIVATE flow is shared with a group as
  ``config``; deactivated by each test.
* ``company_owner``: the admin who created a COMPANY-MANAGED flow (the creator
  is kept on ``user_id``, as ``toggle_company_managed_list_field`` does); also
  deactivated, which must change nothing for it.
* ``member``: an active non-admin in that group; ``admin``: an active admin;
  ``other``: an active non-admin with a private flow of their own.

Same scratch-database recipe as ``test_flow_runner_postgres.py``, the app's own
session factories bound to it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.requests import Request

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.flow import FLOW_STATUS_ACTIVE, Flow
from app.models.flow_event import FlowEventType
from app.models.flow_run import FlowRun, FlowRunStep
from app.models.group import Group, GroupMember
from app.models.user import User

# Imported at collection, not first inside a test: ``flow_events`` imports it
# lazily, and a first import while a test patches ``flow_runner.create_flow_run``
# binds the patch into it for the rest of the session (tests run in random order).
from app.services import flow_triggers as _imported_at_collection  # noqa: F401
from app.services.assignment_pause import (
    FLOW_OWNER_DEACTIVATED_MESSAGE,
    FLOW_OWNER_DEACTIVATED_RUN_ERROR,
    OWNER_DEACTIVATED_CODE,
    is_flow_paused_by_deactivated_owner,
    is_paused_by_deactivated_owner,
    owner_not_deactivated,
    paused_entity_ids,
)
from app.services.flow_runner import FlowWalker, create_flow_run
from tests.agentic_gate_stub import stub_agentic_gate
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "flowpause",
        tables=ALL_TABLES,
        extensions=("vector",),
    ) as db:
        yield db.sessions


@pytest.fixture(autouse=True)
def _quiet_side_channels(monkeypatch):
    """AI may run, and no wake is published to a Redis the test has none of."""
    from app.services import orchestration_engine

    stub_agentic_gate(monkeypatch)
    monkeypatch.setattr(orchestration_engine, "notify_orchestration_wake", AsyncMock())


def _node(node_id, kind, **config):
    return {"id": node_id, "kind": kind, "position": {"x": 0, "y": 0}, "config": config}


def _graph(trigger_kind: str, **trigger_config) -> dict:
    """A trigger followed by a deterministic node: valid, runnable, no AI."""
    return {
        "nodes": [
            _node("t1", trigger_kind, **trigger_config),
            _node("c1", "condition.if", left="x", operator="is_not_empty"),
        ],
        "edges": [
            {
                "id": "e-t1-c1",
                "source": "t1",
                "source_handle": "out",
                "target": "c1",
                "target_handle": "in",
                "kind": "data",
                "label": "",
            }
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }


@dataclass
class World:
    users: dict[str, User]
    group_id: str


async def _seed(sessionmaker: async_sessionmaker) -> World:
    async with sessionmaker() as db:
        users = {
            name: User(
                email=f"{name}-{uuid.uuid4().hex[:8]}@example.com",
                hashed_password="x",
                full_name=name,
                role="admin" if name in ("admin", "company_owner") else "user",
            )
            for name in ("owner", "company_owner", "member", "admin", "other")
        }
        db.add_all(users.values())
        await db.flush()
        group = Group(name=f"team-{uuid.uuid4().hex[:8]}")
        db.add(group)
        await db.flush()
        db.add(GroupMember(group_id=group.id, user_id=users["member"].id))
        await db.commit()
    return World(users=users, group_id=str(group.id))


async def _add_flow(
    sessionmaker: async_sessionmaker, world: World, kind: str, graph: dict, **overrides
) -> Flow:
    """``private`` (owner, group-shared), ``company`` or ``other``."""
    owner = {"private": "owner", "company": "company_owner", "other": "other"}[kind]
    fields = {
        "user_id": world.users[owner].id,
        "name": f"{kind}-{uuid.uuid4().hex[:6]}",
        "graph": graph,
        "status": FLOW_STATUS_ACTIVE,
        "is_company_managed": kind == "company",
    }
    if kind == "private":
        fields["shared_group_ids"] = [world.group_id]
        fields["shared_group_permissions"] = {world.group_id: "config"}
    fields.update(overrides)
    flow = Flow(**fields)
    async with sessionmaker() as db:
        db.add(flow)
        await db.commit()
    return flow


async def _set_active(sessionmaker, world: World, active: bool, *names: str) -> None:
    async with sessionmaker() as db:
        for name in names:
            user = await db.get(User, world.users[name].id)
            user.is_active = active
        await db.commit()


async def _user(db, world: World, name: str) -> User:
    return await db.get(User, world.users[name].id)


async def _runs_of(sessionmaker, flow: Flow) -> list[FlowRun]:
    async with sessionmaker() as db:
        return list(
            (await db.execute(select(FlowRun).where(FlowRun.flow_id == flow.id)))
            .scalars()
            .all()
        )


def _request() -> Request:
    return Request(
        {"type": "http", "method": "POST", "path": "/x", "headers": [], "client": ("127.0.0.1", 1)}
    )


def _assert_refused(excinfo) -> None:
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == OWNER_DEACTIVATED_CODE
    assert excinfo.value.detail["message"] == FLOW_OWNER_DEACTIVATED_MESSAGE


# ===================================================================
# The rule itself: the forms agree for flows
# ===================================================================


async def test_the_sql_batch_and_instance_forms_agree_for_flows():
    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        graph = _graph("trigger.manual")
        cases = {
            "private of a deactivated owner": (await _add_flow(sessionmaker, world, "private", graph), True),
            # Shared with a person rather than a group: paused all the same.
            "shared with a person, deactivated owner": (
                await _add_flow(
                    sessionmaker,
                    world,
                    "private",
                    graph,
                    shared_group_ids=None,
                    shared_group_permissions=None,
                    shared_user_ids=[str(world.users["member"].id)],
                    shared_user_permissions={str(world.users["member"].id): "config"},
                ),
                True,
            ),
            "private of an active owner": (await _add_flow(sessionmaker, world, "other", graph), False),
            "company-managed of a deactivated admin": (await _add_flow(sessionmaker, world, "company", graph), False),
            "private without an owner": (
                await _add_flow(sessionmaker, world, "private", graph, user_id=None),
                False,
            ),
        }
        await _set_active(sessionmaker, world, False, "owner", "company_owner")

        async with sessionmaker() as db:
            runnable = set(
                (await db.execute(select(Flow.id).where(owner_not_deactivated(Flow)))).scalars().all()
            )
            rows = (
                (await db.execute(select(Flow).where(Flow.id.in_([f.id for f, _ in cases.values()]))))
                .scalars()
                .all()
            )
            paused = await paused_entity_ids(db, rows)
            for row in rows:
                label = next(k for k, (f, _) in cases.items() if f.id == row.id)
                expected = cases[label][1]
                assert (row.id not in runnable) is expected, label
                assert await is_paused_by_deactivated_owner(db, row) is expected, label
                assert (row.id in paused) is expected, label
                assert await is_flow_paused_by_deactivated_owner(db, row.id) is expected, label


# ===================================================================
# Automatic triggers
# ===================================================================


@pytest.mark.parametrize(
    "trigger_kind", ["trigger.schedule", "trigger.integration_event", "trigger.work_item"]
)
async def test_the_trigger_sweeps_do_not_select_a_paused_flow(trigger_kind):
    """The schedule tick, the integration-event poller and the work-item
    trigger all pick their flows through ``_active_flows_with_trigger``."""
    from app.services.flow_triggers import _active_flows_with_trigger

    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        graph = _graph(trigger_kind)
        paused = await _add_flow(sessionmaker, world, "private", graph)
        company = await _add_flow(sessionmaker, world, "company", graph)
        other = await _add_flow(sessionmaker, world, "other", graph)
        await _set_active(sessionmaker, world, False, "owner", "company_owner")

        async with sessionmaker() as db:
            picked = {f.id for f, _ in await _active_flows_with_trigger(db, trigger_kind)}
        assert picked == {company.id, other.id}

        await _set_active(sessionmaker, world, True, "owner")
        async with sessionmaker() as db:
            picked = {f.id for f, _ in await _active_flows_with_trigger(db, trigger_kind)}
        assert picked == {paused.id, company.id, other.id}


async def test_the_schedule_tick_skips_only_the_paused_flow_until_reactivation():
    from app.services.flow_triggers import process_scheduled_flows

    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        graph = _graph("trigger.schedule", cron="* * * * *")
        an_hour_ago = datetime.now(UTC) - timedelta(hours=1)
        paused = await _add_flow(sessionmaker, world, "private", graph, last_triggered_at=an_hour_ago)
        company = await _add_flow(sessionmaker, world, "company", graph, last_triggered_at=an_hour_ago)
        other = await _add_flow(sessionmaker, world, "other", graph, last_triggered_at=an_hour_ago)
        await _set_active(sessionmaker, world, False, "owner", "company_owner")

        async with sessionmaker() as db:
            assert await process_scheduled_flows(db) == 2
        assert await _runs_of(sessionmaker, paused) == []
        assert len(await _runs_of(sessionmaker, company)) == 1
        assert len(await _runs_of(sessionmaker, other)) == 1
        async with sessionmaker() as db:
            reloaded = await db.get(Flow, paused.id)
            # Nothing written: still active, and the window it missed is open.
            assert reloaded.status == FLOW_STATUS_ACTIVE
            assert reloaded.last_triggered_at == an_hour_ago

        await _set_active(sessionmaker, world, True, "owner")
        async with sessionmaker() as db:
            await process_scheduled_flows(db)
        runs = await _runs_of(sessionmaker, paused)
        assert [r.trigger_type for r in runs] == ["schedule"]


async def test_a_flow_event_does_not_start_a_paused_listener(monkeypatch):
    from app.core.scopes import EVENT_SCOPE_FLOW, EVENT_SCOPE_GLOBAL
    from app.services import flow_events

    monkeypatch.setattr(flow_events, "_debounce_allows", AsyncMock(return_value=True))

    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        async with sessionmaker() as db:
            event_type = FlowEventType(user_id=world.users["owner"].id, name="ticket.opened", fields=[])
            db.add(event_type)
            await db.commit()
        graph = _graph("trigger.event", event_type_id=str(event_type.id))
        paused = await _add_flow(sessionmaker, world, "private", graph)
        company = await _add_flow(sessionmaker, world, "company", graph)
        await _set_active(sessionmaker, world, False, "owner", "company_owner")

        async def _emit(scope: str, flow_id) -> set[str]:
            async with sessionmaker() as db:
                result = await flow_events.emit_flow_event(
                    db,
                    event_type=event_type,
                    user_id=world.users["owner"].id,
                    flow_id=flow_id,
                    run_id=None,
                    scope=scope,
                    payload={},
                    chain_depth=0,
                )
                await db.commit()
            async with sessionmaker() as db:
                rows = await db.execute(
                    select(FlowRun.flow_id).where(FlowRun.id.in_([uuid.UUID(r) for r in result.started_run_ids]))
                )
                return {str(f) for f in rows.scalars().all()}

        assert await _emit(EVENT_SCOPE_GLOBAL, None) == {str(company.id)}
        assert await _emit(EVENT_SCOPE_FLOW, paused.id) == set()

        await _set_active(sessionmaker, world, True, "owner")
        assert str(paused.id) in await _emit(EVENT_SCOPE_GLOBAL, None)
        assert await _emit(EVENT_SCOPE_FLOW, paused.id) == {str(paused.id)}


# ===================================================================
# An owner deactivated after a sweep's query picked the flow
# ===================================================================


def _deactivating_create(sessionmaker, world: World):
    """The real ``create_flow_run``, reached after the owner is deactivated:
    the query saw the owner active, the backstop does not."""
    from app.services import flow_runner

    real = flow_runner.create_flow_run

    async def _create(db, flow, **kwargs):
        await _set_active(sessionmaker, world, False, "owner")
        return await real(db, flow, **kwargs)

    return _create


async def test_the_schedule_tick_keeps_the_window_of_a_flow_paused_mid_tick(
    monkeypatch, caplog
):
    import logging

    from app.services import flow_triggers

    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        an_hour_ago = datetime.now(UTC) - timedelta(hours=1)
        paused = await _add_flow(
            sessionmaker,
            world,
            "private",
            _graph("trigger.schedule", cron="* * * * *"),
            last_triggered_at=an_hour_ago,
        )
        monkeypatch.setattr(flow_triggers, "create_flow_run", _deactivating_create(sessionmaker, world))

        with caplog.at_level(logging.INFO):
            async with sessionmaker() as db:
                assert await flow_triggers.process_scheduled_flows(db) == 0
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert await _runs_of(sessionmaker, paused) == []
        async with sessionmaker() as db:
            assert (await db.get(Flow, paused.id)).last_triggered_at == an_hour_ago


async def test_the_work_item_trigger_hands_back_an_item_claimed_as_the_owner_is_deactivated(
    monkeypatch,
):
    from app.services import flow_triggers, work_item
    from app.services.company_managed_writes import flow_writer

    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        paused = await _add_flow(
            sessionmaker, world, "private", _graph("trigger.work_item", owner_agent_id="agent-1")
        )
        monkeypatch.setattr(
            work_item,
            "list_dispatchable_work_items",
            AsyncMock(return_value=[{"id": "item-1", "title": "t"}]),
        )
        monkeypatch.setattr(
            work_item,
            "claim_work_item",
            AsyncMock(return_value={"claimed": True, "work_item": {"id": "item-1", "title": "t"}}),
        )
        release = AsyncMock()
        monkeypatch.setattr(work_item, "release_work_item_claim", release)
        monkeypatch.setattr(flow_triggers, "create_flow_run", _deactivating_create(sessionmaker, world))

        async with sessionmaker() as db:
            assert await flow_triggers.process_work_item_flows(db) == 0
        assert await _runs_of(sessionmaker, paused) == []
        # Back to the queue, no failed attempt counted, released as the flow's
        # tier, the writer it claimed with.
        release.assert_awaited_once_with(
            work_item_id="item-1",
            user_id=str(world.users["owner"].id),
            writer=flow_writer(is_company_managed=False),
        )


async def test_a_listener_refused_as_its_owner_is_deactivated_gives_its_window_back(
    monkeypatch,
):
    from app.core.scopes import EVENT_SCOPE_GLOBAL
    from app.services import flow_events, flow_runner

    monkeypatch.setattr(flow_events, "_debounce_allows", AsyncMock(return_value=True))
    released = AsyncMock()
    monkeypatch.setattr(flow_events, "_release_debounce", released)

    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        async with sessionmaker() as db:
            event_type = FlowEventType(user_id=world.users["owner"].id, name="ticket.opened", fields=[])
            db.add(event_type)
            await db.commit()
        graph = _graph("trigger.event", event_type_id=str(event_type.id), debounce_seconds=60)
        paused = await _add_flow(sessionmaker, world, "private", graph)
        company = await _add_flow(sessionmaker, world, "company", graph)
        monkeypatch.setattr(flow_runner, "create_flow_run", _deactivating_create(sessionmaker, world))

        async with sessionmaker() as db:
            result = await flow_events.emit_flow_event(
                db,
                event_type=event_type,
                user_id=world.users["owner"].id,
                flow_id=None,
                run_id=None,
                scope=EVENT_SCOPE_GLOBAL,
                payload={},
                chain_depth=0,
            )
            await db.commit()
        assert len(result.started_run_ids) == 1
        assert await _runs_of(sessionmaker, paused) == []
        assert len(await _runs_of(sessionmaker, company)) == 1
        released.assert_awaited_once()
        assert released.await_args.args[0] == paused.id


# ===================================================================
# Runs started on request
# ===================================================================


async def test_a_manual_run_is_refused_for_the_member_and_the_admin_until_reactivation():
    from app.api.v1 import flow_runs
    from app.api.v1.schemas.flow import FlowRunRequest

    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        paused = await _add_flow(sessionmaker, world, "private", _graph("trigger.manual"))
        company = await _add_flow(sessionmaker, world, "company", _graph("trigger.manual"))
        await _set_active(sessionmaker, world, False, "owner", "company_owner")

        async def _run(name: str, flow: Flow):
            async with sessionmaker() as db:
                return await flow_runs.run_flow(
                    str(flow.id),
                    FlowRunRequest(),
                    _request(),
                    db=db,
                    user=await _user(db, world, name),
                    _agentic_gate=None,
                )

        for name in ("member", "admin"):
            with pytest.raises(HTTPException) as excinfo:
                await _run(name, paused)
            _assert_refused(excinfo)
        assert await _runs_of(sessionmaker, paused) == []
        # The company-managed flow of a deactivated admin still runs.
        assert (await _run("admin", company)).status == "pending"

        await _set_active(sessionmaker, world, True, "owner")
        assert (await _run("member", paused)).status == "pending"


async def test_the_agent_flow_tool_refuses_to_run_it_until_reactivation():
    from app.services.builtin_tools.flow_builder import build_flow_builder_tool
    from app.tools.base import ToolCall

    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        paused = await _add_flow(sessionmaker, world, "private", _graph("trigger.manual"))
        await _set_active(sessionmaker, world, False, "owner")

        async def _call() -> str:
            async with sessionmaker() as db:
                _, executor = build_flow_builder_tool(db, str(world.users["member"].id))
                result = await executor.execute_tool(
                    ToolCall(id=str(uuid.uuid4()), name="run_flow", arguments={"flow_id": str(paused.id)})
                )
            return result.content if isinstance(result.content, str) else str(result.content)

        body = await _call()
        assert FLOW_OWNER_DEACTIVATED_MESSAGE in body
        assert await _runs_of(sessionmaker, paused) == []

        await _set_active(sessionmaker, world, True, "owner")
        assert "started" in await _call()
        assert len(await _runs_of(sessionmaker, paused)) == 1


async def test_a_sub_flow_call_to_a_paused_flow_fails_its_node_with_the_reason():
    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        callee = await _add_flow(sessionmaker, world, "private", _graph("trigger.manual"))
        company_callee = await _add_flow(sessionmaker, world, "company", _graph("trigger.manual"))

        def _caller_graph(target: Flow) -> dict:
            return {
                "nodes": [_node("t1", "trigger.manual"), _node("call", "flow.call", flow_id=str(target.id))],
                "edges": [
                    {
                        "id": "e-t1-call",
                        "source": "t1",
                        "source_handle": "out",
                        "target": "call",
                        "target_handle": "in",
                        "kind": "data",
                        "label": "",
                    }
                ],
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            }

        # The node resolves its callee without the caller's groups, so the one
        # who can call another user's private flow is an admin.
        caller = await _add_flow(
            sessionmaker, world, "other", _caller_graph(callee), user_id=world.users["admin"].id
        )
        company_caller = await _add_flow(
            sessionmaker, world, "other", _caller_graph(company_callee), user_id=world.users["admin"].id
        )
        await _set_active(sessionmaker, world, False, "owner", "company_owner")

        async def _burst(flow: Flow):
            async with sessionmaker() as db:
                admin = await _user(db, world, "admin")
                run = await create_flow_run(
                    db, flow, trigger_type="manual", trigger_payload={}, trigger_context={"type": "manual"},
                    user_id=admin.id,
                )
                await db.commit()
                outcome = await FlowWalker(db, run, admin).run_burst()
                await db.commit()
                return run, outcome

        run, outcome = await _burst(caller)
        assert outcome.kind == "failed"
        assert FLOW_OWNER_DEACTIVATED_MESSAGE in (run.error or "")
        assert await _runs_of(sessionmaker, callee) == []

        _, outcome = await _burst(company_caller)
        assert outcome.kind == "park"
        assert len(await _runs_of(sessionmaker, company_callee)) == 1


# ===================================================================
# Runs that already exist
# ===================================================================


async def test_a_queued_run_ends_skipped_before_any_step_and_resumes_after_reactivation():
    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        paused = await _add_flow(sessionmaker, world, "private", _graph("trigger.manual"))
        company = await _add_flow(sessionmaker, world, "company", _graph("trigger.manual"))

        async def _queue(flow: Flow, name: str) -> FlowRun:
            async with sessionmaker() as db:
                run = await create_flow_run(
                    db, flow, trigger_type="manual", trigger_payload={}, trigger_context={"type": "manual"},
                    user_id=world.users[name].id,
                )
                await db.commit()
                return run

        async def _burst(run_id, name: str):
            async with sessionmaker() as db:
                run = await db.get(FlowRun, run_id)
                outcome = await FlowWalker(db, run, await _user(db, world, name)).run_burst()
                await db.commit()
                steps = await db.scalar(select(func.count(FlowRunStep.id)).where(FlowRunStep.flow_run_id == run_id))
                return outcome, await db.get(FlowRun, run_id), steps

        # Queued while the owners were active, started after they were deactivated.
        queued = await _queue(paused, "owner")
        company_queued = await _queue(company, "company_owner")
        await _set_active(sessionmaker, world, False, "owner", "company_owner")

        outcome, run, steps = await _burst(queued.id, "owner")
        assert outcome.kind == "skipped"
        assert run.status == "skipped"
        assert run.result_summary == FLOW_OWNER_DEACTIVATED_RUN_ERROR
        assert run.completed_at is not None
        # Never started: no step, so nothing a node would charge.
        assert steps == 0

        outcome, run, _ = await _burst(company_queued.id, "company_owner")
        assert outcome.kind == "completed"

        await _set_active(sessionmaker, world, True, "owner")
        again = await _queue(paused, "owner")
        outcome, run, steps = await _burst(again.id, "owner")
        assert outcome.kind == "completed"
        assert steps >= 1


async def test_a_waiting_parent_is_told_why_its_child_was_skipped():
    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        callee = await _add_flow(sessionmaker, world, "private", _graph("trigger.manual"))
        caller = await _add_flow(
            sessionmaker,
            world,
            "other",
            {
                "nodes": [_node("t1", "trigger.manual"), _node("call", "flow.call", flow_id=str(callee.id))],
                "edges": [
                    {
                        "id": "e-t1-call",
                        "source": "t1",
                        "source_handle": "out",
                        "target": "call",
                        "target_handle": "in",
                        "kind": "data",
                        "label": "",
                    }
                ],
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            },
            user_id=world.users["admin"].id,
        )
        async with sessionmaker() as db:
            admin = await _user(db, world, "admin")
            parent = await create_flow_run(
                db, caller, trigger_type="manual", trigger_payload={}, trigger_context={"type": "manual"},
                user_id=admin.id,
            )
            await db.commit()
            assert (await FlowWalker(db, parent, admin).run_burst()).kind == "park"
            await db.commit()
        # The owner is deactivated while the child is still queued.
        await _set_active(sessionmaker, world, False, "owner")
        async with sessionmaker() as db:
            admin = await _user(db, world, "admin")
            child = (await db.execute(select(FlowRun).where(FlowRun.parent_run_id == parent.id))).scalar_one()
            assert (await FlowWalker(db, child, admin).run_burst()).kind == "skipped"
            await db.commit()
            parent = await db.get(FlowRun, parent.id)
            outcome = await FlowWalker(db, parent, admin).run_burst()
            await db.commit()
        assert outcome.kind == "failed"
        assert FLOW_OWNER_DEACTIVATED_RUN_ERROR in (outcome.error or "")


async def test_an_ask_user_answer_waits_for_the_reactivation():
    from app.api.v1 import flow_runs

    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        paused = await _add_flow(sessionmaker, world, "private", _graph("trigger.manual"))
        async with sessionmaker() as db:
            run = FlowRun(
                flow_id=paused.id,
                flow_version_number=1,
                graph=paused.graph,
                user_id=world.users["owner"].id,
                status="awaiting",
                trigger_type="manual",
                state={"waiting": {"kind": "human", "node": "ask"}},
                shared_group_ids=[world.group_id],
            )
            db.add(run)
            await db.commit()
        await _set_active(sessionmaker, world, False, "owner")

        async def _answer(name: str):
            async with sessionmaker() as db:
                return await flow_runs.respond_flow_run(
                    str(run.id), {"response": "yes"}, db=db,
                    user=await _user(db, world, name), _agentic_gate=None,
                )

        with pytest.raises(HTTPException) as excinfo:
            await _answer("admin")
        _assert_refused(excinfo)
        async with sessionmaker() as db:
            assert not (await db.get(FlowRun, run.id)).state["waiting"].get("released")

        await _set_active(sessionmaker, world, True, "owner")
        await _answer("admin")
        async with sessionmaker() as db:
            assert (await db.get(FlowRun, run.id)).state["waiting"]["released"] is True


# ===================================================================
# What people see
# ===================================================================


async def test_the_list_detail_and_chat_entries_report_the_pause_to_member_and_admin():
    from app.api.v1 import flows as flows_api

    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        chat_graph = {
            "nodes": [
                _node("chat", "trigger.chat"),
                _node("a1", "agent", agent_id=str(uuid.uuid4()), chat_enabled=True),
            ],
            "edges": [
                {
                    "id": "e-chat-a1",
                    "source": "chat",
                    "source_handle": "out",
                    "target": "a1",
                    "target_handle": "in",
                    "kind": "data",
                    "label": "",
                }
            ],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }
        paused = await _add_flow(sessionmaker, world, "private", chat_graph)
        company = await _add_flow(sessionmaker, world, "company", chat_graph)
        await _set_active(sessionmaker, world, False, "owner", "company_owner")

        async def _seen(name: str) -> tuple[dict[str, bool], bool, set[str]]:
            async with sessionmaker() as db:
                user = await _user(db, world, name)
                # The admin is in no group: a colleague's flow is read through
                # scope=all, what a page linking to it sends.
                scope = "all" if name == "admin" else "visible"
                listed = {
                    f.id: f.paused_by_owner_deactivation
                    for f in await flows_api.list_flows(scope=scope, user=user, db=db)
                }
                detail = await flows_api.get_flow(str(paused.id), user=user, db=db)
                entries = await flows_api.list_flow_chat_entries(user=user, db=db)
                return listed, detail.paused_by_owner_deactivation, {e.flow_id for e in entries}

        for name in ("member", "admin"):
            listed, detail, entries = await _seen(name)
            assert listed[str(paused.id)] is True, name
            assert listed[str(company.id)] is False, name
            assert detail is True, name
            assert str(paused.id) not in entries, name
            assert str(company.id) in entries, name

        # The admin's library list stays three-tier: the colleague's
        # paused flow is not in it, the company-managed one is, still flagged.
        async with sessionmaker() as db:
            library = {
                f.id: f.paused_by_owner_deactivation
                for f in await flows_api.list_flows(
                    scope="visible", user=await _user(db, world, "admin"), db=db
                )
            }
        assert str(paused.id) not in library
        assert library[str(company.id)] is False

        await _set_active(sessionmaker, world, True, "owner")
        listed, detail, entries = await _seen("member")
        assert listed[str(paused.id)] is False
        assert detail is False
        assert str(paused.id) in entries


async def test_activating_or_deactivating_a_paused_flow_answers_that_it_is_paused():
    """The flow a group member (or an admin) activates is still paused: the
    answer says so, like the detail route does."""
    from app.api.v1 import flow_runs as flow_runs_api

    async with _temp_database() as sessionmaker:
        world = await _seed(sessionmaker)
        paused = await _add_flow(
            sessionmaker,
            world,
            "private",
            _graph("trigger.schedule", cron="0 3 * * *"),
            status="draft",
        )
        await _set_active(sessionmaker, world, False, "owner")

        for name in ("member", "admin"):
            async with sessionmaker() as db:
                user = await _user(db, world, name)
                activated = await flow_runs_api.activate_flow(
                    str(paused.id), _request(), db=db, user=user
                )
                deactivated = await flow_runs_api.deactivate_flow(
                    str(paused.id), _request(), db=db, user=user
                )
            assert activated.flow.paused_by_owner_deactivation is True, name
            assert deactivated.flow.paused_by_owner_deactivation is True, name

        await _set_active(sessionmaker, world, True, "owner")
        async with sessionmaker() as db:
            user = await _user(db, world, "member")
            activated = await flow_runs_api.activate_flow(
                str(paused.id), _request(), db=db, user=user
            )
        assert activated.flow.paused_by_owner_deactivation is False
