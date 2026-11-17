"""Deleting a dataset entry or a dataset keeps the files other things use.

Both routes used to call ``delete_file_attachments`` for every id their entries
listed, deleting the row and the stored object of files that belong to a
conversation, a run, a template, a deck, a flow run or another dataset (``file_ids``
is written as the client sends it). They now go through the same helper as
removing one entry file: a listed file is deleted only when it is a
dataset upload nothing else uses, locked while deciding; otherwise only the
reference goes away with the entry. The entry / dataset deletion itself is
unchanged.

The SQL half of the rule (the locked read, the foreign keys, the text scan)
runs against a real database in ``test_dataset_file_references_postgres.py``;
this module pins the routes' side of the contract and the helper's decisions.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

import app.models  # noqa: F401 - every table on Base.metadata, for the inventory tripwires
from app.api import deps
from app.api.v1 import datasets as datasets_api


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


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_UPLOADED_AT = datetime(2026, 9, 1, tzinfo=UTC)


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


class _Result:
    def __init__(self, value):
        self._value = value

    def all(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: self._value)


class _RouteSession:
    """Pops one queued answer per ``execute``; records the order of what the route does."""

    def __init__(self, answers=()):
        self._answers = list(answers)
        self.statements: list = []
        self.deleted: list = []
        self.log: list[str] = []

    async def execute(self, statement):
        self.statements.append(statement)
        if not self._answers:
            raise AssertionError("unexpected query")
        return _Result(self._answers.pop(0))

    async def delete(self, obj):
        self.deleted.append(obj)
        self.log.append("delete")

    async def flush(self):
        self.log.append("flush")

    async def commit(self):
        self.log.append("commit")


def _admin():
    return SimpleNamespace(id=uuid.uuid4(), role="admin", is_admin=True)


def _dataset():
    return SimpleNamespace(
        id=uuid.uuid4(), name="Bench", is_company_managed=False, is_read_only=False, user_id=uuid.uuid4(), content_hash="h"
    )


@pytest.fixture
def released(monkeypatch):
    """What the routes hand the shared rule, and the keys purged once committed."""
    calls: list[SimpleNamespace] = []
    purged: list[list[str]] = []

    async def _release(db, file_uuids):
        calls.append(SimpleNamespace(file_uuids=set(file_uuids), log=list(db.log)))
        db.log.append("release")
        return [f"key-{file_uuid}" for file_uuid in sorted(file_uuids)]

    async def _purge(keys):
        purged.append(list(keys))
        return len(keys)

    monkeypatch.setattr(datasets_api, "_delete_dataset_files_if_unreferenced", _release)
    monkeypatch.setattr("app.services.file_cleanup.purge_storage_objects", _purge)
    return SimpleNamespace(calls=calls, purged=purged)


# -- DELETE /datasets/entries/{entry_id} -------------------------------------


def _delete_entry(monkeypatch, file_ids, user=None):
    entry = SimpleNamespace(id=uuid.uuid4(), file_ids=file_ids)
    dataset = _dataset()

    async def _load(_db, _user, _entry_id):
        return entry, dataset

    monkeypatch.setattr(datasets_api, "_get_user_dataset_entry_with_dataset", _load)
    db = _RouteSession()
    assert asyncio.run(datasets_api.delete_entry(str(entry.id), user=user or _admin(), db=db)) is None
    # The entry deletion itself is unchanged.
    assert db.deleted == [entry] and dataset.content_hash is None
    return entry, dataset, db


def test_entry_delete_hands_every_listed_file_to_the_shared_rule(monkeypatch, released):
    first, second = uuid.uuid4(), uuid.uuid4()
    _entry, _dataset_row, db = _delete_entry(monkeypatch, [str(first), second.hex.upper(), "not-a-uuid"])

    (call,) = released.calls
    assert call.file_uuids == {first, second}, "a malformed stored id names nothing"
    # The entry goes first (flushed), the file rows after, then the commit,
    # and only then the stored objects.
    assert call.log == ["delete", "flush"]
    assert db.log == ["delete", "flush", "release", "commit"]
    assert released.purged == [sorted(f"key-{f}" for f in (first, second))]


def test_entry_delete_without_files_changes_no_file(monkeypatch, released):
    _delete_entry(monkeypatch, None)
    assert released.calls[0].file_uuids == set()


# -- DELETE /datasets/{dataset_id} -------------------------------------------


def _delete_dataset(monkeypatch, entry_rows, user=None):
    dataset = _dataset()
    audits: list[dict] = []

    async def _groups(_db, _user_id):
        return []

    async def _load(_db, _user_id, _dataset_id, _groups, *, is_admin=False):
        return dataset

    async def _audit(_db, **kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(datasets_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(datasets_api, "_get_user_dataset", _load)
    monkeypatch.setattr(datasets_api, "log_audit", _audit)
    db = _RouteSession([entry_rows])
    assert asyncio.run(datasets_api.delete_dataset(str(dataset.id), user=user or _admin(), db=db)) is None
    assert db.deleted == [dataset]
    assert [a["summary"] for a in audits] == [f"Deleted dataset '{dataset.name}'"]
    return dataset, db


def test_dataset_delete_hands_the_files_of_its_entries_to_the_shared_rule(monkeypatch, released):
    upload, other = uuid.uuid4(), uuid.uuid4()
    rows = [([str(upload), "junk"],), ([str(upload), str(other)],)]
    _dataset_row, db = _delete_dataset(monkeypatch, rows)

    (call,) = released.calls
    assert call.file_uuids == {upload, other}
    assert db.log == ["delete", "flush", "release", "commit"]
    assert released.purged == [sorted(f"key-{f}" for f in (upload, other))]


def test_dataset_delete_reads_only_the_entries_that_list_files(monkeypatch, released):
    """A HuggingFace import holds up to 500k entries and no file: none of them is loaded."""
    _dataset_row, db = _delete_dataset(monkeypatch, [])
    sql = _sql(db.statements[0])
    assert "dataset_entries.file_ids IS NOT NULL" in sql
    assert "NOT IN ('null', '[]')" in sql
    assert released.calls[0].file_uuids == set()


# -- The shared rule ----------------------------------------------------------


class _HelperSession:
    """Answers the helper's locked read and foreign-key checks from in-memory rows."""

    def __init__(self, uploads, fk_used=None):
        self.uploads = set(uploads)  # the unbound dataset uploads the locked read finds
        self.fk_used = fk_used or {}  # {table: {file id}}
        self.statements: list[tuple[object, str]] = []

    async def execute(self, statement):
        sql = _sql(statement)
        self.statements.append((statement, sql))
        asked = {uuid.UUID(found) for found in _UUID_RE.findall(sql)}
        if "FOR UPDATE" in sql:
            return _Result([(fid, _UPLOADED_AT) for fid in sorted(self.uploads & asked)])
        table = next((name for name in self.fk_used if f"FROM {name}" in sql), None)
        return _Result([fid for fid in self.fk_used.get(table, ()) if fid in asked])


