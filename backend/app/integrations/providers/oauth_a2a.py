# SPDX-License-Identifier: Apache-2.0

"""OAuth A2A provider bootstrap — credential minting and connectivity.

An ``oauth_a2a`` integration IS a partner credential: an operator generates
its OAuth client-credentials registration from the Config tab
(:func:`mint_credential`; nothing mints on create), and binding it to an
agent exposes that agent to whoever holds the secret. The provider contributes no BOUND
tools, so it is not in ``PROVIDER_TOOLS``; the one tool an inbound binding
brings — ``ask_the_caller`` — is registered by the external-task executor for
the length of a partner's task rather than by the binding. Importing this
module (see ``api.v1.integrations``) registers the seams below.

The secret is returned in clear exactly once — from the explicit generate /
regenerate action — and stored only as a bcrypt hash. That is not an
inconvenience to route around: re-display would mean keeping a recoverable
secret for a credential whose whole purpose is to authenticate an external
party.

Scope, and the trade-off it carries: ONE registration per integration,
valid on every agent the integration is bound to. A partner integrating
with three agents holds one secret rather than three — which is what makes
credentials manageable now that exposure follows bindings — but a leaked
secret reaches all three, and regenerating re-keys all three. Narrower
blast radius is available by creating a separate integration per agent.
"""

from __future__ import annotations

import logging
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.a2a.client.probe import outbound_probe as _outbound_probe
from app.a2a.providers import OAUTH_A2A_PROVIDER, agents_binding
from app.integrations.connectivity import (
    ProbeContext,
    ProbeResult,
    register_connectivity,
)
from app.models.integration import Integration

logger = logging.getLogger(__name__)

PROVIDER = OAUTH_A2A_PROVIDER

#: Where the minted, non-secret half is kept for display. The secret never
#: lands here — only its bcrypt hash, on the OAuthClient row.
CLIENT_ID_KEY = "client_id"


#: How many retired client ids an inbound integration remembers, so a partner
#: still presenting one is told apart from an id nobody issued (#105).
RETIRED_CLIENT_IDS_KEPT = 5


def _new_client_id() -> str:
    return f"a2a-{secrets.token_urlsafe(16)}"


