# SPDX-License-Identifier: Apache-2.0

"""TOTP two-factor authentication.

Implements RFC 6238 time-based one-time passwords against the parameters every
mainstream authenticator app assumes by default — SHA-1, 6 digits, 30-second
period. Those parameters are not a security choice so much as an
interoperability one: Google Authenticator and Microsoft Authenticator silently
ignore the ``algorithm`` / ``digits`` parameters of an ``otpauth://`` URI, so a
deployment that "hardened" to SHA-256 would hand out QR codes that scan fine
and then never produce a working code. KeePassXC, 1Password, Bitwarden, Aegis
and FreeOTP honour them, which is precisely what makes the mismatch so hard to
diagnose in the field: it works for the tester and fails for half the users.

Framework-agnostic on purpose — this module knows nothing about FastAPI, HTTP
status codes, or the ORM session. The API layer (``app/api/v1/auth.py``) owns
persistence, audit logging and rate limiting; everything here is pure
computation over values passed in.

Threat model notes that shaped the code below:

* **Secrets at rest.** The base32 secret is Fernet-encrypted with the same
  helper that protects integration credentials. A database dump must not yield
  working second factors.
* **Replay.** A 6-digit code stays valid for its whole 30-second step, plus the
  drift window on either side. An attacker who observes one code (shoulder
  surfing, a phishing proxy, an over-eager log line) can otherwise replay it for
  the remainder of that window. :func:`verify_totp` therefore returns the *step
  it consumed*, and the caller persists it so the same step can never be spent
  twice.
* **Challenge tokens.** The token minted between "password accepted" and "code
  accepted" is a JWT like any other, which means it would authenticate against
  every endpoint in the API if nothing distinguished it. It carries
  ``scope: "mfa"``, and ``app.api.deps.get_current_user_ungated`` rejects any
  token carrying a ``scope`` claim — the two halves must stay in sync.
"""

from __future__ import annotations

import base64
import hmac
import io
import logging
import re
import secrets
import time
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import jwt
import pyotp

from app.core.config import settings
from app.core.security import (
    JWT_CLOCK_SKEW_LEEWAY,
    decrypt_value,
    encrypt_value,
    hash_token,
    timing_safe_str_eq,
)

logger = logging.getLogger(__name__)

# --- TOTP parameters (see module docstring on why these are not tunable) ---
TOTP_DIGITS = 6
TOTP_PERIOD_SECONDS = 30
TOTP_ALGORITHM = "SHA1"

# Steps of clock drift accepted on either side of "now". One step (±30 s) is
# the conventional tolerance: enough for a phone whose clock never syncs, small
# enough that the replay window stays under two minutes.
TOTP_DRIFT_STEPS = 1

# --- Recovery codes ---
RECOVERY_CODE_COUNT = 10
# Crockford-style alphabet: no I, L, O, U, 0 or 1, so a code read off a screen
# and typed back in cannot be lost to O/0 or I/1 confusion.
_RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
_RECOVERY_GROUPS = 4
_RECOVERY_GROUP_LEN = 4  # 16 chars over a 30-symbol alphabet ≈ 78 bits

# Longer than any spelling of a code (six digits, or four groups of four, with
# the separators and spaces typed around them): refused before NFKC, which can
# expand a string eighteen-fold.
MAX_CODE_INPUT_CHARS = 128

# --- MFA challenge token (between password and code) ---
MFA_CHALLENGE_TTL_SECONDS = 300
MFA_TOKEN_SCOPE = "mfa"  # noqa: S105 — a JWT scope label, not a secret.


class MfaChallengeError(Exception):
    """The MFA challenge token is missing, malformed, expired, or not ours."""


# ---------------------------------------------------------------------------
# Secret lifecycle
# ---------------------------------------------------------------------------


def generate_secret() -> str:
    """Return a fresh base32 TOTP secret (160 bits, the RFC 4226 recommendation)."""
    return pyotp.random_base32()


def encrypt_secret(secret: str) -> str:
    """Encrypt a base32 secret for storage in ``users.mfa_secret``."""
    return encrypt_value(secret)


def decrypt_secret(stored: str | None) -> str | None:
    """Decrypt a stored secret, returning ``None`` when it is absent or unreadable.

    An unreadable secret means the platform ``SECRET_KEY`` changed after
    enrollment (the Fernet key is derived from it). Returning ``None`` rather
    than raising lets the caller answer "this code is wrong" — which is the
    truth from the user's side — instead of 500ing on every login attempt for
    every enrolled account. The warning is what tells an operator that a key
    rotation orphaned the enrollments.
    """
    if not stored:
        return None
    try:
        return decrypt_value(stored)
    except Exception:
        logger.warning(
            "Stored MFA secret could not be decrypted — the platform SECRET_KEY "
            "has most likely changed since enrollment. Affected users must reset "
            "their second factor (admin: DELETE /api/v1/users/{id}/mfa)."
        )
        return None