@pytest.fixture
def rule(monkeypatch):
    """The helper with its text scan answered from a table of holders."""
    state = SimpleNamespace(named={}, scans=[], deleted=[])

    async def _named_in(_db, text, criteria, file_ids):
        state.scans.append((_sql(text), set(file_ids)))
        table = next((name for name in state.named if name in state.scans[-1][0]), None)
        return {fid for fid in state.named.get(table, ()) if fid in state.scans[-1][1]}

    async def _rows(_db, **filters):
        batch = list(filters["id"])
        state.deleted.append(batch)
        return [f"key-{fid}" for fid in batch]

    monkeypatch.setattr(datasets_api, "_file_ids_named_in", _named_in)
    monkeypatch.setattr("app.services.file_cleanup.delete_file_attachment_rows", _rows)
    return state


def _run_rule(db, file_uuids):
    return asyncio.run(datasets_api._delete_dataset_files_if_unreferenced(db, file_uuids))


def test_the_locked_read_asks_only_for_unbound_dataset_uploads_in_id_order(rule):
    db = _HelperSession(set())
    ids = [uuid.uuid4() for _ in range(3)]
    _run_rule(db, ids)
    statement, sql = db.statements[0]
    assert statement._for_update_arg is not None, "the rows stay locked while deciding"
    assert "file_attachments.source = 'dataset'" in sql
    for column in ("conversation_id", "message_id", "assignment_run_id", "template_container_id"):
        assert f"file_attachments.{column} IS NULL" in sql
    assert "ORDER BY file_attachments.id" in sql, "a fixed lock order: two deletions never deadlock"


