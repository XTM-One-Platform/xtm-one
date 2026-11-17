"""Tests for the dependency-free AWS Signature V4 signer.

Two layers:

1. Deterministic unit tests for the canonical path / query encoding rules and
   the structure of the signed headers (no network, no botocore).
2. A cross-check of the full ``Authorization`` signature against **botocore**
   (the AWS reference implementation) across GET/POST/PUT/DELETE, query
   strings, JSON bodies, special-character paths, and a session token. botocore
   is a transitive dependency (via boto3); the cross-check is skipped if it is
   ever absent.

The cross-check feeds botocore the *raw* (unencoded) path and lets it encode
once — matching what :func:`encode_path` produces — then reads botocore's
chosen ``X-Amz-Date`` back so both implementations sign the identical instant.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac

import pytest

from app.integrations.aws_sigv4 import (
    EMPTY_PAYLOAD_HASH,
    build_canonical_querystring,
    encode_path,
    sign_request,
)

_AK = "AKIDEXAMPLE"
_SK = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
_REGION = "us-east-1"
_SERVICE = "ses"
_HOST = "email.us-east-1.amazonaws.com"
_FIXED = datetime.datetime(2015, 8, 30, 12, 36, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# encode_path
# ---------------------------------------------------------------------------


def test_encode_path_encodes_reserved_but_preserves_separators_and_unreserved():
    # '+', '@', and space are percent-encoded; '/' separators preserved.
    assert (
        encode_path("/v2/email/identities/user+tag@ex.com")
        == "/v2/email/identities/user%2Btag%40ex.com"
    )
    assert encode_path("/a b/c") == "/a%20b/c"
    # Unreserved set (A-Z a-z 0-9 - _ . ~) is never escaped.
    assert encode_path("/x/-_.~") == "/x/-_.~"


def test_encode_path_empty_is_root():
    assert encode_path("") == "/"


# ---------------------------------------------------------------------------
# build_canonical_querystring
# ---------------------------------------------------------------------------


def test_canonical_querystring_sorts_and_encodes():
    qs = build_canonical_querystring({"PageSize": "50", "NextToken": "a+b/c=="})
    # Sorted by encoded key (NextToken < PageSize); '+', '/', '=' encoded.
    assert qs == "NextToken=a%2Bb%2Fc%3D%3D&PageSize=50"


def test_canonical_querystring_empty():
    assert build_canonical_querystring(None) == ""
    assert build_canonical_querystring({}) == ""


# ---------------------------------------------------------------------------
# sign_request — structure / determinism (no botocore)
# ---------------------------------------------------------------------------


def test_sign_request_structure_and_signed_header_set():
    headers = sign_request(
        method="GET",
        service=_SERVICE,
        region=_REGION,
        access_key_id=_AK,
        secret_access_key=_SK,
        host=_HOST,
        canonical_uri="/v2/email/account",
        now=_FIXED,
    )
    assert headers["X-Amz-Date"] == "20150830T123600Z"
    # Body-less request hashes the empty payload.
    assert headers["X-Amz-Content-Sha256"] == EMPTY_PAYLOAD_HASH
    auth = headers["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 ")
    assert f"Credential={_AK}/20150830/{_REGION}/{_SERVICE}/aws4_request" in auth
    # Minimal, deterministic signed-header set.
    assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in auth
    assert "Signature=" in auth
    # No session token header unless one is supplied.
    assert "X-Amz-Security-Token" not in headers


def test_sign_request_includes_session_token_when_present():
    headers = sign_request(
        method="GET",
        service=_SERVICE,
        region=_REGION,
        access_key_id=_AK,
        secret_access_key=_SK,
        host=_HOST,
        canonical_uri="/v2/email/account",
        session_token="FwoGZXIvYXdzEXAMPLE",
        now=_FIXED,
    )
    assert headers["X-Amz-Security-Token"] == "FwoGZXIvYXdzEXAMPLE"
    assert "x-amz-security-token" in headers["Authorization"]


def test_sign_request_is_deterministic_for_fixed_now():
    kwargs = dict(
        method="POST",
        service=_SERVICE,
        region=_REGION,
        access_key_id=_AK,
        secret_access_key=_SK,
        host=_HOST,
        canonical_uri="/v2/email/outbound-emails",
        payload=b'{"x":1}',
        now=_FIXED,
    )
    assert sign_request(**kwargs)["Authorization"] == sign_request(**kwargs)["Authorization"]


def test_payload_hash_matches_sha256_of_body():
    body = b'{"hello":"world"}'
    headers = sign_request(
        method="POST",
        service=_SERVICE,
        region=_REGION,
        access_key_id=_AK,
        secret_access_key=_SK,
        host=_HOST,
        canonical_uri="/v2/email/outbound-emails",
        payload=body,
        now=_FIXED,
    )
    assert headers["X-Amz-Content-Sha256"] == hashlib.sha256(body).hexdigest()


def test_signing_key_ladder_matches_manual_derivation():
    # Re-derive the signing key independently and confirm the signature is a
    # plain HMAC over the string-to-sign (guards the HMAC ladder/order).
    headers = sign_request(
        method="GET",
        service=_SERVICE,
        region=_REGION,
        access_key_id=_AK,
        secret_access_key=_SK,
        host=_HOST,
        canonical_uri="/",
        now=_FIXED,
    )

    def _h(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k_date = _h(f"AWS4{_SK}".encode(), "20150830")
    k_region = _h(k_date, _REGION)
    k_service = _h(k_region, _SERVICE)
    k_signing = _h(k_service, "aws4_request")
    canonical_request = "\n".join(
        [
            "GET",
            "/",
            "",
            f"host:{_HOST}\n"
            f"x-amz-content-sha256:{EMPTY_PAYLOAD_HASH}\n"
            "x-amz-date:20150830T123600Z\n",
            "host;x-amz-content-sha256;x-amz-date",
            EMPTY_PAYLOAD_HASH,
        ]
    )
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            "20150830T123600Z",
            f"20150830/{_REGION}/{_SERVICE}/aws4_request",
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    expected_sig = hmac.new(
        k_signing, string_to_sign.encode(), hashlib.sha256
    ).hexdigest()
    assert f"Signature={expected_sig}" in headers["Authorization"]


# ---------------------------------------------------------------------------
# botocore cross-check — the AWS reference implementation
# ---------------------------------------------------------------------------


def _botocore_authorization(method, raw_path, *, params=None, body=b"", session_token=None):
    """Sign with botocore and return (authorization, x_amz_date)."""
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    qs = build_canonical_querystring(params)
    url = f"https://{_HOST}{raw_path}" + (f"?{qs}" if qs else "")
    req = AWSRequest(method=method, url=url, data=body or b"")
    # Match our signed header set: we always sign x-amz-content-sha256.
    req.headers["X-Amz-Content-SHA256"] = hashlib.sha256(body or b"").hexdigest()
    creds = Credentials(_AK, _SK, token=session_token)
    SigV4Auth(creds, _SERVICE, _REGION).add_auth(req)
    return req.headers["Authorization"], req.headers["X-Amz-Date"]


@pytest.mark.parametrize(
    "method,raw_path,params,body",
    [
        ("GET", "/v2/email/account", None, b""),
        ("GET", "/v2/email/identities/user+tag@example.com", None, b""),
        ("GET", "/v2/email/identities", {"PageSize": "50", "NextToken": "a+b/c=="}, b""),
        ("POST", "/v2/email/outbound-emails", None, '{"Subject":"é a/b+c"}'.encode()),
        ("PUT", "/v2/email/suppression/addresses", None, b'{"EmailAddress":"a@b.co"}'),
        ("DELETE", "/v2/email/templates/My Template", None, b""),
    ],
)
def test_signature_matches_botocore(method, raw_path, params, body):
    pytest.importorskip("botocore")
    boto_auth, boto_date = _botocore_authorization(
        method, raw_path, params=params, body=body
    )
    moment = datetime.datetime.strptime(boto_date, "%Y%m%dT%H%M%SZ").replace(
        tzinfo=datetime.UTC
    )
    mine = sign_request(
        method=method,
        service=_SERVICE,
        region=_REGION,
        access_key_id=_AK,
        secret_access_key=_SK,
        host=_HOST,
        canonical_uri=encode_path(raw_path),
        canonical_querystring=build_canonical_querystring(params),
        payload=body,
        now=moment,
    )
    assert mine["Authorization"] == boto_auth


def test_signature_matches_botocore_with_session_token():
    pytest.importorskip("botocore")
    token = "FwoGZXIvYXdzEXAMPLESESSIONTOKEN"
    boto_auth, boto_date = _botocore_authorization(
        "GET", "/v2/email/account", session_token=token
    )
    moment = datetime.datetime.strptime(boto_date, "%Y%m%dT%H%M%SZ").replace(
        tzinfo=datetime.UTC
    )
    mine = sign_request(
        method="GET",
        service=_SERVICE,
        region=_REGION,
        access_key_id=_AK,
        secret_access_key=_SK,
        host=_HOST,
        canonical_uri="/v2/email/account",
        session_token=token,
        now=moment,
    )
    assert mine["Authorization"] == boto_auth
