"""Browser sign-in handoff for XTM One Desktop.

Pure tests over the handoff's pieces (fake Redis, fake sessions):

* the record lifecycle - start, complete once, consume once, expire;
* the routes - who may do what, and the terminal codes the app polls on;
* the SSO carry - the id survives the provider round-trip in ``state`` /
  ``RelayState`` and lands in the callback's fragment, and only a well-formed
  id is ever placed there.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import re
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import auth_sso, deps
from app.api.v1 import desktop_login as routes
from app.services import desktop_login as svc

# ── Fakes ────────────────────────────────────────────────────────────────────


class _FakeRedis:
    """Just enough of redis.asyncio: set with ex, get, getdel, delete, ttl, and
    the one Lua script the service runs (``_COMPLETE_IF_PENDING_LUA``), with
    the same semantics: rewrite only while the stored record reads pending,
    keep the TTL. Every call is recorded so a test can assert HOW a step
    reached Redis (one atomic round-trip, not a read followed by a write).
    """

    def __init__(self):
        self.store: dict[str, tuple[str, int]] = {}
        self.calls: list[tuple[str, str]] = []

    async def set(self, key, value, ex=None):
        self.calls.append(("set", key))
        self.store[key] = (value, int(ex or -1))

    async def get(self, key):
        self.calls.append(("get", key))
        entry = self.store.get(key)
        return entry[0] if entry else None

    async def getdel(self, key):
        self.calls.append(("getdel", key))
        entry = self.store.pop(key, None)
        return entry[0] if entry else None

    async def delete(self, key):
        self.calls.append(("delete", key))
        self.store.pop(key, None)

    async def ttl(self, key):
        entry = self.store.get(key)
        return entry[1] if entry else -2

    async def eval(self, script, numkeys, key, *argv):
        assert script == svc._COMPLETE_IF_PENDING_LUA and numkeys == 1
        self.calls.append(("eval", key))
        entry = self.store.get(key)
        if entry is None:
            return 0
        try:
            record = json.loads(entry[0])
        except ValueError:
            return 0
        if not isinstance(record, dict) or record.get("status") != argv[0]:
            return 0
        self.store[key] = (argv[1], entry[1])  # KEEPTTL
        return 1


@pytest.fixture
def redis(monkeypatch):
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr(svc, "redis_client", _client)
    return fake


def _run(coro):
    return asyncio.run(coro)


def _force_login_ids(monkeypatch, *login_ids: str) -> None:
    """Make ``start_login`` hand out *login_ids* in order instead of random ones."""
    ids = iter(login_ids)
    monkeypatch.setattr(svc, "secrets", SimpleNamespace(token_urlsafe=lambda _n: next(ids)))


# A compact JWS / JWT: three dot-separated base64url segments.
_JWT_SHAPED = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")


def _assert_no_session_token(raw: str, *, minted: tuple[str, ...] = ()) -> None:
    """Redis holds WHO confirmed, never a session token.

    Checked on the record's structure, never as a substring of the raw JSON:
    that JSON also carries the random 43-character ``login_id``, and an id that
    happened to contain ``jwt`` failed the substring check.
    """
    stored = json.loads(raw)
    assert set(stored) == {field.name for field in dataclasses.fields(svc.PendingLogin)}
    assert not {"access_token", "token", "jwt", "session"} & set(stored)
    for name, value in stored.items():
        if isinstance(value, str):
            assert not _JWT_SHAPED.fullmatch(value), f"{name} holds a JWT-shaped value"
            assert value not in minted, f"{name} holds a minted token"


def _runner(name="laptop", *, user_id=None, is_company_managed=False):
    """A runner row: private to *user_id* (a fresh stranger by default), or
    company-managed (no owner)."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        platform="windows",
        user_id=None if is_company_managed else (user_id or uuid.uuid4()),
        is_company_managed=is_company_managed,
    )


def _user(*, credential="session_jwt", impersonated_by=None):
    """A signed-in person as ``deps`` hands them to a route: a first-hand browser
    session by default, or one of the credentials the handoff must refuse."""
    user = SimpleNamespace(id=uuid.uuid4(), email="a@b.c", full_name="A B")
    user.auth_credential_kind = credential
    if impersonated_by is not None:
        user.impersonated_by = impersonated_by
    return user


