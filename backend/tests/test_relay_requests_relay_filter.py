"""The relay group request log filters by several relays at once (#10977, T2 slice D).

The Activity tab's Relay filter is a shared filter pill: each relay picked is one
repeated ``relay_id`` and the log keeps the requests of any of them.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.api.v1 import relays


class _Db:
    def __init__(self) -> None:
        self.statements: list = []

    async def scalar(self, statement):
        self.statements.append(statement)
        return 0

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))


@pytest.fixture
def group(monkeypatch):
    group = SimpleNamespace(id=uuid.uuid4())

    async def _visible(group_id, user, db):
        return group, None

    monkeypatch.setattr(relays, "_require_enabled", lambda: None)
    monkeypatch.setattr(relays, "_get_visible_group", _visible)
    return group


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


async def _list(db, relay_id):
    return await relays.list_relay_requests(
        str(uuid.uuid4()),
        limit=50,
        offset=0,
        relay_id=relay_id,
        outcome=None,
        host=None,
        source_type=None,
        direction="desc",
        user=SimpleNamespace(),
        db=db,
    )


async def test_several_relays_keep_the_requests_of_any_of_them(group):
    first, second = uuid.uuid4(), uuid.uuid4()
    db = _Db()
    assert await _list(db, [str(first), str(second)]) == {"items": [], "total": 0}
    for statement in db.statements:
        sql = _sql(statement).split("WHERE", 1)[1]
        assert "relay_request_logs.relay_id IN" in sql
        assert first.hex in sql.replace("-", "") and second.hex in sql.replace("-", "")


async def test_no_relay_reads_every_request_of_the_group(group):
    db = _Db()
    await _list(db, None)
    # The selected row names the column; the WHERE clause never does.
    assert all("relay_request_logs.relay_id" not in _sql(statement).split("WHERE", 1)[1] for statement in db.statements)


async def test_an_id_that_is_not_one_is_refused(group):
    with pytest.raises(HTTPException) as caught:
        await _list(_Db(), [str(uuid.uuid4()), "not-an-id"])
    assert caught.value.status_code == 404
