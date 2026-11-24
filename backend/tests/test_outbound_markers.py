"""Artifact markers in a send tool's text reach the reader as links (#9990).

The web chat draws ``[[PRESENTATION:id]]`` and its siblings as cards; a Teams
chat, a mail or a Slack post draws none, so the send tools resolve them the
way the channel reply path does, in the run's scope, before the provider runs.
"""

from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.services import outbound_markers
from app.services.outbound_markers import (
    has_artifact_marker,
    markers_as_outbound_links,
    outbound_text_arguments,
    send_markers_as_links,
)
from app.tools.base import CallableToolExecutor, SendGuard, ToolCall, ToolDefinition

DECK = uuid.uuid4()
CONVERSATION = uuid.uuid4()
USER = uuid.uuid4()


def _definition(name: str, **kwargs) -> ToolDefinition:
    return ToolDefinition(name=name, description="", input_schema={}, **kwargs)


# ── Which arguments are outbound text ─────────────────────────────────────────


def test_outbound_text_is_declared_or_read_from_the_send_guard_body():
    assert outbound_text_arguments(_definition("send", outbound_text=("text",))) == ("text",)
    guarded = _definition("mail", send_guard=SendGuard(recipients=("to",), body=("subject", "body")))
    # A subject is a header line: it keeps its text.
    assert outbound_text_arguments(guarded) == ("body",)
    assert outbound_text_arguments(_definition("read")) == ()


def test_has_artifact_marker():
    assert has_artifact_marker(f"see [[PRESENTATION:{DECK}]]")
    assert has_artifact_marker("[[file:abc")
    assert not has_artifact_marker("[[not a marker]]")
    assert not has_artifact_marker(None)


# ── The transform ─────────────────────────────────────────────────────────────


async def test_every_kind_of_marker_is_resolved_and_none_is_left_raw():
    text = (
        f"Deck [[PRESENTATION:{DECK}]], file [[FILE:{uuid.uuid4()}]], "
        f"case [[INVESTIGATION:{uuid.uuid4()}]] and [[WORK_ITEM:{uuid.uuid4()}]]"
    )

    async def _file(_db, text, base, **scope):
        assert scope["conversation_id"] == CONVERSATION and scope["user_id"] == USER
        return re.sub(r"\[\[FILE:[^\]]+\]\]", "[report.pdf](https://api.example.com/f)", text)

    async def _links(_db, text, **scope):
        return text.replace(f"[[PRESENTATION:{DECK}]]", f"[Q3](https://app.example.com/presentations/{DECK})")

    with (
        patch("app.channels.base.resolve_channel_file_markers", _file),
        patch("app.services.platform_chat_artifacts.markers_as_links", _links),
    ):
        out = await markers_as_outbound_links(None, text, conversation_id=CONVERSATION, user_id=USER)
    assert f"[Q3](https://app.example.com/presentations/{DECK})" in out
    assert "[report.pdf](https://api.example.com/f)" in out
    assert "[[" not in out


async def test_a_tool_that_sends_files_keeps_its_file_markers() -> None:
    """``outbound_keeps_file_markers``: file markers are left to the tool, the others linked."""
    file_id = uuid.uuid4()
    text = f"Deck [[PRESENTATION:{DECK}]] [[FILE:{file_id}]]"

    async def _file(_db: object, text: str, base: str, **scope: object) -> str:
        raise AssertionError("file markers are not resolved for this tool")

    async def _links(_db: object, text: str, **scope: object) -> str:
        return text.replace(f"[[PRESENTATION:{DECK}]]", "<deck>")

    with (
        patch("app.channels.base.resolve_channel_file_markers", _file),
        patch("app.services.platform_chat_artifacts.markers_as_links", _links),
    ):
        out = await markers_as_outbound_links(None, text, keep_file_markers=True)
    assert out == f"Deck <deck> [[FILE:{file_id}]]"
    assert not has_artifact_marker(f"[[FILE:{file_id}]]", keep_file_markers=True)


async def test_text_without_a_marker_is_returned_as_is():
    assert await markers_as_outbound_links(None, "plain [link](x)") == "plain [link](x)"


async def test_a_deck_outside_the_scope_is_dropped_never_linked():
    """No conversation and no user: the channel resolvers fail closed."""
    from app.channels.base import resolve_channel_presentation_markers

    out = await resolve_channel_presentation_markers(
        AsyncMock(), f"see [[PRESENTATION:{DECK}]]", "https://app.example.com"
    )
    assert out == "see "


# ── The wrapper on the send tools ─────────────────────────────────────────────


