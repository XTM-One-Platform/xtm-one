# SPDX-License-Identifier: Apache-2.0

"""Cross-platform Ed25519 JWKS authentication.

Implements the Filigran XTM cross-platform JWT spec:
- Derives an Ed25519 keypair deterministically from SECRET_KEY via HKDF
- Exposes the public key as a JWKS payload
- Signs JWTs for cross-platform calls (EdDSA / Ed25519)
- Validates incoming JWTs from trusted issuers (registered platforms)
  using PyJWKClient for automatic JWKS fetch, cache and kid resolution
- Publishes this platform's identity (``GET /xtm/auth/metadata``)

Issuers and audiences are identities, compared normalized
(:mod:`app.core.url_identity`); who is trusted and where its keys are fetched
is :mod:`app.core.platform_identity`.
"""

import asyncio
import functools
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)
from jwt import PyJWK, PyJWKClient, PyJWKClientError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.platform_identity import RegisteredPlatform, platform_for_issuer
from app.core.security import JWT_CLOCK_SKEW_LEEWAY
from app.core.url_identity import normalize_url_identity, url_identity

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_XTM_JWT_TTL_MINUTES = 10
#: How long a token this platform signs lives, in seconds (``expires_in`` of the exchange).
XTM_JWT_TTL_SECONDS = _XTM_JWT_TTL_MINUTES * 60

#: The longest an incoming cross-platform token may live (``exp - iat``). Every
#: product signs for 10 minutes; the margin covers a signer that reads its clock
#: once for ``iat`` and again for ``exp``.
XTM_JWT_MAX_LIFETIME = timedelta(minutes=15)

#: Claims an incoming cross-platform token must carry for PyJWT to check them:
#: an absent ``exp`` is otherwise never checked, and the token never expires.
_XTM_JWT_REQUIRED_TIME_CLAIMS = ["exp", "iat"]

#: Where every XTM Suite product serves its keys and its identity, under its base URL.
XTM_JWKS_PATH = "/xtm/auth/jwks"
XTM_METADATA_PATH = "/xtm/auth/metadata"

# ---------------------------------------------------------------------------
# Key derivation (deterministic from SECRET_KEY, cached at module level)
# ---------------------------------------------------------------------------


def _hkdf_derive(ikm: bytes, info: bytes, length: int = 32) -> bytes:
    """Derive bytes using HKDF-SHA256 with empty salt (matches OpenCTI convention)."""
    return HKDF(
        algorithm=SHA256(),
        length=length,
        salt=None,  # empty salt
        info=info,
    ).derive(ikm)


def _derive_ed25519_keypair() -> tuple[Ed25519PrivateKey, str, bytes]:
    """Derive the Ed25519 private key and kid from SECRET_KEY.

    Derivation paths follow OpenCTI's ``derivationPath.join(":") + "::v" + version``
    convention, enabling future key rotation by bumping the version number.

    Returns:
        (private_key, kid_hex, public_key_raw_bytes)
    """
    seed = settings.secret_key.encode("utf-8")

    # Derive the 32-byte Ed25519 seed
    ed25519_seed = _hkdf_derive(seed, info=b"xtm:jwt:ed25519:seed::v1")

    # Derive the kid
    kid_material = _hkdf_derive(seed, info=b"xtm:jwt:ed25519:kid::v1")
    kid = hashlib.sha256(kid_material).digest()[:8].hex()

    # Build the key objects
    private_key = Ed25519PrivateKey.from_private_bytes(ed25519_seed)
    public_key_raw = private_key.public_key().public_bytes(
        encoding=Encoding.Raw, format=PublicFormat.Raw
    )

    return private_key, kid, public_key_raw


# Module-level cache — deterministic, computed once
_private_key, _kid, _public_key_raw = _derive_ed25519_keypair()


# ---------------------------------------------------------------------------
# JWKS payload (public, no DB access needed)
# ---------------------------------------------------------------------------


