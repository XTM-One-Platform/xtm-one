# SPDX-License-Identifier: Apache-2.0

"""SSO authentication – OpenID Connect (OIDC) and SAML2 flows.

OIDC:
  GET  /api/auth/oidc/login     → redirect to IdP (e.g. Microsoft Entra ID)
  GET  /api/auth/oidc/callback  → handle IdP redirect, create/login user, redirect to frontend

SAML2:
  GET  /api/auth/saml/login     → redirect to IdP via SAML AuthnRequest
  POST /api/auth/saml/acs       → Assertion Consumer Service (handle IdP POST-back)
  GET  /api/auth/saml/metadata  → SP metadata XML
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from types import SimpleNamespace
from typing import NamedTuple

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_path import with_base_path
from app.core.config import settings
from app.core.database import get_db
from app.core.email_domains import email_domain_allowed
from app.core.security import create_access_token, decrypt_value
from app.core.utils import format_exception_message
from app.llm.provider_access import carry_group_exclusions
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.user import User
from app.services.audit import log_audit
from app.services.departments import record_directory_department
from app.services.group_auto_join import AUTO_JOIN_SOURCE, join_auto_join_groups
from app.services.vault import reveal_field, reveal_json_field
from app.services.web_fetch import guarded_client_options

logger = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
# A discovery document is a few KB; this bounds a response that never ends.
_DISCOVERY_MAX_BYTES = 256 * 1024
# Sign-in fetches the document twice (login, then callback), seconds apart.
_DISCOVERY_TTL_SECONDS = 300
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
_TENANT_RE = re.compile(
    r"login\.microsoftonline\.com/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
    r"-[0-9a-f]{4}-[0-9a-f]{12})",
    re.I,
)

router = APIRouter()


# ===================================================================
# Helpers
# ===================================================================


class SSOUserNotProvisionedError(Exception):
    """Raised when no account matches an SSO identity and none may be created for it.

    The IdP authenticated the person, but neither the SSO subject nor the
    email matched an existing user, and this instance creates no account for
    them: on-the-fly user creation is off for that method
    (``oidc_auto_create_users`` / ``saml_auto_create_users``), or the email's
    domain is not one of the allowed email domains
    (``sso_config.allowed_domains``). Both callbacks catch it and send the
    browser back to the login page with the same ``not_provisioned`` marker
    whatever the ``reason``, which only the log carries — the refusal happens
    BEFORE any user row is written, so a refused sign-in leaves no account
    behind.
    """

    def __init__(self, email: str, sso_provider: str, reason: str = "") -> None:
        self.email = email
        self.sso_provider = sso_provider
        self.reason = reason or f"on-the-fly user creation is disabled for {sso_provider}"
        super().__init__(f"No existing account for {email} and {self.reason}")


class SSOAccountDeactivatedError(Exception):
    """Raised when the SSO identity resolves to a deactivated account.

    The IdP authenticated the person, but the account it resolves to (by SSO
    subject, or by email when not yet linked) has ``is_active = false``, which
    the local password login and every credential consumer refuse as well.

    Raised BEFORE the account is touched: no profile update, no subject link,
    no auto-admin promotion, no SAML group sync, and no second account created
    alongside it. Reactivation is an administrator's toggle, never a side
    effect of signing in. Both callbacks catch it, audit the refusal against
    the account, and send the browser back to the login page with the
    ``account_deactivated`` marker.
    """

    def __init__(self, user: User, sso_provider: str) -> None:
        self.user = user
        self.sso_provider = sso_provider
        super().__init__(
            f"The account {user.email} is deactivated; refusing the "
            f"{sso_provider} sign-in"
        )


async def _determine_role(db: AsyncSession) -> str:
    """First registered user is always admin."""
    count = await db.scalar(select(func.count()).select_from(User))
    if count == 0:
        return "admin"
    return "user"


def _apply_auto_admin(user: User, auto_admin: bool, sso_provider: str) -> None:
    """Promote an SSO user to admin when auto-admin is enabled.

    Applied uniformly across every branch of ``_get_or_create_sso_user`` —
    newly-created accounts as well as existing ones — so the "Automatically
    admin users" toggle takes effect on account creation and on every
    subsequent login, not just one path. A user who is created via SSO, or who
    already exists (local registration, admin pre-provisioning, or an earlier
    non-admin SSO login), is promoted whenever they authenticate while the
    toggle is on.

    This is **promote-only** — it never demotes — so manually-assigned admins
    and the platform admin are preserved, and disabling the toggle later does
    not strip existing admins of their role.
    """
    if auto_admin and user.role != "admin":
        user.role = "admin"
        logger.info(
            "Auto-promoted SSO user %s to admin (provider=%s)",
            user.email,
            sso_provider,
        )


class HubStatus(StrEnum):
    """The three outcomes of the XTM Hub gate.

    An enum rather than bare strings because the comparison that matters —
    "is this refused?" — fails *open* when mistyped: the callback would sail
    past the guard and admit everyone, which is the entire feature silently
    not happening. A misspelt member is an ``AttributeError`` on the line
    instead, and this repository runs no type checker that would catch the
    string form.
    """

    #: No administrator marker configured — the gate does not exist here.
    DISABLED = "disabled"
    #: Not entitled to this instance. No account is created or modified.
    REFUSED = "refused"
    #: Entitled; ``is_admin`` says with what role.
    ENTITLED = "entitled"


class HubEntitlement(NamedTuple):
    """What the XTM Hub gate decided about one sign-in.

    ``reason`` is operator-facing only and is never shown to the person being
    refused (FR-008).
    """

    status: HubStatus
    is_admin: bool = False
    reason: str = ""


def _resolve_hub_entitlement(userinfo: dict) -> HubEntitlement:
    """Decide, from OIDC userinfo alone, whether this instance admits a person.

    Pure by design: no database, no network, no clock. Every branch of the
    decision table in ``contracts/oidc-entitlement.md`` is therefore provable in
    a unit test, which matters because the expensive branch to get wrong — the
    refusal — is the one nobody exercises by hand.

    XTM Hub emits **one claim per instance a person may use**, named after that
    instance: ``{platform_id}_groups``. Its *presence* is the entitlement; there
    is no value to compare against. The value lists the roles that person holds
    on this instance, and the configured admin marker within it means
    administrator.

    The gate is enabled by ``XTM_HUB_ADMIN_GROUP`` alone. The instance
    identifier is this platform's own ``platform_id``, which is set for reasons
    unrelated to XTM Hub — so keying the switch off it would give a gate to
    every instance that forces an ID for clone/restore continuity.

    The claims are unnamespaced, so Auth0 only permits them in the **userinfo
    response** — never in an ID token. An implementation that decodes the ID
    token instead will find nothing and refuse everyone.

    Deliberately not read: the namespaced corporate group claim the Hub also
    emits (FR-004a). It is the person's full organisational membership — broad
    personal data answering a different question, when the per-instance claim
    already answers ours.
    """
    if not (settings.xtm_hub_admin_group or "").strip():
        return HubEntitlement(status=HubStatus.DISABLED)

    platform = settings.xtm_hub_effective_platform_id
    if not platform:
        # Startup refuses this combination, so reaching it means the settings
        # were changed after boot. Fail closed: an enabled gate that cannot name
        # the claim it needs has not been satisfied by anything.
        return HubEntitlement(
            status=HubStatus.REFUSED,
            reason="the entitlement gate is enabled but this instance has no identifier",
        )

    claim = f"{platform}_groups"
    if claim not in userinfo:
        return HubEntitlement(
            status=HubStatus.REFUSED,
            reason=f"userinfo carries no {claim!r} claim for this instance",
        )

    groups = userinfo[claim]
    if not isinstance(groups, list):
        # Malformed is refused rather than coerced: a claim we cannot read is a
        # claim we cannot make an admin decision from, and admitting on a
        # best-effort reading is how a gate quietly stops being one.
        return HubEntitlement(
            status=HubStatus.REFUSED,
            reason=f"claim {claim!r} is not a list of roles",
        )

    # Exact, case-sensitive match: the Hub owns these names, and a lenient
    # comparison here would grant administrator off a near-miss.
    marker = settings.xtm_hub_admin_group.strip()
    return HubEntitlement(status=HubStatus.ENTITLED, is_admin=marker in groups)


def _apply_hub_role(user: User, is_admin: bool) -> str | None:
    """Set an entitled person's role from the Hub, in both directions.

    Deliberately NOT routed through ``_apply_auto_admin``, which is documented
    promote-only. Under this gate the Hub is authoritative, so administrator
    must also be *removed* when the Hub stops asserting it (FR-005) — but
    changing ``_apply_auto_admin`` to demote would make every existing SSO
    deployment start stripping manually-assigned admins, which is why this is a
    separate path rather than a flag on that one.

    The platform admin is never demoted (FR-006). A group change on the Hub side
    would otherwise cost an operator administrator access to their own instance,
    with the gate itself standing between them and any way back.

    Returns the **previous** role when one was actually applied, so the caller
    can audit it, or ``None`` when nothing changed. A privilege change made from
    the Hub deserves the same durable trail as one made in the admin UI (see
    ``users.py``), and an ``INFO`` log line is not that: an operator asking who
    was granted administrator, and when, must be able to query the answer.
    """
    desired = "admin" if is_admin else "user"
    if user.role == desired:
        return None

    if desired == "user" and user.email == settings.admin_email:
        logger.info(
            "XTM Hub no longer asserts admin for the platform admin %s; "
            "keeping the role (never demoting the platform admin)",
            user.email,
        )
        return None

    previous = user.role
    logger.info(
        "XTM Hub role for %s: %s -> %s",
        user.email,
        previous,
        desired,
    )
    user.role = desired
    return previous


async def _record_refusal(db: AsyncSession, email: str, reason: str) -> None:
    """Record a refused sign-in where an operator can find it.

    Durability follows attribution. ``audit_logs.user_id`` is ``NOT NULL`` and
    the table is shared by every feature that writes a trail, so a refusal for
    an address with no account has nobody to attribute a row to. Rather than
    widen that column for rows belonging to no one, a refusal is written durably
    when the address already has an account (FR-009) and logged when it does not
    (FR-009a).

    That narrowing costs little: a refusal can only follow a *successful*
    authentication at the Hub, so an unknown address is a person the Hub
    vouched for but did not entitle here — routine rather than hostile. The
    refusals worth querying later are the ones against accounts that exist:
    someone who had access and lost it, or someone probing a real account.

    ``reason`` names the claim and this instance only — never the wider
    corporate group claim (FR-004a) and never the person's group list.
    """
    logger.warning(
        "XTM Hub entitlement refused an OIDC sign-in for %s: %s", email, reason
    )
    result = await db.execute(
        select(User).where(func.lower(User.email) == email.strip().lower())
    )
    user = result.scalar_one_or_none()
    if user is None:
        return

    await log_audit(
        db,
        user=user,
        action="login_refused",
        entity_type="user",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=f"Refused OIDC sign-in ({email}): {reason}",
    )


async def _refuse_deactivated_sign_in(
    db: AsyncSession,
    exc: SSOAccountDeactivatedError,
    provider_label: str,
    *,
    request: Request | None = None,
) -> None:
    """Audit and commit an SSO sign-in refused because the account is deactivated.

    Same ``login_refused`` trail as the XTM Hub refusals, attributed to the
    deactivated account: someone who lost access and tried again is exactly
    the event an operator wants to find later. The audit row is the only write;
    the account itself was never modified (see ``SSOAccountDeactivatedError``).
    """
    logger.warning("%s sign-in refused: %s", provider_label, exc)
    user = exc.user
    await log_audit(
        db,
        user=user,
        action="login_refused",
        entity_type="user",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=(
            f"Refused {provider_label} sign-in ({user.email}): "
            "the account is deactivated"
        ),
        details={"login_method": exc.sso_provider, "reason": "account_deactivated"},
        request=request,
    )
    await db.commit()


async def _get_or_create_sso_user(
    db: AsyncSession,
    email: str,
    full_name: str,
    sso_provider: str,
    sso_subject: str,
    title: str = "",
    auto_admin: bool = False,
    auto_create: bool = True,
    allowed_domains: Sequence[str] = (),
) -> User:
    """Find or create a user from SSO claims.

    When ``auto_create`` is False (the "On-the-fly user creation" toggle is
    off for this SSO method), an unknown identity raises
    :class:`SSOUserNotProvisionedError` instead of creating an account —
    existing users (matched by SSO subject or linked by email) keep signing
    in unchanged.

    ``allowed_domains`` (``ResolvedSSOConfig.allowed_domain_list``) restricts
    creation the way it restricts self-registration: an unknown identity
    whose email domain is not one of them raises the same error. Only
    creation is restricted: an existing account signs in, or is linked by
    email, whatever its domain.

    An existing account that is deactivated raises
    :class:`SSOAccountDeactivatedError` from either lookup, before anything on
    it is changed. The subject match does not fall through to the email
    lookup, and neither lookup falls through to creation: the identity belongs
    to that account, and a deactivated account must not get a fresh one.
    """
    # An IdP returns the address in its own case (Entra: ``First.Last@…``) while
    # every other path stores it lowercased; matching case-sensitively here made
    # a second account for the same person.
    email = email.strip().lower()

    # Try by SSO subject first
    result = await db.execute(
        select(User).where(
            User.sso_provider == sso_provider, User.sso_subject == sso_subject
        )
    )
    user = result.scalar_one_or_none()
    if user:
        if not user.is_active:
            raise SSOAccountDeactivatedError(user, sso_provider)
        # Update name/title if provided and changed
        if full_name and full_name != user.full_name:
            user.full_name = full_name
        if title and title != user.title:
            user.title = title
        _apply_auto_admin(user, auto_admin, sso_provider)
        await db.flush()
        return user

    # Try by email (link existing account)
    result = await db.execute(select(User).where(func.lower(User.email) == email))
    user = result.scalar_one_or_none()
    if user:
        if not user.is_active:
            raise SSOAccountDeactivatedError(user, sso_provider)
        user.sso_provider = sso_provider
        user.sso_subject = sso_subject
        if full_name and full_name != user.full_name:
            user.full_name = full_name
        if title and title != user.title:
            user.title = title
        _apply_auto_admin(user, auto_admin, sso_provider)
        await db.flush()
        return user

    # No existing account. Both refusals come before any row is written: with
    # on-the-fly creation disabled for this SSO method only pre-provisioned
    # accounts may enter, and with allowed email domains set only an address
    # at one of them gets an account.
    if not auto_create:
        raise SSOUserNotProvisionedError(email, sso_provider)
    if not email_domain_allowed(email, allowed_domains):
        raise SSOUserNotProvisionedError(
            email,
            sso_provider,
            reason=(
                "its domain is not one of the allowed email domains "
                f"({', '.join(allowed_domains)})"
            ),
        )

    # Create new user.  ``_determine_role`` makes the very first account an
    # admin; ``_apply_auto_admin`` then applies the toggle uniformly with the
    # existing-user branches above (and emits the same promotion log), so the
    # helper is the single source of truth for auto-admin across every path.
    role = await _determine_role(db)
    user = User(
        email=email,
        full_name=full_name or email.split("@")[0],
        title=title or None,
        sso_provider=sso_provider,
        sso_subject=sso_subject,
        role=role,
    )
    _apply_auto_admin(user, auto_admin, sso_provider)

    if settings.is_xtm_one:
        from app.models.agent import Agent

        ctem_r = await db.execute(
            select(Agent.id).where(Agent.slug == "ctem-assistant").limit(1)
        )
        ctem_id = ctem_r.scalar_one_or_none()
        if ctem_id:
            user.default_agent_id = ctem_id
            user.default_agent_source = "platform"
            user.default_agent_set_at = datetime.now(UTC)

    db.add(user)
    await db.flush()
    return user


def _extract_saml_groups(attrs: dict) -> list[str]:
    """Extract group names from SAML attributes.

    Supports multiple common group claim URIs used by identity providers
    (Microsoft Entra ID, Okta, ADFS, generic SAML).
    """
    group_claim_uris = [
        # Microsoft Entra ID (Azure AD)
        "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups",
        # ADFS / generic SAML
        "http://schemas.xmlsoap.org/claims/Group",
        # Simple attribute names
        "groups",
        "group",
        "memberOf",
    ]
    for uri in group_claim_uris:
        values = attrs.get(uri)
        if values:
            return [v.strip() for v in values if v and v.strip()]
    return []


# ── Microsoft Graph group name resolution ─────────────────────────


def _extract_tenant_id(cfg) -> str | None:
    """Extract Microsoft Entra tenant ID from OIDC discovery or SAML metadata URL."""
    for url in (cfg.oidc_discovery_url, cfg.saml_idp_metadata_url):
        if url:
            m = _TENANT_RE.search(url)
            if m:
                return m.group(1)
    return None


async def _get_graph_app_token(
    tenant_id: str, client_id: str, client_secret: str
) -> str | None:
    """Acquire an app-only token via the OAuth2 client_credentials grant."""
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                },
            )
            resp.raise_for_status()
            return resp.json().get("access_token")
    except Exception as exc:
        logger.warning("Failed to acquire Graph app token: %s", format_exception_message(exc))
        return None


async def _find_graph_credentials(db: AsyncSession, cfg) -> tuple[str, str, str] | None:
    """Find Microsoft Graph API credentials (tenant_id, client_id, secret).

    Resolution order:
    1. OIDC client credentials from SSO config (same app registration)
    2. Any Microsoft integration with OAuth client credentials + tenant_id
    """
    tenant_id = _extract_tenant_id(cfg)

    # 1. Try OIDC SSO credentials
    if tenant_id and cfg.oidc_client_id and cfg.oidc_client_secret:
        logger.info("Using OIDC SSO credentials for Graph group resolution")
        return tenant_id, cfg.oidc_client_id, cfg.oidc_client_secret

    # 2. Fall back to a Microsoft integration with client credentials
    result = await db.execute(
        select(Integration).where(
            Integration.provider == "microsoft",
            Integration.client_id_enc.is_not(None),
            Integration.client_secret_enc.is_not(None),
        )
    )
    for integ in result.scalars().all():
        try:
            client_id = decrypt_value(integ.client_id_enc)
            client_secret = await reveal_field(integ, "client_secret_enc") or ""
            extra = await reveal_json_field(integ, "extra_config_enc") or {}
            integ_tenant = extra.get("tenant_id", "")

            effective_tenant = integ_tenant or tenant_id
            if effective_tenant and client_id and client_secret:
                logger.info(
                    "Using Microsoft integration '%s' credentials for Graph group resolution",
                    integ.name,
                )
                return effective_tenant, client_id, client_secret
        except Exception:
            logger.debug(
                "Skipping Microsoft integration %s during Graph credential resolution",
                integ.name,
                exc_info=True,
            )
            continue

    logger.info(
        "No Microsoft credentials available for Graph group resolution "
        "(configure OIDC SSO or add a Microsoft integration with "
        "client credentials)"
    )
    return None


async def _resolve_group_ids_via_graph(
    group_values: list[str], cfg, db: AsyncSession
) -> dict[str, str]:
    """Resolve Entra ID group Object IDs to display names via Microsoft Graph.

    Returns a mapping ``{object_id: display_name}`` for every value that
    looks like a UUID and could be resolved.  Non-UUID values and values
    that fail resolution are silently skipped.

    Requires the app registration to have ``Group.Read.All`` (Application)
    permission in Entra ID.
    """
    uuid_values = [v for v in group_values if _UUID_RE.match(v)]
    if not uuid_values:
        return {}

    creds = await _find_graph_credentials(db, cfg)
    if not creds:
        return {}

    tenant_id, client_id, client_secret = creds
    token = await _get_graph_app_token(tenant_id, client_id, client_secret)
    if not token:
        return {}

    resolved: dict[str, str] = {}
    headers = {"Authorization": f"Bearer {token}"}

    # Batch in chunks of 15 (Graph $filter `in` operator limit is 15)
    for i in range(0, len(uuid_values), 15):
        chunk = uuid_values[i : i + 15]
        id_list = ",".join(f"'{gid}'" for gid in chunk)
        url = f"{_GRAPH_BASE}/groups?$filter=id in ({id_list})&$select=id,displayName&$top=15"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                for entry in data.get("value", []):
                    oid = entry.get("id", "")
                    name = entry.get("displayName", "")
                    if oid and name:
                        resolved[oid] = name
        except Exception as exc:
            logger.warning("Graph group resolution failed for chunk %d: %s", i, format_exception_message(exc))

    if resolved:
        logger.info(
            "Resolved %d/%d group IDs to display names via Graph API",
            len(resolved),
            len(uuid_values),
        )
    return resolved


async def _sync_user_groups(
    db: AsyncSession,
    user: User,
    group_names: list[str],
    auto_create: bool,
) -> bool:
    """Synchronize SAML-sourced group memberships from SAML group claims.

    Only manages memberships with source="saml". Manually assigned
    memberships (source="manual") are never touched, so admins can add
    users to Copilot-only groups without SAML login overwriting them.

    Existing memberships whose group appears in the SAML assertion are
    progressively re-tagged to source="saml" on each login so that the
    system converges after the migration that backfilled everything as
    "manual".

    Returns whether a membership was added or removed (a re-tag changes
    neither), for the caller to drop the user's cached quota resolution once
    the change has committed.
    """
    # Load ALL memberships in one query — the unique constraint is on
    # (group_id, user_id) regardless of source, so we must know about
    # every existing row to avoid insert conflicts.
    result = await db.execute(select(GroupMember).where(GroupMember.user_id == user.id))
    all_memberships = result.scalars().all()

    membership_by_group: dict = {m.group_id: m for m in all_memberships}
    saml_memberships = {
        gid: m for gid, m in membership_by_group.items() if m.source == "saml"
    }
    current_saml_group_ids = set(saml_memberships.keys())
    all_group_ids = set(membership_by_group.keys())
    # A group with auto join on keeps every person who signs in (#1901): a
    # SAML-sourced membership of such a group (one the mapping wrote before
    # the switch was turned on) is never removed as stale.
    kept_auto_join: set[uuid.UUID] = set()
    if current_saml_group_ids:
        result = await db.execute(
            select(Group.id).where(Group.id.in_(current_saml_group_ids), Group.auto_join.is_(True))
        )
        kept_auto_join = set(result.scalars().all())

    if not group_names:
        # No groups in assertion — remove all SAML-sourced memberships
        # except those of auto-join groups.
        removed = [m for gid, m in saml_memberships.items() if gid not in kept_auto_join]
        for membership in removed:
            await db.delete(membership)
        if removed:
            logger.info(
                "SAML group sync for %s: removed %d SAML memberships (no groups in assertion)",
                user.email,
                len(removed),
            )
        return bool(removed)

    # Resolve group IDs — find existing groups by name (or old UUID name)
    result = await db.execute(select(Group).where(Group.name.in_(group_names)))
    existing_groups = {g.name: g for g in result.scalars().all()}

    target_group_ids: set = set()
    for name in group_names:
        if name in existing_groups:
            target_group_ids.add(existing_groups[name].id)
        elif auto_create:
            new_group = Group(
                name=name,
                description="Auto-created from SAML group claim",
            )
            db.add(new_group)
            await db.flush()
            target_group_ids.add(new_group.id)
            logger.info("Auto-created group '%s' from SAML assertion", name)

    # Re-tag existing non-SAML memberships whose group is in the assertion
    # so they are progressively marked as "saml" (convergence after the
    # migration that backfilled all rows as "manual"). An auto-join
    # membership keeps its source: the mapping never takes it over, so it
    # never removes it either.
    auto_joined = {gid for gid, m in membership_by_group.items() if m.source == AUTO_JOIN_SOURCE}
    to_retag = (target_group_ids & all_group_ids) - current_saml_group_ids - auto_joined
    for gid in to_retag:
        membership_by_group[gid].source = "saml"

    # Insert memberships only for groups the user doesn't belong to at all
    to_add = target_group_ids - all_group_ids
    for gid in to_add:
        db.add(GroupMember(group_id=gid, user_id=user.id, source="saml"))

    # Remove stale SAML memberships (only SAML-sourced ones, never those of
    # an auto-join group)
    to_remove = current_saml_group_ids - target_group_ids - kept_auto_join
    for gid in to_remove:
        await db.delete(saml_memberships[gid])

    if to_add or to_remove or to_retag:
        logger.info(
            "SAML group sync for %s: added %d, re-tagged %d, removed %d memberships",
            user.email,
            len(to_add),
            len(to_retag),
            len(to_remove),
        )
    return bool(to_add or to_remove)


async def _rename_uuid_groups(
    db: AsyncSession,
    resolved: dict[str, str],
    *,
    moved_members: set[uuid.UUID] | None = None,
) -> int:
    """Rename existing groups whose name is a UUID to the resolved display name.

    Skips renaming if another group with the target display name already
    exists (avoids unique-constraint violations).  Returns the number of
    groups renamed.

    A group merged into an existing one moves its members; their ids are added
    to *moved_members*, for the caller to drop their cached quota resolutions
    once the change has committed.
    """
    if not resolved:
        return 0

    uuid_names = list(resolved.keys())
    result = await db.execute(select(Group).where(Group.name.in_(uuid_names)))
    uuid_groups = {g.name: g for g in result.scalars().all()}

    if not uuid_groups:
        return 0

    # Pre-check: which display names already exist as group names?
    target_names = list(resolved.values())
    result = await db.execute(select(Group).where(Group.name.in_(target_names)))
    existing_target_names = {g.name for g in result.scalars().all()}

    renamed = 0
    for uuid_name, group in uuid_groups.items():
        display_name = resolved.get(uuid_name, "")
        if not display_name:
            continue
        if display_name in existing_target_names:
            # A group with the display name already exists — merge by
            # reassigning members from the UUID group to the existing one,
            # then delete the UUID group.
            target_result = await db.execute(
                select(Group).where(Group.name == display_name)
            )
            target_group = target_result.scalar_one_or_none()
            if target_group:
                # Move memberships
                members_result = await db.execute(
                    select(GroupMember).where(GroupMember.group_id == group.id)
                )
                for member in members_result.scalars().all():
                    # Check if already a member of target
                    existing = await db.execute(
                        select(GroupMember).where(
                            GroupMember.group_id == target_group.id,
                            GroupMember.user_id == member.user_id,
                        )
                    )
                    if not existing.scalar_one_or_none():
                        db.add(
                            GroupMember(
                                group_id=target_group.id,
                                user_id=member.user_id,
                                source=member.source,
                            )
                        )
                    if moved_members is not None:
                        moved_members.add(member.user_id)
                    await db.delete(member)
                # Its members now belong to the target group: so do the LLM
                # provider exclusions that kept them off a vendor.
                await carry_group_exclusions(db, group.id, target_group.id)
                await db.delete(group)
                renamed += 1
                logger.info(
                    "Merged UUID group '%s' into existing group '%s'",
                    uuid_name,
                    display_name,
                )
        else:
            group.name = display_name
            existing_target_names.add(display_name)
            renamed += 1
            logger.info(
                "Renamed group '%s' → '%s'",
                uuid_name,
                display_name,
            )

    if renamed:
        await db.flush()
    return renamed


def _first(lst: list | None) -> str | None:
    """Return first element of a list or None."""
    if lst and len(lst) > 0:
        return lst[0]
    return None


def _build_frontend_redirect(
    user: User, *, provider: str, desktop_login: str | None = None
) -> str:
    """Build the redirect URL to the frontend, carrying the JWT in the fragment.

    The token is delivered as ``/login#token=...`` (NOT ``?token=...``):
    fragments are processed client-side only and are never sent to servers,
    written to proxy/access logs, or leaked through ``Referer`` headers,
    while a query string is exposed to all three. The login page reads the
    fragment (with a legacy query-string fallback) and scrubs it from the
    address bar after consumption.

    No ``role`` claim is embedded — the role is authoritative server-side only
    and the frontend reads it from ``GET /auth/me`` (see ``create_access_token``).

    **Two-factor.** When the account is enrolled, this hands back
    ``#mfa_token=...`` instead — a scoped, 5-minute challenge that only
    ``POST /auth/mfa/verify`` accepts — and the login page renders the code step
    rather than signing the user in. The IdP has proven the *first* factor; the
    second one is XTM One's own, which is the point: a deployment federating to
    a provider whose MFA policy it does not control can still enforce one at the
    application boundary. Both SSO callbacks funnel through here, so OIDC and
    SAML get this from the single change.

    The fragment (not a query string) matters twice over here: the challenge
    token is a bearer credential too, and keeping it out of access logs and
    ``Referer`` headers is the same requirement as for the session token.

    Uses ``frontend_url`` for split-origin deployments, falling back to
    ``base_url`` for single-origin ones, with trailing slashes stripped —
    the same convention as every other frontend link builder
    (``auth.forgot_password``, ``users.send_password_reset``,
    ``invitations``, ``oauth_callbacks``).

    **XTM One Desktop.** When the sign-in was started from the Desktop
    app (``desktop_login`` came back through the provider's ``state`` /
    ``RelayState``), the id rides along in the same fragment. The login page
    then hands the session to the app instead of signing this browser in - see
    ``app.services.desktop_login``. Still the fragment, for the same reason.
    """
    from app.services.mfa import challenge_required, create_challenge_token

    frontend_base = (settings.frontend_url or settings.base_url).rstrip("/")
    suffix = f"&desktop_login={desktop_login}" if desktop_login else ""
    if challenge_required(user):
        challenge = create_challenge_token(str(user.id), method=provider)
        return f"{frontend_base}/login#mfa_token={challenge}{suffix}"
    token = create_access_token(str(user.id))
    return f"{frontend_base}/login#token={token}{suffix}"


def _desktop_login_from_query(desktop_login: str | None) -> str | None:
    """A well-formed handoff id from the SSO start route's query, else ``None``.

    Validated BEFORE it is placed in the provider ``state`` / ``RelayState``:
    the parameter is client-supplied, and only our own id shape may travel the
    round-trip. A malformed value is simply an ordinary browser sign-in.
    """
    from app.services.desktop_login import is_valid_login_id

    return desktop_login if is_valid_login_id(desktop_login) else None


def _sso_error_redirect(marker: str, *, status_code: int = 307) -> RedirectResponse:
    """Send the browser back to the login page with an opaque failure marker.

    ``status_code`` matters for the SAML ACS: that endpoint is a POST, and a
    307 preserves the method — the browser would re-POST the SAML form to the
    login page. Pass 303 there (same as its success redirect) so the browser
    follows with a GET.

    Every way a *sign-in* can fail here used to surface as raw JSON — an
    ``HTTPException`` body, an unhandled provider error, or FastAPI's own 422 for
    the missing ``code`` when the provider replies with an error instead. A
    person who declined a consent screen is not debugging an API; they get a page.

    The one deliberate exception is the 404 for OIDC being disabled: that is not
    a failed sign-in but an inactive endpoint, and it keeps its API shape.

    The marker is one of a small closed set that this module chooses. Provider
    text is never echoed into the URL: it is attacker-influencable in the general
    case, and it describes the identity provider's configuration, which an
    unauthenticated visitor has no business reading (FR-008).
    """
    frontend_base = (settings.frontend_url or settings.base_url).rstrip("/")
    return RedirectResponse(
        f"{frontend_base}/login?sso_error={marker}", status_code=status_code
    )


#: Longest provider-supplied text written to the log. An OAuth ``error`` is a
#: short token and a description is a sentence; anything beyond this is padding.
_PROVIDER_TEXT_LIMIT = 200


def _sanitise_provider_text(value: str | None, *, limit: int = _PROVIDER_TEXT_LIMIT) -> str:
    """Make provider-supplied text safe to write to the operator's log.

    ``/oidc/callback`` answers **unauthenticated** — the identity provider
    redirects a browser to it, so anyone can request it directly with whatever
    query string they like. Its ``error`` and ``error_description`` are therefore
    attacker-controlled in practice, not merely provider-controlled.

    Writing them verbatim would let a caller embed newlines and forge whole log
    entries, and pad a single request out to an arbitrary length. That matters
    here specifically because the log is where FR-008 puts the truth: the person
    is told nothing, on the understanding that an operator can read what really
    happened. A log a stranger can write into is not that record.

    Collapsing all whitespace kills the line-forging (a log entry becomes one
    line by construction) and truncation bounds the volume per request.
    """
    if not value:
        return ""
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "…(truncated)"


def _login_audit_summary(user: User, provider_label: str) -> str:
    """Audit line for an SSO callback, honest about whether the login finished.

    The callbacks write their audit entry before redirecting, so an enrolled
    account would otherwise be recorded as "Logged in via OIDC" at the moment it
    was actually only halfway there — and, if the user never produces a code,
    the trail would show a completed login that never happened. The completion
    entry is written by ``POST /auth/mfa/verify`` instead.
    """
    from app.services.mfa import challenge_required

    if challenge_required(user):
        return (
            f"Authenticated via {provider_label}, awaiting second factor "
            f"({user.email})"
        )
    return f"Logged in via {provider_label} ({user.email})"


# ===================================================================
# OIDC (OpenID Connect)
# ===================================================================


def _oidc_client_from_cfg(cfg) -> AsyncOAuth2Client:
    return AsyncOAuth2Client(
        client_id=cfg.oidc_client_id,
        client_secret=cfg.oidc_client_secret,
        redirect_uri=f"{settings.base_url}/api/auth/oidc/callback",
        scope=cfg.oidc_scopes,
    )


class _OidcDiscoveryError(Exception):
    """The provider's discovery document could not be fetched or is unusable.

    Already logged where it is raised; the routes only turn it into a redirect.
    ``marker`` is ``misconfigured`` unless the provider looks temporarily down.
    ``reason`` is what an administrator testing the URL is shown.
    """

    def __init__(self, marker: str = "misconfigured", reason: str = "") -> None:
        super().__init__(marker)
        self.marker = marker
        self.reason = reason


def _discovery_url_hint(url: str) -> str:
    """Log suffix for a Discovery URL that is probably the issuer root."""
    if not url or ".well-known" in url:
        return ""
    return (
        " The Discovery URL should end in /.well-known/openid-configuration,"
        " not be the issuer URL."
    )


def _discovery_error(marker: str, url: str, detail: str) -> _OidcDiscoveryError:
    """Log a failed discovery fetch and return the error to raise.

    The URL hint is for a wrong URL only, not for a provider that is down.
    """
    hint = _discovery_url_hint(url) if marker == "misconfigured" else ""
    logger.warning(
        "OIDC discovery for %s failed: %s.%s",
        _sanitise_provider_text(url),
        detail,
        hint,
    )
    return _OidcDiscoveryError(marker, f"{detail}.{hint}")


async def _read_capped(resp: httpx.Response) -> bytes | None:
    """Read a streamed body, or ``None`` once it passes the metadata size limit."""
    body = bytearray()
    async for chunk in resp.aiter_bytes():
        body.extend(chunk)
        if len(body) > _DISCOVERY_MAX_BYTES:
            return None
    return bytes(body)


# Discovery URL -> (monotonic expiry, document). Successes only, so a provider
# that comes back is used on the next sign-in; cleared when the settings are saved.
_discovery_cache: dict[str, tuple[float, dict]] = {}


def clear_oidc_discovery_cache() -> None:
    _discovery_cache.clear()


async def _oidc_discovery_from_cfg(cfg, *, use_cache: bool = True) -> dict:
    """Fetch OIDC discovery document using resolved config."""
    url = cfg.oidc_discovery_url
    if not url:
        return {
            "authorization_endpoint": cfg.oidc_authorization_url,
            "token_endpoint": cfg.oidc_token_url,
            "userinfo_endpoint": cfg.oidc_userinfo_url,
        }
    cached = _discovery_cache.get(url)
    if use_cache and cached and cached[0] > time.monotonic():
        return cached[1]
    try:
        async with (
            # Operator-network policy: an intranet IdP is fine; loopback, link-local
            # and cloud-metadata addresses are refused, on every redirect hop too.
            httpx.AsyncClient(
                follow_redirects=True, **guarded_client_options(allow_private=True)
            ) as client,
            client.stream("GET", url) as resp,
        ):
            status = resp.status_code
            content_type = _sanitise_provider_text(
                resp.headers.get("content-type"), limit=64
            )
            body = await _read_capped(resp) if resp.is_success else b""
            # Where the redirects ended, without the query or fragment: a login
            # redirect can carry a ticket there.
            landed = str(resp.url.copy_with(query=None, fragment=None))
    # InvalidURL is not an HTTPError; a missing scheme is a mistake in the URL.
    except (httpx.InvalidURL, httpx.UnsupportedProtocol) as exc:
        raise _discovery_error("misconfigured", url, type(exc).__name__) from exc
    # The provider is unreachable or slow: not something an administrator set wrong.
    except httpx.TransportError as exc:
        raise _discovery_error("failed", url, type(exc).__name__) from exc
    except httpx.HTTPError as exc:
        raise _discovery_error("misconfigured", url, type(exc).__name__) from exc
    detail = f"HTTP {status}, content-type {content_type}"
    if landed != url:
        detail += f", ended at {_sanitise_provider_text(landed)}"
    if status >= 500 or status in (408, 429):
        raise _discovery_error("failed", url, detail)
    if not resp.is_success:
        raise _discovery_error("misconfigured", url, detail)
    if body is None:
        raise _discovery_error("misconfigured", url, f"{detail}, body over the size limit")
    try:
        doc = json.loads(body)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        raise _discovery_error("misconfigured", url, f"{detail}, not a JSON object")
    if use_cache:
        _discovery_cache[url] = (time.monotonic() + _DISCOVERY_TTL_SECONDS, doc)
    return doc


def _oidc_endpoint(disco: dict, key: str, cfg, *, required: bool = True) -> str:
    """Return ``disco[key]``, or log what the document held and raise.

    An optional endpoint may be absent or blank (``""``); a value of any other
    type is still refused.
    """
    value = disco.get(key)
    if isinstance(value, str) and value:
        return value
    if not required and (value is None or value == ""):
        return ""
    url = cfg.oidc_discovery_url
    if not url:
        logger.warning("OIDC %s is blank in the SSO settings", key)
        reason = f"{key} is blank in the SSO settings."
    elif key in disco:
        logger.warning(
            "OIDC discovery document for %s has an empty or non-string %s.%s",
            _sanitise_provider_text(url),
            key,
            _discovery_url_hint(url),
        )
        reason = (
            f"The document has an empty or non-string {key}."
            f"{_discovery_url_hint(url)}"
        )
    else:
        # Keys only, never values: a realm-info body carries a public key.
        keys = _sanitise_provider_text(", ".join(sorted(map(str, disco))), limit=300)
        logger.warning(
            "OIDC discovery document for %s has no %s; keys received: %s.%s",
            _sanitise_provider_text(url),
            key,
            keys,
            _discovery_url_hint(url),
        )
        reason = (
            f"The document has no {key}; keys received: {keys}."
            f"{_discovery_url_hint(url)}"
        )
    raise _OidcDiscoveryError(reason=reason)


async def check_oidc_discovery(url: str) -> _OidcDiscoveryError | None:
    """Fetch ``url`` live, as sign-in would, and return why it is unusable.

    Bypasses the cache so the administrator sees the provider as it is now.
    """
    cfg = SimpleNamespace(
        oidc_discovery_url=url,
        oidc_authorization_url="",
        oidc_token_url="",
        oidc_userinfo_url="",
    )
    try:
        disco = await _oidc_discovery_from_cfg(cfg, use_cache=False)
        _oidc_endpoint(disco, "authorization_endpoint", cfg)
        _oidc_endpoint(disco, "token_endpoint", cfg)
        _oidc_endpoint(disco, "userinfo_endpoint", cfg, required=False)
    except _OidcDiscoveryError as exc:
        return exc
    return None


@router.get("/oidc/login")
async def oidc_login(
    desktop_login: str | None = None, db: AsyncSession = Depends(get_db)
):
    """Redirect the user to the OIDC identity provider.

    ``desktop_login`` marks a sign-in the XTM One Desktop app started
    in the system browser; it travels in ``state`` and comes back to the
    callback, which then routes the session to the app instead of this browser.
    """
    from app.core.sso_helpers import get_sso_config
    from app.services.desktop_login import encode_state

    cfg = await get_sso_config(db)
    if not cfg.oidc_enabled:
        raise HTTPException(status_code=404, detail="OIDC is not enabled")

    try:
        disco = await _oidc_discovery_from_cfg(cfg)
        authorization_endpoint = _oidc_endpoint(disco, "authorization_endpoint", cfg)
    except _OidcDiscoveryError as exc:
        return _sso_error_redirect(exc.marker)
    client = _oidc_client_from_cfg(cfg)
    handoff = _desktop_login_from_query(desktop_login)
    uri, _state = client.create_authorization_url(
        authorization_endpoint,
        response_type="code",
        **({"state": encode_state(handoff)} if handoff else {}),
    )
    return RedirectResponse(uri)


@router.get("/oidc/callback")
async def oidc_callback(
    code: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    state: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Handle the OIDC callback after the user authenticates with the IdP.

    ``code`` is optional because the provider does not always send one. An
    authorization-code flow that the person abandons — declining the consent
    screen is the ordinary case — comes back as ``?error=access_denied`` with no
    code at all. Declaring ``code`` required made FastAPI answer that with a 422
    validation body, so choosing "no" at the identity provider showed the person
    raw JSON about a missing query parameter.

    ``state`` is read for one thing only: a desktop handoff id the start route
    placed there. Any other value is ignored.
    """
    from app.core.sso_helpers import get_sso_config
    from app.services.desktop_login import decode_state

    desktop_login = decode_state(state)

    if error:
        # Logged, not shown. ``access_denied`` is a choice the person made and
        # gets a matter-of-fact page; anything else is a real failure an
        # operator needs the detail of, and they read it here.
        logger.warning(
            "OIDC provider returned an error instead of a code: %s (%s)",
            _sanitise_provider_text(error, limit=64),
            _sanitise_provider_text(error_description) or "no description",
        )
        return _sso_error_redirect(
            "cancelled" if error == "access_denied" else "failed"
        )

    if not code:
        # Neither a code nor an error: a malformed callback, or someone opening
        # the URL directly. Nothing to authenticate, so nothing to say.
        logger.warning("OIDC callback reached with neither a code nor an error")
        return _sso_error_redirect("failed")

    cfg = await get_sso_config(db)
    if not cfg.oidc_enabled:
        raise HTTPException(status_code=404, detail="OIDC is not enabled")

    try:
        disco = await _oidc_discovery_from_cfg(cfg)
        token_endpoint = _oidc_endpoint(disco, "token_endpoint", cfg)
        userinfo_url = _oidc_endpoint(disco, "userinfo_endpoint", cfg, required=False)
    except _OidcDiscoveryError as exc:
        return _sso_error_redirect(exc.marker)
    client = _oidc_client_from_cfg(cfg)

    # The exchange and the profile fetch both talk to the provider, and both
    # fail in ordinary ways: an authorization code is single-use and short-lived,
    # so reloading this URL, pressing back onto it, or leaving the consent screen
    # open too long all raise here. That is a person mishandling a browser, not
    # an incident, and it used to reach them as ``{"detail":"Internal Server
    # Error"}``. The detail goes to the log, where an operator can act on it.
    try:
        token_resp = await client.fetch_token(
            token_endpoint,
            code=code,
            grant_type="authorization_code",
        )

        # Fetch user info
        if userinfo_url:
            async with httpx.AsyncClient() as http:
                resp = await http.get(
                    userinfo_url,
                    headers={"Authorization": f"Bearer {token_resp['access_token']}"},
                )
                resp.raise_for_status()
                userinfo = resp.json()
        else:
            userinfo = token_resp.get("userinfo", {})
    except Exception:
        logger.warning(
            "OIDC token exchange or userinfo fetch failed", exc_info=True
        )
        return _sso_error_redirect("failed")

    email = userinfo.get("email") or userinfo.get("preferred_username", "")
    full_name = userinfo.get("name", "")
    subject = userinfo.get("sub", email)
    title = userinfo.get("jobTitle", "") or userinfo.get("title", "")

    if not email:
        # A configuration problem at the provider (no email claim in the granted
        # scopes) rather than anything the person did, but they are the one
        # holding the browser, so they get a page and the operator gets the log.
        logger.warning(
            "OIDC provider returned no email claim; check the requested scopes"
        )
        return _sso_error_redirect("failed")

    # XTM Hub entitlement gate — deliberately HERE, above
    # ``_get_or_create_sso_user``, which is what writes the user row. Returning
    # from this point is what makes "a refused sign-in leaves no account behind"
    # true by construction (FR-002), rather than compensated for afterwards:
    # create-then-check-then-delete leaves a window in which the account exists,
    # and a trail of accounts that should never have been created.
    #
    # It also runs for people who already have an account (FR-003) — an existing
    # user whose Hub entitlement has gone must be turned away too, not merely
    # denied a new account.
    #
    # ``userinfo`` is already populated at this point, from the userinfo endpoint
    # or from ``token_resp["userinfo"]``, so one check covers both provider
    # shapes.
    entitlement = _resolve_hub_entitlement(userinfo)
    if entitlement.status is HubStatus.REFUSED:
        await _record_refusal(db, email, entitlement.reason)
        await db.commit()
        # The reason stays server-side. The person is unauthenticated to us, and
        # the criteria describe how the organisation's identity provider is
        # configured (FR-008).
        return _sso_error_redirect("not_authorised")

    try:
        user = await _get_or_create_sso_user(
            db,
            email,
            full_name,
            "oidc",
            subject,
            title=title,
            auto_admin=cfg.oidc_auto_admin_new_users,
            auto_create=cfg.oidc_auto_create_users,
            allowed_domains=cfg.allowed_domain_list,
        )
    except SSOUserNotProvisionedError as exc:
        # The person is authenticated at the IdP but has no account here and
        # none is created for them (on-the-fly creation off, or an email
        # domain outside the allowed ones). The detail stays in the log for
        # the operator; the person gets a readable page, not a JSON body.
        logger.warning("OIDC sign-in refused: %s", exc)
        return _sso_error_redirect("not_provisioned")
    except SSOAccountDeactivatedError as exc:
        # Refused before any token is minted and before the account is
        # touched; the Hub role sync below never runs for it either.
        await _refuse_deactivated_sign_in(db, exc, "OIDC")
        return _sso_error_redirect("account_deactivated")
    await record_directory_department(db, user, userinfo)
    # Every group with auto join on, at every single sign-on (#1901).
    auto_joined = await join_auto_join_groups(db, user, provider="OIDC")
    if entitlement.status is HubStatus.ENTITLED:
        # Applied AFTER account resolution so the Hub is the last word:
        # ``_get_or_create_sso_user`` may have promoted via the promote-only
        # auto-admin toggle, and under this gate the Hub decides both ways.
        previous_role = _apply_hub_role(user, entitlement.is_admin)
        if previous_role is not None:
            # Same shape as a role change made in the admin UI (``users.py``),
            # so one query answers "who became an administrator, and when"
            # whoever made the decision.
            await log_audit(
                db,
                user=user,
                action="update",
                entity_type="user",
                entity_id=user.id,
                entity_name=user.full_name,
                summary=(
                    f"Changed role of '{user.email}' from '{previous_role}' to "
                    f"'{user.role}' (asserted by XTM Hub)"
                ),
            )
    await log_audit(
        db,
        user=user,
        action="login",
        entity_type="user",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=_login_audit_summary(user, "OIDC"),
    )
    await db.commit()
    if auto_joined:
        from app.services.quota import invalidate_membership_quota_cache

        # Never raises: a Redis outage must not fail the sign-in.
        await invalidate_membership_quota_cache({user.id})

    redirect_url = _build_frontend_redirect(
        user, provider="oidc", desktop_login=desktop_login
    )
    return RedirectResponse(redirect_url)


