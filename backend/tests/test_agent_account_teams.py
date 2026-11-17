"""Microsoft Teams as an agent-account provider, with Graph answered by a fake."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.services.agent_accounts import providers
from app.services.agent_accounts.events import AccountEvent
from app.services.agent_accounts.providers import InboundNotification, ProviderError, ProviderSession, get_provider
from app.services.agent_accounts.providers import microsoft_graph as graph
from app.services.agent_accounts.providers import microsoft_teams as teams
from app.services.agent_accounts.rules import ChannelTarget, ListeningPlan

GRAPH = teams.GRAPH
ME = "me-id"


class FakeGraph:
    """Answers ``_call`` from a table of ``(method, url) -> body``; records every call."""

    def __init__(self, routes: dict[tuple[str, str], Any]):
        self.routes = routes
        self.calls: list[tuple[str, str, dict | None, dict | None]] = []

    async def __call__(self, session, method, url, *, params=None, payload=None, headers=None, allow_missing=False):
        self.calls.append((method, url, params, payload))
        key = (method, url)
        if key not in self.routes:
            if allow_missing:
                return None
            raise ProviderError(f"unexpected {method} {url}")
        body = self.routes[key]
        return body(params, payload) if callable(body) else body


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch):
    monkeypatch.setattr(teams, "_chats", teams._TTLCache())
    monkeypatch.setattr(teams, "_channels", teams._TTLCache())
    monkeypatch.setattr(teams, "_emails", teams._TTLCache())


def _provider(monkeypatch, routes) -> tuple[teams.MicrosoftTeamsProvider, FakeGraph]:
    provider = teams.MicrosoftTeamsProvider()
    fake = FakeGraph(routes)
    monkeypatch.setattr(provider, "_call", fake)
    return provider, fake


def _session() -> ProviderSession:
    return ProviderSession(token="token", account_external_id=ME)


def _message(msg_id: str, *, author: str = "alice-id", name: str = "Alice", text: str = "Hello",
             mentions: list[str] | None = None, reply_to: str | None = None, minutes: int = 0, app: bool = False):
    sender = {"application": {"id": "bot", "displayName": "Bot"}} if app else {"user": {"id": author, "displayName": name}}
    return {
        "id": msg_id,
        "messageType": "message",
        "replyToId": reply_to,
        "createdDateTime": (datetime(2026, 10, 6, 9, 0, tzinfo=UTC) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"),
        "from": sender,
        "body": {"contentType": "html", "content": f"<p>{text}</p>"},
        "mentions": [{"mentioned": {"user": {"id": m}}} for m in (mentions or [])],
    }


def test_the_provider_is_registered():
    assert get_provider("microsoft_teams").integration_provider == "microsoft"


def test_the_graph_validation_handshake_is_echoed():
    provider = teams.MicrosoftTeamsProvider()
    assert provider.handshake({"validationToken": "abc"}) == "abc"
    assert provider.handshake({}) is None


def test_notifications_are_parsed_and_malformed_ones_refused():
    provider = teams.MicrosoftTeamsProvider()
    body = json.dumps(
        {"value": [
            {"subscriptionId": "s1", "clientState": "c1", "resource": "chats('19:a')/messages('1')", "changeType": "created"},
            {"subscriptionId": "s2", "clientState": "c2", "lifecycleEvent": "reauthorizationRequired"},
            {"subscriptionId": "s3"},
        ]}
    ).encode()
    entries = provider.parse_notifications(body)
    assert [(e.subscription_id, e.lifecycle_event) for e in entries] == [("s1", None), ("s2", "reauthorizationRequired")]
    for bad in (b"[]", b"{}", b'{"value": ["x"]}', b"not json"):
        with pytest.raises(ValueError):
            provider.parse_notifications(bad)


async def test_a_chat_notification_becomes_an_event_read_back_with_the_accounts_token(monkeypatch):
    provider, _fake = _provider(
        monkeypatch,
        {
            ("GET", f"{GRAPH}/chats/19:a@thread.v2/messages/1"): _message("1", mentions=[ME]),
            ("GET", f"{GRAPH}/chats/19:a@thread.v2"): {"chatType": "group", "topic": "Q4"},
            ("GET", f"{GRAPH}/users/alice-id"): {"mail": "alice@example.com"},
        },
    )
    entry = InboundNotification("s1", "c1", None, {"resource": "chats('19:a@thread.v2')/messages('1')", "changeType": "created"})
    event = await provider.event_from_notification(_session(), entry)
    assert event is not None
    assert (event.conversation_type, event.conversation_id, event.thread_id) == ("group", "19:a@thread.v2", "19:a@thread.v2")
    assert event.mentions_account and not event.from_account and not event.from_application
    assert event.sender_email == "alice@example.com" and event.text == "Hello"
    assert event.route == {"kind": "chat", "chat_id": "19:a@thread.v2"}
    assert event.conversation_name == "Q4"


async def test_the_sender_is_known_by_their_mail_address_and_their_sign_in_name(monkeypatch):
    replies = iter([ProviderError("throttled"), {"mail": "alice@example.com", "userPrincipalName": "Alice.Smith@corp.example"}])

    def _user(_params, _payload):
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    provider, fake = _provider(
        monkeypatch,
        {
            ("GET", f"{GRAPH}/chats/c1/messages/1"): _message("1"),
            ("GET", f"{GRAPH}/chats/c1"): {"chatType": "oneOnOne"},
            ("GET", f"{GRAPH}/users/alice-id"): _user,
        },
    )
    entry = InboundNotification("s", "c", None, {"resource": "chats('c1')/messages('1')"})
    # A directory read that failed is not remembered: the next message asks again.
    failed = await provider.event_from_notification(_session(), entry)
    assert failed.sender_email is None and failed.sender_addresses == ()
    event = await provider.event_from_notification(_session(), entry)
    assert event.sender_email == "alice@example.com"
    assert event.sender_addresses == ("alice@example.com", "Alice.Smith@corp.example")
    await provider.event_from_notification(_session(), entry)
    assert sum(1 for _, url, _, _ in fake.calls if url.endswith("/users/alice-id")) == 2


async def test_the_accounts_own_message_and_an_apps_message_are_flagged(monkeypatch):
    provider, _ = _provider(
        monkeypatch,
        {
            ("GET", f"{GRAPH}/chats/c1/messages/1"): _message("1", author=ME, name="Sam"),
            ("GET", f"{GRAPH}/chats/c1/messages/2"): _message("2", app=True),
            ("GET", f"{GRAPH}/chats/c1"): {"chatType": "oneOnOne"},
        },
    )
    own = await provider.event_from_notification(_session(), InboundNotification("s", "c", None, {"resource": "chats('c1')/messages('1')"}))
    app = await provider.event_from_notification(_session(), InboundNotification("s", "c", None, {"resource": "chats('c1')/messages('2')"}))
    assert own.from_account and own.conversation_type == "direct"
    assert app.from_application


async def test_a_channel_reply_takes_its_thread_from_graph_never_from_the_notification(monkeypatch):
    base = f"{GRAPH}/teams/t1/channels/ch1/messages/root"
    provider, _ = _provider(
        monkeypatch,
        {
            ("GET", f"{base}/replies/r1"): _message("r1", reply_to="other-root"),
            ("GET", f"{base}/replies/r2"): _message("r2", reply_to="root"),
            ("GET", f"{GRAPH}/teams/t1"): {"displayName": "Ops"},
            ("GET", f"{GRAPH}/teams/t1/channels/ch1"): {"displayName": "Incidents"},
            ("GET", f"{GRAPH}/users/alice-id"): {"mail": "a@x"},
        },
    )
    forged = InboundNotification("s", "c", None, {"resource": "teams('t1')/channels('ch1')/messages('root')/replies('r1')"})
    assert await provider.event_from_notification(_session(), forged) is None
    real = InboundNotification("s", "c", None, {"resource": "teams('t1')/channels('ch1')/messages('root')/replies('r2')"})
    event = await provider.event_from_notification(_session(), real)
    assert event.conversation_type == "channel" and event.thread_id == "ch1:root"
    assert event.conversation_name == "Ops > Incidents"
    assert event.route == {"kind": "channel", "team_id": "t1", "channel_id": "ch1", "root_id": "root"}


async def test_an_unsafe_id_never_reaches_a_graph_url(monkeypatch):
    provider, fake = _provider(monkeypatch, {})
    entry = InboundNotification("s", "c", None, {"resource": "chats('../me')/messages('1')"})
    with pytest.raises(ProviderError):
        await provider.event_from_notification(_session(), entry)
    assert fake.calls == []


async def test_the_subscriptions_follow_the_listening_plan(monkeypatch):
    provider, _ = _provider(
        monkeypatch,
        {
            ("GET", f"{GRAPH}/me/joinedTeams"): {"value": [{"id": "t1", "displayName": "Ops"}]},
            ("GET", f"{GRAPH}/teams/t1/channels"): {"value": [{"id": "ch1", "displayName": "A"}, {"id": "ch2", "displayName": "B"}]},
        },
    )
    plan = ListeningPlan(
        chats=True,
        channels=(ChannelTarget("ch1", "t1", True, "Ops > A"),),
        all_channels=True,
        all_channels_mentions_only=False,
    )
    desired = await provider.desired_subscriptions(_session(), plan)
    mention = f"$filter=mentions/any(u: u/mentioned/user/id eq '{ME}')"
    assert [d.resource for d in desired] == [
        f"/users/{ME}/chats/getAllMessages",
        f"/teams/t1/channels/ch1/messages?{mention}",
        "/teams/t1/channels/ch2/messages",
    ]
    # What each one is, as the account's owner reads it when the provider refuses it.
    assert [(d.covers, d.name, d.permission) for d in desired] == [
        ("chats", None, "Chat.Read"),
        ("channel", "Ops > A", "ChannelMessage.Read.All"),
        ("channel", "Ops > B", "ChannelMessage.Read.All"),
    ]


async def test_every_channel_the_rules_cover_but_no_subscription_hears_is_named_with_its_cause(monkeypatch):
    # 52 channels in one team, a team whose channels cannot be listed, a named channel without its team.
    channels = [{"id": f"ch{i}", "displayName": f"C{i}"} for i in range(teams.MAX_DISCOVERED_CHANNELS + 2)]

    def _refused(params, payload):
        raise ProviderError("Microsoft Graph answered 403: Forbidden", kind="refused")

    provider, _ = _provider(
        monkeypatch,
        {
            ("GET", f"{GRAPH}/me/joinedTeams"): {"value": [
                {"id": "t1", "displayName": "Filigran"}, {"id": "t2", "displayName": "Locked"},
            ]},
            ("GET", f"{GRAPH}/teams/t1/channels"): {"value": channels},
            ("GET", f"{GRAPH}/teams/t2/channels"): _refused,
        },
    )
    plan = ListeningPlan(channels=(ChannelTarget("orphan", None, True, "Old > Orphan"),), all_channels=True)
    wanted, unheard = await provider.desired_listening(_session(), plan)
    assert len(wanted) == teams.MAX_DISCOVERED_CHANNELS
    assert [u.name for u in unheard] == [
        "Old > Orphan",
        f"Filigran > C{teams.MAX_DISCOVERED_CHANNELS}",
        f"Filigran > C{teams.MAX_DISCOVERED_CHANNELS + 1}",
        "Locked",
    ]
    assert [u.covers for u in unheard] == ["channel", "channel", "channel", "team"]
    assert all(u.detail for u in unheard)
    assert "first 50 channels" in unheard[1].detail and "refused to list" in unheard[3].detail
    # The same reasons as codes the frontend words in the viewer's language.
    assert [u.note for u in unheard] == [
        "channel_without_team", "past_channel_cap", "past_channel_cap", "team_channels_unlisted",
    ]
    assert unheard[1].note_values == {"limit": teams.MAX_DISCOVERED_CHANNELS}
    # The subscriptions alone are the same list.
    assert [d.resource for d in await provider.desired_subscriptions(_session(), plan)] == [d.resource for d in wanted]


async def test_a_reply_to_a_root_post_is_read_in_its_thread_and_routed_back_to_it(monkeypatch):
    # The thread a tool reply went to: its follow-ups come from the root post's replies.
    base = f"{GRAPH}/teams/t1/channels/ch1/messages/1792649781277"
    provider, _ = _provider(
        monkeypatch,
        {
            ("GET", f"{base}/replies"): {"value": [
                _message("1793615178741", reply_to="1792649781277", text="one more", minutes=86),
                _message("1793615154387", reply_to="1792649781277", text="follow-up", minutes=85),
                _message("1793614978045", author=ME, name="Me", reply_to="1792649781277", text="owner", minutes=82),
            ]},
            ("GET", f"{GRAPH}/teams/t1"): {"displayName": "Filigran"},
            ("GET", f"{GRAPH}/teams/t1/channels/ch1"): {"displayName": "AI for Enterprise"},
            ("GET", f"{GRAPH}/users/alice-id"): {"mail": "a@x"},
            ("GET", f"{GRAPH}/users/{ME}"): {"mail": "me@x"},
        },
    )
    route = {"kind": "channel", "team_id": "t1", "channel_id": "ch1", "root_id": "1792649781277"}
    events = await provider.thread_messages_since(_session(), route, datetime(2026, 10, 6, 10, 22, tzinfo=UTC))
    others = [e for e in events if not e.from_account]
    assert [e.message_id for e in others] == ["1793615154387", "1793615178741"]
    assert {e.thread_id for e in others} == {"ch1:1792649781277"}
    assert all(e.route == route for e in others)


class _Response:
    def __init__(self, status: int, body: Any = None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


@pytest.mark.parametrize(
    ("status", "kind"),
    [(401, "reauthorize"), (403, "refused"), (404, "not_found"), (429, "throttled"), (503, "unavailable"), (400, "error")],
)
def test_a_graph_failure_says_what_can_be_done_from_its_status_and_keeps_graphs_words(status, kind):
    words = "Operation: Create; Exception: [Status Code: Forbidden; Reason: anything]"
    error = graph._graph_error(_Response(status, {"error": {"code": "ExtensionError", "message": words}}))
    assert error.kind == kind
    assert str(error) == f"Microsoft Graph answered {status}: {words}"
    assert graph._graph_error(_Response(status)).kind == kind


def test_a_conversation_is_one_slot_whatever_filter_or_spelling_its_resource_has():
    provider = teams.MicrosoftTeamsProvider()
    mention = f"/teams/T1/channels/19:A@thread.tacv2/messages?$filter=mentions/any(u: u/mentioned/user/id eq '{ME}')"
    assert provider.subscription_slot(mention) == provider.subscription_slot("teams/t1/channels/19:a@thread.tacv2/messages")
    assert provider.subscription_slot(mention) != provider.subscription_slot("/teams/t1/channels/other/messages")


async def test_a_followed_thread_brings_its_new_replies_oldest_first(monkeypatch):
    base = f"{GRAPH}/teams/t1/channels/ch1/messages/root"
    provider, fake = _provider(
        monkeypatch,
        {
            ("GET", f"{base}/replies"): {"value": [
                _message("r3", reply_to="root", text="and now?", minutes=30),
                _message("r2", reply_to="root", text="thanks", minutes=20),
                _message("r1", reply_to="root", text="before the answer", minutes=5),
                _message("x", reply_to="elsewhere", minutes=25),
            ]},
            ("GET", f"{GRAPH}/teams/t1"): {"displayName": "Ops"},
            ("GET", f"{GRAPH}/teams/t1/channels/ch1"): {"displayName": "Incidents"},
            ("GET", f"{GRAPH}/users/alice-id"): {"mail": "a@x"},
        },
    )
    route = {"kind": "channel", "team_id": "t1", "channel_id": "ch1", "root_id": "root"}
    since = datetime(2026, 10, 6, 9, 10, tzinfo=UTC)
    events = await provider.thread_messages_since(_session(), route, since)
    assert [(e.message_id, e.thread_id, e.conversation_type) for e in events] == [
        ("r2", "ch1:root", "channel"), ("r3", "ch1:root", "channel"),
    ]
    assert events[0].route == route and events[0].conversation_name == "Ops > Incidents"
    assert await provider.thread_messages_since(_session(), {"kind": "chat", "chat_id": "c1"}, since) == []
    assert all(url.startswith(GRAPH) for _, url, _, _ in fake.calls)


async def test_a_subscription_needs_an_https_address(monkeypatch):
    provider, fake = _provider(monkeypatch, {})
    with pytest.raises(ProviderError, match="HTTPS"):
        await provider.create_subscription(
            _session(), "/users/x/chats/getAllMessages",
            notification_url="http://localhost/x", lifecycle_url="http://localhost/x", client_state="s",
        )
    assert fake.calls == []


async def test_a_subscription_is_created_with_a_lifecycle_url_and_no_resource_data(monkeypatch):
    provider, fake = _provider(
        monkeypatch, {("POST", f"{GRAPH}/subscriptions"): {"id": "sub-1", "expirationDateTime": "2026-10-09T09:00:00Z"}}
    )
    created = await provider.create_subscription(
        _session(), "/users/x/chats/getAllMessages",
        notification_url="https://p/x", lifecycle_url="https://p/x?lifecycle=1", client_state="secret",
    )
    assert created.external_id == "sub-1"
    payload = fake.calls[0][3]
    assert payload["lifecycleNotificationUrl"] == "https://p/x?lifecycle=1"
    assert payload["includeResourceData"] is False and payload["clientState"] == "secret"
    assert payload["changeType"] == "created"


async def test_a_reply_goes_to_the_chat_or_the_channel_thread(monkeypatch):
    provider, fake = _provider(
        monkeypatch,
        {
            ("POST", f"{GRAPH}/chats/c1/messages"): {"id": "new-1"},
            ("POST", f"{GRAPH}/teams/t1/channels/ch1/messages/root/replies"): {"id": "new-2"},
        },
    )
    assert await provider.send_reply(_session(), {"kind": "chat", "chat_id": "c1"}, "**Hi**") == "new-1"
    assert await provider.send_reply(
        _session(), {"kind": "channel", "team_id": "t1", "channel_id": "ch1", "root_id": "root"}, "Hi"
    ) == "new-2"
    body = fake.calls[0][3]["body"]
    assert body["contentType"] == "html" and "<strong>Hi</strong>" in body["content"]


FILE_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
ETAG = '"{11111111-2222-3333-4444-555555555555},1"'
CHAT_ITEM = f"{GRAPH}/me/drive/items/item-1"


def test_file_markers_are_split_from_the_text_each_id_once():
    text, ids = providers.split_file_markers(
        f"Here is the report.\n\n[[FILE:{FILE_ID.upper()}]]\n[[file:{FILE_ID}]]"
    )
    assert text == "Here is the report."
    assert ids == [FILE_ID]
    assert providers.split_file_markers("No file") == ("No file", [])


def test_a_reference_attachment_needs_the_etag_guid_and_a_url():
    item = {"eTag": ETAG, "webDavUrl": "https://x/dav/r.pdf", "webUrl": "https://x/r.pdf", "name": "r.pdf"}
    assert teams.reference_attachment(item) == {
        "id": "11111111-2222-3333-4444-555555555555",
        "contentType": "reference",
        "contentUrl": "https://x/dav/r.pdf",
        "name": "r.pdf",
    }
    assert teams.reference_attachment({"webUrl": "https://x/r.pdf"}) is None
    assert teams.reference_attachment({"eTag": ETAG}) is None


def _chat_file_routes(granted: list[str]) -> dict:
    return {
        ("GET", CHAT_ITEM): {"id": "item-1", "name": "r.pdf", "eTag": ETAG, "webDavUrl": "https://x/dav/r.pdf"},
        ("GET", f"{GRAPH}/chats/c1/members"): {"value": [
            {"userId": ME, "displayName": "Agent"},
            {"userId": "alice-id", "displayName": "Alice", "email": "alice@contoso.com"},
        ]},
        ("POST", f"{CHAT_ITEM}/invite"): {"value": []},
        ("GET", f"{CHAT_ITEM}/permissions"): {"value": [
            {"roles": ["read"], "grantedToV2": {"user": {"id": uid}}} for uid in granted
        ]},
        ("POST", f"{GRAPH}/chats/c1/messages"): {"id": "new-1"},
    }


def _uploads(monkeypatch, provider, answer=None):
    puts: list[str] = []

    async def fake_put(session, url, data, content_type):
        puts.append(url)
        if answer is not None:
            raise answer
        return {"id": "item-1"}

    monkeypatch.setattr(provider, "_put_bytes", fake_put)
    return puts


async def test_a_reply_file_is_shared_with_the_chat_and_posted_as_a_card(monkeypatch):
    provider, fake = _provider(monkeypatch, _chat_file_routes(["alice-id"]))
    puts = _uploads(monkeypatch, provider)
    file = providers.ReplyFile(name="r.pdf", content_type="application/pdf", data=b"%PDF")
    sent = await provider.send_reply_files(_session(), {"kind": "chat", "chat_id": "c1"}, "Here it is", [file])
    assert sent == "new-1"
    assert puts == [f"{GRAPH}/me/drive/root:/Microsoft Teams Chat Files/r.pdf:/content"]
    order = [(method, url) for method, url, _params, _payload in fake.calls]
    assert order.index(("POST", f"{CHAT_ITEM}/invite")) < order.index(("POST", f"{GRAPH}/chats/c1/messages"))
    invite = next(p for m, u, _q, p in fake.calls if u.endswith("/invite"))
    assert invite["recipients"] == [{"objectId": "alice-id"}] and invite["sendInvitation"] is False
    message = fake.calls[-1][3]
    assert message["attachments"][0]["contentType"] == "reference"
    assert '<attachment id="11111111-2222-3333-4444-555555555555"></attachment>' in message["body"]["content"]
    assert "Here it is" in message["body"]["content"] and "[[FILE" not in message["body"]["content"]


async def test_a_reply_file_nobody_can_open_is_named_in_a_plain_line(monkeypatch):
    provider, fake = _provider(monkeypatch, _chat_file_routes([]))
    _uploads(monkeypatch, provider)
    file = providers.ReplyFile(name="r.pdf", content_type="application/pdf", data=b"%PDF")
    await provider.send_reply_files(_session(), {"kind": "chat", "chat_id": "c1"}, "Here it is", [file])
    message = fake.calls[-1][3]
    assert "attachments" not in message
    assert "File not attached: r.pdf" in message["body"]["content"]


async def test_a_reply_file_that_cannot_be_uploaded_or_read_is_named(monkeypatch):
    provider, fake = _provider(monkeypatch, {("POST", f"{GRAPH}/chats/c1/messages"): {"id": "new-1"}})
    _uploads(monkeypatch, provider, ProviderError("Microsoft Graph answered 507"))
    files = [
        providers.ReplyFile(name="big.pdf", content_type="application/pdf", data=b"%PDF"),
        providers.ReplyFile(name="gone.pdf", content_type="application/pdf", data=b""),
    ]
    await provider.send_reply_files(_session(), {"kind": "chat", "chat_id": "c1"}, "", files)
    content = fake.calls[-1][3]["body"]["content"]
    assert "File not attached: big.pdf" in content and "File not attached: gone.pdf" in content


async def test_a_channel_reply_file_goes_to_the_channel_files_folder(monkeypatch):
    item = f"{GRAPH}/drives/d1/items/item-1"
    provider, fake = _provider(monkeypatch, {
        ("GET", f"{GRAPH}/teams/t1/channels/ch1/filesFolder"): {"id": "folder-1", "parentReference": {"driveId": "d1"}},
        ("GET", item): {"id": "item-1", "name": "r.pdf", "eTag": ETAG, "webUrl": "https://x/r.pdf"},
        ("POST", f"{GRAPH}/teams/t1/channels/ch1/messages/root/replies"): {"id": "new-2"},
    })
    puts = _uploads(monkeypatch, provider)
    file = providers.ReplyFile(name="r.pdf", content_type="application/pdf", data=b"%PDF")
    route = {"kind": "channel", "team_id": "t1", "channel_id": "ch1", "root_id": "root"}
    assert await provider.send_reply_files(_session(), route, "Report", [file]) == "new-2"
    assert puts == [f"{GRAPH}/drives/d1/items/folder-1:/r.pdf:/content"]
    assert not any(u.endswith("/invite") for _m, u, _q, _p in fake.calls)
    assert fake.calls[-1][3]["attachments"][0]["contentUrl"] == "https://x/r.pdf"


async def test_the_default_provider_names_each_file_in_a_plain_line(monkeypatch):
    provider, fake = _provider(monkeypatch, {("POST", f"{GRAPH}/chats/c1/messages"): {"id": "new-1"}})
    file = providers.ReplyFile(name="r.pdf", content_type="application/pdf", data=b"%PDF")
    sent = await providers.AccountProvider.send_reply_files(
        provider, _session(), {"kind": "chat", "chat_id": "c1"}, "Hi", [file]
    )
    assert sent == "new-1"
    assert "File not attached: r.pdf" in fake.calls[-1][3]["body"]["content"]


async def test_the_context_is_the_thread_before_the_message_oldest_first(monkeypatch):
    provider, _ = _provider(
        monkeypatch,
        {
            ("GET", f"{GRAPH}/chats/c1/messages"): {"value": [
                _message("3", text="later", minutes=10),
                _message("2", author=ME, name="Sam", text="mine", minutes=2),
                _message("1", text="first", minutes=1),
                {"id": "x", "messageType": "systemEventMessage", "createdDateTime": "2026-10-06T09:00:00Z"},
            ]},
        },
    )
    event = AccountEvent(
        provider="microsoft_teams", conversation_type="direct", conversation_id="c1", thread_id="c1",
        message_id="9", sender_id="alice-id", sender_name="Alice", text="now",
        created_at=datetime(2026, 10, 6, 9, 5, tzinfo=UTC), route={"kind": "chat", "chat_id": "c1"},
    )
    context = await provider.fetch_context(_session(), event, 10)
    assert [(m.text, m.from_account) for m in context] == [("first", False), ("mine", True)]
    assert await provider.account_messages_since(_session(), event) == []


def _with_files(msg: dict, *, chat_id: str = "19:a@thread.v2") -> dict:
    """*msg* with a shared file and an image pasted into it, as Graph returns them."""
    hosted = f"{GRAPH}/chats/{chat_id}/messages/{msg['id']}/hostedContents/aWQ9eF8wLXd1cy1kMTA=/$value"
    msg["body"] = {"contentType": "html", "content": f'<p>What does this show?</p><img src="{hosted}">'}
    msg["attachments"] = [
        {"id": "a1", "contentType": "reference", "name": "plan.pdf",
         "contentUrl": "https://contoso.sharepoint.com/sites/x/Shared%20Documents/plan.pdf"},
        {"id": "a2", "contentType": "application/vnd.microsoft.card.adaptive", "content": "{}"},
    ]
    return msg


async def test_a_shared_file_and_a_pasted_image_ride_on_the_event(monkeypatch):
    """What a person shares in the chat reaches the agent, read with the account's token."""
    provider, _ = _provider(
        monkeypatch,
        {
            ("GET", f"{GRAPH}/chats/19:a@thread.v2/messages/1"): _with_files(_message("1")),
            ("GET", f"{GRAPH}/chats/19:a@thread.v2"): {"chatType": "oneOnOne"},
            ("GET", f"{GRAPH}/users/alice-id"): {"mail": "alice@example.com"},
        },
    )
    entry = InboundNotification("s1", "c1", None, {"resource": "chats('19:a@thread.v2')/messages('1')"})
    event = await provider.event_from_notification(_session(), entry)
    assert event is not None and "What does this show?" in event.text
    shared, pasted = event.attachments
    assert shared.name == "plan.pdf" and shared.content_type == "application/pdf"
    assert shared.url.startswith(f"{GRAPH}/shares/u!") and shared.url.endswith("/driveItem/content")
    assert pasted.url == (
        f"{GRAPH}/chats/19:a@thread.v2/messages/1/hostedContents/aWQ9eF8wLXd1cy1kMTA%3D/$value"
    )
    assert pasted.name.startswith("teams-image-") and pasted.content_type == "application/octet-stream"
    # The event survives the grace period's round trip through Redis.
    assert AccountEvent.from_dict(json.loads(json.dumps(event.to_dict()))).attachments == event.attachments


