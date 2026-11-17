"""A non-ASCII signature or token is a 401, never a 500 or a skipped check.

``hmac.compare_digest`` raises ``TypeError`` when a ``str`` argument holds a
non-ASCII character. Starlette decodes request headers as latin-1 and query
strings as percent-decoded UTF-8, so any byte >= 0x80 an unauthenticated
caller puts in a signature or a token reaches the comparison as exactly such a
character. Where nothing caught the exception the caller got a 500. Where an
``except Exception`` did - around the integration webhook verifiers - the
check was skipped and the delivery went on as if it had been verified.

``timing_safe_str_eq`` (``app.core.security``) compares UTF-8 bytes: still
constant-time, and crafted input is an ordinary mismatch answered like any
other wrong credential. The tripwire at the end fails the build on a new
``compare_digest`` outside the audited sites.

Closing the skip must not refuse a legitimate sender, nor open a new one: a
secret sent verbatim in a header matches in both wire spellings a sender uses
(``timing_safe_header_eq``), and a configured secret that is not a string is a
refusal - never "no secret" when it is falsy, never a 500 when it is not
(``configured_secret``).

Headers are real ``starlette.datastructures.Headers`` built from raw wire
bytes: a plain dict skips the latin-1 decode that turns ``b"caf\\xc3\\xa9"``
into the string that broke the comparison.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import hmac
import json
import time
import uuid
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.security import HTTPBearer
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from starlette.requests import Request

from app.api.v1 import chat as chat_api
from app.api.v1 import evaluations as evaluations_api
from app.api.v1 import metrics as metrics_api
from app.api.v1 import webhooks
from app.api.v1.schemas.evaluation import ResumeEvaluationEpisodeRequest
from app.core import security
from app.core.config import _PROMETHEUS_TOKEN_MIN_LENGTH
from app.core.database import get_db
from app.core.security import (
    UnusableSecretError,
    configured_secret,
    generate_image_token,
    timing_safe_header_eq,
    timing_safe_str_eq,
    verify_github_signature,
    verify_image_token,
    verify_linkedin_signature,
    verify_slack_signature,
)
from app.services.evaluation.resume_secret import stored_trigger_config
from tests import app_sources

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"

#: The issue's reproduction value: ``"café"`` as ``Headers({...})`` hands it back.
NA = "caf\xe9"
#: The same word as a UTF-8 sender actually puts it on the wire.
NA_WIRE = b"caf\xc3\xa9"
SECRET = "s3cr3t"


def _wire(*pairs: tuple[str, bytes]) -> Headers:
    """Real ``Headers`` over raw bytes, exactly as the ASGI server hands them over."""
    return Headers(raw=[(name.lower().encode("latin-1"), value) for name, value in pairs])


class _Delivery(SimpleNamespace):
    """What the webhook handlers read off a request."""

    async def body(self) -> bytes:
        return self._body


def _delivery(headers: Headers, body: bytes = b'{"action":"opened"}') -> _Delivery:
    return _Delivery(
        headers=headers,
        _body=body,
        method="POST",
        url="https://xtm.example/api/hooks/integrations/x",
    )


def _crafted(scheme: str) -> list[tuple[str, bytes]]:
    """One crafted value per scheme, past every prefix and timestamp guard.

    Built at call time: a timestamp taken at collection could be stale by the
    time the test runs, and a stale one is refused before the comparison is
    ever reached - the test would pass with the bug still there.
    """
    now_s = str(int(time.time())).encode()
    now_ms = str(int(time.time() * 1000)).encode()
    return {
        "github": [("X-Hub-Signature-256", b"sha256=" + NA_WIRE)],
        "slack": [("X-Slack-Signature", b"v0=" + NA_WIRE), ("X-Slack-Request-Timestamp", now_s)],
        "linkedin": [("X-LI-Signature", b"hmacsha256=" + NA_WIRE)],
        "hubspot-v3": [("X-HubSpot-Signature-v3", NA_WIRE), ("X-HubSpot-Request-Timestamp", now_ms)],
        "hubspot-v2": [("X-HubSpot-Signature-v2", NA_WIRE)],
        "hubspot-v1": [("X-HubSpot-Signature", NA_WIRE)],
        "perk": [("Tk-Signature", NA_WIRE)],
        "opencti": [("X-OpenCTI-Token", NA_WIRE)],
        "openaev": [("X-OpenAEV-Token", NA_WIRE)],
        "generic": [("X-Signature", NA_WIRE)],
    }[scheme]


_SCHEMES = [
    "github",
    "slack",
    "linkedin",
    "hubspot-v3",
    "hubspot-v2",
    "hubspot-v1",
    "perk",
    "opencti",
    "openaev",
    "generic",
]


def _github_signature(body: bytes) -> bytes:
    return b"sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest().encode()


# ── The helper ──────────────────────────────────────────────────────────────


def test_the_helper_lives_in_core_and_the_old_imports_still_resolve():
    from app.channels import base as channels_base

    assert channels_base.timing_safe_str_eq is security.timing_safe_str_eq
    assert webhooks.timing_safe_str_eq is security.timing_safe_str_eq


@pytest.mark.parametrize(
    "provided",
    ["caf\xe9", "\xff" * 40, "\udcff", "\ud800", "\udfff", "\U0001f600", "\u0661\u0662", ""],
)
def test_the_helper_never_raises_whatever_the_string(provided):
    # A lone surrogate reaches it from a JSON body (``"\ud800"`` escapes), and
    # ``surrogateescape`` can only encode U+DC80..U+DCFF.
    assert timing_safe_str_eq(provided, SECRET) is False
    assert timing_safe_str_eq(SECRET, provided) is False
    assert timing_safe_str_eq(provided, provided) is True


def test_two_different_strings_never_compare_equal():
    # ``surrogateescape`` encodes both of the first pair as b"\xc3\xa9".
    assert timing_safe_str_eq("\udcc3\udca9", "\xe9") is False
    assert timing_safe_str_eq("\ud83d\ude00", "\U0001f600") is False


def test_the_helper_is_compare_digest_over_bytes(monkeypatch):
    calls: list[tuple[object, object]] = []
    real_compare_digest = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real_compare_digest(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)

    assert timing_safe_str_eq(SECRET, SECRET) is True
    assert timing_safe_str_eq(NA, SECRET) is False
    assert len(calls) == 2
    assert all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in calls)


# ── A secret sent verbatim in a header: both wire spellings ─────────────────

#: Non-ASCII secrets a legitimate sender may hold: one inside latin-1, which a
#: Node.js sender (OpenCTI) writes as latin-1 bytes, one outside it, which only
#: a UTF-8 sender (curl, Go) can send at all. The first ends in U+00E0, whose
#: UTF-8 ends in byte 0xA0 - a no-break space once read as latin-1, which
#: ``str.strip()`` would eat.
_NON_ASCII_SECRETS = ["s\xe9cret-voil\xe0", "\u5bc6\u7801-s3cr3t"]


def _as_sent(secret: str) -> list[tuple[str, bytes]]:
    """The wire bytes of *secret* for each encoding a sender may write it in."""
    spellings = [("utf-8", secret.encode("utf-8"))]
    with contextlib.suppress(UnicodeEncodeError):  # a latin-1 sender refuses to send it
        spellings.append(("latin-1", secret.encode("latin-1")))
    return spellings


def _header_value(wire: bytes) -> str:
    """What Starlette hands the app for a header sent as *wire*."""
    return _wire(("X-Webhook-Token", wire)).get("X-Webhook-Token")


@pytest.mark.parametrize("secret", _NON_ASCII_SECRETS)
def test_a_header_carries_a_non_ascii_secret_in_either_wire_spelling(secret):
    for _encoding, wire in _as_sent(secret):
        assert timing_safe_header_eq(_header_value(wire), secret) is True
    assert timing_safe_header_eq(_header_value(NA_WIRE), secret) is False
    assert timing_safe_header_eq(_header_value(b"s3cr3t"), secret) is False


@pytest.mark.parametrize(
    "wire", [NA_WIRE, b"s3cr3", b"s3cr3t ", b"S3CR3T", b"s3cr3t\xc3\xa9", b"", b"\xff" * 6]
)
def test_for_an_ascii_secret_only_the_secret_itself_matches(wire):
    """The second comparison widens nothing: the latin-1 bytes of an ASCII
    string are the string."""
    assert timing_safe_header_eq(_header_value(wire), SECRET) is False
    assert timing_safe_header_eq(_header_value(SECRET.encode()), SECRET) is True


def test_the_header_comparison_always_runs_both_comparisons_over_bytes(monkeypatch):
    calls: list[tuple[object, object]] = []
    real_compare_digest = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real_compare_digest(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)

    assert timing_safe_header_eq(SECRET, SECRET) is True
    assert len(calls) == 2
    assert all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in calls)


def test_a_value_no_header_could_hold_is_compared_as_text_and_never_raises():
    # Above U+00FF: not something Starlette decodes out of header bytes.
    assert timing_safe_header_eq("\u5bc6\u7801", "\u5bc6\u7801") is True
    assert timing_safe_header_eq("\u5bc6\u7801", SECRET) is False
    assert timing_safe_header_eq("\ud800", SECRET) is False


# ── A secret read out of a JSON config ──────────────────────────────────────

#: Configured values no sender can prove. Falsy ones included: only an absent,
#: ``null`` or empty secret means none is configured.
_UNUSABLE_SECRETS = [False, True, 1.5, 0.0, [], {}, [SECRET], {"k": SECRET}, "\ud800"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("", None), (SECRET, SECRET), (" ", " "), (12345, "12345"), (0, "0")],
)
def test_configured_secret_reads_what_a_sender_can_prove(value, expected):
    assert configured_secret(value) == expected


@pytest.mark.parametrize("value", _UNUSABLE_SECRETS)
def test_configured_secret_refuses_what_no_sender_can_prove(value):
    with pytest.raises(UnusableSecretError):
        configured_secret(value)


# ── The shared verifiers in core/security.py ────────────────────────────────


def test_the_issue_reproduction_is_a_mismatch_not_a_type_error():
    """The four calls from the issue, verbatim: each raised ``TypeError``."""
    assert verify_github_signature(b"{}", SECRET, "sha256=" + NA) is False
    assert verify_slack_signature(b"{}", SECRET, "v0=" + NA, str(int(time.time()))) is False
    assert verify_linkedin_signature(b"{}", SECRET, "hmacsha256=" + NA) is False
    assert verify_image_token("img1", NA, int(time.time()) + 600) is False


def test_a_correct_signature_still_verifies():
    body = b'{"action":"opened"}'
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert verify_github_signature(body, SECRET, f"sha256={digest}") is True
    assert verify_linkedin_signature(body, SECRET, f"hmacsha256={digest}") is True

    ts = str(int(time.time()))
    base = f"v0:{ts}:{body.decode()}".encode()
    slack = "v0=" + hmac.new(SECRET.encode(), base, hashlib.sha256).hexdigest()
    assert verify_slack_signature(body, SECRET, slack, ts) is True

    token, expires = generate_image_token("img1")
    assert verify_image_token("img1", token, expires) is True
    assert verify_image_token("img2", token, expires) is False


def _linkedin_documented(body: bytes, secret: str = SECRET) -> str:
    """LinkedIn's documented ``X-LI-Signature``: the bare hex HMAC-SHA256 of
    ``hmacsha256=`` followed by the raw body, keyed with the client secret."""
    return hmac.new(secret.encode(), b"hmacsha256=" + body, hashlib.sha256).hexdigest()


def test_the_linkedin_signature_linkedin_documents_verifies():
    """https://learn.microsoft.com/linkedin/shared/api-guide/webhook-validation:
    "The X-LI-Signature header contains only the hex digest. The hmacsha256=
    string is used only when building the string-to-sign." The verifier used
    to require the prefix in the header, refusing that exact value."""
    body = b'{"type":"ORGANIZATION_SOCIAL_ACTION_NOTIFICATIONS","notifications":[]}'
    assert verify_linkedin_signature(body, SECRET, _linkedin_documented(body)) is True
    # The form the platform always checked keeps verifying.
    legacy = "hmacsha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert verify_linkedin_signature(body, SECRET, legacy) is True
    # Each is bound to its body and its key.
    assert verify_linkedin_signature(body + b" ", SECRET, _linkedin_documented(body)) is False
    assert verify_linkedin_signature(body, "other", _linkedin_documented(body)) is False
    assert verify_linkedin_signature(body, SECRET, "hmacsha256=" + _linkedin_documented(body)) is False
    assert verify_linkedin_signature(body, SECRET, _linkedin_documented(body).upper()) is False
    assert verify_linkedin_signature(body, SECRET, "") is False


def test_an_empty_linkedin_client_secret_verifies_nothing():
    """Anyone can compute an HMAC keyed with ``b""``, and an OAuth default saved
    with an empty client secret still holds a ciphertext: both LinkedIn gates
    call the verifier, which must refuse rather than accept the forgery."""
    body = b'{"type":"ORGANIZATION_SOCIAL_ACTION_NOTIFICATIONS","notifications":[]}'
    forgeries = (
        _linkedin_documented(body, ""),
        "hmacsha256=" + hmac.new(b"", body, hashlib.sha256).hexdigest(),
    )
    for signature in forgeries:
        assert verify_linkedin_signature(body, "", signature) is False


def test_a_captured_linkedin_signature_verifies_no_second_body():
    """The legacy string-to-sign is the body alone, so over ``hmacsha256=`` + B
    it is the documented string-to-sign of B: prefixed, the signature LinkedIn
    sent for B would verify that second body - a replay the dedup cache, keyed
    on the body, does not recognise."""
    body = b'{"type":"ORGANIZATION_SOCIAL_ACTION_NOTIFICATIONS","notifications":[]}'
    captured = _linkedin_documented(body)
    assert verify_linkedin_signature(body, SECRET, captured) is True
    assert verify_linkedin_signature(b"hmacsha256=" + body, SECRET, "hmacsha256=" + captured) is False
    # The legacy form still verifies any body a sender actually posts.
    legacy = "hmacsha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert verify_linkedin_signature(body, SECRET, legacy) is True


async def test_the_linkedin_provider_gate_refuses_a_delivery_signed_with_an_empty_key(monkeypatch):
    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda _enc: "")
    body = b'{"type":"ORGANIZATION_SOCIAL_ACTION_NOTIFICATIONS","notifications":[]}'
    request = _delivery(_wire(("X-LI-Signature", _linkedin_documented(body, "").encode())), body=body)
    with pytest.raises(HTTPException) as exc_info:
        await webhooks._verify_linkedin_provider_signature(body, "linkedin", "enc", request)
    assert exc_info.value.status_code == 401


# ── Webhooks: the verifiers ─────────────────────────────────────────────────


@pytest.mark.parametrize("scheme", _SCHEMES)
def test_the_integration_verifier_answers_every_crafted_scheme_with_401(scheme):
    """``_verify_signature`` serves the integration, per-assignment and
    provider routes: GitHub, Slack, LinkedIn, HubSpot v3 / v2 / v1, Perk,
    ``X-OpenCTI-Token`` and the generic ``X-Signature``."""
    with pytest.raises(HTTPException) as exc_info:
        webhooks._verify_signature(b"{}", SECRET, _delivery(_wire(*_crafted(scheme))))
    assert exc_info.value.status_code == 401


def test_a_hubspot_timestamp_too_long_for_a_float_is_a_401():
    """Not a comparison, the same failure: ``int()`` accepts the digits, the
    subtraction from ``time.time()`` raises ``OverflowError`` - a 500 before,
    or a skipped check behind the integration routes' ``except Exception``."""
    headers = _wire(
        ("X-HubSpot-Signature-v3", b"sig"),
        ("X-HubSpot-Request-Timestamp", b"1" + b"0" * 400),
    )
    with pytest.raises(HTTPException) as exc_info:
        webhooks._verify_hubspot_signature(b"[]", "client-secret", _delivery(headers))
    assert exc_info.value.status_code == 401