# ===================================================================
# SAML2
# ===================================================================


def _saml_request_data(
    route_path: str,
    *,
    get_data: dict | None = None,
    post_data: dict | None = None,
) -> dict:
    """The request description python3-saml reconstructs the SP's URLs from.

    ``script_name`` must be the PUBLIC path of the route, so it carries the URL
    path prefix the platform is served under (``BASE_PATH``): the
    library rebuilds ``https://host{script_name}`` and, in strict mode, refuses
    a Response whose ``Destination`` / ``Recipient`` - the ACS URL advertised
    from ``BASE_URL`` - is not that URL. Host and scheme come from ``BASE_URL``
    for the same reason.
    """
    return {
        "https": "on" if settings.base_url.startswith("https") else "off",
        "http_host": settings.base_url.split("//")[1].split("/")[0],
        "script_name": with_base_path(route_path),
        "get_data": get_data or {},
        "post_data": post_data or {},
    }


@router.get("/saml/login")
async def saml_login(
    desktop_login: str | None = None, db: AsyncSession = Depends(get_db)
):
    """Redirect the user to the SAML2 IdP.

    ``desktop_login`` rides ``RelayState`` - echoed verbatim by the
    IdP, and well under the 80-byte cap the SAML 2.0 bindings put on it.
    """
    from app.core.sso_helpers import get_sso_config
    from app.services.desktop_login import encode_state

    cfg = await get_sso_config(db)
    if not cfg.saml_enabled:
        raise HTTPException(status_code=404, detail="SAML2 is not enabled")

    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
    except ImportError as exc:
        raise HTTPException(
            status_code=500, detail="python3-saml not installed"
        ) from exc

    saml_settings = await _build_saml_settings_from_cfg(cfg)
    req = _saml_request_data("/api/auth/saml/login")
    auth = OneLogin_Saml2_Auth(req, saml_settings)
    handoff = _desktop_login_from_query(desktop_login)
    redirect_url = auth.login(return_to=encode_state(handoff)) if handoff else auth.login()
    return RedirectResponse(redirect_url)


