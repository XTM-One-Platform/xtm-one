"""A token minted by a clock a few seconds ahead is not refused.

PyJWT's ``leeway`` is 0 by default and PyJWT 2.13 refuses an ``nbf`` or ``iat``
later than the verifier's clock by any amount: OpenCTI signs ``nbf`` = ``iat`` =
its own now, so a platform clock a second ahead of XTM One's refused the
embedded chat. Every verifier of a token minted on another host tolerates
:data:`JWT_CLOCK_SKEW_LEEWAY`, and the cross-platform lifetime cap
stays computed from the claims alone.
"""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.core.config import settings
from app.core.security import JWT_CLOCK_SKEW_LEEWAY, decode_access_token
from app.core.xtm_auth import XTM_JWT_MAX_LIFETIME, _kid, _private_key, _public_key_raw, verify_xtm_jwt
from tests import app_sources
from tests.jwt_clock import freeze_pyjwt_clock

PLATFORM = "http://localhost:8080"
XTM_ONE = "http://localhost:8090"
WITHIN = JWT_CLOCK_SKEW_LEEWAY - timedelta(seconds=15)
BEYOND = JWT_CLOCK_SKEW_LEEWAY + timedelta(seconds=15)
LEEWAY = int(JWT_CLOCK_SKEW_LEEWAY.total_seconds())
#: The instant the frozen-clock tests sign at: a whole POSIX second.
T = 2_000_000_000


@pytest.fixture(autouse=True)
def _identity_and_keys(monkeypatch):
    monkeypatch.setattr(settings, "base_url", XTM_ONE)
    monkeypatch.setattr(settings, "xtm_auth_accepted_audiences", [])
    monkeypatch.setattr(settings, "secret_key", "clock-skew-test-secret-key-0123456789abcdef")

    class _Client:
        def get_signing_key_from_jwt(self, _token: str):
            return SimpleNamespace(key=Ed25519PublicKey.from_public_bytes(_public_key_raw))

    monkeypatch.setattr("app.core.jwks.get_jwk_client", lambda _url, **_kwargs: _Client())


class _Result:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return list(self._rows)


class _Db:
    async def execute(self, _statement):
        return _Result([SimpleNamespace(platform_identifier="opencti", platform_url=PLATFORM, api_url=None)])


def _signed(iat: int, exp: int) -> str:
    """A token as OpenCTI signs it: ``nbf`` = ``iat``."""
    payload = {
        "iss": PLATFORM,
        "sub": str(uuid.uuid4()),
        "email": "analyst@example.com",
        "aud": XTM_ONE,
        "iat": iat,
        "nbf": iat,
        "exp": exp,
        "jti": str(uuid.uuid4()),
    }
    return pyjwt.encode(payload, _private_key, algorithm="EdDSA", headers={"kid": _kid})


def _platform_token(*, ahead: timedelta = timedelta(0), lifetime: timedelta = timedelta(minutes=10)) -> str:
    """A token signed now by a clock *ahead* of ours."""
    issued = datetime.now(UTC) + ahead
    return _signed(int(issued.timestamp()), int((issued + lifetime).timestamp()))


async def _accepted_at(monkeypatch, token: str, instant: int) -> bool:
    freeze_pyjwt_clock(monkeypatch, instant)
    return await verify_xtm_jwt(token, _Db()) is not None


def test_the_leeway_is_small_against_the_ten_minute_contract():
    """Room for a deliberate change, while an accidental large value is caught."""
    assert timedelta(seconds=30) <= JWT_CLOCK_SKEW_LEEWAY <= timedelta(minutes=2)


async def test_a_token_from_a_synchronised_clock_is_accepted():
    assert await verify_xtm_jwt(_platform_token(), _Db()) is not None


async def test_a_token_from_a_clock_ahead_within_the_leeway_is_accepted():
    assert await verify_xtm_jwt(_platform_token(ahead=WITHIN), _Db()) is not None


async def test_a_token_from_a_clock_ahead_beyond_the_leeway_is_refused():
    assert await verify_xtm_jwt(_platform_token(ahead=BEYOND), _Db()) is None


async def test_a_token_dated_ahead_is_accepted_up_to_the_leeway_exactly(monkeypatch):
    token = _signed(T, T + 600)
    assert await _accepted_at(monkeypatch, token, T - LEEWAY + 1)
    assert await _accepted_at(monkeypatch, token, T - LEEWAY)
    assert not await _accepted_at(monkeypatch, token, T - LEEWAY - 1)


async def test_an_expired_token_is_accepted_up_to_the_leeway_exactly(monkeypatch):
    token = _signed(T, T + 600)
    assert await _accepted_at(monkeypatch, token, T + 600 + LEEWAY - 1)
    assert not await _accepted_at(monkeypatch, token, T + 600 + LEEWAY)
    assert not await _accepted_at(monkeypatch, token, T + 600 + LEEWAY + 1)


