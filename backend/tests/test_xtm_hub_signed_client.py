"""The XTM Hub client signs its platform calls once the instance is enrolled.

The Hub is an ``httpx.MockTransport`` behind the real ``xtm_hub_graphql()``,
so what is checked is what goes on the wire: the token before enrollment,
one fresh assertion after (never both), an assertion that verifies with the
instance's public JWK, and each answer of ``enrollPlatformKey`` read as one
outcome - an older Hub that does not know the operation and a network
failure included.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt as pyjwt
import pytest

from app.core.config import settings
from app.core.xtm_hub_key import ASSERTION_AUDIENCE, PLATFORM_IDENTIFIER, derive_key
from app.integrations.providers import xtm_hub as hub
from app.services import xtm_hub_registration as registration

_HUB = "https://hub.example.com"
_PLATFORM = uuid.UUID("6a3c2a8e-1f2b-4c3d-9e8f-0a1b2c3d4e5f")
_SECRET = "x" * 24 + "-a-strong-test-secret-key"
# Computed, so no literal looks like a credential to a secret scanner.
_TOKEN = str(uuid.uuid5(uuid.NAMESPACE_URL, "test:xtm-hub-platform-token"))

_ASSERTION = "xtm-hub-platform-assertion"
_TOKEN_HEADERS = ("xtm-hub-platform-id", "xtm-hub-platform-token")


@pytest.fixture(autouse=True)
def _strong_secret(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", _SECRET)


class _Hub:
    """A fake XTM Hub: one answer for every request, every request recorded."""

    def __init__(self, answer: httpx.Response | Exception):
        self.answer = answer
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        # A fresh response every time, streamed as off the network.
        headers = {k: v for k, v in self.answer.headers.items() if k == "content-type"}
        return httpx.Response(self.answer.status_code, headers=headers, stream=httpx.ByteStream(self.answer.content))

    def only_request(self) -> tuple[httpx.Request, dict[str, Any]]:
        (request,) = self.requests
        return request, json.loads(request.content)


@pytest.fixture
def install(monkeypatch):
    """Route the client's egress to a fake Hub; returns the installer."""
    real_client = hub.egress_client

    def _install(answer: httpx.Response | Exception) -> _Hub:
        fake = _Hub(answer)
        transport = httpx.MockTransport(fake)
        monkeypatch.setattr(hub, "egress_client", lambda **kwargs: real_client(transport=transport, **kwargs))
        return fake

    return _install


