# SPDX-License-Identifier: Apache-2.0

"""AWS Signature Version 4 (SigV4) request signing.

A small, dependency-free implementation of the `AWS Signature Version 4
<https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv4_signing.html>`_
signing process, built on the standard library (``hashlib`` / ``hmac``) so it
can be used from the async ``httpx`` request path that every integration
provider already shares — no synchronous ``boto3`` calls in the event loop.

The signer is intentionally generic (it takes a ``service`` name and a
``region``) so any AWS JSON/REST API can reuse it. Today it powers the Amazon
SES v2 provider; future AWS integrations (SNS, S3, …) can sign with the same
helper.

Usage::

    headers = sign_request(
        method="POST",
        service="ses",
        region="us-east-1",
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        host="email.us-east-1.amazonaws.com",
        canonical_uri=encode_path("/v2/email/outbound-emails"),
        canonical_querystring="",
        payload=body_bytes,
    )
    # `headers` already contains Authorization + X-Amz-Date (+ token) — merge
    # them into the request headers and send the *exact same* canonical_uri /
    # canonical_querystring / body that were signed.

Correctness notes (the pitfalls this module exists to avoid):

* The canonical URI must be the **URI-encoded path** (every segment encoded
  with the AWS unreserved-character set, ``/`` preserved). Callers MUST pass
  the request to the HTTP client using the very same encoded path, otherwise
  the client re-encodes ``@`` / ``+`` differently and the signature breaks.
  Use :func:`encode_path` for the path and :func:`build_canonical_querystring`
  for the query so the signed and sent forms are byte-identical.
* The payload hash is computed over the **exact bytes** that are sent on the
  wire, so the caller must send ``content=payload`` (not ``json=…``, which
  would re-serialise and change the bytes).
* ``host`` and ``x-amz-date`` are always signed (plus ``x-amz-security-token``
  when a session token is supplied). ``Content-Type`` is intentionally *not*
  signed — SigV4 only requires ``host`` to be signed, and keeping the signed
  set minimal removes a whole class of "client added/normalised a header"
  signature-mismatch failures.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import quote

_ALGORITHM = "AWS4-HMAC-SHA256"
_AWS4_REQUEST = "aws4_request"

# SHA-256 of the empty byte string — the payload hash for body-less requests
# (GET/DELETE). Precomputed so we never hash ``b""`` on every call.
EMPTY_PAYLOAD_HASH = hashlib.sha256(b"").hexdigest()


def encode_path(path: str) -> str:
    """URI-encode a request path the way SigV4 (non-S3) expects.

    Each path *segment* is percent-encoded with the AWS unreserved set
    (``A-Z a-z 0-9 - _ . ~``); the ``/`` separators are preserved. Python's
    :func:`urllib.parse.quote` never escapes ``_.-~`` (since 3.7), so
    ``quote(seg, safe="")`` matches the AWS rule exactly while still encoding
    ``@`` → ``%40``, ``+`` → ``%2B``, space → ``%20``, etc.

    A path that contains an email address or domain (SES identities, contacts,
    suppressed destinations) is therefore signed and sent identically.
    """
    if not path:
        return "/"
    return "/".join(quote(segment, safe="") for segment in path.split("/"))


def build_canonical_querystring(params: dict[str, str] | None) -> str:
    """Build the canonical query string for SigV4.

    Keys and values are individually percent-encoded (AWS unreserved set) and
    the pairs are sorted by encoded key (then encoded value) per the spec.
    Returns ``""`` when there are no parameters. The caller must append the
    returned string to the request URL verbatim so the sent query matches the
    signed query byte-for-byte.
    """
    if not params:
        return ""
    encoded = sorted(
        (quote(str(k), safe="~"), quote(str(v), safe="~")) for k, v in params.items()
    )
    return "&".join(f"{k}={v}" for k, v in encoded)


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _signing_key(secret_access_key: str, date_stamp: str, region: str, service: str) -> bytes:
    """Derive the SigV4 signing key (the chained HMAC ladder)."""
    k_date = _sign(f"AWS4{secret_access_key}".encode(), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    return _sign(k_service, _AWS4_REQUEST)


def sign_request(
    *,
    method: str,
    service: str,
    region: str,
    access_key_id: str,
    secret_access_key: str,
    host: str,
    canonical_uri: str,
    canonical_querystring: str = "",
    payload: bytes = b"",
    session_token: str | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """Compute the SigV4 ``Authorization`` (and ``X-Amz-*``) headers.

    Parameters
    ----------
    method:
        HTTP verb in upper-case (``GET``, ``POST``, ``PUT``, ``DELETE``).
    service:
        AWS service signing name (e.g. ``"ses"`` for Amazon SES — note this is
        the *signing* name, which differs from the ``email.…`` endpoint host).
    region:
        AWS region (e.g. ``"us-east-1"``).
    access_key_id / secret_access_key:
        The IAM credentials.
    host:
        The request host (e.g. ``email.us-east-1.amazonaws.com``) — signed as
        the ``host`` header.
    canonical_uri:
        The already-encoded request path (use :func:`encode_path`).
    canonical_querystring:
        The already-encoded, sorted query string (use
        :func:`build_canonical_querystring`), or ``""``.
    payload:
        The exact request body bytes (``b""`` for body-less requests).
    session_token:
        Optional STS session token for temporary credentials.
    now:
        Override the timestamp (used by tests for determinism).

    Returns
    -------
    dict[str, str]
        Headers to merge into the outgoing request: ``Authorization``,
        ``X-Amz-Date``, ``X-Amz-Content-Sha256`` and, when a session token is
        provided, ``X-Amz-Security-Token``.
    """
    moment = now or datetime.now(UTC)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = moment.strftime("%Y%m%d")

    payload_hash = (
        EMPTY_PAYLOAD_HASH if not payload else hashlib.sha256(payload).hexdigest()
    )

    # Signed headers — always host + x-amz-date + x-amz-content-sha256, plus
    # the session token when present. Kept minimal on purpose (see module docs).
    signed: dict[str, str] = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if session_token:
        signed["x-amz-security-token"] = session_token

    signed_header_names = sorted(signed)
    canonical_headers = "".join(f"{name}:{signed[name]}\n" for name in signed_header_names)
    signed_headers = ";".join(signed_header_names)

    canonical_request = "\n".join(
        [
            method.upper(),
            canonical_uri or "/",
            canonical_querystring,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )

    credential_scope = f"{date_stamp}/{region}/{service}/{_AWS4_REQUEST}"
    string_to_sign = "\n".join(
        [
            _ALGORITHM,
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )

    signature = hmac.new(
        _signing_key(secret_access_key, date_stamp, region, service),
        string_to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()

    authorization = (
        f"{_ALGORITHM} "
        f"Credential={access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )

    headers = {
        "Authorization": authorization,
        "X-Amz-Date": amz_date,
        "X-Amz-Content-Sha256": payload_hash,
    }
    if session_token:
        headers["X-Amz-Security-Token"] = session_token
    return headers