def get_jwks_payload() -> dict[str, list[dict[str, str]]]:
    """Return the JWKS payload with this platform's Ed25519 public key."""
    import base64

    x = base64.urlsafe_b64encode(_public_key_raw).rstrip(b"=").decode("ascii")
    return {
        "keys": [
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "use": "sig",
                "kid": _kid,
                "alg": "EdDSA",
                "x": x,
            }
        ]
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_url(url: str) -> str:
    """The canonical form of a URL configured for this platform (``BASE_URL``, a
    run's callback base URL, the LLM gateway), whose userinfo has always been
    dropped rather than refused. Identities read from a token or a
    registration are compared strictly (:func:`app.core.url_identity.url_identity`)."""
    return normalize_url_identity(url, lenient=True)


def xtm_issuer() -> str:
    """This platform's identity: the ``iss`` it signs, the ``aud`` it expects."""
    return _normalize_url(settings.base_url)


@functools.lru_cache(maxsize=8)
def _accepted_audiences(base_url: str, configured: tuple[str, ...]) -> frozenset[str]:
    audiences = {_normalize_url(base_url)}
    audiences.update(identity for identity in map(url_identity, configured) if identity)
    return frozenset(audiences)


def accepted_audiences() -> frozenset[str]:
    """Every ``aud`` an incoming cross-platform token may name, canonical.

    The identity, plus ``XTM_AUTH_ACCEPTED_AUDIENCES``: the URLs platforms are
    configured to reach this XTM One on, for a platform that addresses its
    tokens to that URL rather than to the identity it can discover at
    ``/xtm/auth/metadata``. Computed once per settings value.
    """
    return _accepted_audiences(settings.base_url, tuple(settings.xtm_auth_accepted_audiences))


def log_accepted_audiences() -> None:
    """Say at startup which extra audiences a cross-platform token may carry."""
    extra = list(settings.xtm_auth_accepted_audiences)
    if extra:
        logger.warning(
            "Cross-platform tokens addressed to %s are accepted besides this platform's identity %s "
            "(XTM_AUTH_ACCEPTED_AUDIENCES). Remove an entry once the platforms addressing it read "
            "%s.",
            ", ".join(extra),
            xtm_issuer(),
            XTM_METADATA_PATH,
        )


def get_auth_metadata() -> dict[str, Any]:
    """This platform's identity document, served at ``GET /xtm/auth/metadata``.

    ``issuer`` is the ``iss`` of every token this platform signs, ``audience``
    the ``aud`` a token addressed to it must carry, ``jwks_uri`` its keys at
    its public URL. A platform that reaches XTM One on another URL trusts this
    issuer, addresses this audience, and fetches the keys next to this document
    on the URL it reached it on.
    """
    issuer = xtm_issuer()
    return {
        "issuer": issuer,
        "audience": issuer,
        "jwks_uri": f"{issuer}{XTM_JWKS_PATH}",
        "token_signing_alg_values_supported": ["EdDSA"],
    }


# ---------------------------------------------------------------------------
# JWT emission
# ---------------------------------------------------------------------------


def issue_xtm_jwt(user_id: uuid.UUID, user_email: str, audience_url: str) -> str:
    """Sign a cross-platform JWT for the given user.

    Claims:
        iss: this platform's base URL
        sub: user ID (Copilot UUID — used to identify user when validating own JWTs)
        email: user email (used by other XTM platforms to match a local user)
        aud: target platform base URL
        iat/exp: short TTL (10 min)
        jti: unique token ID (for future replay protection)
    """
    now = datetime.now(UTC)

    payload: dict[str, Any] = {
        "iss": xtm_issuer(),
        "sub": str(user_id),
        "email": user_email,
        "iat": now,
        "exp": now + timedelta(minutes=_XTM_JWT_TTL_MINUTES),
        "jti": str(uuid.uuid4()),
        "aud": audience_url,
    }

    return jwt.encode(
        payload,
        _private_key,
        algorithm="EdDSA",
        headers={"kid": _kid},
    )


