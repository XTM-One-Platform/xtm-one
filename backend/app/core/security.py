# SPDX-License-Identifier: Apache-2.0

"""Security utilities: JWT tokens, password hashing, Fernet encryption."""

import asyncio
import math
import time
import warnings
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt
from cryptography.fernet import Fernet

from app.core.config import settings

warnings.filterwarnings("ignore", message=".*HMAC key.*below the minimum recommended.*")

# ---------------------------------------------------------------------------
# Password hashing (bcrypt directly, no passlib)
# ---------------------------------------------------------------------------


def normalize_password(password: str) -> str:
    """Trim leading/trailing whitespace from a password.

    Accidental surrounding whitespace (copy-paste, mobile keyboards,
    autofill) should not change a password. Applied consistently on hashing,
    verification, and policy checks so they stay symmetric — a password set
    with a trailing space still authenticates when typed without one.
    """
    return password.strip()


#: bcrypt reads at most this many bytes of its input. bcrypt < 5 silently
#: truncated a longer one; bcrypt 5 (pinned here) raises ``ValueError`` instead.
BCRYPT_MAX_BYTES = 72


def bcrypt_input(candidate: object) -> bytes | None:
    """The bytes bcrypt compares for a presented credential.

    Cut to :data:`BCRYPT_MAX_BYTES`, as bcrypt < 5 did when it hashed: a
    stored hash of a longer secret keeps verifying the secret it was made
    from, and a crafted over-long value is an ordinary mismatch instead of the
    500 bcrypt 5 raises. A lone surrogate (a JSON ``"\\ud800"`` escape) is
    encoded with ``surrogatepass``: no UTF-8 text encodes to those bytes, so
    the value is a mismatch that still costs a full bcrypt round. ``None``
    for anything that is not text or bytes: no stored hash can match it.
    """
    if isinstance(candidate, str):
        candidate = candidate.encode("utf-8", "surrogatepass")
    if not isinstance(candidate, bytes):
        return None
    return candidate[:BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    password = normalize_password(password)
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a password against its bcrypt hash.

    Tries the raw input first so any existing account whose stored hash was
    created from a password containing intentional/legacy leading or trailing
    whitespace can still authenticate.  Only when that fails do we retry with
    the normalized (stripped) form — that second attempt is what makes
    accidental whitespace from copy-paste, mobile autofill, etc. transparent
    *forward* (passwords newly hashed via :func:`hash_password` always store
    the normalized form, but the symmetry must not regress old accounts).

    The raw and normalized inputs are only different when the supplied
    password actually has surrounding whitespace, so the second
    ``bcrypt.checkpw`` call is short-circuited away on the common path.
    """
    raw_bytes = bcrypt_input(plain)
    if raw_bytes is None:
        return False
    hashed_bytes = hashed.encode("utf-8")
    if bcrypt.checkpw(raw_bytes, hashed_bytes):
        return True
    normalized = normalize_password(plain)
    if normalized == plain:
        return False
    return bcrypt.checkpw(bcrypt_input(normalized), hashed_bytes)


# bcrypt is pure CPU work (~100-300 ms at default cost). Calling it inline in
# an async handler blocks the whole event loop for that long — on per-request
# auth paths (API keys, LLM proxy, MCP endpoint) this serializes the entire
# worker process. These wrappers offload the hashing to the default thread
# pool; use them from any async code path.


async def hash_password_async(password: str) -> str:
    """Async wrapper for :func:`hash_password` (bcrypt off the event loop)."""
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(plain: str, hashed: str) -> bool:
    """Async wrapper for :func:`verify_password` (bcrypt off the event loop)."""
    return await asyncio.to_thread(verify_password, plain, hashed)


async def bcrypt_checkpw_async(raw: object, hashed: bytes) -> bool:
    """``bcrypt.checkpw`` off the event loop, over :func:`bcrypt_input`.

    *raw* is the presented credential, as text or bytes; anything else, or a
    value no stored hash can match, is ``False`` - never a raise.
    """
    candidate = bcrypt_input(raw)
    if candidate is None:
        return False
    return await asyncio.to_thread(bcrypt.checkpw, candidate, hashed)


async def bcrypt_hashpw_async(raw: bytes) -> bytes:
    """Async wrapper for ``bcrypt.hashpw`` with a fresh salt.

    Hashes the bytes :func:`bcrypt_checkpw_async` will compare: a key longer
    than :data:`BCRYPT_MAX_BYTES` (an operator's ``ADMIN_TOKEN``; generated
    keys are shorter) counts by its first 72 bytes, as with bcrypt < 5.
    """

    def _hash() -> bytes:
        return bcrypt.hashpw(raw[:BCRYPT_MAX_BYTES], bcrypt.gensalt())

    return await asyncio.to_thread(_hash)


def check_password_policy(password: str) -> None:
    """Validate a password against the platform policy.

    Raises ``ValueError`` (with a user-facing message) on the first violated
    rule. Framework-agnostic on purpose — the API layer translates this into an
    HTTP 400 (see ``app.api.deps.validate_password``). The password is
    normalized first so the policy reflects what will actually be stored.
    """
    password = normalize_password(password)
    try:
        stored_bytes = len(password.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("Password contains characters that cannot be stored") from None
    # bcrypt 5 refuses to hash more; an earlier bcrypt would have ignored the rest.
    if stored_bytes > BCRYPT_MAX_BYTES:
        raise ValueError(
            f"Password must be at most {BCRYPT_MAX_BYTES} bytes long "
            "(fewer characters when they are not plain ASCII)"
        )
    from app.core.security_options import security_option

    min_length = security_option("min_password_length")
    if len(password) < min_length:
        raise ValueError(f"Password must be at least {min_length} characters")
    if not any(c.isalpha() for c in password):
        raise ValueError("Password must contain at least one letter")
    if not any(c.isdigit() for c in password):
        raise ValueError("Password must contain at least one number")


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

#: The clock skew every JWT verifier tolerates on ``exp``, ``nbf`` and ``iat``
#: (PyJWT ``leeway``, 0 by default) for a token minted on another host: an
#: XTM Suite product, an identity provider, another replica or the worker.
#: Without it a token minted by a clock a second ahead is refused as "not yet
#: valid". Spring Security's default; the Teams and Google Chat
#: webhooks keep the 5 minutes their vendors document.
JWT_CLOCK_SKEW_LEEWAY = timedelta(seconds=60)


def create_access_token(
    subject: str, extra: dict | None = None, *, continues: Mapping[str, Any] | None = None
) -> str:
    """Create a signed session JWT.

    The ``extra`` dict is merged into the payload. Keep it minimal — the token
    must NOT carry authorization-relevant claims such as ``role``. The token is
    signed, so a claim inside it can't be forged directly; the problem is that
    shipping ``role`` encourages the frontend to *derive* authorization from the
    token (or to copy it into mutable client UI state), trusting a value the
    server never re-reads. The authoritative role lives in the database and is
    surfaced via ``GET /auth/me``, and ``require_admin`` re-checks
    ``User.is_admin`` from the DB on every request — so the token has no role to
    keep in sync or to leak into spoofable client state. Legitimate extras are
    session metadata like ``impersonated_by``.

    ``iat`` (issued-at) is included so the server can reject tokens that
    predate a password change (see ``app.api.deps.get_current_user``).

    ``auth_time`` and ``sid`` say which session the token belongs to
    (``app.core.sessions``): a new one, or *continues* - the claims of the
    session a re-minted token carries on, whose sign-in time is kept so the
    absolute lifetime is never reset. ``exp`` is that sign-in plus the
    absolute lifetime in force, and never later than the end of the session it
    continues: a lifetime raised since that sign-in applies from the next one.
    """
    from app.core.sessions import session_claims, session_expires_at

    now = int(datetime.now(UTC).timestamp())
    payload: dict[str, Any] = {"sub": subject, "iat": now}
    if extra:
        payload.update(extra)
    payload.update(session_claims(continues, now=now))
    expires_at = session_expires_at(payload)
    carried = session_expires_at(continues) if continues else None
    payload["exp"] = min(expires_at, carried) if expires_at is not None and carried is not None else expires_at
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm], leeway=JWT_CLOCK_SKEW_LEEWAY)
    except UnicodeError as exc:  # a lone surrogate: PyJWT encodes before it parses
        raise jwt.DecodeError("Invalid token") from exc


def is_confined_token(payload: dict) -> bool:
    """True when a decoded platform JWT must NOT authenticate as a session.

    A platform session token carries only ``sub``/``iat``/``exp`` (plus
    session metadata such as ``impersonated_by``). Every confined credential
    the platform mints marks itself with ``type`` (resource tokens: MCP OAuth,
    A2A partner) or ``scope`` (flow tokens: the MFA challenge, A2A again) —
    and every session surface must refuse on the presence of EITHER claim.

    One predicate on purpose. When each surface checked a single claim
    (``/api/v1`` refused only ``scope``, the OAuth consent surface only
    ``type``), each accepted exactly the tokens marked with the other claim:
    an MCP access token (``type``, no ``scope``) was a full ``/api/v1``
    session, and the MFA challenge token (``scope``, no ``type``) could drive
    the consent flow with only the first factor proven. Presence, not value,
    so a future token kind is closed by default.
    """
    return "type" in payload or "scope" in payload


def is_expired_session_token(token: str) -> bool:
    """True when a token refused as expired is a session's own, signed by this platform.

    Only such a token ended a session: an expired confined token (MFA
    challenge, MCP or A2A resource token) is answered as an invalid token like
    any other confined token, never as a session that ended.
    """
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.jwt_algorithm],
            options={"verify_exp": False},
            leeway=JWT_CLOCK_SKEW_LEEWAY,
        )
    except (jwt.PyJWTError, UnicodeError):
        return False
    return "sub" in payload and not is_confined_token(payload)


# ---------------------------------------------------------------------------
# Invitation tokens
# ---------------------------------------------------------------------------


def hash_token(raw: str) -> str:
    """Hash a high-entropy token (invitation, password reset) for storage / lookup.

    SHA-256 (not bcrypt) because these tokens are looked up by exact value —
    an indexed equality lookup — and the raw token already carries 256 bits of
    entropy (``secrets.token_urlsafe(32)``), so it is not brute-forceable.
    """
    import hashlib

    # ``surrogatepass``: a presented token from a JSON body may hold a lone
    # surrogate; it hashes to a value no minted token has, instead of raising.
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()


# ---------------------------------------------------------------------------
# Fernet symmetric encryption (for storing API keys / OAuth tokens at rest)
# ---------------------------------------------------------------------------


def _derive_fernet_key() -> bytes:
    """Derive a 32-byte URL-safe base64-encoded key from the app secret."""
    import base64
    import hashlib

    digest = hashlib.sha256(settings.secret_key.encode()).digest()
    return base64.urlsafe_b64encode(digest)


_fernet = Fernet(_derive_fernet_key())

#: The ``SECRET_KEY`` placeholders of 32 characters or more this repository
#: publishes (``.env.sample``, the CI workflows): anyone can read them.
PUBLISHED_SECRET_KEYS = frozenset(
    {
        "change-me-to-a-random-string-in-production",
        "ci-test-secret-key-not-for-production",
    }
)


def secret_key_is_guessable(secret: str) -> bool:
    """True when *secret* is shorter than 32 characters or a published placeholder."""
    return len(secret) < 32 or secret in PUBLISHED_SECRET_KEYS


def encrypt_value(plaintext: str) -> str:
    """Encrypt a string and return a URL-safe token."""
    return _fernet.encrypt(plaintext.encode()).decode()


class SecretReferenceError(ValueError):
    """A stored secret is a vault reference: it is resolved where it is used, never read as a value."""


def decrypt_value(ciphertext: str) -> str:
    """Decrypt a Fernet token back to the original string.

    A stored vault reference raises :class:`SecretReferenceError`: the secret
    it names is read through ``app.services.vault.reveal_field`` where it is
    used, so a reader that was not taught references fails closed instead of
    sending the reference as the credential.
    """
    from app.core.secret_refs import holds_secret_reference

    plaintext = _fernet.decrypt(ciphertext.encode()).decode()
    if holds_secret_reference(plaintext):
        raise SecretReferenceError("This secret is stored as a vault reference, which this feature does not read")
    return plaintext


def decrypt_stored_value(ciphertext: str) -> str:
    """The stored text as written - a value or a vault reference - for code that keeps, compares or exports it.

    Never for code that USES the secret: that reads it through
    ``app.services.vault.reveal_field``, which resolves a reference.
    """
    return _fernet.decrypt(ciphertext.encode()).decode()


def decrypt_json(ciphertext: str | None) -> dict | list | None:
    """Decrypt a Fernet token and parse the result as JSON.

    Returns ``None`` when *ciphertext* is ``None`` or empty, which is a very
    common guard pattern throughout the codebase (replaces the repeated
    ``json.loads(decrypt_value(x)) if x else None`` idiom).
    """
    if not ciphertext:
        return None
    import json

    # A vault reference in the document is one of its string values, resolved by
    # ``app.services.vault.reveal_json_field`` where the values are used.
    return json.loads(decrypt_stored_value(ciphertext))


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------


def timing_safe_str_eq(provided: str, expected: str) -> bool:
    """Constant-time string comparison that never raises on non-ASCII input.

    ``hmac.compare_digest`` raises ``TypeError`` on non-ASCII ``str``
    arguments — and *provided* typically comes straight from a request
    header (Starlette decodes headers as latin-1, so crafted bytes >= 0x80
    yield non-ASCII strings). Comparing UTF-8 bytes keeps the comparison
    timing-safe while turning crafted input into a clean mismatch instead
    of an unhandled 500.

    Encoded with ``surrogatepass`` so that holds for every ``str``: a JSON body
    can carry a lone surrogate such as U+D800, which strict UTF-8 refuses to
    encode (and ``surrogateescape`` too, outside U+DC80..U+DCFF), and the
    encoding stays one-to-one, so two different strings never compare equal.
    """
    import hmac as _hmac

    return _hmac.compare_digest(
        provided.encode("utf-8", "surrogatepass"),
        expected.encode("utf-8", "surrogatepass"),
    )


def timing_safe_header_eq(header_value: str, expected: str) -> bool:
    """Whether a header carries *expected* verbatim, in constant time.

    For a secret sent as is (``X-OpenCTI-Token``, ``X-Webhook-Token``,
    ``Authorization: Bearer``). Starlette decodes header bytes as latin-1, and
    senders put a non-ASCII secret on the wire in one of two encodings:
    Node.js (OpenCTI's webhook sender) and Python's ``http.client`` write
    latin-1 and refuse anything above U+00FF, while curl, Go and most others
    write UTF-8. The first arrives as the secret itself, the second as its
    UTF-8 bytes read as latin-1, so the wire bytes are compared with the
    secret's UTF-8 bytes too. Both comparisons always run, so the time taken
    does not say which one matched; for an ASCII secret they are the same.
    """
    import hmac as _hmac

    as_text = timing_safe_str_eq(header_value, expected)
    try:
        wire = header_value.encode("latin-1")
    except UnicodeEncodeError:
        # Not a value Starlette decoded from header bytes: nothing to re-read.
        return as_text
    as_utf8_wire = _hmac.compare_digest(wire, expected.encode("utf-8", "surrogatepass"))
    return as_text | as_utf8_wire


class UnusableSecretError(ValueError):
    """A configured secret no sender can prove: refuse, never skip the check."""


def configured_secret(value: object) -> str | None:
    """Read a secret out of a JSON config (``extra_config``, ``trigger_config``).

    ``None`` when none is configured - the key absent, ``null`` or ``""`` - and
    only then. A JSON number stands for its digits: typed without quotes, it is
    still the secret the sender was given. Any other value (a boolean, a float,
    a list, an object, a string UTF-8 cannot encode) raises
    :class:`UnusableSecretError`, so the caller refuses the delivery instead of
    reading "no secret" into a falsy value or failing with a 500.
    """
    if value is None or value == "":
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return str(value)
        except ValueError:  # past Python's int-to-str digit limit
            raise UnusableSecretError("the configured secret is too long a number") from None
    if not isinstance(value, str):
        raise UnusableSecretError(
            f"the configured secret is a JSON {type(value).__name__}, not a string"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise UnusableSecretError(
            "the configured secret holds a lone surrogate, which UTF-8 cannot encode"
        ) from None
    return value


def verify_github_signature(body: bytes, secret: str, signature: str) -> bool:
    """Verify a GitHub webhook HMAC-SHA256 signature.

    GitHub sends the signature in the ``X-Hub-Signature-256`` header as
    ``sha256=<hex-digest>``.
    """
    import hashlib
    import hmac as _hmac

    if not signature.startswith("sha256="):
        return False
    expected = "sha256=" + _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return timing_safe_str_eq(signature, expected)


def verify_slack_signature(
    body: bytes,
    signing_secret: str,
    signature: str,
    timestamp: str,
) -> bool:
    """Verify a Slack webhook request signature.

    Slack sends the signature in ``X-Slack-Signature`` and the timestamp
    in ``X-Slack-Request-Timestamp``.  Requests older than 5 minutes are
    rejected to prevent replay attacks.
    """
    import hashlib
    import hmac as _hmac

    # Fail closed on crafted input: a non-numeric timestamp header or a
    # non-UTF-8 body must yield a 401 (verification failure), never a 500.
    try:
        ts = float(timestamp or 0)
        decoded_body = body.decode()
    except (ValueError, UnicodeDecodeError):
        return False
    if not math.isfinite(ts) or abs(time.time() - ts) > 300:
        return False
    base = f"v0:{timestamp}:{decoded_body}"
    expected = (
        "v0="
        + _hmac.new(signing_secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    )
    return timing_safe_str_eq(signature, expected)


def verify_linkedin_signature(body: bytes, client_secret: str, signature: str) -> bool:
    """Verify a LinkedIn webhook push-event ``X-LI-Signature``.

    LinkedIn's documented scheme
    (https://learn.microsoft.com/linkedin/shared/api-guide/webhook-validation):
    the header is the bare lowercase hex HMAC-SHA256, keyed with the
    application's ``clientSecret``, of the literal ``hmacsha256=`` followed by
    the raw POST body. The ``hmacsha256=<hex HMAC of the body>`` form this
    platform has always checked is accepted too, so a sender built against it
    keeps working. Both comparisons always run, in constant time.

    An empty client secret verifies nothing: anyone can compute an HMAC keyed
    with ``b""``. And a body that starts with ``hmacsha256=`` is refused the
    legacy form, whose HMAC over it is the documented signature of the rest:
    a captured LinkedIn signature would otherwise verify a second body.
    """
    import hashlib
    import hmac as _hmac

    if not client_secret:
        return False
    prefix = "hmacsha256="
    key = client_secret.encode()
    documented = _hmac.new(key, prefix.encode() + body, hashlib.sha256).hexdigest()
    legacy = prefix + _hmac.new(key, body, hashlib.sha256).hexdigest()
    matches_documented = timing_safe_str_eq(signature, documented)
    matches_legacy = timing_safe_str_eq(signature, legacy)
    return matches_documented | (matches_legacy and not body.startswith(prefix.encode()))


def compute_linkedin_challenge(challenge_code: str, client_secret: str) -> str:
    """Compute the HMACSHA256 challenge response for LinkedIn webhook validation.

    LinkedIn sends a GET request with a ``challengeCode`` query parameter.
    The response must include the challengeCode and a hex-encoded
    ``HMACSHA256(challengeCode, clientSecret)`` as the ``challengeResponse``.
    """
    import hashlib
    import hmac as _hmac

    return _hmac.new(
        client_secret.encode(),
        challenge_code.encode(),
        hashlib.sha256,
    ).hexdigest()


# ---------------------------------------------------------------------------
# Public image access tokens (time-limited HMAC, no JWT required)
# ---------------------------------------------------------------------------

_IMAGE_TOKEN_TTL = 24 * 3600  # 24 hours


def generate_image_token(image_id: str, expires: int | None = None) -> tuple[str, int]:
    """Generate a time-limited HMAC token for public image access.

    Returns ``(token_hex, expires_timestamp)``.
    """
    import hashlib
    import hmac as _hmac

    if expires is None:
        expires = int(time.time()) + _IMAGE_TOKEN_TTL
    payload = f"{image_id}:{expires}"
    token = _hmac.new(
        settings.secret_key.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return token, expires


def verify_image_token(image_id: str, token: str, expires: int) -> bool:
    """Verify a public image access token."""
    import hashlib
    import hmac as _hmac

    if int(time.time()) > expires:
        return False
    payload = f"{image_id}:{expires}"
    expected = _hmac.new(
        settings.secret_key.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return timing_safe_str_eq(token, expected)