@pytest.mark.parametrize("scheme", _SCHEMES)
def test_the_integration_gate_rejects_a_crafted_value_instead_of_skipping_the_check(
    monkeypatch, scheme
):
    """The gate used to tolerate every error raised while checking, not only an
    unreadable config: the ``TypeError`` was logged and the delivery accepted."""
    monkeypatch.setattr(
        webhooks, "decrypt_value", lambda _enc: json.dumps({"webhook_secret": SECRET})
    )
    with pytest.raises(HTTPException) as exc_info:
        webhooks._try_verify_webhook_signature(
            b"{}", "enc", _delivery(_wire(*_crafted(scheme)))
        )
    assert exc_info.value.status_code == 401


def test_an_error_while_verifying_is_not_taken_for_an_unreadable_config(monkeypatch):
    monkeypatch.setattr(
        webhooks, "decrypt_value", lambda _enc: json.dumps({"webhook_secret": SECRET})
    )

    def _broken_verifier(*_args, **_kwargs):
        raise RuntimeError("the verifier itself failed")

    monkeypatch.setattr(webhooks, "_verify_signature", _broken_verifier)
    with pytest.raises(RuntimeError):
        webhooks._try_verify_webhook_signature(b"{}", "enc", _delivery(_wire()))


# ── Webhooks: the X-OpenCTI-Token check of the per-assignment route ─────────


