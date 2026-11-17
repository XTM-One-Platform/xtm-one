# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The A2A partner grant.

RFC 6749 §4.4 client credentials, registered onto the platform's authorization
server at startup.

It lives here rather than in the authorization server because it is not a
generic grant that A2A happens to use — it refuses any client without a live
exposure, so it *is* the A2A partner grant. The authorization server keeps what
is genuinely its own: the endpoint, client authentication, and the response
envelope. This module keeps the policy: which clients qualify, whose identity
the token carries, and what claims confine it.

That split is also what lets the dependency point one way. The authorization
server no longer imports anything from ``app.a2a``; this module imports from
it.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.a2a.tokens import (
    A2A_TOKEN_EXPIRE_MINUTES,
    A2A_TOKEN_SCOPE,
    A2A_TOKEN_TYPE,
    create_a2a_access_token,
)


async def handle_client_credentials(data: dict, db: AsyncSession) -> JSONResponse:
    """Issue a partner token for a client bound to a live exposure.

    Every rejection returns the SAME ``invalid_client``. Distinguishing "no
    such client" from "wrong secret" from "not an A2A client" would let a
    caller enumerate registrations, and the enumeration would be free.
    """
    from app.a2a.exposure import (
        get_resolvable_exposure,
        resolvable_exposures_for_integration,
    )
    from app.a2a.providers import OAUTH_A2A_PROVIDER
    from app.api.v1.mcp_oauth import authenticate_client

    client_id = data.get("client_id")
    client_secret = data.get("client_secret")
    if not client_id or not client_secret:
        return JSONResponse(content={"error": "invalid_request"}, status_code=400)

    invalid = JSONResponse(content={"error": "invalid_client"}, status_code=401)

    client = await authenticate_client(
        db, client_id, client_secret, grant_type="client_credentials"
    )
    if client is None:
        return invalid

    # A client that is neither an integration's credential nor an exposure's
    # is refused even though it asked for the right grant: A2A partner
    # credentials have their own lifecycle, distinct from the
    # partner-initiated dynamic registration the authorization-code flow
    # serves.
    #
    # Whichever shape it is, the SAME rule decides: the credential must still
    # address a surface that answers. That is enforced through
    # ``_resolvable_exposures`` in both branches — shared with the task
    # surface precisely so the two cannot diverge, and it is what makes
    # revocation (and FR-007's deactivated run-as user) need no separate step
    # anyone could forget to run.
    if client.a2a_exposure_id is not None:
        exposure = await get_resolvable_exposure(db, client.a2a_exposure_id)
        if exposure is None:
            return invalid
        # ``sub`` is the exposure's run-as user, never the actor who exposed
        # the agent - exposing must not lend the exposer's own reach.
        subject = str(exposure.run_as_user_id)
    elif client.a2a_integration_id is not None:
        # An ``oauth_a2a`` integration's credential reaches every agent that
        # binds it, so it stays mintable while ANY of them is live. The token
        # is not narrowed to one of them here: the task surface re-checks the
        # binding per request (``_client_covers_exposure``), which is where
        # an unbinding must take effect.
        reachable = await resolvable_exposures_for_integration(
            db, client.a2a_integration_id, OAUTH_A2A_PROVIDER
        )
        if not reachable:
            return invalid
        # No single run-as user exists across several agents, and it would be
        # the wrong thing to bake in anyway: each run executes as ITS OWN
        # exposure's run-as user (``tasks._executing_user_id``), never as the
        # token's subject. ``sub`` is attribution only — the human who owns
        # this partner relationship, falling back to a reachable exposure's
        # run-as user for a company-managed integration, which has no owner.
        subject = str(
            await _integration_owner(db, client.a2a_integration_id)
            or reachable[0].run_as_user_id
        )
    else:
        return invalid
    return JSONResponse(
        content={
            "access_token": create_a2a_access_token(subject, client_id=client_id),
            "token_type": "Bearer",
            "expires_in": A2A_TOKEN_EXPIRE_MINUTES * 60,
            "scope": A2A_TOKEN_SCOPE,
        },
        headers={"Cache-Control": "no-store"},
    )


async def _integration_owner(db: AsyncSession, integration_id) -> object | None:
    """The user who owns an integration, or ``None`` for company-managed."""
    from sqlalchemy import select

    from app.models.integration import Integration

    return (
        await db.execute(
            select(Integration.user_id).where(Integration.id == integration_id)
        )
    ).scalar_one_or_none()


def register_a2a_grants() -> None:
    """Register the partner grant with the authorization server.

    Idempotent — safe to call twice from startup.

    Registered unconditionally at startup (see ``app.main``); the function
    does exactly what its name says so it stays directly testable.

    Not gated: the task surface, where a token is spent, refuses a deployment
    without chat with a stated reason (``require_chat_enabled``), where this
    grant could only answer ``invalid_client``. A token minted for such a
    deployment buys nothing.
    """
    from app.api.v1.mcp_oauth import register_grant_handler

    register_grant_handler(
        "client_credentials",
        handle_client_credentials,
        token_type=A2A_TOKEN_TYPE,
    )
