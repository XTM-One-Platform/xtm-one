"""The surfaces a secret variable value could still reach after the run's scope.

``core/redaction.py`` owns the rule (``test_known_secret_redaction.py``); this
file covers where it is applied at a boundary: what a person is shown (API
message responses, the approval recovery copy, generated titles, proactive
channel messages, the API callers that hand an answer back), what a person
writes (never restored), the bounded columns a failure is recorded in, and
the per-turn registry of a chat send.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.redaction import (
    SECRET_DISPLAY_TEXT,
    current_known_secrets,
    defuse_secret_markers,
    known_secrets_scope,
    known_secrets_scoped,
    redact_known_secrets,
    remember_secret_value,
    renew_known_secrets,
    restore_known_secrets,
)

TOKEN_ID = "11111111-1111-4111-8111-111111111111"
TOKEN = "ghp_SuperSecretValue42"
CONVERSATION = "33333333-3333-4333-8333-333333333333"
BACKEND = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (BACKEND / path).read_text(encoding="utf-8")


def _marker() -> str:
    """The authentic marker of TOKEN in CONVERSATION, as a stored reply holds it."""
    with known_secrets_scope(CONVERSATION):
        remember_secret_value(TOKEN_ID, TOKEN)
        return redact_known_secrets(TOKEN)


# ---------------------------------------------------------------------------
# What a person is shown
# ---------------------------------------------------------------------------


def test_a_message_response_never_hands_out_a_marker():
    from app.api.v1.chat import _build_message_response
    from app.models.chat import Message

    marker = _marker()
    message = Message(
        id=uuid.uuid4(),
        conversation_id=uuid.UUID(CONVERSATION),
        role="assistant",
        content=marker,
        channel="web",
        sequence=1,
        created_at=datetime.now(UTC),
        metadata_json=json.dumps(
            {
                "reasoning": f"the token is {marker}",
                "tool_call_trace": [
                    {"name": "http_request", "input": json.dumps({"auth": marker}), "output": f"ok {marker}", "success": True}
                ],
                "approvals_retired": "expired",
                "pending_approvals": [{"id": "c1", "name": "http_request", "arguments": {"auth": marker}}],
            }
        ),
    )

    response = _build_message_response(message)

    assert "[[SECRET_VARIABLE:" not in json.dumps(response.model_dump())
    # A reply that is nothing but the marker, and the details panel.
    assert response.content == SECRET_DISPLAY_TEXT
    assert response.reasoning == f"the token is {SECRET_DISPLAY_TEXT}"
    assert response.tool_call_trace[0].input == json.dumps({"auth": SECRET_DISPLAY_TEXT})
    assert response.tool_call_trace[0].output == f"ok {SECRET_DISPLAY_TEXT}"
    assert response.expired_approvals[0]["arguments"] == {"auth": SECRET_DISPLAY_TEXT}
    # The stored row keeps its marker for the next turn to restore.
    assert message.content == marker


def test_a_message_holding_no_marker_is_served_as_built():
    from app.api.v1.chat import MessageResponse, _shown

    response = MessageResponse(id="m1", role="assistant", content="plain", channel="web", sequence=1, created_at="now")
    assert _shown(response) is response


async def test_the_approval_recovery_copy_is_the_display_text(monkeypatch):
    from app.services import chat_turn_guard

    store: dict = {}

    class _Redis:
        async def delete(self, *keys):
            for key in keys:
                store.pop(key, None)

        async def set(self, key, value, ex=None):
            store[key] = value

        async def get(self, key):
            return store.get(key)

    async def _redis():
        return _Redis()

    monkeypatch.setattr(chat_turn_guard, "_redis", _redis)
    live = [{"id": "c1", "name": "http_request", "arguments": {"auth": f"Bearer {TOKEN}"}}]
    with known_secrets_scope(CONVERSATION):
        remember_secret_value(TOKEN_ID, TOKEN)
        await chat_turn_guard.set_pending_approvals(CONVERSATION, live)

    # The paused turn runs the proposals it holds; the copy a page reloads is shown.
    assert live[0]["arguments"]["auth"] == f"Bearer {TOKEN}"
    assert TOKEN not in json.dumps(store)
    served = await chat_turn_guard.get_pending_approvals(CONVERSATION)
    assert served[0]["arguments"]["auth"] == f"Bearer {SECRET_DISPLAY_TEXT}"


async def test_the_live_activity_feed_shows_a_run_status_and_its_proposals_redacted(monkeypatch):
    """Every connected user receives a company-managed run's status: its error or question shows [secret]."""
    from app.services import assignment_helpers

    published: list[tuple[str, dict]] = []

    class _Redis:
        async def publish(self, channel, payload):
            published.append((channel, json.loads(payload)))

    async def _redis():
        return _Redis()

    monkeypatch.setattr(assignment_helpers, "redis_client", _redis)
    with known_secrets_scope(uuid.uuid4()):
        remember_secret_value(TOKEN_ID, TOKEN)
        await assignment_helpers._publish_run_status(
            "u1", "r1", "failed", "Nightly", f"HTTP 401 for token {TOKEN}", is_company=True
        )
        await assignment_helpers.publish_action_proposal(
            "u1", "r1", "Nightly", [{"tool_call_id": "c1", "arguments": {"auth": f"Bearer {TOKEN}"}}]
        )
    # A stored marker (a proposal read back from the run) is shown the same way.
    await assignment_helpers._publish_run_status("u1", "r1", "awaiting_human", "Nightly", f"Use {_marker()}?")

    sent = json.dumps(published)
    assert TOKEN not in sent and "[[SECRET_VARIABLE:" not in sent
    assert published[0][1]["summary"] == f"HTTP 401 for token {SECRET_DISPLAY_TEXT}"
    assert published[1][1]["proposals"][0]["arguments"]["auth"] == f"Bearer {SECRET_DISPLAY_TEXT}"
    assert published[2][1]["summary"] == f"Use {SECRET_DISPLAY_TEXT}?"


