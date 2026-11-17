"""Writes of a provider reference are serialized with provider deletions.

A deletion locks every provider config row, decides from what it reads and
applies it. The other writers of a provider reference (the General Assistant /
platform defaults save, agent model edits and imports, the environment seeds)
take the same lock before reading what they write, and refuse a new reference
to a config that does not exist.
The races themselves run against a real database in
``test_llm_provider_deletion_postgres.py``; this pins the parts that need none.
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError

from app.api.v1 import agents as agents_api
from app.api.v1 import settings as settings_api
from app.api.v1.schemas.agent import CreateAgentRequest, UpdateAgentRequest
from app.api.v1.schemas.settings import LLMProviderUpdateRequest
from app.services.llm_provider_deletion import (
    _PROVIDER_CONFIGS_LOCK_KEY,
    PROVIDER_LOCK_BUSY_DETAIL,
    ProviderLockBusyError,
    lock_provider_configs,
    provider_reference_problem,
)


class _Recorder:
    def __init__(self, found=None):
        self.statements: list[str] = []
        self.found = found

    async def execute(self, statement, *_args, **_kwargs):
        self.statements.append(
            str(statement.compile(dialect=postgresql.dialect()))
        )
        return SimpleNamespace(scalar_one_or_none=lambda: self.found)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shared", "advisory", "clause"),
    [
        (False, "pg_advisory_xact_lock(", "FOR NO KEY UPDATE"),
        (True, "pg_advisory_xact_lock_shared(", "FOR SHARE"),
    ],
)
async def test_the_lock_is_advisory_then_every_config_row_in_id_order(shared, advisory, clause):
    """The advisory lock covers a config inserted after the row scan; the rows
    are what a catalog edit's own row lock meets. Always in this order."""
    db = _Recorder()

    await lock_provider_configs(db, shared=shared)

    advisory_sql, rows_sql = db.statements
    assert f"{advisory}{_PROVIDER_CONFIGS_LOCK_KEY})" in advisory_sql
    assert "FROM llm_provider_configs" in rows_sql
    assert "ORDER BY llm_provider_configs.id" in rows_sql
    assert rows_sql.rstrip().endswith(clause)
    assert "WHERE" not in rows_sql


class _Cancelled:
    """A session whose every statement is cancelled with *sqlstate*."""

    def __init__(self, sqlstate: str):
        self.sqlstate = sqlstate

    async def execute(self, statement, *_args, **_kwargs):
        raise DBAPIError(str(statement), None, SimpleNamespace(sqlstate=self.sqlstate))


@pytest.mark.asyncio
@pytest.mark.parametrize("sqlstate", ["57014", "55P03"], ids=["statement-timeout", "lock-timeout"])
async def test_a_lock_wait_that_runs_out_is_refused_as_busy(sqlstate):
    with pytest.raises(ProviderLockBusyError) as refused:
        await lock_provider_configs(_Cancelled(sqlstate), shared=True)
    with pytest.raises(ProviderLockBusyError):
        await provider_reference_problem(
            _Cancelled(sqlstate), uuid.uuid4(), stored=None, label="x"
        )

    assert refused.value.status_code == 503
    assert refused.value.headers == {"Retry-After": "5"}
    assert refused.value.detail == PROVIDER_LOCK_BUSY_DETAIL


@pytest.mark.asyncio
async def test_any_other_database_error_is_left_alone():
    with pytest.raises(DBAPIError) as raised:
        await lock_provider_configs(_Cancelled("40P01"))
    assert not isinstance(raised.value, ProviderLockBusyError)


