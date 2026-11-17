"""A Slack sender is identified by their own member id, never by the channel.

The adapter used to put the channel id in ``InboundMessage.channel_user_id``.
The router links that key to a sender who resolves by email and falls back on
it for a sender whose email does not resolve, so in a shared channel every
such sender ran as the first member who had written there by email.

Pinned here, through the real webhook and the real router: each sender of one
channel reaches the router under their own member id, the identity fallback
and the auto-link are asked for that id, and the reply still goes to the
channel, as does the answer to a queued message.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.channels.base import ChannelType, InboundMessage, OutboundMessage
from tests.test_channel_routing_logs import _adapter, _bot, _request, _route, _run, _user

CHANNEL = "C0SHARED"
ALICE, BOB = "U0ALICE", "U0BOB"


def _event(user: str, ts: str) -> dict:
    return {
        "type": "event_callback",
        "authorizations": [{"user_id": "UBOT"}],
        "event": {
            "type": "app_mention",
            "text": "<@UBOT> any news on the VPN alerts?",
            "user": user,
            "channel": CHANNEL,
            "channel_type": "channel",
            "ts": ts,
        },
    }


class _RecordingChannel:
    """A Slack client that records what it would post and sends nothing."""

    supports_typing = False
    sent: list[OutboundMessage] = []

    def __init__(self, *_args, **_kwargs):
        pass

    async def send_message(self, message: OutboundMessage) -> str:
        _RecordingChannel.sent.append(message)
        return "1700000009.000100"

    def __getattr__(self, _name):
        return AsyncMock(return_value=None)


def _slack_adapter(monkeypatch, emails: dict[str, str | None]):
    from app.channels import slack

    spawned = _adapter(monkeypatch, slack)
    _RecordingChannel.sent = []
    monkeypatch.setattr(slack, "SlackChannel", _RecordingChannel)
    monkeypatch.setattr(slack, "_get_slack_bot_creds", AsyncMock(return_value=("xoxb-token", "signing")))
    monkeypatch.setattr(slack, "verify_slack_signature", lambda *_a: True)
    monkeypatch.setattr(slack, "_is_duplicate_event", AsyncMock(return_value=False))

    async def _resolve(_token, member):
        return emails[member], None

    monkeypatch.setattr(slack, "_resolve_slack_user", _resolve)
    return slack, spawned


async def _deliver(slack, spawned, user: str, ts: str) -> InboundMessage:
    """The message the webhook hands the router for *user*'s post in the channel."""
    await slack.slack_events(_request("/api/channels/slack/events", _event(user, ts)))
    await _run(spawned)
    return slack.handle_message.await_args.args[0]


@pytest.mark.asyncio
async def test_each_sender_of_a_channel_reaches_the_router_under_their_own_member_id(monkeypatch):
    slack, spawned = _slack_adapter(monkeypatch, {ALICE: "alice@example.com", BOB: None})
    # The router answers with the sender's id, as it does; the adapter targets the channel.
    slack.handle_message.side_effect = lambda inbound: OutboundMessage(
        text="On it.", channel_user_id=inbound.channel_user_id
    )

    alice = await _deliver(slack, spawned, ALICE, "1700000000.000100")
    bob = await _deliver(slack, spawned, BOB, "1700000001.000100")

    assert (alice.channel_user_id, alice.user_email) == (ALICE, "alice@example.com")
    assert (bob.channel_user_id, bob.user_email) == (BOB, None)
    assert (alice.channel_thread_id, bob.channel_thread_id) == (
        f"{CHANNEL}:1700000000.000100",
        f"{CHANNEL}:1700000001.000100",
    )
    # The replies go to the channel, in each message's thread, never to a member.
    assert [(m.channel_user_id, m.metadata["thread_ts"]) for m in _RecordingChannel.sent] == [
        (CHANNEL, "1700000000.000100"),
        (CHANNEL, "1700000001.000100"),
    ]


@pytest.mark.asyncio
async def test_the_router_links_and_looks_up_the_senders_member_id_never_the_channel(monkeypatch):
    slack, spawned = _slack_adapter(monkeypatch, {ALICE: "alice@example.com", BOB: None})
    alice_msg = await _deliver(slack, spawned, ALICE, "1700000000.000100")
    bob_msg = await _deliver(slack, spawned, BOB, "1700000001.000100")

    alice = _user()
    router = _route(monkeypatch, bot=_bot(), user=alice)
    await router.handle_message(alice_msg)
    ((_db, linked_user, channel, key),) = [c.args for c in router.link_channel_identity.await_args_list]
    assert (linked_user, channel, key) == (alice, "slack", ALICE)

    # Bob's email does not resolve: the fallback asks for Bob's own id, finds no one.
    router = _route(monkeypatch, bot=_bot(), user=None)
    router.resolve_user_by_channel_identity.return_value = None
    await router.handle_message(bob_msg)
    ((_db, channel, key),) = [c.args for c in router.resolve_user_by_channel_identity.await_args_list]
    assert (channel, key) == ("slack", BOB)
    router.link_channel_identity.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_queued_message_is_answered_in_its_channel_not_to_its_sender(monkeypatch):
    from app.channels import slack

    monkeypatch.setattr(slack, "_get_slack_bot_creds", AsyncMock(return_value=("xoxb-token", "signing")))
    process = AsyncMock()
    monkeypatch.setattr(slack, "_process_slack_message", process)
    queued = InboundMessage(
        channel=ChannelType.SLACK,
        text="and the figures?",
        user_email="alice@example.com",
        channel_user_id=ALICE,
        channel_thread_id="D0ALICE:1700000000.000100",
        metadata={"channel_type": "im"},
    )

    await slack.run_queued_message(queued)

    process.assert_awaited_once_with(queued, "xoxb-token", "D0ALICE", "1700000000.000100")


@pytest.mark.asyncio
async def test_a_queued_message_naming_no_channel_is_undeliverable(monkeypatch):
    from app.channels import slack
    from app.services.channel_continuation import QueuedMessageUndeliverableError

    monkeypatch.setattr(slack, "_get_slack_bot_creds", AsyncMock(return_value=("xoxb-token", "signing")))
    send = AsyncMock()
    monkeypatch.setattr(slack.SlackChannel, "send_message", send)
    queued = SimpleNamespace(channel_thread_id="", channel_user_id=ALICE)

    with pytest.raises(QueuedMessageUndeliverableError):
        await slack.run_queued_message(queued)
    assert await slack.post_queued_notice(queued, "dropped") is False
    send.assert_not_awaited()