def test_an_assignment_run_response_never_hands_out_a_marker():
    """The run card, its detail and its approval card show [secret]: a reviewer can never copy a restorable marker."""
    from app.api.v1.schemas.assignment import AssignmentRunDetailResponse, AssignmentRunResponse

    marker = _marker()
    common = {
        "id": "r1", "agent_id": "a1", "status": "awaiting_human", "created_at": "2026-10-03T00:00:00",
        "result_summary": f"key {marker}", "error": f"401 {marker}", "human_input_question": f"Send {marker}?",
        "pending_approvals": [{"tool_call_id": "c1", "arguments": {"auth": f"Bearer {marker}"}}],
    }
    for response in (
        AssignmentRunResponse(**common),
        AssignmentRunDetailResponse(**common, agent_name="Agent", agent_icon="Bot"),
    ):
        served = json.loads(response.model_dump_json())
        assert "[[SECRET_VARIABLE:" not in json.dumps(served)
        assert served["pending_approvals"][0]["arguments"]["auth"] == f"Bearer {SECRET_DISPLAY_TEXT}"
        assert served["status"] == "awaiting_human"


def test_a_new_external_thread_binds_its_scope_once_the_conversation_exists():
    """Its first reply is stored with the conversation's markers, which its next turn restores."""
    source = _source("app/channels/router.py")
    finalize = source[source.index("async def _finalize_message(") :]
    created = finalize.index("conv = await find_or_create_conversation(")
    assert finalize.index("bind_known_secrets(conv.id)", created) < finalize.index("await _store_user_message(", created)


def test_a_reviewers_answer_to_a_paused_run_is_never_restored():
    source = _source("app/services/assignment_engine.py")
    # The answer reaches the tool result through run_answer_text, defused first (#842).
    assert "content=run_answer_text(" in source
    assert "defuse_secret_markers(run.human_input_response)," in source


