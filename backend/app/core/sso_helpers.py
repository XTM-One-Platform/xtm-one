# SPDX-License-Identifier: Apache-2.0

"""Shared SSO configuration helpers.

Provides a single function to load the canonical SSO config from the
database.  All authentication configuration is managed via the Settings
UI — there are no environment-variable fallbacks.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.email_domains import parse_allowed_domains
from app.core.security import SecretReferenceError, decrypt_value
from app.models.sso_config import SSOConfig

logger = logging.getLogger(__name__)


def _oidc_secret(ciphertext: str | None, label: str) -> str | None:
    """A stored OIDC credential (``""`` when none), or ``None`` when it is a vault reference, which this field does not read.

    Every login (local included) reads this configuration first: a value the
    field cannot use must cost the OIDC sign-in only, never the login page.
    """
    if not ciphertext:
        return ""
    try:
        return decrypt_value(ciphertext)
    except SecretReferenceError:
        logger.warning("The stored OIDC %s is a vault reference, which this field does not read: OIDC sign-in is off until it is replaced", label)
        return None


#: How long the login configuration waits for the OIDC client secret's vault: every
#: login reads it, a public page, so a vault that does not answer turns OIDC off quickly
#: instead of holding the login page for the vault's own request timeout.
OIDC_SECRET_READ_SECONDS = 3.0


async def _oidc_client_secret(row: SSOConfig) -> str | None:
    """The OIDC client secret, a vault reference resolved; ``None`` when its vault cannot hand it over.

    Like :func:`_oidc_secret`, a secret that cannot be read costs the OIDC
    sign-in only, never the login page; nor may a slow vault keep the page
    waiting longer than :data:`OIDC_SECRET_READ_SECONDS`. The read itself
    goes on in the resolver's own task, and its result is cached for the next
    login like any read (the provider's cache duration, a failure 15 s).
    """
    if not row.oidc_client_secret_enc:
        return ""
    from app.services.vault import SecretUnavailableError, reveal_field

    try:
        return await asyncio.wait_for(reveal_field(row, "oidc_client_secret_enc"), OIDC_SECRET_READ_SECONDS) or ""
    except SecretUnavailableError:
        logger.warning("The OIDC client secret could not be read from its vault: OIDC sign-in is off until it can be")
        return None
    except TimeoutError:
        logger.warning(
            "The OIDC client secret's vault did not answer within %.0f s: OIDC sign-in is off until it does",
            OIDC_SECRET_READ_SECONDS,
        )
        return None


@dataclass
class ResolvedSSOConfig:
    """SSO configuration loaded from the database."""

    # OIDC
    oidc_enabled: bool = False
    oidc_display_name: str = "Microsoft"
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_discovery_url: str = ""
    oidc_authorization_url: str = ""
    oidc_token_url: str = ""
    oidc_userinfo_url: str = ""
    oidc_scopes: str = "openid email profile"

    # SAML
    saml_enabled: bool = False
    saml_display_name: str = "Corporate SSO"
    saml_idp_metadata_url: str = ""
    saml_sp_entity_id: str = ""
    saml_requested_authn_context: bool = False
    saml_groups_mapping_enabled: bool = True
    saml_groups_auto_create: bool = True

    # Auto-admin at every SSO sign-in, existing accounts included (despite
    # the "new users" in the names)
    oidc_auto_admin_new_users: bool = False
    saml_auto_admin_new_users: bool = False

    # On-the-fly user creation on first SSO login (default on — historical
    # behavior; disabling restricts SSO to pre-provisioned accounts)
    oidc_auto_create_users: bool = True
    saml_auto_create_users: bool = True

    # General auth
    local_auth_enabled: bool = True
    registration_enabled: bool = True
    # Restricts self-registration and SSO account creation (``email_domains``)
    allowed_domains: str = ""

    @property
    def allowed_domain_list(self) -> list[str]:
        return parse_allowed_domains(self.allowed_domains)


async def get_sso_config(db: AsyncSession) -> ResolvedSSOConfig:
    """Load SSO config from DB.  Returns sensible defaults when no row exists."""
    result = await db.execute(select(SSOConfig).limit(1))
    row = result.scalar_one_or_none()

    if row:
        oidc_client_id = _oidc_secret(row.oidc_client_id_enc, "client id")
        oidc_client_secret = await _oidc_client_secret(row)
        return ResolvedSSOConfig(
            # A credential it cannot read turns OIDC off: no sign-in starts with an empty one.
            oidc_enabled=row.oidc_enabled and oidc_client_id is not None and oidc_client_secret is not None,
            oidc_display_name=row.oidc_display_name or "Microsoft",
            oidc_client_id=oidc_client_id or "",
            oidc_client_secret=oidc_client_secret or "",
            oidc_discovery_url=row.oidc_discovery_url or "",
            oidc_authorization_url=row.oidc_authorization_url or "",
            oidc_token_url=row.oidc_token_url or "",
            oidc_userinfo_url=row.oidc_userinfo_url or "",
            oidc_scopes=row.oidc_scopes or "openid email profile",
            saml_enabled=row.saml_enabled,
            saml_display_name=row.saml_display_name or "Corporate SSO",
            saml_idp_metadata_url=row.saml_idp_metadata_url or "",
            saml_sp_entity_id=row.saml_sp_entity_id or "",
            saml_requested_authn_context=row.saml_requested_authn_context,
            saml_groups_mapping_enabled=row.saml_groups_mapping_enabled,
            saml_groups_auto_create=row.saml_groups_auto_create,
            oidc_auto_admin_new_users=row.oidc_auto_admin_new_users,
            saml_auto_admin_new_users=row.saml_auto_admin_new_users,
            oidc_auto_create_users=row.oidc_auto_create_users,
            saml_auto_create_users=row.saml_auto_create_users,
            local_auth_enabled=row.local_auth_enabled,
            registration_enabled=row.registration_enabled,
            allowed_domains=row.allowed_domains or "",
        )

    # No DB config yet (fresh install): local auth enabled, SSO disabled,
    # no domain restrictions.  Registration defaults off in xtm_one mode
    # (admin-only user management) and on in copilot mode (self-service).
    from app.core.config import settings

    return ResolvedSSOConfig(
        registration_enabled=settings.is_copilot_like,
    )