async def mint_credential(
    db: AsyncSession, integration: Integration, *, rotate_client_id: bool = False
) -> tuple[str, str]:
    """Generate, or rotate, this integration's credential. Returns ``(id, secret)``.

    **A regenerate rotates the SECRET only** (#105): the client id the partner
    already holds stays, and the bcrypt hash on the same ``OAuthClient`` row is
    replaced. The old secret stops working at once, and a bearer minted with
    it before the rotation is refused by the task surface (``secret_rotated_at``
    against the token's ``iat``). Re-minting the client id on every regenerate,
    as this did before, left partners pasting the new secret next to the old
    client id: the pair never matched and every token request was refused.

    ``rotate_client_id`` is the explicit "Rotate client id and secret" action:
    the previous row is deleted and a new client id minted, the retired id
    remembered on the settings row so a refused request can name it.

    The first Generate (no row yet) mints both, as always. Still ONE
    registration per integration: a stray second row from before is deleted
    rather than left usable.

    **Nothing mints on create, deliberately.** Create returns the
    integration, not the credential, so a secret minted then could never be
    shown. Minting is operator-driven only, from the Config tab, where the
    secret is shown at the moment it is asked for.
    """
    import json
    from datetime import UTC, datetime

    from app.core.security import bcrypt_hashpw_async, encrypt_value
    from app.models.oauth_client import OAuthClient
    from app.services.vault import reveal_json_field

    existing = (
        (
            await db.execute(
                select(OAuthClient)
                .where(OAuthClient.a2a_integration_id == integration.id)
                .order_by(OAuthClient.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    client_secret = secrets.token_urlsafe(32)
    secret_hash = (await bcrypt_hashpw_async(client_secret.encode())).decode()

    keep = existing[0] if existing and not rotate_client_id else None
    retired = [row.client_id for row in existing if row is not keep]
    for row in existing:
        if row is not keep:
            await db.delete(row)

    if keep is not None:
        client_id = keep.client_id
        keep.client_secret_hash = secret_hash
        keep.secret_rotated_at = datetime.now(UTC)
    else:
        client_id = _new_client_id()
        db.add(
            OAuthClient(
                client_id=client_id,
                client_secret_hash=secret_hash,
                client_name=f"A2A partner — {integration.name}",
                redirect_uris=[],
                # Only this grant. An A2A partner credential must never be usable
                # in the authorization-code flow, which would put a *user* behind
                # it.
                grant_types=["client_credentials"],
                response_types=[],
                # S106 false positive: RFC 7591's *name* for the client
                # authentication method, not a credential.
                token_endpoint_auth_method="client_secret_post",  # noqa: S106
                a2a_integration_id=integration.id,
            )
        )
    if retired:
        await _remember_retired_client_ids(db, integration, retired)

    # The client id is an identifier the partner sends in clear on every
    # exchange, so it re-displays freely alongside the pins-style config.
    try:
        extra = await reveal_json_field(integration, "extra_config_enc") or {}
    except Exception:
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    extra[CLIENT_ID_KEY] = client_id
    integration.extra_config_enc = encrypt_value(json.dumps(extra))
    integration.status = "connected"

    await db.flush()
    return client_id, client_secret


async def _remember_retired_client_ids(db: AsyncSession, integration: Integration, retired: list[str]) -> None:
    """Keep the last few retired client ids on the inbound settings row."""
    from app.a2a.providers import get_settings

    row = await get_settings(db, integration.id)
    if row is None:
        return
    known = [cid for cid in (row.retired_client_ids or []) if cid not in retired]
    row.retired_client_ids = (known + retired)[-RETIRED_CLIENT_IDS_KEPT:]


async def check_connectivity(ctx: ProbeContext) -> ProbeResult:
    """What "connected" can honestly mean for a credential nobody has used yet.

    Verifiable: the feature is on, a credential exists, and the integration
    reaches at least one agent. NOT verifiable: whether the partner holds the
    secret — only their first exchange proves that.

    An OUTBOUND registration is a different question entirely — there, the
    platform HAS used the credential, and the answer is the stored health
    verdict rather than anything this probe can re-derive. It is delegated
    below rather than reimplemented, so the probe and the verdict cannot
    disagree about the same registration (FR-020).
    """
    from app.api.v1.platform import CHAT_UNAVAILABLE_DETAIL, is_chat_enabled
    from app.models.oauth_client import OAuthClient

    outbound = await _outbound_probe(ctx)
    if outbound is not None:
        return outbound

    # A2A is offered exactly while agentic chat is.
    if ctx.db is not None and not await is_chat_enabled(ctx.db):
        return ProbeResult.failed(CHAT_UNAVAILABLE_DETAIL)
    if ctx.db is None or ctx.integration is None:
        return ProbeResult.degraded(
            "No integration context — cannot check the credential."
        )

    client_id = (
        await ctx.db.execute(
            select(OAuthClient.client_id).where(
                OAuthClient.a2a_integration_id == ctx.integration.id
            )
        )
    ).scalar_one_or_none()
    if client_id is None:
        return ProbeResult.failed(
            "No credential has been generated yet — generate one from the "
            "integration's Config tab so the partner has something to "
            "authenticate with."
        )

    bound = await agents_binding(ctx.db, ctx.integration.id, PROVIDER)
    if not bound:
        return ProbeResult.degraded(
            f"Credential {client_id} is ready, but no agent binds this "
            "integration yet — bind it in an agent's Tools tab to expose "
            "that agent."
        )
    return ProbeResult.ok(
        f"Credential {client_id} is ready and exposes "
        f"{len(bound)} agent{'' if len(bound) == 1 else 's'}. Whether the "
        "partner holds the matching secret is proven only by its first "
        "token exchange."
    )


register_connectivity(PROVIDER, check_connectivity)