@pytest.mark.asyncio
async def test_a_route_waiting_past_the_timeout_answers_503():
    """No handler of its own: FastAPI answers the refusal like any HTTPException."""
    import httpx
    from fastapi import FastAPI

    app = FastAPI()

    @app.put("/save")
    async def save():
        await lock_provider_configs(_Cancelled("57014"), shared=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put("/save")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json() == {"detail": PROVIDER_LOCK_BUSY_DETAIL}


@pytest.mark.asyncio
async def test_a_reference_the_write_does_not_change_is_not_checked():
    db = _Recorder()
    stored = uuid.uuid4()

    assert await provider_reference_problem(db, stored, stored=stored, label="x") is None
    assert await provider_reference_problem(db, None, stored=stored, label="x") is None
    assert db.statements == []


@pytest.mark.asyncio
async def test_a_new_reference_to_a_missing_config_is_named():
    db = _Recorder(found=None)
    missing = await provider_reference_problem(
        db, uuid.uuid4(), stored=None, label="the image model"
    )
    # The row found stays locked until the write commits, even one created
    # after the lock scan.
    [sql] = db.statements
    assert sql.rstrip().endswith("FOR SHARE")
    present = await provider_reference_problem(
        _Recorder(found=uuid.uuid4()), uuid.uuid4(), stored=None, label="the image model"
    )

    assert missing is not None
    assert "the image model" in missing
    assert "no longer exists" in missing
    assert present is None


class _StopError(Exception):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "locks"),
    [
        (UpdateAgentRequest(llm_model="gpt-4o"), True),
        (UpdateAgentRequest(llm_provider_id=str(uuid.uuid4())), True),
        (UpdateAgentRequest(llm_provider="openai"), True),
        (UpdateAgentRequest(name="Renamed"), False),
    ],
    ids=["model", "provider-id", "provider-name", "no-model-field"],
)
async def test_an_agent_save_choosing_a_model_locks_before_it_reads_the_agent(
    monkeypatch, body, locks
):
    db = _Recorder()
    order: list[str] = []

    async def _group_ids(_db, _user_id):
        order.append("read")
        return []

    async def _get_agent(*_args, **_kwargs):
        raise _StopError

    monkeypatch.setattr(agents_api, "get_user_group_ids", _group_ids)
    monkeypatch.setattr(agents_api, "get_user_agent", _get_agent)
    original_execute = db.execute

    async def _execute(statement, *args, **kwargs):
        order.append("lock")
        return await original_execute(statement, *args, **kwargs)

    db.execute = _execute

    with pytest.raises(_StopError):
        await agents_api.update_agent(
            agent_id=str(uuid.uuid4()),
            body=body,
            request=None,
            user=SimpleNamespace(id=uuid.uuid4(), is_admin=False),
            db=db,
            _ai_gate=None,
        )

    if locks:
        assert order == ["lock", "lock", "read"]
        assert "pg_advisory_xact_lock_shared(" in db.statements[0]
        assert db.statements[1].rstrip().endswith("FOR SHARE")
    else:
        assert order == ["read"]


class _LockThenStop(_Recorder):
    """Records the provider config lock and stops the write at its first other statement."""

    async def execute(self, statement, *args, **kwargs):
        sql = str(statement.compile(dialect=postgresql.dialect())).rstrip()
        if "pg_advisory_xact_lock" not in sql and not sql.endswith(("FOR NO KEY UPDATE", "FOR SHARE")):
            raise _StopError(sql)
        return await super().execute(statement, *args, **kwargs)

    def add(self, _obj):
        raise _StopError("add")


class _Session:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_exc):
        return False


def _assert_locked(db: _LockThenStop, *, shared: bool) -> None:
    advisory, rows = db.statements
    assert ("pg_advisory_xact_lock_shared(" if shared else "pg_advisory_xact_lock(") in advisory
    assert rows.rstrip().endswith("FOR SHARE" if shared else "FOR NO KEY UPDATE")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fields", "locks"),
    [
        ({"llm_model": "gpt-4o"}, True),
        ({"llm_provider": "openai"}, True),
        ({"llm_provider_id": str(uuid.uuid4())}, True),
        ({}, False),
    ],
    ids=["model", "provider-name", "provider-id", "no-model-field"],
)
async def test_an_agent_creation_choosing_a_model_locks_before_it_reads_anything(fields, locks):
    db = _LockThenStop()

    with pytest.raises(_StopError):
        await agents_api.create_agent(
            body=CreateAgentRequest(name="Analyst", **fields),
            request=None,
            user=SimpleNamespace(id=uuid.uuid4(), is_admin=False),
            db=db,
            _agentic_gate=None,
        )

    if locks:
        _assert_locked(db, shared=True)
    else:
        assert db.statements == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_fields", "locks"),
    [
        ({"llm_provider": "openai", "llm_model": "gpt-4o"}, True),
        ({"llm_model": "gpt-4o"}, True),
        # An agent exported with a provider and no model: the import stores the
        # name, which a deletion resets by name.
        ({"llm_provider": "openai"}, True),
        ({"llm_provider": ""}, False),
        # Never imported: a config id of the exporting instance.
        ({"llm_provider_id": str(uuid.uuid4())}, False),
        ({}, False),
    ],
    ids=["model-and-provider", "model", "provider-only", "blank-provider", "provider-id", "no-model"],
)
async def test_an_import_choosing_a_model_locks_before_it_writes_anything(model_fields, locks):
    from app.api.v1.agent_import import create_agent_from_bundle

    db = _LockThenStop()
    bundle = {"version": 3, "export_type": "agent", "agent": {"name": "Imported", **model_fields}}

    with pytest.raises(_StopError):
        await create_agent_from_bundle(db, user=SimpleNamespace(id=uuid.uuid4()), bundle=bundle)

    if locks:
        _assert_locked(db, shared=True)
    else:
        assert db.statements == []


