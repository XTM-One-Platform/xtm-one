"""Sync errors are served per caller, on a real PostgreSQL.

A failed sync keeps its full error text on the source and in the activity feed,
for the people who fix the source. A reader of the knowledge base who cannot
manage it is served a fixed sentence instead, and the activity search and its
count, which run in SQL, match only what each caller is served: a match on the
stored details would let a reader's count answer whether a hidden term is in
them. Runs the real routes, as non-admin callers except
the company-managed knowledge base's admin, in an isolated temporary database.
"""

from __future__ import annotations

import uuid

import pytest

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1 import knowledge_bases as kb_mod
from app.models.group import Group, GroupMember
from app.models.knowledge_base import KBActivityLog, KBSyncSource, KnowledgeBase
from app.models.user import User
from app.services.kb_document_errors import EMBEDDING_FAILED_EVENT, public_activity_details
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

# What a failed sync stores: the HTTP client's message, with the URL of the
# request. Nothing of it may reach a reader, or be found by a reader's search.
_HIDDEN = ("payroll-repo", "git.internal.example", "hidden-marker")
_RAW = (
    "Client error '401 Unauthorized' for url 'https://git.internal.example/acme/payroll-repo/contents' (hidden-marker)"
)

# ``last_sync_error`` as the scheduler writes it: the exception text, its own
# timeout and quarantine messages, a configuration-error disable, a success.
_SOURCE_ERRORS = (
    _RAW,
    "Sync timed out after 1200s",
    "Source quarantined after 5 consecutive failures (type=github). The source "
    "configuration may be permanently invalid (e.g. a renamed folder or deleted "
    "path). Update the configuration and use Force Re-sync to retry, or re-enable "
    f"the source in settings. Last error: {_RAW}",
    "Source needs reconfiguration after 5 consecutive 'not found' errors "
    "(type=github). The configured path, folder, or resource no longer exists - "
    f"update the source configuration and use Force Re-sync to retry. Last error: {_RAW}",
    f"Source disabled after configuration error: {_RAW}",
    None,
)

# Activity entries as the scheduler, the API and the document processor write them.
_ENTRIES = (
    ("sync_started", "Sync started for github"),
    ("sync_completed", "Synced 12 document(s) from github"),
    ("sync_failed", f"Sync failed: {_RAW}"),
    ("sync_failed", "Sync timed out after 1200s"),
    ("sync_failed", None),
    ("sync_alert", f"Sync has failed 5 consecutive times (github). Last error: {_RAW}"),
    (
        "sync_alert",
        "Stale Notion page reference(s) quarantined after 3 consecutive 404/410 "
        "responses and skipped in future syncs: 1a2b3c. Remove the reference(s) "
        "from the sync source or use Force Re-sync to retry.",
    ),
    (
        "sync_alert",
        "Sync source has had page-level failures in 3 consecutive run(s) (notion). "
        "Some page references may be stale or inaccessible (e.g. deleted Notion "
        "pages). Review the source configuration and remove broken page IDs.",
    ),
    (
        "sync_quarantined",
        "Source disabled after 10 consecutive failures (github). The source "
        "configuration may be permanently invalid (e.g. a renamed folder or deleted "
        f"path). Last error: {_RAW}",
    ),
    ("sync_quarantined", _SOURCE_ERRORS[3]),
    (
        "sync_quarantined",
        "Source quarantined after 3 consecutive stale resets (type=github). The "
        "sync repeatedly hangs without completing - consider narrowing the sync "
        "scope. Use Force Re-sync to retry or re-enable the source in settings.",
    ),
    (
        "sync_config_error",
        f"Sync source disabled due to a configuration error (microsoft-drive): {_RAW}",
    ),
    # A sync event no rule names yet: hidden from readers until one does.
    ("sync_warning", f"Something new happened: {_RAW}"),
    (EMBEDDING_FAILED_EVENT, f"Failed to embed 'report.pdf': {_RAW}"),
    ("document_added", "Uploaded file 'handbook.md'"),
    ("force_resync", "Force re-sync started for github (purged 12 documents)"),
)

_TERMS = (
    *_HIDDEN,
    "unauthorized",
    "timed out",
    "1200s",
    "sync failed",
    "the sync",
    "disabled",
    "quarantined",
    "configuration error",
    "manage this knowledge base",
    "several times",
    "notion",
    "sync_failed",
    "sync_alert",
    "sync_warning",
    "document(s)",
    "failed to embed",
    "internal error",
    "handbook.md",
    "purged",
)


def _user(label: str, *, role: str = "user") -> User:
    return User(
        email=f"{label}-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name=label.title(),
        role=role,
    )


async def _served_sources(kb: KnowledgeBase, user: User, session) -> dict[str, str | None]:
    listed = await kb_mod.list_sync_sources(str(kb.id), user=user, db=session)
    return {source.id: source.last_sync_error for source in listed}