async def test_the_leeway_never_widens_the_lifetime_cap():
    """``exp - iat`` is read off the claims: a clock ahead does not buy a longer token."""
    token = _platform_token(ahead=WITHIN, lifetime=XTM_JWT_MAX_LIFETIME + timedelta(seconds=1))
    assert await verify_xtm_jwt(token, _Db()) is None
    assert await verify_xtm_jwt(_platform_token(ahead=WITHIN, lifetime=XTM_JWT_MAX_LIFETIME), _Db()) is not None


async def test_a_token_is_accepted_for_at_most_seventeen_minutes_of_our_clock(monkeypatch):
    """The two leeways add up: from its ``iat`` minus the leeway to its ``exp`` plus the leeway."""
    lifetime = int(XTM_JWT_MAX_LIFETIME.total_seconds())
    token = _signed(T, T + lifetime)
    first, end = T - LEEWAY, T + lifetime + LEEWAY
    assert await _accepted_at(monkeypatch, token, first)
    assert await _accepted_at(monkeypatch, token, end - 1)
    assert not await _accepted_at(monkeypatch, token, first - 1)
    assert not await _accepted_at(monkeypatch, token, end)
    window = timedelta(seconds=end - first)
    assert window == XTM_JWT_MAX_LIFETIME + 2 * JWT_CLOCK_SKEW_LEEWAY
    assert window == timedelta(minutes=17)


def _session_token(*, ahead: timedelta) -> str:
    issued = datetime.now(UTC) + ahead
    payload = {"sub": str(uuid.uuid4()), "iat": int(issued.timestamp()), "exp": issued + timedelta(hours=1)}
    return pyjwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def test_a_session_token_minted_by_a_replica_ahead_within_the_leeway_is_accepted():
    assert decode_access_token(_session_token(ahead=WITHIN))["sub"]


def test_a_session_token_minted_by_a_replica_ahead_beyond_the_leeway_is_refused():
    with pytest.raises(pyjwt.ImmatureSignatureError):
        decode_access_token(_session_token(ahead=BEYOND))


# ---------------------------------------------------------------------------
# Every verifying decode passes the one leeway
# ---------------------------------------------------------------------------

_APP = app_sources.APP_DIR
_JWT_MODULE_NAMES = {"jwt", "pyjwt", "_jwt"}
#: Vendor-documented 5 minutes for the vendor's own webhook tokens.
_VENDOR_LEEWAY_MODULES = {"channels/teams.py", "channels/google_chat.py"}


def _is_jwt_decode(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "decode"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in _JWT_MODULE_NAMES
    )


def _is_unverified_peek(call: ast.Call) -> bool:
    """Only an ``options`` literal holding ``"verify_signature": False`` skips the check."""
    options = next((kw.value for kw in call.keywords if kw.arg == "options"), None)
    return isinstance(options, ast.Dict) and any(
        isinstance(k, ast.Constant)
        and k.value == "verify_signature"
        and isinstance(v, ast.Constant)
        and v.value is False
        for k, v in zip(options.keys, options.values, strict=True)
    )


def _verifying_decodes() -> list[tuple[str, ast.Call]]:
    found = []
    for path in app_sources.python_files(_APP):
        for node in app_sources.walk(app_sources.tree(path)):
            if _is_jwt_decode(node) and not _is_unverified_peek(node):
                found.append((path.relative_to(_APP).as_posix(), node))
    return found


def _call(source: str) -> ast.Call:
    node = ast.parse(source, mode="eval").body
    assert isinstance(node, ast.Call)
    return node


@pytest.mark.parametrize(
    ("source", "peek"),
    [
        ('jwt.decode(t, options={"verify_signature": False})', True),
        ('jwt.decode(t, key, options={"verify_signature": True})', False),
        ('jwt.decode(t, key, options={"verify_signature": flag})', False),
        ('jwt.decode(t, key, options={"verify_signature": 0})', False),
        ("jwt.decode(t, key, options=opts)", False),
        ('jwt.decode(t, key, options={"verify_aud": False})', False),
        ("jwt.decode(t, key)", False),
    ],
)
def test_the_tripwire_skips_only_a_literal_unverified_peek(source, peek):
    assert _is_unverified_peek(_call(source)) is peek


def test_every_verifying_jwt_decode_tolerates_clock_skew():
    decodes = _verifying_decodes()
    assert len(decodes) >= 9
    missing = []
    for module, call in decodes:
        leeway = next((kw.value for kw in call.keywords if kw.arg == "leeway"), None)
        if module in _VENDOR_LEEWAY_MODULES:
            ok = leeway is not None
        else:
            ok = isinstance(leeway, ast.Name) and leeway.id == "JWT_CLOCK_SKEW_LEEWAY"
        if not ok:
            missing.append(f"{module}:{call.lineno}")
    assert missing == [], f"jwt.decode without leeway=JWT_CLOCK_SKEW_LEEWAY: {missing}"
