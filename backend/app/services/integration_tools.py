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

"""Integration tools dispatcher — routes to per-provider tool builders."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import enum
import functools
import inspect
import json as _json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
import structlog
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import TRUNCATE_SNIPPET
from app.core.egress import EgressRoute, egress_client, egress_route
from app.core.integration_tool_slug import derive_tool_slug, tool_slug_error
from app.core.module_versions import registered_version_reader
from app.core.platform_identity import (
    RegisteredPlatform,
    platform_for_integration,
    platforms_reached_internally,
    registered_procedure_manifest,
)
from app.core.security import decrypt_json, decrypt_value, encrypt_value
from app.integrations.http import format_http_error, format_network_error
from app.integrations.providers.abusech import build_tools as abusech_tools
from app.integrations.providers.abuseipdb import build_tools as abuseipdb_tools
from app.integrations.providers.aikido import build_tools as aikido_tools
from app.integrations.providers.airwallex import build_tools as airwallex_tools
from app.integrations.providers.anomali import build_tools as anomali_tools
from app.integrations.providers.anthropic import build_tools as anthropic_tools
from app.integrations.providers.anyrun import build_tools as anyrun_tools
from app.integrations.providers.ashby import build_tools as ashby_tools
from app.integrations.providers.attackiq import build_tools as attackiq_tools
from app.integrations.providers.aws_security import build_tools as aws_security_tools
from app.integrations.providers.bluesky import build_tools as bluesky_tools
from app.integrations.providers.brave_search import build_tools as brave_search_tools
from app.integrations.providers.concord import build_tools as concord_tools
from app.integrations.providers.credly import build_tools as credly_tools
from app.integrations.providers.crowdsec import build_tools as crowdsec_tools
from app.integrations.providers.crowdstrike import build_tools as crowdstrike_tools
from app.integrations.providers.crowdstrike_ngsiem import (
    build_tools as crowdstrike_ngsiem_tools,
)
from app.integrations.providers.cybersixgill import (
    build_tools as cybersixgill_tools,
)
from app.integrations.providers.cyware import build_tools as cyware_tools
from app.integrations.providers.databricks import build_tools as databricks_tools
from app.integrations.providers.dealhub import build_tools as dealhub_tools
from app.integrations.providers.defender import build_tools as defender_tools
from app.integrations.providers.discord_bot import build_tools as discord_bot_tools
from app.integrations.providers.docusign import build_tools as docusign_tools
from app.integrations.providers.domaintools import build_tools as domaintools_tools
from app.integrations.providers.doppel import build_tools as doppel_tools
from app.integrations.providers.drata import build_tools as drata_tools
from app.integrations.providers.elastic_security import (
    build_tools as elastic_security_tools,
)
from app.integrations.providers.entra import build_tools as entra_tools
from app.integrations.providers.facebook import build_tools as facebook_tools
from app.integrations.providers.feedly import build_tools as feedly_tools
from app.integrations.providers.figma import build_tools as figma_tools
from app.integrations.providers.filigran_ai import build_tools as filigran_ai_tools
from app.integrations.providers.flashpoint import build_tools as flashpoint_tools
from app.integrations.providers.fortisiem import build_tools as fortisiem_tools
from app.integrations.providers.gemini import build_tools as gemini_tools
from app.integrations.providers.giphy import MEDIA_HOSTS as GIPHY_MEDIA_HOSTS
from app.integrations.providers.giphy import build_tools as giphy_tools
from app.integrations.providers.github import build_tools as github_tools
from app.integrations.providers.gitlab import build_tools as gitlab_tools
from app.integrations.providers.google import build_tools as google_tools
from app.integrations.providers.google_search import build_tools as google_search_tools
from app.integrations.providers.google_secops import (
    build_tools as google_secops_tools,
)
from app.integrations.providers.googlechat_bot import (
    build_tools as googlechat_bot_tools,
)
from app.integrations.providers.grafana import build_tools as grafana_tools
from app.integrations.providers.greynoise import build_tools as greynoise_tools
from app.integrations.providers.gti import build_tools as gti_tools
from app.integrations.providers.hackerone import build_tools as hackerone_tools
from app.integrations.providers.harfanglab import build_tools as harfanglab_tools
from app.integrations.providers.hibob import build_tools as hibob_tools
from app.integrations.providers.hubspot import build_tools as hubspot_tools
from app.integrations.providers.infoblox import build_tools as infoblox_tools
from app.integrations.providers.intel471 import build_tools as intel471_tools
from app.integrations.providers.intune import build_tools as intune_tools
from app.integrations.providers.jira import build_tools as jira_tools
from app.integrations.providers.letsignit import build_tools as letsignit_tools
from app.integrations.providers.linkedin import build_tools as linkedin_tools
from app.integrations.providers.logrhythm import build_tools as logrhythm_tools
from app.integrations.providers.mattermost import build_tools as mattermost_tools
from app.integrations.providers.metabase import build_tools as metabase_tools
from app.integrations.providers.microsoft import build_tools as microsoft_tools
from app.integrations.providers.misp import build_tools as misp_tools
from app.integrations.providers.mistral import build_tools as mistral_tools
from app.integrations.providers.mitre_atlas import build_tools as mitre_atlas_tools
from app.integrations.providers.mitre_attack import build_tools as mitre_attack_tools
from app.integrations.providers.n8n import build_tools as n8n_tools
from app.integrations.providers.netsuite import build_tools as netsuite_tools
from app.integrations.providers.newsapi import build_tools as newsapi_tools
from app.integrations.providers.notion import build_tools as notion_tools
from app.integrations.providers.odoo import build_tools as odoo_tools
from app.integrations.providers.onepassword import build_tools as onepassword_tools
from app.integrations.providers.openaev import build_tools as openaev_tools
from app.integrations.providers.openai import build_tools as openai_tools
from app.integrations.providers.openapi import (
    GRANT_TOKEN as _OPENAPI_GRANT_TOKEN,
)
from app.integrations.providers.openapi import build_tools as openapi_tools
from app.integrations.providers.openapi import load_openapi_runtime
from app.integrations.providers.opencrq import build_tools as opencrq_tools
from app.integrations.providers.opencrq.procedure_manifest import PROBE, Manifest, ProbeLater
from app.integrations.providers.opencti import build_tools as opencti_tools
from app.integrations.providers.ovhcloud import build_tools as ovhcloud_tools
from app.integrations.providers.pagerduty import build_tools as pagerduty_tools
from app.integrations.providers.pennylane import build_tools as pennylane_tools
from app.integrations.providers.perk import build_tools as perk_tools
from app.integrations.providers.picus import build_tools as picus_tools
from app.integrations.providers.planhat import build_tools as planhat_tools
from app.integrations.providers.platform_email import build_tools as platform_email_tools
from app.integrations.providers.proofpoint import build_tools as proofpoint_tools
from app.integrations.providers.purview import build_tools as purview_tools
from app.integrations.providers.qradar import build_tools as qradar_tools
from app.integrations.providers.qualys import build_tools as qualys_tools
from app.integrations.providers.quotapath import build_tools as quotapath_tools
from app.integrations.providers.ransomware_live import (
    build_tools as ransomware_live_tools,
)
from app.integrations.providers.rapid7 import build_tools as rapid7_tools
from app.integrations.providers.recorded_future import (
    build_tools as recorded_future_tools,
)
from app.integrations.providers.rss import build_tools as rss_tools
from app.integrations.providers.rydoo import build_tools as rydoo_tools
from app.integrations.providers.securityscorecard import (
    build_tools as securityscorecard_tools,
)
from app.integrations.providers.sekoia import build_tools as sekoia_tools
from app.integrations.providers.sentinel import build_tools as sentinel_tools
from app.integrations.providers.sentinelone import build_tools as sentinelone_tools
from app.integrations.providers.servicenow import build_tools as servicenow_tools
from app.integrations.providers.ses import build_tools as ses_tools
from app.integrations.providers.shodan import build_tools as shodan_tools
from app.integrations.providers.slack import build_tools as slack_tools
from app.integrations.providers.slack_bot import build_tools as slack_bot_tools
from app.integrations.providers.socradar import build_tools as socradar_tools
from app.integrations.providers.spendesk import build_tools as spendesk_tools
from app.integrations.providers.splunk import build_tools as splunk_tools
from app.integrations.providers.sublime import build_tools as sublime_tools
from app.integrations.providers.sumologic import build_tools as sumologic_tools
from app.integrations.providers.supabase import build_tools as supabase_tools
from app.integrations.providers.supabase_postgres import encode_access as supabase_database_segment
from app.integrations.providers.tanium import build_tools as tanium_tools
from app.integrations.providers.teams_bot import build_tools as teams_bot_tools
from app.integrations.providers.telegram_bot import build_tools as telegram_bot_tools
from app.integrations.providers.tenable import build_tools as tenable_tools
from app.integrations.providers.thehive import build_tools as thehive_tools
from app.integrations.providers.threatconnect import (
    build_tools as threatconnect_tools,
)
from app.integrations.providers.threatq import build_tools as threatq_tools
from app.integrations.providers.urlscan import build_tools as urlscan_tools
from app.integrations.providers.virustotal import build_tools as virustotal_tools
from app.integrations.providers.vuln_intel import build_tools as vuln_intel_tools
from app.integrations.providers.vulncheck import build_tools as vulncheck_tools
from app.integrations.providers.whatsapp import build_tools as whatsapp_tools
from app.integrations.providers.whatsapp_bot import build_tools as whatsapp_bot_tools
from app.integrations.providers.whatsapp_gateway import BACKEND_GATEWAY as WHATSAPP_GATEWAY_BACKEND
from app.integrations.providers.whatsapp_gateway import gateway_token as whatsapp_gateway_token
from app.integrations.providers.wiz import build_tools as wiz_tools
from app.integrations.providers.wordsmith import build_tools as wordsmith_tools
from app.integrations.providers.x import build_tools as x_tools
from app.integrations.providers.xdr import build_tools as xdr_tools
from app.integrations.providers.xsoar import build_tools as xsoar_tools
from app.integrations.providers.xtm_hub import build_tools as xtm_hub_tools
from app.integrations.providers.youtrack import build_tools as youtrack_tools
from app.integrations.providers.zendesk import build_tools as zendesk_tools
from app.integrations.service_token_exchange import (
    oauth_exchange_spec,
    read_service_token_answer,
    refresh_request,
    service_base_url,
)
from app.models.channel_bot import ChannelBot
from app.models.custom_integration_definition import CustomIntegrationDefinition
from app.models.integration import Integration
from app.models.integration_user_credential import IntegrationUserCredential
from app.services.hitl_consent import hitl_history_approves
from app.services.integration_credentials import (
    invisible_integration_bindings,
    normalize_acting_user_id,
    oauth_credentials_usable,
    oauth_credentials_usable_clause,
    own_account_not_connected_binding,
    refused_binding,
)
from app.services.integration_user_credentials import (
    CREDENTIAL_MODE_PER_USER,
    binding_integration_id,
    connected_user_ids,
    holder_ref,
    integration_of,
    is_per_user,
    load_token_holder,
    needs_own_credential,
    resolve_token_holder,
    usable_credential_clause,
)
from app.services.openapi_import.service import (
    OPENAPI_PROVIDER,
    PER_USER_UNRESOLVABLE_PROVIDERS,
    provider_resolves_per_user,
)
from app.services.vault import (
    REFERENCE_COLUMNS,
    SecretUnavailableError,
    invalidate_entity_secrets,
    reveal_field,
    reveal_json_field,
)
from app.tools.base import (
    CallableToolExecutor,
    HumanInputRequestedError,
    ToolCall,
    ToolDefinition,
    ToolResult,
    is_error_shaped_result,
    register_fetched_file,
)

logger = logging.getLogger(__name__)


def _decrypt_dict(enc: str | None) -> dict[str, Any]:
    """Decrypt an encrypted JSON config and narrow the type to ``dict``.

    ``extra_config_enc`` is always a serialised JSON object in practice.
    This wrapper handles the ``None`` / empty / non-dict cases defensively
    so callers get a guaranteed ``dict[str, Any]`` on success.

    NOTE: like ``decrypt_json``, this still raises when the ciphertext
    itself cannot be decrypted (corrupted value, rotated key). Call sites
    that must degrade gracefully keep their own try/except so they can log
    provider-specific context — do not swallow the error here, several
    paths deliberately surface decryption failures instead of silently
    treating the config as empty.
    """
    if not enc:
        return {}
    raw = decrypt_json(enc)
    return raw if isinstance(raw, dict) else {}


async def _reveal_extra(integ: Any) -> dict[str, Any]:
    """*integ*'s ``extra_config`` with every vault reference in it resolved, narrowed to ``dict``.

    The reader of the secrets a tool sends (#6080): ``reveal_json_field`` resolves
    each string value that is a vault reference, nested ones included, and records
    an unreadable one against the integration. Like :func:`_decrypt_dict`, it
    raises when the ciphertext cannot be decrypted, and also
    ``SecretUnavailableError`` when a referenced secret cannot be read; the call
    sites keep their own try/except.
    """
    if not getattr(integ, "extra_config_enc", None):
        return {}
    raw = await reveal_json_field(integ, "extra_config_enc")
    return raw if isinstance(raw, dict) else {}


async def _noop_refresh() -> None:
    """No-op token refresh for channel bots (they don't use OAuth)."""


def _binding_owner_user_id(integ: Integration) -> str | None:
    """Return the owning Copilot user ID for logging."""
    return str(integ.user_id) if integ.user_id else None


def _extract_github_binding_metadata(integ: Integration) -> dict[str, list[str]]:
    """Return non-secret GitHub account/install metadata for binding logs."""
    if integ.provider != "github" or not integ.extra_config_enc:
        return {}

    try:
        extra = _decrypt_dict(integ.extra_config_enc)
        installs = extra.get("installations", [])
        if not installs and extra.get("installation_id"):
            installs = [{"id": extra.get("installation_id")}]

        if not isinstance(installs, list):
            return {}

        install_ids: list[str] = []
        account_logins: list[str] = []
        account_types: list[str] = []

        for inst in installs:
            if not isinstance(inst, dict):
                continue
            install_id = str(inst.get("id", "")).strip()
            account_login = str(inst.get("account_login", "")).strip()
            account_type = str(inst.get("account_type", "")).strip()
            if install_id:
                install_ids.append(install_id)
            if account_login:
                account_logins.append(account_login)
            if account_type:
                account_types.append(account_type)

        return {
            "github_installation_ids": sorted(set(install_ids)),
            "github_account_logins": sorted(set(account_logins)),
            "github_account_types": sorted(set(account_types)),
        }
    except Exception as exc:
        # Non-critical (metadata for logs only) but should never silently
        # vanish — without this log, broken GitHub installation rows
        # produce empty audit fields with no visible cause.
        logger.warning(
            "Failed to extract GitHub installation metadata for integration "
            "%s: %s",
            integ.id,
            format_exception_message(exc),
            exc_info=True,
        )
        return {}


# ---------------------------------------------------------------------------
# Public URL extraction (read-only metadata for agent context — no secrets)
# ---------------------------------------------------------------------------
#
# Providers whose ``extra_config.base_url`` is a self-hosted user-facing host
# (the same URL is used for both API calls AND the web UI).  These are safe
# to surface to agents as link targets via ``get_integration_public_url``.
#
# Excluded on purpose:
#   - ``drata``         — region-derived API URL only, not a UI URL
#   - ``filigran_ai``   — ``base_url_stix`` / ``base_url_ttp`` are API-only
#   - ``xsoar`` / ``xdr`` — Cortex XSOAR 8/Cloud and Cortex XDR/XSIAM store the
#                         *tenant API host* (``https://api-<fqdn>``), which is
#                         NOT the console UI host, so surfacing it would yield
#                         broken/misleading deep links. (XSOAR 6 on-prem shares
#                         one host, but we can't reliably distinguish it here,
#                         so exclude both generations.)
#   - all SaaS providers (Microsoft, Google, GitHub, Slack, Notion, …) —
#     their UI hosts are well-known to the LLM and have no per-integration
#     base URL stored
_USER_FACING_BASE_URL_PROVIDERS: tuple[str, ...] = (
    "metabase",
    # Databricks' stored base_url is the workspace URL, which serves both the
    # REST API and the web UI - safe to surface for agent deep links.
    "databricks",
    "opencti",
    "openaev",
    "opencrq",
    "n8n",
    "grafana",
    "youtrack",
    # QRadar's configured base_url IS the console UI host (the API lives at
    # ``{base_url}/api``), so it doubles as a user-facing deep-link target.
    "qradar",
    # FortiSIEM's configured base_url is the Supervisor host, which serves both
    # the REST API ({base}/phoenix/rest) and the web UI — safe to surface for
    # agent deep links.
    "fortisiem",
    # SentinelOne's stored base_url is the management console URL, which is the
    # same host used for the web UI — safe to surface for agent deep links.
    "sentinelone",
    # HarfangLab's stored base_url is the manager URL, which serves both the
    # API (``{base_url}/api``) and the web console — safe to surface for
    # agent deep links.
    "harfanglab",
    # AttackIQ's stored base_url is the tenant instance URL
    # (https://<tenant>.attackiq.com), which is also the web UI host — safe to
    # surface for agent deep links.
    "attackiq",
    # Elastic Security's stored base_url is the Kibana URL (the web UI host) —
    # safe to surface for agent deep links into the Security app.
    "elastic_security",
    # Tenable's stored base_url is the Tenable Vulnerability Management cloud
    # host (e.g. https://cloud.tenable.com), which is also the web UI host —
    # safe to surface for agent deep links.
    "tenable",
    # ServiceNow's stored base_url is the instance URL
    # (https://<instance>.service-now.com), which is also the web UI host — safe
    # to surface for record deep links (e.g. {base}/incident.do?sys_id=...).
    "servicenow",
    # ThreatConnect's stored base_url is the instance URL (the API lives at
    # ``{base_url}/api``), and every v3 object returns a webLink on that same
    # host — safe to surface for agent deep links.
    "threatconnect",
    # MISP serves the REST API and the web UI from the same host, so the
    # stored base_url is the user-facing UI host — safe to surface for event
    # deep links (e.g. {base}/events/view/{id}).
    "misp",
    # ThreatQ's stored base_url is the appliance host, which serves both the
    # REST API ({base}/api) and the web UI — safe to surface for Threat
    # Library deep links (e.g. {base}/indicators/{id}/details).
    "threatq",
    # TheHive serves the REST API ({base}/api/v1) and the web UI from the
    # same host, so the stored base_url is the user-facing UI host — safe to
    # surface for case/alert deep links (e.g. {base}/cases/{id}/details).
    "thehive",
    # NOTE: Tanium, Cortex XDR, and Cortex XSOAR are intentionally NOT listed.
    # Their configured base_url is an API host (e.g.
    # ``https://<customer>-api.cloud.tanium.com``, ``https://api-<fqdn>`` for
    # Cortex), not the console/UI host — surfacing it would produce misleading
    # deep links. See test_xdr.py / test_xsoar.py. CrowdStrike and Google SecOps
    # are likewise API-only and intentionally excluded. Splunk is also excluded:
    # its configured base_url is the management REST host (``:8089``), not the
    # web UI host (``:8000`` / a different Splunk Cloud host), so a deep link
    # built from it would be broken. Anomali ThreatStream is excluded for the
    # same reason: the default base_url (https://api.threatstream.com) is the
    # API host, not the web UI host (https://ui.threatstream.com). Cybersixgill
    # is excluded too: its base_url is the API host
    # (https://api.cybersixgill.com), not the investigative portal UI host
    # (https://portal.cybersixgill.com) - the provider builds portal deep links
    # from its own fixed PORTAL_BASE_URL constant instead.
)


def get_integration_public_url(
    integ: Integration, platform: RegisteredPlatform | None = None
) -> str | None:
    """Return the user-facing base URL of an integration, or ``None``.

    *platform* is the registration that owns a platform-managed integration
    reached on an internal URL (``platforms_reached_internally``): a
    ``base_url`` on that internal URL is returned on the platform's registered
    URL, the one a browser reaches.

    This is the host URL that the agent can use to construct deep links into
    the underlying platform's UI (e.g. ``https://demo.opencti.io``).  Only
    self-hosted providers whose configured ``base_url`` doubles as the
    public UI host are exposed (see ``_USER_FACING_BASE_URL_PROVIDERS``).
    API-only or region-derived URLs are intentionally NOT returned, and no
    credentials are ever included.

    The result is guaranteed:

    * **secret-free** — only the ``base_url`` key is read out of
      ``extra_config``; every other key (``api_key``, ``refresh_token``,
      ``client_secret``, …) is ignored.
    * **scheme-safe** — only ``http://`` and ``https://`` are accepted.
      Other schemes (``javascript:``, ``file:``, ``data:``, …) are
      rejected so a poisoned integration config can never inject an
      executable link into the agent's response.
    * **userinfo-free** — URLs carrying ``user:pass@host`` credentials
      in the authority are rejected; credentials must live in the
      dedicated encrypted fields, not in ``base_url``.
    * **path-only** — query strings, fragments and embedded control
      characters are stripped so a crafted ``base_url`` can't break
      out of the system-prompt block or the markdown link target.
    """
    from urllib.parse import urlparse, urlunparse

    provider = (integ.provider or "").lower()
    if provider not in _USER_FACING_BASE_URL_PROVIDERS:
        return None
    if not integ.extra_config_enc:
        return None
    try:
        extra = _decrypt_dict(integ.extra_config_enc)
    except Exception as exc:
        logger.warning(
            "get_integration_public_url: failed to decrypt extra_config for '%s' (%s): %s",
            integ.name,
            provider,
            format_exception_message(exc),
        )
        return None
    raw = extra.get("base_url")
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().rstrip("/")
    if platform is not None:
        cleaned = platform.shown(cleaned).rstrip("/")
    if not cleaned:
        return None

    # Defence in depth: validate the URL shape before surfacing it to
    # the LLM. ASCII control characters (incl. CR/LF) would let a
    # crafted base_url break out of the system-prompt block; reject
    # outright.
    if any(ch < " " or ch == "\x7f" for ch in cleaned):
        logger.warning(
            "get_integration_public_url: rejecting base_url for '%s' (%s) — contains control characters",
            integ.name,
            provider,
        )
        return None

    try:
        parsed = urlparse(cleaned)
    except ValueError:
        return None

    if parsed.scheme not in ("http", "https"):
        logger.warning(
            "get_integration_public_url: rejecting base_url for '%s' (%s) — scheme %r is not http(s)",
            integ.name,
            provider,
            parsed.scheme,
        )
        return None
    if not parsed.netloc:
        return None
    # ``user:pass@host`` carries credentials — those belong in the
    # encrypted credential fields, never in the user-facing URL.
    if parsed.username or parsed.password:
        logger.warning(
            "get_integration_public_url: rejecting base_url for '%s' (%s) — contains userinfo",
            integ.name,
            provider,
        )
        return None

    # Drop query / fragment — the UI host should have neither, and
    # they would otherwise compose unpredictably with the per-entity
    # paths the agent appends downstream
    # (e.g. ``{base_url}/dashboard/id/{uuid}``).
    safe = urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", "")
    )
    return safe or None


async def _delegate_in_own_session(
    delegate: Callable[..., Awaitable[Any]], integration: Any, target: Any, **kwargs: Any
) -> Any:
    """Run one A2A delegation in a database session of its own, committed at its end.

    Not on the run's session: the tool calls of one model round run together
    and share it, so a delegation's reads and record writes would interleave
    with its siblings', and one database error that aborts the transaction
    (a statement timeout, a constraint) would fail every later send, collect
    and record of the run on ``InFailedSQLTransaction``. In a session of its
    own, a delegation commits its record when it ends, rolls back alone when
    its session breaks, and never poisons the run's session or a sibling's.

    ``delegate`` never raises by contract; a failure of the commit itself is
    logged and the outcome still returned, since the partner's answer is
    real whatever happened to the bookkeeping.
    """
    from app.core.database import async_session

    async with async_session() as own_db:
        result = await delegate(own_db, integration, target, **kwargs)
        try:
            await own_db.commit()
        except Exception:
            logger.warning(
                "a2a: could not commit the record of a delegation to integration %s",
                getattr(integration, "id", None),
                exc_info=True,
            )
            with contextlib.suppress(Exception):
                await own_db.rollback()
    return result


async def _build_a2a_delegation_tool(
    db,
    provider: str,
    integration_id: str,
    executor: CallableToolExecutor | None = None,
    context: dict[str, Any] | None = None,
    *,
    taken: set[str] | None = None,
):
    """The delegation tool for an OUTBOUND A2A binding, or ``None``.

    ``None`` means "not mine, carry on" for three distinct cases that must all
    behave identically: a non-A2A provider, an A2A integration facing INBOUND
    (which confers exposure, never a tool), and one with no settings row at
    all (which belongs to neither direction). Collapsing them here is right —
    the caller's question is only "did A2A handle this binding".

    Registers a HANDLER as well as returning a definition, and both halves
    are required: a definition without a registered handler is offered to the
    model and answers "Unknown tool" when called.

    ``taken`` is shared across bindings: two registrations with the same
    operator name and declared agent would otherwise get one name, and
    ``executor.register`` overwrites silently.
    """
    from app.a2a.providers import ALL_A2A_PROVIDERS

    if provider not in ALL_A2A_PROVIDERS:
        return None

    from app.a2a.client.tool import build_delegation_tool
    from app.a2a.providers import get_settings, outbound_targets
    from app.api.v1.platform import is_chat_enabled
    from app.models.a2a import DIRECTION_OUTBOUND, USABILITY_USABLE
    from app.models.integration import Integration

    try:
        integration_uuid = uuid.UUID(str(integration_id))
    except ValueError:
        return None

    settings_row = await get_settings(db, integration_uuid)
    if settings_row is None or settings_row.direction != DIRECTION_OUTBOUND:
        return None

    if not await is_chat_enabled(db):
        # A2A is offered exactly while agentic chat is. Asked again in the
        # pre-flight; here it keeps the tool out of the model's context
        # entirely, which beats a tool that always refuses.
        # After the direction check: that is one lookup, this is two, and an
        # inbound binding never needs the answer.
        return None

    integration = (
        await db.execute(select(Integration).where(Integration.id == integration_uuid))
    ).scalar_one_or_none()
    if integration is None:
        return None

    # Attribution is a precondition, not a detail. FR-042 requires every
    # delegation to record the agent and user that sent it, AT submission,
    # and the recording happens inside ``delegate``. A context with no agent
    # identity — the tool-test endpoints, flow nodes, the general assistant —
    # therefore cannot delegate at all, and gets NO tool rather than one that
    # refuses when called: an absent tool cannot be called, while a tool that
    # appears and refuses teaches a model to retry.
    ctx = context or {}
    calling = _a2a_calling_run(ctx)
    if calling is None or executor is None:
        # ``[]``, NOT ``None``. None means "not an A2A binding, carry on",
        # which would send an A2A integration down the ordinary provider path
        # looking for an API token it has none of. This binding IS ours; it
        # just yields no tool.
        return []

    # Tasks a partner reported ``input-required`` for, awaiting this run's
    # answer: task id -> (integration, target). Kept on the executor — built
    # per run — so teardown can cancel whatever is still open (FR-025). A
    # conversation's turn tracks none: the conversation outlives the turn.
    outstanding = getattr(executor, "a2a_outstanding", None)
    if outstanding is None:
        outstanding = {}
        executor.a2a_outstanding = outstanding

    # ONE TOOL PER AGENT the partner exposes. A card describes exactly one
    # agent, so a registration reaching three of them contributes three tools
    # — and the model choosing between them is the whole reason this is not a
    # single tool with a target argument: a tool per agent carries that agent's
    # declared skills and card provenance in its own description, which is
    # what the choice should be made on.
    definitions: list[ToolDefinition] = []
    # Settled after the loop below, and read from the closure at call time: the
    # cancel's name is only final once every delegation name is taken.
    _cancel_tool_name: str | None = None
    if taken is None:
        taken = set()
    for target in await outbound_targets(db, integration_uuid):
        # Only a target that has proved itself. An unusable one is left out
        # rather than offered-and-refusing, for the same reason the feature
        # switch keeps the tool out of the context entirely.
        if target.usability != USABILITY_USABLE:
            # Said, not silent (#105): the run's log names the partner and the
            # reason; the operator reads the same reason on the target. Not a
            # notice in the run's context, which would put an operator's
            # problem in front of every user's model turn.
            logger.info(
                "a2a: partner target %s of integration %s left out of the tools (%s: %s)",
                target.declared_name or target.card_url,
                integration_id,
                target.usability,
                target.unusable_reason or "not verified yet",
            )
            continue
        definition = build_delegation_tool(integration, target, taken=taken)
        taken.add(definition.name)
        partner_name = target.declared_name or integration.name

        # A factory, so each tool binds ITS registration: closing over the loop
        # variable directly would make all three delegate to whichever agent
        # came last. Not default arguments, which bind per iteration too but
        # are parameters — and the executor filters a tool call to the
        # handler's parameter names, so the model could set them.
        def _make_delegate(_target, _partner_name):
            async def _handler(
                message: str = "",
                continue_task_id: str | None = None,
            ):
                # Imported here, like the rest of this function's A2A imports, so
                # a deployment with the feature off never loads the client package.
                from app.a2a.client.delegation import delegate
                from app.a2a.client.tool import render_result
                from app.core.config import settings as app_settings

                # A collect names a task and carries nothing else, so ``message``
                # has to be optional — which leaves the empty call sayable. It is
                # a mistake rather than work: sent on, it reaches the partner as
                # a delegation with no instruction.
                if not (message or "").strip() and not continue_task_id:
                    return {
                        "status": "failed",
                        "reason": "nothing_to_send",
                        "detail": (
                            "Provide a message, or a continue_task_id to "
                            "collect a task that was not ready."
                        ),
                    }

                # A collect (a task id, nothing to say) is a question, not a
                # wait: bounded by the grace period like the floor's collect,
                # so a partner still working hands back in seconds instead of
                # holding the tool call for the whole delegation ceiling.
                collecting = bool(continue_task_id) and not (message or "").strip()
                result = await _delegate_in_own_session(
                    delegate,
                    integration,
                    _target,
                    message=message,
                    continue_task_id=continue_task_id,
                    calling=calling,
                    **(
                        {"ceiling_seconds": app_settings.a2a_client_grace_seconds}
                        if collecting
                        else {}
                    ),
                )
                # A task the partner left awaiting input is OUTSTANDING: if this
                # run ends without continuing it, teardown cancels it at the
                # partner (FR-025, ``cancel_outstanding_delegations``). A terminal
                # answer to a continuation retires the entry. Not in a
                # conversation: the agent relays the question to its user and
                # ends the turn to get the answer, so the next turn is the one
                # that continues the task.
                if result.partner_task_id:
                    if result.needs_input and calling.conversation_ref is None:
                        outstanding[result.partner_task_id] = (integration, _target)
                    else:
                        outstanding.pop(result.partner_task_id, None)
                # ``delegate`` never raises — every failure is an outcome the
                # calling model may reason about — so there is deliberately no
                # try/except here. One would only convert a named reason into a
                # generic error.
                return render_result(
                    result,
                    partner_name=_partner_name,
                    cancel_tool=_cancel_tool_name,
                    # A run with no conversation: it ends when its work does,
                    # and is not resumed after (#12079).
                    run_may_end=calling.run_ref is not None and calling.conversation_ref is None,
                )

            return _handler

        _delegate = _make_delegate(target, partner_name)

        executor.register(definition.name, _delegate)
        definitions.append(definition)

    # One cancel per REGISTRATION, offered only where there is something it
    # could reach. It takes a task id the hand-back gave the model, so it
    # needs no target — and the scope that decides what it may touch is the
    # calling run or conversation, not the id.
    if definitions:
        from app.a2a.client.tool import build_cancel_tool

        cancel_def = build_cancel_tool(integration, taken=taken)
        taken.add(cancel_def.name)
        _cancel_tool_name = cancel_def.name

        async def _cancel(task_id: str | None = None, collect_task_id: str | None = None):
            return await _cancel_partner_task(
                db,
                calling=calling,
                integration=integration,
                task_id=task_id,
                collect_task_id=collect_task_id,
            )

        executor.register(cancel_def.name, _cancel)
        definitions.append(cancel_def)

    return definitions


async def _cancel_partner_task(
    db,
    *,
    calling,
    integration,
    task_id: str | None = None,
    collect_task_id: str | None = None,
) -> dict[str, Any]:
    """Stop partner work this run or conversation left running.

    Both spellings, because the hand-back renders the id as
    ``collect_task_id`` and the tool asks for ``task_id``. Neither is required:
    the scope is the run or the conversation, so an absent id is the ordinary
    case — stop what this conversation left running.
    """
    from app.a2a.client import delegation as delegation_mod

    narrowed = task_id or collect_task_id
    stopped = await delegation_mod.cancel_outstanding_for(
        db,
        run_ref=calling.run_ref,
        conversation_ref=calling.conversation_ref,
        partner_task_id=narrowed,
        # This tool is one partner's.
        integration_id=integration.id,
    )
    if not stopped:
        # Already concluded, or never this caller's — one answer for both, or
        # one conversation could probe another's work by id.
        return {
            "status": "nothing_to_cancel",
            "detail": (
                "No task by that id is still running for this conversation. "
                "It may have finished already."
                if narrowed
                else "Nothing is still running for this conversation. Any "
                "work sent to the partner has already finished."
            ),
        }
    await db.commit()
    if not stopped.cancelled:
        # Every row ended, but nothing at the partner heard it. Saying "told to
        # stop" would have the user believe work that is still running, and
        # still billing, has halted.
        return {
            "status": "closed_locally",
            "detail": (
                "The partner could not be reached, so the work was closed on "
                "this side only. It may still be running there."
            ),
        }
    if stopped.concluded:
        return {
            "status": "cancelled",
            "detail": (
                f"{stopped.cancelled} told to stop; {stopped.concluded} could "
                "not be reached and were closed on this side only."
            ),
        }
    return {
        "status": "cancelled",
        "detail": "The partner has been told to stop.",
    }


async def cancel_outstanding_delegations(executor: object) -> None:
    """Cancel, at the partner, every task still awaiting this run's answer.

    FR-025: a run that ends — completed, failed or cancelled — with a delegated
    task at ``input-required`` leaves a partner working toward an answer nobody
    will give. Called from run teardown, best-effort. A chat turn's executor
    holds none of these: its user answers in a later turn.

    Reaches only what the executor holds, which is the tasks parked on a
    question. A task handed back as still running is scoped to the run or the
    conversation instead, and cancelled from the delegation rows.
    """
    outstanding = getattr(executor, "a2a_outstanding", None)
    if not outstanding:
        return
    from app.a2a.client.delegation import cancel

    for task_id, (integration, target) in list(outstanding.items()):
        outstanding.pop(task_id, None)
        try:
            await cancel(integration, target, task_id)
        except Exception:
            logger.warning(
                "a2a.client: run-teardown cancel failed for task %s",
                task_id,
                exc_info=True,
            )


async def cancel_run_partner_tasks(db, run_id) -> None:
    """Cancel partner tasks an assignment run leaves outstanding (FR-005).

    Called for a run that failed, was cancelled or was dead-lettered: a run
    that COMPLETED keeps them, their answers raised as
    ``a2a.delegation.completed`` (``assignment_engine._teardown_partner_work``).

    Read from the delegation rows rather than from the executor: a task handed
    back as still running outlives the turn that made it, and by teardown there
    is no executor left holding it.
    """
    from app.a2a.client.delegation import cancel_outstanding_for

    try:
        await cancel_outstanding_for(db, run_ref=run_id)
    except Exception:
        logger.warning(
            "a2a.client: could not cancel partner tasks for run %s", run_id,
            exc_info=True,
        )


def _a2a_calling_run(context: dict[str, Any]):
    """``CallingRun`` for this run, or ``None`` when it cannot be attributed.

    Both ids must be present AND parseable. Fabricating either — a nil UUID,
    the agent's creator, "system" — would put a delegation on record against
    something that did not send it, and the audit trail is the reason this
    feature can be operated at all.
    """
    from app.a2a.client.delegation import CallingRun

    agent_id, user_id = context.get("agent_id"), context.get("user_id")
    if not agent_id or not user_id:
        return None
    try:
        parsed = CallingRun(
            agent_id=uuid.UUID(str(agent_id)),
            user_id=uuid.UUID(str(user_id)),
            run_ref=uuid.UUID(str(context["run_id"])) if context.get("run_id") else None,
            conversation_ref=(
                uuid.UUID(str(context["conversation_id"]))
                if context.get("conversation_id")
                else None
            ),
            # The dispatcher's own record of the item this run works (#600),
            # set by the engine only for an inbox-dispatched run.
            work_item_ref=_optional_uuid(context.get("work_item_id")),
        )
    except ValueError:
        return None
    return parsed


def _optional_uuid(value: Any) -> uuid.UUID | None:
    """*value* as a UUID, ``None`` when absent or unparseable.

    For an id that only widens what a caller may do: an unreadable one leaves
    the caller with its narrower rights rather than withdrawing the tool.
    """
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


# Provider → tool builder mapping (integrations only — user-delegated OAuth tokens)
PROVIDER_TOOLS = {
    # Direct LLM-query integrations — query/compare/evaluate external LLMs as tools
    "anthropic": anthropic_tools,
    "openai": openai_tools,
    "gemini": gemini_tools,
    "mistral": mistral_tools,
    "microsoft": microsoft_tools,
    "google": google_tools,
    "github": github_tools,
    "slack": slack_tools,
    "notion": notion_tools,
    "onepassword": onepassword_tools,
    "planhat": planhat_tools,
    "metabase": metabase_tools,
    "odoo": odoo_tools,
    "databricks": databricks_tools,
    "supabase": supabase_tools,
    "perk": perk_tools,
    "openaev": openaev_tools,
    "opencti": opencti_tools,
    "opencrq": opencrq_tools,
    "newsapi": newsapi_tools,
    "feedly": feedly_tools,
    "rss": rss_tools,
    "platform_email": platform_email_tools,
    "google_search": google_search_tools,
    "brave_search": brave_search_tools,
    "giphy": giphy_tools,
    "grafana": grafana_tools,
    "figma": figma_tools,
    "filigran_ai": filigran_ai_tools,
    "hubspot": hubspot_tools,
    "drata": drata_tools,
    "pennylane": pennylane_tools,
    "credly": credly_tools,
    "concord": concord_tools,
    "dealhub": dealhub_tools,
    "wordsmith": wordsmith_tools,
    "docusign": docusign_tools,
    "netsuite": netsuite_tools,
    "jira": jira_tools,
    "ashby": ashby_tools,
    "quotapath": quotapath_tools,
    "hibob": hibob_tools,
    "linkedin": linkedin_tools,
    "x": x_tools,
    "facebook": facebook_tools,
    "bluesky": bluesky_tools,
    "n8n": n8n_tools,
    # Any REST API described by a Swagger 2.0 / OpenAPI 3.x document; the
    # tools come from the integration's own imported spec (see the openapi
    # branch of the build loop, which pre-loads it).
    "openapi": openapi_tools,
    "youtrack": youtrack_tools,
    "sentinel": sentinel_tools,
    "intune": intune_tools,
    "defender": defender_tools,
    "entra": entra_tools,
    "purview": purview_tools,
    "tanium": tanium_tools,
    "xsoar": xsoar_tools,
    "xdr": xdr_tools,
    "xtm_hub": xtm_hub_tools,
    "qradar": qradar_tools,
    "fortisiem": fortisiem_tools,
    "qualys": qualys_tools,
    "logrhythm": logrhythm_tools,
    "crowdsec": crowdsec_tools,
    "crowdstrike": crowdstrike_tools,
    "crowdstrike_ngsiem": crowdstrike_ngsiem_tools,
    "greynoise": greynoise_tools,
    "hackerone": hackerone_tools,
    "intel471": intel471_tools,
    "sentinelone": sentinelone_tools,
    "harfanglab": harfanglab_tools,
    "attackiq": attackiq_tools,
    "elastic_security": elastic_security_tools,
    "google_secops": google_secops_tools,
    "sekoia": sekoia_tools,
    "ses": ses_tools,
    "letsignit": letsignit_tools,
    "aws_security": aws_security_tools,
    "ovhcloud": ovhcloud_tools,
    "aikido": aikido_tools,
    "airwallex": airwallex_tools,
    "rydoo": rydoo_tools,
    "spendesk": spendesk_tools,
    "sublime": sublime_tools,
    "proofpoint": proofpoint_tools,
    "servicenow": servicenow_tools,
    "tenable": tenable_tools,
    "thehive": thehive_tools,
    "threatconnect": threatconnect_tools,
    "cyware": cyware_tools,
    "domaintools": domaintools_tools,
    "threatq": threatq_tools,
    "infoblox": infoblox_tools,
    "rapid7": rapid7_tools,
    "recorded_future": recorded_future_tools,
    "virustotal": virustotal_tools,
    "gti": gti_tools,
    "sumologic": sumologic_tools,
    "splunk": splunk_tools,
    "abuseipdb": abuseipdb_tools,
    "abusech": abusech_tools,
    "ransomware_live": ransomware_live_tools,
    "misp": misp_tools,
    "mitre_atlas": mitre_atlas_tools,
    "mitre_attack": mitre_attack_tools,
    "shodan": shodan_tools,
    "urlscan": urlscan_tools,
    "picus": picus_tools,
    "anomali": anomali_tools,
    "cybersixgill": cybersixgill_tools,
    "socradar": socradar_tools,
    "vuln_intel": vuln_intel_tools,
    "vulncheck": vulncheck_tools,
    "flashpoint": flashpoint_tools,
    "doppel": doppel_tools,
    "wiz": wiz_tools,
    "whatsapp": whatsapp_tools,
    "securityscorecard": securityscorecard_tools,
    "pagerduty": pagerduty_tools,
    "gitlab": gitlab_tools,
    "zendesk": zendesk_tools,
    "mattermost": mattermost_tools,
    "anyrun": anyrun_tools,
}

#: Provider → the hosts it serves media from (its module's ``MEDIA_HOSTS``,
#: a host or any of its subdomains). An external channel sends a Markdown
#: image at one of them as an image (``channels.base.trusted_media_hosts``);
#: a provider absent here never makes a reply fetch or forward an address.
PROVIDER_MEDIA_HOSTS: dict[str, tuple[str, ...]] = {
    "giphy": GIPHY_MEDIA_HOSTS,
}

# Channel bot provider → tool builder mapping (dedicated ChannelBot entity)
CHANNEL_BOT_TOOLS = {
    "teams_bot": teams_bot_tools,
    "slack_bot": slack_bot_tools,
    "whatsapp_bot": whatsapp_bot_tools,
    "telegram_bot": telegram_bot_tools,
    "discord_bot": discord_bot_tools,
    "googlechat_bot": googlechat_bot_tools,
}

# Providers whose download tools store the file they fetch as a real
# FileAttachment of the run (``persist_file_fn``): a download card, a
# file_id in the run's FileContext.
_PERSIST_FILE_PROVIDERS = frozenset(
    {
        "aikido",
        "airwallex",
        "spendesk",
        "anyrun",
        "ashby",
        "bluesky",
        "concord",
        "dealhub",
        "wordsmith",
        "crowdstrike",
        "crowdstrike_ngsiem",
        "cybersixgill",
        "databricks",
        "defender",
        "docusign",
        "doppel",
        "drata",
        "pennylane",
        "facebook",
        "figma",
        "flashpoint",
        "github",
        "google",
        "grafana",
        "gti",
        "hackerone",
        "hibob",
        "hubspot",
        "jira",
        "linkedin",
        "metabase",
        "microsoft",
        "netsuite",
        "notion",
        "odoo",
        "onepassword",
        "openaev",
        "opencti",
        "perk",
        "rydoo",
        "quotapath",
        "servicenow",
        "slack",
        "tanium",
        "tenable",
        "threatq",
        "whatsapp",
        "x",
        "xdr",
        "xtm_hub",
        "youtrack",
    }
)
_PERSIST_FILE_CHANNEL_BOTS = frozenset(
    {"teams_bot", "slack_bot", "discord_bot", "whatsapp_bot"}
)

#: The build context key of a list the caller gives to collect how each bound
#: platform names the objects it holds: a provider whose builder declares a
#: ``platform_names_sink`` parameter appends a lookup read on its tools' own
#: connection (relay route included). The grounded STIX extraction uses the one
#: lookup of a run with exactly one such platform
#: (``builtin_tools.stix_extraction``).
PLATFORM_NAMES_CONTEXT_KEY = "platform_names"
#: Context key of a caller's list of the run's bound ATT&CK sources, each
#: ``(source_url, version, relay route)`` as its own tools read it: a provider whose
#: builder declares an ``attack_source_sink`` parameter gives its source, and the
#: grounded STIX extraction reads ATT&CK from it, not from the deployment's most recent one.
ATTACK_SOURCES_CONTEXT_KEY = "attack_sources"


def _declares(builder: Callable[..., Any], parameter: str) -> bool:
    """Whether a provider's tool builder takes *parameter* by name: a capability the
    provider declares, so the generic build names no vendor (invariant 18)."""
    try:
        return parameter in inspect.signature(builder).parameters
    except (TypeError, ValueError):
        return False


def _routed_lookup(lookup: Callable[..., Awaitable[Any]], route: EgressRoute | None) -> Callable[..., Awaitable[Any]]:
    """*lookup* sending its requests through *route*'s relay group, as the tools do."""
    if route is None:
        return lookup

    async def _routed(*args: Any, **kwargs: Any) -> Any:
        with egress_route(route):
            return await lookup(*args, **kwargs)

    return _routed


def _custom_declared_tools_from_metadata(metadata_json: dict | None) -> list[dict[str, Any]]:
    if not isinstance(metadata_json, dict):
        return []
    raw_tools = metadata_json.get("tools")
    if not isinstance(raw_tools, list):
        raw_tools = metadata_json.get("available_tools")
    if not isinstance(raw_tools, list):
        return []

    out: list[dict[str, Any]] = []
    for row in raw_tools:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        description = row.get("description")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(description, str) or not description.strip():
            continue
        schema = row.get("input_schema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        out.append(
            {
                "name": name.strip(),
                "description": description.strip(),
                "input_schema": schema,
                "raw": row,
            }
        )
    return out


def custom_declared_tool_names(metadata_json: dict | None) -> list[str]:
    """The names of the tools a custom provider's definition declares."""
    return [tool["name"] for tool in _custom_declared_tools_from_metadata(metadata_json)]


def _coerce_str_arg(kwargs: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = kwargs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _youtube_api_key_is_valid(api_key: str | None) -> bool:
    if not api_key:
        return False
    key = api_key.strip()
    return key.startswith("AIza") and len(key) >= 20


def _youtube_transcript_text_from_xml(xml_text: str) -> str:
    if not xml_text.strip():
        return ""
    chunks = re.findall(r"<text[^>]*>(.*?)</text>", xml_text, flags=re.S)
    if not chunks:
        return ""
    # Lightweight unescape for common transcript entities.
    merged = " ".join(chunks)
    merged = (
        merged.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
    )
    return re.sub(r"\s+", " ", merged).strip()


def _extract_video_id_from_url(url: str | None) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    raw = url.strip()
    if "youtube.com/watch" in raw and "?" in raw:
        query = raw.split("?", 1)[1]
        video_id = parse_qs(query).get("v", [""])[0].strip()
        return video_id or None
    if "youtu.be/" in raw:
        tail = raw.split("youtu.be/", 1)[1].split("?", 1)[0].split("/", 1)[0]
        return tail.strip() or None
    return None


async def _youtube_v3_get(
    *,
    api_key: str,
    resource: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    if not _youtube_api_key_is_valid(api_key):
        raise ValueError(
            "YouTube API key is missing or invalid. Configure a valid API key in the integration."
        )
    url = f"https://www.googleapis.com/youtube/v3/{resource}"
    qp = {k: v for k, v in params.items() if v is not None}
    qp["key"] = api_key
    async with egress_client(timeout=20.0) as client:
        resp = await client.get(url, params=qp)
        if resp.status_code == 403:
            try:
                payload = resp.json()
            except Exception:
                payload = {}

            # Google API error payloads nest the details under a top-level
            # "error" object: {"error": {"message": ..., "errors": [...],
            # "details": [...]}}. Fall back to the flat shape defensively.
            error_obj = payload.get("error")
            if not isinstance(error_obj, dict):
                error_obj = payload

            message = str(error_obj.get("message") or "")
            errors = error_obj.get("errors") or []
            reasons = {
                str(err.get("reason") or "")
                for err in errors
                if isinstance(err, dict)
            }
            # Newer google.rpc.ErrorInfo format carries the reason in details[].
            for detail in error_obj.get("details") or []:
                if isinstance(detail, dict) and detail.get("reason"):
                    reasons.add(str(detail["reason"]))

            # Only trigger on referrer-specific signals: the generic
            # "forbidden" reason covers many unrelated 403s (quota exceeded,
            # API disabled) that must fall through to raise_for_status.
            if (
                "referer" in message.lower()
                or "API_KEY_HTTP_REFERRER_BLOCKED" in message
                or "API_KEY_HTTP_REFERRER_BLOCKED" in reasons
                or "ipRefererBlocked" in reasons
            ):
                raise ValueError(
                    "YouTube API key is restricted by HTTP referrer, which blocks backend/server calls. "
                    "Use a server key with no Application restriction or with IP address restriction for this backend host, "
                    "and keep API restriction limited to YouTube Data API v3."
                )
        resp.raise_for_status()
        return resp.json()


def _register_custom_youtube_tools(
    executor: CallableToolExecutor,
    *,
    provider: str,
    integration_id: str,
    api_key: str,
    declared: list[dict[str, Any]],
    integration_extra_config: dict[str, Any] | None,
) -> list[ToolDefinition]:
    defaults = integration_extra_config or {}
    default_channel_id = _coerce_str_arg(defaults, "channel_id", "channelId")
    default_region_code = _coerce_str_arg(defaults, "region_code", "regionCode")

    definitions: list[ToolDefinition] = []
    for tool in declared:
        tool_name = tool["name"]

        if tool_name == "search_youtube_videos":

            async def _search_youtube_videos(
                query: str,
                max_results: int = 10,
                region_code: str | None = None,
                order: str = "relevance",
            ) -> dict[str, Any]:
                payload = await _youtube_v3_get(
                    api_key=api_key,
                    resource="search",
                    params={
                        "part": "snippet",
                        "type": "video",
                        "q": query,
                        "maxResults": max(1, min(50, int(max_results))),
                        "regionCode": region_code or default_region_code,
                        "order": order,
                    },
                )
                items = payload.get("items") or []
                videos = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    snippet = item.get("snippet") or {}
                    id_obj = item.get("id") or {}
                    videos.append(
                        {
                            "video_id": id_obj.get("videoId"),
                            "title": snippet.get("title"),
                            "channel_title": snippet.get("channelTitle"),
                            "published_at": snippet.get("publishedAt"),
                            "description": snippet.get("description"),
                        }
                    )
                return {
                    "query": query,
                    "count": len(videos),
                    "videos": videos,
                    "next_page_token": payload.get("nextPageToken"),
                }

            executor.register(tool_name, _search_youtube_videos)

        elif tool_name == "get_channel_recent_videos":

            async def _get_channel_recent_videos(
                channel_id: str | None = None,
                max_results: int = 10,
                order: str = "date",
            ) -> dict[str, Any]:
                effective_channel_id = channel_id or default_channel_id
                if not effective_channel_id:
                    raise ValueError(
                        "channel_id is required (or configure a default channel_id in integration extra config)."
                    )
                payload = await _youtube_v3_get(
                    api_key=api_key,
                    resource="search",
                    params={
                        "part": "snippet",
                        "type": "video",
                        "channelId": effective_channel_id,
                        "maxResults": max(1, min(50, int(max_results))),
                        "order": order,
                    },
                )
                items = payload.get("items") or []
                videos = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    snippet = item.get("snippet") or {}
                    id_obj = item.get("id") or {}
                    videos.append(
                        {
                            "video_id": id_obj.get("videoId"),
                            "title": snippet.get("title"),
                            "published_at": snippet.get("publishedAt"),
                            "description": snippet.get("description"),
                        }
                    )
                return {
                    "channel_id": effective_channel_id,
                    "count": len(videos),
                    "videos": videos,
                    "next_page_token": payload.get("nextPageToken"),
                }

            executor.register(tool_name, _get_channel_recent_videos)

        elif tool_name == "list_video_comments":

            async def _list_video_comments(
                video_id: str | None = None,
                video_url: str | None = None,
                max_results: int = 20,
                order: str = "relevance",
                page_token: str | None = None,
            ) -> dict[str, Any]:
                effective_video_id = video_id or _extract_video_id_from_url(video_url)
                if not effective_video_id:
                    raise ValueError("video_id is required (or provide a valid video_url).")
                payload = await _youtube_v3_get(
                    api_key=api_key,
                    resource="commentThreads",
                    params={
                        "part": "snippet,replies",
                        "videoId": effective_video_id,
                        "textFormat": "plainText",
                        "order": order,
                        "maxResults": max(1, min(100, int(max_results))),
                        "pageToken": page_token,
                    },
                )
                comments = []
                for item in payload.get("items") or []:
                    if not isinstance(item, dict):
                        continue
                    snippet = (item.get("snippet") or {}).get("topLevelComment", {}).get(
                        "snippet", {}
                    )
                    top_id = (item.get("snippet") or {}).get("topLevelComment", {}).get(
                        "id"
                    )
                    comments.append(
                        {
                            "comment_id": top_id,
                            "author": snippet.get("authorDisplayName"),
                            "text": snippet.get("textDisplay") or snippet.get("textOriginal"),
                            "like_count": snippet.get("likeCount"),
                            "published_at": snippet.get("publishedAt"),
                            "reply_count": (item.get("snippet") or {}).get("totalReplyCount"),
                        }
                    )
                return {
                    "video_id": effective_video_id,
                    "count": len(comments),
                    "comments": comments,
                    "next_page_token": payload.get("nextPageToken"),
                }

            executor.register(tool_name, _list_video_comments)

        elif tool_name == "list_video_comment_replies":

            async def _list_video_comment_replies(
                parent_comment_id: str | None = None,
                comment_id: str | None = None,
                max_results: int = 50,
                page_token: str | None = None,
            ) -> dict[str, Any]:
                effective_parent = parent_comment_id or comment_id
                if not effective_parent:
                    raise ValueError("parent_comment_id (or comment_id) is required.")
                payload = await _youtube_v3_get(
                    api_key=api_key,
                    resource="comments",
                    params={
                        "part": "snippet",
                        "parentId": effective_parent,
                        "textFormat": "plainText",
                        "maxResults": max(1, min(100, int(max_results))),
                        "pageToken": page_token,
                    },
                )
                replies = []
                for item in payload.get("items") or []:
                    if not isinstance(item, dict):
                        continue
                    snippet = item.get("snippet") or {}
                    replies.append(
                        {
                            "comment_id": item.get("id"),
                            "author": snippet.get("authorDisplayName"),
                            "text": snippet.get("textDisplay") or snippet.get("textOriginal"),
                            "like_count": snippet.get("likeCount"),
                            "published_at": snippet.get("publishedAt"),
                        }
                    )
                return {
                    "parent_comment_id": effective_parent,
                    "count": len(replies),
                    "replies": replies,
                    "next_page_token": payload.get("nextPageToken"),
                }

            executor.register(tool_name, _list_video_comment_replies)

        elif tool_name == "get_video_transcript_public":

            async def _get_video_transcript_public(
                video_id: str | None = None,
                video_url: str | None = None,
                language: str = "en",
            ) -> dict[str, Any]:
                effective_video_id = video_id or _extract_video_id_from_url(video_url)
                if not effective_video_id:
                    raise ValueError("video_id is required (or provide a valid video_url).")
                async with egress_client(timeout=20.0) as client:
                    resp = await client.get(
                        "https://www.youtube.com/api/timedtext",
                        params={
                            "v": effective_video_id,
                            "lang": language,
                        },
                    )
                    resp.raise_for_status()
                    raw_xml = resp.text
                transcript_text = _youtube_transcript_text_from_xml(raw_xml)
                return {
                    "video_id": effective_video_id,
                    "language": language,
                    "has_transcript": bool(transcript_text),
                    "transcript_text": transcript_text,
                    "note": (
                        "Public timedtext endpoint is unofficial and may be unavailable for some videos/languages."
                    ),
                }

            executor.register(tool_name, _get_video_transcript_public)

        else:

            async def _metadata_only_handler(
                _tool_name: str = tool_name,
                **_kwargs: Any,
            ) -> ToolResult:
                return ToolResult(
                    tool_call_id="",
                    content=(
                        f"Tool '{_tool_name}' is declared in the imported bundle for "
                        f"provider '{provider}' but has no runtime implementation yet."
                    ),
                    is_error=True,
                )

            executor.register(tool_name, _metadata_only_handler)

        definitions.append(
            ToolDefinition(
                name=tool_name,
                description=tool["description"],
                input_schema=tool["input_schema"],
                source=f"integration:{provider}",
                entity_id=integration_id,
            )
        )
    return definitions


def _make_declarative_handler(
    *,
    spec: dict[str, Any],
    tool_name: str,
    token: str,
    extra_config: dict[str, Any] | None,
    provider_label: str,
):
    """Build a declarative HTTP handler with the spec/credentials closed over.

    Thin wrapper over the executor's single factory
    (:func:`~app.services.declarative_http_executor.make_declarative_handler`),
    which captures the values in a closure — NOT as default keyword
    parameters — so LLM-supplied tool arguments can never override the
    execution spec, the decrypted credential, or the configured
    ``extra_config``.

    That ``extra_config`` (its base URL) is user-supplied - anyone who sees
    the provider may configure an integration of it - so every request is
    held to the base URL policy (``app.integrations.base_url_policy``). The
    spec itself comes from the bundle an admin imported: without
    a configured base URL, its per-operation server is the integration's own.
    """
    from app.services.declarative_http_executor import make_declarative_handler

    return make_declarative_handler(
        spec=spec,
        tool_name=tool_name,
        token=token,
        extra_config=extra_config,
        provider_label=provider_label,
        enforce_destination_guard=True,
        spec_servers_own=True,
    )


def _make_invalid_spec_handler(tool_name: str, errors: list[str]):
    """Handler for bundle tools whose persisted execution spec fails validation.

    See :func:`~app.services.declarative_http_executor.make_invalid_spec_handler`
    (shared with OpenAPI integrations).
    """
    from app.services.declarative_http_executor import make_invalid_spec_handler

    return make_invalid_spec_handler(tool_name, errors)


def _register_custom_declared_tools(
    executor: CallableToolExecutor,
    *,
    provider: str,
    integration_id: str,
    metadata_json: dict | None,
    integration_token: str,
    integration_extra_config: dict[str, Any] | None,
) -> list[ToolDefinition]:
    declared = _custom_declared_tools_from_metadata(metadata_json)
    if provider == "custom:youtube-integration":
        return _register_custom_youtube_tools(
            executor,
            provider=provider,
            integration_id=integration_id,
            api_key=integration_token,
            declared=declared,
            integration_extra_config=integration_extra_config,
        )

    from app.services.declarative_http_executor import spec_is_read

    definitions: list[ToolDefinition] = []
    for tool_index, tool in enumerate(declared):
        tool_name = tool["name"]
        execution_spec = tool["raw"].get("execution")
        spec_errors: list[str] = []

        if execution_spec is not None:
            # Declarative HTTP handler — the bundle provides an execution spec
            # so we can make real HTTP calls at runtime. Re-validate at
            # registration time (defense-in-depth): rows imported before
            # parse-time validation existed may carry malformed specs,
            # including non-dict values (validate_execution_spec rejects
            # those too, so they surface a clear configuration error instead
            # of the misleading metadata-only stub).
            from app.services.declarative_http_executor import (
                validate_execution_spec,
            )

            spec_errors = validate_execution_spec(
                execution_spec, tool_index=tool_index
            )
            if spec_errors:
                logger.warning(
                    "Custom tool '%s' (provider '%s') has an invalid execution "
                    "spec; registering error handler: %s",
                    tool_name,
                    provider,
                    "; ".join(spec_errors),
                )
                executor.register(
                    tool_name, _make_invalid_spec_handler(tool_name, spec_errors)
                )
            else:
                executor.register(
                    tool_name,
                    _make_declarative_handler(
                        spec=execution_spec,
                        tool_name=tool_name,
                        token=integration_token,
                        extra_config=integration_extra_config,
                        provider_label=provider.removeprefix("custom:"),
                    ),
                )
        else:
            # Metadata-only stub — no execution spec declared.
            async def _metadata_only_handler(
                _tool_name: str = tool_name,
                **_kwargs: Any,
            ) -> ToolResult:
                return ToolResult(
                    tool_call_id="",
                    content=(
                        f"Tool '{_tool_name}' is declared in the imported bundle for "
                        f"provider '{provider}' but has no runtime implementation yet."
                    ),
                    is_error=True,
                )

            executor.register(tool_name, _metadata_only_handler)
        definitions.append(
            ToolDefinition(
                name=tool_name,
                description=tool["description"],
                input_schema=tool["input_schema"],
                source=f"integration:{provider}",
                entity_id=integration_id,
                # A valid read (GET) spec offers to_workspace_path.
                workspace_ingest=execution_spec is not None and not spec_errors and spec_is_read(execution_spec),
            )
        )
    return definitions


class _SafeIntegrationExecutor(CallableToolExecutor):
    """CallableToolExecutor with httpx-aware error handling for integration tools.

    Integration tool functions *should* catch HTTP/network errors internally
    and return user-readable error strings.  However, some providers miss
    ``except httpx.HTTPError`` blocks (especially for network-level failures
    like connection timeouts), and others use ``resp.raise_for_status()``
    which raises ``httpx.HTTPStatusError`` with a terse message that omits
    the response body.

    This subclass overrides ``execute_tool`` to catch httpx exceptions
    *before* the generic ``Exception`` fallback, producing nicely formatted
    error messages via ``format_http_error`` / ``format_network_error``
    for anything that slips through.
    """

    async def execute_tool(self, call: ToolCall) -> ToolResult:
        handler = self._handlers.get(call.name)
        if handler is None:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Unknown tool: {call.name}",
                is_error=True,
            )
        try:
            # Defensive parsing: malformed payloads can carry non-dict
            # arguments (or None). Treat those as empty kwargs.
            raw_args = call.arguments if isinstance(call.arguments, dict) else {}
            kwargs = dict(raw_args)

            # Filter hallucinated/unsupported kwargs when the handler does
            # not accept **kwargs (mirrors base CallableToolExecutor logic).
            accepted = self._accepted_params.get(call.name)
            if accepted is not None:
                hallucinated = {k for k in kwargs if k not in accepted}
                if hallucinated:
                    logger.warning(
                        "Integration tool '%s' received unexpected kwargs %s; "
                        "filtering them out before dispatch.",
                        call.name,
                        sorted(hallucinated),
                    )
                kwargs = {k: v for k, v in kwargs.items() if k in accepted}

            # Inject file_context if the handler accepts it (same as parent)
            if self.file_context is not None and self._accepts_file_context.get(
                call.name, False
            ):
                kwargs["file_context"] = self.file_context
            raw = await handler(**kwargs)
            if isinstance(raw, ToolResult):
                raw.tool_call_id = call.id
                return raw
            content = raw if isinstance(raw, str) else _json.dumps(raw, default=str)
            # Same contract as the parent: a handler that caught an
            # upstream failure and RETURNED it as a string is still a failure
            # on the trace, the tool_done event and the loop's error counts.
            # This override never carried it, so no native provider had it.
            returned_error = is_error_shaped_result(content)
            return ToolResult(
                tool_call_id=call.id,
                content=content,
                is_error=returned_error,
                error_returned=returned_error,
            )
        except HumanInputRequestedError:
            raise
        except TypeError as exc:
            # Safety net: if kwargs filtering was bypassed (e.g. introspection
            # failed and _accepted_params is None) the LLM may still pass an
            # unexpected argument.  Return a structured, actionable error so
            # the agent can self-correct instead of seeing a raw traceback.
            msg = str(exc)
            logger.warning(
                "Integration tool '%s' called with unsupported arguments: %s",
                call.name,
                msg,
            )
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"Unsupported argument for tool '{call.name}': {msg}. "
                    "Please retry using only the parameters declared in the "
                    "tool schema."
                ),
                is_error=True,
            )
        except httpx.HTTPStatusError as exc:
            # Provider used raise_for_status() or didn't catch the status error.
            # Extract meaningful detail from the response body.
            logger.warning(
                "Integration tool '%s' HTTP %d error: %s",
                call.name,
                exc.response.status_code,
                format_exception_message(exc),
            )
            content = format_http_error(exc.response, call.name, "execute")
            return ToolResult(tool_call_id=call.id, content=content, is_error=True)
        except httpx.HTTPError as exc:
            # Network-level failures (ConnectError, TimeoutException, etc.)
            logger.warning(
                "Integration tool '%s' network error: %s",
                call.name,
                format_exception_message(exc),
            )
            content = format_network_error(exc, call.name, "execute")
            return ToolResult(tool_call_id=call.id, content=content, is_error=True)
        except Exception as exc:
            logger.exception("Integration tool '%s' execution failed", call.name)
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error executing tool '{call.name}': {exc}",
                is_error=True,
            )


