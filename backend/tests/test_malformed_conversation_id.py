"""A malformed conversation id gets the answer an unknown one gets.

Conversation ids come back from storage the server does not control: the
``@filigran/chatbot`` panel replays the id it keeps in ``localStorage`` on every
restore, the public widget does the same with its session id, and URLs get
pasted, cut short and hand-edited. A value that is not a UUID names no
conversation, exactly like a well-formed id nobody issued, so every route that
acts on one conversation gives both the same answer - never the 500 a bare
``uuid.UUID(...)`` raised, which also kept the chatbot's own "drop the id and
start over" recovery from ever running.

The same holds for the other ids a route acts on - files, images, delegations,
shares, dataset entry files, file ids sent with a message or stored on a dataset
entry - and a malformed list filter gets the 422 every list filter gives.

Driven over HTTP (a minimal app per router, dependencies overridden) so the
status code a client sees is what is pinned, not a handler's return value.
"""

from __future__ import annotations

import ast
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.sql.dml import Update

from app.api import deps
from app.api.v1 import agent_chats as agent_chats_api
from app.api.v1 import chat as chat_api
from app.api.v1 import datasets as datasets_api
from app.api.v1 import platform as platform_api
from app.api.v1 import public_chat as public_chat_api
from app.api.v1.helpers import parse_conversation_id, parse_path_id
from app.core.database import get_db


@pytest.fixture(autouse=True)
def _dataset_version_bump_is_a_collaborator(monkeypatch):
    """Dataset versioning is something these routes delegate to, not their subject.

    It is stubbed like every other collaborator here so the "the route itself
    must not query" rule keeps measuring the route rather than the service it
    hands the dataset to.
    """

    async def _bump(_db, _dataset):
        return None

    monkeypatch.setattr(datasets_api.dataset_versioning, "bump_version", _bump)


# The two ids of the issue's reproduction: well-formed but issued to nobody,
# and the non-UUID value the chatbot replayed from localStorage.
UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"
MALFORMED_ID = "conv-1"

_MESSAGE_ID = str(uuid.uuid4())
_FILE_ID = str(uuid.uuid4())
_SHARE_ID = str(uuid.uuid4())


class _Rows:
    """One query result, in every read shape the routes use."""

    def __init__(self, row=None):
        self._row = row

    def scalar_one_or_none(self):
        return self._row

    def one_or_none(self):
        return self._row

    def first(self):
        return self._row

    def scalars(self):
        return SimpleNamespace(all=lambda: [] if self._row is None else [self._row])

    def all(self):
        return [] if self._row is None else [self._row]

    def __iter__(self):
        return iter(self.all())


class _FakeDB:
    """Answers queries from a script (nothing once it runs out) and records them.

    With an empty script it is a database holding no conversation at all -
    what an unknown id meets. The recorded statements show whether a route
    went to the database for a malformed id instead of answering it outright.
    """

    def __init__(self, *rows):
        self._rows = list(rows)
        self.statements: list = []
        self.added: list = []

    async def execute(self, statement, *_args, **_kwargs):
        self.statements.append(statement)
        return _Rows(self._rows.pop(0) if self._rows else None)

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        return None

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self):
        await self.flush()

    async def rollback(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _user():
    return SimpleNamespace(id=uuid.uuid4(), email="user@example.com", role="user", is_admin=False)


def _client(router, prefix: str, db: _FakeDB, user) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix=prefix)

    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[deps.require_chat_enabled] = lambda: None
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def _ask_with_both_ids(make_client, request_for):
    """Send the same request once with the unknown id and once with the malformed one."""
    answers = []
    for conversation_id in (UNKNOWN_ID, MALFORMED_ID):
        db = _FakeDB()
        method, url, kwargs = request_for(conversation_id)
        answers.append((make_client(db).request(method, url, **kwargs), db))
    return answers


def _assert_same_answer_as_unknown(answers, *, status: int = 404):
    (unknown, _), (malformed, malformed_db) = answers
    assert unknown.status_code == status
    assert (malformed.status_code, malformed.json()) == (unknown.status_code, unknown.json())
    assert malformed_db.statements == [], "a malformed id names no row: nothing to look up"


# ---------------------------------------------------------------------------
# POST /platform/chat/sessions - the issue's two curls
# ---------------------------------------------------------------------------

_REQUESTED_AGENT = SimpleNamespace(id=uuid.uuid4(), name="Requested Agent")


@pytest.fixture
def platform_client(monkeypatch):
    async def _resolve_platform_agent(_db, _agent_slug):
        return _REQUESTED_AGENT

    monkeypatch.setattr(platform_api, "_resolve_platform_agent", _resolve_platform_agent)
    return lambda db: _client(platform_api.router, "/api/v1/platform", db, _user())


def _restore_session(platform_client, conversation_id: str):
    db = _FakeDB()
    resp = platform_client(db).post(
        "/api/v1/platform/chat/sessions",
        json={"conversation_id": conversation_id, "agent_slug": "agent"},
    )
    return resp, db


def _assert_fresh_conversation(resp, db, stale_id: str) -> None:
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["conversation_id"] != stale_id
    assert body["messages"] == []
    assert (body["agent_id"], body["agent_name"]) == (str(_REQUESTED_AGENT.id), _REQUESTED_AGENT.name)
    (created,) = db.added
    assert str(created.id) == body["conversation_id"]
    assert created.channel == platform_api.PLATFORM_CHAT_CHANNEL
    assert created.agent_id == _REQUESTED_AGENT.id


def test_a_valid_but_unknown_id_starts_a_fresh_conversation(platform_client):
    resp, db = _restore_session(platform_client, UNKNOWN_ID)

    _assert_fresh_conversation(resp, db, UNKNOWN_ID)
    assert len(db.statements) == 1, "the unknown id is looked up once, then replaced"


@pytest.mark.parametrize(
    "malformed",
    [MALFORMED_ID, "not-a-uuid", "12345", UNKNOWN_ID[:-1] + "g", " " + UNKNOWN_ID],
)
def test_a_malformed_id_starts_a_fresh_conversation_instead_of_a_500(platform_client, malformed):
    resp, db = _restore_session(platform_client, malformed)

    _assert_fresh_conversation(resp, db, malformed)
    assert db.statements == [], "a malformed id is ignored, not looked up"


def test_both_stale_ids_get_the_same_session_shape(platform_client):
    unknown, _ = _restore_session(platform_client, UNKNOWN_ID)
    malformed, _ = _restore_session(platform_client, MALFORMED_ID)

    assert unknown.status_code == malformed.status_code == 200

    def _shape(resp):
        return {key: value for key, value in resp.json().items() if key != "conversation_id"}

    assert _shape(malformed) == _shape(unknown)


# ---------------------------------------------------------------------------
# The other platform chat routes (the embedded chatbot's surface)
# ---------------------------------------------------------------------------

_PLATFORM_ROUTES = {
    "send-message": lambda cid: (
        "POST",
        "/api/v1/platform/chat/messages",
        {"json": {"content": "hello", "conversation_id": cid, "agent_slug": "agent", "stream": False}},
    ),
    "steer": lambda cid: (
        "POST",
        "/api/v1/platform/chat/messages/steer",
        {"json": {"conversation_id": cid, "content": "hello"}},
    ),
    "delete-session": lambda cid: ("DELETE", f"/api/v1/platform/chat/sessions/{cid}", {}),
    # Message feedback: either id may be the malformed one.
    "rate-message": lambda cid: (
        "POST",
        f"/api/v1/platform/chat/conversations/{cid}/messages/{UNKNOWN_ID}/feedback",
        {"json": {"rating": "positive"}},
    ),
    "rate-message-bad-message-id": lambda mid: (
        "POST",
        f"/api/v1/platform/chat/conversations/{UNKNOWN_ID}/messages/{mid}/feedback",
        {"json": {"rating": "negative", "comment": "wrong"}},
    ),
    "retract-rating": lambda cid: (
        "DELETE",
        f"/api/v1/platform/chat/conversations/{cid}/messages/{UNKNOWN_ID}/feedback",
        {},
    ),
    "retract-rating-bad-message-id": lambda mid: (
        "DELETE",
        f"/api/v1/platform/chat/conversations/{UNKNOWN_ID}/messages/{mid}/feedback",
        {},
    ),
}


@pytest.mark.parametrize("request_for", list(_PLATFORM_ROUTES.values()), ids=list(_PLATFORM_ROUTES))
def test_platform_chat_routes_answer_a_malformed_id_like_an_unknown_one(platform_client, request_for):
    _assert_same_answer_as_unknown(_ask_with_both_ids(platform_client, request_for))


# ---------------------------------------------------------------------------
# Native chat routes
# ---------------------------------------------------------------------------


@pytest.fixture
def chat_client(monkeypatch):
    async def _no_rate_limit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chat_api, "enforce_rate_limit", _no_rate_limit)
    return lambda db, user=None: _client(chat_api.router, "/api/v1/chat", db, user or _user())


def _conversation_url(cid: str, suffix: str = "") -> str:
    return f"/api/v1/chat/conversations/{cid}{suffix}"


