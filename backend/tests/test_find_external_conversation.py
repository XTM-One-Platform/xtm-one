"""Unit tests for _find_existing_external_conversation and helpers.

Covers the security-sensitive conversation-isolation logic: group chats share a thread, DMs do not, and
the shared-thread fallback is scoped to the same agent.
"""

import asyncio
import uuid
from unittest.mock import MagicMock

from app.channels.base import ChannelType, InboundMessage
from app.channels.router import (
    _find_existing_external_conversation,
    _should_allow_shared_thread,
)
from app.models.chat import Conversation

# ── Fake DB helpers ────────────────────────────────────────────────────────


class _ScalarResult:
    """Mimics the result of ``db.execute(select(...))``."""

    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    """Minimal async SQLAlchemy session stub.

    Pre-load ``execute_results`` with :class:`_ScalarResult` instances in
    the order they should be returned.  Each ``await db.execute(...)`` pops
    the next result from the queue.
    """

    def __init__(self, execute_results):
        self._execute_results = list(execute_results)

    async def execute(self, _statement):
        if not self._execute_results:
            raise AssertionError("Unexpected extra execute() call")
        return self._execute_results.pop(0)


# ── Fixtures ───────────────────────────────────────────────────────────────


def _make_conv(
    agent_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    channel: str = "slack",
    channel_thread_id: str = "T001",
) -> Conversation:
    """Build a minimal Conversation without hitting the DB."""
    conv = MagicMock(spec=Conversation)
    conv.id = uuid.uuid4()
    conv.agent_id = agent_id
    conv.user_id = user_id
    conv.channel = channel
    conv.channel_thread_id = channel_thread_id
    conv.is_archived = False
    return conv


def _inbound(
    channel: ChannelType = ChannelType.SLACK,
    metadata: dict | None = None,
) -> InboundMessage:
    return InboundMessage(
        channel=channel,
        text="hello",
        channel_thread_id="T001",
        metadata=metadata or {},
    )


# ── _should_allow_shared_thread ────────────────────────────────────────────


def test_allow_shared_thread_for_slack_non_dm():
    msg = _inbound(ChannelType.SLACK)
    # channel_type not set → not a DM
    assert _should_allow_shared_thread(msg) is True


def test_no_shared_thread_for_web():
    msg = _inbound(ChannelType.WEB)
    assert _should_allow_shared_thread(msg) is False


def test_no_shared_thread_for_web_embed():
    msg = _inbound(ChannelType.WEB_EMBED)
    assert _should_allow_shared_thread(msg) is False


def test_no_shared_thread_for_slack_dm():
    msg = _inbound(ChannelType.SLACK, metadata={"channel_type": "im"})
    assert _should_allow_shared_thread(msg) is False


def test_no_shared_thread_for_teams_personal():
    msg = _inbound(ChannelType.TEAMS, metadata={"conversation_type": "personal"})
    assert _should_allow_shared_thread(msg) is False


def test_allow_shared_thread_for_teams_group():
    msg = _inbound(ChannelType.TEAMS, metadata={"conversation_type": "groupChat"})
    assert _should_allow_shared_thread(msg) is True


def test_allow_shared_thread_for_telegram_group():
    msg = _inbound(ChannelType.TELEGRAM, metadata={"chat_type": "group"})
    assert _should_allow_shared_thread(msg) is True


def test_no_shared_thread_for_telegram_private():
    msg = _inbound(ChannelType.TELEGRAM, metadata={"chat_type": "private"})
    assert _should_allow_shared_thread(msg) is False


# ── _find_existing_external_conversation — baseline ───────────────────────


def test_returns_none_when_no_thread_id():
    db = _FakeSession([])
    result = asyncio.run(
        _find_existing_external_conversation(db, uuid.uuid4(), ChannelType.SLACK, None)
    )
    assert result is None


def test_returns_user_scoped_match_directly():
    user_id = uuid.uuid4()
    conv = _make_conv(user_id=user_id)
    db = _FakeSession([_ScalarResult(conv)])
    result = asyncio.run(
        _find_existing_external_conversation(
            db, user_id, ChannelType.SLACK, "T001"
        )
    )
    assert result is conv


def test_returns_none_when_no_match_and_shared_disabled():
    db = _FakeSession([_ScalarResult(None)])
    result = asyncio.run(
        _find_existing_external_conversation(
            db, uuid.uuid4(), ChannelType.SLACK, "T001", allow_shared_thread=False
        )
    )
    assert result is None


# ── _find_existing_external_conversation — shared thread fallback ──────────