class _FakeSession:
    """A session that answers ``db.get`` from the rows it was given by id."""

    def __init__(self, rows=()):
        self.committed = False
        self.rows = {row.id: row for row in rows}

    async def get(self, model, key):
        return self.rows.get(key)

    async def commit(self):
        self.committed = True


# ── Ids and provider state ───────────────────────────────────────────────────


def test_login_ids_are_well_formed_and_only_those_pass():
    assert svc.is_valid_login_id("A" * 43)
    assert svc.is_valid_login_id("abcDEF012_-" * 4)  # 44 chars
    assert not svc.is_valid_login_id("short")
    assert not svc.is_valid_login_id("A" * 65)
    assert not svc.is_valid_login_id("has space" + "a" * 30)
    assert not svc.is_valid_login_id("bad/slash" + "a" * 30)
    assert not svc.is_valid_login_id(None)
    assert not svc.is_valid_login_id(42)


def test_state_round_trips_and_rejects_everything_else():
    login_id = "x" * 43
    state = svc.encode_state(login_id)
    assert state == f"desktop:{login_id}"
    assert svc.decode_state(state) == login_id
    # A provider echoing an ordinary CSRF state, or an attacker's crafted one.
    assert svc.decode_state("abc123") is None
    assert svc.decode_state("desktop:") is None
    assert svc.decode_state("desktop:not valid!") is None
    assert svc.decode_state(None) is None
    assert svc.decode_state(["desktop:" + login_id]) is None


def test_relay_state_fits_the_saml_80_byte_cap(redis):
    record = _run(svc.start_login(runner_id="r", runner_name="n", platform="macos"))
    assert len(svc.encode_state(record.login_id).encode()) <= 80


# ── Record lifecycle ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "forced_id",
    # A random id, and the id that once failed CI: it spells ``jwt``.
    [None, "u7vbIqwF5ZHn0UQjwtIfIfCSU0a2zwujf4g9w1Kqhl4"],
    ids=["random-id", "id-containing-jwt"],
)
def test_start_then_complete_then_consume_exactly_once(redis, monkeypatch, forced_id):
    if forced_id is not None:
        _force_login_ids(monkeypatch, forced_id)
    record = _run(svc.start_login(runner_id="r1", runner_name="laptop", platform="windows"))
    assert svc.is_valid_login_id(record.login_id)
    assert record.status == svc.STATUS_PENDING
    key = f"desktop_login:{record.login_id}"
    assert redis.store[key][1] == svc.LOGIN_TTL_SECONDS

    loaded = _run(svc.load_login(record.login_id))
    assert loaded is not None and loaded.runner_id == "r1" and loaded.user_id is None

    assert _run(svc.complete_login(loaded, "user-1")) is True
    # Completion keeps the remaining window, never extends it.
    assert redis.store[key][1] <= svc.LOGIN_TTL_SECONDS
    stored = json.loads(redis.store[key][0])
    assert stored["status"] == svc.STATUS_COMPLETED and stored["user_id"] == "user-1"
    # Redis holds WHO confirmed, never a session token.
    _assert_no_session_token(redis.store[key][0])

    # A second completion is refused: the first decision stands.
    again = _run(svc.load_login(record.login_id))
    assert _run(svc.complete_login(again, "user-2")) is False

    consumed = _run(svc.consume_login(record.login_id))
    assert consumed is not None and consumed.user_id == "user-1"
    assert _run(svc.consume_login(record.login_id)) is None
    assert _run(svc.load_login(record.login_id)) is None


def test_expired_record_cannot_be_completed(redis):
    record = _run(svc.start_login(runner_id="r1", runner_name="laptop", platform="windows"))
    # Expired in Redis = gone: the compare-and-set finds no record to rewrite.
    del redis.store[f"desktop_login:{record.login_id}"]
    assert _run(svc.complete_login(record, "jwt")) is False
    assert f"desktop_login:{record.login_id}" not in redis.store