_CHAT_ROUTES = {
    "get-conversation": lambda cid: ("GET", _conversation_url(cid), {}),
    "page-messages": lambda cid: ("GET", _conversation_url(cid, "/messages"), {}),
    "rename": lambda cid: ("PATCH", _conversation_url(cid), {"json": {"title": "Renamed"}}),
    "bind-presentation": lambda cid: ("PUT", _conversation_url(cid, "/presentation"), {"json": {"presentation_id": None}}),
    "bind-dashboard": lambda cid: ("PUT", _conversation_url(cid, "/dashboard"), {"json": {"custom_dashboard_id": None}}),
    "archive": lambda cid: ("DELETE", _conversation_url(cid), {}),
    "return-to-source": lambda cid: ("POST", _conversation_url(cid, "/return-to-source"), {}),
    "upload": lambda cid: (
        "POST",
        _conversation_url(cid, "/upload"),
        {"files": {"file": ("notes.txt", b"hello", "text/plain")}},
    ),
    "list-files": lambda cid: ("GET", _conversation_url(cid, "/files"), {}),
    "files-access-ack": lambda cid: ("POST", _conversation_url(cid, "/files/access-ack"), {}),
    "list-shares": lambda cid: ("GET", _conversation_url(cid, "/shares"), {}),
    "create-share": lambda cid: ("POST", _conversation_url(cid, "/shares"), {"json": {"user_id": str(uuid.uuid4())}}),
    "delete-share": lambda cid: ("DELETE", _conversation_url(cid, f"/shares/{_SHARE_ID}"), {}),
    "link-access": lambda cid: ("PUT", _conversation_url(cid, "/link-access"), {"json": {"link_access": "private"}}),
    "send-message": lambda cid: ("POST", _conversation_url(cid, "/messages"), {"json": {"content": "hello"}}),
    "get-feedback": lambda cid: ("GET", _conversation_url(cid, f"/messages/{_MESSAGE_ID}/feedback"), {}),
    "upsert-feedback": lambda cid: (
        "POST",
        _conversation_url(cid, f"/messages/{_MESSAGE_ID}/feedback"),
        {"json": {"rating": "positive"}},
    ),
    "delete-feedback": lambda cid: ("DELETE", _conversation_url(cid, f"/messages/{_MESSAGE_ID}/feedback"), {}),
    "download-message-file": lambda cid: (
        "GET",
        _conversation_url(cid, f"/messages/{_MESSAGE_ID}/files/{_FILE_ID}/download"),
        {},
    ),
}


@pytest.mark.parametrize("request_for", list(_CHAT_ROUTES.values()), ids=list(_CHAT_ROUTES))
def test_chat_routes_answer_a_malformed_id_like_an_unknown_one(chat_client, request_for):
    _assert_same_answer_as_unknown(_ask_with_both_ids(chat_client, request_for))


def test_feedback_routes_answer_a_malformed_message_id_like_an_unknown_one(chat_client):
    conversation_id = str(uuid.uuid4())
    answers = []
    for message_id in (str(uuid.uuid4()), "msg-1"):
        db = _FakeDB()
        resp = chat_client(db).get(_conversation_url(conversation_id, f"/messages/{message_id}/feedback"))
        answers.append(((resp.status_code, resp.json()), len(db.statements)))

    (unknown, _), (malformed, malformed_queries) = answers
    assert unknown == malformed == (404, {"detail": "Message not found"})
    assert malformed_queries == 0


@pytest.mark.parametrize(
    ("part", "detail"),
    [("message_id", "Message not found"), ("file_id", "File not found")],
)
def test_download_route_answers_malformed_message_and_file_ids_like_unknown_ones(chat_client, part, detail):
    user = _user()
    conversation = SimpleNamespace(id=uuid.uuid4(), user_id=user.id)
    message = SimpleNamespace(id=uuid.UUID(_MESSAGE_ID), conversation_id=conversation.id)
    found_before = (conversation,) if part == "message_id" else (conversation, message)

    answers = []
    for bad in (str(uuid.uuid4()), "not-a-uuid"):
        ids = {"message_id": _MESSAGE_ID, "file_id": _FILE_ID, part: bad}
        db = _FakeDB(*found_before)
        resp = chat_client(db, user).get(
            _conversation_url(conversation.id, f"/messages/{ids['message_id']}/files/{ids['file_id']}/download")
        )
        answers.append(((resp.status_code, resp.json()), len(db.statements)))

    (unknown, unknown_queries), (malformed, malformed_queries) = answers
    assert unknown == malformed == (404, {"detail": detail})
    assert malformed_queries == unknown_queries - 1, "the malformed id itself is never looked up"


def test_share_delete_answers_a_malformed_share_id_like_an_unknown_one(chat_client):
    """``parse_uuid``'s 422 used to answer a malformed share id; an unknown one gets 404."""
    user = _user()
    conversation = SimpleNamespace(id=uuid.uuid4(), user_id=user.id)
    answers = []
    for share_id in (str(uuid.uuid4()), "share-1"):
        db = _FakeDB(conversation)
        resp = chat_client(db, user).delete(_conversation_url(conversation.id, f"/shares/{share_id}"))
        answers.append(((resp.status_code, resp.json()), len(db.statements)))

    (unknown, unknown_queries), (malformed, malformed_queries) = answers
    assert unknown == malformed == (404, {"detail": "Share not found"})
    assert malformed_queries == unknown_queries - 1, "the malformed id itself is never looked up"


