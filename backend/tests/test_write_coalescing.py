"""Write coalescing in one tool round.

Two updates of one calendar event in one round reach the provider ONCE with
the merged patch; the absorbed call is answered by the loop and tagged
``not_run="coalesced"``. Message edits keep the last body. A call naming the
same object in between closes the group; the flag turns it all off. What is
coalescible is declared on each tool (``ToolDefinition.coalesce``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import settings
from app.llm.base import ChatMessage, ChatResponse
from app.tools import agent_loop as al
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
    WriteCoalescing,
)
from app.tools.write_coalescing import coalesce_write_calls, format_coalesced_answer
from tests.test_tool_call_dedup import (
    _quiet_langfuse,  # noqa: F401 - pytest fixture
    _ScriptedLLM,
    _tool_call_response,
    _tool_messages,
)

_UPDATE = "update_event"
_EDIT = "edit_message"
_PATCH = WriteCoalescing(identity=("event_id", "calendar_id"), mode="patch", union_lists=("attachment_file_ids",))
_REPLACE = WriteCoalescing(identity=("message_id", "chat_id"), mode="replace")
_DECLARED = {_UPDATE: _PATCH, _EDIT: _REPLACE}


def _call(cid: str, name: str = _UPDATE, **arguments) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=arguments)


def _coalesce(calls: list[ToolCall]):
    return coalesce_write_calls(calls, coalescing_for=_DECLARED.get)


# ── Unit: the merge ────────────────────────────────────────────────────────


def test_two_updates_of_one_event_become_one_call_with_the_merged_patch():
    first = _call("c1", event_id="E1", body="new agenda", location="")
    second = _call("c2", event_id="E1", location="Room 4", body="")
    out = _coalesce([first, second])
    assert [c.id for c in out.calls] == ["c1"]
    assert out.calls[0].arguments == {"event_id": "E1", "body": "new agenda", "location": "Room 4"}
    assert [(a.id, leader.id) for a, leader in out.absorbed] == [("c2", "c1")]
    # The assistant message keeps what the model wrote.
    assert first.arguments == {"event_id": "E1", "body": "new agenda", "location": ""}


def test_a_later_non_empty_value_wins_and_declared_lists_are_unioned():
    out = _coalesce(
        [
            _call("c1", event_id="E1", subject="A", attachment_file_ids=["f1"]),
            _call("c2", event_id="E1", subject="B", attachment_file_ids=["f1", "f2"]),
            _call("c3", event_id="E1", subject="", attachment_file_ids=["f3"]),
        ]
    )
    (leader,) = out.calls
    assert leader.arguments["subject"] == "B"
    assert leader.arguments["attachment_file_ids"] == ["f1", "f2", "f3"]
    assert [a.id for a, _ in out.absorbed] == ["c2", "c3"]


def test_different_events_or_calendars_are_never_merged():
    calls = [
        _call("c1", event_id="E1", body="x"),
        _call("c2", event_id="E2", body="y"),
        _call("c3", event_id="E1", calendar_id="shared", body="z"),
    ]
    out = _coalesce(calls)
    assert [c.id for c in out.calls] == ["c1", "c2", "c3"]
    assert out.absorbed == []


def test_a_call_naming_the_event_in_between_closes_the_group():
    out = _coalesce(
        [
            _call("c1", event_id="E1", body="x"),
            _call("c2", name="get_event", event_id="E1"),
            _call("c3", event_id="E1", location="Room 4"),
        ]
    )
    assert [c.id for c in out.calls] == ["c1", "c2", "c3"]
    assert out.absorbed == []


def test_an_unrelated_call_in_between_keeps_the_group_open():
    out = _coalesce(
        [
            _call("c1", event_id="E1", body="x"),
            _call("c2", name="search_lookup", q="weather"),
            _call("c3", event_id="E1", location="Room 4"),
        ]
    )
    assert [c.id for c in out.calls] == ["c1", "c2"]
    assert out.calls[0].arguments == {"event_id": "E1", "body": "x", "location": "Room 4"}


def test_a_message_edit_keeps_the_last_body_whole():
    out = _coalesce(
        [
            _call("c1", name=_EDIT, chat_id="C", message_id="M", message="draft", card="{}"),
            _call("c2", name=_EDIT, chat_id="C", message_id="M", message="final"),
        ]
    )
    (leader,) = out.calls
    assert leader.arguments == {"chat_id": "C", "message_id": "M", "message": "final"}


def test_a_call_without_its_object_id_is_never_coalesced():
    out = _coalesce([_call("c1", body="x"), _call("c2", body="y")])
    assert [c.id for c in out.calls] == ["c1", "c2"]
    assert out.absorbed == []


def test_a_tool_that_declares_nothing_is_never_coalesced():
    out = _coalesce([_call("c1", name="post", event_id="E1"), _call("c2", name="post", event_id="E1")])
    assert [c.id for c in out.calls] == ["c1", "c2"]


def test_the_answer_names_the_carrier():
    text = format_coalesced_answer(_call("c1"))
    assert "call c1" in text and "do not repeat" in text


def test_the_calendar_and_teams_update_tools_declare_their_coalescing():
    from app.integrations.providers import google, teams_bot
    from app.integrations.providers.microsoft import calendar, teams_channels, teams_chats

    sources = {
        "update_microsoft_calendar_event": calendar,
        "update_microsoft_channel_message": teams_channels,
        "update_microsoft_chat_message": teams_chats,
        "update_google_event": google,
        "edit_teams_bot_channel_message": teams_bot,
        "edit_teams_bot_group_chat_message": teams_bot,
    }
    for name, module in sources.items():
        text = Path(module.__file__).read_text(encoding="utf-8")
        block = text[text.index(f'name="{name}"') :]
        block = block[: block.index("ToolDefinition(") if "ToolDefinition(" in block else len(block)]
        assert "coalesce=WriteCoalescing(" in block, name


# ── Integration: the loop sends one PATCH ─────────────────────────────────


def _recording_provider() -> tuple[CompositeToolExecutor, list[dict]]:
    """A recorded provider: every executed update is one PATCH (one notification)."""
    patches: list[dict] = []
    callable_exec = CallableToolExecutor()
    definition = ToolDefinition(
        name=_UPDATE,
        description="update an event",
        source="custom:test",
        input_schema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "body": {"type": "string", "default": ""},
                "location": {"type": "string", "default": ""},
            },
            "required": ["event_id"],
        },
        coalesce=_PATCH,
    )

    async def _patch(**kwargs):
        patches.append(dict(kwargs))
        return f"Event {kwargs['event_id']} updated"

    callable_exec.register(_UPDATE, _patch)
    composite = CompositeToolExecutor()
    composite.add_tools([definition], callable_exec)
    return composite, patches


def _two_updates_llm() -> _ScriptedLLM:
    return _ScriptedLLM(
        [
            _tool_call_response(
                _call("c1", event_id="E1", body="new agenda"),
                _call("c2", event_id="E1", location="Room 4"),
            ),
            ChatResponse(content="Done.", model="stub"),
        ]
    )


@pytest.mark.asyncio
async def test_two_updates_in_one_round_send_exactly_one_patch(_quiet_langfuse, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "self_verification_loop", True)
    composite, patches = _recording_provider()
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    response = await al.run_agent_loop(
        llm=_two_updates_llm(),
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        on_event=on_event,
    )
    assert response.content == "Done."
    assert len(patches) == 1
    assert patches[0]["event_id"] == "E1"
    assert patches[0]["body"] == "new agenda" and patches[0]["location"] == "Room 4"
    (absorbed,) = _tool_messages(messages, "c2")
    assert absorbed.not_run == "coalesced"
    assert "call c1" in absorbed.content
    (carrier,) = _tool_messages(messages, "c1")
    assert "Event E1 updated" in carrier.content
    assert [e["coalesced_into"] for e in events if e.get("status") == "tool_done" and e.get("coalesced_into")] == [
        "c1"
    ]


@pytest.mark.asyncio
async def test_the_flag_off_sends_one_patch_per_call(_quiet_langfuse, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "self_verification_loop", False)
    composite, patches = _recording_provider()
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    await al.run_agent_loop(
        llm=_two_updates_llm(), messages=messages, tools=composite.all_tools, executor=composite, max_iterations=5
    )
    assert len(patches) == 2
    assert not [m for m in messages if getattr(m, "not_run", None) == "coalesced"]