def test_an_unused_dataset_upload_goes_whoever_uploaded_it(rule):
    """Like deleting its entries always did: a file only they used is never left behind."""
    ids = {uuid.uuid4() for _ in range(3)}
    db = _HelperSession(ids)
    keys = _run_rule(db, ids)
    assert sorted(keys) == sorted(f"key-{fid}" for fid in ids)
    (_statement, sql), *_rest = db.statements
    assert "user_id" not in sql and "s3_object_name" not in sql, "no uploader or storage key is looked at"


def test_a_file_a_foreign_key_or_a_text_holder_names_is_kept(rule):
    free, workset_item, runner_result, in_deck, in_run = (uuid.uuid4() for _ in range(5))
    ids = [free, workset_item, runner_result, in_deck, in_run]
    db = _HelperSession(ids, fk_used={"workset_items": {workset_item}, "runner_commands": {runner_result}})
    rule.named = {"presentation_versions": {in_deck}, "assignment_runs": {in_run}}

    keys = _run_rule(db, ids)

    assert keys == [f"key-{free}"]
    # The text scans were only asked about files no foreign key holds.
    assert all(not ({workset_item, runner_result} & asked) for _sql_text, asked in rule.scans)


def test_queries_are_batched_not_per_file(rule, monkeypatch):
    monkeypatch.setattr(datasets_api, "_FILE_ID_BATCH", 10)
    ids = [uuid.uuid4() for _ in range(25)]
    db = _HelperSession(ids)

    keys = _run_rule(db, ids)

    assert len(keys) == 25
    locked = [sql for _statement, sql in db.statements if "FOR UPDATE" in sql]
    assert [len(_UUID_RE.findall(sql)) for sql in locked] == [10, 10, 5]
    foreign_keys = len(datasets_api._file_attachment_foreign_keys())
    assert len(db.statements) == 3 + 3 * foreign_keys
    # ONE scan per text column for all 25 candidates, never one per file.
    assert len(rule.scans) == len(datasets_api._file_id_text_holders())
    assert all(asked == set(ids) for _sql_text, asked in rule.scans)
    assert [len(batch) for batch in rule.deleted] == [10, 10, 5]


def test_the_scans_stop_once_every_candidate_is_known_to_be_in_use(rule):
    held = uuid.uuid4()
    db = _HelperSession({held})
    first_table = _sql(datasets_api._file_id_text_holders()[0][0])
    rule.named = {"template_containers": {held}}

    assert _run_rule(db, [held]) == []
    assert "template_containers" in first_table
    assert len(rule.scans) == 1 and rule.deleted == []


def test_nothing_is_read_for_no_file(rule):
    db = _HelperSession(set())
    assert _run_rule(db, set()) == []
    assert db.statements == [] and rule.scans == []


# -- The reference inventory ---------------------------------------------------


def test_every_foreign_key_to_file_attachments_is_checked():
    """A new foreign key to ``file_attachments.id`` must be added to the check (or this fails)."""
    from app.core.database import Base

    referencing = {
        (fk.parent.table.name, fk.parent.name)
        for table in Base.metadata.tables.values()
        if table.name != "file_attachments"
        for fk in table.foreign_keys
        if fk.column.table.name == "file_attachments" and fk.column.name == "id"
    }
    checked = {(column.table.name, column.name) for column in datasets_api._file_attachment_foreign_keys()}
    assert checked == referencing == {("workset_items", "file_attachment_id"), ("runner_commands", "result_file_id")}


