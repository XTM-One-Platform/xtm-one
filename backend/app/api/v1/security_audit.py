# SPDX-License-Identifier: Apache-2.0

"""Security audit endpoint – automated configuration checking.

Performs a comprehensive review of the platform's security posture:
  - Encryption configuration
  - Stored webhook configurations whose deliveries are refused
  - Authentication settings (admin account, SSO, self-registration, local
    sign-in, MFA coverage, password and session lifetimes), and the automations
    of accounts nobody signs in to any more
  - Platform registration token (OpenCTI / OpenAEV / OpenCRQ)
  - Integration credential health, OAuth token URL strict checks and their
    exceptions
  - Slack events endpoint signature verification
  - Agent guardrail coverage
  - Tool approvals, and the bots, platforms, users and whitelist additions
    that let tool calls run unasked
  - Tool deny list usage
  - Integration-event triggers whose events their acting user may not receive
  - Personal credentials other people act with, and through what
  - Reads with no account that keep the company-managed tier (restricted reach):
    ownerless company-managed assignments, the global MCP key, ownerless evaluations
  - Custom tool sandbox isolation, the code interpreter, re-enabled modules
  - Tool-call budget enforcement
  - Content security policy (mode, violations reported, frame ancestors)
  - Channel bot security settings, and whether the Teams Graph notification
    endpoint acts only on notifications that prove their subscription
  - Client addresses behind an undeclared reverse proxy
  - MCP dynamic client registration, the XTM One MCP server, A2A exceptions
  - LLM provider configuration and trace payloads
  - Redis connectivity, fail modes, debug mode, CORS and reverse proxy trust
    (TRUST_PROXY_HEADERS included), whether runner enrollment and the MCP
    token endpoint refuse an unconfirmed transport, and how the per-IP rate
    limits key an IPv6 client

Admin-only endpoint.  Returns a structured report with findings
categorised by severity (critical, warning, info, pass), and every security
control with its state (enforced, monitoring, off). The contract the checks
follow - literal ids, one category taxonomy, one severity rubric, ``control=``
on a finding about a switch, no silent pass - is stated in
``app/core/security_controls.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, Text, and_, case, cast, exists, func, literal, literal_column, not_, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.api.v1.webhooks import (
    UnreadableWebhookConfigError,
    decrypt_webhook_config,
    hubspot_webhook_secret,
    provider_webhook_unverified_reason,
    reveal_webhook_client_secret,
)
from app.core.config import settings
from app.core.csp import permissive_frame_ancestor_sources
from app.core.database import get_db
from app.core.email_domains import parse_allowed_domains
from app.core.json_sql import json_unparseable, jsonb_has_key, when_json_parseable
from app.core.proxy_headers import effective_forwarded_allow_ips, trusted_proxy_entries
from app.core.redis import redis_client
from app.core.registration_token import (
    REGISTRATION_TOKEN_GENERATE_COMMAND,
    registration_token_state,
)
from app.core.secret_refs import holds_secret_reference
from app.core.security import UnusableSecretError, configured_secret, decrypt_stored_value
from app.core.security_controls import (
    UNREADABLE_ROW,
    configured_custom_tool_sandbox,
    control_statuses,
    controls_summary,
    stored_value,
)
from app.core.security_options import (
    SECURITY_OPTIONS,
    ignored_environment_value,
    is_pinned,
    option_reference,
    security_option,
    where_to_set,
)
from app.core.undeclared_proxy import (
    CLIENT_ADDRESSES_DOCS_URL,
    FORWARDED_FOR,
    HEADER_LABELS,
    Sighting,
    believed_from_any_peer,
    forwarded_allow_ips,
    parse_forwarded_allow_ips,
    proxy_declared,
    sighting_stands,
    undeclared_proxy_seen,
)
from app.core.utils import format_exception_message
from app.models.a2a import A2AClientTarget, A2AExposure
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.api_key import UserApiKey
from app.models.assignment import (
    EVENT_MATCHING_EXACT,
    EVENT_MATCHING_KEY,
    EVENT_MATCHING_TRIGGER_TYPES,
    LEGACY_TRIGGER_EVENT_KEY,
    TRIGGER_EVENT_KEY,
    Assignment,
)
from app.models.audit_log import AuditLog
from app.models.channel_bot import ChannelBot
from app.models.custom_tool import CustomTool
from app.models.evaluation import Evaluation
from app.models.flow import FLOW_STATUS_ACTIVE, Flow
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase
from app.models.mcp_server import MCPServer
from app.models.oauth_client import OAuthClient
from app.models.oauth_default import OAuthDefault
from app.models.platform_registration import PlatformRegistration
from app.models.platform_settings import PlatformSettings
from app.models.prompt import Prompt
from app.models.runner import PERMISSION_MODE_FULL_CONTROL, Runner
from app.models.security_audit_finding_state import DISMISSAL_REASON_MAX_CHARS, FINDING_ID_MAX_CHARS
from app.models.sso_config import SSOConfig
from app.models.template_container import TemplateContainer
from app.models.tool_approval import HitlWhitelistOverride
from app.models.user import User
from app.models.variable import Variable
from app.services import api_key_access
from app.services import security_audit_finding_states as finding_states
from app.services._sandbox_utils import ALWAYS_BLOCKED_MODULES
from app.services.assignment_pause import owner_not_deactivated
from app.services.csp_reports import RecentViolations, recent_violations
from app.services.hitl_whitelist import ACTION_ADD
from app.services.integration_notification_auth import (
    NO_CLIENT_SECRET,
    NO_OAUTH_DEFAULT,
    NO_WEBHOOK_SECRET,
    PROVIDER_WEBHOOK_PATH,
    SLACK_NOTIFICATION_PATH,
    SLACK_SIGNATURE_SETUP_HINT,
    load_slack_signing_secrets,
)
from app.services.output_schema_validation import references_another_document
from app.services.registered_platform_origins import registered_platforms_may_frame
from app.services.runners import RUNNER_INLINE_TEXT_BYTES
from app.services.schema_guard import REF_KEYWORDS
from app.services.tool_classification import infer_tool_type
from app.tools.approval import platform_gates_tools

logger = logging.getLogger(__name__)

router = APIRouter()

# The client retries a timed-out command (three times, with backoff), so its
# socket timeouts alone would let a Redis that never answers hold the audit
# for tens of seconds.
_REDIS_PING_TIMEOUT_SECONDS = 5.0


# ─── Schemas ──────────────────────────────────────────────────────────


class FindingDismissal(BaseModel):
    at: str | None = None  # None on the dismissal a re-opening cleared
    severity: str  # the severity it was dismissed at
    reason: str | None = None
    by_user_id: str | None = None
    by: str | None = None  # the administrator's name; None once the account is gone


class FindingReopening(BaseModel):
    at: str | None = None  # None until the next hourly run or dismiss / restore records it
    severity: str  # the severity that re-opened it
    dismissal: FindingDismissal | None = None


class AuditFinding(BaseModel):
    # ``<category>.<name>``, the same from one run to the next: titles embed
    # counts, so they cannot identify a finding. Every finding built in this
    # module names one (tests/test_security_audit_finding_ids.py); optional so
    # a check that does not set it still validates.
    id: str | None = None
    category: str
    severity: str  # "critical" | "warning" | "info" | "pass"
    title: str
    description: str
    recommendation: str | None = None
    # The id of the control the finding reports on, a registered one
    # (tests/test_security_controls.py).
    control: str | None = None
    # An administrator accepted it: out of every count and the score.
    dismissed: FindingDismissal | None = None
    # A dismissal the finding escalated past: it counts again.
    reopened: FindingReopening | None = None


class FindingDismissalRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=DISMISSAL_REASON_MAX_CHARS)


class SecurityControlStatus(BaseModel):
    id: str
    category: str
    title: str
    setting: str  # the environment variable, or the stored column(s)
    stored_in: str  # "config" | "platform_settings" | "sso_config" | "agents"
    where: str
    value: str
    default: str
    hardened: str
    state: str  # "enforced" | "monitoring" | "off"
    opt_in: bool
    impact: str
    evidence: str
    # The platform mode forces the value (in XTM One, quotas always fail
    # closed): there is nothing to change.
    pinned: bool = False


class SecurityAuditReport(BaseModel):
    timestamp: str
    overall_score: str  # "healthy" | "needs_attention" | "at_risk"
    findings: list[AuditFinding]
    summary: dict[str, int]  # severity → count, dismissed findings excluded
    controls: list[SecurityControlStatus] = Field(default_factory=list)
    controls_summary: dict[str, int] = Field(default_factory=dict)  # state → count
    dismissed_count: int = 0


async def _load_row(db: AsyncSession, model: type[PlatformSettings] | type[SSOConfig]) -> object | None:
    """The singleton row of *model*: ``None`` before one exists, ``UNREADABLE_ROW`` when it cannot be read.

    Read in a savepoint, so a failed read (a column a rolling upgrade has not
    added yet) leaves the session usable by every other check.
    """
    try:
        async with db.begin_nested():
            return (await db.execute(select(model).limit(1))).scalar_one_or_none()
    except Exception as exc:
        logger.warning(
            "Security audit: cannot read %s: %s", model.__tablename__, format_exception_message(exc)
        )
        return UNREADABLE_ROW


# ─── Audit checks ────────────────────────────────────────────────────


async def _check_encryption(findings: list[AuditFinding]) -> None:
    """Check that Fernet encryption is properly configured."""
    secret_key = settings.secret_key
    if not secret_key or len(secret_key) < 32:
        findings.append(
            AuditFinding(
                id="encryption.secret_key_weak",
                category="encryption",
                severity="critical",
                title="Weak or missing secret key",
                description="The SECRET_KEY environment variable is too short or missing. All encrypted data is at risk.",
                recommendation="Set SECRET_KEY to a strong random string of at least 32 characters.",
            )
        )
    else:
        findings.append(
            AuditFinding(
                id="encryption.secret_key_configured",
                category="encryption",
                severity="pass",
                title="Secret key configured",
                description="SECRET_KEY is set and meets minimum length requirements.",
            )
        )

    # Check for default/placeholder secret key
    if secret_key and secret_key in (
        "changeme",
        "secret",
        "your-secret-key",
        "change-me",
    ):
        findings.append(
            AuditFinding(
                id="encryption.secret_key_default",
                category="encryption",
                severity="critical",
                title="Default secret key detected",
                description="The SECRET_KEY appears to be a default/placeholder value. All encrypted data is effectively unprotected.",
                recommendation="Replace with a randomly generated secret key.",
            )
        )


#: How many configurations a finding names; the others are counted.
_CONFIG_NAMES_SHOWN = 5


@dataclass(frozen=True)
class _StoredWebhookConfig:
    """A stored configuration a webhook gate reads a delivery's secret out of."""

    #: How a finding names it.
    label: str
    extra_config_enc: str | None
    #: A HubSpot integration: verified with the app's client secret first.
    hubspot: bool = False
    #: The client secret the provider route decrypts too (LinkedIn), if any.
    client_secret_enc: str | None = None


def _provider_name(provider: str) -> str:
    return str(PROVIDER_CATALOG.get(provider, {}).get("name") or provider)


async def _stored_webhook_configs(db: AsyncSession) -> list[_StoredWebhookConfig]:
    """Every stored configuration a webhook gate reads, OAuth defaults first.

    The provider route reads an OAuth default's ``extra_config`` and, for
    LinkedIn, the client secret LinkedIn signs with
    (``_verify_linkedin_provider_signature``). The integration routes read an
    integration's ``extra_config``, HubSpot's client secret first
    (``_hubspot_secret``).
    """
    defaults = (
        await db.execute(
            select(OAuthDefault.provider, OAuthDefault.extra_config_enc, OAuthDefault.client_secret_enc).order_by(
                OAuthDefault.provider
            )
        )
    ).all()
    integrations = (
        await db.execute(
            select(Integration.id, Integration.name, Integration.provider, Integration.extra_config_enc)
            .where(Integration.extra_config_enc.is_not(None))
            .order_by(Integration.name, Integration.id)
        )
    ).all()
    return [
        *(
            _StoredWebhookConfig(
                label=f"{_provider_name(row.provider)} OAuth default",
                extra_config_enc=row.extra_config_enc,
                client_secret_enc=row.client_secret_enc if row.provider == "linkedin" else None,
            )
            for row in defaults
        ),
        *(
            _StoredWebhookConfig(
                label=f"{row.name} ({_provider_name(row.provider)}, {row.id})",
                extra_config_enc=row.extra_config_enc,
                hubspot=row.provider == "hubspot",
            )
            for row in integrations
        ),
    ]


def _listed(labels: list[str]) -> str:
    """*labels* for a finding: the first few named, the rest counted."""
    shown = ", ".join(labels[:_CONFIG_NAMES_SHOWN])
    hidden = len(labels) - _CONFIG_NAMES_SHOWN
    return f"{shown} and {hidden} more" if hidden > 0 else shown


def _refused_webhook_configs(configs: list[_StoredWebhookConfig]) -> tuple[list[str], list[str], list[str]]:
    """The labels of the configurations the webhook gates cannot read, of those whose secret is not text,
    and of those whose client secret is read from a vault (checked by :func:`_unreadable_vault_client_secrets`).

    Decrypts each stored value once: CPU-bound. A configuration whose
    ``extra_config`` cannot be read is never checked for its secret.
    """
    unreadable: list[str] = []
    unusable: list[str] = []
    referenced: list[str] = []
    for config in configs:
        try:
            extra = decrypt_webhook_config(config.extra_config_enc)
        except UnreadableWebhookConfigError:
            unreadable.append(config.label)
            continue
        if config.client_secret_enc:
            try:
                stored = decrypt_stored_value(config.client_secret_enc)
            except Exception:
                unreadable.append(config.label)
            else:
                if holds_secret_reference(stored):
                    referenced.append(config.label)
        try:
            if config.hubspot:
                hubspot_webhook_secret(extra)
            else:
                configured_secret(extra.get("webhook_secret"))
        except UnusableSecretError:
            unusable.append(config.label)
    return unreadable, unusable, referenced


async def _unreadable_vault_client_secrets(
    configs: list[_StoredWebhookConfig], referenced: list[str]
) -> list[str]:
    """The labels of *referenced* configurations whose client secret its vault does not give.

    The gate's own read (``reveal_webhook_client_secret``): what the audit names
    unreadable is what a delivery is refused on.
    """
    unreadable: list[str] = []
    for config in configs:
        if config.label not in referenced or not config.client_secret_enc:
            continue
        try:
            await reveal_webhook_client_secret(config.client_secret_enc)
        except UnreadableWebhookConfigError:
            unreadable.append(config.label)
    return unreadable


