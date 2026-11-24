"""External work is bounded by identity, not by a tool or peer list.

An exposed agent answers a partner with its own full configuration — every
tool, knowledge base, integration and handover peer it has, by consult,
transfer or background delegation — as the exposure's run-as user. Four
things hold, one class each:

1. the task path passes no session scope (a non-None one *replaces* what
   ``run_agent_chat`` resolves, so passing none is the mechanism);
2. handover is offered in full, every type included;
3. every hop runs as the same run-as user;
4. except the platform-builder surface: the partner is an external
   caller, so every hop runs in the partner-task scope, which wires no trace
   inspection and only the agent builder's read half, as an anonymous caller,
   whatever the run-as identity - for a built-in, the seed admin.
"""

import asyncio
import inspect
import re
import uuid
from types import SimpleNamespace

import pytest

from app.a2a import tasks as task_svc
from app.a2a.partner_scope import (
    PARTNER_PRINCIPAL_PAYLOAD_KEY,
    PARTNER_TASK_PAYLOAD_KEY,
    PartnerPrincipal,
    current_partner,
    in_partner_task,
    partner_principal_payload,
    partner_task_scope,
    queued_partner_principal,
    queued_partner_task,
)

pytestmark = pytest.mark.asyncio


class _Result:
    def __init__(self, value, rowcount=1):
        self._value = value
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._value


class _DB:
    """Answers ``execute_task_run``'s four loads, then its claim UPDATE."""

    def __init__(self, *answers):
        self._answers = list(answers)

    async def execute(self, _stmt):
        return _Result(self._answers.pop(0) if self._answers else None)

    async def flush(self):
        pass

    async def commit(self):
        pass


class _Ctx:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_exc):
        return False


@pytest.fixture
def executed(monkeypatch):
    """The kwargs ``execute_task_run`` hands ``run_agent_chat``.

    The supervisor and the terminal write are stubbed out; both belong to
    ``test_a2a_tasks``.
    """
    run_as, agent_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    task = type("T", (), {"id": uuid.uuid4(), "run_id": run_id,
                          "exposure_id": uuid.uuid4(),
                          "caller_question": None,
                          "clarification_count": 0,
                          "lease_heartbeat_at": None})()
    run = type("R", (), {"id": run_id, "status": "pending", "agent_id": agent_id,
                         "trigger_context": {"client_id": "partner-1"},
                         "trigger_payload": {"message": "do the thing"}})()
    exposure = type("E", (), {"id": task.exposure_id, "revoked_at": None,
                              "run_as_user_id": run_as})()
    # ``agent_builder`` deliberately: a management-capable built-in reaches
    # the run like any other tool.
    agent = type("A", (), {"id": agent_id, "name": "Exposed Agent",
                           "builtin_tool_ids": ["web_fetch", "agent_builder"],
                           "handover_agent_ids": [str(uuid.uuid4())],
                           "handover_types": {}})()

    captured: dict = {"_run_as": run_as}

    async def _fake_chat(**kwargs):
        captured.update(kwargs)
        captured["_partner_task"] = in_partner_task()
        return "answer", None

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _fake_chat)
    monkeypatch.setattr(task_svc, "_supervise_run", _noop)
    monkeypatch.setattr(task_svc, "_finalise_run", _noop)
    monkeypatch.setattr(
        "app.core.database.async_session",
        lambda: _Ctx(_DB(task, run, exposure, agent)),
    )
    return captured


class TestTheRunIsNotToolNarrowed:
    async def test_no_session_scope_reaches_the_run(self, executed):
        """Asserted on the ``session_`` PREFIX, so a capability family added to
        ``run_agent_chat`` tomorrow is covered without extending a list here.
        """
        await task_svc.execute_task_run(uuid.uuid4())
        passed = sorted(k for k in executed if k.startswith("session_"))
        assert not passed, f"{', '.join(passed)} narrowed the run"


class TestHandoverIsOfferedInFull:
    async def test_every_type_over_every_configured_peer(self, executed):
        await task_svc.execute_task_run(uuid.uuid4())
        assert executed["allow_handover"] is True
        # Neither direction of the type filter is passed: every type the agent
        # configures is offered, ``mandatory_transfer`` included.
        assert "allowed_handover_types" not in executed
        # No per-peer scoping: a peer is reachable whether or not it is itself
        # exposed to this partner.
        assert "consult_peer_scopes" not in executed


