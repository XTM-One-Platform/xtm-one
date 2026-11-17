"""Regression tests for the user_id scoping fix in cleanup_tool_test_files.

Before the fix, ``POST /custom-tools/{id}/test/cleanup`` looked up
``FileAttachment`` rows by ID only — no ``user_id`` check.  Any authenticated
user with access to any custom tool could pass arbitrary file IDs and delete
another user's uploads from object storage.

The fix adds ``FileAttachment.user_id == user.id`` to the WHERE clause so
only the requesting user's own files are ever touched.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.api.v1.helpers as api_helpers
import app.services.file_storage as file_storage_module
from app.api.v1 import custom_tools as custom_tools_api


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, file_row):
        self._file_row = file_row
        self.statements: list = []
        self.deleted: list = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _ScalarResult(self._file_row)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        pass


def _user():
    return SimpleNamespace(id=uuid.uuid4(), is_admin=False, role="user")


@pytest.fixture
def stub_get_manageable(monkeypatch):
    tool_id = str(uuid.uuid4())

    async def _get_manageable(_db, _model, _user, _id, **_kw):
        return SimpleNamespace(id=uuid.UUID(tool_id))

    # The package-origin guard runs its own DB query before the
    # file lookup; ``_FakeDB`` answers every query with the same stubbed
    # row, which would make the guard 403. Stub its lookup (read by
    # the shared gate in ``helpers``) so the fake DB only serves the file
    # lookup these tests pin.
    async def _not_package_origin(_db, _tool_id):
        return False

    monkeypatch.setattr(custom_tools_api, "get_manageable_entity", _get_manageable)
    monkeypatch.setattr(
        api_helpers, "is_package_origin_custom_tool", _not_package_origin
    )
    return tool_id


@pytest.mark.asyncio
async def test_foreign_file_not_deleted(stub_get_manageable):
    """A file owned by a different user must not be deleted.

    The DB returns None (file not found for this user) — the endpoint must
    report purged=0 and issue no delete.
    """
    user = _user()
    fake_db = _FakeDB(file_row=None)  # file not visible to this user

    result = await custom_tools_api.cleanup_tool_test_files(
        tool_id=stub_get_manageable,
        body={"file_ids": [str(uuid.uuid4())]},
        user=user,
        db=fake_db,
    )

    assert result["purged"] == 0
    assert fake_db.deleted == []


@pytest.mark.asyncio
async def test_own_file_deleted(stub_get_manageable, monkeypatch):
    """The requesting user's own file must be deleted from storage and the DB."""
    user = _user()
    file_id = str(uuid.uuid4())
    own_file = SimpleNamespace(
        id=uuid.UUID(file_id),
        user_id=user.id,
        s3_object_name="uploads/test/file.txt",
    )

    deleted_keys: list[str] = []

    class _FakeStorage:
        async def delete(self, key: str) -> None:
            deleted_keys.append(key)

    monkeypatch.setattr(file_storage_module, "file_storage", _FakeStorage())

    fake_db = _FakeDB(file_row=own_file)
    result = await custom_tools_api.cleanup_tool_test_files(
        tool_id=stub_get_manageable,
        body={"file_ids": [file_id]},
        user=user,
        db=fake_db,
    )

    assert result["purged"] == 1
    assert fake_db.deleted == [own_file]
    assert deleted_keys == ["uploads/test/file.txt"]


@pytest.mark.asyncio
async def test_package_origin_tool_cleanup_rejected(monkeypatch):
    """Package-origin tools are guarded: cleanup must 403 before touching files."""
    from fastapi import HTTPException

    tool_id = str(uuid.uuid4())

    async def _get_manageable(_db, _model, _user, _id, **_kw):
        return SimpleNamespace(id=uuid.UUID(tool_id))

    async def _is_package_origin(_db, _tool_id):
        return True

    monkeypatch.setattr(custom_tools_api, "get_manageable_entity", _get_manageable)
    monkeypatch.setattr(
        api_helpers, "is_package_origin_custom_tool", _is_package_origin
    )

    fake_db = _FakeDB(file_row=None)
    with pytest.raises(HTTPException) as exc_info:
        await custom_tools_api.cleanup_tool_test_files(
            tool_id=tool_id,
            body={"file_ids": [str(uuid.uuid4())]},
            user=_user(),
            db=fake_db,
        )

    assert exc_info.value.status_code == 403
    assert fake_db.deleted == []
