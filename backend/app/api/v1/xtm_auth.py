# SPDX-License-Identifier: Apache-2.0

"""XTM cross-platform authentication — JWKS, metadata and user-token exchange.

Exposes the platform's Ed25519 public key(s) so other Filigran products
can validate JWTs emitted by this instance, and the identity those JWTs
carry, so a product that reaches this instance on an internal URL still
knows the ``iss`` to trust and the ``aud`` to address.

The keys and the metadata are public. The exchange (``POST /exchange``)
answers only a registered platform's per-user token: XTM One is the single
issuer of the token a platform forwards to another one for that user.
"""

import logging

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.platform_identity import platform_for_issuer
from app.core.rate_limit import enforce_rate_limit
from app.core.url_identity import url_identity
from app.core.xtm_auth import (
    XTM_JWT_TTL_SECONDS,
    CrossJWTPayload,
    accepted_audiences,
    get_auth_metadata,
    get_jwks_payload,
    issue_xtm_jwt,
    verify_xtm_jwt,
)
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter()

#: Longest audience the exchange reads: an address, never a payload (the
#: ``platform_url`` bound of a registration).
EXCHANGE_AUDIENCE_MAX_LENGTH = 2048
#: Exchanges one registered platform may make per minute, all its users
#: together: a lookup per page a person opens fits, a runaway loop does not.
EXCHANGE_LIMIT_PER_MINUTE = 600

CALLER_REFUSED = "A registered platform's user token is required"
AUDIENCE_REFUSED = "The audience is not another active registered platform"
USER_REFUSED = "User not found"


@router.get("/jwks", summary="JWKS public keys", tags=["xtm-auth"])
async def jwks() -> JSONResponse:
    """Return the JWKS payload with this platform's Ed25519 public key(s).

    Standard JWKS format with OKP/Ed25519 keys. Used by other Filigran
    products to verify cross-platform JWTs.
    """
    return JSONResponse(
        content=get_jwks_payload(),
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/metadata", summary="Cross-platform token metadata", tags=["xtm-auth"])
async def metadata() -> JSONResponse:
    """Return the identity this platform's cross-platform JWTs carry.

    ``issuer`` is the ``iss`` of every token this platform signs and
    ``audience`` the ``aud`` it expects on the tokens addressed to it: its
    public base URL, which in a split-horizon deployment differs from the URL
    a caller reaches it on. The keys stay at ``/xtm/auth/jwks`` on whichever
    URL the caller used to fetch this document; ``jwks_uri`` names them at the
    public URL.
    """
    return JSONResponse(
        content=get_auth_metadata(),
        headers={"Cache-Control": "public, max-age=3600"},
    )


class TokenExchangeRequest(BaseModel):
    #: The ``platform_url`` of the registered platform the token is for.
    audience: str = Field(min_length=1, max_length=EXCHANGE_AUDIENCE_MAX_LENGTH)


def _bearer(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=CALLER_REFUSED)
    return token


async def _verified_caller(token: str, db: AsyncSession) -> CrossJWTPayload:
    """The claims of *token* when an ACTIVE registration signed it for this XTM One, else 401.

    The platform sign-in's own verification (:func:`verify_xtm_jwt`): issuer,
    keys, signature, lifetime and audience. A local session or an API key is
    never a platform's user token.
    """
    try:
        algorithm = jwt.get_unverified_header(token).get("alg")
    except jwt.PyJWTError:
        algorithm = None
    payload = await verify_xtm_jwt(token, db) if algorithm == "EdDSA" else None
    if payload is None or not payload.platform_identifier:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=CALLER_REFUSED)
    return payload


@router.post("/exchange", summary="Exchange a platform user token", tags=["xtm-auth"])
async def exchange_user_token(
    body: TokenExchangeRequest,
    request: Request,
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """An XTM One token for the caller's user, addressed to another registered platform.

    The bearer is the calling platform's own per-user token addressed to XTM
    One, verified as at sign-in (401 otherwise: unknown, suspended or revoked
    registration, expired, foreign audience). The user is the one that token
    names, never one the body names, and must hold an active XTM One account
    (403, nothing provisioned). The audience must be another ACTIVE
    registration, never the caller nor XTM One (403). The answer is signed
    like every XTM One token (EdDSA, ``/xtm/auth/jwks``), ``sub`` the XTM One
    user id, ``aud`` the target's registered identity, for 10 minutes. Never
    cached, never logged.
    """
    caller = await _verified_caller(_bearer(authorization), db)
    await enforce_rate_limit(
        request,
        "xtm-auth-exchange-platform",
        limit=EXCHANGE_LIMIT_PER_MINUTE,
        window_seconds=60,
        bucket=caller.platform_identifier,
    )

    target = await platform_for_issuer(db, body.audience)
    if (
        target is None
        or target.platform_identifier == caller.platform_identifier
        or url_identity(target.identity) in accepted_audiences()
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=AUDIENCE_REFUSED)

    email = caller.email.strip().lower()
    user = (await db.execute(select(User).where(func.lower(User.email) == email))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=USER_REFUSED)

    token = issue_xtm_jwt(user.id, user.email, target.identity)
    logger.info(
        "XTM user token exchanged: platform %s for platform %s, user %s",
        caller.platform_identifier,
        target.platform_identifier,
        user.id,
    )
    return JSONResponse(
        content={
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": XTM_JWT_TTL_SECONDS,
            "audience": target.identity,
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )
