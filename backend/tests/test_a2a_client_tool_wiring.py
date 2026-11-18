"""The delegation tool actually reaches a run — and only an outbound one.

SC-007 and SC-014 meet here. A tool that exists, is tested and never reaches a
run is the failure this guards, and there was a live route to it: the
integration tool dispatcher skips any binding with no stored token, and an A2A
registration has none — it mints a bearer per call. Without the branch this
file pins, the delegation tool would be unreachable.
"""

from __future__ import annotations

import inspect
import uuid

import pytest

from app.services import integration_tools


class TestResolvedBeforeTheTokenStep:
    def test_the_branch_runs_before_the_token_check(self):
        # Order is the whole point. Below ``if not token: continue`` the tool
        # is silently dropped; above it, it is contributed.
        #
        # Matched on the STATEMENT, not the substring: the branch's own
        # comment quotes the check it must precede, and a naive search finds
        # the comment first.
        lines = inspect.getsource(
            integration_tools._build_integration_tools_inner
        ).splitlines()
        a2a_at = next(
            i for i, ln in enumerate(lines)
            if "_build_a2a_delegation_tool(" in ln and not ln.strip().startswith("#")
        )
        token_at = next(
            i for i, ln in enumerate(lines) if ln.strip() == "if not token:"
        )
        assert a2a_at < token_at, (
            "A2A must resolve before the token check — it has no stored token"
        )

    def test_every_loop_that_confers_tools_wires_the_branch(self):
        """A second loop over the bindings must not confer tools without it.

        The concern: a per-user template loop that skipped this branch would
        leave a template-bound partner with no delegation tool (FR-006).

        Assert the property, not a proxy: a loop that resolves a TOKEN
        confers tools and must reach the A2A branch first; a loop that merely
        tallies bindings (the counting pre-pass for multi-instance provider
        naming) confers nothing and is exempt.
        """
        src = inspect.getsource(integration_tools._build_integration_tools_inner)
        lines = src.splitlines()

        conferring = []
        for i, ln in enumerate(lines):
            if not (ln.strip().startswith("for ") and "bindings.items()" in ln):
                continue
            indent = len(ln) - len(ln.lstrip())
            body = []
            for nxt in lines[i + 1 :]:
                if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent:
                    break
                body.append(nxt)
            if any("_get_token(" in b for b in body):
                conferring.append((ln, body))

        assert conferring, "no loop resolves a token — the dispatcher has moved"
        for header, body in conferring:
            assert any("_build_a2a_delegation_tool(" in b for b in body), (
                f"loop {header.strip()!r} resolves tokens without the A2A "
                "branch, so a partner bound through it confers no tool"
            )

    def test_a_handled_binding_short_circuits(self):
        # ``continue`` rather than falling through, so the generic path does
        # not then try to resolve a token for it and log a spurious failure.
        src = inspect.getsource(integration_tools._build_integration_tools_inner)
        idx = src.index("_build_a2a_delegation_tool")
        assert "continue" in src[idx : idx + 220]


