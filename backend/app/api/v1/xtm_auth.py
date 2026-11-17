# SPDX-License-Identifier: Apache-2.0

"""XTM cross-platform authentication — JWKS and metadata endpoints.

Exposes the platform's Ed25519 public key(s) so other Filigran products
can validate JWTs emitted by this instance, and the identity those JWTs
carry, so a product that reaches this instance on an internal URL still
knows the ``iss`` to trust and the ``aud`` to address.

Public endpoints — no authentication required.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.xtm_auth import get_auth_metadata, get_jwks_payload

router = APIRouter()


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