@router.post("/saml/acs")
async def saml_acs(request: Request, db: AsyncSession = Depends(get_db)):
    """SAML2 Assertion Consumer Service – receives the IdP POST-back."""
    from app.core.sso_helpers import get_sso_config

    cfg = await get_sso_config(db)
    if not cfg.saml_enabled:
        raise HTTPException(status_code=404, detail="SAML2 is not enabled")

    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
    except ImportError as exc:
        raise HTTPException(
            status_code=500, detail="python3-saml not installed"
        ) from exc

    form = await request.form()
    saml_settings = await _build_saml_settings_from_cfg(cfg)
    # A desktop handoff id the start route placed in RelayState; any
    # other RelayState is ignored.
    from app.services.desktop_login import decode_state

    desktop_login = decode_state(form.get("RelayState"))

    req = _saml_request_data(
        "/api/auth/saml/acs",
        get_data=dict(request.query_params),
        post_data=dict(form),
    )
    auth = OneLogin_Saml2_Auth(req, saml_settings)
    auth.process_response()
    errors = auth.get_errors()

    if errors:
        logger.error(
            "SAML ACS errors: %s – reason: %s", errors, auth.get_last_error_reason()
        )
        raise HTTPException(status_code=400, detail=f"SAML error: {', '.join(errors)}")

    if not auth.is_authenticated():
        raise HTTPException(status_code=401, detail="SAML authentication failed")

    attrs = auth.get_attributes()
    name_id = auth.get_nameid()
    logger.info("SAML attributes received: %s", {k: v for k, v in attrs.items()})

    email = (
        _first(
            attrs.get(
                "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"
            )
        )
        or _first(attrs.get("email"))
        or name_id
    )

    # Resolve full name – prefer explicit display name, then build from
    # givenname + surname.  The standard "name" claim is checked last because
    # Microsoft Entra maps it to the UPN (email-like), not the display name.
    given = (
        _first(
            attrs.get("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname")
        )
        or _first(attrs.get("givenName"))
        or _first(attrs.get("firstName"))
        or ""
    )
    surname = (
        _first(
            attrs.get("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname")
        )
        or _first(attrs.get("surname"))
        or _first(attrs.get("lastName"))
        or ""
    )
    full_name = (
        _first(attrs.get("http://schemas.microsoft.com/identity/claims/displayname"))
        or _first(
            attrs.get(
                "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/displayname"
            )
        )
        or _first(attrs.get("displayName"))
        or (f"{given} {surname}".strip() if (given or surname) else "")
        or _first(attrs.get("cn"))
        or _first(
            attrs.get("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name")
        )
        or ""
    )

    # Resolve title/jobTitle from SAML attributes
    title = (
        _first(
            attrs.get("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/jobtitle")
        )
        or _first(attrs.get("http://schemas.microsoft.com/identity/claims/jobtitle"))
        or _first(
            attrs.get("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/title")
        )
        or _first(attrs.get("jobTitle"))
        or _first(attrs.get("title"))
        or ""
    )
    subject = name_id or email

    if not email:
        raise HTTPException(status_code=400, detail="SAML IdP did not return an email")

    try:
        user = await _get_or_create_sso_user(
            db,
            email,
            full_name,
            "saml",
            subject,
            title=title,
            auto_admin=cfg.saml_auto_admin_new_users,
            auto_create=cfg.saml_auto_create_users,
            allowed_domains=cfg.allowed_domain_list,
        )
    except SSOUserNotProvisionedError as exc:
        # Same contract as the OIDC callback: the refusal happens before any
        # user row is written, the operator reads the detail in the log, and
        # the browser lands back on the login page with a readable message.
        logger.warning("SAML sign-in refused: %s", exc)
        return _sso_error_redirect("not_provisioned", status_code=303)
    except SSOAccountDeactivatedError as exc:
        # Before the group mapping below: a deactivated account's memberships
        # stay exactly as they were, like the rest of the account.
        await _refuse_deactivated_sign_in(db, exc, "SAML", request=request)
        return _sso_error_redirect("account_deactivated", status_code=303)

    await record_directory_department(db, user, attrs)

    # ── SAML group mapping ──────────────────────────────────────────
    # Whose memberships the mapping changed: their cached quota resolutions
    # (which include their groups' quotas) are dropped once it has committed.
    membership_changed: set[uuid.UUID] = set()
    # Every group with auto join on, at every single sign-on (#1901). Before
    # the mapping: it then reads these memberships and leaves them alone (it
    # manages only its own), and the mapping's pending rows are never flushed
    # outside the race handling below.
    if await join_auto_join_groups(db, user, provider="SAML"):
        membership_changed.add(user.id)
    if cfg.saml_groups_mapping_enabled:
        saml_groups = _extract_saml_groups(attrs)
        if saml_groups:
            logger.info("SAML groups for %s: %s", email, saml_groups)

        # If group values look like Entra ID Object IDs (UUIDs), try to
        # resolve them to human-readable display names via Microsoft Graph.
        resolved = await _resolve_group_ids_via_graph(saml_groups, cfg, db)
        if resolved:
            # Rename existing DB groups that still have UUID names
            renamed = await _rename_uuid_groups(
                db, resolved, moved_members=membership_changed
            )
            if renamed:
                logger.info("Renamed %d UUID-named groups to display names", renamed)

            # Replace UUID values with display names for the sync step
            saml_groups = [resolved.get(g, g) for g in saml_groups]
            logger.info("Resolved SAML groups for %s: %s", email, saml_groups)

        if await _sync_user_groups(
            db, user, saml_groups, auto_create=cfg.saml_groups_auto_create
        ):
            membership_changed.add(user.id)

    try:
        await log_audit(
            db,
            user=user,
            action="login",
            entity_type="user",
            entity_id=user.id,
            entity_name=user.full_name,
            summary=_login_audit_summary(user, "SAML"),
            request=request,
        )
        await db.commit()
    except IntegrityError:
        # Concurrent SAML login for the same user — the other request
        # already committed the group memberships.  Roll back and retry
        # with just the audit log (memberships already persisted).
        await db.rollback()
        # The rollback expired the account: reading it now would lazy-load,
        # which the async session refuses (MissingGreenlet).
        await db.refresh(user)
        logger.warning(
            "SAML group sync race for %s — retrying without group changes",
            email,
        )
        await log_audit(
            db,
            user=user,
            action="login",
            entity_type="user",
            entity_id=user.id,
            entity_name=user.full_name,
            summary=_login_audit_summary(user, "SAML"),
            request=request,
        )
        await db.commit()

    if membership_changed:
        from app.services.quota import invalidate_membership_quota_cache

        # Never raises: a Redis outage must not fail the sign-in.
        await invalidate_membership_quota_cache(membership_changed)

    redirect_url = _build_frontend_redirect(
        user, provider="saml", desktop_login=desktop_login
    )
    return RedirectResponse(redirect_url, status_code=303)


