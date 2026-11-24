"""A deactivated owner's flows are paused unless company-managed - no
database.

The behaviour against PostgreSQL is in ``test_flow_owner_deactivation_postgres.py``.
Here: the refusal shapes, the flow webhook's answer (never cached), the run
gate's side effects, and a tripwire over EVERY place that starts a flow run, so
a new one cannot be added without the rule.
"""

from __future__ import annotations

import ast
import functools
import inspect
import pathlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.models.flow import Flow
from app.services import assignment_pause
from app.services.agentic_gate import refusal_code
from app.services.assignment_pause import (
    FLOW_OWNER_DEACTIVATED_MESSAGE,
    FLOW_OWNER_DEACTIVATED_RUN_ERROR,
    OWNER_DEACTIVATED_CODE,
    OWNER_DEACTIVATED_MESSAGE,
    AssignmentOwnerDeactivatedError,
    FlowOwnerDeactivatedError,
    OwnerDeactivatedError,
    owner_not_deactivated,
    refuse_flow_if_owner_deactivated,
)
from tests import app_sources

OWNER = uuid.uuid4()
APP = pathlib.Path(__file__).resolve().parents[1] / "app"


def _flow(**overrides) -> SimpleNamespace:
    fields = {
        "id": uuid.uuid4(),
        "name": "Nightly sync",
        "user_id": OWNER,
        "is_company_managed": False,
        "status": "active",
        "graph": {"nodes": [{"id": "w1", "kind": "trigger.webhook", "config": {"path": "in"}}]},
        "webhook_secret": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


# ===================================================================
# The rule and its refusal
# ===================================================================


def test_the_sql_form_reads_the_flow_columns():
    sql = str(owner_not_deactivated(Flow).compile(dialect=postgresql.dialect()))
    assert "flows.is_company_managed IS true" in sql
    assert "users.id = flows.user_id" in sql
    assert "users.is_active IS false" in sql


def test_the_assignment_form_is_unchanged():
    sql = str(owner_not_deactivated().compile(dialect=postgresql.dialect()))
    assert "assignments.is_company_managed IS true" in sql
    assert "users.id = assignments.user_id" in sql


def test_the_flow_refusal_is_the_same_409_a_client_branches_on():
    exc = FlowOwnerDeactivatedError(_flow())
    assert isinstance(exc, OwnerDeactivatedError)
    assert exc.status_code == 409
    assert exc.detail == {"code": OWNER_DEACTIVATED_CODE, "message": FLOW_OWNER_DEACTIVATED_MESSAGE}
    # Not an AI refusal: the agentic gate must not reword it.
    assert refusal_code(exc) is None
    # The assignment one keeps its shape and its attribute.
    assignment_exc = AssignmentOwnerDeactivatedError(SimpleNamespace(id=OWNER))
    assert assignment_exc.detail == {"code": OWNER_DEACTIVATED_CODE, "message": OWNER_DEACTIVATED_MESSAGE}
    assert assignment_exc.assignment_id == OWNER


async def test_refuse_flow_if_owner_deactivated_raises_only_for_a_paused_flow(monkeypatch):
    paused = AsyncMock(return_value=True)
    monkeypatch.setattr(assignment_pause, "is_paused_by_deactivated_owner", paused)
    with pytest.raises(HTTPException) as excinfo:
        await refuse_flow_if_owner_deactivated(object(), _flow())
    assert excinfo.value.detail["code"] == OWNER_DEACTIVATED_CODE

    paused.return_value = False
    await refuse_flow_if_owner_deactivated(object(), _flow())


# ===================================================================
# The flow webhook
# ===================================================================


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


def _webhook_session(flow, inactive_answers: list[bool]):
    """A session answering the flow lookups with *flow*, and each query of the
    rule with the next entry of *inactive_answers* (paused: the flow's id)."""

    class _One:
        def scalar_one_or_none(self):
            return flow

    class _DB:
        async def execute(self, statement):
            if "FROM users" in str(statement):
                return _Rows([(flow.id,)] if inactive_answers.pop(0) else [])
            return _One()

    class _Session:
        async def __aenter__(self):
            return _DB()

        async def __aexit__(self, *args):
            return False

    return lambda: _Session()


def _request(body: bytes = b'{"alert": 1}'):
    return SimpleNamespace(
        headers={},
        method="POST",
        url="http://t",
        client=SimpleNamespace(host="203.0.113.9"),
        body=AsyncMock(return_value=body),
    )


def _quiet_webhook(monkeypatch, webhooks):
    monkeypatch.setattr(webhooks, "enforce_rate_limit", AsyncMock())
    monkeypatch.setattr(webhooks, "_verify_assignment_webhook_secret", lambda *a, **k: None)
    trigger = AsyncMock(side_effect=AssertionError("no run expected"))
    monkeypatch.setattr("app.services.flow_triggers.trigger_flow_from_webhook_outcome", trigger)
    cache = AsyncMock(side_effect=AssertionError("the answer must not be cached"))
    monkeypatch.setattr(webhooks, "_persist_webhook_dedup_response", cache)


async def test_a_flow_webhook_delivery_is_ignored_before_any_dedup_claim(monkeypatch):
    from app.api.v1 import webhooks

    flow = _flow()
    monkeypatch.setattr(webhooks, "async_session", _webhook_session(flow, [True]))
    _quiet_webhook(monkeypatch, webhooks)
    claim = AsyncMock(side_effect=AssertionError("no dedup claim expected"))
    monkeypatch.setattr(webhooks, "_claim_webhook_dedup_slot", claim)

    response = await webhooks.receive_flow_webhook(str(flow.id), "in", _request())

    assert response == {"status": "ignored", "reason": webhooks._FLOW_OWNER_DEACTIVATED_REASON}


async def test_an_owner_deactivated_after_the_claim_releases_the_slot(monkeypatch):
    """The race: active at the pre-claim read, deactivated by the second one."""
    from app.api.v1 import webhooks

    flow = _flow()
    monkeypatch.setattr(webhooks, "async_session", _webhook_session(flow, [False, True]))
    _quiet_webhook(monkeypatch, webhooks)
    monkeypatch.setattr(
        webhooks, "_claim_webhook_dedup_slot", AsyncMock(return_value=(False, None, "dedup-key"))
    )
    release = AsyncMock()
    monkeypatch.setattr(webhooks, "_release_webhook_dedup_slot", release)

    response = await webhooks.receive_flow_webhook(str(flow.id), "in", _request())

    assert response == {"status": "ignored", "reason": webhooks._FLOW_OWNER_DEACTIVATED_REASON}
    release.assert_awaited_once_with("dedup-key")


async def test_a_run_created_as_the_owner_is_deactivated_is_ignored_uncached(monkeypatch):
    """The race past both checks: the run's creation refuses, the route gives
    the same uncached answer instead of a 409 or a cached one."""
    from app.api.v1 import webhooks

    flow = _flow()
    monkeypatch.setattr(webhooks, "async_session", _webhook_session(flow, [False, False]))
    _quiet_webhook(monkeypatch, webhooks)
    monkeypatch.setattr(
        "app.services.flow_triggers.trigger_flow_from_webhook_outcome",
        AsyncMock(side_effect=FlowOwnerDeactivatedError(flow)),
    )
    monkeypatch.setattr(
        webhooks, "_claim_webhook_dedup_slot", AsyncMock(return_value=(False, None, "dedup-key"))
    )
    release = AsyncMock()
    monkeypatch.setattr(webhooks, "_release_webhook_dedup_slot", release)

    response = await webhooks.receive_flow_webhook(str(flow.id), "in", _request())

    assert response == {"status": "ignored", "reason": webhooks._FLOW_OWNER_DEACTIVATED_REASON}
    release.assert_awaited_once_with("dedup-key")


# ===================================================================
# The backstop at run creation
# ===================================================================


async def test_create_flow_run_refuses_a_paused_flow_before_adding_anything(monkeypatch):
    from app.services.flow_runner import create_flow_run

    monkeypatch.setattr(
        assignment_pause, "is_paused_by_deactivated_owner", AsyncMock(return_value=True)
    )

    class _DB:
        def add(self, _row):
            raise AssertionError("nothing may be added for a paused flow")

    with pytest.raises(FlowOwnerDeactivatedError):
        await create_flow_run(
            _DB(),
            _flow(),
            trigger_type="schedule",
            trigger_payload={},
            trigger_context={},
            user_id=OWNER,
        )


class _PollRedis:
    def __init__(self):
        self.values: dict = {}
        self.seen: set = set()
        self.released: list = []

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, **_kwargs):
        self.values[key] = value

    async def delete(self, key):
        self.values.pop(key, None)

    async def sadd(self, _key, member):
        if member in self.seen:
            return 0
        self.seen.add(member)
        return 1

    async def srem(self, _key, member):
        self.seen.discard(member)
        self.released.append(member)

    async def expire(self, *_args, **_kwargs):
        return True