async def test_the_accounts_own_files_and_another_messages_images_are_not_taken(monkeypatch):
    provider, _ = _provider(monkeypatch, {("GET", f"{GRAPH}/chats/c1"): {"chatType": "oneOnOne"}})
    own = await provider._chat_event(_session(), "c1", _with_files(_message("2", author=ME), chat_id="c1"))
    assert own is not None and own.attachments == ()
    # An image of another message (a quote) is that message's, not this one's.
    other = _with_files(_message("3"), chat_id="c1")
    other["body"]["content"] = other["body"]["content"].replace("/messages/3/", "/messages/99/")
    other["attachments"] = []
    event = await provider._chat_event(_session(), "c1", other)
    assert event is not None and event.attachments == ()


async def test_an_attachment_is_downloaded_as_the_account_under_the_cap(monkeypatch):
    from app.integrations.file_delivery import FetchedFile, FetchError
    from app.services.agent_accounts.events import AccountAttachment

    seen: list[tuple[str, int, bool]] = []

    async def fetch(client, method, url, *, provider, action, max_bytes, follow_redirects=None, **kwargs):
        seen.append((url, max_bytes, bool(follow_redirects)))
        if url.endswith("/big"):
            return FetchError("Error: the Microsoft Teams file is over the 1 MB download limit.", too_large=True)
        return FetchedFile(data=b"%PDF-1.7", content_type="application/pdf", filename="")

    monkeypatch.setattr(teams, "fetch_file", fetch)
    provider = teams.MicrosoftTeamsProvider()
    data, declared = await provider.download_attachment(
        _session(), AccountAttachment(name="plan.pdf", url=f"{GRAPH}/shares/u!x/driveItem/content"), 1024
    )
    assert (data, declared) == (b"%PDF-1.7", "application/pdf")
    assert seen == [(f"{GRAPH}/shares/u!x/driveItem/content", 1024, True)]
    with pytest.raises(ProviderError, match="over the 1 MB"):
        await provider.download_attachment(_session(), AccountAttachment(name="x", url=f"{GRAPH}/big"), 1024)
    # The account's token never goes anywhere but Graph.
    with pytest.raises(ProviderError, match="not a Microsoft Graph address"):
        await provider.download_attachment(
            _session(), AccountAttachment(name="x", url="https://attacker.example/x"), 1024
        )
    assert len(seen) == 2