def sign_platform_jwt(payload: dict[str, Any]) -> str:
    """Sign ``payload`` with the platform's Ed25519 key (the ``kid`` of the JWKS).

    For tokens other parties verify against ``/xtm/auth/jwks`` (the OpenID
    Connect ID token); the caller owns the claims, ``aud`` included.
    """
    return jwt.encode(payload, _private_key, algorithm="EdDSA", headers={"kid": _kid})


# ---------------------------------------------------------------------------
# JWT verification (incoming cross-platform JWTs)
# ---------------------------------------------------------------------------


@dataclass
class CrossJWTPayload:
    """Verified cross-platform JWT claims."""

    iss: str
    sub: str  # user ID on the issuing platform
    email: str
    aud: str | list[str]
    jti: str
    # The ``platform_identifier`` of the active registration whose URL is
    # ``iss``: the platform's identity, which outlives a change of its URL.
    platform_identifier: str | None = None


def _get_jwk_client(base_url: str) -> PyJWKClient:
    """Get or create the PyJWKClient of the keys served under *base_url*.

    *base_url* is where XTM One reaches a trusted issuer, read from its
    registration (:class:`RegisteredPlatform`), never a URL a token names. The
    instance cache lives in ``core.jwks`` (shared with A2A Entra federation),
    keyed by the raw JWKS URL this builds.
    """
    from app.core.jwks import get_jwk_client

    return get_jwk_client(f"{base_url.strip().rstrip('/')}{XTM_JWKS_PATH}")


async def _resolve_platform_signing_key(platform: RegisteredPlatform, token: str) -> PyJWK:
    """The signing key of *token*, fetched where XTM One reaches *platform*.

    Its internal URL when the registration captured one, then its identity as
    registered, where the keys were always fetched before the internal URL
    was: an internal URL that does not serve the keys never refuses a token
    the identity still verifies. Never a URL read from the token.
    """
    sources = [platform.reach_url]
    if platform.api_url:
        sources.append(platform.identity)
    failures: list[str] = []
    for source in sources:
        client = _get_jwk_client(source)
        try:
            return await asyncio.to_thread(client.get_signing_key_from_jwt, token)
        except PyJWKClientError as exc:
            failures.append(f"{source}{XTM_JWKS_PATH}: {exc}")
    raise ValueError(
        f"Failed to resolve signing key for {platform.identity} from " + "; ".join(failures)
    )


def _check_audience(claim: object) -> None:
    """Refuse a token not addressed to this platform (RFC 7519 ``aud``: a string or an array).

    Each value is compared as an identity against :func:`accepted_audiences`,
    so a trailing slash, the case of the scheme and host or a default port
    never refuse a token, while another host, port, path or any userinfo does.
    """
    if claim is None:
        raise jwt.MissingRequiredClaimError("aud")
    values = [claim] if isinstance(claim, str) else claim
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise jwt.InvalidAudienceError("Invalid claim format in token")
    accepted = accepted_audiences()
    if not any(url_identity(value) in accepted for value in values):
        raise jwt.InvalidAudienceError("Audience doesn't match")


def _check_lifetime(payload: dict[str, Any]) -> None:
    """Refuse a token signed to live longer than :data:`XTM_JWT_MAX_LIFETIME`.

    Computed from the claims alone, so the clock-skew leeway never widens it.
    Sound because PyJWT has already refused an ``iat`` more than
    :data:`~app.core.security.JWT_CLOCK_SKEW_LEEWAY` in the future, so the
    bound caps ``exp`` at that leeway past this platform's clock plus the
    maximum. The expiry takes the leeway too, so a token is accepted for at
    most the maximum plus twice the leeway (17 minutes) of this platform's
    clock. Both claims were read as integers by PyJWT's own checks, which is
    how they are read here.
    """
    lifetime = int(payload["exp"]) - int(payload["iat"])
    limit = int(XTM_JWT_MAX_LIFETIME.total_seconds())
    if lifetime > limit:
        raise jwt.InvalidTokenError(f"Token lifetime {lifetime}s exceeds the {limit}s maximum")