def test_send_links_only_the_attachments_it_resolved(chat_client, monkeypatch, caplog):
    """A malformed file id is dropped at resolution, like an unknown one.

    The link step re-parsed every id the client sent, so a malformed one was
    logged at ERROR as a failed link on every send that carried it.
    """
    from app.services import chat_turn_guard

    user = _user()
    conversation = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user.id,
        title="Chat",
        presentation_id=None,
        video_project_id=None,
        custom_dashboard_id=None,
        fleet_id=None,
        agent_id=None,
        tool_overrides_json=None,
    )
    attachment = SimpleNamespace(
        id=uuid.uuid4(), filename="notes.txt", content_type="text/plain", size=5, s3_object_name="chat/notes.txt"
    )

    async def _no_context(*_args, **_kwargs):
        return None

    async def _store_message(_db, _conversation_id, **fields):
        return SimpleNamespace(id=uuid.uuid4(), sequence=1, created_at=datetime.now(UTC), **fields)

    async def _fold(*_args, **_kwargs):
        return None, True

    monkeypatch.setattr(chat_api, "load_studio_context", _no_context)
    monkeypatch.setattr(chat_api, "load_desktop_context", _no_context)
    monkeypatch.setattr(chat_api, "store_message", _store_message)
    monkeypatch.setattr(chat_api.chat_turn_stream, "publish_live", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chat_turn_guard, "acquire_or_fold", _fold)
    caplog.set_level(logging.WARNING, logger=chat_api.logger.name)

    db = _FakeDB(conversation, attachment)
    resp = chat_client(db, user).post(
        _conversation_url(conversation.id, "/messages"),
        json={"content": "hello", "file_ids": [str(attachment.id), "not-a-uuid"]},
    )

    assert resp.status_code == 202, resp.text
    assert [f["file_id"] for f in resp.json()["user_message"]["attachments"]] == [str(attachment.id)]
    links = [statement for statement in db.statements if isinstance(statement, Update)]
    assert len(links) == 1, "only the resolved attachment is linked to the message"
    assert [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR] == []


def test_delegations_filter_refuses_a_malformed_conversation_id_instead_of_a_500(chat_client):
    """A list filter has no unknown-conversation answer (an unknown id is an empty page).

    So it keeps ``parse_uuid``'s 422 - but no longer lets the value through to
    the service's bare ``uuid.UUID``.
    """
    unknown = chat_client(_FakeDB()).get("/api/v1/chat/delegations", params={"conversation_id": UNKNOWN_ID})
    malformed = chat_client(_FakeDB()).get("/api/v1/chat/delegations", params={"conversation_id": MALFORMED_ID})

    assert (unknown.status_code, unknown.json()) == (200, [])
    assert malformed.status_code == 422


# ---------------------------------------------------------------------------
# Agent detail > Chats
# ---------------------------------------------------------------------------


def test_agent_chat_detail_answers_a_malformed_id_like_an_unknown_one(monkeypatch):
    agent = SimpleNamespace(id=uuid.uuid4())

    async def _get_user_agent(*_args, **_kwargs):
        return agent

    async def _get_user_group_ids(*_args, **_kwargs):
        return []

    monkeypatch.setattr(agent_chats_api, "get_user_agent", _get_user_agent)
    monkeypatch.setattr(agent_chats_api, "get_user_group_ids", _get_user_group_ids)
    monkeypatch.setattr(agent_chats_api, "can_view_agent_chats", lambda *_args: True)

    answers = _ask_with_both_ids(
        lambda db: _client(agent_chats_api.router, "/api/v1/agents", db, _user()),
        lambda cid: ("GET", f"/api/v1/agents/{agent.id}/chats/{cid}", {}),
    )
    _assert_same_answer_as_unknown(answers)


# ---------------------------------------------------------------------------
# Public chat widget (its session id is a conversation id)
# ---------------------------------------------------------------------------


@pytest.fixture
def widget_client(monkeypatch):
    agent = SimpleNamespace(id=uuid.uuid4(), name="Widget agent", web_enabled=True, enabled=True)

    async def _resolve_agent(_token, _db=None):
        return agent

    async def _no_rate_limit(*_args, **_kwargs):
        return None

    async def _chat_enabled(_db):
        return True

    monkeypatch.setattr(public_chat_api, "_resolve_agent", _resolve_agent)
    monkeypatch.setattr(public_chat_api, "enforce_rate_limit", _no_rate_limit)
    monkeypatch.setattr(platform_api, "is_chat_enabled", _chat_enabled)

    def _make(db):
        monkeypatch.setattr(public_chat_api, "async_session", lambda: db)
        app = FastAPI()
        app.include_router(public_chat_api.router, prefix="/api/v1/public")
        return TestClient(app, raise_server_exceptions=False)

    return _make


_WIDGET_ROUTES = {
    "session-messages": lambda sid: ("GET", f"/api/v1/public/chat/web-token/sessions/{sid}", {}),
    "send-message": lambda sid: (
        "POST",
        f"/api/v1/public/chat/web-token/sessions/{sid}/messages",
        {"json": {"content": "hello"}},
    ),
}


@pytest.mark.parametrize("request_for", list(_WIDGET_ROUTES.values()), ids=list(_WIDGET_ROUTES))
def test_widget_routes_answer_a_malformed_session_id_like_an_unknown_one(widget_client, request_for):
    answers = _ask_with_both_ids(widget_client, request_for)

    _assert_same_answer_as_unknown(answers)
    assert answers[1][0].json() == {"detail": "Session not found"}


def test_widget_stream_answers_a_malformed_session_id_with_the_unknown_id_event(widget_client):
    """The stream's answer to an unknown session is an in-band error event.

    A malformed one used to raise inside the event generator instead, after
    the 200 was sent: the widget got a broken stream and no event at all.
    """
    answers = []
    for session_id in (UNKNOWN_ID, MALFORMED_ID):
        db = _FakeDB()
        resp = widget_client(db).post(
            f"/api/v1/public/chat/web-token/sessions/{session_id}/messages/stream",
            json={"content": "hello"},
        )
        answers.append(((resp.status_code, resp.text), len(db.statements)))

    (unknown, _), (malformed, malformed_queries) = answers
    assert malformed == unknown
    assert '"type": "error"' in malformed[1]
    assert "Session not found" in malformed[1]
    assert malformed_queries == 0


# ---------------------------------------------------------------------------
# The web chat (``/api/v1/web``, the chat page's surface)
# ---------------------------------------------------------------------------


@pytest.fixture
def web_chat_client(monkeypatch):
    from app.api.v1 import chat_web_stream

    def _make(db):
        client = _client(chat_web_stream.router, "/api/v1/web", db, _user())
        client.app.dependency_overrides[chat_web_stream.web_chat_user] = _user
        return client

    return _make


_WEB_CHAT_ROUTES = {
    "send-message": lambda cid: (
        "POST",
        "/api/v1/web/chat/messages",
        {"json": {"content": "hello", "conversation_id": cid}},
    ),
    "steer": lambda cid: (
        "POST",
        "/api/v1/web/chat/messages/steer",
        {"json": {"conversation_id": cid, "content": "hello"}},
    ),
    "cancel": lambda cid: ("POST", "/api/v1/web/chat/messages/cancel", {"json": {"conversation_id": cid}}),
    "skip-wait": lambda cid: ("POST", "/api/v1/web/chat/messages/skip-wait", {"json": {"conversation_id": cid}}),
    "pending-approvals": lambda cid: ("GET", f"/api/v1/web/chat/conversations/{cid}/pending-approvals", {}),
    "events": lambda cid: ("GET", f"/api/v1/web/chat/conversations/{cid}/events", {}),
    "delete-session": lambda cid: ("DELETE", f"/api/v1/web/chat/sessions/{cid}", {}),
}


@pytest.mark.parametrize("request_for", list(_WEB_CHAT_ROUTES.values()), ids=list(_WEB_CHAT_ROUTES))
def test_web_chat_routes_answer_a_malformed_id_like_an_unknown_one(web_chat_client, request_for):
    _assert_same_answer_as_unknown(_ask_with_both_ids(web_chat_client, request_for))


async def test_a_turn_on_a_malformed_id_gets_the_unknown_id_refusal(monkeypatch):
    """The connection runner answers a malformed id like an unknown one, without a lookup."""
    import app.core.database as database_module
    from app.services.chat_web_transport import HeadlessTurnConnection

    db = _FakeDB()

    async def _nothing(*_args, **_kwargs):
        return None

    async def _session_holds(session):
        return session

    monkeypatch.setattr(chat_api, "resolve_account_rejection", _nothing)
    monkeypatch.setattr(chat_api, "recheck_session", _session_holds)
    monkeypatch.setattr(chat_api, "enforce_rate_limit", _nothing)
    monkeypatch.setattr(database_module, "async_session", lambda: db)

    sent = {}
    queries = {}
    for conversation_id in (UNKNOWN_ID, MALFORMED_ID):
        db.statements.clear()
        frames: list[dict] = []

        async def _written(message, frames=frames):
            body = (message.get("body") or b"").decode()
            frames.extend(json.loads(c[6:]) for c in body.split("\n\n") if c.startswith("data: "))

        connection = HeadlessTurnConnection(conversation_id, {"content": "hello"})
        connection.bind(_written)
        auth = chat_api.ChatConnectionAuth(user_id=uuid.uuid4(), session=SimpleNamespace())
        await chat_api.run_chat_connection(connection, conversation_id, auth)
        sent[conversation_id] = (frames, connection.refusal)
        queries[conversation_id] = len(db.statements)

    assert sent[UNKNOWN_ID][1].detail == "Conversation not found"
    assert sent[MALFORMED_ID] == sent[UNKNOWN_ID]
    assert queries[MALFORMED_ID] == 0


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [MALFORMED_ID, "not-a-uuid", "12345", "", None, " " + UNKNOWN_ID, UNKNOWN_ID[:-1] + "g"],
)
def test_parse_conversation_id_answers_a_malformed_id_with_the_unknown_id_404(value):
    with pytest.raises(HTTPException) as exc_info:
        parse_conversation_id(value)

    assert (exc_info.value.status_code, exc_info.value.detail) == (404, "Conversation not found")


def test_parse_conversation_id_speaks_the_route_s_own_not_found_wording():
    with pytest.raises(HTTPException) as exc_info:
        parse_conversation_id(MALFORMED_ID, not_found_detail="Session not found")

    assert exc_info.value.detail == "Session not found"


def test_parse_conversation_id_returns_the_uuid_of_a_well_formed_id():
    assert parse_conversation_id(UNKNOWN_ID) == uuid.UUID(UNKNOWN_ID)


