"""The Terms of Use acceptance endpoints.

Terms of Use are accepted ONCE for the whole platform, by an admin, on behalf
of the organisation. Covers the contract of ``/auth/terms``,
``/auth/terms/accept`` and ``/auth/terms/decline``:

* acceptance is written to the ``PlatformSettings`` singleton (version / when /
  accepting user), not per-user;
* only an **admin** may accept — a regular user gets 403 and cannot bind the
  whole platform;
* acceptance is **idempotent** — resubmitting the current version returns 200
  and does NOT rewrite the original ``tou_accepted_at`` or re-audit, because
  that timestamp is the evidence the audit view reports;
* a **stale version** is refused with 409 rather than silently recorded;
* acceptance and refusal both land in the **audit log**, the compliance artifact;
* all three routes are wired to ``get_current_user_ungated``, so they stay
  reachable while the gate is closed.

Fake-session convention (``test_auth_me_group_ids.py``): the backend suite has
no real-DB harness, so the session is a stub and the platform singleton is a
mutable stub the route writes onto.
"""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import auth as auth_api
from app.core.config import settings
from app.core.database import get_db

CURRENT = "2099-01-01"


class _FakeSession:
    """Minimal AsyncSession stand-in: records executes, no-ops commits.

    Emulates the accept endpoint's conditional acceptance UPDATE against the
    stub singleton with the same IS-DISTINCT-FROM semantics the real statement
    has: the write applies (rowcount 1) only while the stored version differs
    from the incoming one, so the race/idempotency contract is exercised for
    real instead of being mocked away.
    """

    def __init__(self, ps=None) -> None:
        self.executed: list[object] = []
        self.commits = 0
        self._ps = ps

    async def execute(self, stmt, *_args, **_kwargs):
        self.executed.append(stmt)

        class _Result:
            rowcount = 0

        result = _Result()
        params = getattr(stmt, "compile", lambda: None)()
        values = getattr(params, "params", {}) or {}
        new_version = values.get("tou_accepted_version")
        if (
            self._ps is not None
            and new_version is not None
            and self._ps.tou_accepted_version != new_version
        ):
            self._ps.tou_accepted_version = new_version
            self._ps.tou_accepted_at = values.get("tou_accepted_at")
            self._ps.tou_accepted_by_user_id = values.get("tou_accepted_by_user_id")
            result.rowcount = 1
        return result

    async def commit(self) -> None:
        self.commits += 1


class _StubPS:
    """Mutable stand-in for the PlatformSettings singleton."""

    def __init__(self, version: str | None = None, at: datetime | None = None):
        self.id = uuid.uuid4()
        self.tou_accepted_version = version
        self.tou_accepted_at = at
        self.tou_accepted_by_user_id: uuid.UUID | None = None


def _fake_user(*, is_admin: bool = True):
    return type(
        "_User",
        (),
        {
            "id": uuid.uuid4(),
            "email": "u@example.com",
            "full_name": "Test User",
            "is_admin": is_admin,
            "auth_credential_kind": "session_jwt",
        },
    )()


def _make_client(monkeypatch, user, *, agentic: bool = True, enabled: bool = True,
                 version: str = CURRENT, ps: _StubPS | None = None):
    monkeypatch.setattr(settings, "terms_of_use_enabled", enabled)
    monkeypatch.setattr(settings, "terms_of_use_version", version)

    async def _agentic(_db):
        return agentic

    import app.api.v1.platform as platform_api

    monkeypatch.setattr(platform_api, "is_agentic_ai_licensed", _agentic)

    ps_obj = ps if ps is not None else _StubPS()

    import app.api.v1.platform_settings_helpers as ps_helpers

    deps.reset_terms_of_use_cache()

    async def _get_ps(_db):
        return ps_obj

    # The accept endpoint loads via ``get_platform_settings`` (create-if-missing
    # is correct there: it writes), while every read path (``_build_terms_state``)
    # goes through the read-only ``peek_platform_settings`` — stub both onto the
    # same mutable singleton.
    monkeypatch.setattr(ps_helpers, "get_platform_settings", _get_ps)
    monkeypatch.setattr(ps_helpers, "peek_platform_settings", _get_ps)

    audits: list[dict] = []

    async def _log_audit(_db, **kwargs):
        audits.append(kwargs)
        return None

    monkeypatch.setattr(auth_api, "log_audit", _log_audit)

    session = _FakeSession(ps_obj)
    app = FastAPI()
    app.include_router(auth_api.router, prefix="/api/v1/auth")

    async def _get_db():
        yield session

    app.dependency_overrides[deps.get_current_user_ungated] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    return TestClient(app), session, audits, ps_obj