class TestDirectionGatesTheTool:
    @pytest.mark.asyncio
    async def test_a_non_a2a_provider_is_not_mine(self):
        assert await integration_tools._build_a2a_delegation_tool(
            None, "github", str(uuid.uuid4())
        ) is None

    @pytest.mark.asyncio
    async def test_an_inbound_registration_confers_no_tool(self, monkeypatch):
        # The mirror of the exposure rule: an inbound binding exposes the
        # agent and gives it nothing to call with.
        from types import SimpleNamespace

        from app.a2a import providers
        from tests.a2a_chat_gate import stub_chat_gate

        stub_chat_gate(monkeypatch)

        async def _settings(_db, _id):
            return SimpleNamespace(direction="inbound")

        monkeypatch.setattr(providers, "get_settings", _settings)
        assert await integration_tools._build_a2a_delegation_tool(
            None, "oauth_a2a", str(uuid.uuid4())
        ) is None

    @pytest.mark.asyncio
    async def test_a_missing_settings_row_confers_no_tool(self, monkeypatch):
        # Fail closed in both directions: no row means neither exposure nor
        # tool, so a bug that loses the row under-permits rather than over.
        from app.a2a import providers
        from tests.a2a_chat_gate import stub_chat_gate

        stub_chat_gate(monkeypatch)

        async def _none(_db, _id):
            return None

        monkeypatch.setattr(providers, "get_settings", _none)
        assert await integration_tools._build_a2a_delegation_tool(
            None, "oauth_a2a", str(uuid.uuid4())
        ) is None

    @pytest.mark.asyncio
    async def test_the_chat_gate_keeps_the_tool_out_of_the_model_s_context(
        self, monkeypatch
    ):
        # Better than a tool that always refuses: an offered-then-refusing
        # tool teaches a model to retry, and costs context on every run. An
        # OUTBOUND row, so the direction check passes and the chat gate is the
        # one thing standing between the binding and the tool.
        from types import SimpleNamespace

        from app.a2a import providers
        from tests.a2a_chat_gate import stub_chat_gate

        stub_chat_gate(monkeypatch, enabled=False)

        async def _outbound(_db, _id):
            return SimpleNamespace(direction="outbound")

        monkeypatch.setattr(providers, "get_settings", _outbound)
        assert await integration_tools._build_a2a_delegation_tool(
            None, "oauth_a2a", str(uuid.uuid4())
        ) is None

    @pytest.mark.asyncio
    async def test_a_malformed_binding_id_is_skipped_not_raised(self, monkeypatch):
        from tests.a2a_chat_gate import stub_chat_gate

        stub_chat_gate(monkeypatch)
        assert await integration_tools._build_a2a_delegation_tool(
            None, "oauth_a2a", "not-a-uuid"
        ) is None