# How many minutes before expiry we proactively refresh
_REFRESH_BUFFER_MINUTES = 5

# Back-off after a transient chat-time refresh failure (5xx, 429, network):
# every tool call that meets a 401/403 asks for a refresh, so without it one
# turn re-hits a failing token endpoint once per call, 30 s timeout each.
_INLINE_TRANSIENT_FAIL_COOLDOWN = 60  # seconds


async def _try_inline_refresh(db: AsyncSession, integ: Integration) -> str | None:
    """Attempt to refresh an OAuth token inline and return the new access token.

    Returns the new decrypted access token on success, None on failure.

    Uses the same Redis lock as the scheduler-based refresh in
    ``token_refresh.py`` to prevent concurrent refreshes from
    invalidating rotating refresh tokens (Atlassian Jira). The token request
    follows the integration's route: through its relay group when it has one.
    """
    from app.services.relays import integration_egress_route

    route = await integration_egress_route(db, integration_of(integ))
    with egress_route(route):
        return await _refresh_inline(db, integ)


async def _refresh_inline(db: AsyncSession, integ: Integration) -> str | None:
    from app.services.token_refresh import (
        acquire_refresh_lock,
        release_refresh_lock,
        stored_tokens,
    )

    if not integ.refresh_token_enc or not integ.client_id_enc or not integ.token_url:
        logger.warning(
            "Cannot inline-refresh %s (%s): missing refresh_token, client_id, or token_url",
            integ.name,
            integ.provider,
        )
        return None

    # A dead grant or a back-off the scheduler set applies here too: a chat
    # turn must not re-hit a token endpoint that already refused this
    # integration. The stored token is surfaced instead.
    if integ.status == "reauthorize":
        logger.debug(
            "Skipping inline refresh for %s (%s): re-authorization required",
            integ.name,
            integ.provider,
        )
        return None

    # Hand the pooled connection back before any waiting / network I/O:
    # this runs during tool building on the caller's (read-only so far)
    # session, and the 2s lock-wait sleep or the 30s-timeout token-endpoint
    # POST below would otherwise pin that connection for its whole duration.
    # SQLAlchemy re-acquires on the next session use
    # (``db.refresh`` below); the refresh itself runs on a session of its own.
    from app.core.database import release_db_connection_before_loop

    await release_db_connection_before_loop(db)

    if await _inline_refresh_on_cooldown(integ):
        logger.debug(
            "Skipping inline refresh for %s (%s): refresh is cooling down after a failure",
            integ.name,
            integ.provider,
        )
        return None

    # The tokens this turn read, compared under the lock with the stored ones.
    seen_tokens = stored_tokens(integ)

    # Acquire per-integration lock to prevent concurrent refreshes
    # (critical for providers with rotating refresh tokens like Jira).
    # The holder's own lock (an integration, or a person's credential on one).
    lock_id = holder_ref(integ)
    lock = await acquire_refresh_lock(lock_id)
    if lock is None:
        logger.debug(
            "Skipping inline refresh for %s (%s): another refresh in progress",
            integ.name,
            integ.provider,
        )
        # Another refresh is in progress — wait briefly and re-read from DB
        import asyncio

        await asyncio.sleep(2)
        await db.refresh(integ)
        if integ.access_token_enc:
            return decrypt_value(integ.access_token_enc)
        return None

    try:
        return await _refresh_inline_locked(integ, seen_tokens)
    finally:
        await release_refresh_lock(lock_id, lock)


async def _refresh_inline_locked(integ: Any, seen_tokens: tuple[str | None, str | None]) -> str | None:
    """Refresh while holding the lock, from the row as stored now.

    The turn read ``integ`` earlier; another turn, a worker's sweep or a
    re-authorization may have stored newer tokens since. Their access token is
    used as is: presenting the rotating refresh token they spent would be
    answered like a dead grant. The refresh runs on a session of its own, so it
    sends the stored credentials, token URL and scopes and never writes the
    turn's session.
    """
    from app.core.database import async_session, release_db_connection_before_loop
    from app.services.token_refresh import lock_stored_tokens, stored_tokens

    async with async_session() as db:
        try:
            stored = await load_token_holder(db, holder_ref(integ))
        except Exception as exc:
            # The new tokens could not be saved either: a rotating refresh token
            # spent without persisting its successor is a dead grant.
            logger.warning(
                "Skipping inline refresh for %s (%s): could not read the stored token: %s",
                integ.name,
                integ.provider,
                format_exception_message(exc),
            )
            return None
        if stored is None or stored.status == "reauthorize":
            return None
        if stored_tokens(stored) != seen_tokens:
            logger.debug(
                "Skipping inline refresh for %s (%s): another process stored a new token",
                integ.name,
                integ.provider,
            )
            return decrypt_value(stored.access_token_enc) if stored.access_token_enc else None
        service_exchange = await oauth_exchange_spec(db, stored.provider)
        # No pooled connection is held across the token request.
        await release_db_connection_before_loop(db)

        async def _tokens_unchanged() -> bool:
            return await lock_stored_tokens(db, stored) == seen_tokens

        return await _do_inline_refresh(
            db, stored, _tokens_unchanged, service_exchange=service_exchange
        )