class TestTheOpenCTITokenCheckFailsClosed:
    """``_verify_opencti_token_header`` backs both inline checks of
    ``receive_assignment_webhook`` (before and after the dedup claim). Their
    ``except Exception`` skipped the comparison when it raised."""

    @staticmethod
    def _request(token: bytes | None = NA_WIRE) -> _Delivery:
        pairs = [("X-OpenCTI-Token", token)] if token is not None else []
        return _delivery(_wire(*pairs))

    @staticmethod
    def _config(monkeypatch, config: object) -> None:
        monkeypatch.setattr(webhooks, "decrypt_value", lambda _enc: json.dumps(config))

    def test_a_crafted_token_is_refused(self, monkeypatch):
        self._config(monkeypatch, {"webhook_secret": SECRET})
        with pytest.raises(HTTPException) as exc_info:
            webhooks._verify_opencti_token_header("enc", self._request())
        assert (exc_info.value.status_code, exc_info.value.detail) == (
            401,
            "Invalid OpenCTI webhook token",
        )

    def test_the_right_token_passes(self, monkeypatch):
        self._config(monkeypatch, {"webhook_secret": SECRET})
        webhooks._verify_opencti_token_header("enc", self._request(SECRET.encode()))

    def test_without_the_header_nothing_is_read(self, monkeypatch):
        def _must_not_decrypt(_enc):
            raise AssertionError("no token presented, nothing to check")

        monkeypatch.setattr(webhooks, "decrypt_value", _must_not_decrypt)
        webhooks._verify_opencti_token_header("enc", self._request(None))

    @pytest.mark.parametrize(
        "config",
        [{"base_url": "https://opencti.example"}, {"webhook_secret": None}, {"webhook_secret": ""}],
    )
    def test_without_a_configured_secret_a_token_proves_nothing_and_is_let_through(
        self, monkeypatch, config
    ):
        """Unchanged: an integration with no secret is unauthenticated by design."""
        self._config(monkeypatch, config)
        webhooks._verify_opencti_token_header("enc", self._request())

    def test_a_token_that_cannot_be_checked_is_refused(self, monkeypatch):
        """Fail closed: the delivery presented a credential, and the config it
        must be checked against is unreadable - the 503 every webhook gate
        answers for an unreadable secret."""

        def _unreadable(_enc):
            raise ValueError("InvalidToken")

        monkeypatch.setattr(webhooks, "decrypt_value", _unreadable)
        for token in (SECRET.encode(), NA_WIRE):
            with pytest.raises(HTTPException) as exc_info:
                webhooks._verify_opencti_token_header("enc", self._request(token))
            assert exc_info.value.status_code == 503

    @pytest.mark.parametrize("secret", _UNUSABLE_SECRETS)
    def test_a_secret_that_is_not_a_string_refuses_every_token(self, monkeypatch, secret):
        """Falsy ones included: only an absent or empty secret is "none configured"."""
        self._config(monkeypatch, {"webhook_secret": secret})
        for presented in (str(secret).encode("utf-8", "surrogatepass"), SECRET.encode(), NA_WIRE):
            with pytest.raises(HTTPException) as exc_info:
                webhooks._verify_opencti_token_header("enc", self._request(presented))
            assert (exc_info.value.status_code, exc_info.value.detail) == (
                401,
                "Invalid OpenCTI webhook token",
            )

    @pytest.mark.parametrize("secret", [12345, 0])
    def test_a_json_number_secret_is_compared_as_its_digits(self, monkeypatch, secret):
        """Typed without quotes, it is still the secret the sender was given."""
        self._config(monkeypatch, {"webhook_secret": secret})
        webhooks._verify_opencti_token_header("enc", self._request(str(secret).encode()))
        with pytest.raises(HTTPException) as exc_info:
            webhooks._verify_opencti_token_header("enc", self._request(b"1"))
        assert exc_info.value.status_code == 401

    @pytest.mark.parametrize("secret", _NON_ASCII_SECRETS)
    def test_a_non_ascii_secret_matches_in_either_wire_spelling(self, monkeypatch, secret):
        self._config(monkeypatch, {"webhook_secret": secret})
        for _encoding, wire in _as_sent(secret):
            webhooks._verify_opencti_token_header("enc", self._request(wire))
        with pytest.raises(HTTPException):
            webhooks._verify_opencti_token_header("enc", self._request(NA_WIRE))


# ── Webhooks: a configured secret that is not a string ──────────────────────

_BODY = b'{"action":"opened"}'


def _signed(scheme: str, secret: str = SECRET, body: bytes = _BODY) -> list[tuple[str, bytes]]:
    """What a legitimate sender holding *secret* puts on the wire, per scheme."""
    key = secret.encode()
    digest = hmac.new(key, body, hashlib.sha256).hexdigest()
    now_s = str(int(time.time()))
    slack = hmac.new(key, f"v0:{now_s}:{body.decode()}".encode(), hashlib.sha256).hexdigest()
    return {
        "github": [("X-Hub-Signature-256", f"sha256={digest}".encode())],
        "slack": [
            ("X-Slack-Signature", f"v0={slack}".encode()),
            ("X-Slack-Request-Timestamp", now_s.encode()),
        ],
        "linkedin": [("X-LI-Signature", _linkedin_documented(body, secret).encode())],
        "perk": [("Tk-Signature", digest.encode())],
        "generic": [("X-Signature", digest.encode())],
        "opencti": [("X-OpenCTI-Token", key)],
        "unsigned": [],
    }[scheme]


@pytest.mark.parametrize("scheme", ["github", "opencti", "generic", "unsigned"])
@pytest.mark.parametrize("secret", _UNUSABLE_SECRETS)
def test_the_integration_gate_refuses_every_delivery_for_a_secret_that_is_not_a_string(
    monkeypatch, secret, scheme
):
    """On ``main`` a falsy value read as "no secret" and a truthy one raised
    inside the ``except Exception``: every delivery went through. With the
    verifier out of that ``try``, a truthy one became an unauthenticated 500."""
    monkeypatch.setattr(
        webhooks, "decrypt_value", lambda _enc: json.dumps({"webhook_secret": secret})
    )
    with pytest.raises(HTTPException) as exc_info:
        webhooks._try_verify_webhook_signature(
            _BODY, "enc", _delivery(_wire(*_signed(scheme)), body=_BODY)
        )
    assert (exc_info.value.status_code, exc_info.value.detail) == (
        401,
        "Invalid webhook signature",
    )


def test_the_integration_gate_compares_a_json_number_secret_as_its_digits(monkeypatch):
    monkeypatch.setattr(
        webhooks, "decrypt_value", lambda _enc: json.dumps({"webhook_secret": 12345})
    )
    for scheme in ("github", "opencti", "generic"):
        webhooks._try_verify_webhook_signature(
            _BODY, "enc", _delivery(_wire(*_signed(scheme, "12345")), body=_BODY)
        )
    with pytest.raises(HTTPException):
        webhooks._try_verify_webhook_signature(
            _BODY, "enc", _delivery(_wire(*_signed("opencti", "1234")), body=_BODY)
        )