@pytest.fixture
def resolved(monkeypatch):
    """The transform as a recorder: each marker becomes ``<link>``."""
    seen: list[dict] = []

    async def _resolve(_db, text, **scope):
        seen.append(scope)
        return text.replace(f"[[PRESENTATION:{DECK}]]", "<link>")

    @asynccontextmanager
    async def _session():
        yield None

    monkeypatch.setattr(outbound_markers, "markers_as_outbound_links", _resolve)
    monkeypatch.setattr("app.core.database.async_session", _session)
    return seen


async def test_a_send_tool_gets_links_and_its_other_arguments_unchanged(resolved):
    received: dict = {}

    async def send(chat_id: str, message: str, content_type: str = "text") -> str:
        received.update(chat_id=chat_id, message=message, content_type=content_type)
        return "sent"

    executor = CallableToolExecutor()
    executor.register("send", send)
    send_markers_as_links(
        [_definition("send", send_guard=SendGuard(recipients=("chat_id",), body=("message",)))],
        executor,
        {"conversation_id": str(CONVERSATION), "user_id": str(USER)},
    )
    result = await executor.execute_tool(
        ToolCall(
            id="1",
            name="send",
            arguments={
                "chat_id": f"19:[[PRESENTATION:{DECK}]]",
                "message": f"deck [[PRESENTATION:{DECK}]]",
                "bogus": 1,
            },
        )
    )
    assert result.content == "sent"
    # Only the outbound text is touched; the recipient keeps its value.
    assert received == {"chat_id": f"19:[[PRESENTATION:{DECK}]]", "message": "deck <link>", "content_type": "text"}
    assert resolved == [{"conversation_id": CONVERSATION, "user_id": USER}]


async def test_a_tool_that_is_not_a_send_is_left_alone(resolved):
    async def read(query: str) -> str:
        return query

    executor = CallableToolExecutor()
    executor.register("read", read)
    send_markers_as_links([_definition("read")], executor, {})
    assert executor.handler("read") is read


async def test_a_message_without_a_marker_opens_no_session(monkeypatch):
    @asynccontextmanager
    async def _session():
        raise AssertionError("no database for a text without marker")
        yield

    monkeypatch.setattr("app.core.database.async_session", _session)

    async def send(text: str) -> str:
        return text

    executor = CallableToolExecutor()
    executor.register("send", send)
    send_markers_as_links([_definition("send", outbound_text=("text",))], executor, {})
    assert (await executor.execute_tool(ToolCall(id="1", name="send", arguments={"text": "hi"}))).content == "hi"


# ── One test per adapter: the send tools declare their outbound text ─────────


def _defs(module, token: str, **kwargs) -> dict[str, ToolDefinition]:
    return {d.name: d for d in module.build_tools(token, CallableToolExecutor(), **kwargs)}


@pytest.mark.parametrize(
    ("tool", "argument"),
    [
        ("send_microsoft_chat_message", "message"),
        ("send_microsoft_channel_message", "message"),
        ("reply_microsoft_channel_message", "message"),
        ("send_microsoft_email", "body"),
        ("reply_microsoft_email_in_thread", "body"),
    ],
)
def test_microsoft_teams_and_outlook_sends_carry_links(tool, argument):
    from app.integrations.providers import microsoft

    assert argument in outbound_text_arguments(_defs(microsoft, "graph-token")[tool])


@pytest.mark.parametrize(
    "tool",
    ["send_teams_bot_chat_message", "send_teams_bot_group_chat_message", "send_teams_bot_channel_message"],
)
def test_teams_bot_sends_carry_links(tool):
    from app.integrations.providers import teams_bot

    assert "message" in outbound_text_arguments(_defs(teams_bot, "tenant-123||client-abc||secret-xyz")[tool])


@pytest.mark.parametrize(
    ("module_name", "tools"),
    [
        ("slack", ("send_slack_message", "reply_slack_thread")),
        ("slack_bot", ("send_slack_bot_message", "reply_slack_bot_thread")),
    ],
)
def test_slack_sends_carry_links(module_name, tools):
    import importlib

    module = importlib.import_module(f"app.integrations.providers.{module_name}")
    defs = _defs(module, "xoxb-token")
    for tool in tools:
        assert outbound_text_arguments(defs[tool]) == ("text",)


def test_gmail_sends_carry_links():
    from app.integrations.providers import google

    defs = _defs(google, "dummy")
    assert outbound_text_arguments(defs["send_gmail"]) == ("body", "body_html")
    assert outbound_text_arguments(defs["reply_gmail"]) == ("body", "body_html")
