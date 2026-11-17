"""Regression tests for the group-chat shared-thread conversation lookup.

Covers `_find_existing_external_conversation` with `allow_shared_thread`
on/off, DM vs group-chat detection, archived-thread isolation, and the
agent-scope filter that prevents cross-agent context bleed.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.channels.base import ChannelType, InboundMessage
from app.channels.router import (
    _find_existing_external_conversation,
    _shared_thread_fallback,
    _should_allow_shared_thread,
)

# ---------------------------------------------------------------------------
# `_should_allow_shared_thread` — pure detection
# ---------------------------------------------------------------------------


def _inbound(
    *,
    channel: ChannelType,
    conversation_type: str | None = None,
    chat_type: str | None = None,
    channel_type: str | None = None,
) -> InboundMessage:
    metadata: dict = {}
    if conversation_type is not None:
        metadata["conversation_type"] = conversation_type
    if chat_type is not None:
        metadata["chat_type"] = chat_type
    if channel_type is not None:
        metadata["channel_type"] = channel_type
    return InboundMessage(
        channel=channel,
        text="hi",
        user_email="user@example.com",
        metadata=metadata,
    )


class TestShouldAllowSharedThread:
    def test_web_is_never_shared(self):
        assert _should_allow_shared_thread(_inbound(channel=ChannelType.WEB)) is False

    def test_web_embed_is_never_shared(self):
        assert (
            _should_allow_shared_thread(_inbound(channel=ChannelType.WEB_EMBED))
            is False
        )

    def test_teams_personal_chat_is_not_shared(self):
        # Teams DMs report `conversation_type == "personal"`.
        assert (
            _should_allow_shared_thread(
                _inbound(channel=ChannelType.TEAMS, conversation_type="personal")
            )
            is False
        )

    def test_teams_group_chat_is_shared(self):
        assert (
            _should_allow_shared_thread(
                _inbound(channel=ChannelType.TEAMS, conversation_type="groupChat")
            )
            is True
        )

    def test_slack_dm_is_not_shared(self):
        # Slack DMs report `channel_type == "im"`.
        assert (
            _should_allow_shared_thread(
                _inbound(channel=ChannelType.SLACK, channel_type="im")
            )
            is False
        )

    def test_slack_public_channel_is_shared(self):
        assert (
            _should_allow_shared_thread(
                _inbound(channel=ChannelType.SLACK, channel_type="channel")
            )
            is True
        )

    def test_telegram_private_is_not_shared(self):
        assert (
            _should_allow_shared_thread(
                _inbound(channel=ChannelType.TELEGRAM, chat_type="private")
            )
            is False
        )

    def test_telegram_group_is_shared(self):
        assert (
            _should_allow_shared_thread(
                _inbound(channel=ChannelType.TELEGRAM, chat_type="group")
            )
            is True
        )


# ---------------------------------------------------------------------------
# `_find_existing_external_conversation` and `_shared_thread_fallback`
#
# These are tested by stubbing `db.execute` to return one of a sequence of
# canned results. The lookup makes either one DB call (user-scoped match
# found) or two DB calls (user-scoped miss + shared-thread fallback). The
# stub asserts the expected number of calls.
# ---------------------------------------------------------------------------


def _make_conversation(
    *,
    user_id: uuid.UUID | None,
    channel: ChannelType,
    channel_thread_id: str,
    agent_id: uuid.UUID | None = None,
    is_archived: bool = False,
    created_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        channel=channel.value,
        channel_thread_id=channel_thread_id,
        agent_id=agent_id,
        is_archived=is_archived,
        title="seeded",
        created_at=created_at or datetime.now(UTC),
    )


class _ResultStub:
    def __init__(self, value: Any | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any | None:
        return self._value


class _StubSession:
    """Mock async session that returns a queued sequence of results."""

    def __init__(self, results: list[Any | None]) -> None:
        self._results: list[Any | None] = list(results)
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _ResultStub:
        self.statements.append(statement)
        if not self._results:
            raise AssertionError(
                "Unexpected extra execute() call — provide more queued results."
            )
        return _ResultStub(self._results.pop(0))


@pytest.mark.asyncio
async def test_returns_none_when_no_thread_id():
    db = _StubSession([])  # no DB calls expected
    result = await _find_existing_external_conversation(
        db,  # type: ignore[arg-type]
        uuid.uuid4(),
        ChannelType.TEAMS,
        None,
        allow_shared_thread=True,
    )
    assert result is None
    assert db.statements == []


@pytest.mark.asyncio
async def test_returns_user_scoped_match_immediately():
    """When the user-scoped query hits, no shared-thread fallback runs."""
    thread_id = "19:thread-1"
    user_id = uuid.uuid4()
    user_conv = _make_conversation(
        user_id=user_id,
        channel=ChannelType.TEAMS,
        channel_thread_id=thread_id,
    )
    db = _StubSession([user_conv])

    result = await _find_existing_external_conversation(
        db,  # type: ignore[arg-type]
        user_id,
        ChannelType.TEAMS,
        thread_id,
        allow_shared_thread=True,
    )
    assert result is user_conv
    assert len(db.statements) == 1  # only the user-scoped query


@pytest.mark.asyncio
async def test_dm_does_not_run_shared_fallback():
    """DM threads (`allow_shared_thread=False`) issue exactly one query."""
    db = _StubSession([None])  # user-scoped miss

    result = await _find_existing_external_conversation(
        db,  # type: ignore[arg-type]
        uuid.uuid4(),
        ChannelType.TEAMS,
        "19:dm-thread",
        allow_shared_thread=False,
    )
    assert result is None
    assert len(db.statements) == 1  # no fallback issued


@pytest.mark.asyncio
async def test_group_chat_falls_back_to_shared_thread():
    thread_id = "19:group-thread"
    other_user_conv = _make_conversation(
        user_id=uuid.uuid4(),
        channel=ChannelType.TEAMS,
        channel_thread_id=thread_id,
    )
    # User-scoped query misses, shared-thread fallback hits.
    db = _StubSession([None, other_user_conv])

    result = await _find_existing_external_conversation(
        db,  # type: ignore[arg-type]
        uuid.uuid4(),  # secondary participant
        ChannelType.TEAMS,
        thread_id,
        allow_shared_thread=True,
    )
    assert result is other_user_conv
    assert len(db.statements) == 2  # user-scoped + fallback


@pytest.mark.asyncio
async def test_archived_thread_is_not_resurrected():
    """If the fallback query also misses, no conversation is returned."""
    db = _StubSession([None, None])  # user-scoped + fallback both miss

    result = await _find_existing_external_conversation(
        db,  # type: ignore[arg-type]
        uuid.uuid4(),
        ChannelType.TEAMS,
        "19:archived-thread",
        allow_shared_thread=True,
    )
    assert result is None
    assert len(db.statements) == 2


@pytest.mark.asyncio
async def test_anonymous_lookup_falls_back_to_authenticated_thread():
    thread_id = "19:mixed-thread"
    authed_conv = _make_conversation(
        user_id=uuid.uuid4(),
        channel=ChannelType.TEAMS,
        channel_thread_id=thread_id,
    )
    # Anonymous user-scoped query misses, fallback finds authenticated thread.
    db = _StubSession([None, authed_conv])

    result = await _find_existing_external_conversation(
        db,  # type: ignore[arg-type]
        None,
        ChannelType.TEAMS,
        thread_id,
        allow_shared_thread=True,
    )
    assert result is authed_conv
    assert len(db.statements) == 2


@pytest.mark.asyncio
async def test_anonymous_lookup_without_shared_thread_returns_none():
    db = _StubSession([None])  # anonymous-scoped only

    result = await _find_existing_external_conversation(
        db,  # type: ignore[arg-type]
        None,
        ChannelType.TEAMS,
        "19:dm-thread",
        allow_shared_thread=False,
    )
    assert result is None
    assert len(db.statements) == 1


@pytest.mark.asyncio
async def test_shared_thread_lookup_scopes_by_agent_id():
    """`_shared_thread_fallback(agent_id=...)` includes the agent filter."""
    thread_id = "19:scoped-thread"
    agent_id = uuid.uuid4()
    db = _StubSession([None])

    await _shared_thread_fallback(
        db,  # type: ignore[arg-type]
        ChannelType.TEAMS,
        thread_id,
        agent_id,
    )
    assert len(db.statements) == 1
    where_str = str(db.statements[0].whereclause).lower()
    # The compiled WHERE clause must include an `agent_id` predicate so
    # cross-agent conversations cannot be returned.
    assert "agent_id" in where_str


@pytest.mark.asyncio
async def test_shared_thread_lookup_skips_agent_filter_when_none():
    """`agent_id=None` falls back to the bare channel-thread match."""
    db = _StubSession([None])

    await _shared_thread_fallback(
        db,  # type: ignore[arg-type]
        ChannelType.TEAMS,
        "19:bare-thread",
        None,
    )
    assert len(db.statements) == 1
    # No `agent_id` predicate is appended on this code path.
    where_str = str(db.statements[0].whereclause).lower()
    assert "agent_id" not in where_str


@pytest.mark.asyncio
async def test_find_existing_forwards_agent_id_to_fallback():
    """`_find_existing_external_conversation` forwards `agent_id`."""
    thread_id = "19:scoped-thread"
    agent_id = uuid.uuid4()
    db = _StubSession([None, None])  # user-scoped miss + fallback miss

    await _find_existing_external_conversation(
        db,  # type: ignore[arg-type]
        uuid.uuid4(),
        ChannelType.TEAMS,
        thread_id,
        allow_shared_thread=True,
        agent_id=agent_id,
    )
    # Second statement is the fallback — confirm it carries an agent_id
    # filter so cross-agent fallback is not possible.
    fallback_stmt = db.statements[1]
    where_str = str(fallback_stmt.whereclause).lower()
    assert "agent_id" in where_str