@pytest.mark.parametrize("secret", _UNUSABLE_SECRETS)
def test_the_assignment_gate_refuses_every_delivery_for_a_secret_that_is_not_a_string(
    monkeypatch, secret
):
    """``trigger_config`` is JSON too: a falsy value read as "no secret" (the
    URL alone authenticated the delivery), a truthy one raised - a 500 on the
    direct, flow and legacy routes alike."""
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    presented = [
        [("X-Webhook-Token", SECRET.encode())],
        [("Authorization", b"Bearer " + SECRET.encode())],
        _signed("github"),
        [],
    ]
    for pairs in presented:
        with pytest.raises(HTTPException) as exc_info:
            webhooks._verify_assignment_webhook_secret(
                _BODY, _delivery(_wire(*pairs), body=_BODY), secret, assignment_id="a-1"
            )
        assert (exc_info.value.status_code, exc_info.value.detail) == (
            401,
            "Invalid webhook signature",
        )


# ── Webhooks: a legitimate non-ASCII secret, in either wire spelling ────────


@pytest.mark.parametrize("secret", _NON_ASCII_SECRETS)
def test_the_integration_gate_accepts_a_non_ascii_token_in_either_wire_spelling(
    monkeypatch, secret
):
    """On ``main`` the comparison raised and the check was skipped, so both
    spellings got through. Closing the skip must not refuse the right one:
    Node.js (OpenCTI's sender) writes latin-1, curl and Go write UTF-8."""
    monkeypatch.setattr(
        webhooks, "decrypt_value", lambda _enc: json.dumps({"webhook_secret": secret})
    )
    for _encoding, wire in _as_sent(secret):
        webhooks._try_verify_webhook_signature(
            _BODY, "enc", _delivery(_wire(("X-OpenCTI-Token", wire)), body=_BODY)
        )
    with pytest.raises(HTTPException) as exc_info:
        webhooks._try_verify_webhook_signature(
            _BODY, "enc", _delivery(_wire(("X-OpenCTI-Token", NA_WIRE)), body=_BODY)
        )
    assert exc_info.value.detail == "Invalid OpenCTI webhook token"


@pytest.mark.parametrize("scheme", ["github", "slack", "linkedin", "perk", "generic"])
@pytest.mark.parametrize("secret", _NON_ASCII_SECRETS)
def test_a_body_signature_keyed_with_a_non_ascii_secret_still_verifies(scheme, secret):
    """Unchanged: senders key the HMAC with the secret's UTF-8 bytes, and the
    digest they send is ASCII."""
    webhooks._verify_signature(_BODY, secret, _delivery(_wire(*_signed(scheme, secret)), body=_BODY))


@pytest.mark.parametrize(
    "header", ["X-Webhook-Token", "X-Webhook-Secret", "X-OpenCTI-Token", "Authorization"]
)
@pytest.mark.parametrize("secret", _NON_ASCII_SECRETS)
def test_the_assignment_gate_accepts_a_non_ascii_token_in_either_wire_spelling(
    monkeypatch, secret, header
):
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)

    def _presenting(wire: bytes) -> _Delivery:
        value = b"Bearer " + wire if header == "Authorization" else wire
        return _delivery(_wire((header, value)), body=_BODY)

    for _encoding, wire in _as_sent(secret):
        webhooks._verify_assignment_webhook_secret(_BODY, _presenting(wire), secret)
    with pytest.raises(HTTPException) as exc_info:
        webhooks._verify_assignment_webhook_secret(_BODY, _presenting(NA_WIRE), secret)
    assert exc_info.value.status_code == 401


# ── Webhooks: hostile header values never raise ─────────────────────────────

#: Values a caller controls, for every header a gate reads: empty and blank,
#: non-numeric / infinite / negative / overflowing timestamps, invalid hex and
#: base64, bare or wrong prefixes, raw bytes >= 0x80, and very long values.
_HOSTILE = [
    b"",
    b" ",
    b"\t",
    b"nan",
    b"inf",
    b"-inf",
    b"1e999",
    b"-1",
    b"0",
    b"1" + b"0" * 400,
    b"9" * 5000,
    b"\xff" * 64,
    NA_WIRE,
    b"sha256=",
    b"sha256=" + b"g" * 64,
    b"v0=",
    b"hmacsha256=",
    b"hmacsha256=" + NA_WIRE,
    b"==",
    b"not base64!",
    b"Bearer",
    b"Bearer ",
    b"x" * 8000,
]

_SIGNATURE_HEADERS = [
    "X-Hub-Signature-256",
    "X-Slack-Signature",
    "X-LI-Signature",
    "X-HubSpot-Signature-v3",
    "X-HubSpot-Signature-v2",
    "X-HubSpot-Signature",
    "Tk-Signature",
    "X-OpenCTI-Token",
    "X-Signature",
]


def _hostile_deliveries(headers: list[str]):
    """Each header alone with each hostile value, the timestamped schemes with
    a hostile timestamp next to a fresh one, and a duplicated header whose
    first value is hostile and second one valid (Starlette reads the first)."""
    now_s = str(int(time.time())).encode()
    now_ms = str(int(time.time() * 1000)).encode()
    for header in headers:
        for value in _HOSTILE:
            pairs = [(header, value)]
            if header == "X-Slack-Signature":
                pairs.append(("X-Slack-Request-Timestamp", now_s))
            if header == "X-HubSpot-Signature-v3":
                pairs.append(("X-HubSpot-Request-Timestamp", now_ms))
            yield pairs
    for value in _HOSTILE:
        yield [("X-Slack-Signature", b"v0=" + b"0" * 64), ("X-Slack-Request-Timestamp", value)]
        yield [("X-HubSpot-Signature-v3", b"c2ln"), ("X-HubSpot-Request-Timestamp", value)]
    yield [("X-Hub-Signature-256", NA_WIRE), *_signed("github")]


def _always_refused(gate, pairs) -> None:
    try:
        gate(_delivery(_wire(*pairs), body=_BODY))
    except HTTPException as exc:
        assert exc.status_code == 401, (pairs, exc.detail)
    else:
        raise AssertionError(f"accepted: {pairs!r}")


def test_the_integration_gate_refuses_every_hostile_header_with_a_401():
    for pairs in _hostile_deliveries(_SIGNATURE_HEADERS):
        _always_refused(lambda request: webhooks._verify_signature(_BODY, SECRET, request), pairs)


def test_the_hubspot_verifier_refuses_every_hostile_header_with_a_401():
    headers = ["X-HubSpot-Signature-v3", "X-HubSpot-Signature-v2", "X-HubSpot-Signature"]
    for pairs in _hostile_deliveries(headers):
        _always_refused(
            lambda request: webhooks._verify_hubspot_signature(_BODY, SECRET, request), pairs
        )


def test_the_assignment_gate_refuses_every_hostile_header_with_a_401(monkeypatch):
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    headers = [
        "X-Webhook-Token",
        "X-Webhook-Secret",
        "X-OpenCTI-Token",
        "X-Hub-Signature-256",
        "X-Slack-Signature",
        "X-Signature",
        "Authorization",
    ]
    for pairs in _hostile_deliveries(headers):
        _always_refused(
            lambda request: webhooks._verify_assignment_webhook_secret(_BODY, request, SECRET),
            pairs,
        )


def test_the_opencti_token_check_refuses_every_hostile_token_with_a_401(monkeypatch):
    monkeypatch.setattr(
        webhooks, "decrypt_value", lambda _enc: json.dumps({"webhook_secret": SECRET})
    )
    for value in _HOSTILE:
        if not value:
            continue  # no token presented: nothing to check
        _always_refused(
            lambda request: webhooks._verify_opencti_token_header("enc", request),
            [("X-OpenCTI-Token", value)],
        )


# ── Webhooks: the HubSpot secret and signature ──────────────────────────────


