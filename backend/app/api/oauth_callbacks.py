# SPDX-License-Identifier: Apache-2.0

"""OAuth callback handler – receives redirects from external providers.

Uses per-entity credentials stored in the DB (not env vars).
The state parameter encodes which entity to update (integration or MCP server),
so we load the correct row and use its client_id/client_secret for token exchange.

Also handles GitHub App installation callbacks, which arrive without a state
parameter (GitHub redirects with ``code``, ``installation_id``, and
``setup_action=install`` after a user installs the app on an org/account).
"""

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.api.v1.integration_catalog import (
    PROVIDER_CATALOG,
    integration_owns_oauth_scopes,
)
from app.core.config import settings
from app.core.database import get_db, release_db_connection_before_loop
from app.core.egress import egress_client, egress_route
from app.core.rate_limit import enforce_rate_limit
from app.core.security import decrypt_json, decrypt_value, encrypt_value
from app.core.utils import format_exception_message
from app.integrations.granted_scopes import missing_scopes, record_granted_scopes, split_scopes
from app.integrations.oauth import (
    apply_mcp_token_expiry,
    apply_oauth_token_expiry,
    exchange_code_for_tokens_with_integration,
    exchange_code_for_tokens_with_mcp_server,
    get_state_data,
)
from app.integrations.providers.github import GITHUB_API
from app.integrations.service_token_exchange import oauth_exchange_spec, redeem_code
from app.models.integration import Integration
from app.models.mcp_server import MCPServer
from app.models.oauth_default import OAuthDefault
from app.models.user import User
from app.services.audit import log_audit, log_system_audit
from app.services.integration_credentials import (
    is_shared_integration,
    normalize_acting_user_id,
)
from app.services.integration_state import INTEGRATION_CONNECTED
from app.services.integration_user_credentials import (
    USER_CREDENTIAL_STATE,
    account_extra,
    holder_ref,
    is_per_user,
    is_user_credential,
    may_connect_own_account,
    save_account_extra,
    store_tokens,
    upsert_user_credential,
)
from app.services.relays import integration_egress_route, mcp_server_egress_route
from app.services.vault import SecretUnavailableError, reveal_field

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_extra(integration: Any) -> dict:
    """Decrypt a token holder's extra_config_enc or return an empty dict.

    A person's own credential reads its integration's, with what their
    sign-in learned about their account over it.
    """
    if is_user_credential(integration):
        return account_extra(integration)
    if not integration.extra_config_enc:
        return {}
    try:
        return decrypt_json(integration.extra_config_enc) or {}
    except Exception:
        return {}


def _save_extra(integration: Any, extra: dict) -> None:
    """Encrypt and persist an extra-config dict on a token holder (on a person's
    credential, only the account keys: ``integration_user_credentials.save_account_extra``)."""
    if is_user_credential(integration):
        save_account_extra(integration, extra)
        return
    integration.extra_config_enc = encrypt_value(json.dumps(extra))


async def _record_credential_owner(
    db: AsyncSession, integration: Integration, authorizing_user_id: str | None
) -> bool:
    """Make the integration's owner the user whose token is about to be stored.

    The personal OAuth token rule (``integration_credentials``) trusts
    ``user_id`` as the token's owner. Only a company-managed integration can be
    authorized by someone else than its owner (any admin may re-authorize it):
    the credential then moves to that admin. The previous owner's share consent
    is dropped - it covered THEIR token, not this one - and
    ``acknowledge_share_on_authorize`` records the admin's own once the token
    is stored, since authorizing a shared integration is that choice. Audited.
    Returns whether the owner changed.
    """
    if not authorizing_user_id or not integration.is_company_managed:
        return False
    new_owner = await db.get(User, uuid.UUID(str(authorizing_user_id)))
    if new_owner is None or integration.user_id == new_owner.id:
        return False
    previous_owner_id = integration.user_id
    consent_reset = bool(integration.oauth_share_acknowledged)
    if consent_reset:
        from app.api.v1.integrations import _forget_oauth_share_dismissal

        await _forget_oauth_share_dismissal(db, integration, actor=new_owner)
    integration.user_id = new_owner.id
    integration.oauth_share_acknowledged = False
    await log_audit(
        db,
        user=new_owner,
        action="update",
        entity_type="integration",
        entity_id=integration.id,
        entity_name=integration.name,
        summary=(
            "Re-authorized a company-managed OAuth integration: its credential "
            "owner changed; sharing consent is now the new owner's"
        ),
        details={
            "previous_owner_id": str(previous_owner_id) if previous_owner_id else None,
            "new_owner_id": str(new_owner.id),
            "previous_owner_consent_dropped": consent_reset,
        },
    )
    logger.warning(
        "Company-managed integration %s re-authorized by %s: credential owner %s -> %s, "
        "previous owner's share consent dropped (was %s)",
        integration.id,
        new_owner.id,
        previous_owner_id,
        new_owner.id,
        consent_reset,
    )
    return True


async def store_authorized_tokens(
    db: AsyncSession,
    integration: Integration,
    authorizing_user_id: str | None,
    *,
    access_token: str,
    refresh_token: str | None,
) -> None:
    """Store the tokens a user's own sign-in produced, as that user's credential.

    Shared by the OAuth callback and the sign-ins that do not end on it (an
    OpenAPI integration's device code and implicit grant), so every one of them
    records the same owner.
    """
    # Whose credential the row holds until now: whoever connected a token an
    # owner transfer kept, else the owner. Nobody recorded (an
    # owner-less company-managed row) says nothing, and keeps main's behaviour.
    previous_holder = normalize_acting_user_id(
        integration.oauth_token_owner_id
    ) or normalize_acting_user_id(integration.user_id)
    held_by_someone_else = previous_holder not in (
        None,
        normalize_acting_user_id(authorizing_user_id),
    )
    await _record_credential_owner(db, integration, authorizing_user_id)
    # The authorizer is the owner now, so the token is the owner's own.
    integration.oauth_token_owner_id = None
    integration.access_token_enc = encrypt_value(access_token)
    if refresh_token:
        integration.refresh_token_enc = encrypt_value(refresh_token)
    elif held_by_someone_else:
        # A provider may omit the refresh token on re-consent, and the stored one
        # then belongs to whoever held the credential before: refreshing it would
        # bring their access back under the new owner's identity.
        integration.refresh_token_enc = None


async def mark_authorized(
    db: AsyncSession, integration: Integration, authorizing_user_id: str | None
) -> None:
    """Record the sign-in's share consent and the connected status (before the commit)."""
    await acknowledge_share_on_authorize(db, integration, authorizing_user_id)
    integration.status = "connected"
    # Written even when it already reads "connected": a refresh may have
    # recorded a verdict about the replaced tokens since the row was read.
    flag_modified(integration, "status")