class TestOneChainOneIdentity:
    async def test_the_run_executes_as_the_exposures_run_as_user(self, executed):
        run_as = executed["_run_as"]
        await task_svc.execute_task_run(uuid.uuid4())
        assert executed["user_id"] == str(run_as)

    @pytest.mark.parametrize(
        "anchor",
        [
            # The consulted turn runs under the stall watchdog.
            "consulted_text, _ = await run_watched(",
            "transferred_text, _inner_transfer_id = await _run_helper_turn(",
        ],
    )
    def test_a_peer_inherits_the_callers_identity(self, anchor):
        """``user_id`` straight through — not the peer's own exposure, not a
        nomination. What makes one chain one identity."""
        import inspect

        from app.services.agent_chat import run_agent_chat

        src = inspect.getsource(run_agent_chat)
        assert "user_id," in src[src.index(anchor) :][:200]

    async def test_the_peer_wrapper_passes_the_callers_arguments_through(self, monkeypatch):
        """The consult and transfer calls go through ``_run_helper_turn``,
        which hands the peer exactly what the caller passed, identity included."""
        from app.services import agent_chat

        seen: dict = {}

        async def _peer(*args, **kwargs):
            seen["args"], seen["kwargs"] = args, kwargs
            return "the peer's answer", None

        monkeypatch.setattr(agent_chat, "run_agent_chat", _peer)
        db, agent, history = object(), object(), []
        answer = await agent_chat._run_helper_turn(
            db, "run-as-user", agent, history, "question?", conversation_id=None, evaluation_mode=True
        )

        assert answer == ("the peer's answer", None)
        assert seen["args"] == (db, "run-as-user", agent, history, "question?")
        assert seen["kwargs"].pop("loop_outcome_out") == {}
        assert seen["kwargs"] == {"conversation_id": None, "evaluation_mode": True}

    def test_a_background_task_inherits_it_through_the_payload(self):
        """It crosses a process boundary, so the identity travels in the
        execution payload — never re-resolved on the worker."""
        from app.services.sub_agent import _execute_sub_agent_task

        src = inspect.getsource(_execute_sub_agent_task)
        assert "user_id," in src[src.index("run_agent_chat(") :][:120]


def _seed_admin_agent():
    """A built-in as XTM One seeds it: company-managed, owned by the seed admin, carrying the
    builder and, explicitly, both trace suites."""
    admin = SimpleNamespace(id=uuid.uuid4(), role="admin", is_admin=True)
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        builtin_tool_ids=["agent_builder", "flow_builder", "inspect_my_traces", "inspect_agent_traces"],
        is_company_managed=True,
        user_id=admin.id,
        shared_user_ids=None,
        shared_user_permissions=None,
        shared_group_ids=None,
        shared_group_permissions=None,
        trace_supervision_target_agent_ids=None,
    )
    return admin, agent