async def _do_inline_refresh(
    db: AsyncSession,
    integ: Integration,
    tokens_unchanged: Callable[[], Awaitable[bool]],
    *,
    service_exchange: dict | None = None,
) -> str | None:
    """Execute the actual inline token refresh.

    ``integ`` is the row ``db`` read under the refresh lock. ``tokens_unchanged``
    is awaited right before an outcome is applied: it row-locks the stored
    integration in the transaction that commits the outcome and says whether it
    still stores the tokens this refresh started from. ``service_exchange``:
    the bundle's ``oauth_exchange`` block, whose service refreshes its own
    tokens (``integrations/service_token_exchange.py``).
    """
    from app.integrations.oauth import (
        apply_oauth_token_expiry,
        build_token_request,
        provider_token_url,
        substitute_tenant,
    )
    from app.integrations.token_endpoint import assert_token_endpoint_allowed
    from app.services.token_refresh import (
        _AUTH_FAIL_COOLDOWN,
        _JSON_REFRESH_PROVIDERS,
        MalformedTokenAnswerError,
        permanent_refresh_failure_reason,
        read_token_answer,
        rotated_client_secret,
        service_refresh_failure_reason,
    )

    # Read up front: a failed commit expires the row's attributes. The back-off
    # is the holder's own; the OAuth app (and its vault record) the integration's.
    integration_id, name, provider = holder_ref(integ), integ.name, integ.provider
    app_row = integration_of(integ)
    presented_refresh_token = integ.refresh_token_enc
    try:
        client_id = decrypt_value(integ.client_id_enc)  # type: ignore[arg-type]  # guarded by caller
        # A service that refreshes its own tokens is never sent the client
        # secret: its vault is not read, so a vault outage does not stop it.
        client_secret = (
            ""
            if service_exchange is not None
            else (await reveal_field(app_row, "client_secret_enc")) or ""
        )
        refresh_token = decrypt_value(integ.refresh_token_enc)  # type: ignore[arg-type]  # guarded by caller

        params: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }

        is_json_provider = integ.provider in _JSON_REFRESH_PROVIDERS

        # Only include scope for providers that expect it in refresh requests.
        # Atlassian (Jira) does NOT accept a scope parameter during refresh.
        if integ.scopes and not is_json_provider:
            params["scope"] = integ.scopes

        service_request: dict | None = None
        if service_exchange is not None:
            # ValueError (no base_url) is answered with None by the handler.
            token_url, service_request = refresh_request(service_exchange, await service_base_url(integ), refresh_token)
        else:
            # Handle Microsoft tenant-specific token URL; DocuSign's environment host.
            token_url = await provider_token_url(app_row, integ.token_url)
        if not token_url:
            # _try_inline_refresh guards this already — keep the invariant
            # explicit so a missing token_url never turns into a confusing
            # empty-URL httpx error.
            logger.warning(
                "Cannot inline-refresh %s (%s): token_url is not set",
                integ.name,
                integ.provider,
            )
            return None
        if service_request is None and "/common/" in token_url and integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
                tenant_id = extra.get("tenant_id")
                if tenant_id:
                    token_url = substitute_tenant(token_url, tenant_id)
            except (ValueError, KeyError) as exc:
                logger.warning(
                    "Failed to apply tenant_id to token URL for %s (%s): %s",
                    integ.name,
                    integ.provider,
                    format_exception_message(exc),
                )

        # Applies the provider-specific auth style: Notion needs HTTP Basic auth
        # + JSON body (rejects credentials-in-body with 401 invalid_client),
        # Atlassian needs a JSON body, everyone else uses a form-encoded body.
        request_kwargs = service_request or build_token_request(
            integ.provider,
            client_id,
            client_secret,
            params,
            json_provider=is_json_provider,
        )

        # Refused -> TokenEndpointRefusedError, logged and answered with None
        # by the handler below, like any other failed refresh.
        await assert_token_endpoint_allowed(token_url)
        client_options: dict[str, Any] = {}
        if integ.provider == OPENAPI_PROVIDER:
            from app.integrations.providers.openapi import token_client_options

            client_options = token_client_options(app_row)
        async with egress_client(**client_options) as client:
            resp = await client.post(token_url, timeout=30, **request_kwargs)
            # A rotated client secret only matters to the standard token request.
            if service_request is None and (
                retry_secret := await rotated_client_secret(app_row, client_secret, resp)
            ):
                retry_kwargs = build_token_request(
                    integ.provider, client_id, retry_secret, params, json_provider=is_json_provider
                )
                resp = await client.post(token_url, timeout=30, **retry_kwargs)
            resp.raise_for_status()
            tokens = (
                read_service_token_answer(service_exchange, resp)
                if service_exchange is not None
                else read_token_answer(resp)
            )

        new_access = tokens["access_token"]

        if not await tokens_unchanged():
            logger.info(
                "Ignoring the refresh answer for %s (%s): its tokens were replaced while the refresh was in flight",
                name,
                provider,
            )
            return decrypt_value(integ.access_token_enc) if integ.access_token_enc else None

        # Persist new tokens
        integ.access_token_enc = encrypt_value(new_access)
        if tokens.get("refresh_token"):
            integ.refresh_token_enc = encrypt_value(tokens["refresh_token"])
        apply_oauth_token_expiry(integ, tokens)
        from app.integrations.granted_scopes import record_granted_scopes

        record_granted_scopes(integ, tokens, keep_when_absent=True)
        # ``reauthorize`` too: a refresh that outlived its lock may have recorded
        # a refusal of these same tokens meanwhile, and they just worked.
        if integ.status in ("error", "reauthorize"):
            integ.status = "connected"
        await db.commit()

        logger.info("Inline-refreshed OAuth token for %s (%s)", name, provider)
        return new_access

    except httpx.HTTPStatusError as e:
        # Full body for the permanent-failure check (a marker could sit past
        # the truncation point); truncated for logging.
        full_body = ""
        with contextlib.suppress(Exception):
            full_body = e.response.text
        logger.warning(
            "Inline token refresh failed for %s (%s): %s | body: %s",
            name,
            provider,
            format_exception_message(e),
            full_body[:TRUNCATE_SNIPPET],
        )
        status_code = e.response.status_code
        return await _record_inline_refresh_failure(
            db,
            integ,
            tokens_unchanged,
            presented_refresh_token,
            service_refresh_failure_reason(status_code, full_body)
            if service_exchange is not None
            else permanent_refresh_failure_reason(status_code, full_body),
            auth_failure=status_code in (401, 403),
        )
    except Exception as e:
        logger.warning("Inline token refresh failed for %s (%s): %s", name, provider, format_exception_message(e))
        if isinstance(e, MalformedTokenAnswerError) and e.reason:
            # A refusal inside a 2xx that no retry can overturn (GitHub, Slack):
            # the same verdict, under the same check, as a permanent error answer.
            return await _record_inline_refresh_failure(
                db, integ, tokens_unchanged, presented_refresh_token, e.reason, auth_failure=True
            )
        if isinstance(e, MalformedTokenAnswerError) and e.refused:
            await _start_inline_back_off(integration_id, presented_refresh_token, _AUTH_FAIL_COOLDOWN)
        else:
            await _start_inline_back_off(
                integration_id, presented_refresh_token, _INLINE_TRANSIENT_FAIL_COOLDOWN, only_if_absent=True
            )
        return None


async def _inline_refresh_on_cooldown(integ: Any) -> bool:
    """Whether the refresh back-off shared with the scheduler holds this token back.

    Fails open: a Redis outage must not stop a chat turn from refreshing.
    """
    from app.services.token_refresh import refresh_cooldown_key, refresh_cooling_down

    try:
        return await refresh_cooling_down(refresh_cooldown_key(holder_ref(integ)), integ.refresh_token_enc)
    except Exception as exc:
        logger.debug(
            "Could not read the refresh cooldown for %s (%s): %s",
            integ.name,
            integ.provider,
            format_exception_message(exc),
        )
        return False


async def _record_inline_refresh_failure(
    db: AsyncSession,
    integ: Any,
    tokens_unchanged: Callable[[], Awaitable[bool]],
    presented_refresh_token: str | None,
    reason: str | None,
    *,
    auth_failure: bool,
) -> str | None:
    """Apply the scheduler's verdict to a failed chat-time refresh.

    ``reason`` is ``token_refresh.permanent_refresh_failure_reason``'s verdict
    on the answer. A permanent failure (dead grant, rejected client
    credentials) marks the integration ``reauthorize`` exactly as
    ``token_refresh`` does, which also takes it out of the refresh sweep. An
    auth failure otherwise starts the same back-off the scheduler uses, so the
    next chat turns do not retry it. Transient failures leave the status alone
    (``error`` would drop the integration from template bindings over a
    network blip) and only start a short back-off.

    A refusal is only a verdict while the stored tokens are still the ones the
    refresh started from, checked under a row lock in the transaction that
    records it: when a re-authorization or another refresh replaced them while
    this one was in flight, the answer was about a spent token, and the stored
    access token is returned instead.
    """
    from app.services.token_refresh import (
        _AUTH_FAIL_COOLDOWN,
        _SWEEP_STATUSES,
        _mark_needs_reauthorization,
    )

    # Read up front: a failed commit expires the row's attributes.
    integration_id, name, provider = holder_ref(integ), integ.name, integ.provider
    try:
        unchanged: bool | None = await tokens_unchanged()
    except Exception as exc:
        logger.warning(
            "Could not re-read %s (%s) after a refused refresh: %s",
            name,
            provider,
            format_exception_message(exc),
        )
        unchanged = None
    if unchanged is False:
        logger.info(
            "Ignoring the refresh answer for %s (%s): its tokens were replaced while the refresh was in flight",
            name,
            provider,
        )
        return decrypt_value(integ.access_token_enc) if integ.access_token_enc else None

    if reason and unchanged and integ.status in _SWEEP_STATUSES:
        _mark_needs_reauthorization(integ, reason)
        try:
            await db.commit()
            return None
        except Exception as exc:
            logger.warning(
                "Could not persist re-authorization status for %s (%s): %s",
                name,
                provider,
                format_exception_message(exc),
            )
    if reason or auth_failure:
        # Also covers a verdict that could not be confirmed or saved: the
        # scheduler, which re-reads the row under the lock, records it later.
        await _start_inline_back_off(integration_id, presented_refresh_token, _AUTH_FAIL_COOLDOWN)
    else:
        await _start_inline_back_off(
            integration_id, presented_refresh_token, _INLINE_TRANSIENT_FAIL_COOLDOWN, only_if_absent=True
        )
    return None


async def _start_inline_back_off(
    integration_id: object, refresh_token_enc: str | None, seconds: int, *, only_if_absent: bool = False
) -> None:
    """Start the refresh back-off shared with the scheduler (best effort)."""
    from app.services.token_refresh import refresh_cooldown_key, start_refresh_cooldown

    try:
        await start_refresh_cooldown(
            refresh_cooldown_key(integration_id), refresh_token_enc, seconds, only_if_absent=only_if_absent
        )
    except Exception as exc:
        logger.warning(
            "Could not set the refresh cooldown for integration %s: %s",
            integration_id,
            format_exception_message(exc),
        )


async def _generate_platform_jwt(
    db: AsyncSession, user_id: str, audience_url: str,
) -> str | None:
    """Generate a short-lived cross-platform JWT for a platform-managed integration.

    Delegates to :func:`issue_xtm_jwt` which signs with Ed25519 (EdDSA) using
    the keypair derived from ``SECRET_KEY``.  The target platform validates the
    JWT by fetching this platform's JWKS at ``/xtm/auth/jwks``.

    Parameters
    ----------
    audience_url:
        The target platform's identity (becomes the ``aud`` claim), as
        :func:`app.core.platform_identity.platform_token_audience` resolves it.
    """
    from app.core.xtm_auth import issue_xtm_jwt
    from app.models.user import User

    result = await db.execute(
        select(User.email).where(User.id == uuid.UUID(user_id))
    )
    row = result.one_or_none()
    if not row:
        logger.warning("Cannot generate platform JWT: user %s not found", user_id)
        return None

    (email,) = row
    return issue_xtm_jwt(
        user_id=uuid.UUID(user_id),
        user_email=email,
        audience_url=audience_url,
    )


async def _report_invalid_config(
    integ: Integration | None,
    *,
    binding_key: str,
    missing_bindings: Any,
    context: dict[str, Any],
) -> None:
    """A bound integration that yielded no credential because its stored
    extra_config lacks a key its provider needs (#9898) is never a silent
    absence: the turn's ``missing_integration_bindings`` gets an
    ``integration_invalid`` entry naming the keys (the turn tells the model), and
    the owners are notified once per invalid state. Any other reason for a
    missing credential (an expired token, a revoked secret) is left as it was.
    Never raises.
    """
    if integ is None or not getattr(integ, "extra_config_enc", None):
        return
    try:
        from app.api.v1.integration_catalog import missing_required_extra_fields
        from app.services.integration_credentials import invalid_config_binding
        from app.services.integration_invalid_notice import notify_integration_invalid_once

        missing = missing_required_extra_fields(integ.provider, await _reveal_extra(integ))
        if not missing:
            return
        logger.warning(
            "Integration '%s' (%s) is invalid: %s missing in extra_config - its tools are unavailable",
            integ.name,
            integ.id,
            ", ".join(missing),
        )
        if isinstance(missing_bindings, list):
            missing_bindings.append(invalid_config_binding(integ, missing, binding_key=binding_key))
        await notify_integration_invalid_once(
            integ,
            missing,
            agent_id=context.get("agent_id"),
            agent_name=context.get("agent"),
        )
    except Exception as exc:
        logger.warning(
            "Could not check the configuration of integration %s: %s",
            getattr(integ, "id", None),
            format_exception_message(exc),
        )


