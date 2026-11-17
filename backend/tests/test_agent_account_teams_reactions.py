"""Reactions on an agent account's Teams answers, with Graph answered by a fake."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.services.agent_accounts import listening, reactions
from app.services.agent_accounts.providers import InboundNotification, ProviderSession, UpdatedMessage
from app.services.agent_accounts.providers import microsoft_teams as teams
from app.services.agent_accounts.rules import ChannelTarget, ListeningPlan
from app.services.reaction_feedback import PlatformReaction
from tests.test_agent_account_teams import GRAPH, ME, _provider

UP = "\U0001f44d"


def _session() -> ProviderSession:
    return ProviderSession(token="token", account_external_id=ME)


def _notification(resource: str, change_type: str) -> InboundNotification:
    return InboundNotification("s1", "c1", None, {"resource": resource, "changeType": change_type})


@pytest.mark.parametrize(
    ("resource", "change_type", "expected"),
    [
        pytest.param("chats('19:a')/messages('171')", "updated", ("19:a", "171"), id="chat"),
        pytest.param(
            "teams('t1')/channels('19:c')/messages('100')/replies('171')", "updated", ("19:c", "171"), id="reply"
        ),
        pytest.param("teams('t1')/channels('19:c')/messages('100')", "updated", ("19:c", "100"), id="root"),
        pytest.param("chats('19:a')/messages('171')", "created", None, id="created"),
        pytest.param("chats('19:a')/messages('171')", None, None, id="no-change-type"),
        pytest.param("users('x')", "updated", None, id="other-resource"),
    ],
)
def test_an_updated_message_is_named_from_the_notification_alone(resource, change_type, expected):
    updated = teams.MicrosoftTeamsProvider().updated_message(_notification(resource, change_type))
    assert (None if updated is None else (updated.conversation_id, updated.message_id)) == expected


@pytest.mark.parametrize(
    ("reaction_type", "expected"),
    [
        pytest.param("like", UP, id="legacy-like"),
        pytest.param("Heart", "❤️", id="legacy-heart"),
        pytest.param("\U0001f44e", "\U0001f44e", id="unicode"),
        pytest.param("custom", None, id="custom"),
        pytest.param("", None, id="empty"),
        pytest.param(None, None, id="null"),
    ],
)
def test_a_teams_reaction_type_is_read_as_unicode(reaction_type, expected):
    assert teams.teams_reaction_emoji(reaction_type) == expected


async def test_with_reactions_only_the_unfiltered_subscriptions_hear_changes(monkeypatch):
    provider, _ = _provider(
        monkeypatch,
        {
            ("GET", f"{GRAPH}/me/joinedTeams"): {"value": [{"id": "t1", "displayName": "Ops"}]},
            ("GET", f"{GRAPH}/teams/t1/channels"): {"value": [{"id": "ch1"}, {"id": "ch2"}]},
        },
    )
    plan = ListeningPlan(
        chats=True, channels=(ChannelTarget("ch1", "t1", True),), all_channels=True, all_channels_mentions_only=False
    )
    mention = f"$filter=mentions/any(u: u/mentioned/user/id eq '{ME}')"
    with_reactions = await provider.desired_subscriptions(_session(), plan, reactions=True)
    assert [(d.resource, d.change_type, d.fallback_change_type) for d in with_reactions] == [
        # Refused with the changes, a subscription still hears the new messages.
        (f"/users/{ME}/chats/getAllMessages", "created,updated", "created"),
        # Graph grants one subscription per channel: the mention filter stays, without changes.
        (f"/teams/t1/channels/ch1/messages?{mention}", "created", None),
        ("/teams/t1/channels/ch2/messages", "created,updated", "created"),
    ]
    without = await provider.desired_subscriptions(_session(), plan)
    assert {(d.change_type, d.fallback_change_type) for d in without} == {("created", None)}


async def test_a_subscription_is_created_with_its_change_types(monkeypatch):
    provider, fake = _provider(
        monkeypatch, {("POST", f"{GRAPH}/subscriptions"): {"id": "sub-1", "expirationDateTime": "2026-10-09T09:00:00Z"}}
    )
    await provider.create_subscription(
        _session(), "/users/x/chats/getAllMessages",
        notification_url="https://p/x", lifecycle_url="https://p/x?lifecycle=1", client_state="s",
        change_type="created,updated",
    )
    assert fake.calls[0][3]["changeType"] == "created,updated"


def test_an_answer_is_pushed_only_where_a_subscription_hears_changes():
    provider = teams.MicrosoftTeamsProvider()
    chat = {"kind": "chat", "chat_id": "19:a"}
    channel = {"kind": "channel", "team_id": "t1", "channel_id": "ch1", "root_id": "100"}
    all_chats = ("/users/me/chats/getAllMessages", "created,updated")
    assert provider.reactions_pushed(chat, [all_chats]) is True
    assert provider.reactions_pushed(chat, [("/users/me/chats/getAllMessages", "created")]) is False
    assert provider.reactions_pushed(channel, [all_chats]) is False
    assert provider.reactions_pushed(channel, [("/teams/t1/channels/ch1/messages", "created,updated")]) is True
    assert provider.reactions_pushed(
        channel, [("/teams/t1/channels/ch1/messages?$filter=mentions/any(u: true)", "created")]
    ) is False


async def test_the_reactions_are_read_back_from_the_chat_message_or_the_channel_reply(monkeypatch):
    reacted = {
        "id": "171",
        "reactions": [
            {"reactionType": "like", "user": {"user": {"id": "alice"}}},
            {"reactionType": "\U0001f44e", "user": {"user": {"id": "bob"}}},
            {"reactionType": "custom", "user": {"user": {"id": "carol"}}},
            {"reactionType": "like", "user": {"application": {"id": "bot"}}},
            "not-a-reaction",
        ],
    }
    provider, fake = _provider(
        monkeypatch,
        {
            ("GET", f"{GRAPH}/chats/19:a/messages/171"): reacted,
            ("GET", f"{GRAPH}/teams/t1/channels/ch1/messages/100/replies/171"): {"id": "171", "reactions": []},
            ("GET", f"{GRAPH}/chats/19:a/messages/172"): {"id": "172", "deletedDateTime": "2026-10-08T09:00:00Z"},
        },
    )
    chat = {"kind": "chat", "chat_id": "19:a"}
    assert await provider.read_reactions(_session(), chat, "171") == [
        PlatformReaction("alice", UP),
        PlatformReaction("bob", "\U0001f44e"),
    ]
    channel = {"kind": "channel", "team_id": "t1", "channel_id": "ch1", "root_id": "100"}
    assert await provider.read_reactions(_session(), channel, "171") == []
    # A deleted or vanished message changes nothing.
    assert await provider.read_reactions(_session(), chat, "172") is None
    assert await provider.read_reactions(_session(), chat, "173") is None
    assert [call[0] for call in fake.calls] == ["GET"] * 4


@pytest.mark.parametrize(
    ("age", "interval"),
    [
        pytest.param(timedelta(minutes=10), timedelta(minutes=5), id="first-hour"),
        pytest.param(timedelta(hours=5), timedelta(minutes=30), id="first-day"),
        pytest.param(timedelta(days=3), timedelta(hours=6), id="later"),
    ],
)
def test_an_answer_is_checked_less_often_as_it_ages(age, interval):
    assert reactions.check_interval(age) == interval


async def test_a_notified_change_goes_to_the_reactions_without_a_session_of_its_own(monkeypatch):
    """``handle_notification`` -> ``updated_message``: the reactions read the answer themselves."""
    provider = teams.MicrosoftTeamsProvider()
    monkeypatch.setattr(listening, "get_provider", lambda key: provider)
    handed: list[tuple[str, UpdatedMessage]] = []

    async def _on_updated_message(account_id, updated):
        handed.append((account_id, updated))

    def _no_session():
        raise AssertionError("a change opened a database session in handle_notification")

    monkeypatch.setattr(reactions, "on_updated_message", _on_updated_message)
    monkeypatch.setattr(listening, "async_session", _no_session)
    await listening.handle_notification(
        "acct", provider.key, _notification("teams('t1')/channels('19:c')/messages('100')/replies('171')", "updated")
    )
    assert handed == [("acct", UpdatedMessage("19:c", "171"))]


async def test_a_lifecycle_event_is_never_read_as_a_change(monkeypatch):
    provider = teams.MicrosoftTeamsProvider()
    monkeypatch.setattr(listening, "get_provider", lambda key: provider)
    handed: list = []
    opened: list = []

    async def _on_updated_message(account_id, updated):  # pragma: no cover - must not run
        handed.append(updated)

    class _Session:
        async def __aenter__(self):
            opened.append(True)
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, model, key):
            return None

    monkeypatch.setattr(reactions, "on_updated_message", _on_updated_message)
    monkeypatch.setattr(listening, "async_session", _Session)
    entry = InboundNotification(
        "s1", "c1", "subscriptionRemoved", {"resource": "chats('19:a')/messages('171')", "changeType": "updated"}
    )
    await listening.handle_notification("00000000-0000-0000-0000-000000000001", provider.key, entry)
    assert handed == [] and opened == [True]

