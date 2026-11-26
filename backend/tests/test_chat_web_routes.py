"""The web chat's routes (``/api/v1/web/chat/*``) as their callers see them.

* approve, pending-approvals, upload and the session routes answer the shapes
  the ``@filigran/chatbot`` panel reads;
* where chat is unavailable, every route that starts or feeds a turn (send,
  steer, approve, a new session, an upload) answers
  ``403 {detail, code: "chat_unavailable"}``; reads (a session's restore
  included) and Stop / "Continue now" stay open, as AI-disabled mode keeps them
  (``test_ai_disabled_read_surface``);
* Stop, "Continue now" and steering fail closed (503) when the turn runs on
  another replica and Redis cannot reach it, and a steer on the turn's own
  replica reaches it with neither Redis nor the bridge's poll;
* a send refused before its turn is an HTTP error with its ``detail``,
  never a 200 stream; a failure once the turn runs stays a frame;
* the message stream says something at most ``SSE_KEEPALIVE_SECONDS`` after
  its last frame.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import deps
from app.api.v1 import chat as chat_module
from app.api.v1 import chat_web_stream
from app.core.database import get_db
from app.services import chat_turn_guard, chat_turn_stream, chat_web_transport
from app.services.chat_messages import sent_message_text
from tests.test_chat_web_sse_transport import _Asgi, _stream, _wait_until, web_env  # noqa: F401 - fixtures
from tests.test_chat_web_steering import (  # noqa: F401 - fixtures
    _FakeSession,
    _patch_general_chat,
    _start,
    _steer,
    _turn_running,
    fake_rl,
    ws_env,
)


# ── A client on the web chat router, as the app serves it ─────────────────
class _Db:
    """Every lookup finds *conv* (``None``: nothing found); writes are recorded."""

    def __init__(self, conv=None):
        self.conv = conv
        self.added: list = []
        self.commits = 0

    async def execute(self, _stmt):
        conv = self.conv

        class _R:
            def scalar_one_or_none(self):
                return conv

            def scalar(self):
                return conv

        return _R()

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        for row in self.added:
            if getattr(row, "id", None) is None:
                row.id = uuid.uuid4()

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        return None

    def in_transaction(self):
        return False


def _user(user_id=None):
    return SimpleNamespace(
        id=user_id or uuid.uuid4(),
        auth_credential_kind="session_jwt",
        web_session=SimpleNamespace(),
        impersonated_by=None,
        is_admin=False,
    )


def _client(db: _Db, user, *, gated: bool = False) -> TestClient:
    """The router under ``/api/v1/web``, its refusals rendered by the app's own handler."""
    from app.main import _log_http_exception

    app = FastAPI()
    app.include_router(chat_web_stream.router, prefix="/api/v1/web")
    app.add_exception_handler(StarletteHTTPException, _log_http_exception)

    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[chat_web_stream.web_chat_user] = lambda: user
    app.dependency_overrides[deps.get_current_user] = lambda: user
    if not gated:
        app.dependency_overrides[deps.require_chat_enabled] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def _conv(user_id, **fields):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        channel="web",
        agent_id=None,
        original_agent_id=None,
        workspace_id=None,
        **fields,
    )


# ── Chat unavailable ──────────────────────────────────────────────────────
#: What starts or feeds a turn. Reads, a session's own metadata and Stop /
#: "Continue now" stay open, as AI-disabled mode keeps them
#: (``tests/test_ai_disabled_read_surface.py``, ``tests/test_ai_toggle.py``).
_CHAT_ACTIONS = {
    ("POST", "/chat/messages"),
    ("POST", "/chat/messages/steer"),
    ("POST", "/chat/messages/approve"),
    # A new session only: the conversation named below exists nowhere.
    ("POST", "/chat/sessions"),
    ("POST", "/chat/upload"),
}


def _web_routes() -> list[tuple[str, str]]:
    return sorted(
        (method, route.path)
        for route in chat_web_stream.router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    )


def _url(path: str) -> str:
    for name in ("conversation_id", "message_id", "workspace_id", "artifact_id"):
        path = path.replace("{" + name + "}", str(uuid.uuid4()))
    return "/api/v1/web" + path.replace("{kind}", "presentation")


@pytest.fixture
def chat_unlicensed(monkeypatch):
    import app.api.v1.platform as platform_module
    import app.services.ai_state as ai_state

    async def _no(*_a, **_kw):
        return False

    async def _yes(*_a, **_kw):
        return True

    async def _edition(_db):
        return "community"

    monkeypatch.setattr(platform_module, "is_chat_enabled", _no)
    monkeypatch.setattr(platform_module, "is_chat_licensed", _no)
    monkeypatch.setattr(platform_module, "get_deployment_edition", _edition)
    monkeypatch.setattr(ai_state, "is_ai_enabled", _yes)


@pytest.mark.parametrize(("method", "path"), _web_routes())
def test_only_the_routes_that_start_or_feed_a_turn_answer_chat_unavailable(chat_unlicensed, method, path):
    user = _user()
    client = _client(_Db(None), user, gated=True)
    body = {"conversation_id": str(uuid.uuid4()), "content": "hello"}
    if path == "/chat/upload":
        response = client.request(method, _url(path), data={"conversation_id": str(uuid.uuid4())})
    elif method in {"POST", "PATCH"}:
        response = client.request(method, _url(path), json=body)
    else:
        response = client.request(method, _url(path))

    if (method, path) in _CHAT_ACTIONS:
        assert response.status_code == 403, response.text
        assert response.json() == {"detail": deps.chat_unavailable_detail(), "code": deps.CHAT_UNAVAILABLE_CODE}
        return
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else None
    assert not (isinstance(body, dict) and body.get("code") == deps.CHAT_UNAVAILABLE_CODE), response.text


# ── Web sessions only ────────────────────────────────────────────────────
def _caller_client(kind: str) -> TestClient:
    """The router with only the caller's authentication stubbed: *kind* is its credential."""
    user = _user()
    user.auth_credential_kind = kind
    client = _client(_Db(None), user)
    client.app.dependency_overrides.pop(chat_web_stream.web_chat_user)
    return client


def _request(client: TestClient, method: str, path: str):
    if path == "/chat/upload":
        return client.request(method, _url(path), data={"conversation_id": str(uuid.uuid4())})
    if method in {"POST", "PATCH"}:
        return client.request(method, _url(path), json={"conversation_id": str(uuid.uuid4()), "content": "hi"})
    return client.request(method, _url(path))


_WEB_SESSION_REFUSAL = {"detail": "The web chat needs a web session"}


@pytest.mark.parametrize("kind", ["api_key", "platform_jwt"])
@pytest.mark.parametrize(("method", "path"), _web_routes())
def test_every_web_route_refuses_a_caller_without_a_web_session(kind, method, path):
    """The re-mounted handlers (quota, prompts, artifacts, workspaces...) included:
    ``/api/v1/web`` has one auth rule, on its router."""
    response = _request(_caller_client(kind), method, path)
    assert response.status_code == 403, response.text
    assert response.json() == _WEB_SESSION_REFUSAL


@pytest.mark.parametrize(("method", "path"), _web_routes())
def test_every_web_route_admits_a_web_session(method, path):
    response = _request(_caller_client("session_jwt"), method, path)
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else None
    assert body != _WEB_SESSION_REFUSAL, response.text


# ── Approve ──────────────────────────────────────────────────────────────
class _ApprovalDb:
    """The ownership lookup, then the existence lookup, answered from *env*."""

    def __init__(self, env):
        self._answers = [uuid.uuid4() if env.owned else None, uuid.uuid4() if env.exists else None]

    async def execute(self, _stmt):
        answer = self._answers.pop(0)

        class _R:
            def scalar_one_or_none(self):
                return answer

        return _R()