def _json(payload: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(status, headers={"content-type": "application/json"}, content=json.dumps(payload).encode())


def _refusal(code: str, status: int = 200) -> httpx.Response:
    return _json({"errors": [{"message": "refused", "extensions": {"code": code}}], "data": None}, status)


def _verify(assertion: str) -> dict[str, Any]:
    """Verify *assertion* as the Hub does: the enrolled public JWK, EdDSA only."""
    return pyjwt.decode(
        assertion,
        pyjwt.PyJWK(derive_key(1).public_jwk()).key,
        algorithms=["EdDSA"],
        audience=ASSERTION_AUDIENCE,
        issuer=str(_PLATFORM),
        options={"require": ["iss", "aud", "iat", "exp", "jti"]},
    )


_ACTIVE = _json({"data": {"refreshPlatformRegistrationConnectivityStatus": {"status": "active"}}})


# ---------------------------------------------------------------------------
# The credential a call carries
# ---------------------------------------------------------------------------


async def test_before_enrollment_the_check_carries_the_token_and_no_assertion(install):
    fake = install(_ACTIVE)

    credential = registration.PlatformCredential(_PLATFORM, token=_TOKEN)
    assert await registration.check_registration(_HUB, credential, "CE") == "active"

    request, body = fake.only_request()
    assert body["variables"]["input"]["token"] == _TOKEN
    assert _ASSERTION not in request.headers


async def test_once_enrolled_the_check_is_signed_and_carries_no_token(install):
    fake = install(_ACTIVE)

    credential = registration.PlatformCredential(_PLATFORM, key=derive_key(1))
    assert await registration.check_registration(_HUB, credential, "EE") == "active"

    request, body = fake.only_request()
    assert "token" not in body["variables"]["input"]
    assert body["variables"]["input"]["platformId"] == str(_PLATFORM)
    assert not any(name in request.headers for name in _TOKEN_HEADERS)
    assert _TOKEN not in request.content.decode() + str(request.headers)
    claims = _verify(request.headers[_ASSERTION])
    assert claims["identifier"] == PLATFORM_IDENTIFIER


async def test_the_assertion_is_typed_and_lives_sixty_seconds(install):
    fake = install(_ACTIVE)

    before = int(datetime.now(UTC).timestamp())
    credential = registration.PlatformCredential(_PLATFORM, key=derive_key(1))
    await registration.check_registration(_HUB, credential, "CE")

    request, _body = fake.only_request()
    assertion = request.headers[_ASSERTION]
    header = pyjwt.get_unverified_header(assertion)
    assert header == {"alg": "EdDSA", "kid": derive_key(1).kid, "typ": "xtm-hub-platform+jwt"}
    claims = _verify(assertion)
    assert (claims["iss"], claims["aud"]) == (str(_PLATFORM), "xtm-hub")
    assert claims["exp"] - claims["iat"] == 60
    assert before - 1 <= claims["iat"] <= before + 5
    assert "htu" not in claims
    uuid.UUID(claims["jti"])


async def test_every_signed_call_has_its_own_jti(install):
    fake = install(_ACTIVE)

    credential = registration.PlatformCredential(_PLATFORM, key=derive_key(1))
    for _ in range(3):
        await registration.check_registration(_HUB, credential, "CE")

    assert len({_verify(r.headers[_ASSERTION])["jti"] for r in fake.requests}) == 3


async def test_the_automatic_registration_carries_the_token_headers(install):
    fake = install(_json({"data": {"autoRegisterPlatform": {"success": True}}}))

    await hub.xtm_hub_graphql(
        _HUB,
        registration.AUTO_REGISTER_MUTATION,
        {},
        "auto-register",
        **registration.PlatformCredential(_PLATFORM, token=_TOKEN).headers(),
    )

    request, _body = fake.only_request()
    assert (request.headers["xtm-hub-platform-id"], request.headers["xtm-hub-platform-token"]) == (
        str(_PLATFORM),
        _TOKEN,
    )
    assert _ASSERTION not in request.headers


async def test_an_assertion_never_travels_over_plain_http(install):
    fake = install(_ACTIVE)

    credential = registration.PlatformCredential(_PLATFORM, key=derive_key(1))
    assert await registration.check_registration("http://hub.example.com", credential, "CE") == "refused"
    assert fake.requests == []


@pytest.mark.parametrize(
    "credential",
    [
        lambda: registration.PlatformCredential(_PLATFORM),
        lambda: registration.PlatformCredential(_PLATFORM, token=_TOKEN, key=derive_key(1)),
    ],
    ids=["neither", "both"],
)
def test_a_credential_is_the_token_or_the_key_never_both(credential):
    with pytest.raises(ValueError, match="exactly one"):
        credential()


def _snapshot(enrolled: bool) -> Any:
    from app.core.security import encrypt_value

    return registration._Snapshot(
        generation=1,
        hub_url=_HUB,
        platform_id=_PLATFORM,
        token_enc=encrypt_value(_TOKEN),
        status="registered",
        key_enrolled_at=datetime.now(UTC) if enrolled else None,
    )


def test_the_enrollment_date_alone_switches_the_credential():
    token = registration.snapshot_credential(_snapshot(enrolled=False), 1)
    signed = registration.snapshot_credential(_snapshot(enrolled=True), 1)

    assert token is not None and (token.token, token.signed) == (_TOKEN, False)
    assert signed is not None and (signed.token, signed.signed) == (None, True)


def test_the_enrolled_key_follows_the_key_version():
    signed = registration.snapshot_credential(_snapshot(enrolled=True), 2)
    assert signed is not None and signed.key is not None
    assert signed.key.public_jwk() == derive_key(2).public_jwk()


def test_an_enrolled_registration_never_falls_back_to_its_token(monkeypatch):
    # The key cannot be derived (SECRET_KEY guessable): nothing is sent, the token included.
    snapshot = _snapshot(enrolled=True)
    monkeypatch.setattr(settings, "secret_key", "short")
    assert registration.snapshot_credential(snapshot, 1) is None


# ---------------------------------------------------------------------------
# The Hub's refusals of a platform credential
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("PLATFORM_ASSERTION_INVALID", "assertion_invalid"),
        ("PLATFORM_KEY_MISMATCH", "key_mismatch"),
        ("PLATFORM_TOKEN_REFUSED", "token_refused"),
        ("PLATFORM_TOKEN_PATH_CLOSED", "token_path_closed"),
        ("FORBIDDEN_ACCESS", None),
    ],
)
async def test_each_hub_error_code_is_one_platform_refusal(install, code, expected):
    install(_refusal(code))
    _data, err = await hub.xtm_hub_graphql(_HUB, registration.REFRESH_MUTATION, {}, "check")
    assert hub.refusal_codes(err) == (code,)
    assert registration.platform_refusal(err) == expected