async def _served_activity(kb: KnowledgeBase, user: User, session, search: str | None = None):
    kb_id = str(kb.id)
    listed = await kb_mod.list_activity_logs(kb_id, limit=200, offset=0, search=search, user=user, db=session)
    counted = await kb_mod.count_activity_logs(kb_id, search=search, user=user, db=session)
    return listed, counted["count"]


def _hides_everything(text: str | None) -> bool:
    return not any(marker in (text or "") for marker in _HIDDEN)


async def _assert_the_search_matches_only_what_is_served(kb, user, session) -> list:
    everything, total = await _served_activity(kb, user, session)
    assert total == len(everything) == len(_ENTRIES)
    for term in _TERMS:
        needle = term.lower()
        expected = {
            entry.id for entry in everything if needle in entry.event.lower() or needle in (entry.details or "").lower()
        }
        listed, counted = await _served_activity(kb, user, session, search=term)
        assert {entry.id for entry in listed} == expected, (user.full_name, term)
        assert counted == len(expected), (user.full_name, term)
    return everything


async def test_readers_are_served_sentences_and_managers_the_full_error():
    async with (
        scratch_database(
            "kbsyncreaders", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
        ) as db,
        db.sessions() as session,
    ):
        owner, editor, reader, member = (
            _user("owner"),
            _user("editor"),
            _user("reader"),
            _user("member"),
        )
        admin = _user("admin", role="admin")
        session.add_all([owner, editor, reader, member, admin])
        await session.flush()
        editors = Group(name=f"Editors {uuid.uuid4().hex[:8]}")
        session.add(editors)
        await session.flush()
        session.add(GroupMember(group_id=editors.id, user_id=editor.id))

        # The owner's knowledge base, shared read-only with one user and with
        # config rights with a group; and a company-managed one, which every
        # user reads and only admins manage.
        docs = KnowledgeBase(
            name="Docs",
            user_id=owner.id,
            shared_user_ids=[str(reader.id)],
            shared_user_permissions={str(reader.id): "read_only"},
            shared_group_ids=[str(editors.id)],
            shared_group_permissions={str(editors.id): "config"},
        )
        company = KnowledgeBase(name="Company docs", user_id=None, is_company_managed=True)
        session.add_all([docs, company])
        await session.flush()

        stored: dict[str, str | None] = {}
        rows: dict[str, KBActivityLog] = {}
        for kb in (docs, company):
            for error in _SOURCE_ERRORS:
                source = KBSyncSource(
                    knowledge_base_id=kb.id,
                    user_id=owner.id if kb is docs else admin.id,
                    source_type="github",
                    config={"repo": "acme/docs"},
                    last_sync_status="error" if error else "success",
                    last_sync_error=error,
                )
                session.add(source)
                await session.flush()
                stored[str(source.id)] = error
            for event, details in _ENTRIES:
                row = KBActivityLog(knowledge_base_id=kb.id, event=event, details=details)
                session.add(row)
                await session.flush()
                rows[str(row.id)] = row
        await session.commit()
        assert not any(u.is_admin for u in (owner, editor, reader, member))

        managers = ((docs, owner), (docs, editor), (company, admin))
        readers = ((docs, reader), (company, member))

        for kb, user in managers:
            # Exactly what they were served before: the stored error, and the
            # entry details as ``public_activity_details`` cleans them.
            sources = await _served_sources(kb, user, session)
            assert sources == {sid: stored[sid] for sid in sources}, user.full_name
            everything = await _assert_the_search_matches_only_what_is_served(kb, user, session)
            for entry in everything:
                row = rows[entry.id]
                assert entry.details == public_activity_details(row.event, row.details)
            for marker in _HIDDEN:
                _listed, counted = await _served_activity(kb, user, session, search=marker)
                assert counted > 0, (user.full_name, marker)

        for kb, user in readers:
            sources = await _served_sources(kb, user, session)
            assert len(sources) == len(_SOURCE_ERRORS)
            for sid, served in sources.items():
                assert _hides_everything(served), (user.full_name, served)
                if stored[sid] is None:
                    assert served is None
                else:
                    assert served != stored[sid]
                    assert "manage this knowledge base" in served
            served_errors = [served for served in sources.values() if served]
            assert any("timed out" in served for served in served_errors)
            assert any("configuration error" in served for served in served_errors)

            everything = await _assert_the_search_matches_only_what_is_served(kb, user, session)
            for entry in everything:
                row = rows[entry.id]
                assert _hides_everything(entry.details), (user.full_name, entry.details)
                if row.event.startswith("sync_") and row.event not in (
                    "sync_started",
                    "sync_completed",
                ):
                    if row.details:
                        assert "manage this knowledge base" in entry.details
                    else:
                        assert entry.details == row.details
                else:
                    assert entry.details == public_activity_details(row.event, row.details)
            for marker in _HIDDEN:
                listed, counted = await _served_activity(kb, user, session, search=marker)
                assert (listed, counted) == ([], 0), (user.full_name, marker)