@pytest.fixture
def approvals(monkeypatch, ws_env):  # noqa: F811
    from app.services import chat_approval_resume

    env = SimpleNamespace(pushed=[], refused=[], waiting=True, owned=True, exists=True, kv=ws_env.fake_kv)

    async def _push(conversation_id, decisions):
        env.pushed.append((conversation_id, decisions))
        return env.waiting

    async def _refuse(_db, **kwargs):
        env.refused.append(kwargs["reason"])

    monkeypatch.setattr(chat_turn_guard, "push_approval_decision", _push)
    monkeypatch.setattr(chat_approval_resume, "record_refused_decision", _refuse)
    return env


def _approve(env, user, conversation_id, decisions):
    body = {"conversation_id": conversation_id, "decisions": decisions}
    return _client(_ApprovalDb(env), user).post("/api/v1/web/chat/messages/approve", json=body)


def test_an_approval_reaches_the_waiting_turn(approvals):
    conversation_id = str(uuid.uuid4())
    response = _approve(
        approvals,
        _user(),
        conversation_id,
        [{"tool_call_id": "c1", "decision": "approve"}, {"tool_call_id": "c2", "decision": "reject"}],
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "decided": 2}
    [(pushed_for, decisions)] = approvals.pushed
    assert pushed_for == conversation_id
    assert {k: v["decision"] for k, v in decisions.items()} == {"c1": "approve", "c2": "reject"}
    assert approvals.kv.kv[chat_turn_guard.client_seen_key(conversation_id)] == "1"


def test_an_approval_with_nothing_waiting_is_409_and_recorded(approvals):
    approvals.waiting = False
    response = _approve(approvals, _user(), str(uuid.uuid4()), [{"tool_call_id": "c1", "decision": "approve"}])

    assert response.status_code == 409
    assert approvals.refused == ["no_pending_proposal"]


def test_an_approval_for_another_users_conversation_is_404_and_recorded(approvals):
    approvals.owned = False
    response = _approve(approvals, _user(), str(uuid.uuid4()), [{"tool_call_id": "c1", "decision": "approve"}])

    assert response.status_code == 404
    assert approvals.pushed == []
    assert approvals.refused == ["not_the_paused_user"]


def test_an_approval_without_decisions_is_400(approvals):
    assert _approve(approvals, _user(), str(uuid.uuid4()), []).status_code == 400


def test_an_approval_naming_no_tool_call_is_400(approvals):
    response = _approve(approvals, _user(), str(uuid.uuid4()), [{"tool_call_id": "", "decision": "approve"}])
    assert response.status_code == 400
    assert approvals.pushed == []


def test_an_approval_reaches_the_turn_under_the_id_its_keys_are_written_with(approvals):
    conversation_id = str(uuid.uuid4())
    response = _approve(approvals, _user(), conversation_id.upper(), [{"tool_call_id": "c1", "decision": "approve"}])

    assert response.status_code == 200
    assert [pushed_for for pushed_for, _ in approvals.pushed] == [conversation_id]
    assert approvals.kv.kv[chat_turn_guard.client_seen_key(conversation_id)] == "1"


def test_an_unknown_verdict_is_refused_before_it_reaches_the_turn(approvals):
    response = _approve(approvals, _user(), str(uuid.uuid4()), [{"tool_call_id": "c1", "decision": "maybe"}])
    assert response.status_code == 422
    assert approvals.pushed == []


def test_an_approval_counts_only_the_decisions_it_handed_over(approvals):
    response = _approve(
        approvals,
        _user(),
        str(uuid.uuid4()),
        [{"tool_call_id": "", "decision": "approve"}, {"tool_call_id": "c1", "decision": "reject"}],
    )

    assert response.json() == {"status": "accepted", "decided": 1}
    [(_conv_id, decisions)] = approvals.pushed
    assert list(decisions) == ["c1"]


@pytest.fixture
async def redis_down(monkeypatch):
    import app.core.redis as redis_module
    from tests import redis_unreachable

    client, _pool = redis_unreachable.unreachable_redis()

    async def _refusing():
        return client

    monkeypatch.setattr(redis_module, "redis_client", _refusing)
    yield
    await client.aclose()


async def test_an_approval_redis_cannot_carry_is_503_and_applies_nothing(redis_down, monkeypatch):
    from app.api.v1 import platform as platform_module
    from app.services import chat_approval_resume

    refused: list = []

    async def _refuse(_db, **kwargs):
        refused.append(kwargs)

    monkeypatch.setattr(chat_approval_resume, "record_refused_decision", _refuse)
    owned = SimpleNamespace(owned=True, exists=True)
    decisions = [{"tool_call_id": "c1", "decision": "approve"}]
    conversation_id = str(uuid.uuid4())

    web = await asyncio.to_thread(_approve, owned, _user(), conversation_id, decisions)
    with pytest.raises(HTTPException) as platform:
        await platform_module.approve_platform_chat_tools(
            platform_module.PlatformChatApprovalRequest(conversation_id=conversation_id, decisions=decisions),
            db=_ApprovalDb(owned),
            user=_user(),
            _chat_gate=None,
        )

    assert (web.status_code, web.json()["detail"]) == (503, platform_module.APPROVAL_UNAVAILABLE_DETAIL)
    assert (platform.value.status_code, platform.value.detail) == (503, platform_module.APPROVAL_UNAVAILABLE_DETAIL)
    assert refused == []


# ── Pending approvals ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("path", "fields"),
    [
        ("/api/v1/web/chat/messages", {"content": "hi", "source_pack_slugs": ["web-general", 7]}),
        ("/api/v1/web/chat/messages", {"content": "hi", "source_pack_slugs": "web-general"}),
        ("/api/v1/web/chat/messages", {"content": "hi", "file_ids": ["f1", 7]}),
        ("/api/v1/web/chat/messages/steer", {"content": "hi", "file_ids": ["f1", {"id": "f2"}]}),
    ],
)
def test_a_send_or_steer_naming_anything_but_strings_is_422(path, fields):
    """The request model is the one validator: nothing past it filters the lists again."""
    user = _user()
    conv = _conv(user.id)
    client = _client(_Db(conv), user)

    response = client.post(path, json={"conversation_id": str(conv.id), **fields})

    assert response.status_code == 422


def test_pending_approvals_say_what_the_paused_turn_waits_on(ws_env):  # noqa: F811
    conversation_id = str(ws_env.conv.id)
    proposals = [{"tool_call_id": "c1", "name": "delete_report"}]
    ws_env.fake_kv.kv[chat_turn_guard.processing_key(conversation_id)] = "turn-a"
    ws_env.fake_kv.kv[chat_turn_guard.pending_approvals_key(conversation_id)] = json.dumps(proposals)

    response = _client(_Db(ws_env.conv), _user(ws_env.user_id)).get(
        f"/api/v1/web/chat/conversations/{conversation_id}/pending-approvals"
    )

    assert response.status_code == 200
    assert response.json() == {"conversation_id": conversation_id, "proposals": proposals, "turn": "running"}


def test_pending_approvals_of_an_idle_conversation(ws_env):  # noqa: F811
    conversation_id = str(ws_env.conv.id)
    response = _client(_Db(ws_env.conv), _user(ws_env.user_id)).get(
        f"/api/v1/web/chat/conversations/{conversation_id}/pending-approvals"
    )
    assert response.json() == {"conversation_id": conversation_id, "proposals": [], "turn": "idle"}


