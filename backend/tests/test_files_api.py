"""Tests for the cross-context file repository (Work > Files).

The contract worth pinning is the **scope**: the list is derived from the two
download endpoints' authorization, so a row that is listed is a row that can be
fetched, and nothing more — no ambient admin read of other users' conversation
files, no template assets, shared viewers read but never delete.

No database is required — the predicates are compiled to PostgreSQL SQL and
inspected, and the handlers run against the same pop-style fake session the
conversation files panel tests use.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.api.v1 import files as files_api
from app.api.v1.files import (
    can_delete_file,
    context_sql_predicate,
    delete_file,
    download_url_for,
    file_context,
    list_files,
    mine_files_filter,
    readable_files_filter,
    readable_run_ids,
)
from app.models.template_container import TEMPLATE_ASSET_SOURCE

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, *, scalar=None, count=None, rows=None, tuples=None):
        self._scalar = scalar
        self._count = count
        self._rows = rows or []
        self._tuples = tuples or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self._count if self._count is not None else self._scalar

    def scalars(self):
        return SimpleNamespace(all=lambda: self._rows)

    def all(self):
        return self._tuples


class _FakeDB:
    """Pop-style fake session: each ``execute`` returns the next queued result."""

    def __init__(self, results):
        self._queue = list(results)
        self.commits = 0

    async def execute(self, _statement, *_args, **_kwargs):
        if not self._queue:
            raise AssertionError("unexpected extra DB query")
        return self._queue.pop(0)

    async def commit(self):
        self.commits += 1


def _user(role="user", uid=None):
    return SimpleNamespace(id=uid or uuid.uuid4(), role=role, email=f"{role}@example.test")


def _run(owner_id, *, company=False):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=owner_id,
        is_company_managed=company,
        assignment_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
    )


def _row(
    *,
    user_id=None,
    conversation_id=None,
    assignment_run_id=None,
    message_id=None,
    source="chat",
    filename="report.csv",
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        conversation_id=conversation_id,
        assignment_run_id=assignment_run_id,
        message_id=message_id,
        source=source,
        filename=filename,
        content_type="text/csv",
        size=2048,
        created_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
    )


def _sql(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


# ---------------------------------------------------------------------------
# Context partition
# ---------------------------------------------------------------------------


def test_file_context_partitions_by_binding_conversation_first():
    conv = uuid.uuid4()
    run = uuid.uuid4()
    assert file_context(_row(conversation_id=conv)) == "conversation"
    assert file_context(_row(assignment_run_id=run)) == "run"
    assert file_context(_row()) == "other"
    # A row somehow carrying both is filed under the transcript.
    assert file_context(_row(conversation_id=conv, assignment_run_id=run)) == "conversation"


def test_context_sql_predicate_mirrors_the_python_partition():
    """``conversation + run + other`` must cover the set exactly once."""
    assert context_sql_predicate(None) is None
    conversation = _sql(context_sql_predicate("conversation"))
    run = _sql(context_sql_predicate("run"))
    other = _sql(context_sql_predicate("other"))
    assert "conversation_id IS NOT NULL" in conversation
    assert "conversation_id IS NULL" in run and "assignment_run_id IS NOT NULL" in run
    assert "conversation_id IS NULL" in other and "assignment_run_id IS NULL" in other


# ---------------------------------------------------------------------------
# Readability predicate
# ---------------------------------------------------------------------------


def test_template_assets_are_excluded_for_everyone():
    for role in ("user", "admin"):
        sql = _sql(readable_files_filter(_user(role=role)))
        assert f"file_attachments.source != '{TEMPLATE_ASSET_SOURCE}'" in sql
        assert "file_attachments.source IS NULL" in sql


def test_non_admin_conversation_branch_requires_a_downloadable_path():
    """A shared viewer downloads through the message-scoped URL (needs a
    ``message_id``) or the id-scoped conversation fallback (``user_id`` NULL);
    a row with neither would be listed but 403 on click."""
    sql = _sql(readable_files_filter(_user()))
    assert "file_attachments.message_id IS NOT NULL" in sql
    assert "file_attachments.user_id IS NULL" in sql


def test_admin_conversation_branch_is_still_scoped_to_readable_conversations():
    """No ambient admin read: the admin branch is wider on the ROW (the
    id-scoped endpoint admits any row for an admin) but the conversation set
    is the same one — own, shared, org-link — never every conversation."""
    sql = _sql(readable_files_filter(_user(role="admin")))
    assert "file_attachments.message_id IS NOT NULL" not in sql
    assert "conversation_shares" in sql
    assert "conversations.user_id" in sql
    assert "'org_read'" in sql


def test_readable_run_ids_is_the_download_rule_not_the_visibility_rule():
    """Every user *sees* company-managed runs in Operations, only admins may
    download from them (``require_run_access``). Listing by the wider rule
    would show non-admins files they cannot fetch."""
    non_admin = _sql(readable_run_ids(_user()))
    admin = _sql(readable_run_ids(_user(role="admin")))
    assert "is_company_managed IS NOT true" in non_admin
    assert "is_company_managed IS true" in admin


def test_mine_filter_covers_upload_conversation_and_run_ownership():
    sql = _sql(mine_files_filter(uuid.uuid4()))
    assert "file_attachments.user_id =" in sql
    assert "conversations.user_id =" in sql
    assert "assignment_runs.user_id =" in sql
    # Every term NULL-guarded, so NOT mine is never NULL on a run's file.
    assert "file_attachments.user_id IS NOT NULL" in sql
    assert "file_attachments.conversation_id IS NOT NULL" in sql
    assert "file_attachments.assignment_run_id IS NOT NULL" in sql


# ---------------------------------------------------------------------------
# Download URL
# ---------------------------------------------------------------------------


def test_download_url_is_message_scoped_for_a_linked_conversation_row():
    conv, msg = uuid.uuid4(), uuid.uuid4()
    row = _row(conversation_id=conv, message_id=msg)
    assert download_url_for(row, _user()) == (
        f"/api/v1/chat/conversations/{conv}/messages/{msg}/files/{row.id}/download"
    )


def test_download_url_is_id_scoped_for_admins_and_unlinked_rows():
    """``download_message_file`` 403s every non-owner regardless of role, so an
    admin gets the id-scoped URL whose admin branch admits them; a run output
    has no message to scope by."""
    conv, msg = uuid.uuid4(), uuid.uuid4()
    linked = _row(conversation_id=conv, message_id=msg)
    assert download_url_for(linked, _user(role="admin")) == (f"/api/v1/chat/files/{linked.id}/download")
    orphan = _row(conversation_id=conv, message_id=None)
    assert "/messages/" not in download_url_for(orphan, _user())
    produced = _row(assignment_run_id=uuid.uuid4())
    assert download_url_for(produced, _user()) == (f"/api/v1/chat/files/{produced.id}/download")


# ---------------------------------------------------------------------------
# Deletion rights
# ---------------------------------------------------------------------------


def test_owner_of_the_file_may_delete_it():
    me = _user()
    row = _row(user_id=me.id, conversation_id=uuid.uuid4())
    assert can_delete_file(row, me, conversation_owner_id=uuid.uuid4(), run=None)


def test_conversation_owner_may_delete_files_in_it_shared_viewer_may_not():
    owner, viewer = _user(), _user()
    row = _row(user_id=None, conversation_id=uuid.uuid4(), message_id=uuid.uuid4())
    assert can_delete_file(row, owner, conversation_owner_id=owner.id, run=None)
    assert not can_delete_file(row, viewer, conversation_owner_id=owner.id, run=None)


def test_run_deletion_follows_require_run_access():
    me, other, admin = _user(), _user(), _user(role="admin")
    personal = _run(me.id)
    row = _row(assignment_run_id=personal.id)
    assert can_delete_file(row, me, conversation_owner_id=None, run=personal)
    assert not can_delete_file(row, other, conversation_owner_id=None, run=personal)
    # An admin acts on every run, another user's personal one included...
    assert can_delete_file(row, admin, conversation_owner_id=None, run=personal)
    # ...and a company-managed one, which its non-admin owner does not.
    company = _run(me.id, company=True)
    company_row = _row(assignment_run_id=company.id)
    assert can_delete_file(company_row, admin, conversation_owner_id=None, run=company)
    assert not can_delete_file(company_row, me, conversation_owner_id=None, run=company)


def test_template_assets_are_never_deletable_here():
    me = _user()
    row = _row(user_id=me.id, source=TEMPLATE_ASSET_SOURCE)
    assert not can_delete_file(row, me, conversation_owner_id=None, run=None)


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_files_labels_each_row_with_its_context():
    me = _user()
    conv_id, msg_id = uuid.uuid4(), uuid.uuid4()
    run = _run(me.id)
    uploaded = _row(user_id=me.id, conversation_id=conv_id, message_id=msg_id, source="chat")
    produced = _row(assignment_run_id=run.id, source="agent_generated", filename="digest.pdf")
    loose = _row(user_id=me.id, source="dataset", filename="cases.csv")
    db = _FakeDB(
        [
            _Result(count=3),
            _Result(rows=[uploaded, produced, loose]),
            _Result(tuples=[(conv_id, "Investigation", me.id)]),
            _Result(tuples=[(run, None, "Analyst")]),
            # The file retention rules (#396): nothing stored, the defaults.
            _Result(scalar=None),
        ]
    )

    resp = await list_files(
        limit=50,
        offset=0,
        search=None,
        context=None,
        origin=None,
        ownership=None,
        vis=None,
        conversation_id=None,
        assignment_run_id=None,
        user=me,
        db=db,
    )

    assert resp.total == 3
    by_name = {f.filename: f for f in resp.files}

    chat_file = by_name["report.csv"]
    assert chat_file.context == "conversation"
    assert chat_file.origin == "uploaded"
    assert chat_file.conversation_title == "Investigation"
    assert chat_file.can_delete is True
    assert "/messages/" in chat_file.download_url

    run_file = by_name["digest.pdf"]
    assert run_file.context == "run"
    assert run_file.origin == "generated"
    # No assignment on the run: the agent's name stands in for the label.
    assert run_file.run_label == "Analyst"
    assert run_file.agent_name == "Analyst"
    assert run_file.can_delete is True

    dataset_file = by_name["cases.csv"]
    assert dataset_file.context == "other"
    assert dataset_file.source == "dataset"
    assert dataset_file.origin == "other"

    # Expires (#396): chat uploads are kept for good by default; a dataset
    # belongs to no retention category; none of them is deleted.
    assert chat_file.expires_at is None
    assert dataset_file.expires_at is None
    assert chat_file.deleted_at is None


@pytest.mark.asyncio
async def test_list_files_skips_label_lookups_when_the_page_has_no_bindings():
    """Two set-based lookups per page, and none when nothing references a
    conversation or a run — a fake session with only the count and the page
    queued would otherwise raise on the extra query."""
    me = _user()
    db = _FakeDB([_Result(count=1), _Result(rows=[_row(user_id=me.id, source="tool_test")])])

    resp = await list_files(
        limit=50,
        offset=0,
        search=None,
        context=None,
        origin=None,
        ownership=None,
        vis=None,
        conversation_id=None,
        assignment_run_id=None,
        user=me,
        db=db,
    )

    assert resp.total == 1
    assert resp.files[0].context == "other"
    assert resp.files[0].conversation_title is None
    assert resp.files[0].run_label is None


@pytest.mark.asyncio
async def test_list_files_rejects_a_malformed_deep_link_id():
    with pytest.raises(HTTPException) as exc:
        await list_files(
            limit=50,
            offset=0,
            search=None,
            context=None,
            origin=None,
            ownership=None,
            vis=None,
            conversation_id="not-a-uuid",
            assignment_run_id=None,
            user=_user(),
            db=_FakeDB([]),
        )
    # ``parse_uuid``'s contract: a malformed id is a validation error, and it is
    # raised before any query runs (the fake session has nothing queued).
    assert exc.value.status_code == 422


# ---------------------------------------------------------------------------
# Deletion endpoint
# ---------------------------------------------------------------------------


def _patch_cleanup(monkeypatch):
    deleted: list[dict] = []

    async def _fake_delete(_db, **filters):
        deleted.append(filters)
        return 1

    import app.services.file_cleanup as cleanup_mod

    monkeypatch.setattr(cleanup_mod, "delete_file_attachments", _fake_delete)
    return deleted


def _patch_audit(monkeypatch):
    audited: list[dict] = []

    async def _fake_log_audit(_db, **kwargs):
        audited.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(files_api, "log_audit", _fake_log_audit)
    return audited


@pytest.mark.asyncio
async def test_delete_removes_row_and_object_and_audits(monkeypatch):
    me = _user()
    row = _row(user_id=me.id, filename="draft.docx")
    deleted = _patch_cleanup(monkeypatch)
    audited = _patch_audit(monkeypatch)
    db = _FakeDB([_Result(scalar=row)])

    assert await delete_file(file_id=str(row.id), user=me, db=db) is None

    assert deleted == [{"id": row.id}]
    assert len(audited) == 1
    assert audited[0]["action"] == "delete"
    assert audited[0]["entity_type"] == "file"
    assert audited[0]["entity_id"] == row.id
    assert audited[0]["entity_name"] == "draft.docx"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_delete_of_an_unreadable_file_is_404_not_403(monkeypatch):
    """The id must not be an oracle for files the requester cannot see."""
    _patch_cleanup(monkeypatch)
    # Readable lookup finds nothing; the template-asset probe finds nothing.
    db = _FakeDB([_Result(scalar=None), _Result(scalar=None)])
    with pytest.raises(HTTPException) as exc:
        await delete_file(file_id=str(uuid.uuid4()), user=_user(), db=db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_of_a_template_asset_points_at_its_template(monkeypatch):
    """A template asset the requester may READ is pointed at its template."""
    from app.services import file_access

    _patch_cleanup(monkeypatch)
    asset = _row(user_id=uuid.uuid4(), source=TEMPLATE_ASSET_SOURCE)

    async def _readable(*_args, **_kwargs):
        return True

    monkeypatch.setattr(file_access, "can_read_file", _readable)
    db = _FakeDB([_Result(scalar=None), _Result(scalar=asset)])
    with pytest.raises(HTTPException) as exc:
        await delete_file(file_id=str(asset.id), user=_user(), db=db)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_delete_of_an_unreadable_template_asset_is_answered_like_an_unknown_id(monkeypatch):
    """A template asset in a template the requester cannot see is not pointed
    at anything: the answer is the unknown-id 404, word for word."""
    from app.services import file_access

    _patch_cleanup(monkeypatch)
    asset = _row(user_id=uuid.uuid4(), source=TEMPLATE_ASSET_SOURCE)

    async def _unreadable(*_args, **_kwargs):
        return False

    monkeypatch.setattr(file_access, "can_read_file", _unreadable)
    with pytest.raises(HTTPException) as unreadable:
        await delete_file(
            file_id=str(asset.id), user=_user(), db=_FakeDB([_Result(scalar=None), _Result(scalar=asset)])
        )
    with pytest.raises(HTTPException) as unknown:
        await delete_file(
            file_id=str(uuid.uuid4()), user=_user(), db=_FakeDB([_Result(scalar=None), _Result(scalar=None)])
        )
    assert (unreadable.value.status_code, unreadable.value.detail, unreadable.value.headers) == (
        unknown.value.status_code,
        unknown.value.detail,
        unknown.value.headers,
    )
    assert unknown.value.status_code == 404


@pytest.mark.asyncio
async def test_shared_viewer_cannot_delete_a_readable_file(monkeypatch):
    """Reading and deleting are different rights: the row IS in the viewer's
    readable set (they got it through a share) but the conversation is not
    theirs, so the delete is refused — and refused as 404, like the rest."""
    viewer, owner = _user(), _user()
    conv_id = uuid.uuid4()
    row = _row(user_id=None, conversation_id=conv_id, message_id=uuid.uuid4())
    deleted = _patch_cleanup(monkeypatch)
    audited = _patch_audit(monkeypatch)
    db = _FakeDB([_Result(scalar=row), _Result(tuples=[(conv_id, "Investigation", owner.id)])])

    with pytest.raises(HTTPException) as exc:
        await delete_file(file_id=str(row.id), user=viewer, db=db)

    assert exc.value.status_code == 404
    assert deleted == []
    assert audited == []
    assert db.commits == 0