class TestThePartnerGetsNoPlatformBuilderReach:
    async def test_the_run_answers_inside_the_partner_task_scope(self, executed):
        await task_svc.execute_task_run(uuid.uuid4())
        assert executed["_partner_task"] is True
        assert in_partner_task() is False

    async def test_a_consult_or_transfer_inherits_it(self):
        """In-process hops run in the task's asyncio context, or in a copy of it."""

        async def _hop():
            return in_partner_task()

        with partner_task_scope():
            assert await _hop() is True
            assert await asyncio.create_task(_hop()) is True
        assert in_partner_task() is False

    def test_a_background_delegation_carries_it_to_the_worker(self):
        from app.services.orchestration_adapters.sub_agent import SubAgentAdapter
        from app.services.sub_agent import create_sub_agent_task

        assert "PARTNER_TASK_PAYLOAD_KEY: in_partner_task()" in inspect.getsource(create_sub_agent_task)
        assert "partner_task_scope(queued_partner_task(payload), principal=" in inspect.getsource(
            SubAgentAdapter.execute
        )
        assert queued_partner_task({PARTNER_TASK_PAYLOAD_KEY: True}) is True
        for payload in ({}, {PARTNER_TASK_PAYLOAD_KEY: "true"}, None):
            assert queued_partner_task(payload) is False

    def test_the_partner_principal_rides_with_the_scope_and_the_delegation(self) -> None:
        """The run history a partner task may read is scoped by the partner,
        so the partner travels with the scope, in process and to the worker."""
        from app.services.orchestration_adapters.sub_agent import SubAgentAdapter
        from app.services.sub_agent import create_sub_agent_task

        partner = PartnerPrincipal(exposure_id=uuid.uuid4(), client_id="partner-1")
        assert current_partner() is None
        with partner_task_scope(principal=partner):
            assert current_partner() == partner
            payload = {PARTNER_TASK_PAYLOAD_KEY: True, PARTNER_PRINCIPAL_PAYLOAD_KEY: partner_principal_payload()}
        assert current_partner() is None
        assert queued_partner_principal(payload) == partner
        # Not a partner task, or a malformed principal: no partner, nothing lent.
        for bad in (
            {PARTNER_PRINCIPAL_PAYLOAD_KEY: partner.as_payload()},
            {PARTNER_TASK_PAYLOAD_KEY: True, PARTNER_PRINCIPAL_PAYLOAD_KEY: {"exposure_id": "x", "client_id": "c"}},
            {PARTNER_TASK_PAYLOAD_KEY: True, PARTNER_PRINCIPAL_PAYLOAD_KEY: {"exposure_id": str(uuid.uuid4())}},
            {PARTNER_TASK_PAYLOAD_KEY: True},
        ):
            assert queued_partner_principal(bad) is None
        assert "PARTNER_PRINCIPAL_PAYLOAD_KEY: partner_principal_payload()" in inspect.getsource(create_sub_agent_task)
        assert "principal=queued_partner_principal(payload)" in inspect.getsource(SubAgentAdapter.execute)

    async def test_the_executor_names_the_partner_that_sent_the_task(
        self, monkeypatch: pytest.MonkeyPatch, executed: object
    ) -> None:
        seen: list[PartnerPrincipal | None] = []

        async def _fake_chat(**kwargs: object) -> tuple[str, None]:
            seen.append(current_partner())
            return "answer", None

        monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _fake_chat)
        await task_svc.execute_task_run(uuid.uuid4())
        assert len(seen) == 1 and seen[0] is not None
        assert seen[0].client_id == "partner-1"

    async def test_the_seed_admin_identity_gets_no_trace_inspection(self):
        from app.services.agent_chat import _turn_trace_tool_ids
        from app.services.builtin_tools.traces import may_read_agent_traces

        admin, agent = _seed_admin_agent()
        with partner_task_scope():
            assert await _turn_trace_tool_ids(
                object(), admin, agent, agent.builtin_tool_ids, user_group_ids=[]
            ) == set()
            assert await may_read_agent_traces(object(), admin, agent, []) is False
        # The same admin chatting with the agent keeps them.
        assert await _turn_trace_tool_ids(object(), admin, agent, agent.builtin_tool_ids, user_group_ids=[]) == {
            "inspect_my_traces",
            "inspect_agent_traces",
        }

    def test_the_seed_admin_identity_gets_only_the_builders_read_half_as_an_anonymous_caller(self, monkeypatch):
        from app.api.v1.mcp_xtm import XTM_MCP_READ_TOOLS
        from app.services import builtin_tools
        from app.services.agent_chat import _add_platform_builder_tools
        from app.tools.base import CompositeToolExecutor

        admin, agent = _seed_admin_agent()
        actors: list[str] = []
        real_builder = builtin_tools.build_agent_builder_tool

        def _spy(db, user_id):
            actors.append(user_id)
            return real_builder(db, user_id)

        monkeypatch.setattr("app.services.builtin_tools.build_agent_builder_tool", _spy)
        flow_tools = {d.name for d in builtin_tools.build_flow_builder_tool(None, str(admin.id))[0]}
        evaluation_tools = {d.name for d in builtin_tools.build_evaluation_tools(None, str(admin.id))[0]}

        def wired() -> set[str]:
            composite = CompositeToolExecutor()
            _add_platform_builder_tools(composite, None, str(admin.id), agent.builtin_tool_ids)
            return set(composite.tool_names)

        with partner_task_scope():
            partner = wired()
        assert actors == ["anonymous"]
        assert "list_agents" in partner and partner <= XTM_MCP_READ_TOOLS
        for writer in ("create_agent", "update_agent", "delete_agent", "create_assignment", "trigger_assignment"):
            assert writer not in partner
        assert not partner & (flow_tools | evaluation_tools)

        actors.clear()
        chatting = wired()
        assert actors == [str(admin.id)]
        assert {"create_agent", "update_agent", "delete_agent"} <= chatting
        assert flow_tools <= chatting and evaluation_tools <= chatting


class TestThePartnerDrivesNoRunner:
    """``computer_use`` drives the run-as identity's enrolled machines (shell,
    files, screenshots, input): a partner's task never gets it, and the model is
    told why rather than hearing "unknown tool"."""

    def _suite(self) -> set[str]:
        from app.services.builtin_tools import build_computer_use_tools

        defs, _ = build_computer_use_tools(
            user_id=None, is_admin=False, user_group_ids=[], agent_runner_ids=[], agent_runner_labels=[]
        )
        return {d.name for d in defs}

    async def test_a_partner_task_withholds_the_suite(self):
        from app.a2a.partner_scope import withhold_runner_tools
        from app.tools.base import CompositeToolExecutor

        composite = CompositeToolExecutor()
        with partner_task_scope():
            assert withhold_runner_tools(composite) is True
        assert not set(composite.tool_names)
        assert set(composite.withheld) == self._suite()

    async def test_any_other_turn_keeps_it(self):
        from app.a2a.partner_scope import withhold_runner_tools
        from app.tools.base import CompositeToolExecutor

        composite = CompositeToolExecutor()
        assert withhold_runner_tools(composite) is False
        assert not composite.withheld

    def test_every_builder_of_an_agent_run_asks_first(self):
        from app.services import agent_chat, assignment_engine

        gate = re.compile(
            r'runners_enabled\s+and "computer_use" in agent_builtin_tool_ids\s+and not withhold_runner_tools\(composite\)'
        )
        for module in (agent_chat, assignment_engine):
            source = inspect.getsource(module)
            assert source.count("build_computer_use_tools(") == 1, module.__name__
            assert gate.search(source), module.__name__
