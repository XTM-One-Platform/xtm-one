"""The run finalizer settles only the work item the inbox dispatched.

When the work-item inbox dispatches an item, the run's end is the item's
outcome: a completed run completes it, a failed run releases its claim. The
finalizer settles as the run's agent, and a company-managed agent may write a
company-managed item, so WHICH item it settles must come from the dispatcher,
never from a payload anyone else wrote: a manual trigger of a per-user
company-managed assignment (open to non-admins) or a webhook body can carry
``trigger_type: work_item`` and any ``work_item_id``. The dispatcher's own
record is the server-built trigger context, which retries copy.

Driven through ``execute_assignment_run`` end to end with a fake agent loop and
the two settle functions replaced by mocks.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.assignment_engine import _inbox_dispatched_work_item
from tests.agentic_gate_stub import stub_agentic_gate

SUMMARY = "Contained the incident on the three hosts."


@pytest.fixture(autouse=True)
def _agentic_ai_available(monkeypatch):
    stub_agentic_gate(monkeypatch)


class _EngineResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return SimpleNamespace(all=list, first=lambda: None)

    def all(self):
        return []


class _EngineSession:
    """Serves the run, its assignment and its agent, then empty results."""

    def __init__(self, run, assignment, agent):
        self._queue = [run, assignment, agent]
        self.added: list = []

    async def execute(self, _stmt, *_args, **_kwargs):
        if self._queue:
            return _EngineResult(scalar=self._queue.pop(0))
        return _EngineResult()

    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def flush(self):
        return None

    def add(self, obj):
        self.added.append(obj)

    def in_transaction(self):
        return False

    def begin_nested(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _dispatch(item_id: str, item_user: str | None) -> tuple[dict, dict]:
    """The payload and trigger context the inbox dispatcher writes."""
    payload = {
        "trigger_type": "work_item",
        "work_item_id": item_id,
        "work_item": {"id": item_id, "user_id": item_user},
    }
    context = {"type": "work_item", "work_item_id": item_id}
    return payload, context


def _objects(*, payload: dict, context: dict):
    from app.models.agent import Agent
    from app.models.assignment import Assignment, AssignmentRun

    agent = Agent(
        id=uuid.uuid4(),
        name="Incident desk",
        slug="incident-desk",
        enabled=True,
        user_id=None,
        is_builtin=False,
        is_company_managed=True,
        integration_bindings={},
        builtin_tool_ids=[],
        handover_agent_ids=[],
        output_format="markdown",
    )
    assignment = Assignment(
        id=uuid.uuid4(),
        agent_id=agent.id,
        user_id=None,
        is_company_managed=True,
        name="Work the incident queue",
        trigger_type="work_item",
        trigger_config={},
        prompt="Work the item you were given.",
        enabled=True,
        dry_run=False,
        timeout_seconds=300,
        retry_on_failure=False,
        max_retries=0,
        notify_on_complete=False,
        execution_count=0,
    )
    run = AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=assignment.id,
        agent_id=agent.id,
        user_id=uuid.uuid4(),
        is_company_managed=False,
        status="pending",
        trigger_payload=payload,
        trigger_context=context,
        retry_count=0,
    )
    return run, assignment, agent


async def _drive(monkeypatch, *, payload: dict, context: dict, fail: bool = False):
    from app.core.config import settings
    from app.llm.base import ChatMessage
    from app.services import assignment_engine
    from app.tools.wait_policy import WaitPolicy

    run, assignment, agent = _objects(payload=payload, context=context)
    session = _EngineSession(run, assignment, agent)

    async def _fake_loop(**kwargs):
        if fail:
            raise RuntimeError("the containment tool crashed")
        kwargs["messages"].append(ChatMessage(role="assistant", content=SUMMARY))
        return SimpleNamespace(
            content=SUMMARY,
            usage={"input_tokens": 10, "output_tokens": 5},
            model="model-x",
            stop_reason="end_turn",
        )

    settle = {
        "complete": AsyncMock(return_value="completed"),
        "release": AsyncMock(return_value={"id": "x", "status": "open"}),
    }
    llm = SimpleNamespace(provider_name="anthropic", default_model="model-x", last_usage=None)
    for target, value in {
        "app.services.assignment_engine.async_session": lambda: session,
        "app.services.quota.check_agentic_quota": AsyncMock(),
        "app.services.quota.increment_agentic_usage": AsyncMock(),
        "app.services.quota.refund_agentic_usage_for_failure": AsyncMock(return_value=True),
        "app.services.quota.refund_agentic_usage_for_provider_outage": AsyncMock(return_value=True),
        "app.services.assignment_engine.record_agentic_execution": AsyncMock(),
        "app.services.assignment_engine._publish_run_status": AsyncMock(),
        "app.services.assignment_engine.event_bus": SimpleNamespace(emit=AsyncMock()),
        "app.api.v1.helpers.get_user_group_ids": AsyncMock(return_value=[]),
        "app.services.variable_resolver.load_variables": AsyncMock(return_value={}),
        "app.llm.router.get_llm_for_user": AsyncMock(return_value=(llm, "model-x")),
        "app.services.langfuse_tracing.trace_llm_call": AsyncMock(return_value=None),
        "app.services.assignment_engine._build_trigger_information": lambda _run: "",
        "app.services.assignment_engine._load_memory_context": AsyncMock(return_value=("", "")),
        "app.services.builtin_tools.load_kb_names": AsyncMock(return_value={}),
        "app.services.builtin_tools.load_write_locked_kb_ids": AsyncMock(return_value=set()),
        "app.services.assignment_engine._build_system_message": AsyncMock(return_value="You are an agent."),
        "app.services.assignment_engine.load_authorized_trigger_files": AsyncMock(return_value=[]),
        "app.services.builtin_tools.load_mcp_tools": AsyncMock(return_value=None),
        "app.services.builtin_tools.load_custom_tools": AsyncMock(return_value=None),
        "app.services.integration_tools.build_integration_tools": AsyncMock(return_value=([], None)),
        "app.services.assignment_engine.resolve_run_max_iterations": AsyncMock(return_value=10),
        "app.services.assignment_engine.resolve_run_wait_policy": AsyncMock(return_value=WaitPolicy()),
        "app.tools.run_budget.build_run_budget_for_run": AsyncMock(return_value=None),
        "app.tools.agent_loop.run_agent_loop": _fake_loop,
        "app.services.tool_tracking.track_tool_usage": AsyncMock(),
        "app.services.assignment_engine._track_assignment_auth_failures": AsyncMock(),
        "app.services.assignment_engine._track_stale_reference_failure": AsyncMock(),
        "app.services.integration_tools.cancel_run_partner_tasks": AsyncMock(),
        "app.services.builtin_tools.cleanup_mcp_manager": AsyncMock(),
        "app.services.assignment_engine.create_execution": AsyncMock(),
        "app.services.work_item.settle_inbox_work_item": settle["complete"],
        "app.services.work_item.release_work_item_claim": settle["release"],
    }.items():
        monkeypatch.setattr(target, value)
    monkeypatch.setattr(settings, "agent_self_fanout_enabled", False)

    await assignment_engine.execute_assignment_run(str(run.id))
    return run, settle


# ── The dispatcher's record ────────────────────────────────────────────────────


def test_only_the_dispatchers_record_names_the_item():
    item, user = str(uuid.uuid4()), str(uuid.uuid4())
    payload, context = _dispatch(item, user)
    run = SimpleNamespace(trigger_payload=payload, trigger_context=context, user_id=None)
    assert _inbox_dispatched_work_item(run) == (item, user)

    # The item's own user scopes it; the run's user is the fallback.
    run_user = uuid.uuid4()
    no_user = {**payload, "work_item": {"id": item}}
    run = SimpleNamespace(trigger_payload=no_user, trigger_context=context, user_id=run_user)
    assert _inbox_dispatched_work_item(run) == (item, str(run_user))


@pytest.mark.parametrize(
    "context",
    [
        {"type": "manual", "user_id": str(uuid.uuid4())},  # a manual trigger
        {"type": "webhook", "body": {}},  # a webhook body
        None,  # a delegation, a flow node
        {"type": "work_item", "work_item_id": str(uuid.uuid4())},  # another item
        {"type": "work_item"},  # no item at all
    ],
    ids=["manual", "webhook", "no-context", "other-item", "no-item"],
)
def test_a_payload_naming_an_item_is_not_a_dispatch(context):
    payload, _ = _dispatch(str(uuid.uuid4()), None)
    run = SimpleNamespace(trigger_payload=payload, trigger_context=context, user_id=None)
    assert _inbox_dispatched_work_item(run) is None


# ── The finalizer, end to end ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_dispatched_run_completes_its_item(monkeypatch):
    item, user = str(uuid.uuid4()), str(uuid.uuid4())
    payload, context = _dispatch(item, user)

    run, settle = await _drive(monkeypatch, payload=payload, context=context)

    assert run.status == "completed", run.error
    settle["complete"].assert_awaited_once()
    assert settle["complete"].await_args.args == (item,)
    assert settle["complete"].await_args.kwargs["user_id"] == user
    # The company-managed agent settles a company-managed item.
    assert settle["complete"].await_args.kwargs["writer"].may_write is True
    # The ending run is named: a second dispatch never settles the holder's item.
    assert settle["complete"].await_args.kwargs["run_id"] == run.id
    # No completion tool offered and a clean end: the run's end may complete it.
    assert settle["complete"].await_args.kwargs["may_complete"] is True
    assert settle["complete"].await_args.kwargs["release_reason"] is None
    settle["release"].assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_dispatched_run_hands_its_item_back(monkeypatch):
    item = str(uuid.uuid4())
    payload, context = _dispatch(item, None)

    run, settle = await _drive(monkeypatch, payload=payload, context=context, fail=True)

    assert run.status == "failed"
    settle["complete"].assert_not_awaited()
    settle["release"].assert_awaited_once()
    assert settle["release"].await_args.kwargs["work_item_id"] == item
    assert settle["release"].await_args.kwargs["failed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True], ids=["completed", "failed"])
@pytest.mark.parametrize(
    "context",
    [
        {"type": "manual", "user_id": str(uuid.uuid4())},
        {"type": "webhook", "body": {}},
        None,
    ],
    ids=["manual", "webhook", "no-context"],
)
async def test_a_forged_payload_settles_nothing(monkeypatch, context, fail):
    """A non-admin's manual trigger of a per-user company-managed assignment,
    or a webhook body, naming a company-managed item: the company-managed
    agent's run neither closes it nor hands its claim back."""
    payload, _ = _dispatch(str(uuid.uuid4()), None)

    _run, settle = await _drive(monkeypatch, payload=payload, context=context, fail=fail)

    settle["complete"].assert_not_awaited()
    settle["release"].assert_not_awaited()
