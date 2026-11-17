"""Tests for /api/v1/feedback-criteria, Settings > Agents and automations > Quality.

Writes are admin-only (a non-admin gets a 403 before any query), reads are open
to every signed-in user because the rating dialog draws the enabled criteria,
and every definition goes through the rules of ``services.feedback_criteria``:
shape, non-negative weight, never all enabled weights at zero, one name per
criterion, a type that never changes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.api import deps
from app.api.v1 import feedback_criteria as criteria_api
from app.core.database import get_db
from app.models.feedback_criterion import FeedbackCriterion


class _Scalars:
    def __init__(self, values):
        self._values = list(values)

    def all(self):
        return list(self._values)


class _Result:
    def __init__(self, values=()):
        self._values = list(values)

    def scalars(self):
        return _Scalars(self._values)


class _FakeDb:
    """``criteria`` answers every ``load_criteria``; ``name_taken`` the name check."""

    def __init__(self, criteria=(), *, name_taken=False):
        self.criteria = list(criteria)
        self.name_taken = name_taken
        self.added: list[object] = []
        self.deleted: list[object] = []
        self.commits = 0
        self.statements: list[object] = []

    async def execute(self, statement, params=None):
        self.statements.append(statement)
        enabled_only = "enabled is true" in str(statement).lower()
        return _Result([c for c in self.criteria if c.enabled or not enabled_only])

    async def scalar(self, statement):
        self.statements.append(statement)
        return 1 if self.name_taken else 0

    async def get(self, _model, criterion_id):
        return next((c for c in self.criteria if c.id == criterion_id), None)

    def add(self, row):
        now = datetime.now(UTC)
        row.id = row.id or uuid.uuid4()
        row.created_at = now
        row.updated_at = now
        self.added.append(row)

    async def flush(self):
        return None

    async def delete(self, row):
        self.deleted.append(row)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks = getattr(self, "rollbacks", 0) + 1


def _user(*, admin: bool):
    return SimpleNamespace(id=uuid.uuid4(), role="admin" if admin else "user", is_admin=admin)


def _criterion(*, name="Accuracy", key="accuracy", weight=1.0, enabled=True, criterion_type="scale"):
    now = datetime.now(UTC)
    return FeedbackCriterion(
        id=uuid.uuid4(),
        key=key,
        name=name,
        description=None,
        type=criterion_type,
        weight=weight,
        options=None,
        min_value=1.0,
        max_value=5.0,
        sort_order=0,
        enabled=enabled,
        created_at=now,
        updated_at=now,
    )


def _client(fake_db, user, monkeypatch, audits=None):
    async def _log_audit(_db, **kwargs):
        if audits is not None:
            audits.append(kwargs)

    monkeypatch.setattr(criteria_api, "log_audit", _log_audit)
    app = FastAPI()
    app.include_router(criteria_api.router, prefix="/api/v1/feedback-criteria")
    app.dependency_overrides[deps.get_current_user] = lambda: user

    async def _get_db():
        yield fake_db

    app.dependency_overrides[get_db] = _get_db
    return TestClient(app)


URL = "/api/v1/feedback-criteria"
SCALE = {"name": "Accuracy", "type": "scale", "min_value": 1, "max_value": 5}


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def test_every_user_reads_the_enabled_criteria(monkeypatch):
    enabled, disabled = _criterion(), _criterion(name="Old", key="old", enabled=False)
    fake_db = _FakeDb([enabled, disabled])
    client = _client(fake_db, _user(admin=False), monkeypatch)

    resp = client.get(URL, params={"include_disabled": "true"})

    assert resp.status_code == 200
    # A non-admin never sees the disabled ones, even when asking for them.
    assert [item["key"] for item in resp.json()] == ["accuracy"]


def test_an_admin_may_read_the_disabled_criteria(monkeypatch):
    fake_db = _FakeDb([_criterion(), _criterion(name="Old", key="old", enabled=False)])
    client = _client(fake_db, _user(admin=True), monkeypatch)

    resp = client.get(URL, params={"include_disabled": "true"})

    assert sorted(item["key"] for item in resp.json()) == ["accuracy", "old"]


def test_a_non_admin_cannot_write(monkeypatch):
    criterion = _criterion()
    fake_db = _FakeDb([criterion])
    client = _client(fake_db, _user(admin=False), monkeypatch)

    assert client.post(URL, json=SCALE).status_code == 403
    assert client.put(f"{URL}/{criterion.id}", json={"weight": 2}).status_code == 403
    assert client.delete(f"{URL}/{criterion.id}").status_code == 403
    assert fake_db.statements == []
    assert fake_db.commits == 0


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_an_admin_creates_a_categorical_criterion(monkeypatch):
    fake_db = _FakeDb([_criterion(key="tone")])
    audits: list[dict] = []
    client = _client(fake_db, _user(admin=True), monkeypatch, audits)

    resp = client.post(
        URL,
        json={
            "name": "  Tone  ",
            "type": "categorical",
            "weight": 2,
            "options": [
                {"value": "cold", "label": "Cold", "score": 0},
                {"value": "warm", "label": "Warm", "score": 1},
            ],
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Tone"
    # "tone" is taken: the key is made unique, the name is what people read.
    assert body["key"] == "tone-2"
    assert body["type"] == "categorical"
    assert body["weight"] == 2.0
    assert body["min_value"] is None
    assert [option["value"] for option in body["options"]] == ["cold", "warm"]
    assert fake_db.commits == 1
    assert audits and audits[0]["action"] == "create"
    assert audits[0]["entity_type"] == "feedback_criterion"


def test_a_duplicate_name_is_a_409(monkeypatch):
    fake_db = _FakeDb([_criterion()], name_taken=True)
    client = _client(fake_db, _user(admin=True), monkeypatch)

    resp = client.post(URL, json=SCALE)

    assert resp.status_code == 409
    assert fake_db.commits == 0


def test_a_concurrent_create_of_the_same_name_is_a_409_not_a_500(monkeypatch):
    """Both requests pass the name check before either commits; the second
    then meets the key's unique constraint at its flush."""

    class _RacingDb(_FakeDb):
        async def flush(self):
            raise IntegrityError("INSERT INTO feedback_criteria", None, Exception("uq_feedback_criteria_key"))

    fake_db = _RacingDb([])
    audits: list[dict] = []
    client = _client(fake_db, _user(admin=True), monkeypatch, audits)

    resp = client.post(URL, json=SCALE)

    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]
    assert fake_db.rollbacks == 1
    assert fake_db.commits == 0
    assert audits == []


