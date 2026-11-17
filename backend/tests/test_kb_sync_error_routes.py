"""The sync routes serve a reader a sentence and a manager the full error.

Runs the real route functions and the real manage rule, as non-admin callers
unless the case is about an admin, with the knowledge base lookup and the
database stubbed. The SQL search itself is pinned on a real PostgreSQL in
``test_kb_sync_error_readers_postgres.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from app.api.v1 import knowledge_bases as kb_mod
from app.services.kb_document_errors import EMBEDDING_FAILED_EVENT, public_activity_details
from tests.test_knowledge_bases_list import _fake_kb

# What a failed sync stores: the HTTP client's message, with the request URL.
_HIDDEN = ("payroll-repo", "git.internal.example", "hidden-marker")
_RAW = (
    "Client error '401 Unauthorized' for url 'https://git.internal.example/acme/payroll-repo/contents' (hidden-marker)"
)
_READER_WORDING = "manage this knowledge base"

_SOURCE_ERRORS = (
    _RAW,
    "Sync timed out after 1200s",
    f"Source quarantined after 5 consecutive failures (type=github). Last error: {_RAW}",
    f"Source disabled after configuration error: {_RAW}",
    None,
)
_ENTRIES = (
    ("sync_started", "Sync started for github"),
    ("sync_completed", "Synced 12 document(s) from github"),
    ("sync_failed", f"Sync failed: {_RAW}"),
    ("sync_alert", f"Sync has failed 5 consecutive times (github). Last error: {_RAW}"),
    ("sync_quarantined", f"Source disabled after 10 consecutive failures (github). Last error: {_RAW}"),
    ("sync_config_error", f"Sync source disabled due to a configuration error (github): {_RAW}"),
    ("sync_failed", None),
    (EMBEDDING_FAILED_EVENT, f"Failed to embed 'report.pdf': {_RAW}"),
    ("document_added", "Uploaded file 'handbook.md'"),
)

_GROUP = str(uuid.uuid4())
_READER_ID = uuid.uuid4()


def _caller(user_id: uuid.UUID | None = None, *, role: str = "user") -> SimpleNamespace:
    return SimpleNamespace(id=user_id or uuid.uuid4(), role=role, is_admin=role == "admin")


def _shared_kb() -> SimpleNamespace:
    # Read-only for one user, config rights for one group.
    return _fake_kb(
        shared_user_ids=[str(_READER_ID)],
        shared_user_permissions={str(_READER_ID): "read_only"},
        shared_group_ids=[_GROUP],
        shared_group_permissions={_GROUP: "config"},
    )


def _company_kb() -> SimpleNamespace:
    return _fake_kb(user_id=None, is_company_managed=True)


def _source(error: str | None) -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        source_type="github",
        config={"repo": "acme/docs"},
        sync_interval="daily",
        enabled=True,
        integration_id=None,
        last_synced_at=now,
        last_sync_status="error" if error else "success",
        last_sync_error=error,
        last_sync_documents=0,
        created_at=now,
        updated_at=now,
    )


def _entry(event: str, details: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        event=event,
        details=details,
        source_type="github",
        document_count=None,
        created_at=datetime.now(UTC),
    )


class _Rows:
    def __init__(self, items: list) -> None:
        self._items = items

    def scalars(self) -> SimpleNamespace:
        return SimpleNamespace(all=lambda: list(self._items))

    def scalar_one(self) -> int:
        return len(self._items)


class _Session:
    """Answers every query with the same rows and keeps the statements."""

    def __init__(self, items: list) -> None:
        self.items = items
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Rows(self.items)


def _stub_lookups(monkeypatch, kb: SimpleNamespace) -> None:
    async def _groups(_db, user_id):
        return [_GROUP] if user_id == _EDITOR.id else []

    monkeypatch.setattr(kb_mod, "get_user_group_ids", _groups)
    monkeypatch.setattr(kb_mod, "get_user_knowledge_base", AsyncMock(return_value=kb))


_EDITOR = _caller()
_SHARED_KB = _shared_kb()
_COMPANY_KB = _company_kb()

# (knowledge base, caller): who manages it, who only reads it.
_MANAGERS = {
    "owner": (_SHARED_KB, _caller(_SHARED_KB.user_id)),
    "config group grantee": (_SHARED_KB, _EDITOR),
    "admin of a company-managed kb": (_COMPANY_KB, _caller(role="admin")),
}
_READERS = {
    "read-only grantee": (_SHARED_KB, _caller(_READER_ID)),
    "any user of a company-managed kb": (_COMPANY_KB, _caller()),
}


def _hides_everything(text: str | None) -> bool:
    return not any(marker in (text or "") for marker in _HIDDEN)


@pytest.mark.parametrize("who", sorted(_MANAGERS))
@pytest.mark.asyncio
async def test_a_manager_is_served_the_stored_sync_error(monkeypatch, who):
    kb, caller = _MANAGERS[who]
    _stub_lookups(monkeypatch, kb)
    sources = [_source(error) for error in _SOURCE_ERRORS]

    served = await kb_mod.list_sync_sources(str(kb.id), user=caller, db=_Session(sources))

    assert [s.last_sync_error for s in served] == list(_SOURCE_ERRORS)


@pytest.mark.parametrize("who", sorted(_READERS))
@pytest.mark.asyncio
async def test_a_reader_is_served_a_sentence_never_the_sync_error(monkeypatch, who):
    kb, caller = _READERS[who]
    _stub_lookups(monkeypatch, kb)
    sources = [_source(error) for error in _SOURCE_ERRORS]

    served = await kb_mod.list_sync_sources(str(kb.id), user=caller, db=_Session(sources))

    assert len(served) == len(_SOURCE_ERRORS)
    for source, stored in zip(served, _SOURCE_ERRORS, strict=True):
        assert _hides_everything(source.last_sync_error), source.last_sync_error
        if stored is None:
            assert source.last_sync_error is None
        else:
            assert source.last_sync_error != stored
            assert _READER_WORDING in source.last_sync_error
        # Nothing else about the source changes.
        assert source.last_sync_status == ("error" if stored else "success")
    assert "timed out" in served[1].last_sync_error
    assert "configuration error" in served[3].last_sync_error


@pytest.mark.parametrize("who", sorted(_MANAGERS))
@pytest.mark.asyncio
async def test_a_manager_is_served_the_activity_details_as_before(monkeypatch, who):
    kb, caller = _MANAGERS[who]
    _stub_lookups(monkeypatch, kb)
    entries = [_entry(event, details) for event, details in _ENTRIES]

    served = await kb_mod.list_activity_logs(
        str(kb.id), limit=50, offset=0, search=None, user=caller, db=_Session(entries)
    )

    assert [e.details for e in served] == [public_activity_details(event, details) for event, details in _ENTRIES]


@pytest.mark.parametrize("who", sorted(_READERS))
@pytest.mark.asyncio
async def test_a_reader_is_served_sentences_in_the_activity_feed(monkeypatch, who):
    kb, caller = _READERS[who]
    _stub_lookups(monkeypatch, kb)
    entries = [_entry(event, details) for event, details in _ENTRIES]

    served = await kb_mod.list_activity_logs(
        str(kb.id), limit=50, offset=0, search=None, user=caller, db=_Session(entries)
    )

    for entry, (event, details) in zip(served, _ENTRIES, strict=True):
        assert _hides_everything(entry.details), entry.details
        if event in ("sync_failed", "sync_alert", "sync_quarantined", "sync_config_error"):
            if details is None:
                assert entry.details is None
            else:
                assert _READER_WORDING in entry.details
        else:
            # Entries that never carry a sync error are served as before.
            assert entry.details == public_activity_details(event, details)


def _compiled(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


@pytest.mark.parametrize("who", sorted({**_MANAGERS, **_READERS}))
@pytest.mark.asyncio
async def test_the_activity_search_runs_on_what_the_caller_is_served(monkeypatch, who):
    kb, caller = {**_MANAGERS, **_READERS}[who]
    _stub_lookups(monkeypatch, kb)
    session = _Session([])

    await kb_mod.list_activity_logs(str(kb.id), limit=50, offset=0, search="payroll-repo", user=caller, db=session)
    await kb_mod.count_activity_logs(str(kb.id), search="payroll-repo", user=caller, db=session)

    assert len(session.statements) == 2
    for statement in session.statements:
        sql = _compiled(statement)
        # Never the stored column itself ...
        assert "kb_activity_logs.details ILIKE" not in sql
        # ... and, for a reader only, the sentences a reader is served.
        assert (_READER_WORDING in sql) is (who in _READERS), sql
