"""Regression tests for 500s previously seen on ``GET /intents/catalog``
(and any other endpoint behind ``get_current_user``) when auto-provisioning
a brand-new cross-platform automation account.

``_resolve_trusted_platform_jwt`` (app/api/deps.py) auto-provisions a local
``User`` row the first time it sees a trusted-platform JWT for an email it
doesn't recognise yet. Two concurrent callers for the same not-yet-existing
account both see "no user" from the initial lookup and both attempt to
provision a row, which can collide two different ways:

* **Same issuer** — the new row's id is deterministic
  (``uuid5(NAMESPACE_URL, f"platform:{iss}:{email}")``), so both callers
  compute the identical id and their INSERTs collide on ``users_pkey``.
* **Different issuers, same email** — the ids differ (the seed includes
  ``iss``), so the INSERTs instead collide on the separate ``users.email``
  unique constraint.

Before the fix this was a plain ``db.add()`` + ``db.commit()``: the loser
hit ``UniqueViolationError`` uncaught, and the endpoint returned 500 instead
of the caller getting the (by-then existing) user back.

The fix (mirroring ``get_or_create_intent`` in app/api/v1/helpers.py) uses ``INSERT ... ON CONFLICT DO NOTHING`` (no ``index_elements``, so
it absorbs a conflict on *either* constraint) followed by a re-``SELECT``
keyed on the same normalized email used for the initial lookup — not on the
locally-computed id, which the losing caller's own row may never have
appeared under when the conflict was on ``email`` rather than ``id``.

This is a check-then-insert race, not something a single-threaded test can
force via real concurrency — the fake DB below reproduces the race *window*
deterministically (each request's own first "does a user with this email
exist" lookup always sees "no user", exactly like a read that predates
either commit) while enforcing the real uniqueness constraints at INSERT
time via a table shared across both calls, the same way Postgres's unique
indexes would.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Select
from sqlalchemy.sql.dml import Insert

from app.api import deps
from app.api.v1 import intents as intents_api
from app.core import xtm_auth
from app.core.database import get_db

_AUTOMATION_ISS = "https://openaev.example.test"
_AUTOMATION_ISS_2 = "https://opencti.example.test"
_AUTOMATION_EMAIL = "AUTOMATION MANAGER"  # payload.email, pre-lowercasing


@dataclass
class _FakePayload:
    iss: str
    email: str


class _Result:
    def __init__(self, value=None, rows=None):
        self._value = value
        self._rows = rows if rows is not None else []

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _UsersTable:
    """Shared "physical" users table backing both requests' ``_RacyDB``
    instances — standing in for the single Postgres table both real
    connections would see, with both of ``users``' real unique constraints
    (``id`` primary key, ``email`` unique column) enforced."""

    def __init__(self):
        self._by_id: dict = {}
        self._by_email: dict = {}

    def insert_or_ignore(self, **params) -> None:
        key_id = params["id"]
        key_email = params["email"].lower()
        if key_id in self._by_id or key_email in self._by_email:
            return
        row = SimpleNamespace(**params, is_admin=(params["role"] == "admin"))
        self._by_id[key_id] = row
        self._by_email[key_email] = row

    def find_by_email(self, email: str):
        return self._by_email.get(email.lower())

    @property
    def row_count(self) -> int:
        return len(self._by_id)


class _RacyDB:
    """Async-session stub modelling the auto-provision race.

    ``_resolve_trusted_platform_jwt`` issues exactly two ``users``-table
    SELECTs of identical shape (``func.lower(User.email) == ...``) per
    call — the pre-insert existence check and the post-insert re-fetch —
    with an unrelated ``SSOConfig`` lookup (falls back to defaults on
    ``None``) in between. They're told apart by position *within this
    instance* (each simulated request gets its own ``_RacyDB``):

      * 1st ``users`` SELECT — forced empty, modelling both callers'
        reads predating either commit (the race window itself).
      * ``INSERT ... ON CONFLICT DO NOTHING`` on ``users`` — resolves
        against the shared table; a no-op for whichever call loses.
      * 2nd ``users`` SELECT (the re-fetch) — reflects the shared table's
        now-resolved state, i.e. the winner's row, for both callers.
    """

    def __init__(self, table: _UsersTable):
        self._table = table
        self._user_select_count = 0

    async def execute(self, stmt, *args, **kwargs):
        if isinstance(stmt, Insert) and stmt.table.name == "users":
            self._table.insert_or_ignore(**stmt.compile().params)
            return _Result(None)

        froms = stmt.get_final_froms() if isinstance(stmt, Select) else []
        if (
            froms
            and getattr(froms[0], "name", None) == "users"
            and stmt.whereclause is not None
        ):
            self._user_select_count += 1
            if self._user_select_count == 1:
                return _Result(None)
            bound_email = next(iter(stmt.compile().params.values()))
            return _Result(self._table.find_by_email(bound_email))

        return _Result(None, rows=[])

    async def commit(self):
        pass


def _client(monkeypatch, table: _UsersTable, *, iss: str) -> TestClient:
    async def _fake_verify_xtm_jwt(_token, _db):
        return _FakePayload(iss=iss, email=_AUTOMATION_EMAIL)

    monkeypatch.setattr(xtm_auth, "verify_xtm_jwt", _fake_verify_xtm_jwt)

    # Bypass the real EdDSA header check (deps.py's quick pre-check) so a
    # plain dummy bearer token is accepted without signing an actual JWT.
    monkeypatch.setattr(
        deps.jwt, "get_unverified_header", lambda _token: {"alg": "EdDSA"}
    )

    app = FastAPI()
    app.include_router(intents_api.router, prefix="/api/v1")

    async def _get_db():
        yield _RacyDB(table)

    app.dependency_overrides[get_db] = _get_db
    return TestClient(app, raise_server_exceptions=False)


def test_concurrent_auto_provision_same_issuer_no_longer_500s(monkeypatch):
    """Both calls share the same ``iss`` — their INSERTs collide on the
    deterministic ``id`` (``users_pkey``)."""
    table = _UsersTable()
    headers = {"Authorization": "Bearer dummy.trusted.jwt"}

    client = _client(monkeypatch, table, iss=_AUTOMATION_ISS)

    first = client.get("/api/v1/intents/catalog", headers=headers)
    assert first.status_code == 200, first.text
    assert table.row_count == 1

    second = client.get("/api/v1/intents/catalog", headers=headers)
    assert second.status_code == 200, (
        "second call for the same not-yet-provisioned automation account "
        f"should resolve via ON CONFLICT DO NOTHING, not 500; got "
        f"{second.status_code}: {second.text}"
    )
    # The loser's INSERT must be a no-op — still exactly one row, not a
    # duplicate or a second distinct id.
    assert table.row_count == 1


def test_concurrent_auto_provision_different_issuers_same_email_no_longer_500s(
    monkeypatch,
):
    """Two *different* trusted issuers auto-provisioning the same email
    concurrently compute different deterministic ids (the seed includes
    ``iss``), so their INSERTs collide on the ``users.email`` unique
    constraint instead of ``users_pkey``. The re-fetch must key on email
    (not on the locally-computed id, which the loser's row never has under
    this conflict shape) or the loser gets ``NoResultFound`` -> 500."""
    table = _UsersTable()
    headers = {"Authorization": "Bearer dummy.trusted.jwt"}

    first_resp = _client(monkeypatch, table, iss=_AUTOMATION_ISS).get(
        "/api/v1/intents/catalog", headers=headers
    )
    assert first_resp.status_code == 200, first_resp.text
    assert table.row_count == 1

    second_resp = _client(monkeypatch, table, iss=_AUTOMATION_ISS_2).get(
        "/api/v1/intents/catalog", headers=headers
    )
    assert second_resp.status_code == 200, (
        "second call from a different issuer for the same not-yet-"
        "provisioned email should resolve via the email unique constraint, "
        f"not 500; got {second_resp.status_code}: {second_resp.text}"
    )
    # Still exactly one row: the second issuer's differently-keyed INSERT
    # must have been absorbed by ON CONFLICT DO NOTHING on `email`, not
    # created as a second account for the same address.
    assert table.row_count == 1


def test_deterministic_id_depends_on_issuer():
    """Pin the root cause of the email-conflict variant: two different
    issuers for the same email produce two *different* ids, so their
    INSERTs cannot collide on ``users_pkey`` — only on ``users.email`` —
    which is exactly why the re-SELECT must key on email."""
    id_a = uuid.uuid5(
        uuid.NAMESPACE_URL, f"platform:{_AUTOMATION_ISS}:{_AUTOMATION_EMAIL}"
    )
    id_b = uuid.uuid5(
        uuid.NAMESPACE_URL, f"platform:{_AUTOMATION_ISS_2}:{_AUTOMATION_EMAIL}"
    )
    assert id_a != id_b


def test_deterministic_id_is_identical_for_same_issuer():
    """Pin the same-issuer variant's root cause: the auto-provisioned id
    depends only on ``(iss, email)``, so any two concurrent callers from
    the same issuer for the same not-yet-existing account collide on the
    exact same primary key."""
    seed = f"platform:{_AUTOMATION_ISS}:{_AUTOMATION_EMAIL}"
    first_id = uuid.uuid5(uuid.NAMESPACE_URL, seed)
    second_id = uuid.uuid5(uuid.NAMESPACE_URL, seed)
    assert first_id == second_id