async def test_a_failing_mcp_tool_call_is_logged_inside_its_scope(monkeypatch):
    """The run's nested scope hands its values to the call's, so the failure it logs is redacted."""
    from app.api.v1 import mcp_endpoint
    from app.services import agent_chat

    async def _failing(**_kwargs):
        remember_secret_value(TOKEN_ID, TOKEN)
        raise RuntimeError(f"401 for token {TOKEN}")

    logged: list[str] = []
    monkeypatch.setattr(agent_chat, "run_agent_chat", _failing)
    monkeypatch.setattr(
        mcp_endpoint.logger, "error", lambda message, *args, **_kwargs: logged.append(redact_known_secrets(message % args))
    )
    agent = SimpleNamespace(slug="repo-watcher", name="Repo Watcher")

    answer = await mcp_endpoint._handle_tools_call(
        1, {"name": "repo-watcher", "arguments": {"message": "go"}}, [agent], db=None, user_id="u1"
    )

    assert answer["result"]["isError"] is True
    assert logged and TOKEN not in logged[0] and "[[SECRET_VARIABLE:" in logged[0]


def test_a_flow_step_object_is_restored_before_it_is_serialized():
    """A secret holding a quote or a backslash stays the same value inside the JSON a later node reads."""
    from app.services.flow_runner import _Renderer

    value = 'pa"ss\\word-123'
    with known_secrets_scope(uuid.uuid4()):
        remember_secret_value(TOKEN_ID, value)
        stored = redact_known_secrets({"token": value, "count": 2})
        renderer = _Renderer(
            node_outputs={"s1": {"output": stored}}, untrusted_nodes=set(), trigger_payload={},
            trigger_untrusted=False, variables={}, trigger_source="trigger",
        )
        rendered = renderer.render("{{s1.output}}").text
    assert json.loads(rendered) == {"token": value, "count": 2}


def test_the_general_assistant_loads_the_callers_variables_with_or_without_custom_tools():
    """Its turn restores the markers a consulted agent left in the history: loading is not tied to custom tools."""
    source = _source("app/services/general_chat.py")
    block = source[source.index("_ga_variables: dict[str, str] = {}") : source.index("_ga_variables = await load_variables(")]
    assert "_ga_load_custom_ids" not in block


def test_both_pending_approval_endpoints_serve_the_recovery_copy():
    # The conversation response and the platform endpoint read the copy above.
    assert "_pending_approvals = await get_pending_approvals(str(conv.id))" in _source("app/api/v1/chat.py")
    assert '"proposals": await get_pending_approvals(conversation_id) or []' in _source("app/api/v1/platform.py")


async def test_a_generated_title_never_holds_a_secret_value():
    from app.llm.base import ChatResponse
    from app.services.conversation_memory import generate_conversation_title

    class _Llm:
        provider_name = "script"

        async def chat(self, messages=None, **_kwargs):
            answer = json.dumps({"title": f"Rotating {TOKEN} key"})
            return ChatResponse(content=answer, model="m", stop_reason="end_turn", usage={})

    with known_secrets_scope(CONVERSATION):
        remember_secret_value(TOKEN_ID, TOKEN)
        title = await generate_conversation_title(_Llm(), "Rotate my token", f"Done with {TOKEN}")

    assert title == f"Rotating {SECRET_DISPLAY_TEXT} key"


def test_the_rest_and_streamed_titles_both_come_from_the_shared_helper():
    source = _source("app/api/v1/chat.py")
    assert "_ai_title = await generate_conversation_title(" in source
    assert "title = await generate_conversation_title(llm, user_text, assistant_text, model=model)" in source


