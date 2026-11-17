"""Mid-turn approval for chat (FR-002a/c).

Chat was ungated: the same agent calling the same tool paused on a schedule and
ran freely in a conversation. Confirmed as a spec miss.

The interesting part is *how* it pauses. A scheduled run parks in
``awaiting_human`` because nobody is there. In chat somebody already is, so the
loop waits **within the turn** — the agent stops at the gated call, the reviewer
decides, and the same turn continues with the tool result. Unwinding and
restarting would discard the reasoning already done and make an approval feel
like a rejection.

The channel is optional. Without one the loop raises as before, which is what
assignment runs and plain REST rely on.
"""

from __future__ import annotations

from app.tools.approval import evaluate_gate, fingerprint
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    HumanInputRequestedError,
    ScopedToolExecutor,
    ToolCall,
    ToolDefinition,
)

SCHEMA = {"type": "object", "properties": {"target": {"type": "string"}}}


def _build(*names: str, whitelist: frozenset[str] = frozenset()):
    executed: list[str] = []
    callable_exec = CallableToolExecutor()
    definitions = []
    for name in names:

        async def _handler(_name=name, **kwargs):
            executed.append(_name)
            return f"{_name} ran"

        callable_exec.register(name, _handler)
        definitions.append(
            ToolDefinition(
                name=name, description=f"does {name}", input_schema=SCHEMA, source="test"
            )
        )

    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)

    def gate(call: ToolCall) -> None:
        decision = evaluate_gate(
            call.name,
            hitl_enabled=True,
            whitelist=whitelist,
            active_exceptions={},
            current_fingerprint=fingerprint(SCHEMA),
        )
        if decision.action == "pause":
            raise HumanInputRequestedError(
                question=f"Approve {call.name}?", tool_call_id=call.id, conversation=[]
            )

    composite.set_approval_gate(gate)
    return composite, executed, definitions


def _call(cid: str, name: str) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments={"target": "x"})


# --- the bypass that makes mid-turn resumption possible ------------------


async def test_approved_call_does_not_re_gate():
    """Without this the turn would ask forever.

    The reviewer approves; the loop then executes that same call through the
    same executor. If the gate fired again it would pause on the very action
    just signed off.
    """
    composite, executed, _ = _build("delete_entity")

    composite.bypass_approval_for({"c1"})
    result = await composite.execute_tool(_call("c1", "delete_entity"))

    assert executed == ["delete_entity"]
    assert result.is_error is False


async def test_bypass_is_scoped_to_the_approved_call_only():
    """Approving one call must not open the door for its siblings."""
    composite, executed, _ = _build("delete_entity")

    composite.bypass_approval_for({"c1"})

    await composite.execute_tool(_call("c1", "delete_entity"))
    try:
        await composite.execute_tool(_call("c2", "delete_entity"))
        raise AssertionError("a different call id bypassed the gate")
    except HumanInputRequestedError:
        pass

    assert executed == ["delete_entity"]


async def test_bypassed_calls_are_not_re_screened():
    """The pre-screen must agree with dispatch, or the turn re-proposes."""
    composite, _executed, _ = _build("delete_entity")

    assert composite.screen_for_approval([_call("c1", "delete_entity")])
    composite.bypass_approval_for({"c1"})
    assert composite.screen_for_approval([_call("c1", "delete_entity")]) == []


async def test_bypass_is_additive():
    composite, _executed, _ = _build("a_tool", "b_tool")

    composite.bypass_approval_for({"c1"})
    composite.bypass_approval_for({"c2"})

    assert composite.screen_for_approval(
        [_call("c1", "a_tool"), _call("c2", "b_tool")]
    ) == []


async def test_scoped_executor_delegates_the_bypass():
    """The scoped wrapper is what the real chat path uses."""
    composite, executed, definitions = _build("delete_entity")
    scoped = ScopedToolExecutor(composite, definitions)

    scoped.bypass_approval_for({"c1"})
    await scoped.execute_tool(_call("c1", "delete_entity"))

    assert executed == ["delete_entity"]


# --- the loop only waits when given a channel ----------------------------


def test_loop_accepts_an_approval_channel():
    import inspect

    from app.tools.agent_loop import run_agent_loop

    params = inspect.signature(run_agent_loop).parameters
    assert "request_approval" in params
    assert params["request_approval"].default is None, (
        "the channel must be opt-in — assignment runs and REST rely on the "
        "loop unwinding instead"
    )


