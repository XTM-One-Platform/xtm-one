"""One gate for every write to a knowledge base's content.

``helpers.knowledge_base_write_refusal`` is shared by the REST document routes,
the agent-builder document tools, ``store_knowledge`` /
``update_knowledge_document`` and the chat upload. The chat upload used to copy
every file into the agent's FIRST knowledge base with no check at all: no write
grant, no read-only lock, no package lock, no built-in lock and no
company-managed rule. These tests run as a NON-admin wherever rights matter -
``is_admin`` short-circuits the manage rule, so an admin-only test proves
nothing about it.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import chat as chat_api
from app.api.v1 import helpers as api_helpers
from app.core.database import get_db
from app.models.agent import Agent
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User

OWNER_ID = uuid.uuid4()
OTHER_ID = uuid.uuid4()


def _user(user_id=OWNER_ID, *, admin=False) -> User:
    return User(id=user_id, email="u@example.com", role="admin" if admin else "user")


def _kb(
    name="Notes",
    *,
    owner=OWNER_ID,
    company_managed=False,
    read_only=False,
    builtin=False,
    shared_user_ids=None,
    shared_user_permissions=None,
) -> KnowledgeBase:
    return KnowledgeBase(
        id=uuid.uuid4(),
        name=name,
        user_id=None if company_managed else owner,
        is_company_managed=company_managed,
        is_read_only=read_only,
        is_builtin=builtin,
        shared_group_ids=None,
        shared_group_permissions=None,
        shared_user_ids=shared_user_ids,
        shared_user_permissions=shared_user_permissions,
        access_restricted=False,
        document_count=0,
    )


def _agent(*kbs, write=()) -> Agent:
    return Agent(
        id=uuid.uuid4(),
        name="Helper",
        knowledge_base_ids=[str(kb.id) for kb in kbs],
        knowledge_base_permissions={str(kb.id): "write" for kb in write} or None,
    )


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))


class _Db:
    """Answers the resolver's reads by table: KBs, group memberships, origins."""

    def __init__(self, kbs=(), *, package_ids=()):
        self.kbs = list(kbs)
        self.package_ids = [uuid.UUID(str(i)) for i in package_ids]

    async def execute(self, statement):
        sql = str(statement)
        if "agent_skill_package_origin" in sql:
            return _Result(self.package_ids)
        if "group_members" in sql:
            return _Result([])
        if "knowledge_bases" in sql:
            return _Result(self.kbs)
        raise AssertionError(f"unexpected statement: {sql}")


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_gate_lets_an_owner_write_an_unlocked_knowledge_base():
    kb = _kb()
    assert await api_helpers.knowledge_base_write_refusal(_Db(), kb, _user()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kb_kwargs", "package", "reason", "wording"),
    [
        ({"read_only": True}, False, "read_only", "is read-only"),
        ({}, True, "package_installed", "imported from skill packages"),
        ({"builtin": True}, False, "builtin", "Built-in knowledge bases"),
        ({"company_managed": True}, False, "company_managed", "Only admins"),
        ({"owner": OTHER_ID}, False, "not_permitted", "don't have permission"),
    ],
)
async def test_the_gate_refuses_every_lock_for_a_non_admin(
    kb_kwargs, package, reason, wording
):
    kb = _kb(**kb_kwargs)
    db = _Db(package_ids=[kb.id] if package else [])

    refusal = await api_helpers.knowledge_base_write_refusal(db, kb, _user())

    assert refusal is not None
    assert refusal.reason == reason
    assert wording in refusal.detail


@pytest.mark.asyncio
async def test_a_read_only_share_cannot_write_but_a_config_share_can():
    read_only = _kb(
        owner=OTHER_ID,
        shared_user_ids=[str(OWNER_ID)],
        shared_user_permissions={str(OWNER_ID): "read_only"},
    )
    config = _kb(
        owner=OTHER_ID,
        shared_user_ids=[str(OWNER_ID)],
        shared_user_permissions={str(OWNER_ID): "config"},
    )

    refused = await api_helpers.knowledge_base_write_refusal(_Db(), read_only, _user())
    assert refused is not None and refused.reason == "not_permitted"
    assert await api_helpers.knowledge_base_write_refusal(_Db(), config, _user()) is None


@pytest.mark.asyncio
async def test_the_read_only_lock_is_reported_only_to_someone_who_could_lift_it():
    """A non-admin on a company-managed read-only KB is told it is admin-only,
    not advised to turn off a lock they cannot reach."""
    kb = _kb(company_managed=True, read_only=True)

    refusal = await api_helpers.knowledge_base_write_refusal(_Db(), kb, _user())

    assert refusal is not None and refusal.reason == "company_managed"


@pytest.mark.asyncio
async def test_an_agent_write_skips_the_rights_but_not_the_locks():
    """``user=None`` is an agent writing under its own grant: a company-managed
    KB is fine, a read-only one is not."""
    assert (
        await api_helpers.knowledge_base_write_refusal(_Db(), _kb(company_managed=True))
        is None
    )
    refusal = await api_helpers.knowledge_base_write_refusal(_Db(), _kb(read_only=True))
    assert refusal is not None and refusal.reason == "read_only"


# ---------------------------------------------------------------------------
# The chat upload target
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_uses_the_first_knowledge_base_the_uploader_may_write():
    locked = _kb("Locked", read_only=True)
    writable = _kb("Writable")
    agent = _agent(locked, writable, write=(locked, writable))

    kb, refusal = await chat_api._resolve_upload_knowledge_base(
        _Db([locked, writable]), _user(), agent
    )

    assert kb is writable
    assert refusal is None