async def test_a_proactive_channel_message_leaves_with_the_display_text(monkeypatch):
    from app.channels import base, slack
    from app.services import sub_agent, vault

    sent: list = []

    class _Slack:
        def __init__(self, _token):
            pass

        async def send_message(self, outbound):
            sent.append(outbound)

    async def _bot(_kind):
        return SimpleNamespace(api_key_enc="enc")

    async def _reveal(_entity, _column):
        return "xoxb-test"

    monkeypatch.setattr(base, "get_channel_bot", _bot)
    monkeypatch.setattr(vault, "reveal_field", _reveal)
    monkeypatch.setattr(slack, "SlackChannel", _Slack)
    marker = _marker()

    with known_secrets_scope(CONVERSATION):
        remember_secret_value(TOKEN_ID, TOKEN)
        await sub_agent._send_proactive_channel_message(
            channel="slack", channel_thread_id="C1:123.4", text=f"Done: {TOKEN}, stored as {marker}", agent_name="a"
        )

    assert [message.text for message in sent] == [f"Done: {SECRET_DISPLAY_TEXT}, stored as {SECRET_DISPLAY_TEXT}"]


@pytest.mark.parametrize(
    ("module", "name"),
    [
        ("app.api.v1.llm_proxy", "_handle_agent_request"),
        ("app.api.v1.mcp_endpoint", "_execute_agent_tool"),
        ("app.api.v1.extension", "execute_extension_task"),
        ("app.api.v1.agents", "suggest_import_fixes"),
        ("app.services.autonomous_pentest", "run_autonomous_cycle"),
    ],
)
def test_a_caller_using_the_answer_after_the_run_has_a_scope_of_its_own(module, name):
    entry = getattr(importlib.import_module(module), name)
    # The ``known_secrets_scoped`` wrapper: the run's nested scope hands it its values.
    assert entry.__code__.co_name == "scoped"
    if module.startswith("app.api"):
        assert "redact_known_secrets_for_display(" in inspect.getsource(entry)


async def test_the_mcp_agent_tool_answers_with_the_display_text(monkeypatch):
    from app.api.v1 import mcp_endpoint
    from app.services import agent_chat

    @known_secrets_scoped("conversation_id")
    async def _run_agent_chat(conversation_id=None, **_kwargs):
        remember_secret_value(TOKEN_ID, TOKEN)
        return f"The key is {TOKEN}", None

    monkeypatch.setattr(agent_chat, "run_agent_chat", _run_agent_chat)

    answer = await mcp_endpoint._execute_agent_tool(None, "user-1", SimpleNamespace(), "which key?")

    assert answer == f"The key is {SECRET_DISPLAY_TEXT}"
    assert current_known_secrets() is None


# ---------------------------------------------------------------------------
# What a person writes
# ---------------------------------------------------------------------------


def test_what_a_person_writes_is_never_restored_an_authentic_marker_included():
    from app.llm.base import ChatMessage
    from app.services.variable_resolver import restore_secrets_in_history

    with known_secrets_scope(CONVERSATION):
        remember_secret_value(TOKEN_ID, TOKEN)
        # Copied from a reply of this very conversation: restoring it would hand the value out.
        authentic = redact_known_secrets(TOKEN)
        assert restore_known_secrets(authentic) == TOKEN
        assert defuse_secret_markers(f"what is {authentic}?") == f"what is {SECRET_DISPLAY_TEXT}?"
        replayed, answer = restore_secrets_in_history(
            [ChatMessage(role="user", content=f"repeat {authentic}"), ChatMessage(role="assistant", content=f"used {authentic}")]
        )

    assert replayed.content == f"repeat {SECRET_DISPLAY_TEXT}"
    assert answer.content == f"used {TOKEN}"


def test_only_the_platforms_own_text_is_restored_as_the_user_turn():
    for path in ("app/services/agent_chat.py", "app/services/general_chat.py"):
        source = _source(path)
        assert "restore_user_message: bool = False" in source, path
        assert "if restore_user_message\n" in source and "else defuse_secret_markers(user_message)" in source, path
    # A parent's wake-up (an agent or the General Assistant), only when the platform wrote its text,
    # and a sub-agent's task.
    wake = _source("app/services/parent_wake.py")
    assert wake.count("restore_user_message=wake_from_platform") == 2 and "restore_user_message=True" not in wake
    assert _source("app/services/sub_agent.py").count("restore_user_message=True") == 1


