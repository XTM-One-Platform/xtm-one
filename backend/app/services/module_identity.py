# SPDX-License-Identifier: Apache-2.0

"""CTEM and organization-context module calls act as a person, never as a
service account.

OpenCTI, OpenAEV and OpenCRQ judge every call by the identity that makes it,
and record it under that identity in their audit logs. The CTEM posture build
(dashboard load, its background refresh, the per-user stage fan-outs) and the
organization-context syncs therefore authenticate as the person they act for:

* a ``platform_managed`` integration mints that person's own short-lived JWT
  (``integration_tools._get_token``);
* a person's own credential (their ``per_user`` connection) is theirs;
* an integration whose stored credential that person holds (they connected
  it) is theirs.

A stored credential held by nobody, or by someone else (a company-managed
static API key, an integration shared by another person) is a service
account for that person: on a REGISTERED deployment (a ``platform_managed``
row exists for the provider) the organization-context syncs never use it.
An unregistered deployment, whose only way in is that static key, syncs as
before. No stored credential is ever deleted or ignored by the shared token
resolver.

Scope (Sam, 13 Nov 2026): the rule covers the CTEM and organization-context
code paths only. Agents, assignments, custom dashboards, event pollers and
trigger provisioning bound to a company-managed static key behave as before.
"""

from __future__ import annotations

from typing import Any

from app.services.integration_credentials import normalize_acting_user_id
from app.services.integration_user_credentials import (
    credential_holder_id,
    is_user_credential,
)

#: The Filigran modules whose calls must carry the acting person's identity.
MODULE_PROVIDERS: frozenset[str] = frozenset({"opencti", "openaev", "opencrq"})


def acts_as(holder: Any, acting_user_id: Any) -> bool:
    """Whether the credential *holder* authenticates as *acting_user_id* itself."""
    acting = normalize_acting_user_id(acting_user_id)
    if acting is None:
        return False
    if is_user_credential(holder):
        return normalize_acting_user_id(getattr(holder, "user_id", None)) == acting
    if getattr(holder, "auth_type", None) == "platform_managed":
        return True  # the JWT is minted for the acting user at call time
    return credential_holder_id(holder) == acting


async def platform_is_registered(db: Any, provider: str) -> bool:
    """Whether a registered (``platform_managed``) integration exists for *provider*."""
    from sqlalchemy import select

    from app.models.integration import Integration

    row = await db.execute(
        select(Integration.id)
        .where(Integration.provider == provider, Integration.auth_type == "platform_managed")
        .limit(1)
    )
    return row.first() is not None


def refused_for_unattended(provider: str, holder: Any, acting_user_id: Any) -> bool:
    """Whether a CTEM or organization-context call must not use *holder* for *provider*."""
    return provider in MODULE_PROVIDERS and not acts_as(holder, acting_user_id)


def service_account_refusal(
    provider: str, integration: Any, acting_user_id: Any, *, registered: bool
) -> str | None:
    """The reason an organization-context sync must not read *integration*'s credential for
    *acting_user_id*, or ``None`` when the call acts as that person or the deployment is
    not *registered* (its static key is then the only way in, used as before)."""
    if not registered or not refused_for_unattended(provider, integration, acting_user_id):
        return None
    return (
        f"the {provider} integration holds a credential that is not the acting "
        "user's own (a service account): connect the platform through its "
        "registration, so each call carries the acting user's identity"
    )