@pytest.mark.asyncio
async def test_upload_never_writes_a_knowledge_base_without_a_write_grant():
    """The old behaviour: the first bound KB, grant or not."""
    kb = _kb()
    agent = _agent(kb)

    target, refusal = await chat_api._resolve_upload_knowledge_base(
        _Db([kb]), _user(), agent
    )

    assert target is None
    assert refusal.reason == chat_api.KB_UPLOAD_NO_WRITE_GRANT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kb_kwargs", "package", "reason"),
    [
        ({"read_only": True}, False, "read_only"),
        ({}, True, "package_installed"),
        ({"builtin": True}, False, "builtin"),
        ({"company_managed": True}, False, "company_managed"),
    ],
)
async def test_upload_skips_a_granted_but_locked_knowledge_base(kb_kwargs, package, reason):
    kb = _kb(**kb_kwargs)
    agent = _agent(kb, write=(kb,))
    db = _Db([kb], package_ids=[kb.id] if package else [])

    target, refusal = await chat_api._resolve_upload_knowledge_base(db, _user(), agent)

    assert target is None
    assert refusal.reason == reason


@pytest.mark.asyncio
async def test_an_admin_upload_reaches_a_company_managed_knowledge_base():
    kb = _kb(company_managed=True)
    agent = _agent(kb, write=(kb,))

    target, refusal = await chat_api._resolve_upload_knowledge_base(
        _Db([kb]), _user(admin=True), agent
    )

    assert target is kb and refusal is None


@pytest.mark.asyncio
async def test_an_agent_without_knowledge_bases_reports_nothing_skipped():
    target, refusal = await chat_api._resolve_upload_knowledge_base(
        _Db(), _user(), _agent()
    )
    assert target is None and refusal is None


# ---------------------------------------------------------------------------
# The upload route: the file is attached whatever happens to the KB copy
# ---------------------------------------------------------------------------


class _RouteDb:
    def __init__(self, conv, agent):
        self.conv = conv
        self.agent = agent
        self.added: list = []
        self.commits = 0

    async def execute(self, statement):
        sql = str(statement)
        if "FROM conversations" in sql:
            return SimpleNamespace(scalar_one_or_none=lambda: self.conv)
        if "FROM agents" in sql:
            return SimpleNamespace(scalar_one_or_none=lambda: self.agent)
        raise AssertionError(f"unexpected statement: {sql}")

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", "missing") is None:
                obj.id = uuid.uuid4()

    async def commit(self):
        self.commits += 1


def _upload(monkeypatch, resolved):
    conv = SimpleNamespace(id=uuid.uuid4(), agent_id=uuid.uuid4(), user_id=OWNER_ID)
    db = _RouteDb(conv, SimpleNamespace(id=conv.agent_id))
    stored: list = []

    async def _store_message(_db, _conv_id, **kwargs):
        stored.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(chat_api, "store_message", _store_message)
    monkeypatch.setattr(
        chat_api, "_resolve_upload_knowledge_base", AsyncMock(return_value=resolved)
    )
    monkeypatch.setattr(
        "app.services.file_storage.file_storage.upload",
        AsyncMock(return_value="chat/key"),
    )
    monkeypatch.setattr(
        "app.services.chunking.extract_text_from_file_async",
        AsyncMock(return_value="A file body long enough to be kept."),
    )
    monkeypatch.setattr("app.api.v1.helpers.log_kb_activity", AsyncMock())
    # The revision itself is tested on PostgreSQL (test_kb_revision_postgres.py).
    monkeypatch.setattr("app.services.kb_helpers.bump_kb_revision", AsyncMock())
    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_api, "enqueue_document_processing", enqueue)

    app = FastAPI()
    app.include_router(chat_api.router, prefix="/api/v1/chat")
    app.dependency_overrides[deps.get_current_user] = lambda: _user()
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None

    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/chat/conversations/{conv.id}/upload",
            files={"file": ("notes.txt", b"A file body long enough to be kept.", "text/plain")},
        )
    return resp, db, stored, enqueue


def test_a_refused_copy_still_attaches_the_file_and_says_why(monkeypatch):
    refusal = api_helpers.KnowledgeBaseWriteRefusal(
        "read_only", "'Notes' is read-only: its content is locked."
    )
    resp, db, stored, enqueue = _upload(monkeypatch, (None, refusal))

    assert resp.status_code == 200
    body = resp.json()
    assert body["vectorized"] is False
    assert body["knowledge_base_skipped_reason"] == "read_only"
    assert "not added to the agent's knowledge base" in body["message"]
    # The file is stored, attached to the message, and its text is in it.
    assert len(stored) == 1
    attachments = json.loads(stored[0]["metadata_json"])["attachments"]
    assert attachments[0]["file_id"] == body["file_id"]
    assert "A file body long enough" in stored[0]["content"]
    assert not any(type(o).__name__ == "KnowledgeDocument" for o in db.added)
    enqueue.assert_not_awaited()


def test_an_allowed_copy_is_queued_into_the_resolved_knowledge_base(monkeypatch):
    kb = _kb()
    resp, db, stored, enqueue = _upload(monkeypatch, (kb, None))

    assert resp.status_code == 200
    body = resp.json()
    assert body["vectorized"] is True
    assert body["knowledge_base_id"] == str(kb.id)
    assert body["knowledge_base_skipped_reason"] is None
    docs = [o for o in db.added if type(o).__name__ == "KnowledgeDocument"]
    assert len(docs) == 1 and docs[0].knowledge_base_id == kb.id
    assert kb.document_count == 1
    assert len(stored) == 1
    enqueue.assert_awaited_once()