@pytest.mark.parametrize("cursor_before", ["cursor-0", None])
async def test_an_event_polled_for_a_flow_as_its_owner_is_deactivated_is_given_back(
    monkeypatch, cursor_before
):
    """The event, the cursor the poll advanced and the poll stamp are given
    back, so the poll is fetched again once the owner is back."""
    from app.core import database, security
    from app.core import redis as redis_module
    from app.services import event_poller, flow_triggers, trigger_integration_access

    flow = _flow(graph={})
    node = {"id": "e1", "config": {"integration_id": str(uuid.uuid4()), "event_type": "new_email"}}
    target_key = f"flow:{flow.id}:e1"
    redis = _PollRedis()
    if cursor_before is not None:
        redis.values[f"event_cursor:{target_key}"] = cursor_before
    redis.values[f"event_poll:{target_key}"] = "0"

    async def _provider(_provider, _event, _token, shim, redis_, *_args):
        await redis_.set(f"event_cursor:{shim.id}", "cursor-after")
        return [{"message_id": "m-1"}, {"message_id": "m-2"}]

    class _One:
        def scalar_one_or_none(self):
            return SimpleNamespace(
                id=node["config"]["integration_id"],
                provider="microsoft",
                access_token_enc="sealed",
            )

    class _DB:
        async def execute(self, _statement):
            return _One()

    monkeypatch.setattr(
        flow_triggers, "_active_flows_with_trigger", AsyncMock(return_value=[(flow, [node])])
    )
    monkeypatch.setattr(redis_module, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(security, "decrypt_value", lambda _v: "token")
    monkeypatch.setattr(database, "release_db_connection_before_loop", AsyncMock())
    monkeypatch.setattr(event_poller, "_poll_provider", _provider)
    monkeypatch.setattr(
        trigger_integration_access.TriggerIntegrationVisibility,
        "sees",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(flow_triggers, "has_active_run", AsyncMock(return_value=False))
    monkeypatch.setattr(flow_triggers, "hold_back_if_ai_required", AsyncMock(return_value=False))
    monkeypatch.setattr(
        flow_triggers, "create_flow_run", AsyncMock(side_effect=FlowOwnerDeactivatedError(flow))
    )

    assert await flow_triggers.poll_flow_integration_events(_DB()) == 0
    assert redis.released == ["m-1"] and redis.seen == set()
    assert redis.values.get(f"event_cursor:{target_key}") == cursor_before
    assert redis.values.get(f"event_poll:{target_key}") == "0"


# ===================================================================
# The run gate
# ===================================================================


def _walker(monkeypatch, *, trigger_type="manual", state=None, parent_run_id=None, paused=True):
    from app.services import flow_runner

    run = SimpleNamespace(
        id=uuid.uuid4(),
        flow_id=uuid.uuid4(),
        status="pending",
        graph={},
        state=state or {},
        trigger_type=trigger_type,
        trigger_context={"work_item_id": "wi-1"} if trigger_type == "work_item" else {},
        is_company_managed=False,
        parent_run_id=parent_run_id,
        completed_at=None,
        result_summary=None,
        error=None,
    )
    # Anything past the gate reads the database first (the flow's name).
    db = SimpleNamespace(
        commit=AsyncMock(), execute=AsyncMock(side_effect=RuntimeError("past the gate"))
    )
    check = AsyncMock(return_value=paused)
    monkeypatch.setattr(assignment_pause, "is_flow_paused_by_deactivated_owner", check)
    monkeypatch.setattr(flow_runner, "publish_flow_run_update", AsyncMock())
    walker = flow_runner.FlowWalker(db, run, SimpleNamespace(id=OWNER))
    return walker, run, db, check


async def test_a_queued_run_of_a_paused_flow_ends_skipped_before_anything_starts(monkeypatch):
    from app.services import work_item
    from app.services.company_managed_writes import flow_writer

    walker, run, db, _ = _walker(monkeypatch, trigger_type="work_item", parent_run_id=uuid.uuid4())
    release = AsyncMock()
    monkeypatch.setattr(work_item, "release_work_item_claim", release)
    notify = AsyncMock()
    monkeypatch.setattr(walker, "_notify_parent", notify)

    outcome = await walker.run_burst()

    assert outcome.kind == "skipped"
    assert run.status == "skipped"
    assert run.result_summary == FLOW_OWNER_DEACTIVATED_RUN_ERROR
    assert run.error is None
    assert run.completed_at is not None
    # The item goes back to the queue, no failed attempt counted, released as
    # the flow's tier, the writer its trigger claimed with.
    release.assert_awaited_once_with(
        work_item_id="wi-1",
        user_id=str(OWNER),
        writer=flow_writer(is_company_managed=False),
    )
    notify.assert_awaited_once()
    db.commit.assert_awaited()
    # Nothing past the gate ran: no read, so no trace, step or charge.
    db.execute.assert_not_awaited()


async def test_a_run_already_under_way_is_not_gated(monkeypatch):
    walker, run, _, check = _walker(monkeypatch, state={"started": True})
    run.status = "running"

    with pytest.raises(RuntimeError, match="past the gate"):
        await walker.run_burst()
    check.assert_not_awaited()


async def test_the_adapter_ends_a_skipped_run_cleanly(monkeypatch):
    from app.services import flow_runner
    from app.services.flow_runner import BurstOutcome
    from app.services.orchestration_adapters.flow import FlowAdapter

    run = SimpleNamespace(
        id=uuid.uuid4(), status="pending", user_id=OWNER, error=None, graph={}, state={}
    )

    class _One:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    answers = [run, SimpleNamespace(id=OWNER)]

    class _DB:
        async def execute(self, statement):
            return _One(answers.pop(0))

        async def flush(self):
            return None

    monkeypatch.setattr(
        flow_runner.FlowWalker, "run_burst", AsyncMock(return_value=BurstOutcome(kind="skipped"))
    )
    result = await FlowAdapter().execute(
        SimpleNamespace(db=_DB(), cancel_event=None), SimpleNamespace(payload={"run_id": str(run.id)})
    )
    assert result.kind == "completed"
    assert result.payload_update == {"skipped": True}


# ===================================================================
# Tripwire: every place that starts a flow run applies the rule
# ===================================================================


@functools.cache
def _callers() -> dict[str | None, frozenset[str]]:
    """Called name -> ``module:function`` of every call to it anywhere in ``app/``."""
    found: dict[str | None, set[str]] = {}

    def visit(node, rel: str, owner: str | None = None) -> None:
        for child in ast.iter_child_nodes(node):
            name = owner
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = child.name
            if isinstance(child, ast.Call) and owner is not None:
                func = child.func
                target = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                found.setdefault(target, set()).add(f"{rel}:{owner}")
            visit(child, rel, name)

    for rel, tree in app_sources.modules(APP):
        visit(tree, rel)
    return {target: frozenset(owners) for target, owners in found.items()}


def _enclosing_functions(called: str) -> set[str]:
    """``module:function`` of every call to *called* anywhere in ``app/``."""
    return set(_callers().get(called, ()))


def test_every_caller_of_create_flow_run_is_known():
    """A new caller must be added here AND apply the rule (see the next test)."""
    assert _enclosing_functions("create_flow_run") == {
        "services/flow_triggers.py:process_scheduled_flows",
        "services/flow_triggers.py:trigger_flow_from_webhook_outcome",
        "services/flow_triggers.py:poll_flow_integration_events",
        "services/flow_triggers.py:process_work_item_flows",
        "services/flow_triggers.py:dispatch_flow_platform_event",
        "services/flow_events.py:emit_flow_event",
        "services/flow_runner.py:_exec_flow_call",
        "api/v1/flow_runs.py:run_flow",
        "services/builtin_tools/flow_builder.py:_run_flow",
        "services/health/sandbox/runs.py:start_sandbox_flow_run",
        # An evaluation's flow subject (#8257): the entry is left out, with the reason.
        "services/evaluation/flow_subject.py:run_flow_for_entry",
    }


def test_every_other_flow_run_row_is_known():
    """``FlowRun(...)`` outside ``create_flow_run`` is the AI hold-back record,
    which only ever follows a path the rule already filtered."""
    assert _enclosing_functions("FlowRun") == {
        "services/flow_runner.py:create_flow_run",
        "services/flow_triggers.py:hold_back_if_ai_required",
    }


def _source(obj) -> str:
    return inspect.getsource(obj)


def test_every_path_that_starts_a_flow_run_applies_the_rule():
    from app.api.v1 import flow_runs, flows, webhooks
    from app.services import flow_events, flow_runner, flow_triggers
    from app.services.builtin_tools import flow_builder

    # The three sweeps pick their flows through one query.
    assert "owner_not_deactivated(Flow)" in _source(flow_triggers._active_flows_with_trigger)
    for sweep in (
        flow_triggers.process_scheduled_flows,
        flow_triggers.poll_flow_integration_events,
        flow_triggers.process_work_item_flows,
        flow_triggers.dispatch_flow_platform_event,
    ):
        assert "_active_flows_with_trigger(" in _source(sweep), sweep.__name__
    # The webhook: the only caller of trigger_flow_from_webhook_outcome,
    # checked before the claim and after it.
    assert _source(webhooks.receive_flow_webhook).count("is_paused_by_deactivated_owner(") == 2
    # Both listener queries (flow scope and global scope).
    assert _source(flow_events.emit_flow_event).count("owner_not_deactivated(Flow)") == 2
    assert "is_paused_by_deactivated_owner(" in _source(flow_runner.FlowWalker._exec_flow_call)
    assert "refuse_flow_if_owner_deactivated(" in _source(flow_runs.run_flow)
    assert "is_paused_by_deactivated_owner(" in _source(flow_builder.build_flow_builder_tool)
    # A Platform Health sandbox run acts as its source flow's owner (#8255).
    from app.services.health.sandbox import runs

    assert "is_paused_by_deactivated_owner(" in _source(runs.start_sandbox_flow_run)
    # Releasing a parked run, and the run gate every queued run passes.
    # The Ask-user answer is held in answer_flow_park, the one path both the
    # respond endpoint and the decisions router (#842 S2) go through.
    for route in (flow_runs.answer_flow_park, flow_runs.debug_flow_run):
        assert "_refuse_if_run_flow_paused(" in _source(route), route.__name__
    assert "answer_flow_park(" in _source(flow_runs.respond_flow_run)
    assert "is_flow_paused_by_deactivated_owner(" in _source(flow_runner.FlowWalker.run_burst)
    # What people see.
    assert "owner_not_deactivated(Flow)" in _source(flows.list_flows)
    assert "owner_not_deactivated(Flow)" in _source(flows.list_flow_chat_entries)


def test_every_caller_of_create_flow_run_absorbs_its_refusal():
    """``create_flow_run`` refuses a paused flow, the backstop for an owner
    deactivated after a query picked the flow. Every caller but the manual
    route (whose 409 is its answer) takes its own pause path instead."""
    from app.api.v1 import webhooks
    from app.services import flow_events, flow_runner, flow_triggers
    from app.services.builtin_tools import flow_builder
    from app.services.evaluation import flow_subject

    assert "refuse_flow_if_owner_deactivated(db, flow)" in _source(flow_runner.create_flow_run)
    for caller in (
        flow_triggers.process_scheduled_flows,
        flow_triggers.poll_flow_integration_events,
        flow_triggers.process_work_item_flows,
        flow_triggers.dispatch_flow_platform_event,
        flow_events.emit_flow_event,
        webhooks.receive_flow_webhook,
        flow_runner.FlowWalker._exec_flow_call,
        flow_builder.build_flow_builder_tool,
        flow_subject.run_flow_for_entry,
    ):
        assert "FlowOwnerDeactivatedError" in _source(caller), caller.__name__