def test_concurrent_confirmations_attach_exactly_one_session(redis):
    """Two confirmation tabs that BOTH read ``pending`` (a reload, a double
    click, two browsers): the write is a compare-and-set on the stored state,
    so the second one is refused even though its own copy still says pending -
    the first session stands, the app never receives a second one."""
    record = _run(svc.start_login(runner_id="r1", runner_name="laptop", platform="windows"))
    first = _run(svc.load_login(record.login_id))
    second = _run(svc.load_login(record.login_id))  # a stale pending copy
    assert first.status == second.status == svc.STATUS_PENDING

    assert _run(svc.complete_login(first, "user-first")) is True
    assert _run(svc.complete_login(second, "user-second")) is False

    stored = json.loads(redis.store[f"desktop_login:{record.login_id}"][0])
    assert stored["status"] == svc.STATUS_COMPLETED
    assert stored["user_id"] == "user-first"
    # The transition was ONE Redis round-trip - never a read here and a write
    # there that a concurrent caller could slip between.
    assert ("eval", f"desktop_login:{record.login_id}") in redis.calls
    assert ("set", f"desktop_login:{record.login_id}") not in redis.calls[1:]


def test_completion_never_resurrects_a_cancelled_login(redis):
    """A cancel (the person gave up in the app) landing between the browser's
    read and its confirmation must win: the confirmation finds no record and
    does not recreate one - a ``SET ... EX`` after the fact would have."""
    record = _run(svc.start_login(runner_id="r1", runner_name="laptop", platform="windows"))
    loaded = _run(svc.load_login(record.login_id))
    _run(svc.cancel_login(record.login_id))
    assert _run(svc.complete_login(loaded, "user-1")) is False
    assert redis.store == {}


def test_consume_is_one_atomic_getdel(redis):
    """The exchange read is a ``GETDEL``: of two polls racing for a completed
    record, exactly one can receive the session. A ``GET`` followed by a
    ``DELETE`` would let both read the record before either deleted it."""
    record = _run(svc.start_login(runner_id="r1", runner_name="laptop", platform="windows"))
    _run(svc.complete_login(_run(svc.load_login(record.login_id)), "user-1"))
    redis.calls.clear()

    consumed = _run(svc.consume_login(record.login_id))
    assert consumed is not None and consumed.user_id == "user-1"
    assert redis.calls == [("getdel", f"desktop_login:{record.login_id}")]
    assert _run(svc.consume_login(record.login_id)) is None
    # And a malformed id is refused before Redis is even asked.
    redis.calls.clear()
    assert _run(svc.consume_login("nope")) is None
    assert redis.calls == []


def test_unreadable_record_is_dropped_not_served(redis):
    redis.store["desktop_login:" + "z" * 43] = ("not json", 100)
    assert _run(svc.load_login("z" * 43)) is None
    assert ("desktop_login:" + "z" * 43) not in redis.store


def test_malformed_ids_never_touch_redis(redis):
    assert _run(svc.load_login("nope")) is None
    _run(svc.cancel_login("nope"))
    assert redis.store == {}


# ── Routes ───────────────────────────────────────────────────────────────────


def test_start_binds_the_login_to_the_calling_runner(redis):
    owner = SimpleNamespace(id=uuid.uuid4(), full_name="Samuel Hassine")
    runner = _runner("SAMUEL-LAPTOP", user_id=owner.id)
    out = _run(routes.start_desktop_login(runner=runner, db=_FakeSession([owner])))
    assert out["expires_in_seconds"] == svc.LOGIN_TTL_SECONDS
    record = _run(svc.load_login(out["login_id"]))
    assert record.runner_id == str(runner.id)
    assert record.runner_name == "SAMUEL-LAPTOP"
    assert record.platform == "windows"
    # Who enrolled the machine rides along for the confirmation page ...
    assert record.enrolled_by == "Samuel Hassine"
    # ... a company machine names nobody, and so does one whose owner is gone.
    company = _run(routes.start_desktop_login(runner=_runner(is_company_managed=True), db=_FakeSession()))
    assert _run(svc.load_login(company["login_id"])).enrolled_by is None
    orphan = _run(routes.start_desktop_login(runner=_runner("orphan"), db=_FakeSession()))
    assert _run(svc.load_login(orphan["login_id"])).enrolled_by is None