def test_loop_waits_when_a_channel_is_supplied():
    """Source pin: the branch cannot be driven without a full loop harness."""
    import inspect

    from app.tools import agent_loop

    source = inspect.getsource(agent_loop.run_agent_loop)

    # Raced against the turn's cancel: a stop must end the wait, or an answer
    # given afterwards would run a tool for a stopped turn.
    assert "request_approval(_proposals), cancel_event" in source
    assert "bypass_approval_for" in source, (
        "approved calls would re-gate and the turn would ask forever"
    )
    assert "_apply_approval_decisions" in source, (
        "decisions must produce tool results, or the turn resumes with "
        "unanswered tool calls"
    )


def test_chat_resolves_gating_from_the_agent_switch():
    """Chat has no assignment, so there is no override to consult (FR-012a)."""
    import inspect

    from app.services import agent_chat

    source = inspect.getsource(agent_chat.run_agent_chat)

    assert "resolve_approve_tool_calls(db, agent)" in source
    assert "human_in_the_loop" not in source.split("build_approval_gate")[1][:400]


# --- the channel reaches the streaming client ---------------------------


def test_run_agent_chat_forwards_the_approval_channel():
    """A channel that stops at run_agent_chat never reaches the loop."""
    import inspect

    from app.services.agent_chat import run_agent_chat

    assert "request_approval" in inspect.signature(run_agent_chat).parameters
    source = inspect.getsource(run_agent_chat)
    assert "request_approval=request_approval" in source, (
        "the channel is accepted but not passed on, so the loop would still "
        "unwind instead of waiting"
    )


def test_platform_streaming_chat_supplies_a_channel():
    """This is the endpoint the external chatbot actually calls.

    Source-pinned: the surrounding endpoint needs a live SSE turn to exercise.
    """
    import inspect

    from app.api.v1 import platform

    source = inspect.getsource(platform)

    assert "_request_approval if body.supports_tool_approval" in source
    assert '"type": "approval_required"' in source, (
        "the client is never told a decision is needed, so the turn waits on "
        "an approval nobody can see"
    )
    assert "await_approval_decision" in source


def test_approve_endpoint_exists_and_reports_a_missed_turn():
    """409 rather than silent acceptance.

    A decision for a turn that already finished has nothing to apply to, and
    accepting it would tell the user they approved something that never ran.
    """
    import inspect

    from app.api.v1 import platform

    assert "apply_approval_decisions(" in inspect.getsource(platform.approve_platform_chat_tools)
    source = inspect.getsource(platform.apply_approval_decisions)

    assert "push_approval_decision" in source
    assert "409" in source


def test_decision_delivery_survives_a_replica_hop():
    """The deciding request need not land on the replica running the turn."""
    import inspect

    from app.services import chat_turn_guard

    source = inspect.getsource(chat_turn_guard.push_approval_decision)
    assert "rpush" in source, "an in-process queue would strand cross-replica decisions"


# --- the native web chat closes the same loop ---------------------------


def test_web_chat_supplies_a_channel_and_handles_the_reply():
    """Both halves, because either alone leaves the turn stuck.

    Emitting without handling the reply waits forever; handling without
    emitting means the user is never asked.
    """
    import inspect

    from app.api.v1 import chat, chat_web_stream

    source = inspect.getsource(chat)

    assert "_turn_request_approval" in source
    assert '"type": "approval_required"' in source
    assert "apply_approval_decisions(" in inspect.getsource(chat_web_stream.approve_web_chat_tools), (
        "the client's decision is read by nobody, so the turn waits on "
        "an answer that already arrived"
    )


# --- the General Assistant is the other half of chat ----------------------


def _general_chat_call_in_the_runner() -> str:
    """The ``run_general_chat(...)`` call in the web chat's connection runner.

    Sliced out rather than searched for across the whole module, because the
    agent branch in the same function passes the identical expression — a
    substring check on the module would pass on the agent's wiring alone, which
    is exactly the bug these tests exist to catch. The REST handler calls the
    same function and is *meant* to stay channel-less, so it must not be the
    one measured either.
    """
    import inspect

    from app.api.v1 import chat

    source = inspect.getsource(chat)
    # ``rpartition``: the REST handler calls the same function earlier in the
    # module, and the web chat's stream is the surface that can hold a turn open.
    tail = source.rpartition("await run_general_chat(")[2]
    assert tail, "the connection runner no longer calls run_general_chat"
    lines: list[str] = []
    for line in tail.splitlines():
        if line.strip() == ")":
            break
        lines.append(line)
    else:  # pragma: no cover - the call is always closed
        raise AssertionError("could not find the end of the run_general_chat call")
    return "\n".join(lines)


