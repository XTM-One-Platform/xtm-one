"""Real Teams mentions in an agent account's conversational reply (``teams_mentions``)."""

from __future__ import annotations

from app.services.agent_accounts import teams_mentions as tm
from app.services.agent_accounts.conversation import account_brief
from app.services.agent_accounts.providers import ProviderSession
from app.services.agent_accounts.providers import microsoft_teams as teams
from tests.test_agent_account_teams import FakeGraph

GRAPH = teams.GRAPH
ME = "me-id"

MEMBERS = [
    {"userId": ME, "displayName": "Agent"},
    {"userId": "sahar-id", "displayName": "Sahar Kassem", "email": "sahar@contoso.com"},
    {"userId": "jane-id", "displayName": "Jane Doe", "email": "jane@contoso.com"},
    {"userId": "jane2-id", "displayName": "Jane Roe", "email": "roe@contoso.com"},
]


def _members() -> list[tm.Member]:
    return tm.members_of(MEMBERS, ME)


def test_a_unique_first_name_a_full_name_and_an_email_become_mentions():
    resolved = tm.resolve_reply_mentions(
        "@Sahar, the deck is ready. @Jane Doe and @roe@contoso.com, please review.", _members()
    )
    assert resolved.text == '<at id="0"/>, the deck is ready. <at id="1"/> and <at id="2"/>, please review.'
    assert [(m.id, m.display_name) for m in resolved.mentions] == [
        ("sahar-id", "Sahar"),
        ("jane-id", "Jane Doe"),
        ("jane2-id", "Jane Roe"),
    ]
    assert resolved.report() == {
        "mentions_resolved": ["Sahar", "Jane Doe", "Jane Roe"],
        "mentions_unresolved": [],
    }


def test_an_unknown_or_ambiguous_name_stays_plain_text_never_a_wrong_person():
    resolved = tm.resolve_reply_mentions("@Jane and @Sahra, see above.", _members())
    assert resolved.text == "@Jane and @Sahra, see above."
    assert resolved.mentions == []
    assert resolved.unresolved == ["@Jane", "@Sahra"]


def test_the_account_itself_code_and_email_addresses_in_text_are_never_mentioned():
    resolved = tm.resolve_reply_mentions(
        "Write to sahar@contoso.com, run `@Sahar` or\n```\n@Jane Doe\n```\n@Agent", _members()
    )
    assert resolved.mentions == []
    assert "`@Sahar`" in resolved.text and "@Jane Doe" in resolved.text
    assert resolved.unresolved == ["@Agent"]


def test_a_text_without_a_token_or_with_its_own_markers_is_left_alone():
    assert not tm.may_mention("No mention here, mail sahar@contoso.com.")
    assert not tm.may_mention('Hi <at id="0">Sahar</at>, @Jane')
    assert tm.may_mention("Hi @Sahar")
    plain = tm.resolve_reply_mentions("No mention here.", _members())
    assert plain.text == "No mention here." and plain.report() == {"mentions_resolved": [], "mentions_unresolved": []}


def test_a_name_must_end_on_a_word_boundary():
    resolved = tm.resolve_reply_mentions("@Saharan dunes", _members())
    assert resolved.mentions == [] and resolved.unresolved == ["@Saharan"]


def _provider(monkeypatch, routes):
    provider = teams.MicrosoftTeamsProvider()
    fake = FakeGraph(routes)
    monkeypatch.setattr(provider, "_call", fake)
    return provider, fake


async def test_a_channel_reply_carries_real_mentions_and_reports_them(monkeypatch):
    provider, fake = _provider(
        monkeypatch,
        {
            ("GET", f"{GRAPH}/teams/t1/channels/ch1/members"): {"value": MEMBERS},
            ("POST", f"{GRAPH}/teams/t1/channels/ch1/messages/root/replies"): {"id": "new-1"},
        },
    )
    session = ProviderSession(token="token", account_external_id=ME)
    route = {"kind": "channel", "team_id": "t1", "channel_id": "ch1", "root_id": "root"}
    assert await provider.send_reply(session, route, "@Sahar, this edition's deck is ready. @Jne too.") == "new-1"
    payload = fake.calls[-1][3]
    assert '<at id="0">Sahar</at>' in payload["body"]["content"]
    assert "@Jne" in payload["body"]["content"]
    assert payload["mentions"] == [
        {
            "id": 0,
            "mentionText": "Sahar",
            "mentioned": {"user": {"id": "sahar-id", "displayName": "Sahar", "userIdentityType": "aadUser"}},
        }
    ]
    assert session.delivery == {"mentions_resolved": ["Sahar"], "mentions_unresolved": ["@Jne"]}


async def test_a_group_chat_reply_carries_real_mentions(monkeypatch):
    provider, fake = _provider(
        monkeypatch,
        {
            ("GET", f"{GRAPH}/chats/c1/members"): {"value": MEMBERS},
            ("POST", f"{GRAPH}/chats/c1/messages"): {"id": "new-2"},
        },
    )
    session = ProviderSession(token="token", account_external_id=ME)
    await provider.send_reply(session, {"kind": "chat", "chat_id": "c1"}, "Thanks @Jane Doe")
    payload = fake.calls[-1][3]
    assert payload["body"]["content"].count("<at id=") == 1
    assert payload["mentions"][0]["mentioned"]["user"]["id"] == "jane-id"


async def test_a_reply_without_a_token_reads_no_member_and_is_unchanged(monkeypatch):
    provider, fake = _provider(monkeypatch, {("POST", f"{GRAPH}/chats/c1/messages"): {"id": "new-3"}})
    session = ProviderSession(token="token", account_external_id=ME)
    await provider.send_reply(session, {"kind": "chat", "chat_id": "c1"}, "**Hi** all")
    assert [(method, url) for method, url, _p, _b in fake.calls] == [("POST", f"{GRAPH}/chats/c1/messages")]
    assert "mentions" not in fake.calls[-1][3] and session.delivery == {}


async def test_members_that_cannot_be_read_leave_the_tokens_as_text(monkeypatch):
    provider, fake = _provider(monkeypatch, {("POST", f"{GRAPH}/chats/c1/messages"): {"id": "new-4"}})
    session = ProviderSession(token="token", account_external_id=ME)
    await provider.send_reply(session, {"kind": "chat", "chat_id": "c1"}, "@Sahar hello")
    payload = fake.calls[-1][3]
    assert "mentions" not in payload and "@Sahar" in payload["body"]["content"]
    assert session.delivery == {"mentions_resolved": [], "mentions_unresolved": ["@Sahar"]}


def test_the_teams_brief_says_how_to_mention():
    brief = account_brief(
        mode="ai_user",
        platform_label="Microsoft Teams",
        account_name="Agent",
        conversation_name="EMT",
        instructions="",
        mention_hint=teams.MicrosoftTeamsProvider.reply_mention_hint,
    )
    assert "real mention" in brief and "@Jane Doe" in brief
    assert "real mention" not in account_brief(
        mode="ai_user", platform_label="Gmail", account_name="Agent", conversation_name=None, instructions=""
    )
