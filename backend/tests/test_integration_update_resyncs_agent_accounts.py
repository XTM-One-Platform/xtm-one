"""An integration update schedules a sync of the agent accounts acting with it (#9903).

An agent account otherwise resyncs only on its own change or every hour: a
fixed integration kept showing the old error, a broken one the old status.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.services.agent_accounts import listening


class _Rows:
    def __init__(self, ids: list[uuid.UUID]) -> None:
        self._ids = ids

    def scalars(self) -> SimpleNamespace:
        return SimpleNamespace(all=lambda: list(self._ids))


class _Db:
    def __init__(self, ids: list[uuid.UUID], fail: bool = False) -> None:
        self.ids = ids
        self.fail = fail
        self.statements: list[object] = []

    async def __aenter__(self) -> _Db:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, statement: object) -> _Rows:
        if self.fail:
            raise RuntimeError("database unavailable")
        self.statements.append(statement)
        return _Rows(self.ids)


async def test_every_enabled_account_of_the_integration_is_scheduled(monkeypatch) -> None:
    ids = [uuid.uuid4(), uuid.uuid4()]
    db = _Db(ids)
    scheduled: list[str] = []
    monkeypatch.setattr(listening, "async_session", lambda: db)
    monkeypatch.setattr(listening, "schedule_sync", lambda account_id, **_kw: scheduled.append(account_id))

    integration_id = uuid.uuid4()
    await listening.sync_integration_accounts(str(integration_id))

    assert scheduled == [str(i) for i in ids]
    # The query filters on the integration and on enabled accounts only.
    sql = str(db.statements[0])
    assert "agent_accounts.integration_id" in sql
    assert "agent_accounts.enabled" in sql


async def test_an_integration_without_accounts_schedules_nothing(monkeypatch) -> None:
    scheduled: list[str] = []
    monkeypatch.setattr(listening, "async_session", lambda: _Db([]))
    monkeypatch.setattr(listening, "schedule_sync", lambda account_id, **_kw: scheduled.append(account_id))

    await listening.sync_integration_accounts(str(uuid.uuid4()))

    assert scheduled == []


async def test_a_failed_listing_never_raises(monkeypatch) -> None:
    scheduled: list[str] = []
    monkeypatch.setattr(listening, "async_session", lambda: _Db([], fail=True))
    monkeypatch.setattr(listening, "schedule_sync", lambda account_id, **_kw: scheduled.append(account_id))

    # The edit is already committed: a sync that cannot be scheduled is logged, never raised.
    await listening.sync_integration_accounts(str(uuid.uuid4()))

    assert scheduled == []