def test_run_general_chat_forwards_the_approval_channel():
    """``general_chat`` is a parallel implementation of ``agent_chat``, and the
    channel was wired into one sibling only. So the General Assistant installed
    the gate, paused on a destructive tool, and had no way to ask — the turn
    unwound into prose while the same tool on an agent showed a prompt.
    """
    import inspect

    from app.services.general_chat import run_general_chat

    assert "request_approval" in inspect.signature(run_general_chat).parameters
    source = inspect.getsource(run_general_chat)
    assert "request_approval=request_approval" in source, (
        "the channel is accepted but not passed on, so the turn would still "
        "unwind instead of waiting"
    )


def test_the_general_assistant_is_offered_the_channel_over_the_web_chat():
    """Accepting the parameter is inert until a caller supplies it, and the
    web chat's stream is the caller that can."""
    call = _general_chat_call_in_the_runner()

    assert "request_approval=" in call, (
        "the General Assistant branch never hands over the channel, so every "
        "gated call there still ends the turn"
    )
    assert '_turn_supports_approval["value"]' in call, (
        "same opt-in as the agent branch: a stale bundle would be prompted and "
        "never answer"
    )


def test_a_standing_approval_from_the_general_assistant_is_recorded():
    """"Yes, always" promises the reviewer this also covers their scheduled
    runs. Without ``on_approve_always`` the loop has nowhere to report the
    verdict, so nothing is persisted and the tool asks again next turn — the
    disclosure they were shown becomes false.
    """
    import inspect

    from app.services.general_chat import run_general_chat

    source = inspect.getsource(run_general_chat)
    assert "on_approve_always=" in source
    assert "grant_chat_standing_approval(" in source, (
        "the callback has to write the same preference a run would create, or "
        "the standing approval only lasts the turn"
    )
    assert "turn_impersonated_by=" in source, (
        "an impersonated admin's consent must name both identities (FR-017b)"
    )
    assert "approval_outcomes=" in source, (
        "a human verdict is the call's gate outcome (FR-020) and only the loop "
        "sees it"
    )


def test_the_general_assistant_moves_its_own_deadline_for_a_pause():
    """Same correction as the agent path: this turn ran under ``wait_for``,
    which cannot be extended once started, so a reviewer slower than the loop
    budget had their answer arrive to a cancelled turn.
    """
    import inspect

    from app.services import general_chat

    source = inspect.getsource(general_chat.run_general_chat)
    assert "asyncio.timeout(chat_loop_timeout)" in source, (
        "wait_for cannot be extended once started"
    )
    assert "reschedule(" in source and "on_approval_wait=" in source


def test_web_chat_decision_routes_through_redis_not_memory():
    """The approve route runs in another request, maybe on another replica, than the turn."""
    import inspect

    from app.api.v1 import platform

    source = inspect.getsource(platform.apply_approval_decisions)
    assert "push_approval_decision" in source


# --- an old client must degrade, never hang -----------------------------


def test_approval_channel_is_opt_in_on_the_platform_endpoint():
    """The failure this prevents is the worst kind: silent.

    A client that predates approvals ignores the ``approval_required`` event and
    never answers. If the server had supplied a channel anyway it would wait
    forever — no error, no timeout, just a turn that never ends and a user
    watching a spinner.
    """
    from app.api.v1.platform import PlatformChatRequest

    assert PlatformChatRequest.model_fields["supports_tool_approval"].default is False, (
        "defaulting to True would hang every client that has not been updated"
    )


def test_platform_only_supplies_the_channel_when_declared():
    import inspect

    from app.api.v1 import platform

    source = inspect.getsource(platform)
    assert "if body.supports_tool_approval else None" in source


def test_web_chat_only_supplies_the_channel_when_declared():
    """Same hazard for our own UI: a stale browser tab is an old client."""
    import inspect

    from app.api.v1 import chat

    source = inspect.getsource(chat)
    assert '_turn_supports_approval = {"value": False}' in source
    assert 'data.get("supports_tool_approval")' in source, (
        "support must be declared on the send frame — inferring it from a "
        "decision that only arrives after a prompt is circular"
    )