def test_an_invalid_shape_is_a_422_naming_the_problem(monkeypatch):
    client = _client(_FakeDb(), _user(admin=True), monkeypatch)

    resp = client.post(URL, json={"name": "Accuracy", "type": "scale", "min_value": 1.5, "max_value": 5})

    assert resp.status_code == 422
    assert "whole number" in resp.json()["detail"]


def test_the_only_enabled_criterion_cannot_weigh_zero(monkeypatch):
    client = _client(_FakeDb(), _user(admin=True), monkeypatch)

    resp = client.post(URL, json={**SCALE, "weight": 0})

    assert resp.status_code == 422
    assert "all weigh zero" in resp.json()["detail"]


def test_a_zero_weight_criterion_is_fine_beside_a_weighted_one(monkeypatch):
    client = _client(_FakeDb([_criterion()]), _user(admin=True), monkeypatch)

    resp = client.post(URL, json={**SCALE, "name": "Tone", "weight": 0})

    assert resp.status_code == 201


def test_unknown_fields_are_refused(monkeypatch):
    client = _client(_FakeDb(), _user(admin=True), monkeypatch)

    assert client.post(URL, json={**SCALE, "key": "custom"}).status_code == 422


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def test_an_admin_updates_a_criterion(monkeypatch):
    criterion = _criterion()
    fake_db = _FakeDb([criterion])
    client = _client(fake_db, _user(admin=True), monkeypatch)

    resp = client.put(f"{URL}/{criterion.id}", json={"name": "Correctness", "max_value": 10, "description": " Is it right? "})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["name"], body["max_value"], body["description"]) == ("Correctness", 10.0, "Is it right?")
    # The key survives a rename, so every stored score keeps its dimension.
    assert body["key"] == "accuracy"
    assert fake_db.commits == 1


def test_the_type_cannot_change(monkeypatch):
    criterion = _criterion()
    client = _client(_FakeDb([criterion]), _user(admin=True), monkeypatch)

    assert client.put(f"{URL}/{criterion.id}", json={"type": "numeric"}).status_code == 422


def test_an_explicit_null_on_a_required_field_is_refused(monkeypatch):
    criterion = _criterion()
    client = _client(_FakeDb([criterion]), _user(admin=True), monkeypatch)

    resp = client.put(f"{URL}/{criterion.id}", json={"weight": None})

    assert resp.status_code == 422
    assert "weight cannot be null" in resp.json()["detail"]


def test_disabling_the_only_weighted_criterion_is_refused(monkeypatch):
    weighted = _criterion(weight=1.0)
    weightless = _criterion(name="Tone", key="tone", weight=0.0)
    fake_db = _FakeDb([weighted, weightless])
    client = _client(fake_db, _user(admin=True), monkeypatch)

    resp = client.put(f"{URL}/{weighted.id}", json={"enabled": False})

    assert resp.status_code == 422
    assert fake_db.commits == 0


def test_renaming_onto_another_criterion_is_a_409(monkeypatch):
    criterion = _criterion()
    client = _client(_FakeDb([criterion], name_taken=True), _user(admin=True), monkeypatch)

    assert client.put(f"{URL}/{criterion.id}", json={"name": "Tone"}).status_code == 409


def test_an_unknown_criterion_is_a_404(monkeypatch):
    client = _client(_FakeDb(), _user(admin=True), monkeypatch)

    assert client.put(f"{URL}/{uuid.uuid4()}", json={"weight": 2}).status_code == 404
    assert client.delete(f"{URL}/{uuid.uuid4()}").status_code == 404


def test_a_malformed_criterion_id_answers_like_an_unknown_one(monkeypatch):
    client = _client(_FakeDb(), _user(admin=True), monkeypatch)

    put = client.put(f"{URL}/not-a-uuid", json={"weight": 2})
    delete = client.delete(f"{URL}/not-a-uuid")

    assert (put.status_code, delete.status_code) == (404, 404)
    assert put.json()["detail"] == delete.json()["detail"] == "Criterion not found"


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_an_admin_deletes_a_criterion(monkeypatch):
    criterion, other = _criterion(), _criterion(name="Tone", key="tone")
    fake_db = _FakeDb([criterion, other])
    audits: list[dict] = []
    client = _client(fake_db, _user(admin=True), monkeypatch, audits)

    resp = client.delete(f"{URL}/{criterion.id}")

    assert resp.status_code == 204
    assert fake_db.deleted == [criterion]
    assert fake_db.commits == 1
    assert audits[0]["action"] == "delete"


def test_deleting_the_only_weighted_criterion_is_refused(monkeypatch):
    weighted = _criterion(weight=1.0)
    weightless = _criterion(name="Tone", key="tone", weight=0.0)
    fake_db = _FakeDb([weighted, weightless])
    client = _client(fake_db, _user(admin=True), monkeypatch)

    resp = client.delete(f"{URL}/{weighted.id}")

    assert resp.status_code == 422
    assert fake_db.deleted == []
