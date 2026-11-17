"""Duplicate-send guard in the agentic loop.

A send whose recipients and body match a successful send of the same run, or
one the same agent made in the last ten minutes, is refused with the earlier
call's id and tagged ``not_run="duplicate_send"``; ``allow_duplicate: true``
lets it through without the flag reaching the tool; another body or other
recipients are never refused; a failed send is not remembered; the flag
turns it all off. What is a send is declared on each tool
(``ToolDefinition.send_guard``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import settings
from app.llm.base import ChatMessage, ChatResponse
from app.tools import agent_loop as al
from app.tools import send_guard
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    SendGuard,
    ToolCall,
    ToolDefinition,
)
from tests.test_tool_call_dedup import (
    _quiet_langfuse,  # noqa: F401 - pytest fixture
    _ScriptedLLM,
    _tool_call_response,
    _tool_messages,
)

_SEND = "post_message"
_SPEC = SendGuard(recipients=("chat_id", "to"), body=("message",))


def _call(cid: str, **arguments) -> ToolCall:
    return ToolCall(id=cid, name=_SEND, arguments=arguments)


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch):
    audits: list[tuple[str, str]] = []

    async def _audit(action, *, tool, fp, call_id, earlier):
        audits.append((action, call_id))

    monkeypatch.setattr(send_guard, "audit", _audit)
    return audits


@pytest.fixture
def _redis(monkeypatch):
    store: dict[str, str] = {}

    class _Fake:
        async def get(self, key):
            return store.get(key)

        async def set(self, key, value, ex=None):
            store[key] = value

    async def _client():
        return _Fake()

    import app.core.redis as redis_mod

    monkeypatch.setattr(redis_mod, "redis_client", _client)
    return store


# ── Unit: the fingerprint ─────────────────────────────────────────────────


def test_recipients_are_a_set_case_and_order_free():
    a = send_guard.fingerprint(_call("a", to="Bob@x.io, ann@x.io", message="hi"), _SPEC)
    b = send_guard.fingerprint(_call("b", to=["ann@x.io", "bob@X.io"], message="hi"), _SPEC)
    assert a is not None and b is not None and a.key == b.key


def test_another_body_or_recipient_is_another_send():
    base = send_guard.fingerprint(_call("a", chat_id="C1", message="hi"), _SPEC)
    assert base.key != send_guard.fingerprint(_call("b", chat_id="C1", message="hi."), _SPEC).key
    assert base.key != send_guard.fingerprint(_call("c", chat_id="C2", message="hi"), _SPEC).key


def test_no_recipient_or_no_body_is_never_guarded():
    assert send_guard.fingerprint(_call("a", message="hi"), _SPEC) is None
    assert send_guard.fingerprint(_call("b", chat_id="C1"), _SPEC) is None


def test_allow_duplicate_is_read_and_stripped():
    call = _call("a", chat_id="C1", message="hi", allow_duplicate=True)
    assert send_guard.wants_duplicate(call)
    assert "allow_duplicate" not in send_guard.strip_flag(call).arguments
    assert not send_guard.wants_duplicate(_call("b", chat_id="C1", message="hi"))


@pytest.mark.asyncio
async def test_the_agent_window_spans_runs(_redis):
    token = send_guard.bind_sender("agent-1")
    try:
        fp = send_guard.fingerprint(_call("a", chat_id="C1", message="hi"), _SPEC)
        await send_guard.SendLedger().record(fp, "a")
        earlier = await send_guard.SendLedger().earlier(fp)
        assert earlier is not None and earlier.call_id == "a" and not earlier.same_run
        (key,) = _redis
        assert key.startswith("send_guard:agent-1:")
        assert json.loads(_redis[key])["call_id"] == "a"
    finally:
        send_guard.reset_sender(token)
    other = send_guard.bind_sender("agent-2")
    try:
        assert await send_guard.SendLedger().earlier(fp) is None
    finally:
        send_guard.reset_sender(other)


@pytest.mark.asyncio
async def test_an_unreachable_store_fails_open(monkeypatch):
    async def _boom():
        raise ConnectionError("down")

    import app.core.redis as redis_mod

    monkeypatch.setattr(redis_mod, "redis_client", _boom)
    token = send_guard.bind_sender("agent-1")
    try:
        fp = send_guard.fingerprint(_call("a", chat_id="C1", message="hi"), _SPEC)
        assert await send_guard.SendLedger().earlier(fp) is None
    finally:
        send_guard.reset_sender(token)


def test_every_declared_send_tool_carries_a_guard():
    from app.integrations.providers import google, teams_bot
    from app.integrations.providers.microsoft import mail, teams_channels, teams_chats

    sources = {
        "send_gmail": google,
        "send_microsoft_email": mail,
        "send_microsoft_channel_message": teams_channels,
        "send_microsoft_chat_message": teams_chats,
        "send_teams_bot_channel_message": teams_bot,
        "send_teams_bot_group_chat_message": teams_bot,
    }
    for name, module in sources.items():
        text = Path(module.__file__).read_text(encoding="utf-8")
        block = text[text.index(f'name="{name}"') :]
        block = block[: block.index("ToolDefinition(") if "ToolDefinition(" in block else len(block)]
        assert "send_guard=SendGuard(" in block, name


# ── Integration: the loop ─────────────────────────────────────────────────


def _recording_sender(*, fail: bool = False) -> tuple[CompositeToolExecutor, list[dict]]:
    sent: list[dict] = []
    callable_exec = CallableToolExecutor()
    definition = ToolDefinition(
        name=_SEND,
        description="post a message",
        source="custom:test",
        input_schema={
            "type": "object",
            "properties": {"chat_id": {"type": "string"}, "message": {"type": "string"}},
            "required": ["chat_id", "message"],
        },
        send_guard=_SPEC,
    )

    async def _post(**kwargs):
        sent.append(dict(kwargs))
        if fail:
            raise RuntimeError("provider down")
        return f"Posted to {kwargs['chat_id']}"

    callable_exec.register(_SEND, _post)
    composite = CompositeToolExecutor()
    composite.add_tools([definition], callable_exec)
    return composite, sent


async def _run(composite, rounds):
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    llm = _ScriptedLLM([*[_tool_call_response(*r) for r in rounds], ChatResponse(content="Done.", model="stub")])
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=6
    )
    return messages


@pytest.mark.asyncio
async def test_the_same_message_twice_in_a_run_is_refused(_quiet_langfuse, monkeypatch, _no_audit):  # noqa: F811
    monkeypatch.setattr(settings, "self_verification_loop", True)
    composite, sent = _recording_sender()
    messages = await _run(
        composite,
        [[_call("c1", chat_id="C1", message="hello")], [_call("c2", chat_id="C1", message="hello")]],
    )
    assert len(sent) == 1
    (refused,) = _tool_messages(messages, "c2")
    assert refused.not_run == "duplicate_send"
    assert "call c1" in refused.content and "allow_duplicate" in refused.content
    assert _no_audit == [("duplicate_send_refused", "c2")]


@pytest.mark.asyncio
async def test_two_identical_sends_in_one_round_send_once(_quiet_langfuse, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "self_verification_loop", True)
    composite, sent = _recording_sender()
    messages = await _run(
        composite, [[_call("c1", chat_id="C1", message="hello"), _call("c2", chat_id="C1", message="hello")]]
    )
    assert len(sent) == 1
    (refused,) = _tool_messages(messages, "c2")
    assert refused.not_run == "duplicate_send"


@pytest.mark.asyncio
async def test_allow_duplicate_sends_again_without_the_flag(_quiet_langfuse, monkeypatch, _no_audit):  # noqa: F811
    monkeypatch.setattr(settings, "self_verification_loop", True)
    composite, sent = _recording_sender()
    await _run(
        composite,
        [
            [_call("c1", chat_id="C1", message="hello")],
            [_call("c2", chat_id="C1", message="hello", allow_duplicate=True)],
        ],
    )
    assert len(sent) == 2
    assert "allow_duplicate" not in sent[1]
    assert _no_audit == [("duplicate_send_allowed", "c2")]


@pytest.mark.asyncio
async def test_another_body_goes_through(_quiet_langfuse, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "self_verification_loop", True)
    composite, sent = _recording_sender()
    await _run(
        composite,
        [[_call("c1", chat_id="C1", message="hello")], [_call("c2", chat_id="C1", message="hello again")]],
    )
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_a_failed_send_is_not_remembered(_quiet_langfuse, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "self_verification_loop", True)
    composite, sent = _recording_sender(fail=True)
    messages = await _run(
        composite,
        [[_call("c1", chat_id="C1", message="hello")], [_call("c2", chat_id="C1", message="hello")]],
    )
    assert len(sent) == 2
    assert not [m for m in messages if getattr(m, "not_run", None) == "duplicate_send"]


@pytest.mark.asyncio
async def test_the_flag_off_sends_every_call(_quiet_langfuse, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "self_verification_loop", False)
    composite, sent = _recording_sender()
    await _run(
        composite,
        [[_call("c1", chat_id="C1", message="hello")], [_call("c2", chat_id="C1", message="hello")]],
    )
    assert len(sent) == 2
