"""The worker signs its gateway calls (re-embedding after a restart).

Only the platform's bootstrap set the LLM service identity, and a document job
reaches the worker with no bound user: every Filigran AI Gateway call the
worker made failed in the auth hook (``resolve_llm_caller`` raised) before it
was sent, reported by the OpenAI SDK as "Connection error.".
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.llm import identity
from app.llm.gateway_provider import StaticTokenAuth, XtmJwtAuth, _gateway_http_client


@pytest.fixture(autouse=True)
def _embedding_config_not_moved(monkeypatch):
    """The platform embedding config is the one each document job embedded with."""
    monkeypatch.setattr("app.services.document_processor.embedding_config_moved", AsyncMock(return_value=False))


ADMIN_ID = uuid.uuid4()
GATEWAY_URL = "http://gateway.local:8080"


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


def _session(answers: list):
    """An ``async_session`` whose queries answer *answers* in turn (an exception is raised)."""
    calls = {"n": 0}

    @asynccontextmanager
    async def session():
        db = MagicMock()

        async def execute(_query):
            calls["n"] += 1
            answer = answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return _Result(answer)

        db.execute = execute
        yield db

    return session, calls


def _user(user_id, email: str, *, is_active: bool = True):
    return SimpleNamespace(id=user_id, email=email, is_active=is_active)


def _db(users: list):
    """A job's own session whose user loads answer *users* in turn."""
    db = MagicMock()
    db.calls = 0

    async def get(_model, _key):
        db.calls += 1
        return users.pop(0)

    db.get = get
    return db


@pytest.fixture(autouse=True)
def _no_identity(monkeypatch):
    monkeypatch.setattr(identity, "_service_identity", None)
    token = identity._llm_caller.set(None)
    yield
    identity._llm_caller.reset(token)


async def _send_through_gateway(auth: httpx.Auth) -> httpx.Request:
    """What the gateway receives once *auth* signed a call (the OpenAI SDK's async client)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    async with _gateway_http_client(auth, None, transport=httpx.MockTransport(handler)) as client:
        await client.post(f"{GATEWAY_URL}/v1/embeddings", json={"input": "x"})
    return seen[0]


# ── The service identity ─────────────────────────────────────────────────────


async def test_a_worker_without_the_bootstrap_loads_the_service_identity(monkeypatch):
    session, _calls = _session([ADMIN_ID])
    monkeypatch.setattr("app.core.database.async_session", session)
    with pytest.raises(RuntimeError):
        identity.resolve_llm_caller()  # what every worker gateway call hit

    assert await identity.load_service_identity() is True
    assert identity.resolve_llm_caller().user_id == ADMIN_ID


async def test_no_admin_yet_loads_nothing(monkeypatch):
    session, _calls = _session([None])
    monkeypatch.setattr("app.core.database.async_session", session)
    assert await identity.load_service_identity() is False
    with pytest.raises(RuntimeError):
        identity.resolve_llm_caller()


async def test_a_failed_lookup_keeps_the_identity_it_had(monkeypatch):
    session, _calls = _session([ADMIN_ID, OSError("db down")])
    monkeypatch.setattr("app.core.database.async_session", session)
    await identity.load_service_identity()
    assert await identity.load_service_identity() is False
    assert identity.resolve_llm_caller().user_id == ADMIN_ID


async def test_a_lookup_picks_up_a_re_created_admin(monkeypatch):
    """The platform re-created the admin (a new id): the worker's next lookup signs as it."""
    new_admin = uuid.uuid4()
    session, _calls = _session([ADMIN_ID, new_admin])
    monkeypatch.setattr("app.core.database.async_session", session)
    await identity.load_service_identity()
    await identity.load_service_identity()
    assert identity.resolve_llm_caller().user_id == new_admin


async def test_a_lookup_that_fails_without_a_message_names_its_error(monkeypatch, caplog):
    session, _calls = _session([TimeoutError()])
    monkeypatch.setattr("app.core.database.async_session", session)
    await identity.load_service_identity()
    assert "TimeoutError" in caplog.text