async def _handle_integration_callback(
    state_data: dict,
    code: str,
    db: AsyncSession,
) -> RedirectResponse:
    """Handle OAuth callback for integration entities."""
    integration_id = state_data.get("integration_id")
    user_id = state_data.get("user_id")

    if not integration_id:
        raise HTTPException(status_code=400, detail="Missing integration_id in state")

    # A person connecting their own account on an integration whose people
    # each connect theirs: the integration is theirs to see, never
    # theirs to write; the tokens land on their own credential.
    if state_data.get("credential") == USER_CREDENTIAL_STATE:
        integration = await _own_account_integration(db, integration_id, user_id)
        with egress_route(await integration_egress_route(db, integration)):
            return await _complete_integration_callback(integration, state_data, code, db)

    result = await db.execute(
        select(Integration).where(
            Integration.id == uuid.UUID(integration_id),
            Integration.user_id == uuid.UUID(user_id),
        )
    )
    integration = result.scalar_one_or_none()
    if not integration:
        # Fallback: for company-managed integrations user_id is NULL
        result = await db.execute(
            select(Integration).where(
                Integration.id == uuid.UUID(integration_id),
                # access_restricted: bypass - the row named in a server-issued authorize state.
                Integration.is_company_managed.is_(True),
            )
        )
        integration = result.scalar_one_or_none()
    if not integration:
        raise HTTPException(status_code=404, detail="Integration not found")

    # The code exchange and every lookup that follows reach the provider from
    # the integration's own network when it is routed through a relay.
    with egress_route(await integration_egress_route(db, integration)):
        return await _complete_integration_callback(integration, state_data, code, db)


async def _own_account_integration(db: AsyncSession, integration_id: str, user_id: str | None) -> Integration:
    """The integration a person's own-account sign-in names, when they may still
    connect their own account on it (``may_connect_own_account``, as at
    ``/oauth/start``): its holder or its sharing may have changed since."""
    iid, uid = normalize_acting_user_id(integration_id), normalize_acting_user_id(user_id)
    integration = await db.get(Integration, iid) if iid is not None else None
    user = await db.get(User, uid) if uid is not None else None
    if integration is None or user is None or not await may_connect_own_account(db, user, integration):
        raise HTTPException(status_code=404, detail="Integration not found")
    return integration


async def _complete_integration_callback(
    integration: Integration,
    state_data: dict,
    code: str,
    db: AsyncSession,
) -> RedirectResponse:
    integration_id = state_data.get("integration_id")
    user_id = state_data.get("user_id")
    provider = state_data.get("provider", "")
    # The sign-in of a person's own account: its tokens and what it
    # learns about the account go to their credential, and the integration's
    # configuration is left as it is.
    own_account = state_data.get("credential") == USER_CREDENTIAL_STATE

    logger.warning(
        "OAuth callback for provider=%s integration=%s",
        provider,
        integration_id,
    )

    # For X/Twitter OAuth 2.0 with PKCE, retrieve the code_verifier from state data
    code_verifier: str | None = state_data.get("code_verifier")

    try:
        # A bundle whose service redeems the code itself (``oauth_exchange``,
        # SaaS Manager) gets it handed over instead of exchanged at token_url.
        # Without its code_verifier: no PKCE is generated for custom providers.
        service_exchange = await oauth_exchange_spec(db, integration.provider)
        if service_exchange is not None:
            tokens = await redeem_code(
                service_exchange,
                integration,
                code,
                redirect_uri=f"{settings.base_url}/api/oauth/callback",
            )
        else:
            tokens = await exchange_code_for_tokens_with_integration(
                integration, code, code_verifier=code_verifier
            )
    except Exception as exc:
        error_msg = str(exc)
        logger.warning(
            "OAuth token exchange failed for %s integration %s: %s",
            provider,
            integration_id,
            error_msg,
        )
        return _integration_page_redirect(integration, error="token_exchange_failed", provider=provider)

    # Slack user tokens have a different response structure
    if provider == "slack" and "authed_user" in tokens:
        user_token = tokens["authed_user"]
        access_token = user_token.get("access_token", "")
        refresh_token = user_token.get("refresh_token")
    else:
        access_token = tokens.get("access_token", "")
        refresh_token = tokens.get("refresh_token")

    if not access_token:
        raise HTTPException(
            status_code=400, detail="No access token in provider response"
        )

    target: Any
    if own_account:
        installations_seen = None
        target = await upsert_user_credential(db, integration, user_id)
        store_tokens(target, access_token=access_token, refresh_token=refresh_token)
    else:
        installations_seen = await _installations_seen_on_connect(db, integration, provider, access_token)
        await store_authorized_tokens(
            db, integration, user_id, access_token=access_token, refresh_token=refresh_token
        )
        target = integration

    # Force-sync scopes to the catalog definition (single source of truth).
    # The authorize URL already requested the catalog scopes; here we persist
    # them so the instance is fully aligned regardless of what the provider
    # echoes back (some providers omit meta-scopes like offline_access).
    # For GitHub Apps, catalog scopes are intentionally empty — clear any
    # stale scopes from older authorizations so the token is a proper App
    # user-to-server token (which shows "with [App Name]" on GitHub).
    # LinkedIn: resolve scopes based on account_type (personal vs page).
    # Imported providers and OpenAPI integrations own their scopes (from the
    # imported spec, stored on the integration) — there is no catalog value
    # to sync to, and clearing them would break the next authorization.
    catalog = PROVIDER_CATALOG.get(provider, {})
    if not own_account and not integration_owns_oauth_scopes(provider):
        if provider == "linkedin":
            extra = _load_extra(integration)
            account_type = extra.get("account_type", "personal")
            if account_type == "page":
                catalog_scopes = catalog.get("scopes_page", catalog.get("scopes", ""))
            else:
                catalog_scopes = catalog.get("scopes_personal", catalog.get("scopes", ""))
        else:
            catalog_scopes = catalog.get("scopes", "")
        integration.scopes = catalog_scopes or None

    apply_oauth_token_expiry(target, tokens)
    # What the person actually granted (a granular consent screen lets them
    # untick a scope); slack answers it on the user token for a user sign-in.
    granted_answer = tokens["authed_user"] if provider == "slack" and "authed_user" in tokens else tokens
    record_granted_scopes(target, granted_answer, keep_when_absent=False)

    # --- LinkedIn: fetch administered organizations for page mode --------
    if provider == "linkedin":
        try:
            extra = _load_extra(target)
            if extra.get("account_type") == "page":
                async with egress_client(timeout=15) as hc:
                    orgs_resp = await hc.get(
                        "https://api.linkedin.com/rest/organizationAcls",
                        params={
                            "q": "roleAssignee",
                            "role": "ADMINISTRATOR",
                            "count": "50",
                        },
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "LinkedIn-Version": "202402",
                            "X-Restli-Protocol-Version": "2.0.0",
                        },
                    )
                    if orgs_resp.is_success:
                        elements = orgs_resp.json().get("elements", [])
                        organizations = []
                        for e in elements:
                            org_urn = e.get("organization", "")
                            org_id = (
                                org_urn.split(":")[-1] if ":" in org_urn else org_urn
                            )
                            organizations.append(
                                {"id": org_id, "role": e.get("role", "")}
                            )
                        extra["organizations"] = organizations
                        if organizations:
                            extra["organization_id"] = organizations[0]["id"]
                        _save_extra(target, extra)
                    else:
                        logger.warning(
                            "Failed to fetch LinkedIn organizations (HTTP %d)",
                            orgs_resp.status_code,
                        )
        except Exception as exc:
            logger.warning("Failed to fetch LinkedIn organizations: %s", format_exception_message(exc))

    # --- Facebook: fetch managed pages for page mode --------------------
    if provider == "facebook":
        try:
            extra = _load_extra(target)
            if extra.get("account_type") == "page":
                async with egress_client(timeout=15) as hc:
                    pages_resp = await hc.get(
                        "https://graph.facebook.com/v21.0/me/accounts",
                        params={
                            "access_token": access_token,
                            "fields": "id,name,category,access_token",
                        },
                    )
                    if pages_resp.is_success:
                        pages = pages_resp.json().get("data", [])
                        extra["pages"] = [
                            {
                                "id": p.get("id"),
                                "name": p.get("name"),
                                "category": p.get("category"),
                            }
                            for p in pages
                        ]
                        if pages:
                            first_page = pages[0]
                            extra["page_id"] = first_page.get("id", "")
                            extra["page_name"] = first_page.get("name", "")
                            # Store the page access token (does not expire for long-lived tokens)
                            page_access_token = first_page.get("access_token", "")
                            if page_access_token:
                                extra["page_access_token"] = page_access_token
                        _save_extra(target, extra)
                    else:
                        logger.warning(
                            "Failed to fetch Facebook pages (HTTP %d)",
                            pages_resp.status_code,
                        )
        except Exception as exc:
            logger.warning("Failed to fetch Facebook pages: %s", format_exception_message(exc))

    # --- Atlassian Jira: fetch accessible resources (cloud_id) -----------
    if provider == "jira":
        try:
            async with egress_client(timeout=15) as hc:
                res_resp = await hc.get(
                    "https://api.atlassian.com/oauth/token/accessible-resources",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json",
                    },
                )
                if res_resp.is_success:
                    extra = _load_extra(target)
                    # Store all accessible sites; use the first as default
                    sites = [
                        {
                            "id": r["id"],
                            "name": r.get("name", ""),
                            "url": r.get("url", ""),
                        }
                        for r in res_resp.json()
                    ]
                    extra["sites"] = sites
                    if sites:
                        extra["cloud_id"] = sites[0]["id"]
                    _save_extra(target, extra)
                else:
                    logger.warning(
                        "Failed to fetch Jira accessible resources (HTTP %d)",
                        res_resp.status_code,
                    )
        except Exception as exc:
            logger.warning("Failed to fetch Jira accessible resources: %s", format_exception_message(exc))

    if installations_seen:
        _record_installations_seen_on_connect(integration, installations_seen)

    signed_in = await db.get(User, uuid.UUID(str(user_id))) if own_account else None
    if signed_in is not None:
        await log_audit(
            db,
            user=signed_in,
            action="update",
            entity_type="integration",
            entity_id=integration.id,
            entity_name=integration.name,
            summary=f"Connected their own account to '{integration.name}'",
            details={"credential": "own_account"},
        )
    await _audit_oauth_connect(db, integration, target, user_id, own_account=own_account)
    if not own_account:
        await mark_authorized(db, integration, user_id)
    await db.commit()

    from app.services.token_refresh import clear_refresh_cooldown, refresh_cooldown_key

    await clear_refresh_cooldown(refresh_cooldown_key(holder_ref(target)))
    if provider in ("defender", "sentinel", "purview"):
        # A new grant (often right after an admin consent): the per-audience
        # tokens redeemed from the old one lack its permissions (#2386).
        from app.services.integration_tools import forget_azure_delegated_tokens

        await forget_azure_delegated_tokens(holder_ref(target))

    return _integration_page_redirect(integration, connected=provider)