def test_get_terms_reports_pending_state(monkeypatch):
    client, _, _, _ = _make_client(monkeypatch, _fake_user())
    body = client.get("/api/v1/auth/terms").json()
    assert body["required"] is True
    assert body["accepted"] is False
    assert body["version"] == CURRENT
    assert body["url"] == settings.terms_of_use_url


def test_get_terms_reports_accepted_state(monkeypatch):
    when = datetime(2099, 1, 2, 10, 30, tzinfo=UTC)
    client, _, _, _ = _make_client(
        monkeypatch, _fake_user(), ps=_StubPS(version=CURRENT, at=when)
    )
    body = client.get("/api/v1/auth/terms").json()
    assert body["accepted"] is True
    assert body["accepted_version"] == CURRENT
    assert body["accepted_at"] == when.isoformat()


def test_can_accept_only_for_admins(monkeypatch):
    """``can_accept`` mirrors admin — the frontend shows the accept button off it."""
    admin_client, _, _, _ = _make_client(monkeypatch, _fake_user(is_admin=True))
    assert admin_client.get("/api/v1/auth/terms").json()["can_accept"] is True

    user_client, _, _, _ = _make_client(monkeypatch, _fake_user(is_admin=False))
    assert user_client.get("/api/v1/auth/terms").json()["can_accept"] is False


def test_community_edition_reports_accepted(monkeypatch):
    """``accepted`` collapses to True when the gate doesn't apply."""
    client, _, _, _ = _make_client(monkeypatch, _fake_user(), agentic=False)
    body = client.get("/api/v1/auth/terms").json()
    assert body["required"] is False
    assert body["accepted"] is True


def test_admin_accept_records_platform_wide_and_unblocks(monkeypatch):
    user = _fake_user(is_admin=True)
    client, session, audits, ps = _make_client(monkeypatch, user)

    body = client.post("/api/v1/auth/terms/accept", json={"version": CURRENT}).json()

    assert body["accepted"] is True
    assert body["accepted_version"] == CURRENT
    # The platform-wide state the request gate actually reads.
    assert ps.tou_accepted_version == CURRENT
    assert ps.tou_accepted_at is not None
    assert ps.tou_accepted_by_user_id == user.id
    assert session.commits == 1
    # The compliance artifact.
    assert len(audits) == 1
    assert audits[0]["action"] == "accept"
    assert audits[0]["entity_type"] == "terms_of_use"
    assert audits[0]["details"]["tou_version"] == CURRENT


def test_non_admin_cannot_accept(monkeypatch):
    """A regular user cannot bind the whole organisation."""
    user = _fake_user(is_admin=False)
    client, session, audits, ps = _make_client(monkeypatch, user)

    resp = client.post("/api/v1/auth/terms/accept", json={"version": CURRENT})

    assert resp.status_code == 403
    assert ps.tou_accepted_version is None
    assert session.commits == 0
    assert audits == []


def test_accept_is_idempotent(monkeypatch):
    """Re-accepting must not move the timestamp or write a second audit entry."""
    original = datetime(2099, 1, 2, 10, 30, tzinfo=UTC)
    ps = _StubPS(version=CURRENT, at=original)
    client, session, audits, ps = _make_client(monkeypatch, _fake_user(), ps=ps)

    resp = client.post("/api/v1/auth/terms/accept", json={"version": CURRENT})

    assert resp.status_code == 200
    assert resp.json()["accepted"] is True
    assert ps.tou_accepted_at == original
    assert session.commits == 0
    assert audits == []


