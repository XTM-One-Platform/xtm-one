# SPDX-License-Identifier: Apache-2.0

"""Time-based one-time passwords (RFC 6238): the one implementation of the platform.

A browser profile's TOTP secret (``services/browser_profiles``), a secret read
from a vault for it, and the one-time password field of a 1Password item
(``integrations/providers/onepassword.py``) all compute their codes here, so a
seed gives the same code wherever the platform uses it. The secret is read as
people paste it: a base32 string (spaces, dashes and lowercase accepted,
padding optional) or the ``otpauth://totp/...`` URI a QR code encodes, whose
``algorithm``, ``digits`` and ``period`` parameters are honoured.

Standard library only (``hmac`` / ``hashlib`` / ``base64``), pinned to the
RFC 6238 appendix B vectors by ``tests/test_browser_profile_totp.py``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import struct
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

_ALGORITHMS = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256, "SHA512": hashlib.sha512}
_BASE32_RE = re.compile(r"^[A-Z2-7]+=*$")
#: A value that is a reference INTO a secret manager, not the secret itself:
#: a ``scheme://`` other than ``otpauth`` (1Password ``op://``, ``vault://``,
#: ``awssecret://``, ...) or a ``${...}`` / ``{{...}}`` interpolation. Matched so
#: the error names the shape instead of the opaque "not base32".
_SECRET_REFERENCE_RE = re.compile(r"^(?:\$\{.+\}|\{\{.+\}\}|[a-zA-Z][a-zA-Z0-9+.\-]*://)")


class TOTPSecretError(ValueError):
    """A TOTP secret that cannot be read; ``str()`` never contains the secret."""


def _looks_like_secret_reference(text: str) -> bool:
    """Whether *text* is a pointer into a secret manager rather than the secret (never an ``otpauth://`` URI)."""
    if text.lower().startswith("otpauth://"):
        return False
    return bool(_SECRET_REFERENCE_RE.match(text))


@dataclass(frozen=True, slots=True)
class TOTPConfig:
    key: bytes
    digits: int = 6
    period: int = 30
    algorithm: str = "SHA1"
    issuer: str = ""


def _decode_base32(value: str) -> bytes:
    cleaned = re.sub(r"[\s-]", "", value).upper().rstrip("=")
    if not cleaned or not _BASE32_RE.match(cleaned):
        raise TOTPSecretError("The TOTP secret is not a base32 key or an otpauth:// URI.")
    padded = cleaned + "=" * (-len(cleaned) % 8)
    try:
        key = base64.b32decode(padded)
    except ValueError:  # binascii.Error is a ValueError
        raise TOTPSecretError("The TOTP secret is not a valid base32 key.") from None
    if len(key) < 10:
        # RFC 4226 asks for at least 128 bits and recommends 160; 80 bits is
        # the shortest real authenticators issue.
        raise TOTPSecretError("The TOTP secret is too short to be an authenticator key.")
    return key


def parse_secret(value: str) -> TOTPConfig:
    """The TOTP parameters a pasted secret or ``otpauth://`` URI stands for."""
    text = (value or "").strip()
    if not text:
        raise TOTPSecretError("The TOTP secret is empty.")
    if _looks_like_secret_reference(text):
        raise TOTPSecretError(
            "The TOTP secret looks like a secret-manager reference (op://, ${...}), not the secret: connect the "
            "secret manager as a secret vault and pick the one-time password from it, or paste the base32 secret "
            "itself or the otpauth:// URI the authenticator QR code encodes."
        )
    if text.lower().startswith("otpauth://"):
        parsed = urlparse(text)
        if parsed.netloc.lower() != "totp":
            raise TOTPSecretError("Only time-based (otpauth://totp/...) one-time passwords are supported.")
        params = {k.lower(): v[-1] for k, v in parse_qs(parsed.query).items() if v}
        secret = unquote(params.get("secret", ""))
        if not secret:
            raise TOTPSecretError("The otpauth:// URI has no secret.")
        algorithm = params.get("algorithm", "SHA1").upper()
        if algorithm not in _ALGORITHMS:
            raise TOTPSecretError("The otpauth:// URI names an unsupported algorithm.")
        try:
            digits = int(params.get("digits", "6"))
            period = int(params.get("period", "30"))
        except ValueError as exc:
            raise TOTPSecretError("The otpauth:// URI has a digits or period that is not a number.") from exc
        if digits not in (6, 7, 8) or not 10 <= period <= 300:
            raise TOTPSecretError("The otpauth:// URI has digits or a period no authenticator uses.")
        issuer = params.get("issuer", "")
        if not issuer and parsed.path:
            label = unquote(parsed.path.lstrip("/"))
            if ":" in label:
                issuer = label.split(":", 1)[0]
        return TOTPConfig(key=_decode_base32(secret), digits=digits, period=period, algorithm=algorithm, issuer=issuer)
    return TOTPConfig(key=_decode_base32(text))


def hotp(key: bytes, counter: int, *, digits: int = 6, algorithm: str = "SHA1") -> str:
    """RFC 4226 HOTP value of *counter*."""
    digest = hmac.new(key, struct.pack(">Q", counter), _ALGORITHMS[algorithm]).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**digits)).zfill(digits)


