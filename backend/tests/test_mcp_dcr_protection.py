"""Abuse-resistance tests for MCP OAuth Dynamic Client Registration
(``app.api.v1.mcp_oauth.register_client``) — SEC-6.

RFC 7591 / the MCP spec require ``POST /mcp/oauth/register`` to stay
unauthenticated (clients self-register before any user session exists), so
the endpoint cannot be protected with a login gate. Instead it is hardened
against abuse with three independent layers, each pinned here:

* an operator kill switch (``settings.mcp_dcr_enabled``) that turns the
  endpoint into a 403 when the platform does not use MCP OAuth at all;
* a per-IP rate limit (``enforce_rate_limit``) that runs BEFORE any DB
  work, so a registration flood is rejected with 429 without touching
  Postgres;
* a bounded client table (``settings.mcp_dcr_max_clients``) with
  oldest-first eviction — eviction is preferred over a hard rejection
  because a hard cap would let an attacker who filled the table
  permanently DoS registration for legitimate clients, while an evicted
  still-active client self-heals by re-registering (standard RFC 7591
  client behaviour on ``invalid_client``). The count → evict → insert
  critical section runs under a transaction-scoped Postgres advisory
  lock so concurrent registrations cannot all observe ``count < cap``
  and insert past the cap.

These tests exercise the route function directly with lightweight stubs
(the codebase has no FastAPI test client / DB session fixture — see
``test_mcp_oauth_authorize.py`` for the same approach).
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from app.api.v1 import mcp_oauth as mcp_oauth_module
from app.api.v1.mcp_oauth import (
    DCR_RATE_WINDOW_SECONDS,
    DCR_REGISTRATION_LOCK_KEY,
    ClientRegistrationRequest,
    register_client,
)
from app.core.config import Settings, settings

REDIRECT_URI = "https://app.example.com/callback"


class _StubDB:
    """Async DB stub for the registration path.

    * ``scalar()`` returns the queued client count (cap check);
    * ``execute()`` answers the advisory-lock statement (tracked in
      ``lock_calls``) and captures the bulk eviction ``DELETE`` (tracked
      in ``delete_stmts``, with a configurable ``rowcount``);
    * ``add`` / ``commit`` calls are captured so tests can assert what
      was written;
    * ``ops`` records the operation order so tests can pin that the
      advisory lock is taken BEFORE the cap check.
    """

    def __init__(self, *, count: int = 0, evict_rowcount: int = 0):
        self._count = count
        self._evict_rowcount = evict_rowcount
        self.added: list = []
        self.delete_stmts: list = []
        self.commits = 0
        self.scalar_calls = 0
        self.lock_calls = 0
        self.lock_params: dict | None = None
        self.ops: list[str] = []

    async def scalar(self, _stmt):
        self.scalar_calls += 1
        self.ops.append("count")
        return self._count

    async def execute(self, stmt, params=None):
        if "pg_advisory_xact_lock" in str(stmt):
            self.lock_calls += 1
            self.lock_params = params
            self.ops.append("lock")
            return None
        self.delete_stmts.append(stmt)
        self.ops.append("evict")
        return SimpleNamespace(rowcount=self._evict_rowcount)

    def add(self, obj):
        self.added.append(obj)
        self.ops.append("insert")

    async def commit(self):
        self.commits += 1


def _evict_limit(stmt) -> int:
    """Extract the LIMIT bound into the eviction DELETE's IN-subquery."""
    compiled = str(
        stmt.compile(compile_kwargs={"literal_binds": True})
    ).upper()
    assert compiled.startswith("DELETE"), "eviction must be a bulk DELETE"
    assert " IN (SELECT " in compiled, (
        "eviction must use an IN-subquery, not per-row deletes"
    )
    match = re.search(r"LIMIT\s+(\d+)", compiled)
    assert match, "the eviction subquery must carry a LIMIT"
    return int(match.group(1))


def _make_request(host: str = "203.0.113.7") -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host=host), headers={})


def _patch_rate_limit_passthrough(monkeypatch) -> dict:
    """Replace ``enforce_rate_limit`` with a no-op that records its
    envelope (bucket name, limit, window) for assertion.
    """
    captured: dict[str, object] = {}

    async def _rate_limit(_request, name, *, limit, window_seconds):
        captured.update(name=name, limit=limit, window_seconds=window_seconds)

    monkeypatch.setattr(mcp_oauth_module, "enforce_rate_limit", _rate_limit)
    return captured


async def _call(db, body: ClientRegistrationRequest | None = None):
    return await register_client(
        request=_make_request(),
        body=body
        or ClientRegistrationRequest(
            client_name="VS Code", redirect_uris=[REDIRECT_URI]
        ),
        db=db,
    )


# ── kill switch (settings.mcp_dcr_enabled) ──────────────────────────────


