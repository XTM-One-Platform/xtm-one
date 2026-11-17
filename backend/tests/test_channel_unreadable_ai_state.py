"""A channel message while whether agentic AI may run cannot be read.

Either half of the gate - the AI switch or the licence - unreadable (a database
or Redis error, a timeout) is not a refusal, and nothing is processed:

* a direct message (``router._is_private_conversation``, per platform) is told
  the AI service is temporarily unavailable;
* a group channel gets silence;
* the failure is logged at most once per window, without the message content;
* a failed read is not remembered as "off": the next message that finds the
  state readable is processed.

A readable state behaves as before: AI on processes the message, the switch off
answers "AI is disabled", Community Edition goes on to routing (where the
licence refusal is decided, ``test_edition_gate_followups``).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import RedisError
from sqlalchemy.exc import OperationalError

from app.api import deps
from app.api.v1 import platform as platform_api
from app.channels.base import ChannelType, InboundMessage
from app.services import ai_state
from tests.test_background_edition_gate import (
    AI_SWITCHED_OFF,
    COMMUNITY,
    ENTERPRISE,
    _pin,
    _pin_unreadable,
    _Session,
)

_CONTENT = "Incident notes: the VPN pre-shared key is hunter2"

# A direct message and a group message per platform, in the shape each adapter
# builds: (DM metadata, DM thread, group metadata, group thread).
_PLATFORMS = {
    ChannelType.SLACK: (
        {"channel_type": "im"},
        "D0123",
        {"channel_type": "channel"},
        "C0123:1700000000.000100",
    ),
    ChannelType.TEAMS: (
        {"conversation_type": "personal"},
        "a:1personal",
        {"conversation_type": "channel"},
        "19:abc@thread.tacv2;messageid=1700000000000",
    ),
    ChannelType.WHATSAPP: (
        {"chat_type": "private"},
        "33600000000",
        {"chat_type": "group"},
        "120363000000000000@g.us",
    ),
    ChannelType.TELEGRAM: (
        {"chat_type": "private"},
        "123456",
        {"chat_type": "group"},
        "-1001234567890",
    ),
    ChannelType.DISCORD: ({"is_dm": True}, "dm-1", {"is_dm": False}, "guild-1:chan-1"),
    ChannelType.GOOGLE_CHAT: (
        {"space_type": "DM"},
        "spaces/dm-1",
        {"space_type": "SPACE"},
        "spaces/room-1",
    ),
}
_CHANNELS = list(_PLATFORMS)


class _PastTheGateError(Exception):
    """Processing went on past the gate."""


def _inbound(channel: ChannelType, *, private: bool) -> InboundMessage:
    dm_meta, dm_thread, group_meta, group_thread = _PLATFORMS[channel]
    return InboundMessage(
        channel=channel,
        text=_CONTENT,
        user_email="analyst@example.com",
        channel_user_id="U12345",
        channel_thread_id=dm_thread if private else group_thread,
        metadata=dict(dm_meta if private else group_meta),
    )


@pytest.fixture(autouse=True)
def _router(monkeypatch):
    """Sessions counted; the first step past the gate raises."""
    from app.channels import router

    monkeypatch.setattr(router, "_unreadable_state_logged_at", None, raising=False)
    monkeypatch.setattr(router, "_unreadable_state_unlogged", 0, raising=False)
    opened: list[_Session] = []

    def _open():
        session = _Session()
        opened.append(session)
        return session

    monkeypatch.setattr(router, "async_session", _open)
    past = AsyncMock(side_effect=_PastTheGateError)
    monkeypatch.setattr(router, "_get_channel_bot", past)
    return SimpleNamespace(opened=opened, past=past)


def _processed_nothing(state) -> bool:
    """Only the gate's own session was opened, and routing never started."""
    return len(state.opened) == 1 and not state.past.await_count


@pytest.mark.asyncio
@pytest.mark.parametrize("half", ["switch", "licence"])
@pytest.mark.parametrize("channel", _CHANNELS, ids=lambda c: c.value)
async def test_an_unreadable_state_answers_a_direct_message_as_temporarily_unavailable(
    monkeypatch, _router, channel, half
):
    from app.channels import router

    _pin_unreadable(monkeypatch, half=half)

    out = await router.handle_message(_inbound(channel, private=True))

    assert out.text == deps.AI_TEMPORARILY_UNAVAILABLE_DETAIL
    assert not out.suppress
    assert _processed_nothing(_router)