class TestTheToolIsDispatchableNotMerelyPresent:
    """The gap every other test in this file walks past.

    The assertions above pin where the branch SITS in the source — before the
    token check, one loop, followed by ``continue``. All of that can hold
    while the tool is undispatchable: a definition can enter the model's
    context with no handler registered on the executor, and calling it then
    answers "Unknown tool: delegate_to_...".

    Pinning source-text placement is not the same claim as "reaches a run",
    so these call the handler.
    """

    @pytest.fixture
    def outbound(self, monkeypatch):
        """An outbound registration whose delegation is captured, not sent."""
        from types import SimpleNamespace

        from app.a2a import providers
        from app.a2a.client import delegation
        from app.models.a2a import OUTCOME_COMPLETED
        from tests.a2a_chat_gate import stub_chat_gate

        stub_chat_gate(monkeypatch)

        settings_row = SimpleNamespace(direction="outbound")

        async def _settings(_db, _id):
            return settings_row

        monkeypatch.setattr(providers, "get_settings", _settings)

        # One agent this partner exposes. The card state lives here now, so a
        # registration reaching three agents would supply three of these and
        # get three tools.
        target = SimpleNamespace(
            id=uuid.uuid4(),
            card_url="https://partner.example/.well-known/agent-card.json",
            declared_name="Partner Agent",
            declared_skills=["detection"],
            signature_state="unsigned",
            usability="usable",
        )

        async def _targets(_db, _id):
            return [target]

        monkeypatch.setattr(providers, "outbound_targets", _targets)

        integration = SimpleNamespace(
            id=uuid.uuid4(), name="XTM One B to A", provider="oauth_a2a"
        )

        class _Db:
            async def execute(self, _stmt):
                return SimpleNamespace(scalar_one_or_none=lambda: integration)

        sent = {}

        async def _fake_delegate(db, integ, row, *, message, continue_task_id, calling, **kw):
            sent.update(
                message=message,
                continue_task_id=continue_task_id,
                calling=calling,
                integration=integ,
                ceiling_seconds=kw.get("ceiling_seconds"),
            )
            return SimpleNamespace(
                outcome=OUTCOME_COMPLETED,
                still_running=False,
                needs_input=False,
                question=None,
                partner_task_id=None,
                result_text="partner answered",
                signature_state="unsigned",
                protocol_version="0.3",
            )

        monkeypatch.setattr(delegation, "delegate", _fake_delegate)
        return SimpleNamespace(db=_Db(), integration=integration, sent=sent)

    async def _dispatch(self, outbound, arguments):
        """Call the tool exactly as the model would — only the keys it sent."""
        from app.services.integration_tools import _SafeIntegrationExecutor
        from app.tools.base import ToolCall

        executor = _SafeIntegrationExecutor()
        defs = await integration_tools._build_a2a_delegation_tool(
            outbound.db,
            "oauth_a2a",
            str(outbound.integration.id),
            executor,
            {"agent_id": str(uuid.uuid4()), "user_id": str(uuid.uuid4())},
        )
        return await executor.execute_tool(
            ToolCall(id="c9", name=defs[0].name, arguments=arguments)
        )

    async def test_a_collect_carries_no_message(self, outbound):
        """What ``render_result`` tells the model to do when a partner is
        still running: name the task and send nothing else. The schema makes
        ``message`` optional, so the handler must have a default — without one
        the advertised recovery path answers a TypeError."""
        result = await self._dispatch(outbound, {"continue_task_id": "t-9"})
        assert not result.is_error, result.content
        assert outbound.sent["continue_task_id"] == "t-9"
        assert outbound.sent["message"] == ""

    async def test_a_collect_is_bounded_by_the_grace_not_the_ceiling(self, outbound):
        """Found live: a same-run collect of a partner still working held the
        tool call for the whole delegation ceiling (300 s) and then handed back
        still_running. A collect is a question: it hands back at the grace."""
        from app.core.config import settings

        await self._dispatch(outbound, {"continue_task_id": "t-9"})
        assert outbound.sent["ceiling_seconds"] == settings.a2a_client_grace_seconds
        assert outbound.sent["ceiling_seconds"] < 30

    async def test_a_send_keeps_the_full_ceiling(self, outbound):
        """Work sent (a message, a continuation answer included) is a wait,
        bounded by the ceiling as before."""
        await self._dispatch(outbound, {"message": "go", "continue_task_id": "t-9"})
        assert outbound.sent["ceiling_seconds"] is None

    async def test_a_call_with_neither_reaches_no_partner(self, outbound):
        """Optional on both sides leaves the empty call sayable. Sent on, it
        is a delegation with no instruction — a mistake, not work."""
        result = await self._dispatch(outbound, {})
        assert outbound.sent == {}, "nothing may be submitted to the partner"
        assert "nothing_to_send" in str(result.content)

    async def test_a_handler_is_registered_under_the_advertised_name(self, outbound):
        from app.services.integration_tools import _SafeIntegrationExecutor

        executor = _SafeIntegrationExecutor()
        defs = await integration_tools._build_a2a_delegation_tool(
            outbound.db,
            "oauth_a2a",
            str(outbound.integration.id),
            executor,
            {"agent_id": str(uuid.uuid4()), "user_id": str(uuid.uuid4())},
        )
        assert defs, "an outbound registration must contribute a tool"
        # The exact bug: advertised, undispatchable. The name the model is
        # given must be the name the executor answers to.
        assert defs[0].name in executor.tool_names

    async def test_calling_the_tool_delegates_and_renders(self, outbound):
        from app.services.integration_tools import _SafeIntegrationExecutor
        from app.tools.base import ToolCall

        executor = _SafeIntegrationExecutor()
        agent_id, user_id = uuid.uuid4(), uuid.uuid4()
        defs = await integration_tools._build_a2a_delegation_tool(
            outbound.db,
            "oauth_a2a",
            str(outbound.integration.id),
            executor,
            {"agent_id": str(agent_id), "user_id": str(user_id)},
        )
        result = await executor.execute_tool(
            ToolCall(
                id="c1",
                name=defs[0].name,
                arguments={"message": "classify this sample"},
            )
        )
        assert not result.is_error, result.content
        assert "Unknown tool" not in str(result.content)
        assert "partner answered" in str(result.content)
        # FR-042: attribution recorded at submission, from the calling run.
        assert outbound.sent["message"] == "classify this sample"
        assert outbound.sent["calling"].agent_id == agent_id
        assert outbound.sent["calling"].user_id == user_id

    async def test_a_continuation_id_is_passed_through(self, outbound):
        from app.services.integration_tools import _SafeIntegrationExecutor
        from app.tools.base import ToolCall

        executor = _SafeIntegrationExecutor()
        defs = await integration_tools._build_a2a_delegation_tool(
            outbound.db,
            "oauth_a2a",
            str(outbound.integration.id),
            executor,
            {"agent_id": str(uuid.uuid4()), "user_id": str(uuid.uuid4())},
        )
        await executor.execute_tool(
            ToolCall(
                id="c2",
                name=defs[0].name,
                arguments={"message": "the answer is 4", "continue_task_id": "t-9"},
            )
        )
        # FR-031: a continuation must continue THAT task rather than opening a
        # new one, so the id has to survive the hop from model to delegate().
        assert outbound.sent["continue_task_id"] == "t-9"

    @pytest.mark.parametrize(
        ("context", "why"),
        [
            ({}, "no identity at all — tool-test endpoints, flow nodes"),
            ({"user_id": str(uuid.uuid4())}, "user but no agent"),
            ({"agent_id": str(uuid.uuid4())}, "agent but no user"),
            (
                {"agent_id": "not-a-uuid", "user_id": str(uuid.uuid4())},
                "unparseable agent id",
            ),
        ],
    )
    async def test_without_attribution_there_is_no_tool(self, outbound, context, why):
        # FR-042 makes attribution a precondition. The alternative — deriving
        # a plausible identity — would put a delegation on record against
        # something that did not send it.
        from app.services.integration_tools import _SafeIntegrationExecutor

        executor = _SafeIntegrationExecutor()
        defs = await integration_tools._build_a2a_delegation_tool(
            outbound.db, "oauth_a2a", str(outbound.integration.id), executor, context
        )
        assert defs == [], why
        assert not executor.tool_names

    async def test_an_unattributable_binding_is_still_ours(self, outbound):
        # ``[]`` not ``None``: None means "not an A2A binding, carry on", and
        # the caller would then send this integration down the ordinary
        # provider path hunting for a stored token it never has.
        from app.services.integration_tools import _SafeIntegrationExecutor

        defs = await integration_tools._build_a2a_delegation_tool(
            outbound.db,
            "oauth_a2a",
            str(outbound.integration.id),
            _SafeIntegrationExecutor(),
            {},
        )
        assert defs is not None