class TestTheHubSpotSecret:
    """``_hubspot_secret`` backs the four HubSpot checks (pre-claim and
    handler, integration and per-assignment routes)."""

    @staticmethod
    def _read(monkeypatch, config: object) -> str | None:
        monkeypatch.setattr(webhooks, "decrypt_value", lambda _enc: json.dumps(config))
        return webhooks._hubspot_secret("enc", context="integration=x")

    def test_an_unreadable_config_refuses_the_delivery(self, monkeypatch):
        """Fail closed with the 503 every webhook gate answers; no
        config at all is still no check."""

        def _unreadable(_enc):
            raise ValueError("InvalidToken")

        monkeypatch.setattr(webhooks, "decrypt_value", _unreadable)
        with pytest.raises(HTTPException) as exc_info:
            webhooks._hubspot_secret("enc", context="integration=x")
        assert exc_info.value.status_code == 503
        assert webhooks._hubspot_secret(None, context="integration=x") is None

    @pytest.mark.parametrize(
        ("config", "expected"),
        [
            ({}, None),
            ({"client_secret": "cs", "webhook_secret": "ws"}, "cs"),
            ({"client_secret": "", "webhook_secret": "ws"}, "ws"),
            ({"client_secret": None, "webhook_secret": "ws"}, "ws"),
            ({"webhook_secret": "ws"}, "ws"),
            ({"client_secret": 12345}, "12345"),
            # A value no sender can prove gives way to one it can.
            ({"client_secret": ["cs"], "webhook_secret": "ws"}, "ws"),
            ({"client_secret": "cs", "webhook_secret": False}, "cs"),
        ],
    )
    def test_the_client_secret_then_the_webhook_secret(self, monkeypatch, config, expected):
        assert self._read(monkeypatch, config) == expected

    @pytest.mark.parametrize("secret", _UNUSABLE_SECRETS)
    @pytest.mark.parametrize("field", ["client_secret", "webhook_secret"])
    def test_nothing_usable_refuses_instead_of_skipping_the_check(self, monkeypatch, field, secret):
        """On ``main`` a falsy value skipped the check, a truthy one raised
        inside the ``except Exception`` and skipped it too."""
        with pytest.raises(HTTPException) as exc_info:
            self._read(monkeypatch, {field: secret})
        assert (exc_info.value.status_code, exc_info.value.detail) == (
            401,
            "Invalid HubSpot signature",
        )


def _no_session():
    raise AssertionError("the database was read before the HubSpot signature was checked")


@pytest.mark.parametrize(
    "config", [{"client_secret": SECRET}, {"client_secret": True}, {"webhook_secret": []}]
)
@pytest.mark.parametrize(
    "site", ["batch-handler", "assignment-batch-handler", "integration-route", "assignment-route"]
)
async def test_every_hubspot_check_refuses_before_anything_is_read(monkeypatch, site, config):
    """The four HubSpot checks the verifier left the ``except Exception`` of, one
    by one: a crafted signature, or a secret no sender can prove, is a 401
    before the dedup cache or the database - never a 500, never a skip."""
    monkeypatch.setattr(webhooks, "decrypt_value", lambda _enc: json.dumps(config))
    now_ms = str(int(time.time() * 1000)).encode()
    request = _delivery(
        _wire(("X-HubSpot-Signature-v3", NA_WIRE), ("X-HubSpot-Request-Timestamp", now_ms)),
        body=b"[]",
    )
    integration = _integration("hubspot")
    if site in ("integration-route", "assignment-route"):
        _serve(monkeypatch, integration)
        _forbid_dedup_claim(monkeypatch)
    else:
        monkeypatch.setattr(webhooks, "async_session", _no_session)

    with pytest.raises(HTTPException) as exc_info:
        if site == "batch-handler":
            await webhooks._handle_hubspot_webhook(
                str(integration.id), integration, b"[]", [], request
            )
        elif site == "assignment-batch-handler":
            await webhooks._handle_hubspot_assignment_webhook(
                str(integration.id), integration, b"[]", [], str(uuid.uuid4()), request
            )
        elif site == "integration-route":
            await webhooks.receive_integration_webhook(str(uuid.uuid4()), request)
        else:
            await webhooks.receive_assignment_webhook(
                str(uuid.uuid4()), str(uuid.uuid4()), request
            )
    assert exc_info.value.status_code == 401


def _starlette_request(headers: list[tuple[bytes, bytes]], query: bytes) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "server": ("xtm.example", 443),
            "path": "/api/hooks/integrations/x",
            "query_string": query,
            "headers": [(b"host", b"xtm.example"), *headers],
        }
    )


@pytest.mark.parametrize("version", ["v3", "v2"])
def test_a_url_that_cannot_be_spelled_is_a_hubspot_mismatch_not_a_500(version):
    """HubSpot signs the URL, and Starlette spells a raw non-UTF-8 query byte by
    raising. uvicorn refuses such a byte before the app runs; another ASGI
    server might not, and the verifier no longer runs inside a catch-all."""
    headers = (
        [
            (b"x-hubspot-signature-v3", b"sig"),
            (b"x-hubspot-request-timestamp", str(int(time.time() * 1000)).encode()),
        ]
        if version == "v3"
        else [(b"x-hubspot-signature-v2", b"sig")]
    )
    request = _starlette_request(headers, query=b"a=\xff")
    with pytest.raises(UnicodeDecodeError):
        str(request.url)
    with pytest.raises(HTTPException) as exc_info:
        webhooks._verify_hubspot_signature(b"[]", "client-secret", request)
    assert (exc_info.value.status_code, exc_info.value.detail) == (
        401,
        f"Invalid HubSpot {version} signature",
    )


def test_a_hubspot_signature_over_a_spellable_url_still_verifies():
    body = b"[]"
    request = _starlette_request([], query=b"a=1")
    message = f"POST{request.url}{body.decode()}"
    signature = hashlib.sha256(("client-secret" + message).encode()).hexdigest()
    request = _starlette_request([(b"x-hubspot-signature-v2", signature.encode())], query=b"a=1")
    webhooks._verify_hubspot_signature(body, "client-secret", request)


# ── Webhooks: the routes ────────────────────────────────────────────────────


class _Session:
    """``async_session()`` answering each query with the next row given (the
    last one repeats)."""

    def __init__(self, rows: list[object]):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, *_args, **_kwargs):
        row = self._rows.pop(0) if len(self._rows) > 1 else self._rows[0]
        return SimpleNamespace(
            scalar_one_or_none=lambda: row,
            scalars=lambda: SimpleNamespace(all=lambda: [row]),
        )


def _serve(monkeypatch, *rows: object) -> None:
    session = _Session(list(rows))
    monkeypatch.setattr(webhooks, "async_session", lambda: session)

    async def _allow(*_args, **_kwargs):
        return None

    monkeypatch.setattr(webhooks, "enforce_rate_limit", _allow)
    monkeypatch.setattr(webhooks, "_clear_direct_webhook_drift_state", _allow)


def _forbid_dedup_claim(monkeypatch) -> None:
    async def _claim(*_args, **_kwargs):
        raise AssertionError(
            "the dedup slot was claimed: the signature check was skipped"
        )

    monkeypatch.setattr(webhooks, "_claim_webhook_dedup_slot", _claim)


def _integration(provider: str, extra_config_enc: str | None = "enc") -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), provider=provider, extra_config_enc=extra_config_enc)


async def test_the_direct_assignment_route_answers_a_crafted_signature_with_401(monkeypatch):
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        trigger_type="webhook",
        enabled=True,
        trigger_config={"webhook_secret": SECRET},
        agent_id=uuid.uuid4(),
        name="WF",
        # The two columns the owner-deactivation rule reads: ownerless.
        user_id=None,
        is_company_managed=False,
    )
    _serve(monkeypatch, assignment)
    _forbid_dedup_claim(monkeypatch)

    request = _delivery(_wire(*_crafted("github")))
    with pytest.raises(HTTPException) as exc_info:
        await webhooks.receive_direct_assignment_webhook(str(assignment.id), request)
    assert exc_info.value.status_code == 401


@pytest.mark.parametrize(
    ("provider", "config", "scheme", "body"),
    [
        ("github", {"webhook_secret": SECRET}, "github", b'{"action":"opened"}'),
        ("opencti", {"webhook_secret": SECRET}, "opencti", b'{"type":"create"}'),
        ("hubspot", {"client_secret": "hs-secret"}, "hubspot-v3", b"[]"),
    ],
)
async def test_the_integration_route_rejects_a_crafted_value_before_the_dedup_cache(
    monkeypatch, provider, config, scheme, body
):
    """The bypass: this route's gates swallowed the ``TypeError``, so a crafted
    header reached the dedup cache and the assignments unverified."""
    _serve(monkeypatch, _integration(provider))
    monkeypatch.setattr(webhooks, "decrypt_value", lambda _enc: json.dumps(config))
    _forbid_dedup_claim(monkeypatch)

    request = _delivery(_wire(*_crafted(scheme)), body=body)
    with pytest.raises(HTTPException) as exc_info:
        await webhooks.receive_integration_webhook(str(uuid.uuid4()), request)
    assert exc_info.value.status_code == 401


