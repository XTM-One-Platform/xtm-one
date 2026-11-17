"""A registration stores the procedure manifest of an OpenCRQ and asks for it when it holds none.

``POST /platform/register`` is also the heartbeat. An OpenCRQ reports ``procedure_manifest_fingerprint``
on every one; XTM One answers ``procedure_manifest_wanted`` when it holds no manifest under that
fingerprint and the request carries none, and the platform registers once more with ``procedure_manifest``.
Nothing about it may refuse a heartbeat, and under the published default registration token nothing is
stored and nothing is asked for. These tests drive the real route with a session double: the SQL is
pinned by the migration integrity suite and by ``test_platform_registration_lock_postgres.py``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.elements import TextClause

from app.api.deps import get_db
from app.api.v1 import helpers as helpers_api
from app.api.v1 import platform as platform_api
from app.core import platform_identity
from app.core.config import settings
from app.core.registration_token import PUBLISHED_DEFAULT_REGISTRATION_TOKEN
from app.integrations.providers.opencrq import procedure_manifest as pm
from app.models.integration import Integration
from app.models.platform_procedure_manifest import PlatformProcedureManifest
from app.models.platform_registration import PlatformRegistration
from app.services import registered_platform_origins

TOKEN = "test-registration-token"
URL = "https://crq.example.com"
FINGERPRINT = "ab" * 32
OTHER_FINGERPRINT = "cd" * 32


def _manifest(path: str = "groups.createGroup", **extra) -> dict:
    """What OpenCRQ sends: the procedures with their inputs, value constraints included."""
    return {
        "formatVersion": 1,
        "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
        "procedures": [
            {
                "path": path,
                "type": "mutation",
                "input": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "minLength": 1, "pattern": "^a"}},
                    "required": ["name"],
                },
            },
            {"path": "groups.listGroups", "type": "query", "input": None},
        ],
        **extra,
    }


class _Rows:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _Savepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _DB:
    """The registration and the manifest row the route reads; every write is kept."""

    def __init__(self, registration: PlatformRegistration | None, stored: PlatformProcedureManifest | None = None):
        self.registration = registration
        self.stored = stored
        self.manifest_reads = 0
        self.full_manifest_reads = 0
        self.savepoints = 0
        self.fail_manifest_write = False

    async def execute(self, statement, *_args, **_kwargs):
        if isinstance(statement, TextClause):
            return _Rows(None)
        entity = statement.column_descriptions[0]["entity"]
        if entity is PlatformRegistration:
            return _Rows(self.registration)
        if entity is Integration:
            return _Rows(None)
        if entity is PlatformProcedureManifest:
            self.manifest_reads += 1
            if statement.column_descriptions[0]["name"] == "fingerprint":
                return _Rows(self.stored.fingerprint if self.stored is not None else None)
            self.full_manifest_reads += 1
            return _Rows(self.stored)
        raise AssertionError(f"register_platform does not query {entity}")

    def begin_nested(self):
        self.savepoints += 1
        return _Savepoint()

    def add(self, obj: object) -> None:
        if isinstance(obj, PlatformProcedureManifest):
            self.stored = obj
        elif isinstance(obj, PlatformRegistration):
            self.registration = obj

    async def flush(self) -> None:
        if self.registration is not None and self.registration.id is None:
            self.registration.id = uuid.uuid4()
        if self.fail_manifest_write and self.stored is not None and self.stored.received_at is None:
            raise RuntimeError("manifest table missing")

    async def commit(self) -> None:
        return None

    async def refresh(self, _obj: object) -> None:
        return None


def _registration(fingerprint: str | None = None) -> PlatformRegistration:
    return PlatformRegistration(
        id=uuid.uuid4(),
        platform_identifier="opencrq",
        platform_url=URL,
        platform_title="OpenCRQ",
        platform_version="",
        contract="CE",
        license_validated=False,
        status="active",
        procedure_manifest_fingerprint=fingerprint,
    )


def _held(registration: PlatformRegistration, fingerprint: str = FINGERPRINT) -> PlatformProcedureManifest:
    return PlatformProcedureManifest(
        registration_id=registration.id,
        fingerprint=fingerprint,
        procedures={"groups.listGroups": {"type": "query", "input": None}},
        received_at=datetime.now(UTC),
    )


@pytest.fixture
def send(monkeypatch):
    """``POST /api/v1/platform/register`` through the route; returns (response, db)."""

    async def _nothing(*_args, **_kwargs) -> None:
        return None

    async def _load_ps(_db) -> SimpleNamespace:
        return SimpleNamespace(enterprise_license=None, created_at=datetime.now(UTC))

    monkeypatch.setattr(settings, "enterprise_license", "")
    monkeypatch.setattr(platform_api, "enforce_rate_limit", _nothing)
    monkeypatch.setattr(platform_api, "refund_rate_limit", _nothing)
    monkeypatch.setattr(platform_api, "_load_ps", _load_ps)
    monkeypatch.setattr(platform_api, "_sync_platform_intents", _nothing)
    monkeypatch.setattr(helpers_api, "resolve_company_managed_owner_id", _nothing)
    # It reads the registrations to frame-ancestors; no database here.
    monkeypatch.setattr(registered_platform_origins, "refresh_for_registration", _nothing)

    def _send(db: _DB, token: str = TOKEN, **fields: object):
        monkeypatch.setattr(settings, "platform_registration_token", token)
        app = FastAPI()
        app.include_router(platform_api.router, prefix="/api/v1/platform")
        app.dependency_overrides[get_db] = lambda: db
        return TestClient(app).post(
            "/api/v1/platform/register",
            headers={"Authorization": f"Bearer {token}"},
            json={"platform_identifier": "opencrq", "platform_url": URL, **fields},
        )

    return _send


# --- the fingerprint ------------------------------------------------------------------------


def test_the_fingerprint_is_kept_and_a_manifest_is_wanted_when_none_is_held(send):
    db = _DB(_registration())
    response = send(db, procedure_manifest_fingerprint=FINGERPRINT)
    assert response.status_code == 200
    assert response.json()["procedure_manifest_wanted"] is True
    assert db.registration.procedure_manifest_fingerprint == FINGERPRINT
    assert db.stored is None


def test_a_first_registration_with_a_fingerprint_wants_the_manifest(send):
    db = _DB(None)
    response = send(db, procedure_manifest_fingerprint=FINGERPRINT)
    assert response.status_code == 200
    assert response.json()["procedure_manifest_wanted"] is True
    assert db.registration.procedure_manifest_fingerprint == FINGERPRINT


def test_a_manifest_held_under_the_fingerprint_is_not_wanted_again(send):
    registration = _registration(FINGERPRINT)
    db = _DB(registration, _held(registration))
    response = send(db, procedure_manifest_fingerprint=FINGERPRINT)
    assert response.json()["procedure_manifest_wanted"] is False
    assert db.stored.fingerprint == FINGERPRINT


def test_a_heartbeat_reads_the_fingerprint_alone_and_opens_no_savepoint(send):
    registration = _registration(FINGERPRINT)
    db = _DB(registration, _held(registration))
    send(db, procedure_manifest_fingerprint=FINGERPRINT)
    assert db.manifest_reads == 1
    assert db.full_manifest_reads == 0
    assert db.savepoints == 0


def test_a_manifest_that_is_sent_is_read_whole_in_a_savepoint(send):
    db = _DB(_registration())
    send(db, procedure_manifest_fingerprint=FINGERPRINT, procedure_manifest=_manifest())
    assert db.full_manifest_reads == 1
    assert db.savepoints == 1


def test_a_changed_fingerprint_wants_the_manifest_again_and_keeps_the_old_row_until_it_comes(send):
    registration = _registration(FINGERPRINT)
    db = _DB(registration, _held(registration))
    response = send(db, procedure_manifest_fingerprint=OTHER_FINGERPRINT)
    assert response.json()["procedure_manifest_wanted"] is True
    assert registration.procedure_manifest_fingerprint == OTHER_FINGERPRINT
    assert db.stored.fingerprint == FINGERPRINT


# --- the manifest ---------------------------------------------------------------------------


def test_the_follow_up_stores_the_reduced_manifest_and_asks_for_nothing_more(send):
    db = _DB(_registration())
    response = send(db, procedure_manifest_fingerprint=FINGERPRINT, procedure_manifest=_manifest())
    assert response.status_code == 200
    assert response.json()["procedure_manifest_wanted"] is False
    assert db.stored.registration_id == db.registration.id
    assert db.stored.fingerprint == FINGERPRINT
    assert db.stored.procedures == {
        "groups.createGroup": {
            "type": "mutation",
            "input": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
        "groups.listGroups": {"type": "query", "input": None},
    }
    assert db.registration.procedure_manifest_fingerprint == FINGERPRINT


def test_a_new_manifest_replaces_the_one_held(send):
    registration = _registration(FINGERPRINT)
    held = _held(registration)
    db = _DB(registration, held)
    response = send(
        db, procedure_manifest_fingerprint=OTHER_FINGERPRINT, procedure_manifest=_manifest("groups.makeGroup")
    )
    assert response.json()["procedure_manifest_wanted"] is False
    assert db.stored is held
    assert held.fingerprint == OTHER_FINGERPRINT
    assert set(held.procedures) == {"groups.makeGroup", "groups.listGroups"}


def test_a_manifest_for_a_fingerprint_already_held_is_left_alone(send):
    registration = _registration(FINGERPRINT)
    held = _held(registration)
    db = _DB(registration, held)
    send(db, procedure_manifest_fingerprint=FINGERPRINT, procedure_manifest=_manifest())
    assert set(held.procedures) == {"groups.listGroups"}


def test_a_manifest_carrying_another_fingerprint_than_the_reported_one_is_refused(send, caplog):
    db = _DB(_registration())
    with caplog.at_level(logging.WARNING, logger=platform_api.logger.name):
        response = send(
            db, procedure_manifest_fingerprint=FINGERPRINT, procedure_manifest=_manifest(fingerprint=OTHER_FINGERPRINT)
        )
    assert response.status_code == 200
    assert response.json()["procedure_manifest_wanted"] is False
    assert db.stored is None
    assert "ignored" in caplog.text


def test_a_manifest_carrying_the_reported_fingerprint_is_kept(send):
    db = _DB(_registration())
    send(db, procedure_manifest_fingerprint=FINGERPRINT, procedure_manifest=_manifest(fingerprint=FINGERPRINT))
    assert db.stored is not None


# --- what never refuses a heartbeat ---------------------------------------------------------


@pytest.mark.parametrize("fingerprint", ["", "XY", "AB" * 32, "ab" * 31, 12, ["ab" * 32], {"a": 1}, True])
def test_a_fingerprint_that_does_not_read_is_ignored_without_a_422(send, fingerprint, caplog):
    registration = _registration(OTHER_FINGERPRINT)
    db = _DB(registration)
    with caplog.at_level(logging.WARNING, logger=platform_api.logger.name):
        response = send(db, procedure_manifest_fingerprint=fingerprint, procedure_manifest=_manifest())
    assert response.status_code == 200
    assert response.json()["procedure_manifest_wanted"] is False
    assert registration.procedure_manifest_fingerprint is None
    assert db.stored is None
    assert db.manifest_reads == 0
    assert "ignored" in caplog.text


@pytest.mark.parametrize(
    "manifest",
    [
        "text",
        [],
        12,
        {},
        {"formatVersion": 2, "procedures": [{"path": "a.b", "type": "query"}]},
        {"formatVersion": 1, "procedures": []},
        {"formatVersion": 1, "procedures": [{"path": "a.b", "type": "subscription"}]},
        {"formatVersion": 1, "procedures": [{"path": "a.b", "type": "query", "input": "string"}]},
    ],
)
def test_a_manifest_that_does_not_read_is_ignored_without_a_422(send, manifest):
    db = _DB(_registration())
    response = send(db, procedure_manifest_fingerprint=FINGERPRINT, procedure_manifest=manifest)
    assert response.status_code == 200
    assert response.json()["procedure_manifest_wanted"] is False
    assert db.stored is None
    assert db.registration.procedure_manifest_fingerprint == FINGERPRINT


def test_a_manifest_over_the_size_cap_is_ignored(send, monkeypatch):
    monkeypatch.setattr(pm, "MAX_MANIFEST_BYTES", 100)
    db = _DB(_registration())
    response = send(db, procedure_manifest_fingerprint=FINGERPRINT, procedure_manifest=_manifest())
    assert response.status_code == 200
    assert db.stored is None


def test_a_manifest_without_a_fingerprint_is_ignored(send):
    db = _DB(_registration())
    response = send(db, procedure_manifest=_manifest())
    assert response.status_code == 200
    assert db.stored is None


def test_a_manifest_that_cannot_be_written_leaves_the_heartbeat_alone(send, caplog):
    db = _DB(_registration())
    db.fail_manifest_write = True
    with caplog.at_level(logging.WARNING, logger=platform_api.logger.name):
        response = send(db, procedure_manifest_fingerprint=FINGERPRINT, procedure_manifest=_manifest())
    assert response.status_code == 200
    assert response.json()["procedure_manifest_wanted"] is False
    assert db.registration.status == "active"
    assert "Could not record the procedure manifest" in caplog.text


def test_a_registration_without_the_fields_is_unchanged(send):
    db = _DB(_registration())
    response = send(db)
    assert response.status_code == 200
    assert response.json()["procedure_manifest_wanted"] is False
    assert db.registration.procedure_manifest_fingerprint is None
    assert db.manifest_reads == 0 and db.stored is None


def test_a_platform_that_stops_reporting_a_fingerprint_decides_nothing_by_its_old_manifest(send):
    registration = _registration(FINGERPRINT)
    db = _DB(registration, _held(registration))
    send(db)
    assert registration.procedure_manifest_fingerprint is None
    assert db.stored is not None


# --- the published default ------------------------------------------------------------------


def test_the_published_default_stores_nothing_and_never_wants_a_manifest(send):
    registration = _registration(FINGERPRINT)
    db = _DB(registration)
    response = send(
        db,
        PUBLISHED_DEFAULT_REGISTRATION_TOKEN,
        procedure_manifest_fingerprint=OTHER_FINGERPRINT,
        procedure_manifest=_manifest(),
    )
    assert response.status_code == 200
    assert response.json()["procedure_manifest_wanted"] is False
    assert db.stored is None
    assert db.manifest_reads == 0
    assert registration.procedure_manifest_fingerprint == FINGERPRINT


def test_the_published_default_never_wants_a_manifest_for_a_fingerprint_alone(send):
    db = _DB(_registration())
    response = send(db, PUBLISHED_DEFAULT_REGISTRATION_TOKEN, procedure_manifest_fingerprint=FINGERPRINT)
    assert response.status_code == 200
    assert response.json()["procedure_manifest_wanted"] is False
    assert db.registration.procedure_manifest_fingerprint is None


# --- the size of the body ------------------------------------------------------------------


def test_a_body_over_the_cap_is_refused_before_it_is_parsed(send, monkeypatch):
    def _never(*_args, **_kwargs):
        raise AssertionError("an oversize body must not be parsed")

    monkeypatch.setattr(platform_api, "_REGISTER_BODY_MAX_BYTES", 200)
    monkeypatch.setattr(platform_api.RegisterRequest, "model_validate_json", _never)
    db = _DB(_registration())
    response = send(db, procedure_manifest_fingerprint=FINGERPRINT, procedure_manifest=_manifest())
    assert response.status_code == 413
    assert db.stored is None


def test_a_body_that_does_not_validate_answers_the_422_of_a_model_body(send):
    response = send(_DB(None), platform_url=None)
    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert error["loc"] == ["body", "platform_url"]
    assert error["type"] == "string_type"


def test_the_cap_leaves_room_for_the_largest_manifest_and_the_rest_of_the_registration():
    assert platform_api._REGISTER_BODY_MAX_BYTES > pm.MAX_MANIFEST_BYTES


def test_the_request_body_is_documented_although_the_route_reads_it_itself():
    app = FastAPI()
    app.include_router(platform_api.router, prefix="/api/v1/platform")
    body = app.openapi()["paths"]["/api/v1/platform/register"]["post"]["requestBody"]
    schema = body["content"]["application/json"]["schema"]
    assert {"platform_identifier", "platform_url", "intents", "procedure_manifest_fingerprint"} <= set(
        schema["properties"]
    )
    assert "$ref" not in str(schema)


# --- what a tool build reads ----------------------------------------------------------------


class _Selected:
    def __init__(self, row: object) -> None:
        self._row = row

    def first(self) -> object:
        return self._row


@pytest.fixture(autouse=True)
def _empty_manifest_cache():
    platform_identity._manifests.clear()
    yield
    platform_identity._manifests.clear()


class _Reader:
    def __init__(self, row: object) -> None:
        self.row = row
        self.statements: list[object] = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Selected(self.row)


_PLATFORM = platform_identity.RegisteredPlatform(platform_identifier="opencrq", platform_url=URL)


async def test_a_stored_manifest_is_read_as_a_manifest():
    row = SimpleNamespace(
        registration_id=uuid.uuid4(),
        fingerprint=FINGERPRINT,
        procedures={
            "groups.listGroups": {"type": "query", "input": None},
            "groups.createGroup": {"type": "mutation", "input": {"type": "object"}},
        },
    )
    manifest = await platform_identity.registered_procedure_manifest(_Reader(row), _PLATFORM)
    assert manifest is not None
    assert manifest.fingerprint == FINGERPRINT
    assert manifest.serves("groups.createGroup") and not manifest.serves("groups.gone")
    assert manifest.procedures["groups.createGroup"] == {"type": "mutation", "input": {"type": "object"}}


async def test_no_row_is_no_manifest():
    assert await platform_identity.registered_procedure_manifest(_Reader(None), _PLATFORM) is None


@pytest.mark.parametrize("procedures", [None, {}, [], {"a.b": "query"}, {"a.b": {"type": "subscription"}}])
async def test_a_stored_document_that_does_not_read_is_no_manifest(procedures):
    row = SimpleNamespace(registration_id=uuid.uuid4(), fingerprint=FINGERPRINT, procedures=procedures)
    assert await platform_identity.registered_procedure_manifest(_Reader(row), _PLATFORM) is None


async def test_the_manifest_is_read_only_under_the_fingerprint_the_registration_reported():
    reader = _Reader(None)
    await platform_identity.registered_procedure_manifest(reader, _PLATFORM)
    [statement] = reader.statements
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "platform_registrations.procedure_manifest_fingerprint = platform_procedure_manifests.fingerprint" in sql
    assert "platform_registrations.platform_identifier = " in sql
    assert statement.compile(dialect=postgresql.dialect()).params == {"platform_identifier_1": "opencrq"}


def _row(fingerprint: str, registration_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        registration_id=registration_id or uuid.uuid4(),
        fingerprint=fingerprint,
        procedures={"groups.listGroups": {"type": "query", "input": None}},
    )


async def test_the_stored_document_is_read_and_built_once_per_fingerprint(monkeypatch):
    built = []
    real = pm.stored_manifest
    monkeypatch.setattr(pm, "stored_manifest", lambda *args: built.append(args) or real(*args))
    row = _row(FINGERPRINT)
    reader = _Reader(row)
    first = await platform_identity.registered_procedure_manifest(reader, _PLATFORM)
    second = await platform_identity.registered_procedure_manifest(reader, _PLATFORM)
    assert first is not None and second is first
    assert len(built) == 1
    # The key is read twice, the document once.
    assert len(reader.statements) == 3


async def test_a_changed_fingerprint_builds_the_manifest_again(monkeypatch):
    built = []
    real = pm.stored_manifest
    monkeypatch.setattr(pm, "stored_manifest", lambda *args: built.append(args) or real(*args))
    registration_id = uuid.uuid4()
    await platform_identity.registered_procedure_manifest(_Reader(_row(FINGERPRINT, registration_id)), _PLATFORM)
    moved = await platform_identity.registered_procedure_manifest(
        _Reader(_row(OTHER_FINGERPRINT, registration_id)), _PLATFORM
    )
    assert moved is not None and moved.fingerprint == OTHER_FINGERPRINT
    assert len(built) == 2


async def test_the_manifest_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(platform_identity, "_MANIFEST_CACHE_SIZE", 2)
    for _ in range(3):
        await platform_identity.registered_procedure_manifest(_Reader(_row(FINGERPRINT)), _PLATFORM)
    assert len(platform_identity._manifests) == 2


def test_the_stored_manifest_is_the_one_a_follow_up_kept():
    manifest = pm.parse_registered_manifest(_manifest(), FINGERPRINT)
    assert manifest is not None and manifest.fingerprint == FINGERPRINT
    row = {path: dict(entry) for path, entry in manifest.procedures.items()}
    assert pm.stored_manifest(FINGERPRINT, row).procedures == manifest.procedures


# --- what the card reads --------------------------------------------------------------------


class _StatusDB:
    """A session whose one select answers *row* and whose savepoint is a no-op."""

    def __init__(self, row: object = None, *, fails: bool = False) -> None:
        self.row, self.fails, self.statements = row, fails, []

    def begin_nested(self):
        return _Savepoint()

    async def execute(self, statement):
        self.statements.append(statement)
        if self.fails:
            raise RuntimeError("database down")
        return _Selected(self.row)


@pytest.fixture
def owned(monkeypatch):
    """The registration that owns the integration is ``_PLATFORM`` and holds *stored* as its manifest."""

    def _owned(stored: object) -> None:
        async def _platform(db, integration_id):
            return _PLATFORM

        async def _manifest_of(db, platform):
            return stored

        monkeypatch.setattr(platform_identity, "platform_for_integration", _platform)
        monkeypatch.setattr(platform_identity, "registered_procedure_manifest", _manifest_of)

    return _owned


async def test_the_status_carries_the_reported_version_fingerprint_and_the_manifest_in_force(owned):
    stored = pm.parse_registered_manifest(_manifest(), FINGERPRINT)
    owned(stored)
    received = datetime(2026, 10, 22, tzinfo=UTC)
    row = SimpleNamespace(platform_version=" 1.4.0 ", procedure_manifest_fingerprint=FINGERPRINT, received_at=received)
    status = await platform_identity.procedure_manifest_status(_StatusDB(row), uuid.uuid4())
    assert status == platform_identity.ProcedureManifestStatus("1.4.0", FINGERPRINT, stored, received)


async def test_a_manifest_that_decides_nothing_has_no_received_time(owned):
    owned(None)
    row = SimpleNamespace(platform_version="1.4.0", procedure_manifest_fingerprint=FINGERPRINT, received_at=None)
    status = await platform_identity.procedure_manifest_status(_StatusDB(row), uuid.uuid4())
    assert status == platform_identity.ProcedureManifestStatus("1.4.0", FINGERPRINT, None, None)


async def test_the_manifest_row_is_joined_only_under_the_fingerprint_reported(owned):
    owned(None)
    db = _StatusDB(SimpleNamespace(platform_version="", procedure_manifest_fingerprint=None, received_at=None))
    await platform_identity.procedure_manifest_status(db, uuid.uuid4())
    sql = str(db.statements[0].compile(dialect=postgresql.dialect()))
    assert "LEFT OUTER JOIN platform_procedure_manifests ON" in sql
    assert "platform_procedure_manifests.fingerprint = platform_registrations.procedure_manifest_fingerprint" in sql


async def test_an_integration_no_registration_owns_has_no_status(monkeypatch):
    async def _none(db, integration_id):
        return None

    monkeypatch.setattr(platform_identity, "platform_for_integration", _none)
    assert await platform_identity.procedure_manifest_status(_StatusDB(), uuid.uuid4()) is None


async def test_a_registration_that_cannot_be_read_has_no_status(owned, caplog):
    owned(None)
    with caplog.at_level(logging.WARNING):
        assert await platform_identity.procedure_manifest_status(_StatusDB(fails=True), uuid.uuid4()) is None
    assert "Could not read the procedure manifest" in caplog.text
