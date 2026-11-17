# SPDX-License-Identifier: Apache-2.0

"""Reach an OpenCTI, OpenAEV or OpenCRQ integration as one person.

The one resolver behind what a dashboard reads from a platform (live widget
data, previews, publishing): given a person and an integration id, it returns
where to send requests and with which credentials, or refuses. The rules:

* the integration is one the person sees (:func:`user_visible_filter`, as a
  non-admin: an administrator does not borrow a colleague's private
  integration either - an admin who wants to read through one shares it);
* its provider is the one asked for;
* its stored credential may be used by this person
  (``oauth_credentials_usable``: a ``per_user`` integration lends nobody its
  key);
* a platform-managed integration authenticates with a short-lived token minted
  for this person, so the platform answers with their own rights; an API-key
  integration answers with the key's rights, which the person was given by
  being shown the integration;
* requests follow the integration's egress route (relay).

Every refusal is a :class:`PlatformAccessError` with a fixed code; the reason
goes to the logs only.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.egress import EgressRoute
from app.models.integration import Integration

logger = logging.getLogger(__name__)

PLATFORM_PROVIDERS: tuple[str, ...] = ("opencti", "openaev", "opencrq")
#: What the resolver reaches: the platforms, and the XTM Hub a dashboard is shared on.
RESOLVABLE_PROVIDERS: tuple[str, ...] = (*PLATFORM_PROVIDERS, "xtm_hub")


class PlatformAccessError(Exception):
    """The integration cannot be used by this person; ``code`` is safe to show."""

    def __init__(self, code: str = "integration_unavailable") -> None:
        super().__init__(code)
        self.code = code


@dataclass(slots=True)
class PlatformClient:
    """Where and how one person reaches one platform."""

    integration_id: uuid.UUID
    provider: str
    name: str
    base_url: str
    headers: dict[str, str] = field(repr=False)
    route: EgressRoute | None = None
    #: The tenant the integration names (OpenAEV / OpenCRQ), ``""`` when unset.
    tenant_id: str = ""
    #: The credential itself, for a client that sends it its own way (the XTM Hub's API token).
    credential: str = field(default="", repr=False)


async def visible_platform_integrations(
    db: AsyncSession,
    user_id: uuid.UUID,
    provider: str | None = None,
    *,
    providers: tuple[str, ...] = PLATFORM_PROVIDERS,
) -> list[Integration]:
    """The integrations of *providers* (the platforms by default) *user_id* may read dashboards through."""
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.services.integration_credentials import oauth_credentials_usable

    providers = (provider,) if provider else providers
    rows = (
        await db.execute(
            select(Integration)
            .where(
                Integration.provider.in_(providers),
                user_visible_filter(Integration, user_id, await get_user_group_ids(db, user_id), is_admin=False),
            )
            .order_by(Integration.name)
        )
    ).scalars()
    return [row for row in rows if oauth_credentials_usable(row, user_id)]


async def resolve_platform_client(
    db: AsyncSession, user_id: uuid.UUID, integration_id: uuid.UUID | str, provider: str
) -> PlatformClient:
    """The client *user_id* reaches *integration_id* with, or :class:`PlatformAccessError`."""
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.integrations.http import parse_compound_token
    from app.services.integration_credentials import oauth_credentials_usable
    from app.services.integration_tools import _get_token, _reveal_extra
    from app.services.relays import integration_egress_route

    if provider not in RESOLVABLE_PROVIDERS:
        raise PlatformAccessError()
    try:
        iid = integration_id if isinstance(integration_id, uuid.UUID) else uuid.UUID(str(integration_id))
    except ValueError:
        raise PlatformAccessError() from None
    integ = (
        await db.execute(
            select(Integration).where(
                Integration.id == iid,
                user_visible_filter(Integration, user_id, await get_user_group_ids(db, user_id), is_admin=False),
            )
        )
    ).scalar_one_or_none()
    if integ is None or integ.provider != provider:
        logger.info("Dashboard platform access refused: integration %s not visible as %s", iid, provider)
        raise PlatformAccessError()
    if not oauth_credentials_usable(integ, user_id):
        logger.info("Dashboard platform access refused: credentials of %s not usable by %s", iid, user_id)
        raise PlatformAccessError()
    try:
        token = await _get_token(db, str(iid), provider, user_id=str(user_id), integ=integ)
    except Exception as exc:
        logger.warning("Dashboard platform access: no token for %s: %r", iid, str(exc)[:200])
        token = None
    if not token:
        raise PlatformAccessError()
    base_url, api_token = parse_compound_token(token, 2, greedy_last=True)
    base_url, api_token = base_url.strip().rstrip("/"), api_token.strip()
    if not base_url.startswith(("https://", "http://")) or not api_token:
        logger.warning("Dashboard platform access: integration %s has no usable base URL or token", iid)
        raise PlatformAccessError()
    tenant_id = ""
    try:
        tenant_id = str((await _reveal_extra(integ)).get("tenant_id") or "").strip()
    except Exception:
        tenant_id = ""
    headers = {"Authorization": f"Bearer {api_token}"}
    if provider == "opencti":
        # Without a draft header OpenCTI works in the account's own UI draft:
        # name the live graph, as every request of the OpenCTI provider does.
        from app.integrations.providers.opencti._common import _request_headers

        headers = _request_headers(headers, None)
    return PlatformClient(
        integration_id=iid,
        provider=provider,
        name=integ.name,
        base_url=base_url,
        headers=headers,
        route=await integration_egress_route(db, integ),
        tenant_id=tenant_id[:100],
        credential=api_token,
    )


__all__ = [
    "PLATFORM_PROVIDERS",
    "RESOLVABLE_PROVIDERS",
    "PlatformAccessError",
    "PlatformClient",
    "resolve_platform_client",
    "visible_platform_integrations",
]
