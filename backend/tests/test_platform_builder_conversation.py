"""The conversation an agent chat turn runs in reaches the agent builder.

The workspace tools file "this conversation" by the id the builder is given;
an external caller's builder acts as no account and is given none."""

from __future__ import annotations

import uuid

from app.a2a.partner_scope import partner_task_scope
from app.services.agent_chat import _add_platform_builder_tools
from app.tools.base import CompositeToolExecutor


def _builder_calls(monkeypatch, conversation_id: str | None) -> list[dict]:
    from app.services import builtin_tools

    calls: list[dict] = []
    real_builder = builtin_tools.build_agent_builder_tool

    def _spy(db, user_id, **kwargs):
        calls.append({"user_id": user_id, **kwargs})
        return real_builder(db, user_id, **kwargs)

    monkeypatch.setattr("app.services.builtin_tools.build_agent_builder_tool", _spy)
    _add_platform_builder_tools(
        CompositeToolExecutor(),
        None,
        str(uuid.uuid4()),
        ["agent_builder"],
        conversation_id=conversation_id,
    )
    return calls


def test_the_turn_s_conversation_reaches_the_builder(monkeypatch):
    conversation_id = str(uuid.uuid4())

    calls = _builder_calls(monkeypatch, conversation_id)

    assert len(calls) == 1
    assert calls[0]["conversation_id"] == conversation_id


def test_an_external_caller_s_builder_gets_no_conversation(monkeypatch):
    with partner_task_scope():
        calls = _builder_calls(monkeypatch, str(uuid.uuid4()))

    assert calls == [{"user_id": "anonymous"}]