@pytest.mark.asyncio
async def test_the_env_provider_seed_locks_before_it_reads_a_config(monkeypatch):
    """Switching AI on at runtime runs the env seed next to an admin's delete:
    it edits catalogs and pins defaults to configs, so it takes the lock of a
    catalog edit before it reads anything."""
    import dotenv

    from app import main as app_main
    from app.core import database

    db = _LockThenStop()
    monkeypatch.setattr(database, "async_session", lambda: _Session(db))
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    for key in [key for key in os.environ if key.startswith("LLM_PROVIDER_")]:
        monkeypatch.delenv(key)
    monkeypatch.setattr(app_main.settings, "default_model", "gpt-4o")

    with pytest.raises(_StopError):
        await app_main._seed_llm_providers_from_env()

    _assert_locked(db, shared=False)


@pytest.mark.asyncio
async def test_the_builtin_embedding_seed_locks_before_it_reads_a_config(monkeypatch):
    from app import main as app_main
    from app.core import database

    db = _LockThenStop()
    monkeypatch.setattr(database, "async_session", lambda: _Session(db))
    monkeypatch.setattr(app_main.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(app_main.settings, "builtin_embedding_server_base_url", "http://tei.test")
    monkeypatch.setattr(app_main.settings, "builtin_embedding_server_model", "bge-m3")

    with pytest.raises(_StopError):
        await app_main._seed_xtm_one_embedding_server_from_env()

    _assert_locked(db, shared=False)


@pytest.mark.asyncio
async def test_a_missing_agent_provider_is_a_422():
    """``llm_provider_id`` is a body field, not the id of the row the route acts
    on: a malformed one keeps ``parse_uuid``'s 422, never ``parse_path_id``'s
    404."""
    admin = SimpleNamespace(id=uuid.uuid4(), is_admin=True)
    with pytest.raises(HTTPException) as exc:
        await agents_api._refuse_missing_agent_provider(
            _Recorder(found=None), str(uuid.uuid4()), stored=None, user=admin
        )
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as malformed:
        await agents_api._refuse_missing_agent_provider(
            _Recorder(), "not-a-uuid", stored=None, user=admin
        )
    assert malformed.value.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["update_llm_provider", "update_agent", "duplicate_agent"])
async def test_a_malformed_path_id_is_a_404_before_any_wait_for_the_lock(monkeypatch, route):
    """These routes took no lock before: a malformed id, which writes nothing,
    is still answered at once with the route's 404, never after a
    deletion in flight or with a 503 once the wait outlasts the statement
    timeout."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "models_env_providing_only", False)
    db = _Recorder()
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True)
    calls = {
        "update_llm_provider": lambda: settings_api.update_llm_provider(
            provider_id="not-a-uuid",
            body=LLMProviderUpdateRequest(available_models=["gpt-4o"]),
            request=None,
            user=user,
            db=db,
            _ai_gate=None,
        ),
        "update_agent": lambda: agents_api.update_agent(
            agent_id="not-a-uuid",
            body=UpdateAgentRequest(llm_model="gpt-4o"),
            request=None,
            user=user,
            db=db,
            _ai_gate=None,
        ),
        "duplicate_agent": lambda: agents_api.duplicate_agent(
            agent_id="not-a-uuid", request=None, user=user, db=db, _agentic_gate=None
        ),
    }

    with pytest.raises(HTTPException) as refused:
        await calls[route]()

    assert refused.value.status_code == 404
    assert db.statements == []


@pytest.mark.parametrize(
    ("stored", "completes"),
    [
        ({}, False),
        ({"agent_default_provider": "openai", "agent_default_provider_id": uuid.uuid4()}, False),
        ({"agent_default_provider": "openai"}, True),
        ({"image_provider_id": uuid.uuid4()}, True),
    ],
    ids=["nothing-stored", "complete-pair", "name-only", "id-only"],
)
def test_a_general_assistant_save_completing_a_stored_pair_writes_a_reference(stored, completes):
    """A save touching no model field still completes a pair stored with one
    side only, so it takes the lock too."""
    fields = {attr: None for pair in settings_api._GA_ALIGNED_PROVIDER_PAIRS for attr in pair}
    fields.update(stored)

    assert settings_api._completes_a_provider_pair(SimpleNamespace(**fields)) is completes
    assert settings_api._completes_a_provider_pair(None) is False