def test_pending_approvals_are_503_when_redis_cannot_say(ws_env, monkeypatch):  # noqa: F811
    """An ``idle`` the panel trusted would drop the prompt the turn still waits on."""

    async def _down(*_a, **_kw):
        raise ConnectionError("redis down")

    monkeypatch.setattr(chat_turn_guard, "is_turn_active", _down)
    response = _client(_Db(ws_env.conv), _user(ws_env.user_id)).get(
        f"/api/v1/web/chat/conversations/{ws_env.conv.id}/pending-approvals"
    )
    assert response.status_code == 503


def test_pending_approvals_of_another_users_conversation_are_404():
    response = _client(_Db(None), _user()).get(
        f"/api/v1/web/chat/conversations/{uuid.uuid4()}/pending-approvals"
    )
    assert response.status_code == 404


# ── Events ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("client_id", ["x" * 129, "tab\n1"])
def test_an_events_stream_replaces_an_invalid_client_id(monkeypatch, client_id):
    origins: list[str] = []

    async def _relay(_conv_id, *, origin, send, **_kw):
        origins.append(origin)
        await send({"type": "conversation_title", "title": "Logs"})

    async def _release(_db):
        return None

    monkeypatch.setattr(chat_turn_stream, "relay_turn_frames", _relay)
    monkeypatch.setattr(chat_web_stream, "release_db_connection_before_loop", _release)
    user = _user()
    conv = _conv(user.id)

    response = _client(_Db(conv), user).get(
        f"/api/v1/web/chat/conversations/{conv.id}/events", params={"client_id": client_id}
    )

    assert response.status_code == 200, response.text
    [origin] = origins
    assert origin != client_id and len(origin) == 32


# ── Upload ───────────────────────────────────────────────────────────────
@pytest.fixture
def storage(monkeypatch):
    from app.services import chunking, file_storage

    uploaded: list[dict] = []

    async def _upload(**kwargs):
        uploaded.append(kwargs)
        return f"chat/{uuid.uuid4()}"

    async def _text(_raw, _filename):
        return "the extracted text"

    monkeypatch.setattr(file_storage.file_storage, "upload", _upload)
    monkeypatch.setattr(chunking, "extract_text_from_file_async", _text)
    return uploaded