@pytest.mark.asyncio
@pytest.mark.parametrize("half", ["switch", "licence"])
@pytest.mark.parametrize("channel", _CHANNELS, ids=lambda c: c.value)
async def test_an_unreadable_state_keeps_a_group_channel_silent(
    monkeypatch, _router, channel, half
):
    from app.channels import router

    _pin_unreadable(monkeypatch, half=half)

    out = await router.handle_message(_inbound(channel, private=False))

    assert out.suppress is True and out.text == ""
    assert _processed_nothing(_router)


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [True, False], ids=["direct", "group"])
@pytest.mark.parametrize("channel", _CHANNELS, ids=lambda c: c.value)
async def test_a_readable_state_behaves_as_before(monkeypatch, _router, channel, private):
    from app.channels import router

    inbound = _inbound(channel, private=private)

    # AI on: processed.
    _pin(monkeypatch, **ENTERPRISE)
    with pytest.raises(_PastTheGateError):
        await router.handle_message(inbound)

    # Community Edition: on to routing, where the licence refusal is decided.
    _pin(monkeypatch, **COMMUNITY)
    with pytest.raises(_PastTheGateError):
        await router.handle_message(inbound)

    # The switch off: its own reply, on every message.
    _pin(monkeypatch, **AI_SWITCHED_OFF)
    out = await router.handle_message(inbound)
    assert out.text.startswith("AI is disabled on this platform")
    assert not out.suppress
    assert deps.AI_TEMPORARILY_UNAVAILABLE_DETAIL not in out.text


@pytest.mark.asyncio
@pytest.mark.parametrize("half", ["switch", "licence"])
async def test_a_failed_read_is_not_remembered_as_off(monkeypatch, _router, half):
    """Nothing cached from the read that failed: the next message reads again."""
    from app.channels import router

    calls = {"n": 0}

    def _flaky() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("SELECT 1", {}, Exception("database failover"))

    if half == "switch":

        async def _override(_db):
            _flaky()
            return False  # ai_disabled = False: AI on

        monkeypatch.setattr(ai_state, "_read_override_isolated", _override)
        monkeypatch.setattr(platform_api, "is_agentic_ai_licensed", AsyncMock(return_value=True))
    else:

        async def _licence(_db):
            _flaky()
            return True

        async def _ai(_db):
            return True

        monkeypatch.setattr(ai_state, "is_ai_enabled", _ai)
        monkeypatch.setattr(platform_api, "is_agentic_ai_licensed", _licence)
        ai_state.prime_cache(True)

    inbound = _inbound(ChannelType.SLACK, private=True)
    first = await router.handle_message(inbound)
    assert first.text == deps.AI_TEMPORARILY_UNAVAILABLE_DETAIL

    with pytest.raises(_PastTheGateError):
        await router.handle_message(inbound)
    assert calls["n"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        RedisError("connection reset"),
        OperationalError("SELECT 1", {}, Exception("database failover")),
        TimeoutError("read timed out"),
    ],
    ids=["redis", "database", "timeout"],
)
@pytest.mark.parametrize("private", [True, False], ids=["direct", "group"])
async def test_a_store_failure_escaping_the_gate_is_an_unreadable_state(
    monkeypatch, _router, error, private
):
    """Whatever store the read ends up touching: a Redis error, a database
    error or a timeout the gate did not absorb reads as unreadable, never as
    the adapter's generic error (which would answer a group @mention)."""
    from app.channels import router
    from app.services import agentic_gate

    monkeypatch.setattr(agentic_gate, "agentic_verdict", AsyncMock(side_effect=error))

    out = await router.handle_message(_inbound(ChannelType.SLACK, private=private))

    if private:
        assert out.text == deps.AI_TEMPORARILY_UNAVAILABLE_DETAIL and not out.suppress
    else:
        assert out.suppress is True and out.text == ""
    assert not _router.past.await_count


@pytest.mark.asyncio
async def test_a_switch_read_timeout_is_absorbed_as_unreadable(monkeypatch, _router):
    from app.channels import router

    async def _slow(_db):
        raise TimeoutError("statement timeout")

    monkeypatch.setattr(ai_state, "_read_override_isolated", _slow)
    monkeypatch.setattr(platform_api, "is_agentic_ai_licensed", AsyncMock(return_value=True))

    out = await router.handle_message(_inbound(ChannelType.DISCORD, private=False))

    assert out.suppress is True and out.text == ""


@pytest.mark.asyncio
async def test_a_programming_error_in_the_gate_still_surfaces(monkeypatch, _router):
    from app.channels import router
    from app.services import agentic_gate

    monkeypatch.setattr(agentic_gate, "agentic_verdict", AsyncMock(side_effect=KeyError("bug")))

    with pytest.raises(KeyError):
        await router.handle_message(_inbound(ChannelType.SLACK, private=True))


@pytest.mark.asyncio
async def test_the_failure_is_logged_once_per_window_without_the_message(
    monkeypatch, _router, caplog
):
    from app.channels import router

    clock = SimpleNamespace(now=1_000.0)
    monkeypatch.setattr(router, "time", SimpleNamespace(monotonic=lambda: clock.now))
    _pin_unreadable(monkeypatch, half="licence")
    caplog.set_level(logging.DEBUG)

    for private in (True, False, True):
        await router.handle_message(_inbound(ChannelType.TEAMS, private=private))
    clock.now += router._UNREADABLE_STATE_LOG_INTERVAL_SECONDS
    await router.handle_message(_inbound(ChannelType.TEAMS, private=False))

    notices = [
        r
        for r in caplog.records
        if r.name == router.logger.name and "Could not read whether agentic AI" in r.getMessage()
    ]
    assert len(notices) == 2, "one per window"
    assert "(2 more since the last notice)" in notices[1].getMessage()
    for record in caplog.records:
        assert "hunter2" not in record.getMessage()