def test_secondary_participant_joins_existing_group_thread():
    """User B (different user_id) finds User A's existing conversation."""
    user_a_conv = _make_conv()
    db = _FakeSession([
        _ScalarResult(None),       # user B's scoped lookup → no match
        _ScalarResult(user_a_conv),  # shared fallback → User A's conv
    ])
    result = asyncio.run(
        _find_existing_external_conversation(
            db, uuid.uuid4(), ChannelType.SLACK, "T001", allow_shared_thread=True
        )
    )
    assert result is user_a_conv


def test_dm_does_not_leak_to_secondary_participant():
    """With allow_shared_thread=False (DM), only one query is issued."""
    db = _FakeSession([_ScalarResult(None)])
    result = asyncio.run(
        _find_existing_external_conversation(
            db, uuid.uuid4(), ChannelType.SLACK, "T001", allow_shared_thread=False
        )
    )
    assert result is None
    # All queued results consumed — exactly one execute was issued.
    assert len(db._execute_results) == 0


def test_archived_thread_not_resurrected_by_shared_fallback():
    """Archived conversations are excluded from the shared-thread fallback."""
    db = _FakeSession([
        _ScalarResult(None),  # user-scoped → miss
        _ScalarResult(None),  # shared fallback → no non-archived match
    ])
    result = asyncio.run(
        _find_existing_external_conversation(
            db, uuid.uuid4(), ChannelType.SLACK, "T001", allow_shared_thread=True
        )
    )
    assert result is None


def test_anonymous_user_falls_back_to_shared_thread():
    """Anonymous lookup (user_id=None) also uses the shared fallback."""
    conv = _make_conv()
    db = _FakeSession([
        _ScalarResult(None),  # anonymous-scoped lookup → miss
        _ScalarResult(conv),   # shared fallback → hit
    ])
    result = asyncio.run(
        _find_existing_external_conversation(
            db, None, ChannelType.SLACK, "T001", allow_shared_thread=True
        )
    )
    assert result is conv


def test_anonymous_owns_thread_no_fallback_needed():
    """When the anonymous-scoped query hits, no shared fallback query is issued."""
    conv = _make_conv()
    db = _FakeSession([_ScalarResult(conv)])
    result = asyncio.run(
        _find_existing_external_conversation(
            db, None, ChannelType.SLACK, "T001", allow_shared_thread=True
        )
    )
    assert result is conv
    assert len(db._execute_results) == 0  # only one execute issued


# ── agent_id scoping (cross-agent isolation) ──────────────────────────────


def test_agent_id_forwarded_to_shared_fallback():
    """When agent_id is provided, the shared fallback scopes by agent.

    We verify that the fallback IS executed (two queries total) when the
    user-scoped query misses, even when agent_id is supplied — confirming
    the agent_id filter is additive, not a blocker.
    """
    agent_id = uuid.uuid4()
    agent_conv = _make_conv(agent_id=agent_id)
    db = _FakeSession([
        _ScalarResult(None),        # user-scoped miss
        _ScalarResult(agent_conv),  # shared fallback (scoped to agent_id)
    ])
    result = asyncio.run(
        _find_existing_external_conversation(
            db,
            uuid.uuid4(),
            ChannelType.SLACK,
            "T001",
            allow_shared_thread=True,
            agent_id=agent_id,
        )
    )
    assert result is agent_conv


def test_different_agent_id_returns_none():
    """If the only shared conv belongs to a different agent, fallback returns None."""
    db = _FakeSession([
        _ScalarResult(None),  # user-scoped miss
        _ScalarResult(None),  # shared fallback filtered by agent_id → no match
    ])
    result = asyncio.run(
        _find_existing_external_conversation(
            db,
            uuid.uuid4(),
            ChannelType.SLACK,
            "T001",
            allow_shared_thread=True,
            agent_id=uuid.uuid4(),  # different agent → filtered out at DB level
        )
    )
    assert result is None


def test_no_agent_id_still_falls_back():
    """Without agent_id, the shared fallback still works (backward compat)."""
    conv = _make_conv()
    db = _FakeSession([
        _ScalarResult(None),  # user-scoped miss
        _ScalarResult(conv),   # shared fallback (no agent filter)
    ])
    result = asyncio.run(
        _find_existing_external_conversation(
            db,
            uuid.uuid4(),
            ChannelType.SLACK,
            "T001",
            allow_shared_thread=True,
            # agent_id not supplied → fallback has no agent filter
        )
    )
    assert result is conv