async def verify_xtm_jwt(token: str, db: AsyncSession) -> CrossJWTPayload | None:
    """Verify an incoming cross-platform JWT from a trusted issuer.

    Steps:
        1. Decode without verification to peek at iss
        2. Check iss against registered platform URLs
        3. Use PyJWKClient to resolve the signing key from the platform's
           API URL (handles JWKS fetch, cache, kid lookup, and force-refresh
           on unknown kid)
        4. Verify the EdDSA signature + claims

    Returns:
        CrossJWTPayload on success, None on any failure (logged as warning).
    """
    try:
        return await _verify_xtm_jwt(token, db)
    except ValueError as exc:
        # Configuration / trust failures: untrusted issuer, unreachable JWKS,
        # missing claims. These are almost always actionable (a base-URL or
        # platform-registration mismatch), so surface the specific reason —
        # ``exc`` already carries the issuer URL and the underlying cause —
        # instead of a generic "verification failed" that hides it.
        logger.warning("XTM JWT rejected: %s", exc)
        return None
    except jwt.ExpiredSignatureError:
        logger.warning("XTM JWT rejected: token expired")
        return None
    except jwt.InvalidAudienceError as exc:
        logger.warning(
            "XTM JWT rejected: audience mismatch (this platform accepts %s): %s",
            ", ".join(sorted(accepted_audiences())),
            exc,
        )
        return None
    except jwt.PyJWTError as exc:
        logger.warning(
            "XTM JWT rejected: signature/claim validation failed: %s", exc
        )
        return None
    except Exception:
        logger.warning(
            "XTM JWT verification failed (unexpected error)", exc_info=True
        )
        return None


async def _verify_xtm_jwt(token: str, db: AsyncSession) -> CrossJWTPayload:
    """Verify an incoming cross-platform JWT from a trusted issuer.

    Steps:
        1. Decode without verification to peek at iss
        2. Check iss against registered platform URLs
        3. Use PyJWKClient to resolve the signing key from the platform's
           API URL (handles JWKS fetch, cache, kid lookup, and force-refresh
           on unknown kid)
        4. Verify the EdDSA signature + claims

    Raises:
        jwt.PyJWTError: on any JWT validation failure
        ValueError: if the issuer is not trusted or claims are invalid
        PyJWKClientError: if the JWKS endpoint is unreachable or kid not found
    """
    # 1. Peek at unverified claims to get issuer
    try:
        unverified_payload = jwt.decode(
            token,
            options={"verify_signature": False},
            algorithms=["EdDSA"],
        )
    except jwt.PyJWTError:
        raise

    issuer = unverified_payload.get("iss")
    if not issuer:
        raise ValueError("JWT missing 'iss' claim")

    # 2. Check issuer against trusted platform registrations
    platform = await platform_for_issuer(db, issuer)
    if platform is None:
        raise ValueError(f"Untrusted JWT issuer: {issuer}")

    # 3. Resolve the signing key where the registration is reached, never at a
    # URL the token names (handles fetch, cache, kid lookup)
    signing_key = await _resolve_platform_signing_key(platform, token)

    # 4. Full verification with signature check. The audience is compared
    # normalized (a trailing slash or a default port is the same URL).
    payload = jwt.decode(
        token,
        signing_key.key,
        algorithms=["EdDSA"],
        issuer=issuer,
        leeway=JWT_CLOCK_SKEW_LEEWAY,
        options={
            "require": _XTM_JWT_REQUIRED_TIME_CLAIMS,
            "verify_exp": True,
            "verify_iat": True,
            "verify_aud": False,
            "verify_iss": True,
        },
    )
    _check_lifetime(payload)
    _check_audience(payload.get("aud"))

    # 5. Validate required claims
    sub = payload.get("sub")
    email = payload.get("email")
    jti = payload.get("jti")
    if not sub:
        raise ValueError("JWT missing 'sub' claim")
    if not email:
        raise ValueError("JWT missing 'email' claim")
    if not jti:
        raise ValueError("JWT missing 'jti' claim")

    return CrossJWTPayload(
        iss=payload["iss"],
        sub=sub,
        email=email,
        aud=payload["aud"],
        jti=jti,
        platform_identifier=platform.platform_identifier,
    )