def test_an_upload_stores_the_file_for_the_next_message(storage):
    user = _user()
    db = _Db(_conv(user.id))
    response = _client(db, user).post(
        "/api/v1/web/chat/upload",
        data={"conversation_id": str(db.conv.id)},
        files={"file": ("notes.txt", b"some notes", "text/plain")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["file_ids"] == [body["file_id"]]
    assert body["filename"] == "notes.txt" and body["size"] == len(b"some notes")
    [row] = db.added
    assert str(row.id) == body["file_id"] and row.conversation_id == db.conv.id
    assert db.commits == 1
    assert storage[0]["prefix"] == f"chat/{db.conv.id}"


def test_an_upload_over_the_size_limit_is_413(storage, monkeypatch):
    from app.services import file_storage

    monkeypatch.setattr(file_storage, "MAX_UPLOAD_SIZE_BYTES", 4)
    user = _user()
    db = _Db(_conv(user.id))
    response = _client(db, user).post(
        "/api/v1/web/chat/upload",
        data={"conversation_id": str(db.conv.id)},
        files={"file": ("big.txt", b"more than four bytes", "text/plain")},
    )

    assert response.status_code == 413
    assert storage == [] and db.added == []


def test_an_upload_to_another_users_conversation_is_404(storage):
    response = _client(_Db(None), _user()).post(
        "/api/v1/web/chat/upload",
        data={"conversation_id": str(uuid.uuid4())},
        files={"file": ("notes.txt", b"some notes", "text/plain")},
    )
    assert response.status_code == 404
    assert storage == []


def test_an_upload_without_a_file_is_422(storage):
    response = _client(_Db(None), _user()).post(
        "/api/v1/web/chat/upload", data={"conversation_id": str(uuid.uuid4())}
    )
    assert response.status_code == 422


# ── Sessions ─────────────────────────────────────────────────────────────
def test_a_new_session_is_a_new_conversation(monkeypatch):
    created_id = str(uuid.uuid4())

    async def _create(*, body, user, db, _chat_gate):
        return SimpleNamespace(id=created_id, agent_id=None, workspace_id=body.workspace_id, channel="web")

    async def _chat_available(_db):
        return None

    monkeypatch.setattr(chat_module, "create_conversation", _create)
    monkeypatch.setattr(chat_web_stream, "require_chat_enabled", _chat_available)
    response = _client(_Db(None), _user()).post("/api/v1/web/chat/sessions", json={})

    assert response.status_code == 200
    assert response.json() == {
        "conversation_id": created_id,
        "messages": [],
        "agent_id": None,
        "agent_name": None,
        "workspace_id": None,
        "channel": "web",
        "has_more": False,
        "is_processing": False,
        "pending_approvals": None,
        "interrupted_turn": None,
    }


@pytest.fixture
def restore(monkeypatch):
    from app.api.v1 import platform as platform_module

    async def _window(_db, _user, conv, *, supports_artifacts, keep_dashboards=False):
        # The web panel draws a dashboard's marker as the dashboard: always kept here.
        assert keep_dashboards is True
        return platform_module.PlatformChatSessionResponse(conversation_id=str(conv.id), messages=[]), True

    monkeypatch.setattr(platform_module, "restore_chat_session_window", _window)


@pytest.fixture
def turn_state_redis(monkeypatch, ws_env):  # noqa: F811
    """``chat.read_conversation_turn_state`` reads the harness's Redis."""

    async def _client():
        return ws_env.fake_kv

    monkeypatch.setattr(chat_module, "redis_client", _client)
    return ws_env


def test_a_restore_stays_open_while_chat_is_unavailable(chat_unlicensed, ws_env, turn_state_redis, restore):  # noqa: F811
    conversation_id = str(ws_env.conv.id)
    response = _client(_Db(ws_env.conv), _user(ws_env.user_id), gated=True).post(
        "/api/v1/web/chat/sessions", json={"conversation_id": conversation_id}
    )

    assert response.status_code == 200, response.text
    assert response.json()["conversation_id"] == conversation_id


def test_a_new_session_is_refused_while_chat_is_unavailable(chat_unlicensed, monkeypatch):
    created: list = []

    async def _create(**kwargs):
        created.append(kwargs)

    monkeypatch.setattr(chat_module, "create_conversation", _create)
    response = _client(_Db(None), _user(), gated=True).post(
        "/api/v1/web/chat/sessions", json={"conversation_id": str(uuid.uuid4())}
    )

    assert response.status_code == 403
    assert response.json() == {"detail": deps.chat_unavailable_detail(), "code": deps.CHAT_UNAVAILABLE_CODE}
    assert created == []


def test_a_restore_says_where_the_turn_stands(ws_env, turn_state_redis, restore):  # noqa: F811
    """What ``GET /api/v1/chat/conversations/{id}`` says, from the same builder."""
    conversation_id = str(ws_env.conv.id)
    proposals = [{"tool_call_id": "c1", "name": "delete_report", "arguments": {"id": 1}}]
    ws_env.fake_kv.kv[chat_turn_guard.processing_key(conversation_id)] = "turn-a"
    ws_env.fake_kv.kv[chat_turn_guard.pending_approvals_key(conversation_id)] = json.dumps(proposals)

    response = _client(_Db(ws_env.conv), _user(ws_env.user_id)).post(
        "/api/v1/web/chat/sessions", json={"conversation_id": conversation_id}
    )

    body = response.json()
    assert body["has_more"] is True
    assert (body["is_processing"], body["pending_approvals"], body["interrupted_turn"]) == (True, proposals, None)


def test_a_restore_shows_the_turn_that_ended_without_its_answer(ws_env, turn_state_redis, restore, monkeypatch):  # noqa: F811
    from datetime import UTC, datetime

    async def _interrupted(_conversation_id):
        return SimpleNamespace(started_at=datetime(2026, 10, 1, tzinfo=UTC), interrupted_at=None)

    monkeypatch.setattr(chat_turn_guard, "interrupted_turn", _interrupted)
    response = _client(_Db(ws_env.conv), _user(ws_env.user_id)).post(
        "/api/v1/web/chat/sessions", json={"conversation_id": str(ws_env.conv.id)}
    )

    body = response.json()
    assert body["is_processing"] is False
    assert body["interrupted_turn"]["started_at"].startswith("2026-10-01")


async def test_a_shared_viewer_reads_the_pause_without_its_arguments(ws_env, turn_state_redis):  # noqa: F811
    conversation_id = str(ws_env.conv.id)
    proposals = [{"tool_call_id": "c1", "name": "delete_report", "arguments": {"id": 1}}]
    ws_env.fake_kv.kv[chat_turn_guard.processing_key(conversation_id)] = "turn-a"
    ws_env.fake_kv.kv[chat_turn_guard.pending_approvals_key(conversation_id)] = json.dumps(proposals)

    owner = await chat_module.read_conversation_turn_state(_Db(ws_env.conv), ws_env.conv, owner=True)
    viewer = await chat_module.read_conversation_turn_state(_Db(ws_env.conv), ws_env.conv, owner=False)

    assert owner.pending_approvals == proposals
    assert viewer.is_processing is True
    assert "arguments" not in viewer.pending_approvals[0]


class _ThreadDb:
    """Answers the restore's reads by the table each statement reads: *rows* oldest first."""

    def __init__(self, conv, rows: list, agent_names: dict):
        self.conv = conv
        self.rows = rows
        self.agent_names = agent_names

    async def execute(self, stmt):
        table = stmt.get_final_froms()[0].name
        rows, pairs, scalar = [], [], None
        if table == "conversations":
            scalar = self.conv
        elif table == "messages":
            rows = list(reversed(self.rows))
        elif table == "agents":
            pairs = list(self.agent_names.items())
        return SimpleNamespace(
            scalar_one_or_none=lambda: scalar,
            scalars=lambda: SimpleNamespace(all=lambda: rows),
            all=lambda: pairs,
        )

    async def commit(self):
        return None

    async def rollback(self):
        return None

    def in_transaction(self):
        return False


def _mention_thread():
    from datetime import UTC, datetime

    def _row(role, content, meta=None):
        return SimpleNamespace(
            id=uuid.uuid4(),
            role=role,
            content=content,
            metadata_json=json.dumps(meta) if meta else None,
            created_at=datetime(2026, 10, 1, tzinfo=UTC),
        )

    return [
        _row("user", "@Beta @Gamma what changed?"),
        _row("assistant", "Alpha's answer"),
        _row("assistant", "Beta's answer", {"answered_by": {"agent_id": "b", "agent_name": "Beta"}}),
        _row(
            "assistant",
            "Gamma could not answer.",
            {"answered_by": {"agent_id": "g", "agent_name": "Gamma"}, "mention_failed": True},
        ),
        _row("user", "thanks"),
        _row("assistant", "Alpha again"),
    ]


def _authorship(messages: list[dict]) -> list[tuple]:
    return [(m.get("agent_name"), m.get("answered_by"), m.get("mention_failed")) for m in messages]


@pytest.mark.parametrize("stand_in", [None, "Linked Assistant"])
def test_a_restore_of_a_conversation_without_an_agent_names_who_answers_it(
    ws_env, turn_state_redis, monkeypatch, stand_in  # noqa: F811
):
    """No agent of its own: the conversation is answered by the agent standing in
    for the General Assistant, else by the General Assistant, and the restore
    names that one, never leaving the panel to name the agent picked for new chats."""
    from app.services import general_assistant_link

    asked: list[dict] = []

    async def _turn_stand_in(_db, **kwargs):
        asked.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4(), name=stand_in) if stand_in else None

    monkeypatch.setattr(general_assistant_link, "turn_stand_in", _turn_stand_in)
    ws_env.conv.agent_id = None
    ws_env.conv.original_agent_id = None
    ws_env.conv.workspace_id = None
    db = _ThreadDb(ws_env.conv, [], {})

    response = _client(db, _user(ws_env.user_id)).post(
        "/api/v1/web/chat/sessions", json={"conversation_id": str(ws_env.conv.id)}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["agent_id"] is None
    assert body["agent_name"] == (stand_in or "General Assistant")
    assert asked and asked[0]["agent_id"] is None and asked[0]["conversation_channel"] == ws_env.conv.channel


def test_a_restore_names_who_answered_each_reply_of_a_mention_turn(ws_env, turn_state_redis):  # noqa: F811
    """``answered_by`` / ``mention_failed`` as the live ``done`` carries them; the
    reply's author wins over the conversation's agent, which stays the agent of
    the replies after it."""
    alpha = uuid.uuid4()
    ws_env.conv.agent_id = alpha
    ws_env.conv.original_agent_id = alpha
    ws_env.conv.workspace_id = None
    db = _ThreadDb(ws_env.conv, _mention_thread(), {alpha: "Alpha"})

    response = _client(db, _user(ws_env.user_id)).post(
        "/api/v1/web/chat/sessions", json={"conversation_id": str(ws_env.conv.id)}
    )

    assert response.status_code == 200
    assert _authorship(response.json()["messages"]) == [
        (None, None, None),
        ("Alpha", None, None),
        ("Beta", {"agent_id": "b", "agent_name": "Beta"}, None),
        ("Gamma", {"agent_id": "g", "agent_name": "Gamma"}, True),
        (None, None, None),
        ("Alpha", None, None),
    ]


def test_a_restore_marks_the_replies_a_cut_turn_left_and_the_one_its_resume_ended_with(ws_env, turn_state_redis):  # noqa: F811
    """``interrupted`` and ``resumed_from`` as the REST serializer carries them: a host folds them into one card."""
    alpha = uuid.uuid4()
    ws_env.conv.agent_id = alpha
    ws_env.conv.original_agent_id = alpha
    ws_env.conv.workspace_id = None
    from datetime import UTC, datetime

    resumed_from = {"cause": "stop", "resume": 1, "of": 20, "cut_at": "2026-11-06T09:00:00Z"}

    def _row(role, content, meta=None):
        return SimpleNamespace(
            id=uuid.uuid4(),
            role=role,
            content=content,
            metadata_json=json.dumps(meta) if meta else None,
            created_at=datetime(2026, 11, 6, tzinfo=UTC),
        )

    thread = [
        _row("user", "run the sweep"),
        _row("assistant", "This turn was interrupted before it could finish.", {"interrupted": True}),
        _row("assistant", "Sweep done.", {"resumed_from": resumed_from}),
        _row("assistant", "A plain answer"),
    ]
    db = _ThreadDb(ws_env.conv, thread, {alpha: "Alpha"})

    response = _client(db, _user(ws_env.user_id)).post(
        "/api/v1/web/chat/sessions", json={"conversation_id": str(ws_env.conv.id)}
    )

    assert response.status_code == 200
    assert [(m.get("interrupted"), m.get("resumed_from")) for m in response.json()["messages"]] == [
        (None, None),
        (True, None),
        (None, resumed_from),
        (None, None),
    ]


def test_a_page_of_messages_names_who_answered_each_reply(ws_env, monkeypatch):  # noqa: F811
    """A page starts mid-thread: only a reply that records its author is named."""
    db = _ThreadDb(ws_env.conv, _mention_thread(), {})

    async def _readable(_db, _user, _conversation_id):
        return ws_env.conv

    monkeypatch.setattr(chat_web_stream, "_readable_conversation", _readable)
    response = _client(db, _user(ws_env.user_id)).get(f"/api/v1/web/chat/sessions/{ws_env.conv.id}/messages")

    assert response.status_code == 200
    assert _authorship(response.json()["messages"]) == [
        (None, None, None),
        (None, None, None),
        ("Beta", {"agent_id": "b", "agent_name": "Beta"}, None),
        ("Gamma", {"agent_id": "g", "agent_name": "Gamma"}, True),
        (None, None, None),
        (None, None, None),
    ]


def test_the_session_list_names_each_conversation(monkeypatch):
    row = SimpleNamespace(id="c-1", model_dump=lambda: {"id": "c-1", "title": "Logs"})
    seen: dict = {}

    async def _list(**kwargs):
        seen.update(kwargs)
        return [row]

    monkeypatch.setattr(chat_module, "list_conversations", _list)
    response = _client(_Db(None), _user()).get("/api/v1/web/chat/sessions?limit=10&scope=shared")

    assert response.json() == {"conversations": [{"id": "c-1", "title": "Logs", "conversation_id": "c-1"}]}
    assert (seen["limit"], seen["scope"], seen["archived"]) == (10, "shared", False)


def test_a_session_rename_answers_what_it_now_is(monkeypatch):
    conversation_id = str(uuid.uuid4())

    async def _update(*, conversation_id, body, user, db):
        return SimpleNamespace(id=conversation_id, title=body.title, workspace_id=None)

    monkeypatch.setattr(chat_module, "update_conversation", _update)
    client = _client(_Db(None), _user())
    response = client.patch(f"/api/v1/web/chat/sessions/{conversation_id}", json={"title": "Renamed"})

    assert response.json() == {"conversation_id": conversation_id, "title": "Renamed", "workspace_id": None}
    assert client.patch(f"/api/v1/web/chat/sessions/{conversation_id}", json={}).status_code == 422


def test_deleting_one_session_or_all_is_204(monkeypatch):
    archived: list = []

    async def _archive(*, conversation_id, user, db):
        archived.append(conversation_id)

    async def _archive_all(_db, _user, *, web_listed_only):
        archived.append("all listed" if web_listed_only else "all")

    monkeypatch.setattr(chat_module, "archive_conversation", _archive)
    monkeypatch.setattr(chat_module, "archive_user_conversations", _archive_all)
    client = _client(_Db(None), _user())
    conversation_id = str(uuid.uuid4())

    one = client.delete(f"/api/v1/web/chat/sessions/{conversation_id}")
    every = client.delete("/api/v1/web/chat/sessions")

    assert (one.status_code, one.content) == (204, b"")
    assert (every.status_code, every.content) == (204, b"")
    assert archived == [conversation_id, "all listed"]


# ── Stop, "Continue now", steering: fail closed ──────────────────────────
@pytest.fixture
def remote_turn(monkeypatch, ws_env):  # noqa: F811
    """The conversation's turn runs on another replica: nothing here holds it."""
    stopped: list[str] = []

    async def _stop(conversation_id, request_cancel):
        requested = await request_cancel()
        stopped.append(conversation_id)
        return requested

    monkeypatch.setattr(chat_module, "stop_conversation_work", _stop)
    ws_env.stopped = stopped
    return ws_env


def _down(*_a, **_kw):
    raise ConnectionError("redis down")


async def _redis_down(*_a, **_kw):
    _down()


@pytest.mark.parametrize(("route", "request_name"), [("cancel", "request_turn_cancel"), ("skip-wait", "request_wait_skip")])
def test_a_control_with_the_turn_elsewhere_and_redis_down_is_503(remote_turn, monkeypatch, route, request_name):
    monkeypatch.setattr(chat_turn_guard, request_name, _redis_down)
    response = _client(_Db(remote_turn.conv), _user(remote_turn.user_id)).post(
        f"/api/v1/web/chat/messages/{route}", json={"conversation_id": str(remote_turn.conv.id)}
    )
    assert response.status_code == 503
    assert response.json()["detail"] == chat_web_stream.CONTROL_UNAVAILABLE_DETAIL
    # Nothing reached the turn, so nothing of its work was stopped either.
    assert remote_turn.stopped == []


@pytest.mark.parametrize(("route", "status_word"), [("cancel", "cancelled"), ("skip-wait", "wait_skipped")])
def test_a_control_that_reached_redis_is_202(remote_turn, route, status_word):
    conversation_id = str(remote_turn.conv.id)
    remote_turn.fake_kv.kv[chat_turn_guard.processing_key(conversation_id)] = "turn-a"
    response = _client(_Db(remote_turn.conv), _user(remote_turn.user_id)).post(
        f"/api/v1/web/chat/messages/{route}", json={"conversation_id": conversation_id}
    )
    assert response.status_code == 202
    assert response.json() == {"status": status_word, "turn": "remote"}


def test_a_stop_with_no_turn_anywhere_is_still_202(remote_turn):
    response = _client(_Db(remote_turn.conv), _user(remote_turn.user_id)).post(
        "/api/v1/web/chat/messages/cancel", json={"conversation_id": str(remote_turn.conv.id)}
    )
    assert response.status_code == 202
    assert response.json() == {"status": "cancelled", "turn": "none"}


@pytest.mark.parametrize("awaiting_send", [False, True])
def test_a_stop_with_no_turn_is_kept_only_for_a_send_on_its_way(remote_turn, awaiting_send):
    conversation_id = str(remote_turn.conv.id)
    response = _client(_Db(remote_turn.conv), _user(remote_turn.user_id)).post(
        "/api/v1/web/chat/messages/cancel",
        json={"conversation_id": conversation_id, "awaiting_send": awaiting_send},
        headers={chat_web_stream.CLIENT_ID_HEADER: "tab-1"},
    )
    assert response.json() == {"status": "cancelled", "turn": "none"}
    kept = chat_turn_guard.pending_cancel_key(conversation_id, "tab-1")
    assert (kept in remote_turn.fake_kv.kv) is awaiting_send


async def test_steering_with_the_turn_elsewhere_and_redis_down_is_503(ws_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(chat_turn_guard, "is_turn_active", _redis_down)
    answer = await _steer(ws_env, "also the logs")
    assert isinstance(answer, HTTPException) and answer.status_code == 503
    assert ws_env.stored == []


async def test_steering_whose_hand_off_fails_on_redis_is_stored_not_503(ws_env, monkeypatch):  # noqa: F811
    """The push may have landed: the message stays, and the client keeps its bubble rather than resending a copy."""
    ws_env.fake_kv.kv[chat_turn_guard.processing_key(str(ws_env.conv.id))] = "turn-a"
    monkeypatch.setattr(chat_turn_guard, "fold_into_inflight_turn", _redis_down)
    withdrawn: list = []

    async def _withdraw(steer):
        withdrawn.append(steer)

    monkeypatch.setattr(chat_module, "withdraw_web_steer", _withdraw)
    answer = await _steer(ws_env, "also the logs", steer_id="bubble-1")
    assert answer == {"status": "stored", "steer_id": "bubble-1", "turn_id": None}
    assert ws_env.stored == [("user", "also the logs")]
    assert withdrawn == []


async def test_an_unanswered_steer_that_cannot_be_withdrawn_is_stored_not_503(ws_env, monkeypatch):  # noqa: F811
    """No turn took it and it stays in the thread: a 503 would have the client store it a second time."""

    async def _not_folded(*_a, **_kw):
        return False

    async def _withdraw_fails(_steer):
        raise ConnectionError("database down")

    ws_env.fake_kv.kv[chat_turn_guard.processing_key(str(ws_env.conv.id))] = "turn-a"
    monkeypatch.setattr(chat_turn_guard, "fold_into_inflight_turn", _not_folded)
    monkeypatch.setattr(chat_module, "withdraw_web_steer", _withdraw_fails)
    answer = await _steer(ws_env, "also the logs", steer_id="bubble-1")
    assert answer == {"status": "stored", "steer_id": "bubble-1", "turn_id": None, "answered": False}
    assert ws_env.stored == [("user", "also the logs")]


async def test_a_refused_steer_that_cannot_be_withdrawn_says_no_turn_answers_it(ws_env, monkeypatch):  # noqa: F811
    async def _refused(*_a, **_kw):
        raise chat_turn_guard.FoldRefusedError("stopped")

    async def _withdraw_fails(_steer):
        raise ConnectionError("database down")

    ws_env.fake_kv.kv[chat_turn_guard.processing_key(str(ws_env.conv.id))] = "turn-a"
    monkeypatch.setattr(chat_turn_guard, "fold_into_inflight_turn", _refused)
    monkeypatch.setattr(chat_module, "withdraw_web_steer", _withdraw_fails)
    answer = await _steer(ws_env, "also the logs", steer_id="bubble-1")
    assert answer == {"status": "stored", "steer_id": "bubble-1", "turn_id": None, "answered": False}


async def test_an_unanswered_steer_withdrawn_is_409(ws_env, monkeypatch):  # noqa: F811
    async def _not_folded(*_a, **_kw):
        return False

    withdrawn: list = []

    async def _withdraw(steer):
        withdrawn.append(steer)

    ws_env.fake_kv.kv[chat_turn_guard.processing_key(str(ws_env.conv.id))] = "turn-a"
    monkeypatch.setattr(chat_turn_guard, "fold_into_inflight_turn", _not_folded)
    monkeypatch.setattr(chat_module, "withdraw_web_steer", _withdraw)
    answer = await _steer(ws_env, "also the logs")
    assert isinstance(answer, HTTPException) and answer.status_code == 409
    assert len(withdrawn) == 1


async def test_a_steer_the_turn_refuses_is_409(ws_env, monkeypatch):  # noqa: F811
    async def _refused(*_a, **_kw):
        raise chat_turn_guard.FoldRefusedError("stopped")

    ws_env.fake_kv.kv[chat_turn_guard.processing_key(str(ws_env.conv.id))] = "turn-a"
    monkeypatch.setattr(chat_turn_guard, "fold_into_inflight_turn", _refused)
    answer = await _steer(ws_env, "also the logs")
    assert isinstance(answer, HTTPException) and answer.status_code == 409


async def test_a_steer_on_the_turns_own_replica_reaches_it_without_redis_or_a_poll(ws_env, monkeypatch):  # noqa: F811
    """Handed to the running turn in-process, its wait ended at once."""
    monkeypatch.setattr(chat_turn_guard, "BRIDGE_POLL_SECONDS", 30)
    received: list = []
    release = asyncio.Event()

    async def _run(_db, _uid, _history, _content, **kwargs):
        from app.tools.agent_loop import drain_steering_queue

        await release.wait()
        assert await _wait_until(lambda: kwargs["wait_skip_event"].is_set(), timeout=2.0)
        received.extend(drain_steering_queue(kwargs["steering_queue"]))
        return ("final answer", None)

    _patch_general_chat(monkeypatch, _run)
    conn, task = _start(ws_env)
    turn_id = await _turn_running(conn)
    assert await _wait_until(lambda: conn.accepts_steering)
    for name in ("is_turn_active", "channel_turn_holder", "fold_into_inflight_turn", "drain_steering_inbox"):
        monkeypatch.setattr(chat_turn_guard, name, _redis_down)

    answer = await _steer(ws_env, "also the logs", steer_id="bubble-1")
    release.set()
    await asyncio.wait_for(task, 5)

    assert answer == {"status": "steered", "steer_id": "bubble-1", "turn_id": turn_id}
    assert received == ["also the logs"]
    assert ("user", "also the logs") in ws_env.stored


@pytest.mark.parametrize(
    ("content", "file_ids", "text"),
    [
        ("  look at this  ", ["f1"], "look at this"),
        ("", ["f1"], "Please analyze the attached 1 file."),
        (None, ["f1", "f2"], "Please analyze the attached 2 files."),
        ("   ", ["", "f2"], "Please analyze the attached 1 file."),
        ("   ", None, ""),
        ("", [], ""),
    ],
)
def test_a_message_of_files_alone_asks_to_analyze_them(content, file_ids, text):
    assert sent_message_text(content, file_ids) == text


async def test_a_send_of_files_alone_runs_its_turn_on_the_request_to_read_them(web_env, monkeypatch):  # noqa: F811
    """The composer sends attachments with no text: the turn still runs."""
    asked: list[str] = []

    async def _run(_db, _uid, _history, content, **_kwargs):
        asked.append(content)
        return ("read them", None)

    _patch_general_chat(monkeypatch, _run)
    asgi = _Asgi()
    _connection, response = _stream(web_env, {"content": "", "file_ids": ["f1", "f2"]})
    await asyncio.wait_for(response({"type": "http"}, asgi.receive, asgi.send), 5)

    assert _start_message(asgi)["status"] == 200
    assert asked and asked[0].startswith("Please analyze the attached 2 files.")
    assert ("user", "Please analyze the attached 2 files.") in web_env.stored


async def test_a_steer_of_files_alone_reaches_the_running_turn(ws_env, monkeypatch):  # noqa: F811
    received: list = []
    release = asyncio.Event()

    async def _run(_db, _uid, _history, _content, **kwargs):
        from app.tools.agent_loop import drain_steering_queue

        await release.wait()
        received.extend(drain_steering_queue(kwargs["steering_queue"]))
        return ("final answer", None)

    _patch_general_chat(monkeypatch, _run)
    conn, task = _start(ws_env)
    await _turn_running(conn)
    assert await _wait_until(lambda: conn.accepts_steering)

    answer = await _steer(ws_env, "", steer_id="bubble-1", file_ids=["f1"])
    release.set()
    await asyncio.wait_for(task, 5)

    assert answer["status"] == "steered"
    assert ("user", "Please analyze the attached 1 file.") in ws_env.stored


class _RecordingSession(_FakeSession):
    """The fake session, keeping each statement it ran."""

    def __init__(self, conv, statements: list):
        super().__init__(conv, [])
        self.statements = statements

    async def execute(self, stmt):
        self.statements.append(stmt)
        return await super().execute(stmt)


async def test_a_steer_whose_turn_ended_on_the_way_is_withdrawn_and_409(ws_env, monkeypatch):  # noqa: F811
    """The turn elsewhere ends between the route's check and its hand-off: the
    fold takes the push back, so no turn answers the stored copy. It is deleted,
    its attachments unlinked, never shown, and the 409 makes the client send the
    text as the next turn."""
    import app.core.database as database_module

    conversation_id = str(ws_env.conv.id)
    marker = chat_turn_guard.processing_key(conversation_id)
    ws_env.fake_kv.kv[marker] = "turn-a"
    persist = chat_module.persist_web_steer
    stored_rows: list = []

    async def _persist_then_the_turn_ends(**kwargs):
        steer = await persist(**kwargs)
        stored_rows.append(steer.message)
        del ws_env.fake_kv.kv[marker]
        return steer

    monkeypatch.setattr(chat_module, "persist_web_steer", _persist_then_the_turn_ends)
    published: list = []
    monkeypatch.setattr(chat_turn_stream, "publish_user_message", lambda *a, **kw: published.append(a))
    statements: list = []
    monkeypatch.setattr(database_module, "async_session", lambda: _RecordingSession(ws_env.conv, statements))

    answer = await _steer(ws_env, "also the logs")

    assert isinstance(answer, HTTPException) and answer.status_code == 409
    assert answer.detail == chat_web_stream.NO_RUNNING_TURN_DETAIL
    assert published == []
    assert ws_env.fake_kv.lists.get(chat_turn_guard.steering_inbox_key(conversation_id), []) == []
    [message_id] = [row.id for row in stored_rows]
    withdrawn = [
        (stmt.__visit_name__, stmt.table.name, stmt.compile().params)
        for stmt in statements
        if stmt.__visit_name__ in ("update", "delete")
    ]
    assert ("update", "file_attachments", {"message_id": None, "message_id_1": message_id}) in withdrawn
    assert ("delete", "messages", {"id_1": message_id}) in withdrawn


async def test_a_withdrawn_steer_and_its_resend_cost_one_send(web_env, monkeypatch):  # noqa: F811
    """The steer no turn took is deleted and refused; the panel sends its text as
    the next turn. Only that turn's message is kept, so only it is charged."""
    conversation_id = str(web_env.conv.id)
    marker = chat_turn_guard.processing_key(conversation_id)
    web_env.fake_kv.kv[marker] = "turn-a"
    persist = chat_module.persist_web_steer

    async def _persist_then_the_turn_ends(**kwargs):
        steer = await persist(**kwargs)
        del web_env.fake_kv.kv[marker]
        return steer

    monkeypatch.setattr(chat_module, "persist_web_steer", _persist_then_the_turn_ends)

    async def _run(*_a, **_kw):
        return ("answer", None)

    _patch_general_chat(monkeypatch, _run)
    bucket = f"rl:chat-send:{web_env.user_id}"

    answer = await _steer(web_env, "also the logs")
    assert isinstance(answer, HTTPException) and answer.status_code == 409
    asgi = _Asgi()
    _connection, response = _stream(web_env, {"content": "also the logs"})
    await asyncio.wait_for(response({"type": "http"}, asgi.receive, asgi.send), 5)

    assert "turn_started" in [f["type"] for f in asgi.frames]
    assert web_env.fake_rl.store[bucket] == 1


async def test_a_steer_a_running_turn_took_is_shown_once_taken(ws_env, monkeypatch):  # noqa: F811
    """Folded into the turn elsewhere: the stored message is published after the hand-off, once."""
    conversation_id = str(ws_env.conv.id)
    ws_env.fake_kv.kv[chat_turn_guard.processing_key(conversation_id)] = "turn-a"
    events: list[str] = []
    fold = chat_turn_guard.fold_into_inflight_turn

    async def _fold(*a, **kw):
        events.append("fold")
        return await fold(*a, **kw)

    monkeypatch.setattr(chat_turn_guard, "fold_into_inflight_turn", _fold)
    monkeypatch.setattr(chat_turn_stream, "publish_user_message", lambda *a, **kw: events.append("published"))

    answer = await _steer(ws_env, "also the logs", steer_id="bubble-1")

    assert answer["status"] == "steered"
    assert events == ["fold", "published"]


async def test_a_steer_the_turn_here_no_longer_reads_is_409_and_not_stored(ws_env):  # noqa: F811
    """The turn on this process closed its steering (its last leftover check, its
    teardown): folding through the inbox would leave the message unanswered."""
    conversation_id = str(ws_env.conv.id)
    ws_env.fake_kv.kv[chat_turn_guard.processing_key(conversation_id)] = "turn-a"
    conn = chat_web_transport.HeadlessTurnConnection(conversation_id, {"content": "x"})
    conn.accept_steering(lambda _payload: None)
    conn.accept_steering(None)
    chat_web_transport.register(conn)
    try:
        answer = await _steer(ws_env, "also the logs")
    finally:
        chat_web_transport.unregister(conn)

    assert isinstance(answer, HTTPException) and answer.status_code == 409
    assert ws_env.stored == []


async def test_a_steer_handed_over_that_the_bridge_never_read_is_answered_by_a_follow_up(
    ws_env, monkeypatch  # noqa: F811
):
    """Handed to the turn in-process after its bridge's last read: the leftover
    check takes it and runs a follow-up pass for it, instead of the teardown
    logging it away while the client believes it steered."""
    hold_bridge = asyncio.Event()
    bridge_held = asyncio.Event()
    consume_cancel = chat_turn_guard.consume_turn_cancel

    async def _consume_cancel(*a, **kw):
        if hold_bridge.is_set():
            bridge_held.set()
            await asyncio.Event().wait()  # the bridge never reads again
        return await consume_cancel(*a, **kw)

    monkeypatch.setattr(chat_turn_guard, "consume_turn_cancel", _consume_cancel)
    # A bridge stuck in a Redis call is cancelled once the turn's end stops waiting for it.
    monkeypatch.setattr(chat_turn_guard, "BRIDGE_STOP_SECONDS", 0.05)
    passes: list[str] = []
    answers: list = []

    async def _run(_db, _uid, _history, content, **_kwargs):
        passes.append(content)
        if len(passes) == 1:
            hold_bridge.set()
            await asyncio.wait_for(bridge_held.wait(), 2)
            answers.append(await _steer(ws_env, "also the logs"))
            return ("segment one", None)
        return ("final answer", None)

    _patch_general_chat(monkeypatch, _run)
    conn, task = _start(ws_env)
    turn_id = await _turn_running(conn)
    await asyncio.wait_for(task, 5)

    assert answers == [{"status": "steered", "steer_id": None, "turn_id": turn_id}]
    assert passes == ["build the report", "also the logs"]
    assert [f["content"] for f in conn.sent if f.get("type") == "done"] == ["segment one", "final answer"]


async def test_the_steer_route_429_says_when_to_retry(ws_env):  # noqa: F811
    ws_env.fake_kv.kv[chat_turn_guard.processing_key(str(ws_env.conv.id))] = "turn-a"
    ws_env.fake_rl.store[f"rl:chat-send:{ws_env.user_id}"] = chat_module.CHAT_MESSAGES_PER_MINUTE
    answer = await _steer(ws_env, "also the logs")
    assert isinstance(answer, HTTPException) and answer.status_code == 429
    assert int(answer.headers["Retry-After"]) > 0


# ── Refusals before the turn ─────────────────────────────────────────────
def _start_message(asgi: _Asgi) -> dict:
    return next(m for m in asgi.messages if m["type"] == "http.response.start")


def _json_body(asgi: _Asgi) -> dict:
    return json.loads(b"".join(m.get("body") or b"" for m in asgi.messages if m["type"] == "http.response.body"))


def _refuse_with(monkeypatch, env, how: str):
    import app.core.database as database_module

    async def _says(*_a, **_kw):
        return how

    if how == "pack options":

        def _unreadable(_raw):
            raise chat_module.InvalidPackOptionsError("bad")

        monkeypatch.setattr(chat_module, "pack_options_from_frame", _unreadable)
    elif how == "rate limit":
        env.fake_rl.store[f"rl:chat-send:{env.user_id}"] = chat_module.CHAT_MESSAGES_PER_MINUTE
    elif how == "unknown conversation":
        monkeypatch.setattr(database_module, "async_session", lambda: _FakeSession(None, env.timeline))
    elif how == "continuation":
        monkeypatch.setattr(chat_module, "web_send_refusal", _says)
    elif how == "selection":
        monkeypatch.setattr(chat_module, "refused_chat_selection", _says)
    elif how == "channel hold":

        async def _held(_conv_id):
            return None, how

        monkeypatch.setattr(chat_module, "hold_off_channel_turns", _held)
    elif how == "account":

        async def _rejected(_db, _uid, _issued_at, **_kw):
            return "Invalid token"

        monkeypatch.setattr(chat_module, "resolve_account_rejection", _rejected)


@pytest.mark.parametrize(
    ("how", "content", "status", "detail"),
    [
        ("pack options", "hello", 422, "The investigation's choices could not be read. Confirm the sources again."),
        ("empty", "   ", 400, "Empty message"),
        ("rate limit", "hello", 429, "Too many requests. Please slow down and try again shortly."),
        ("unknown conversation", "hello", 404, "Conversation not found"),
        ("continuation", "hello", 409, "continuation"),
        ("selection", "hello", 400, "selection"),
        ("channel hold", "hello", 409, "channel hold"),
        ("account", "hello", 401, "Invalid token"),
    ],
)
async def test_a_send_refused_before_its_turn_is_an_http_error(web_env, monkeypatch, how, content, status, detail):  # noqa: F811
    ran: list[str] = []

    async def _run(*_a, **_kw):
        ran.append("turn")
        return ("answer", None)

    _patch_general_chat(monkeypatch, _run)
    _refuse_with(monkeypatch, web_env, how)
    # A Stop pressed while this send was on its way, kept for its turn.
    kept = chat_turn_guard.pending_cancel_key(str(web_env.conv.id), "tab-1")
    await web_env.fake_kv.set(kept, "1")
    asgi = _Asgi()
    _connection, response = _stream(web_env, {"content": content}, origin="tab-1")
    await asyncio.wait_for(response({"type": "http"}, asgi.receive, asgi.send), 5)

    start = _start_message(asgi)
    headers = dict(start["headers"])
    assert start["status"] == status
    assert headers[b"content-type"] == b"application/json"
    assert _json_body(asgi) == {"detail": detail}
    assert asgi.frames == [] and ran == [] and web_env.stored == []
    assert (b"retry-after" in headers) is (status == 429)
    # No turn will take it: the tab's next send must not start stopped.
    assert kept not in web_env.fake_kv.kv


@pytest.mark.parametrize(
    ("how", "status"),
    [("rate limit", 429), ("account", 401), ("continuation", 409), ("unknown conversation", 404)],
)
async def test_a_send_refused_after_a_slow_pre_turn_phase_is_still_an_http_error(
    web_env, monkeypatch, how, status  # noqa: F811
):
    """A keep-alive owed before the send knows its outcome writes nothing: it
    would start a 200 stream (the refusal then a frame, its headers lost) and
    give back the admission limiter's slot while setup still runs."""
    from app.main import ConcurrencyLimitMiddleware

    interval = 0.02
    monkeypatch.setattr(chat_web_transport, "SSE_KEEPALIVE_SECONDS", interval)

    async def _run(*_a, **_kw):
        return ("answer", None)

    _patch_general_chat(monkeypatch, _run)
    _refuse_with(monkeypatch, web_env, how)
    limiter = ConcurrencyLimitMiddleware(None, limit=1)
    slot_held_through_setup: list[bool] = []
    rate_limit = chat_module.enforce_rate_limit

    async def _slow_rate_limit(*a, **kw):
        # Setup outlasts several keep-alive intervals (a saturated pool, a slow Redis).
        await asyncio.sleep(interval * 8)
        slot_held_through_setup.append(limiter._active == 1)
        return await rate_limit(*a, **kw)

    monkeypatch.setattr(chat_module, "enforce_rate_limit", _slow_rate_limit)
    asgi = _Asgi()
    _connection, response = _stream(web_env, {"content": "hello"})
    limiter.app = response
    scope = {"type": "http", "method": "POST", "path": "/api/v1/web/chat/messages", "headers": []}
    await asyncio.wait_for(limiter(scope, asgi.receive, asgi.send), 5)

    start = _start_message(asgi)
    assert start["status"] == status
    assert dict(start["headers"])[b"content-type"] == b"application/json"
    assert (b"retry-after" in dict(start["headers"])) is (status == 429)
    assert not any(m.get("body") == chat_web_transport.SSE_KEEPALIVE for m in asgi.messages)
    assert slot_held_through_setup == [True]
    assert limiter._active == 0


async def test_a_failure_once_the_turn_runs_stays_a_frame(web_env, monkeypatch):  # noqa: F811
    async def _run(*_a, **_kw):
        raise RuntimeError("the model went away")

    _patch_general_chat(monkeypatch, _run)
    asgi = _Asgi()
    _connection, response = _stream(web_env, {"content": "hello"})
    await asyncio.wait_for(response({"type": "http"}, asgi.receive, asgi.send), 5)

    assert _start_message(asgi)["status"] == 200
    types = [f["type"] for f in asgi.frames]
    assert types[0] == "thinking" and "turn_started" in types
    assert any(f["type"] == "error" and "the model went away" not in f["content"] for f in asgi.frames)


# ── Keep-alive ───────────────────────────────────────────────────────────
def test_a_keepalive_is_due_one_interval_after_the_last_write(monkeypatch):
    monkeypatch.setattr(chat_web_transport, "SSE_KEEPALIVE_SECONDS", 15.0)
    assert chat_web_transport.keepalive_delay(100.0, 100.0) == 15.0
    assert chat_web_transport.keepalive_delay(100.0, 110.0) == 5.0
    assert chat_web_transport.keepalive_delay(100.0, 120.0) == 0.0


async def test_the_message_stream_keeps_alive_one_interval_after_its_last_frame(web_env, monkeypatch):  # noqa: F811
    """At most one interval after the last frame, never the up-to-a-third-more of a polling ticker."""
    interval = 0.5
    monkeypatch.setattr(chat_web_transport, "SSE_KEEPALIVE_SECONDS", interval)
    loop = asyncio.get_running_loop()
    writes: list[tuple[float, bytes]] = []
    asgi = _Asgi()
    recorded_send = asgi.send

    async def _send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            writes.append((loop.time(), message["body"]))
        await recorded_send(message)

    async def _run(*_a, **_kw):
        await asyncio.sleep(interval * 2.6)
        return ("answer", None)

    _patch_general_chat(monkeypatch, _run)
    _connection, response = _stream(web_env, {"content": "hello"})
    await asyncio.wait_for(response({"type": "http"}, asgi.receive, _send), 5)

    keepalives = [i for i, (_t, body) in enumerate(writes) if body == chat_web_transport.SSE_KEEPALIVE]
    assert len(keepalives) >= 2
    first = keepalives[0]
    gaps = [writes[i][0] - writes[i - 1][0] for i in keepalives]
    assert writes[first - 1][1] != chat_web_transport.SSE_KEEPALIVE
    assert all(interval * 0.95 <= gap <= interval * 1.15 for gap in gaps), gaps


async def test_the_events_stream_keeps_alive_on_the_same_rule(web_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(chat_web_transport, "SSE_KEEPALIVE_SECONDS", 0.05)

    async def _quiet_relay(_conv_id, **_kw):
        await asyncio.Event().wait()

    async def _release(_db):
        return None

    monkeypatch.setattr(chat_turn_stream, "relay_turn_frames", _quiet_relay)
    monkeypatch.setattr(chat_web_stream, "release_db_connection_before_loop", _release)
    response = await chat_web_stream.stream_web_chat_events(
        str(web_env.conv.id),
        SimpleNamespace(headers={}),
        client_id="tab-1",
        db=SimpleNamespace(execute=_FakeSession(web_env.conv, []).execute),
        user=SimpleNamespace(id=web_env.user_id, web_session=SimpleNamespace()),
    )
    iterator = response.body_iterator
    chunks = [await asyncio.wait_for(anext(iterator), 1.0) for _ in range(2)]
    await iterator.aclose()
    assert chunks == [chat_web_transport.SSE_KEEPALIVE] * 2