@router.get("/saml/metadata")
async def saml_metadata(db: AsyncSession = Depends(get_db)):
    """Return the SAML2 SP metadata XML."""
    from app.core.sso_helpers import get_sso_config

    cfg = await get_sso_config(db)
    if not cfg.saml_enabled:
        raise HTTPException(status_code=404, detail="SAML2 is not enabled")

    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
    except ImportError as exc:
        raise HTTPException(
            status_code=500, detail="python3-saml not installed"
        ) from exc

    saml_settings = await _build_saml_settings_from_cfg(cfg)
    req = _saml_request_data("/api/auth/saml/metadata")
    auth = OneLogin_Saml2_Auth(req, saml_settings)
    metadata = auth.get_settings().get_sp_metadata()
    return Response(content=metadata, media_type="application/xml")


# ===================================================================
# SAML helpers
# ===================================================================

_IDP_METADATA_CACHE_MAX = 10
_IDP_METADATA_TTL = 3600
_idp_metadata_cache: dict[str, tuple[dict, float]] = {}


async def _build_saml_settings_from_cfg(cfg) -> dict:
    """Build the python3-saml settings dict from resolved config.

    Fetches and caches the IdP metadata XML so python3-saml has the
    entityId, SSO URL, and x509 certificate it requires.
    """
    from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser

    sp_entity_id = (
        cfg.saml_sp_entity_id or f"{settings.base_url}/api/auth/saml/metadata"
    )

    base_settings: dict = {
        "strict": True,
        "debug": settings.debug,
        "sp": {
            "entityId": sp_entity_id,
            "assertionConsumerService": {
                "url": f"{settings.base_url}/api/auth/saml/acs",
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
        },
        "idp": {
            "entityId": "",
            "singleSignOnService": {
                "url": "",
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "x509cert": "",
        },
        "security": {
            # When False (default), do not constrain the IdP authentication
            # method.  The python3-saml default asks for
            # PasswordProtectedTransport which rejects users authenticating
            # with X509/certificate-based MFA (Azure AD error AADSTS75011).
            # Admins can re-enable this in Settings → SSO if their IdP
            # requires an explicit RequestedAuthnContext.
            "requestedAuthnContext": cfg.saml_requested_authn_context,
        },
    }

    metadata_url = cfg.saml_idp_metadata_url
    if not metadata_url:
        logger.warning("SAML IdP metadata URL is not configured")
        return base_settings

    now = time.time()
    cached = _idp_metadata_cache.get(metadata_url)
    if cached and (now - cached[1]) < _IDP_METADATA_TTL:
        idp_data = cached[0]
    else:
        # Evict stale entries before inserting to bound cache size
        stale_keys = [
            k
            for k, (_, ts) in _idp_metadata_cache.items()
            if (now - ts) >= _IDP_METADATA_TTL
        ]
        for k in stale_keys:
            _idp_metadata_cache.pop(k, None)
        if len(_idp_metadata_cache) >= _IDP_METADATA_CACHE_MAX:
            oldest_key = min(
                _idp_metadata_cache, key=lambda k: _idp_metadata_cache[k][1]
            )
            _idp_metadata_cache.pop(oldest_key, None)

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(metadata_url)
                resp.raise_for_status()
                metadata_xml = resp.text

            idp_data = OneLogin_Saml2_IdPMetadataParser.parse(metadata_xml)
            _idp_metadata_cache[metadata_url] = (idp_data, now)
            logger.info("Fetched and cached SAML IdP metadata from %s", metadata_url)
        except Exception as e:
            logger.error(
                "Failed to fetch SAML IdP metadata from %s: %s", metadata_url, format_exception_message(e)
            )
            # Try to use stale cache if available
            if cached:
                idp_data = cached[0]
            else:
                return base_settings

    # Merge IdP data from metadata into our settings
    if "idp" in idp_data:
        base_settings["idp"] = idp_data["idp"]

    return base_settings