@pytest.mark.parametrize(
    ("provider", "config", "scheme", "body"),
    [
        ("github", {"webhook_secret": [SECRET]}, "github", b'{"action":"opened"}'),
        ("opencti", {"webhook_secret": False}, "opencti", b'{"type":"create"}'),
        ("opencti", {"webhook_secret": 0.0}, "unsigned", b'{"type":"create"}'),
        ("hubspot", {"client_secret": True}, "hubspot-v3", b"[]"),
        ("hubspot", {"client_secret": {}}, "hubspot-v1", b"[]"),
    ],
)
async def test_the_integration_route_refuses_a_secret_that_is_not_a_string_before_the_dedup_cache(
    monkeypatch, provider, config, scheme, body
):
    """A 500 for a truthy one, an unchecked delivery for a falsy one - never again."""
    _serve(monkeypatch, _integration(provider))
    monkeypatch.setattr(webhooks, "decrypt_value", lambda _enc: json.dumps(config))
    _forbid_dedup_claim(monkeypatch)

    pairs = [] if scheme == "unsigned" else _crafted(scheme)
    with pytest.raises(HTTPException) as exc_info:
        await webhooks.receive_integration_webhook(
            str(uuid.uuid4()), _delivery(_wire(*pairs), body=body)
        )
    assert exc_info.value.status_code == 401