def test_second_admin_cannot_rewrite_acceptance_evidence(monkeypatch):
    """The conditional UPDATE makes exactly one admin the recorded acceptor.

    Two admins racing the endpoint both pass the version check; the atomic
    IS-DISTINCT-FROM guard means only the first write matches a row. The
    second admin's accept must not move the timestamp, must not replace the
    accepting user, and must not emit a duplicate audit entry.
    """
    first_admin = _fake_user(is_admin=True)
    client, _session, audits, ps = _make_client(monkeypatch, first_admin)

    assert client.post(
        "/api/v1/auth/terms/accept", json={"version": CURRENT}
    ).status_code == 200
    recorded_at = ps.tou_accepted_at
    assert ps.tou_accepted_by_user_id == first_admin.id
    assert len(audits) == 1

    # A different admin re-accepts the same (now current) version.
    second_admin = _fake_user(is_admin=True)
    client2, _, audits2, _ = _make_client(monkeypatch, second_admin, ps=ps)

    resp = client2.post("/api/v1/auth/terms/accept", json={"version": CURRENT})

    assert resp.status_code == 200
    assert resp.json()["accepted"] is True
    assert ps.tou_accepted_by_user_id == first_admin.id
    assert ps.tou_accepted_at == recorded_at
    assert audits2 == []


def test_accept_rejects_stale_version(monkeypatch):
    user = _fake_user(is_admin=True)
    client, session, audits, ps = _make_client(monkeypatch, user)

    resp = client.post("/api/v1/auth/terms/accept", json={"version": "2026-07-14"})

    assert resp.status_code == 409
    assert CURRENT in resp.json()["detail"]
    assert ps.tou_accepted_version is None
    assert session.commits == 0
    assert audits == []


def test_accept_is_a_noop_when_gate_disabled(monkeypatch):
    """No record is written against a version this platform does not publish."""
    client, session, audits, ps = _make_client(
        monkeypatch, _fake_user(), enabled=False
    )

    resp = client.post("/api/v1/auth/terms/accept", json={"version": CURRENT})

    assert resp.status_code == 200
    assert resp.json()["accepted"] is True
    assert ps.tou_accepted_version is None
    assert session.commits == 0
    assert audits == []


def test_decline_audits_without_recording_acceptance(monkeypatch):
    client, _session, audits, ps = _make_client(monkeypatch, _fake_user())

    resp = client.post("/api/v1/auth/terms/decline")

    assert resp.status_code == 200
    # Still gated — declining changes no platform state, it only makes the
    # refusal distinguishable from a closed tab.
    assert resp.json()["accepted"] is False
    assert ps.tou_accepted_version is None
    assert len(audits) == 1
    assert audits[0]["action"] == "decline"
    assert audits[0]["entity_type"] == "terms_of_use"


def test_terms_routes_bypass_the_gate(monkeypatch):
    """The escape hatch: these routes must not depend on the gated resolver."""
    def _calls(dependant) -> set:
        found = set()
        for sub in dependant.dependencies:
            found.add(sub.call)
            found |= _calls(sub)
        return found

    routes = {
        r.path: r
        for r in auth_api.router.routes
        if getattr(r, "path", "").startswith("/terms")
    }
    assert set(routes) == {"/terms", "/terms/accept", "/terms/decline"}
    for path, route in routes.items():
        reachable = _calls(route.dependant)
        assert deps.get_current_user_ungated in reachable, path
        assert deps.get_current_user not in reachable, path

    me_route = next(r for r in auth_api.router.routes if getattr(r, "path", "") == "/me")
    me_reachable = _calls(me_route.dependant)
    assert deps.get_current_user_ungated in me_reachable
    assert deps.get_current_user not in me_reachable


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