def test_parse_path_id_answers_a_malformed_id_with_the_route_s_404():
    with pytest.raises(HTTPException) as exc_info:
        parse_path_id(MALFORMED_ID, not_found_detail="Image not found")

    assert (exc_info.value.status_code, exc_info.value.detail) == (404, "Image not found")
    assert parse_path_id(UNKNOWN_ID, not_found_detail="Image not found") == uuid.UUID(UNKNOWN_ID)


# ---------------------------------------------------------------------------
# Other ids: files, images, delegations, dataset entry files
# ---------------------------------------------------------------------------


@pytest.fixture
def valid_signed_links(monkeypatch):
    """The public routes check their HMAC token before the id; let it pass."""
    import app.core.security as security_module

    monkeypatch.setattr(security_module, "verify_image_token", lambda *_args: True)


_SIGNED = {"params": {"token": "signature", "expires": 1}}

_OTHER_ID_ROUTES = {
    "file-download": lambda fid: ("GET", f"/api/v1/chat/files/{fid}/download", {}),
    "file-info": lambda fid: ("GET", f"/api/v1/chat/files/{fid}/info", {}),
    "file-public": lambda fid: ("GET", f"/api/v1/chat/files/{fid}/public", _SIGNED),
    "image": lambda iid: ("GET", f"/api/v1/chat/images/{iid}", {}),
    "image-public": lambda iid: ("GET", f"/api/v1/chat/images/{iid}/public", _SIGNED),
    "delegation": lambda did: ("GET", f"/api/v1/chat/delegations/{did}", {}),
}


@pytest.mark.parametrize("request_for", list(_OTHER_ID_ROUTES.values()), ids=list(_OTHER_ID_ROUTES))
def test_other_path_ids_answer_a_malformed_id_like_an_unknown_one(chat_client, valid_signed_links, request_for):
    _assert_same_answer_as_unknown(_ask_with_both_ids(chat_client, request_for))


@pytest.mark.parametrize("filter_name", ["agent_id", "parent_agent_id", "target_agent_id"])
def test_delegations_agent_filters_refuse_a_malformed_id_instead_of_a_500(chat_client, filter_name):
    unknown = chat_client(_FakeDB()).get("/api/v1/chat/delegations", params={filter_name: UNKNOWN_ID})
    malformed = chat_client(_FakeDB()).get("/api/v1/chat/delegations", params={filter_name: "not-a-uuid"})

    assert (unknown.status_code, unknown.json()) == (200, [])
    assert malformed.status_code == 422


def test_dataset_entry_file_delete_answers_a_malformed_file_id_like_an_unknown_one(monkeypatch):
    """The delete is idempotent: an unknown file id is a 204 that removes nothing."""
    dataset = SimpleNamespace(is_company_managed=False, is_read_only=False)
    entry = SimpleNamespace(id=uuid.uuid4(), file_ids=[str(uuid.uuid4())])
    released: list[set] = []

    async def _entry_with_dataset(_db, _user, _entry_id):
        return entry, dataset

    async def _release(_db, file_uuids):
        released.append(set(file_uuids))
        return []

    monkeypatch.setattr(datasets_api, "_get_user_dataset_entry_with_dataset", _entry_with_dataset)
    monkeypatch.setattr(datasets_api, "_delete_dataset_files_if_unreferenced", _release)

    admin = SimpleNamespace(id=uuid.uuid4(), email="admin@example.com", role="admin", is_admin=True)
    listed = list(entry.file_ids)
    statuses = []
    for file_id in (UNKNOWN_ID, MALFORMED_ID):
        client = _client(datasets_api.router, "/api/v1/datasets", _FakeDB(), admin)
        client.app.dependency_overrides[deps.require_admin] = lambda: admin
        statuses.append(client.delete(f"/api/v1/datasets/entries/{entry.id}/files/{file_id}").status_code)

    assert statuses == [204, 204]
    assert released == [] and entry.file_ids == listed, "an id the entry does not list removes nothing"


# An entry's ``file_ids`` is stored as its create / update body sent it, so a
# malformed id is read back later: it names no file, like an unknown id, and
# must not turn every read, delete or export of the dataset into a 500.
_ADMIN = SimpleNamespace(id=uuid.uuid4(), email="admin@example.com", role="admin", is_admin=True)
_KNOWN_FILE_ID = uuid.uuid4()


def _admin_datasets_client(db: _FakeDB) -> TestClient:
    client = _client(datasets_api.router, "/api/v1/datasets", db, _ADMIN)
    client.app.dependency_overrides[deps.require_admin] = lambda: _ADMIN
    return client


def _entry(file_ids):
    return SimpleNamespace(
        id=uuid.uuid4(),
        dataset_id=uuid.uuid4(),
        position=0,
        description=None,
        input_prompt="question",
        expected_answer="answer",
        evaluation_prompt=None,
        file_ids=file_ids,
        created_at=datetime.now(UTC),
        # The entry's own override of the dataset's answer shape.
        output_schema=None,
    )


def _dataset():
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Eval set",
        description=None,
        evaluation_instructions=None,
        is_company_managed=False,
        is_read_only=False,
        content_hash="stale",
        # The evaluation family and its two declarations ride in
        # the export manifest alongside the fields above.
        family=None,
        label_vocabulary=None,
        output_schema=None,
    )