async def test_the_worker_retries_soon_without_an_identity_and_refreshes_rarely_with_one(monkeypatch):
    """A worker started before the admin existed signs within seconds of its creation."""
    session, calls = _session([None, ADMIN_ID])
    monkeypatch.setattr("app.core.database.async_session", session)
    waits: list[float] = []

    async def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(identity.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await identity.keep_service_identity_current()

    assert waits == [
        identity.SERVICE_IDENTITY_RETRY_SECONDS,
        identity.SERVICE_IDENTITY_RETRY_SECONDS,
        identity.SERVICE_IDENTITY_REFRESH_SECONDS,
    ]
    assert (identity.resolve_llm_caller().user_id, calls["n"]) == (ADMIN_ID, 2)


# ── The gateway's auth hook ──────────────────────────────────────────────────


async def test_a_gateway_call_with_nobody_bound_signs_as_the_service_identity_without_a_lookup(monkeypatch):
    """The hook only reads what the process loaded: no gateway request waits on the database."""
    import jwt as pyjwt

    session, calls = _session([])
    monkeypatch.setattr("app.core.database.async_session", session)
    identity.set_service_identity(ADMIN_ID, "admin@example.com")

    signed = await _send_through_gateway(XtmJwtAuth(GATEWAY_URL))

    claims = pyjwt.decode(signed.headers["Authorization"].removeprefix("Bearer "), options={"verify_signature": False})
    assert claims["sub"] == str(ADMIN_ID)
    assert signed.headers["X-User-Id"] == str(ADMIN_ID)
    assert calls["n"] == 0


async def test_a_gateway_call_signs_as_the_bound_person():
    person = uuid.uuid4()
    identity.set_service_identity(ADMIN_ID, "admin@example.com")
    identity.set_llm_caller(person, "person@example.com")

    signed = await _send_through_gateway(XtmJwtAuth(GATEWAY_URL))

    assert signed.headers["X-User-Id"] == str(person)


async def test_a_static_token_call_is_attributed_to_the_service_identity():
    identity.set_service_identity(ADMIN_ID, "admin@example.com")

    signed = await _send_through_gateway(StaticTokenAuth("static-token"))

    assert signed.headers["Authorization"] == "Bearer static-token"
    assert signed.headers["X-User-Id"] == str(ADMIN_ID)


async def test_a_gateway_call_with_no_identity_at_all_is_still_refused():
    with pytest.raises(RuntimeError, match="No LLM caller identity"):
        await _send_through_gateway(XtmJwtAuth(GATEWAY_URL))


# ── Binding a job's person ───────────────────────────────────────────────────


async def test_a_job_binds_the_person_it_acts_for():
    owner = uuid.uuid4()
    db = _db([_user(owner, "owner@example.com")])
    assert await identity.bind_llm_caller(db, str(owner)) is True
    caller = identity.resolve_llm_caller()
    assert (caller.user_id, caller.email) == (owner, "owner@example.com")


@pytest.mark.parametrize("user_id", [None, "", "system"])
async def test_a_job_with_no_person_binds_nobody_and_asks_nothing(user_id):
    db = _db([])
    assert await identity.bind_llm_caller(db, user_id) is False
    assert identity._llm_caller.get() is None
    assert db.calls == 0


async def test_a_job_whose_person_is_gone_binds_nobody():
    db = _db([None])
    assert await identity.bind_llm_caller(db, uuid.uuid4()) is False
    assert identity._llm_caller.get() is None


async def test_a_job_whose_person_is_deactivated_binds_nobody():
    """Their content signs as the service identity: a deactivated person's quota is not charged."""
    person = uuid.uuid4()
    db = _db([_user(person, "gone@example.com", is_active=False)])
    assert await identity.bind_llm_caller(db, person) is False
    assert identity._llm_caller.get() is None


def test_a_scope_unbinds_whoever_it_bound():
    before = uuid.uuid4()
    identity.set_llm_caller(before, "before@example.com")
    with identity.llm_caller_scope():
        identity.set_llm_caller(uuid.uuid4(), "inside@example.com")
    assert identity.resolve_llm_caller().user_id == before


# ── Document jobs ────────────────────────────────────────────────────────────


async def _embed_document(
    monkeypatch, *, company_managed: bool = False, on_behalf_of=None, inactive=(), through_job=False
):
    """Run a document job; what the provider's ``embed`` saw as its caller, and the users it loaded.

    *through_job* runs ``process_document`` (the job's call), not one attempt of it.
    """
    from app.services import document_processor
    from tests.test_document_processor import _build_doc_and_kb, _FakeSession

    doc, kb = _build_doc_and_kb()
    kb.is_company_managed = company_managed
    session = _FakeSession(doc=doc, kb=kb)
    loaded: list = []

    async def get(_model, key):
        loaded.append(key)
        return _user(key, f"{key}@example.com", is_active=str(key) not in inactive)

    session.get = get
    seen: list = []

    async def embed(_chunk, model=None):
        seen.append(identity._llm_caller.get())
        return []

    provider = SimpleNamespace(embed=embed, provider_name="test")
    monkeypatch.setattr(document_processor, "async_session", lambda: session)
    monkeypatch.setattr(
        document_processor, "chunk_text", lambda *_a, **_k: ["This chunk has enough content to be embedded."]
    )
    monkeypatch.setattr(document_processor, "log_kb_activity", AsyncMock())
    monkeypatch.setattr(
        "app.llm.router.get_embedding_provider", AsyncMock(return_value=(provider, "embed-model", str(uuid.uuid4())))
    )
    if through_job:
        await document_processor.process_document(str(doc.id), on_behalf_of=on_behalf_of)
    else:
        await document_processor._process_document_impl(str(doc.id), on_behalf_of=on_behalf_of)
    assert seen, "the provider was never called"
    return kb, seen[0], loaded


async def test_a_document_job_signs_as_the_kb_owner(monkeypatch):
    kb, caller, _loaded = await _embed_document(monkeypatch)
    assert (caller.user_id, caller.email) == (kb.user_id, f"{kb.user_id}@example.com")


async def test_a_document_job_signs_as_the_person_who_added_the_document(monkeypatch):
    person = uuid.uuid4()
    _kb, caller, _loaded = await _embed_document(monkeypatch, on_behalf_of=str(person))
    assert caller.user_id == person


async def test_a_deactivated_uploader_signs_as_the_kb_owner(monkeypatch):
    """Not as the service identity: the KB's still-active owner keeps the attribution."""
    person = str(uuid.uuid4())
    kb, caller, _loaded = await _embed_document(monkeypatch, on_behalf_of=person, inactive={person})
    assert caller.user_id == kb.user_id


async def test_a_company_managed_kb_binds_nobody(monkeypatch):
    """Nobody to act for: the gateway's hook signs as the service identity."""
    _kb, caller, loaded = await _embed_document(monkeypatch, company_managed=True)
    assert caller is None
    assert loaded == []


async def test_the_document_job_leaves_nobody_bound_for_its_next_step(monkeypatch):
    """``process_document_job`` runs ``run_auto_pass`` next, in the same task: it must
    not sign as the person the embed was made for."""
    kb, caller, _loaded = await _embed_document(monkeypatch, through_job=True)
    assert caller.user_id == kb.user_id
    assert identity._llm_caller.get() is None