def test_only_a_graphql_refusal_has_codes():
    assert hub.refusal_codes(hub._UNREACHABLE) == ()
    assert hub.refusal_codes(None) == ()
    assert registration.platform_refusal(hub._NOT_A_HUB) is None


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("PLATFORM_KEY_MISMATCH", "key_mismatch"),
        ("PLATFORM_TOKEN_PATH_CLOSED", "token_path_closed"),
        ("PLATFORM_ASSERTION_INVALID", "refused"),
        ("PLATFORM_TOKEN_REFUSED", "refused"),
    ],
)
async def test_a_refused_check_reads_as_one_result(install, code, expected):
    install(_refusal(code))
    credential = registration.PlatformCredential(_PLATFORM, key=derive_key(1))
    assert await registration.check_registration(_HUB, credential, "CE") == expected


# ---------------------------------------------------------------------------
# enrollPlatformKey
# ---------------------------------------------------------------------------


def _enrollment(status: str) -> httpx.Response:
    return _json({"data": {"enrollPlatformKey": {"status": status}}})


async def test_the_enrollment_carries_the_token_an_assertion_and_the_public_jwk(install):
    fake = install(_enrollment("enrolled"))
    key = derive_key(1)

    assert await registration.enroll_platform_key(_HUB, _PLATFORM, _TOKEN, key) == "enrolled"

    request, body = fake.only_request()
    assert body["query"] == registration.ENROLL_MUTATION
    assert body["variables"] == {"input": {"publicKey": key.public_jwk()}}
    assert "d" not in body["variables"]["input"]["publicKey"]
    assert (request.headers["xtm-hub-platform-id"], request.headers["xtm-hub-platform-token"]) == (
        str(_PLATFORM),
        _TOKEN,
    )
    # The proof of possession: it verifies with the JWK sent beside it.
    assert _verify(request.headers[_ASSERTION])["iss"] == str(_PLATFORM)


@pytest.mark.parametrize("status", ["enrolled", "conflict", "not_found", "key_mismatch", "not_available"])
async def test_each_enrollment_answer_is_its_outcome(install, status):
    install(_enrollment(status))
    assert await registration.enroll_platform_key(_HUB, _PLATFORM, _TOKEN, derive_key(1)) == status


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("PLATFORM_KEY_MISMATCH", "key_mismatch"),
        ("PLATFORM_TOKEN_REFUSED", "not_found"),
        ("PLATFORM_TOKEN_PATH_CLOSED", "not_found"),
        ("PLATFORM_ASSERTION_INVALID", "refused"),
        ("FORBIDDEN_ACCESS", "refused"),
    ],
)
async def test_each_enrollment_refusal_is_one_outcome(install, code, expected):
    install(_refusal(code))
    assert await registration.enroll_platform_key(_HUB, _PLATFORM, _TOKEN, derive_key(1)) == expected


async def test_an_older_hub_without_the_operation_is_an_unknown_operation(install):
    # What Apollo answers a mutation its schema does not have.
    install(
        _json(
            {
                "errors": [
                    {
                        "message": 'Cannot query field "enrollPlatformKey" on type "Mutation".',
                        "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"},
                    }
                ]
            },
            status=400,
        )
    )
    assert await registration.enroll_platform_key(_HUB, _PLATFORM, _TOKEN, derive_key(1)) == "unknown_operation"


@pytest.mark.parametrize(
    "answer",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("slow"),
        httpx.Response(503),
        httpx.Response(500),
        httpx.Response(429),
    ],
    ids=["connect-error", "timeout", "503", "500", "429"],
)
async def test_a_network_failure_or_a_5xx_is_unreachable(install, answer):
    install(answer)
    assert await registration.enroll_platform_key(_HUB, _PLATFORM, _TOKEN, derive_key(1)) == "unreachable"


@pytest.mark.parametrize(
    "payload",
    [
        {"data": {"enrollPlatformKey": {"status": "maybe"}}},
        {"data": {"enrollPlatformKey": None}},
        {"data": {}},
    ],
    ids=["unknown-status", "null", "empty"],
)
async def test_an_unexpected_answer_is_refused(install, payload):
    install(_json(payload))
    assert await registration.enroll_platform_key(_HUB, _PLATFORM, _TOKEN, derive_key(1)) == "refused"


async def test_the_enrollment_never_sends_the_token_over_plain_http(install):
    fake = install(_enrollment("enrolled"))
    outcome = await registration.enroll_platform_key("http://hub.example.com", _PLATFORM, _TOKEN, derive_key(1))
    assert outcome == "refused"
    assert fake.requests == []