async def _audit_oauth_connect(
    db: AsyncSession,
    integration: Integration,
    target: Any,
    user_id: str | None,
    *,
    own_account: bool,
) -> None:
    """Audit an OAuth (re)connect with the scopes requested, granted and missing.

    A declined scope is the usual cause of a 403 right after a reconnect; the
    entry says whether the sign-in obtained what the integration asks for.
    """
    try:
        actor = await db.get(User, uuid.UUID(str(user_id)))
    except (TypeError, ValueError):
        actor = None
    if actor is None:
        return
    catalog = PROVIDER_CATALOG.get(integration.provider, {})
    requested = integration.scopes or catalog.get("scopes")
    granted = getattr(target, "granted_scopes", None)
    missing = missing_scopes(requested, granted, catalog.get("optional_scopes"))
    summary = f"Connected the OAuth account of '{integration.name}'"
    if missing:
        summary += f": {len(missing)} requested scope(s) not granted"
    await log_audit(
        db,
        user=actor,
        action="connect",
        entity_type="integration",
        entity_id=integration.id,
        entity_name=integration.name,
        summary=summary,
        details={
            "credential": "own_account" if own_account else "integration",
            "requested_scopes": split_scopes(requested),
            "granted_scopes": None if granted is None else split_scopes(granted),
            "missing_scopes": missing,
        },
    )


def _integration_page_redirect(integration: Integration, **query: str) -> RedirectResponse:
    """Back to the integration's own page, where its sign-in started.

    The path is this platform's page of the row the callback loaded - its id,
    never text from the state or the provider - and the query is encoded, so
    the browser cannot be sent anywhere else.
    """
    frontend_url = (settings.frontend_url or settings.base_url).rstrip("/")
    return RedirectResponse(url=f"{frontend_url}/integrations/{uuid.UUID(str(integration.id))}?{urlencode(query)}")


async def acknowledge_share_on_authorize(
    db: AsyncSession, integration: Integration, authorizer_id: str | None
) -> None:
    """Record consent when a shared OAuth integration is authorized by its owner.

    Connecting an integration that is already shared (group, per-user grant,
    company-managed) makes the new token work for everyone it is shared with,
    the owner - or an admin, for a company-managed one -
    chooses that by authorizing it, and the Connect button says so. Anyone
    else's authorization is left unacknowledged, and so is any authorization
    of an integration whose people each connect their own account:
    its owner chose that its account is never lent, and re-authorizing does
    not change that choice.
    """
    if (
        integration.auth_type != "oauth"
        or integration.oauth_share_acknowledged
        or is_per_user(integration)
    ):
        return
    if not is_shared_integration(integration):
        return
    try:
        authorizer = uuid.UUID(str(authorizer_id))
    except (TypeError, ValueError):
        return
    if integration.user_id == authorizer:
        integration.oauth_share_acknowledged = True
        return
    if integration.is_company_managed:
        user = await db.get(User, authorizer)
        if user is not None and user.role == "admin":
            integration.oauth_share_acknowledged = True