def provisioning_uri(secret: str, *, account_name: str, issuer: str | None = None) -> str:
    """Build the ``otpauth://totp/...`` URI an authenticator app scans.

    ``issuer`` defaults to the deployment's branding name, so a user enrolled on
    several XTM One instances sees them as distinct entries rather than three
    identical "XTM One" rows they cannot tell apart.
    """
    if issuer is None:
        from app.services.branding import get_cached_branding

        issuer = get_cached_branding().name or "XTM One"
    return pyotp.TOTP(
        secret, digits=TOTP_DIGITS, interval=TOTP_PERIOD_SECONDS
    ).provisioning_uri(name=account_name, issuer_name=issuer)


def qr_data_uri(uri: str) -> str:
    """Render *uri* as an SVG QR code, returned as a ``data:`` URL.

    SVG (not PNG) because ``qrcode``'s SVG factory is pure Python — no Pillow,
    no system image libraries — and the result scales to any display without
    resampling artefacts that break scanning. Rendering server-side keeps the
    frontend dependency-free and, more importantly, keeps enrollment working on
    air-gapped deployments: no third-party QR service is ever contacted.

    The caller embeds the result in an ``<img src=...>``. Browsers do not
    execute script inside an SVG loaded that way, so this stays inert even
    though the payload is markup.
    """
    import qrcode
    import qrcode.image.svg

    buffer = io.BytesIO()
    qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage).save(buffer)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


# ---------------------------------------------------------------------------
# Code verification
# ---------------------------------------------------------------------------


def _normalize_totp_code(code: str) -> str:
    """Strip whitespace and separators an app or user may have introduced.

    Several authenticators display codes as ``123 456``; pasting that in should
    work rather than fail an exact-length check. NFKC first: a Japanese input
    method in full-width mode types ``１２３４５６``, which stands for the same
    ASCII digits.
    """
    return re.sub(r"[\s-]", "", unicodedata.normalize("NFKC", code or ""))


def current_step(at: float | None = None) -> int:
    """Return the TOTP time-step counter for *at* (defaults to now)."""
    return int(at if at is not None else time.time()) // TOTP_PERIOD_SECONDS


def verify_totp(
    secret: str,
    code: str,
    *,
    last_used_step: int | None = None,
    at: float | None = None,
) -> int | None:
    """Verify *code* against *secret*, returning the time-step it consumed.

    Returns ``None`` when the code is invalid, malformed, or targets a step at
    or below ``last_used_step`` — the replay guard. The caller MUST persist the
    returned step, otherwise the same code remains usable for the rest of its
    window.

    Steps are walked newest-first so a legitimate code is matched against the
    step it was most likely generated for, which keeps ``last_used_step``
    advancing to the highest plausible value instead of pinning it to the
    trailing edge of the drift window.
    """
    if len(code or "") > MAX_CODE_INPUT_CHARS:
        return None
    code = _normalize_totp_code(code)
    # [0-9], not \d: \d also matches the non-ASCII digits NFKC keeps as they
    # are (Arabic-Indic), which no authenticator produces.
    if not re.fullmatch(rf"[0-9]{{{TOTP_DIGITS}}}", code):
        return None

    totp = pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_PERIOD_SECONDS)
    now_step = current_step(at)
    for step in range(now_step + TOTP_DRIFT_STEPS, now_step - TOTP_DRIFT_STEPS - 1, -1):
        if last_used_step is not None and step <= last_used_step:
            continue
        # Constant-time, not ==, so the comparison cannot leak how many leading
        # digits were right through its timing.
        if timing_safe_str_eq(code, totp.generate_otp(step)):
            return step
    return None


# ---------------------------------------------------------------------------
# Recovery codes
# ---------------------------------------------------------------------------


def _format_recovery_code() -> str:
    """Generate one grouped recovery code, e.g. ``A3KP-9WTM-2XQ7-BHND``."""
    groups = [
        "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_GROUP_LEN))
        for _ in range(_RECOVERY_GROUPS)
    ]
    return "-".join(groups)


def normalize_recovery_code(code: str) -> str:
    """Canonicalise user input before hashing: upper-case, separators dropped.

    Users retype these from paper or a password manager, so ``a3kp 9wtm…`` and
    ``A3KP-9WTM-…`` must hash identically. Hashing happens over the
    *un-grouped* upper-case form, which is what :func:`generate_recovery_codes`
    stores. NFKC first, so the full-width letters and digits an input method
    types read as the ASCII ones (generated codes are ASCII: stored hashes are
    unchanged).
    """
    return re.sub(r"[^A-Z0-9]", "", unicodedata.normalize("NFKC", code or "").upper())


