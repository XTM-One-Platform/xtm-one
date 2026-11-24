"""Sending the Google Chat decision card (#842, S11 slice 2, human-gates.md I.4 path (d)), without a database.

The notify ask ``channel: google_chat`` names a space or a person; the Chat app
posts the cardsV2 card there (app auth, in a space or the app's DM with the
person), and where the app cannot, the ask's Google integration posts the
question with the signed answer links as a user. An answer given anywhere
else edits every card the app posted.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import google_chat_decision_cards as cards
from app.services import work_item_notify as notify

RECIPIENT = uuid.UUID("11111111-1111-1111-1111-111111111111")
AGENT = uuid.UUID("22222222-2222-2222-2222-222222222222")


class _Db:
    def __init__(self, details=()):
        self.added: list = []
        self._details = list(details)
        self.commit = AsyncMock()

    def add(self, row):
        self.added.append(row)

    async def execute(self, _statement):
        details = self._details
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: details))


def _decision(**fields):
    values = {"id": uuid.uuid4(), "recipient": f"user:{RECIPIENT}", "status": "open", "public_id": "work_item:x"}
    values.update(fields)
    return SimpleNamespace(**values)


def _item(notify_spec):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        created_by_agent_id=AGENT,
        owner_agent_id=None,
        payload={"notify": notify_spec},
    )


# --- The notify ask --------------------------------------------------------------


def test_a_google_chat_ask_names_a_space_or_a_person_and_needs_no_microsoft_integration():
    space = notify.normalize_notify_payload({"notify": {"channel": "google_chat", "space": "AAAA1"}})
    assert space["notify"] == {"channel": "google_chat", "space": "spaces/AAAA1"}
    person = notify.normalize_notify_payload(
        {"notify": {"channel": "google_chat", "user": "ada@example.com", "integration": "Google"}}
    )
    assert person["notify"] == {"channel": "google_chat", "user": "ada@example.com", "integration": "Google"}


@pytest.mark.parametrize(
    "spec",
    [
        {"channel": "google_chat"},
        {"channel": "google_chat", "space": "spaces/../x"},
        {"channel": "google_chat", "space": "spaces/A", "user": "ada@example.com"},
        {"channel": "google_chat", "user": "not an address"},
    ],
)
def test_a_google_chat_ask_without_one_valid_target_is_refused(spec):
    with pytest.raises(notify.NotifyError):
        notify.normalize_notify_payload({"notify": spec})


def test_a_microsoft_channel_still_needs_its_integration():
    with pytest.raises(notify.NotifyError, match="Microsoft integration"):
        notify.normalize_notify_payload({"notify": {"channel": "teams_dm", "user": "ada@example.com"}})


def test_the_target_label_names_the_space_or_the_person():
    assert notify.target_label({"channel": "google_chat", "space": "spaces/A"}) == "the Google Chat space spaces/A"
    assert notify.target_label({"channel": "google_chat", "user": "a@b.c"}) == "a@b.c on Google Chat"


# --- Resource names ---------------------------------------------------------------


def test_only_documented_resource_names_reach_a_url():
    assert cards.space_name("AAAA") == "spaces/AAAA"
    assert cards.space_name("spaces/A/../../x") is None
    linked = SimpleNamespace(channel_identities={"whatsapp:+1": True, "google_chat:users/123": True})
    assert cards.chat_user_name(linked) == "users/123"
    assert cards.chat_user_name(SimpleNamespace(channel_identities={"google_chat:users/../x": True})) is None
    assert cards.chat_user_name(SimpleNamespace(channel_identities=None)) is None


@pytest.mark.asyncio
async def test_the_dm_lookup_refuses_a_bad_user_name_without_a_call(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr(cards.httpx, "AsyncClient", client)
    assert await cards.direct_message_space("t", "users/../spaces") is None
    client.assert_not_called()


# --- App auth: the card, in a space or the app's DM --------------------------------


@pytest.mark.asyncio
async def test_a_sent_card_is_recorded_with_its_message_so_an_answer_elsewhere_edits_it(monkeypatch):
    monkeypatch.setattr(cards, "load_view", AsyncMock(return_value="view"))
    monkeypatch.setattr(cards, "render_google_chat_decision", lambda view, language: {"text": "q", "cardsV2": []})
    monkeypatch.setattr(cards, "post_message", AsyncMock(return_value={"name": "spaces/A/messages/m1"}))
    db = _Db()
    assert await cards.send_decision_card(db, _decision(), "tok", "spaces/A", language="fr")
    (event,) = db.added
    assert event.event == "notified" and event.surface == cards.SURFACE
    assert event.detail == {
        "via": cards.VIA_CARD,
        "space": "spaces/A",
        "message_name": "spaces/A/messages/m1",
        "language": "fr",
    }


@pytest.mark.asyncio
async def test_a_person_without_a_linked_chat_identity_gets_no_app_card(monkeypatch):
    token = AsyncMock(return_value="tok")
    monkeypatch.setattr(cards, "app_token", token)
    user = SimpleNamespace(channel_identities={})
    assert await cards.send_to_person(_Db(), _decision(), user, language="en") is False
    token.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_person_gets_the_card_in_the_apps_dm(monkeypatch):
    monkeypatch.setattr(cards, "app_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(cards, "direct_message_space", AsyncMock(return_value="spaces/DM1"))
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(cards, "send_decision_card", send)
    user = SimpleNamespace(channel_identities={"google_chat:users/9": True})
    assert await cards.send_to_person(_Db(), _decision(), user, language="en")
    assert send.await_args.args[2:] == ("tok", "spaces/DM1")


@pytest.mark.asyncio
async def test_a_work_item_ask_naming_a_space_gets_the_apps_card(monkeypatch):
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(cards, "send_to_space", send)
    item = _item({"channel": "google_chat", "space": "spaces/ROOM"})
    assert await cards.deliver_work_item_card(_Db(), _decision(), item)
    assert send.await_args.args[2] == "spaces/ROOM"


# --- User auth: the link message where the app is not ----------------------------


def _caller(answers: list, calls: list):
    def factory(sender, integration, provider="microsoft"):
        @asynccontextmanager
        async def context():
            async def call(tool, arguments):
                calls.append((sender, integration, provider, tool, arguments))
                return answers.pop(0)

            yield call

        return context()

    return factory


@pytest.mark.asyncio
async def test_where_the_app_is_not_the_google_integration_posts_the_answer_links_as_a_user(monkeypatch):
    monkeypatch.setattr(cards, "send_to_space", AsyncMock(return_value=False))
    monkeypatch.setattr(cards, "link_message", lambda decision, recipient, language=None: f"links for {recipient}")
    calls: list = []
    db = _Db()
    item = _item({"channel": "google_chat", "space": "spaces/ROOM", "integration": "Google"})
    sent = await cards.deliver_work_item_card(
        db, _decision(), item, caller=_caller([(True, "Message sent to spaces/ROOM.")], calls)
    )
    assert sent
    ((sender, integration, provider, tool, arguments),) = calls
    assert (integration, provider, tool) == ("Google", "google", "send_google_chat_message")
    assert sender.created_by_agent_id == AGENT
    assert arguments == {"space_name": "spaces/ROOM", "message": f"links for {RECIPIENT}"}
    (event,) = db.added
    assert event.surface == cards.LINK_SURFACE and event.detail == {
        "via": cards.VIA_LINKS,
        "space": "spaces/ROOM",
        "sent": True,
    }


@pytest.mark.asyncio
async def test_no_app_and_no_integration_is_recorded_as_a_permanent_refusal(monkeypatch):
    monkeypatch.setattr(cards, "app_space_for", AsyncMock(return_value=None))
    db = _Db()
    assert not await cards.deliver_work_item_card(db, _decision(), _item({"channel": "google_chat", "user": "a@b.c"}))
    (event,) = db.added
    assert event.detail["permanent"] is True and "error" in event.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["space", "send"])
async def test_a_transient_chat_failure_is_retried_not_recorded_as_permanent(monkeypatch, where):
    # Review on #1120: a Chat 5xx, a timeout, no OAuth token or a failed DM lookup is retried within the bounded attempts.
    failure = AsyncMock(side_effect=cards.ChatUnavailableError("Google Chat answered HTTP 503"))
    if where == "space":
        monkeypatch.setattr(cards, "app_space_for", failure)
    else:
        monkeypatch.setattr(cards, "app_space_for", AsyncMock(return_value="spaces/ROOM"))
        monkeypatch.setattr(cards, "send_to_space", failure)
    db = _Db()
    assert not await cards.deliver_work_item_card(db, _decision(), _item({"channel": "google_chat", "user": "a@b.c"}))
    (event,) = db.added
    assert "permanent" not in event.detail and "HTTP 503" in event.detail["error"]
    assert await cards._attempts(_Db([event.detail]), uuid.uuid4()) == (False, 1)


@pytest.mark.asyncio
async def test_a_transient_chat_failure_still_falls_back_to_the_user_links(monkeypatch):
    monkeypatch.setattr(cards, "app_space_for", AsyncMock(side_effect=cards.ChatUnavailableError("timeout")))
    monkeypatch.setattr(cards, "link_message", lambda decision, recipient, language=None: "links")
    calls: list = []
    db = _Db()
    item = _item({"channel": "google_chat", "space": "spaces/ROOM", "integration": "Google"})
    assert await cards.deliver_work_item_card(db, _decision(), item, caller=_caller([(True, "sent")], calls))
    (event,) = db.added
    assert event.detail["sent"] is True and len(calls) == 1


class _Resp:
    def __init__(self, status: int, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


def _client(answer):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            if isinstance(answer, Exception):
                raise answer
            return answer

        get = post

    return _Client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "transient"),
    [
        (_Resp(503), True),
        (_Resp(429), True),
        (_Resp(408), True),
        (cards.httpx.ConnectTimeout("timeout"), True),
        (_Resp(403, {"error": {"status": "PERMISSION_DENIED"}}), False),
        (_Resp(404, {"error": {}}), False),
    ],
)
async def test_only_a_transient_chat_answer_raises_to_a_strict_caller(monkeypatch, answer, transient):
    monkeypatch.setattr(cards.httpx, "AsyncClient", _client(answer))
    assert await cards.post_message("t", "spaces/A", {}) is None
    assert await cards.direct_message_space("t", "users/1") is None
    for call in (
        lambda: cards.post_message("t", "spaces/A", {}, strict=True),
        lambda: cards.direct_message_space("t", "users/1", strict=True),
    ):
        if transient:
            with pytest.raises(cards.ChatUnavailableError):
                await call()
        else:
            assert await call() is None


@pytest.mark.asyncio
async def test_a_configured_bot_without_a_token_is_transient_only_when_strict(monkeypatch):
    from app.channels import google_chat

    monkeypatch.setattr(google_chat, "_get_googlechat_creds", AsyncMock(return_value={"credentials_json": "{}"}))
    monkeypatch.setattr(google_chat.GoogleChatChannel, "_get_access_token", AsyncMock(return_value=""))
    assert await cards.app_token() == ""
    with pytest.raises(cards.ChatUnavailableError):
        await cards.app_token(strict=True)
    monkeypatch.setattr(google_chat, "_get_googlechat_creds", AsyncMock(return_value=None))
    assert await cards.app_token(strict=True) == ""


@pytest.mark.asyncio
async def test_a_sent_or_refused_decision_is_not_sent_again(monkeypatch):
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(cards, "send_to_space", send)
    item = _item({"channel": "google_chat", "space": "spaces/ROOM"})
    for details in (
        [{"via": cards.VIA_CARD, "message_name": "spaces/ROOM/messages/1"}],
        [{"via": cards.VIA_LINKS, "permanent": True, "error": "x"}],
        [{"via": cards.VIA_LINKS, "error": "x"}] * cards.MAX_SEND_ATTEMPTS,
    ):
        assert not await cards.deliver_work_item_card(_Db(details), _decision(), item)
    send.assert_not_awaited()


# --- An answer given elsewhere edits the app's cards -------------------------------


@pytest.mark.asyncio
async def test_an_answer_elsewhere_edits_every_card_the_app_posted(monkeypatch):
    monkeypatch.setattr(cards, "load_view", AsyncMock(return_value="view"))
    monkeypatch.setattr(cards, "render_google_chat_decision", lambda view, language: {"text": language})
    patch = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(cards, "patch_message", patch)
    details = [
        {"via": cards.VIA_CARD, "message_name": "spaces/A/messages/1", "language": "fr"},
        {"via": cards.VIA_CARD, "message_name": "spaces/B/messages/2"},
        {"via": cards.VIA_LINKS, "sent": True},
        {"via": "graph_link_card", "message_id": "m"},
    ]
    counts = await cards.update_sent_cards(_Db(details), _decision(status="answered"), token="tok")
    assert counts == {"updated": 1, "refused": 1}
    assert [c.args[1:] for c in patch.await_args_list] == [
        ("spaces/A/messages/1", {"text": "fr"}),
        ("spaces/B/messages/2", {"text": "en"}),
    ]


@pytest.mark.asyncio
async def test_an_open_decision_edits_nothing(monkeypatch):
    patch = AsyncMock()
    monkeypatch.setattr(cards, "patch_message", patch)
    assert await cards.update_sent_cards(_Db(), _decision(), token="tok") == {"updated": 0, "refused": 0}
    patch.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_patch_refuses_a_message_name_that_is_not_one(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr(cards.httpx, "AsyncClient", client)
    assert await cards.patch_message("t", "spaces/A/../../users", {}) is False
    client.assert_not_called()


# --- The work item end notification -----------------------------------------------


@pytest.mark.asyncio
async def test_the_end_notification_goes_as_the_app_and_falls_back_to_the_user(monkeypatch):
    from app.core import database

    @asynccontextmanager
    async def session():
        yield _Db()

    monkeypatch.setattr(database, "async_session", session)
    monkeypatch.setattr(cards, "app_space_for", AsyncMock(return_value="spaces/ROOM"))
    monkeypatch.setattr(cards, "app_token", AsyncMock(return_value="tok"))
    post = AsyncMock(return_value={"name": "spaces/ROOM/messages/1"})
    monkeypatch.setattr(cards, "post_message", post)
    item = _item({"channel": "google_chat", "space": "spaces/ROOM"})
    await notify._send_teams_dm(item, notify.notify_spec(item.payload), "done")
    assert post.await_args.args == ("tok", "spaces/ROOM", {"text": "done"})

    post.return_value = None
    with pytest.raises(notify.PermanentNotifyError, match="no Google integration"):
        await notify._send_teams_dm(item, notify.notify_spec(item.payload), "done")