@pytest.mark.parametrize("body", [b"[1, 2]", b'"a string"', b"42", b"null"])
async def test_the_provider_route_answers_a_json_body_that_is_not_an_object_with_its_401(
    monkeypatch, body
):
    """``payload.get`` on a list or a scalar was an unauthenticated 500 before the
    signature was ever checked; the body now gets the integration route's shape."""
    routing = webhooks._ProviderRouting(
        oauth_default_exists=True,
        extra_config_enc="enc",
        client_secret_enc=None,
        has_agents=True,
        matchers=(),
    )

    async def _routing(_provider):
        return routing

    async def _no_refresh(_provider):
        return None

    monkeypatch.setattr(webhooks, "_get_provider_routing", _routing)
    monkeypatch.setattr(webhooks, "_refresh_provider_routing_after_auth_failure", _no_refresh)
    monkeypatch.setattr(
        webhooks, "decrypt_value", lambda _enc: json.dumps({"webhook_secret": SECRET})
    )
    _forbid_dedup_claim(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        await webhooks.receive_provider_webhook("github", _delivery(_wire(), body=body))
    assert (exc_info.value.status_code, exc_info.value.detail) == (
        401,
        "Missing webhook signature",
    )


async def test_the_per_assignment_route_refuses_an_unreadable_config(monkeypatch):
    """A config the platform cannot decrypt may hold a secret, so the delivery
    is refused before the dedup claim - a 503, not a skipped check,
    even for a correct OpenCTI token."""
    _serve(monkeypatch, _integration("opencti"))

    def _unreadable(_enc):
        raise ValueError("InvalidToken")

    monkeypatch.setattr(webhooks, "decrypt_value", _unreadable)
    _forbid_dedup_claim(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        await webhooks.receive_assignment_webhook(
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            _delivery(_wire(("X-OpenCTI-Token", SECRET.encode())), body=b'{"type":"create"}'),
        )
    assert exc_info.value.status_code == 503


@pytest.mark.parametrize(
    "valid_signature",
    [False, True],
    ids=["token-alone", "token-next-to-a-valid-signature"],
)
async def test_the_per_assignment_route_refuses_a_crafted_opencti_token(
    monkeypatch, valid_signature
):
    """Next to a valid GitHub signature, ``_verify_signature`` never reads the
    token - only the inline check does, and its comparison raised."""
    body = b'{"type":"create"}'
    pairs = [("X-OpenCTI-Token", NA_WIRE)]
    if valid_signature:
        pairs.insert(0, ("X-Hub-Signature-256", _github_signature(body)))
    _serve(monkeypatch, _integration("opencti"))
    monkeypatch.setattr(
        webhooks, "decrypt_value", lambda _enc: json.dumps({"webhook_secret": SECRET})
    )
    _forbid_dedup_claim(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        await webhooks.receive_assignment_webhook(
            str(uuid.uuid4()), str(uuid.uuid4()), _delivery(_wire(*pairs), body=body)
        )
    assert (exc_info.value.status_code, exc_info.value.detail) == (
        401,
        "Invalid OpenCTI webhook token",
    )


async def test_the_second_opencti_token_check_refuses_a_crafted_token_too(monkeypatch):
    """The route re-reads the integration after the dedup claim and checks the
    token again. The first read here finds no config (the secret was set in
    between), so the second check is the only one standing."""
    integration_id = uuid.uuid4()
    _serve(
        monkeypatch,
        SimpleNamespace(id=integration_id, provider="opencti", extra_config_enc=None),
        SimpleNamespace(id=integration_id, provider="opencti", extra_config_enc="enc"),
    )
    monkeypatch.setattr(
        webhooks, "decrypt_value", lambda _enc: json.dumps({"webhook_secret": SECRET})
    )
    released: list[str | None] = []

    async def _claim(*_args, **_kwargs):
        return False, None, "dedup-key"

    async def _release(key):
        released.append(key)

    monkeypatch.setattr(webhooks, "_claim_webhook_dedup_slot", _claim)
    monkeypatch.setattr(webhooks, "_release_webhook_dedup_slot", _release)

    body = b'{"type":"create"}'
    headers = _wire(
        ("X-Hub-Signature-256", _github_signature(body)),
        ("X-OpenCTI-Token", NA_WIRE),
    )
    with pytest.raises(HTTPException) as exc_info:
        await webhooks.receive_assignment_webhook(
            str(integration_id), str(uuid.uuid4()), _delivery(headers, body=body)
        )
    assert (exc_info.value.status_code, exc_info.value.detail) == (
        401,
        "Invalid OpenCTI webhook token",
    )
    assert released == ["dedup-key"]


# ── Webhooks: every route on the assignment gate, and the Slack channel ─────

#: A credential a route must refuse, in each shape a caller controls.
_REFUSED_VALUES = {
    "non-ascii": NA_WIRE,
    "empty": b"",
    "over-long": b"x" * 8000,
    "wrong-ascii": b"0" * 64,
}

_ASSIGNMENT_GATE_SCHEMES = (
    "X-Webhook-Token",
    "Authorization",
    "X-Hub-Signature-256",
    "X-Slack-Signature",
    "X-Signature",
)

_ASSIGNMENT_GATE_ROUTES = ("direct", "flow", "legacy")


def _presented(scheme: str, value: bytes) -> list[tuple[str, bytes]]:
    """*value* as *scheme* carries it, built at call time: a Slack timestamp must be fresh."""
    if scheme == "Authorization":
        return [(scheme, b"Bearer " + value)]
    if scheme == "X-Hub-Signature-256":
        return [(scheme, b"sha256=" + value)]
    if scheme == "X-Slack-Signature":
        return [(scheme, b"v0=" + value), ("X-Slack-Request-Timestamp", str(int(time.time())).encode())]
    return [(scheme, value)]


async def _deliver_to(route: str, monkeypatch, pairs: list[tuple[str, bytes]]) -> None:
    """One delivery to *route*, whose webhook secret is ``SECRET``."""
    request = _delivery(_wire(*pairs), body=_BODY)
    # Ownerless stand-ins: the owner-deactivation rule reads
    # these two columns before the secret check and asks nothing of an
    # ownerless row.
    owner = {"user_id": None, "is_company_managed": False}
    if route == "direct":
        _serve(monkeypatch, SimpleNamespace(trigger_config={"webhook_secret": SECRET}, **owner))
        await webhooks.receive_direct_assignment_webhook(str(uuid.uuid4()), request)
    elif route == "flow":
        from app.services import flow_triggers

        monkeypatch.setattr(flow_triggers, "resolve_webhook_trigger", lambda _graph, _path: {"id": "t"})
        _serve(monkeypatch, SimpleNamespace(graph={}, webhook_secret=SECRET, **owner))
        await webhooks.receive_flow_webhook(str(uuid.uuid4()), "inbound", request)
    else:
        _serve(
            monkeypatch,
            SimpleNamespace(id=uuid.uuid4(), trigger_config={"path": "inbound", "webhook_secret": SECRET}),
        )
        await webhooks.receive_webhook(str(uuid.uuid4()), "inbound", request)


@pytest.mark.parametrize("value", list(_REFUSED_VALUES.values()), ids=list(_REFUSED_VALUES))
@pytest.mark.parametrize("scheme", _ASSIGNMENT_GATE_SCHEMES)
@pytest.mark.parametrize("route", _ASSIGNMENT_GATE_ROUTES)
async def test_every_route_on_the_assignment_gate_refuses_a_bad_credential_with_401(
    monkeypatch, route, scheme, value
):
    """Direct, flow and legacy routes: non-ASCII, empty, over-long or wrong, the
    answer is the route's 401, before the dedup cache is consulted."""
    _forbid_dedup_claim(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        await _deliver_to(route, monkeypatch, _presented(scheme, value))
    assert exc_info.value.status_code == 401


@pytest.mark.parametrize("route", _ASSIGNMENT_GATE_ROUTES)
async def test_the_right_secret_gets_past_the_gate_of_every_route(monkeypatch, route):
    """The control: with the right secret, each route goes on to the dedup cache."""
    _forbid_dedup_claim(monkeypatch)
    with pytest.raises(AssertionError, match="dedup slot was claimed"):
        await _deliver_to(route, monkeypatch, [("X-Webhook-Token", SECRET.encode())])


_SLACK_CHALLENGE = b'{"type": "url_verification", "challenge": "c"}'


def _slack_bot(monkeypatch):
    from app.channels import slack as slack_channel

    async def _creds():
        return "xoxb-test", SECRET

    monkeypatch.setattr(slack_channel, "_get_slack_bot_creds", _creds)
    return slack_channel


@pytest.mark.parametrize("value", list(_REFUSED_VALUES.values()), ids=list(_REFUSED_VALUES))
async def test_the_slack_channel_route_refuses_a_bad_signature_with_401(monkeypatch, value):
    slack_channel = _slack_bot(monkeypatch)
    request = _delivery(_wire(*_presented("X-Slack-Signature", value)), body=_SLACK_CHALLENGE)
    with pytest.raises(HTTPException) as exc_info:
        await slack_channel.slack_events(request)
    assert (exc_info.value.status_code, exc_info.value.detail) == (401, "Invalid Slack signature")


async def test_the_slack_channel_route_answers_a_signed_request(monkeypatch):
    slack_channel = _slack_bot(monkeypatch)
    request = _delivery(_wire(*_signed("slack", body=_SLACK_CHALLENGE)), body=_SLACK_CHALLENGE)
    assert await slack_channel.slack_events(request) == {"challenge": "c"}


# ── Public image and file tokens ────────────────────────────────────────────


class _NoDb:
    async def execute(self, *_args, **_kwargs):
        raise AssertionError("a refused token must not reach the database")


@pytest.fixture
def chat_client() -> TestClient:
    app = FastAPI()
    app.include_router(chat_api.router, prefix="/api/chat")
    app.dependency_overrides[get_db] = lambda: _NoDb()
    return TestClient(app)


@pytest.mark.parametrize("route", ["images", "files"])
def test_a_crafted_public_token_is_a_403(chat_client, route):
    response = chat_client.get(
        f"/api/chat/{route}/{uuid.uuid4()}/public?token=caf%C3%A9&expires=9999999999"
    )
    assert response.status_code == 403


# ── Prometheus bearer token ─────────────────────────────────────────────────


def _metrics_request(headers: list[tuple[bytes, bytes]], query: bytes = b"") -> Request:
    return Request(
        {"type": "http", "method": "GET", "path": "/metrics", "headers": headers, "query_string": query}
    )


@pytest.fixture
def prometheus_token(monkeypatch) -> str:
    token = "t" * _PROMETHEUS_TOKEN_MIN_LENGTH
    monkeypatch.setattr(metrics_api.settings, "prometheus_enabled", True)
    monkeypatch.setattr(metrics_api.settings, "prometheus_bearer_token", token)
    monkeypatch.setattr(metrics_api.settings, "platform_mode", "xtm_one")
    return token


async def test_a_crafted_prometheus_bearer_token_is_a_401(prometheus_token):
    request = _metrics_request([(b"authorization", b"Bearer " + NA_WIRE)])
    credentials = await HTTPBearer(auto_error=False)(request)
    assert credentials is not None
    with pytest.raises(HTTPException) as exc_info:
        metrics_api.verify_prometheus_access(request, credentials)
    assert exc_info.value.status_code == 401


async def test_a_crafted_dev_mode_query_token_is_a_401(prometheus_token, monkeypatch):
    monkeypatch.setattr(metrics_api.settings, "platform_mode", "dev")
    request = _metrics_request([], query=b"token=caf%C3%A9")
    with pytest.raises(HTTPException) as exc_info:
        metrics_api.verify_prometheus_access(request, None)
    assert exc_info.value.status_code == 401


async def test_the_right_prometheus_token_still_passes(prometheus_token):
    request = _metrics_request([(b"authorization", f"Bearer {prometheus_token}".encode())])
    credentials = await HTTPBearer(auto_error=False)(request)
    metrics_api.verify_prometheus_access(request, credentials)


@pytest.mark.parametrize("encoding", ["utf-8", "latin-1"])
async def test_a_non_ascii_prometheus_token_matches_in_either_wire_spelling(
    monkeypatch, encoding
):
    """Prometheus is a Go client and writes the header in UTF-8: that token
    would be a wrong one without the wire comparison."""
    token = "t" * _PROMETHEUS_TOKEN_MIN_LENGTH + "\xe9"
    monkeypatch.setattr(metrics_api.settings, "prometheus_enabled", True)
    monkeypatch.setattr(metrics_api.settings, "prometheus_bearer_token", token)
    monkeypatch.setattr(metrics_api.settings, "platform_mode", "xtm_one")

    request = _metrics_request([(b"authorization", b"Bearer " + token.encode(encoding))])
    credentials = await HTTPBearer(auto_error=False)(request)
    metrics_api.verify_prometheus_access(request, credentials)


# ── Evaluation resume callback ──────────────────────────────────────────────


def _evaluation_db(resume_secret: object = SECRET, trigger_config: dict | None = None) -> object:
    episode = SimpleNamespace(run_id=uuid.uuid4())
    evaluation = SimpleNamespace(
        trigger_config=(
            stored_trigger_config({"resume_secret": resume_secret})
            if trigger_config is None
            else trigger_config
        ),
        timeout_seconds=60,
    )
    results = iter(
        [
            SimpleNamespace(scalar_one_or_none=lambda: episode),
            SimpleNamespace(one_or_none=lambda: (object(), evaluation)),
        ]
    )

    class _Db:
        async def execute(self, *_args, **_kwargs):
            return next(results)

    return _Db()


async def _resume_with(
    signature: bytes,
    monkeypatch,
    resumed: list,
    resume_secret: object = SECRET,
    trigger_config: dict | None = None,
) -> None:
    async def _resume(*_args, **_kwargs):
        resumed.append(1)
        return None, False

    monkeypatch.setattr(
        evaluations_api.ConversationContinuationService, "resume_from_callback", _resume
    )
    body = ResumeEvaluationEpisodeRequest(correlation_id="c-1", idempotency_key="k-1")
    headers = _wire(("X-Signature", signature))
    request = _delivery(headers, body=body.model_dump_json().encode())
    await evaluations_api.resume_evaluation_episode(
        body, request, headers.get("X-Signature"), _evaluation_db(resume_secret, trigger_config)
    )


async def test_a_crafted_evaluation_callback_signature_is_a_401(monkeypatch):
    resumed: list = []
    with pytest.raises(HTTPException) as exc_info:
        await _resume_with(NA_WIRE, monkeypatch, resumed)
    assert exc_info.value.status_code == 401
    assert resumed == []


async def test_the_right_evaluation_callback_signature_still_resumes(monkeypatch):
    body = ResumeEvaluationEpisodeRequest(correlation_id="c-1", idempotency_key="k-1")
    signature = hmac.new(
        SECRET.encode(), body.model_dump_json().encode(), hashlib.sha256
    ).hexdigest()
    resumed: list = []
    # The stub resumes nothing, which the route answers with a 404 - past the gate.
    with pytest.raises(HTTPException) as exc_info:
        await _resume_with(signature.encode(), monkeypatch, resumed)
    assert exc_info.value.status_code == 404
    assert resumed == [1]


@pytest.mark.parametrize("resume_secret", _UNUSABLE_SECRETS)
def test_an_evaluation_resume_secret_that_is_not_a_string_is_refused_when_written(
    resume_secret,
):
    """``trigger_config`` is caller-supplied JSON: a truthy non-string would raise
    on ``.encode()``, so it is refused before it is stored, never an
    unauthenticated 500 on a public callback."""
    with pytest.raises(UnusableSecretError):
        stored_trigger_config({"resume_secret": resume_secret})


@pytest.mark.parametrize(
    ("trigger_config", "status"),
    [
        ({"resume_secret_enc": "not-a-fernet-token"}, 401),
        ({"resume_secret_enc": 123}, 401),
        ({"resume_secret_enc": security.encrypt_value("vault://kv/evaluations#resume")}, 401),
        ({"resume_secret": SECRET}, 400),
    ],
    ids=["undecryptable", "not-a-string", "a-vault-reference", "stored-in-clear-before-5519"],
)
async def test_a_stored_resume_secret_the_callback_cannot_use_refuses_it(
    monkeypatch, trigger_config, status
):
    """A stored secret no sender can prove (encrypted under another ``SECRET_KEY``,
    not a string, a vault reference) is refused as a wrong signature is, never a
    500 on a public callback; one stored in clear is not read, so even
    its right signature is refused (the migration encrypts such a secret)."""
    body = ResumeEvaluationEpisodeRequest(correlation_id="c-1", idempotency_key="k-1")
    signature = hmac.new(
        SECRET.encode(), body.model_dump_json().encode(), hashlib.sha256
    ).hexdigest()
    resumed: list = []
    with pytest.raises(HTTPException) as exc_info:
        await _resume_with(
            signature.encode(), monkeypatch, resumed, trigger_config=trigger_config
        )
    assert exc_info.value.status_code == status
    assert resumed == []


async def test_an_evaluation_resume_secret_that_is_a_json_number_is_its_digits(monkeypatch):
    body = ResumeEvaluationEpisodeRequest(correlation_id="c-1", idempotency_key="k-1")
    signature = hmac.new(b"12345", body.model_dump_json().encode(), hashlib.sha256).hexdigest()
    resumed: list = []
    with pytest.raises(HTTPException) as exc_info:
        await _resume_with(signature.encode(), monkeypatch, resumed, 12345)
    assert exc_info.value.status_code == 404
    assert resumed == [1]


# ── Tripwire ────────────────────────────────────────────────────────────────

#: Every ``compare_digest`` reference the application may hold, keyed by module
#: and enclosing function, valued by how many that function holds. The one
#: question each site answers: can a non-ASCII ``str`` reach it?
_AUDITED_COMPARE_DIGEST_SITES: dict[tuple[str, str], int] = {
    # The helpers: both sides are encoded to bytes first.
    ("app/core/security.py", "timing_safe_str_eq"): 1,
    ("app/core/security.py", "timing_safe_header_eq"): 1,
    # UTF-8 bytes of the Authorization header value and of the configured token.
    ("app/core/registration_token.py", "registration_token_matches"): 1,
    # Two hex SHA-256 digests: ``hash_token`` of the normalised code, the stored hash.
    ("app/services/mfa.py", "consume_recovery_code"): 1,
    # Two 16-character hex tags: one matched by ``SECRET_MARKER_RE``, one an HMAC hexdigest.
    ("app/core/redaction.py", "KnownSecrets._restorable"): 1,
    # UTF-8 bytes of a delivered value's fingerprint, as computed and as stored on a runner command.
    ("app/core/redaction.py", "KnownSecrets.knows_fingerprint"): 1,
}

# Well below the real count (hundreds of modules), so a moved ``app/`` tree
# fails loudly instead of letting the scan pass over nothing.
_MIN_SCANNED_MODULES = 100


def _compare_digest_references(source: str, tree: ast.Module | None = None) -> list[tuple[str, int]]:
    """Every reference to ``compare_digest`` in *source*, as (enclosing scope, line).

    A reference, not only a call: ``from hmac import compare_digest`` or
    ``eq = hmac.compare_digest`` would slip past a call-only scan, and a
    ``getattr(hmac, "compare_digest")`` past a name-only one. Comments and
    prose that mention it are not references.
    """
    found: list[tuple[str, int]] = []

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if (
                (isinstance(child, ast.Attribute) and child.attr == "compare_digest")
                or (isinstance(child, ast.Name) and child.id == "compare_digest")
                or (isinstance(child, ast.alias) and child.name == "compare_digest")
                or (isinstance(child, ast.Constant) and child.value == "compare_digest")
            ):
                found.append((scope or "<module>", child.lineno))
            child_scope = scope
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                child_scope = f"{scope}.{child.name}" if scope else child.name
            visit(child, child_scope)

    visit(ast.parse(source) if tree is None else tree, "")
    return found


def test_every_compare_digest_in_the_application_is_an_audited_site():
    found: Counter[tuple[str, str]] = Counter()
    lines: dict[tuple[str, str], list[int]] = {}
    scanned = 0
    for path in app_sources.python_files(APP):
        scanned += 1
        rel = path.relative_to(BACKEND).as_posix()
        for scope, lineno in _compare_digest_references(app_sources.source(path), app_sources.tree(path)):
            found[(rel, scope)] += 1
            lines.setdefault((rel, scope), []).append(lineno)

    # Never pass vacuously: the scan only proves anything if it walked the tree.
    assert scanned >= _MIN_SCANNED_MODULES, f"scanned only {scanned} modules under {APP}"
    unaudited = sorted(
        site for site, count in found.items() if count > _AUDITED_COMPARE_DIGEST_SITES.get(site, 0)
    )
    assert not unaudited, (
        "hmac.compare_digest / secrets.compare_digest raise TypeError on a non-ASCII str, "
        "and a caller can put one in any header (decoded as latin-1) or query parameter: "
        "an unauthenticated 500, or a check an `except Exception` silently skips. "
        "Compare a request-derived str with app.core.security.timing_safe_str_eq instead. "
        "Only a site that provably compares bytes or ASCII-only values belongs in "
        "_AUDITED_COMPARE_DIGEST_SITES, with a comment saying why.\n"
        + "\n".join(
            f"  {rel} line(s) {lines[(rel, scope)]} in {scope}(): {found[(rel, scope)]} reference(s)"
            for rel, scope in unaudited
        )
    )
    stale = {
        site: (found.get(site, 0), count)
        for site, count in sorted(_AUDITED_COMPARE_DIGEST_SITES.items())
        if found.get(site, 0) < count
    }
    assert not stale, (
        "audited sites holding fewer references than listed - lower the count, or drop "
        f"the entry once none is left ((found, listed) per site): {stale}"
    )


def test_the_tripwire_sees_every_spelling_of_a_reference():
    for snippet in (
        "hmac.compare_digest(a, b)",
        "import hmac as _h\n_h.compare_digest(a, b)",
        "secrets.compare_digest(a, b)",
        "from hmac import compare_digest\ncompare_digest(a, b)",
        "from secrets import compare_digest as eq",
        "eq = hmac.compare_digest",
        "getattr(hmac, 'compare_digest')(a, b)",
    ):
        assert _compare_digest_references(snippet), snippet
    for snippet in (
        "# hmac.compare_digest raises on non-ASCII",
        '"""``hmac.compare_digest(a, b)`` in prose is not a reference."""',
        "timing_safe_str_eq(a, b)",
    ):
        assert not _compare_digest_references(snippet), snippet


def test_the_tripwire_names_the_enclosing_function():
    source = (
        "class K:\n"
        "    def m(self):\n"
        "        return hmac.compare_digest(a, b)\n"
        "\n"
        "\n"
        "async def f():\n"
        "    return hmac.compare_digest(a, b)\n"
    )
    assert _compare_digest_references(source) == [("K.m", 3), ("f", 7)]