async def test_register_refuses_with_403_when_dcr_disabled(monkeypatch):
    """With ``MCP_DCR_ENABLED=false`` the endpoint must refuse with 403
    before doing ANY work — no rate-limit bucket consumed, no DB access.
    """

    async def _rate_limit_must_not_run(*_a, **_kw):  # pragma: no cover
        raise AssertionError("rate limit must not run when DCR is disabled")

    monkeypatch.setattr(
        mcp_oauth_module, "enforce_rate_limit", _rate_limit_must_not_run
    )
    monkeypatch.setattr(settings, "mcp_dcr_enabled", False)

    db = _StubDB()
    with pytest.raises(HTTPException) as exc:
        await _call(db)

    assert exc.value.status_code == 403
    assert "disabled" in exc.value.detail.lower()
    assert db.scalar_calls == 0 and not db.delete_stmts and not db.added
    assert db.lock_calls == 0, "no advisory lock when DCR is disabled"


async def test_register_enabled_by_default():
    """The kill switch must default to enabled — DCR is part of the MCP
    spec onboarding flow and must work out of the box.

    Pins the CODE default on the ``Settings`` model (not the runtime
    ``settings`` instance, which an ambient ``MCP_DCR_ENABLED`` env var
    could override and make this test environment-dependent) — same
    pattern as ``test_assignment_engine_schedule_spread``.
    """
    assert Settings.model_fields["mcp_dcr_enabled"].default is True
    assert Settings.model_fields["mcp_dcr_max_clients"].default == 500


# ── per-IP rate limiting ────────────────────────────────────────────────


async def test_register_rate_limit_runs_before_any_db_work(monkeypatch):
    """A throttled request must be rejected with 429 before the route
    touches the DB, so a registration flood cannot hammer Postgres.
    """

    async def _rate_limited(*_a, **_kw):
        raise HTTPException(status_code=429, detail="Too many requests")

    monkeypatch.setattr(mcp_oauth_module, "enforce_rate_limit", _rate_limited)

    db = _StubDB()
    with pytest.raises(HTTPException) as exc:
        await _call(db)

    assert exc.value.status_code == 429
    assert db.scalar_calls == 0 and not db.delete_stmts and not db.added, (
        "rate limit must short-circuit before any DB query"
    )
    assert db.lock_calls == 0, "no advisory lock for a throttled request"


async def test_register_throttle_envelope_is_pinned(monkeypatch):
    """Pin the throttle envelope (bucket name + limit + window) so a future
    tweak is a conscious change, mirroring the authorize-endpoint test.
    """
    captured = _patch_rate_limit_passthrough(monkeypatch)

    db = _StubDB(count=0)
    resp = await _call(db)

    assert isinstance(resp, JSONResponse)
    assert captured["name"] == "mcp-oauth-register"
    assert captured["limit"] == max(1, settings.mcp_dcr_rate_limit)
    assert Settings.model_fields["mcp_dcr_rate_limit"].default == 5
    assert captured["window_seconds"] == DCR_RATE_WINDOW_SECONDS == 3600


async def test_register_throttle_reads_mcp_dcr_rate_limit(monkeypatch):
    """``MCP_DCR_RATE_LIMIT`` sizes the per-IP budget; a value below 1
    is clamped to 1 rather than turning registration off by accident."""
    captured = _patch_rate_limit_passthrough(monkeypatch)

    monkeypatch.setattr(settings, "mcp_dcr_rate_limit", 60)
    await _call(_StubDB(count=0))
    assert captured["limit"] == 60

    monkeypatch.setattr(settings, "mcp_dcr_rate_limit", 0)
    await _call(_StubDB(count=0))
    assert captured["limit"] == 1


# ── bounded client table with oldest-first eviction ─────────────────────


async def test_register_under_cap_does_not_evict(monkeypatch):
    """Below the cap, registration must succeed (201) without running the
    eviction query or deleting anything.
    """
    _patch_rate_limit_passthrough(monkeypatch)
    monkeypatch.setattr(settings, "mcp_dcr_max_clients", 500)

    db = _StubDB(count=10)
    resp = await _call(db)

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 201
    assert not db.delete_stmts, "no eviction below the cap"
    assert len(db.added) == 1, "the new client row must be persisted"
    assert db.lock_calls == 1, "the advisory lock guards every registration"


async def test_register_at_cap_evicts_oldest_and_succeeds(monkeypatch):
    """At the cap, the oldest registration(s) must be evicted to make room
    and the new registration must still succeed — an attacker who fills
    the table must not be able to permanently DoS legitimate clients.
    Eviction must be a single bulk DELETE (IN-subquery ordered by
    ``created_at``), not a load-then-delete-per-row loop that would hold
    the advisory lock open while materializing ORM objects.
    """
    _patch_rate_limit_passthrough(monkeypatch)
    monkeypatch.setattr(settings, "mcp_dcr_max_clients", 500)

    db = _StubDB(count=500, evict_rowcount=1)
    resp = await _call(db)

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 201
    assert len(db.delete_stmts) == 1, "exactly one bulk eviction DELETE"
    assert _evict_limit(db.delete_stmts[0]) == 1, (
        "count(500) - cap(500) + 1 = 1 oldest row must be evicted"
    )
    assert len(db.added) == 1

    payload = resp.body.decode()
    assert "client_id" in payload


