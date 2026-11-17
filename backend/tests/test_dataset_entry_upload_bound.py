"""``POST /datasets/entries/{entry_id}/upload`` reads an upload to its limit and one byte.

Before, the route read the whole upload and only then compared its length with
``MAX_UPLOAD_SIZE_BYTES``: an oversized upload was held whole before its refusal.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile

from app.api.v1 import datasets as datasets_api
from app.services import file_storage as file_storage_module


class _SpyUpload(UploadFile):
    """An upload recording the size each ``read`` asks for."""

    def __init__(self, data: bytes):
        super().__init__(file=io.BytesIO(data), filename="attachment.bin")
        self.asked: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.asked.append(size)
        return await super().read(size)


@pytest.fixture
def entry(monkeypatch):
    entry = SimpleNamespace(id=uuid.uuid4(), file_ids=[])
    dataset = SimpleNamespace(id=uuid.uuid4(), is_company_managed=False, is_read_only=False)

    async def _get(_db, _user, _entry_id):
        return entry, dataset

    monkeypatch.setattr(datasets_api, "_get_user_dataset_entry_with_dataset", _get)
    monkeypatch.setattr(datasets_api, "require_admin_for_company_managed", lambda *_a: None)
    monkeypatch.setattr(datasets_api, "_check_entry_access", lambda *_a: None)
    monkeypatch.setattr(file_storage_module, "MAX_UPLOAD_SIZE_BYTES", _LIMIT)
    return entry


_LIMIT = 2 * 1024 * 1024


@pytest.fixture
def versions(monkeypatch) -> list[object]:
    """The datasets an upload bumped the version of (``dataset_versioning.bump_version``)."""
    bumped: list[object] = []

    async def _bump(_db, dataset, **_kwargs):
        bumped.append(dataset)

    monkeypatch.setattr(datasets_api.dataset_versioning, "bump_version", _bump)
    return bumped


class _Session:
    def __init__(self):
        self.added: list[object] = []
        self.committed = False

    def add(self, row):
        row.id = uuid.uuid4()
        self.added.append(row)

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True


def _upload(entry, upload, db):
    return asyncio.run(
        datasets_api.upload_entry_file(
            entry_id=str(entry.id),
            file=upload,
            user=SimpleNamespace(id=uuid.uuid4()),
            db=db,
            _ai_gate=None,
        )
    )


def test_an_oversized_upload_is_read_to_the_limit_and_one_byte(entry, versions):
    upload = _SpyUpload(b"x" * (_LIMIT + 5000))
    db = _Session()
    with pytest.raises(HTTPException) as err:
        _upload(entry, upload, db)
    assert upload.asked == [_LIMIT + 1]
    assert err.value.status_code == 413
    assert err.value.detail == "File too large. Maximum is 2 MB."
    assert db.added == []
    assert entry.file_ids == []
    assert versions == []


def test_an_upload_at_the_limit_is_stored_whole(entry, versions, monkeypatch):
    stored: list[bytes] = []

    async def _store(*, data, **_kwargs):
        stored.append(data)
        return "dataset/key"

    monkeypatch.setattr(file_storage_module, "file_storage", SimpleNamespace(upload=_store))
    upload = _SpyUpload(b"x" * _LIMIT)
    db = _Session()
    response = _upload(entry, upload, db)
    assert upload.asked == [_LIMIT + 1]
    assert stored == [b"x" * _LIMIT]
    assert response["size"] == _LIMIT
    assert db.committed is True
    assert entry.file_ids == [response["file_id"]]
    # The entry's file changed: its dataset is a new version.
    assert len(versions) == 1