async def _check_webhook_configs(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """Stored webhook configurations whose deliveries the gates refuse, loaded and decrypted once."""
    configs = await _stored_webhook_configs(db)
    if not configs:
        return
    unreadable, unusable, referenced = await asyncio.to_thread(_refused_webhook_configs, configs)
    if referenced:
        unreadable += await _unreadable_vault_client_secrets(configs, referenced)
    if unreadable:
        findings.append(_unreadable_webhook_configs_finding(unreadable))
    else:
        findings.append(
            AuditFinding(
                id="encryption.webhook_configs_readable",
                category="encryption",
                severity="pass",
                title=f"All {len(configs)} stored webhook configuration(s) can be read",
                description=(
                    "The current SECRET_KEY decrypts every stored configuration the webhook "
                    "gates read, integrations and OAuth defaults alike."
                ),
            )
        )
    if unusable:
        findings.append(_unusable_webhook_secrets_finding(unusable))


#: Where an administrator connects, grants and tests vault providers, as a finding names it.
VAULTS_PATH = "Settings > Access and security > Secret vaults"


async def _check_secret_vaults(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """The vault providers and the secrets read from them; nothing when neither is configured."""
    from app.services.vault.audit import gather_vault_facts

    facts = await gather_vault_facts(db)
    if not (
        facts.providers
        or facts.refused
        or facts.references
        or facts.invalid_references
        or facts.shadowed
        or facts.failing
    ):
        return
    if facts.refused:
        findings.append(
            AuditFinding(
                id="encryption.vault_provider_refused",
                category="encryption",
                severity="warning",
                title=f"{len(facts.refused)} vault provider(s) cannot be used",
                description=(
                    "These providers (a SECRET_VAULT_<NAME>_* declaration, or a provider connected in "
                    f"{VAULTS_PATH}) are refused, so every secret read from them fails: "
                    f"{_listed([f'{name}: {why}' for name, why in facts.refused.items()])}."
                ),
                recommendation=(
                    "Fix what each line names: the variable, then restart, for a declaration; the "
                    f"provider's settings in {VAULTS_PATH} for one connected there."
                ),
            )
        )
    if facts.shadowed:
        findings.append(
            AuditFinding(
                id="encryption.vault_provider_shadowed",
                category="encryption",
                severity="warning",
                title=f"{len(facts.shadowed)} vault provider(s) connected in the UI are hidden by the environment",
                description=(
                    "An environment declaration of the same name takes precedence, so the settings and the "
                    f"grants of these providers have no effect: {_listed(facts.shadowed)}."
                ),
                recommendation=(
                    "Remove the SECRET_VAULT_<NAME>_* declaration and restart to use the provider "
                    f"connected in {VAULTS_PATH}, or delete that provider."
                ),
            )
        )
    if facts.failing:
        findings.append(
            AuditFinding(
                id="encryption.vault_provider_failing",
                category="encryption",
                severity="warning",
                title=f"{len(facts.failing)} vault provider(s) failed their last connection test",
                description=(
                    "Secrets read from these providers may fail until the vault accepts the platform again: "
                    f"{_listed(facts.failing)}."
                ),
                recommendation=f"Open the provider in {VAULTS_PATH}, fix its settings and run Test connection.",
            )
        )
    if facts.static_keys:
        findings.append(
            AuditFinding(
                id="encryption.vault_provider_static_key",
                category="encryption",
                severity="info",
                title=f"{len(facts.static_keys)} vault provider(s) sign in with a stored cloud key",
                description=(
                    "These providers keep a long-lived cloud credential in the platform's database, "
                    f"encrypted, instead of using the workload's identity: {_listed(facts.static_keys)}."
                ),
                recommendation=(
                    "Where the platform runs with a cloud identity (IRSA, GKE Workload Identity, an Azure "
                    "managed identity), switch the provider to it and rotate the stored key."
                ),
            )
        )
    if facts.unused:
        findings.append(
            AuditFinding(
                id="encryption.vault_provider_unused",
                category="encryption",
                severity="info",
                title=f"{len(facts.unused)} vault provider(s) are referenced by no secret field",
                description=(
                    "These providers are connected and enabled but no secret field reads from them: "
                    f"{_listed(facts.unused)}."
                ),
                recommendation=f"Disable or delete a provider nobody needs in {VAULTS_PATH}.",
            )
        )
    if facts.plain_http:
        findings.append(
            AuditFinding(
                id="encryption.vault_provider_plain_http",
                category="encryption",
                severity="warning",
                title=f"{len(facts.plain_http)} vault provider(s) reached over plain HTTP",
                description=(
                    "The secrets these providers hand over, and the tokens the platform presents to "
                    f"them, cross the network unencrypted: {_listed(facts.plain_http)}."
                ),
                recommendation=(
                    "Give the provider an https:// address: its address in "
                    f"{VAULTS_PATH}, or SECRET_VAULT_<NAME>_ADDRESS for HashiCorp, "
                    "_VAULT_URL for Azure, _ENDPOINT_URL for an AWS endpoint."
                ),
            )
        )
    if facts.open_writers:
        findings.append(
            AuditFinding(
                id="encryption.vault_provider_open_writers",
                category="encryption",
                severity="warning",
                title=f"{len(facts.open_writers)} vault provider(s) let any user reference any secret",
                description=(
                    "Open to everyone with no path restriction (WRITERS=all with no PATHS, or a grant to "
                    "everyone with no path prefix): anyone who may edit an integration, an MCP server or a "
                    "secret variable can reference any secret the platform's own vault identity can read, and "
                    f"have it sent where that entity points: {_listed(facts.open_writers)}."
                ),
                recommendation=(
                    f"Give the grant path prefixes, or grant the provider to groups, in {VAULTS_PATH}; for a "
                    "declaration, set SECRET_VAULT_<NAME>_PATHS or SECRET_VAULT_<NAME>_WRITERS=admins."
                ),
            )
        )
    if facts.unknown_provider:
        findings.append(
            AuditFinding(
                id="encryption.vault_reference_unknown_provider",
                category="encryption",
                severity="warning",
                title=f"{len(facts.unknown_provider)} stored secret(s) reference an undeclared vault provider",
                description=(
                    "Every use of these secrets fails until the provider they name is declared: "
                    f"{_listed(facts.unknown_provider)}."
                ),
                recommendation=(
                    f"Connect a provider of that name in {VAULTS_PATH} (or declare it with "
                    "SECRET_VAULT_<NAME>_TYPE and its settings), or pick the secret again from a provider "
                    "that exists."
                ),
            )
        )
    if facts.invalid_references:
        findings.append(
            AuditFinding(
                id="encryption.vault_reference_invalid",
                category="encryption",
                severity="warning",
                title=f"{len(facts.invalid_references)} stored secret(s) hold a malformed vault reference",
                description=(
                    "These secret fields hold a value starting with vault:// that is not a valid "
                    "vault://<provider>/<path>#<key> reference, so every use of them fails: "
                    f"{_listed(facts.invalid_references)}."
                ),
                recommendation=(
                    "Replace each field with a valid reference to a declared provider, or with the secret "
                    "itself when it does not start with vault://."
                ),
            )
        )
    if facts.failures:
        findings.append(
            AuditFinding(
                id="encryption.vault_reference_failing",
                category="encryption",
                severity="warning",
                title=f"{len(facts.failures)} secret(s) could not be read from their vault lately",
                description=(
                    "The features using these secrets failed with a generic error; the server log "
                    f"holds each detail: {_listed(facts.failures)}."
                ),
                recommendation=(
                    "Check the reference and the provider's access policy for the platform's "
                    "identity; a fixed secret is used again on its next read."
                ),
            )
        )
    # No pass is claimed over a history that could not be read, or that is full
    # (an older failure may have left the index) with none of it current.
    failures_unknown = bool(facts.references) and (
        facts.failures is None or (facts.failures_truncated and not facts.failures)
    )
    if failures_unknown:
        findings.append(
            AuditFinding(
                id="encryption.vault_failures_unknown",
                category="encryption",
                severity="info",
                title="Recent vault read failures could not be checked",
                description=(
                    "Redis did not answer, so the record of failed vault reads could not be read."
                    if facts.failures is None
                    else "The record of failed vault reads is full, so an older failure of a stored reference "
                    "may not be listed."
                ),
            )
        )
    if not (
        facts.refused
        or facts.shadowed
        or facts.failing
        or facts.plain_http
        or facts.open_writers
        or facts.unknown_provider
        or facts.invalid_references
        or facts.failures
        or failures_unknown
    ):
        providers = _listed([f"{name} ({details.get('type')})" for name, details in facts.providers.items()])
        findings.append(
            AuditFinding(
                id="encryption.vault_references_resolving",
                category="encryption",
                severity="pass",
                title=f"{len(facts.references)} secret(s) read from vault providers",
                description=(
                    f"Declared vault providers: {providers or 'none'}. Every stored reference names a "
                    "declared provider and none failed lately; the values live in memory only."
                ),
            )
        )


def _unreadable_webhook_configs_finding(unreadable: list[str]) -> AuditFinding:
    """Stored webhook configurations the current SECRET_KEY cannot read.

    One that cannot be read may hold a secret, so the webhook gates refuse
    every delivery to it with a 503 until it can be read again.
    """
    return AuditFinding(
        id="encryption.webhook_config_unreadable",
        category="encryption",
        severity="warning",
        title=f"{len(unreadable)} stored webhook configuration(s) cannot be read",
        description=(
            "The current SECRET_KEY cannot decrypt these stored configurations, or they do "
            f"not hold a JSON object once decrypted: {_listed(unreadable)}. Every webhook "
            "delivery to them is refused (HTTP 503) until the secret or configuration is "
            "saved again with the current key."
        ),
        recommendation=(
            "Restore the SECRET_KEY these configurations were encrypted with, or save each "
            "one again: re-enter the webhook secret in the integration's settings, and an "
            "OAuth default with its client secret in Settings > Integrations > OAuth Defaults."
        ),
    )


def _unusable_webhook_secrets_finding(unusable: list[str]) -> AuditFinding:
    """Webhook configurations whose secret is not text.

    No sender can sign with a JSON boolean, float, list or object, so the
    webhook gates refuse every delivery to such a configuration with a 401.
    """
    return AuditFinding(
        id="integrations.webhook_secret_not_text",
        category="integrations",
        severity="warning",
        title=f"{len(unusable)} webhook configuration(s) with a secret that is not text",
        description=(
            "The webhook secret (or HubSpot client secret) of these integrations or OAuth "
            "defaults is a JSON value no sender can sign with, such as a boolean, a float, a "
            f"list or an object: {_listed(unusable)}. Every webhook delivery to them is "
            "refused (HTTP 401) until the value is replaced by a string."
        ),
        recommendation=(
            "Re-enter the secret as text in the integration's settings, or for an OAuth "
            "default in Settings > Integrations > OAuth Defaults."
        ),
    )


async def _check_authentication(db: AsyncSession, findings: list[AuditFinding], sso: object | None) -> None:
    """Check authentication configuration."""
    # Check admin email is configured
    if not settings.admin_email:
        findings.append(
            AuditFinding(
                id="authentication.admin_email_missing",
                category="authentication",
                severity="warning",
                title="No admin email configured",
                description="ADMIN_EMAIL is not set. The first registered user becomes admin by default.",
                recommendation="Set ADMIN_EMAIL to a specific administrator email.",
            )
        )
    else:
        findings.append(
            AuditFinding(
                id="authentication.admin_email_configured",
                category="authentication",
                severity="pass",
                title="Admin email configured",
                description=f"Platform admin is set to {settings.admin_email}.",
            )
        )

    # Check for users with weak or no passwords
    users_no_pw = (
        await db.execute(
            select(func.count(User.id)).where(
                User.hashed_password.is_(None),
                User.sso_provider.is_(None),
                User.is_active.is_(True),
            )
        )
    ).scalar() or 0
    if users_no_pw > 0:
        findings.append(
            AuditFinding(
                id="authentication.users_without_credentials",
                category="authentication",
                severity="warning",
                title=f"{users_no_pw} active user(s) without password or SSO",
                description="These users cannot log in through any authentication method.",
                recommendation="Ensure all active users have either a password set or SSO provider configured.",
            )
        )
    else:
        findings.append(
            AuditFinding(
                id="authentication.users_have_credentials",
                category="authentication",
                severity="pass",
                title="Every active user has a password or an SSO identity",
                description="No active account is left without a way to sign in.",
            )
        )

    # Check for SSO configuration
    if sso is UNREADABLE_ROW:
        findings.append(
            AuditFinding(
                id="authentication.sso_check_skipped",
                category="authentication",
                severity="info",
                title="SSO configuration check skipped",
                description="Could not verify SSO configuration.",
            )
        )
        return
    oidc = bool(stored_value(sso, "sso_config", "oidc_enabled"))
    saml = bool(stored_value(sso, "sso_config", "saml_enabled"))
    if oidc or saml:
        findings.append(
            AuditFinding(
                id="authentication.sso_enabled",
                category="authentication",
                severity="pass",
                title="SSO enabled",
                description=f"SSO is configured (OIDC: {oidc}, SAML: {saml}).",
            )
        )
    else:
        findings.append(
            AuditFinding(
                id="authentication.sso_not_configured",
                category="authentication",
                severity="info",
                title="SSO not configured",
                description="No SSO provider (OIDC or SAML) is enabled. Users authenticate with local passwords only.",
                recommendation="Consider enabling SSO for stronger authentication and centralized user management.",
            )
        )


_SSO_METHODS = (("oidc", "OIDC"), ("saml", "SAML"))
_SSO_SETTINGS_PAGE = "Settings > Access and security > Authentication"


def _joined(labels: list[str]) -> str:
    return " and ".join(labels)


async def _check_sso(findings: list[AuditFinding], sso: object | None) -> None:
    """Who an SSO sign-in lets in, and with which role (``auth_sso._get_or_create_sso_user``)."""
    if sso is UNREADABLE_ROW:
        return
    enabled = [(key, label) for key, label in _SSO_METHODS if stored_value(sso, "sso_config", f"{key}_enabled")]
    if not enabled:
        return
    # XTM_HUB_ADMIN_GROUP: the XTM Hub admits an OIDC sign-in and sets its role both ways.
    hub = bool((settings.xtm_hub_admin_group or "").strip())
    hub_note = (
        " The XTM Hub decides who signs in through OIDC, and with which role (XTM_HUB_ADMIN_GROUP)."
        if hub and any(key == "oidc" for key, _label in enabled)
        else ""
    )
    decided_here = [(key, label) for key, label in enabled if not (key == "oidc" and hub)]
    creates = [label for key, label in decided_here if stored_value(sso, "sso_config", f"{key}_auto_create_users")]
    promotes = [label for key, label in decided_here if stored_value(sso, "sso_config", f"{key}_auto_admin_new_users")]
    # The allowed email domains restrict the accounts SSO creates, as they restrict self-registration,
    # including those of an OIDC sign-in the XTM Hub admits.
    domains = parse_allowed_domains(stored_value(sso, "sso_config", "allowed_domains"))
    creates_for_anyone = [] if domains else creates
    creates_at_domains = (
        [label for key, label in enabled if stored_value(sso, "sso_config", f"{key}_auto_create_users")]
        if domains
        else []
    )
    anyone_admin = [label for label in promotes if label in creates_for_anyone]
    domain_admin = [label for label in promotes if label in creates] if domains else []

    if promotes:
        if anyone_admin:
            who_signs_in = (
                f"{_joined(anyone_admin)} creates an account on the fly for everyone the identity "
                "provider authenticates and makes each of them an administrator: whoever can sign "
                "in at the identity provider administers this platform. "
            )
        elif domain_admin:
            who_signs_in = (
                f"On-the-fly user creation is on for {_joined(domain_admin)}, for an email address at "
                f"{_listed(domains)} only, and everyone who signs in through {_joined(promotes)} is made "
                "an administrator. "
            )
        else:
            who_signs_in = (
                f"On-the-fly user creation is off for {_joined(promotes)}, so only provisioned "
                "accounts sign in, but each of them is made an administrator. "
            )
        findings.append(
            AuditFinding(
                id="authentication.sso_auto_admin_on",
                category="authentication",
                severity="critical" if anyone_admin else "warning",
                title=(
                    f"Anyone who signs in through {_joined(anyone_admin)} becomes an administrator"
                    if anyone_admin
                    else f"Everyone who signs in through {_joined(promotes)} becomes an administrator"
                ),
                description=(
                    who_signs_in
                    + "The promotion happens at every sign-in, existing accounts included, whatever their "
                    "email domain."
                ),
                recommendation=(
                    f"Turn off Automatically admin users in {_SSO_SETTINGS_PAGE} and grant the admin role "
                    "by hand. Turning it off demotes nobody: review the administrators on the Users page."
                ),
                control="authentication.sso_auto_admin",
            )
        )
    else:
        findings.append(
            AuditFinding(
                id="authentication.sso_roles_kept",
                category="authentication",
                severity="pass",
                title="SSO sign-ins keep their role",
                description=f"No SSO method makes the people who sign in administrators.{hub_note}",
                control="authentication.sso_auto_admin",
            )
        )

    if creates_for_anyone:
        findings.append(
            AuditFinding(
                id="authentication.sso_open_account_creation",
                category="authentication",
                severity="warning",
                title=(
                    "SSO creates an account for everyone the identity provider authenticates "
                    f"({_joined(creates_for_anyone)})"
                ),
                description=(
                    f"On-the-fly user creation is on for {_joined(creates_for_anyone)} with no allowed email "
                    "domain: whoever the identity provider lets sign in to this application gets an account "
                    "at first sign-in."
                ),
                recommendation=(
                    f"Set Allowed email domains in {_SSO_SETTINGS_PAGE} to your own domains, so that SSO creates "
                    "accounts for those addresses only; make sure the identity provider only lets the intended "
                    "people sign in to this application (user assignment required, a single tenant); or turn "
                    "off On-the-fly user creation and create each account first, by invitation or from the "
                    "Users page."
                ),
                control="authentication.sso_account_creation",
            )
        )
    elif creates_at_domains:
        findings.append(
            AuditFinding(
                id="authentication.sso_account_creation_restricted",
                category="authentication",
                severity="pass",
                title=f"SSO creates accounts for {len(domains)} email domain(s) only",
                description=(
                    f"On-the-fly user creation is on for {_joined(creates_at_domains)}, for an email address at "
                    f"{_listed(domains)} only: an identity at another domain gets no account unless one exists "
                    "already. The domain is read from the email address the identity provider asserts."
                    f"{hub_note}"
                ),
                control="authentication.sso_account_creation",
            )
        )
    else:
        findings.append(
            AuditFinding(
                id="authentication.sso_accounts_provisioned",
                category="authentication",
                severity="pass",
                title="SSO signs in provisioned accounts only",
                description=f"An SSO sign-in needs an account that already exists.{hub_note}",
                control="authentication.sso_account_creation",
            )
        )

    if stored_value(sso, "sso_config", "local_auth_enabled"):
        findings.append(
            AuditFinding(
                id="authentication.local_sign_in_with_sso",
                category="authentication",
                severity="info",
                title="Password sign-in stays on next to SSO",
                description=(
                    "Local Authentication is on while SSO is configured: people can still sign in with a "
                    "password, which the identity provider's policies (MFA, conditional access, offboarding) "
                    "do not cover."
                ),
                recommendation=(
                    f"Once every administrator signs in through SSO, turn off Local Authentication in "
                    f"{_SSO_SETTINGS_PAGE}. With it off an identity provider outage locks everyone out, so "
                    "keep one administrator who can sign in through SSO first."
                ),
                control="authentication.local_sign_in",
            )
        )


async def _check_self_registration(findings: list[AuditFinding], sso: object | None) -> None:
    """Who can create an account from the sign-in page (``auth.register``)."""
    if sso is UNREADABLE_ROW:
        return
    # With local sign-in off, registration is refused whatever it says.
    if not stored_value(sso, "sso_config", "local_auth_enabled"):
        return
    if not stored_value(sso, "sso_config", "registration_enabled"):
        findings.append(
            AuditFinding(
                id="authentication.self_registration_off",
                category="authentication",
                severity="pass",
                title="Nobody can create an account from the sign-in page",
                description=(
                    "Allow local registration is off: accounts come from an administrator, an invitation or SSO."
                ),
                control="authentication.self_registration",
            )
        )
        return
    domains = parse_allowed_domains(stored_value(sso, "sso_config", "allowed_domains"))
    if domains:
        findings.append(
            AuditFinding(
                id="authentication.self_registration_restricted",
                category="authentication",
                severity="info",
                title=f"Self-registration is open to {len(domains)} email domain(s)",
                description=(
                    f"Anyone with an email address at {_listed(domains)} can create an account from the "
                    "sign-in page."
                ),
                recommendation=(
                    f"Turn off Allow local registration in {_SSO_SETTINGS_PAGE} if accounts should come from "
                    "an administrator, an invitation or SSO only."
                ),
                control="authentication.self_registration",
            )
        )
        return
    findings.append(
        AuditFinding(
            id="authentication.self_registration_open",
            category="authentication",
            severity="warning",
            title="Anyone can create an account",
            description=(
                "Allow local registration is on with no allowed email domain: anyone who reaches the "
                "sign-in page can create an account, with any email address."
            ),
            recommendation=(
                f"Turn off Allow local registration in {_SSO_SETTINGS_PAGE}, or restrict it to your own "
                "email domains (Allowed email domains)."
            ),
            control="authentication.self_registration",
        )
    )


async def _check_mfa(db: AsyncSession, findings: list[AuditFinding], sso: object | None) -> None:
    """Second-factor coverage of the accounts that sign in with a password (``mfa.challenge_required``)."""
    if sso is UNREADABLE_ROW:
        return
    if not stored_value(sso, "sso_config", "local_auth_enabled"):
        return  # every password sign-in is refused
    enrolled = and_(User.mfa_enabled.is_(True), User.mfa_secret.is_not(None))
    is_admin = User.role == "admin"
    row = (
        await db.execute(
            select(
                func.count(User.id),
                func.count(User.id).filter(not_(enrolled)),
                func.count(User.id).filter(is_admin),
                func.count(User.id).filter(and_(is_admin, not_(enrolled))),
            ).where(User.is_active.is_(True), User.hashed_password.is_not(None))
        )
    ).one()
    local, local_without, admins, admins_without = (int(value or 0) for value in row)
    if admins_without:
        findings.append(
            AuditFinding(
                id="authentication.admins_without_mfa",
                category="authentication",
                severity="warning",
                title=f"{admins_without} of {admins} administrator(s) sign in with a password and no second factor",
                description=(
                    "These administrators sign in with a password alone: whoever learns one of those "
                    "passwords administers the platform. Two-factor authentication is set per account."
                ),
                recommendation=(
                    "Ask each of them to turn on two-factor authentication in their profile, or to sign "
                    "in through SSO."
                ),
            )
        )
    elif admins:
        findings.append(
            AuditFinding(
                id="authentication.admins_mfa_enrolled",
                category="authentication",
                severity="pass",
                title=f"All {admins} administrator(s) who sign in with a password use a second factor",
                description="Every active administrator account with a password has two-factor authentication on.",
            )
        )
    if local_without:
        findings.append(
            AuditFinding(
                id="authentication.local_users_without_mfa",
                category="authentication",
                severity="info",
                title=f"{local_without} of {local} account(s) with a password have no second factor",
                description="These active accounts sign in with a password alone.",
                recommendation="Encourage them to turn on two-factor authentication in their profile, or to move to SSO.",
            )
        )


def _duration(minutes: int) -> str:
    if minutes % 1440 == 0:
        return f"{minutes // 1440} day(s)"
    if minutes % 60 == 0:
        return f"{minutes // 60} hour(s)"
    return f"{minutes} minute(s)"


#: The idle timeout NIST SP 800-63B sets at AAL2: a longer one, or none, is reported.
_IDLE_TIMEOUT_MINUTES_HARDENED = 30


async def _check_sessions(findings: list[AuditFinding]) -> None:
    """How long a web session lasts after its sign-in, and whether an unused one ends."""
    minutes = int(security_option("jwt_expire_minutes"))
    if minutes > 1440:
        findings.append(
            AuditFinding(
                id="authentication.session_lifetime_long",
                category="authentication",
                severity="info",
                title=f"A sign-in lasts {_duration(minutes)}",
                description=(
                    f"{option_reference('jwt_expire_minutes')} is {minutes}: a session ends that long "
                    "after its sign-in, even in use, so a stolen session token keeps working as long (a "
                    "password change ends the sessions of a local account)."
                ),
                recommendation=(
                    where_to_set("jwt_expire_minutes", "1440", sentence_start=True)
                    + " (a day), or less: the sessions already older end on their next request."
                ),
                control="authentication.session_lifetime",
            )
        )
    else:
        findings.append(
            AuditFinding(
                id="authentication.session_lifetime_bounded",
                category="authentication",
                severity="pass",
                title=f"A sign-in lasts {_duration(minutes)} at most",
                description=(
                    f"{option_reference('jwt_expire_minutes')} is {minutes}: a session ends that long "
                    "after its sign-in, even in use."
                ),
                control="authentication.session_lifetime",
            )
        )

    idle = int(security_option("session_idle_timeout_minutes"))
    unenforced_while_redis_down = (
        "While Redis cannot be reached, sessions are not ended for inactivity; their absolute lifetime "
        "still ends them."
    )
    if idle <= 0:
        findings.append(
            AuditFinding(
                id="authentication.session_idle_timeout_off",
                category="authentication",
                severity="info",
                title="Unused sessions stay open",
                description=(
                    f"{option_reference('session_idle_timeout_minutes')} is 0: a session nobody uses "
                    "stays open until its absolute lifetime ends, so a browser left signed in can be "
                    "used by whoever sits at it."
                ),
                recommendation=(
                    where_to_set("session_idle_timeout_minutes", str(_IDLE_TIMEOUT_MINUTES_HARDENED), sentence_start=True)
                    + ", or less: the web app then signs out a session nobody used for that long, and "
                    "warns a minute before."
                ),
                control="authentication.session_idle_timeout",
            )
        )
    elif idle > _IDLE_TIMEOUT_MINUTES_HARDENED:
        findings.append(
            AuditFinding(
                id="authentication.session_idle_timeout_long",
                category="authentication",
                severity="info",
                title=f"Unused sessions end after {_duration(idle)}",
                description=(
                    f"{option_reference('session_idle_timeout_minutes')} is {idle}: a session nobody "
                    f"uses ends that long after its last activity. {unenforced_while_redis_down}"
                ),
                recommendation=(
                    where_to_set("session_idle_timeout_minutes", str(_IDLE_TIMEOUT_MINUTES_HARDENED), sentence_start=True)
                    + ", or less, as NIST SP 800-63B asks at AAL2."
                ),
                control="authentication.session_idle_timeout",
            )
        )
    else:
        findings.append(
            AuditFinding(
                id="authentication.session_idle_timeout_on",
                category="authentication",
                severity="pass",
                title=f"Unused sessions end after {_duration(idle)}",
                description=(
                    f"{option_reference('session_idle_timeout_minutes')} is {idle}: a session nobody "
                    f"uses ends that long after its last activity. {unenforced_while_redis_down}"
                ),
                control="authentication.session_idle_timeout",
            )
        )


async def _check_account_lifetimes(findings: list[AuditFinding], sso: object | None) -> None:
    """Password policy and the lifetime of the links that open an account."""
    # With local sign-in off no password is used to sign in (an invitation
    # still sets one); a row the audit cannot read counts as the default, on.
    passwords_used = sso is UNREADABLE_ROW or bool(stored_value(sso, "sso_config", "local_auth_enabled"))
    if passwords_used:
        length = int(security_option("min_password_length"))
        if length < 12:
            findings.append(
                AuditFinding(
                    id="authentication.password_length_short",
                    category="authentication",
                    severity="warning" if length < 8 else "info",
                    title=f"Passwords need only {length} character(s)",
                    description=(
                        f"{option_reference('min_password_length')} is {length}: a local password needs "
                        "that many characters, with a letter and a digit. OWASP ASVS asks for at least 12"
                        + (", and NIST SP 800-63B sets 8 as the floor." if length < 8 else ".")
                    ),
                    recommendation=(
                        where_to_set("min_password_length", "12", sentence_start=True)
                        + ", or higher: passwords set from then on need it, existing ones keep working "
                        "until they change."
                    ),
                    control="authentication.min_password_length",
                )
            )
        else:
            findings.append(
                AuditFinding(
                    id="authentication.password_length_sufficient",
                    category="authentication",
                    severity="pass",
                    title=f"Passwords need at least {length} characters",
                    description=(
                        f"{option_reference('min_password_length')} meets the 12 characters OWASP ASVS asks for."
                    ),
                    control="authentication.min_password_length",
                )
            )
        reset_hours = int(security_option("password_reset_expire_hours"))
        if reset_hours > 1:
            findings.append(
                AuditFinding(
                    id="authentication.password_reset_link_long",
                    category="authentication",
                    severity="info",
                    title=f"Password reset links stay valid for {_duration(reset_hours * 60)}",
                    description=(
                        f"{option_reference('password_reset_expire_hours')} is {reset_hours}: a reset link "
                        "left in a mailbox works that long."
                    ),
                    recommendation=where_to_set("password_reset_expire_hours", "1", sentence_start=True) + ".",
                    control="authentication.password_reset_expiry",
                )
            )

    invitation_hours = int(security_option("invitation_expire_hours"))
    # A week, the default: an invitation is admin-issued, bound to one email and single-use.
    if invitation_hours > 168:
        findings.append(
            AuditFinding(
                id="authentication.invitation_link_long",
                category="authentication",
                severity="info",
                title=f"Invitation links stay valid for {_duration(invitation_hours * 60)}",
                description=(
                    f"{option_reference('invitation_expire_hours')} is {invitation_hours}: an invitation "
                    "link, which opens an account with the role it carries, works that long unless the "
                    "administrator who creates it picks a shorter lifetime."
                ),
                recommendation=(
                    where_to_set("invitation_expire_hours", "168", sentence_start=True)
                    + ", or less: a week is the default."
                ),
                control="authentication.invitation_expiry",
            )
        )


#: An account nobody signed in to for this long whose automations still run is reported.
DORMANT_ACCOUNT_DAYS = 30


def _completed_sign_in() -> ColumnElement[bool]:
    """An audit row of a completed sign-in: every sign-in path records ``login`` on the user,
    the password step of a sign-in awaiting its second factor records it on ``mfa``, and a
    declined XTM One Desktop request records ``login_declined``.

    The predicate of ``ix_audit_logs_sign_ins``, so its values are written into the SQL,
    never bound: a plan can only use a partial index whose predicate it sees."""
    return and_(
        AuditLog.action == literal("login", literal_execute=True),
        AuditLog.entity_type == literal("user", literal_execute=True),
    )


def _dormant_account_line(
    name: str, email: str, created_at: datetime, last_sign_in: datetime | None, assignments: int, flows: int
) -> str:
    owned = [f"{count} {noun}(s)" for count, noun in ((assignments, "assignment"), (flows, "flow")) if count]
    since = (
        f"last sign-in {last_sign_in.date().isoformat()}"
        if last_sign_in
        else f"no sign-in recorded since the account was created on {created_at.date().isoformat()}"
    )
    who = email if not name or name == email else f"{name} ({email})"
    return f"{who}: {since}, {' and '.join(owned)}"


async def _check_dormant_account_automations(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """Automations that run as an account nobody signed in to for ``DORMANT_ACCOUNT_DAYS``.

    Deactivating an account pauses what it owns unless it is company-managed
    (``assignment_pause.owner_not_deactivated``). Removing someone
    from the identity provider only stops their sign-in: the account stays
    active, and so do their assignments and flows, with their identity and
    integrations. Sessions are not refreshed (the absolute session lifetime,
    ``JWT_EXPIRE_MINUTES``, 7 days by default), so whoever uses the web
    app signs in within the window unless that lifetime is raised past it; API keys
    and the chat embedded in OpenCTI / OpenAEV never sign in. A newer account is
    not reported before it is ``DORMANT_ACCOUNT_DAYS`` old.
    """
    assignments_owned = (
        select(Assignment.user_id)
        .join(Agent, Agent.id == Assignment.agent_id)
        .where(
            Assignment.enabled.is_(True),
            Agent.enabled.is_(True),
            Assignment.is_company_managed.is_(False),
            Assignment.user_id.is_not(None),
        )
    )
    flows_owned = select(Flow.user_id).where(
        Flow.status == FLOW_STATUS_ACTIVE,
        Flow.is_company_managed.is_(False),
        Flow.user_id.is_not(None),
    )
    cutoff = datetime.now(UTC) - timedelta(days=DORMANT_ACCOUNT_DAYS)
    signed_in_since_cutoff = exists().where(
        AuditLog.user_id == User.id, _completed_sign_in(), AuditLog.created_at >= cutoff
    )
    owners = (
        await db.execute(
            select(
                User.id,
                User.full_name,
                User.email,
                User.created_at,
                and_(User.created_at < cutoff, ~signed_in_since_cutoff).label("dormant"),
            ).where(
                User.is_active.is_(True),
                or_(User.id.in_(assignments_owned), User.id.in_(flows_owned)),
            )
        )
    ).all()
    if not owners:
        return
    dormant = [owner for owner in owners if owner.dormant]
    if not dormant:
        findings.append(
            AuditFinding(
                id="authentication.automation_owners_signed_in",
                category="authentication",
                severity="pass",
                title=f"No active automation belongs to an account nobody signed in to for {DORMANT_ACCOUNT_DAYS} days",
                description=(
                    f"{len(owners)} account(s) own assignments or flows that are not company-managed; each "
                    f"signed in within the last {DORMANT_ACCOUNT_DAYS} days or was created since."
                ),
            )
        )
        return

    dormant_ids = [owner.id for owner in dormant]
    assignment_counts = dict(
        (
            await db.execute(
                select(Assignment.user_id, func.count())
                .where(Assignment.id.in_(assignments_owned.with_only_columns(Assignment.id)))
                .where(Assignment.user_id.in_(dormant_ids))
                .group_by(Assignment.user_id)
            )
        ).all()
    )
    flow_counts = dict(
        (
            await db.execute(
                select(Flow.user_id, func.count())
                .where(Flow.id.in_(flows_owned.with_only_columns(Flow.id)), Flow.user_id.in_(dormant_ids))
                .group_by(Flow.user_id)
            )
        ).all()
    )
    last_sign_in = dict(
        (
            await db.execute(
                select(AuditLog.user_id, func.max(AuditLog.created_at))
                .where(_completed_sign_in(), AuditLog.user_id.in_(dormant_ids))
                .group_by(AuditLog.user_id)
            )
        ).all()
    )
    # The longest without a sign-in first: since the last sign-in, or since its creation for an
    # account never signed in to.
    dormant.sort(key=lambda owner: (last_sign_in.get(owner.id) or owner.created_at, owner.email))
    lines = [
        _dormant_account_line(
            owner.full_name,
            owner.email,
            owner.created_at,
            last_sign_in.get(owner.id),
            assignment_counts.get(owner.id, 0),
            flow_counts.get(owner.id, 0),
        )
        for owner in dormant[:_MAX_LISTED]
    ]
    if len(dormant) > len(lines):
        lines.append(f"and {len(dormant) - len(lines)} more")
    automations = sum(assignment_counts.values()) + sum(flow_counts.values())
    findings.append(
        AuditFinding(
            id="authentication.dormant_account_automations",
            category="authentication",
            severity="warning",
            title=(
                f"{automations} active automation(s) run as {len(dormant)} account(s) nobody signed in to "
                f"for {DORMANT_ACCOUNT_DAYS} days"
            ),
            description=(
                "These accounts are active, so their assignments and flows keep running with their identity "
                f"and their integrations, but nobody has signed in to them for {DORMANT_ACCOUNT_DAYS} days: "
                "often someone who left, since removing a person from the identity provider stops their "
                f"sign-in, not their account. Accounts: {'; '.join(lines)}. A sign-in is one the audit log "
                "records (password, SSO, MCP OAuth, XTM One Desktop): API keys and the chat embedded in "
                "OpenCTI or OpenAEV do not sign in."
            ),
            recommendation=(
                "Deactivate the account of anyone who left, in Administration > Users and groups: its assignments and flows pause, "
                "company-managed ones excepted, and resume unchanged if the account is reactivated. To keep one "
                "running, transfer its agent (its assignments follow) or the flow to an active owner, or make it "
                "company-managed."
            ),
        )
    )


#: Keys named in an API key finding; the others are counted.
_MAX_LISTED_API_KEYS = 20
#: Where an administrator sees and revokes every personal API key.
_API_KEYS_ADMIN_PATH = "Settings > Access and security > API keys"


def _api_key_line(key: UserApiKey, email: str) -> str:
    last = f"last used {key.last_used_at.date().isoformat()}" if key.last_used_at else "never used"
    source = ", ADMIN_TOKEN" if key.is_from_env else ""
    return f"{key.name} ({email}{source}, {last})"


def _api_key_lines(keys: list[tuple[UserApiKey, str]]) -> str:
    lines = [_api_key_line(key, email) for key, email in keys[:_MAX_LISTED_API_KEYS]]
    if len(keys) > len(lines):
        lines.append(f"and {len(keys) - len(lines)} more")
    return "; ".join(lines)


async def _check_api_keys(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """Personal API keys that never expire, outlive the maximum lifetime, or carry full scope.

    Only keys that authenticate today: active, unexpired, of an active account. A
    maximum lifetime set after a key was created never cuts that key off - the
    platform reports it here instead, for its owner to re-date or an
    administrator to revoke.
    """
    now = datetime.now(UTC)
    keys = [
        (key, email)
        for key, email in (
            await db.execute(
                select(UserApiKey, User.email)
                .join(User, User.id == UserApiKey.user_id)
                .where(
                    UserApiKey.is_active.is_(True),
                    User.is_active.is_(True),
                    or_(UserApiKey.expires_at.is_(None), UserApiKey.expires_at > now),
                )
                .order_by(UserApiKey.created_at)
            )
        ).all()
    ]
    if not keys:
        return

    days = api_key_access.max_lifetime_days()
    beyond = [(key, email) for key, email in keys if api_key_access.beyond_max_lifetime(key.expires_at, now)]
    forever = [(key, email) for key, email in keys if key.expires_at is None]
    if days > 0 and beyond:
        findings.append(
            AuditFinding(
                id="authentication.api_keys_beyond_max_lifetime",
                category="authentication",
                severity="warning",
                title=f"{len(beyond)} API key(s) outlive the {days}-day maximum lifetime",
                description=(
                    f"{option_reference('api_key_max_lifetime_days')} is {days}, but these keys never expire or "
                    f"expire more than {days} days from now, later than a key created or re-dated today may: they "
                    "were created before the limit (or by ADMIN_TOKEN), and the platform never cuts an existing key "
                    f"off on its own. Keys: {_api_key_lines(beyond)}."
                ),
                recommendation=(
                    "Have their owners give them an expiry in My Profile > API Keys, or revoke them in "
                    f"{_API_KEYS_ADMIN_PATH}."
                ),
                control="authentication.api_key_max_lifetime",
            )
        )
    elif days == 0 and forever:
        findings.append(
            AuditFinding(
                id="authentication.api_keys_without_expiry",
                category="authentication",
                severity="info",
                title=f"{len(forever)} API key(s) never expire",
                description=(
                    "A personal API key without an expiry works until someone deletes or revokes it, so a key "
                    f"left in a script or a CI secret keeps working for good. Keys: {_api_key_lines(forever)}."
                ),
                recommendation=(
                    where_to_set("api_key_max_lifetime_days", "365", sentence_start=True)
                    + ", or less: keys created or re-dated from then on must expire within it, and the keys "
                    "listed here are reported until their owners give them an expiry."
                ),
                control="authentication.api_key_max_lifetime",
            )
        )
    else:
        findings.append(
            AuditFinding(
                id="authentication.api_keys_expire",
                category="authentication",
                severity="pass",
                title=f"Every active API key expires ({len(keys)} key(s))",
                description=(
                    f"Each expires within the {days}-day maximum lifetime."
                    if days > 0
                    else "Each carries an expiry; no maximum lifetime is set for new keys."
                ),
                control="authentication.api_key_max_lifetime",
            )
        )

    full = [(key, email) for key, email in keys if api_key_access.has_full_scope(key)]
    if full:
        findings.append(
            AuditFinding(
                id="authentication.api_keys_full_scope",
                category="authentication",
                severity="info",
                title=f"{len(full)} API key(s) have full scope",
                description=(
                    "These keys reach every surface (REST API, LLM proxy, MCP endpoints), read-write, for every "
                    "agent: all their owner can do. A key created before scopes existed keeps that access until "
                    f"it is changed. Keys: {_api_key_lines(full)}."
                ),
                recommendation=(
                    "Give each key only the surfaces it calls, read-only where it only reads, and the agents it "
                    f"talks to, in My Profile > API Keys; {_API_KEYS_ADMIN_PATH} lists them."
                ),
            )
        )
    else:
        findings.append(
            AuditFinding(
                id="authentication.api_keys_scoped",
                category="authentication",
                severity="pass",
                title=f"Every active API key is scoped ({len(keys)} key(s))",
                description="Each is limited to some surfaces, read-only, or to selected agents.",
            )
        )


async def _check_platform_registration(findings: list[AuditFinding]) -> None:
    """Check the token OpenCTI / OpenAEV / OpenCRQ platforms register with."""
    state = registration_token_state()
    if state == "published_default":
        findings.append(
            AuditFinding(
                id="authentication.registration_token_default",
                category="authentication",
                severity="critical",
                title="Platform registration token is the published default",
                description=(
                    "PLATFORM_REGISTRATION_TOKEN is set to the default value shipped in "
                    "docker-compose.yml. The value is public, so it authenticates nobody, and a "
                    "registered platform's URL is trusted to sign XTM One users in. It therefore "
                    "only keeps the platforms already registered with this instance connected: it "
                    "cannot register a new platform, change a registered platform's URL or "
                    "unregister one. Until it is replaced, the Filigran license certificate is "
                    "withheld from registration answers, so platforms sub-licensed by it cannot "
                    "prove their Enterprise Edition."
                ),
                recommendation=(
                    f"Generate a random token ({REGISTRATION_TOKEN_GENERATE_COMMAND}), set it as "
                    "PLATFORM_REGISTRATION_TOKEN on XTM One and on every connected OpenCTI, OpenAEV "
                    "or OpenCRQ platform, then restart them. Review the registered platforms in "
                    "Settings and purge any you do not recognise: earlier releases let anyone "
                    "holding the default register one, and a registration stays trusted after the "
                    "token changes. This audit names any registration Settings does not list."
                ),
            )
        )
    elif state == "unset":
        findings.append(
            AuditFinding(
                id="authentication.registration_disabled",
                category="authentication",
                severity="info",
                title="Platform registration disabled",
                description=(
                    "PLATFORM_REGISTRATION_TOKEN is not set, so no OpenCTI, OpenAEV or OpenCRQ "
                    "platform can register with this instance."
                ),
                recommendation=(
                    "To connect platforms, set PLATFORM_REGISTRATION_TOKEN to a random secret "
                    f"({REGISTRATION_TOKEN_GENERATE_COMMAND}) and use the same value on each platform."
                ),
            )
        )
    else:
        findings.append(
            AuditFinding(
                id="authentication.registration_token_configured",
                category="authentication",
                severity="pass",
                title="Platform registration token configured",
                description="PLATFORM_REGISTRATION_TOKEN is set to a value of your own.",
            )
        )


# How many registrations, and how much of each URL, the finding below names.
_LISTED_REGISTRATIONS = 10
_LISTED_URL_CHARS = 120


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


async def _check_registered_platforms(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """Name the active registrations no OpenCTI, OpenAEV or OpenCRQ platform made.

    A registered platform's URL is trusted to sign XTM One users in whatever
    its identifier, until the registration is purged - changing the token
    does not end that trust - and Settings lists the registrations of these
    products only. A registration under any other identifier, which earlier
    releases let anyone holding the published default token create, would be
    trusted and seen nowhere. Counted and listed in SQL, so the audit reads a
    bounded amount: an install polluted that way may hold any number of them,
    each with a URL of any length.
    """
    from app.api.v1.platform import PRODUCT_PLATFORM_IDENTIFIERS
    from app.models.platform_registration import PlatformRegistration

    active = PlatformRegistration.status == "active"
    unknown = and_(active, PlatformRegistration.platform_identifier.not_in(sorted(PRODUCT_PLATFORM_IDENTIFIERS)))
    total, count = (
        await db.execute(
            select(
                func.count(PlatformRegistration.id).filter(active),
                func.count(PlatformRegistration.id).filter(unknown),
            )
        )
    ).one()
    if not total:
        return
    if not count:
        findings.append(
            AuditFinding(
                id="authentication.registered_platforms_listed",
                category="authentication",
                severity="pass",
                title=f"Settings lists every registered platform ({total} registration(s))",
                description=(
                    "Every active platform registration uses an OpenCTI, OpenAEV or OpenCRQ "
                    "identifier, so the integration catalog in Settings shows every platform "
                    "trusted to sign XTM One users in."
                ),
            )
        )
        return
    rows = (
        await db.execute(
            select(
                PlatformRegistration.id,
                PlatformRegistration.platform_identifier,
                # One character past the limit, so ``_clip`` sees that a URL was cut.
                func.substr(PlatformRegistration.platform_url, 1, _LISTED_URL_CHARS + 1).label("platform_url"),
            )
            .where(unknown)
            .order_by(PlatformRegistration.registered_at, PlatformRegistration.id)
            .limit(_LISTED_REGISTRATIONS)
        )
    ).all()
    named = "; ".join(
        f"'{row.platform_identifier}' at {_clip(row.platform_url, _LISTED_URL_CHARS)} (id {row.id})"
        for row in rows
    )
    if count > len(rows):
        named += f"; and {count - len(rows)} more"
    registrations = "registration uses" if count == 1 else "registrations use"
    findings.append(
        AuditFinding(
            id="authentication.registered_platform_unknown",
            category="authentication",
            severity="critical",
            title="Platform registered under an unknown identifier",
            description=(
                f"{count} active platform {registrations} an identifier no OpenCTI, OpenAEV or "
                f"OpenCRQ platform registers under: {named}. Settings does not list such a "
                "registration, yet a registered platform's URL is trusted to sign XTM One users in."
            ),
            recommendation=(
                "Unless you connected that client yourself, purge each one with "
                "DELETE /api/v1/platform/registrations/{id}/purge (administrators): earlier "
                "releases let anyone holding the published default registration token register "
                "a platform, and changing the token does not end that trust."
            ),
        )
    )


async def _check_platform_token_audiences(findings: list[AuditFinding]) -> None:
    """The audiences a registered platform's token may carry to sign a user in."""
    from app.core.url_identity import redact_url
    from app.core.xtm_auth import xtm_issuer

    try:
        identity = xtm_issuer()
    except ValueError:
        identity = redact_url(settings.base_url)
    extra = list(settings.xtm_auth_accepted_audiences)
    if not extra:
        findings.append(
            AuditFinding(
                id="authentication.platform_token_audiences_identity_only",
                category="authentication",
                severity="pass",
                title="Platform tokens accepted only when addressed to BASE_URL",
                description=(
                    "A token a registered platform signs a user in with is accepted only when its "
                    f"audience is XTM One's identity, {identity} (BASE_URL), published at "
                    "/xtm/auth/metadata."
                ),
                control="authentication.platform_token_audiences",
            )
        )
        return
    findings.append(
        AuditFinding(
            id="authentication.platform_token_audiences_extra",
            category="authentication",
            severity="info",
            title=f"Platform tokens also accepted when addressed to {len(extra)} other URL(s)",
            description=(
                f"XTM_AUTH_ACCEPTED_AUDIENCES lists {_listed(extra)}: a token a registered platform "
                "signs a user in with is accepted when it is addressed to one of them as well as "
                f"to XTM One's identity, {identity} (BASE_URL). The list exists for platform "
                "versions that address their tokens to the internal URL they reach XTM One on."
            ),
            recommendation=(
                "Remove an entry once every platform addressing it reads /xtm/auth/metadata and "
                "addresses its tokens to BASE_URL."
            ),
            control="authentication.platform_token_audiences",
        )
    )


async def _check_integrations(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """Check integration credential health."""
    total = (await db.execute(select(func.count(Integration.id)))).scalar() or 0
    errored = (
        await db.execute(
            select(func.count(Integration.id)).where(Integration.status == "error")
        )
    ).scalar() or 0
    expired = (
        await db.execute(
            select(func.count(Integration.id)).where(Integration.status == "expired")
        )
    ).scalar() or 0
    reauthorize = (
        await db.execute(
            select(func.count(Integration.id)).where(
                Integration.status == "reauthorize"
            )
        )
    ).scalar() or 0

    if errored > 0:
        findings.append(
            AuditFinding(
                id="integrations.error_state",
                category="integrations",
                severity="warning",
                title=f"{errored} integration(s) in error state",
                description="These integrations have failed and may need reconfiguration.",
                recommendation="Review integration settings and re-authorize failed integrations.",
            )
        )
    if expired > 0:
        findings.append(
            AuditFinding(
                id="integrations.expired_credentials",
                category="integrations",
                severity="warning",
                title=f"{expired} integration(s) with expired credentials",
                description="These integrations need their OAuth tokens refreshed.",
                recommendation="Users should re-authorize expired integrations.",
            )
        )
    if reauthorize > 0:
        findings.append(
            AuditFinding(
                id="integrations.reauthorization_required",
                category="integrations",
                severity="warning",
                title=f"{reauthorize} integration(s) requiring re-authorization",
                description=(
                    "These integrations' OAuth grants were revoked or expired "
                    "and cannot be refreshed automatically."
                ),
                recommendation=(
                    "Ask the owning users to re-authorize these integrations "
                    "from the integration detail page."
                ),
            )
        )
    if total > 0 and errored == 0 and expired == 0 and reauthorize == 0:
        findings.append(
            AuditFinding(
                id="integrations.healthy",
                category="integrations",
                severity="pass",
                title=f"All {total} integration(s) healthy",
                description="No integration credential issues detected.",
            )
        )


async def _token_urls_relying_on_the_default(db: AsyncSession) -> list[str]:
    """The stored OAuth token URLs the strict checks would refuse, each with its owner and why.

    Judged as written (``strict_refusal_reason``): a name is not resolved here.
    """
    from app.integrations.token_endpoint import strict_refusal_reason

    integrations = (
        await db.execute(
            select(Integration.id, Integration.name, Integration.provider, Integration.token_url)
            .where(Integration.auth_type == "oauth", Integration.token_url.is_not(None))
            .order_by(Integration.name, Integration.id)
        )
    ).all()
    servers = (
        await db.execute(
            select(MCPServer.id, MCPServer.name, MCPServer.token_url)
            .where(MCPServer.token_url.is_not(None))
            .order_by(MCPServer.name, MCPServer.id)
        )
    ).all()
    stored = [
        *(
            (f"integration {row.name} ({_provider_name(row.provider)}, {row.id})", row.token_url)
            for row in integrations
        ),
        *((f"MCP server {row.name} ({row.id})", row.token_url) for row in servers),
    ]
    return [f"{label}: {reason}" for label, url in stored if (reason := strict_refusal_reason(url))]


async def _check_oauth_token_url_policy(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """Whether the opt-in strict checks on OAuth token URLs are on.

    Off, a warning only while a stored token URL relies on it - one the strict
    checks would refuse - since that is what they protect in use; otherwise
    context, as for a deployment whose token URLs all pass them.
    """
    if security_option("oauth_token_url_strict"):
        findings.append(
            AuditFinding(
                id="integrations.oauth_token_url_strict_on",
                category="integrations",
                severity="pass",
                title="OAuth token URLs held to the strict checks",
                description=(
                    f"{option_reference('oauth_token_url_strict')} is on: when a token URL is saved and "
                    "before each token request, a URL whose host is, or resolves during the check to, a "
                    f"loopback (outside {option_reference('oauth_token_dev_trusted_origins')}), "
                    "link-local, unspecified, reserved, multicast or cloud-metadata address is refused, "
                    "and so is any scheme but http(s). "
                    "A name the check cannot resolve is left to the request, which resolves it again."
                ),
                control="integrations.oauth_token_url_strict",
            )
        )
        return
    origins = list(security_option("oauth_token_dev_trusted_origins"))
    unused = (
        f" {option_reference('oauth_token_dev_trusted_origins')} lists {_listed(origins)}: the list "
        "has no effect until the strict checks are on."
        if origins
        else ""
    )
    relying = await _token_urls_relying_on_the_default(db)
    in_use = (
        f" {len(relying)} stored token URL(s) are accepted only because they are off, judged from the URL "
        f"as written (a name is resolved by the token request, not here): {_listed(relying)}."
        if relying
        else ""
    )
    findings.append(
        AuditFinding(
            id="integrations.oauth_token_url_strict_off",
            category="integrations",
            severity=("info" if settings.debug else "warning") if relying else "info",
            title=(
                f"{len(relying)} OAuth token URL(s) are accepted only while the strict checks are off"
                if relying
                else "Strict OAuth token URL checks are off"
            ),
            description=(
                "A token URL whose host is, or resolves during the check to, a cloud-metadata "
                "address is refused; token URLs on loopback, link-local, unspecified, reserved or "
                "multicast addresses, and ws:// or wss:// URLs, are accepted." + in_use + unused
            ),
            recommendation=(
                (
                    "Point each of these at an address of its identity provider the strict checks accept "
                    "(on a development machine, list a loopback origin in "
                    f"{option_reference('oauth_token_dev_trusted_origins')}), then "
                    + where_to_set("oauth_token_url_strict", "true")
                    + "."
                )
                if relying
                else where_to_set("oauth_token_url_strict", "true", sentence_start=True)
                + " once no integration or MCP server needs such a token URL. On a development "
                f"machine, {option_reference('oauth_token_dev_trusted_origins')} then keeps a "
                "loopback token URL working."
            ),
            control="integrations.oauth_token_url_strict",
        )
    )


async def _check_oauth_token_dev_origins(findings: list[AuditFinding]) -> None:
    """A development allow-list that lets OAuth token URLs resolve to loopback under the strict checks."""
    origins = list(security_option("oauth_token_dev_trusted_origins"))
    # Without the strict checks every loopback token URL is accepted, so the
    # list relaxes nothing: the strict checks' own finding says so.
    if not origins or not security_option("oauth_token_url_strict"):
        return
    findings.append(
        AuditFinding(
            id="integrations.oauth_token_dev_origins_set",
            category="integrations",
            severity="info" if settings.debug else "warning",
            title=f"OAuth token URLs may resolve to loopback for {len(origins)} origin(s)",
            description=(
                f"{option_reference('oauth_token_dev_trusted_origins')} lists {_listed(origins)}: a token "
                "request (code exchange or refresh, for integrations and MCP servers) to these origins may "
                "reach a loopback address, which the strict checks refuse every other token URL. The list "
                "exists for local development against an identity provider on the same machine."
            ),
            recommendation=where_to_set("oauth_token_dev_trusted_origins", "", sentence_start=True) + ".",
            control="integrations.oauth_token_dev_trusted_origins",
        )
    )


async def _check_agents(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """Check agent security configuration."""
    total = (await db.execute(select(func.count(Agent.id)))).scalar() or 0
    if total == 0:
        return

    # Check for agents without guardrails
    no_guardrails = (
        await db.execute(
            select(func.count(Agent.id)).where(
                Agent.enabled.is_(True),
                (Agent.guardrails.is_(None)) | (Agent.guardrails == ""),
            )
        )
    ).scalar() or 0

    if no_guardrails > 0:
        findings.append(
            AuditFinding(
                id="agents.without_guardrails",
                category="agents",
                severity="info",
                title=f"{no_guardrails} active agent(s) without guardrails",
                description="These agents have no custom guardrails set. They rely on the platform's default safety prompt.",
                recommendation="Consider adding guardrails for sensitive agents to restrict their behavior.",
            )
        )

    # Check for agents with tool deny lists
    with_deny = (
        await db.execute(
            select(func.count(Agent.id)).where(
                Agent.enabled.is_(True),
                Agent.tool_deny_list.isnot(None),
            )
        )
    ).scalar() or 0

    findings.append(
        AuditFinding(
            id="agents.tool_deny_lists",
            category="agents",
            severity="info",
            title=f"{with_deny}/{total} agent(s) use tool deny lists",
            description=(
                "Tool deny lists provide a security backstop by explicitly "
                "blocking dangerous tools. Denied tools are removed from the "
                "advertised tool list AND blocked at execution time (fail "
                "closed) in chat, assignment, and public embed paths."
            ),
            recommendation=(
                "Review agent tool deny lists to ensure sensitive tools are blocked where appropriate."
                if with_deny == 0
                else None
            ),
        )
    )

    # Agents scoping their built-in tool groups to a subset of functions
    with_builtin_scope = (
        await db.execute(
            select(func.count(Agent.id)).where(
                Agent.enabled.is_(True),
                Agent.builtin_allowed_tools.isnot(None),
            )
        )
    ).scalar() or 0

    findings.append(
        AuditFinding(
            id="agents.builtin_allowed_tools",
            category="agents",
            severity="info",
            title=f"{with_builtin_scope}/{total} agent(s) scope built-in tools per function",
            description=(
                "A built-in tool allow list keeps only the named functions of a "
                "built-in group (for example the read functions of the agent "
                "builder). Functions outside it are neither advertised nor "
                "loadable on demand, in chat, assignment, scheduled and public "
                "embed paths; the deny list still applies on top."
            ),
            recommendation=None,
        )
    )


#: The built-in suites whose tools take the admin bypass through
#: ``agent_tool_admin_bypass`` (pinned against the tool modules by
#: ``tests/test_agent_tool_admin_access.py``).
_ADMIN_BYPASS_SUITES: tuple[str, ...] = (
    "agent_builder",
    "flow_builder",
    "fleets",
    "work_items",
    "inspect_agent_traces",
    # The Platform Health orchestrator's tools (#8261) read cycles and start them as the REST routes do.
    "concierge_orchestrator",
    # The ROI tools (#10006) read reports and records past the caller's own as the REST routes do.
    "roi",
)


async def _check_agent_tool_admin_access(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """Whether an admin's agent tools reach objects past the admin's three tiers."""
    from app.api.v1.platform import is_chat_enabled
    from app.api.v1.platform_settings_helpers import peek_platform_settings

    agent_rows = await db.execute(select(Agent.builtin_tool_ids).where(Agent.enabled.is_(True)))
    agents = sum(
        1
        for tool_ids in agent_rows.scalars()
        if isinstance(tool_ids, list) and any(suite in tool_ids for suite in _ADMIN_BYPASS_SUITES)
    )
    # The chat composer offers the agent builder to every General Assistant
    # conversation, whatever its configured tool lists say.
    assistant = await is_chat_enabled(db)
    platform = await peek_platform_settings(db)
    mcp_server = bool(getattr(platform, "xtm_mcp_enabled", False))
    reach = [
        *(
            [
                f"{agents} agent(s) carrying the agent builder, the flow builder, "
                "the fleets, the work items or the agent trace inspection"
            ]
            if agents
            else []
        ),
        *(["the General Assistant"] if assistant else []),
        *(["the XTM One MCP server"] if mcp_server else []),
    ]
    if not reach:
        return
    if security_option("agent_tools_strict_admin_access"):
        findings.append(
            AuditFinding(
                id="access.admin_reach_strict",
                category="access",
                severity="pass",
                title="Admins' agents keep to their three tiers",
                description=(
                    f"{option_reference('agent_tools_strict_admin_access')} is on: through "
                    + ", ".join(reach)
                    + ", an admin's agent reaches only the objects the admin's own, group-shared "
                    "and company-managed ones include."
                ),
                control="access.agent_tools_strict_admin_access",
            )
        )
        return
    findings.append(
        AuditFinding(
            id="access.admin_reach_private_assignments",
            category="access",
            severity="warning",
            title="Admins' agents can reach colleagues' private objects",
            description=(
                "Through " + ", ".join(reach) + ", an admin's agent can read, "
                "edit, delete or run agents, assignments and their runs, traces, "
                "flows, evaluations, knowledge bases and the other objects the "
                "admin's own, group-shared and company-managed ones do not "
                "include - the admin bypass the REST API has. An agent steered by "
                "a prompt injection acts with it."
            ),
            recommendation=(
                where_to_set("agent_tools_strict_admin_access", "true", sentence_start=True)
                + " to keep admins' agents to their three tiers; admins keep full "
                "access through the UI and the REST API."
            ),
            control="access.agent_tools_strict_admin_access",
        )
    )


async def _check_tool_result_guard(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """Whether the connected prompt guard screens tool results, and what a match does.

    With a guard connected, no tool screened is a warning (every tool result
    reaches agents unscreened), log-only screening a monitoring mode,
    withholding the pass; without one, a listed tool is screened by nothing.
    """
    from app.services.integration_state import any_provider_connected
    from app.services.prompt_guards import GUARD_PROVIDER

    scope = security_option("prompt_guard_scanned_tools")
    if not await any_provider_connected(db, GUARD_PROVIDER):
        if scope:
            findings.append(
                AuditFinding(
                    id="agents.tool_result_guard_not_connected",
                    category="agents",
                    severity="info",
                    title=f"{len(scope)} tool(s) listed for screening, but no prompt guard is connected",
                    description=(
                        f"{option_reference('prompt_guard_scanned_tools')} lists tools whose results the "
                        "prompt guard screens, but no company-managed Nova integration is connected: no tool "
                        "result is screened."
                    ),
                    recommendation="Connect a company-managed Nova integration to screen these tool results.",
                    control="agents.tool_result_guard_scope",
                )
            )
        return
    if not scope:
        findings.append(
            AuditFinding(
                id="agents.tool_result_guard_unscoped",
                category="agents",
                severity="warning",
                title="The prompt guard screens no tool result",
                description=(
                    "A prompt guard (Nova) is connected and screens chat messages and memory writes, but "
                    f"{option_reference('prompt_guard_scanned_tools')} lists no tool: what tools return to "
                    "agents (fetched pages, tickets, messages, other servers' answers) reaches them unscreened."
                ),
                recommendation=(
                    "Screen the tools that return outside content: for fetched pages, "
                    + where_to_set("prompt_guard_scanned_tools", "web_fetch")
                    + ", and list there too the integration and MCP tools that read tickets, messages or "
                    "other servers' answers."
                ),
                control="agents.tool_result_guard_scope",
            )
        )
        return
    if security_option("prompt_guard_block_tool_results"):
        findings.append(
            AuditFinding(
                id="agents.tool_result_guard_blocking",
                category="agents",
                severity="pass",
                title=f"The prompt guard screens {len(scope)} tool(s) and withholds what a block rule matches",
                description=(
                    "A screened tool result that a block rule matches is withheld from the agent and "
                    "recorded in the audit log; the agent waits for the guard's verdict on each one."
                ),
                control="agents.tool_result_guard_blocking",
            )
        )
        return
    findings.append(
        AuditFinding(
            id="agents.tool_result_guard_log_only",
            category="agents",
            severity="info",
            title=f"The prompt guard screens {len(scope)} tool(s) in log-only mode",
            description=(
                "A rule match on a screened tool result is recorded in the audit log, a block rule's "
                "included, and the result still reaches the agent as it is: the guard evaluates it "
                "beside the agent, which does not wait for the verdict."
            ),
            recommendation=(
                where_to_set("prompt_guard_block_tool_results", "true", sentence_start=True)
                + " to withhold what a block rule matches; each screened result then waits for the "
                "guard's verdict before the agent continues."
            ),
            control="agents.tool_result_guard_blocking",
        )
    )


async def _check_agent_output_schemas(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """Whether enabled agents' output schemas reference another document, never retrieved so never checked."""
    # Read from the stored text: a cleared schema is a JSON null, not SQL NULL.
    schema_text = cast(Agent.output_schema, Text)
    with_schema = and_(Agent.enabled.is_(True), Agent.output_schema.isnot(None), schema_text != "null")
    total = (await db.execute(select(func.count(Agent.id)).where(with_schema))).scalar() or 0
    if not total:
        return
    # Only a schema whose text holds a reference keyword can name another document.
    rows = (
        await db.execute(
            select(Agent.name, Agent.output_schema).where(
                with_schema, or_(*(schema_text.contains(keyword) for keyword in REF_KEYWORDS))
            )
        )
    ).all()
    # Each read is bounded, but a large schema still costs tens of milliseconds.
    names = sorted(await asyncio.to_thread(lambda: [name for name, schema in rows if references_another_document(schema)]))
    if not names:
        findings.append(
            AuditFinding(
                id="agents.output_schema_references_local",
                category="agents",
                severity="pass",
                title="Agent output schemas reference no other document",
                description=(
                    f"{total} enabled agent(s) with an output schema, none with a $ref / $dynamicRef naming "
                    "another document (never retrieved, so what it describes would go unvalidated)."
                ),
            )
        )
        return
    findings.append(
        AuditFinding(
            id="agents.output_schema_remote_references",
            category="agents",
            severity="warning",
            title=f"{len(names)} agent output schema(s) reference another document",
            description=(
                f"{_listed(names)}: a $ref / $dynamicRef naming another document is never retrieved, "
                "so the part of the output it describes is not validated."
            ),
            recommendation="Inline the referenced definitions under $defs and point the $ref at a '#' fragment.",
        )
    )


async def _check_tool_approvals(db: AsyncSession, findings: list[AuditFinding], platform: object | None) -> None:
    """Whether tool calls wait for a person's approval, and what lets them run unasked.

    Approvals are in use while an enabled agent, or the General Assistant,
    would ask: the gate's own resolution (``services/hitl_whitelist.py``,
    ``resolve_approve_tool_calls``) - an agent's ``approve_tool_calls`` wins,
    and one that sets nothing, like the General Assistant, follows
    ``approve_tool_calls_default``. ``tool_approvals_enabled`` off lets every
    call run for everyone (``resolve_gate_suppression``), so while it is off
    nothing else is reported: no bot, platform or exemption changes what runs.
    """
    if platform is UNREADABLE_ROW:
        return
    from app.api.v1.platform import is_chat_enabled
    from app.services.hitl_whitelist import agent_asks, install_default_asks

    approvals_on = stored_value(platform, "platform_settings", "tool_approvals_enabled") is not False
    asks_by_default = install_default_asks(stored_value(platform, "platform_settings", "approve_tool_calls_default"))
    agents = (
        await db.execute(
            select(Agent.name, Agent.approve_tool_calls).where(Agent.enabled.is_(True)).order_by(Agent.name)
        )
    ).all()
    assistant = ["the General Assistant"] if await is_chat_enabled(db) else []
    following = [name for name, own in agents if own is None] + assistant
    asking = [name for name, own in agents if agent_asks(own, asks_by_default)]
    asking += assistant if asks_by_default else []

    if not approvals_on:
        if asking:
            findings.append(
                AuditFinding(
                    id="agents.tool_approvals_off",
                    category="agents",
                    severity="warning",
                    title="Tool approvals are suspended for the whole platform",
                    description=(
                        f"No tool call waits for anyone: {_listed(asking)} would otherwise ask before running "
                        "a tool that asks first, and run tools that change data without asking instead."
                    ),
                    recommendation=(
                        "Resume approvals under Suspend all approvals in Settings > Access and security > Tool approvals; the "
                        "tool rules, the connected platforms' policies and every personal approval apply again as "
                        "they were."
                    ),
                    control="agents.tool_approvals",
                )
            )
        else:
            findings.append(
                AuditFinding(
                    id="agents.tool_approvals_off_unused",
                    category="agents",
                    severity="info",
                    title="Tool approvals are suspended, and no agent would ask",
                    description=(
                        "No tool call waits for anyone, and neither an enabled agent nor the General Assistant "
                        "is set to ask before running tools: resuming approvals changes nothing until one is."
                    ),
                    recommendation=(
                        "Resume approvals in Settings > Access and security > Tool approvals before setting an agent to ask, "
                        "or its tool calls still run without asking."
                    ),
                    control="agents.tool_approvals",
                )
            )
        return

    if asking:
        findings.append(
            AuditFinding(
                id="agents.tool_approvals_on",
                category="agents",
                severity="pass",
                title="Tool calls wait for approval",
                description=f"{_listed(asking)} wait for a person to approve each tool call that asks first.",
                control="agents.tool_approvals",
            )
        )
    if following and not asks_by_default:
        findings.append(
            AuditFinding(
                id="agents.tool_approvals_default_off",
                category="agents",
                # The opt-in baseline of every install, never a new alert on one that changed nothing.
                severity="info",
                title=f"{len(following)} agent(s) run tools without asking, by default",
                description=(
                    f"{_listed(following)} follow the platform default, which is off: their tool calls run "
                    "without a person approving them, tools that change data included. Tool approvals are "
                    "opt-in on purpose: no agent starts asking until an administrator turns it on."
                ),
                recommendation=(
                    "Turn on Every agent asks by default in Settings > Access and security > Tool approvals, or set Ask "
                    "before running tools on each agent that should (Agents > the agent > Configuration > Tooling)."
                ),
                control="agents.tool_approvals_default",
            )
        )
    elif following:
        findings.append(
            AuditFinding(
                id="agents.tool_approvals_default_on",
                category="agents",
                severity="pass",
                title="Agents ask before running tools by default",
                description=(
                    f"{_listed(following)} follow the platform default, which is on: they wait for a person "
                    "to approve each tool call that asks first."
                ),
                control="agents.tool_approvals_default",
            )
        )
    opted_out = [name for name, own in agents if own is False]
    if opted_out:
        findings.append(
            AuditFinding(
                id="agents.tool_approvals_agents_opted_out",
                category="agents",
                severity="info",
                title=f"{len(opted_out)} agent(s) set to run tools without asking",
                description=(
                    f"{_listed(opted_out)} have Ask before running tools set to Don't ask: their tool calls run "
                    "without a person approving them, in chat and in assignment runs, whatever the platform default."
                ),
                recommendation=(
                    "Review Ask before running tools on each of them (Agents > the agent > Configuration > "
                    "Tooling); Don't ask is meant for agents whose tools may run without asking."
                ),
                control="agents.tool_approvals",
            )
        )
    if not asking:
        return

    trusted_bots = (
        await db.execute(
            select(ChannelBot.name).where(ChannelBot.requires_approval.is_(False)).order_by(ChannelBot.name)
        )
    ).scalars().all()
    if trusted_bots:
        findings.append(
            AuditFinding(
                id="agents.tool_approvals_bots_trusted",
                category="agents",
                severity="warning",
                title=f"{len(trusted_bots)} channel bot(s) let agents run tools without asking",
                description=(
                    f"{_listed(list(trusted_bots))} have Ask before running tools off: nothing an agent does in "
                    "a conversation through them waits for approval, tools that change data included, and "
                    "their own send, edit and delete tools run without asking wherever they are called."
                ),
                recommendation=(
                    "Turn Ask before running tools back on for these bots in Settings > Integrations > Channels & Bots, "
                    "unless each is meant to run its agents' tools without asking."
                ),
                control="agents.tool_approvals",
            )
        )

    registrations = (
        await db.execute(
            select(
                PlatformRegistration.platform_title,
                PlatformRegistration.supports_approval_prompts,
                PlatformRegistration.approval_policy,
                PlatformRegistration.approval_policy_set_by,
            )
            .where(PlatformRegistration.status != "unregistered")
            .order_by(PlatformRegistration.platform_title)
        )
    ).all()
    unasked_platforms = [
        title
        for title, supports, policy, set_by in registrations
        if not platform_gates_tools(
            supports_approval_prompts=supports, approval_policy=policy, approval_policy_set_by=set_by
        )
    ]
    if unasked_platforms:
        findings.append(
            AuditFinding(
                id="agents.tool_approvals_platforms_skip",
                category="agents",
                severity="warning",
                title=f"{len(unasked_platforms)} connected platform(s) have their tools run without asking",
                description=(
                    f"The tools of {_listed(unasked_platforms)} run without a person approving them, in every "
                    "agent, including the ones that change data on the platform."
                ),
                recommendation=(
                    "Turn on Require approval for these platforms under Approval for connected platforms in "
                    "Settings > Access and security > Tool approvals, unless their tools are meant to run without asking."
                ),
                control="agents.tool_approvals",
            )
        )

    allow_all = (
        await db.execute(
            select(func.count(User.id)).where(User.is_active.is_(True), User.approve_all_tools.is_(True))
        )
    ).scalar() or 0
    if allow_all:
        findings.append(
            AuditFinding(
                id="agents.tool_approvals_users_allow_all",
                category="agents",
                severity="info",
                title=f"{allow_all} user(s) let every tool run without asking them",
                description=(
                    f"{allow_all} active user(s) turned on Allow every tool for me: tool calls in their "
                    "conversations and in their assignment runs no longer ask them first, company-managed "
                    "assignment runs excepted."
                ),
                control="agents.tool_approvals",
            )
        )

    additions = (
        await db.execute(
            select(HitlWhitelistOverride.tool_key)
            .where(HitlWhitelistOverride.action == ACTION_ADD)
            .order_by(HitlWhitelistOverride.tool_key)
        )
    ).scalars().all()
    writes = [tool for tool in additions if infer_tool_type(tool) == "write"]
    if writes:
        findings.append(
            AuditFinding(
                id="agents.tool_approvals_whitelist_additions",
                category="agents",
                severity="info",
                title=f"{len(writes)} tool(s) that may change data run without asking",
                description=(
                    f"An administrator set {_listed(writes)} to run without asking under Tool rules; by their "
                    "names they create, change, delete or send, and no longer ask anyone first."
                ),
                recommendation="Review them under Tool rules in Settings > Access and security > Tool approvals.",
                control="agents.tool_approvals",
            )
        )


async def _check_public_widget(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """Whether the public chat widget renders answers in strict mode."""
    widgets = (
        await db.execute(
            select(func.count(Agent.id)).where(
                Agent.enabled.is_(True), Agent.web_enabled.is_(True)
            )
        )
    ).scalar() or 0
    if widgets == 0:
        return
    if security_option("public_widget_strict_rendering"):
        findings.append(
            AuditFinding(
                id="web.public_widget_strict",
                category="web",
                severity="pass",
                title=f"Public chat widget answers render in strict mode ({widgets} agent(s) embedded)",
                description=(
                    "Answers show only the images the platform serves, open every link in a new tab, and "
                    "drop inline styles, SVG, MathML and links other than http(s), mailto and tel."
                ),
                control="web.public_widget_strict_rendering",
            )
        )
        return
    findings.append(
        AuditFinding(
            id="web.public_widget_remote_images",
            category="web",
            severity="warning",
            title=f"Public chat widget answers can load remote images ({widgets} agent(s) embedded)",
            description=(
                "An answer's HTML images are loaded from any host by the "
                "visitor's browser, and its HTML links open in the embedding "
                "page. An answer steered by a prompt injection can use an "
                "image URL to send conversation data to a third party. Answers "
                "also keep their inline styles, SVG / MathML drawings and links "
                "of any scheme that runs no script (msteams:, sms:, ...); "
                "script is removed in every mode."
            ),
            recommendation=(
                where_to_set("public_widget_strict_rendering", "true", sentence_start=True)
                + " to show only the images the platform serves (others become "
                "links), open every link in a new tab, and render answers without "
                "styles, SVG, MathML or links other than http(s), mailto and tel."
            ),
            control="web.public_widget_strict_rendering",
        )
    )


async def _check_public_widget_uploads(db: AsyncSession, findings: list[AuditFinding]) -> SimpleNamespace:
    """Which public chat widgets let a visitor attach files; the count the control reads."""
    live = (Agent.enabled.is_(True), Agent.web_enabled.is_(True))
    widgets = (await db.execute(select(func.count(Agent.id)).where(*live))).scalar() or 0
    names = list(
        (
            await db.execute(
                select(Agent.name).where(*live, Agent.web_uploads_enabled.is_(True)).order_by(Agent.name)
            )
        ).scalars()
    )
    if names:
        findings.append(
            AuditFinding(
                id="web.public_widget_uploads_on",
                category="web",
                severity="warning",
                title=f"{len(names)} public chat widget(s) accept files from visitors",
                description=(
                    f"{_listed(names)}: anyone who can open the embedding page may attach a document or "
                    "an image to a message, without an account, and the agent reads it. Each file is "
                    "bounded - accepted types, the chat upload's size, budgets per address and per "
                    "widget, storage per conversation and per widget - and deleted once its session is "
                    "over; a connected prompt guard evaluates its text."
                ),
                recommendation=(
                    "Turn off Allow visitor uploads (Agents > the agent > Configuration > Web Exposure) "
                    "on every widget that does not need files. On those that do, connect a prompt guard "
                    "and "
                    + where_to_set("prompt_guard_block_public_widget", "true")
                    + " so a file a block rule matches is refused."
                ),
                control="web.public_widget_uploads",
            )
        )
    elif widgets:
        findings.append(
            AuditFinding(
                id="web.public_widget_uploads_off",
                category="web",
                severity="pass",
                title=f"No public chat widget accepts files from visitors ({widgets} agent(s) embedded)",
                description="Visitors of the embedded widgets can only type: nothing they send is stored as a file.",
                control="web.public_widget_uploads",
            )
        )
    return SimpleNamespace(web_uploads_enabled=len(names))


async def _check_model_output_guard(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """What the connected prompt guard does with the model's answers.

    Nothing is reported while no guard is connected: whatever the option
    says, no answer is evaluated. With one connected, off is a warning (every
    answer goes unevaluated), log a monitoring mode, block the pass.
    """
    from app.services.integration_state import any_provider_connected
    from app.services.prompt_guards import GUARD_PROVIDER

    if not await any_provider_connected(db, GUARD_PROVIDER):
        return
    mode = security_option("prompt_guard_model_output")
    streamed = (
        "A streamed answer (web chat, the streaming chat embedded in OpenCTI / OpenAEV, the widget's stream) "
        "has already been shown when it is evaluated, so it is only audited."
    )
    if mode == "block":
        findings.append(
            AuditFinding(
                id="agents.prompt_guard_model_output_blocks",
                category="agents",
                severity="pass",
                title="The prompt guard withholds model answers its block rules match",
                description=(
                    "Every answer is evaluated before it is delivered and one a block rule matches is "
                    f"replaced by a withheld notice and audited. {streamed}"
                ),
                control="agents.prompt_guard_model_output",
            )
        )
        return
    if mode == "log":
        findings.append(
            AuditFinding(
                id="agents.prompt_guard_model_output_logs_only",
                category="agents",
                severity="info",
                title="The prompt guard only audits model answers its rules match",
                description=(
                    "Every answer is evaluated in the background after it is delivered, and a match "
                    "is audited (security_alert, surface model_output); no answer is withheld."
                ),
                recommendation=(
                    where_to_set("prompt_guard_model_output", "block", sentence_start=True)
                    + f" to withhold an answer a block rule matches before it is delivered. {streamed} "
                    "Every answer then waits for its evaluation."
                ),
                control="agents.prompt_guard_model_output",
            )
        )
        return
    findings.append(
        AuditFinding(
            id="agents.prompt_guard_model_output_off",
            category="agents",
            severity="warning",
            title="The prompt guard does not evaluate model answers",
            description=(
                "The connected guard evaluates what users send, not what the model answers: an answer "
                "that followed an injected instruction is delivered and nothing records it."
            ),
            recommendation=(
                where_to_set("prompt_guard_model_output", "log", sentence_start=True)
                + " to evaluate every answer in the background after it is delivered and audit a "
                "match, or to block to withhold one before it is delivered. Each evaluation runs the "
                "guard's rules, llm: rules included, once per answer."
            ),
            control="agents.prompt_guard_model_output",
        )
    )


async def _check_nova_semantic_calibration(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """Whether the Nova ``semantics:`` thresholds mean what they say on the model scoring them.

    The community rules' thresholds were tuned for all-MiniLM-L6-v2, and a
    model with no measured calibration is scored raw: its rules may fire on
    benign messages, or never. Nothing is reported while no guard is
    connected, the full engine is not installed (the keyword tier ignores
    ``semantics:``), no embedding model is configured or no enabled platform
    rule has a ``semantics:`` pattern. A Nova semantic override that is not the
    model scoring (``nova_engine.semantic_scoring``) is reported as well, and
    the calibration findings then describe the platform model that scores.
    """
    from app.models.nova_rule import NOVA_ACTION_BLOCK, NovaRule
    from app.services import nova_engine
    from app.services.integration_state import any_provider_connected
    from app.services.nova_calibration import CALIBRATIONS, REFERENCE_MODEL, is_calibrated
    from app.services.nova_guard import has_semantic_patterns
    from app.services.prompt_guards import GUARD_PROVIDER

    if not nova_engine.NOVA_HUNTING_AVAILABLE or not await any_provider_connected(db, GUARD_PROVIDER):
        return
    rows = (
        await db.execute(
            select(NovaRule.name, NovaRule.action, NovaRule.rule_text).where(
                NovaRule.enabled.is_(True),
                NovaRule.is_company_managed.is_(True),
                NovaRule.rule_text.ilike("%semantics%"),
            )
        )
    ).all()
    semantic = [(name, action) for name, action, text in rows if has_semantic_patterns(text)]
    scoring = await nova_engine.semantic_scoring(db)
    model = scoring.model
    if not semantic or model is None:
        return
    if scoring.override_ignored:
        why = (
            "was chosen for another embedding provider"
            if scoring.override_reason == nova_engine.OVERRIDE_OF_ANOTHER_PROVIDER
            else "returned no embedding from the platform embedding provider"
        )
        findings.append(
            AuditFinding(
                id="agents.nova_semantic_override_ignored",
                category="agents",
                severity="warning",
                title=f"The Nova semantic model {scoring.override_ignored} is not used",
                description=(
                    f"The semantic model picked on the Nova rules tab {why}, so the {len(semantic)} enabled "
                    f"platform rule(s) with a semantics: pattern are scored on the platform embedding model "
                    f"{model} instead."
                ),
                recommendation=(
                    "Pick a model of the platform embedding provider for the semantics: tier on the Nova "
                    "rules tab, or the platform embedding model."
                ),
            )
        )
    if is_calibrated(model, scoring.dimensions):
        findings.append(
            AuditFinding(
                id="agents.nova_semantics_calibrated",
                category="agents",
                severity="pass",
                title=f"Nova semantics: thresholds are calibrated for {model}",
                description=(
                    f"The {len(semantic)} enabled platform rule(s) with a semantics: pattern are scored "
                    "on the scale their thresholds were written in."
                ),
            )
        )
        return
    if scoring.dimensions is not None:
        model = f"{model} at {scoring.dimensions} dimensions"
    calibrated = ", ".join([REFERENCE_MODEL, *sorted(CALIBRATIONS)])
    blocking = sorted(name for name, action in semantic if action == NOVA_ACTION_BLOCK)
    if blocking:
        findings.append(
            AuditFinding(
                id="agents.nova_semantics_uncalibrated_block_rules",
                category="agents",
                severity="warning",
                title=f"{len(blocking)} Nova block rule(s) rest on semantics: thresholds not calibrated for {model}",
                description=(
                    f"{_listed(blocking)} refuse what they match, and their semantics: thresholds were "
                    f"written for {REFERENCE_MODEL}: on {model}, which has no measured calibration, "
                    "they may refuse benign messages or let through what they should stop."
                ),
                recommendation=(
                    "Switch these rules to Log on the Nova rules tab, or pick a calibrated embedding model "
                    f"for the semantics: tier there ({calibrated}). A model is calibrated with "
                    "scripts/calibrate_nova_semantics.py."
                ),
            )
        )
        return
    findings.append(
        AuditFinding(
            id="agents.nova_semantics_uncalibrated",
            category="agents",
            severity="info",
            title=f"Nova semantics: thresholds are not calibrated for {model}",
            description=(
                f"The {len(semantic)} enabled platform rule(s) with a semantics: pattern have thresholds "
                f"written for {REFERENCE_MODEL} and only log what they match: on {model}, which has no "
                "measured calibration, their matches may be too loose or too strict."
            ),
            recommendation=(
                f"Keep these rules on Log until {model} is calibrated (scripts/calibrate_nova_semantics.py), "
                f"or pick a calibrated embedding model for the semantics: tier on the Nova rules tab ({calibrated})."
            ),
        )
    )


async def _check_prompt_guards(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """Whether a connected prompt guard refuses what its block rules match on each chat surface.

    Agent chat has always refused; the General Assistant and the public
    widget only log unless an option opts them in. Nothing is reported while
    no guard is connected: there is no match to refuse or to log.
    """
    from app.services.integration_state import any_provider_connected
    from app.services.prompt_guards import GUARD_PROVIDER

    if not await any_provider_connected(db, GUARD_PROVIDER):
        return
    if security_option("prompt_guard_block_general_assistant"):
        findings.append(
            AuditFinding(
                id="agents.prompt_guard_general_assistant_blocks",
                category="agents",
                severity="pass",
                title="The prompt guard refuses General Assistant messages its block rules match",
                description="A matched message is refused before the model is called, and audited.",
                control="agents.prompt_guard_blocks_general_assistant",
            )
        )
    else:
        findings.append(
            AuditFinding(
                id="agents.prompt_guard_general_assistant_logs_only",
                category="agents",
                severity="info",
                title="The prompt guard only logs General Assistant messages its block rules match",
                description=(
                    "Blocking is off on the General Assistant: a message a block rule matches is "
                    "audited (security_alert, action allowed) and answered. Agent chat refuses it."
                ),
                recommendation=(
                    where_to_set("prompt_guard_block_general_assistant", "true", sentence_start=True)
                    + " to refuse it with the guard's message. Read the audit log's matches first: "
                    "a false positive then refuses a colleague's question."
                ),
                control="agents.prompt_guard_blocks_general_assistant",
            )
        )
    widgets = (
        await db.execute(
            select(func.count(Agent.id)).where(Agent.enabled.is_(True), Agent.web_enabled.is_(True))
        )
    ).scalar() or 0
    if widgets == 0:
        return
    if security_option("prompt_guard_block_public_widget"):
        findings.append(
            AuditFinding(
                id="agents.prompt_guard_public_widget_blocks",
                category="agents",
                severity="pass",
                title=f"The prompt guard refuses widget messages its block rules match ({widgets} agent(s) embedded)",
                description="A matched visitor message is refused before the model is called, and audited.",
                control="agents.prompt_guard_blocks_public_widget",
            )
        )
        return
    findings.append(
        AuditFinding(
            id="agents.prompt_guard_public_widget_logs_only",
            category="agents",
            severity="info",
            title=f"The prompt guard only logs widget messages its block rules match ({widgets} agent(s) embedded)",
            description=(
                "Blocking is off on the public chat widget: an anonymous visitor's message a block "
                "rule matches is audited (security_alert, action allowed) and answered. Agent chat "
                "refuses it."
            ),
            recommendation=(
                where_to_set("prompt_guard_block_public_widget", "true", sentence_start=True)
                + " to refuse it with the guard's message. Read the audit log's matches first: "
                "a false positive then refuses a visitor."
            ),
            control="agents.prompt_guard_blocks_public_widget",
        )
    )


async def _check_integration_scopes(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """The Scope(s) selections an agent's tools are not held to.

    Enforcing a selection is a choice made on each one, and guidance is the
    default, so this is informational: it lists the selections of the types a
    provider could enforce that nothing enforces, and never moves the score.
    An assignment's selection of a type its agent enforces is a boundary (the
    stricter wins) and is not listed.
    """
    from app.services.element_scope import (
        enforceable_scope_labels,
        enforced_scope_keys,
        guidance_only_scope_keys,
    )

    labels = enforceable_scope_labels()

    def _types(keys: list[str]) -> str:
        return ", ".join(labels[key] for key in keys)

    agent_rows = (
        await db.execute(
            select(Agent.name, Agent.integration_element_bindings, Agent.integration_element_enforcement)
            .where(Agent.enabled.is_(True), Agent.integration_element_bindings.isnot(None))
            .order_by(Agent.name, Agent.id)
        )
    ).all()
    assignment_rows = (
        await db.execute(
            select(
                Assignment.name,
                Assignment.integration_element_bindings,
                Assignment.integration_element_enforcement,
                Agent.name.label("agent_name"),
                Agent.integration_element_bindings.label("agent_bindings"),
                Agent.integration_element_enforcement.label("agent_enforcement"),
            )
            .join(Agent, Agent.id == Assignment.agent_id)
            .where(
                Assignment.enabled.is_(True),
                Agent.enabled.is_(True),
                Assignment.integration_element_bindings.isnot(None),
            )
            .order_by(Agent.name, Assignment.name, Assignment.id)
        )
    ).all()

    listed: list[str] = []
    agents = assignments = 0
    for row in agent_rows:
        keys = guidance_only_scope_keys(row.integration_element_bindings, row.integration_element_enforcement)
        if keys:
            agents += 1
            listed.append(f"agent '{row.name}' ({_types(keys)})")
    for row in assignment_rows:
        keys = guidance_only_scope_keys(
            row.integration_element_bindings,
            row.integration_element_enforcement,
            enforced_elsewhere=enforced_scope_keys(row.agent_bindings, row.agent_enforcement),
        )
        if keys:
            assignments += 1
            listed.append(f"assignment '{row.name}' of agent '{row.agent_name}' ({_types(keys)})")
    if not listed:
        return
    findings.append(
        AuditFinding(
            id="integrations.integration_scopes_guidance_only",
            category="integrations",
            severity="info",
            title=(
                f"Scope(s) selections kept as guidance ({agents} agent(s), "
                f"{assignments} assignment(s))"
            ),
            description=(
                "These GitHub repository, Slack channel, Microsoft Teams channel or "
                "HubSpot CRM object type selections are given to the agent as "
                "instructions, but its integration tools can still reach other "
                f"elements: {_listed(listed)}. Guidance is the default, chosen on "
                "each selection."
            ),
            recommendation=(
                "Turn on 'Enforce as a boundary' next to a selection in its Scope(s) "
                "card where the agent's tools, and the agents it hands work to, must "
                "be held to it."
            ),
        )
    )


async def _check_runners(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """Runner file confinement and read bounds, whose safe settings are opt-in."""
    active = (
        await db.execute(select(func.count(Runner.id)).where(Runner.is_active.is_(True)))
    ).scalar() or 0
    bound = settings.runner_max_result_bytes
    if bound <= 0:
        findings.append(
            AuditFinding(
                id="tools.runner_reads_unbounded",
                category="tools",
                severity="warning" if active else "info",
                title="Runner file reads are not bounded",
                description=(
                    f"RUNNER_MAX_RESULT_BYTES is {bound}: every "
                    "non-empty runner file result is refused at upload, and a file read is "
                    "delivered with the size the agent asks for, so an agent can "
                    "make a runner read a large file into memory."
                    + ("" if active else " No runner is active today.")
                ),
                recommendation="Set RUNNER_MAX_RESULT_BYTES to a positive size (default 25000000).",
                control="tools.runner_result_bound",
            )
        )
    elif active:
        # Text up to RUNNER_INLINE_TEXT_BYTES comes back inline, which the
        # upload limit never caps: under a smaller limit a read asks for that.
        if bound >= RUNNER_INLINE_TEXT_BYTES:
            read = "a file read never asks a runner for more,"
        else:
            read = (
                f"a file read never asks a runner for more than {RUNNER_INLINE_TEXT_BYTES:,} "
                "bytes - text up to that size comes back inline, which the setting does not cap -"
            )
        findings.append(
            AuditFinding(
                id="tools.runner_reads_bounded",
                category="tools",
                severity="pass",
                title=f"Runner file reads are bounded ({active} active runner(s))",
                description=(
                    f"RUNNER_MAX_RESULT_BYTES is {bound:,}: {read} and a larger file result "
                    "is refused."
                ),
                control="tools.runner_result_bound",
            )
        )
    if active == 0:
        return
    runners = (
        await db.execute(
            select(Runner.allowed_paths, Runner.capabilities).where(
                Runner.is_active.is_(True),
                Runner.permission_mode != PERMISSION_MODE_FULL_CONTROL,
            )
        )
    ).all()
    # Only a runner that can run file operations, and has a folder to hold
    # them to, has a link to escape through.
    confined = sum(
        1
        for paths, capabilities in runners
        if "file_ops" in (capabilities or [])
        and any(isinstance(path, str) and path.strip() for path in paths or [])
    )
    if confined == 0:
        return
    if security_option("runner_strict_path_confinement"):
        findings.append(
            AuditFinding(
                id="tools.runner_path_confinement_strict",
                category="tools",
                severity="pass",
                title=f"Runner allowed folders cannot be left through a link ({confined} runner(s))",
                description=(
                    f"{option_reference('runner_strict_path_confinement')} is on: a restricted runner of "
                    "this release or later refuses a file command whose path leads outside its allowed "
                    "folders through a symbolic link, junction or mount point."
                ),
                control="tools.runner_strict_path_confinement",
            )
        )
        return
    findings.append(
        AuditFinding(
            id="tools.runner_path_links",
            category="tools",
            severity="warning",
            title=f"Runner allowed folders can be left through a link ({confined} runner(s))",
            description=(
                "A file command whose path lies inside a restricted runner's "
                "allowed folders but leads outside them through a symbolic "
                "link, junction or mount point still runs, with a warning in "
                "its result. Anything able to create a link in an allowed "
                "folder (a cloned repository, an extracted archive) can point "
                "it at another folder of the machine."
            ),
            recommendation=(
                where_to_set("runner_strict_path_confinement", "true", sentence_start=True)
                + " to refuse such commands (runners from this release or later)."
            ),
            control="tools.runner_strict_path_confinement",
        )
    )


def _names(items: list[str], limit: int = 5) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


async def _check_relays(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """On-premises relays: what they may reach, their version, whether a routed group has one online."""
    from app.models.relay import Relay, RelayGroup
    from app.services.relays import (
        group_relay_counts,
        is_relay_online,
        relay_version_below,
        relays_active,
        routed_resource_counts,
    )

    if not relays_active():
        return
    relays = (await db.execute(select(Relay).where(Relay.is_active.is_(True)))).scalars().all()
    groups = (await db.execute(select(RelayGroup))).scalars().all()
    if not relays and not groups:
        return
    group_names = {g.id: g.name for g in groups}

    def label(relay: Relay) -> str:
        return f"{relay.name} ({group_names.get(relay.group_id, 'unknown group')})"

    if relays:
        open_relays = [label(r) for r in relays if r.allows_all_destinations]
        if open_relays:
            findings.append(
                AuditFinding(
                    id="network.relay_allows_all_destinations",
                    category="network",
                    severity="warning",
                    title=f"{len(open_relays)} relay(s) allow every destination",
                    description=(
                        f"The local allow-list of {_names(open_relays)} contains '*': each reaches anything "
                        "its network reaches (never loopback, link-local or cloud metadata addresses). The "
                        "relay's own list is the boundary the platform cannot widen; a group's platform-side "
                        "list narrows what is asked of it, not what it would carry."
                    ),
                    recommendation=(
                        "Replace '*' in allowed_destinations of each relay's relay.toml (or "
                        "XTM_RELAY_ALLOWED_DESTINATIONS) with the hosts and ports it must reach, e.g. "
                        "splunk.corp.local:8089 or 10.20.0.0/16:443. The relay reloads the file when it changes."
                    ),
                )
            )
        else:
            findings.append(
                AuditFinding(
                    id="network.relay_destinations_restricted",
                    category="network",
                    severity="pass",
                    title=f"Every relay reaches only the destinations it lists ({len(relays)} relay(s))",
                    description="No active relay allows every destination: each refuses what its local list does not name.",
                )
            )

        floor = (settings.relay_min_supported_version or "").strip()
        if not floor:
            findings.append(
                AuditFinding(
                    id="network.relay_version_floor_unset",
                    category="network",
                    severity="info",
                    title="Relays of any version are accepted",
                    description=(
                        "RELAY_MIN_SUPPORTED_VERSION is empty: a relay that stopped updating keeps carrying "
                        "requests, whatever fixes it misses."
                    ),
                    recommendation="Set RELAY_MIN_SUPPORTED_VERSION to the oldest relay version you support.",
                    control="network.relay_version_floor",
                )
            )
        else:
            outdated = [label(r) for r in relays if relay_version_below(r.agent_version, floor)]
            if outdated:
                findings.append(
                    AuditFinding(
                        id="network.relay_outdated",
                        category="network",
                        severity="warning",
                        title=f"{len(outdated)} relay(s) are older than {floor}",
                        description=(
                            f"{_names(outdated)} run a version older than RELAY_MIN_SUPPORTED_VERSION: they "
                            "are refused when they connect and carry no request until updated."
                        ),
                        recommendation=(
                            "Let them update themselves (auto_update, on by default outside containers), "
                            "re-run the installer, or pull a newer xtmone/relay image."
                        ),
                        control="network.relay_version_floor",
                    )
                )
            else:
                findings.append(
                    AuditFinding(
                        id="network.relays_current",
                        category="network",
                        severity="pass",
                        title=f"Every relay runs {floor} or later",
                        description="No active relay is older than RELAY_MIN_SUPPORTED_VERSION.",
                        control="network.relay_version_floor",
                    )
                )

    if not groups:
        return
    ids = [g.id for g in groups]
    counts = await group_relay_counts(db, ids)
    routed = await routed_resource_counts(db, ids)
    in_use = [g for g in groups if routed.get(g.id, 0) > 0]
    if not in_use:
        return
    offline = [g.name for g in in_use if counts.get(g.id, (0, 0))[0] == 0]
    if offline:
        findings.append(
            AuditFinding(
                id="network.relay_group_offline_in_use",
                category="network",
                severity="warning",
                title=f"{len(offline)} relay group(s) in use have no relay online",
                description=(
                    f"Resources are routed through {_names(offline)}, but none of its relays is connected: "
                    "their requests fail (a routed resource never falls back to direct)."
                ),
                recommendation="Check the relays of these groups (xtm-one-relay status on their machines), or enroll one.",
            )
        )
    else:
        findings.append(
            AuditFinding(
                id="network.relay_groups_online",
                category="network",
                severity="pass",
                title=f"Every relay group in use has a relay online ({len(in_use)} group(s))",
                description="Each group a resource is routed through has at least one connected relay.",
            )
        )
    online_now = {r.group_id for r in relays if is_relay_online(r)}
    single = [g.name for g in in_use if counts.get(g.id, (0, 0))[1] == 1 and g.id in online_now]
    if single:
        findings.append(
            AuditFinding(
                id="network.relay_single_instance_group",
                category="network",
                severity="info",
                title=f"{len(single)} relay group(s) in use rely on a single relay",
                description=(
                    f"{_names(single)} have one relay: when it stops, every resource routed through the group "
                    "stops reaching its destinations."
                ),
                recommendation="Enroll a second relay in each group, on another machine; the group balances requests across them.",
            )
        )


async def _check_channel_bots(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """Check channel bot security settings."""
    bots = (await db.execute(select(ChannelBot))).scalars().all()

    anonymous_bots = [b for b in bots if b.allow_anonymous]
    if anonymous_bots:
        findings.append(
            AuditFinding(
                id="channels.anonymous_access",
                category="channels",
                severity="warning",
                title=f"{len(anonymous_bots)} channel bot(s) allow anonymous access",
                description="Anonymous users can interact with agents through these bots without authentication.",
                recommendation="Review whether anonymous access is necessary for each bot. Disable when not required.",
            )
        )
    elif bots:
        findings.append(
            AuditFinding(
                id="channels.no_anonymous_access",
                category="channels",
                severity="pass",
                title=f"No channel bot allows anonymous access ({len(bots)} bot(s))",
                description="Every channel bot answers only people who have an account on this platform.",
            )
        )

    no_receive_restriction = [
        b
        for b in bots
        if not b.allowed_receive_channels
        and b.provider in ("slack_bot", "teams_bot", "discord_bot")
    ]
    if no_receive_restriction:
        findings.append(
            AuditFinding(
                id="channels.unrestricted_receive",
                category="channels",
                severity="info",
                title=f"{len(no_receive_restriction)} channel bot(s) without receive channel restrictions",
                description="These bots will process messages from any channel they can access.",
                recommendation="Consider restricting receive channels to limit bot exposure.",
            )
        )


_TEAMS_GRAPH_NOTIFICATION_PATH = "/api/channels/teams/graph-notifications"


def _teams_graph_health_text(health) -> str:
    """The monitored channels and the recent unverified notifications, as the findings say them."""
    if health is None:
        return " The subscription records could not be read (Redis did not answer)."
    text = f" {health.monitored} private or shared channel(s) are monitored"
    if health.checked < health.monitored:
        text += f" (the first {health.checked} were checked)"
    text += (
        f": {health.derivable} subscription(s) still use the derivable clientState, "
        f"{health.unreadable} record(s) cannot be read or decrypted, and "
        f"{health.missing} channel(s) have no subscription record."
    )
    if health.unverified:
        text += (
            f" {health.unverified} notification(s) were acted on without proving their subscription "
            f"in the last 7 days (the last at {health.unverified_last_at or 'an unknown time'})."
        )
    else:
        text += " No notification was acted on without proving its subscription in the last 7 days."
    return text


async def _check_teams_graph_notifications(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """Whether the Teams Graph notification endpoint acts only on proven notifications.

    In use while a Teams bot is configured: the endpoint is public, and by
    default it still acts on every notification earlier releases acted on. The
    switch is the one of the integration notification endpoints.
    """
    strict = bool(security_option("integration_notifications_require_verification"))
    teams_bots = (
        await db.execute(select(func.count(ChannelBot.id)).where(ChannelBot.provider == "teams_bot"))
    ).scalar() or 0
    if not teams_bots and not strict:
        return
    health = None
    try:
        from app.channels.teams import graph_notification_health

        health = await asyncio.wait_for(graph_notification_health(), timeout=_REDIS_PING_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning(
            "Security audit: cannot read the Teams Graph subscriptions: %s", format_exception_message(exc)
        )
    if strict:
        findings.append(
            AuditFinding(
                id="channels.teams_graph_notifications_verified",
                category="channels",
                severity="pass",
                title="Teams channel notifications must prove their subscription",
                description=(
                    f"{option_reference('integration_notifications_require_verification')} is on: "
                    f"{_TEAMS_GRAPH_NOTIFICATION_PATH} acts only on a Microsoft Graph notification that "
                    "carries the subscription id and the random clientState recorded for its channel, and "
                    "the maintenance pass replaces the subscriptions whose clientState is derivable or "
                    "cannot be decrypted." + _teams_graph_health_text(health)
                ),
                control="integrations.notifications_require_verification",
            )
        )
        return
    findings.append(
        AuditFinding(
            id="channels.teams_graph_notifications_unverified",
            category="channels",
            severity="warning",
            title="Teams channel notifications are accepted unverified",
            description=(
                f"{_TEAMS_GRAPH_NOTIFICATION_PATH} still acts on a Microsoft Graph notification that does "
                "not prove the subscription recorded for its channel whenever earlier releases acted on it: "
                "one carrying the derivable clientState of the subscriptions created before 2026-07-02 "
                "(copilot_ followed by the team id) for a channel with no readable record or a record from "
                "before 2026-07-02, or the channel's clientState under another subscription id or tenant. "
                "Anyone who knows a team id can then make the platform read a channel message with its "
                "Microsoft Graph credentials and answer it as a thread follow-up." + _teams_graph_health_text(health)
            ),
            recommendation=(
                "To refuse every unverified notification, "
                + where_to_set("integration_notifications_require_verification", "true")
                + ". The next subscription maintenance pass then replaces the subscriptions whose clientState "
                "is derivable or cannot be decrypted, and the follow-ups a refused notification held back are "
                "read back from Microsoft Graph once the channel has a subscription again."
            ),
            control="integrations.notifications_require_verification",
        )
    )


#: How many assignments / flows a finding names before summarising.
_MAX_LISTED_TRIGGERS = 50


def _listed_triggers(rows: list[tuple]) -> str:
    listed = [f"{name} ({row_id})" for row_id, name in rows[:_MAX_LISTED_TRIGGERS]]
    if len(rows) > len(listed):
        listed.append(f"and {len(rows) - len(listed)} more")
    return "; ".join(listed)


async def _check_trigger_integration_delivery(
    db: AsyncSession, findings: list[AuditFinding]
) -> None:
    """The integration-event triggers whose acting user cannot see the
    trigger's integration. Their events are delivered all the same -
    nothing is withheld at run time - so each kind found is a warning, and a
    pass when every trigger in use acts as a user who can see its integration."""
    from app.services.trigger_integration_access import (
        assignment_trigger_visibility,
        flow_trigger_visibility,
    )

    assignments = await assignment_trigger_visibility(db)
    flows = await flow_trigger_visibility(db)
    if assignments.unseen:
        findings.append(
            AuditFinding(
                id="access.assignment_trigger_integration_not_visible",
                category="access",
                severity="warning",
                title=(
                    f"{len(assignments.unseen)} integration-event assignment(s) receive "
                    "events from an integration their acting user cannot see"
                ),
                description=(
                    "The user their runs act as (the \"run as\" user, else the "
                    "owner) cannot see the integration their trigger reads, and its "
                    "events still reach those runs."
                    f" Assignments: {_listed_triggers(assignments.unseen)}."
                ),
                recommendation=(
                    "Share the integration with that user, or edit the trigger to "
                    "name an integration they can see."
                ),
            )
        )
    if flows.unseen:
        findings.append(
            AuditFinding(
                id="access.flow_trigger_integration_not_visible",
                category="access",
                severity="warning",
                title=(
                    f"{len(flows.unseen)} active flow(s) receive events from an "
                    "integration their owner cannot see"
                ),
                description=(
                    "The flow's owner cannot see the integration its trigger node "
                    "reads, and its events still reach the flow's runs."
                    f" Flows: {_listed_triggers(flows.unseen)}."
                ),
                recommendation=(
                    "Share the integration with the flow's owner, or edit the "
                    "trigger node to name an integration they can see."
                ),
            )
        )
    if not (assignments.unseen or flows.unseen) and (assignments.checked or flows.checked):
        findings.append(
            AuditFinding(
                id="access.trigger_integrations_visible",
                category="access",
                severity="pass",
                title="Every integration-event trigger acts as a user who can see its integration",
                description=(
                    f"{assignments.checked} assignment(s) and {flows.checked} active flow(s) "
                    "receive integration events, each for a user who can see the "
                    "integration they come from."
                ),
            )
        )


async def _check_kb_sync_source_integrations(
    db: AsyncSession, findings: list[AuditFinding]
) -> None:
    """The knowledge base sync sources whose user cannot see the integration
    they are bound to. They sync with it all the same - nothing is
    withheld at run time - so a warning, and a pass when every bound source
    syncs as a user who can see its integration."""
    from app.services.trigger_integration_access import sync_source_integration_visibility

    sources = await sync_source_integration_visibility(db)
    if sources.unseen:
        findings.append(
            AuditFinding(
                id="access.kb_sync_source_integration_not_visible",
                category="access",
                severity="warning",
                title=(
                    f"{len(sources.unseen)} knowledge base sync source(s) sync with an "
                    "integration their user cannot see"
                ),
                description=(
                    "The user a source syncs as (the user who configured it, else the "
                    "knowledge base's owner) cannot see the integration it is bound to, "
                    "and every sync of it still reads through that integration."
                    f" Sources: {_listed_triggers(sources.unseen)}."
                ),
                recommendation=(
                    "Share the integration with that user, or bind the source to an "
                    "integration they can see."
                ),
            )
        )
    elif sources.checked:
        findings.append(
            AuditFinding(
                id="access.kb_sync_source_integrations_visible",
                category="access",
                severity="pass",
                title="Every knowledge base sync source syncs as a user who can see its integration",
                description=(
                    f"{sources.checked} sync source(s) are bound to an integration, each "
                    "syncing as a user who can see it."
                ),
            )
        )


#: How many lent personal credentials the finding names; the others are counted.
_LENT_LISTED = 20

#: How a lent personal credential reaches other people, as the finding names it.
_LENT_THROUGH = {
    "group": "shared with groups",
    "people": "shared with people",
    "owner": "its owner, with an account another person connected",
    "agent": "agents and what they attach",
    "assignment": "assignment overrides, triggers and per-user bindings",
    "flow": "flows and what they attach",
    "knowledge_base": "knowledge base sync sources",
    "skill": "skill sources",
    "general_assistant": "the General Assistant",
    "personal_assistant": "people's own General Assistant lists",
}


def _lent_reach(reached) -> str:
    """Who beyond its holder acts with a lent credential (an ``integration_exposure.Audience``)."""
    parts = []
    if reached.everyone:
        parts.append("every user")
    elif reached.user_ids:
        parts.append(f"{len(reached.user_ids)} other person(s)")
    if reached.public_link:
        parts.append("anyone with a public widget link")
    if reached.anonymous_channels:
        parts.append(f"people with no account on {', '.join(sorted(reached.anonymous_channels))}")
    if reached.mcp_key_holders:
        parts.append("holders of the platform MCP key")
    if reached.a2a_partners:
        parts.append("A2A partners")
    if reached.webhook_callers:
        parts.append("holders of a webhook URL")
    return _joined(parts)


async def _check_shared_personal_credentials(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """The personal credentials other people act with, and through what.

    A share of the integration lends its account once its holder consented,
    and an entity bound to it lends it to its whole audience: its runs use it
    as bound (``app.services.integration_credentials``). Each lending was a
    choice the UI confirmed, so this is context; the integration's Sharing and
    access section is where to review it.
    """
    from app.services.integration_credentials import WITHHOLD_CREDENTIALS_SWITCH
    from app.services.integration_exposure import lent_personal_credentials

    lent, truncated = await lent_personal_credentials(db)
    if not lent:
        return
    lines = [
        f"{credential.name} ({_provider_name(credential.provider)}, {credential.integration_id})"
        + (f" of {credential.holder_name}" if credential.holder_name else "")
        + f": {', '.join(_LENT_THROUGH.get(kind, kind) for kind in credential.kinds)}"
        + f" - {_lent_reach(credential.reached)}"
        for credential in lent[:_LENT_LISTED]
    ]
    if len(lent) > len(lines):
        lines.append(f"and {len(lent) - len(lines)} more")
    findings.append(
        AuditFinding(
            id="access.personal_credentials_lent",
            category="access",
            severity="info",
            title=f"{len(lent)} personal credential(s) are used by other people",
            description=(
                "These integrations hold a person's own credential - a connected account, a personal access "
                "token, an API key - and other people act with it at the provider, as that person: through a "
                "share of the integration, or an agent, assignment, flow, knowledge base, skill or the General "
                f"Assistant that holds it. {'; '.join(lines)}."
                + (" Some references were beyond the lookup's bound, so others may be missing." if truncated else "")
            ),
            recommendation=(
                "Review each in its integration's Sharing and access section. Where others should not act as its "
                f'holder, turn on "{WITHHOLD_CREDENTIALS_SWITCH}" in its configuration (a connected account), '
                "switch the agent to per-user integrations, or give the work a company-managed integration of its "
                "own."
            ),
        )
    )


async def _check_assignments(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """Check assignment security."""
    # Both counts are computed in SQL (COUNT + conditional SUM with a JSON
    # key check) so large tenants never load every webhook row into memory
    # just to derive two integers. A row PostgreSQL cannot parse (a lone
    # surrogate or NUL escape) is skipped before the key check: it failed the
    # whole audit. Those rows are read as text and their secretless
    # ones counted below.
    secretless_expr = case(
        (_sql_can_parse(func.coalesce(Assignment.trigger_config["webhook_secret"].as_string(), "") == ""), 1),
        else_=0,
    )
    row = (
        await db.execute(
            select(
                func.count(Assignment.id),
                func.coalesce(func.sum(secretless_expr), 0),
            ).where(
                Assignment.enabled.is_(True),
                Assignment.trigger_type == "webhook",
            )
        )
    ).one()
    webhook_assignments = int(row[0] or 0)
    secretless = int(row[1] or 0) + sum(
        1 for config in await _unparsed_trigger_configs(db, "webhook") if config.get("webhook_secret") in (None, "")
    )

    if webhook_assignments > 0:
        findings.append(
            AuditFinding(
                id="assignments.webhook_triggers",
                category="assignments",
                severity="info",
                title=f"{webhook_assignments} active webhook-triggered assignment(s)",
                description=(
                    "Webhook endpoints are publicly accessible. Deliveries are "
                    "verified against the assignment's webhook secret when one "
                    "is configured; assignments without a secret are "
                    "authenticated by URL path only."
                ),
                recommendation=(
                    "Keep webhook URLs secret and ensure every webhook "
                    "assignment has a webhook_secret configured (new webhook "
                    "assignments generate one automatically)."
                ),
                control="assignments.webhook_require_secret",
            )
        )

    if secretless > 0 and security_option("webhook_require_secret"):
        findings.append(
            AuditFinding(
                id="assignments.webhook_without_secret_refused",
                category="assignments",
                severity="info",
                title=f"{secretless} webhook-triggered assignment(s) without a secret refuse every delivery",
                description=(
                    f"{option_reference('webhook_require_secret')} is on, so every delivery to these "
                    "assignments is refused (401): they cannot be triggered until each has a webhook "
                    "secret the sender signs with."
                ),
                recommendation=(
                    "Configure a webhook_secret in each assignment's trigger settings, or disable the "
                    "assignments that no longer receive deliveries."
                ),
                control="assignments.webhook_require_secret",
            )
        )
    elif secretless > 0:
        findings.append(
            AuditFinding(
                id="assignments.webhook_without_secret",
                category="assignments",
                severity="warning",
                title=f"{secretless} webhook-triggered assignment(s) without a webhook secret",
                description=(
                    "These assignment webhook endpoints accept unsigned requests: anyone who "
                    "discovers the assignment UUID can trigger agentic execution with the "
                    "agent's full tool set."
                ),
                recommendation=(
                    "Configure a webhook_secret in each assignment's trigger settings (new "
                    "webhook assignments generate one automatically). To reject secretless "
                    "webhook deliveries, "
                    + where_to_set("webhook_require_secret", "true")
                    + "."
                ),
                control="assignments.webhook_require_secret",
            )
        )
    elif webhook_assignments > 0:
        findings.append(
            AuditFinding(
                id="assignments.webhook_secrets_configured",
                category="assignments",
                severity="pass",
                title="All webhook-triggered assignments have a webhook secret",
                description="Every enabled webhook assignment requires a signed delivery.",
                control="assignments.webhook_require_secret",
            )
        )


def _config_text(key: str):
    return func.coalesce(func.trim(Assignment.trigger_config[key].as_string()), "")


def _stored_config_text(config: dict, key: str) -> str:
    """``_config_text`` for a config decoded in Python (a row ``_sql_can_parse`` leaves out)."""
    value = config.get(key)
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else json.dumps(value)


async def _check_event_matching(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """Event triggers still on the prefix match, and the legacy ``event_type`` key."""
    unmarked_config = _sql_can_parse(_config_text(EVENT_MATCHING_KEY) != EVENT_MATCHING_EXACT)
    # Read only on a trigger marked exact (``models.assignment.read_trigger_config``).
    unread_legacy_key = and_(
        Assignment.trigger_type == "integration_event",
        _sql_can_parse(
            and_(
                _config_text(LEGACY_TRIGGER_EVENT_KEY) != "",
                _config_text(TRIGGER_EVENT_KEY) == "",
                _config_text(EVENT_MATCHING_KEY) != EVENT_MATCHING_EXACT,
            )
        ),
    )
    row = (
        await db.execute(
            select(
                func.count(Assignment.id),
                func.coalesce(func.sum(case((unmarked_config, 1), else_=0)), 0),
                func.coalesce(func.sum(case((unread_legacy_key, 1), else_=0)), 0),
            ).where(
                Assignment.enabled.is_(True),
                Assignment.trigger_type.in_(sorted(EVENT_MATCHING_TRIGGER_TYPES)),
            )
        )
    ).one()
    triggers, unmarked, unread = (int(value or 0) for value in row)
    for trigger_type in sorted(EVENT_MATCHING_TRIGGER_TYPES):
        for config in await _unparsed_trigger_configs(db, trigger_type):
            if _stored_config_text(config, EVENT_MATCHING_KEY) == EVENT_MATCHING_EXACT:
                continue
            unmarked += 1
            if (
                trigger_type == "integration_event"
                and _stored_config_text(config, LEGACY_TRIGGER_EVENT_KEY)
                and not _stored_config_text(config, TRIGGER_EVENT_KEY)
            ):
                unread += 1
    everywhere = bool(security_option("webhook_exact_event_matching"))

    if unmarked and not everywhere:
        findings.append(
            AuditFinding(
                id="assignments.legacy_event_matching",
                category="assignments",
                severity="info",
                title=f"{unmarked} event trigger(s) on legacy prefix matching",
                description=(
                    "These integration-event or webhook triggers were saved before exact event "
                    "matching and keep the prefix match they always had: a selection such as "
                    "pull_request also runs on pull_request_review, and each entry of an events "
                    "list on any event whose name starts with it. A selection no other event name "
                    "starts with matches the same events either way. The first delivery each one "
                    "runs on only by prefix is logged."
                ),
                recommendation=(
                    "Switch each one with Use exact matching on its assignment's trigger card, or "
                    + where_to_set("webhook_exact_event_matching", "true")
                    + "."
                ),
                control="assignments.exact_event_matching",
            )
        )
    elif triggers:
        findings.append(
            AuditFinding(
                id="assignments.event_matching_exact",
                category="assignments",
                severity="pass",
                title="Every event trigger matches its event exactly",
                description=(
                    f"{option_reference('webhook_exact_event_matching')} is on: every "
                    "integration-event and webhook trigger runs only on the events it selects."
                    if everywhere
                    else "Every enabled integration-event and webhook trigger was created or given "
                    "its events on a version with exact event matching."
                ),
                control="assignments.exact_event_matching",
            )
        )

    if unread:
        findings.append(
            AuditFinding(
                id="assignments.legacy_event_type_key",
                category="assignments",
                severity="info",
                title=f"{unread} integration-event trigger(s) name their event with the old event_type key",
                description=(
                    "The agent builder once stored an integration-event trigger's event under "
                    "event_type, a key the platform does not read on these triggers: each runs on "
                    "every event its provider delivers by webhook, and the poller never runs it. "
                    "They keep working as they always did."
                ),
                recommendation=(
                    "Open each assignment and choose Use exact matching on its trigger card: it then "
                    "runs only on the event it names, a polled one from its next new event on."
                ),
            )
        )


def _sql_can_parse(condition: ColumnElement[bool]) -> ColumnElement[bool]:
    """*condition* on ``trigger_config``, false for a row PostgreSQL cannot parse.

    A lone surrogate or ``\\u0000`` escape (``core/json_sql.py``). Those rows
    are counted from their stored text (``_unparsed_trigger_configs``).
    """
    return when_json_parseable(Assignment.trigger_config, condition)


async def _unparsed_trigger_configs(db: AsyncSession, trigger_type: str) -> list[dict]:
    """The ``trigger_config`` of the enabled *trigger_type* assignments ``_sql_can_parse`` leaves out.

    Read as stored text (``CAST(... AS TEXT)``) and decoded in Python, where a
    lone surrogate is only a character. A document that is not an object reads
    as ``{}``, as ``->>`` finds no key in it.
    """
    stored = cast(Assignment.trigger_config, Text)
    rows = (
        await db.execute(
            select(stored.label("trigger_config_text")).where(
                Assignment.enabled.is_(True),
                Assignment.trigger_type == trigger_type,
                json_unparseable(Assignment.trigger_config),
            )
        )
    ).all()
    configs: list[dict] = []
    for row in rows:
        try:
            config = json.loads(row.trigger_config_text or "")
        except ValueError:
            config = None
        configs.append(config if isinstance(config, dict) else {})
    return configs


async def _check_integration_notifications(
    db: AsyncSession, findings: list[AuditFinding]
) -> None:
    """Check that the shared Slack events endpoint authenticates its sender."""
    slack_assignments = (
        await db.execute(
            select(func.count(Assignment.id)).where(
                Assignment.enabled.is_(True),
                Assignment.trigger_type == "integration_event",
                _sql_can_parse(
                    and_(
                        Assignment.trigger_config["provider"].as_string() == "slack",
                        Assignment.trigger_config["event"].as_string() == "new_slack_message",
                    )
                ),
            )
        )
    ).scalar() or 0
    unparsed = await _unparsed_trigger_configs(db, "integration_event")
    slack_assignments += sum(
        1 for config in unparsed if config.get("provider") == "slack" and config.get("event") == "new_slack_message"
    )
    state = await load_slack_signing_secrets(db)
    strict = security_option("integration_notifications_require_verification")

    if state.unusable or state.unreadable:
        broken = ", ".join((*state.unusable, *state.unreadable))
        findings.append(
            AuditFinding(
                id="integrations.slack_signing_secret_unusable",
                category="integrations",
                severity="warning",
                title="A Slack signing secret cannot be used",
                description=(
                    f"The Slack signing secret configuration of {broken} is not a text "
                    "value or cannot be decrypted, so it can never verify a delivery: "
                    + (
                        f"{SLACK_NOTIFICATION_PATH} refuses their real-time events, and their "
                        "new messages are polled every 5 minutes instead."
                        if strict
                        else f"{SLACK_NOTIFICATION_PATH} accepts their events without a verified "
                        "signature, as before signatures were checked."
                    )
                ),
                recommendation=f"Re-enter the signing secret. {SLACK_SIGNATURE_SETUP_HINT}",
                control="integrations.notifications_require_verification",
            )
        )
    if slack_assignments > 0 and state.unknown and strict:
        findings.append(
            AuditFinding(
                id="integrations.slack_integrations_polled",
                category="integrations",
                severity="info",
                title="Real-time Slack events are off for some integrations",
                description=(
                    f"{slack_assignments} active assignment(s) are triggered by new Slack "
                    f"messages, and {len(state.unknown)} Slack integration(s) have no "
                    f"signing secret stored for their Slack app: {SLACK_NOTIFICATION_PATH} "
                    "refuses their events, so they receive their new messages through "
                    "polling (every 5 minutes) instead of in real time."
                ),
                recommendation=(
                    f"{SLACK_SIGNATURE_SETUP_HINT} Then re-enable Event Subscriptions in the "
                    "Slack app if Slack turned them off after refused deliveries."
                ),
                control="integrations.notifications_require_verification",
            )
        )
    elif slack_assignments > 0 and state.unknown:
        findings.append(
            AuditFinding(
                id="integrations.slack_integrations_unverified",
                category="integrations",
                severity="warning",
                title="Slack events are accepted unverified for some integrations",
                description=(
                    f"{slack_assignments} active assignment(s) are triggered by new Slack "
                    f"messages, and {len(state.unknown)} Slack integration(s) have no "
                    f"signing secret stored for their Slack app: {SLACK_NOTIFICATION_PATH} "
                    "still accepts events for them without a verified signature, as before "
                    "signatures were checked, so anyone who can reach that URL can post "
                    "events that trigger their assignments."
                ),
                recommendation=(
                    f"{SLACK_SIGNATURE_SETUP_HINT} To refuse every unverified delivery, "
                    + where_to_set("integration_notifications_require_verification", "true")
                    + "."
                ),
                control="integrations.notifications_require_verification",
            )
        )
    elif not (state.unusable or state.unreadable) and any(app.integration_ids for app in state.apps):
        findings.append(
            AuditFinding(
                id="integrations.slack_events_verified",
                category="integrations",
                severity="pass",
                title="Slack events endpoint verifies signatures",
                description=(
                    f"Deliveries to {SLACK_NOTIFICATION_PATH} must carry a valid "
                    "X-Slack-Signature from the signing secret of the Slack app each "
                    "integration runs on, and reach only that app's integrations."
                    + (
                        ""
                        if strict
                        else " An assignment connected to no Slack integration still receives "
                        "unverified deliveries, as before signatures were checked, until "
                        f"{option_reference('integration_notifications_require_verification')} "
                        "is on."
                    )
                ),
                control="integrations.notifications_require_verification",
            )
        )
    await _check_unscoped_admin_app_deliveries(
        db, findings, slack_assignments, state, strict=strict, unparsed=unparsed
    )
    await _check_provider_webhook_verification(db, findings, strict=strict)


async def _check_unscoped_admin_app_deliveries(
    db: AsyncSession,
    findings: list[AuditFinding],
    slack_assignments: int,
    state,
    *,
    strict: bool,
    unparsed: list[dict],
) -> None:
    """What the default keeps accepting as before, said once."""
    if slack_assignments > 0 and not state.apps and not state.unknown and not strict:
        findings.append(
            AuditFinding(
                id="integrations.slack_events_unverified",
                category="integrations",
                severity="warning",
                title="Slack events are accepted unverified",
                description=(
                    f"{slack_assignments} active assignment(s) are triggered by new Slack messages "
                    f"and no Slack signing secret is stored, so {SLACK_NOTIFICATION_PATH} accepts "
                    "events without a verified signature, as before signatures were checked."
                ),
                recommendation=(
                    f"{SLACK_SIGNATURE_SETUP_HINT} To refuse every unverified delivery, "
                    + where_to_set("integration_notifications_require_verification", "true")
                    + "."
                ),
                control="integrations.notifications_require_verification",
            )
        )
    linkedin_assignments = (
        await db.execute(
            select(func.count(Assignment.id)).where(
                Assignment.enabled.is_(True),
                Assignment.trigger_type == "integration_event",
                _sql_can_parse(Assignment.trigger_config["provider"].as_string() == "linkedin"),
            )
        )
    ).scalar() or 0
    linkedin_assignments += sum(1 for config in unparsed if config.get("provider") == "linkedin")
    linkedin_secret = (
        await db.execute(
            select(OAuthDefault.client_secret_enc).where(OAuthDefault.provider == "linkedin")
        )
    ).scalar_one_or_none()
    if linkedin_assignments > 0 and not linkedin_secret and strict:
        findings.append(
            AuditFinding(
                id="integrations.linkedin_notifications_dropped",
                category="integrations",
                severity="info",
                title="LinkedIn notifications are dropped",
                description=(
                    f"{linkedin_assignments} active assignment(s) are triggered by LinkedIn "
                    "notifications, but no LinkedIn OAuth default client secret is configured: "
                    "/api/v1/integrations/notifications/linkedin cannot verify a notification, "
                    "so it hands none to them."
                ),
                recommendation="Configure the LinkedIn app in Settings > Integrations > OAuth Defaults.",
                control="integrations.notifications_require_verification",
            )
        )
    elif linkedin_assignments > 0 and not linkedin_secret:
        findings.append(
            AuditFinding(
                id="integrations.linkedin_notifications_unverified",
                category="integrations",
                severity="warning",
                title="LinkedIn notifications are accepted unverified",
                description=(
                    f"{linkedin_assignments} active assignment(s) are triggered by LinkedIn "
                    "notifications and no LinkedIn OAuth default client secret is configured, so "
                    "/api/v1/integrations/notifications/linkedin accepts them without a verified "
                    "signature, as before signatures were required."
                ),
                recommendation=(
                    "Configure the LinkedIn app in Settings > Integrations > OAuth Defaults. To drop unverified "
                    "notifications, "
                    + where_to_set("integration_notifications_require_verification", "true")
                    + "."
                ),
                control="integrations.notifications_require_verification",
            )
        )
    elif linkedin_assignments > 0:
        findings.append(
            AuditFinding(
                id="integrations.linkedin_notifications_verified",
                category="integrations",
                severity="pass",
                title="LinkedIn notifications are verified",
                description=(
                    f"{linkedin_assignments} active assignment(s) are triggered by LinkedIn notifications, and "
                    "/api/v1/integrations/notifications/linkedin hands them only a notification signed with the "
                    "LinkedIn OAuth default client secret."
                ),
                control="integrations.notifications_require_verification",
            )
        )
    has_oauth_default = (
        await db.execute(select(func.count(OAuthDefault.id)))
    ).scalar() or 0
    if has_oauth_default and not strict:
        findings.append(
            AuditFinding(
                id="integrations.admin_app_scope_not_applied",
                category="integrations",
                severity="info",
                title="Admin-app deliveries are not limited to the admin app's integrations",
                description=(
                    "A verified delivery to /api/hooks/providers/<provider> or to the LinkedIn "
                    "notification URL reaches every assignment it reached before, including "
                    "assignments whose agent has no integration on the admin's OAuth app that "
                    "its owner may use (or, for a GitHub App, none recording the delivery's "
                    "installation). Each such delivery is logged."
                ),
                recommendation=(
                    "To limit them to the admin app's integrations the agent owner may use, "
                    + where_to_set("integration_notifications_require_verification", "true")
                    + "."
                ),
                control="integrations.notifications_require_verification",
            )
        )
    if strict:
        findings.append(
            AuditFinding(
                id="integrations.notifications_verified",
                category="integrations",
                severity="pass",
                title="Integration notifications must prove their sender",
                description=(
                    f"{option_reference('integration_notifications_require_verification')} is on: "
                    "a Slack event, Microsoft Graph change notification or LinkedIn notification "
                    "the platform cannot verify is refused, and the admin OAuth app's webhook and "
                    "LinkedIn notifications reach only the admin app's integrations the agent "
                    "owner may use."
                ),
                control="integrations.notifications_require_verification",
            )
        )


def _decoded_json(text: str | None) -> object:
    """A JSON column read as stored text, ``None`` when it is not JSON (a lone surrogate is only a character here)."""
    try:
        return json.loads(text or "")
    except ValueError:
        return None


async def _check_provider_webhook_verification(
    db: AsyncSession, findings: list[AuditFinding], *, strict: bool
) -> None:
    """Which providers' ``/api/hooks/providers/<provider>`` deliveries reach assignments unverified.

    The route hands a delivery to what ``_load_provider_routing`` selects - the enabled
    ``integration_event`` assignments on the provider and ``webhook`` ones on that path, of
    enabled agents bound to it - whose owner is active. It verifies the delivery with the
    ``webhook_secret`` of the provider's OAuth default (LinkedIn: its client secret); without
    one it accepts it as before signatures were checked, and under the verification option a
    provider without an OAuth default reaches nothing. Counted per provider in SQL; a
    ``trigger_config`` PostgreSQL cannot parse is read as stored text under the same
    predicates, and an agent whose bindings it cannot parse is left out, as the router skips
    it. The count is an upper bound: an event selection no delivery can match, and
    under the option an assignment on no admin-app integration, are counted too. Deliveries
    are not changed here.
    """
    route = case(
        (Assignment.trigger_type == "webhook", Assignment.trigger_config["path"].as_string()),
        else_=Assignment.trigger_config["provider"].as_string(),
    )
    bound = when_json_parseable(
        Agent.integration_bindings, cast(Agent.integration_bindings, JSONB).has_key(route)
    )
    counted = (
        await db.execute(
            select(route.label("route_provider"), func.count(Assignment.id))
            .join(Agent, Agent.id == Assignment.agent_id)
            .where(
                Assignment.enabled.is_(True),
                Assignment.trigger_type.in_(("integration_event", "webhook")),
                Agent.enabled.is_(True),
                owner_not_deactivated(),
                _sql_can_parse(bound),
            )
            # The label, not the expression: its bound parameters would differ from the select's.
            .group_by(literal_column("route_provider"))
        )
    ).all()
    reached: dict[str, int] = {}
    for provider, count in counted:
        if provider:
            reached[provider] = reached.get(provider, 0) + count
    stored = cast(Assignment.trigger_config, Text)
    stored_bindings = cast(Agent.integration_bindings, Text)
    unparsed = (
        await db.execute(
            select(Assignment.trigger_type, stored.label("config_text"), stored_bindings.label("bindings_text"))
            .join(Agent, Agent.id == Assignment.agent_id)
            .where(
                Assignment.enabled.is_(True),
                Assignment.trigger_type.in_(("integration_event", "webhook")),
                Agent.enabled.is_(True),
                owner_not_deactivated(),
                json_unparseable(Assignment.trigger_config),
                not_(json_unparseable(Agent.integration_bindings)),
            )
        )
    ).all()
    for row in unparsed:
        config = _decoded_json(row.config_text)
        key = "path" if row.trigger_type == "webhook" else "provider"
        provider = config.get(key) if isinstance(config, dict) else None
        if isinstance(provider, str) and provider and jsonb_has_key(_decoded_json(row.bindings_text), provider):
            reached[provider] = reached.get(provider, 0) + 1
    if not reached:
        return
    defaults = {
        row.provider: row
        for row in (
            await db.execute(
                select(
                    OAuthDefault.provider,
                    OAuthDefault.extra_config_enc,
                    OAuthDefault.client_secret_enc,
                ).where(OAuthDefault.provider.in_(sorted(reached)))
            )
        ).all()
    }
    unverified: list[tuple[str, str, int]] = []
    for provider in sorted(reached):
        if strict and provider not in defaults:
            continue  # no admin app: the verification option hands these deliveries to nobody
        reason = provider_webhook_unverified_reason(provider, defaults.get(provider))
        if reason:
            unverified.append((provider, reason, reached[provider]))
    path = PROVIDER_WEBHOOK_PATH.format(provider="<provider>")
    if not unverified:
        findings.append(
            AuditFinding(
                id="integrations.provider_webhook_verified",
                category="integrations",
                severity="pass",
                title="The provider webhook verifies the deliveries it hands out",
                description=(
                    f"A delivery to {path} that can trigger an assignment ({', '.join(sorted(reached))}) "
                    "must be signed with the webhook secret of the provider's OAuth default (LinkedIn: "
                    "its client secret)"
                    + (", and without an OAuth default it reaches no assignment." if strict else ".")
                ),
                control="integrations.notifications_require_verification",
            )
        )
        return
    total = sum(count for _provider, _reason, count in unverified)
    listed = "; ".join(f"{provider}: {count} assignment(s), {reason}" for provider, reason, count in unverified)
    by_reason: dict[str, list[str]] = {}
    for provider, reason, _count in unverified:
        by_reason.setdefault(reason, []).append(provider)
    advice: list[str] = []
    unsigned = by_reason.get(NO_WEBHOOK_SECRET, [])
    signable = [
        provider
        for provider in unsigned
        if "webhook_secret" in (PROVIDER_CATALOG.get(provider, {}).get("extra_fields") or {})
    ]
    unsignable = [provider for provider in unsigned if provider not in signable]
    if signable:
        advice.append(
            f"For {', '.join(signable)}, set a webhook secret on the provider's app and the same "
            "value in Settings > Integrations > OAuth Defaults: deliveries are then verified and "
            "unsigned ones refused."
        )
    if unsignable:
        advice.append(
            f"{', '.join(unsignable)} cannot sign a delivery to this URL, and their events do not "
            "arrive through it unless you pointed a webhook at it: block "
            + ", ".join(PROVIDER_WEBHOOK_PATH.format(provider=provider) for provider in unsignable)
            + " at your reverse proxy."
        )
    if by_reason.get(NO_CLIENT_SECRET):
        advice.append(
            f"For {', '.join(by_reason[NO_CLIENT_SECRET])}, configure the app's client secret in Settings > "
            "Integrations > OAuth Defaults: its signature is then required."
        )
    without_default = by_reason.get(NO_OAUTH_DEFAULT, [])
    findings.append(
        AuditFinding(
            id="integrations.provider_webhook_unverified",
            category="integrations",
            severity="warning",
            title="The provider webhook accepts deliveries it cannot verify",
            description=(
                f"Up to {total} active assignment(s) can be triggered through {path} by a delivery the "
                f"platform cannot verify ({listed}). Such a delivery is accepted as before signatures "
                "were checked, so anyone who can reach that URL can forge an event that triggers them."
                + (
                    " Under the verification option, of these only the ones bound to an integration on "
                    "the admin app their agent's owner may use are reached."
                    if strict
                    else ""
                )
            ),
            recommendation=" ".join(
                advice
                + (
                    [
                        f"No admin app of {', '.join(without_default)} delivers to this URL: to keep "
                        "such deliveries from reaching any assignment, "
                        + where_to_set("integration_notifications_require_verification", "true")
                        + "."
                    ]
                    if without_default
                    else []
                )
            ),
            control="integrations.notifications_require_verification",
        )
    )


#: The shareable types a run with no user reads through ``caller_visible_filter``.
_RESTRICTABLE_MODELS = (
    Agent,
    Runner,
    TemplateContainer,
    KnowledgeBase,
    Integration,
    MCPServer,
    CustomTool,
    Prompt,
    AgentSkill,
    Variable,
)
_MAX_LISTED = 50
_MAX_LISTED_ASSIGNMENTS = _MAX_LISTED


async def _restricted_company_managed_counts(db: AsyncSession) -> dict[str, int]:
    """``access_restricted`` company-managed rows per table, the ones a read
    with no account keeping main's tier reaches although an audience narrows
    them."""
    restricted: dict[str, int] = {}
    for model in _RESTRICTABLE_MODELS:
        count = (
            await db.execute(
                select(func.count(model.id)).where(
                    model.is_company_managed.is_(True),
                    model.access_restricted.is_(True),
                )
            )
        ).scalar() or 0
        if count:
            restricted[model.__tablename__] = int(count)
    return restricted


def _listing(rows) -> str:
    listed = [f"{name} ({rid})" for rid, name in rows[:_MAX_LISTED]]
    if len(rows) > len(listed):
        listed.append(f"and {len(rows) - len(listed)} more")
    return "; ".join(listed)


def _reachable(restricted: dict[str, int]) -> str:
    return ", ".join(f"{count} {table}" for table, count in sorted(restricted.items()))


async def _check_restricted_reach_without_account(
    db: AsyncSession, findings: list[AuditFinding]
) -> None:
    """The work that reads with no account yet keeps main's company-managed
    tier: ownerless company-managed assignments, the global MCP platform key,
    ownerless evaluations. Reported only while a restricted row exists."""
    restricted = await _restricted_company_managed_counts(db)
    if not restricted:
        return
    await _check_ownerless_company_managed_assignments(db, findings, restricted)
    await _check_global_mcp_key_reach(db, findings, restricted)
    await _check_ownerless_evaluations(db, findings, restricted)


async def _check_ownerless_company_managed_assignments(
    db: AsyncSession,
    findings: list[AuditFinding],
    restricted: dict[str, int] | None = None,
) -> None:
    """List the company-managed assignments whose automatic runs have no user.

    Such a run keeps every company-managed row, ``access_restricted`` ones
    included (``helpers.no_account_provenance``), so it reaches resources an
    audience was meant to narrow. Reported only while a restricted
    company-managed row exists; informational, never blocking.
    """
    if restricted is None:
        restricted = await _restricted_company_managed_counts(db)
    if not restricted:
        return

    rows = (
        await db.execute(
            select(Assignment.id, Assignment.name)
            .where(
                Assignment.enabled.is_(True),
                Assignment.is_company_managed.is_(True),
                Assignment.user_id.is_(None),
                Assignment.run_as_user_id.is_(None),
                Assignment.trigger_type != "manual",
            )
            .order_by(Assignment.name, Assignment.id)
        )
    ).all()
    if not rows:
        return

    findings.append(
        AuditFinding(
            id="access.ownerless_assignments_restricted_reach",
            category="access",
            severity="info",
            title=(
                f"{len(rows)} company-managed assignment(s) run with no user "
                "and can reach restricted resources"
            ),
            description=(
                "Their scheduled, webhook and integration-event runs have no "
                '"run as" user, so they read every company-managed resource, '
                f"including the ones restricted to an audience ({_reachable(restricted)}). "
                f"Assignments: {_listing(rows)}."
            ),
            recommendation=(
                'Set a "run as" user who is in each restricted resource\'s '
                "audience on these assignments, so their runs read the platform "
                "as that user. Nothing changes for them until you do."
            ),
        )
    )


async def _check_global_mcp_key_reach(
    db: AsyncSession,
    findings: list[AuditFinding],
    restricted: dict[str, int] | None = None,
) -> None:
    """List the agents the global MCP endpoint's platform key runs.

    A call authenticated with the key has no account and keeps every
    company-managed row, ``access_restricted`` ones included. Reported only
    while a key is configured, agents are exposed and a restricted
    company-managed row exists; informational, never blocking.
    """
    from app.api.v1.mcp_endpoint import _get_global_agents
    from app.api.v1.platform_settings_helpers import get_platform_settings

    ps = await get_platform_settings(db)
    if not ps.mcp_global_api_key_hash:
        return
    if restricted is None:
        restricted = await _restricted_company_managed_counts(db)
    if not restricted:
        return
    agents = await _get_global_agents(db, ps)
    if not agents:
        return

    findings.append(
        AuditFinding(
            id="access.global_mcp_key_restricted_reach",
            category="access",
            severity="info",
            title=(
                f"The global MCP endpoint's platform key runs {len(agents)} agent(s) "
                "that can reach restricted resources"
            ),
            description=(
                "A call authenticated with the platform key has no account, so the "
                "exposed agents read every company-managed resource, including the "
                f"ones restricted to an audience ({_reachable(restricted)}). "
                f"Exposed agents: {_listing([(a.id, a.name) for a in agents])}."
            ),
            recommendation=(
                "Expose on the global endpoint only agents whose reach is intended "
                "for every holder of the platform key; the others stay reachable "
                "per user through /mcp/user. Nothing changes for them until you do."
            ),
        )
    )


async def _check_ownerless_evaluations(
    db: AsyncSession,
    findings: list[AuditFinding],
    restricted: dict[str, int] | None = None,
) -> None:
    """List the full-loop evaluations that have no owning user.

    Their agent runs have no account and keep every company-managed row,
    ``access_restricted`` ones included. Reported only while a restricted
    company-managed row exists; informational, never blocking.
    """
    if restricted is None:
        restricted = await _restricted_company_managed_counts(db)
    if not restricted:
        return
    rows = (
        await db.execute(
            select(Evaluation.id, Evaluation.name)
            .where(
                Evaluation.user_id.is_(None),
                Evaluation.execution_mode == "full_loop",
            )
            .order_by(Evaluation.name, Evaluation.id)
        )
    ).all()
    if not rows:
        return

    findings.append(
        AuditFinding(
            id="access.ownerless_evaluations_restricted_reach",
            category="access",
            severity="info",
            title=(
                f"{len(rows)} evaluation(s) with no owning user can reach "
                "restricted resources"
            ),
            description=(
                "Their full-loop agent runs have no account, so they read every "
                "company-managed resource, including the ones restricted to an "
                f"audience ({_reachable(restricted)}). Evaluations: {_listing(rows)}."
            ),
            recommendation=(
                "Give these evaluations an owning user who is in each restricted "
                "resource's audience, so their runs read the platform as that "
                "user. Nothing changes for them until you do."
            ),
        )
    )


async def _check_custom_tool_sandbox(findings: list[AuditFinding]) -> None:
    """Check custom tool sandbox isolation (SEC-4)."""
    if security_option("disable_custom_tools"):
        findings.append(
            AuditFinding(
                id="tools.custom_tools_disabled",
                category="tools",
                severity="pass",
                title="Custom tools disabled",
                description="Custom tool execution is disabled platform-wide; no sandbox exposure.",
                control="tools.custom_tools_disabled",
            )
        )
        return

    try:
        from app.services.custom_tool_executor import get_effective_sandbox

        effective = await get_effective_sandbox()
    except Exception:
        findings.append(
            AuditFinding(
                id="tools.custom_tool_sandbox_check_skipped",
                category="tools",
                severity="info",
                title="Custom tool sandbox check skipped",
                description="Could not determine the effective custom tool sandbox.",
                control="tools.custom_tool_sandbox",
            )
        )
        return

    if effective == "docker":
        findings.append(
            AuditFinding(
                id="tools.custom_tool_sandbox_docker",
                category="tools",
                severity="pass",
                title="Custom tools run in Docker sandbox",
                description="Custom tools execute in a hardened Docker container (no network, read-only filesystem, dropped capabilities, memory cap).",
                control="tools.custom_tool_sandbox",
            )
        )
    elif effective == "opensandbox":
        findings.append(
            AuditFinding(
                id="tools.custom_tool_sandbox_opensandbox",
                category="tools",
                severity="pass",
                title="Custom tools run in OpenSandbox container",
                description="Custom tools execute in an OpenSandbox-managed container with OS-level isolation provided by the OpenSandbox server.",
                control="tools.custom_tool_sandbox",
            )
        )
    elif effective == "docker_unavailable":
        findings.append(
            AuditFinding(
                id="tools.custom_tool_sandbox_docker_unavailable",
                category="tools",
                severity="warning",
                title="Custom tool sandbox requires Docker but Docker is unavailable",
                description="CUSTOM_TOOL_SANDBOX is set to 'docker' but Docker is not reachable. Custom tool executions will fail until Docker is available.",
                recommendation="Install/start Docker on the host, or set CUSTOM_TOOL_SANDBOX=auto to allow subprocess fallback.",
                control="tools.custom_tool_sandbox",
            )
        )
    elif effective == "opensandbox_unavailable":
        findings.append(
            AuditFinding(
                id="tools.custom_tool_sandbox_opensandbox_unavailable",
                category="tools",
                severity="warning",
                title="Custom tool sandbox requires OpenSandbox but server is unreachable",
                description="CUSTOM_TOOL_SANDBOX is set to 'opensandbox' but the OpenSandbox server is not reachable at the configured OPENSANDBOX_DOMAIN. Custom tool executions will fail.",
                recommendation="Check OPENSANDBOX_DOMAIN, OPENSANDBOX_PROTOCOL, and OPENSANDBOX_API_KEY, or set CUSTOM_TOOL_SANDBOX=auto to fall back to Docker/subprocess.",
                control="tools.custom_tool_sandbox",
            )
        )
    else:
        from app.services.custom_tool_executor import (
            describe_js_subprocess_privileges,
            node_permission_active,
        )

        try:
            node_hardened = await node_permission_active()
        except Exception:
            node_hardened = False
        js_clause = describe_js_subprocess_privileges(node_hardened)
        if not node_hardened:
            js_clause += (
                " and could read host files or access the internal network"
            )
        findings.append(
            AuditFinding(
                id="tools.custom_tool_sandbox_none",
                category="tools",
                severity="warning",
                title="Custom tools run without container isolation",
                description=f"Custom tools are enabled and execute in a plain subprocess without the hardened Docker container sandbox. {js_clause}; Python tools run in isolated mode (-I) with a best-effort module blocklist that is defence-in-depth, not a container boundary.",
                recommendation=(
                    "Install Docker on the host so the hardened container sandbox is used "
                    "(CUSTOM_TOOL_SANDBOX=auto picks it up automatically), or "
                    + where_to_set("disable_custom_tools", "true")
                    + "."
                ),
                control="tools.custom_tool_sandbox",
            )
        )


def _reenabled_modules_text(setting: str, modules: frozenset[str], where: str) -> tuple[list[str], str]:
    """The modules *setting* re-enables, and the sentence naming them (and those that stay blocked)."""
    reenabled = sorted(modules - ALWAYS_BLOCKED_MODULES)
    ignored = sorted(modules & ALWAYS_BLOCKED_MODULES)
    text = (
        f"{setting} re-enables {_listed(reenabled)} in {where}, whose module blocklist is what keeps "
        "the code it runs off the network (socket, http, urllib, requests reach whatever the host reaches)."
        if reenabled
        else f"{setting} names only modules that can never be re-enabled."
    )
    if ignored:
        text += f" {_listed(ignored)} stay blocked whatever it says."
    return reenabled, text


def _subprocess_sentence(mode: str) -> str:
    """When custom tools run in the plain subprocess, for a sandbox *mode* that uses it."""
    if mode == "subprocess":
        return " Custom tools are enabled and CUSTOM_TOOL_SANDBOX=subprocess runs every one of them there."
    return f" Custom tools are enabled and CUSTOM_TOOL_SANDBOX={mode} runs them there whenever Docker is out of reach."


async def _node_permission_finding(sandbox_mode: str) -> AuditFinding:
    """``CUSTOM_TOOL_NODE_PERMISSION`` on while custom tools may run in the plain subprocess.

    On only protects where the local node binary runs under the permission
    model (Node.js 24 or later): the probe the executor itself runs, once per
    process, decides whether JavaScript tools get it, and this reads that.
    """
    from app.services.custom_tool_executor import node_permission_active

    if await node_permission_active():
        return AuditFinding(
            id="tools.node_permission_on",
            category="tools",
            severity="pass",
            title="JavaScript custom tools run under the Node.js permission model",
            description=(
                f"{option_reference('custom_tool_node_permission')} is on and the local node binary supports it: "
                "a JavaScript custom tool run in the plain subprocess gets no file system, child process, worker "
                "thread or native addon access." + _subprocess_sentence(sandbox_mode)
            ),
            control="tools.custom_tool_node_permission",
        )
    return AuditFinding(
        id="tools.node_permission_unsupported",
        category="tools",
        severity="warning",
        title="JavaScript custom tools run without the Node.js permission model the platform asks for",
        description=(
            f"{option_reference('custom_tool_node_permission')} is on, but the local node binary did not start "
            "under the permission model when the platform probed it (Node.js 24 or later is needed; node missing "
            "or the probe timing out reads the same): a JavaScript custom tool run in the plain subprocess keeps "
            "file system, child process, worker thread and native addon access."
            + _subprocess_sentence(sandbox_mode)
        ),
        recommendation=(
            "Install Node.js 24 or later on the platform host and restart (the probe runs once per process), or "
            "set CUSTOM_TOOL_SANDBOX=docker so custom tools never run outside a container."
        ),
        control="tools.custom_tool_node_permission",
    )


async def _check_sandbox_features(findings: list[AuditFinding]) -> None:
    """The code interpreter, custom variables, and what re-weakens the Python and JavaScript sandboxes."""
    interpreter_on = not security_option("disable_code_interpreter")
    custom_tools_on = not security_option("disable_custom_tools")
    # Only these two modes ever run a custom tool in the plain subprocess.
    sandbox_mode = configured_custom_tool_sandbox()
    subprocess_reachable = custom_tools_on and sandbox_mode in ("auto", "subprocess")

    if interpreter_on:
        findings.append(
            AuditFinding(
                id="tools.code_interpreter_enabled",
                category="tools",
                severity="info",
                title="The code interpreter is enabled",
                description=(
                    "Agents given the code interpreter run Python on the platform host, in a fresh "
                    "subprocess with a module blocklist and a scrubbed environment: not a container, "
                    "and it shares the host's network."
                ),
                recommendation=(
                    where_to_set("disable_code_interpreter", "true", sentence_start=True)
                    + " if no agent needs it."
                ),
                control="tools.code_interpreter_disabled",
            )
        )
    if not security_option("disable_custom_variables"):
        findings.append(
            AuditFinding(
                id="tools.custom_variables_enabled",
                category="tools",
                severity="info",
                title="Custom variables are enabled",
                description=(
                    "Users can store encrypted variables and have assignment prompts resolve them at run "
                    "time, which sends the value to the model provider with the prompt."
                ),
                recommendation=(
                    where_to_set("disable_custom_variables", "true", sentence_start=True)
                    + " if no assignment uses them."
                ),
                control="tools.custom_variables_disabled",
            )
        )

    interpreter_modules = security_option("code_interpreter_extra_allowed_modules")
    if interpreter_modules:
        reenabled, text = _reenabled_modules_text(
            option_reference("code_interpreter_extra_allowed_modules"), interpreter_modules, "the code interpreter"
        )
        findings.append(
            AuditFinding(
                id="tools.code_interpreter_modules_reenabled",
                category="tools",
                severity="warning" if interpreter_on and reenabled else "info",
                title=f"{len(reenabled)} blocked module(s) re-enabled in the code interpreter",
                description=text + ("" if interpreter_on else " The code interpreter is disabled today."),
                recommendation=(
                    where_to_set("code_interpreter_extra_allowed_modules", "", sentence_start=True)
                    + ", unless the backend runs network-isolated."
                ),
                control="tools.code_interpreter_extra_modules",
            )
        )
    elif interpreter_on:
        findings.append(
            AuditFinding(
                id="tools.code_interpreter_modules_blocked",
                category="tools",
                severity="pass",
                title="The code interpreter keeps its whole module blocklist",
                description=(
                    f"{option_reference('code_interpreter_extra_allowed_modules')} is empty: code the "
                    "interpreter runs cannot import the network modules it blocks."
                ),
                control="tools.code_interpreter_extra_modules",
            )
        )
    tool_modules = security_option("custom_tool_extra_allowed_modules")
    if tool_modules:
        reenabled, text = _reenabled_modules_text(
            option_reference("custom_tool_extra_allowed_modules"), tool_modules, "the Python custom tool sandbox"
        )
        findings.append(
            AuditFinding(
                id="tools.custom_tool_modules_reenabled",
                category="tools",
                severity="warning" if subprocess_reachable and reenabled else "info",
                title=f"{len(reenabled)} blocked module(s) re-enabled for Python custom tools",
                description=text
                + (
                    " The plain subprocess shares the host's network." + _subprocess_sentence(sandbox_mode)
                    if subprocess_reachable
                    else " No custom tool runs in the plain subprocess today: custom tools are disabled, "
                    "or CUSTOM_TOOL_SANDBOX isolates every run in a container."
                ),
                recommendation=(
                    where_to_set("custom_tool_extra_allowed_modules", "", sentence_start=True)
                    + ", or set CUSTOM_TOOL_SANDBOX=docker so custom tools never run outside a container."
                ),
                control="tools.custom_tool_extra_modules",
            )
        )
    elif subprocess_reachable:
        findings.append(
            AuditFinding(
                id="tools.custom_tool_modules_blocked",
                category="tools",
                severity="pass",
                title="Python custom tools keep their whole module blocklist",
                description=(
                    f"{option_reference('custom_tool_extra_allowed_modules')} is empty: a Python custom tool "
                    "run in the plain subprocess cannot import the network modules it blocks."
                    + _subprocess_sentence(sandbox_mode)
                ),
                control="tools.custom_tool_extra_modules",
            )
        )

    if security_option("custom_tool_node_permission"):
        if subprocess_reachable:
            findings.append(await _node_permission_finding(sandbox_mode))
    else:
        findings.append(
            AuditFinding(
                id="tools.node_permission_off",
                category="tools",
                severity="warning" if subprocess_reachable else "info",
                title="JavaScript custom tools run without the Node.js permission model",
                description=(
                    f"{option_reference('custom_tool_node_permission')} is off: a JavaScript custom tool run "
                    "in the plain subprocess keeps file system, child process, worker thread and native "
                    "addon access."
                    + (
                        _subprocess_sentence(sandbox_mode)
                        if subprocess_reachable
                        else " Nothing runs there today: custom tools are disabled, or CUSTOM_TOOL_SANDBOX "
                        "isolates every run in a container."
                    )
                ),
                recommendation=(
                    where_to_set("custom_tool_node_permission", "true", sentence_start=True) + ", the default."
                ),
                control="tools.custom_tool_node_permission",
            )
        )


async def _check_tool_call_budget(findings: list[AuditFinding]) -> None:
    """Flag a platform tool-call budget one parallel batch can overshoot."""
    limit = settings.agent_loop_max_tool_calls
    if limit <= 0:
        return
    if settings.agent_loop_max_tool_calls_strict:
        findings.append(
            AuditFinding(
                id="tools.tool_call_budget_enforced",
                category="tools",
                severity="pass",
                title="Tool-call budget cut strictly",
                description=(
                    f"AGENT_LOOP_MAX_TOOL_CALLS is {limit:,} with AGENT_LOOP_MAX_TOOL_CALLS_STRICT: a "
                    'parallel batch that crosses it runs only the calls that still fit, the others are '
                    'answered "Not executed".'
                ),
                control="tools.tool_call_budget_strict",
            )
        )
        return
    findings.append(
        AuditFinding(
            id="tools.tool_call_budget_overshoot",
            category="tools",
            severity="info",
            title="Tool-call budget can be overshot by one parallel batch",
            description=(
                f"AGENT_LOOP_MAX_TOOL_CALLS is {limit:,} without AGENT_LOOP_MAX_TOOL_CALLS_STRICT: "
                "a model response that asks for more tool calls than a run has left still runs "
                "every one of them (the overshoot is logged and recorded on the run's trace), and "
                "only the next model call is refused."
            ),
            recommendation=(
                "Set AGENT_LOOP_MAX_TOOL_CALLS_STRICT=true to run only the calls that still fit "
                'and answer the others "Not executed".'
            ),
            control="tools.tool_call_budget_strict",
        )
    )


def _csp_violations_evidence(recent: RecentViolations, *, enforced: bool) -> str:
    """What browsers reported against the policy over the period ``recent`` covers."""
    period = f"the last {recent.days} days"
    if not recent.known:
        unread = f"The violations of {period} could not be read (Redis did not answer)"
        return f"{unread}." if enforced else f"{unread}, so what enforcing the policy would block is unknown."
    if recent.total:
        by_directive = ", ".join(f"{directive} {count}" for directive, count in recent.by_directive.items())
        reported = f"{recent.total} violation{'' if recent.total == 1 else 's'}: {by_directive}"
    else:
        reported = "no violation"
    if recent.extensions:
        reported += (
            f" ({'and' if recent.total else 'only'} {recent.extensions} caused by browser extensions, "
            "which are not the platform's)"
        )
    sentence = f"In {period}, across every replica, browsers reported {reported}."
    if not recent.total:
        if enforced:
            return sentence
        return f"{sentence} Enforcing the policy would have blocked nothing the platform's pages loaded in that period."
    if enforced:
        return (
            f"{sentence} Each is a load the browser blocked, which users may have hit, unless it predates the "
            "switch to enforce or came from a page that stays report-only."
        )
    return f"{sentence} Enforcing the policy would block each of them."


def _csp_framing_once_enforced() -> str:
    """Who may frame the platform's pages under the enforced policy, in the
    order ``csp.frame_ancestor_sources`` builds the directive: the platform
    itself, ``CONTENT_SECURITY_POLICY_FRAME_ANCESTORS``, then the registered
    platforms when :func:`registered_platforms_may_frame`. Ends with a period."""
    named = [source for source in settings.content_security_policy_frame_ancestors if source != "'self'"]
    framers = ["the platform itself"]
    if named:
        framers.append(f"{', '.join(named)} (CONTENT_SECURITY_POLICY_FRAME_ANCESTORS)")
    excluded = ""
    if registered_platforms_may_frame():
        framers.append("the platforms registered with it (OpenCTI, OpenAEV, OpenCRQ)")
    else:
        reason = (
            "is the published default, with which anyone can register"
            if registration_token_state() == "published_default"
            else "is unset"
        )
        excluded = f" The platforms registered with it may not frame it while PLATFORM_REGISTRATION_TOKEN {reason}."
    if len(framers) == 1:
        who = "the web application and the MCP OAuth consent page may be framed only by the platform itself"
    else:
        who = (
            f"the web application may be framed only by {', '.join(framers[:-1])} and {framers[-1]}; "
            "the MCP OAuth consent page only by the platform itself"
        )
    return f"{who}.{excluded}"


async def _check_content_security_policy(findings: list[AuditFinding]) -> None:
    """The content security policy: its mode, what browsers reported against it, who may frame the platform."""
    mode = settings.content_security_policy_mode
    if mode == "off":
        findings.append(
            AuditFinding(
                id="web.csp_off",
                control="web.content_security_policy",
                category="web",
                severity="warning",
                title="Content security policy is off",
                description=(
                    "CONTENT_SECURITY_POLICY_MODE=off: the platform sends no Content-Security-Policy, so a "
                    "script injected past its output checks would run. Framing is not restricted either: with "
                    "no frame-ancestors directive, and no X-Frame-Options (which the platform never sends), any "
                    "site may show its pages in an iframe."
                ),
                recommendation=(
                    "Set CONTENT_SECURITY_POLICY_MODE=report-only and restart: browsers then block nothing and "
                    "report what the policy would block. Read this finding for a week, then set the mode to "
                    "enforce."
                ),
            )
        )
        return
    # The middleware's reading (csp.policy_header): any mode but off and report-only enforces.
    enforced = mode != "report-only"
    recent = await recent_violations()
    if not enforced:
        findings.append(
            AuditFinding(
                id="web.csp_report_only",
                control="web.content_security_policy",
                category="web",
                severity="info",
                title="Content security policy is report-only",
                description=(
                    "The platform's pages carry Content-Security-Policy-Report-Only: browsers block nothing "
                    "and report what the policy would block. " + _csp_violations_evidence(recent, enforced=False)
                ),
                recommendation=(
                    "Set CONTENT_SECURITY_POLICY_MODE=enforce and restart once the only violations left are "
                    "understood: each 'CSP violation first seen' log line names the directive, the blocked "
                    "resource and the page."
                ),
            )
        )
    else:
        findings.append(
            AuditFinding(
                id="web.csp_enforced",
                control="web.content_security_policy",
                category="web",
                severity="pass",
                title="Content security policy is enforced",
                description=(
                    "The platform's pages carry Content-Security-Policy: browsers refuse the scripts and "
                    "resources it does not allow (the pages that run third-party code, such as the API "
                    "reference, stay report-only). " + _csp_violations_evidence(recent, enforced=True)
                ),
                recommendation=(
                    "Read the 'CSP violation first seen' log lines: each names the blocked resource and the "
                    "page. If the platform itself needs one, report it rather than working around the policy."
                    if recent.total
                    else None
                ),
            )
        )
    # Framing is restricted only by an enforced policy: a report-only one
    # blocks no frame, and the platform never sends X-Frame-Options.
    permissive = permissive_frame_ancestor_sources(settings.content_security_policy_frame_ancestors)
    if permissive:
        if enforced:
            consequence, meanwhile = "any site these match may show the platform's pages in an iframe", ""
        else:
            consequence = (
                "once the policy is enforced, any site these match may still show the platform's pages in an iframe"
            )
            meanwhile = " Until then the policy is report-only and refuses no site at all."
        findings.append(
            AuditFinding(
                id="web.csp_frame_ancestors_any",
                control="web.csp_frame_ancestors",
                category="web",
                severity="warning",
                title="Frame ancestors admit every site",
                description=(
                    f"CONTENT_SECURITY_POLICY_FRAME_ANCESTORS allows {', '.join(permissive)}: {consequence}, "
                    f"which is what frame-ancestors is there to prevent (clickjacking).{meanwhile}"
                ),
                recommendation=(
                    "List the sites that embed the platform in CONTENT_SECURITY_POLICY_FRAME_ANCESTORS instead, "
                    "such as https://intranet.example.com or https://*.example.com, and restart."
                ),
            )
        )
    elif not enforced:
        findings.append(
            AuditFinding(
                id="web.csp_frame_ancestors_not_enforced",
                control="web.csp_frame_ancestors",
                category="web",
                severity="info",
                title="Any site can frame the platform until the policy is enforced",
                description=(
                    "The policy is report-only and the platform sends no X-Frame-Options, so any site can show "
                    "the platform's pages in an iframe: browsers block no framing until the policy is enforced. "
                    f"Once it is, {_csp_framing_once_enforced()} Until then, browsers report each page another "
                    "site frames as a frame-ancestors violation, counted in the per-directive totals of the "
                    '"Content security policy is report-only" finding.'
                ),
                recommendation=(
                    "Before enforcing, list any other site that embeds the platform, such as an intranet portal, "
                    "in CONTENT_SECURITY_POLICY_FRAME_ANCESTORS and restart: once the policy is enforced, browsers "
                    "refuse every site it does not allow."
                ),
            )
        )
    else:
        findings.append(
            AuditFinding(
                id="web.csp_frame_ancestors_restricted",
                control="web.csp_frame_ancestors",
                category="web",
                severity="pass",
                title="Framing is restricted",
                description=(
                    "The policy is enforced and browsers refuse to show the platform in the iframe of a site it "
                    f"does not allow: {_csp_framing_once_enforced()} The API reference, the Teams tab and the "
                    "Office add-in stay report-only in every mode and restrict no framing."
                ),
            )
        )


async def _check_mcp(db: AsyncSession, findings: list[AuditFinding], platform: object | None) -> None:
    """The MCP authorization server, the XTM One MCP server, and the A2A development exceptions."""
    if security_option("mcp_dcr_enabled"):
        registered = (
            await db.execute(
                select(func.count(OAuthClient.id)).where(
                    OAuthClient.a2a_exposure_id.is_(None), OAuthClient.a2a_integration_id.is_(None)
                )
            )
        ).scalar() or 0
        findings.append(
            AuditFinding(
                id="mcp.dynamic_client_registration_on",
                category="mcp",
                severity="info",
                title=f"MCP clients can register themselves ({registered} registered)",
                description=(
                    "POST /mcp/oauth/register hands an OAuth client to any caller, without "
                    "authentication, as the MCP specification requires; it is throttled and capped at "
                    f"MCP_DCR_MAX_CLIENTS ({settings.mcp_dcr_max_clients}), and a registered client still "
                    "needs a person to sign in and consent."
                ),
                recommendation=(
                    "If no MCP client signs in with OAuth here, "
                    + where_to_set("mcp_dcr_enabled", "false")
                    + ": MCP clients then connect with an API key."
                ),
                control="mcp.dynamic_client_registration",
            )
        )
    if security_option("mcp_cimd_enabled"):
        findings.append(
            AuditFinding(
                id="mcp.client_id_metadata_documents_on",
                category="mcp",
                severity="info",
                title="MCP clients can sign in with a client metadata document",
                description=(
                    "A client may name an HTTPS metadata document as its client_id instead of "
                    "registering (MCP 2025-11-25): the platform fetches it from a public address, "
                    "bounded in size and time and cached, and accepts only public clients whose "
                    "redirect URIs are HTTPS or loopback. A person still signs in and consents."
                ),
                recommendation=(
                    "If no MCP client signs in with OAuth here, "
                    + where_to_set("mcp_cimd_enabled", "false")
                    + ": MCP clients then register or connect with an API key."
                ),
                control="mcp.client_id_metadata_documents",
            )
        )
    if platform is not UNREADABLE_ROW and stored_value(platform, "platform_settings", "xtm_mcp_enabled"):
        findings.append(
            AuditFinding(
                id="mcp.xtm_mcp_server_on",
                category="mcp",
                severity="info",
                title="The XTM One MCP server is on",
                description=(
                    "External MCP clients reach this instance's own tools at /mcp/xtm (traces, the agent "
                    "builder, knowledge search, skills), each caller within what their own account can "
                    "see and change."
                ),
                recommendation="Turn it off in Settings > Integrations > MCP Endpoint if no MCP client needs it.",
                control="mcp.xtm_mcp_server",
            )
        )
    origins = list(security_option("a2a_client_dev_trusted_origins"))
    if not origins:
        partners = (await db.execute(select(func.count(A2AClientTarget.id)))).scalar() or 0
        if partners:
            findings.append(
                AuditFinding(
                    id="mcp.a2a_partners_over_https",
                    category="mcp",
                    severity="pass",
                    title=f"The A2A client reaches its {partners} partner agent(s) over HTTPS only",
                    description=(
                        f"{option_reference('a2a_client_dev_trusted_origins')} is empty: every delegation to a "
                        "partner goes over HTTPS, to an address the A2A destination guard accepts."
                    ),
                    control="mcp.a2a_dev_trusted_origins",
                )
            )
    else:
        findings.append(
            AuditFinding(
                id="mcp.a2a_dev_origins_set",
                category="mcp",
                severity="info" if settings.debug else "warning",
                title=f"The A2A client may reach {len(origins)} partner origin(s) without HTTPS",
                description=(
                    f"{option_reference('a2a_client_dev_trusted_origins')} lists {_listed(origins)}: the A2A "
                    "client reaches these over plain HTTP, and at addresses its destination guard refuses "
                    "otherwise. The list exists to test two deployments on one machine."
                ),
                recommendation=where_to_set("a2a_client_dev_trusted_origins", "", sentence_start=True) + ".",
                control="mcp.a2a_dev_trusted_origins",
            )
        )
    if security_option("a2a_allow_insecure_loopback"):
        findings.append(
            AuditFinding(
                id="mcp.a2a_loopback_counts_as_https",
                category="mcp",
                severity="warning",
                title="Plain HTTP from loopback counts as encrypted on A2A",
                description=(
                    f"{option_reference('a2a_allow_insecure_loopback')} is on: an A2A request over plain "
                    "HTTP from 127.0.0.1 or ::1 passes the HTTPS requirement. Behind a reverse proxy on "
                    "the same host every partner request arrives from loopback, so none of them is held "
                    "to HTTPS."
                ),
                recommendation=(
                    where_to_set("a2a_allow_insecure_loopback", "false", sentence_start=True)
                    + ": it exists for the end-to-end test stack."
                ),
                control="mcp.a2a_insecure_loopback",
            )
        )
    peers = list(settings.test_only_insecure_transport_peers or ())
    if peers:
        findings.append(
            AuditFinding(
                id="mcp.test_only_transport_peers_set",
                category="mcp",
                severity="warning",
                title=f"{len(peers)} network(s) exempted from HTTPS on A2A",
                description=(
                    f"TEST_ONLY_INSECURE_TRANSPORT_PEERS lists {_listed(peers)}: a plain HTTP A2A request "
                    "from these networks counts as encrypted. The setting exists for the end-to-end test "
                    "suite only."
                ),
                recommendation="Unset TEST_ONLY_INSECURE_TRANSPORT_PEERS and restart.",
                control="mcp.test_only_insecure_transport_peers",
            )
        )
    # DEBUG counts loopback as HTTPS too (``infrastructure.debug_mode``).
    if peers or security_option("a2a_allow_insecure_loopback") or settings.debug:
        return
    exposed = (
        await db.execute(select(func.count(A2AExposure.id)).where(A2AExposure.revoked_at.is_(None)))
    ).scalar() or 0
    if exposed:
        findings.append(
            AuditFinding(
                id="mcp.a2a_requests_over_https",
                category="mcp",
                severity="pass",
                title=f"A2A requests to the {exposed} exposed agent(s) must arrive over HTTPS",
                description=(
                    f"{option_reference('a2a_allow_insecure_loopback')} is off and TEST_ONLY_INSECURE_TRANSPORT_PEERS "
                    "is empty: the A2A endpoints refuse a request they cannot confirm arrived over HTTPS, "
                    "loopback included."
                ),
                control="mcp.a2a_insecure_loopback",
            )
        )


async def _check_llm_providers(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """Check LLM provider configuration."""
    try:
        from app.models.general_assistant_config import GeneralAssistantConfig

        result = await db.execute(select(GeneralAssistantConfig).limit(1))
        config = result.scalar_one_or_none()

        if not config or not config.llm_providers:
            findings.append(
                AuditFinding(
                    id="llm.no_provider",
                    category="llm",
                    severity="warning",
                    title="No LLM provider configured",
                    description="The platform has no LLM provider set up. Agents cannot function.",
                    recommendation="Configure at least one LLM provider in Settings.",
                )
            )
        else:
            provider_count = len(config.llm_providers)
            findings.append(
                AuditFinding(
                    id="llm.providers_configured",
                    category="llm",
                    severity="pass",
                    title=f"{provider_count} LLM provider(s) configured",
                    description="At least one LLM provider is available for agent execution.",
                )
            )

            # Check for Langfuse observability
            if config.langfuse_enabled:
                findings.append(
                    AuditFinding(
                        id="llm.observability_enabled",
                        category="llm",
                        severity="pass",
                        title="LLM observability enabled (Langfuse)",
                        description="Langfuse tracing is active for LLM calls.",
                    )
                )
            else:
                findings.append(
                    AuditFinding(
                        id="llm.observability_not_configured",
                        category="llm",
                        severity="info",
                        title="LLM observability not configured",
                        description="Langfuse tracing is not enabled. LLM calls are not being traced.",
                        recommendation="Enable Langfuse in Settings for cost tracking and debugging.",
                    )
                )
    except Exception:
        findings.append(
            AuditFinding(
                id="llm.check_skipped",
                category="llm",
                severity="info",
                title="LLM configuration check skipped",
                description="Could not verify LLM provider configuration.",
            )
        )


async def _check_trace_payloads(findings: list[AuditFinding], platform: object | None) -> None:
    """What native LLM traces keep, and for how long (control ``llm.trace_payloads``).

    ``trace_storage_enabled`` and ``trace_store_payloads`` (Settings >
    Usage and quotas > Observability) are read by the tracing pipeline from a per-process
    snapshot (``services/trace_switches.py``); the finding says what the
    stored switches make it keep. Storing payloads is the default and audit
    material, so it is context (``info``), not a weakness.
    """
    if platform is UNREADABLE_ROW:
        return
    retention = int(getattr(platform, "trace_retention_days", None) or 90)
    earlier = (
        f" Traces stored before keep what they hold until the {retention}-day retention deletes them, and "
        "the Langfuse export, while it is on, still receives each call's payloads."
    )
    if not stored_value(platform, "platform_settings", "trace_storage_enabled"):
        findings.append(
            AuditFinding(
                id="llm.trace_storage_off",
                category="llm",
                severity="pass",
                title="No LLM trace is stored",
                description="Trace storage is off in Settings > Usage and quotas > Observability: no new trace is written." + earlier,
                control="llm.trace_payloads",
            )
        )
        return
    if not stored_value(platform, "platform_settings", "trace_store_payloads"):
        findings.append(
            AuditFinding(
                id="llm.trace_payloads_not_stored",
                category="llm",
                severity="pass",
                title="Traces keep no prompts or answers",
                description=(
                    "Store full payloads is off in Settings > Usage and quotas > Observability: new traces keep each call's "
                    "timings, token counts, cost, model, tool names and status, not its prompts, answers, "
                    f"tool arguments or tool results, for {retention} day(s)." + earlier
                ),
                control="llm.trace_payloads",
            )
        )
        return
    findings.append(
        AuditFinding(
            id="llm.trace_payloads_stored",
            category="llm",
            severity="info",
            title="Traces keep the full input and output of every LLM call",
            description=(
                "Native LLM traces store each call's prompts, tool arguments, tool results and answers, "
                f"which can hold personal or confidential data, for {retention} day(s)."
            ),
            recommendation=(
                "Keep the trace retention in Settings > Usage and quotas > Observability as short as debugging needs, or turn "
                "off Store full payloads there to keep only timings, token counts, cost, model, tool names "
                "and status."
            ),
            control="llm.trace_payloads",
        )
    )


def _quota_fails_closed_description(quota_mode: str) -> str:
    """Why the quotas fail closed: the option says so, or - in XTM One - the licence does."""
    if not is_pinned("quota_fail_mode"):
        return (
            f"{quota_mode} is closed: while the quota store cannot be reached, agentic "
            "executions are refused rather than run unmetered."
        )
    option = SECURITY_OPTIONS["quota_fail_mode"]
    text = (
        "Quotas are part of the Filigran licence: while the quota store cannot be reached, agentic "
        f'executions are always refused rather than run unmetered, and "{option.label}" cannot be changed.'
    )
    ignored = ignored_environment_value("quota_fail_mode")
    if ignored is not None:
        text += f" {option.env_var}={ignored} is set and has no effect: remove it."
    return text


def _fail_mode_findings(redis_reachable: bool) -> list[AuditFinding]:
    """What the rate limits and the quotas do while Redis is out of reach, and whether it is now."""
    findings: list[AuditFinding] = []
    rate_limit_mode = option_reference("rate_limit_fail_mode")
    if security_option("rate_limit_fail_mode") == "closed":
        findings.append(
            AuditFinding(
                id="infrastructure.rate_limit_fails_closed",
                category="infrastructure",
                severity="pass",
                title="Rate limits fail closed",
                description=(
                    f"{rate_limit_mode} is closed: while Redis cannot be reached, rate-limited requests "
                    "are refused (503) rather than let through."
                ),
                control="infrastructure.rate_limit_fail_mode",
            )
        )
    else:
        findings.append(
            AuditFinding(
                id="infrastructure.rate_limit_fails_open",
                category="infrastructure",
                severity="info" if redis_reachable else "critical",
                title=(
                    "Rate limits are skipped while Redis is unreachable"
                    if redis_reachable
                    else "Rate limits are off: Redis is unreachable"
                ),
                description=(
                    f"{rate_limit_mode} is open (the default): while Redis cannot be reached, every "
                    "rate-limited request (sign-in, password reset, webhooks, chat messages) passes "
                    "unthrottled."
                    if redis_reachable
                    else f"Redis cannot be reached and {rate_limit_mode} is open: the sign-in, password "
                    "reset, webhook and chat message rate limits are not enforced right now."
                ),
                recommendation=(
                    where_to_set("rate_limit_fail_mode", "closed", sentence_start=True)
                    + ", where refusing requests during a Redis outage is better than unthrottled "
                    "sign-in attempts."
                ),
                control="infrastructure.rate_limit_fail_mode",
            )
        )
    quota_mode = option_reference("quota_fail_mode")
    if security_option("quota_fail_mode") == "closed":
        findings.append(
            AuditFinding(
                id="infrastructure.quota_fails_closed",
                category="infrastructure",
                severity="pass",
                title="Quotas fail closed",
                description=_quota_fails_closed_description(quota_mode),
                control="infrastructure.quota_fail_mode",
            )
        )
    else:
        findings.append(
            AuditFinding(
                id="infrastructure.quota_fails_open",
                category="infrastructure",
                severity="info" if redis_reachable else "warning",
                title=(
                    "Quotas are skipped while their store is unreachable"
                    if redis_reachable
                    else "Quotas are not enforced: Redis is unreachable"
                ),
                description=(
                    f"{quota_mode} is open (the default): while the quota store cannot be reached, "
                    "agentic executions run unmetered."
                    if redis_reachable
                    else f"Redis cannot be reached and {quota_mode} is open: agentic executions run "
                    "unmetered right now."
                ),
                recommendation=(
                    where_to_set("quota_fail_mode", "closed", sentence_start=True)
                    + ", where unmetered usage during an outage is not acceptable."
                ),
                control="infrastructure.quota_fail_mode",
            )
        )
    # The idle timeout reads its deadlines in Redis and fails open
    # (``core/sessions.py``): refusing every session instead would sign
    # everyone out for as long as Redis is down.
    if int(security_option("session_idle_timeout_minutes")) > 0:
        idle_timeout = option_reference("session_idle_timeout_minutes")
        if redis_reachable:
            findings.append(
                AuditFinding(
                    id="infrastructure.session_idle_timeout_fails_open",
                    category="infrastructure",
                    severity="info",
                    title="The idle timeout is skipped while Redis is unreachable",
                    description=(
                        f"{idle_timeout} reads each session's last activity in Redis: while Redis cannot "
                        "be reached, sessions are not ended for inactivity (their absolute lifetime still "
                        "ends them), rather than every session being refused."
                    ),
                    control="authentication.session_idle_timeout",
                )
            )
        else:
            findings.append(
                AuditFinding(
                    id="infrastructure.session_idle_timeout_unenforced",
                    category="infrastructure",
                    severity="warning",
                    title="Idle sessions are not ended: Redis is unreachable",
                    description=(
                        f"Redis cannot be reached, so {idle_timeout} is not enforced right now: a session "
                        "nobody uses stays open until its absolute lifetime ends."
                    ),
                    recommendation="Restore the Redis connection: the idle timeout applies again at once.",
                    control="authentication.session_idle_timeout",
                )
            )
    return findings


async def _check_infrastructure(findings: list[AuditFinding]) -> None:
    """Check infrastructure health (Redis, environment)."""
    # Check Redis connectivity
    redis_reachable = True
    try:
        r = await redis_client()
        await asyncio.wait_for(r.ping(), timeout=_REDIS_PING_TIMEOUT_SECONDS)
        findings.append(
            AuditFinding(
                id="infrastructure.redis_healthy",
                category="infrastructure",
                severity="pass",
                title="Redis connection healthy",
                description="Redis is reachable and responding.",
            )
        )
    except Exception as exc:
        redis_reachable = False
        logger.warning(
            "Security audit: Redis check failed: %s", format_exception_message(exc)
        )
        findings.append(
            AuditFinding(
                id="infrastructure.redis_unreachable",
                category="infrastructure",
                severity="critical",
                title="Redis connection failed",
                description="Cannot connect to Redis. Scheduled assignments, sessions, and caching will not work.",
                recommendation=(
                    "Check REDIS_SENTINEL_HOSTS and REDIS_SENTINEL_SERVICE and ensure the Sentinels and the Redis master are reachable."
                    if settings.redis_mode == "sentinel"
                    else "Check REDIS_URL configuration and ensure Redis is running."
                ),
            )
        )

    findings.extend(_fail_mode_findings(redis_reachable))

    # Check for debug mode in production
    if settings.debug:
        findings.append(
            AuditFinding(
                id="infrastructure.debug_mode",
                category="infrastructure",
                severity="warning",
                title="Debug mode is enabled",
                description=(
                    "The application is running in debug mode: the database layer echoes every SQL "
                    "statement to the log with the tokens and encrypted values it carries, plain HTTP "
                    "from loopback counts as encrypted, password reset links are written to the log when "
                    "no SMTP server is set, and development-only agent tools are registered."
                ),
                recommendation="Set DEBUG=false in production environments.",
                control="infrastructure.debug_mode",
            )
        )
    else:
        findings.append(
            AuditFinding(
                id="infrastructure.debug_off",
                category="infrastructure",
                severity="pass",
                title="Debug mode is off",
                description="DEBUG is false: nothing is echoed or exempted for development.",
                control="infrastructure.debug_mode",
            )
        )


async def _check_cors(findings: list[AuditFinding]) -> None:
    """The origins a browser page may call the API from (``CORSMiddleware``, credentials allowed)."""
    origins = list(settings.cors_origins or ())
    if "*" in origins:
        findings.append(
            AuditFinding(
                id="web.cors_wildcard",
                category="web",
                severity="warning",
                title="Permissive CORS configuration",
                description="CORS_ORIGINS includes wildcard '*', allowing requests from any origin.",
                recommendation="Restrict CORS_ORIGINS to specific trusted domains in production.",
                control="web.cors_origins",
            )
        )
        return
    findings.append(
        AuditFinding(
            id="web.cors_restricted",
            category="web",
            severity="pass",
            title=f"CORS restricted to {len(origins)} origin(s)",
            description=(
                f"CORS_ORIGINS names the origins a browser page may call the API from: {_listed(origins)}."
                if origins
                else "CORS_ORIGINS is empty: no page on another origin may call the API from a browser."
            ),
            control="web.cors_origins",
        )
    )


def _trusts_every_peer(value: str) -> bool:
    """Whether uvicorn, given ``value``, takes forwarded headers from any peer: ``*``, or a ``/0`` network."""
    return parse_forwarded_allow_ips(value).trusts_every_peer


def _unmatched_entries_note(ignored: list[str] | tuple[str, ...]) -> str:
    if not ignored:
        return ""
    return (
        f" uvicorn matches no peer against {', '.join(ignored)}: a hostname is not resolved, * "
        "means every peer only as the whole value, and a CIDR needs its host bits at zero."
    )


#: Where FORWARDED_ALLOW_IPS is set: uvicorn reads it when the process starts.
_FORWARDED_ALLOW_IPS_IS_ENVIRONMENT = (
    "FORWARDED_ALLOW_IPS is an environment variable of the platform container, read when it starts, not a "
    "Settings > Access and security field: set it in .env with Docker Compose, in xtmOne.platform.env or "
    "xtmone.platform.env with the Helm charts."
)


def _unread_client_address_description(sighting: Sighting, trusted: str, declared: bool, trust_option: str) -> str:
    """What ``network.client_addresses_undeclared_proxy`` says about *sighting*."""
    peer = sighting.peer
    literals = parse_forwarded_allow_ips(trusted).literals
    if sighting.header == FORWARDED_FOR and not declared:
        source = f"{peer}, a peer" if peer else "a peer"
        return (
            f"In the last day, audited requests carrying X-Forwarded-For reached the platform from {source} "
            f"uvicorn does not read that header for, while no reverse proxy is declared: {trust_option} is off "
            f"and FORWARDED_ALLOW_IPS ({trusted or 'empty'}) names no proxy beyond this host."
            f"{_unmatched_entries_note(literals)} The audit trail then records the address of the peer that "
            "relayed each request - the reverse proxy or ingress - instead of the user's. If no proxy fronts the "
            "platform, the clients sent the header themselves and nothing needs to change."
        )
    if sighting.header == FORWARDED_FOR:
        source = peer or "a peer"
        https = (
            " Nor does uvicorn take X-Forwarded-Proto from it, so a request it relays reads as plain HTTP: "
            f"{_plain_http_outcome()}."
            if _base_url_is_https()
            else ""
        )
        return (
            f"In the last day, audited requests carrying X-Forwarded-For reached the platform from {source}, a "
            f"peer FORWARDED_ALLOW_IPS ({trusted}) does not name, so uvicorn does not read the header from it: "
            f"the audit trail records {source} instead of the user's address for every request it relays."
            f"{_unmatched_entries_note(literals)}{https} If it is not a reverse proxy or ingress of yours, a "
            "client sent the header itself and nothing needs to change."
        )
    return (
        f"In the last day, audited requests reached the platform from {peer or 'a peer'} naming their client in "
        f"{HEADER_LABELS[sighting.header]} but not in X-Forwarded-For, the only header the platform reads the "
        f"client address from: the audit trail records {peer or 'the address of the peer that relayed each request'} "
        "instead of the user's. If no proxy fronts the platform, a client sent the header itself and nothing needs "
        "to change."
    )


def _unread_client_address_recommendation(
    sighting: Sighting, trusted: str, declared: bool, trust_option: str
) -> str:
    """What ``network.client_addresses_undeclared_proxy`` recommends for *sighting*."""
    peer = sighting.peer
    where = (
        f"{peer} here, the peer named in the platform's once-per-process warning"
        if peer
        else "the peer named in the platform's once-per-process warning, or the client_ip of an access-log line"
    )
    avoid = f"Avoid * and {trust_option}, which let any client choose the address recorded."
    docs = f"See {CLIENT_ADDRESSES_DOCS_URL}"
    if sighting.header != FORWARDED_FOR:
        steps = [
            "Make the reverse proxy send X-Forwarded-For (with nginx, proxy_set_header X-Forwarded-For "
            "$proxy_add_x_forwarded_for; with HAProxy, option forwardfor; Traefik and ingress-nginx send it by "
            "default)."
        ]
        if not parse_forwarded_allow_ips(trusted).trusts(peer):
            steps += [
                f"Then set FORWARDED_ALLOW_IPS to its address or CIDR as the platform container sees it - {where}; "
                "behind a Kubernetes ingress, the cluster's pod network - and restart.",
                _FORWARDED_ALLOW_IPS_IS_ENVIRONMENT,
                avoid,
            ]
        return " ".join([*steps, docs])
    if declared:
        declare = (
            f"If {peer or 'that peer'} is a reverse proxy or ingress of yours, add its address, or a network covering "
            f"it, to FORWARDED_ALLOW_IPS (a comma-separated list, now {trusted}) as the platform container sees it - "
            "behind a Kubernetes ingress, the cluster's pod network - and restart."
        )
    else:
        declare = (
            f"Set FORWARDED_ALLOW_IPS to the proxy's address or CIDR as the platform container sees it - {where}; "
            "behind a Kubernetes ingress, the cluster's pod network - and restart."
        )
    return " ".join([declare, _FORWARDED_ALLOW_IPS_IS_ENVIRONMENT, avoid, docs])


def _plain_http_outcome() -> str:
    """What a request that reads as plain HTTP meets, the credential routes by ``credential_transport_strict``."""
    if security_option("credential_transport_strict"):
        return (
            "the A2A endpoints and /runners/enroll refuse it (403 https required), /mcp/oauth/register "
            "answers invalid_client_metadata and /mcp/oauth/token answers invalid_request because "
            f"{option_reference('credential_transport_strict')} is on"
        )
    return (
        "the A2A endpoints refuse it (403 https required), /runners/enroll, /mcp/oauth/register "
        "and /mcp/oauth/token log an unconfirmed-transport warning"
    )


def _base_url_is_https() -> bool:
    try:
        return urlsplit(settings.base_url or "").scheme == "https"
    except ValueError:
        return False


async def _check_forwarded_client_addresses(findings: list[AuditFinding]) -> None:
    """Whether the audit trail records users' addresses or a reverse proxy's.

    Traffic-based, where :func:`_check_proxy_trust` reads the configuration: a
    process that sees an audited request whose client address the audit trail
    cannot read - ``X-Forwarded-For`` from a peer ``FORWARDED_ALLOW_IPS`` does
    not name, whether a proxy is declared or not, or a client named only in
    ``X-Real-IP`` or ``Forwarded`` - leaves a marker naming the peer in Redis
    (``app.core.undeclared_proxy``), which the audit reads on any replica and
    reports until the configuration reads that peer. Forwarded headers
    believed from any peer are :func:`_check_proxy_trust`'s warning, which
    says the client then picks the address recorded: nothing is added here
    for them.
    """
    if believed_from_any_peer():
        return
    trusted = forwarded_allow_ips()
    trust_option = option_reference("trust_proxy_headers")
    declared = proxy_declared()
    seen = await undeclared_proxy_seen()
    if seen and sighting_stands(seen):
        findings.append(
            AuditFinding(
                id="network.client_addresses_undeclared_proxy",
                category="network",
                severity="warning",
                title="The audit trail records a proxy's address, not users'",
                description=_unread_client_address_description(seen, trusted, declared, trust_option),
                recommendation=_unread_client_address_recommendation(seen, trusted, declared, trust_option),
                control="network.trust_proxy_headers",
            )
        )
        return
    if declared:
        findings.append(
            AuditFinding(
                id="network.client_addresses_declared_proxy",
                category="network",
                severity="pass",
                title="Client addresses are read through a declared proxy",
                description=(
                    f"FORWARDED_ALLOW_IPS ({trusted}) names a proxy beyond this host: for a request that "
                    "proxy relays, uvicorn replaces the proxy's address with the client address it forwards "
                    "in X-Forwarded-For, and the audit trail records that one."
                    + (
                        " Whether an audited request of the last day came from a peer it does not name could not "
                        "be read (Redis did not answer)."
                        if seen is None
                        else " No audited request of the last day, on any replica, came from a peer it does not "
                        "name or named its client in X-Real-IP or Forwarded only."
                    )
                ),
                control="network.trust_proxy_headers",
            )
        )
        return
    undeclared = f"{trust_option} is off and FORWARDED_ALLOW_IPS ({trusted or 'empty'}) names no proxy beyond this host"
    if seen is None:
        findings.append(
            AuditFinding(
                id="network.client_addresses_check_skipped",
                category="network",
                severity="info",
                title="Undeclared proxy check skipped",
                description=(
                    f"No reverse proxy is declared ({undeclared}), and whether an audited request of the "
                    "last day came through one could not be read (Redis did not answer)."
                ),
                control="network.trust_proxy_headers",
            )
        )
        return
    findings.append(
        AuditFinding(
            id="network.client_addresses_no_undeclared_proxy",
            category="network",
            severity="info",
            title="No undeclared proxy relayed an audited request in the last day",
            description=(
                f"No reverse proxy is declared ({undeclared}), and no audited request of the last day, on any "
                "replica, carried X-Forwarded-For from a peer uvicorn does not read it for or named its client in "
                "X-Real-IP or Forwarded only: the audit trail records the address each request came from. A proxy "
                "that names the client in none of these headers is not seen."
            ),
            recommendation=(
                "If a reverse proxy or an ingress fronts the platform, have it send X-Forwarded-For and set "
                f"FORWARDED_ALLOW_IPS to its address or CIDR, then restart. {_FORWARDED_ALLOW_IPS_IS_ENVIRONMENT}"
            ),
            control="network.trust_proxy_headers",
        )
    )


def _check_proxy_trust(findings: list[AuditFinding]) -> None:
    """Who may state a request's client address and scheme, and whether an HTTPS deployment trusts its proxy.

    Forwarded headers believed from any peer let a client that reaches the
    platform directly pick the address the rate limits and the audit trail
    record. Otherwise, ``BASE_URL`` may say the platform is reached over
    HTTPS while uvicorn's ``FORWARDED_ALLOW_IPS`` lets no proxy beyond
    loopback say so, which is the Helm charts' default and a Compose stack's
    behind an external proxy. Dev builds skip that second check: they are not
    partner-facing, and ``DEBUG`` has a finding of its own.
    """
    trusted = effective_forwarded_allow_ips()
    trust_option = option_reference("trust_proxy_headers")
    flag_on = bool(security_option("trust_proxy_headers"))
    any_peer = [
        reason
        for reason, applies in (
            (f"{trust_option} is on", flag_on),
            (f"FORWARDED_ALLOW_IPS ({trusted}) trusts every peer", _trusts_every_peer(trusted)),
        )
        if applies
    ]
    if any_peer:
        findings.append(
            AuditFinding(
                id="network.forwarded_headers_trusted",
                category="network",
                severity="warning",
                title="Forwarded headers are trusted from any peer",
                description=(
                    f"{_joined(any_peer)}: a client that reaches the platform without going through the "
                    "reverse proxy sets its own X-Forwarded-For and X-Forwarded-Proto, so it picks the "
                    "client address the rate limits and the audit trail record, and can claim HTTPS. "
                    "This is safe only while nothing but the proxy can reach the platform."
                    + (
                        " TRUST_PROXY_HEADERS is also unsafe behind a proxy that appends to "
                        "X-Forwarded-For: the leftmost value it reads is whatever the client sent, "
                        "even through the proxy; it wins over "
                        "FORWARDED_ALLOW_IPS; and a proxy that writes the address with a port makes "
                        "every connection its own rate-limit bucket unless "
                        f"{option_reference('client_ip_strict')} is on."
                        if flag_on
                        else ""
                    )
                ),
                recommendation=(
                    "Set FORWARDED_ALLOW_IPS to the reverse proxy's address (an IP, a CIDR or a "
                    "comma-separated list) and restart, "
                    + (
                        "check that the access log's client_ip shows real client addresses rather than "
                        "the proxy's, then "
                        if flag_on
                        else "and "
                    )
                    + where_to_set("trust_proxy_headers", "false")
                    + ": the forwarded headers then count only when that proxy sends them."
                ),
                control="network.trust_proxy_headers",
            )
        )
        return
    if settings.debug:
        return
    try:
        if urlsplit(settings.base_url or "").scheme != "https":
            return
    except ValueError:
        return
    remote, ignored = trusted_proxy_entries(trusted)
    if remote:
        findings.append(
            AuditFinding(
                id="network.proxy_trust_declared",
                category="network",
                severity="pass",
                title="A reverse proxy is trusted to confirm HTTPS",
                description=(
                    f"FORWARDED_ALLOW_IPS ({trusted}) names the proxy allowed to state the original "
                    "scheme and client address, and nothing else is believed."
                ),
                control="network.trust_proxy_headers",
            )
        )
        return
    ignored_note = _unmatched_entries_note(ignored)
    refused = _plain_http_outcome()
    findings.append(
        AuditFinding(
            id="network.proxy_trust_missing",
            category="network",
            severity="warning",
            title="No reverse proxy is trusted to confirm HTTPS",
            description=(
                f"BASE_URL is an https URL, but {trust_option} is off and FORWARDED_ALLOW_IPS "
                f"({trusted or 'empty'}) lets no proxy but one on this host state the original scheme."
                f"{ignored_note} A request relayed by a TLS-terminating proxy on another host - "
                f"another container, a Kubernetes ingress, a load balancer - reads as plain HTTP: {refused}, "
                "and per-IP rate limiting buckets every caller under the proxy's address. Nothing "
                "needs to change if the proxy connects from this host itself."
            ),
            recommendation=(
                "Set FORWARDED_ALLOW_IPS to the proxy's address as the platform container sees it "
                "(an IP, a CIDR or a comma-separated list; while no proxy is trusted it is the "
                "access log's client_ip and the peer of the unconfirmed-transport warning) and "
                "restart, with the proxy sending X-Forwarded-Proto. With the Helm "
                "charts, set it in the platform's env (xtmOne.platform.env or "
                f"xtmone.platform.env). Avoid * and {trust_option}. See UPGRADING.md."
            ),
            control="network.trust_proxy_headers",
        )
    )


async def _check_ipv6_rate_limit_key(findings: list[AuditFinding]) -> None:
    """How the per-IP rate limits key a native IPv6 client (``rate_limit_ipv6_prefix``).

    A host or a site is handed a whole /64: keyed on anything narrower, one
    client draws a fresh bucket from each address (or network) of it. Only a
    /64 or a wider network enforces the control; 128 is the default.
    """
    prefix = int(security_option("rate_limit_ipv6_prefix"))
    option = option_reference("rate_limit_ipv6_prefix")
    limits = (
        "the per-IP rate limits (login, password reset, invitation registration, MFA, A2A "
        "pre-authentication, MCP OAuth, the public chat widget, content security policy reports)"
    )
    if prefix <= 64:
        findings.append(
            AuditFinding(
                id="network.ipv6_rate_limit_per_prefix",
                category="network",
                severity="pass",
                title=f"IPv6 clients are rate-limited per /{prefix}",
                description=(
                    f"{option} is {prefix}: {limits} key a native IPv6 client on its /{prefix} "
                    "network, so rotating addresses within it draws no fresh bucket."
                ),
                control="network.rate_limit_ipv6_prefix",
            )
        )
        return
    if prefix == 128:
        findings.append(
            AuditFinding(
                id="network.ipv6_rate_limit_per_address",
                category="network",
                severity="info",
                title="IPv6 clients are rate-limited per address",
                description=(
                    f"{option} is 128 (the default): {limits} key a native IPv6 client on its full "
                    "address. A host or a site is handed a whole /64, so one client can draw a fresh "
                    "bucket from each address it owns. IPv4 clients are unaffected."
                ),
                recommendation=(
                    where_to_set("rate_limit_ipv6_prefix", "64", sentence_start=True)
                    + " where IPv6 clients do not share a /64 at scale. Every device of one /64 (a "
                    "corporate IPv6 LAN) then shares one budget, which is why it is not the default."
                ),
                control="network.rate_limit_ipv6_prefix",
            )
        )
        return
    findings.append(
        AuditFinding(
            id="network.ipv6_rate_limit_narrow_network",
            category="network",
            severity="info",
            title=f"IPv6 clients are rate-limited per /{prefix}",
            description=(
                f"{option} is {prefix}: {limits} key a native IPv6 client on its /{prefix} network, "
                f"narrower than the /64 a host or a site is handed, so one client can still draw a "
                f"fresh bucket from each /{prefix} of its /64. IPv4 clients are unaffected."
            ),
            recommendation=(
                where_to_set("rate_limit_ipv6_prefix", "64", sentence_start=True)
                + " where IPv6 clients do not share a /64 at scale: every device of one /64 then "
                "shares one budget."
            ),
            control="network.rate_limit_ipv6_prefix",
        )
    )


async def _check_insecure_transport(db: AsyncSession, findings: list[AuditFinding]) -> None:
    """Whether the credential-issuing routes refuse an unconfirmed transport.

    Opt-in (``credential_transport_strict``). Off, a warning only while what it
    protects is in use: the platform served over plain HTTP (``BASE_URL`` not
    https, ``DEBUG`` off) with an active runner or a registered MCP OAuth
    client, whose credentials those routes issue in clear text. Behind a TLS
    endpoint off is context, never a warning a deployment that changed nothing
    would suddenly see. On is a pass.
    """
    option = option_reference("credential_transport_strict")
    if security_option("credential_transport_strict"):
        findings.append(
            AuditFinding(
                id="network.credential_transport_strict_on",
                category="network",
                severity="pass",
                title="Runner enrollment, MCP client registration and MCP token requests need confirmed HTTPS",
                description=(
                    f"{option} is on: POST /runners/enroll, POST /mcp/oauth/register and POST "
                    "/mcp/oauth/token refuse a request they cannot confirm arrived over HTTPS (403 "
                    "https required, the OAuth invalid_client_metadata error and the OAuth "
                    "invalid_request error) before issuing anything."
                ),
                control="network.credential_transport_strict",
            )
        )
        return
    in_use: list[str] = []
    if not settings.debug and not _base_url_is_https():
        runners = (
            await db.execute(select(func.count(Runner.id)).where(Runner.is_active.is_(True)))
        ).scalar() or 0
        clients = (
            await db.execute(
                select(func.count(OAuthClient.id)).where(
                    OAuthClient.a2a_exposure_id.is_(None), OAuthClient.a2a_integration_id.is_(None)
                )
            )
        ).scalar() or 0
        in_use = [
            text
            for count, text in (
                (runners, f"{runners} active runner(s)"),
                (clients, f"{clients} registered MCP OAuth client(s)"),
            )
            if count
        ]
    served = (
        "POST /runners/enroll, POST /mcp/oauth/register and POST /mcp/oauth/token serve a "
        "request they cannot confirm arrived over HTTPS and only log it (once per route), "
    )
    findings.append(
        AuditFinding(
            id="network.credential_transport_strict_off",
            category="network",
            severity="warning" if in_use else "info",
            title=(
                "Runner and MCP credentials are issued over plain HTTP"
                if in_use
                else "Runner enrollment, MCP client registration and MCP token requests are served "
                "over unconfirmed transport"
            ),
            description=(
                f"BASE_URL is not an https URL, so the platform is served over plain HTTP: {served}and the "
                f"run- credentials, client secrets and bearer tokens they issue cross the network in clear "
                f"text - {_joined(in_use)} hold such credentials today. The A2A surface refuses such a request."
                if in_use
                else f"{served}so the run- credentials, client secrets and bearer tokens they issue may cross "
                "the network in clear text. The A2A surface refuses such a request."
            ),
            recommendation=(
                "Serve the platform over HTTPS - BASE_URL its https URL, behind a TLS-terminating proxy "
                "FORWARDED_ALLOW_IPS names - then re-enroll the runners and reconnect the MCP clients, whose "
                "credentials crossed in clear text, and "
                + where_to_set("credential_transport_strict", "true")
                + ". Turned on first, it refuses every enrollment, registration and token request."
                if in_use
                else "Once every request reaches the platform over HTTPS - behind a TLS-terminating "
                "proxy, with FORWARDED_ALLOW_IPS naming it and no unconfirmed-transport warning "
                "left in the log - "
                + where_to_set("credential_transport_strict", "true")
                + "."
            ),
            control="network.credential_transport_strict",
        )
    )


# ─── Main endpoint ────────────────────────────────────────────────────


@router.get("", response_model=SecurityAuditReport)
async def run_security_audit(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Run a comprehensive security audit of the platform configuration.

    Returns a structured report with findings categorised by severity, and
    every security control with its state. Admin-only.
    """
    return await build_security_audit_report(db)


_FindingId = Annotated[
    str, Path(pattern=finding_states.FINDING_ID_PATTERN, max_length=FINDING_ID_MAX_CHARS)
]


@router.put("/findings/{finding_id}/dismissal", response_model=SecurityAuditReport)
async def dismiss_security_audit_finding(
    finding_id: _FindingId,
    request: Request,
    body: FindingDismissalRequest | None = None,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Accept a finding the audit reports now: it leaves the counts and is never notified.

    Refused for an id the current run does not report (404) and for a passing
    check (422). Dismissing a dismissed finding replaces its reason. It re-opens
    on its own once reported at a higher severity. Answers the updated report.
    """
    report = await build_security_audit_report(db)
    finding = finding_states.reported_finding(report.findings, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="The security audit does not report this finding")
    if finding.severity not in finding_states.DISMISSABLE_SEVERITIES:
        raise HTTPException(status_code=422, detail="Only a critical, warning or info finding can be dismissed")
    await finding_states.persist_reopenings(db, report.findings)
    await finding_states.dismiss_finding(
        db, admin=admin, finding=finding, reason=body.reason if body else None, request=request
    )
    await db.commit()
    return await _refreshed_report(db, report)


@router.delete("/findings/{finding_id}/dismissal", response_model=SecurityAuditReport)
async def restore_security_audit_finding(
    finding_id: _FindingId,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Undo a dismissal: the finding counts again. 404 when it is not dismissed."""
    report = await build_security_audit_report(db)
    await finding_states.persist_reopenings(db, report.findings)
    if not await finding_states.restore_finding(db, admin=admin, finding_id=finding_id, request=request):
        raise HTTPException(status_code=404, detail="This finding is not dismissed")
    await db.commit()
    return await _refreshed_report(db, report)


async def build_security_audit_report(db: AsyncSession) -> SecurityAuditReport:
    """The report the route answers, also run by the scheduler to notify administrators."""
    platform_row = await _load_row(db, PlatformSettings)
    sso_row = await _load_row(db, SSOConfig)
    findings: list[AuditFinding] = []

    await _check_encryption(findings)
    await _check_webhook_configs(db, findings)
    await _check_secret_vaults(db, findings)
    await _check_authentication(db, findings, sso_row)
    await _check_sso(findings, sso_row)
    await _check_self_registration(findings, sso_row)
    await _check_mfa(db, findings, sso_row)
    await _check_sessions(findings)
    await _check_account_lifetimes(findings, sso_row)
    await _check_dormant_account_automations(db, findings)
    await _check_api_keys(db, findings)
    await _check_platform_registration(findings)
    await _check_registered_platforms(db, findings)
    await _check_platform_token_audiences(findings)
    await _check_integrations(db, findings)
    await _check_oauth_token_url_policy(db, findings)
    await _check_oauth_token_dev_origins(findings)
    await _check_integration_notifications(db, findings)
    await _check_agents(db, findings)
    await _check_tool_result_guard(db, findings)
    await _check_agent_tool_admin_access(db, findings)
    await _check_agent_output_schemas(db, findings)
    await _check_tool_approvals(db, findings, platform_row)
    await _check_channel_bots(db, findings)
    await _check_teams_graph_notifications(db, findings)
    await _check_public_widget(db, findings)
    widget_uploads = await _check_public_widget_uploads(db, findings)
    await _check_prompt_guards(db, findings)
    await _check_model_output_guard(db, findings)
    await _check_nova_semantic_calibration(db, findings)
    await _check_integration_scopes(db, findings)
    await _check_runners(db, findings)
    await _check_relays(db, findings)
    await _check_assignments(db, findings)
    await _check_trigger_integration_delivery(db, findings)
    await _check_event_matching(db, findings)
    await _check_kb_sync_source_integrations(db, findings)
    await _check_shared_personal_credentials(db, findings)
    await _check_restricted_reach_without_account(db, findings)
    await _check_custom_tool_sandbox(findings)
    await _check_sandbox_features(findings)
    await _check_tool_call_budget(findings)
    await _check_content_security_policy(findings)
    await _check_mcp(db, findings, platform_row)
    await _check_llm_providers(db, findings)
    await _check_trace_payloads(findings, platform_row)
    await _check_infrastructure(findings)
    await _check_cors(findings)
    await _check_forwarded_client_addresses(findings)
    _check_proxy_trust(findings)
    await _check_insecure_transport(db, findings)
    await _check_ipv6_rate_limit_key(findings)

    statuses = control_statuses(platform_row, sso_row, widget_uploads)
    return await _finish_report(
        db,
        findings,
        [SecurityControlStatus(**dataclasses.asdict(status)) for status in statuses],
        controls_summary(statuses),
    )


async def _refreshed_report(db: AsyncSession, report: SecurityAuditReport) -> SecurityAuditReport:
    """*report* with the finding states read again, without running the checks twice."""
    return await _finish_report(db, report.findings, report.controls, report.controls_summary)


def _dismissal_payload(view: finding_states.DismissalView | None) -> FindingDismissal | None:
    if view is None:
        return None
    return FindingDismissal(
        at=view.at.isoformat() if view.at else None,
        severity=view.severity,
        reason=view.reason,
        by_user_id=str(view.by_user_id) if view.by_user_id else None,
        by=view.by,
    )


async def _finish_report(
    db: AsyncSession,
    findings: list[AuditFinding],
    controls: list[SecurityControlStatus],
    control_counts: dict[str, int],
) -> SecurityAuditReport:
    """Mark the dismissed and re-opened findings, then count and score the others."""
    views = await finding_states.finding_views(db, findings)
    for finding in findings:
        view = views.get(finding.id) if finding.id else None
        finding.dismissed = _dismissal_payload(view.dismissed) if view else None
        finding.reopened = (
            FindingReopening(
                at=view.reopened_at.isoformat() if view.reopened_at else None,
                severity=view.reopened_severity or finding.severity,
                dismissal=_dismissal_payload(view.reopened_dismissal),
            )
            if view and view.reopened
            else None
        )
    active = [finding for finding in findings if finding.dismissed is None]

    summary: dict[str, int] = {"critical": 0, "warning": 0, "info": 0, "pass": 0}
    for f in active:
        summary[f.severity] = summary.get(f.severity, 0) + 1

    if summary["critical"] > 0:
        overall = "at_risk"
    elif summary["warning"] > 0:
        overall = "needs_attention"
    else:
        overall = "healthy"

    return SecurityAuditReport(
        timestamp=datetime.now(UTC).isoformat(),
        overall_score=overall,
        findings=findings,
        summary=summary,
        controls=controls,
        controls_summary=control_counts,
        dismissed_count=len(findings) - len(active),
    )