def test_degraded_message_is_plain_prose_an_old_client_can_render():
    """No structured payload, no new UI, nothing to opt into.

    Whatever an un-updated client shows for ordinary assistant text is what the
    user will read, so the text alone has to explain what happened and what to
    do about it. Checked on the message itself rather than on the source that
    once held the literal — it is now shared by every surface that installs a
    gate without a way to answer it.
    """
    from app.tools.approval import no_approval_channel_message

    class _Paused:
        proposals = [{"tool_name": "delete_entity"}]
        question = "Approval required to run delete_entity"

    message = no_approval_channel_message(_Paused())

    assert "delete_entity" in message, "name what is being refused"
    assert "I need approval before running" in message
    assert "Tools page" in message, (
        "the message should name a way forward, or the reader has none. The "
        "nearest one is the reader's own — they can allow the tool "
        "for themselves there — with the administrator's beside it"
    )
    assert "<" not in message and "{" not in message, (
        "plain prose: no markup or structured payload an old client cannot render"
    )


def test_the_degraded_message_still_names_a_tool_without_proposals():
    """A gate that raises without proposals — the general assistant's does —
    must not degrade to "a tool"; the question already names it."""
    from app.tools.approval import no_approval_channel_message

    class _Paused:
        proposals = None
        question = "Approval required to run purge_index"

    assert "purge_index" in no_approval_channel_message(_Paused())


def test_sse_keepalive_covers_an_approval_pause():
    """The pause is silent, and silence is what proxies kill.

    A turn waiting on a reviewer emits nothing: no tokens, no status events.
    Every read timeout between the browser and this process then becomes an
    approval deadline nobody chose. The tightest one we know of is OpenAEV's
    Java proxy, which sets a 15-minute response timeout on the streaming call;
    OpenCTI passes ``timeout: 0`` and OpenGRC leaves the stream untimed.

    The keepalive that already exists for long silent LLM windows is what
    stops that, because a paused turn is just another silent window on
    ``stream_q``. This test exists so the dependency is not invisible: raising
    the interval past a host's timeout would break approvals in a way that
    looks like a network fault.
    """
    from app.api.v1.platform import _SSE_KEEPALIVE_INTERVAL_SECONDS

    tightest_known_host_timeout = 15 * 60  # OpenAEV, Timeout.ofMinutes(15)
    # Bound to a local so the assertion reads keepalive-first, which is the
    # direction the sentence below argues, without tripping the Yoda-condition
    # lint that an ALL_CAPS name on the left otherwise triggers.
    keepalive_interval = _SSE_KEEPALIVE_INTERVAL_SECONDS
    assert keepalive_interval < tightest_known_host_timeout / 10, (
        "the SSE keepalive must stay well inside the tightest host read "
        "timeout, or a reviewer who takes their time loses the turn"
    )


def test_sse_keepalive_is_a_comment_not_an_event():
    """It must be invisible to clients that never opted into approvals.

    An SSE comment line is dropped by every conformant parser, so the
    keepalive cannot be mistaken for content by a client that has no idea
    approvals exist.
    """
    import inspect

    from app.api.v1 import platform

    source = inspect.getsource(platform)
    assert 'yield ": keepalive\\n\\n"' in source


def test_the_send_stream_keepalive_is_not_gated_on_turn_state():
    """The stream idles hardest exactly when a turn is paused.

    A keepalive that only ran while frames flowed would go quiet during the
    one window it is needed for.
    """
    import inspect

    from app.services.chat_web_transport import DetachedTurnStreamResponse

    body = inspect.getsource(DetachedTurnStreamResponse._keepalive)
    assert "while not connection.detached:" in body
    assert "turn" not in body


def test_every_synthesised_chat_response_carries_a_model():
    """``ChatResponse.model`` has no default, so omitting it raises at the point
    the handler is trying to deliver a message rather than at import.

    The degraded-approval path did exactly that: a client with no way to collect
    an approval got "I encountered an error" instead of the explanation written
    for it, and only that client ever reached the line. Asserted over every
    synthesised response in the module rather than the one that was wrong,
    because the next one will be written the same way.
    """
    import ast
    import inspect

    from app.services import agent_chat

    tree = ast.parse(inspect.getsource(agent_chat))
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if not name or not name.endswith("Resp"):
            continue
        if not any(kw.arg == "model" for kw in node.keywords):
            missing.append((name, node.lineno))

    assert not missing, (
        "ChatResponse requires `model`; these constructions omit it and will "
        f"raise TypeError when their branch is reached: {missing}"
    )


def test_the_loop_can_report_how_long_a_reviewer_took():
    """A chat turn runs under `asyncio.timeout(chat_loop_timeout)` and passes
    the loop a matching `wall_clock_deadline`, so time spent waiting for a
    person counted against a budget meant to bound *work*. A reviewer who read
    the arguments and answered past it had the turn cancelled as "took too
    long" — losing the reasoning the mid-turn pause exists to preserve, moments
    after they did the careful thing.
    """
    import inspect

    from app.tools.agent_loop import run_agent_loop

    params = inspect.signature(run_agent_loop).parameters
    assert "on_approval_wait" in params
    assert params["on_approval_wait"].default is None, (
        "a loop without a channel must not require one"
    )

    source = inspect.getsource(run_agent_loop)
    assert "wall_clock_deadline += _paused_for" in source, (
        "the loop's own deadline must move by the time the reviewer took"
    )
    # Moving the deadline is only half of it. ``_wall_clock_total`` is
    # snapshotted before the loop and never re-derived, and the budget nudge
    # measures against it after subtracting ``_wait_paused_total`` — so without
    # this a 20-minute review on a 10-minute budget tripped "consolidate, stop
    # exploring" and told the user half their time was gone on the very next
    # iteration, with the whole extension still unspent. A reviewer's time is
    # not acting time, exactly as for a wait pause.
    assert "_wait_paused_total += _paused_for" in source


def test_chat_moves_its_own_deadline_for_a_pause():
    """The outer budget is the caller's, so only the caller can move it."""
    import inspect

    from app.services import agent_chat

    source = inspect.getsource(agent_chat)
    assert "asyncio.timeout(chat_loop_timeout)" in source, (
        "wait_for cannot be extended once started"
    )
    assert "reschedule(" in source and "on_approval_wait" in source


# --- Only the paused user may answer, on every surface ---------------------


def test_the_web_chat_scopes_the_decision_to_the_conversation_owner():
    """The approve route takes ``conversation_id`` from the body. Unscoped, an
    authenticated caller who knew a UUID could answer somebody else's paused
    turn — and needed no valid ``tool_call_id`` to do damage: unknown ids leave
    every real proposal at the applier's ``reject`` default, so the batch is
    refused, the pending set cleared, and the reviewer's prompt vanishes while
    the agent is told a human declined.
    """
    import inspect

    from app.api.v1 import chat_web_stream, platform

    # The web chat's decision goes through the helper the embedded chat uses.
    route = inspect.getsource(chat_web_stream.approve_web_chat_tools)
    assert "await platform_api.apply_approval_decisions(" in route
    owner = inspect.getsource(platform.approval_conversation)
    apply = inspect.getsource(platform.apply_approval_decisions)

    assert "Conversation.user_id == user.id" in owner, (
        "the decision must scope the conversation to the caller before "
        "pushing anything to the waiting turn"
    )
    assert 'reason="not_the_paused_user"' in owner
    # And the row is written only when the conversation exists. Otherwise an
    # authenticated caller could post random conversation ids and forge one
    # refusal row per request.
    assert owner.index("_exists = ") < owner.index("record_refused_decision(")
    # And the scoping comes before the push.
    assert apply.index("approval_conversation(") < apply.index("push_approval_decision(")


class _GatedToolLLM:
    """First round proposes two calls, any later round answers."""

    provider_name = "stub"
    context_window = 10_000

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        from app.llm.base import ChatResponse

        self.calls += 1
        if self.calls == 1:
            return ChatResponse(
                content="",
                model=model or "stub",
                tool_calls=[
                    ToolCall(id="tc-gated", name="send_mail", arguments={}),
                    ToolCall(id="tc-free", name="read_inbox", arguments={}),
                ],
                stop_reason="tool_use",
            )
        return ChatResponse(content="final answer", model=model or "stub")


class _GatedExecutor:
    """Gates ``send_mail``, records every call it is asked to run."""

    def __init__(self) -> None:
        self.ran: list[str] = []

    def screen_for_approval(self, calls):
        return [
            {"tool_call_id": c.id, "tool_name": c.name, "arguments": c.arguments}
            for c in calls
            if c.name == "send_mail"
        ]

    async def execute_tool(self, tool_call):
        from app.tools.base import ToolResult

        self.ran.append(tool_call.name)
        return ToolResult(tool_call_id=tool_call.id, content="ok")