def code_at(config: TOTPConfig, timestamp: float) -> str:
    """The code valid at *timestamp* (seconds since the epoch)."""
    return hotp(config.key, int(timestamp) // config.period, digits=config.digits, algorithm=config.algorithm)


def seconds_left(config: TOTPConfig, timestamp: float) -> int:
    """Seconds the code valid at *timestamp* stays valid."""
    return config.period - int(timestamp % config.period)


def current_counter(secret: str, *, now: float | None = None, min_remaining: float = 3.0) -> int:
    """The time step (RFC 6238 ``T``) whose code is typed now for *secret*.

    A code typed in the last seconds of its window may reach the server in the
    next one: with less than *min_remaining* seconds left, the next window is
    used (servers accept one step of clock drift either way).
    """
    config = parse_secret(secret)
    moment = time.time() if now is None else now
    remaining = config.period - (moment % config.period)
    if remaining < min_remaining:
        moment += remaining
    return int(moment) // config.period


def code_for_counter(secret: str, counter: int) -> str:
    """The code of time step *counter* for *secret*."""
    config = parse_secret(secret)
    return hotp(config.key, counter, digits=config.digits, algorithm=config.algorithm)


def seconds_until(secret: str, counter: int, *, now: float | None = None) -> float:
    """Seconds until time step *counter* begins (0 when it has)."""
    config = parse_secret(secret)
    moment = time.time() if now is None else now
    return max(0.0, counter * config.period - moment)


def current_code(secret: str, *, now: float | None = None, min_remaining: float = 3.0) -> str:
    """The code to type now for *secret* (:func:`current_counter`'s window)."""
    return code_for_counter(secret, current_counter(secret, now=now, min_remaining=min_remaining))


def validate_secret(secret: str) -> None:
    """Raise :class:`TOTPSecretError` when *secret* cannot generate codes."""
    parse_secret(secret)


#: A stored TOTP secret's health, as the profile status and the tools report it.
SECRET_NONE = "none"  # noqa: S105 - a status label, not a secret
SECRET_VALID = "valid"  # noqa: S105 - a status label, not a secret
SECRET_INVALID = "invalid"  # noqa: S105 - a status label, not a secret
#: Read from a secret vault when it is used: checked then, never stored.
SECRET_VAULT = "vault"  # noqa: S105 - a status label, not a secret


def secret_status(secret: str | None) -> str:
    """``none`` (unset), ``valid`` (usable) or ``invalid`` (set but not a base32 key or otpauth:// URI)."""
    if not secret:
        return SECRET_NONE
    try:
        validate_secret(secret)
        return SECRET_VALID
    except TOTPSecretError:
        return SECRET_INVALID
