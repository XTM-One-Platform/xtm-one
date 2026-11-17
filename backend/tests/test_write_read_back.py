"""Post-write read-back with a consistency block.

After a write tool that declares ``ToolDefinition.read_back`` succeeds, the
loop appends the object as the system now shows it and the anomalies of the
run: the same object written N times, N sends to the same recipients within
two minutes, a message over 90% identical to an earlier one, an invitation
overlapping another event of the organizer. Read tools are never touched; a
slow or failing read leaves the write's result as it was; the flag turns it
off.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app.core.config import settings
from app.llm.base import ChatMessage, ChatResponse
from app.tools import agent_loop as al
from app.tools import read_back
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
    ToolResult,
    WriteReadBack,
)
from tests.test_tool_call_dedup import (
    _quiet_langfuse,  # noqa: F401 - pytest fixture
    _ScriptedLLM,
    _tool_call_response,
    _tool_messages,
)

_EVENT_SPEC = WriteReadBack(
    kind="event",
    identity=("event_id",),
    read_tool="get_event",
    fields=("Subject", "Start", "End", "Location", "Attendees"),
    body_label="Description",
    overlap_tool="list_events",
)
_MSG_SPEC = WriteReadBack(
    kind="chat_message",
    id_pattern=r"Message ID: ([^\s.]+)",
    recipients=("chat_id",),
    body=("message",),
    shown_arguments=("message",),
)

_EVENT_TEXT = (
    "Subject: Board\n"
    "ID: E1\n"
    "Start: 2026-11-03T10:00:00 (UTC)\n"
    "End: 2026-11-03T11:00:00 (UTC)\n"
    "Location: Room 2\n"
    "Organizer: Sam <sam@x.io>\n"
    "Attendees (1):\n"
    "  ann@x.io (none)\n"
    "\nDescription:\nAgenda v2\n"
)


def _reader(*, events=None, delay: float = 0.0, fail: bool = False):
    calls: list[tuple[str, dict]] = []

    async def _execute(name, arguments):
        calls.append((name, dict(arguments)))
        if delay:
            await asyncio.sleep(delay)
        if fail:
            return ToolResult(tool_call_id="", content="Error: 404", is_error=True)
        if name == "get_event":
            return ToolResult(tool_call_id="", content=_EVENT_TEXT)
        if name == "list_events":
            return ToolResult(tool_call_id="", content="...", structured=events or [])
        return None

    return _execute, calls


def _ok(content: str = "Event updated.") -> ToolResult:
    return ToolResult(tool_call_id="", content=content)


# ── Unit: the read-back block ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_event_write_is_read_back_with_a_body_hash():
    execute, calls = _reader()
    block = await read_back.build(
        _EVENT_SPEC, ToolCall(id="c1", name="update_event", arguments={"event_id": "E1"}), _ok(),
        read_back.ReadBackLedger(), execute,
    )
    assert calls[0] == ("get_event", {"event_id": "E1"})
    assert "read_back:" in block and "id: E1" in block
    assert "Start: 2026-11-03T10:00:00 (UTC)" in block and "Location: Room 2" in block
    assert "ann@x.io (none)" in block
    assert f"body_sha256: {read_back.body_hash('Agenda v2')}" in block
    assert "Agenda v2" not in block  # the body is hashed, never shown
    assert block.rstrip().endswith("- none")


@pytest.mark.asyncio
async def test_a_created_object_takes_its_id_from_the_result():
    execute, calls = _reader()
    spec = WriteReadBack(kind="event", identity=("event_id",), id_pattern=r"(?m)^ID: (\S+)", read_tool="get_event")
    await read_back.build(
        spec, ToolCall(id="c1", name="create_event", arguments={"subject": "x"}),
        _ok("Calendar event created: 'x'\nID: E9\n"), read_back.ReadBackLedger(), execute,
    )
    assert calls[0] == ("get_event", {"event_id": "E9"})


@pytest.mark.asyncio
async def test_a_slow_read_is_skipped_within_the_budget():
    execute, _ = _reader(delay=1.0)
    started = time.monotonic()
    block = await read_back.build(
        _EVENT_SPEC, ToolCall(id="c1", name="update_event", arguments={"event_id": "E1"}), _ok(),
        read_back.ReadBackLedger(), execute, timeout=0.05,
    )
    assert time.monotonic() - started < 0.5
    assert "was skipped" in block


@pytest.mark.asyncio
async def test_a_failed_read_fails_open():
    execute, _ = _reader(fail=True)
    block = await read_back.build(
        _EVENT_SPEC, ToolCall(id="c1", name="update_event", arguments={"event_id": "E1"}), _ok(),
        read_back.ReadBackLedger(), execute,
    )
    assert "the read-back failed" in block and "consistency:" in block


# ── Unit: one test per anomaly kind ───────────────────────────────────────


def test_same_object_written_twice():
    ledger = read_back.ReadBackLedger()
    assert ledger.note(read_back.WriteRecord("event", "E1")) == []
    (anomaly,) = ledger.note(read_back.WriteRecord("event", "E1"))
    assert anomaly.startswith("same_object:") and "written 2 times" in anomaly
    assert ledger.note(read_back.WriteRecord("event", "E2")) == []


def test_burst_send_to_the_same_recipients_within_two_minutes():
    ledger = read_back.ReadBackLedger()
    who = frozenset({"c1"})
    ledger.note(read_back.WriteRecord("email", "", who, "one", at=1000.0))
    (anomaly,) = ledger.note(read_back.WriteRecord("email", "", who, "two entirely different", at=1060.0))
    assert anomaly.startswith("burst_send: 2 sends to c1")
    later = read_back.ReadBackLedger()
    later.note(read_back.WriteRecord("email", "", who, "one", at=1000.0))
    assert later.note(read_back.WriteRecord("email", "", who, "two entirely different", at=1200.0)) == []


def test_near_duplicate_message():
    ledger = read_back.ReadBackLedger()
    ledger.note(read_back.WriteRecord("chat_message", "m1", frozenset({"a"}), "The board meets at 10:00 in room 2."))
    anomalies = ledger.note(
        read_back.WriteRecord("chat_message", "m2", frozenset({"b"}), "The board meets at 10:00 in room 2!")
    )
    assert any(a.startswith("near_duplicate:") and "m1" in a for a in anomalies)
    assert not [
        a
        for a in ledger.note(read_back.WriteRecord("chat_message", "m3", frozenset({"c"}), "Lunch is served."))
        if a.startswith("near_duplicate")
    ]


@pytest.mark.asyncio
async def test_invite_overlapping_another_event_of_the_organizer():
    events = [
        {"id": "E1", "subject": "Board", "start": {"dateTime": "2026-11-03T10:00:00"}, "end": {"dateTime": "2026-11-03T11:00:00"}},
        {"id": "E2", "subject": "1:1 Ann", "start": {"dateTime": "2026-11-03T10:30:00"}, "end": {"dateTime": "2026-11-03T11:30:00"}},
        {"id": "E3", "subject": "Free slot", "showAs": "free", "start": {"dateTime": "2026-11-03T10:00:00"}, "end": {"dateTime": "2026-11-03T11:00:00"}},
        {"id": "E4", "subject": "After", "start": {"dateTime": "2026-11-03T11:00:00"}, "end": {"dateTime": "2026-11-03T12:00:00"}},
    ]
    execute, calls = _reader(events=events)
    block = await read_back.build(
        _EVENT_SPEC, ToolCall(id="c1", name="update_event", arguments={"event_id": "E1"}), _ok(),
        read_back.ReadBackLedger(), execute,
    )
    assert calls[1][0] == "list_events"
    assert "overlap: the invitation overlaps 1:1 Ann" in block
    assert "Free slot" not in block and "After" not in block


# ── Declarations ──────────────────────────────────────────────────────────


def test_every_declared_write_tool_carries_a_read_back():
    from app.integrations.providers import google
    from app.integrations.providers.microsoft import calendar, mail, teams_channels, teams_chats
    from app.services.builtin_tools import presentation, work_items

    sources = {
        "create_microsoft_calendar_event": calendar,
        "update_microsoft_calendar_event": calendar,
        "create_google_event": google,
        "update_google_event": google,
        "send_gmail": google,
        "send_microsoft_email": mail,
        "send_microsoft_channel_message": teams_channels,
        "send_microsoft_chat_message": teams_chats,
        "update_work_item": work_items,
        "edit_presentation": presentation,
    }
    for name, module in sources.items():
        text = Path(module.__file__).read_text(encoding="utf-8")
        block = text[text.index(f'name="{name}"') :]
        block = block[: block.index("ToolDefinition(") if "ToolDefinition(" in block else len(block)]
        assert "read_back=WriteReadBack(" in block, name


def test_the_generic_module_names_no_tool():
    text = Path(read_back.__file__).read_text(encoding="utf-8")
    for name in ("microsoft", "google", "teams", "gmail", "outlook"):
        assert name not in text.lower(), name


# ── Integration: the loop ─────────────────────────────────────────────────


def _messenger() -> tuple[CompositeToolExecutor, list[dict]]:
    sent: list[dict] = []
    callable_exec = CallableToolExecutor()
    post = ToolDefinition(
        name="post_message",
        description="post a message",
        source="custom:test",
        input_schema={
            "type": "object",
            "properties": {"chat_id": {"type": "string"}, "message": {"type": "string"}},
            "required": ["chat_id", "message"],
        },
        read_back=_MSG_SPEC,
    )
    read = ToolDefinition(
        name="read_messages",
        description="read messages",
        source="custom:test",
        input_schema={"type": "object", "properties": {"chat_id": {"type": "string"}}},
    )

    async def _post(**kwargs):
        sent.append(dict(kwargs))
        return f"Chat message sent. Message ID: m{len(sent)}."

    async def _read(**kwargs):
        return "m1: hello"

    callable_exec.register("post_message", _post)
    callable_exec.register("read_messages", _read)
    composite = CompositeToolExecutor()
    composite.add_tools([post, read], callable_exec)
    return composite, sent


async def _run(composite, rounds):
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    llm = _ScriptedLLM([*[_tool_call_response(*r) for r in rounds], ChatResponse(content="Done.", model="stub")])
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=6
    )
    return messages


def _call(cid: str, name: str, **arguments) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=arguments)


@pytest.mark.asyncio
async def test_the_loop_appends_read_back_and_flags_a_near_duplicate(_quiet_langfuse, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "self_verification_loop", True)
    composite, sent = _messenger()
    messages = await _run(
        composite,
        [
            [_call("c1", "post_message", chat_id="C1", message="The board meets at 10:00 in room 2.")],
            [_call("c2", "post_message", chat_id="C2", message="The board meets at 10:00 in room 2!")],
            [_call("c3", "read_messages", chat_id="C1")],
        ],
    )
    assert len(sent) == 2
    (first,) = _tool_messages(messages, "c1")
    assert "read_back:" in first.content and "id: m1" in first.content and "recipients: c1" in first.content
    (second,) = _tool_messages(messages, "c2")
    assert "near_duplicate:" in second.content
    (read,) = _tool_messages(messages, "c3")
    assert "read_back" not in read.content


@pytest.mark.asyncio
async def test_the_flag_off_leaves_the_result_as_it_was(_quiet_langfuse, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "self_verification_loop", False)
    composite, _ = _messenger()
    messages = await _run(composite, [[_call("c1", "post_message", chat_id="C1", message="hi")]])
    (first,) = _tool_messages(messages, "c1")
    assert "read_back" not in first.content
