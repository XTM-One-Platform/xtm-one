"""Tests for TOTP two-factor authentication.

Split in two halves:

* **Service invariants** (``app.services.mfa``) — the parts an attacker probes:
  the replay guard, the drift window, single-use recovery codes, and the fact
  that a challenge token is not a session token. These are pure functions, so
  they are tested directly with no harness.
* **Endpoint contract** (``app.api.v1.auth``) — that ``/auth/login`` withholds
  the session when MFA is on, that ``/auth/mfa/verify`` issues one only against
  a valid code, and that SSO-only accounts are refused enrollment. Follows the
  repo's fake-session convention (FastAPI ``TestClient`` + dependency
  overrides); the backend suite has no real-DB harness.

The replay test is the one worth keeping honest: a 6-digit code stays valid for
its whole 30-second step plus the drift window, so without the guard an observed
code is reusable for up to 90 seconds.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import jwt
import pyotp
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import auth as auth_api
from app.core.config import settings
from app.core.database import get_db
from app.services import mfa

# ---------------------------------------------------------------------------
# Service: TOTP verification
# ---------------------------------------------------------------------------


def _code_for(secret: str, step: int) -> str:
    return pyotp.TOTP(
        secret, digits=mfa.TOTP_DIGITS, interval=mfa.TOTP_PERIOD_SECONDS
    ).generate_otp(step)


def test_verify_totp_accepts_current_code_and_reports_its_step():
    secret = mfa.generate_secret()
    now = time.time()
    step = mfa.current_step(now)

    assert mfa.verify_totp(secret, _code_for(secret, step), at=now) == step


def test_verify_totp_rejects_a_replayed_step():
    """The core replay guard: a step already spent can never be spent again.

    Without this, anyone who observes a code — over a shoulder, through a
    phishing proxy, in an over-eager log line — can reuse it for the rest of its
    window against an endpoint that has already accepted it once.
    """
    secret = mfa.generate_secret()
    now = time.time()
    step = mfa.current_step(now)
    code = _code_for(secret, step)

    assert mfa.verify_totp(secret, code, at=now) == step
    assert mfa.verify_totp(secret, code, last_used_step=step, at=now) is None


def test_verify_totp_rejects_every_step_at_or_below_the_watermark():
    """A stale code from BEFORE the last accepted one is refused too.

    The guard is ``<=``, not ``==``: an attacker replaying an older code that
    still falls inside the drift window must not slip past just because the
    watermark has since moved forward.
    """
    secret = mfa.generate_secret()
    now = time.time()
    step = mfa.current_step(now)
    previous = _code_for(secret, step - 1)

    assert mfa.verify_totp(secret, previous, last_used_step=step, at=now) is None


def test_verify_totp_tolerates_one_step_of_clock_drift_either_side():
    """A phone whose clock never syncs must still be able to sign in."""
    secret = mfa.generate_secret()
    now = time.time()
    step = mfa.current_step(now)

    assert mfa.verify_totp(secret, _code_for(secret, step - 1), at=now) == step - 1
    assert mfa.verify_totp(secret, _code_for(secret, step + 1), at=now) == step + 1


def test_verify_totp_rejects_a_code_outside_the_drift_window():
    secret = mfa.generate_secret()
    now = time.time()
    step = mfa.current_step(now)

    assert mfa.verify_totp(secret, _code_for(secret, step - 2), at=now) is None
    assert mfa.verify_totp(secret, _code_for(secret, step + 2), at=now) is None


@pytest.mark.parametrize(
    "code",
    [
        "",
        "12345",
        "1234567",
        "abcdef",
        "12 34 5",
        None,
        # Non-ASCII digits NFKC keeps as they are (Arabic-Indic): ``\d`` matched
        # them and the constant-time comparison then raised on the non-ASCII
        # ``str``.
        "\u0661\u0662\u0663\u0664\u0665\u0666",
        "12345\u0666",
        # Full-width digits fold to ASCII, but five of them are still five.
        "\uff11\uff12\uff13\uff14\uff15",
    ],
)
def test_verify_totp_rejects_malformed_input(code):
    """Garbage in must be a clean ``None``, never an exception on a login path."""
    secret = mfa.generate_secret()
    assert mfa.verify_totp(secret, code) is None


def _full_width(text: str) -> str:
    """*text* as a Japanese input method types it in full-width mode."""
    return "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in text)


def test_verify_totp_accepts_the_right_code_typed_in_full_width():
    """``１２３４５６`` is ``123456``: NFKC folds it before the format check,
    where it would otherwise be a wrong code."""
    secret = mfa.generate_secret()
    now = time.time()
    step = mfa.current_step(now)
    code = _code_for(secret, step)

    assert mfa.verify_totp(secret, _full_width(code), at=now) == step
    # The full-width space and hyphen an authenticator's grouping turns into.
    grouped_with_space = _full_width(code[:3]) + "\u3000" + _full_width(code[3:])
    assert mfa.verify_totp(secret, grouped_with_space, at=now) == step
    assert mfa.verify_totp(secret, _full_width(f"{code[:3]}-{code[3:]}"), at=now) == step


def _code_outside_the_window(secret: str, now: float) -> str:
    """A well-formed code that no step :func:`mfa.verify_totp` accepts at *now* produces."""
    step = mfa.current_step(now)
    window = range(step - mfa.TOTP_DRIFT_STEPS, step + mfa.TOTP_DRIFT_STEPS + 1)
    accepted = {_code_for(secret, s) for s in window}
    candidate = int(_code_for(secret, step))
    while (code := f"{candidate:0{mfa.TOTP_DIGITS}d}") in accepted:
        candidate = (candidate + 1) % 10**mfa.TOTP_DIGITS
    return code


def _a_wrong_code(secret: str) -> str:
    """A well-formed code no step accepted while the test runs produces.

    For a route that reads the clock itself: :func:`_code_outside_the_window` at
    ``now``, widened by one step either side so crossing a step boundary during the
    request cannot turn it into a valid code. A fixed literal (six zeros) is the
    current code one time in a million.
    """
    step = mfa.current_step(time.time())
    reach = mfa.TOTP_DRIFT_STEPS + 1
    accepted = {_code_for(secret, s) for s in range(step - reach, step + reach + 1)}
    candidate = int(_code_for(secret, step))
    while (code := f"{candidate:0{mfa.TOTP_DIGITS}d}") in accepted:
        candidate = (candidate + 1) % 10**mfa.TOTP_DIGITS
    return code


def test_no_test_uses_a_literal_as_the_wrong_code():
    """The flake: a hard-coded "wrong" code is sometimes the right one."""
    source = Path(__file__).read_text(encoding="utf-8")
    assert '"0' + '00000"' not in source


def test_verify_totp_refuses_a_wrong_code_typed_in_full_width():
    secret = mfa.generate_secret()
    now = time.time()
    wrong = _code_outside_the_window(secret, now)

    assert mfa.verify_totp(secret, _full_width(wrong), at=now) is None


def test_a_code_longer_than_any_spelling_of_one_never_reaches_nfkc(monkeypatch):
    """NFKC expands U+FDFA eighteen-fold, on the event loop: an oversized code is
    an ordinary wrong one, refused before it is normalised."""
    secret = mfa.generate_secret()
    _plaintext, stored = mfa.generate_recovery_codes()

    def _no_nfkc(*_args):
        raise AssertionError("an oversized code was handed to NFKC")

    monkeypatch.setattr(mfa, "unicodedata", SimpleNamespace(normalize=_no_nfkc))
    oversized = "\ufdfa" * (mfa.MAX_CODE_INPUT_CHARS + 1)
    assert mfa.verify_totp(secret, oversized) is None
    assert mfa.consume_recovery_code(stored, oversized) is None


def test_a_code_within_the_bound_still_verifies_however_it_is_spaced():
    secret = mfa.generate_secret()
    now = time.time()
    step = mfa.current_step(now)
    padded = _code_for(secret, step).center(mfa.MAX_CODE_INPUT_CHARS)
    plaintext, stored = mfa.generate_recovery_codes()

    assert mfa.verify_totp(secret, padded, at=now) == step
    assert mfa.consume_recovery_code(stored, plaintext[0].center(mfa.MAX_CODE_INPUT_CHARS)) is not None


def test_a_recovery_code_typed_in_full_width_is_the_same_code():
    plaintext, stored = mfa.generate_recovery_codes()

    updated = mfa.consume_recovery_code(stored, _full_width(plaintext[0].lower()))

    assert updated is not None
    assert mfa.count_unused_recovery_codes(updated) == mfa.RECOVERY_CODE_COUNT - 1


def test_verify_totp_accepts_a_code_typed_with_a_separator():
    """Authenticators display ``123 456``; pasting that must work."""
    secret = mfa.generate_secret()
    now = time.time()
    step = mfa.current_step(now)
    code = _code_for(secret, step)

    assert mfa.verify_totp(secret, f"{code[:3]} {code[3:]}", at=now) == step


# ---------------------------------------------------------------------------
# Service: secret storage
# ---------------------------------------------------------------------------


def test_secret_round_trips_through_encryption():
    secret = mfa.generate_secret()
    stored = mfa.encrypt_secret(secret)

    assert stored != secret  # never at rest in the clear
    assert mfa.decrypt_secret(stored) == secret


def test_decrypt_secret_degrades_to_none_on_an_unreadable_value():
    """A SECRET_KEY rotation orphans enrollments; that must not 500 every login."""
    assert mfa.decrypt_secret("not-a-fernet-token") is None
    assert mfa.decrypt_secret(None) is None
    assert mfa.decrypt_secret("") is None


# ---------------------------------------------------------------------------
# Service: recovery codes
# ---------------------------------------------------------------------------


def test_recovery_codes_are_single_use():
    plaintext, stored = mfa.generate_recovery_codes()
    assert len(plaintext) == mfa.RECOVERY_CODE_COUNT
    assert len(set(plaintext)) == mfa.RECOVERY_CODE_COUNT
    assert mfa.count_unused_recovery_codes(stored) == mfa.RECOVERY_CODE_COUNT

    after = mfa.consume_recovery_code(stored, plaintext[3])
    assert after is not None
    assert mfa.count_unused_recovery_codes(after) == mfa.RECOVERY_CODE_COUNT - 1

    # Same code again → refused, and nothing further is burned.
    assert mfa.consume_recovery_code(after, plaintext[3]) is None


def test_recovery_codes_are_never_stored_in_the_clear():
    plaintext, stored = mfa.generate_recovery_codes()
    blob = repr(stored)
    for code in plaintext:
        assert code not in blob
        assert mfa.normalize_recovery_code(code) not in blob


def test_recovery_code_matching_ignores_case_and_separators():
    """Users retype these off paper; ``a3kp 9wtm…`` must match ``A3KP-9WTM-…``."""
    plaintext, stored = mfa.generate_recovery_codes()
    typed = plaintext[0].lower().replace("-", " ")

    assert mfa.consume_recovery_code(stored, typed) is not None


def test_consume_recovery_code_handles_an_empty_set():
    assert mfa.consume_recovery_code(None, "ABCD-EFGH-JKMN-PQRS") is None
    assert mfa.consume_recovery_code([], "ABCD-EFGH-JKMN-PQRS") is None


def test_consume_recovery_code_returns_a_fresh_list():
    """The column has no mutation tracking — an in-place edit would never flush."""
    _plaintext, stored = mfa.generate_recovery_codes()
    original = [dict(entry) for entry in stored]

    after = mfa.consume_recovery_code(stored, _plaintext[0])

    assert after is not stored
    assert stored == original  # the input was left untouched


# ---------------------------------------------------------------------------
# Service: challenge token
# ---------------------------------------------------------------------------


def test_challenge_token_round_trips():
    user_id = uuid.uuid4()
    challenge = mfa.decode_challenge_token(mfa.create_challenge_token(user_id))

    assert challenge.user_id == user_id
    assert challenge.issued_at <= int(time.time())
    assert challenge.method == "local"


@pytest.mark.parametrize("method", ["local", "oidc", "saml"])
def test_challenge_token_carries_the_login_method(method):
    """So the completion audit names the real path instead of assuming a password."""
    token = mfa.create_challenge_token(uuid.uuid4(), method=method)

    assert mfa.decode_challenge_token(token).method == method


def test_a_challenge_token_without_a_method_claim_reads_as_local():
    """Rolling deploys: a token minted before ``method`` existed was local."""
    legacy = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "scope": mfa.MFA_TOKEN_SCOPE,
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
        },
        settings.secret_key,
        algorithm=settings.jwt_algorithm,
    )

    assert mfa.decode_challenge_token(legacy).method == "local"


def test_a_session_token_is_not_accepted_as_a_challenge():
    """Replaying a stolen session must not skip the factor protecting it."""
    from app.core.security import create_access_token

    with pytest.raises(mfa.MfaChallengeError):
        mfa.decode_challenge_token(create_access_token(str(uuid.uuid4())))


def test_an_expired_challenge_is_rejected():
    from datetime import UTC, datetime, timedelta

    stale = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "scope": mfa.MFA_TOKEN_SCOPE,
            "iat": int(time.time()) - 3600,
            "exp": datetime.now(UTC) - timedelta(minutes=1),
        },
        settings.secret_key,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(mfa.MfaChallengeError):
        mfa.decode_challenge_token(stale)


@pytest.mark.parametrize("token", ["", "garbage", "a.b.c"])
def test_malformed_challenge_tokens_are_rejected(token):
    with pytest.raises(mfa.MfaChallengeError):
        mfa.decode_challenge_token(token)


async def test_a_challenge_token_cannot_authenticate_the_api():
    """The other half of the contract, enforced in ``app.api.deps``.

    The challenge token is signed with the platform secret and carries the same
    ``sub`` as a session token. If ``get_current_user_ungated`` did not reject
    scoped tokens, clearing only the password half of the login would hand out
    full API access.
    """
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer", credentials=mfa.create_challenge_token(uuid.uuid4())
    )
    with pytest.raises(HTTPException) as exc:
        await deps.get_current_user_ungated(credentials=credentials, db=object())
    assert exc.value.status_code == 401


@pytest.mark.parametrize("scope", ["", None, 0, False, "anything"])
async def test_any_scope_claim_at_all_is_refused_as_a_session(scope):
    """The rule is the PRESENCE of ``scope``, not its truthiness.

    A truthiness test (``payload.get("scope")``) would let ``"scope": ""``
    through as a full session, which is the one shape an attacker would reach
    for if they could influence the claim. Pinning every falsy value keeps the
    guard closed-by-default for future scoped tokens too.
    """
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "scope": scope,
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
        },
        settings.secret_key,
        algorithm=settings.jwt_algorithm,
    )
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    with pytest.raises(HTTPException) as exc:
        await deps.get_current_user_ungated(credentials=credentials, db=object())
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


class _FakeDb:
    """Minimal async session stand-in: the MFA paths flush/commit/get/refresh."""

    def __init__(self, user=None):
        self.user = user
        self.locked_refreshes = 0

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def refresh(self, _obj, with_for_update=None):
        if with_for_update:
            self.locked_refreshes += 1

    async def get(self, _model, _pk):
        return self.user

    async def execute(self, _stmt):
        user = self.user
        return SimpleNamespace(scalar_one_or_none=lambda: user)


def _fake_user(**overrides):
    """A user carrying every attribute the MFA + login paths touch."""
    defaults = {
        "id": uuid.uuid4(),
        "email": "u@example.com",
        "full_name": "Test User",
        "role": "user",
        "is_active": True,
        "hashed_password": "bcrypt-hash",
        "password_changed_at": None,
        "sso_provider": None,
        "mfa_enabled": False,
        "mfa_secret": None,
        "mfa_confirmed_at": None,
        "mfa_last_used_step": None,
        "mfa_recovery_codes": None,
    }
    return SimpleNamespace(**{**defaults, **overrides})


@pytest.fixture
def client(monkeypatch):
    """A TestClient over the auth router with the ambient services stubbed out."""

    async def _no_rate_limit(*_a, **_kw):
        return None

    async def _no_audit(*_a, **_kw):
        return None

    monkeypatch.setattr(auth_api, "enforce_rate_limit", _no_rate_limit)
    monkeypatch.setattr(auth_api, "log_audit", _no_audit)

    import app.core.sso_helpers as sso_helpers

    async def _sso_config(_db):
        return SimpleNamespace(local_auth_enabled=True)

    monkeypatch.setattr(sso_helpers, "get_sso_config", _sso_config)

    app = FastAPI()
    app.include_router(auth_api.router, prefix="/api/v1/auth")
    return app


def _mount(app, user):
    db = _FakeDb(user)

    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[deps.get_current_user] = lambda: user
    return TestClient(app)


def test_login_withholds_the_session_when_mfa_is_enabled(client, monkeypatch):
    secret = mfa.generate_secret()
    user = _fake_user(mfa_enabled=True, mfa_secret=mfa.encrypt_secret(secret))

    async def _ok(_plain, _hashed):
        return True

    monkeypatch.setattr(auth_api, "verify_password_async", _ok)

    resp = _mount(client, user).post(
        "/api/v1/auth/login", json={"email": user.email, "password": "pw"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["mfa_required"] is True
    assert body["access_token"] is None
    assert body["mfa_token"]
    # And the challenge is genuinely scoped, not a session in disguise.
    assert mfa.decode_challenge_token(body["mfa_token"])[0] == user.id


def test_login_is_unchanged_for_accounts_without_mfa(client, monkeypatch):
    user = _fake_user()

    async def _ok(_plain, _hashed):
        return True

    monkeypatch.setattr(auth_api, "verify_password_async", _ok)

    resp = _mount(client, user).post(
        "/api/v1/auth/login", json={"email": user.email, "password": "pw"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"]
    assert body["mfa_required"] is False


def test_login_does_not_challenge_an_account_flagged_without_a_secret(
    client, monkeypatch
):
    """A flag with no secret would be an unanswerable challenge — a lockout."""
    user = _fake_user(mfa_enabled=True, mfa_secret=None)

    async def _ok(_plain, _hashed):
        return True

    monkeypatch.setattr(auth_api, "verify_password_async", _ok)

    body = _mount(client, user).post(
        "/api/v1/auth/login", json={"email": user.email, "password": "pw"}
    ).json()

    assert body["mfa_required"] is False
    assert body["access_token"]


def test_mfa_verify_issues_a_session_for_a_valid_code(client):
    secret = mfa.generate_secret()
    user = _fake_user(mfa_enabled=True, mfa_secret=mfa.encrypt_secret(secret))
    step = mfa.current_step()

    resp = _mount(client, user).post(
        "/api/v1/auth/mfa/verify",
        json={
            "mfa_token": mfa.create_challenge_token(user.id),
            "code": _code_for(secret, step),
        },
    )

    assert resp.status_code == 200
    assert resp.json()["access_token"]
    # The spent step is recorded, which is what closes the replay window.
    assert user.mfa_last_used_step == step


def test_mfa_verify_refuses_the_same_totp_code_twice(client):
    secret = mfa.generate_secret()
    user = _fake_user(mfa_enabled=True, mfa_secret=mfa.encrypt_secret(secret))
    code = _code_for(secret, mfa.current_step())
    test_client = _mount(client, user)

    first = test_client.post(
        "/api/v1/auth/mfa/verify",
        json={"mfa_token": mfa.create_challenge_token(user.id), "code": code},
    )
    replayed = test_client.post(
        "/api/v1/auth/mfa/verify",
        json={"mfa_token": mfa.create_challenge_token(user.id), "code": code},
    )

    assert first.status_code == 200
    assert replayed.status_code == 401
    assert replayed.json()["detail"] == "Invalid verification code. Please try again."


def test_mfa_verification_rereads_the_user_under_a_row_lock(client):
    """Concurrent attempts with one code must serialize on the user row."""
    secret = mfa.generate_secret()
    user = _fake_user(mfa_enabled=True, mfa_secret=mfa.encrypt_secret(secret))
    db = _FakeDb(user)

    asyncio.run(
        auth_api._verify_second_factor(
            db,
            user,
            _code_for(secret, mfa.current_step()),
            request=None,
            context="login",
        )
    )

    assert db.locked_refreshes == 1


def test_mfa_verify_rejects_a_wrong_code(client):
    secret = mfa.generate_secret()
    user = _fake_user(mfa_enabled=True, mfa_secret=mfa.encrypt_secret(secret))

    resp = _mount(client, user).post(
        "/api/v1/auth/mfa/verify",
        json={"mfa_token": mfa.create_challenge_token(user.id), "code": _a_wrong_code(secret)},
    )

    assert resp.status_code == 401


def test_mfa_verify_answers_non_ascii_digits_as_a_wrong_code(client):
    """Arabic-Indic digits passed the ``\\d`` format check and the comparison
    then raised: a 500 on the login path instead of a wrong code."""
    secret = mfa.generate_secret()
    user = _fake_user(mfa_enabled=True, mfa_secret=mfa.encrypt_secret(secret))

    resp = _mount(client, user).post(
        "/api/v1/auth/mfa/verify",
        json={
            "mfa_token": mfa.create_challenge_token(user.id),
            "code": "\u0661\u0662\u0663\u0664\u0665\u0666",
        },
    )

    assert resp.status_code == 401
    assert user.mfa_last_used_step is None


def test_mfa_verify_accepts_the_code_typed_in_full_width(client):
    """What a Japanese input method in full-width mode types into the code field."""
    secret = mfa.generate_secret()
    user = _fake_user(mfa_enabled=True, mfa_secret=mfa.encrypt_secret(secret))
    step = mfa.current_step()

    resp = _mount(client, user).post(
        "/api/v1/auth/mfa/verify",
        json={
            "mfa_token": mfa.create_challenge_token(user.id),
            "code": _full_width(_code_for(secret, step)),
        },
    )

    assert resp.status_code == 200
    assert user.mfa_last_used_step == step


def test_mfa_verify_accepts_a_recovery_code_and_burns_it(client):
    secret = mfa.generate_secret()
    plaintext, stored = mfa.generate_recovery_codes()
    user = _fake_user(
        mfa_enabled=True,
        mfa_secret=mfa.encrypt_secret(secret),
        mfa_recovery_codes=stored,
    )

    test_client = _mount(client, user)
    first = test_client.post(
        "/api/v1/auth/mfa/verify",
        json={"mfa_token": mfa.create_challenge_token(user.id), "code": plaintext[0]},
    )
    assert first.status_code == 200
    assert mfa.count_unused_recovery_codes(user.mfa_recovery_codes) == (
        mfa.RECOVERY_CODE_COUNT - 1
    )

    replayed = test_client.post(
        "/api/v1/auth/mfa/verify",
        json={"mfa_token": mfa.create_challenge_token(user.id), "code": plaintext[0]},
    )
    assert replayed.status_code == 401


def test_mfa_verify_rejects_a_session_token_in_place_of_the_challenge(client):
    from app.core.security import create_access_token

    secret = mfa.generate_secret()
    user = _fake_user(mfa_enabled=True, mfa_secret=mfa.encrypt_secret(secret))

    resp = _mount(client, user).post(
        "/api/v1/auth/mfa/verify",
        json={
            "mfa_token": create_access_token(str(user.id)),
            "code": _code_for(secret, mfa.current_step()),
        },
    )

    assert resp.status_code == 401


def test_sso_only_accounts_can_enroll(client):
    """XTM One layers its own second factor on top of the IdP's first one.

    Deployments federating to a provider whose MFA policy they do not control
    (a customer tenant, a partner IdP, a legacy directory) must still be able to
    enforce a factor at the application boundary.
    """
    user = _fake_user(sso_provider="oidc", hashed_password=None)

    resp = _mount(client, user).post("/api/v1/auth/mfa/setup")

    assert resp.status_code == 200
    assert resp.json()["otpauth_uri"].startswith("otpauth://totp/")


def test_mfa_state_is_available_for_sso_only_accounts(client):
    """The enrollment UI is driven by this flag, so it gates the whole feature."""
    user = _fake_user(sso_provider="saml", hashed_password=None)

    assert auth_api._build_mfa_state(user).available is True


def test_mfa_setup_refuses_when_already_enabled(client):
    user = _fake_user(mfa_enabled=True, mfa_secret=mfa.encrypt_secret("A" * 16))

    resp = _mount(client, user).post("/api/v1/auth/mfa/setup")

    assert resp.status_code == 409


def test_mfa_setup_stores_the_secret_encrypted_and_returns_a_qr(client):
    user = _fake_user()

    body = _mount(client, user).post("/api/v1/auth/mfa/setup").json()

    assert body["otpauth_uri"].startswith("otpauth://totp/")
    assert body["qr_code"].startswith("data:image/svg+xml;base64,")
    assert body["digits"] == mfa.TOTP_DIGITS
    assert body["period"] == mfa.TOTP_PERIOD_SECONDS
    # Stored encrypted, and the stored form decrypts back to what was shown.
    assert user.mfa_secret != body["secret"]
    assert mfa.decrypt_secret(user.mfa_secret) == body["secret"]
    # Still inactive until confirmed — a closed tab must not lock the user out.
    assert user.mfa_enabled is False


def test_mfa_enable_requires_a_valid_code(client):
    secret = mfa.generate_secret()
    user = _fake_user(mfa_secret=mfa.encrypt_secret(secret))

    resp = _mount(client, user).post("/api/v1/auth/mfa/enable", json={"code": _a_wrong_code(secret)})

    assert resp.status_code == 401
    assert user.mfa_enabled is False


def test_mfa_enable_activates_and_issues_recovery_codes(client):
    secret = mfa.generate_secret()
    user = _fake_user(mfa_secret=mfa.encrypt_secret(secret))

    resp = _mount(client, user).post(
        "/api/v1/auth/mfa/enable", json={"code": _code_for(secret, mfa.current_step())}
    )

    assert resp.status_code == 200
    assert len(resp.json()["recovery_codes"]) == mfa.RECOVERY_CODE_COUNT
    assert user.mfa_enabled is True
    assert user.mfa_confirmed_at is not None


def test_mfa_enable_refuses_a_recovery_code_as_proof_of_enrollment(client):
    """``enable`` must prove the AUTHENTICATOR works, not that a code exists."""
    secret = mfa.generate_secret()
    plaintext, stored = mfa.generate_recovery_codes()
    user = _fake_user(mfa_secret=mfa.encrypt_secret(secret), mfa_recovery_codes=stored)

    resp = _mount(client, user).post(
        "/api/v1/auth/mfa/enable", json={"code": plaintext[0]}
    )

    assert resp.status_code == 401
    assert user.mfa_enabled is False


def test_mfa_disable_clears_every_trace(client, monkeypatch):
    secret = mfa.generate_secret()
    _plaintext, stored = mfa.generate_recovery_codes()
    user = _fake_user(
        mfa_enabled=True,
        mfa_secret=mfa.encrypt_secret(secret),
        mfa_recovery_codes=stored,
    )

    async def _ok(_plain, _hashed):
        return True

    monkeypatch.setattr(auth_api, "verify_password_async", _ok)

    resp = _mount(client, user).post(
        "/api/v1/auth/mfa/disable",
        json={"password": "pw", "code": _code_for(secret, mfa.current_step())},
    )

    assert resp.status_code == 200
    assert user.mfa_enabled is False
    assert user.mfa_secret is None
    assert user.mfa_recovery_codes is None


def test_mfa_disable_rejects_a_wrong_password_before_touching_the_code(
    client, monkeypatch
):
    secret = mfa.generate_secret()
    user = _fake_user(mfa_enabled=True, mfa_secret=mfa.encrypt_secret(secret))

    async def _wrong(_plain, _hashed):
        return False

    monkeypatch.setattr(auth_api, "verify_password_async", _wrong)

    resp = _mount(client, user).post(
        "/api/v1/auth/mfa/disable",
        json={"password": "nope", "code": _code_for(secret, mfa.current_step())},
    )

    assert resp.status_code == 401
    assert user.mfa_enabled is True


def test_mfa_disable_needs_only_a_code_for_an_sso_only_account(client):
    """There is no password to demand — requiring one would be an unmeetable ask."""
    secret = mfa.generate_secret()
    user = _fake_user(
        sso_provider="oidc",
        hashed_password=None,
        mfa_enabled=True,
        mfa_secret=mfa.encrypt_secret(secret),
    )

    resp = _mount(client, user).post(
        "/api/v1/auth/mfa/disable",
        json={"code": _code_for(secret, mfa.current_step())},
    )

    assert resp.status_code == 200
    assert user.mfa_enabled is False


def test_mfa_disable_still_demands_a_password_when_the_account_has_one(client):
    """The optional field must not become an opt-out for local accounts."""
    secret = mfa.generate_secret()
    user = _fake_user(mfa_enabled=True, mfa_secret=mfa.encrypt_secret(secret))

    resp = _mount(client, user).post(
        "/api/v1/auth/mfa/disable",
        json={"code": _code_for(secret, mfa.current_step())},
    )

    assert resp.status_code == 401
    assert user.mfa_enabled is True


# ---------------------------------------------------------------------------
# Password reset must not bypass the second factor
#
# Found by comparing against OpenCTI, which gates its forgot-password flow on
# MFA. Without this the reset link is a complete bypass: whoever controls the
# mailbox resets the password and walks in — the exact threat the second factor
# is there to stop.
# ---------------------------------------------------------------------------


def _reset_client(client, user, monkeypatch, *, token_payload):
    """Mount ``/auth/reset-password`` with the Redis token store stubbed."""
    from app.services import token_store

    async def _consume(_raw):
        return token_payload

    monkeypatch.setattr(token_store, "consume_password_reset", _consume)

    async def _hash(pw):
        return f"hashed:{pw}"

    monkeypatch.setattr(auth_api, "hash_password_async", _hash)
    return _mount(client, user)


def test_password_reset_withholds_the_session_when_mfa_is_enabled(
    client, monkeypatch
):
    secret = mfa.generate_secret()
    user = _fake_user(mfa_enabled=True, mfa_secret=mfa.encrypt_secret(secret))
    test_client = _reset_client(
        client, user, monkeypatch, token_payload={"user_id": str(user.id)}
    )

    resp = test_client.post(
        "/api/v1/auth/reset-password",
        json={"token": "raw-token", "new_password": "n3wpassword"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["mfa_required"] is True
    assert body["access_token"] is None
    # The password change itself still went through — only the session is owed.
    assert user.hashed_password == "hashed:n3wpassword"
    assert mfa.decode_challenge_token(body["mfa_token"]).user_id == user.id


def test_password_reset_still_signs_in_accounts_without_mfa(client, monkeypatch):
    user = _fake_user()
    test_client = _reset_client(
        client, user, monkeypatch, token_payload={"user_id": str(user.id)}
    )

    body = test_client.post(
        "/api/v1/auth/reset-password",
        json={"token": "raw-token", "new_password": "n3wpassword"},
    ).json()

    assert body["access_token"]
    assert body["mfa_required"] is False


def test_the_reset_challenge_survives_the_password_change_it_just_made(
    client, monkeypatch
):
    """``mfa_verify`` rejects challenges older than ``password_changed_at``.

    The reset sets that timestamp moments before minting the challenge, so an
    off-by-one here would make every post-reset login impossible — the account
    would be permanently stuck at the code screen.
    """
    secret = mfa.generate_secret()
    user = _fake_user(mfa_enabled=True, mfa_secret=mfa.encrypt_secret(secret))
    test_client = _reset_client(
        client, user, monkeypatch, token_payload={"user_id": str(user.id)}
    )

    challenge = test_client.post(
        "/api/v1/auth/reset-password",
        json={"token": "raw-token", "new_password": "n3wpassword"},
    ).json()["mfa_token"]

    verified = test_client.post(
        "/api/v1/auth/mfa/verify",
        json={"mfa_token": challenge, "code": _code_for(secret, mfa.current_step())},
    )

    assert verified.status_code == 200
    assert verified.json()["access_token"]


# ---------------------------------------------------------------------------
# Audit attribution
#
# ``action`` is the indexed, filterable field in the audit UI; ``details`` is
# not. A literal "login" on every entry would file a mistyped code during
# *disable* as a login event, drowning the one signal this feature exists to
# raise: repeated failed second factors during LOGIN with no matching successful
# login. These pin the flow → action mapping so that cannot regress.
# ---------------------------------------------------------------------------


def _captured_audits(monkeypatch):
    """Collect every ``log_audit`` call made by the auth module."""
    entries: list[dict] = []

    async def _capture(_db, **kwargs):
        entries.append(kwargs)

    monkeypatch.setattr(auth_api, "log_audit", _capture)
    return entries


@pytest.mark.parametrize(
    ("context", "expected_action"),
    [
        ("login", "login"),
        ("mcp-oauth", "login"),
        ("disable", "disable"),
        ("recovery-codes", "update"),
    ],
)
def test_failed_verification_is_attributed_to_the_calling_flow(
    client, monkeypatch, context, expected_action
):
    entries = _captured_audits(monkeypatch)
    secret = mfa.generate_secret()
    user = _fake_user(mfa_enabled=True, mfa_secret=mfa.encrypt_secret(secret))
    db = _FakeDb(user)

    # Its own type for the callers that tell it apart, still the
    # same 401 for the JSON routes.
    with pytest.raises(auth_api.MfaInvalidCodeError) as exc:
        asyncio.run(
            auth_api._verify_second_factor(
                db, user, _a_wrong_code(secret), request=None, context=context
            )
        )
    assert isinstance(exc.value, HTTPException)
    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid verification code. Please try again."

    failures = [e for e in entries if e["entity_type"] == "mfa"]
    assert [e["action"] for e in failures] == [expected_action]
    # The flow stays in details too — the indexed field is an addition, not a
    # replacement, so existing queries on details.context keep working.
    assert failures[0]["details"]["context"] == context


@pytest.mark.parametrize(
    ("context", "expected_action"),
    [("login", "login"), ("disable", "disable")],
)
def test_recovery_code_use_is_attributed_to_the_calling_flow(
    client, monkeypatch, context, expected_action
):
    entries = _captured_audits(monkeypatch)
    secret = mfa.generate_secret()
    plaintext, stored = mfa.generate_recovery_codes()
    user = _fake_user(
        mfa_enabled=True,
        mfa_secret=mfa.encrypt_secret(secret),
        mfa_recovery_codes=stored,
    )
    db = _FakeDb(user)

    kind = asyncio.run(
        auth_api._verify_second_factor(
            db, user, plaintext[0], request=None, context=context
        )
    )

    assert kind == "recovery"
    used = [e for e in entries if e["entity_type"] == "mfa"]
    assert [e["action"] for e in used] == [expected_action]


@pytest.mark.parametrize("enabled_at_verify", [True, False])
def test_mfa_verify_audits_the_second_factor_only_when_one_was_checked(
    client, monkeypatch, enabled_at_verify
):
    """Two-factor turned off while the challenge was in flight completes the
    login on the first factor alone: the audit must not claim ``mfa``."""
    entries = _captured_audits(monkeypatch)
    secret = mfa.generate_secret()
    user = _fake_user(
        mfa_enabled=enabled_at_verify,
        mfa_secret=mfa.encrypt_secret(secret) if enabled_at_verify else None,
    )

    resp = _mount(client, user).post(
        "/api/v1/auth/mfa/verify",
        json={
            "mfa_token": mfa.create_challenge_token(user.id),
            "code": _code_for(secret, mfa.current_step()),
        },
    )

    assert resp.status_code == 200
    logins = [e for e in entries if e["entity_type"] == "user"]
    assert [e["details"]["mfa"] for e in logins] == [enabled_at_verify]


def test_an_unknown_context_falls_back_to_login_rather_than_crashing(
    client, monkeypatch
):
    """A future caller that forgets to register its context must still audit."""
    entries = _captured_audits(monkeypatch)
    user = _fake_user(mfa_enabled=True, mfa_secret=mfa.encrypt_secret("A" * 16))

    with pytest.raises(HTTPException):
        asyncio.run(
            auth_api._verify_second_factor(
                _FakeDb(user), user, _a_wrong_code("A" * 16), request=None, context="something-new"
            )
        )

    assert [e["action"] for e in entries if e["entity_type"] == "mfa"] == ["login"]


# ---------------------------------------------------------------------------
# Admin reset (the lockout escape hatch)
# ---------------------------------------------------------------------------


def test_admin_mfa_reset_rejects_a_malformed_user_id_with_422(monkeypatch):
    """A copy-paste slip must be a readable 422, not an opaque 500.

    The path parameter is typed ``uuid.UUID`` so FastAPI validates it before the
    handler runs. Parsing a ``str`` by hand would raise ``ValueError`` inside the
    handler, and there is no global handler for it.
    """
    from app.api.v1 import users as users_api

    async def _no_audit(*_a, **_kw):
        return None

    monkeypatch.setattr(users_api, "log_audit", _no_audit)

    admin = _fake_user(role="admin")
    app = FastAPI()
    app.include_router(users_api.router, prefix="/api/v1/users")

    async def _get_db():
        yield _FakeDb(admin)

    app.dependency_overrides[deps.require_admin] = lambda: admin
    app.dependency_overrides[get_db] = _get_db

    resp = TestClient(app).delete("/api/v1/users/not-a-uuid/mfa")

    assert resp.status_code == 422


def test_admin_mfa_reset_clears_every_trace(monkeypatch):
    from app.api.v1 import users as users_api

    async def _no_audit(*_a, **_kw):
        return None

    monkeypatch.setattr(users_api, "log_audit", _no_audit)

    secret = mfa.generate_secret()
    _plaintext, stored = mfa.generate_recovery_codes()
    target = _fake_user(
        mfa_enabled=True,
        mfa_secret=mfa.encrypt_secret(secret),
        mfa_recovery_codes=stored,
        mfa_confirmed_at="2026-08-08T00:00:00+00:00",
    )
    admin = _fake_user(role="admin")

    app = FastAPI()
    app.include_router(users_api.router, prefix="/api/v1/users")

    async def _get_db():
        yield _FakeDb(target)

    app.dependency_overrides[deps.require_admin] = lambda: admin
    app.dependency_overrides[get_db] = _get_db

    resp = TestClient(app).delete(f"/api/v1/users/{target.id}/mfa")

    assert resp.status_code == 204
    assert target.mfa_enabled is False
    assert target.mfa_secret is None
    assert target.mfa_recovery_codes is None
    assert target.mfa_last_used_step is None


# ---------------------------------------------------------------------------
# SSO login path: XTM One adds the factor, not the IdP
# ---------------------------------------------------------------------------


def test_sso_redirect_carries_a_challenge_when_the_account_is_enrolled():
    """The IdP proved the first factor; the second is still owed.

    Both SSO callbacks funnel through ``_build_frontend_redirect``, so this one
    assertion covers OIDC and SAML alike.
    """
    from app.api import auth_sso

    secret = mfa.generate_secret()
    user = _fake_user(
        sso_provider="oidc",
        hashed_password=None,
        mfa_enabled=True,
        mfa_secret=mfa.encrypt_secret(secret),
    )

    url = auth_sso._build_frontend_redirect(user, provider="oidc")

    assert "#mfa_token=" in url
    assert "#token=" not in url
    challenge = mfa.decode_challenge_token(url.split("#mfa_token=", 1)[1])
    assert challenge.user_id == user.id
    assert challenge.method == "oidc"


def test_sso_redirect_is_unchanged_for_accounts_without_mfa():
    from app.api import auth_sso

    user = _fake_user(sso_provider="saml", hashed_password=None)

    url = auth_sso._build_frontend_redirect(user, provider="saml")

    assert "#token=" in url
    assert "mfa_token" not in url


def test_sso_redirect_does_not_challenge_an_account_flagged_without_a_secret():
    """A flag with no secret would strand the user on an unanswerable screen."""
    from app.api import auth_sso

    user = _fake_user(sso_provider="oidc", hashed_password=None, mfa_enabled=True)

    assert "#token=" in auth_sso._build_frontend_redirect(user, provider="oidc")


def test_sso_audit_does_not_claim_a_completed_login_while_the_factor_is_pending():
    """Otherwise the trail shows a login that may never have finished."""
    from app.api import auth_sso

    secret = mfa.generate_secret()
    enrolled = _fake_user(
        sso_provider="oidc", mfa_enabled=True, mfa_secret=mfa.encrypt_secret(secret)
    )
    plain = _fake_user(sso_provider="oidc")

    assert "awaiting second factor" in auth_sso._login_audit_summary(enrolled, "OIDC")
    assert auth_sso._login_audit_summary(plain, "OIDC").startswith("Logged in via OIDC")


def test_an_sso_challenge_completes_at_the_shared_verify_endpoint(client):
    """The SSO half-login finishes through the same endpoint as the local one."""
    secret = mfa.generate_secret()
    user = _fake_user(
        sso_provider="saml",
        hashed_password=None,
        mfa_enabled=True,
        mfa_secret=mfa.encrypt_secret(secret),
    )

    resp = _mount(client, user).post(
        "/api/v1/auth/mfa/verify",
        json={
            "mfa_token": mfa.create_challenge_token(user.id, method="saml"),
            "code": _code_for(secret, mfa.current_step()),
        },
    )

    assert resp.status_code == 200
    assert resp.json()["access_token"]


def test_the_completion_audit_names_the_sso_provider(client, monkeypatch):
    """``login_method`` must say ``saml``, not assume a password was involved."""
    captured = {}

    async def _capture(_db, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(auth_api, "log_audit", _capture)

    secret = mfa.generate_secret()
    user = _fake_user(
        sso_provider="saml",
        hashed_password=None,
        mfa_enabled=True,
        mfa_secret=mfa.encrypt_secret(secret),
    )

    _mount(client, user).post(
        "/api/v1/auth/mfa/verify",
        json={
            "mfa_token": mfa.create_challenge_token(user.id, method="saml"),
            "code": _code_for(secret, mfa.current_step()),
        },
    )

    assert captured["details"]["login_method"] == "saml"
