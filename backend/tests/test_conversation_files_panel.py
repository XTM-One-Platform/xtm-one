"""Tests for the Conversation files panel endpoints.

Covers the two halves of the feature:

* ``list_conversation_files`` — the listing itself, its origin bucketing and
  the download-URL shape it hands the panel.
* The **break-glass** contract — owners in freely, non-owner admins only after
  an audited confidentiality acknowledgement, everybody else indistinguishably
  404. This is the part worth pinning: a regression here does not break a page,
  it silently exposes one user's private files to another account.

No database or Redis is required — the pop-style fake session below mirrors the
handlers' query order, and the Redis seam is patched per test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import chat as chat_api
from app.api.v1.chat import (
    CONFIDENTIALITY_ACK_REQUIRED_DETAIL,
    CONVERSATION_FILE_ACCESS_GRANT_TTL_SECONDS,
    _conversation_file_origin,
    _origin_sql_predicate,
    acknowledge_conversation_file_access,
    conversation_file_access_grant_key,
    list_conversation_files,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Result:
    """Result stub supporting the accessors the handlers use."""

    def __init__(self, *, scalar=None, count=None, rows=None):
        self._scalar = scalar
        self._count = count
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self._count if self._count is not None else self._scalar

    def scalars(self):
        return SimpleNamespace(all=lambda: self._rows)


class _FakeDB:
    """Pop-style fake session: each ``execute`` returns the next queued result."""

    def __init__(self, results):
        self._queue = list(results)
        self.added: list[object] = []
        self.commits = 0

    async def execute(self, _statement, *_args, **_kwargs):
        if not self._queue:
            raise AssertionError("unexpected extra DB query")
        return self._queue.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1


class _FakeRedis:
    def __init__(self, *, existing: set[str] | None = None, fail: bool = False):
        self.store: dict[str, tuple[int, str]] = {}
        self._existing = existing or set()
        self._fail = fail

    async def exists(self, key):
        if self._fail:
            raise RuntimeError("redis down")
        return 1 if (key in self._existing or key in self.store) else 0

    async def setex(self, key, ttl, value):
        if self._fail:
            raise RuntimeError("redis down")
        self.store[key] = (ttl, value)


def _user(role="user", uid=None):
    return SimpleNamespace(
        id=uid or uuid.uuid4(), role=role, email=f"{role}@example.test"
    )


def _conversation(owner_id, title="Investigation", link_access="private"):
    return SimpleNamespace(
        id=uuid.uuid4(), user_id=owner_id, title=title, link_access=link_access
    )


def _no_share():
    """The conversation-share probe finding nothing.

    ``_resolve_conversation_for_file_listing`` now asks "is this conversation
    shared with the requester?" before falling through to the admin
    break-glass branch, so every non-owner path queues one of these. Sharing
    itself is covered in ``test_conversation_sharing.py``; here it only has to
    be absent so the break-glass contract is exercised.
    """
    return _Result(scalar=None)


def _file_row(
    *,
    source="chat",
    filename="report.csv",
    content_type="text/csv",
    size=2048,
    message_id=None,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        filename=filename,
        content_type=content_type,
        size=size,
        created_at=datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
        source=source,
        message_id=message_id,
    )


def _patch_redis(monkeypatch, redis):
    async def _fake_client():
        return redis

    monkeypatch.setattr(chat_api, "redis_client", _fake_client)


# ---------------------------------------------------------------------------
# Origin bucketing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("chat", "uploaded"),
        ("channel:slack", "uploaded"),
        ("channel:teams", "uploaded"),
        ("agent_generated", "generated"),
        ("agent", "generated"),
        ("mcp", "generated"),
        ("assignment", "other"),
        ("knowledge_base", "other"),
        ("", "other"),
        (None, "other"),
    ],
)
def test_origin_bucketing(source, expected):
    assert _conversation_file_origin(source) == expected


def test_origin_bucketing_is_case_insensitive():
    """A writer that capitalises ``source`` must not fall out of its bucket."""
    assert _conversation_file_origin("CHAT") == "uploaded"
    assert _conversation_file_origin("Agent_Generated") == "generated"


def test_origin_sql_predicate_lowercases_source():
    """The SQL filter must normalise case the same way the Python labeller does.

    If it did not, a row labelled ``uploaded`` in the response could be excluded
    by ``?origin=uploaded`` — the buckets would stop partitioning the set.
    """
    from sqlalchemy.dialects import postgresql

    for origin in ("uploaded", "generated", "other"):
        sql = str(
            _origin_sql_predicate(origin).compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert "lower(file_attachments.source)" in sql

    assert _origin_sql_predicate(None) is None


def test_resume_spill_files_are_never_listed():
    """A tool result an interrupted turn kept for its resume is internal.

    The conversation panel and the file list leave it out (it is no output of
    the conversation and may carry a mailbox read); the predicate names the
    very source the checkpoint writes.
    """
    from sqlalchemy.dialects import postgresql

    from app.services.turn_checkpoint import SPILL_SOURCE

    assert SPILL_SOURCE in chat_api._INTERNAL_FILE_SOURCES
    sql = str(
        chat_api._listed_file_predicate().compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "lower(file_attachments.source) NOT IN ('agent_checkpoint')" in sql
    assert "file_attachments.source IS NULL" in sql


# ---------------------------------------------------------------------------
# Listing — owner path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_lists_own_files_without_any_gate():
    owner = _user()
    conv = _conversation(owner.id)
    msg_id = uuid.uuid4()
    rows = [
        _file_row(source="agent_generated", filename="summary.md", message_id=msg_id),
        _file_row(source="chat", filename="input.csv"),
    ]
    db = _FakeDB([_Result(scalar=conv), _Result(count=2), _Result(rows=rows)])

    resp = await list_conversation_files(
        conversation_id=str(conv.id),
        limit=100,
        offset=0,
        origin=None,
        user=owner,
        db=db,
    )

    assert resp.total == 2
    assert resp.via_break_glass is False
    assert [f.filename for f in resp.files] == ["summary.md", "input.csv"]
    assert [f.origin for f in resp.files] == ["generated", "uploaded"]


@pytest.mark.asyncio
async def test_download_url_prefers_message_scoped_form_when_linked():
    """The narrower message-scoped URL when ``message_id`` exists, else the
    id-scoped fallback — a row whose linkage was dropped must still be
    downloadable rather than pointing at a URL that 404s."""
    owner = _user()
    conv = _conversation(owner.id)
    msg_id = uuid.uuid4()
    linked = _file_row(filename="linked.txt", message_id=msg_id)
    orphan = _file_row(filename="orphan.txt", message_id=None)
    db = _FakeDB(
        [_Result(scalar=conv), _Result(count=2), _Result(rows=[linked, orphan])]
    )

    resp = await list_conversation_files(
        conversation_id=str(conv.id),
        limit=100,
        offset=0,
        origin=None,
        user=owner,
        db=db,
    )

    by_name = {f.filename: f for f in resp.files}
    assert by_name["linked.txt"].message_id == str(msg_id)
    assert by_name["linked.txt"].download_url == (
        f"/api/v1/chat/conversations/{conv.id}/messages/{msg_id}"
        f"/files/{linked.id}/download"
    )
    assert by_name["orphan.txt"].message_id is None
    assert by_name["orphan.txt"].download_url == (
        f"/api/v1/chat/files/{orphan.id}/download"
    )


@pytest.mark.asyncio
async def test_missing_conversation_is_404():
    db = _FakeDB([_Result(scalar=None)])
    with pytest.raises(HTTPException) as exc:
        await list_conversation_files(
            conversation_id=str(uuid.uuid4()),
            limit=100,
            offset=0,
            origin=None,
            user=_user(),
            db=db,
        )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Listing — non-owner paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_owner_non_admin_gets_404_not_403():
    """Indistinguishable from "no such conversation".

    A 403 here would confirm the id exists, letting any authenticated user
    enumerate other people's conversations.
    """
    conv = _conversation(uuid.uuid4())
    db = _FakeDB([_Result(scalar=conv), _no_share()])

    with pytest.raises(HTTPException) as exc:
        await list_conversation_files(
            conversation_id=str(conv.id),
            limit=100,
            offset=0,
            origin=None,
            user=_user(role="user"),
            db=db,
        )
    assert exc.value.status_code == 404
    assert exc.value.detail == "Conversation not found"


@pytest.mark.asyncio
async def test_admin_without_acknowledgement_is_refused(monkeypatch):
    """Being an admin is not sufficient — the glass has to be broken first."""
    conv = _conversation(uuid.uuid4())
    admin = _user(role="admin")
    _patch_redis(monkeypatch, _FakeRedis())
    db = _FakeDB([_Result(scalar=conv), _no_share()])

    with pytest.raises(HTTPException) as exc:
        await list_conversation_files(
            conversation_id=str(conv.id),
            limit=100,
            offset=0,
            origin=None,
            user=admin,
            db=db,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == CONFIDENTIALITY_ACK_REQUIRED_DETAIL


@pytest.mark.asyncio
async def test_admin_with_live_grant_sees_files_and_break_glass_flag(monkeypatch):
    conv = _conversation(uuid.uuid4())
    admin = _user(role="admin")
    _patch_redis(
        monkeypatch,
        _FakeRedis(
            existing={conversation_file_access_grant_key(admin.id, conv.id)}
        ),
    )
    rows = [_file_row(filename="private.pdf", content_type="application/pdf")]
    db = _FakeDB(
        [_Result(scalar=conv), _no_share(), _Result(count=1), _Result(rows=rows)]
    )

    resp = await list_conversation_files(
        conversation_id=str(conv.id),
        limit=100,
        offset=0,
        origin=None,
        user=admin,
        db=db,
    )

    assert resp.via_break_glass is True
    assert [f.filename for f in resp.files] == ["private.pdf"]


@pytest.mark.asyncio
async def test_break_glass_admin_gets_id_scoped_download_urls(monkeypatch):
    """A break-glass admin must not be handed message-scoped download URLs.

    ``download_message_file`` matches ``Conversation.user_id == user.id`` and so
    403s for every non-owner regardless of role — a message-scoped URL would
    list files the admin provably cannot download. The id-scoped endpoint routes
    through ``_authorize_file_access``, which admits admins.
    """
    conv = _conversation(uuid.uuid4())
    admin = _user(role="admin")
    _patch_redis(
        monkeypatch,
        _FakeRedis(
            existing={conversation_file_access_grant_key(admin.id, conv.id)}
        ),
    )
    linked = _file_row(filename="linked.txt", message_id=uuid.uuid4())
    db = _FakeDB(
        [_Result(scalar=conv), _no_share(), _Result(count=1), _Result(rows=[linked])]
    )

    resp = await list_conversation_files(
        conversation_id=str(conv.id),
        limit=100,
        offset=0,
        origin=None,
        user=admin,
        db=db,
    )

    only = resp.files[0]
    # The linkage is still reported (the panel's jump affordance needs it)...
    assert only.message_id == str(linked.message_id)
    # ...but the download must go through the id-scoped endpoint.
    assert only.download_url == f"/api/v1/chat/files/{linked.id}/download"
    assert "/messages/" not in only.download_url


@pytest.mark.asyncio
async def test_grant_is_scoped_to_one_conversation(monkeypatch):
    """A grant on conversation A must not unlock conversation B."""
    admin = _user(role="admin")
    conv_a = _conversation(uuid.uuid4())
    conv_b = _conversation(uuid.uuid4())
    _patch_redis(
        monkeypatch,
        _FakeRedis(
            existing={conversation_file_access_grant_key(admin.id, conv_a.id)}
        ),
    )

    db = _FakeDB([_Result(scalar=conv_b), _no_share()])
    with pytest.raises(HTTPException) as exc:
        await list_conversation_files(
            conversation_id=str(conv_b.id),
            limit=100,
            offset=0,
            origin=None,
            user=admin,
            db=db,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == CONFIDENTIALITY_ACK_REQUIRED_DETAIL


@pytest.mark.asyncio
async def test_grant_is_scoped_to_one_admin(monkeypatch):
    """One admin's acknowledgement must not authorize a different admin."""
    other_admin_id = uuid.uuid4()
    admin = _user(role="admin")
    conv = _conversation(uuid.uuid4())
    _patch_redis(
        monkeypatch,
        _FakeRedis(
            existing={
                conversation_file_access_grant_key(other_admin_id, conv.id)
            }
        ),
    )

    db = _FakeDB([_Result(scalar=conv), _no_share()])
    with pytest.raises(HTTPException) as exc:
        await list_conversation_files(
            conversation_id=str(conv.id),
            limit=100,
            offset=0,
            origin=None,
            user=admin,
            db=db,
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_redis_outage_denies_admin_access(monkeypatch):
    """Fail closed: an unreachable Redis cannot prove an acknowledgement."""
    conv = _conversation(uuid.uuid4())
    admin = _user(role="admin")
    _patch_redis(monkeypatch, _FakeRedis(fail=True))
    db = _FakeDB([_Result(scalar=conv), _no_share()])

    with pytest.raises(HTTPException) as exc:
        await list_conversation_files(
            conversation_id=str(conv.id),
            limit=100,
            offset=0,
            origin=None,
            user=admin,
            db=db,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == CONFIDENTIALITY_ACK_REQUIRED_DETAIL


# ---------------------------------------------------------------------------
# Acknowledgement endpoint
# ---------------------------------------------------------------------------


def _request():
    return SimpleNamespace(headers={}, client=SimpleNamespace(host="10.0.0.9"))


@pytest.mark.asyncio
async def test_acknowledgement_writes_audit_entry_and_grant(monkeypatch):
    admin = _user(role="admin")
    owner_id = uuid.uuid4()
    conv = _conversation(owner_id, title="Incident 42")
    redis = _FakeRedis()
    _patch_redis(monkeypatch, redis)

    audited: list[dict] = []

    async def _fake_log_audit(_db, **kwargs):
        audited.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    import app.services.audit as audit_mod

    monkeypatch.setattr(audit_mod, "log_audit", _fake_log_audit)

    db = _FakeDB([_Result(scalar=conv), _Result(scalar="owner@example.test")])

    resp = await acknowledge_conversation_file_access(
        conversation_id=str(conv.id),
        request=_request(),
        user=admin,
        db=db,
    )

    assert resp.status == "granted"
    assert resp.expires_in_seconds == CONVERSATION_FILE_ACCESS_GRANT_TTL_SECONDS

    # The audit entry is the durable record — it must name the actor, the
    # conversation AND its owner, or it is useless for after-the-fact review.
    assert len(audited) == 1
    entry = audited[0]
    assert entry["action"] == "break_glass"
    assert entry["entity_type"] == "conversation_files"
    assert entry["entity_id"] == conv.id
    assert entry["details"]["conversation_owner_email"] == "owner@example.test"
    assert entry["details"]["conversation_owner_id"] == str(owner_id)
    assert admin.email in entry["summary"]

    # ...and it is committed, not left pending on a session nobody flushes.
    assert db.commits == 1

    key = conversation_file_access_grant_key(admin.id, conv.id)
    assert key in redis.store
    assert redis.store[key][0] == CONVERSATION_FILE_ACCESS_GRANT_TTL_SECONDS


@pytest.mark.asyncio
async def test_owner_acknowledgement_is_a_noop_with_no_audit_entry(monkeypatch):
    """Acknowledging your own conversation writes no audit noise."""
    owner = _user()
    conv = _conversation(owner.id)
    redis = _FakeRedis()
    _patch_redis(monkeypatch, redis)

    audited: list[dict] = []

    async def _fake_log_audit(_db, **kwargs):
        audited.append(kwargs)

    import app.services.audit as audit_mod

    monkeypatch.setattr(audit_mod, "log_audit", _fake_log_audit)

    db = _FakeDB([_Result(scalar=conv)])
    resp = await acknowledge_conversation_file_access(
        conversation_id=str(conv.id),
        request=_request(),
        user=owner,
        db=db,
    )

    assert resp.status == "granted"
    assert audited == []
    assert redis.store == {}


@pytest.mark.asyncio
async def test_non_admin_cannot_acknowledge(monkeypatch):
    """Otherwise any user could mint themselves a grant on someone else's thread."""
    conv = _conversation(uuid.uuid4())
    _patch_redis(monkeypatch, _FakeRedis())
    db = _FakeDB([_Result(scalar=conv)])

    with pytest.raises(HTTPException) as exc:
        await acknowledge_conversation_file_access(
            conversation_id=str(conv.id),
            request=_request(),
            user=_user(role="user"),
            db=db,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_audit_entry_survives_a_failed_grant_write(monkeypatch):
    """Redis failing must not erase the record that the admin acknowledged.

    The commit happens before the grant is minted precisely so a Redis outage
    cannot produce an un-audited acknowledgement; the admin gets a 503 and
    retries.
    """
    admin = _user(role="admin")
    conv = _conversation(uuid.uuid4())
    _patch_redis(monkeypatch, _FakeRedis(fail=True))

    audited: list[dict] = []

    async def _fake_log_audit(_db, **kwargs):
        audited.append(kwargs)

    import app.services.audit as audit_mod

    monkeypatch.setattr(audit_mod, "log_audit", _fake_log_audit)

    db = _FakeDB([_Result(scalar=conv), _Result(scalar="owner@example.test")])

    with pytest.raises(HTTPException) as exc:
        await acknowledge_conversation_file_access(
            conversation_id=str(conv.id),
            request=_request(),
            user=admin,
            db=db,
        )

    assert exc.value.status_code == 503
    assert len(audited) == 1
    assert db.commits == 1