def test_exchange_is_pending_then_the_session_once_then_gone(redis, monkeypatch):
    runner = _runner()
    login_id = _run(routes.start_desktop_login(runner=runner, db=_FakeSession()))["login_id"]
    minted: list[str] = []

    def _mint(subject, *, continues=None):
        minted.append(subject)
        return f"fresh-{subject}"

    monkeypatch.setattr(routes, "create_access_token", _mint)

    pending = _run(routes.exchange_desktop_login(routes.ExchangeRequest(login_id=login_id), runner=runner))
    assert pending.status_code == 202
    assert minted == []  # nothing is minted for a claim not yet confirmable

    record = _run(svc.load_login(login_id))
    _run(svc.complete_login(record, "user-1"))
    done = _run(routes.exchange_desktop_login(routes.ExchangeRequest(login_id=login_id), runner=runner))
    # Minted at the moment it is handed out, for the user who confirmed.
    assert done == {"status": svc.STATUS_COMPLETED, "access_token": "fresh-user-1"}
    assert minted == ["user-1"]

    with pytest.raises(HTTPException) as exc:
        _run(routes.exchange_desktop_login(routes.ExchangeRequest(login_id=login_id), runner=runner))
    assert exc.value.status_code == 404
    assert minted == ["user-1"]  # the second poll minted nothing


def test_exchange_with_a_malformed_id_is_gone_without_touching_redis(redis):
    runner = _runner()
    redis.calls.clear()
    with pytest.raises(HTTPException) as exc:
        _run(routes.exchange_desktop_login(routes.ExchangeRequest(login_id="short"), runner=runner))
    assert exc.value.status_code == 404
    assert redis.calls == []


def test_exchange_refuses_another_runners_login(redis):
    owner, other = _runner("mine"), _runner("theirs")
    login_id = _run(routes.start_desktop_login(runner=owner, db=_FakeSession()))["login_id"]
    with pytest.raises(HTTPException) as exc:
        _run(routes.exchange_desktop_login(routes.ExchangeRequest(login_id=login_id), runner=other))
    assert exc.value.status_code == 404


def test_read_shows_the_machine_and_nothing_secret(redis):
    owner = SimpleNamespace(id=uuid.uuid4(), full_name="Samuel Hassine")
    runner = _runner("SAMUEL-LAPTOP", user_id=owner.id)
    login_id = _run(routes.start_desktop_login(runner=runner, db=_FakeSession([owner])))["login_id"]
    out = _run(routes.read_desktop_login(login_id))
    assert out["runner_name"] == "SAMUEL-LAPTOP"
    assert out["platform"] == "windows"
    assert out["enrolled_by"] == "Samuel Hassine"
    # Who enrolled it, yes; not the runner id, not who confirmed, and not the
    # state either: the id alone must not be an oracle for whether someone
    # signed in.
    shown = {"runner_name", "platform", "enrolled_by", "expires_in_seconds"}
    assert set(out) == shown
    _run(svc.complete_login(_run(svc.load_login(login_id)), "user-1"))
    after = _run(routes.read_desktop_login(login_id))
    assert set(after) == shown
    with pytest.raises(HTTPException) as exc:
        _run(routes.read_desktop_login("q" * 43))
    assert exc.value.status_code == 404


def test_complete_requires_the_user_to_be_allowed_to_use_the_machine(redis, monkeypatch):
    runner = _runner("someone-elses-laptop")
    login_id = _run(routes.start_desktop_login(runner=runner, db=_FakeSession()))["login_id"]
    user = _user()

    async def _invisible(db, *, user_id, runner_id, user_group_ids=None):
        return None

    monkeypatch.setattr(routes, "load_desktop_context", _invisible)
    with pytest.raises(HTTPException) as exc:
        _run(routes.complete_desktop_login(login_id, request=None, user=user, db=_FakeSession()))
    assert exc.value.status_code == 403
    # Refused means untouched: the app keeps polling a still-pending login.
    assert _run(svc.load_login(login_id)).status == svc.STATUS_PENDING