class TestTheRegistrationIsNotAToolArgument:
    def test_the_handler_exposes_only_the_two_model_facing_arguments(self):
        """The executor filters a call to the handler's PARAMETER NAMES.

        Anything bound as a parameter is therefore settable by the model,
        however underscored — a ``_target`` in the tool call would replace the
        registration the closure exists to pin. And a handler taking
        ``**kwargs`` is not filtered at all.
        """
        import inspect
        import re

        src = inspect.getsource(integration_tools)
        match = re.search(r"async def _handler\(\s*(.*?)\s*\):", src, re.S)
        assert match, "the delegation handler was renamed"
        params = [
            p.strip().split(":")[0].split("=")[0].strip()
            for p in match.group(1).split(",")
            if p.strip()
        ]
        assert params == ["message", "continue_task_id"], params
        assert "**" not in match.group(1)


class TestRunTeardownCancelsOutstandingTasks:
    """FR-025's run-teardown half.

    A partner that answered ``input-required`` is working toward an answer
    only this run can give. When the run ends — completed, failed or
    cancelled — whatever is still open is cancelled at the partner, best
    effort, from ``run_agent_chat``'s teardown. A chat turn ending is not
    that: its user answers the question in the next turn.
    """

    @pytest.fixture
    def rig(self, monkeypatch):
        """A dispatchable delegation tool whose partner answers are scripted."""
        from types import SimpleNamespace

        from app.a2a import providers
        from app.a2a.client import delegation
        from app.models.a2a import OUTCOME_COMPLETED
        from tests.a2a_chat_gate import stub_chat_gate

        stub_chat_gate(monkeypatch)

        settings_row = SimpleNamespace(direction="outbound")

        async def _settings(_db, _id):
            return settings_row

        monkeypatch.setattr(providers, "get_settings", _settings)

        target = SimpleNamespace(
            id=uuid.uuid4(),
            card_url="https://partner.example/.well-known/agent-card.json",
            declared_name="Partner Agent",
            declared_skills=["detection"],
            signature_state="unsigned",
            usability="usable",
        )

        async def _targets(_db, _id):
            return [target]

        monkeypatch.setattr(providers, "outbound_targets", _targets)

        integration = SimpleNamespace(
            id=uuid.uuid4(), name="XTM One B to A", provider="oauth_a2a"
        )

        class _Db:
            async def execute(self, _stmt):
                return SimpleNamespace(scalar_one_or_none=lambda: integration)

        answers: list = []

        async def _fake_delegate(db, integ, row, *, message, continue_task_id, calling, **_kw):
            return answers.pop(0)

        monkeypatch.setattr(delegation, "delegate", _fake_delegate)

        cancelled: list = []

        async def _fake_cancel(integ, row, task_id):
            cancelled.append((integ, row, task_id))

        monkeypatch.setattr(delegation, "cancel", _fake_cancel)

        def _result(*, needs_input: bool, task_id: str | None):
            return SimpleNamespace(
                outcome=OUTCOME_COMPLETED,
                still_running=False,
                needs_input=needs_input,
                question="what colour?" if needs_input else None,
                partner_task_id=task_id,
                result_text=None if needs_input else "partner answered",
                signature_state="unsigned",
                protocol_version="0.3",
            )

        return SimpleNamespace(
            db=_Db(),
            integration=integration,
            target=target,
            answers=answers,
            cancelled=cancelled,
            result=_result,
        )

    async def _tool(self, rig, **context):
        from app.services.integration_tools import _SafeIntegrationExecutor

        executor = _SafeIntegrationExecutor()
        defs = await integration_tools._build_a2a_delegation_tool(
            rig.db,
            "oauth_a2a",
            str(rig.integration.id),
            executor,
            {"agent_id": str(uuid.uuid4()), "user_id": str(uuid.uuid4()), **context},
        )
        return executor, defs[0].name

    async def _call(self, executor, name, **arguments):
        from app.tools.base import ToolCall

        return await executor.execute_tool(
            ToolCall(id="c1", name=name, arguments={"message": "go", **arguments})
        )

    async def test_a_task_left_awaiting_input_is_cancelled_at_run_end(self, rig):
        executor, name = await self._tool(rig)
        rig.answers.append(rig.result(needs_input=True, task_id="t-1"))
        await self._call(executor, name)
        assert set(executor.a2a_outstanding) == {"t-1"}

        await integration_tools.cancel_outstanding_delegations(executor)
        assert rig.cancelled == [(rig.integration, rig.target, "t-1")]
        assert executor.a2a_outstanding == {}

    async def test_an_assignment_run_s_question_is_cancelled_at_run_end(self, rig):
        executor, name = await self._tool(rig, run_id=str(uuid.uuid4()))
        rig.answers.append(rig.result(needs_input=True, task_id="t-1"))
        await self._call(executor, name)

        await integration_tools.cancel_outstanding_delegations(executor)
        assert rig.cancelled == [(rig.integration, rig.target, "t-1")]

    async def test_a_chat_turn_s_question_survives_the_turn(self, rig):
        # The agent relays the partner's question to its user, and only the
        # user's NEXT turn can answer it: ending this one is how it asks.
        # Cancelled here, the answer resumes a task already ``canceled``.
        executor, name = await self._tool(rig, conversation_id=str(uuid.uuid4()))
        rig.answers.append(rig.result(needs_input=True, task_id="t-1"))
        await self._call(executor, name)
        assert executor.a2a_outstanding == {}

        await integration_tools.cancel_outstanding_delegations(executor)
        assert rig.cancelled == []

    async def test_a_continuation_that_terminates_retires_the_task(self, rig):
        executor, name = await self._tool(rig)
        rig.answers.append(rig.result(needs_input=True, task_id="t-1"))
        await self._call(executor, name)
        rig.answers.append(rig.result(needs_input=False, task_id="t-1"))
        await self._call(executor, name, continue_task_id="t-1")

        assert executor.a2a_outstanding == {}
        await integration_tools.cancel_outstanding_delegations(executor)
        assert rig.cancelled == []

    async def test_a_failing_cancel_never_breaks_teardown(self, rig, monkeypatch):
        from app.a2a.client import delegation

        executor, name = await self._tool(rig)
        rig.answers.append(rig.result(needs_input=True, task_id="t-1"))
        await self._call(executor, name)

        async def _boom(_i, _t, _id):
            raise RuntimeError("partner gone")

        monkeypatch.setattr(delegation, "cancel", _boom)
        await integration_tools.cancel_outstanding_delegations(executor)
        assert executor.a2a_outstanding == {}

    async def test_the_hand_back_names_the_registration_s_own_cancel_tool(
        self, rig
    ):
        """The name is only settled after every delegation name is taken, so
        the hand-back reads it from the closure at call time. Read at
        definition time instead it is ``None`` for the life of the process,
        and the hand-back tells the user to use a tool it cannot name."""
        from types import SimpleNamespace

        from app.models.a2a import OUTCOME_STILL_RUNNING

        executor, name = await self._tool(rig)
        rig.answers.append(
            SimpleNamespace(
                outcome=OUTCOME_STILL_RUNNING,
                still_running=True,
                needs_input=False,
                question=None,
                partner_task_id="t-1",
                result_text=None,
                signature_state="unsigned",
                protocol_version="0.3",
                awaiting_callback=False,
                continuation_count=0,
                detail=None,
                partner_reason=None,
            )
        )
        handed_back = await self._call(executor, name)
        cancel_name = next(
            n for n in executor._handlers if n.startswith("cancel_task_at_")
        )
        assert cancel_name in handed_back.content

    def test_run_teardown_wires_the_drain(self):
        # The helper working is not the claim "a run's end reaches it".
        # ``run_agent_chat``'s teardown is the one place that sees every way a
        # run can end — completed, failed, cancelled.
        from app.services import agent_chat

        src = inspect.getsource(agent_chat.run_agent_chat)
        drained_at = src.index("cancel_outstanding_delegations(integ_exec)")
        assert src.rindex("finally:", 0, drained_at) > 0

    def test_assignment_run_teardown_wires_the_drain(self):
        # Assignment runs build their own executor and get the delegation
        # tool too; a run ending at ``input-required`` must cancel as well.
        from app.services import assignment_engine

        src = inspect.getsource(assignment_engine.execute_assignment_run)
        # The drain lives in ``_teardown_partner_work``, called from
        # the run's ``finally`` so every ending reaches it.
        teardown = inspect.getsource(assignment_engine._teardown_partner_work)
        assert "cancel_outstanding_delegations(integ_exec)" in teardown
        drained_at = src.index("await _teardown_partner_work(")
        assert src.rindex("finally:", 0, drained_at) > 0
        # Bound before tools are built, or a run that fails before reaching
        # them raises NameError in its own teardown.
        assert src.index("integ_exec = None") < src.index("build_integration_tools(")