def generate_recovery_codes(
    count: int = RECOVERY_CODE_COUNT,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Return ``(plaintext_codes, stored_entries)``.

    The plaintext list is shown to the user exactly once, at generation time.
    Only the hashed entries are persisted — recovering the codes later is
    deliberately impossible, which is why regeneration replaces the whole set.
    """
    plaintext = [_format_recovery_code() for _ in range(count)]
    stored = [
        {"hash": hash_token(normalize_recovery_code(code)), "used_at": None}
        for code in plaintext
    ]
    return plaintext, stored


def consume_recovery_code(
    stored: list[dict[str, Any]] | None, code: str
) -> list[dict[str, Any]] | None:
    """Spend *code* against *stored*, returning the updated list, or ``None``.

    ``None`` means the code does not match any unused entry — the caller treats
    that exactly like a wrong TOTP code, so an attacker cannot tell "no such
    recovery code" apart from "already used" apart from "wrong TOTP".

    Used entries are marked rather than removed so an operator can still see how
    much of the set has been burned. The returned list is a fresh object: the
    ``mfa_recovery_codes`` column is a plain ``JSON`` column with no mutation
    tracking, so reassigning is what makes SQLAlchemy flush the change (an
    in-place edit would be silently dropped).
    """
    if not stored or len(code or "") > MAX_CODE_INPUT_CHARS:
        return None
    candidate = hash_token(normalize_recovery_code(code))
    updated: list[dict[str, Any]] = []
    consumed = False
    for entry in stored:
        entry_hash = entry.get("hash") or ""
        if (
            not consumed
            and entry.get("used_at") is None
            and hmac.compare_digest(entry_hash, candidate)
        ):
            consumed = True
            updated.append({**entry, "used_at": datetime.now(UTC).isoformat()})
        else:
            updated.append(dict(entry))
    return updated if consumed else None


def count_unused_recovery_codes(stored: list[dict[str, Any]] | None) -> int:
    """How many recovery codes are still spendable (surfaced in the UI)."""
    if not stored:
        return 0
    return sum(1 for entry in stored if entry.get("used_at") is None)


# ---------------------------------------------------------------------------
# MFA challenge token
# ---------------------------------------------------------------------------


def create_challenge_token(
    user_id: uuid.UUID | str, *, method: str = "local"
) -> str:
    """Mint the short-lived token that stands in for "first factor already proven".

    Deliberately NOT built with ``create_access_token``: this token must never
    be usable as a session. The ``scope`` claim is what marks it, and
    ``app.api.deps.get_current_user_ungated`` refuses any token that carries
    one.

    ``method`` records *how* the first factor was proven — ``"local"`` for a
    password, ``"oidc"`` / ``"saml"`` for an SSO assertion. It is carried
    through so the audit entry written when the login finally completes names
    the real path instead of assuming a password was involved. It is
    descriptive only: nothing authorizes off it, so a forged value (which would
    require the signing key anyway) buys nothing.
    """
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "scope": MFA_TOKEN_SCOPE,
        "method": method,
        "iat": int(now.timestamp()),
        "exp": now + timedelta(seconds=MFA_CHALLENGE_TTL_SECONDS),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


class MfaChallenge(NamedTuple):
    """A validated challenge token, unpacked."""

    user_id: uuid.UUID
    issued_at: int
    # How the first factor was proven: "local", "oidc", or "saml".
    method: str


def decode_challenge_token(token: str) -> MfaChallenge:
    """Validate a challenge token.

    Raises :class:`MfaChallengeError` on anything that is not a currently-valid
    challenge token — including a perfectly good *session* token, which must not
    be accepted here either: replaying one would let a stolen session bypass the
    second factor it is supposed to be protected by.
    """
    try:
        payload = jwt.decode(
            token, settings.secret_key, algorithms=[settings.jwt_algorithm], leeway=JWT_CLOCK_SKEW_LEEWAY
        )
    # UnicodeError: a lone surrogate from the JSON body, which PyJWT encodes
    # before it parses.
    except (jwt.PyJWTError, UnicodeError) as exc:
        raise MfaChallengeError("Invalid or expired verification session") from exc

    if payload.get("scope") != MFA_TOKEN_SCOPE:
        raise MfaChallengeError("Invalid or expired verification session")
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError, TypeError) as exc:
        raise MfaChallengeError("Invalid or expired verification session") from exc

    issued_at = payload.get("iat")
    if not isinstance(issued_at, int):
        raise MfaChallengeError("Invalid or expired verification session")

    method = payload.get("method")
    return MfaChallenge(
        user_id=user_id,
        issued_at=issued_at,
        # Tokens minted before ``method`` existed (a rolling deploy with a
        # challenge already in flight) read as local, which is what they were.
        method=method if isinstance(method, str) and method else "local",
    )


def challenge_required(user: object) -> bool:
    """Whether *user* must clear a second factor before getting a session.

    The single predicate both login paths ask — local ``POST /auth/login`` and
    the SSO callbacks — so the two can never disagree about who is enrolled.

    ``mfa_secret`` is checked alongside the flag on purpose: an account that
    somehow ended up flagged without a secret would otherwise be handed a
    challenge no code on earth could answer, which is a lockout, not a
    protection.
    """
    return bool(
        getattr(user, "mfa_enabled", False) and getattr(user, "mfa_secret", None)
    )