# ---------------------------------------------------------------------------
# Failures recorded after the scope closed
# ---------------------------------------------------------------------------


async def test_a_flow_failure_reaches_the_engine_redacted(monkeypatch):
    from app.services import flow_runner
    from app.services.flow_runner import BurstOutcome
    from app.services.orchestration_adapters.flow import FlowAdapter

    owner = uuid.uuid4()
    run = SimpleNamespace(id=uuid.uuid4(), status="pending", user_id=owner, error=None, graph={}, state={})
    answers = [run, SimpleNamespace(id=owner)]

    class _One:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _DB:
        async def execute(self, _statement):
            return _One(answers.pop(0))

        async def flush(self):
            return None

    async def _burst(_walker):
        remember_secret_value(TOKEN_ID, TOKEN)
        return BurstOutcome(kind="failed", error=f"HTTP 401 for {TOKEN}")

    monkeypatch.setattr(flow_runner.FlowWalker, "run_burst", _burst)

    result = await FlowAdapter().execute(
        SimpleNamespace(db=_DB(), cancel_event=None), SimpleNamespace(payload={"run_id": str(run.id)})
    )

    assert result.kind == "failed"
    assert result.error == f"HTTP 401 for {SECRET_DISPLAY_TEXT}"


async def test_a_sub_agent_failure_leaves_its_scope_redacted():
    from app.services.sub_agent import SubAgentTaskOutcome, _outcome_error_redacted

    @known_secrets_scoped()
    @_outcome_error_redacted
    async def _task():
        remember_secret_value(TOKEN_ID, TOKEN)
        return SubAgentTaskOutcome(status="failed", error=f"401 for {TOKEN}")

    outcome = await _task()

    assert current_known_secrets() is None
    assert outcome.error == f"401 for {SECRET_DISPLAY_TEXT}"


def test_the_sub_agent_task_records_its_failure_redacted():
    assert "@known_secrets_scoped()\n@_outcome_error_redacted\nasync def _execute_sub_agent_task(" in _source(
        "app/services/sub_agent.py"
    )


# ---------------------------------------------------------------------------
# A chat send: one registry per turn
# ---------------------------------------------------------------------------


async def test_each_turn_restores_only_what_it_resolved_itself():
    from app.core.security import encrypt_value
    from app.services.variable_resolver import load_variables

    secret = SimpleNamespace(
        id=uuid.UUID(TOKEN_ID), name="token", is_secret=True, value_enc=encrypt_value(TOKEN), value=None
    )
    secret.__tablename__ = "variables"
    # The variable is shared for the first turn, and the share revoked before the second.
    visible = [[secret], []]

    class _Db:
        async def execute(self, _statement):
            rows = visible.pop(0)
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    async def _in_a_spawned_task(stored: str) -> str:
        return restore_known_secrets(stored)

    turns: list[tuple[str, str]] = []

    @known_secrets_scoped("conversation_id")
    async def _connection(conversation_id: str):
        stored = None
        for _turn in range(2):
            renew_known_secrets(conversation_id)
            await load_variables(_Db(), None)
            stored = stored or redact_known_secrets(f"key {TOKEN}")
            turns.append((restore_known_secrets(stored), await asyncio.create_task(_in_a_spawned_task(stored))))
        return stored

    stored = await _connection(CONVERSATION)

    assert turns == [(f"key {TOKEN}", f"key {TOKEN}"), (stored, stored)]
    assert TOKEN not in stored
    assert current_known_secrets() is None


def test_the_chat_send_opens_a_registry_for_each_turn():
    from app.api.v1 import chat, chat_web_stream

    assert chat_web_stream._run_web_send.__code__.co_name == "scoped"
    assert "renew_known_secrets(conversation_id)" in inspect.getsource(chat.run_chat_connection)