def test_the_text_holders_cover_every_column_that_names_files_by_id():
    from app.models.orchestration import TERMINAL_EXECUTION_STATES
    from app.services.assignment_engine import ACTIVE_RUN_STATUSES, QUEUED_RUN_STATUS, TERMINAL_RUN_STATUSES

    holders = {}
    for text, criteria in datasets_api._file_id_text_holders():
        rendered = _sql(text)
        (table,) = {m for m in re.findall(r"\b([a-z_]+)\.[a-z_]+\b", rendered)}
        holders[table] = (rendered, [_sql(c) for c in criteria])

    assert set(holders) == {
        "template_containers",
        "orchestration_executions",
        "assignment_runs",
        "dataset_entries",
        "messages",
        "flow_runs",
        "presentations",
        "presentation_versions",
    }
    # Only a user message carries a block naming a file it did not bind.
    assert holders["messages"][1] == ["messages.role = 'user'", "messages.metadata_json IS NOT NULL"]
    for column in ("theme_config", "layouts", "extra_metadata"):
        assert f"template_containers.{column}" in holders["template_containers"][0]
    # Only rows that will read their ids again: a finished run or execution never does.
    (runs,) = holders["assignment_runs"][1]
    live_runs = set(re.findall(r"'([a-z_]+)'", runs))
    assert live_runs == {*ACTIVE_RUN_STATUSES, QUEUED_RUN_STATUS}
    assert not live_runs & set(TERMINAL_RUN_STATUSES)
    (executions,) = holders["orchestration_executions"][1]
    assert set(re.findall(r"'([a-z_]+)'", executions)) == {"pending", "running"}
    assert not {"pending", "running"} & TERMINAL_EXECUTION_STATES
    for table in ("flow_runs", "presentations", "presentation_versions", "template_containers"):
        assert holders[table][1] == [], f"every {table} row counts"


def test_messages_are_read_from_the_oldest_file_asked_about_on():
    """No message written before a file existed can name it: an indexed bound, not the whole table."""
    oldest = datetime(2026, 9, 1, tzinfo=UTC)
    ((_text, criteria),) = [
        (text, criteria) for text, criteria in datasets_api._file_id_text_holders(oldest) if "messages" in _sql(text)
    ]
    (bound,) = [c for c in criteria if "created_at" in _sql(c)]
    assert bound.right.value == oldest - datasets_api._MESSAGE_CLOCK_MARGIN


def test_the_holders_are_asked_from_the_oldest_candidate(rule, monkeypatch):
    asked: list = []
    holders = datasets_api._file_id_text_holders

    def _holders(oldest=None):
        asked.append(oldest)
        return holders(oldest)

    monkeypatch.setattr(datasets_api, "_file_id_text_holders", _holders)
    ids = {uuid.uuid4(), uuid.uuid4()}
    _run_rule(_HelperSession(ids), ids)
    assert asked == [_UPLOADED_AT]


@pytest.mark.parametrize(
    ("model", "ondelete", "nullable"),
    [("EvaluationEpisode", "CASCADE", False), ("EvaluationResult", "SET NULL", True)],
)
def test_an_evaluation_cannot_record_an_entry_deleted_under_it(model, ondelete, nullable):
    """Why a running evaluation is no holder of a deleted entry's files.

    It snapshots every entry's ``file_ids`` when it starts, but for an entry
    deleted meanwhile it can store no episode -- that row is written before the
    entry's files are read, and goes with the entry. That is what keeps a
    running evaluation out of the inventory.

    The result outlives its entry with ``entry_id`` NULL
    (``_swap_entry_id_fk``), so a run's scores survive a dataset edit. Neither
    table is a reader -- neither has a column naming a file, the ids are read
    off the entry while the run executes -- so the inventory is unaffected.
    The day one of them grows one, this is where it shows.
    """
    from app.models import evaluation

    column = getattr(evaluation, model).__table__.c.entry_id
    (fk,) = column.foreign_keys
    assert (fk.column.table.name, fk.ondelete, column.nullable) == ("dataset_entries", ondelete, nullable)
    assert [c.name for c in getattr(evaluation, model).__table__.c if "file" in c.name.lower()] == []


# -- Admin gate ----------------------------------------------------------------


@pytest.mark.parametrize("route", ["delete_entry", "delete_dataset"])
def test_both_routes_stay_admin_only(route):
    signature = inspect.signature(getattr(datasets_api, route))
    assert signature.parameters["user"].default.dependency is deps.require_admin
    with pytest.raises(HTTPException) as exc:
        asyncio.run(deps.require_admin(user=SimpleNamespace(id=uuid.uuid4(), is_admin=False)))
    assert exc.value.status_code == 403
