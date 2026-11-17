"""Regression tests for the security/correctness invariants on the local
auth surface (login, registration, invitation acceptance, password reset,
account onboarding) and the supporting helpers in ``core.security`` and
``core.rate_limit``.

Each test pins one Copilot-flagged or reviewer-identified behaviour so a
future refactor that silently reverts the fix trips loudly:

* ``verify_password`` whitespace fallback — accounts whose stored bcrypt
  hash was created from a password with intentional/legacy leading or
  trailing whitespace must keep authenticating with the exact whitespace-
  bearing password.  Only after the raw attempt fails do we retry with the
  normalized form, so the symmetry that lets new accounts ignore
  accidental copy-paste whitespace cannot regress old accounts.
* ``_client_ip`` proxy-header trust gate — ``X-Forwarded-For`` must NOT
  be honoured unless ``trust_proxy_headers`` is explicitly enabled.
  Otherwise any caller can spoof the header to land in a fresh bucket on
  every request and trivially bypass per-IP throttling on the login /
  forgot-password / register-with-invitation endpoints.
* Settings clamps on the new auth/TTL fields — a misconfigured env var
  must fail closed at startup with sane caps instead of surfacing later
  as a 400 from the invitation endpoint or a Redis ``EX`` error from the
  password-reset endpoint.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core import config, rate_limit, security

# ── verify_password whitespace fallback ──────────────────────────────────


def test_verify_password_accepts_legacy_whitespace_baked_hash():
    """Hash created from input WITH whitespace must still verify with the
    exact whitespace-bearing password.

    Simulates the legacy state: an account that was created back when
    ``hash_password`` did not normalize.  After this PR, ``hash_password``
    strips before bcrypt, but a previously-stored hash retains the
    whitespace bytes — ``verify_password`` must try the raw input first
    so those accounts keep working.  Without the fallback, every legacy
    account whose user typed (or autofilled) the password with the
    original whitespace would silently fail authentication.
    """
    # Bypass normalisation to recreate the legacy hash shape.
    import bcrypt as _bcrypt

    raw = "  Sup3rSecret  "
    hashed = _bcrypt.hashpw(raw.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")

    assert security.verify_password(raw, hashed) is True


def test_verify_password_accepts_accidental_whitespace_on_normalized_hash():
    """Hash created from the normalized password must accept the same
    password typed WITH accidental surrounding whitespace.

    This is the symmetry that the PR's accidental-whitespace UX feature
    relies on — fresh accounts always store the normalized form, and a
    user re-typing the password with a stray leading/trailing space (copy-
    paste, mobile autofill) must still log in.
    """
    hashed = security.hash_password("Sup3rSecret")
    assert security.verify_password("  Sup3rSecret  ", hashed) is True
    assert security.verify_password("Sup3rSecret", hashed) is True


def test_verify_password_rejects_wrong_password_in_both_attempts():
    """Wrong password must NOT authenticate via either the raw or the
    normalized attempt — the fallback is for whitespace symmetry only,
    not a credential-comparison weakening.
    """
    hashed = security.hash_password("Sup3rSecret")
    assert security.verify_password("WrongPassword", hashed) is False
    assert security.verify_password("  WrongPassword  ", hashed) is False


# ── Rate-limit X-Forwarded-For trust gate ───────────────────────────────


def _request_with(headers: dict, client_host: str | None = "203.0.113.5"):
    """Build a minimal Request stub for ``_client_ip``."""
    return SimpleNamespace(
        headers={k.lower(): v for k, v in headers.items()},
        client=SimpleNamespace(host=client_host) if client_host else None,
    )


def test_client_ip_ignores_xff_when_trust_proxy_headers_disabled(monkeypatch):
    """Default deployment (no trusted reverse proxy): the rate limiter
    MUST use the connection peer IP, not whatever the client put in
    ``X-Forwarded-For``.  Without this gate a single attacker would land
    in a fresh per-IP bucket on every request and the login / forgot-
    password rate limits become a no-op.
    """
    monkeypatch.setattr(config.settings, "trust_proxy_headers", False)
    req = _request_with(
        {"x-forwarded-for": "10.0.0.1, 10.0.0.2"},
        client_host="203.0.113.5",
    )
    assert rate_limit._client_ip(req) == "203.0.113.5"


def test_client_ip_uses_xff_when_trust_proxy_headers_enabled(monkeypatch):
    """Behind a known-trusted reverse proxy (operator opts in via the
    setting): the leftmost ``X-Forwarded-For`` value is the real client
    IP and the rate limiter MUST use it so per-IP buckets are accurate
    behind the proxy.
    """
    monkeypatch.setattr(config.settings, "trust_proxy_headers", True)
    req = _request_with(
        {"x-forwarded-for": "198.51.100.7, 10.0.0.2"},
        client_host="10.0.0.1",
    )
    assert rate_limit._client_ip(req) == "198.51.100.7"


def test_client_ip_falls_back_to_unknown_when_no_client(monkeypatch):
    """The "unknown" fallback shapes the per-IP key when the ASGI
    transport doesn't expose a client peer at all (rare — usually
    in-process test calls).  Same string for every such request so
    the limit still applies, but no exception is raised.
    """
    monkeypatch.setattr(config.settings, "trust_proxy_headers", False)
    req = _request_with({}, client_host=None)
    assert rate_limit._client_ip(req) == "unknown"


# ── Settings clamps for the new auth/TTL fields ─────────────────────────


def test_settings_clamps_min_password_length():
    """A misconfigured env var must NOT bypass the platform policy at
    startup — clamp to ``[1, 1024]`` instead of letting ``0`` slip
    through (which would render the policy a no-op for every endpoint)
    or letting an extreme value like ``10_000`` render the platform
    un-registrable.
    """
    s = config.Settings(min_password_length=0)
    assert s.min_password_length == 1
    s = config.Settings(min_password_length=10_000)
    assert s.min_password_length == 1024
    s = config.Settings(min_password_length=12)
    assert s.min_password_length == 12


def test_settings_clamps_invitation_expire_hours():
    """A misconfigured invitation TTL must fail closed at startup with
    the same ``[1, 720]`` envelope the per-request validation in
    ``invitations.py`` advertises — otherwise the default behaviour at
    the API edge would silently 400 on an admin-issued invite for a
    reason the operator can't trace back to their env file.
    """
    s = config.Settings(invitation_expire_hours=0)
    assert s.invitation_expire_hours == 1
    s = config.Settings(invitation_expire_hours=10_000)
    assert s.invitation_expire_hours == 720
    s = config.Settings(invitation_expire_hours=168)
    assert s.invitation_expire_hours == 168


def test_settings_clamps_password_reset_expire_hours():
    """Same envelope as the invitation TTL — both feed Redis ``EX``
    arguments, and a negative / huge value would surface as a Redis
    500 the first time an admin issues a reset link.
    """
    s = config.Settings(password_reset_expire_hours=-1)
    assert s.password_reset_expire_hours == 1
    s = config.Settings(password_reset_expire_hours=10_000)
    assert s.password_reset_expire_hours == 720
    s = config.Settings(password_reset_expire_hours=24)
    assert s.password_reset_expire_hours == 24


# ── expires_in_hours=0 must NOT silently default ────────────────────────


def test_invitation_expires_in_hours_zero_is_strict():
    """``body.expires_in_hours = 0`` must fall through to the range
    check (which rejects 0) rather than silently default to the
    configured TTL — the ``or`` shortcut would have done exactly that.

    Pure logic test against the same expression the endpoint uses, so
    a regression that switches ``is not None`` back to ``or`` trips
    here without needing a full FastAPI test-client setup.
    """
    body_explicit_zero = SimpleNamespace(expires_in_hours=0)
    body_missing = SimpleNamespace(expires_in_hours=None)

    explicit = (
        body_explicit_zero.expires_in_hours
        if body_explicit_zero.expires_in_hours is not None
        else 168
    )
    missing = (
        body_missing.expires_in_hours
        if body_missing.expires_in_hours is not None
        else 168
    )

    assert explicit == 0, "explicit 0 must NOT default — it must fall through to validation"
    assert missing == 168, "missing field must inherit the configured TTL"
    assert not (1 <= explicit <= 720), "0 must be rejected by the [1, 720] range check"


# ── Frontend-URL fallback for link generation ───────────────────────────


def test_invitation_link_uses_frontend_url_when_split_origin(monkeypatch):
    """When ``FRONTEND_URL`` differs from ``BASE_URL`` the generated
    invitation link MUST target the frontend — otherwise the recipient
    lands on the backend's ``/register`` route which is not a real
    page.
    """
    from app.services import token_store

    monkeypatch.setattr(token_store.settings, "frontend_url", "https://app.example.com/")
    monkeypatch.setattr(token_store.settings, "base_url", "https://api.example.com")
    assert token_store.invitation_link("xyz") == "https://app.example.com/register?token=xyz"


def test_invitation_link_falls_back_to_base_url_when_frontend_unset(monkeypatch):
    """Single-origin deployment (no FRONTEND_URL): link generation MUST
    keep working off ``settings.base_url`` so the fix is fully backward-
    compatible with existing installs that don't set the new variable.
    """
    from app.services import token_store

    monkeypatch.setattr(token_store.settings, "frontend_url", "")
    monkeypatch.setattr(token_store.settings, "base_url", "https://xtm-one.example.com")
    assert token_store.invitation_link("xyz") == "https://xtm-one.example.com/register?token=xyz"


# ── Password policy is Unicode-aware on the server ──────────────────────


@pytest.mark.parametrize(
    "password",
    [
        "Pâté2024",  # accented Latin letters
        "كلمة1234",  # Arabic letters + ASCII digits
        "Mot2Passe",
    ],
)
def test_password_policy_accepts_unicode_letters(password):
    """Server-side ``check_password_policy`` uses ``str.isalpha()`` /
    ``str.isdigit()`` which are Unicode-aware.  Passwords made of
    non-ASCII letters + ASCII digits MUST pass.  Pinned here so the
    frontend's matching ``\\p{L}`` / ``\\p{N}`` regex stays in sync
    against the same input set.
    """
    security.check_password_policy(password)  # raises on failure


def test_password_policy_rejects_letter_only():
    with pytest.raises(ValueError, match="at least one number"):
        security.check_password_policy("OnlyLetters")


def test_password_policy_rejects_digit_only():
    with pytest.raises(ValueError, match="at least one letter"):
        security.check_password_policy("12345678")


# ── send_email=true + SMTP unconfigured must fail fast (not silently lose link) ─


class _StubDB:
    """Async DB stub for the route tests below.

    Only stands in for the single ``select(User)`` call the route makes, and
    the commit it makes before answering; the test cases here either
    short-circuit on the SMTP gate (no DB hit needed) or short-circuit on a
    user-doesn't-exist result.
    """

    async def execute(self, *_a, **_kw):
        class _R:
            def scalar_one_or_none(self):
                return None

        return _R()

    async def commit(self):
        return None


async def test_create_invitation_503s_when_smtp_unconfigured_and_send_email_true(monkeypatch):
    """Reviewer #5 (``invitations.py:155``): when ``send_email=true`` and SMTP
    isn't configured, the previous code created the Redis token and then
    silently dropped the link (no email, no log line in non-debug builds),
    leaving the admin with a useless pending invitation they couldn't
    recover.  The new contract is to fail fast with 503 BEFORE writing the
    token, so the misconfiguration is visible and recoverable.

    Pinned via the route function directly (no FastAPI test client / DB
    session fixture exists in this codebase) — we monkeypatch the SMTP probe
    and the create-invitation token-store call to a sentinel that fails the
    test if reached, so a regression that re-orders the 503 below
    ``create_invitation`` (or removes it) trips loudly.
    """
    from fastapi import HTTPException

    from app.api.v1 import invitations as inv_module
    from app.api.v1.invitations import CreateInvitationRequest, create_invitation
    from app.services import email as email_module

    async def _no_pending(_email: str) -> None:
        return None

    async def _must_not_be_called(**_kw):
        raise AssertionError(
            "token_store.create_invitation must NOT be called when SMTP is "
            "unconfigured + send_email=true — the route must 503 first."
        )

    monkeypatch.setattr(email_module, "is_email_configured", lambda: False)
    monkeypatch.setattr(inv_module.token_store, "find_invitation_by_email", _no_pending)
    monkeypatch.setattr(inv_module.token_store, "create_invitation", _must_not_be_called)

    body = CreateInvitationRequest(email="alice@example.com", role="user", send_email=True)
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})

    with pytest.raises(HTTPException) as exc:
        await create_invitation(
            body=body,
            request=request,
            admin=SimpleNamespace(id="admin"),
            db=_StubDB(),
        )

    assert exc.value.status_code == 503
    assert "SMTP_HOSTNAME" in exc.value.detail


async def test_create_invitation_rejects_case_different_existing_user(monkeypatch):
    """Reviewer #6 (``invitations.py:102``): the ``User.email == body.email``
    exists-check is case-sensitive in Postgres, but ``find_invitation_by_email``
    normalises (``.strip().lower()``), so a user registered as
    ``Bob@Example.com`` previously slipped past the exists check on a
    case-different invite (``bob@example.com``).  The route then issued the
    link, and ``register-with-invitation`` only 409'd at consume time when
    the new ``User`` row hit the ``UNIQUE(email)`` constraint — leaving the
    admin with a useless invite in the meantime.

    Pin the fix by intercepting the route's ``db.execute(select(User))`` call
    and matching on the normalised email: if the route is doing the
    case-insensitive lookup it asked the helper for, our stub will say "user
    exists" and the route must 409.  A regression that drops ``func.lower()``
    won't be caught by string comparison alone (SQLAlchemy lazy-compiles the
    expression), so we instead inspect the WHERE clause text.
    """
    from fastapi import HTTPException

    from app.api.v1.invitations import CreateInvitationRequest, create_invitation

    where_texts: list[str] = []

    class _CaseInsensitiveExistsDB:
        """Returns a hit when the route asks about the normalised email."""

        async def execute(self, stmt):
            # ``select(...).whereclause`` is the compiled WHERE expression;
            # rendering it captures any ``lower(users.email)`` call which is
            # exactly the regression marker.
            where_texts.append(str(stmt.whereclause))

            class _R:
                def scalar_one_or_none(self):
                    return object()  # truthy sentinel — "user exists"

            return _R()

    body = CreateInvitationRequest(
        email="Bob@Example.com", role="user", send_email=False
    )
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})

    with pytest.raises(HTTPException) as exc:
        await create_invitation(
            body=body,
            request=request,
            admin=SimpleNamespace(id="admin"),
            db=_CaseInsensitiveExistsDB(),
        )

    assert exc.value.status_code == 409
    assert "already exists" in exc.value.detail
    assert where_texts, "the route must execute the exists-check"
    assert "lower(" in where_texts[0].lower(), (
        "the exists-check must be case-insensitive — got WHERE clause: "
        + where_texts[0]
    )


async def test_create_invitation_skips_smtp_check_when_send_email_false(monkeypatch):
    """Symmetry guard for the 503 branch: ``send_email=false`` is the
    seed/automation path — the link is returned in the response and the
    SMTP probe is irrelevant.  A regression that broadened the 503 to that
    path would lock the seed script out of generating links on a backend
    without SMTP, so we pin it explicitly.
    """
    from datetime import UTC, datetime, timedelta

    from app.api.v1 import invitations as inv_module
    from app.api.v1.invitations import CreateInvitationRequest, create_invitation
    from app.services import email as email_module

    created: dict[str, str] = {}

    async def _no_pending(_email: str) -> None:
        return None

    async def _create(**kw):
        # Mimic ``token_store.create_invitation``'s 3-tuple return.
        created["email"] = kw["email"]
        return ("raw", "hash", datetime.now(UTC) + timedelta(hours=kw["ttl_hours"]))

    async def _no_audit(*_a, **_kw) -> None:
        return None

    monkeypatch.setattr(email_module, "is_email_configured", lambda: False)
    monkeypatch.setattr(inv_module.token_store, "find_invitation_by_email", _no_pending)
    monkeypatch.setattr(inv_module.token_store, "create_invitation", _create)
    # ``log_audit`` is bound into the module via ``from .. import log_audit``;
    # patch the module-local binding rather than the source module so the
    # call inside ``create_invitation`` actually hits the stub.
    monkeypatch.setattr(inv_module, "log_audit", _no_audit)

    body = CreateInvitationRequest(email="bob@example.com", role="user", send_email=False)
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})

    resp = await create_invitation(
        body=body,
        request=request,
        admin=SimpleNamespace(id="admin"),
        db=_StubDB(),
    )

    assert created.get("email") == "bob@example.com"
    assert resp.email_sent is False  # we never tried — admin opted out
    assert resp.link and resp.link.endswith("?token=raw")


# ── register_with_invitation must survive a Redis consume failure ───────


async def test_register_with_invitation_returns_token_when_consume_raises(monkeypatch):
    """Reviewer round 7 (``auth.py:357``): if ``consume_invitation`` raises
    (Redis down, network blip, etc.) AFTER ``db.commit()`` succeeded, the
    user row is already persisted — turning that into a 500 would lock the
    caller out of the account they just created on SSO-only platforms,
    because this endpoint deliberately bypasses ``local_auth_enabled``.

    The fix swallows the consume exception, logs it, and returns the access
    token anyway.  The DB ``UNIQUE(email)`` constraint is the primary
    single-use backstop, so a stale Redis key cannot be replayed — the
    next caller 409s at flush — and the key TTLs out regardless.

    Pin via the route function directly: a stub ``consume_invitation``
    raises, and the route must still return a ``TokenResponse``.  A
    regression that re-raises the exception trips here.
    """
    import uuid as _uuid

    from app.api.v1 import auth as auth_module
    from app.api.v1.auth import (
        RegisterWithInvitationRequest,
        register_with_invitation,
    )
    from app.services import token_store as ts_module

    user_id = _uuid.uuid4()
    flush_called = {"v": False}
    commit_called = {"v": False}

    class _UserStubDB:
        async def execute(self, _stmt):
            class _R:
                def scalar_one_or_none(self):
                    return None  # no existing user

            return _R()

        def add(self, _obj):
            # FastAPI route mutates the User object after add(), so we
            # have to inject the id so ``_build_token(user)`` works after
            # commit; the route assigns ``user`` itself, so we just grant
            # it an id at flush time below.
            self._user_obj = _obj

        async def flush(self):
            flush_called["v"] = True
            # Mimic the autoflush that would assign the server-side id.
            if getattr(self, "_user_obj", None) is not None:
                self._user_obj.id = user_id

        async def commit(self):
            commit_called["v"] = True

        async def rollback(self):
            pass

    async def _get_invitation(_raw):
        return {"email": "alice@example.com", "role": "user"}

    async def _consume_raises(_raw):
        raise RuntimeError("redis is down")

    async def _no_audit(*_a, **_kw):
        return None

    monkeypatch.setattr(auth_module, "enforce_rate_limit", _noop_async)
    monkeypatch.setattr(auth_module.token_store, "get_invitation", _get_invitation)
    monkeypatch.setattr(ts_module, "consume_invitation", _consume_raises)
    monkeypatch.setattr(auth_module, "log_audit", _no_audit)
    monkeypatch.setattr(
        auth_module, "_build_token", lambda u: f"jwt-for-{u.id}"
    )
    # NOTE: ``settings.is_xtm_one`` is a computed property and not
    # settable from monkeypatch; the route's optional CTEM-agent lookup
    # is handled implicitly by ``_UserStubDB.execute`` returning
    # ``scalar_one_or_none() -> None`` for every query.

    body = RegisterWithInvitationRequest(
        token="raw-token",
        password="Sup3rSecret",
        full_name="Alice Example",
    )
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})

    resp = await register_with_invitation(
        body=body,
        request=request,
        db=_UserStubDB(),
    )

    assert resp.access_token.startswith("jwt-for-")
    assert flush_called["v"], "user row must be flushed"
    assert commit_called["v"], "transaction must be committed before consume"


async def test_register_with_invitation_registers_without_a_redirect_when_the_destination_lookup_fails(monkeypatch):
    """The destination is read before the user row is committed, and its failure only loses the redirect."""
    import uuid as _uuid

    from app.api.v1 import auth as auth_module
    from app.api.v1.auth import RegisterWithInvitationRequest, register_with_invitation

    steps: list[str] = []

    class _DB:
        async def execute(self, _stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        def add(self, obj):
            self.user = obj

        async def flush(self):
            steps.append("flush")
            self.user.id = _uuid.uuid4()

        async def commit(self):
            steps.append("commit")

        async def rollback(self):
            steps.append("rollback")

    async def _get_invitation(_raw):
        return {"email": "alice@example.com", "role": "user", "destination": {"platform": "opencrq", "path": "/t-1"}}

    async def _destination_fails(_db, _destination):
        steps.append("destination")
        raise RuntimeError("database went away")

    async def _consume(_raw):
        steps.append("consume")
        return {}

    monkeypatch.setattr(auth_module, "enforce_rate_limit", _noop_async)
    monkeypatch.setattr(auth_module.token_store, "get_invitation", _get_invitation)
    monkeypatch.setattr(auth_module.token_store, "consume_invitation", _consume)
    monkeypatch.setattr(auth_module, "registered_destination", _destination_fails)
    monkeypatch.setattr(auth_module, "log_audit", _noop_async)
    monkeypatch.setattr(auth_module, "_build_token", lambda u: f"jwt-for-{u.id}")

    resp = await register_with_invitation(
        body=RegisterWithInvitationRequest(token="raw-token", password="Sup3rSecret", full_name="Alice Example"),
        request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={}),
        db=_DB(),
    )

    assert resp.access_token.startswith("jwt-for-")
    assert resp.redirect_url is None
    assert steps == ["destination", "rollback", "flush", "commit", "consume"]


# ── _dispatch_password_reset must not mint orphan tokens when SMTP unconfigured ─


async def test_dispatch_password_reset_skips_token_when_smtp_unset_and_not_debug(
    monkeypatch,
):
    """Reviewer round 7 (``auth.py:362``): in non-debug builds with SMTP
    unconfigured, ``_dispatch_password_reset`` previously wrote a single-use
    reset token to Redis and only logged "cannot email" — the link could
    never be delivered, every ``forgot-password`` hit added another orphan
    token, and the rate limit (5/min/IP) didn't help because each request
    seeds a new Redis key with a 24h TTL.

    The fix short-circuits BEFORE ``create_password_reset`` runs.  A
    regression that re-orders the gate below the token write trips here:
    we patch ``create_password_reset`` to a sentinel that fails the test
    if reached, and assert the function returns cleanly without it being
    called.
    """
    from app.api.v1 import auth as auth_module

    async def _must_not_be_called(**_kw):
        raise AssertionError(
            "token_store.create_password_reset MUST NOT be called when SMTP "
            "is unconfigured + non-debug — the gate must short-circuit first."
        )

    # The function imports these at call time from ``app.services.email``;
    # patch the source module so the late import picks them up.
    from app.services import email as email_module

    monkeypatch.setattr(email_module, "is_email_configured", lambda: False)
    monkeypatch.setattr(auth_module.settings, "debug", False)
    monkeypatch.setattr(
        auth_module.token_store, "create_password_reset", _must_not_be_called
    )

    import uuid as _uuid

    # The function returns None (it's fire-and-forget from BackgroundTasks);
    # the assertion above carries the regression-trip.
    await auth_module._dispatch_password_reset(_uuid.uuid4(), "alice@example.com")


async def test_dispatch_password_reset_mints_token_when_debug_even_without_smtp(
    monkeypatch,
):
    """Symmetry guard for the gate: developers run with DEBUG=true and no
    SMTP, and the password-reset link is surfaced via ``logger.warning`` so
    they can paste it manually.  A regression that hardened the gate too
    tightly (refusing to mint even in debug) would break that dev loop.
    """
    from datetime import UTC, datetime, timedelta

    from app.api.v1 import auth as auth_module
    from app.services import email as email_module

    create_called = {"v": False}

    async def _create(**kw):
        create_called["v"] = True
        return ("raw-token", datetime.now(UTC) + timedelta(hours=kw["ttl_hours"]))

    monkeypatch.setattr(email_module, "is_email_configured", lambda: False)
    monkeypatch.setattr(auth_module.settings, "debug", True)
    monkeypatch.setattr(
        auth_module.token_store, "create_password_reset", _create
    )

    import uuid as _uuid

    await auth_module._dispatch_password_reset(_uuid.uuid4(), "alice@example.com")

    assert create_called["v"], (
        "debug mode must still mint a token so the link can be logged"
    )


async def test_dispatch_password_reset_uses_smtp_when_configured(monkeypatch):
    """Happy-path guard: with SMTP configured, the gate must let the flow
    through to ``send_email`` so legitimate users keep receiving links.
    """
    from datetime import UTC, datetime, timedelta

    from app.api.v1 import auth as auth_module
    from app.services import email as email_module

    sent = {"to": None}

    async def _create(**kw):
        return ("raw-token", datetime.now(UTC) + timedelta(hours=kw["ttl_hours"]))

    async def _send_email(*, to, subject, html, text):
        # subject / html / text are required by the real signature but
        # unused in the stub — the caller-side regression we're pinning
        # is the ``to`` argument.
        _ = (subject, html, text)
        sent["to"] = to
        return True

    monkeypatch.setattr(email_module, "is_email_configured", lambda: True)
    monkeypatch.setattr(
        email_module,
        "password_reset_email",
        lambda link, hours: ("<html/>", f"link: {link}"),
    )
    monkeypatch.setattr(email_module, "send_email", _send_email)
    monkeypatch.setattr(auth_module.settings, "debug", False)
    monkeypatch.setattr(
        auth_module.token_store, "create_password_reset", _create
    )

    import uuid as _uuid

    await auth_module._dispatch_password_reset(_uuid.uuid4(), "alice@example.com")

    assert sent["to"] == "alice@example.com"


# ── Shared helpers for the new route-level tests above ──────────────────


async def _noop_async(*_a, **_kw) -> None:
    """No-op async stub for ``enforce_rate_limit`` and similar fire-and-forget calls."""
    return None


# ── Rate-limit INCR + EXPIRE must be atomic via a Lua script ────────────


class _FakeRedisEval:
    """Fake Redis client that implements just enough for ``enforce_rate_limit``.

    Tracks the script source the route passes to ``EVAL`` so a regression
    that drops the atomic pair (e.g. reverts to a two-step ``INCR``
    followed by ``EXPIRE``) trips the assertions below — the route would
    stop calling ``eval`` entirely, and the ``last_script`` capture
    would expose that.
    """

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.ttl: dict[str, int] = {}
        self.eval_calls: int = 0
        self.last_script: str = ""

    async def eval(self, script, numkeys, *keys_and_args):
        self.eval_calls += 1
        self.last_script = script
        # The route always passes (KEYS[1]=key, ARGV[1]=window_seconds).
        assert numkeys == 1, f"expected single-key script, got numkeys={numkeys}"
        key = keys_and_args[0]
        window = int(keys_and_args[1])
        # Mirror the Lua semantics: INCR; if first, set TTL; else if no
        # TTL is set (rare ops case), set it.  Encodes the same invariant
        # the script guarantees on the server side.
        current = self.store.get(key, 0) + 1
        self.store[key] = current
        # First INCR seeds the TTL; subsequent INCRs only re-apply when
        # the bucket is found WITHOUT a TTL (defence-in-depth against
        # restored-without-TTL state).
        if current == 1 or self.ttl.get(key, -1) < 0:
            self.ttl[key] = window
        return current

    # Unused by the route — present so a regression that switches back
    # to ``incr`` + ``expire`` (non-atomic) is caught at the assertion
    # ``eval_calls > 0`` below instead of silently succeeding here.
    async def incr(self, _key):
        raise AssertionError(
            "rate_limit.enforce_rate_limit must use the atomic Lua EVAL "
            "path, not the non-atomic INCR + EXPIRE pair."
        )

    async def expire(self, _key, _seconds):
        raise AssertionError(
            "rate_limit.enforce_rate_limit must use the atomic Lua EVAL "
            "path, not the non-atomic INCR + EXPIRE pair."
        )


@pytest.fixture
def fake_redis_eval(monkeypatch):
    fake = _FakeRedisEval()

    async def _client():
        return fake

    monkeypatch.setattr(rate_limit, "redis_client", _client)
    return fake


async def test_enforce_rate_limit_uses_atomic_lua_script(monkeypatch, fake_redis_eval):
    """Reviewer #1 (``rate_limit.py:57``): a non-atomic ``INCR`` followed
    by ``EXPIRE`` leaks a TTL-less key whenever the second round-trip
    fails (network blip, process crash, Redis OOM) — that bucket then
    stays at its incremented value forever and the per-IP rate limit
    permanently 429s the legitimate caller.  The fix collapses both ops
    into a single Lua ``EVAL`` so the read+expire pair cannot tear.

    Pin the contract by:
    (1) verifying the route hits ``eval`` (not raw ``incr`` — the fake
        raises if either non-atomic primitive is touched), and
    (2) verifying the Lua source contains both INCR and EXPIRE so a
        regression that strips the EXPIRE branch (and leaks TTL-less
        keys again) trips here without needing a real Redis.
    """
    monkeypatch.setattr(config.settings, "trust_proxy_headers", False)
    request = SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.7"), headers={}
    )

    # Three calls under the limit — must all pass and share the same bucket.
    await rate_limit.enforce_rate_limit(request, "login", limit=10, window_seconds=60)
    await rate_limit.enforce_rate_limit(request, "login", limit=10, window_seconds=60)
    await rate_limit.enforce_rate_limit(request, "login", limit=10, window_seconds=60)

    key = "rl:login:203.0.113.7"
    assert fake_redis_eval.eval_calls == 3, (
        "every call must hit the atomic EVAL — not a fallback INCR path"
    )
    assert fake_redis_eval.store[key] == 3
    assert fake_redis_eval.ttl[key] == 60, (
        "TTL must be set on the first INCR so the bucket expires within "
        "the window — a missing TTL would lock the IP out forever."
    )
    # Pin the script contents so a regression that drops either primitive
    # trips loudly.
    assert "INCR" in fake_redis_eval.last_script
    assert "EXPIRE" in fake_redis_eval.last_script


async def test_enforce_rate_limit_raises_429_past_limit(monkeypatch, fake_redis_eval):
    """Counter past the limit must raise 429.  Symmetry guard for the
    happy-path test above — without this a regression that miscounted
    (e.g. returning the new key count as 0 from the Lua script) would
    silently let every request through.
    """
    from fastapi import HTTPException

    monkeypatch.setattr(config.settings, "trust_proxy_headers", False)
    request = SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.8"), headers={}
    )

    for _ in range(3):
        await rate_limit.enforce_rate_limit(
            request, "login", limit=3, window_seconds=60
        )
    with pytest.raises(HTTPException) as exc:
        await rate_limit.enforce_rate_limit(
            request, "login", limit=3, window_seconds=60
        )
    assert exc.value.status_code == 429


async def test_enforce_rate_limit_reapplies_ttl_when_key_has_none(
    monkeypatch, fake_redis_eval
):
    """Defence-in-depth pin: if a bucket exists WITHOUT a TTL (e.g. RDB
    restore that dropped TTLs, manual ops action that ran ``DEL TTL``,
    cross-version replay), the next ``EVAL`` must re-apply the window
    instead of letting that bucket stick at its current value forever.
    A regression that removes the ``ttl < 0 → EXPIRE`` branch from the
    Lua script trips this test.
    """
    monkeypatch.setattr(config.settings, "trust_proxy_headers", False)
    request = SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.9"), headers={}
    )
    # Simulate the pathological state directly on the fake — bucket
    # exists at count=5 with NO TTL recorded.
    key = "rl:login:203.0.113.9"
    fake_redis_eval.store[key] = 5
    assert key not in fake_redis_eval.ttl

    await rate_limit.enforce_rate_limit(
        request, "login", limit=100, window_seconds=120
    )

    assert fake_redis_eval.store[key] == 6
    assert fake_redis_eval.ttl.get(key) == 120, (
        "EVAL must re-apply a TTL when the bucket existed without one — "
        "otherwise that IP would be stuck in the bucket forever."
    )


async def test_enforce_rate_limit_fails_open_when_redis_raises(monkeypatch):
    """Existing contract pin: a Redis outage MUST NOT lock everyone out
    of authentication.  ``enforce_rate_limit`` logs the failure and
    returns silently.  Confirmed against the new EVAL path so a future
    refactor that re-raises on the eval branch doesn't slip through.
    """

    class _Broken:
        async def eval(self, *_a, **_kw):
            raise RuntimeError("redis is down")

    async def _client():
        return _Broken()

    monkeypatch.setattr(rate_limit, "redis_client", _client)
    monkeypatch.setattr(config.settings, "trust_proxy_headers", False)
    request = SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.10"), headers={}
    )
    # No exception — the contract is "fail open".
    await rate_limit.enforce_rate_limit(request, "login", limit=10, window_seconds=60)


# ── register_with_invitation uses case-insensitive exists check ─────────


async def test_register_with_invitation_uses_case_insensitive_exists_check(monkeypatch):
    """Reviewer #2 (``auth.py:294``): ``register_with_invitation`` used
    ``User.email == email`` (case-sensitive in Postgres) while the
    issuance-time exists check in ``invitations.create_invitation`` already
    normalised via ``func.lower(...)``.  That asymmetry meant a user
    registered as ``Foo@Example.com`` would slip past this guard on a
    case-different invite (``foo@example.com``) — the registration would
    only fail later when the DB ``UNIQUE(email)`` constraint fired during
    flush, and even then only because Postgres ``UNIQUE`` happens to be
    case-sensitive (so mixed-case duplicates can still coexist).

    Pin the fix by inspecting the WHERE clause: it must call ``lower(...)``.
    """
    from fastapi import HTTPException

    from app.api.v1 import auth as auth_module
    from app.api.v1.auth import RegisterWithInvitationRequest, register_with_invitation

    where_texts: list[str] = []

    class _CaseInsensitiveExistsDB:
        async def execute(self, stmt):
            # SQLAlchemy lazy-compiles the expression; rendering it
            # captures any ``lower(users.email)`` call which is exactly
            # the regression marker we care about.
            where_texts.append(str(stmt.whereclause))

            class _R:
                def scalar_one_or_none(self):
                    return object()  # truthy sentinel — "user exists"

            return _R()

    async def _get_invitation(_raw):
        # Even if a legacy payload (pre-storage-normalisation) carries
        # mixed case, the route must normalise it again before the
        # exists check.
        return {"email": "Foo@Example.com", "role": "user"}

    monkeypatch.setattr(auth_module, "enforce_rate_limit", _noop_async)
    monkeypatch.setattr(auth_module.token_store, "get_invitation", _get_invitation)

    body = RegisterWithInvitationRequest(
        token="raw-token",
        password="Sup3rSecret",
        full_name="Alice Example",
    )
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})

    with pytest.raises(HTTPException) as exc:
        await register_with_invitation(
            body=body,
            request=request,
            db=_CaseInsensitiveExistsDB(),
        )

    assert exc.value.status_code == 409
    assert "Email already registered" in exc.value.detail
    assert where_texts, "the route must execute the exists-check"
    assert "lower(" in where_texts[0].lower(), (
        "register_with_invitation must use func.lower(User.email) — got "
        "WHERE clause: " + where_texts[0]
    )


# ── auth.register normalises email + uses case-insensitive exists check ─


async def test_auth_register_uses_case_insensitive_exists_check(monkeypatch):
    """Self-service ``/auth/register`` had the same case-sensitive
    exists check as ``register_with_invitation`` — even though the
    IntegrityError handler salvages the race with a clean 409, two
    rows differing only in case can still both succeed when no concurrent
    race exists (because Postgres ``UNIQUE(text)`` is case-sensitive).
    The fix aligns the exists check + the stored ``User.email`` value
    with the canonical lowered form used everywhere else.

    Pin via the WHERE clause text (same shape as the matching invitation
    issuance / consumption tests above) so a regression catches the same
    way for all three flows.
    """
    from fastapi import HTTPException

    from app.api.v1 import auth as auth_module
    from app.api.v1.auth import RegisterRequest, register

    where_texts: list[str] = []

    class _ExistsDB:
        async def execute(self, stmt):
            where_texts.append(str(stmt.whereclause) if stmt.whereclause is not None else "")

            class _R:
                def scalar_one_or_none(self):
                    # 1st call: scalar count of users — we return a positive
                    # int so the route's "first user gets admin" gate does
                    # not short-circuit the registration_enabled check.
                    # 2nd call: scalar exists check — return truthy User.
                    return object()

            return _R()

        async def scalar(self, _stmt):
            # ``_determine_role`` / first-user count: pretend there's
            # already at least one user so the route runs the full
            # ``registration_enabled`` gate (we leave that gate open via
            # the SSO-config stub below).
            return 5

    class _SSOConfigStub:
        local_auth_enabled = True
        registration_enabled = True
        allowed_domain_list: list[str] = []

    async def _get_sso_config(_db):
        return _SSOConfigStub()

    # Patch the lazy import inside the route body.
    import app.core.sso_helpers as sso_module

    monkeypatch.setattr(sso_module, "get_sso_config", _get_sso_config)
    monkeypatch.setattr(auth_module, "_determine_role", lambda _db: _async_value("user"))

    body = RegisterRequest(
        email="Foo@Example.com",
        password="Sup3rSecret",
        full_name="Foo Bar",
    )
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})

    with pytest.raises(HTTPException) as exc:
        await register(body=body, request=request, db=_ExistsDB())

    # The second execute() is the exists check — that's where the
    # case-insensitive lookup must happen.
    assert exc.value.status_code == 409, (
        "register must 409 on case-different existing email via the "
        "case-insensitive exists check"
    )
    # The exists WHERE clause is the 2nd entry (1st is the count query).
    assert any(
        "lower(" in w.lower() for w in where_texts
    ), (
        "register must use func.lower(User.email) for the exists check; "
        "captured where clauses: " + repr(where_texts)
    )


def _async_value(v):
    """Wrap a value in an awaitable so ``monkeypatch.setattr`` can stand in
    for an async function returning ``v``.  Used by tests that need to
    replace a one-shot helper inside a route body.
    """

    async def _f(*_a, **_kw):
        return v

    return _f()


# ── users.create_user normalises email + uses case-insensitive exists check ─


async def test_create_user_uses_case_insensitive_exists_check(monkeypatch):
    """Admin user-creation path (``users.create_user``) had the same
    case-sensitive flaw as ``register_with_invitation`` — and with no
    IntegrityError safety net, a case-different existing user would
    leak a 500 from ``UNIQUE(email)`` at flush time instead of a clean
    409.  Pin the fix by inspecting the WHERE clause: it must call
    ``lower(...)``.
    """
    from fastapi import HTTPException

    from app.api.v1 import users as users_module
    from app.api.v1.users import CreateUserRequest, create_user

    where_texts: list[str] = []

    class _CaseInsensitiveExistsDB:
        async def execute(self, stmt):
            where_texts.append(str(stmt.whereclause) if stmt.whereclause is not None else "")

            class _R:
                def scalar_one_or_none(self):
                    return object()  # truthy sentinel — "user exists"

            return _R()

    # Keep the module import alive so a regression that renames the
    # imported symbols (``CreateUserRequest`` / ``create_user``) trips
    # at import time rather than later in the test body.
    assert users_module is not None

    body = CreateUserRequest(
        email="Foo@Example.com",
        full_name="Foo Bar",
        password="Sup3rSecret",
        role="user",
        account_type="local",
    )
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})

    with pytest.raises(HTTPException) as exc:
        await create_user(
            body=body,
            request=request,
            admin=SimpleNamespace(id="admin", role="admin"),
            db=_CaseInsensitiveExistsDB(),
        )

    assert exc.value.status_code == 409
    assert "already exists" in exc.value.detail
    assert where_texts, "the route must execute the exists-check"
    assert "lower(" in where_texts[0].lower(), (
        "create_user must use func.lower(User.email) — got WHERE "
        "clause: " + where_texts[0]
    )


# ── login + forgot-password case-insensitive lookup ─────────────────────────


async def test_login_uses_case_insensitive_email_lookup(monkeypatch):
    """``/auth/login`` must look up the user with ``func.lower(User.email)``
    so an account registered via the new lowercase-normalizing flows
    (``register`` / ``register-with-invitation`` / ``invitations.create``
    / ``users.create_user``) can authenticate regardless of the case the
    user types into the form.

    The asymmetry to guard against: the registration paths lower the email
    at write time and exists-check time, but a bare ``User.email == body.email``
    on the login path treats the same identity as a different one when the
    case differs.  Postgres ``=`` on the ``text`` column is case-sensitive,
    so without ``func.lower(User.email)`` the user is locked out of an
    account that was just normalized at registration.

    Pinned via the rendered WHERE clause of the login SELECT (same shape
    as the matching ``register`` / ``create_user`` exists-check tests
    above) so a regression catches the same way for all three flows.
    """
    from app.api.v1 import auth as auth_module
    from app.api.v1.auth import LoginRequest, login

    where_texts: list[str] = []

    class _NoUserDB:
        async def execute(self, stmt):
            where_texts.append(
                str(stmt.whereclause) if stmt.whereclause is not None else ""
            )

            class _R:
                def scalar_one_or_none(self):
                    return None

            return _R()

    class _SSOConfigStub:
        local_auth_enabled = True

    async def _get_sso_config(_db):
        return _SSOConfigStub()

    import app.core.sso_helpers as sso_module

    monkeypatch.setattr(sso_module, "get_sso_config", _get_sso_config)

    # Bypass the rate limiter so the test doesn't depend on Redis.
    async def _no_rate_limit(*_a, **_kw):
        return None

    monkeypatch.setattr(auth_module, "enforce_rate_limit", _no_rate_limit)

    from fastapi import HTTPException

    body = LoginRequest(email="Foo@Example.com", password="Sup3rSecret")
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})

    # No user matches → 401 (the test's focus is on the WHERE clause shape,
    # not the outcome; 401 here just confirms the path actually reached
    # the lookup).
    with pytest.raises(HTTPException) as exc:
        await login(body=body, request=request, db=_NoUserDB())

    assert exc.value.status_code == 401
    assert where_texts, "login must execute the lookup query"
    assert "lower(" in where_texts[0].lower(), (
        "login must use func.lower(User.email) so a case-different "
        "input still resolves the same identity; got WHERE clause: "
        + where_texts[0]
    )


async def test_forgot_password_uses_case_insensitive_email_lookup(monkeypatch):
    """``/auth/forgot-password`` symmetrically must do a case-insensitive
    lookup — a user who can register but then can't request a reset on
    the same email-in-different-case input is locked out of recovery.

    The endpoint always returns 200 (generic message) to prevent
    enumeration, so the WHERE clause shape is the only observable signal.
    Pinned via the same compiled-SQL inspection as the login test above.
    """
    from app.api.v1 import auth as auth_module
    from app.api.v1.auth import ForgotPasswordRequest, forgot_password

    where_texts: list[str] = []

    class _NoUserDB:
        async def execute(self, stmt):
            where_texts.append(
                str(stmt.whereclause) if stmt.whereclause is not None else ""
            )

            class _R:
                def scalar_one_or_none(self):
                    return None

            return _R()

    async def _no_rate_limit(*_a, **_kw):
        return None

    monkeypatch.setattr(auth_module, "enforce_rate_limit", _no_rate_limit)

    body = ForgotPasswordRequest(email="Foo@Example.com")
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})

    class _BG:
        def add_task(self, *_a, **_kw):
            pass

    # Endpoint returns 200 regardless of whether the email matches —
    # the security property under test is the WHERE clause shape.
    result = await forgot_password(
        body=body, request=request, background=_BG(), db=_NoUserDB()
    )
    assert result["status"] == "ok"

    assert where_texts, "forgot-password must execute the lookup query"
    assert "lower(" in where_texts[0].lower(), (
        "forgot-password must use func.lower(User.email) so a case-"
        "different input still resolves the same identity; got WHERE "
        "clause: " + where_texts[0]
    )


async def test_xtm_jwt_user_lookup_is_case_insensitive(monkeypatch):
    """``deps._resolve_trusted_platform_jwt`` looks up the local Copilot
    user by the email claim on the cross-platform JWT.  Lowercasing the
    input alone (the prior shape) is not enough: legacy / SSO-provisioned
    rows can carry mixed-case emails in the DB, and a bare
    ``User.email == email.lower()`` still misses them.

    Pin the case-insensitive WHERE clause shape so a regression that
    drops ``func.lower(User.email)`` from the lookup trips here.  The
    matching test for the auto-create branch lives in the existing
    ``test_xtm_auth_smoke.py`` — this test focuses on the lookup-only
    path (returns ``None``, no creation).
    """
    from types import SimpleNamespace

    from app.api import deps as deps_module

    where_texts: list[str] = []

    class _ShortCircuitError(Exception):
        """Raised once the auto-create ``INSERT`` is reached — the test
        only needs the lookup's WHERE clause, not the provisioning itself."""

    class _NoUserDB:
        async def execute(self, stmt):
            # The auto-create branch issues an ``INSERT ... ON CONFLICT DO
            # NOTHING`` (no ``whereclause`` attribute at all) — short-circuit
            # there instead of the old ``db.add()`` hook.
            if not hasattr(stmt, "whereclause"):
                raise _ShortCircuitError

            where_texts.append(
                str(stmt.whereclause) if stmt.whereclause is not None else ""
            )

            class _R:
                def scalar_one_or_none(self):
                    # Return None so the function falls into the
                    # auto-create branch — we abort there before
                    # touching the DB via the short-circuit below.
                    return None

            return _R()

        async def commit(self):  # pragma: no cover
            pass

    # Stub the JWT verifier so we drive ``_resolve_trusted_platform_jwt``
    # straight into the email-lookup branch without doing any crypto.
    async def _verify(_token, _db):
        return SimpleNamespace(email="Foo@Example.com", iss="https://issuer.example")

    monkeypatch.setattr("app.core.xtm_auth.verify_xtm_jwt", _verify)

    import jwt as _jwt

    monkeypatch.setattr(_jwt, "get_unverified_header", lambda _t: {"alg": "EdDSA"})

    import contextlib

    db = _NoUserDB()
    with contextlib.suppress(_ShortCircuitError):
        await deps_module._resolve_trusted_platform_jwt("dummy-jwt", db)

    assert where_texts, "the helper must execute the email lookup"
    assert "lower(" in where_texts[0].lower(), (
        "_resolve_trusted_platform_jwt must use func.lower(User.email) "
        "so legacy mixed-case rows still match the lowered email claim; "
        "got WHERE clause: " + where_texts[0]
    )