class TestTwoRegistrationsCannotShareAToolName:
    """Same operator name, same declared agent, two registrations: two tools,
    not one silently overwritten handler."""

    async def test_the_same_names_twice_yield_two_tools_not_one(self, monkeypatch):
        from types import SimpleNamespace

        from app.a2a import providers
        from app.services.integration_tools import _SafeIntegrationExecutor
        from tests.a2a_chat_gate import stub_chat_gate

        stub_chat_gate(monkeypatch)

        async def _settings(_db, _id):
            return SimpleNamespace(direction="outbound")

        monkeypatch.setattr(providers, "get_settings", _settings)

        async def _targets(_db, _id):
            return [
                SimpleNamespace(
                    id=uuid.uuid4(),
                    card_url="https://partner.example/.well-known/agent-card.json",
                    declared_name="Triage",
                    declared_skills=[],
                    signature_state="unsigned",
                    usability="usable",
                )
            ]

        monkeypatch.setattr(providers, "outbound_targets", _targets)

        def _db_for(integration):
            class _Db:
                async def execute(self, _stmt):
                    return SimpleNamespace(scalar_one_or_none=lambda: integration)

            return _Db()

        first = SimpleNamespace(id=uuid.uuid4(), name="Partner", provider="oauth_a2a")
        second = SimpleNamespace(id=uuid.uuid4(), name="Partner", provider="oauth_a2a")
        executor = _SafeIntegrationExecutor()
        context = {"agent_id": str(uuid.uuid4()), "user_id": str(uuid.uuid4())}
        taken: set[str] = set()

        first_tools = await integration_tools._build_a2a_delegation_tool(
            _db_for(first), "oauth_a2a", str(first.id), executor, context, taken=taken
        )
        second_tools = await integration_tools._build_a2a_delegation_tool(
            _db_for(second), "oauth_a2a", str(second.id), executor, context, taken=taken
        )
        # One delegation tool per reachable agent, plus one cancel for the
        # registration. Every name disambiguates, not just the delegation's:
        # a shared cancel handler would be overwritten as silently.
        assert len(first_tools) == len(second_tools) == 2
        for a, b in zip(first_tools, second_tools, strict=True):
            assert a.name != b.name
        # All dispatchable, under the names the model was given.
        assert {t.name for t in first_tools + second_tools} <= set(
            executor.tool_names
        )

    def test_the_binding_loop_shares_one_set_across_bindings(self):
        src = inspect.getsource(integration_tools._build_integration_tools_inner)
        assert "taken=_a2a_tool_names" in src