async def _get_token(
    db: AsyncSession,
    integration_id: str,
    provider: str = "",
    *,
    user_id: str | None = None,
    source_label: str = "",
    binding_origin: str = "",
    integ: Integration | None = None,
    prefetched_token: str | None = None,
    refresh_attempted: bool = False,
) -> str | None:
    """Get a *fresh* decrypted access token for an integration.

    If the token is expired or about to expire (within 5 min), performs an
    inline refresh before returning.  This guarantees that assignments always
    start with a valid token.

    For Metabase, returns ``base_url||api_key`` so the tool builder can parse both.

    Returns ``None`` (and skips tool loading) when the binding's provider key
    does not match the integration's actual provider — this catches stale or
    mismatched ``integration_bindings`` entries (now ``dict[str, list[str]]``).

    ``integ`` lets the caller pass an already-loaded Integration row so the
    per-binding SELECT is skipped (P3.2 — the tool-build
    loop batch-loads every bound row up-front instead of re-querying one by
    one).  ``prefetched_token`` / ``refresh_attempted`` carry the outcome of
    the caller's PARALLEL proactive OAuth refresh pass (P3.1): a prefetched
    token is used directly, and an already-attempted (failed) refresh is not
    retried sequentially — the possibly-expired stored token is surfaced,
    exactly like the legacy failed-inline-refresh path.
    """
    if integ is None:
        result = await db.execute(
            select(Integration).where(Integration.id == uuid.UUID(integration_id))
        )
        integ = result.scalar_one_or_none()
    if not integ:
        logger.warning(
            "Integration %s not found — skipping tools for binding key '%s'",
            integration_id,
            provider,
        )
        return None

    # Enrich logging context with the resolved integration name so that
    # every downstream warning (including inside provider build_tools())
    # can be traced back to a specific integration.
    binding_owner_user_id = _binding_owner_user_id(integ)
    github_binding_meta = _extract_github_binding_metadata(integ)
    structlog.contextvars.bind_contextvars(
        integration_name=integ.name,
        integration_owner_user_id=binding_owner_user_id,
        integration_auth_type=integ.auth_type,
        integration_is_company_managed=integ.is_company_managed,
        **github_binding_meta,
    )

    if integ.provider == "github":
        logger.info(
            "Resolved GitHub integration binding [%s]: origin=%s acting_user_id=%s integration_id=%s owner_user_id=%s company_managed=%s auth_type=%s github_account_logins=%s github_installation_ids=%s",
            source_label or "unknown",
            binding_origin or "unknown",
            user_id or "anonymous",
            integration_id,
            binding_owner_user_id or "-",
            integ.is_company_managed,
            integ.auth_type,
            github_binding_meta.get("github_account_logins") or [],
            github_binding_meta.get("github_installation_ids") or [],
        )

    # --- Provider mismatch guard ---
    # The agent's integration_bindings maps provider→list[integration_id].  If the
    # binding key (e.g. "rss") doesn't match the actual provider stored on the
    # integration row (e.g. "opencti"), the binding is stale / wrong — skip it.
    if provider and integ.provider != provider:
        logger.warning(
            "Integration '%s' (%s) has provider '%s' but binding key is '%s' — skipping (stale or mismatched binding)",
            integ.name,
            integration_id,
            integ.provider,
            provider,
        )
        return None

    # --- Microsoft Defender XDR / Sentinel (Azure AD security integrations) ---
    # Both span two API audiences, so token resolution depends on the auth mode:
    #   • OAuth (delegated): per-audience tokens are minted at build time by the
    #     token_acquirer (refresh-token redemption). Here we only surface what the
    #     provider still needs that the token can't carry — the workspace
    #     coordinates (Sentinel) — or a non-secret marker (Defender) so the build
    #     loop proceeds.
    #   • api_key (app-only / client credentials): assemble the compound token the
    #     provider parses to mint its own app-only tokens. Credentials are read
    #     from the dedicated columns first, falling back to extra_config for
    #     integrations created before OAuth support existed.
    # This runs before the generic OAuth refresh / token-decryption logic because
    # app-only instances have no access_token or api_key to surface.
    if provider in ("defender", "sentinel"):
        extra: dict = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                # Surface decryption failures (corrupted ciphertext, rotated key)
                # instead of silently treating extra_config as empty and later
                # reporting a misleading "missing credentials" error.
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (%s): %s",
                    integ.name,
                    provider,
                    format_exception_message(exc),
                )

        if integ.auth_type == "oauth":
            if not integ.refresh_token_enc:
                logger.warning(
                    "Integration '%s' (%s) uses OAuth but has no refresh token — "
                    "the user must authorize it (with offline_access).",
                    integ.name,
                    provider,
                )
                return None
            if provider == "sentinel":
                coords = [
                    extra.get("subscription_id", "").strip(),
                    extra.get("resource_group", "").strip(),
                    extra.get("workspace_name", "").strip(),
                ]
                if all(coords):
                    return "||".join(coords)
                logger.warning(
                    "Integration '%s' (sentinel) is missing subscription_id, "
                    "resource_group, or workspace_name in extra_config.",
                    integ.name,
                )
                return None
            # Defender mints both audience tokens via the acquirer; this is a
            # non-secret marker so the build loop proceeds (the value is unused).
            return "__oauth__"

        # app-only (client credentials)
        tenant_id = extra.get("tenant_id", "").strip()
        client_id = (
            decrypt_value(integ.client_id_enc)
            if integ.client_id_enc
            else extra.get("client_id", "").strip()
        )
        client_secret = (
            (await reveal_field(integ, "client_secret_enc"))
            or extra.get("client_secret", "").strip()
        )
        if provider == "sentinel":
            parts = [
                tenant_id,
                client_id,
                client_secret,
                extra.get("subscription_id", "").strip(),
                extra.get("resource_group", "").strip(),
                extra.get("workspace_name", "").strip(),
            ]
            missing_msg = (
                "Ensure tenant_id, client_id, client_secret, subscription_id, "
                "resource_group, and workspace_name are all set."
            )
        else:
            parts = [tenant_id, client_id, client_secret]
            missing_msg = "Ensure tenant_id, client_id, and client_secret are all set."
        if all(parts):
            return "||".join(parts)
        logger.warning(
            "Integration '%s' (%s) is missing required app-only credentials. %s",
            integ.name,
            provider,
            missing_msg,
        )
        return None

    # --- Microsoft Entra ID (Azure AD identity & access management) ---
    # Entra uses a single Microsoft Graph audience, so the OAuth (delegated)
    # mode falls through to the generic OAuth token-decryption path below — the
    # stored access token IS a Graph token, refreshed on 401 like Intune /
    # Microsoft. Only the app-only (API key / client-credentials) mode needs a
    # dedicated branch to assemble the compound tenant_id||client_id||
    # client_secret token the provider mints its own Graph tokens from.
    if provider == "entra" and integ.auth_type != "oauth":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (entra): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        tenant_id = extra.get("tenant_id", "").strip()
        # Strip both the decrypted and extra_config-sourced credentials before
        # joining: a secret copy/pasted into the encrypted column often carries a
        # trailing newline, which would otherwise break the client-credentials
        # token mint in a non-obvious way.
        client_id = (
            decrypt_value(integ.client_id_enc)
            if integ.client_id_enc
            else extra.get("client_id", "")
        ).strip()
        client_secret = (
            (await reveal_field(integ, "client_secret_enc"))
            or extra.get("client_secret", "")
        ).strip()
        parts = [tenant_id, client_id, client_secret]
        if all(parts):
            return "||".join(parts)
        logger.warning(
            "Integration '%s' (entra) is missing app-only credentials. Ensure "
            "tenant_id, client_id, and client_secret are all set.",
            integ.name,
        )
        return None

    # --- Microsoft Purview (Azure AD data governance, risk & compliance) ---
    # Purview spans two API audiences: Microsoft Graph (the compliance pillar)
    # and the Purview Data Map (the governance pillar, https://purview.azure.net).
    # Token resolution mirrors Defender XDR:
    #   • OAuth (delegated): per-audience tokens are minted at build time by the
    #     token_acquirer (refresh-token redemption); return a non-secret marker so
    #     the build loop proceeds.
    #   • api_key (app-only): assemble the compound token the provider parses to
    #     mint its own per-audience tokens. The Purview governance account name
    #     (non-secret) rides as the 4th field so the Data Map tools can target
    #     https://{account}.purview.azure.com (it is optional - the compliance
    #     tools need only Microsoft Graph).
    if provider == "purview":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (purview): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        if integ.auth_type == "oauth":
            if not integ.refresh_token_enc:
                logger.warning(
                    "Integration '%s' (purview) uses OAuth but has no refresh "
                    "token - the user must authorize it (with offline_access).",
                    integ.name,
                )
                return None
            # Per-audience tokens are minted via the token_acquirer; this is a
            # non-secret marker so the build loop proceeds (the value is unused).
            return "__oauth__"
        # app-only (client credentials)
        tenant_id = extra.get("tenant_id", "").strip()
        client_id = (
            decrypt_value(integ.client_id_enc)
            if integ.client_id_enc
            else extra.get("client_id", "")
        ).strip()
        client_secret = (
            (await reveal_field(integ, "client_secret_enc"))
            or extra.get("client_secret", "")
        ).strip()
        account_name = (extra.get("account_name") or "").strip()
        if all([tenant_id, client_id, client_secret]):
            # account_name may be "" - only the Data Map tools require it.
            return "||".join([tenant_id, client_id, client_secret, account_name])
        logger.warning(
            "Integration '%s' (purview) is missing app-only credentials. Ensure "
            "tenant_id, client_id, and client_secret are all set.",
            integ.name,
        )
        return None

    # --- CrowdStrike Falcon + Next-Gen SIEM (OAuth2 client-credentials API key) ---
    # The providers need the compound ``base_url||client_id||client_secret`` so
    # they can mint short-lived bearer tokens against the tenant's cloud region's
    # ``/oauth2/token`` endpoint. Credentials live in the dedicated client_id /
    # client_secret columns; the cloud region (or a raw base URL) lives in
    # extra_config and is resolved to an API base URL. Next-Gen SIEM
    # stores its API client exactly like the endpoint provider.
    if provider in ("crowdstrike", "crowdstrike_ngsiem"):
        from app.integrations.providers.crowdstrike import resolve_base_url

        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (%s): %s",
                    integ.name,
                    provider,
                    format_exception_message(exc),
                )
        client_id = decrypt_value(integ.client_id_enc) if integ.client_id_enc else ""
        client_secret = (
            (await reveal_field(integ, "client_secret_enc")) or ""
        )
        cloud = (extra.get("cloud") or extra.get("base_url") or "").strip()
        base_url = resolve_base_url(cloud)
        if not (client_id and client_secret):
            logger.warning(
                "Integration '%s' (%s) is missing the API client ID or "
                "secret. Ensure both Client ID and Client Secret are set.",
                integ.name,
                provider,
            )
        # Return the (possibly partial) compound rather than None: None makes
        # build_integration_tools skip the provider entirely, hiding the
        # misconfiguration; the partial compound lets the provider's call-time
        # guard surface an actionable error (mirrors sumologic / servicenow).
        return f"{base_url}||{client_id}||{client_secret}"

    # --- Elastic Security (Kibana detection engine + Elasticsearch hunting) ---
    # The provider needs the compound ``kibana_url||es_url||api_key||space`` so
    # it can target the Kibana security APIs (space-aware) and, for hunting,
    # the Elasticsearch APIs. The API key lives in api_key_enc; the Kibana URL
    # (base_url), optional Elasticsearch URL (es_url), and optional Kibana space
    # (space_id) live in extra_config.
    if provider == "elastic_security":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (elastic_security): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        api_key = (await reveal_field(integ, "api_key_enc")) or ""
        kibana_url = (extra.get("base_url") or "").strip().rstrip("/")
        es_url = (extra.get("es_url") or "").strip().rstrip("/")
        space = (extra.get("space_id") or "").strip() or "default"
        if not (kibana_url and api_key):
            logger.warning(
                "Integration '%s' (elastic_security) is missing the Kibana URL or "
                "API key. Ensure both the Base URL (Kibana) and API Key are set.",
                integ.name,
            )
        # Partial compound (not None) so the provider surfaces an actionable
        # error instead of the tools silently disappearing.
        return f"{kibana_url}||{es_url}||{api_key}||{space}"

    # --- Google SecOps / Chronicle (service-account JWT auth) ---
    # The provider needs the compound ``region||project||customer||sa_json`` so
    # it can mint OAuth2 access tokens (by signing a JWT with the service
    # account key) and target the right Chronicle v1alpha instance. The service
    # account JSON lives in api_key_enc; the region, GCP project ID, and SecOps
    # customer (instance) ID live in extra_config. The SA JSON is the final
    # ``||`` segment and is split with maxsplit in the provider, so it is safe
    # even though it contains no ``||`` itself.
    if provider == "google_secops":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (google_secops): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        sa_json = (await reveal_field(integ, "api_key_enc")) or ""
        region = (extra.get("region") or "").strip()
        project = (extra.get("project_id") or "").strip()
        customer = (extra.get("customer_id") or "").strip()
        if not (region and project and customer and sa_json):
            logger.warning(
                "Integration '%s' (google_secops) is missing the region, project ID, "
                "customer ID, or service account JSON.",
                integ.name,
            )
        # Partial compound (not None) so the provider surfaces an actionable
        # error instead of the tools silently disappearing.
        return f"{region}||{project}||{customer}||{sa_json}"

    # --- Odoo: base_url||database||login||api_key[||company_ids] ---
    # The URL, database, login and default companies live in extra_config,
    # the API key in api_key_enc. A missing part still builds the tools: the
    # provider names it at call time.
    if provider == "odoo":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (odoo): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        from app.integrations.providers.odoo import OdooCredential

        return OdooCredential.build(
            str(extra.get("base_url") or ""),
            str(extra.get("database") or ""),
            str(extra.get("login") or ""),
            (await reveal_field(integ, "api_key_enc")) or "",
            str(extra.get("company_ids") or ""),
        )

    # --- NetSuite ---
    # The API Key field holds the secret (the certificate's private key for
    # OAuth 2.0 machine-to-machine, the token secret for TBA); the account,
    # the mode and the other identifiers live in extra_config. The provider
    # mints or signs its own requests, so the OAuth refresh below does not
    # apply. The secret is the last field (split greedily).
    if provider == "netsuite":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (netsuite): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        from app.integrations.providers.netsuite import compound_credential as netsuite_credential

        compound = netsuite_credential((await reveal_field(integ, "api_key_enc")) or "", extra)
        if not compound:
            logger.warning(
                "Integration '%s' (netsuite) is missing the account ID, the secret or the identifiers "
                "of its authentication mode.",
                integ.name,
            )
            return None
        return compound

    # --- DocuSign JWT Grant ("API Key" mode) ---
    # The RSA private key lives in api_key_enc; the integration key, the user
    # to impersonate, the environment and the account live in extra_config.
    # The provider mints its own short-lived access token from them, so the
    # OAuth refresh below does not apply. The key is the last field (it has
    # newlines, never ``||``).
    if provider == "docusign" and integ.auth_type != "oauth":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (docusign): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        from app.integrations.providers.docusign import compound_credential

        compound = compound_credential(
            (await reveal_field(integ, "api_key_enc")) or "",
            extra,
            jwt_grant=True,
            integration_key=decrypt_value(integ.client_id_enc) if integ.client_id_enc else "",
        )
        if not compound:
            logger.warning(
                "Integration '%s' (docusign) is missing the RSA private key, the "
                "integration key or the user ID for the JWT Grant.",
                integ.name,
            )
            return None
        return compound

    # --- Amazon SES (AWS SigV4 with IAM access keys) ---
    # SES signs every request with AWS Signature V4, so there is no single
    # bearer token. The provider needs the compound
    # ``region||access_key_id||secret_access_key||configuration_set`` to sign
    # requests against ``email.{region}.amazonaws.com``. The Secret Access Key
    # (the credential) lives in api_key_enc; the Access Key ID, region, and an
    # optional default configuration set live in extra_config.
    if provider == "ses":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (ses): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        secret_access_key = (
            (await reveal_field(integ, "api_key_enc")) or ""
        )
        access_key_id = (extra.get("access_key_id") or "").strip()
        region = (extra.get("region") or "us-east-1").strip() or "us-east-1"
        config_set = (extra.get("configuration_set") or "").strip()
        if not (access_key_id and secret_access_key):
            logger.warning(
                "Integration '%s' (ses) is missing the AWS Access Key ID or Secret "
                "Access Key. Ensure both are set (Access Key ID in extra config, "
                "Secret Access Key in the API Key field).",
                integ.name,
            )
        # Partial compound (not None) so the provider surfaces an actionable
        # error instead of the tools silently disappearing.
        return f"{region}||{access_key_id}||{secret_access_key}||{config_set}"

    # --- AWS Security (AWS SigV4 with IAM access keys) ---
    # GuardDuty / Security Hub / CloudTrail sign every request with AWS
    # Signature V4, so there is no single bearer token. The provider needs the
    # compound ``region||access_key_id||secret_access_key`` to sign requests
    # against the regional service endpoints. The Secret Access Key (the
    # credential) lives in api_key_enc; the Access Key ID and region live in
    # extra_config.
    if provider == "aws_security":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (aws_security): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        # Strip the secret too - a trailing newline/space from a paste would
        # silently break every SigV4 signature.
        secret_access_key = (
            (await reveal_field(integ, "api_key_enc")) or ""
        ).strip()
        access_key_id = (extra.get("access_key_id") or "").strip()
        region = (extra.get("region") or "us-east-1").strip() or "us-east-1"
        if not (access_key_id and secret_access_key):
            logger.warning(
                "Integration '%s' (aws_security) is missing the AWS Access Key "
                "ID or Secret Access Key. Ensure both are set (Access Key ID in "
                "extra config, Secret Access Key in the API Key field).",
                integ.name,
            )
        # Partial compound (not None) so the provider surfaces an actionable
        # error instead of the tools silently disappearing.
        return f"{region}||{access_key_id}||{secret_access_key}"

    # --- OVHcloud (signed application key or OAuth2 service account) ---
    # The application secret or the OAuth2 client secret lives in api_key_enc;
    # the endpoint, the auth mode, the application key or client id and the
    # consumer key in extra_config. The provider gets the compound
    # ``endpoint||mode||key_or_client_id||consumer_key||secret`` (partial when
    # a part is missing, so a call names what to fix).
    if provider == "ovhcloud":
        from app.integrations.providers.ovhcloud import token_from_config

        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (ovhcloud): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        secret = (await reveal_field(integ, "api_key_enc")) or ""
        return token_from_config(secret, extra)

    # --- Aikido Security (OAuth2 client credentials on a regional host) ---
    # The client secret is the API key column; the region and the client id
    # live in extra_config. The provider gets ``region||client_id||secret``
    # (partial when a part is missing, so a call names what to fix).
    if provider == "aikido":
        from app.integrations.providers.aikido import (
            token_from_config as aikido_token_from_config,
        )

        aikido_extra = {}
        if integ.extra_config_enc:
            try:
                aikido_extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (aikido): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        aikido_secret = (await reveal_field(integ, "api_key_enc")) or ""
        return aikido_token_from_config(aikido_secret, aikido_extra)

    # --- Airwallex (client id + API key exchanged for a 30-minute bearer) ---
    # The API key is the API key column; the environment, the client id, the
    # account to log in as live in extra_config.
    # The provider gets ``environment||client_id||login_as||api_key``.
    if provider == "airwallex":
        from app.integrations.providers.airwallex import (
            token_from_config as airwallex_token_from_config,
        )

        airwallex_extra = {}
        if integ.extra_config_enc:
            try:
                airwallex_extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (airwallex): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        airwallex_secret = (await reveal_field(integ, "api_key_enc")) or ""
        return airwallex_token_from_config(airwallex_secret, airwallex_extra)

    # --- Rydoo (OAuth2 client credentials, one company, branches as entities) ---
    # The client secret is the API key column; the client id lives in
    # extra_config. The provider gets ``client_id||client_secret``.
    if provider == "rydoo":
        from app.integrations.providers.rydoo import (
            token_from_config as rydoo_token_from_config,
        )

        rydoo_extra = {}
        if integ.extra_config_enc:
            try:
                rydoo_extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (rydoo): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        rydoo_secret = (await reveal_field(integ, "api_key_enc")) or ""
        return rydoo_token_from_config(rydoo_secret, rydoo_extra)

    # --- Spendesk (OAuth2 client credentials, production or sandbox host) ---
    # The client secret is the API key column; the environment and the client
    # id live in extra_config. The provider gets ``environment||client_id||secret``
    # (partial when a part is missing, so a call names what to fix).
    if provider == "spendesk":
        from app.integrations.providers.spendesk import (
            token_from_config as spendesk_token_from_config,
        )

        spendesk_extra = {}
        if integ.extra_config_enc:
            try:
                spendesk_extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (spendesk): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        spendesk_secret = (await reveal_field(integ, "api_key_enc")) or ""
        return spendesk_token_from_config(spendesk_secret, spendesk_extra)

    # --- CrowdSec (two independent x-api-key credentials) ---
    # The CTI API (cti.api.crowdsec.net) and the Service API
    # (admin.api.crowdsec.net) each use their own API key, both sent in the
    # x-api-key header. The CTI key lives in api_key_enc; the optional Service
    # API key lives in extra_config.service_api_key (Fernet-encrypted JSON).
    # Either key may be omitted - the provider registers every tool and the
    # subset missing its key returns an actionable error at call time, so a
    # partial compound (not None) is returned on purpose.
    if provider == "crowdsec":
        cti_key = (await reveal_field(integ, "api_key_enc")) or ""
        service_key = ""
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
                service_key = (extra.get("service_api_key") or "").strip()
            except Exception as exc:
                logger.warning(
                    "Failed to extract service_api_key from extra_config for "
                    "'%s' (crowdsec): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        if not (cti_key.strip() or service_key):
            logger.warning(
                "Integration '%s' (crowdsec) has neither a CTI API key nor a "
                "Service API key. Set at least one of them.",
                integ.name,
            )
        return f"{cti_key.strip()}||{service_key}"

    # --- 1Password (Connect server + optional Events API + optional SCIM bridge) ---
    # Three independent bearer credentials, one per subsystem. The provider
    # parses the 6-part compound
    # ``connect_url||connect_token||events_url||events_token||scim_url||scim_token``.
    # The Connect access token (the primary credential) lives in api_key_enc;
    # the Connect base URL and the optional Events / SCIM coordinates live in
    # extra_config (Fernet-encrypted JSON, like crowdsec's service_api_key).
    # Events and SCIM may be omitted - the provider registers every tool and
    # the subset missing its credential returns an actionable error at call
    # time, so a partial compound (not None) is returned on purpose.
    if provider == "onepassword":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (onepassword): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        connect_token = (
            (await reveal_field(integ, "api_key_enc")) or ""
        ).strip()
        connect_url = (extra.get("base_url") or "").strip().rstrip("/")
        events_url = (extra.get("events_base_url") or "").strip().rstrip("/")
        events_token = (extra.get("events_api_key") or "").strip()
        scim_url = (extra.get("scim_base_url") or "").strip().rstrip("/")
        scim_token = (extra.get("scim_api_key") or "").strip()
        if not (connect_url and connect_token):
            logger.warning(
                "Integration '%s' (onepassword) is missing the Connect server "
                "base URL or access token. Vault and item tools will be "
                "unavailable until both are set.",
                integ.name,
            )
        return "||".join(
            [connect_url, connect_token, events_url, events_token, scim_url, scim_token]
        )

    # --- Tenable Vulnerability Management (X-ApiKeys access/secret key pair) ---
    # The provider needs the compound ``base_url||access_key||secret_key`` to
    # build the ``X-ApiKeys`` header. The Secret Key (the credential) lives in
    # api_key_enc; the Access Key (the paired identifier) and the cloud base URL
    # live in extra_config (base_url defaults to https://cloud.tenable.com).
    if provider == "tenable":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (tenable): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        secret_key = (
            (await reveal_field(integ, "api_key_enc")) or ""
        ).strip()
        base_url = (
            extra.get("base_url") or "https://cloud.tenable.com"
        ).strip().rstrip("/")
        access_key = (extra.get("access_key") or "").strip()
        if not (access_key and secret_key):
            logger.warning(
                "Integration '%s' (tenable) is missing the Access Key or Secret Key. "
                "Ensure the Access Key is set in extra config and the Secret Key in "
                "the API Key field.",
                integ.name,
            )
        # Partial compound (not None) so the provider surfaces an actionable
        # error instead of the tools silently disappearing.
        return f"{base_url}||{access_key}||{secret_key}"

    # --- ThreatConnect (HMAC-SHA256 request signing with Access ID/Secret Key) ---
    # The provider signs every request with the Secret Key (no bearer token), so
    # it needs the compound ``base_url||access_id||secret_key``. The Secret Key
    # (the credential) lives in api_key_enc; the instance base URL and the
    # Access ID (the paired identifier) live in extra_config. The instance URL
    # is customer-specific (Public Cloud https://app.threatconnect.com or a
    # Dedicated Cloud / On-Prem host), so there is no default.
    if provider == "threatconnect":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (threatconnect): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        secret_key = (
            (await reveal_field(integ, "api_key_enc")) or ""
        ).strip()
        base_url = (extra.get("base_url") or "").strip().rstrip("/")
        tc_access_id = (extra.get("access_id") or "").strip()
        if not (base_url and tc_access_id and secret_key):
            logger.warning(
                "Integration '%s' (threatconnect) is missing the instance URL, "
                "Access ID, or Secret Key. Set the instance URL and Access ID in "
                "extra config and the Secret Key in the API Key field.",
                integ.name,
            )
        # Partial compound (not None) so a missing Secret Key still registers
        # the tools and surfaces an actionable signing error at call time.
        # Without the instance URL or Access ID the provider cannot build a
        # single valid request, so build_tools() registers nothing — the
        # warning above is what flags that misconfiguration.
        return f"{base_url}||{tc_access_id}||{secret_key}"

    # --- Cyware Intel Exchange (HMAC-SHA1 signed query params) ---
    # The provider signs every request with AccessID/Expires/Signature query
    # params derived from the Secret Key (no bearer token), so it needs the
    # compound ``base_url||access_id||secret_key``. The Secret Key (the
    # credential) lives in api_key_enc; the Open API endpoint URL and the
    # Access ID live in extra_config. The endpoint URL is tenant-specific
    # (e.g. https://tenant.cyware.com/ctixapi), so there is no default.
    if provider == "cyware":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (cyware): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        secret_key = (
            (await reveal_field(integ, "api_key_enc")) or ""
        ).strip()
        base_url = (extra.get("base_url") or "").strip().rstrip("/")
        cw_access_id = (extra.get("access_id") or "").strip()
        if not (base_url and cw_access_id and secret_key):
            logger.warning(
                "Integration '%s' (cyware) is missing the Open API endpoint "
                "URL, Access ID, or Secret Key. Set the endpoint URL and "
                "Access ID in extra config and the Secret Key in the API Key "
                "field.",
                integ.name,
            )
        # Partial compound (not None) so a missing Secret Key still registers
        # the tools and surfaces an actionable signing error at call time.
        # Without the endpoint URL or Access ID the provider cannot build a
        # single valid request, so build_tools() registers nothing — the
        # warning above is what flags that misconfiguration.
        return f"{base_url}||{cw_access_id}||{secret_key}"

    # --- Anomali ThreatStream (apikey user:key header auth) ---
    # The provider needs the compound ``base_url||username||api_key`` to build
    # the ``Authorization: apikey <username>:<api_key>`` header. The API key
    # (the credential) lives in api_key_enc; the username and the instance base
    # URL live in extra_config (base_url defaults to the ThreatStream SaaS host
    # https://api.threatstream.com; on-prem / AirGap instances override it).
    if provider == "anomali":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (anomali): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        api_key = (
            (await reveal_field(integ, "api_key_enc")) or ""
        ).strip()
        base_url = (
            extra.get("base_url") or "https://api.threatstream.com"
        ).strip().rstrip("/")
        anomali_username = (extra.get("username") or "").strip()
        if not (anomali_username and api_key):
            logger.warning(
                "Integration '%s' (anomali) is missing the username or API key. "
                "Ensure the username is set in extra config and the API key in "
                "the API Key field.",
                integ.name,
            )
        # Return the compound even when parts are missing (base_url always has
        # a default, so catalog introspection keeps working). The runtime
        # degradation differs per part: a missing username makes build_tools()
        # return no tools (the warning above tells the admin what to fix),
        # while a missing API key still builds the tools, which then fail with
        # a clear 401 error at call time.
        return f"{base_url}||{anomali_username}||{api_key}"

    # --- SOCRadar (API-Key header auth, company-scoped endpoints) ---
    # The provider needs the compound ``base_url||company_id||api_key`` because
    # every company endpoint is scoped by the numeric Company ID
    # (``/company/{company_id}/...``). The API key (the credential) lives in
    # api_key_enc; the company ID and the base URL live in extra_config
    # (base_url defaults to the SOCRadar SaaS host
    # https://platform.socradar.com/api).
    if provider == "socradar":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (socradar): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        api_key = (
            (await reveal_field(integ, "api_key_enc")) or ""
        ).strip()
        base_url = (
            extra.get("base_url") or "https://platform.socradar.com/api"
        ).strip().rstrip("/")
        socradar_company_id = str(extra.get("company_id") or "").strip()
        if not (socradar_company_id and api_key):
            logger.warning(
                "Integration '%s' (socradar) is missing the Company ID or API "
                "key. Ensure the Company ID is set in extra config and the API "
                "key in the API Key field.",
                integ.name,
            )
        # Return the compound even when parts are missing (base_url always has
        # a default, so catalog introspection keeps working). A missing
        # company ID makes build_tools() return no tools (the warning above
        # tells the admin what to fix); a missing API key still builds the
        # tools, which then fail with a clear 401 error at call time.
        return f"{base_url}||{socradar_company_id}||{api_key}"

    # --- Doppel (V1 x-api-key header auth OR V2 OAuth client credentials) ---
    # The provider needs the compound
    # ``base_url||api_key||user_api_key||organization_code||client_id||client_secret``.
    # V1: the org API key (the secret) lives in api_key_enc; the optional user
    # API key and organization code live in extra_config. V2 (preferred when
    # configured): the OAuth client ID + client secret live in extra_config
    # (Fernet-encrypted JSON) and are exchanged for a bearer token by the
    # provider. The base URL (defaults to the fixed SaaS host
    # https://api.doppel.com) lives in extra_config too.
    if provider == "doppel":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (doppel): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        api_key = (
            (await reveal_field(integ, "api_key_enc")) or ""
        ).strip()
        base_url = (
            extra.get("base_url") or "https://api.doppel.com"
        ).strip().rstrip("/")
        user_api_key = (extra.get("user_api_key") or "").strip()
        organization_code = (extra.get("organization_code") or "").strip()
        client_id = (extra.get("client_id") or "").strip()
        client_secret = (extra.get("client_secret") or "").strip()
        if not api_key and not (client_id and client_secret):
            logger.warning(
                "Integration '%s' (doppel) has no credentials. Set either "
                "the V1 organization API key in the API Key field or a V2 "
                "client_id + client_secret in extra config.",
                integ.name,
            )
        # Partial compound (not None) so the provider surfaces an actionable
        # error at call time instead of the tools silently disappearing.
        return (
            f"{base_url}||{api_key}||{user_api_key}||{organization_code}"
            f"||{client_id}||{client_secret}"
        )

    # --- Qualys VMDR (HTTP Basic auth + the mandatory X-Requested-With header) ---
    # The classic Qualys VM API authenticates with HTTP Basic (username +
    # password) and requires the X-Requested-With header on every /api/2.0/fo/
    # request. The provider needs the compound ``base_url||username||password`` to
    # build the Basic auth header. The password (the credential) lives in
    # api_key_enc; the username and the Qualys API server / POD base URL live in
    # extra_config (the POD URL is customer-specific, so there is no default).
    if provider == "qualys":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (qualys): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        password = (await reveal_field(integ, "api_key_enc")) or ""
        base_url = (extra.get("base_url") or "").strip().rstrip("/")
        username = (extra.get("username") or "").strip()
        if not (base_url and username and password):
            logger.warning(
                "Integration '%s' (qualys) is missing the base URL, username, or "
                "password. Ensure the Qualys API server URL and username are set in "
                "extra config and the password in the API Key field.",
                integ.name,
            )
        # Partial compound (not None) so the provider surfaces an actionable
        # error instead of the tools silently disappearing.
        return f"{base_url}||{username}||{password}"

    # --- Sumo Logic (HTTP Basic with an Access ID / Access Key pair) ---
    # The provider needs the compound ``base_url||access_id||access_key`` to
    # build the HTTP Basic ``Authorization`` header. The Access Key (the secret)
    # lives in api_key_enc; the Access ID (the paired identifier) and the
    # region-specific API base URL (ending in /api) live in extra_config
    # (base_url defaults to the US1 endpoint https://api.sumologic.com/api).
    if provider == "sumologic":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (sumologic): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        access_key = (await reveal_field(integ, "api_key_enc")) or ""
        base_url = (
            extra.get("base_url") or "https://api.sumologic.com/api"
        ).strip().rstrip("/")
        access_id = (extra.get("access_id") or "").strip()
        if not (access_id and access_key):
            # Still return the compound token (with the empty segment preserved)
            # rather than None, so the tools build and the provider's call-time
            # guard surfaces a clear "credentials are not configured" error. A
            # None here makes build_integration_tools skip the provider entirely
            # (`if not token: continue`), hiding the misconfiguration — matching
            # the infoblox/rapid7 and metabase/opencti partial-token behavior.
            logger.warning(
                "Integration '%s' (sumologic) is missing the Access ID or Access "
                "Key. Ensure the Access ID is set in extra config and the Access "
                "Key in the API Key field.",
                integ.name,
            )
        return f"{base_url}||{access_id}||{access_key}"

    # --- ServiceNow (HTTP Basic with an instance URL + username/password) ---
    # The provider needs the compound ``base_url||username||password`` to build
    # the Basic auth header. The password (the credential) lives in api_key_enc;
    # the instance URL and the username live in extra_config. The instance URL is
    # customer-specific (https://<instance>.service-now.com), so there is no
    # default. A partial token (with empty segments preserved) is still returned
    # rather than None so the tools build and the provider's call-time / 401
    # error surfaces the misconfiguration, instead of silently hiding it.
    if provider == "servicenow":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (servicenow): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        password = (await reveal_field(integ, "api_key_enc")) or ""
        base_url = (extra.get("base_url") or "").strip().rstrip("/")
        username = (extra.get("username") or "").strip()
        if not (base_url and username and password):
            logger.warning(
                "Integration '%s' (servicenow) is missing the instance URL, "
                "username, or password. Set the instance URL and username in "
                "extra config and the password in the API Key field.",
                integ.name,
            )
        return f"{base_url}||{username}||{password}"

    # --- ThreatQuotient ThreatQ (OAuth2 password grant on a self-hosted
    # appliance) --- The provider needs the compound
    # ``base_url||client_id||email||password`` so it can mint short-lived
    # bearer tokens against the appliance's ``/api/token`` endpoint (cached
    # in-process, mirroring defender/sentinel/crowdstrike). The password (the
    # secret) lives in api_key_enc; the appliance base URL, OAuth client ID,
    # and API user email live in extra_config. The password is the FINAL
    # segment and is parsed with greedy_last, so a password containing ``||``
    # still round-trips.
    if provider == "threatq":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (threatq): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        password = (await reveal_field(integ, "api_key_enc")) or ""
        base_url = (extra.get("base_url") or "").strip().rstrip("/")
        tq_client_id = (extra.get("client_id") or "").strip()
        tq_email = (extra.get("email") or "").strip()
        if not (base_url and tq_client_id and tq_email and password):
            # Partial compound (not None) so the provider's call-time guard
            # surfaces an actionable error instead of the tools silently
            # disappearing (mirrors sumologic / servicenow).
            logger.warning(
                "Integration '%s' (threatq) is missing the base URL, client ID, "
                "email, or password. Set the base URL, client ID, and email in "
                "extra config and the password in the API Key field.",
                integ.name,
            )
        return f"{base_url}||{tq_client_id}||{tq_email}||{password}"

    # --- Cybersixgill (OAuth2 client-credentials, deep/dark web CTI) ---
    # The provider needs the compound
    # ``base_url||client_id||organization_id||client_secret`` so it can mint
    # short-lived bearer tokens against the vendor's ``/auth/token`` endpoint
    # (cached in-process, mirroring picus/threatq). The client secret (the
    # secret) lives in api_key_enc; the API base URL, client ID, and optional
    # MSSP organization ID live in extra_config. The client secret is the FINAL
    # segment and is parsed with greedy_last, so a secret containing ``||``
    # still round-trips. base_url is OPTIONAL - the provider defaults an empty
    # value to https://api.cybersixgill.com.
    if provider == "cybersixgill":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (cybersixgill): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        client_secret = (await reveal_field(integ, "api_key_enc")) or ""
        base_url = (extra.get("base_url") or "").strip().rstrip("/")
        csg_client_id = (extra.get("client_id") or "").strip()
        csg_org_id = (extra.get("organization_id") or "").strip()
        if not (csg_client_id and client_secret):
            # Partial compound (not None) so the provider's call-time guard
            # surfaces an actionable error instead of the tools silently
            # disappearing (mirrors threatq / sumologic / servicenow).
            logger.warning(
                "Integration '%s' (cybersixgill) is missing the client ID or "
                "client secret. Set the client ID in extra config and the "
                "client secret in the API Key field.",
                integ.name,
            )
        return f"{base_url}||{csg_client_id}||{csg_org_id}||{client_secret}"

    # --- Wiz (OAuth2 client-credentials, cloud security / CNAPP) ---
    # The provider needs the compound ``api_endpoint_url||client_id||client_secret``
    # so it can mint short-lived bearer tokens against the Wiz identity
    # provider (cached in-process, mirroring cybersixgill/picus/threatq). The
    # client secret lives in api_key_enc; the tenant-specific API endpoint URL
    # and service-account client ID live in extra_config. The client secret is
    # the FINAL segment and is parsed with greedy_last, so a secret containing
    # ``||`` still round-trips. api_endpoint_url has NO default - it is
    # tenant-specific (https://api.<region>.app.wiz.io).
    if provider == "wiz":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (wiz): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        client_secret = (await reveal_field(integ, "api_key_enc")) or ""
        api_endpoint_url = (extra.get("api_endpoint_url") or "").strip().rstrip("/")
        wiz_client_id = (extra.get("client_id") or "").strip()
        if not (api_endpoint_url and wiz_client_id and client_secret):
            # Partial compound (not None) so the provider's call-time guard
            # surfaces an actionable error instead of the tools silently
            # disappearing (mirrors cybersixgill / threatq).
            logger.warning(
                "Integration '%s' (wiz) is missing the API endpoint URL, "
                "client ID, or client secret. Set the API endpoint URL and "
                "client ID in extra config and the client secret in the API "
                "Key field.",
                integ.name,
            )
        return f"{api_endpoint_url}||{wiz_client_id}||{client_secret}"

    # --- Databricks (PAT *or* OAuth machine-to-machine on a workspace host) ---
    # The provider needs ``workspace_url||client_id||warehouse_id||secret``. The
    # secret in api_key_enc is the personal access token when no client ID is
    # configured, and the service principal's OAuth secret when one is - the
    # provider infers the auth mode from the presence of the client ID, so the
    # client ID being empty is a NORMAL configuration, not a misconfiguration.
    # The workspace URL is workspace-specific and has no default. The secret is
    # the FINAL segment and is parsed with greedy_last, so a value containing
    # ``||`` still round-trips. A partial compound (empty segments preserved)
    # is returned rather than None so the tools still build and the provider's
    # call-time guard surfaces the misconfiguration.
    if provider == "databricks":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (databricks): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        databricks_secret = (
            (await reveal_field(integ, "api_key_enc")) or ""
        )
        workspace_url = (extra.get("base_url") or "").strip().rstrip("/")
        databricks_client_id = (extra.get("client_id") or "").strip()
        warehouse_id = (extra.get("warehouse_id") or "").strip()
        if not (workspace_url and databricks_secret):
            logger.warning(
                "Integration '%s' (databricks) is missing the workspace URL or "
                "the credential. Set the workspace URL in extra config and the "
                "personal access token - or the service principal's OAuth "
                "secret when a client ID is set - in the API Key field.",
                integ.name,
            )
        return (
            f"{workspace_url}||{databricks_client_id}||{warehouse_id}"
            f"||{databricks_secret}"
        )

    # --- Supabase (Management API personal access token, optional project) ---
    # The provider needs ``project_ref||database_segment||access_token``; the
    # token is the greedy last segment. An empty project ref is a normal
    # configuration (the whole account). A set one restricts every tool to that
    # project, and a database role (``db_role``...) replaces the free SQL tools
    # by function calls as that role, so an extra_config that cannot be read
    # gives no tools at all rather than tools that silently reach every project
    # of the account, or its data past the role. ``decrypt_json``, not
    # ``_decrypt_dict``: the latter reads a non-object document as ``{}``,
    # which would be that same silent widening.
    if provider == "supabase":
        project_ref = ""
        database_segment = ""
        if integ.extra_config_enc:
            try:
                extra = await reveal_json_field(integ, "extra_config_enc")
                if not isinstance(extra, dict):
                    raise TypeError(
                        f"extra_config is a {type(extra).__name__}, not an object"
                    )
                project_ref = str(extra.get("project_ref") or "").strip()
                database_segment = supabase_database_segment(extra)
            except Exception as exc:
                logger.warning(
                    "Failed to read extra_config for '%s' (supabase): %s - "
                    "its tools are withheld so a configured project scope "
                    "cannot be lost.",
                    integ.name,
                    format_exception_message(exc),
                )
                return None
        supabase_token = (await reveal_field(integ, "api_key_enc")) or ""
        if not supabase_token:
            logger.warning(
                "Integration '%s' (supabase) has no access token. Paste a "
                "personal access token in the API Key field.",
                integ.name,
            )
        return f"{project_ref}||{database_segment}||{supabase_token}"

    # --- Proactive token refresh for OAuth integrations ---
    # When an inline refresh succeeds, the fresh access token is captured here
    # and consumed by the normal retrieval + compound-assembly flow below.
    refreshed_token: str | None = None
    if integ.auth_type == "oauth" and integ.access_token_enc:
        if prefetched_token is not None:
            # The caller's parallel refresh pass (P3.1) already produced a
            # fresh access token — feed it into the compound-assembly flow
            # below without a second (sequential) refresh round-trip.
            refreshed_token = prefetched_token
        elif refresh_attempted:
            # The parallel pass tried and failed — do NOT retry
            # sequentially (that would reintroduce the N x 30 s worst
            # case); surface the stored token like the legacy
            # failed-inline-refresh path (the tool will get a 401).
            logger.warning(
                "Parallel token refresh failed earlier for %s (%s) — "
                "using possibly expired token",
                integ.name,
                integ.provider,
            )

        needs_refresh = False
        # Unknown expiry (NULL) is the scheduler's job: it stamps a synthetic
        # TTL via apply_oauth_token_expiry. Treating NULL as "refresh now"
        # here re-hit Notion on every chat turn.
        if (
            refreshed_token is None
            and not refresh_attempted
            and integ.token_expires_at is not None
        ):
            buffer = datetime.now(UTC) + timedelta(
                minutes=_REFRESH_BUFFER_MINUTES
            )
            needs_refresh = integ.token_expires_at < buffer

        if needs_refresh:
            if not integ.refresh_token_enc:
                logger.warning(
                    "Token for %s (%s) is past its recorded expiry and has no refresh token to "
                    "renew it: it is used as stored. If the provider rejects it, the "
                    "integration must be connected again.",
                    integ.name,
                    integ.provider,
                )
                # Return expired token — the tool will get a 401 and surface the error
            else:
                logger.info(
                    "Token for %s (%s) is expired or expiring soon — refreshing inline",
                    integ.name,
                    integ.provider,
                )
                new_token = await _try_inline_refresh(db, integ)
                if new_token:
                    # Feed the refreshed token into the normal retrieval +
                    # compound-assembly flow below. Returning it bare here (the
                    # old behaviour) skipped every provider-specific compound
                    # format — jira ``cloud_id||token``, linkedin
                    # ``token||account_type||org``, facebook, xsoar, etc. — which
                    # silently disabled those providers' tools for the whole
                    # session whenever the token was refreshed inline.
                    refreshed_token = new_token
                else:
                    # Refresh failed — fall through and use the old token
                    # (it might still work; the tool will surface a 401).
                    logger.warning(
                        "Inline refresh failed for %s — using possibly expired token",
                        integ.name,
                    )

    # --- Normal token retrieval ---
    token = None
    if refreshed_token:
        # A successful inline refresh already produced the current access token;
        # prefer it over re-decrypting a possibly-stale in-memory column.
        token = refreshed_token
    elif integ.access_token_enc:
        token = decrypt_value(integ.access_token_enc)
    elif integ.api_key_enc and integ.auth_type != "oauth_password":
        # A password grant's api_key is the user's login, never a token.
        token = await reveal_field(integ, "api_key_enc")

    # MITRE ATT&CK: no credentials — the knowledge base is free and public.
    # Optional config (domains / pinned version / source mirror) lives in
    # extra_config; assemble the compound "domains||version||source_url"
    # token the builder parses. All parts are optional, so a missing
    # extra_config still yields a working token (all domains, latest release).
    if not token and provider == "mitre_attack":
        extra = {}
        if integ.extra_config_enc:
            try:
                decoded = await reveal_json_field(integ, "extra_config_enc")
                # The document can be any JSON type (e.g. a list written
                # via API misuse) — only a mapping is usable here.
                extra = decoded if isinstance(decoded, dict) else {}
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (mitre_attack): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        # Coerce to str defensively — a malformed extra_config value
        # (list/object via API misuse) must not break tool dispatch with
        # AttributeError.
        token = "||".join(
            str(extra.get(key) or "").strip()
            for key in ("domains", "version", "source_url")
        )

    # RSS: no API key — feed URLs stored in extra_config.feed_urls
    if not token and provider == "rss" and integ.extra_config_enc:
        try:
            extra = await _reveal_extra(integ)
            feed_urls = extra.get("feed_urls", "").strip()
            if feed_urls:
                token = feed_urls
        except Exception as exc:
            logger.warning(
                "Failed to extract feed_urls from extra_config for '%s' (rss): %s",
                integ.name,
                format_exception_message(exc),
            )

    # Platform email: no credential, it sends through the deployment's SMTP;
    # the "token" carries its guards (allowed domains, daily limit).
    if not token and provider == "platform_email":
        from app.integrations.providers.platform_email import platform_email_token

        try:
            extra = await _reveal_extra(integ) if integ.extra_config_enc else {}
        except Exception as exc:
            logger.warning(
                "Failed to read the guards of '%s' (platform_email): %s",
                integ.name,
                format_exception_message(exc),
            )
            extra = {}
        token = platform_email_token(extra)

    # MITRE ATLAS: no credentials — the "token" is the data source URL
    # (optional extra_config.source_url override for mirrors / air-gapped
    # hosts, falling back to the official atlas-data release URL). Resolved
    # through the provider's single chokepoint so this auth-none provider is
    # never skipped for lack of a token.
    if provider == "mitre_atlas":
        from app.integrations.providers.mitre_atlas import resolve_atlas_source_url

        source_url = ""
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
                source_url = (extra.get("source_url") or "").strip()
            except Exception as exc:
                logger.warning(
                    "Failed to extract source_url from extra_config for '%s' "
                    "(mitre_atlas): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        return resolve_atlas_source_url(source_url)

    if not token:
        # Platform-managed integrations: generate a short-lived EdDSA JWT
        # for the current user, signed with the Ed25519 key derived from
        # SECRET_KEY.  The target platform validates via /xtm/auth/jwks.
        if integ.auth_type == "platform_managed":
            if not user_id or user_id == "anonymous":
                logger.warning(
                    "Platform-managed integration '%s' (%s): no user context "
                    "available — cannot generate JWT",
                    integ.name,
                    integ.provider,
                )
                return None
            # The base_url the calls go to; the JWT is addressed to the
            # platform's registered identity when its registration vouches for
            # that URL.
            _base_url = ""
            if integ.extra_config_enc:
                try:
                    _extra = await _reveal_extra(integ)
                    _base_url = (_extra.get("base_url") or "").rstrip("/")
                except Exception as exc:
                    logger.debug(
                        "Platform-managed integration '%s' (%s): failed to parse base_url from extra_config: %s",
                        integ.name,
                        integ.provider,
                        format_exception_message(exc),
                    )
            if not _base_url:
                logger.warning(
                    "Platform-managed integration '%s' (%s): base_url (audience) is missing in extra_config — cannot generate JWT",
                    integ.name,
                    integ.provider,
                )
                return None
            try:
                from app.core.platform_identity import platform_token_audience

                _audience_url = await platform_token_audience(db, integ.id, _base_url)
                token = await _generate_platform_jwt(db, user_id, _audience_url)
            except Exception as exc:
                logger.warning(
                    "Platform-managed integration '%s' (%s): JWT generation failed: %s",
                    integ.name,
                    integ.provider,
                    format_exception_message(exc),
                )
                return None
            if not token:
                return None
        elif integ.auth_type == "none":
            # No-auth integrations (e.g. test_pipeline) don't need a token
            token = "__no_auth__"  # noqa: S105
            # Sentinel marker for no-auth integrations, not a real secret.
        elif integ.auth_type == "client_credentials":
            # OAuth2 client credentials (OpenAPI integrations): no stored
            # token — the provider fetches and caches the access token per
            # call from the client id / secret / token URL. A non-secret
            # marker tells the build loop the credential is present.
            if not (integ.client_id_enc and integ.client_secret_enc):
                logger.warning(
                    "Integration '%s' (%s) uses client credentials but has no "
                    "client ID / secret stored — skipping its tools",
                    integ.name,
                    integ.provider,
                )
                return None
            token = _OPENAPI_GRANT_TOKEN
        elif integ.auth_type == "oauth_password":
            # OAuth2 password grant (OpenAPI integrations): the username and
            # password are stored, the access token is fetched per call.
            if not integ.api_key_enc:
                logger.warning(
                    "Integration '%s' (%s) uses the OAuth password grant but has "
                    "no username / password stored — skipping its tools",
                    integ.name,
                    integ.provider,
                )
                return None
            token = _OPENAPI_GRANT_TOKEN
        elif provider == OPENAPI_PROVIDER and integ.auth_type == "api_key":
            # The API key of an OpenAPI integration is optional: without one
            # its calls carry no credential.
            token = "__no_auth__"  # noqa: S105
        else:
            return None

    # DocuSign (OAuth): the environment and the account ride with the access
    # token so the provider picks the OAuth host and the account's base URI.
    if provider == "docusign":
        extra = {}
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt extra_config for '%s' (docusign): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        from app.integrations.providers.docusign import compound_credential

        return compound_credential(token or "", extra, jwt_grant=False)

    # Jira: prepend cloud_id from extra_config so tools know which site to target
    if provider == "jira" and integ.extra_config_enc:
        try:
            extra = await _reveal_extra(integ)
            cloud_id = extra.get("cloud_id", "").strip()
            if cloud_id:
                return f"{cloud_id}||{token}"
            logger.warning(
                "Integration '%s' (jira) extra_config exists but cloud_id is empty. "
                "Re-authorize the integration to fetch accessible resources.",
                integ.name,
            )
        except Exception as exc:
            logger.warning(
                "Failed to extract cloud_id from extra_config for '%s' (jira): %s",
                integ.name,
                format_exception_message(exc),
            )

    # Cortex XSOAR / Cortex XDR: assemble base_url||api_key||api_key_id||auth_mode
    # from extra_config. Both use the Cortex platform auth model (API key +
    # API key ID). For XSOAR, api_key_id empty ⇒ XSOAR 6 (on-prem); XDR always
    # requires api_key_id. auth_mode is 'standard' or 'advanced' (the latter
    # triggers per-request nonce/timestamp hashing).
    if provider in ("xsoar", "xdr"):
        if not integ.extra_config_enc:
            logger.warning(
                "Integration '%s' (%s) has no extra_config — base_url is missing. "
                "Edit the integration and set the base URL.",
                integ.name,
                provider,
            )
            return None
        try:
            extra = await _reveal_extra(integ)
            base_url = extra.get("base_url", "").rstrip("/")
            api_key_id = (extra.get("api_key_id") or "").strip()
            auth_mode = (extra.get("auth_mode") or "standard").strip() or "standard"
            if base_url:
                # XDR mandates the API Key ID (x-xdr-auth-id); without it
                # xdr.build_tools early-returns zero tools. Surface the
                # misconfiguration here (consistent with integration_testing.py)
                # instead of silently producing an unusable integration. XSOAR
                # leaves api_key_id empty for v6/on-prem, so this guard is
                # XDR-only.
                if provider == "xdr" and not api_key_id:
                    logger.warning(
                        "Integration '%s' (xdr) is missing api_key_id "
                        "(x-xdr-auth-id) in extra_config — Cortex XDR requires it. "
                        "Edit the integration and set the API Key ID.",
                        integ.name,
                    )
                    return None
                return f"{base_url}||{token}||{api_key_id}||{auth_mode}"
            logger.warning(
                "Integration '%s' (%s) extra_config exists but base_url is empty. "
                "Edit the integration and set the base URL.",
                integ.name,
                provider,
            )
        except Exception as exc:
            logger.warning(
                "Failed to extract config from extra_config for '%s' (%s): %s",
                integ.name,
                provider,
                format_exception_message(exc),
            )
        return None

    # IBM QRadar: assemble base_url||token||api_version from extra_config. The
    # SEC token lives in api_key_enc (already decrypted into ``token``); the
    # console base URL and the pinned API version live in extra_config.
    if provider == "qradar":
        if not integ.extra_config_enc:
            logger.warning(
                "Integration '%s' (qradar) has no extra_config — base_url is missing. "
                "Edit the integration and set the base URL.",
                integ.name,
            )
            return None
        try:
            extra = await _reveal_extra(integ)
            base_url = extra.get("base_url", "").rstrip("/")
            api_version = (extra.get("api_version") or "").strip()
            if base_url:
                return f"{base_url}||{token}||{api_version}"
            logger.warning(
                "Integration '%s' (qradar) extra_config exists but base_url is empty. "
                "Edit the integration and set the base URL.",
                integ.name,
            )
        except Exception as exc:
            logger.warning(
                "Failed to extract config from extra_config for '%s' (qradar): %s",
                integ.name,
                format_exception_message(exc),
            )
        return None

    # --- Fortinet FortiSIEM (HTTP Basic with an org/username login) ---
    # The provider needs the compound ``base_url||organization||username||password``
    # to build the Basic auth header (FortiSIEM logins are ``org/user``, e.g.
    # ``super/apiuser``). The password (the credential) lives in api_key_enc
    # (already decrypted into ``token``); the Supervisor base URL, the optional
    # organization, and the username live in extra_config.
    if provider == "fortisiem":
        if not integ.extra_config_enc:
            logger.warning(
                "Integration '%s' (fortisiem) has no extra_config — base_url is "
                "missing. Edit the integration and set the Supervisor base URL.",
                integ.name,
            )
            return None
        try:
            extra = await _reveal_extra(integ)
            base_url = (extra.get("base_url") or "").strip().rstrip("/")
            organization = (extra.get("organization") or "").strip()
            username = (extra.get("username") or "").strip()
            if base_url:
                return f"{base_url}||{organization}||{username}||{token}"
            logger.warning(
                "Integration '%s' (fortisiem) extra_config exists but base_url is "
                "empty. Edit the integration and set the Supervisor base URL.",
                integ.name,
            )
        except Exception as exc:
            logger.warning(
                "Failed to extract config from extra_config for '%s' (fortisiem): %s",
                integ.name,
                format_exception_message(exc),
            )
        return None

    # WhatsApp: the Cloud API addresses one business phone number, whose id
    # lives in extra_config; the system user access token is api_key_enc.
    # The tools parse ``phone_number_id||access_token`` (like whatsapp_bot).
    # Its ``backend`` extra field selects the linked-device gateway instead:
    # the token is then the gateway's channel token and the tools parse
    # ``linked_device_gateway||vendor||base_url||channel_token``.
    if provider == "whatsapp":
        phone_number_id = ""
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
                if str(extra.get("backend") or "").strip() == WHATSAPP_GATEWAY_BACKEND:
                    return whatsapp_gateway_token(
                        str(extra.get("gateway_vendor") or "").strip(),
                        str(extra.get("base_url") or "").strip(),
                        token,
                    )
                phone_number_id = str(extra.get("phone_number_id") or "").strip()
            except Exception as exc:
                logger.warning(
                    "Failed to extract phone_number_id from extra_config for '%s' (whatsapp): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        if not phone_number_id:
            logger.warning(
                "Integration '%s' (whatsapp) has no phone_number_id. Edit the integration "
                "and set the phone number id shown in WhatsApp Manager > API Setup.",
                integ.name,
            )
            return None
        return f"{phone_number_id}||{token}"

    # PagerDuty: the REST API key is api_key_enc; the service region (us/eu)
    # and the From email PagerDuty requires on incident writes live in
    # extra_config. The tools parse ``region||from_email||api_key``; a blank
    # from_email still gives the read tools.
    if provider == "pagerduty":
        region = from_email = ""
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
                region = str(extra.get("region") or "").strip().lower()
                from_email = str(extra.get("from_email") or "").strip()
            except Exception as exc:
                logger.warning(
                    "Failed to extract config from extra_config for '%s' (pagerduty): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        return f"{region}||{from_email}||{token}"

    # Zendesk: the API token is api_key_enc; the account subdomain and the
    # agent email the token belongs to live in extra_config. The tools parse
    # ``subdomain||email||api_token`` and build nothing when one is missing.
    if provider == "zendesk":
        subdomain = email = ""
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
                subdomain = str(extra.get("subdomain") or "").strip()
                email = str(extra.get("email") or "").strip()
            except Exception as exc:
                logger.warning(
                    "Failed to extract config from extra_config for '%s' (zendesk): %s",
                    integ.name,
                    format_exception_message(exc),
                )
        if not subdomain or not email:
            logger.warning(
                "Integration '%s' (zendesk) needs its subdomain and the agent email. "
                "Edit the integration and set both.",
                integ.name,
            )
        return f"{subdomain}||{email}||{token}"

    # Splunk: assemble base_url||username||token from extra_config. The secret
    # (a Splunk authentication token for Bearer auth, or the password for HTTP
    # Basic auth) lives in api_key_enc (already decrypted into ``token``); the
    # management base URL and the optional username live in extra_config. A blank
    # username selects Bearer-token auth (recommended; Splunk Cloud + ES); a set
    # username selects HTTP Basic auth (on-prem Splunk Enterprise).
    if provider == "splunk":
        if not integ.extra_config_enc:
            logger.warning(
                "Integration '%s' (splunk) has no extra_config — base_url is missing. "
                "Edit the integration and set the base URL (e.g. "
                "https://splunk.example.com:8089).",
                integ.name,
            )
            return None
        try:
            extra = await _reveal_extra(integ)
            base_url = (extra.get("base_url") or "").strip().rstrip("/")
            username = (extra.get("username") or "").strip()
            if base_url:
                return f"{base_url}||{username}||{token}"
            logger.warning(
                "Integration '%s' (splunk) extra_config exists but base_url is empty. "
                "Edit the integration and set the base URL.",
                integ.name,
            )
        except Exception as exc:
            logger.warning(
                "Failed to extract config from extra_config for '%s' (splunk): %s",
                integ.name,
                format_exception_message(exc),
            )
        return None

    # Infoblox / Rapid7 / Recorded Future / Sekoia.io / Sublime Security /
    # Flashpoint: base_url is OPTIONAL - each provider defaults an empty base
    # URL to its own host (Infoblox -> https://csp.infoblox.com, Rapid7 ->
    # https://us.api.insight.rapid7.com, Recorded Future ->
    # https://api.recordedfuture.com, Sekoia.io -> https://api.sekoia.io,
    # Sublime -> https://platform.sublime.security, Flashpoint ->
    # https://api.flashpoint.io), so a missing/empty base_url is a valid
    # config (not a misconfiguration). Return ``base_url||token`` (or
    # ``||token`` for the default) WITHOUT the "set the base URL" warning the
    # required-base_url providers below emit. Picus also defaults its base URL,
    # and so does XTM Hub (https://hub.filigran.io), whose token is the
    # credential-less ``__no_auth__`` marker. GitLab defaults to
    # https://gitlab.com; a self-managed instance sets its URL.
    if provider in (
        "gitlab",
        "infoblox",
        "rapid7",
        "recorded_future",
        "sekoia",
        "picus",
        "sublime",
        "flashpoint",
        "xtm_hub",
    ):
        base_url = ""
        if integ.extra_config_enc:
            try:
                extra = await _reveal_extra(integ)
                # Strip before rstrip so a pasted base_url with surrounding
                # whitespace normalises cleanly (matches integration_testing).
                base_url = (extra.get("base_url") or "").strip().rstrip("/")
            except Exception as exc:
                logger.warning(
                    "Failed to extract base_url from extra_config for '%s' (%s): %s",
                    integ.name,
                    provider,
                    format_exception_message(exc),
                )
        return f"{base_url}||{token}"

    # Metabase / OpenCTI / OpenAEV / n8n / Grafana / YouTrack / Tanium /
    # SentinelOne / HarfangLab / AttackIQ / LogRhythm / MISP / TheHive /
    # Mattermost: prepend base_url from extra_config (the tools parse
    # ``base_url||token``). These providers
    # REQUIRE a base URL, so a missing one is a real misconfiguration and
    # warrants the warning below.
    if provider in (
        "metabase",
        "opencti",
        "openaev",
        "opencrq",
        "n8n",
        "grafana",
        "youtrack",
        "tanium",
        "sentinelone",
        "harfanglab",
        "attackiq",
        "logrhythm",
        "misp",
        "thehive",
        "mattermost",
    ):
        # When the base URL is missing we still preserve the ``||`` delimiter
        # (returning ``||token`` rather than a bare ``token``). Every provider in
        # this block parses ``base_url||token`` via ``parse_compound_token`` and
        # checks the base URL first; a bare token would otherwise be mis-parsed
        # as the base URL (with an empty token), producing a misleading
        # "token not configured" error instead of the accurate "base URL is
        # missing" one.
        if not integ.extra_config_enc:
            logger.warning(
                "Integration '%s' (%s) has no extra_config — base_url is missing. "
                "Edit the integration and set the base URL.",
                integ.name,
                provider,
            )
            return f"||{token}"
        try:
            extra = await _reveal_extra(integ)
            # Strip first: a whitespace-only base_url (or a pasted URL with
            # surrounding spaces) is otherwise truthy, bypassing the "base_url
            # is empty" warning below and embedding the spaces into the compound
            # token. Matches the connectivity test, which also strips.
            base_url = extra.get("base_url", "").strip().rstrip("/")
            if base_url:
                return f"{base_url}||{token}"
            logger.warning(
                "Integration '%s' (%s) extra_config exists but base_url is empty. "
                "Edit the integration and set the base URL.",
                integ.name,
                provider,
            )
            return f"||{token}"
        except Exception as exc:
            logger.warning(
                "Failed to extract base_url from extra_config for '%s' (%s): %s",
                integ.name,
                provider,
                format_exception_message(exc),
            )
            return f"||{token}"

    # Filigran AI: 2 base URLs
    # Token format: "base_url_stix||base_url_ttp"
    # Env vars FILIGRAN_AI_BASE_URL_STIX / FILIGRAN_AI_BASE_URL_TTP override DB values.
    if provider == "filigran_ai":
        from app.core.config import settings as _settings

        if not integ.extra_config_enc:
            logger.warning(
                "Integration '%s' (filigran_ai) has no extra_config — base URLs are missing.",
                integ.name,
            )
            return token
        try:
            extra = await _reveal_extra(integ)
            extra_stix_raw = extra.get("base_url_stix", "")
            extra_ttp_raw = extra.get("base_url_ttp", "")
            env_stix_raw = _settings.filigran_ai_base_url_stix
            env_ttp_raw = _settings.filigran_ai_base_url_ttp

            # Normalise trailing slashes BEFORE the divergence check so a
            # cosmetic difference (env ``https://host/`` vs integration row
            # ``https://host``) does not spam an INFO line on every
            # ``_get_token`` invocation — the effective ``base_url_*``
            # values below ``rstrip("/")`` both sides anyway, so equal
            # post-strip URLs really do agree.
            env_stix_normalized = env_stix_raw.rstrip("/")
            env_ttp_normalized = env_ttp_raw.rstrip("/")
            extra_stix_normalized = extra_stix_raw.rstrip("/")
            extra_ttp_normalized = extra_ttp_raw.rstrip("/")

            base_url_stix = env_stix_normalized or extra_stix_normalized
            base_url_ttp = env_ttp_normalized or extra_ttp_normalized

            # When an env-level override is set AND the integration row
            # ALSO carries a (different) value, the env wins silently.
            # Without this log the operator who configured the
            # integration UI sees no indication that their value was
            # ignored.  We log per-URL at INFO so the message is visible
            # in normal worker output without becoming noise in steady
            # state (only fires when a real divergence exists post
            # trailing-slash normalisation).
            if (
                env_stix_normalized
                and extra_stix_normalized
                and env_stix_normalized != extra_stix_normalized
            ):
                logger.info(
                    "Integration '%s' (filigran_ai) base_url_stix overridden by "
                    "FILIGRAN_AI_BASE_URL_STIX env: integration row %r → env %r",
                    integ.name,
                    extra_stix_raw,
                    env_stix_raw,
                )
            if (
                env_ttp_normalized
                and extra_ttp_normalized
                and env_ttp_normalized != extra_ttp_normalized
            ):
                logger.info(
                    "Integration '%s' (filigran_ai) base_url_ttp overridden by "
                    "FILIGRAN_AI_BASE_URL_TTP env: integration row %r → env %r",
                    integ.name,
                    extra_ttp_raw,
                    env_ttp_raw,
                )

            if base_url_stix or base_url_ttp:
                return f"{base_url_stix}||{base_url_ttp}"
            logger.warning(
                "Integration '%s' (filigran_ai) extra_config exists but base URLs are empty.",
                integ.name,
            )
        except Exception as exc:
            logger.warning(
                "Failed to extract extra_config for '%s' (filigran_ai): %s",
                integ.name,
                format_exception_message(exc),
            )

    # Wordsmith: a region host from extra_config is prepended as base_url
    if provider == "wordsmith" and integ.extra_config_enc:
        try:
            extra = await _reveal_extra(integ)
            region = extra.get("region", "").strip()
            if region:
                from app.integrations.providers.wordsmith import resolve_base_url as wordsmith_base_url

                return f"{wordsmith_base_url(region)}||{token}"
        except Exception as exc:
            logger.warning(
                "Failed to extract region from extra_config for '%s' (%s): %s",
                integ.name,
                provider,
                format_exception_message(exc),
            )

    # Drata: resolve region from extra_config → prepend base_url
    if provider == "drata" and integ.extra_config_enc:
        try:
            extra = await _reveal_extra(integ)
            region = extra.get("region", "").strip()
            if region:
                from app.integrations.providers.drata import resolve_base_url

                base_url = resolve_base_url(region)
                return f"{base_url}||{token}"
        except Exception as exc:
            logger.warning(
                "Failed to extract region from extra_config for '%s' (%s): %s",
                integ.name,
                provider,
                format_exception_message(exc),
            )

    # Pennylane: a firm token names the company of each request (X-Company-Id).
    if provider == "pennylane" and integ.extra_config_enc:
        try:
            extra = await _reveal_extra(integ)
            company_id = str(extra.get("company_id") or "").strip()
            if company_id:
                return f"{company_id}||{token}"
        except Exception as exc:
            logger.warning(
                "Failed to extract company_id from extra_config for '%s' (%s): %s",
                integ.name,
                provider,
                format_exception_message(exc),
            )

    # LinkedIn: prepend account_type and organization_id from extra_config
    if provider == "linkedin" and integ.extra_config_enc:
        try:
            extra = await _reveal_extra(integ)
            account_type = extra.get("account_type", "personal").strip()
            org_id = extra.get("organization_id", "").strip()
            return f"{token}||{account_type}||{org_id}"
        except Exception as exc:
            logger.warning(
                "Failed to extract account_type from extra_config for '%s' (linkedin): %s",
                integ.name,
                format_exception_message(exc),
            )

    # Facebook: prepend account_type, page_id, and page_access_token from extra_config
    if provider == "facebook" and integ.extra_config_enc:
        try:
            extra = await _reveal_extra(integ)
            account_type = extra.get("account_type", "personal").strip()
            fb_page_id = extra.get("page_id", "").strip()
            fb_page_token = extra.get("page_access_token", "").strip()
            return f"{token}||{account_type}||{fb_page_id}||{fb_page_token}"
        except Exception as exc:
            logger.warning(
                "Failed to extract account_type from extra_config for '%s' (facebook): %s",
                integ.name,
                format_exception_message(exc),
            )

    # Bluesky: prepend handle and optional pds_url from extra_config
    if provider == "bluesky" and integ.extra_config_enc:
        try:
            extra = await _reveal_extra(integ)
            bsky_handle = extra.get("handle", "").strip()
            pds_url = extra.get("pds_url", "").strip()
            if bsky_handle:
                compound = f"{bsky_handle}||{token}"
                if pds_url:
                    compound += f"||{pds_url}"
                return compound
        except Exception as exc:
            logger.warning(
                "Failed to extract handle from extra_config for '%s' (bluesky): %s",
                integ.name,
                format_exception_message(exc),
            )

    # Providers that prepend a single extra_config field to the token
    extra_field_providers: dict[str, str] = {
        "credly": "organization_id",
        "concord": "organization_id",
        # DealHub: the instance host (api.dealhub.io when unset) as ``host||token``.
        "dealhub": "instance_host",
        "google_search": "search_engine_id",
        "hibob": "service_user_id",
        # HackerOne uses HTTP Basic auth: the API token *identifier* (username)
        # is prepended to the token *value* as ``api_username||api_token``.
        "hackerone": "api_username",
        # DomainTools signs every request with HMAC-SHA256 over
        # ``api_username + timestamp + uri_path`` keyed by the API key; the
        # username is prepended as ``api_username||api_key``.
        "domaintools": "api_username",
        # Intel 471 (Titan) uses HTTP Basic auth: the account email is the
        # username and the Titan API key is the password, assembled as
        # ``api_username||api_key`` (mirrors hackerone).
        "intel471": "api_username",
        # Proofpoint TAP uses HTTP Basic auth: the service principal is the
        # username and the secret is the password, assembled as
        # ``principal||secret`` (mirrors hackerone).
        "proofpoint": "principal",
    }
    extra_field = extra_field_providers.get(provider)
    if extra_field and integ.extra_config_enc:
        try:
            extra = await _reveal_extra(integ)
            field_value = extra.get(extra_field, "").strip()
            if field_value:
                return f"{field_value}||{token}"
        except Exception as exc:
            logger.warning(
                "Failed to extract %s from extra_config for '%s' (%s): %s",
                extra_field,
                integ.name,
                provider,
                format_exception_message(exc),
            )

    return token


async def _refresh_by_id(db: AsyncSession | None, integration_id: str) -> str | None:
    """Refresh an OAuth token by holder reference and return the new access token.

    *integration_id* is ``integration_user_credentials.holder_ref``: an
    integration's id, or a person's credential on one.

    Uses an independent DB session to avoid ``InvalidRequestError`` when
    multiple integration tools run concurrently (via ``asyncio.gather``)
    and several of them trigger a 401-based token refresh at the same time.
    ``AsyncSession`` is single-connection and forbids concurrent operations.
    """
    from app.core.database import async_session

    async with async_session() as fresh_db:
        integ = await load_token_holder(fresh_db, integration_id)
        if not integ:
            return None
        # The upstream refused the credential: a referenced secret rotated in its
        # vault is read again by the refresh below and the next tool build.
        await invalidate_entity_secrets(integration_of(integ), REFERENCE_COLUMNS["integrations"])
        return await _try_inline_refresh(fresh_db, integ)


def _make_refresh_fn(
    db: AsyncSession, integration_id: str
) -> Callable[[], Awaitable[str | None]]:
    """Create a refresh callback bound to a specific integration."""

    async def _refresh() -> str | None:
        return await _refresh_by_id(db, integration_id)

    return _refresh


def oauth_refresh_fn(integration_id: str) -> Callable[[], Awaitable[str | None]]:
    """A refresh callback for one OAuth integration, outside any tool session.

    The refresh runs on a session of its own, as the tools' does, so the
    caller hands in no session.
    """

    async def _refresh() -> str | None:
        return await _refresh_by_id(None, integration_id)

    return _refresh


def _make_platform_jwt_refresh_fn(
    integration_id: str, user_id: str | None
) -> Callable[[], Awaitable[str | None]]:
    """Create a refresh callback that RE-MINTS a platform-managed JWT.

    Platform-managed integrations (OpenAEV / OpenCTI / OpenCRQ) authenticate
    with a short-lived cross-platform EdDSA JWT minted once at tool-build time
    (``_XTM_JWT_TTL_MINUTES`` = 10 min). A long autonomous run outlives that
    TTL, after which every call 401s. Unlike OAuth there is no refresh token to
    redeem - the recovery is to MINT A NEW JWT for the same user + audience.
    Bound onto the provider headers via ``AutoRefreshClient``, this turns a
    mid-run 401 into a transparent re-mint + one retry (WS2).

    Returns the bare JWT (``AutoRefreshClient`` prepends ``Bearer``), or
    ``None`` when it cannot be minted (no user context, integration gone, or no
    audience base_url), in which case the original 401/403 is surfaced. Uses its
    own DB session because integration tools run concurrently and
    ``AsyncSession`` is single-connection (same rationale as
    :func:`_refresh_by_id`).
    """

    async def _refresh() -> str | None:
        if not user_id or user_id == "anonymous":
            return None
        from app.core.database import async_session
        from app.core.platform_identity import platform_token_audience

        async with async_session() as fresh_db:
            result = await fresh_db.execute(
                select(Integration).where(Integration.id == uuid.UUID(integration_id))
            )
            integ = result.scalar_one_or_none()
            if not integ or integ.auth_type != "platform_managed":
                return None
            base_url = ""
            if integ.extra_config_enc:
                try:
                    extra = await _reveal_extra(integ)
                    base_url = (extra.get("base_url") or "").rstrip("/")
                except Exception as exc:
                    logger.debug(
                        "Platform JWT re-mint: failed to parse base_url for %s: %s",
                        integration_id,
                        format_exception_message(exc),
                    )
                    return None
            if not base_url:
                return None
            audience_url = await platform_token_audience(fresh_db, integ.id, base_url)
            return await _generate_platform_jwt(fresh_db, user_id, audience_url)

    return _refresh


# Per-integration cap on a proactive parallel OAuth refresh (P3.1).
# The refresh HTTP POST itself is capped at 30 s inside
# ``_do_inline_refresh``; this adds headroom for the Redis refresh lock
# (+2 s lock-wait path) while guaranteeing the whole parallel pass can
# never exceed one refresh worst case — previously N expiring integrations
# refreshed SEQUENTIALLY (N x up to 30 s) in the pre-loop setup phase.
_PARALLEL_REFRESH_TIMEOUT_SECS = 45.0


def _needs_proactive_refresh(integ: Any) -> bool:
    """True when a bound OAuth integration should be refreshed up-front.

    Mirrors the inline-refresh decision in :func:`_get_token` (expiring
    within ``_REFRESH_BUFFER_MINUTES``).  Unknown expiry (``token_expires_at``
    is NULL) is deliberately NOT treated as "refresh now": Notion omits
    ``expires_in``, and treating NULL as stale re-hit the token endpoint on
    every chat turn.  The scheduler stamps a synthetic TTL via
    ``apply_oauth_token_expiry`` so the next sweep waits ~45 minutes.
    """
    if integ.auth_type != "oauth" or not integ.access_token_enc:
        return False
    if not integ.refresh_token_enc:
        return False
    if integ.token_expires_at is None:
        return False
    buffer = datetime.now(UTC) + timedelta(minutes=_REFRESH_BUFFER_MINUTES)
    return integ.token_expires_at < buffer


async def _parallel_refresh_expiring_tokens(
    db: AsyncSession,
    rows: list[Any],
) -> tuple[dict[str, str], set[str]]:
    """Refresh every expiring bound OAuth token CONCURRENTLY (P3.1).

    Returns ``(prefetched, attempted)``: ``prefetched`` maps integration id
    (str) to the fresh access token; ``attempted`` contains every id the
    pass tried (successful or not) so :func:`_get_token` never re-refreshes
    sequentially.  Each refresh runs on its own DB session
    (:func:`_refresh_by_id`), so the gather is safe for the shared caller
    session, and each is capped at ``_PARALLEL_REFRESH_TIMEOUT_SECS``.
    The per-integration Redis refresh lock inside ``_try_inline_refresh``
    still serializes racing refreshes across processes (rotating refresh
    tokens stay safe).

    *rows* are token holders (an integration, or a person's credential on
    one); results are keyed by the id of the integration they serve.
    """
    candidates = [r for r in rows if _needs_proactive_refresh(r)]
    if not candidates:
        return {}, set()

    logger.info(
        "Proactively refreshing %d expiring OAuth token(s) in parallel: %s",
        len(candidates),
        [f"{c.provider}:{c.id}" for c in candidates],
    )

    async def _one(integ: Any) -> str | None:
        return await asyncio.wait_for(
            _refresh_by_id(db, holder_ref(integ)),
            timeout=_PARALLEL_REFRESH_TIMEOUT_SECS,
        )

    results = await asyncio.gather(
        *(_one(c) for c in candidates), return_exceptions=True
    )
    prefetched: dict[str, str] = {}
    attempted: set[str] = set()
    for integ, res in zip(candidates, results, strict=True):
        attempted.add(binding_integration_id(integ))
        if isinstance(res, (KeyboardInterrupt, SystemExit)):
            # Terminating signals must never be rationalised into a
            # "refresh failed" warning (same convention as the agent
            # loop's parallel tool gather).
            raise res
        if isinstance(res, BaseException):
            logger.warning(
                "Parallel token refresh failed for %s (%s): %s",
                integ.name,
                integ.provider,
                res,
            )
        elif res:
            prefetched[binding_integration_id(integ)] = res
    return prefetched, attempted


def _make_hitl_approval_fn(run_id: str) -> Callable[[str], Awaitable[bool]]:
    """Create a callback that verifies a human approved a specific action.

    The returned callable takes an action code (an opaque short digest of the
    exact operation being requested) and returns True only when this run's
    ``human_input_history`` records a human approving that code, as decided by
    ``app.services.hitl_consent``. Binding the approval to a code means a human
    who approved merging A into B has not thereby approved merging C into D,
    and re-using a stale approval for a different operation fails.

    The engine clears ``human_input_question`` / ``human_input_response`` when a
    run resumes, so ``human_input_history`` is the only durable record of who
    approved what — which is why this reads the database instead of trusting the
    model to report the answer truthfully.

    Runs on its own ``async_session`` for the same reason as
    ``_make_persist_file_fn``: integration tools execute concurrently and
    ``AsyncSession`` is single-connection. Fails closed (returns False) on any
    lookup error.
    """

    async def _approved(action_code: str) -> bool:
        code = (action_code or "").strip()
        if not code:
            return False
        try:
            from sqlalchemy import select

            from app.core.database import async_session
            from app.models.assignment import AssignmentRun

            async with async_session() as own_db:
                result = await own_db.execute(
                    select(AssignmentRun.human_input_history).where(
                        AssignmentRun.id == run_id
                    )
                )
                history = result.scalar_one_or_none() or []
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "HITL approval lookup failed for run %s: %s",
                run_id,
                format_exception_message(exc),
            )
            return False

        return hitl_history_approves(history, code)

    return _approved


def _make_persist_file_fn(
    context: dict[str, Any],
    executor: Any | None = None,
) -> Callable[[str, bytes, str], Awaitable[Any]]:
    """Create a callback that persists a tool-generated file as a download.

    Bound to the request's identity (user / conversation / run) so the file
    becomes a real ``FileAttachment`` and surfaces as a download card — the same
    mechanism the ``generate_file`` built-in tool uses. Returns a ``FileRef`` on
    success, or ``None`` on failure (the caller then falls back to an inline,
    LLM-only file reference).

    The ref is also REGISTERED in *executor*'s ``file_context`` when the loop
    bound one (the composite propagates the turn's FileContext onto every inner
    executor), exactly as ``generate_file`` / ``persist_tool_artifact`` do:
    that context is the ONE source every consumer reads to learn what a
    run produced - the chat paths attach the files new to it to the assistant
    message (which is what makes the ``[[FILE:id]]`` marker the tool returns
    render as a chip), and a background task records them on its row so the
    parent can read them. A persisted row the context never saw is a marker
    the chat strips and, in a delegated task, a file nobody can reach. It is
    registered as FETCHED: a download the agent read is shown by the
    reply that names it, never as a chip under every reply of an investigation.

    The write runs on its own **independent** ``async_session`` (the request
    session is intentionally not threaded in — hence no ``db`` parameter):
    integration tools execute concurrently (``asyncio.gather``
    in the agent loop) and ``AsyncSession`` is single-connection — two
    simultaneous downloads sharing the request session would raise
    ``InvalidRequestError`` (same rationale as ``_refresh_by_id``). The user
    message (and thus the conversation / assignment-run row) is committed before
    the agent loop runs, so the ``FileAttachment`` foreign keys resolve in the
    fresh session, and committing here makes the artifact immediately
    resolvable by the download endpoint.
    """

    async def _persist(filename: str, raw_bytes: bytes, content_type: str) -> Any:
        from app.core.database import async_session
        from app.llm.base import FileRef
        from app.services.builtin_tools._shared import _persist_generated_file

        try:
            async with async_session() as own_db:
                file_att = await _persist_generated_file(
                    db=own_db,
                    user_id=context.get("user_id") or "",
                    filename=filename,
                    raw_bytes=raw_bytes,
                    content_type=content_type,
                    conversation_id=context.get("conversation_id"),
                    assignment_run_id=context.get("run_id"),
                    source="agent_download",
                )
                # Build the FileRef while the row's attributes are still loaded
                # (post-flush, pre-commit) to avoid an expired-attribute lazy
                # load after the session closes, then commit so the download
                # card can resolve the artifact.
                file_ref = FileRef.from_attachment(file_att)
                await own_db.commit()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Failed to persist generated file '%s' for download: %s",
                filename,
                format_exception_message(exc),
            )
            return None
        register_fetched_file(getattr(executor, "file_context", None), file_ref)
        return file_ref

    return _persist


def _make_github_self_action_recorder(
    integration_id: str,
) -> Callable[[str, int], Awaitable[None]]:
    """Create a self-action recorder bound to a specific GitHub integration.

    Passed to the GitHub tool builder so its write tools can flag the
    ``(integration, repo, issue/PR number)`` they just mutated. The webhook
    receiver then drops the resulting echo event for assignments that opted into
    ``ignore_self_triggered_events``. See ``services/github_echo.py``.
    """

    async def _record(repo: str, number: int) -> None:
        from app.services.github_echo import record_self_action

        await record_self_action(integration_id, repo, number)

    return _record


def _make_record_pull_request_fn(
    context: dict[str, Any],
) -> Callable[..., Awaitable[str]] | None:
    """The ``record_pull_request`` callback a code host's create-PR tool gets.

    Bound to the agent and the person the run acts for: the pull request is
    named on the work item under the item's own write rules, as the agent's
    ``update_work_item`` would. ``None`` without an agent (no work item to
    write as), so the tool then offers no ``work_item_id``.
    """
    agent_id = context.get("agent_id")
    if not agent_id:
        return None
    user_id = context.get("user_id") or None

    async def _record(
        work_item_id: str,
        repo: str,
        number: int,
        head_branch: str | None = None,
        head_sha: str | None = None,
        url: str | None = None,
        draft: bool | None = None,
    ) -> str:
        from app.core.database import async_session
        from app.models.agent import Agent
        from app.services.company_managed_writes import (
            CompanyManagedWriteRefusedError,
            agent_writer,
        )
        from app.services.work_item import get_work_item
        from app.services.work_item_preflight import work_item_write_refusal
        from app.services.work_item_pull_requests import record_pull_request

        try:
            # A cancelled item takes no pull request: the run is told to stop (#11554).
            refusal = work_item_write_refusal(await get_work_item(work_item_id, user_id=user_id))
            if refusal:
                return f"Not named on the work item: {refusal}"
            async with async_session() as db:
                agent = await db.get(Agent, uuid.UUID(str(agent_id)))
            writer = agent_writer(
                is_company_managed=bool(agent and agent.is_company_managed),
                agent_id=uuid.UUID(str(agent_id)),
            )
            item = await record_pull_request(
                work_item_id,
                user_id=user_id,
                writer=writer,
                repo=repo,
                number=number,
                head_branch=head_branch,
                head_sha=head_sha,
                url=url,
                draft=draft,
            )
        except (ValueError, CompanyManagedWriteRefusedError) as exc:
            return f"Not named on work item {work_item_id}: {exc}"
        except Exception as exc:
            logger.warning(
                "Could not name %s#%s on work item %s: %s",
                repo,
                number,
                work_item_id,
                format_exception_message(exc),
            )
            return f"Not named on work item {work_item_id}: it could not be updated"
        if item is None:
            return f"Not named on work item {work_item_id}: not found or not writable"
        return (
            f"Named on work item {work_item_id}: it is dispatched to its owner again "
            "when this PR's CI completes; the items blocked on it wait for this PR's "
            "deployment when the platform observes this repository's deployments"
        )

    return _record


def _make_work_item_write_check_fn(context: dict[str, Any]) -> Callable[[str], Awaitable[str | None]] | None:
    """The ``check_work_item`` callback create_github_pr calls before it writes (#11554).

    Answers the refusal text for a cancelled work item, ``None`` to go ahead
    (an open, done or unreadable item: a failed read never blocks). ``None``
    without an agent, like :func:`_make_record_pull_request_fn`.
    """
    if not context.get("agent_id"):
        return None
    user_id = context.get("user_id") or None

    async def _check(work_item_id: str) -> str | None:
        from app.services.work_item import get_work_item
        from app.services.work_item_preflight import work_item_write_refusal

        try:
            return work_item_write_refusal(await get_work_item(work_item_id, user_id=user_id))
        except Exception as exc:
            logger.warning(
                "Could not read work item %s before a write: %s", work_item_id, format_exception_message(exc)
            )
            return None

    return _check


def _make_park_full_ci_fn(context: dict[str, Any]) -> Callable[..., Awaitable[bool]] | None:
    """The ``park_full_ci`` callback merge_github_pr gets (#12069).

    Bound to the person the run acts for: their credential runs the parked
    pull request's full CI again at its base's green. ``None`` without one.
    """
    user_id = str(context.get("user_id") or "").strip()
    if not user_id:
        return None

    async def _park(repo: str, base: str, workflow: str, number: int, head_sha: str) -> bool:
        from app.services.full_ci_parking import park

        return await park("github", repo, base, workflow, number, head_sha, user_id)

    return _park


def _make_branch_lease_check_fn(context: dict[str, Any]) -> Callable[[str, str], Awaitable[str | None]] | None:
    """The ``check_branch_lease`` callback the GitHub branch-moving tools get.

    Bound to the calling run (``context["run_id"]``) and its person: a branch
    another LIVE run leases (``hold="take"``) answers the refusal text, so the
    write stands down. ``None`` outside a run: there is no holder to compare.
    """
    run_id = str(context.get("run_id") or "").strip()
    if not run_id:
        return None
    user_id = context.get("user_id") or None

    async def _check(repo: str, branch: str) -> str | None:
        from app.services.shared_state_pull_requests import branch_lease_refusal, live_branch_lease

        lease = await live_branch_lease(repo, branch, user_id=user_id, run_id=run_id)
        return branch_lease_refusal(repo, branch, lease) if lease else None

    return _check


def _make_record_push_fn(
    context: dict[str, Any],
    record: Callable[..., Awaitable[str]] | None,
) -> Callable[..., Awaitable[str]] | None:
    """The ``record_push`` callback the GitHub commit tools get (#9819).

    Names the open pull request a commit was pushed to on the work item the
    run was dispatched for (``context["work_item_id"]``, written by the
    assignment engine from the dispatcher's own record), so the run's end parks
    the item on that pull request instead of releasing it with nothing to wait
    for. ``None`` without a dispatched item.
    """
    work_item_id = str(context.get("work_item_id") or "").strip()
    if record is None or not work_item_id:
        return None

    async def _record_push(
        repo: str,
        number: int,
        head_branch: str | None = None,
        head_sha: str | None = None,
        url: str | None = None,
        draft: bool | None = None,
    ) -> str:
        return await record(work_item_id, repo, number, head_branch, head_sha, url, draft)

    return _record_push


# Real SharePoint Online hosts are ``<tenant>.sharepoint.<tld>`` with a single
# tenant label and a known top-level domain (commercial ``.com``, GCC High
# ``.us``, legacy Germany ``.de``, 21Vianet China ``.cn``). The previous
# open-ended ``\.sharepoint(?:\.[A-Za-z]+)+`` allowed a multi-segment suffix and
# so matched look-alikes such as ``evil.sharepoint.com.evil.com``. Since this
# host becomes an OAuth scope audience (``https://{host}/.default``) and the
# base of SharePoint HTTP requests, pin it to a single tenant label + bounded
# TLD so an unexpected host can never be used.
# ``_acquire_sharepoint_token`` strips and lower-cases ``host`` itself before
# matching, so the pattern is written in lower-case form.
_SHAREPOINT_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.sharepoint\.(?:com|us|de|cn)$")


async def _acquire_sharepoint_token(integration_id: str, host: str) -> str | None:
    """Exchange the integration's delegated refresh token for a SharePoint token.

    Microsoft stores Teams meeting transcripts embedded in the recording, and
    the only delegated way to extract the text for a meeting you didn't organize
    is the SharePoint Stream media endpoint
    (``https://{host}/_api/v2.1/drives/{driveId}/items/{itemId}/media/transcripts``).
    That endpoint lives on the SharePoint host, so it needs an access token whose
    audience is ``https://{host}`` rather than Microsoft Graph.

    Azure AD refresh tokens are not resource-bound, so we redeem the existing
    delegated refresh token for a SharePoint-scoped access token. This requires
    the app registration to have an Office 365 SharePoint Online delegated
    permission (e.g. ``AllSites.Read``) consented. Returns ``None`` (degrading
    gracefully) when prerequisites are missing or the exchange fails. Tokens are
    cached in Redis per (integration, host) until shortly before they expire.
    """
    from app.core.database import async_session
    from app.core.redis import redis_client

    host = (host or "").strip().lower()
    if not _SHAREPOINT_HOST_RE.match(host):
        return None

    # *integration_id* is the token holder's reference.
    async with async_session() as fresh_db:
        integ = await load_token_holder(fresh_db, integration_id)

    if not integ or integ.provider != "microsoft":
        return None
    if not integ.client_id_enc or not integ.refresh_token_enc:
        return None

    cache_key = f"ms_sp_token:{integration_id}:{host}"
    redis = None
    try:
        redis = await redis_client()
        cached = await redis.get(cache_key)
        if cached:
            return cached.decode() if isinstance(cached, bytes) else cached
    except Exception:
        redis = None

    client_id = decrypt_value(integ.client_id_enc)  # type: ignore[arg-type]  # guarded by caller
    try:
        client_secret = (await reveal_field(integration_of(integ), "client_secret_enc")) or ""
    except SecretUnavailableError:
        return None
    refresh_token = decrypt_value(integ.refresh_token_enc)  # type: ignore[arg-type]  # guarded by caller

    tenant_id = ""
    if integ.extra_config_enc:
        with contextlib.suppress(Exception):
            extra = await _reveal_extra(integ)
            tenant_id = (extra.get("tenant_id") or "").strip()
    tenant_segment = tenant_id or "common"
    token_url = f"https://login.microsoftonline.com/{tenant_segment}/oauth2/v2.0/token"

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        # ``.default`` requests every statically-consented permission for the
        # SharePoint resource and must be sent on its own — Azure AD v2 rejects
        # combining ``/.default`` with other scopes, and ``offline_access`` is
        # redundant on a refresh-token redemption (a rotated refresh token is
        # still returned when the original grant included it).
        "scope": f"https://{host}/.default",
    }
    try:
        async with egress_client() as client:
            resp = await client.post(
                token_url,
                data=payload,
                headers={"Accept": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()
            tokens = resp.json()
    except httpx.HTTPStatusError as e:
        body = ""
        with contextlib.suppress(Exception):
            body = e.response.text[:TRUNCATE_SNIPPET]
        logger.warning(
            "Microsoft SharePoint token exchange failed for '%s' (host=%s): %s | body: %s",
            integ.name,
            host,
            format_exception_message(e),
            body,
        )
        return None
    except Exception as e:
        logger.warning(
            "Microsoft SharePoint token exchange failed for '%s' (host=%s): %s",
            integ.name,
            host,
            format_exception_message(e),
        )
        return None

    access = tokens.get("access_token")
    if not access:
        return None
    # Rotate the stored refresh token if Azure AD issued a new one.
    new_refresh = tokens.get("refresh_token")
    if new_refresh:
        with contextlib.suppress(Exception):
            async with async_session() as upd_db:
                fresh = await load_token_holder(upd_db, integration_id)
                if fresh:
                    fresh.refresh_token_enc = encrypt_value(new_refresh)
                    await upd_db.commit()
    expires_in = int(tokens.get("expires_in") or 3600)
    if redis is not None:
        with contextlib.suppress(Exception):
            await redis.set(cache_key, access, ex=max(60, expires_in - 60))
    return access


def _make_sharepoint_token_fn(
    integration_id: str,
) -> Callable[[str], Awaitable[str | None]]:
    """Create a SharePoint token callback (host -> token) for an integration."""

    async def _sp_token(host: str) -> str | None:
        return await _acquire_sharepoint_token(integration_id, host)

    return _sp_token


# Audiences a delegated Microsoft Defender XDR / Sentinel / Purview integration
# may request a token for. Restricting to this allowlist means the per-audience
# acquirer can never be coaxed into minting a token for an arbitrary resource.
_AZURE_DELEGATED_SCOPES = frozenset(
    {
        # Defender XDR
        "https://graph.microsoft.com/.default",
        "https://api.securitycenter.microsoft.com/.default",
        # Sentinel
        "https://management.azure.com/.default",
        "https://api.loganalytics.io/.default",
        # Purview (Graph audience shared above + the Data Map governance audience)
        "https://purview.azure.net/.default",
    }
)


async def _acquire_azure_delegated_token(
    integration_id: str, scope: str
) -> str | None:
    """Redeem a delegated integration's refresh token for a per-audience token.

    Microsoft Defender XDR, Microsoft Sentinel, and Microsoft Purview each span
    two API audiences (Graph + Defender for Endpoint; Azure Resource Manager +
    Log Analytics; and Graph + the Purview Data Map ``https://purview.azure.net``
    respectively). A delegated OAuth access token is bound to a single resource,
    so we keep the long-lived refresh token (multi-resource by design when the
    grant included ``offline_access``) and redeem it on demand for whichever
    ``scope`` a tool needs — exactly the pattern used for SharePoint media tokens.

    ``scope`` must be one of :data:`_AZURE_DELEGATED_SCOPES` (a single resource's
    ``/.default``). Tokens are cached in Redis per (integration, scope) until
    shortly before they expire, and a rotated refresh token is persisted.
    Returns ``None`` (degrading gracefully) when prerequisites are missing or
    the exchange fails.
    """
    from app.core.database import async_session
    from app.core.redis import redis_client

    if scope not in _AZURE_DELEGATED_SCOPES:
        logger.warning("Refusing to acquire delegated token for unknown scope: %s", scope)
        return None

    # *integration_id* is the token holder's reference.
    async with async_session() as fresh_db:
        integ = await load_token_holder(fresh_db, integration_id)

    if not integ or integ.provider not in ("defender", "sentinel", "purview"):
        return None
    if not integ.client_id_enc or not integ.refresh_token_enc:
        return None

    cache_key = f"azure_deleg_token:{integration_id}:{scope}"
    redis = None
    try:
        redis = await redis_client()
        cached = await redis.get(cache_key)
        if cached:
            return cached.decode() if isinstance(cached, bytes) else cached
    except Exception:
        redis = None

    client_id = decrypt_value(integ.client_id_enc)  # type: ignore[arg-type]  # guarded by caller
    try:
        client_secret = (await reveal_field(integration_of(integ), "client_secret_enc")) or ""
    except SecretUnavailableError:
        return None
    refresh_token = decrypt_value(integ.refresh_token_enc)  # type: ignore[arg-type]  # guarded by caller

    tenant_id = ""
    if integ.extra_config_enc:
        try:
            extra = await _reveal_extra(integ)
            tenant_id = (extra.get("tenant_id") or "").strip()
        except Exception as exc:
            # A decryption failure (corrupted ciphertext / rotated key) must not
            # silently fall back to the /common/ tenant endpoint — that hides the
            # real cause behind a later, confusing token-exchange error (the same
            # regression class fixed in _get_token). Surface it and bail.
            logger.warning(
                "Failed to decrypt extra_config for '%s' (%s) while resolving the "
                "tenant for delegated token exchange: %s",
                integ.name,
                integ.provider,
                format_exception_message(exc),
            )
            return None
    tenant_segment = tenant_id or "common"
    token_url = f"https://login.microsoftonline.com/{tenant_segment}/oauth2/v2.0/token"

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        # ``/.default`` requests every statically-consented permission for the
        # target resource and must be sent on its own — Azure AD v2 rejects
        # combining ``/.default`` with other scopes, and ``offline_access`` is
        # redundant on a refresh-token redemption (a rotated refresh token is
        # still returned when the original grant included it).
        "scope": scope,
    }
    try:
        async with egress_client() as client:
            resp = await client.post(
                token_url,
                data=payload,
                headers={"Accept": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()
            tokens = resp.json()
    except httpx.HTTPStatusError as e:
        body = ""
        with contextlib.suppress(Exception):
            body = e.response.text[:TRUNCATE_SNIPPET]
        logger.warning(
            "Azure delegated token exchange failed for '%s' (scope=%s): %s | body: %s",
            integ.name,
            scope,
            format_exception_message(e),
            body,
        )
        return None
    except Exception as e:
        logger.warning(
            "Azure delegated token exchange failed for '%s' (scope=%s): %s",
            integ.name,
            scope,
            format_exception_message(e),
        )
        return None

    access = tokens.get("access_token")
    if not access:
        return None
    # Rotate the stored refresh token if Azure AD issued a new one.
    new_refresh = tokens.get("refresh_token")
    if new_refresh:
        with contextlib.suppress(Exception):
            async with async_session() as upd_db:
                fresh = await load_token_holder(upd_db, integration_id)
                if fresh:
                    fresh.refresh_token_enc = encrypt_value(new_refresh)
                    await upd_db.commit()
    expires_in = int(tokens.get("expires_in") or 3600)
    if redis is not None:
        with contextlib.suppress(Exception):
            await redis.set(cache_key, access, ex=max(60, expires_in - 60))
    return access


def _make_azure_delegated_token_fn(
    integration_id: str,
) -> Callable[[str], Awaitable[str | None]]:
    """Create a per-audience delegated token callback (scope -> token).

    Passed to the Defender / Sentinel tool builders as ``token_acquirer`` when
    the integration authenticates via delegated OAuth, so each tool can obtain a
    token for whichever Microsoft audience it targets.
    """

    async def _token(scope: str) -> str | None:
        return await _acquire_azure_delegated_token(integration_id, scope)

    return _token


async def _get_extra_config_value(db: AsyncSession, integration_id: str, key: str) -> str:
    """Return one optional, non-secret extra_config field of an integration.

    Used for the fields a tool builder takes as a keyword rather than in the
    compound token: the Purview governance account name (the Data Map tools
    target ``https://{account}.purview.azure.com``; the compliance tools need no
    account) and the OpenAEV Tenant field. Returns ``""`` when unset or
    unreadable so the tools degrade to their default or an actionable error.
    """
    row = await db.execute(select(Integration).where(Integration.id == uuid.UUID(integration_id)))
    integ = row.scalar_one_or_none()
    if integ is None:
        return ""
    try:
        # A field held as a vault reference is resolved (#6080).
        extra = await _reveal_extra(integ)
    except Exception:
        return ""
    return str(extra.get(key) or "").strip()


async def _opencrq_manifest(db: AsyncSession, integration_id: str) -> Manifest | ProbeLater | None:
    """What an integration's OpenCRQ build is given of the instance's procedure manifest.

    An integration no registration owns gets :data:`PROBE`: the provider reads the
    manifest itself at the first call of one of its tools, once per build, never
    here. A registration owns the others: the manifest it stored when that is the
    one it reported last, so the tools the instance cannot serve are withheld at
    build with no request; ``None`` otherwise (none stored yet, or one the platform
    has moved past), every tool offered and no input checked. A registration that
    cannot be read is no answer either, so the provider asks the instance itself.
    """
    try:
        async with db.begin_nested():
            platform = await platform_for_integration(db, integration_id)
            if platform is None:
                return PROBE
            return await registered_procedure_manifest(db, platform)
    except Exception as exc:
        logger.warning("Could not read the platform registration of integration %s: %s", integration_id, format_exception_message(exc))
        return PROBE


def flatten_bindings(bindings: dict[str, list[str]]) -> dict[str, str]:
    """Convert ``{provider: [id1, id2]}`` to ``{provider: id1, provider#2: id2}``.

    Single-element lists produce a plain provider key (backward-compatible).
    Multi-element lists produce ``provider#2``, ``provider#3``, … for the
    extra entries so each integration instance gets its own binding slot.
    """
    flat: dict[str, str] = {}
    for provider, ids in bindings.items():
        if not ids:
            continue
        if isinstance(ids, str):
            flat[provider] = ids
            continue
        for idx, iid in enumerate(ids):
            key = provider if idx == 0 else f"{provider}#{idx + 1}"
            flat[key] = iid
    return flat


# Separator between a base tool name and the per-instance slug when several
# integrations of the SAME provider are bound. The separator is NOT assumed
# to be unambiguous in tool names (custom providers accept user-authored
# names verbatim, which may contain ``__``): the original name is recorded on
# ``ToolDefinition.base_name`` at rename time and allow/deny gates match on
# that, never by splitting the string. Keep in sync with
# ``builtin_tools.filtering.INSTANCE_SUFFIX_SEP``.
_INSTANCE_SUFFIX_SEP = "__"

# Hard ceiling shared by Anthropic / OpenAI tool-name validation.
_MAX_TOOL_NAME_LEN = 64

# An instance slug is at most this long...
_MAX_INSTANCE_SLUG_LEN = 24
# ...and needs at least this much room to stay meaningful.
_MIN_INSTANCE_SLUG_LEN = 4

# Caps for the human-readable account label prefixed onto every tool
# description of a multi-instance provider (name part / full label).
_MAX_INSTANCE_NAME_LEN = 80
_MAX_INSTANCE_LABEL_LEN = 160


class InstanceSlugBudgetError(ValueError):
    """No room to append an instance suffix within the provider name ceiling."""


def _sanitize_instance_label(text: str) -> str:
    """Collapse a user-authored account label to a benign charset."""
    return re.sub(r"[^\w .\-/]+", " ", text).strip()


def _instance_tool_slug(
    provider: str,
    integ_name: str | None,
    integration_id: str,
    used_slugs: dict[str, set[str]],
    *,
    max_len: int = 24,
    stored_slug: str | None = None,
) -> str:
    """Return a unique, LLM-safe tool-name suffix for one provider instance.

    The integration's stored ``tool_slug`` when it has a valid one:
    it was derived once from the display name and does not follow a rename,
    so the tool names a persona, a skill or an assignment gate cites stay put.
    Else derived from the display name (``derive_tool_slug``), which falls
    back to the provider plus a short id prefix when the name is empty (two
    unnamed instances must still get DISTINCT tool names — identical
    definitions would leave one of them unreachable). Uniqueness is enforced
    per provider via ``used_slugs`` so two integrations with the same slug
    still diverge.

    The returned slug NEVER exceeds ``max_len`` (uniqueness counter included):
    ``max_len`` is a HARD ceiling — the caller derives it from the 64-char
    Anthropic/OpenAI tool-name limit minus the longest base name — and an
    overshoot here would produce names that collide after downstream 64-char
    truncation, exactly the "wrong account answers" failure this ceiling prevents.
    Raises :class:`InstanceSlugBudgetError` when ``max_len`` leaves no room
    for a meaningful slug (a pathologically long base tool name).
    """
    budget = min(_MAX_INSTANCE_SLUG_LEN, max_len)
    if budget < _MIN_INSTANCE_SLUG_LEN:
        raise InstanceSlugBudgetError(
            f"cannot build an instance slug for provider '{provider}' "
            f"(integration {integration_id}): {max_len} chars available, "
            f"{_MIN_INSTANCE_SLUG_LEN} required under the "
            f"{_MAX_TOOL_NAME_LEN}-char tool-name ceiling"
        )
    if stored_slug and tool_slug_error(stored_slug) is None:
        base = stored_slug
    else:
        # Sanitized either way: a custom provider key ("custom:foo") carries
        # ':' which is invalid in Anthropic/OpenAI tool names.
        base = derive_tool_slug(integ_name, provider, integration_id)
    # Reserve room for a ``_N`` uniqueness counter.
    base = base[: budget - 3].rstrip("_")
    taken = used_slugs.setdefault(provider, set())
    slug = base
    counter = 2
    while slug in taken:
        suffix = f"_{counter}"
        # Re-trim the stem so the counter never pushes past the budget.
        slug = f"{base[: max(1, budget - len(suffix))].rstrip('_')}{suffix}"
        counter += 1
    taken.add(slug)
    return slug


def _stored_tool_slug(row: Integration | None) -> str | None:
    """The stored tool-name suffix of an integration row, ``None`` without one."""
    value = getattr(row, "tool_slug", None) if row is not None else None
    return value if isinstance(value, str) and value else None


def _shown_tool_result(result: Any, platform: RegisteredPlatform) -> Any:
    """A platform tool's result with its links on the platform's registered URL.

    A dict or list result is serialized the way the executor would, so the
    links it carries are moved too. A ``ToolResult`` carries text
    (``content: str``): a result built with anything else is returned as it
    is, never guessed at.
    """
    if isinstance(result, ToolResult):
        if isinstance(result.content, str):
            shown = platform.shown(result.content)
            if shown != result.content:
                return dataclasses.replace(result, content=shown)
        return result
    if isinstance(result, str):
        return platform.shown(result)
    if isinstance(result, (dict, list)):
        serialized = _json.dumps(result, default=str)
        shown = platform.shown(serialized)
        return result if shown == serialized else shown
    return result


def _show_tool_results_on_public_url(
    defs: list[ToolDefinition],
    executor: CallableToolExecutor,
    platform: RegisteredPlatform,
) -> None:
    """Re-register the handlers of *defs* so every result they hand the model
    (and, through it, a person) names the platform on its registered URL.

    The handlers keep calling the internal URL they were built on: only the
    text they return moves. ``functools.wraps`` keeps the handler's
    signature visible to the executor's parameter filtering.
    """

    def _shown(handler: Any) -> Any:
        @functools.wraps(handler)
        async def _shown_handler(*args: Any, **kwargs: Any) -> Any:
            return _shown_tool_result(await handler(*args, **kwargs), platform)

        return _shown_handler

    for d in defs:
        handler = executor.handler(d.name)
        if handler is not None:
            executor.register(d.name, _shown(handler))


def _route_through_relay(
    defs: list[ToolDefinition],
    executor: CallableToolExecutor,
    route: EgressRoute,
) -> None:
    """Re-register the handlers of *defs* so every request they make - token
    refreshes and downloads included - goes through *route*'s relay group and
    never directly from the platform.
    """

    def _routed(handler: Any) -> Any:
        @functools.wraps(handler)
        async def _routed_handler(*args: Any, **kwargs: Any) -> Any:
            with egress_route(route):
                return await handler(*args, **kwargs)

        return _routed_handler

    for d in defs:
        handler = executor.handler(d.name)
        if handler is not None:
            executor.register(d.name, _routed(handler))


def _suffix_instance_tools(
    defs: list[ToolDefinition],
    executor: CallableToolExecutor,
    *,
    provider: str,
    integ_name: str | None,
    integ_desc: str | None,
    integration_id: str,
    used_slugs: dict[str, set[str]],
    tool_slug: str | None = None,
) -> list[ToolDefinition]:
    """Rename one instance's tools to ``tool__<slug>`` and re-route handlers.

    Provider builders register handlers under BASE tool names on the shared
    executor, so with several instances of the same provider the last-built
    instance silently overwrites the previous ones and every base-name call
    executes with the wrong account. For a multi-instance provider,
    EVERY instance (the first included) is therefore:

    * renamed to ``{base}__{slug}`` (slug unique per provider),
    * re-registered under the suffixed name with the handler this instance's
      builder JUST registered (it closes over THIS instance's credentials),
    * stripped of its base-name registration so no call can silently route to
      whichever instance happened to be built last,
    * labeled with the integration name in the description so the LLM can
      pick the right account.

    Each renamed definition records its original name on ``base_name`` —
    allow/deny gates match on that recorded base (via
    ``builtin_tools.filtering.instance_base_tool_name``) so provider tool
    scoping keeps working unchanged, without inferring the base from the
    string (user-authored custom tool names may themselves contain ``__``).

    Raises :class:`InstanceSlugBudgetError` — BEFORE any executor mutation —
    when the longest base tool name leaves no room for a slug under the
    64-char provider ceiling (see :func:`_instance_tool_slug`). An over-long
    tool name would fail the entire LLM request at the provider API, so
    failing fast here (the caller logs and skips this instance's tools) is
    strictly safer than emitting an invalid name.
    """
    longest = max((len(d.name) for d in defs), default=0)
    slug = _instance_tool_slug(
        provider,
        integ_name,
        integration_id,
        used_slugs,
        max_len=_MAX_TOOL_NAME_LEN - longest - len(_INSTANCE_SUFFIX_SEP),
        stored_slug=tool_slug,
    )
    # The label exists solely to let the model tell accounts apart, but the
    # integration name and description are USER-AUTHORED free text prefixed
    # onto every tool description this instance exposes (a shared/company-
    # managed integration's author is not necessarily the agent's user).
    # Collapse both to a benign charset and hard-cap the length so a hostile
    # or oversized name/description cannot flood the model's tool context.
    label = _sanitize_instance_label(integ_name or "")[:_MAX_INSTANCE_NAME_LEN]
    if not label:
        label = _sanitize_instance_label(f"{provider} instance {slug}")
    if integ_desc:
        desc = _sanitize_instance_label(integ_desc)
        if desc:
            label = f"{label}: {desc}"[:_MAX_INSTANCE_LABEL_LEN]
    enriched: list[ToolDefinition] = []
    for d in defs:
        new_name = f"{d.name}{_INSTANCE_SUFFIX_SEP}{slug}"
        handler = executor._handlers.pop(d.name, None)
        executor._accepts_file_context.pop(d.name, None)
        executor._accepted_params.pop(d.name, None)
        if handler is None:
            # Do NOT offer a tool the executor cannot run. The renamed
            # definition would reach the LLM and every call would come back
            # "Unknown tool", burning an iteration and feeding
            # ``tool_error_counts`` / the consecutive-error brake. Dropping
            # it keeps the advertised surface honest.
            logger.warning(
                "Multi-instance suffixing: no handler registered for tool "
                "'%s' (provider %s, integration %s) — dropping the definition",
                d.name,
                provider,
                integration_id,
            )
            continue
        executor.register(new_name, handler)
        enriched.append(
            dataclasses.replace(
                d,
                name=new_name,
                base_name=d.name,
                entity_id=integration_id,
                description=f"[{label}] {d.description}",
                # The surface description keeps the account label too.
                describe_for_surface=_labelled_surface_description(label, d.describe_for_surface),
            )
        )
    return enriched


def _labelled_surface_description(
    label: str,
    describe: Callable[[tuple[ToolDefinition, ...]], str] | None,
) -> Callable[[tuple[ToolDefinition, ...]], str] | None:
    """*describe* with the instance *label* prefixed, as ``description`` is; ``None`` stays ``None``."""
    if describe is None:
        return None
    return lambda surface: f"[{label}] {describe(surface)}"


async def shadow_invisible_bindings(
    db: AsyncSession,
    effective_bindings: dict[str, str],
    binding_origin_map: dict[str, str],
    *,
    acting_user_id: str | None,
    user_group_ids: list[str] | None = None,
    withheld: list[dict] | None = None,
    lent_ids: set[str] | None = None,
    lent_owner_id: Any = None,
) -> list[str]:
    """Drop fixed integration bindings the *acting user* is not allowed to see.

    *lent_ids* (``integration_credentials.lent_integration_ids`` of the agent)
    and *lent_owner_id* (the agent's owner) keep the bindings the agent lends:
    an integration among *lent_ids* that still belongs to *lent_owner_id* and
    is not ``per_user`` stays bound for this run, whatever the acting user can
    see - the owner chose to lend it through this agent only. Nothing else
    changes: the integration stays out of the acting user's own list.

    A shared agent (group-shared or company-managed) must never run a bound
    integration using **another user's personal credentials**. An integration
    is usable by the acting user only when it is visible to them — own +
    group-shared + company-managed — the exact rule the REST API enforces via
    ``user_visible_filter``.

    Any binding whose integration is NOT visible to *acting_user_id* is removed
    from ``effective_bindings`` and ``binding_origin_map`` (both mutated in
    place). Its bare provider key (e.g. ``github`` for a flattened ``github#2``
    slot) is returned so the caller can fall back to per-user *template*
    resolution for that provider — each user then transparently uses their OWN
    connected integration of the same provider instead of borrowing the
    owner's personal token (principle of least privilege).

    A provider whose every integration is a different API (an imported OpenAPI
    spec) has no per-user equivalent: its hidden bindings are dropped without
    fallback, and it is left out of the returned list.

    Returns the sorted list of dropped providers to resolve per user. No-op
    (``[]``) when there are no bindings or no concrete acting user (anonymous / missing) —
    those paths carry no identity to enforce visibility against and are gated
    elsewhere. A binding whose id is not a parseable UUID is unverifiable and is
    therefore dropped fail-closed (treated as not visible), never left behind.

    *withheld*, when given, receives a ``missing_integration_bindings`` entry for
    each dropped binding whose integration exists: the personal OAuth
    token rule's (``integration_credentials.refused_binding``) for one the acting
    user can see, ``integration_credentials.invisible_binding`` - the provider,
    never the name - for one they cannot, so the turn can say what it did not
    use instead of silently lacking the tools.
    """
    if (
        not effective_bindings
        or not acting_user_id
        or acting_user_id == "anonymous"
    ):
        return []
    try:
        acting_uuid = uuid.UUID(acting_user_id)
    except (ValueError, TypeError):
        return []

    # Map each candidate integration id → the binding keys that reference it.
    id_to_keys: dict[str, list[str]] = {}
    for key, integ_id in effective_bindings.items():
        if integ_id:
            id_to_keys.setdefault(integ_id, []).append(key)

    candidate_uuids: list[uuid.UUID] = []
    for integ_id in id_to_keys:
        try:
            candidate_uuids.append(uuid.UUID(integ_id))
        except (ValueError, TypeError):
            continue

    # Resolve which candidate integrations are visible to the acting user.
    # When every binding id is malformed there is nothing to query, but we must
    # NOT short-circuit here: those ids are unverifiable, so fail-closed by
    # falling through to the removal loop and dropping them. Leaving a malformed
    # id in ``effective_bindings`` would later crash tool resolution
    # (``_build_integration_tools_inner`` does ``uuid.UUID(iid)`` over the
    # values) and would also defeat the least-privilege guard.
    visible_uuids: set[uuid.UUID] = set()
    # Visible rows whose personal OAuth token the acting user may not use.
    token_withheld_rows: dict[uuid.UUID, Any] = {}
    # Visible ``per_user`` rows on which the acting user connected no account.
    own_account_rows: dict[uuid.UUID, Any] = {}
    if candidate_uuids:
        # Imported lazily to avoid a circular import (helpers → models, and
        # this module is imported by the chat/assignment services that helpers
        # does not depend on).
        from app.api.v1.helpers import user_visible_filter

        result = await db.execute(
            select(
                Integration.id,
                Integration.provider,
                Integration.name,
                Integration.auth_type,
                Integration.user_id,
                Integration.oauth_share_acknowledged,
                Integration.oauth_token_owner_id,
                Integration.credential_mode,
            ).where(
                Integration.id.in_(candidate_uuids),
                user_visible_filter(Integration, acting_uuid, user_group_ids),
            )
        )
        # Compare by canonical UUID value, not raw string. Bindings can carry a
        # valid but non-canonical UUID (no hyphens / different case) that the
        # UUID-parsed query above still matches; a raw-string compare against
        # the DB's canonical form would then wrongly shadow a *visible*
        # integration. ``Integration.id`` is a UUID column, so rows are
        # ``uuid.UUID`` objects.
        #
        # OAuth tokens are personal to whoever connected them: an integration
        # "visible" via ``user_visible_filter`` only lends its token to the
        # acting user once the owner's consent is recorded - the
        # sharing choice the owner made; a share made without it
        # removed the token at write time. Both conditions are required.
        rows = result.all()
        # A ``per_user`` integration acts with each person's own account:
        # it stays for a member who connected theirs (the tool build
        # substitutes it), and tells the others to connect theirs.
        own_connected = await connected_user_ids(
            db,
            [row.id for row in rows if needs_own_credential(row, acting_uuid)],
            acting_uuid,
        )
        for row in rows:
            if needs_own_credential(row, acting_uuid):
                if str(row.id) in own_connected:
                    visible_uuids.add(row.id)
                else:
                    own_account_rows[row.id] = row
            elif oauth_credentials_usable(row, acting_uuid):
                visible_uuids.add(row.id)
            else:
                token_withheld_rows[row.id] = row

        lent_candidates = [
            u for u in candidate_uuids if str(u) in (lent_ids or set()) and u not in visible_uuids
        ]
        lent_owner = normalize_acting_user_id(lent_owner_id)
        if lent_candidates and lent_owner is not None:
            lent_result = await db.execute(
                select(Integration.id).where(
                    Integration.id.in_(lent_candidates),
                    Integration.user_id == lent_owner,
                    Integration.credential_mode != CREDENTIAL_MODE_PER_USER,
                )
            )
            for lent_id in lent_result.scalars().all():
                visible_uuids.add(lent_id)
                token_withheld_rows.pop(lent_id, None)

    shadowed: set[str] = set()
    removed_count = 0
    hidden_uuids: list[uuid.UUID] = []
    for integ_id, keys in id_to_keys.items():
        try:
            parsed = uuid.UUID(integ_id)
        except (ValueError, TypeError):
            parsed = None
        if parsed is not None and parsed in visible_uuids:
            continue
        for key in keys:
            effective_bindings.pop(key, None)
            binding_origin_map.pop(key, None)
            shadowed.add(key.split("#", 1)[0])
            removed_count += 1
        if withheld is None or parsed is None:
            continue
        if parsed in own_account_rows:
            withheld.append(own_account_not_connected_binding(own_account_rows[parsed]))
        elif parsed in token_withheld_rows:
            withheld.append(refused_binding(token_withheld_rows[parsed]))
        elif parsed not in hidden_uuids:
            hidden_uuids.append(parsed)

    if withheld is not None and hidden_uuids:
        withheld.extend(
            await invisible_integration_bindings(db, acting_user_id, hidden_uuids)
        )

    fallback = sorted(p for p in shadowed if provider_resolves_per_user(p))
    if shadowed:
        logger.info(
            "Least-privilege: dropped %d fixed integration binding(s) not "
            "visible to acting user %s; falling back to per-user resolution "
            "for provider(s) %s%s",
            # Count actual binding slots removed (not unique integration ids)
            # so the audit line is accurate when several keys reference the
            # same hidden integration.
            removed_count,
            acting_user_id,
            fallback,
            (
                f"; no per-user fallback for {sorted(shadowed - set(fallback))}"
                if len(fallback) != len(shadowed)
                else ""
            ),
        )
    return fallback


class BindingBasis(enum.StrEnum):
    """Why a tool build may use the integration bindings it is handed."""

    #: ``resolve_agent_integration_bindings`` resolved them for the person the
    #: run acts for (a chat turn's session selection, checked by
    #: ``resolve_session_integration_bindings``, counts as well).
    RESOLVED = "resolved"
    #: The run acts as the agent's owner and no one else (the public widget).
    OWNER_RUN = "owner_run"
    #: The caller checked that the person may use each integration itself.
    CALLER_CHECKED = "caller_checked"
    #: An administrator bound them for everyone (the General Assistant).
    ADMIN_CONFIGURED = "admin_configured"
    #: Used as bound for whoever acts, with no check: a known gap, to close.
    UNCHECKED = "unchecked"


class ToolBindings(Mapping[str, Any]):
    """The integration bindings a tool build may use, and why (*basis*, *reason*).

    The only thing :func:`build_integration_tools` accepts: a plain dict is
    refused, so every caller says on what ground the credentials it builds
    with may act for the person the run serves. ``tests/test_agent_integration_bindings.py``
    pins which module uses which basis.
    """

    __slots__ = ("_bindings", "basis", "reason")

    def __init__(self, bindings: Mapping[str, Any], *, basis: BindingBasis, reason: str) -> None:
        if not isinstance(basis, BindingBasis) or not reason.strip():
            raise ValueError("ToolBindings needs a BindingBasis and the reason it applies")
        self._bindings = dict(bindings)
        self.basis = basis
        self.reason = reason

    def __getitem__(self, key: str) -> Any:
        return self._bindings[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._bindings)

    def __len__(self) -> int:
        return len(self._bindings)

    def __repr__(self) -> str:
        return f"ToolBindings({self._bindings!r}, basis={self.basis.value!r})"


@dataclasses.dataclass
class AgentIntegrationBindings:
    """An agent's integration bindings resolved for the person a run acts for.

    *bindings* / *origins*: flattened slot -> integration id, and how each slot
    was bound. *withheld*: what the run does not use for that person and
    nothing replaced, ``missing_integration_bindings`` entries told to the
    model and shown on the reply.
    """

    bindings: dict[str, str]
    origins: dict[str, str]
    withheld: list[dict]

    @property
    def tool_bindings(self) -> ToolBindings:
        """The resolved bindings, for :func:`build_integration_tools`."""
        return ToolBindings(
            self.bindings, basis=BindingBasis.RESOLVED, reason="resolved for the person the run acts for"
        )


async def resolve_agent_integration_bindings(
    db: AsyncSession,
    agent: Any,
    acting_user_id: str | None,
    *,
    company_automation: bool = False,
    scope_headers: dict[str, str] | None = None,
) -> AgentIntegrationBindings:
    """THE resolution of *agent*'s integration bindings for *acting_user_id*.

    Every run that builds an agent's integration tools for a person resolves
    them here, so who may use which credential is decided once:

    1. the direct bindings, minus the ones the owner lost access to
       (``integration_credentials.drop_orphaned_bindings``);
    2. for anyone but the owner, minus the ones they may not use
       (:func:`shadow_invisible_bindings`), the integrations the agent lends
       (``integration_credentials.lent_integration_ids``) kept;
    3. the template bindings, plus the providers step 2 dropped, resolved to
       the person's own integrations (:func:`template_candidate_filter`).

    *acting_user_id* ``None`` or not a UUID: no one to check against, so the
    run is the owner's own (steps 2 and 3 skipped), as an anonymous chat turn.
    """
    from app.api.v1.helpers import get_user_group_ids
    from app.services.integration_credentials import drop_orphaned_bindings, lent_integration_ids
    from app.services.integration_user_credentials import connected_for_clause

    bindings = flatten_bindings(getattr(agent, "integration_bindings", None) or {})
    origins = dict.fromkeys(bindings, DIRECT_BINDING_ORIGIN)
    dropped: list[dict] = []
    await drop_orphaned_bindings(db, agent, bindings, origins, withheld=dropped)

    acting_uuid = normalize_acting_user_id(acting_user_id)
    if acting_uuid is None:
        return AgentIntegrationBindings(bindings, origins, dropped)

    shadowed: list[str] = []
    if bindings and acting_uuid != normalize_acting_user_id(getattr(agent, "user_id", None)):
        shadowed = await shadow_invisible_bindings(
            db,
            bindings,
            origins,
            acting_user_id=str(acting_uuid),
            user_group_ids=await get_user_group_ids(db, acting_uuid),
            withheld=dropped,
            lent_ids=lent_integration_ids(agent),
            lent_owner_id=getattr(agent, "user_id", None),
        )

    templates = list(getattr(agent, "integration_template_bindings", None) or [])
    needed = [p for p in dict.fromkeys([*templates, *shadowed]) if p not in bindings]
    if needed:
        result = await db.execute(
            select(Integration)
            .where(
                Integration.provider.in_(needed),
                await template_candidate_filter(db, acting_uuid, company_automation=company_automation),
                connected_for_clause(acting_uuid),
            )
            .order_by(Integration.provider, Integration.created_at, Integration.id)
        )
        by_provider: dict[str, list[Integration]] = {}
        for row in result.scalars().all():
            by_provider.setdefault(row.provider, []).append(row)
        for provider, candidates in by_provider.items():
            picked, origin = await resolve_template_integration(
                db,
                provider,
                candidates,
                scope_headers,
                acting_user_id=acting_uuid,
                context=f"agent={getattr(agent, 'name', '')}",
            )
            if picked is not None:
                bindings[provider] = str(picked.id)
                origins[provider] = origin or ""

    # A dropped binding the person's own integration of the provider replaced
    # is not missing.
    withheld = [
        entry for entry in dropped if not (entry.get("provider") in shadowed and entry.get("provider") in bindings)
    ]
    return AgentIntegrationBindings(bindings, origins, withheld)


def prebuilt_tools_refusal(
    prebuilt: Any,
    *,
    agent: Any,
    acting_user_id: str | None,
    effective_bindings: Mapping[str, str],
) -> str | None:
    """Why a run may not use integration tools built before it, or ``None``.

    Tools built once for several runs (an evaluation's entries) must say for
    whom (``acting_user_id``) and from which bindings (``bindings``) they were
    built. A run acting for a person takes them only when built for that
    person from bindings it resolved itself
    (:func:`resolve_agent_integration_bindings`); a run with no person (the
    owner's own) only when built for the owner or no one. Anything else is
    refused, and the run builds its own tools.
    """
    from app.services.integration_credentials import canonical_integration_ids

    built_from = getattr(prebuilt, "bindings", None)
    if not isinstance(built_from, Mapping):
        return "they do not say which bindings they were built from"
    built_for = normalize_acting_user_id(getattr(prebuilt, "acting_user_id", None))
    acting = normalize_acting_user_id(acting_user_id)
    if acting is None:
        if built_for not in (None, normalize_acting_user_id(getattr(agent, "user_id", None))):
            return "they were built for someone other than the agent's owner"
        return None
    if built_for != acting:
        return "they were built for someone other than the person the run acts for"
    if not canonical_integration_ids(built_from.values()) <= canonical_integration_ids(effective_bindings.values()):
        return "they hold an integration the run's own resolution does not give its person"
    return None


async def pin_bindings(
    db: AsyncSession,
    effective_bindings: dict[str, str],
    binding_origin_map: dict[str, str],
    pinned: dict[str, str],
    *,
    acting_user_id: str,
    used_providers: tuple[str, ...] | list[str] = (),
) -> list[str]:
    """Bind each pinned provider to its pinned integration first (both maps mutated in place).

    A turn that speaks through one account - an agent account - must
    reach that account's platform as that account: the pinned integration takes
    the provider's base slot, and every binding of that provider the turn did
    not choose on purpose gives way to it - the template resolved for the acting
    user (which would otherwise hand the account its owner's own mailbox), a
    session override, the extra instances (``provider#2``) of those origins
    alike. Only a provider the agent uses is pinned - bound, or declared in
    *used_providers* (its template bindings): the pin chooses which account a
    capability acts as, never adds one.

    A DIRECT agent binding of the pinned provider that names ANOTHER
    integration is kept as an extra instance (``provider#N``), its origin
    unchanged: the agent's owner bound it on purpose (an assistant's own
    sign-in beside the executive's calendar it manages), and a chat run exposes
    both. With two instances every tool of the provider is then suffixed by
    ``build_integration_tools`` (``tool__<slug>``), exactly as in a chat run,
    so no base-name call can route to the wrong account. A direct binding of
    the pinned integration itself is folded into the pin (never two slots for
    one account).

    The pinned integration must be one the acting user may see and whose token
    they may use, the rule every binding follows. One that is not drops the
    provider's tools instead - the kept direct bindings included: never another
    account of that provider in its place.

    Returns the providers bound to their pinned integration.
    """
    if not pinned:
        return []
    used = {key.split("#", 1)[0] for key in effective_bindings} | set(used_providers)
    wanted = {provider: integration_id for provider, integration_id in pinned.items() if provider in used}
    if not wanted:
        return []
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter

    acting = uuid.UUID(acting_user_id)
    candidates: dict[str, uuid.UUID] = {}
    for provider, integration_id in wanted.items():
        try:
            candidates[provider] = uuid.UUID(str(integration_id))
        except (TypeError, ValueError):
            continue
    usable: set[tuple[str, uuid.UUID]] = set()
    if candidates:
        rows = await db.execute(
            select(Integration.id, Integration.provider).where(
                Integration.id.in_(list(candidates.values())),
                user_visible_filter(Integration, acting, await get_user_group_ids(db, acting)),
                oauth_credentials_usable_clause(acting),
            )
        )
        usable = {(row.provider, row.id) for row in rows.all()}
    bound: list[str] = []
    for provider in wanted:
        integration_id = candidates.get(provider)
        pinned_usable = integration_id is not None and (provider, integration_id) in usable
        # The direct bindings of the provider that name another integration,
        # in their binding order: kept beside the pin as extra instances.
        kept: list[tuple[str, str]] = []
        for key in [k for k in effective_bindings if k.split("#", 1)[0] == provider]:
            other = effective_bindings.pop(key, None)
            origin = binding_origin_map.pop(key, None)
            if (
                pinned_usable
                and origin == DIRECT_BINDING_ORIGIN
                and other
                and str(other) != str(integration_id)
            ):
                kept.append((str(other), origin))
        if pinned_usable:
            effective_bindings[provider] = str(integration_id)
            binding_origin_map[provider] = "pinned"
            for index, (other, origin) in enumerate(kept, start=2):
                key = f"{provider}#{index}"
                effective_bindings[key] = other
                binding_origin_map[key] = origin
            bound.append(provider)
        else:
            logger.warning(
                "Pinned %s integration %s is not usable by user %s: its tools are withheld",
                provider,
                wanted[provider],
                acting_user_id,
            )
    return bound


async def build_integration_tools(
    db: AsyncSession,
    bindings: ToolBindings,
    *,
    context: dict[str, Any] | None = None,
    channel_bot_mode: bool = False,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build tools for all bound integrations.

    Parameters
    ----------
    bindings:
        :class:`ToolBindings`: maps provider keys to lists of Integration
        UUIDs (new format) or single Integration UUIDs (legacy/flattened
        format), with the basis on which they may be used. Lists are
        internally flattened to ``provider#N`` keys for multi-instance
        support. A plain dict is refused (``TypeError``).
    context:
        Optional dict of contextual fields (agent, assignment, source, …)
        used to enrich log messages so tool-resolution issues can be traced
        back to the caller.
    channel_bot_mode:
        When True, resolves credentials from the ChannelBot table instead of
        Integration table.  Used by the channel bot test-tool endpoint.

    Returns (definitions, executor) ready to be added to a CompositeToolExecutor.
    Each provider receives a *refresh_fn* callback so its tools can
    auto-refresh expired OAuth tokens on 401/403 responses.
    """
    if not isinstance(bindings, ToolBindings):
        raise TypeError(
            "build_integration_tools takes ToolBindings: resolve_agent_integration_bindings(...)"
            ".tool_bindings, or ToolBindings(..., basis=..., reason=...) saying why they may be used"
        )
    logger.debug("Integration tool build: basis=%s (%s)", bindings.basis.value, bindings.reason)
    flat = _ensure_flat(dict(bindings))
    if channel_bot_mode:
        definitions, executor = await _build_channel_bot_tools_inner(db, flat, context or {})
    else:
        definitions, executor = await _build_integration_tools_inner(db, flat, context or {})
    return with_source_labels(definitions), executor


def _compose_style_of(integ: Any) -> str | None:
    """The Compose style field of a Microsoft integration row, ``None`` when unset or unreadable.

    Read from the row already loaded for the binding (no second query); a
    value that cannot be decrypted leaves the default compose style.
    """
    encrypted = getattr(integ, "extra_config_enc", None)
    if not encrypted:
        return None
    try:
        value = _decrypt_dict(encrypted).get("compose_style")
    except Exception as exc:
        logger.warning(
            "compose_style: failed to decrypt extra_config of integration %s: %s",
            getattr(integ, "id", "?"),
            exc,
        )
        return None
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _holder_ref_of(holders: dict[str, Any], integration_id: str) -> str:
    """The token-holder reference a binding's callbacks are keyed by."""
    holder = holders.get(integration_id)
    return holder_ref(holder) if holder is not None else integration_id


def _ensure_flat(bindings: dict[str, list[str]] | dict[str, str]) -> dict[str, str]:
    """Normalize bindings to ``dict[str, str]``, flattening lists if present."""
    if not bindings:
        return {}
    first_val = next(iter(bindings.values()), None)
    if isinstance(first_val, list):
        return flatten_bindings(bindings)  # type: ignore[arg-type]
    return bindings  # type: ignore[return-value]


async def _build_channel_bot_tools_inner(
    db: AsyncSession,
    bindings: dict[str, str],
    context: dict[str, Any],
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build tools using ChannelBot credentials (for test-tool endpoint)."""
    executor = _SafeIntegrationExecutor()
    definitions: list[ToolDefinition] = []

    for provider, bot_id in bindings.items():
        try:
            token, bot = await _get_channel_bot_token(db, bot_id, provider)
        except Exception as exc:
            logger.warning("Failed to get channel bot token for %s: %s", provider, format_exception_message(exc))
            continue
        if not token:
            continue

        builder = CHANNEL_BOT_TOOLS.get(provider)
        if not builder:
            continue

        refresh_fn = _noop_refresh
        extra_kwargs: dict[str, Any] = {"refresh_fn": refresh_fn}
        # Enforce the bot-level publish whitelist in the test endpoint too.
        if bot is not None and bot.allowed_publish_channels:
            extra_kwargs["publish_channels"] = bot.allowed_publish_channels
        # File persistence for download tools (same gate as the agent path).
        if provider in _PERSIST_FILE_CHANNEL_BOTS and any(
            context.get(k) for k in ("user_id", "conversation_id", "run_id")
        ):
            extra_kwargs["persist_file_fn"] = _make_persist_file_fn(context, executor)
        defs = builder(token, executor, **extra_kwargs)
        if defs:
            definitions.extend(defs)

    return definitions, executor


async def _build_integration_tools_inner(
    db: AsyncSession,
    bindings: dict[str, str],
    context: dict[str, Any],
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    executor = _SafeIntegrationExecutor()
    definitions: list[ToolDefinition] = []

    ctx_source = context.get("source", "unknown")
    ctx_agent = context.get("agent", "")
    ctx_assignment = context.get("assignment", "")
    source_label = ctx_source
    if ctx_agent:
        source_label += f" / agent={ctx_agent}"
    if ctx_assignment:
        source_label += f" / assignment={ctx_assignment}"

    # ``element_scope`` is the run's ENFORCED Scope(s) (``RunScope.enforced``
    # in services.element_scope): only the element types an agent or an
    # assignment enforces as a boundary, never a raw stored selection - there
    # an empty list means "not selected", here it allows nothing. A type that
    # is guidance only is absent, so its provider builds its full toolbox.
    _tool_scope = context.get("element_scope")

    provider_keys = list(bindings.keys())
    logger.info(
        "Resolving integration tools for providers %s [%s]",
        provider_keys,
        source_label,
    )

    # Batch-load the full Integration rows in a single query instead of one
    # SELECT per binding inside the loop (P3.2 — the rows
    # are handed to ``_get_token`` so the legacy per-binding re-query is
    # gone).  ``_meta_cache`` keeps its historical shape for the delegated-
    # OAuth / multi-instance-naming consumers below.
    _all_integ_ids = [uuid.UUID(iid) for iid in bindings.values() if iid]
    # (name, description, auth_type) — auth_type drives delegated-OAuth token
    # acquisition for Azure security providers (defender / sentinel).
    _meta_cache: dict[str, tuple[str | None, str | None, str | None]] = {}
    _row_cache: dict[str, Integration] = {}
    if _all_integ_ids:
        _rows_result = await db.execute(
            select(Integration).where(Integration.id.in_(_all_integ_ids))
        )
        for _integ_row in _rows_result.scalars().all():
            _row_cache[str(_integ_row.id)] = _integ_row
            _name = (_integ_row.name or "").strip() or None
            _desc = (_integ_row.description or "").strip() or None
            _meta_cache[str(_integ_row.id)] = (_name, _desc, _integ_row.auth_type)

    # Whose account each bound integration acts with. A ``per_user``
    # integration acts with the acting person's own credential - never with
    # its holder's for anyone else, on every path that builds tools (chat,
    # assignments, flows, the widget...): the exception, for that mode only,
    # to a bound credential being used as bound. One with no
    # credential for that person gives no tools, and the turn says so.
    # ``credential_user_id`` names the acting person where ``user_id`` is not
    # set (the widget, a flow node: the agent's / the flow's owner).
    _acting_user_id = context.get("credential_user_id") or context.get("user_id")
    _holders: dict[str, Any] = {}
    _own_account_missing: dict[str, Integration] = {}
    # The acting person's groups, read once for every per_user binding.
    _acting_groups: list[str] | None = None
    for _iid, _integ_row in list(_row_cache.items()):
        if not is_per_user(_integ_row):
            _holders[_iid] = _integ_row
            continue
        _acting_uuid = normalize_acting_user_id(_acting_user_id)
        if _acting_groups is None and _acting_uuid is not None:
            from app.api.v1.helpers import get_user_group_ids

            _acting_groups = await get_user_group_ids(db, _acting_uuid)
        _holder = await resolve_token_holder(
            db, _integ_row, _acting_user_id, user_group_ids=_acting_groups
        )
        if _holder is None:
            _own_account_missing[_iid] = _row_cache.pop(_iid)
        else:
            _holders[_iid] = _holder

    # The Email signature setting of each email-sending integration,
    # resolved per integration: the agent's binding override, else the
    # integration's setting, else on; the signature integration chosen, else
    # the one bound alongside, else the one the acting person may use.
    from app.services.email_signature_settings import (
        SIGNATURE_CONTEXT_KEY,
        plan_signatures,
    )

    _signature_plans = await plan_signatures(
        db,
        _row_cache,
        overrides=context.get(SIGNATURE_CONTEXT_KEY),
        acting_user_id=_acting_user_id,
    )

    # The integrations routed through a relay group: their tools, and
    # every request those tools make, leave from a relay, never from here.
    from app.services.relays import load_egress_routes

    _routes = await load_egress_routes(
        db,
        [
            ("integration", _iid, getattr(_row, "relay_group_id", None))
            for _iid, _row in _row_cache.items()
        ],
    )

    # The platforms XTM One reaches on an internal URL: their tools call
    # it, and hand back their links on the platform's registered URL.
    _internally_reached = await platforms_reached_internally(
        db,
        [
            _iid
            for _iid, _row in _row_cache.items()
            if _row.auth_type == "platform_managed"
        ],
    )

    # Proactively refresh every expiring bound OAuth token in PARALLEL
    # (P3.1).  Previously each expiring token refreshed
    # inline and sequentially inside ``_get_token`` — N x up to 30 s worst
    # case in the pre-loop setup phase.  Each refresh uses its own DB
    # session; ``_get_token`` consumes the results below.
    _prefetched_tokens: dict[str, str] = {}
    _refresh_attempted: set[str] = set()
    if _row_cache:
        try:
            (
                _prefetched_tokens,
                _refresh_attempted,
            ) = await _parallel_refresh_expiring_tokens(
                db, list(_holders.values())
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            # Best-effort: a broken parallel pass degrades to the legacy
            # sequential inline refresh inside ``_get_token``.
            logger.warning(
                "Parallel OAuth refresh pass failed — falling back to "
                "sequential inline refresh",
                exc_info=True,
            )
            _prefetched_tokens, _refresh_attempted = {}, set()

    missing_bindings = context.setdefault("missing_integration_bindings", [])

    # Multi-instance support: when several bindings resolve the SAME
    # provider (e.g. two Microsoft integrations with different accounts),
    # every instance — the FIRST included — must get uniquely named tools
    # routed to its own credentials. Count instances per base provider up
    # front; ``binding_key != provider`` is NOT a reliable signal because
    # ``flatten_bindings`` gives the first instance a bare provider key.
    # Only bindings that resolved to a REAL Integration row participate: the
    # main loop below skips stale references (deleted integrations), so a
    # provider with one usable instance plus one stale binding must remain a
    # single-instance provider — its base tool names must not change because
    # of an unrelated broken binding.
    _provider_instance_counts: dict[str, int] = {}
    for _bk, _iid in bindings.items():
        if not _iid or str(_iid) not in _row_cache:
            continue
        _base_provider = _bk.split("#")[0]
        _provider_instance_counts[_base_provider] = (
            _provider_instance_counts.get(_base_provider, 0) + 1
        )
    _used_instance_slugs: dict[str, set[str]] = {}
    # A2A tool names handed out so far; the counterpart of ``_used_instance_slugs``.
    _a2a_tool_names: set[str] = set()
    # The bindings whose write tools are withheld and refused.
    from app.services.integration_read_only import (
        READ_ONLY_CONTEXT_KEY,
        is_read_only_binding,
        withhold_write_tools,
    )

    _read_only_ids = context.get(READ_ONLY_CONTEXT_KEY) or frozenset()

    def _read_only_label(integration_id: str, provider: str) -> str:
        name = _meta_cache.get(integration_id, (None, None, None))[0]
        return f"the {provider} integration '{name}'" if name else f"the {provider} integration"

    for binding_key, integration_id in bindings.items():
        if not integration_id:
            continue

        # Support multi-instance providers: binding keys may have a "#N"
        # suffix (e.g. "opencti#2") to distinguish multiple instances of
        # the same provider.  Strip it to get the real provider name.
        provider = binding_key.split("#")[0]

        try:
            _parsed_id = uuid.UUID(str(integration_id))
        except (ValueError, TypeError):
            _parsed_id = None

        if _parsed_id is not None and str(_parsed_id) in _own_account_missing:
            logger.info(
                "Integration %s acts with each person's own account and %s has "
                "not connected theirs [%s] — skipping provider '%s' tools",
                integration_id,
                _acting_user_id or "nobody",
                source_label,
                provider,
            )
            if isinstance(missing_bindings, list):
                missing_bindings.append(
                    own_account_not_connected_binding(
                        _own_account_missing[str(_parsed_id)], binding_key=binding_key
                    )
                )
            continue

        if _parsed_id is not None and str(_parsed_id) not in _row_cache:
            logger.error(
                "Integration %s referenced by binding key '%s' could not be "
                "resolved [%s] — skipping provider '%s' tools for this binding",
                integration_id,
                binding_key,
                source_label,
                provider,
            )
            if isinstance(missing_bindings, list):
                missing_bindings.append(
                    {
                        "binding_key": binding_key,
                        "provider": provider,
                        "integration_id": str(_parsed_id),
                    }
                )
            continue

        # Bind per-provider context so every downstream warning includes
        # which integration (provider + id) is affected.  The integration
        # name is added later by _get_token once the row is loaded.
        structlog.contextvars.bind_contextvars(
            integration_provider=provider,
            integration_id=integration_id,
        )
        # A2A outbound is resolved BEFORE the token step, because it has no
        # stored token to resolve: a bearer is minted per call from the
        # registration's credentials, so ``_get_token`` would find nothing and
        # the ``if not token: continue`` below would silently drop the
        # delegation tool — a tool that exists, is tested, and never reaches
        # a run.
        a2a_defs = await _build_a2a_delegation_tool(
            db, provider, integration_id, executor, context, taken=_a2a_tool_names
        )
        if a2a_defs is not None:
            definitions.extend(a2a_defs)
            continue

        try:
            try:
                token = await _get_token(
                    db,
                    integration_id,
                    provider,
                    user_id=context.get("user_id"),
                    source_label=source_label,
                    binding_origin=(
                        (context.get("binding_origin_map") or {}).get(binding_key)
                        or (context.get("binding_origin_map") or {}).get(provider)
                        or ""
                    ),
                    # Batch-loaded row (P3.2) + parallel refresh outcome
                    # (P3.1).  A row missing from the cache (malformed /
                    # unknown id) falls back to the per-id SELECT inside
                    # ``_get_token``. The holder is the row, or the acting
                    # person's own credential on it.
                    integ=_holders.get(integration_id),
                    prefetched_token=_prefetched_tokens.get(integration_id),
                    refresh_attempted=integration_id in _refresh_attempted,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to get token for %s → %s [%s]: %s",
                    provider,
                    integration_id,
                    source_label,
                    format_exception_message(exc),
                )
                continue
            if not token:
                await _report_invalid_config(
                    _row_cache.get(str(integration_id)),
                    binding_key=binding_key,
                    missing_bindings=missing_bindings,
                    context=context,
                )
                continue

            builder = PROVIDER_TOOLS.get(provider)
            if not builder:
                if provider.startswith("custom:"):
                    row_result = await db.execute(
                        select(CustomIntegrationDefinition).where(
                            CustomIntegrationDefinition.provider_key == provider
                        )
                    )
                    row = row_result.scalar_one_or_none()
                    if row and not bool(row.enabled):
                        # Disable is a reversible kill-switch: besides
                        # blocking new integration creation, it must stop
                        # existing integrations from resolving tools —
                        # otherwise a "disabled" provider stays effectively
                        # usable. (Destructive cleanup is the uninstall
                        # endpoint's job.)
                        logger.info(
                            "Custom provider '%s' is disabled — skipping tool "
                            "resolution [%s]",
                            provider,
                            source_label,
                        )
                        continue
                    if row:
                        # Reuse the batch-loaded row (P3.2); fall back to a
                        # per-id SELECT only when the cache misses (malformed /
                        # unknown id), mirroring the token path above.
                        integration_row = _row_cache.get(integration_id)
                        if integration_row is None:
                            integration_row_result = await db.execute(
                                select(Integration).where(
                                    Integration.id == uuid.UUID(str(integration_id))
                                )
                            )
                            integration_row = integration_row_result.scalar_one_or_none()
                        # The holder's: a person's credential adds what their
                        # sign-in learned about their account.
                        integration_row = _holders.get(integration_id, integration_row)
                        integration_extra_config = (
                            await _reveal_extra(integration_row)
                            if integration_row and integration_row.extra_config_enc
                            else None
                        )
                        custom_defs = _register_custom_declared_tools(
                            executor,
                            provider=provider,
                            integration_id=str(integration_id),
                            metadata_json=row.metadata_json,
                            integration_token=token,
                            integration_extra_config=integration_extra_config,
                        )
                        # A custom provider's catalog is its definition row.
                        custom_defs = [dataclasses.replace(d, source_label=row.name) for d in custom_defs or []]
                        if custom_defs:
                            if _provider_instance_counts.get(provider, 0) > 1:
                                # Same clobbering hazard as built-in providers:
                                # a second instance of the same custom
                                # provider re-registers the same declared tool
                                # names on the shared executor.
                                _c_name, _c_desc, _ = _meta_cache.get(
                                    integration_id, (None, None, None)
                                )
                                try:
                                    custom_defs = _suffix_instance_tools(
                                        custom_defs,
                                        executor,
                                        provider=provider,
                                        integ_name=_c_name,
                                        integ_desc=_c_desc,
                                        integration_id=str(integration_id),
                                        used_slugs=_used_instance_slugs,
                                        tool_slug=_stored_tool_slug(_row_cache.get(integration_id)),
                                    )
                                except InstanceSlugBudgetError as exc:
                                    # Declared tool names are user-authored, so
                                    # a name too long to suffix is reachable
                                    # here. Skip this instance's tools (its
                                    # defs never reach the LLM) rather than
                                    # emitting >64-char names that would fail
                                    # the whole request.
                                    logger.error(
                                        "Multi-instance suffixing failed for "
                                        "custom provider '%s' (integration "
                                        "%s) — skipping its tools: %s",
                                        provider,
                                        integration_id,
                                        exc,
                                    )
                                    continue
                            if is_read_only_binding(integration_id, _read_only_ids):
                                custom_defs = withhold_write_tools(
                                    custom_defs,
                                    executor,
                                    integration_label=_read_only_label(
                                        str(integration_id), provider
                                    ),
                                )
                            definitions.extend(custom_defs)
                            logger.info(
                                "Loaded %d declarative custom tool(s) for provider %s [%s]",
                                len(custom_defs),
                                provider,
                                source_label,
                            )
                        else:
                            logger.warning(
                                "Custom provider '%s' has no declared tools in metadata [%s]",
                                provider,
                                source_label,
                            )
                    else:
                        logger.warning(
                            "Custom provider '%s' not found while resolving tools [%s]",
                            provider,
                            source_label,
                        )
                else:
                    logger.debug(
                        "No tool builder for provider '%s' — skipping [%s]",
                        provider,
                        source_label,
                    )
                continue

            try:
                # Platform-managed integrations (OpenAEV / OpenCTI / OpenCRQ)
                # carry no OAuth refresh token: the OAuth refresh callback is a
                # no-op for them. Give them a JWT re-minter instead so a mid-run
                # 401 re-issues the short-lived cross-platform JWT (WS2) rather
                # than surfacing a dead 401 for the rest of the run.
                _integ_auth_type = _meta_cache.get(
                    integration_id, (None, None, None)
                )[2]
                if _integ_auth_type == "platform_managed":
                    refresh_fn = _make_platform_jwt_refresh_fn(
                        integration_id, context.get("user_id")
                    )
                else:
                    # Keyed by the holder: a person's refresh refreshes their
                    # own credential.
                    _holder = _holders.get(integration_id)
                    refresh_fn = _make_refresh_fn(
                        db, holder_ref(_holder) if _holder is not None else integration_id
                    )
                _builder_kwargs: dict[str, Any] = {"refresh_fn": refresh_fn}
                # Forward scope_headers to provider build_tools so providers
                # can pick up request-scoped headers (e.g. opencti-draft-id).
                _ctx_scope_headers = context.get("scope_headers")
                if _ctx_scope_headers:
                    _builder_kwargs["scope_headers"] = _ctx_scope_headers
                # Forward the autonomous (server-side, no interactive user) hint
                # ONLY to providers that accept it — the OpenCTI builder uses it
                # to auto-provision a draft instead of asking the user. Several
                # other provider builders take a fixed signature (no **kwargs),
                # so this must stay provider-scoped.
                if provider == "opencti" and context.get("autonomous"):
                    _builder_kwargs["autonomous"] = True
                # Platform MCP servers (/mcp/opencti) bypass the draft gate:
                # they are stateless (a draft header set by one JSON-RPC
                # request cannot persist to the next) so knowledge mutations
                # write directly to the live graph. Provider-scoped like
                # ``autonomous`` — other builders take fixed signatures.
                if provider == "opencti" and context.get("direct_mutations"):
                    _builder_kwargs["direct_mutations"] = True
                # Live writes (off by default): knowledge changes skip the draft
                # gate, and apply_opencti_draft and create_opencti_label are
                # offered; off, neither is registered. The agent binding's
                # override wins over the integration's setting, and a read-only
                # binding never writes live (services/integration_live_writes).
                # Every run type builds its OpenCTI tools here (chat, assignment,
                # lending, handover, consult).
                if provider == "opencti":
                    from app.services.integration_live_writes import (
                        LIVE_WRITES_CONTEXT_KEY,
                        binding_override,
                        effective_live_writes,
                    )

                    _override = binding_override(context.get(LIVE_WRITES_CONTEXT_KEY), integration_id)
                    _live = (
                        None
                        if _override is not None
                        else await _get_extra_config_value(db, integration_id, "live_writes")
                    )
                    if effective_live_writes(
                        _live,
                        _override,
                        read_only=is_read_only_binding(integration_id, _read_only_ids),
                    ):
                        _builder_kwargs["live_writes"] = True
                    # A fleet gate naming apply_opencti_draft (the CTEM analyst
                    # review, #1480) offers it with Live writes off: the gate,
                    # not the setting, decides the validation. Never on a
                    # read-only binding.
                    from app.services.fleet_autonomy import FLEET_GATED_TOOLS_CONTEXT_KEY

                    if "apply_opencti_draft" in (
                        context.get(FLEET_GATED_TOOLS_CONTEXT_KEY) or ()
                    ) and not is_read_only_binding(integration_id, _read_only_ids):
                        _builder_kwargs["gated_draft_validation"] = True
                # When HITL is on, give OpenCTI a way to VERIFY a human approved
                # an irreversible action (entity merge) rather than trusting the
                # model's word for it. The run's ``human_input_history`` is the
                # only durable record of who answered what, so the check has to
                # read the database — hence a closure bound to this run, in the
                # same shape as ``persist_file_fn`` above.
                if (
                    provider == "opencti"
                    and context.get("hitl_enabled")
                    and context.get("run_id")
                ):
                    _builder_kwargs["hitl_approval_fn"] = _make_hitl_approval_fn(
                        str(context["run_id"])
                    )
                # GitHub write tools record a short-lived self-action marker per
                # mutated issue/PR so assignments with
                # ``ignore_self_triggered_events`` can drop the webhook echo of
                # their own writes (loop protection). See services/github_echo.py.
                if provider == "github":
                    _builder_kwargs["self_action_recorder"] = (
                        _make_github_self_action_recorder(integration_id)
                    )
                    # create_github_pr names the PR on the work item it is
                    # opened for.
                    _record_pr = _make_record_pull_request_fn(context)
                    if _record_pr is not None:
                        _builder_kwargs["record_pull_request"] = _record_pr
                        # ...and refuses to open one for a cancelled item (#11554).
                        _builder_kwargs["check_work_item"] = _make_work_item_write_check_fn(context)
                    # A commit pushed to an open pull request names it on the
                    # work item the run was dispatched for (#9819).
                    _record_push = _make_record_push_fn(context, _record_pr)
                    if _record_push is not None:
                        _builder_kwargs["record_push"] = _record_push
                    # merge_github_pr parks a PR whose full CI is red on its
                    # base's own failure, run again at the base's green (#12069).
                    _park = _make_park_full_ci_fn(context)
                    if _park is not None:
                        _builder_kwargs["park_full_ci"] = _park
                    # A branch another live run leases is not moved by this run.
                    _lease_check = _make_branch_lease_check_fn(context)
                    if _lease_check is not None:
                        _builder_kwargs["check_branch_lease"] = _lease_check
                    # An enforced repository Scope(s) is a hard boundary on the
                    # GitHub tools.
                    from app.integrations.providers.github.scope import (
                        repo_scope_from_bindings,
                    )

                    _repo_scope = repo_scope_from_bindings(_tool_scope)
                    if _repo_scope is not None:
                        _builder_kwargs["repo_scope"] = _repo_scope
                # An enforced Slack channel Scope(s) is a hard boundary on the
                # Slack tools, read from the same enforced scope.
                if provider == "slack":
                    from app.integrations.providers.slack_scope import (
                        channel_scope_from_bindings,
                    )

                    _channel_scope = channel_scope_from_bindings(_tool_scope)
                    if _channel_scope is not None:
                        _builder_kwargs["channel_scope"] = _channel_scope
                # A transcript embedded in a Teams recording (a channel's
                # SharePoint folder, a OneDrive) is read via the SharePoint
                # Stream media API, with a token for the recording's host.
                # Every provider registered as an email sender signs through
                # the same two kwargs (``email_signatures.MailSigner``).
                _signature_plan = _signature_plans.get(str(_parsed_id))
                if _signature_plan is not None:
                    _builder_kwargs["signature_plan"] = _signature_plan
                    _builder_kwargs["signature_fn"] = _signature_plan.signature_fn()
                if provider == "microsoft":
                    _builder_kwargs["sp_token_fn"] = _make_sharepoint_token_fn(
                        _holder_ref_of(_holders, integration_id)
                    )
                    # The Compose style field: the CSS of the text the mail
                    # tools write (empty: Outlook's native compose style).
                    _compose_style = _compose_style_of(_row_cache.get(integration_id))
                    if _compose_style is not None:
                        _builder_kwargs["compose_style"] = _compose_style
                    # An enforced Teams channel Scope(s) is a hard boundary on
                    # the Teams tools, read from the same enforced
                    # scope as the GitHub one above.
                    from app.integrations.providers.microsoft.teams_scope import (
                        teams_channel_scope_from_bindings,
                    )

                    _teams_scope = teams_channel_scope_from_bindings(_tool_scope)
                    if _teams_scope is not None:
                        _builder_kwargs["teams_channel_scope"] = _teams_scope
                # Defender XDR / Sentinel in delegated-OAuth mode mint a token per
                # Microsoft audience (Graph + Defender for Endpoint, or ARM + Log
                # Analytics) by redeeming the refresh token on demand.
                if provider in ("defender", "sentinel"):
                    _integ_auth_type = _meta_cache.get(
                        integration_id, (None, None, None)
                    )[2]
                    if _integ_auth_type == "oauth":
                        _builder_kwargs["token_acquirer"] = (
                            _make_azure_delegated_token_fn(
                                _holder_ref_of(_holders, integration_id)
                            )
                        )
                # Microsoft Purview spans two audiences (Graph for the compliance
                # pillar + the Data Map https://purview.azure.net for governance).
                # In delegated-OAuth mode the per-audience tokens are minted by
                # redeeming the refresh token; the governance account name (for
                # the Data Map base URL) is surfaced in both modes.
                if provider == "purview":
                    _builder_kwargs["account_name"] = await _get_extra_config_value(
                        db, integration_id, "account_name"
                    )
                    _integ_auth_type = _meta_cache.get(
                        integration_id, (None, None, None)
                    )[2]
                    if _integ_auth_type == "oauth":
                        _builder_kwargs["token_acquirer"] = (
                            _make_azure_delegated_token_fn(
                                _holder_ref_of(_holders, integration_id)
                            )
                        )
                # OpenAEV: the optional Tenant field picks the tenant on a
                # multi-tenant platform (empty: chosen from the memberships).
                if provider == "openaev":
                    _builder_kwargs["tenant_id"] = await _get_extra_config_value(
                        db, integration_id, "tenant_id"
                    )
                # OpenCRQ: the connected instance's manifest decides which tools
                # it can serve, so the provider withholds the others.
                if provider == "opencrq":
                    _builder_kwargs["opencrq_manifest"] = await _opencrq_manifest(db, integration_id)
                # An enforced HubSpot CRM object-type Scope(s) is a hard
                # boundary on the HubSpot tools, read from the same
                # enforced scope as the GitHub one above.
                if provider == "hubspot":
                    from app.integrations.providers.hubspot_scope import (
                        object_type_scope_from_bindings,
                    )

                    _object_type_scope = object_type_scope_from_bindings(_tool_scope)
                    if _object_type_scope is not None:
                        _builder_kwargs["object_type_scope"] = _object_type_scope
                # The download tools of ``_PERSIST_FILE_PROVIDERS`` (HackerOne's
                # report PDF, the Jira / ServiceNow / Slack attachment
                # downloads, download_notion_file, download_figma_image,
                # download_drata_file, the GitHub / OpenCTI / OpenAEV / HiBob
                # downloads...) persist the downloaded bytes as a real
                # FileAttachment so they surface as a download card. Inject a
                # persistence callback bound to this request's identity (it
                # writes on its own independent session, not the request db).
                # Only inject when the context carries a scope identifier (user /
                # conversation / run): without one the FileAttachment would be a
                # NULL-scope orphan that the download endpoint rejects (403), so
                # the provider should fall back to an inline FileRef instead
                # (e.g. the evaluation engine passes only source/agent).
                if provider in _PERSIST_FILE_PROVIDERS and any(
                    context.get(k) for k in ("user_id", "conversation_id", "run_id")
                ):
                    _builder_kwargs["persist_file_fn"] = _make_persist_file_fn(
                        context, executor
                    )
                # Pass integration_id to the Notion provider so it can track
                # cross-run 404 failures on stale Notion references and emit
                # an escalated WARNING after repeated misses.
                # Microsoft: the mailbox an agent's send goes through, so that
                # mailbox's new_email poller skips it.
                # WhatsApp: the integration whose received messages its read
                # tools return.
                if provider in ("notion", "microsoft", "whatsapp"):
                    _builder_kwargs["integration_id"] = str(integration_id)
                # Platform email: the agent the daily limit counts and the
                # person its audit entry is anchored on.
                if provider == "platform_email":
                    _builder_kwargs["agent_id"] = str(context.get("agent_id") or "")
                    _builder_kwargs["user_id"] = str(context.get("user_id") or "")
                # Microsoft: the agent the run acts for scopes the mail send
                # guard (one answer per conversation across its runs).
                if provider == "microsoft" and context.get("agent_id"):
                    _builder_kwargs["agent_id"] = str(context["agent_id"])
                # Microsoft, WhatsApp: what produced the run, logged with every
                # message sent through an agent account on the integration.
                if provider in ("microsoft", "whatsapp"):
                    from app.services.agent_accounts.sent_log import origin_of

                    _builder_kwargs["send_origin"] = origin_of(context)
                # OpenAPI integrations carry their own API: pre-load the
                # converted spec (selected tools + auth option) and the
                # decrypted config the sync builder needs. No spec → no tools.
                if provider == "openapi":
                    _openapi_row = _row_cache.get(integration_id)
                    _openapi_runtime = (
                        await load_openapi_runtime(db, _openapi_row)
                        if _openapi_row is not None
                        else None
                    )
                    if _openapi_runtime is None:
                        logger.warning(
                            "OpenAPI integration %s has no imported spec — no tools [%s]",
                            integration_id,
                            source_label,
                        )
                        continue
                    _builder_kwargs["openapi_runtime"] = _openapi_runtime
                # How the platform names what it holds, on this integration's
                # own connection, for a caller that collects it.
                _platform_names: list[Callable[..., Awaitable[Any]]] = []
                _names_sink = context.get(PLATFORM_NAMES_CONTEXT_KEY)
                if isinstance(_names_sink, list) and _declares(builder, "platform_names_sink"):
                    _builder_kwargs["platform_names_sink"] = _platform_names.append
                # A module XTM One registered names the version it registered beside
                # its own live version read (#888): a platform-managed integration only.
                _version_row = _row_cache.get(integration_id)
                if (
                    _version_row is not None
                    and _version_row.auth_type == "platform_managed"
                    and _declares(builder, "registered_version_fn")
                ):
                    _builder_kwargs["registered_version_fn"] = registered_version_reader(str(integration_id))
                _attack_found: list[tuple[str, str]] = []
                _attack_sink = context.get(ATTACK_SOURCES_CONTEXT_KEY)
                if isinstance(_attack_sink, list) and _declares(builder, "attack_source_sink"):
                    _builder_kwargs["attack_source_sink"] = _attack_found.append
                defs = builder(token, executor, **_builder_kwargs)
                # A send tool's text reaches a surface that draws no chat card:
                # its artifact markers are sent as links (#9990).
                if defs:
                    send_markers_as_links(defs, executor, context)
                _route = _routes.get(str(integration_id))
                if defs and _route is not None:
                    _route_through_relay(defs, executor, _route)
                if defs and isinstance(_attack_sink, list):
                    _attack_sink.extend((url, version, _route) for url, version in _attack_found)
                _reached = _internally_reached.get(str(integration_id))
                if defs and _reached is not None:
                    _show_tool_results_on_public_url(defs, executor, _reached)
                if defs:
                    integ_name, integ_desc, _ = _meta_cache.get(
                        integration_id, (None, None, None)
                    )
                    is_multi_instance = (
                        _provider_instance_counts.get(provider, 0) > 1
                    )
                    if is_multi_instance:
                        # Every instance is suffixed + re-routed (see
                        # _suffix_instance_tools) so no base-name handler can
                        # silently execute with another account's credentials.
                        enriched = _suffix_instance_tools(
                            defs,
                            executor,
                            provider=provider,
                            integ_name=integ_name,
                            integ_desc=integ_desc,
                            integration_id=str(integration_id),
                            used_slugs=_used_instance_slugs,
                            tool_slug=_stored_tool_slug(_row_cache.get(integration_id)),
                        )
                    else:
                        enriched = []
                        for d in defs:
                            kw: dict[str, object] = {
                                "entity_id": str(integration_id)
                            }
                            if integ_desc:
                                kw["description"] = (
                                    f"[Integration context: {integ_desc}] "
                                    f"{d.description}"
                                )
                            enriched.append(dataclasses.replace(d, **kw))

                    if is_read_only_binding(integration_id, _read_only_ids):
                        enriched = withhold_write_tools(
                            enriched,
                            executor,
                            integration_label=_read_only_label(
                                str(integration_id), provider
                            ),
                        )
                    definitions.extend(enriched)
                    if isinstance(_names_sink, list):
                        _names_sink.extend(_routed_lookup(lookup, _route) for lookup in _platform_names)
                    logger.debug(
                        "Loaded %d tools for provider %s [%s]",
                        len(defs),
                        provider,
                        source_label,
                    )
                else:
                    logger.warning(
                        "Provider '%s' (integration %s) returned 0 tools — likely misconfigured. [%s]",
                        provider,
                        integration_id,
                        source_label,
                    )
            except Exception as exc:
                # error (was warning): the agent silently runs WITHOUT this
                # provider's tools — same event class is already logged at
                # error by assignment_engine and general_chat.
                logger.error(
                    "Failed to build tools for provider %s (integration %s) [%s]: %s",
                    provider,
                    integration_id,
                    source_label,
                    format_exception_message(exc),
                    exc_info=True,
                )
        finally:
            # Unbind per-provider fields so the next iteration (or caller)
            # doesn't carry stale integration context.
            structlog.contextvars.unbind_contextvars(
                "integration_provider",
                "integration_id",
                "integration_name",
            )

    return definitions, executor


from app.channels.base import CHANNEL_TO_BOT_PROVIDER, teams_channel_component
from app.core.utils import format_exception_message
from app.services.outbound_markers import send_markers_as_links


async def _get_channel_bot_token(
    db: AsyncSession,
    channel_bot_id: str,
    provider: str,
) -> tuple[str | None, ChannelBot | None]:
    """Get a decrypted token (and the bot row) for a channel bot.

    Channel bots don't use OAuth, so no refresh logic is needed — just
    decrypt the stored credential.  Returns ``(token, bot)`` where *token*
    is the compound format expected by each provider's tool builder; the
    bot row is returned so callers can enforce bot-level configuration
    (e.g. ``allowed_publish_channels``).
    """
    result = await db.execute(
        select(ChannelBot).where(ChannelBot.id == uuid.UUID(channel_bot_id))
    )
    bot = result.scalar_one_or_none()
    if not bot:
        logger.warning("Channel bot %s not found — skipping tools", channel_bot_id)
        return None, None

    if bot.provider != provider:
        logger.warning(
            "Channel bot '%s' has provider '%s' but expected '%s' — skipping",
            bot.name,
            bot.provider,
            provider,
        )
        return None, None

    structlog.contextvars.bind_contextvars(integration_name=bot.name)

    # Build the token in the format expected by each provider's tool builder
    if provider == "teams_bot":
        app_id = decrypt_value(bot.client_id_enc) if bot.client_id_enc else ""
        app_secret = (
            (await reveal_field(bot, "client_secret_enc")) or ""
        )
        extra = await reveal_json_field(bot, "extra_config_enc") or {}
        tenant_id = extra.get("tenant_id", "")
        # teams_bot builder expects "tenant_id||client_id||client_secret"
        return (f"{tenant_id}||{app_id}||{app_secret}" if app_id else None), bot
    elif provider == "whatsapp_bot":
        access_token = (await reveal_field(bot, "api_key_enc")) or ""
        extra = await reveal_json_field(bot, "extra_config_enc") or {}
        phone_number_id = extra.get("phone_number_id", "")
        # whatsapp_bot builder expects "phone_number_id||access_token"
        return (f"{phone_number_id}||{access_token}" if access_token else None), bot
    else:
        # slack_bot, telegram_bot, discord_bot, googlechat_bot: simple api_key
        return ((await reveal_field(bot, "api_key_enc")) or None), bot


def _effective_publish_channels(
    agent_channels: list[dict] | None,
    bot_channels: list[dict] | None,
    channel_type: str | None = None,
) -> tuple[list[dict], bool]:
    """Merge agent-level ``publish_channels`` with the bot-level whitelist.

    The bot's ``allowed_publish_channels`` is the hard boundary set by the
    admin — previously it was only checked at agent-save time for non-admin
    personal agents, so admin/company-managed agents (or an empty agent
    selection) got fully unrestricted publish tools despite the bot
    whitelist.

    For Teams, channel IDs exist in two formats (``{entra_group_guid}:{id}``
    from the Graph picker vs thread-style ``19:...@thread.tacv2`` team
    components), so each agent entry matches by exact ID first, then by the
    normalized channel component — the same per-entry rule the
    receive-whitelist matching uses (see ``_channel_id_matches`` in
    ``channels/router.py`` and ``teams_channel_component``).

    Returns ``(channels, blocked)``:
      - bot list empty → the agent list as-is (may be empty = unrestricted)
      - agent list empty → the bot list (bot boundary applies)
      - both set → the intersection; when the intersection is EMPTY the
        second element is ``True`` (nothing is publishable — the caller
        should disable publish tools for this channel).
    """
    agent_list = agent_channels or []
    bot_list = bot_channels or []
    if not bot_list:
        return agent_list, False
    if not agent_list:
        return bot_list, False
    bot_ids = {c.get("id") for c in bot_list if c.get("id")}
    bot_components = (
        {teams_channel_component(i) for i in bot_ids if i}
        if channel_type == "teams"
        else set()
    )

    def _matches(entry: dict) -> bool:
        entry_id = entry.get("id")
        if not entry_id:
            return False
        if entry_id in bot_ids:
            return True
        return bool(
            bot_components and teams_channel_component(entry_id) in bot_components
        )

    merged = [c for c in agent_list if _matches(c)]
    return merged, not merged


#: Sentinel whitelist entry used when the agent/bot publish intersection is
#: empty.  Providers enforce publish whitelists at call time via
#: ``is_publish_allowed``, so a never-matching entry makes every
#: channel-keyed publish/write tool fail closed with an explicit "Access
#: denied" result (previously the WHOLE channel tool set was silently
#: skipped).  Because providers historically derive their READ
#: scoping from the same ``publish_channels`` kwarg, the blocked case also
#: passes ``read_channels`` (the agent's own selection) so read tools stay
#: genuinely usable, and ``publish_blocked=True`` so writes that are not
#: keyed by a channel ID (e.g. WhatsApp mark-read/react, Google Chat
#: message deletion) fail closed too.
_BLOCKED_PUBLISH_CHANNEL: dict = {
    "id": "__no_publishable_channels__",
    "name": "(agent publish channels do not overlap the bot whitelist)",
}


async def build_channel_tools(
    db: AsyncSession,
    channel_config: dict[str, dict] | None,
    *,
    context: dict[str, Any] | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build tools for channel bots from channel_config.

    Loads tools for every channel that has a ``channel_bot_id`` configured,
    regardless of the ``receive_enabled`` / ``publish_enabled`` toggles.
    Applies ``publish_channels`` as a whitelist and ``allowed_tools``
    for tool-level scoping.
    """
    executor = _SafeIntegrationExecutor()
    definitions: list[ToolDefinition] = []

    if not channel_config:
        return definitions, executor

    ctx = context or {}
    ctx_source = ctx.get("source", "unknown")

    for ch_type, ch_cfg in channel_config.items():
        channel_bot_id = ch_cfg.get("channel_bot_id")
        if not channel_bot_id:
            continue

        provider = CHANNEL_TO_BOT_PROVIDER.get(ch_type)
        if not provider:
            continue

        structlog.contextvars.bind_contextvars(
            integration_provider=provider,
            channel_bot_id=channel_bot_id,
        )
        try:
            try:
                token, bot = await _get_channel_bot_token(
                    db, channel_bot_id, provider
                )
            except Exception as exc:
                logger.warning(
                    "Failed to get token for channel %s → %s [%s]: %s",
                    ch_type,
                    channel_bot_id,
                    ctx_source,
                    format_exception_message(exc),
                )
                continue
            if not token:
                continue

            builder = CHANNEL_BOT_TOOLS.get(provider)
            if not builder:
                continue

            refresh_fn = _noop_refresh
            extra_kwargs: dict[str, Any] = {"refresh_fn": refresh_fn}

            # File persistence for download tools (e.g. Teams
            # download_teams_bot_file) — same contract as the integration
            # path: only inject when the context carries a scope identifier,
            # otherwise the FileAttachment would be a NULL-scope orphan.
            if provider in _PERSIST_FILE_CHANNEL_BOTS and any(
                ctx.get(k) for k in ("user_id", "conversation_id", "run_id")
            ):
                extra_kwargs["persist_file_fn"] = _make_persist_file_fn(ctx, executor)

            # Effective publish whitelist = agent-level publish_channels
            # bounded by the bot-level allowed_publish_channels (the
            # admin-set hard boundary, previously never enforced at
            # runtime for admin/company-managed agents).  Agents with the
            # admin-only ``bypass_whitelist`` flag ignore the bot boundary
            # entirely — mirroring the receive-side bypass in
            # ``channels/router.py``.
            if ch_cfg.get("bypass_whitelist"):
                publish_channels = ch_cfg.get("publish_channels", []) or []
                blocked = False
            else:
                publish_channels, blocked = _effective_publish_channels(
                    ch_cfg.get("publish_channels", []),
                    bot.allowed_publish_channels if bot else None,
                    ch_type,
                )
            if blocked:
                # Nothing publishable — keep the tool definitions loaded but
                # make every publish/write call fail closed with an explicit
                # "Access denied": channel-keyed writes via the
                # never-matching sentinel whitelist, channel-less writes via
                # ``publish_blocked``.  Read tools keep working, scoped to
                # the agent's own channel selection via ``read_channels``.
                # The whole channel tool set used to be skipped here,
                # silently removing even list/read/download tools.
                logger.warning(
                    "Channel %s: agent publish_channels have no overlap with "
                    "the bot's allowed_publish_channels — publish tools fail "
                    "closed, read tools stay scoped to the agent's selection "
                    "[%s]",
                    ch_type,
                    ctx_source,
                )
                extra_kwargs["publish_channels"] = [dict(_BLOCKED_PUBLISH_CHANNEL)]
                extra_kwargs["read_channels"] = list(
                    ch_cfg.get("publish_channels", []) or []
                )
                extra_kwargs["publish_blocked"] = True
            elif publish_channels:
                extra_kwargs["publish_channels"] = publish_channels

            defs = builder(token, executor, **extra_kwargs)
            if defs:
                # A channel bot's send reaches Teams / Slack / WhatsApp: its
                # artifact markers are sent as links (#9990).
                send_markers_as_links(defs, executor, ctx)
                # Apply tool-level scoping from channel_config
                allowed_tools = ch_cfg.get("allowed_tools")
                if allowed_tools is not None:
                    allowed_set = set(allowed_tools)
                    defs = [d for d in defs if d.name in allowed_set]

                # ``channel_bot:<provider>:<id>``. Both halves are
                # load-bearing: the approval gate keys its per-bot switch on the
                # id, because two bots of one provider can disagree, while the
                # activity trail, the loop's dynamic-tool accounting and the
                # evaluation engine all read the provider out of the source.
                # Stamping the id alone dropped bot calls out of all three.
                definitions.extend(
                    dataclasses.replace(
                        d,
                        source=f"channel_bot:{provider}:{channel_bot_id}",
                        entity_id=str(channel_bot_id),
                    )
                    for d in defs
                )
                logger.debug(
                    "Loaded %d channel tools for %s [%s]",
                    len(defs),
                    ch_type,
                    ctx_source,
                )
        finally:
            structlog.contextvars.unbind_contextvars(
                "integration_provider",
                "channel_bot_id",
                "integration_name",
            )

    return with_source_labels(definitions), executor


def with_source_labels(definitions: list[ToolDefinition]) -> list[ToolDefinition]:
    """*definitions* with the display name their source has in the catalogs.

    The label the tool catalogue shows the model for a group of tools: an
    integration's or a channel bot's catalog ``name``, never a list of names
    kept by the generic layers.
    """
    from app.api.v1.channel_bots import CHANNEL_BOT_CATALOG
    from app.api.v1.integration_catalog import PROVIDER_CATALOG

    # A channel bot's tools built in ``channel_bot_mode`` keep an
    # ``integration:<bot provider>`` source: both catalogs answer for it.
    catalogs = {"integration": (PROVIDER_CATALOG, CHANNEL_BOT_CATALOG), "channel_bot": (CHANNEL_BOT_CATALOG,)}
    labelled = []
    for definition in definitions:
        kind, _, rest = (definition.source or "").partition(":")
        provider = rest.partition(":")[0].partition("#")[0]
        entry = next((catalog[provider] for catalog in catalogs.get(kind, ()) if provider in catalog), {})
        name = entry.get("name")
        if name and not definition.source_label:
            definition = dataclasses.replace(definition, source_label=name)
        labelled.append(definition)
    return labelled


def format_channel_context(channel_config: dict[str, dict] | None) -> str:
    """Build a system-prompt fragment describing the configured publish channels.

    When channels are whitelisted the LLM gets the exact team/channel IDs and
    names so it can call send/reply tools directly without needing to list
    channels first via the API.
    """
    if not channel_config:
        return ""

    sections: list[str] = []
    for ch_type, ch_cfg in channel_config.items():
        if not ch_cfg.get("publish_enabled", False):
            continue
        # Only advertise channels the agent actually has send tools for —
        # providers without a tool builder (e.g. Google Chat) would tell
        # the LLM to publish somewhere it has no tool to reach.
        provider = CHANNEL_TO_BOT_PROVIDER.get(ch_type)
        if not provider or provider not in CHANNEL_BOT_TOOLS:
            continue
        publish_channels: list[dict] = ch_cfg.get("publish_channels", [])
        if not publish_channels:
            continue

        label = ch_type.replace("_", " ").title()  # e.g. "teams" → "Teams"
        lines: list[str] = [f"  {label}:"]
        for ch in publish_channels:
            ch_id = ch.get("id", "")
            ch_name = ch.get("name", ch_id)
            # Teams IDs are in "team_id:channel_id" format
            if ":" in ch_id:
                team_id, channel_id = ch_id.split(":", 1)
                lines.append(
                    f"    - #{ch_name}  (team_id: {team_id}, channel_id: {channel_id})"
                )
            else:
                lines.append(f"    - #{ch_name}  (id: {ch_id})")
        sections.append("\n".join(lines))

    if not sections:
        return ""

    return (
        "You have access to the following channels for publishing messages. "
        "Use the IDs below directly when sending messages — no need to list "
        "channels first.\n" + "\n".join(sections)
    )


# --- Per-user ("template") binding resolution guardrails ---------------
#
# A per-user binding ("Resolved per user at runtime" in the UI) means: at run
# time, use the ACTING USER's own integration of this provider, falling back to
# a genuine company-wide instance. It must NEVER resolve to a *colleague's*
# personal integration — that silently returns their mailbox / chats / CRM data
# under the acting user's identity, which is both a data leak and invisible to
# everyone involved.
#
# Two flavours count as a legitimate company-wide fallback:
#   • ``is_company_managed`` — the explicit "Company-managed" instance.
#   • ``auth_type == "platform_managed"`` — credentials are minted per acting
#     user from platform config (``_generate_platform_jwt``), so the row carries
#     no personal token. Historically this clause was unconditional; it is now
#     paired with a company-wide check so one user's *private* platform-managed
#     row can no longer be picked for everybody else (it used to be *preferred*
#     over the real company-managed instance by the "prefer private" tie-break).

#: ``binding_origin_map`` value for a binding the agent carries itself
#: (``Agent.integration_bindings``): chosen by the agent's owner, never resolved.
DIRECT_BINDING_ORIGIN = "direct_agent_binding"

#: ``binding_origin_map`` value for a per-user binding that resolved to the
#: acting user's OWN integration — the intended outcome.
TEMPLATE_ORIGIN_OWN = "template_binding"

#: ``binding_origin_map`` value for a per-user binding that resolved to a
#: company-wide instance because the acting user has none of their own. Distinct
#: from :data:`TEMPLATE_ORIGIN_OWN` so downstream logs and traces show which
#: credentials a run actually used.
TEMPLATE_ORIGIN_COMPANY_FALLBACK = "template_binding_company_fallback"


def is_company_wide_integration(integ: Any) -> bool:
    """True when *integ* is a genuine company-wide instance.

    Either an explicit "Company-managed" row or a ``platform_managed`` one
    (whose credentials are derived per acting user, not stored per owner).
    """
    if getattr(integ, "is_company_managed", False):
        return True
    return getattr(integ, "auth_type", None) == "platform_managed"


def is_foreign_personal_integration(
    integ: Any, acting_user_id: uuid.UUID | str | None
) -> bool:
    """True when *integ* is **another user's personal** integration.

    Company-wide rows are never "foreign" (they are shared on purpose), and an
    owner-less legacy row has nobody to leak from. Everything else is foreign
    unless it belongs to *acting_user_id* — including the case where there is no
    acting user at all, which is fail-closed on purpose.
    """
    if is_company_wide_integration(integ):
        return False
    owner = getattr(integ, "user_id", None)
    if owner is None:
        return False
    if acting_user_id is None:
        return True
    return str(owner) != str(acting_user_id)


def drop_foreign_personal_integrations(
    candidates: list[Integration],
    acting_user_id: uuid.UUID | str | None,
    *,
    provider: str = "",
) -> list[Integration]:
    """Remove another user's personal integrations from *candidates*.

    Defence in depth: the candidate queries already exclude them, so anything
    dropped here is a bug upstream (or a caller that forgot to scope its query)
    and is logged at WARNING with enough identity to trace the run.
    """
    kept = [
        c for c in candidates if not is_foreign_personal_integration(c, acting_user_id)
    ]
    if len(kept) != len(candidates):
        logger.warning(
            "Per-user binding guard: dropped %d %s integration(s) owned by "
            "another user while resolving for acting user %s — a per-user "
            "binding must never use a colleague's personal credentials",
            len(candidates) - len(kept),
            provider or "candidate",
            acting_user_id,
        )
    return kept


def template_candidate_clause(
    acting_user_uuid: uuid.UUID | None,
    user_group_ids: list[str] | None = None,
    *,
    is_admin: bool = False,
):
    """SQLAlchemy clause selecting rows eligible for per-user resolution.

    The acting user's OWN integrations, plus company-wide ones. A company-wide
    OAuth row is only eligible once its owner explicitly acknowledged sharing
    the token (``oauth_share_acknowledged``); without that gate,
    per-user resolution would re-leak exactly the tokens
    :func:`shadow_invisible_bindings` drops from fixed bindings.

    A company-managed row must also be visible to the acting user under the
    three-tier rule (``user_visible_filter``): one narrowed with
    ``access_restricted`` is eligible only for its grantees (user or group,
    *user_group_ids*), and with no acting user never - except in work that
    keeps main's tier with no account (``helpers.no_account_provenance``: the
    run of a company-managed assignment with no user), where every other read
    keeps it too; *is_admin* lifts that for a company-managed automation
    acting as an admin only. Callers resolve through
    :func:`template_candidate_filter`, which decides both.

    Group-shared personal rows are deliberately NOT eligible: group sharing
    grants *visibility*, never permission to run as the owner.

    Providers whose every integration is a different API (imported OpenAPI
    specs, :data:`PER_USER_UNRESOLVABLE_PROVIDERS`) are never eligible: "the
    user's own integration of that provider" could be an unrelated API.

    A company-managed integration whose people each connect their own account
    (``credential_mode = "per_user"``) is eligible for an acting user
    who connected theirs on it: the tool build then acts with that account.
    """
    from app.api.v1.helpers import caller_visible_filter, user_visible_filter

    visible = (
        user_visible_filter(Integration, acting_user_uuid, user_group_ids, is_admin=is_admin)
        if acting_user_uuid is not None
        else caller_visible_filter(Integration, None)
    )
    company_wide = and_(
        or_(
            # access_restricted: honoured through ``visible``.
            and_(Integration.is_company_managed.is_(True), visible),
            and_(
                Integration.auth_type == "platform_managed",
                Integration.is_company_managed.isnot(True),
            ),
        ),
        oauth_credentials_usable_clause(None),
    )
    resolvable = Integration.provider.notin_(PER_USER_UNRESOLVABLE_PROVIDERS)
    if acting_user_uuid is None:
        # No identity to resolve against — only company-wide rows are eligible.
        return and_(resolvable, company_wide)
    own_account_on_company = and_(
        # access_restricted: honoured through ``visible``, as ``company_wide``.
        Integration.is_company_managed.is_(True),
        Integration.credential_mode == CREDENTIAL_MODE_PER_USER,
        visible,
        exists().where(
            IntegrationUserCredential.integration_id == Integration.id,
            IntegrationUserCredential.user_id == acting_user_uuid,
            usable_credential_clause(),
        ),
    )
    return and_(
        resolvable,
        or_(Integration.user_id == acting_user_uuid, company_wide, own_account_on_company),
    )


async def template_candidate_filter(
    db: AsyncSession,
    acting_user_uuid: uuid.UUID | None,
    *,
    company_automation: bool = False,
):
    """:func:`template_candidate_clause` for *acting_user_uuid*, grants loaded.

    The one entry point of every per-user resolution path (agent chat, the
    pipeline fallback, assignments, evaluations), so a restricted
    company-managed integration resolves for its grantees only. An admin is
    held to the grants too: this picks an integration for the acting
    user, and the integration picker offers an admin what any user in their
    place would get (``integration_credentials.visible_integration_ids``),
    like the chat gate whose drops agent chat re-resolves here
    (:func:`shadow_invisible_bindings`).

    *company_automation* marks a company automation's own run of a
    company-managed agent: an assignment run, an evaluation. Acting as an
    admin, it keeps every company-managed row: an admin may
    set any integration on the agent (``integration_ids_writer_cannot_see``),
    so its template reaches nothing that admin could not have bound. The role
    is read for it only.
    """
    if acting_user_uuid is None:
        return template_candidate_clause(None)
    from app.api.v1.helpers import get_user_group_ids

    is_admin = False
    if company_automation:
        from app.models.user import User

        role = (
            await db.execute(select(User.role).where(User.id == acting_user_uuid))
        ).scalar_one_or_none()
        is_admin = role == "admin"
    return template_candidate_clause(
        acting_user_uuid,
        await get_user_group_ids(db, acting_user_uuid),
        is_admin=is_admin,
    )


def classify_template_pick(
    integ: Integration | None,
    acting_user_id: uuid.UUID | str | None,
    *,
    provider: str = "",
    context: str = "",
) -> tuple[Integration | None, str | None]:
    """Label a per-user pick as the user's own or a company-wide fallback.

    Returns ``(integ, origin)`` with *origin* one of
    :data:`TEMPLATE_ORIGIN_OWN` / :data:`TEMPLATE_ORIGIN_COMPANY_FALLBACK`, and
    ``(None, None)`` for no pick. The fallback case is logged at INFO so a run
    that used shared rather than personal credentials is always traceable
    instead of being indistinguishable from a personal one.
    """
    if integ is None:
        return None, None
    owner = getattr(integ, "user_id", None)
    is_fallback = is_company_wide_integration(integ) and (
        acting_user_id is None or str(owner) != str(acting_user_id)
    )
    if not is_fallback:
        return integ, TEMPLATE_ORIGIN_OWN
    logger.info(
        "Per-user binding for %s resolved to the company-wide instance %s for "
        "acting user %s%s — the user has no personal %s integration connected",
        provider or getattr(integ, "provider", "?"),
        getattr(integ, "id", None),
        acting_user_id,
        f" ({context})" if context else "",
        provider or getattr(integ, "provider", "?"),
    )
    return integ, TEMPLATE_ORIGIN_COMPANY_FALLBACK


def pick_template_integration(
    candidate_integrations: list[Integration],
    acting_user_id: uuid.UUID | str | None,
    *,
    provider: str = "",
    context: str = "",
) -> tuple[Integration | None, str | None]:
    """Pick a per-user (template) binding without scope disambiguation.

    For callers with no request scope to match on (assignment runs, scheduled work items). Drops
    another user's personal integrations, prefers the acting user's own private
    row, and otherwise falls back to a company-wide one. Returns
    ``(integration, origin)`` — see :func:`classify_template_pick`.
    """
    candidates = drop_foreign_personal_integrations(
        candidate_integrations, acting_user_id, provider=provider
    )
    picked = _prefer_private_integration(candidates, acting_user_id)
    return classify_template_pick(
        picked, acting_user_id, provider=provider, context=context
    )


async def resolve_template_integration(
    db: AsyncSession,
    provider: str,
    candidate_integrations: list[Integration],
    scope_headers: dict[str, str] | None,
    *,
    acting_user_id: uuid.UUID | str | None,
    context: str = "",
) -> tuple[Integration | None, str | None]:
    """Resolve one per-user (template) binding, ownership-aware.

    Like :func:`pick_template_integration` but routed through
    :func:`resolve_integration_by_scope`, so a provider with a ``scope_config``
    is disambiguated from *scope_headers* / platform registration instead of
    auto-picked. Returns ``(None, None)`` when nothing is eligible — the caller
    then reports the provider as missing rather than borrowing someone else's
    account.
    """
    candidates = drop_foreign_personal_integrations(
        candidate_integrations, acting_user_id, provider=provider
    )
    if not candidates:
        return None, None

    integ = await resolve_integration_by_scope(
        db, provider, candidates, scope_headers, acting_user_id=acting_user_id
    )
    return classify_template_pick(
        integ, acting_user_id, provider=provider, context=context
    )


async def resolve_integration_by_scope(
    db: AsyncSession,
    provider: str,
    candidate_integrations: list[Integration],
    scope_headers: dict[str, str] | None,
    *,
    acting_user_id: uuid.UUID | str | None,
) -> Integration | None:
    """Resolve the best integration from candidates using integration scope_config.

    Reads scope_config from the candidate integrations themselves (not from the
    agent).  Filters candidates by match_key if a scope is defined and a value
    can be resolved from headers or platform registration.
    Otherwise, auto-picks the first available (preferring user-owned).

    Resolution is always ownership-aware: candidates that are
    **another user's personal** integration are dropped fail-closed before any
    pick happens, and the "prefer a private connection over a shared one" step
    prefers the acting user's OWN row. *acting_user_id* is deliberately
    REQUIRED (keyword-only, no default): a caller that forgot it used to get the
    identity-blind behaviour and could bind a colleague's personal account. A
    caller with genuinely no identity passes ``None`` explicitly and gets
    company-wide (and owner-less legacy) rows only.
    """
    if not candidate_integrations:
        return None

    candidate_integrations = drop_foreign_personal_integrations(
        candidate_integrations, acting_user_id, provider=provider
    )
    if not candidate_integrations:
        return None

    if len(candidate_integrations) == 1:
        return candidate_integrations[0]

    # Find scope_config from the candidates (all same-provider candidates share it)
    _scope_cfg = next(
        (c.scope_config for c in candidate_integrations if c.scope_config),
        None,
    )
    if _scope_cfg and _scope_cfg.get("match_key"):
        _match_key = _scope_cfg["match_key"]
        _header_name = _scope_cfg.get("resolve_from_header")
        _scope_value = (
            scope_headers.get(_header_name.lower())
            if scope_headers and _header_name
            else None
        )

        if _scope_value:
            logger.info(
                "Scope resolved from header for %s: %s=%s",
                provider,
                _header_name,
                _scope_value,
            )
        else:
            _platform_field = _scope_cfg.get("resolve_from_platform")
            if _platform_field:
                from app.models.platform_registration import PlatformRegistration
                _plat_result = await db.execute(
                    select(PlatformRegistration).where(
                        PlatformRegistration.platform_identifier == provider,
                        PlatformRegistration.status == "active",
                    )
                )
                _plat_regs = list(_plat_result.scalars().all())
                if len(_plat_regs) == 1:
                    _scope_value = getattr(_plat_regs[0], _platform_field, None)
                    if _scope_value:
                        logger.info(
                            "Scope resolved from platform registration for %s: %s=%s",
                            provider,
                            _platform_field,
                            _scope_value,
                        )
                elif len(_plat_regs) > 1:
                    logger.info(
                        "Multiple active %s registrations — cannot auto-resolve scope from platform",
                        provider,
                    )

        if _scope_value:
            # A platform reached on an internal URL keeps it in its
            # integration's ``base_url`` while it is known by its registered
            # URL: either names it.
            _reached = await platforms_reached_internally(
                db,
                [
                    c.id
                    for c in candidate_integrations
                    if c.auth_type == "platform_managed"
                ],
            )
            _matched: list[Integration] = []
            for _integ in candidate_integrations:
                try:
                    _extra = await _reveal_extra(_integ)
                    _integ_val = _extra.get(_match_key, "")
                    _platform = _reached.get(str(_integ.id))
                    if _integ_val.rstrip("/") == _scope_value.rstrip("/") or (
                        _platform is not None
                        and _platform.is_reached_at(_integ_val)
                        and _platform.is_reached_at(_scope_value)
                    ):
                        _matched.append(_integ)
                except Exception:
                    logger.warning(
                        "Failed to decrypt/parse extra_config for integration %s (%s)",
                        _integ.id,
                        provider,
                        exc_info=True,
                    )
            if len(_matched) == 1:
                return _matched[0]
            elif len(_matched) > 1:
                return _prefer_private_integration(_matched, acting_user_id)
            else:
                logger.info(
                    "Scope filter: no %s integration matches %s=%s",
                    provider,
                    _match_key,
                    _scope_value,
                )
                return None
        else:
            logger.info(
                "Scope required for %s but no scope value provided — skipping auto-bind",
                provider,
            )
            return None
    else:
        # No scope — auto-pick, preferring a private integration over a shared
        # (company-managed) one.
        return _prefer_private_integration(candidate_integrations, acting_user_id)


def _prefer_private_integration(
    candidates: list[Integration],
    acting_user_id: uuid.UUID | str | None,
) -> Integration | None:
    """Pick a private integration ahead of a shared one, ownership-aware.

    Company-managed integrations keep a non-null owner (always-set-owner
    invariant), so ``user_id is not None`` does not distinguish private
    from shared — the private test is ``not is_company_managed``. Binding a
    shared row ahead of the user's own connection would use the wrong
    credentials, hence the preference.

    "Private" additionally means *theirs*. Without that check the
    preference actively selects a colleague's personal row ahead of the real
    company-managed instance whenever one leaks into the candidate list. With
    no acting user no row is theirs, so nothing is preferred and the first
    candidate wins - the pick a user who owns none of them gets.
    """
    if not candidates:
        return None
    private = [
        i
        for i in candidates
        if i.user_id is not None
        and not i.is_company_managed
        and acting_user_id is not None
        and str(i.user_id) == str(acting_user_id)
    ]
    return private[0] if private else candidates[0]
