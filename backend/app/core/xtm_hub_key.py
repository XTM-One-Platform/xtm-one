# SPDX-License-Identifier: Apache-2.0

"""XTM One's signing key on the XTM Hub.

The XTM Hub is moving every product from the platform token received at
registration to one Ed25519 key pair per product instance: the instance signs
a short, single-use assertion for every Hub call (header
``XTM-Hub-Platform-Assertion``) and the Hub stores only the public key.

The key is never generated nor stored: it is derived from ``SECRET_KEY`` with
HKDF-SHA256 (empty salt, the convention of :func:`app.core.xtm_auth._hkdf_derive`)
on a label of its own, ``xtmone:xtm-hub:platform-assertion::v<version>``, so
every replica derives the same key and ``SECRET_KEY`` - already the root of the
Fernet key of every stored secret - stays the one secret to protect. The label
differs from the xtm-auth key's (``xtm:jwt:ed25519:seed::v1``, public at
``/xtm/auth/jwks``): the Hub key can be replaced alone, and a token made for one
use is never valid for the other.

The version is the instance setting ``PlatformSettings.xtm_hub_key_version``
(1 until changed). A new version - or a new ``SECRET_KEY`` - gives a new key,
which the Hub learns only when an administrator registers the platform again:
there is no rotation API.

While ``SECRET_KEY`` is guessable (:func:`app.core.security.secret_key_is_guessable`)
anyone could derive the key and sign as this instance, so no key is produced:
:class:`XtmHubKeyUnavailableError`.

Nothing calls the Hub with it yet: signing the Hub calls, enrolling the key
and registering with it come next.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import secret_key_is_guessable
from app.core.xtm_auth import _hkdf_derive
from app.models.platform_settings import PlatformSettings

#: The identifier XTM One registers under on the Hub (``platformIdentifier``),
#: carried by every assertion as ``identifier``.
PLATFORM_IDENTIFIER = "xtmone"

#: The HTTP header an assertion travels in.
ASSERTION_HEADER = "XTM-Hub-Platform-Assertion"
#: The ``aud`` of every assertion: a constant, not a URL, because the URL a
#: platform reaches the Hub on differs between deployments.
ASSERTION_AUDIENCE = "xtm-hub"
#: How long an assertion lives; the Hub refuses one signed for longer.
ASSERTION_TTL = timedelta(minutes=5)

#: The key version an instance starts at (``PlatformSettings.xtm_hub_key_version``).
DEFAULT_KEY_VERSION = 1

_LABEL = "xtmone:xtm-hub:platform-assertion"


class XtmHubKeyUnavailableError(RuntimeError):
    """No key can be derived: ``SECRET_KEY`` is guessable. ``str()`` is safe to show an admin."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class XtmHubKey:
    """One version of the instance's Hub key pair."""

    version: int
    kid: str
    public_key_raw: bytes
    _private_key: Ed25519PrivateKey = field(repr=False)

    def public_jwk(self) -> dict[str, str]:
        """The public JWK sent to the Hub: never the private part."""
        return {
            "kty": "OKP",
            "crv": "Ed25519",
            "x": _b64url(self.public_key_raw),
            "kid": self.kid,
            "alg": "EdDSA",
            "use": "sig",
        }

    def sign_assertion(self, platform_id: uuid.UUID | str) -> str:
        """A fresh assertion for one Hub call, as *platform_id*.

        Header ``{alg: EdDSA, kid, typ: JWT}``; claims ``iss`` (the platform
        id), ``aud`` (``xtm-hub``), ``iat``, ``exp`` (``iat`` + 5 minutes), a
        unique ``jti`` the Hub refuses to see twice, and ``identifier``.
        """
        now = datetime.now(UTC).replace(microsecond=0)
        payload: dict[str, Any] = {
            "iss": str(platform_id),
            "aud": ASSERTION_AUDIENCE,
            "iat": now,
            "exp": now + ASSERTION_TTL,
            "jti": str(uuid.uuid4()),
            "identifier": PLATFORM_IDENTIFIER,
        }
        return jwt.encode(
            payload,
            self._private_key,
            algorithm="EdDSA",
            headers={"kid": self.kid, "typ": "JWT"},
        )


def _jwk_thumbprint(public_key_raw: bytes) -> str:
    """The RFC 7638 thumbprint of an Ed25519 public key (RFC 8037), base64url."""
    members = {"crv": "Ed25519", "kty": "OKP", "x": _b64url(public_key_raw)}
    canonical = json.dumps(members, separators=(",", ":"), sort_keys=True).encode("ascii")
    return _b64url(hashlib.sha256(canonical).digest())


def derive_key(version: int) -> XtmHubKey:
    """The instance's Hub key at *version*, derived from ``SECRET_KEY``.

    The same version always gives the same key, on every replica; another
    version gives another key and ``kid`` (the key's RFC 7638 thumbprint).
    """
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError(f"The XTM Hub key version must be a positive integer, got {version!r}")
    secret = settings.secret_key
    if secret_key_is_guessable(secret):
        raise XtmHubKeyUnavailableError(
            "XTM One has no XTM Hub signing key while SECRET_KEY is guessable: "
            "set SECRET_KEY to a random string of at least 32 characters."
        )
    seed = _hkdf_derive(secret.encode("utf-8"), info=f"{_LABEL}::v{version}".encode("ascii"))
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    public_key_raw = private_key.public_key().public_bytes(
        encoding=Encoding.Raw, format=PublicFormat.Raw
    )
    return XtmHubKey(
        version=version,
        kid=_jwk_thumbprint(public_key_raw),
        public_key_raw=public_key_raw,
        _private_key=private_key,
    )


async def key_version(db: AsyncSession) -> int:
    """The instance setting ``xtm_hub_key_version`` (1 before the settings row exists)."""
    stored = await db.scalar(select(PlatformSettings.xtm_hub_key_version).limit(1))
    return DEFAULT_KEY_VERSION if stored is None else int(stored)


async def instance_key(db: AsyncSession) -> XtmHubKey:
    """The instance's Hub key at its configured version."""
    return derive_key(await key_version(db))