async def _handle_github_app_install(
    code: str,
    installation_id: str,
    db: AsyncSession,
    request: Request | None = None,
) -> RedirectResponse:
    """Handle GitHub App installation callback (no state parameter).

    When a user installs a GitHub App from the GitHub UI, GitHub redirects to
    the callback URL with ``code``, ``installation_id``, and
    ``setup_action=install`` — but **no** ``state`` parameter: nothing names
    the XTM One user who installed it, nor an integration. This handler used
    to write the installer's token and the installation onto whichever GitHub
    integration it found first, possibly another user's. It now
    exchanges the code only to learn, from GitHub, whose account installed
    and whether that account sees the installation, and records the
    installation on the integrations proven to be that account's
    (``_installer_integrations``). The installer's token is stored nowhere:
    each of them already holds a working token of the same account. A
    waiting integration is connected from its page, whose flow carries a
    ``state`` naming it (``_handle_integration_callback``).
    """

    if not _GITHUB_INSTALLATION_ID.fullmatch(installation_id):
        logger.warning("GitHub App install callback with a malformed installation id: nothing recorded")
        return RedirectResponse(
            url=f"{settings.frontend_url}/integrations?github_installed=true"
        )

    # Each accepted callback asks GitHub once per candidate integration. The
    # installation bucket is keyed on the id checked numeric just above.
    if request is not None:
        await enforce_rate_limit(
            request,
            "github-app-install",
            limit=_INSTALL_CALLBACKS_PER_CLIENT,
            window_seconds=_INSTALL_CALLBACK_WINDOW_SECS,
        )
    await enforce_rate_limit(
        None,
        "github-app-install-installation",
        limit=_INSTALL_CALLBACKS_PER_INSTALLATION,
        window_seconds=_INSTALL_CALLBACK_WINDOW_SECS,
        bucket=installation_id,
    )

    # 1. Find GitHub OAuth credentials ---------------------------------
    #    Prefer admin-configured OAuthDefault, then fall back to any
    #    GitHub integration that has client_id_enc set.
    client_id: str | None = None
    client_secret: str = ""

    default_result = await db.execute(
        select(OAuthDefault).where(OAuthDefault.provider == "github")
    )
    oauth_default = default_result.scalar_one_or_none()
    if oauth_default:
        client_id = decrypt_value(oauth_default.client_id_enc)
        # The secret may be a vault reference: resolved here, and an
        # unreadable vault ends the install like missing credentials would.
        try:
            client_secret = await reveal_field(oauth_default, "client_secret_enc") or ""
        except SecretUnavailableError as exc:
            logger.error(
                "GitHub App install callback: the GitHub OAuth default's client secret "
                "cannot be read from its vault (%s)",
                exc,
            )
            return RedirectResponse(
                url=f"{settings.frontend_url}/integrations?error=github_token_exchange_failed"
            )

    if not client_id:
        result = await db.execute(
            select(Integration)
            .where(
                Integration.provider == "github",
                Integration.client_id_enc.isnot(None),
            )
            .order_by(Integration.created_at.desc())
        )
        any_gh = result.scalars().first()
        if any_gh:
            client_id = decrypt_value(any_gh.client_id_enc)
            client_secret = await reveal_field(any_gh, "client_secret_enc") or ""

    if not client_id:
        logger.warning(
            "GitHub App install callback but no GitHub integration with OAuth credentials found"
        )
        return RedirectResponse(
            url=f"{settings.frontend_url}/integrations?error=no_github_credentials"
        )

    # Only GitHub is asked until the candidates are read: no pooled
    # connection is held across the exchange and the proof below.
    await release_db_connection_before_loop(db)

    # 2. Exchange the code for tokens -----------------------------------
    token_url = "https://github.com/login/oauth/access_token"  # noqa: S105
    # OAuth endpoint URL, not a credential.
    redirect_uri = f"{settings.base_url}/api/oauth/callback"

    try:
        async with egress_client(timeout=30) as client:
            resp = await client.post(
                token_url,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            tokens = resp.json()
    except Exception as exc:
        logger.error("Failed to exchange GitHub App install code: %s", format_exception_message(exc))
        return RedirectResponse(
            url=f"{settings.frontend_url}/integrations?error=github_token_exchange_failed"
        )

    access_token = tokens.get("access_token", "")
    if not access_token:
        error = tokens.get("error_description") or tokens.get("error", "unknown")
        logger.warning("GitHub App install: no access_token in response: %s", error)
        return RedirectResponse(
            url=f"{settings.frontend_url}/integrations?error=github_no_token"
        )

    # 3. What the installer's own token proves -----------------
    #    No state names the XTM One user or the integration: GitHub's answers
    #    about the exchanged token are the only proof. The installation must
    #    be one the installer sees - the query string alone proves nothing.
    #    One deadline bounds every proof call, whatever the pages.
    deadline = time.monotonic() + _INSTALL_PROOF_BUDGET_SECS
    installer_account = await _before(deadline, _github_account_id(access_token))
    install_entry = await _before(deadline, _installation_seen_by(access_token, installation_id))
    if installer_account is None or install_entry is None:
        logger.warning(
            "GitHub App installed (installation_id=%s) but the installer's token does not "
            "show %s: the installation is recorded on no integration",
            installation_id,
            "whose account it is" if installer_account is None else "this installation",
        )
        return RedirectResponse(
            url=f"{settings.frontend_url}/integrations?github_installed=true"
        )

    # 4. Record it on the installer's own integrations ------------------
    proven = await _installer_integrations(db, client_id, installer_account, deadline)
    targets, token_changed = await _record_on_proven(db, proven, client_id, install_entry)
    for target in targets:
        await _audit_recorded_installation(db, target, install_entry)
    if token_changed:
        targets += await _record_after_token_change(
            db, token_changed, client_id, installer_account, install_entry, deadline
        )
    if not targets:
        logger.info(
            "GitHub App installed (installation_id=%s): no integration of this app connected "
            "with the installer's GitHub account records installations; none updated",
            installation_id,
        )
        await db.rollback()
        return RedirectResponse(
            url=f"{settings.frontend_url}/integrations?github_installed=true"
        )
    await db.commit()

    logger.info(
        "GitHub App installed: integrations=%s installation_id=%s",
        [str(target.id) for target in targets],
        installation_id,
    )

    # Nothing says whose browser this is, and a row proven to be the
    # installer's GitHub account may be another XTM One user's: no id.
    return RedirectResponse(url=f"{settings.frontend_url}/integrations?connected=github")


# A state-less install proves ownership by comparing GitHub accounts.
# Bounded: a sluggish GitHub costs the redirect a moment, not a minute.
_INSTALL_PROOF_TIMEOUT_SECS = 10
_INSTALL_PROOF_BUDGET_SECS = 20.0
_INSTALL_PROOF_CONCURRENCY = 5
_INSTALL_PROOF_MAX_CANDIDATES = 50
_INSTALLATIONS_PER_PAGE = 100
_INSTALLATIONS_MAX_PAGES = 10
# One paged ``GET /user/installations`` while a Connect waits on its redirect.
_CONNECT_INSTALLATIONS_BUDGET_SECS = 20.0
# A legitimate install is one callback, a retried one a few.
_INSTALL_CALLBACK_WINDOW_SECS = 600
_INSTALL_CALLBACKS_PER_CLIENT = 30
_INSTALL_CALLBACKS_PER_INSTALLATION = 10
# GitHub installation ids are integers; anything else is a crafted callback.
_GITHUB_INSTALLATION_ID = re.compile(r"[0-9]{1,20}")


def _github_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


async def _before(deadline: float, call: Awaitable):
    """*call*'s answer, or None once *deadline* (``time.monotonic``) has passed."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        if asyncio.iscoroutine(call):
            call.close()
        return None
    try:
        return await asyncio.wait_for(call, timeout=remaining)
    except TimeoutError:
        logger.warning("GitHub App install: GitHub did not answer within the proof budget")
        return None


async def _github_account_id(token: str) -> str | None:
    """The GitHub account a user token acts as (``GET /user``), or None."""
    try:
        async with egress_client(timeout=_INSTALL_PROOF_TIMEOUT_SECS) as gh_client:
            resp = await gh_client.get(f"{GITHUB_API}/user", headers=_github_headers(token))
        data = resp.json() if resp.is_success else None
    except Exception as exc:
        logger.debug("Could not read a GitHub account: %s", format_exception_message(exc))
        return None
    account_id = data.get("id") if isinstance(data, dict) else None
    if account_id is None or isinstance(account_id, bool):
        return None
    return str(account_id)


async def _installation_seen_by(token: str, installation_id: str) -> dict[str, str] | None:
    """The installation's entry when *token* sees it (``GET /user/installations``)."""
    try:
        async with egress_client(timeout=_INSTALL_PROOF_TIMEOUT_SECS) as gh_client:
            for page in range(1, _INSTALLATIONS_MAX_PAGES + 1):
                resp = await gh_client.get(
                    f"{GITHUB_API}/user/installations",
                    headers=_github_headers(token),
                    params={"per_page": str(_INSTALLATIONS_PER_PAGE), "page": str(page)},
                )
                if not resp.is_success:
                    return None
                data = resp.json()
                entries = data.get("installations") if isinstance(data, dict) else None
                if not isinstance(entries, list):
                    return None
                for inst in entries:
                    if isinstance(inst, dict) and str(inst.get("id")) == str(installation_id):
                        acct = inst.get("account") if isinstance(inst.get("account"), dict) else {}
                        return {
                            "id": installation_id,
                            "account_login": acct.get("login", ""),
                            "account_type": acct.get("type", ""),
                        }
                if len(entries) < _INSTALLATIONS_PER_PAGE:
                    return None
    except Exception as exc:
        logger.debug("Could not read GitHub installations: %s", format_exception_message(exc))
    return None


async def _installations_seen(token: str) -> list[dict[str, str]] | None:
    """Every installation of the token's app it sees (``GET /user/installations``), or None."""
    seen: list[dict[str, str]] = []
    try:
        async with egress_client(timeout=_INSTALL_PROOF_TIMEOUT_SECS) as gh_client:
            for page in range(1, _INSTALLATIONS_MAX_PAGES + 1):
                resp = await gh_client.get(
                    f"{GITHUB_API}/user/installations",
                    headers=_github_headers(token),
                    params={"per_page": str(_INSTALLATIONS_PER_PAGE), "page": str(page)},
                )
                if not resp.is_success:
                    return None
                data = resp.json()
                entries = data.get("installations") if isinstance(data, dict) else None
                if not isinstance(entries, list):
                    return None
                for inst in entries:
                    inst_id = inst.get("id") if isinstance(inst, dict) else None
                    if isinstance(inst_id, bool) or not _GITHUB_INSTALLATION_ID.fullmatch(str(inst_id)):
                        continue
                    acct = inst.get("account") if isinstance(inst.get("account"), dict) else {}
                    seen.append(
                        {
                            "id": str(inst_id),
                            "account_login": acct.get("login", ""),
                            "account_type": acct.get("type", ""),
                        }
                    )
                if len(entries) < _INSTALLATIONS_PER_PAGE:
                    return seen
    except Exception as exc:
        logger.debug("Could not read GitHub installations: %s", format_exception_message(exc))
        return None
    return seen


def _lists_installations_on_connect(integration: Integration, provider: str) -> bool:
    """Whether a Connect of *integration* records the installations its new token sees.

    Under "Require verified integration notifications" the admin app's
    deliveries reach an integration only for the installations it records
    (``webhooks._reaches_installation``), and an install made from GitHub is
    recorded only on integrations connected with the installer's own account:
    one connected with another account (a bot) never learned of it. Its
    Connect adds every installation of its app the token it just received
    sees - on this integration only, never one the token cannot see, and only
    when it already records installations (one recording none is narrowed by
    what its own token sees, not by a record). With the option off
    nothing is asked and nothing changes.
    """
    from app.services.integration_notification_auth import admin_app_scoping_required

    return (
        provider == "github"
        and integration.auth_type == "oauth"
        and admin_app_scoping_required()
        and _records_installations(_load_extra(integration))
    )


async def _installations_seen_on_connect(
    db: AsyncSession, integration: Integration, provider: str, access_token: str
) -> list[dict[str, str]] | None:
    """The installations the Connect's new token sees, asked before anything is written.

    No pooled connection is held while GitHub is asked; the integration is
    read again afterwards, so an edit made meanwhile is the one extended.
    """
    if not _lists_installations_on_connect(integration, provider):
        return None
    await release_db_connection_before_loop(db)
    deadline = time.monotonic() + _CONNECT_INSTALLATIONS_BUDGET_SECS
    seen = await _before(deadline, _installations_seen(access_token))
    await db.refresh(integration)
    if seen is None:
        logger.info(
            "GitHub Connect: the installations of integration %s could not be read; "
            "those it records are kept as they are",
            integration.id,
        )
    return seen


def _record_installations_seen_on_connect(integration: Integration, seen: list[dict[str, str]]) -> None:
    """Add *seen* to the installations *integration* records, if it still records some."""
    if not _records_installations(_load_extra(integration)):
        return
    added = _add_installations(integration, seen)
    if added:
        logger.info(
            "GitHub Connect: integration %s now records installation(s) %s its token sees",
            integration.id,
            added,
        )


def _add_installations(target: Integration, entries: list[dict[str, str]]) -> list[str]:
    """Add the *entries* *target* does not record yet; the ones it records stay as they are."""
    extra = _load_extra(target)
    installations = [i for i in extra.get("installations") or [] if isinstance(i, dict)]
    if not installations and extra.get("installation_id"):
        installations.append({"id": str(extra["installation_id"])})
    known = {str(i.get("id")) for i in installations}
    added: list[dict[str, str]] = []
    for entry in entries:
        if entry["id"] not in known:
            known.add(entry["id"])
            added.append(entry)
    if not added:
        return []
    installations.extend(added)
    extra["installations"] = installations
    extra.pop("installation_id", None)
    _save_extra(target, extra)
    return [entry["id"] for entry in added]


def _decrypt_or_none(value_enc: str | None) -> str | None:
    if not value_enc:
        return None
    try:
        return decrypt_value(value_enc) or None
    except Exception:
        return None


def _records_installations(extra: object) -> bool:
    if not isinstance(extra, dict):
        return False
    installations = extra.get("installations")
    return bool(extra.get("installation_id")) or (
        isinstance(installations, list) and bool(installations)
    )


def _may_record_on(integration: Integration, client_id: str) -> bool:
    """Whether a state-less install of *client_id*'s app may land on *integration*.

    A connected OAuth GitHub integration of that app that already records
    installations; whose account its token is, is for GitHub to answer.
    """
    return (
        integration.provider == "github"
        and integration.auth_type == "oauth"
        and integration.status == INTEGRATION_CONNECTED
        and _decrypt_or_none(integration.client_id_enc) == client_id
        and _records_installations(_load_extra(integration))
    )


async def _installer_integrations(
    db: AsyncSession, client_id: str, installer_account: str, deadline: float
) -> list[tuple[Integration, str]]:
    """The integrations a state-less install provably belongs to.

    Those of the same app (the client id the code was exchanged with),
    connected with the installer's own GitHub account (``GET /user`` on their
    stored token answers the installer's account) and already recording
    installations. One recording none is not scoped to installations - adding
    one would narrow the events it receives - and one that is not connected
    (waiting, or marked ``error`` / ``reauthorize`` with its old token still
    stored) is connected from its page, whose flow carries a ``state``. Each
    comes with the token ciphertext the proof was made with.
    """
    result = await db.execute(
        select(Integration)
        .where(
            Integration.provider == "github",
            Integration.auth_type == "oauth",
            Integration.status == INTEGRATION_CONNECTED,
            Integration.access_token_enc.isnot(None),
        )
        .order_by(Integration.created_at.desc())
    )
    # (integration, its token, the ciphertext that token was read from)
    candidates: list[tuple[Integration, str, str]] = []
    for integration in result.scalars().all():
        if not _may_record_on(integration, client_id):
            continue
        token = _decrypt_or_none(integration.access_token_enc)
        if token:
            candidates.append((integration, token, integration.access_token_enc))
    if len(candidates) > _INSTALL_PROOF_MAX_CANDIDATES:
        logger.warning(
            "GitHub App install: %d integrations record installations; only the %d most "
            "recent are compared with the installer's account",
            len(candidates),
            _INSTALL_PROOF_MAX_CANDIDATES,
        )
        candidates = candidates[:_INSTALL_PROOF_MAX_CANDIDATES]
    if not candidates:
        return []

    # Every read is done: hand the pooled connection back before asking GitHub.
    await release_db_connection_before_loop(db)
    return await _prove_accounts(candidates, installer_account, deadline)


async def _prove_accounts(
    candidates: list[tuple[Integration, str, str]], installer_account: str, deadline: float
) -> list[tuple[Integration, str]]:
    """The *candidates* (integration, token, token ciphertext) whose token is *installer_account*.

    Asked of GitHub (``GET /user``) with no pooled connection held, all within
    *deadline*; a token GitHub does not answer for in time proves nothing.
    """
    if not candidates:
        return []
    if deadline - time.monotonic() <= 0:
        logger.warning("GitHub App install: GitHub did not answer within the proof budget")
        return []
    semaphore = asyncio.Semaphore(_INSTALL_PROOF_CONCURRENCY)

    async def _account(token: str) -> str | None:
        async with semaphore:
            return await _github_account_id(token)

    tasks = [asyncio.ensure_future(_account(token)) for _integration, token, _enc in candidates]
    done, pending = await asyncio.wait(tasks, timeout=max(0.0, deadline - time.monotonic()))
    for task in pending:
        task.cancel()
    if pending:
        # Their clients close before the rows are read again.
        await asyncio.gather(*pending, return_exceptions=True)
        logger.warning(
            "GitHub App install: %d integration account(s) could not be read in time; "
            "they are not updated",
            len(pending),
        )
    proven: list[tuple[Integration, str]] = []
    for (integration, _token, token_enc), task in zip(candidates, tasks, strict=True):
        if task not in done or task.cancelled() or task.exception() is not None:
            continue
        if task.result() == installer_account:
            proven.append((integration, token_enc))
    return proven


async def _record_on_proven(
    db: AsyncSession,
    proven: list[tuple[Integration, str]],
    client_id: str,
    install_entry: dict[str, str],
) -> tuple[list[Integration], list[Integration]]:
    """Record the installation on the proven integrations, as they are NOW.

    The proof ran with no row locked: each integration is read again under
    ``FOR UPDATE`` and its current configuration is the one extended, so an
    edit made meanwhile is kept. One whose status, app or recorded
    installations changed since the proof is left alone. One whose token
    changed (a refresh, a reconnection) is returned apart, untouched: the
    proof was about the token it held.
    """
    if not proven:
        return [], []
    proven_tokens = {integration.id: token_enc for integration, token_enc in proven}
    result = await db.execute(
        select(Integration)
        .where(Integration.id.in_(list(proven_tokens)))
        .order_by(Integration.created_at.desc())
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    updated: list[Integration] = []
    token_changed: list[Integration] = []
    for integration in result.scalars().all():
        if not _may_record_on(integration, client_id):
            logger.info(
                "GitHub App install: integration %s is no longer a connected integration of "
                "this app recording installations; installation %s not recorded on it",
                integration.id,
                install_entry["id"],
            )
            continue
        if integration.access_token_enc != proven_tokens.get(integration.id):
            token_changed.append(integration)
            continue
        _record_installation(integration, install_entry)
        updated.append(integration)
    return updated, token_changed


async def _record_after_token_change(
    db: AsyncSession,
    token_changed: list[Integration],
    client_id: str,
    installer_account: str,
    install_entry: dict[str, str],
    deadline: float,
) -> list[Integration]:
    """Prove once more the integrations whose token changed during the proof.

    A refresh replaces the token between GitHub's answer and the write. The
    new token is asked who it is, within the same deadline and with no pooled
    connection or row lock held (``release_db_connection_before_loop`` commits
    what was written so far, audit entries included),
    and the row is recorded if it is still the installer's account on the same
    app - read again under ``FOR UPDATE``. Once: a token that changed again is
    left alone, with a logged reason, like every row not recorded.
    """
    candidates: list[tuple[Integration, str, str]] = []
    for integration in token_changed:
        token = _decrypt_or_none(integration.access_token_enc)
        if token:
            candidates.append((integration, token, integration.access_token_enc))
        else:
            logger.info(
                "GitHub App install: integration %s no longer holds a readable token; "
                "installation %s not recorded on it",
                integration.id,
                install_entry["id"],
            )
    await release_db_connection_before_loop(db)
    reproven = await _prove_accounts(candidates, installer_account, deadline)
    reproven_ids = {integration.id for integration, _enc in reproven}
    for integration, _token, _enc in candidates:
        if integration.id not in reproven_ids:
            logger.info(
                "GitHub App install: the new token of integration %s is not the installer's "
                "GitHub account, or GitHub did not say in time; installation %s not recorded on it",
                integration.id,
                install_entry["id"],
            )
    recorded, changed_again = await _record_on_proven(db, reproven, client_id, install_entry)
    for integration in changed_again:
        logger.info(
            "GitHub App install: the token of integration %s changed again while GitHub was "
            "asked; installation %s not recorded on it",
            integration.id,
            install_entry["id"],
        )
    for integration in recorded:
        await _audit_recorded_installation(db, integration, install_entry)
    return recorded


async def _existing_user_id(db: AsyncSession, candidate: uuid.UUID | None) -> uuid.UUID | None:
    if candidate is None:
        return None
    return (await db.execute(select(User.id).where(User.id == candidate))).scalar_one_or_none()


async def _install_audit_anchor(db: AsyncSession, integration: Integration) -> uuid.UUID | None:
    """The user an installation recorded on *integration* concerns.

    Its owner. A company-managed row may have none: then whoever connected the
    token it holds (``oauth_token_owner_id``), still a user, else the oldest
    admin, as for the platform's other actions on company-level configuration
    (the LLM provider deletion in ``settings.py``). ``None`` only on a platform
    with no admin at all.
    """
    if integration.user_id is not None:
        return integration.user_id
    if not integration.is_company_managed:
        return None
    holder = await _existing_user_id(db, integration.oauth_token_owner_id)
    if holder is not None:
        return holder
    return (
        await db.execute(
            select(User.id).where(User.role == "admin").order_by(User.created_at, User.id).limit(1)
        )
    ).scalar_one_or_none()


async def _audit_recorded_installation(
    db: AsyncSession, integration: Integration, install_entry: dict[str, str]
) -> None:
    """Audit an installation a state-less callback recorded on *integration*.

    No XTM One user made the request, so the platform is the actor and the
    entry is anchored on the user it concerns (``_install_audit_anchor``).
    """
    details = {
        "installation_id": install_entry["id"],
        "installation_account": install_entry.get("account_login", ""),
        "installation_account_type": install_entry.get("account_type", ""),
    }
    anchor = await _install_audit_anchor(db, integration)
    if anchor is None:
        logger.warning(
            "GitHub App install: installation %s recorded on integration %s, which has no "
            "owner, and no admin exists to anchor its audit entry on",
            install_entry["id"],
            integration.id,
        )
        return
    await log_system_audit(
        db,
        actor="github_app_install",
        anchor_user_id=anchor,
        action="update",
        entity_type="integration",
        entity_id=integration.id,
        entity_name=integration.name,
        summary=(
            "Recorded a GitHub App installation made from GitHub: the installer's "
            "GitHub account is the one this integration is connected with"
        ),
        details=details,
    )


def _record_installation(target: Integration, install_entry: dict[str, str]) -> None:
    """Add *install_entry* to the installations *target* records (deduplicated by id)."""
    extra = _load_extra(target)
    installations = [i for i in extra.get("installations") or [] if isinstance(i, dict)]
    # Migrate legacy single installation_id to array
    if not installations and extra.get("installation_id"):
        installations.append({"id": extra.pop("installation_id")})
    installations = [
        i for i in installations if str(i.get("id")) != str(install_entry["id"])
    ]
    installations.append(install_entry)
    extra["installations"] = installations
    extra.pop("installation_id", None)  # Remove legacy field
    _save_extra(target, extra)


async def _handle_mcp_server_callback(
    state_data: dict,
    code: str,
    db: AsyncSession,
) -> RedirectResponse:
    """Handle OAuth callback for MCP server entities."""
    entity_id = state_data.get("entity_id")
    user_id = state_data.get("user_id")

    if not entity_id:
        raise HTTPException(status_code=400, detail="Missing entity_id in state")

    if state_data.get("credential") == "per_user":
        return await _handle_mcp_user_credential_callback(state_data, code, db)

    result = await db.execute(
        select(MCPServer).where(
            MCPServer.id == uuid.UUID(entity_id),
            MCPServer.user_id == uuid.UUID(user_id),
        )
    )
    mcp_server = result.scalar_one_or_none()
    if mcp_server is not None and mcp_server.credential_mode == "per_user":
        # Switched to per_user while this authorization was in flight: the
        # account authorized was meant to be shared, and nothing is shared now.
        mcp_server = None
    if not mcp_server:
        # Fallback: for company-managed servers user_id is NULL, so the owner
        # match above never hits. The admin gate ran when this flow was
        # started (``start_mcp_oauth``), and the state token that carried us
        # here was minted only after it passed.
        result = await db.execute(
            select(MCPServer).where(
                MCPServer.id == uuid.UUID(entity_id),
                MCPServer.is_company_managed.is_(True),
                MCPServer.credential_mode != "per_user",
            )
        )
        mcp_server = result.scalar_one_or_none()
    if not mcp_server:
        raise HTTPException(status_code=404, detail="MCP server not found")

    frontend_url = settings.frontend_url or settings.base_url

    try:
        with egress_route(await mcp_server_egress_route(db, mcp_server)):
            tokens = await exchange_code_for_tokens_with_mcp_server(
                mcp_server,
                code,
                # PKCE verifier for a public client — stored alongside the state
                # when the authorization request was built.
                code_verifier=state_data.get("code_verifier"),
            )
    except Exception as exc:
        detail = ""
        if isinstance(exc, httpx.HTTPStatusError):
            with contextlib.suppress(Exception):
                detail = exc.response.text or ""
        logger.warning(
            "OAuth token exchange failed for MCP server %s: %s",
            entity_id,
            format_exception_message(exc),
        )
        # A dynamically registered client is disposable, and authorization
        # servers do forget them (expiry, garbage collection, a bounded
        # registration table). Discard ours so the next Connect re-registers
        # instead of failing forever against a client_id nobody remembers.
        if "invalid_client" in detail:
            from app.services.mcp_oauth_discovery import forget_dynamic_client

            await forget_dynamic_client(db, mcp_server)
        return RedirectResponse(
            url=f"{frontend_url}/mcp-servers/{entity_id}?error=token_exchange_failed"
        )

    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token")
    scope = tokens.get("scope", "")

    if not access_token:
        logger.warning(
            "MCP server %s OAuth callback returned no access token", entity_id
        )
        return RedirectResponse(
            url=f"{frontend_url}/mcp-servers/{entity_id}?error=no_access_token"
        )

    mcp_server.access_token_enc = encrypt_value(access_token)
    if refresh_token:
        mcp_server.refresh_token_enc = encrypt_value(refresh_token)
    if scope:
        mcp_server.scopes = scope
    apply_mcp_token_expiry(mcp_server, tokens)
    mcp_server.oauth_status = "connected"
    # Written even when it already reads "connected": a refresh may have
    # recorded a verdict about the replaced tokens since the row was read.
    flag_modified(mcp_server, "oauth_status")
    # Commit the credentials before anything else can fail: the authorization
    # is the irreplaceable part (it cost the user a browser round-trip), while
    # the tool catalog below can always be rebuilt with one click.
    await db.commit()

    from app.services.token_refresh import clear_refresh_cooldown, mcp_refresh_cooldown_key

    await clear_refresh_cooldown(mcp_refresh_cooldown_key(mcp_server.id))

    await _discover_tools_after_authorization(db, mcp_server)

    return RedirectResponse(
        url=f"{frontend_url}/mcp-servers/{entity_id}?connected=true"
    )


async def _handle_mcp_user_credential_callback(
    state_data: dict,
    code: str,
    db: AsyncSession,
) -> RedirectResponse:
    """Store the account a person authorized on a ``per_user`` MCP server as their own.

    The server must still be ``per_user`` and visible to that person (it may
    have been unshared or switched back while the browser was away); the token
    lands on their ``MCPServerUserCredential``, never on the server row, and
    the server's tool catalog is left as it is (each person's catalog may
    differ; Discover tools stays the manager's).
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.integrations.oauth import apply_mcp_token_expiry
    from app.models.mcp_server_user_credential import MCPServerUserCredential
    from app.services.mcp_user_credentials import is_per_user, mcp_holder_key, user_credential
    from app.services.token_refresh import clear_refresh_cooldown, mcp_refresh_cooldown_key

    entity_id = state_data["entity_id"]
    try:
        user_uuid = uuid.UUID(str(state_data.get("user_id")))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid state") from None
    user = await db.get(User, user_uuid)
    if user is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    group_ids = await get_user_group_ids(db, user.id)
    mcp_server = await db.scalar(
        select(MCPServer).where(
            MCPServer.id == uuid.UUID(entity_id),
            user_visible_filter(MCPServer, user.id, group_ids, is_admin=bool(user.is_admin)),
        )
    )
    if mcp_server is None or not is_per_user(mcp_server):
        raise HTTPException(status_code=404, detail="MCP server not found")

    frontend_url = settings.frontend_url or settings.base_url
    try:
        with egress_route(await mcp_server_egress_route(db, mcp_server)):
            tokens = await exchange_code_for_tokens_with_mcp_server(
                mcp_server, code, code_verifier=state_data.get("code_verifier")
            )
    except Exception as exc:
        detail = ""
        if isinstance(exc, httpx.HTTPStatusError):
            with contextlib.suppress(Exception):
                detail = exc.response.text or ""
        logger.warning(
            "OAuth token exchange failed for a person's account on MCP server %s: %s",
            entity_id,
            format_exception_message(exc),
        )
        if "invalid_client" in detail:
            from app.services.mcp_oauth_discovery import forget_dynamic_client

            await forget_dynamic_client(db, mcp_server)
        return RedirectResponse(url=f"{frontend_url}/mcp-servers/{entity_id}?error=token_exchange_failed")

    access_token = tokens.get("access_token", "")
    if not access_token:
        return RedirectResponse(url=f"{frontend_url}/mcp-servers/{entity_id}?error=no_access_token")

    credential = await user_credential(db, mcp_server.id, user.id)
    if credential is None:
        credential = MCPServerUserCredential(id=uuid.uuid4(), server_id=mcp_server.id, user_id=user.id)
        credential.server = mcp_server
        db.add(credential)
    credential.access_token_enc = encrypt_value(access_token)
    if tokens.get("refresh_token"):
        credential.refresh_token_enc = encrypt_value(tokens["refresh_token"])
    if tokens.get("scope"):
        credential.granted_scopes = tokens["scope"]
    apply_mcp_token_expiry(credential, tokens)
    credential.oauth_status = "connected"
    flag_modified(credential, "oauth_status")
    cooldown_key = mcp_refresh_cooldown_key(mcp_holder_key(credential))
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="mcp_server",
        entity_id=mcp_server.id,
        entity_name=mcp_server.name,
        summary=f"Connected their own account to MCP server '{mcp_server.name}'",
    )
    await db.commit()
    await clear_refresh_cooldown(cooldown_key)
    return RedirectResponse(url=f"{frontend_url}/mcp-servers/{entity_id}?connected=true")


# Bounded well below the MCP client's own 60s connect ceiling: this runs while
# the browser sits on the callback waiting to be redirected, so a sluggish
# server must cost a moment, not a minute. Past the budget the catalog is
# simply left for "Discover Tools" to fetch.
_POST_AUTH_DISCOVERY_TIMEOUT_SECS = 20


async def _discover_tools_after_authorization(
    db: AsyncSession, mcp_server: MCPServer
) -> None:
    """Populate the tool catalog now that a token exists — best effort.

    Without this the flow ends on a freshly connected server showing no tools
    at all, and the operator has to guess that "Discover Tools" is the missing
    step. It also *replaces* any catalog gathered before authorization: a
    server that answers ``tools/list`` to anyone may well have returned a
    reduced or stubbed set, so the post-authorization listing is the
    authoritative one.

    Never raises. A failure here leaves a perfectly usable connection whose
    catalog is one click away.
    """
    if mcp_server.transport not in ("sse", "streamable_http"):
        return

    from app.api.v1.helpers import build_mcp_runtime
    from app.core.database import release_db_connection_before_loop
    from app.tools.mcp_client import MCPManager

    # The commit above already ended the transaction; hand the pooled
    # connection back explicitly so it is not pinned across the MCP round-trip.
    await release_db_connection_before_loop(db)

    try:
        runtime = await build_mcp_runtime(mcp_server)
        # Run as a child task so the timeout cancels *it*: test_connection
        # deliberately catches BaseException (to recover a hidden 401), and a
        # cancellation delivered directly to it could be absorbed there.
        result = await asyncio.wait_for(
            asyncio.create_task(MCPManager().test_connection(runtime)),
            timeout=_POST_AUTH_DISCOVERY_TIMEOUT_SECS,
        )
    except Exception as exc:
        logger.warning(
            "Post-authorization tool discovery failed for MCP server %s: %s",
            mcp_server.id,
            format_exception_message(exc),
        )
        return

    tools = result.get("tools")
    if not tools:
        logger.info(
            "MCP server %s authorized but returned no tools (status=%s)",
            mcp_server.id,
            result.get("status"),
        )
        return

    mcp_server.discovered_tools = tools
    mcp_server.tools_discovered_at = datetime.now(UTC)
    await db.commit()
    logger.info(
        "Discovered %d tool(s) for MCP server %s after authorization",
        len(tools),
        mcp_server.id,
    )


@router.get("/callback")
async def oauth_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Unified OAuth callback for all providers and entity types.

    Handles four flows:
    1. Standard OAuth (has ``state``): dispatch by entity type (integration or MCP server)
    2. GitHub App installation (has ``setup_action=install`` + ``installation_id``, no ``state``)
    3. OAuth2 implicit grant (no query at all): the token is in the URL
       fragment, which never reaches the server. The browser is redirected
       to the frontend's implicit page — a redirect keeps the fragment — and
       that page posts the token back with the state
       (``POST /api/v1/integrations/openapi/oauth/implicit``).
    4. Otherwise: 400 error
    """
    if not request.query_params:
        frontend_url = settings.frontend_url or settings.base_url
        return RedirectResponse(url=f"{frontend_url}/oauth/implicit")

    code = request.query_params.get("code")
    state = request.query_params.get("state")

    # --- GitHub App installation callback (no state) ---
    setup_action = request.query_params.get("setup_action")
    installation_id = request.query_params.get("installation_id")

    if setup_action == "install" and installation_id and code and not state:
        return await _handle_github_app_install(code, installation_id, db, request)

    # --- Standard OAuth callback (requires state) ---
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    state_data = await get_state_data(state)
    if not state_data:
        raise HTTPException(status_code=400, detail="Invalid or expired state")

    # Determine entity type — default to "integration" for backward compatibility
    entity_type = state_data.get("entity_type", "integration")

    if entity_type == "mcp_server":
        return await _handle_mcp_server_callback(state_data, code, db)
    else:
        return await _handle_integration_callback(state_data, code, db)