async def test_a_cancel_ends_an_approval_wait_and_runs_nothing():
    """A stop pressed while a turn waits on a reviewer (the user's own cancel,
    or an emergency stop reaching another replica through the turn's bridge)
    only sets the turn's cancel event. The wait must end on it, the channel's
    wait must be cancelled so it withdraws the prompt, and neither the proposed
    call nor the one that passed the gate may run afterwards.
    """
    import asyncio

    from app.llm.base import ChatMessage
    from app.tools.agent_loop import _CANCELLED_MESSAGE, run_agent_loop

    cancel = asyncio.Event()
    asked = asyncio.Event()
    withdrawn = asyncio.Event()

    async def _request_approval(proposals):
        asked.set()
        try:
            await asyncio.sleep(60)
        finally:
            withdrawn.set()
        return {p["tool_call_id"]: {"decision": "approve"} for p in proposals}

    async def _stop_once_asked():
        await asked.wait()
        cancel.set()

    stopper = asyncio.create_task(_stop_once_asked())
    executor = _GatedExecutor()
    tools = [
        ToolDefinition(
            name=name, description="test", input_schema={"type": "object", "properties": {}}
        )
        for name in ("send_mail", "read_inbox")
    ]

    response = await asyncio.wait_for(
        run_agent_loop(
            llm=_GatedToolLLM(),
            messages=[ChatMessage(role="user", content="mail Bob")],
            tools=tools,
            executor=executor,
            max_iterations=3,
            cancel_event=cancel,
            request_approval=_request_approval,
        ),
        timeout=5,
    )
    await stopper

    assert response.content == _CANCELLED_MESSAGE
    assert response.stop_reason == "cancelled"
    assert withdrawn.is_set(), "the channel's wait must be cancelled, so it cleans up"
    assert executor.ran == []


async def test_an_answer_landing_with_the_cancel_runs_nothing():
    """The cancel and the reviewer's answer can resolve together. The race
    then hands back the answer, so the loop reads the cancel again before it
    applies anything: no approved call or call that passed the gate runs, and
    a "Yes, always" given with the stop is not recorded either.
    """
    import asyncio

    from app.llm.base import ChatMessage
    from app.tools.agent_loop import _CANCELLED_MESSAGE, run_agent_loop
    from app.tools.base import ToolDefinition

    cancel = asyncio.Event()

    remembered: list = []

    async def _answer_as_the_stop_lands(proposals):
        cancel.set()
        return {p["tool_call_id"]: {"decision": "approve_always"} for p in proposals}

    async def _remember(proposal, _decision):
        remembered.append(proposal["tool_name"])

    executor = _GatedExecutor()
    tools = [
        ToolDefinition(
            name=name, description="test", input_schema={"type": "object", "properties": {}}
        )
        for name in ("send_mail", "read_inbox")
    ]

    response = await asyncio.wait_for(
        run_agent_loop(
            llm=_GatedToolLLM(),
            messages=[ChatMessage(role="user", content="mail Bob")],
            tools=tools,
            executor=executor,
            max_iterations=3,
            cancel_event=cancel,
            request_approval=_answer_as_the_stop_lands,
            on_approve_always=_remember,
        ),
        timeout=5,
    )

    assert response.content == _CANCELLED_MESSAGE
    assert response.stop_reason == "cancelled"
    assert executor.ran == []
    assert remembered == []


def test_every_resume_hands_the_turn_cancel_to_the_calls_it_runs():
    """Each caller that runs approved or cleared calls on a resume passes the
    turn's cancel event, so a stop reaches them too."""
    import ast

    from tests import app_sources

    helpers = {"_apply_approval_decisions", "_dispatch_calls_the_pause_left_behind"}
    calls: list[tuple[str, int, bool]] = []
    for path, module in app_sources.modules():
        for node in app_sources.walk(module):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in helpers:
                continue
            passes = any(
                kw.arg == "cancel_event"
                and isinstance(kw.value, ast.Name)
                and kw.value.id == "cancel_event"
                for kw in node.keywords
            )
            calls.append((path, node.lineno, passes))

    assert sorted({path for path, _, _ in calls}) == [
        "services/agent_chat.py",
        "services/assignment_engine.py",
        "services/general_chat.py",
        "tools/agent_loop.py",
    ]
    assert [c for c in calls if not c[2]] == []