async def test_register_over_cap_evicts_overflow_plus_one(monkeypatch):
    """If the table somehow grew past the cap (e.g. the cap was lowered),
    a single registration must evict enough rows to land back AT the cap
    after the insert (``count - cap + 1``).
    """
    _patch_rate_limit_passthrough(monkeypatch)
    monkeypatch.setattr(settings, "mcp_dcr_max_clients", 500)

    db = _StubDB(count=502, evict_rowcount=3)
    resp = await _call(db)

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 201
    assert len(db.delete_stmts) == 1
    assert _evict_limit(db.delete_stmts[0]) == 3, (
        "count(502) - cap(500) + 1 = 3 oldest rows must be evicted"
    )


async def test_register_serializes_cap_check_with_advisory_lock(monkeypatch):
    """The cap check must run under the transaction-scoped Postgres advisory
    lock (``pg_advisory_xact_lock``), taken BEFORE the count: without it, N
    concurrent registrations could all observe ``count < cap``, skip
    eviction, and each insert — exceeding the cap by the burst size. The
    lock is released by the final ``commit()``, so the whole
    count → evict → insert section is serialized.
    """
    _patch_rate_limit_passthrough(monkeypatch)
    monkeypatch.setattr(settings, "mcp_dcr_max_clients", 500)

    db = _StubDB(count=500, evict_rowcount=1)
    await _call(db)

    assert db.lock_calls == 1, "exactly one advisory lock per registration"
    assert db.lock_params == {"lock_key": DCR_REGISTRATION_LOCK_KEY}, (
        "the lock key must be bound as a parameter (repo advisory-lock "
        "convention), not interpolated into the SQL string"
    )
    assert db.ops.index("lock") < db.ops.index("count"), (
        "the advisory lock must be taken BEFORE the cap check"
    )
    assert db.ops.index("count") < db.ops.index("evict")
    assert db.ops.index("evict") < db.ops.index("insert")
    assert db.commits == 1, "the lock is held until the final commit"


async def test_register_clamps_misconfigured_cap_to_one(monkeypatch):
    """A 0/negative ``MCP_DCR_MAX_CLIENTS`` must be clamped to 1, not fed
    into the overflow math raw — otherwise ``count - cap + 1`` would evict
    essentially the whole table on every registration. With the clamp,
    a single-row table at cap 0 behaves exactly like cap 1: one eviction.
    """
    _patch_rate_limit_passthrough(monkeypatch)
    monkeypatch.setattr(settings, "mcp_dcr_max_clients", 0)

    db = _StubDB(count=1, evict_rowcount=1)
    resp = await _call(db)

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 201
    assert len(db.delete_stmts) == 1
    assert _evict_limit(db.delete_stmts[0]) == 1, (
        "clamped cap=1: count(1) - 1 + 1 = 1 eviction"
    )
    assert len(db.added) == 1

    # An empty table must register without evicting even at cap 0.
    monkeypatch.setattr(settings, "mcp_dcr_max_clients", -5)
    db = _StubDB(count=0)
    resp = await _call(db)
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 201
    assert not db.delete_stmts


async def test_register_takes_lock_only_after_cpu_work(monkeypatch):
    """The advisory lock must be acquired AFTER the CPU-only work
    (client_id generation, bcrypt secret hashing — ~100-300ms for a
    confidential client) so the globally serialized critical section
    covers only the DB work (count → evict → insert → commit). Taking
    the lock before the hashing would make every concurrent registration
    queue behind another request's bcrypt run.
    """
    _patch_rate_limit_passthrough(monkeypatch)
    monkeypatch.setattr(settings, "mcp_dcr_max_clients", 500)

    db = _StubDB(count=0)

    async def _fake_hashpw_async(_secret):
        db.ops.append("hash")
        return b"$2b$12$stub-hash"

    monkeypatch.setattr(mcp_oauth_module, "bcrypt_hashpw_async", _fake_hashpw_async)

    body = ClientRegistrationRequest(
        client_name="Confidential client",
        redirect_uris=[REDIRECT_URI],
        token_endpoint_auth_method="client_secret_post",
    )
    resp = await _call(db, body)

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 201
    assert "hash" in db.ops, "confidential clients must get a hashed secret"
    assert db.ops.index("hash") < db.ops.index("lock"), (
        "bcrypt hashing must run BEFORE the advisory lock is taken — the "
        "lock must serialize only the DB critical section"
    )


# ── existing validation still applies after the hardening ───────────────


async def test_register_still_rejects_invalid_redirect_uri(monkeypatch):
    """The redirect URI scheme validation (loopback or HTTPS) must keep
    running after the new guards — the hardening must not reorder it away.
    """
    _patch_rate_limit_passthrough(monkeypatch)

    db = _StubDB()
    body = ClientRegistrationRequest(
        client_name="Evil", redirect_uris=["http://evil.example.com/cb"]
    )
    with pytest.raises(HTTPException) as exc:
        await _call(db, body)

    assert exc.value.status_code == 400
    assert not db.added
