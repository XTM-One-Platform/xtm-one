"""``DELETE /datasets/entries/{entry_id}/files/{file_id}`` acts only on the entry's own files.

Before, the route dropped the id from ``entry.file_ids`` and then deleted the
``FileAttachment`` row and its stored object for WHATEVER id it was given -
another user's conversation attachment, a run output, a template asset - even
one the entry never listed. It now changes nothing for an id the entry does not
list, and answers it as before, like a removal already done (204: a stale tab or
a retried request keeps succeeding). For a listed id it drops the reference and
hands the file to the shared rule (``_delete_dataset_files_if_unreferenced``),
which deletes it only when nothing else uses it. The rule itself is pinned in
``test_dataset_delete_file_scope.py`` and, on a real database, in
``test_dataset_file_references_postgres.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import datasets as datasets_api
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



class _Session:
    """Records what the route does, in order; it must issue no query of its own."""

    def __init__(self, *, fail_commit: bool = False):
        self.log: list[str] = []
        self.fail_commit = fail_commit

    async def execute(self, _statement):
        raise AssertionError("the route itself must not query")

    async def flush(self):
        self.log.append("flush")

    async def commit(self):
        if self.fail_commit:
            raise RuntimeError("commit failed")
        self.log.append("commit")


def _admin():
    return SimpleNamespace(id=uuid.uuid4(), role="admin", is_admin=True)


def _dataset():
    return SimpleNamespace(id=uuid.uuid4(), is_company_managed=False, is_read_only=False, user_id=uuid.uuid4())


@pytest.fixture
def harness(monkeypatch):
    entry = SimpleNamespace(id=uuid.uuid4(), file_ids=[])
    dataset = _dataset()
    released: list[SimpleNamespace] = []
    purged: list[list[str]] = []

    async def _load(_db, _user, _entry_id):
        return entry, dataset

    async def _release(db, file_uuids):
        released.append(
            SimpleNamespace(file_uuids=set(file_uuids), entry_file_ids=list(entry.file_ids), log=list(db.log))
        )
        db.log.append("release")
        return [f"dataset/{entry.id}/{file_uuid.hex}.pdf" for file_uuid in sorted(file_uuids)]

    async def _purge(keys):
        purged.append(list(keys))
        return len(keys)

    monkeypatch.setattr(datasets_api, "_get_user_dataset_entry_with_dataset", _load)
    monkeypatch.setattr(datasets_api, "_delete_dataset_files_if_unreferenced", _release)
    monkeypatch.setattr("app.services.file_cleanup.purge_storage_objects", _purge)
    return SimpleNamespace(entry=entry, dataset=dataset, released=released, purged=purged)


def _remove(entry, file_id, db, user=None):
    return asyncio.run(datasets_api.remove_entry_file(str(entry.id), file_id, user=user or _admin(), db=db))


def _http_status(entry, file_id, db) -> int:
    app = FastAPI()
    app.include_router(datasets_api.router, prefix="/api/v1/datasets")
    admin = _admin()
    app.dependency_overrides[deps.require_admin] = lambda: admin
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        return client.delete(f"/api/v1/datasets/entries/{entry.id}/files/{file_id}").status_code


def test_the_route_stays_admin_only():
    signature = inspect.signature(datasets_api.remove_entry_file)
    assert signature.parameters["user"].default.dependency is deps.require_admin
    with pytest.raises(HTTPException) as exc:
        asyncio.run(deps.require_admin(user=SimpleNamespace(id=uuid.uuid4(), is_admin=False)))
    assert exc.value.status_code == 403


def test_a_listed_file_is_dropped_then_handed_to_the_shared_rule(harness):
    mine = uuid.uuid4()
    other = str(uuid.uuid4())
    harness.entry.file_ids = [str(mine), other]
    db = _Session()

    assert _remove(harness.entry, str(mine), db) is None

    assert harness.entry.file_ids == [other]
    (call,) = harness.released
    assert call.file_uuids == {mine}
    # The entry lets go first (flushed), then the file rows are locked: the
    # same order as deleting the entry, so the two cannot deadlock.
    assert call.entry_file_ids == [other] and call.log == ["flush"]
    # The stored objects go only once the transaction committed.
    assert db.log == ["flush", "release", "commit"]
    assert harness.purged == [[f"dataset/{harness.entry.id}/{mine.hex}.pdf"]]


def test_a_listed_id_matches_whatever_its_spelling(harness):
    mine = uuid.uuid4()
    odd = mine.hex.upper()
    harness.entry.file_ids = [str(mine).upper(), f"{{{odd[:5]}-{odd[5:19]}-{odd[19:]}}}", str(uuid.uuid4())]
    _remove(harness.entry, mine.hex, _Session())
    assert len(harness.entry.file_ids) == 1, "every spelling of the id goes, the other file stays"
    assert harness.released[0].file_uuids == {mine}


@pytest.mark.parametrize(
    "asked",
    [
        # Unknown, already removed, or a real file attached elsewhere: the
        # route cannot tell them apart and reads none of them.
        pytest.param(lambda: str(uuid.uuid4()), id="not-listed"),
        pytest.param(lambda: "not-a-uuid", id="malformed"),
    ],
)
def test_an_id_the_entry_does_not_list_changes_nothing(harness, asked):
    attached = str(uuid.uuid4())
    harness.entry.file_ids = [attached]
    db = _Session()  # nothing may be read, let alone deleted

    assert _remove(harness.entry, asked(), db) is None

    assert harness.entry.file_ids == [attached]
    assert harness.released == [] and harness.purged == []
    assert db.log == []


def test_every_removal_is_answered_204_like_before(harness):
    """What an open tab or an API client reads back, a listed id or not."""
    listed = uuid.uuid4()
    harness.entry.file_ids = [str(listed)]

    assert _http_status(harness.entry, str(uuid.uuid4()), _Session()) == 204
    assert _http_status(harness.entry, "not-a-uuid", _Session()) == 204
    assert harness.released == []
    assert _http_status(harness.entry, str(listed), _Session()) == 204
    assert harness.entry.file_ids == []
    # Removed already (a stale tab clicking again, a retried request).
    assert _http_status(harness.entry, str(listed), _Session()) == 204
    assert [call.file_uuids for call in harness.released] == [{listed}]


def test_a_file_the_rule_keeps_only_loses_the_reference(harness, monkeypatch):
    shared = uuid.uuid4()
    harness.entry.file_ids = [str(shared)]

    async def _keep(db, file_uuids):
        db.log.append("release")
        return []

    monkeypatch.setattr(datasets_api, "_delete_dataset_files_if_unreferenced", _keep)
    db = _Session()
    _remove(harness.entry, str(shared), db)

    assert harness.entry.file_ids == []
    assert db.log == ["flush", "release", "commit"]
    assert harness.purged == [[]]


def test_nothing_is_purged_when_the_transaction_does_not_commit(harness):
    mine = uuid.uuid4()
    harness.entry.file_ids = [str(mine)]
    with pytest.raises(RuntimeError, match="commit failed"):
        _remove(harness.entry, str(mine), _Session(fail_commit=True))
    # The rows come back with the rollback: their bytes must still be there.
    assert harness.purged == []