def test_complete_is_authentication_only_never_the_terms_of_use_gate():
    """``complete`` IS the sign-in step: with the Terms of Use published and
    not yet accepted, the gated dependency would refuse the session before the
    app ever holds one (an admin could then never sign in on the desktop to
    accept them). The app's own requests hit the gate once it is signed in.
    Only the companion's PIN lock applies: a locked phone hands nothing over."""
    param = inspect.signature(routes.complete_desktop_login).parameters["user"]
    assert param.default.dependency is deps.get_current_user_unlocked
    unlocked = inspect.signature(deps.get_current_user_unlocked).parameters["user"]
    assert unlocked.default.dependency is deps.get_current_user_ungated


def test_complete_refuses_a_machine_enrolled_by_someone_else_even_when_shared(redis, monkeypatch):
    """Visible is not enough. A group-shared machine is one its owner lets
    colleagues DRIVE under their own account; confirming a sign-in ON it would
    hand the colleague's session to the owner's polling Desktop app - the
    login-CSRF the flow is designed against. Only the enroller may."""
    owner = uuid.uuid4()
    runner = _runner("owners-laptop", user_id=owner)
    login_id = _run(routes.start_desktop_login(runner=runner, db=_FakeSession()))["login_id"]
    colleague = _user()

    async def _visible_because_shared(db, *, user_id, runner_id, user_group_ids=None):
        return SimpleNamespace(runner=runner)

    monkeypatch.setattr(routes, "load_desktop_context", _visible_because_shared)
    with pytest.raises(HTTPException) as exc:
        _run(routes.complete_desktop_login(login_id, request=None, user=colleague, db=_FakeSession()))
    assert exc.value.status_code == 403
    assert "enrolled by someone else" in exc.value.detail
    # Refused means untouched: still pending, no session attached.
    record = _run(svc.load_login(login_id))
    assert record.status == svc.STATUS_PENDING and record.user_id is None


@pytest.mark.parametrize(
    "user",
    [
        _user(credential="api_key"),
        _user(credential="platform_jwt"),
        _user(credential=None),
        _user(impersonated_by=str(uuid.uuid4())),
    ],
    ids=["api-key", "platform-jwt", "untagged", "impersonation"],
)
def test_complete_refuses_everything_but_a_first_hand_browser_session(redis, monkeypatch, user):
    """The session minted for the app carries no confinement and no
    ``impersonated_by`` mark, so only a real browser session may be turned into
    one: an ``fcp-`` API key (ToU-exempt, long-lived), a cross-platform JWT
    held by another platform, or an impersonating admin must all be refused -
    before the record is even looked at."""
    runner = _runner("laptop", user_id=user.id)
    login_id = _run(routes.start_desktop_login(runner=runner, db=_FakeSession()))["login_id"]

    async def _never(db, *, user_id, runner_id, user_group_ids=None):
        raise AssertionError("visibility must not be consulted for a refused credential")

    monkeypatch.setattr(routes, "load_desktop_context", _never)
    with pytest.raises(HTTPException) as exc:
        _run(routes.complete_desktop_login(login_id, request=None, user=user, db=_FakeSession()))
    assert exc.value.status_code == 403
    assert "own browser session" in exc.value.detail
    record = _run(svc.load_login(login_id))
    assert record.status == svc.STATUS_PENDING and record.user_id is None
    # The same rule guards a decline: it drops someone's request and audits it.
    with pytest.raises(HTTPException) as exc:
        _run(routes.decline_desktop_login(login_id, request=None, user=user, db=_FakeSession()))
    assert exc.value.status_code == 403
    assert _run(svc.load_login(login_id)) is not None


