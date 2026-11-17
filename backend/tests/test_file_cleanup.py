"""The deferred half of the file cascade: rows now, objects after the commit.

``delete_file_attachments`` deletes a row's object before its caller commits,
which is what an endpoint deleting one entity wants. The built-in template seed
removes hundreds of rows at boot, under the bootstrap lock: it deletes them with
``delete_file_attachment_rows`` and hands the keys it returns to
``purge_storage_objects`` once it committed - which gives up at the first sign
of an unreachable object store rather than waiting out every timeout in turn.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Delete

from app.services import file_cleanup


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


class _Session:
    def __init__(self, keys):
        self.keys = list(keys)
        self.statements: list = []

    async def execute(self, statement, *a, **k):
        self.statements.append(statement)
        keys = self.keys
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(keys)))


def test_rows_are_deleted_in_one_statement_that_returns_their_object_keys():
    container = uuid.uuid4()
    session = _Session(["template-asset/a/seed/1-logo.svg", None, "template-asset/a/seed/2-team.png"])

    keys = asyncio.run(file_cleanup.delete_file_attachment_rows(session, template_container_id=container))

    assert keys == ["template-asset/a/seed/1-logo.svg", "template-asset/a/seed/2-team.png"]
    (statement,) = session.statements
    assert isinstance(statement, Delete)
    sql = _sql(statement)
    assert sql.startswith("DELETE FROM file_attachments")
    assert str(container) in sql
    assert "RETURNING file_attachments.s3_object_name" in sql


def test_a_filter_that_can_match_nothing_deletes_nothing():
    """No filter, or an empty collection, never widens to the whole table."""
    session = _Session(["never"])
    assert asyncio.run(file_cleanup.delete_file_attachment_rows(session)) == []
    assert asyncio.run(file_cleanup.delete_file_attachment_rows(session, id=[])) == []
    assert session.statements == []


def _storage(monkeypatch, *, unreachable_at: str | None = None) -> list[str]:
    """``file_storage.delete`` swallows an S3 error answer (a missing object, a
    refusal) and lets anything else through: a store that does not answer."""
    attempted: list[str] = []

    async def _delete(key):
        attempted.append(key)
        if key == unreachable_at:
            raise TimeoutError("connection to the object store timed out")

    monkeypatch.setattr("app.services.file_storage.file_storage.delete", _delete, raising=False)
    return attempted


def test_the_object_purge_removes_every_key_it_is_handed(monkeypatch):
    attempted = _storage(monkeypatch)
    assert asyncio.run(file_cleanup.purge_storage_objects(["a", "b", "c"])) == 3
    assert attempted == ["a", "b", "c"]


def test_the_object_purge_stops_at_the_first_unreachable_store_error(monkeypatch, caplog):
    """Each delete against a store that does not answer waits out its own
    connect timeout and retries; a boot purging a whole built-in would wait that
    long per object, holding the bootstrap lock. The first failure ends the
    purge: what is left is unreferenced objects, named in the log - never a
    stalled start."""
    attempted = _storage(monkeypatch, unreachable_at="b")
    with caplog.at_level(logging.WARNING, logger=file_cleanup.__name__):
        removed = asyncio.run(file_cleanup.purge_storage_objects(["a", "b", "c", "d"]))

    assert removed == 1
    assert attempted == ["a", "b"], "nothing is attempted after the store stopped answering"
    assert "3 object(s) left in the bucket" in caplog.text
    assert "b" in caplog.text