async def test_a_poll_starts_now_and_then_reads_what_is_newer(monkeypatch):
    provider, _ = _provider(
        monkeypatch,
        {
            ("GET", f"{GRAPH}/me/chats"): {"value": [
                {"id": "c1", "chatType": "oneOnOne", "lastMessagePreview": {"createdDateTime": "2026-10-06T09:10:00Z"}},
                {"id": "c2", "chatType": "group", "lastMessagePreview": {"createdDateTime": "2026-10-06T08:00:00Z"}},
            ]},
            ("GET", f"{GRAPH}/chats/c1/messages"): {"value": [_message("5", minutes=10), _message("4", minutes=-30)]},
            ("GET", f"{GRAPH}/users/alice-id"): {"mail": "a@x"},
        },
    )
    plan = ListeningPlan(chats=True)
    events, cursor = await provider.poll(_session(), plan, None)
    assert events == [] and cursor
    events, cursor = await provider.poll(_session(), plan, "2026-10-06T09:00:00+00:00")
    assert [e.message_id for e in events] == ["5"] and cursor.startswith("2026-10-06T09:10")


def _answered(route: dict, message_id: str = "m2") -> AccountEvent:
    return AccountEvent(
        provider="microsoft_teams",
        conversation_type="group",
        conversation_id="conv",
        thread_id="conv",
        message_id=message_id,
        sender_id="alice-id",
        sender_name="Alice",
        text="ok?",
        created_at=datetime(2026, 10, 6, 9, 5, tzinfo=UTC),
        route=route,
    )


def test_the_answered_chat_message_is_referenced_by_the_reaction_tools_arguments():
    provider = get_provider("microsoft_teams")
    assert provider.message_reference(_answered({"kind": "chat", "chat_id": "19:g@thread.v2"})) == {
        "chat_id": "19:g@thread.v2"
    }


def test_a_channel_reply_names_its_root_and_a_root_post_does_not():
    provider = get_provider("microsoft_teams")
    route = {"kind": "channel", "team_id": "t1", "channel_id": "19:c@thread.tacv2", "root_id": "r1"}
    assert provider.message_reference(_answered(route, message_id="m2")) == {
        "team_id": "t1",
        "channel_id": "19:c@thread.tacv2",
        "parent_message_id": "r1",
    }
    assert provider.message_reference(_answered(route, message_id="r1")) == {
        "team_id": "t1",
        "channel_id": "19:c@thread.tacv2",
    }
    assert provider.message_reference(_answered({})) == {}