def test_decline_drops_the_request_and_audits_the_refusal(redis, monkeypatch):
    """"Not my machine": the request dies so nobody else can complete it in
    its window, and the administrator the page points at finds the refusal -
    with the machine - in the audit trail. Idempotent once the record is gone."""
    runner = _runner("suspicious-laptop")
    login_id = _run(routes.start_desktop_login(runner=runner, db=_FakeSession()))["login_id"]
    user = _user()
    audits: list[dict] = []

    async def _audit(db, **kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(routes, "log_audit", _audit)
    db = _FakeSession()
    _run(routes.decline_desktop_login(login_id, request=None, user=user, db=db))
    assert _run(svc.load_login(login_id)) is None
    assert db.committed
    assert len(audits) == 1
    # Not a sign-in: the Security Audit's dormant accounts and the last login read ``login``.
    assert audits[0]["action"] == "login_declined"
    assert "Declined" in audits[0]["summary"] and "suspicious-laptop" in audits[0]["summary"]
    assert audits[0]["details"]["runner_id"] == str(runner.id)
    # The app polling for it is told the request is gone, and a second
    # decline (a double click) is a quiet no-op.
    with pytest.raises(HTTPException) as exc:
        _run(routes.exchange_desktop_login(routes.ExchangeRequest(login_id=login_id), runner=runner))
    assert exc.value.status_code == 404
    _run(routes.decline_desktop_login(login_id, request=None, user=user, db=db))
    assert len(audits) == 1


def test_complete_grants_nothing_when_the_audit_write_fails(redis, monkeypatch):
    """Fail closed: the audit row is written and committed BEFORE the record
    turns completed. A failed audit therefore leaves the login pending - the
    app keeps polling and mints nothing - instead of an unaudited session."""
    user = _user()
    runner = _runner("laptop", user_id=user.id)
    login_id = _run(routes.start_desktop_login(runner=runner, db=_FakeSession()))["login_id"]

    async def _visible(db, *, user_id, runner_id, user_group_ids=None):
        return SimpleNamespace(runner=runner)

    async def _audit_down(db, **kwargs):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(routes, "load_desktop_context", _visible)
    monkeypatch.setattr(routes, "log_audit", _audit_down)
    with pytest.raises(RuntimeError):
        _run(routes.complete_desktop_login(login_id, request=None, user=user, db=_FakeSession()))
    record = _run(svc.load_login(login_id))
    assert record.status == svc.STATUS_PENDING and record.user_id is None
    pending = _run(routes.exchange_desktop_login(routes.ExchangeRequest(login_id=login_id), runner=runner))
    assert pending.status_code == 202


def test_complete_allows_a_company_managed_machine(redis, monkeypatch):
    """A company-managed machine has no enroller of its own (a shared
    workstation): anyone who may target it may sign in on it."""
    runner = _runner("kiosk", is_company_managed=True)
    login_id = _run(routes.start_desktop_login(runner=runner, db=_FakeSession()))["login_id"]
    user = _user()

    async def _visible(db, *, user_id, runner_id, user_group_ids=None):
        return SimpleNamespace(runner=runner)

    async def _audit(db, **kwargs):
        pass

    monkeypatch.setattr(routes, "load_desktop_context", _visible)
    monkeypatch.setattr(routes, "log_audit", _audit)
    monkeypatch.setattr(routes, "create_access_token", lambda subject, *, continues=None: f"fresh-{subject}")
    out = _run(routes.complete_desktop_login(login_id, request=None, user=user, db=_FakeSession()))
    assert out["status"] == svc.STATUS_COMPLETED


@pytest.mark.parametrize(
    "forced_ids",
    # Random ids, and ids that spell the fake token's ``fresh-`` prefix.
    [None, ("fresh-" + "a" * 37, "fresh-" + "b" * 37)],
    ids=["random-ids", "ids-containing-the-token-prefix"],
)
def test_complete_records_the_user_and_audits_and_exchange_mints_the_session(redis, monkeypatch, forced_ids):
    if forced_ids is not None:
        _force_login_ids(monkeypatch, *forced_ids)
    user = _user()
    runner = _runner("laptop", user_id=user.id)
    login_id = _run(routes.start_desktop_login(runner=runner, db=_FakeSession()))["login_id"]

    async def _visible(db, *, user_id, runner_id, user_group_ids=None):
        return SimpleNamespace(runner=runner)

    audits: list[dict] = []

    async def _audit(db, **kwargs):
        audits.append(kwargs)

    minted: list[str] = []
    continued: list[object] = []

    def _mint(subject, *, continues=None):
        minted.append(subject)
        continued.append(continues)
        return f"fresh-{subject}"

    # The confirming browser session signed in at 1_700_000_000 and ends 8
    # hours later.
    user.web_session = SimpleNamespace(
        claims={"auth_time": 1_700_000_000, "sid": "browser-sid"}, expires_at=1_700_028_800
    )
    monkeypatch.setattr(routes, "load_desktop_context", _visible)
    monkeypatch.setattr(routes, "log_audit", _audit)
    monkeypatch.setattr(routes, "create_access_token", _mint)
    db = _FakeSession()
    out = _run(routes.complete_desktop_login(login_id, request=None, user=user, db=db))
    assert out == {"status": svc.STATUS_COMPLETED, "runner_name": "laptop"}
    assert db.committed
    assert audits and "XTM One Desktop" in audits[0]["summary"] and "laptop" in audits[0]["summary"]
    assert audits[0]["action"] == "login" and audits[0]["entity_type"] == "user"
    assert audits[0]["details"]["runner_id"] == str(runner.id)
    # Confirmation stored WHO, not a token: nothing was minted yet and Redis
    # holds no session anywhere.
    assert minted == []
    assert redis.store
    for value, _ttl in redis.store.values():
        _assert_no_session_token(value, minted=(f"fresh-{user.id}",))
    # The app receives a FRESH session minted at the exchange, for that user.
    got = _run(routes.exchange_desktop_login(routes.ExchangeRequest(login_id=login_id), runner=runner))
    assert got["access_token"] == f"fresh-{user.id}"
    assert minted == [str(user.id)]
    # Its lifetime counts from the browser's sign-in, never from the exchange,
    # and ends no later than the browser's session, under a session of its
    # own (no ``sid`` carried over).
    assert continued == [{"auth_time": 1_700_000_000, "exp": 1_700_028_800}]
    # And a second confirmation (a reloaded tab) is told it is too late.
    login_id2 = _run(routes.start_desktop_login(runner=runner, db=_FakeSession()))["login_id"]
    _run(svc.complete_login(_run(svc.load_login(login_id2)), "x"))
    with pytest.raises(HTTPException) as exc:
        _run(routes.complete_desktop_login(login_id2, request=None, user=user, db=db))
    assert exc.value.status_code == 409


def test_cancel_drops_only_the_callers_login(redis):
    owner, other = _runner("mine"), _runner("theirs")
    login_id = _run(routes.start_desktop_login(runner=owner, db=_FakeSession()))["login_id"]
    _run(routes.cancel_desktop_login(login_id, runner=other))
    assert _run(svc.load_login(login_id)) is not None
    _run(routes.cancel_desktop_login(login_id, runner=owner))
    assert _run(svc.load_login(login_id)) is None


# ── The SSO carry ────────────────────────────────────────────────────────────


def test_frontend_redirect_carries_the_handoff_in_the_fragment(monkeypatch):
    import app.services.mfa as mfa

    monkeypatch.setattr(mfa, "challenge_required", lambda user: False)
    monkeypatch.setattr(auth_sso, "create_access_token", lambda subject: "jwt")
    user = SimpleNamespace(id=uuid.uuid4())
    plain = auth_sso._build_frontend_redirect(user, provider="oidc")
    assert plain.endswith("/login#token=jwt")
    handed = auth_sso._build_frontend_redirect(user, provider="oidc", desktop_login="d" * 43)
    assert handed.endswith(f"/login#token=jwt&desktop_login={'d' * 43}")
    # Never a query string: the fragment is what keeps the token out of logs.
    assert "?" not in handed.split("/login", 1)[1]


def test_frontend_redirect_carries_the_handoff_through_the_second_factor(monkeypatch):
    import app.services.mfa as mfa

    monkeypatch.setattr(mfa, "challenge_required", lambda user: True)
    monkeypatch.setattr(mfa, "create_challenge_token", lambda subject, method: "challenge")
    user = SimpleNamespace(id=uuid.uuid4())
    handed = auth_sso._build_frontend_redirect(user, provider="saml", desktop_login="d" * 43)
    assert handed.endswith(f"/login#mfa_token=challenge&desktop_login={'d' * 43}")


def test_only_a_well_formed_handoff_id_travels_to_the_provider():
    assert auth_sso._desktop_login_from_query("d" * 43) == "d" * 43
    assert auth_sso._desktop_login_from_query("evil<script>") is None
    assert auth_sso._desktop_login_from_query(None) is None


def test_oidc_login_places_the_handoff_in_state(monkeypatch):
    import app.core.sso_helpers as helpers

    captured: dict = {}

    class _Client:
        def create_authorization_url(self, endpoint, **kwargs):
            captured.update(kwargs)
            return "https://idp.example/authorize", kwargs.get("state")

    async def _cfg(db):
        return SimpleNamespace(oidc_enabled=True)

    async def _disco(cfg):
        return {"authorization_endpoint": "https://idp.example/authorize"}

    monkeypatch.setattr(helpers, "get_sso_config", _cfg)
    monkeypatch.setattr(auth_sso, "_oidc_discovery_from_cfg", _disco)
    monkeypatch.setattr(auth_sso, "_oidc_client_from_cfg", lambda cfg: _Client())

    _run(auth_sso.oidc_login(desktop_login="d" * 43, db=None))
    assert captured["state"] == "desktop:" + "d" * 43

    captured.clear()
    _run(auth_sso.oidc_login(desktop_login=None, db=None))
    assert "state" not in captured  # an ordinary sign-in keeps authlib's own state

    captured.clear()
    _run(auth_sso.oidc_login(desktop_login="not valid", db=None))
    assert "state" not in captured


def test_saml_login_places_the_handoff_in_relay_state(monkeypatch):
    import app.core.sso_helpers as helpers

    captured: dict = {}

    class _Auth:
        def __init__(self, req, settings):
            pass

        def login(self, return_to=None):
            captured["return_to"] = return_to
            return "https://idp.example/sso"

    async def _cfg(db):
        return SimpleNamespace(saml_enabled=True)

    async def _settings(cfg):
        return {}

    import sys
    import types

    fake_module = types.ModuleType("onelogin.saml2.auth")
    fake_module.OneLogin_Saml2_Auth = _Auth
    monkeypatch.setitem(sys.modules, "onelogin", types.ModuleType("onelogin"))
    monkeypatch.setitem(sys.modules, "onelogin.saml2", types.ModuleType("onelogin.saml2"))
    monkeypatch.setitem(sys.modules, "onelogin.saml2.auth", fake_module)
    monkeypatch.setattr(helpers, "get_sso_config", _cfg)
    monkeypatch.setattr(auth_sso, "_build_saml_settings_from_cfg", _settings)

    _run(auth_sso.saml_login(desktop_login="d" * 43, db=None))
    assert captured["return_to"] == "desktop:" + "d" * 43
    _run(auth_sso.saml_login(desktop_login=None, db=None))
    assert captured["return_to"] is None


def test_frontend_login_page_wires_both_halves():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "frontend" / "src"
    login = (root / "pages" / "Login.tsx").read_text(encoding="utf-8")
    # In the browser: the callback's handoff id is read next to the token.
    assert '"desktop_login"' in login
    assert "DesktopHandoffStep" in login
    # In the app: an SSO button starts the browser flow instead of navigating.
    assert "BrowserLoginStep" in login
    assert 'setBrowserLogin("oidc")' in login and 'setBrowserLogin("saml")' in login
    bridge = (root / "lib" / "desktop.ts").read_text(encoding="utf-8")
    assert '"desktop_begin_browser_login"' in bridge
    assert '"desktop_cancel_browser_login"' in bridge
    assert 'DESKTOP_BROWSER_LOGIN_EVENT = "desktop-browser-login"' in bridge