def test_dataset_entry_update_with_a_malformed_file_id_reads_back_instead_of_a_500(monkeypatch):
    entry, dataset = _entry(None), _dataset()
    known = SimpleNamespace(id=_KNOWN_FILE_ID, filename="spec.pdf", size=3, content_type="application/pdf")

    async def _entry_with_dataset(_db, _user, _entry_id):
        return entry, dataset

    monkeypatch.setattr(datasets_api, "_get_user_dataset_entry_with_dataset", _entry_with_dataset)

    resp = _admin_datasets_client(_FakeDB(known)).put(
        f"/api/v1/datasets/entries/{entry.id}",
        json={"expected_answer": "answer", "file_ids": [str(_KNOWN_FILE_ID), MALFORMED_ID]},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["file_ids"] == [str(_KNOWN_FILE_ID), MALFORMED_ID]
    assert [f["id"] for f in resp.json()["files"]] == [str(_KNOWN_FILE_ID)]


def test_an_entry_naming_only_a_malformed_file_id_lists_no_file_like_an_unknown_one(monkeypatch):
    entry, dataset = _entry(None), _dataset()

    async def _entry_with_dataset(_db, _user, _entry_id):
        return entry, dataset

    monkeypatch.setattr(datasets_api, "_get_user_dataset_entry_with_dataset", _entry_with_dataset)

    files = []
    for file_id in (UNKNOWN_ID, MALFORMED_ID):
        resp = _admin_datasets_client(_FakeDB()).put(
            f"/api/v1/datasets/entries/{entry.id}", json={"expected_answer": "answer", "file_ids": [file_id]}
        )
        assert resp.status_code == 200, resp.text
        files.append(resp.json()["files"])

    assert files == [[], []]


def _released_files(monkeypatch) -> list[set]:
    """The ids the dataset routes hand the shared file rule, nothing purged."""
    import app.services.file_cleanup as file_cleanup

    released: list[set] = []

    async def _release(_db, file_uuids):
        released.append(set(file_uuids))
        return []

    async def _purge(_keys):
        return 0

    monkeypatch.setattr(datasets_api, "_delete_dataset_files_if_unreferenced", _release)
    monkeypatch.setattr(file_cleanup, "purge_storage_objects", _purge)
    return released


def test_dataset_entry_delete_skips_a_malformed_stored_file_id(monkeypatch):
    entry, dataset = _entry([str(_KNOWN_FILE_ID), MALFORMED_ID]), _dataset()

    async def _entry_with_dataset(_db, _user, _entry_id):
        return entry, dataset

    monkeypatch.setattr(datasets_api, "_get_user_dataset_entry_with_dataset", _entry_with_dataset)
    released = _released_files(monkeypatch)

    resp = _admin_datasets_client(_FakeDB()).delete(f"/api/v1/datasets/entries/{entry.id}")

    assert resp.status_code == 204, resp.text
    assert released == [{_KNOWN_FILE_ID}]


def test_dataset_delete_skips_a_malformed_stored_file_id(monkeypatch):
    dataset = _dataset()

    async def _get_user_dataset(*_args, **_kwargs):
        return dataset

    async def _get_user_group_ids(*_args, **_kwargs):
        return []

    async def _log_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(datasets_api, "_get_user_dataset", _get_user_dataset)
    monkeypatch.setattr(datasets_api, "get_user_group_ids", _get_user_group_ids)
    monkeypatch.setattr(datasets_api, "log_audit", _log_audit)
    released = _released_files(monkeypatch)

    stored_file_ids = ([str(_KNOWN_FILE_ID), MALFORMED_ID],)
    resp = _admin_datasets_client(_FakeDB(stored_file_ids)).delete(f"/api/v1/datasets/{dataset.id}")

    assert resp.status_code == 204, resp.text
    assert released == [{_KNOWN_FILE_ID}]


def test_evaluation_run_detail_skips_a_malformed_stored_file_id(monkeypatch):
    from app.api.v1 import evaluations as evaluations_api

    # A run holds its mode: the route reads no result for it.
    run = SimpleNamespace(
        id=uuid.uuid4(),
        status="completed",
        evaluation_id=uuid.uuid4(),
        execution_mode="full_loop",
        # Outside any placebo comparison: no placebo to serve.
        placebo_snapshot=None,
    )
    result = SimpleNamespace(
        id=uuid.uuid4(),
        run_id=run.id,
        entry_id=uuid.uuid4(),
        agent_output="answer",
        score=1.0,
        duration_seconds=None,
        speed_score=None,
        overall_score=None,
        f1_score=None,
        sub_scores={},
        evaluator_reasoning=None,
        execution_details=None,
        created_at=datetime.now(UTC),
        # Recorded beside a result: the reuse fingerprint and link,
        # the judge-failure flag and the execution-anomaly classification.
        input_fingerprint=None,
        is_reused=False,
        reused_from_result_id=None,
        is_judge_failure=False,
        execution_anomaly_kind=None,
    )
    known = SimpleNamespace(id=_KNOWN_FILE_ID, filename="spec.pdf", size=3, content_type="application/pdf")
    result_row = (result, "question", None, [str(_KNOWN_FILE_ID), MALFORMED_ID], "answer")

    async def _build_run_response(_db, built_run, **_kwargs):
        return {
            "id": str(built_run.id),
            "evaluation_id": str(uuid.uuid4()),
            "agent_id": str(uuid.uuid4()),
            "model_id": "model",
            "status": "completed",
            "trigger_reason": "manual",
            "created_at": datetime.now(UTC).isoformat(),
        }

    monkeypatch.setattr(evaluations_api, "_build_run_response", _build_run_response)

    # The route reads more than its own lookup before it answers: after
    # the callback-pending check it re-reads the run's headline columns, then
    # each result's live flags, then the results themselves, then their linked
    # traces -- and only then the file rows this test is about.
    headline = SimpleNamespace(
        score=None,
        quality_score=None,
        f1_score=None,
        status="completed",
        error=None,
        completed_entries=0,
        headline_details=None,
    )
    result_flags = SimpleNamespace(
        sub_scores={}, is_reused=False, is_judge_failure=False, execution_anomaly_kind=None
    )
    db = _FakeDB(run, result_row, None, headline, result_flags, result_row, None, known)
    client = _client(evaluations_api.router, "/api/v1/evaluations", db, _ADMIN)
    client.app.dependency_overrides[deps.require_admin] = lambda: _ADMIN
    resp = client.get(f"/api/v1/evaluations/runs/{run.id}")

    assert resp.status_code == 200, resp.text
    assert [[f["id"] for f in r["files"]] for r in resp.json()["results"]] == [[str(_KNOWN_FILE_ID)]]


def test_dataset_export_skips_a_malformed_stored_file_id(monkeypatch):
    import io
    import json
    import zipfile

    import app.services.file_storage as file_storage_module

    dataset = _dataset()
    entry = _entry([str(_KNOWN_FILE_ID), MALFORMED_ID])
    known = SimpleNamespace(
        id=_KNOWN_FILE_ID, filename="spec.pdf", size=3, content_type="application/pdf", s3_object_name="dataset/spec.pdf"
    )

    async def _get_user_dataset(*_args, **_kwargs):
        return dataset

    async def _get_user_group_ids(*_args, **_kwargs):
        return []

    async def _download(_object_name):
        return b"pdf"

    monkeypatch.setattr(datasets_api, "_get_user_dataset", _get_user_dataset)
    monkeypatch.setattr(datasets_api, "get_user_group_ids", _get_user_group_ids)
    monkeypatch.setattr(file_storage_module.file_storage, "download", _download)

    db = _FakeDB(entry, known)
    resp = _admin_datasets_client(db).get(f"/api/v1/datasets/{dataset.id}/export")

    assert resp.status_code == 200, resp.text
    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert [exported["files"] for exported in manifest["entries"]] == [["f0"]]
    assert [file["filename"] for file in manifest["files"]] == ["spec.pdf"]


# ---------------------------------------------------------------------------
# Tripwire
# ---------------------------------------------------------------------------

#: What a client-supplied conversation id is called in the API layer. The
#: public widget calls its conversation a session.
_CONVERSATION_ID_NAMES = frozenset({"conversation_id"})
_CONVERSATION_ID_NAMES_BY_MODULE = {"public_chat.py": frozenset({"session_id"})}
#: Route parameters naming other rows. Matched as bare names only - the
#: way a path or query parameter arrives - because the same attribute names
#: are read off database rows (``ref.file_id``), which are not client input.
_PATH_ID_PARAMETERS = frozenset({"file_id", "image_id", "delegation_id", "message_id", "share_id"})


def _names(node: ast.AST) -> set[str]:
    return {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}


def _identifiers(node: ast.AST) -> set[str]:
    found = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            found.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            found.add(sub.attr)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            found.add(sub.value)
    return found


def test_no_api_module_parses_a_client_id_with_a_bare_uuid_call():
    """``uuid.UUID(<client id>)`` in the API layer is this bug waiting for a stale id.

    Use ``parse_conversation_id()`` / ``parse_path_id()`` on a route acting on
    one row, ``parse_uuid()`` for a list filter, or ``safe_uuid()`` where the
    route has its own answer for an id that names nothing (a fresh session, an
    in-band error frame, an idempotent delete).
    """
    api_dir = Path(chat_api.__file__).resolve().parent.parent
    offenders = []
    for path in sorted(api_dir.rglob("*.py")):
        watched = _CONVERSATION_ID_NAMES | _CONVERSATION_ID_NAMES_BY_MODULE.get(path.name, frozenset())
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
            if callee == "UUID" and any(
                _identifiers(arg) & watched or _names(arg) & _PATH_ID_PARAMETERS for arg in arguments
            ):
                offenders.append(f"{path.relative_to(api_dir.parent).as_posix()}:{node.lineno}")

    assert offenders == [], (
        "client-supplied ids parsed with a bare uuid.UUID(...) - a malformed id becomes a 500 "
        f"instead of the route's unknown-id answer: {offenders}"
    )
