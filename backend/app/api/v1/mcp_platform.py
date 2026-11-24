# SPDX-License-Identifier: Apache-2.0

"""Platform MCP servers – expose OpenCTI / OpenAEV / OpenCRQ business tools via MCP.

Implements MCP Streamable HTTP transport (JSON-RPC 2.0 over POST) with one
endpoint per connected platform:

- ``POST /mcp/opencti`` – OpenCTI threat-intelligence knowledge tools
  (SDOs, SCOs, relationships, indicators, containers, labels, markings).
- ``POST /mcp/openaev`` – OpenAEV adversarial-exposure-validation tools
  (scenarios, simulations, payloads, injects, atomic tests, findings).
- ``POST /mcp/opencrq`` – OpenCRQ governance-risk-compliance tools
  (frameworks, controls, assessments, risk scenarios, assets, CVEs).

Availability model
------------------
A platform MCP server is live as soon as the corresponding platform is
**registered** with XTM One (``POST /api/v1/platform/register``) — the same
registration that powers the embedded chat.  Registration auto-creates a
company-managed ``platform_managed`` Integration; at tool-execution time a
short-lived Ed25519 JWT is minted for the calling user, so every OpenCTI /
OpenAEV / OpenCRQ API call is authenticated **as that user** (matched by
email on the platform side).

This is a full Community Edition feature: unlike the agent MCP endpoints
(``/mcp/user``, ``/mcp/global``) there is **no** chat/EE license gate —
tool calls go straight to the platform APIs without any LLM involvement.

Authentication
--------------
Bearer token: the user's personal API key (``fcp-...``) or an OAuth 2.1
access token.  Unauthenticated requests receive ``401`` with a
``WWW-Authenticate`` header pointing at the Protected Resource Metadata
(RFC 9728) for automatic OAuth discovery — same flow as ``/mcp/user``.

Tool surface
------------
Tools map 1:1 to the native platform integration tools, filtered
through an explicit fail-closed allow-list: only business / knowledge tools
are exposed.  Platform-orchestration tooling (users, groups, roles,
playbooks, dashboards, PIRs, connectors, settings, ...) is never exposed,
and any new integration tool stays hidden until explicitly allow-listed.

OpenCTI mutations run in **direct mode** (no draft staging): the MCP
transport is stateless, so the conversational draft workflow used by agents
does not apply here.

Admins can disable all platform servers with the ``platform_mcp_enabled``
toggle in Settings > Integrations > MCP Endpoint.

Only Streamable HTTP transport is supported – no SSE, no stdio.
"""

from __future__ import annotations

import logging
import uuid as _uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.mcp_auth import authenticate_user
from app.api.v1.mcp_transport import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    MCPRequest,
    ServerIdentity,
    add_unsupported_method_routes,
    dispatch_all,
    initialize_result,
    read_messages,
    require_valid_origin,
    respond,
    tool_error_result,
    tool_result,
)
from app.api.v1.mcp_transport import jsonrpc_error as _jsonrpc_error
from app.api.v1.mcp_transport import jsonrpc_response as _jsonrpc_response
from app.core.database import get_db
from app.core.utils import format_exception_message
from app.models.integration import Integration
from app.models.platform_registration import PlatformRegistration
from app.models.user import User
from app.services.api_key_access import mcp_tools_for_key, require_configuration_surface
from app.services.branding import get_cached_branding
from app.services.mcp_tool_metadata import tools_to_mcp
from app.tools.base import ScopedToolExecutor, ToolCall, ToolDefinition, offering_tools

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Platform catalog ─────────────────────────────────────────────────────────

#: Provider key → human display name for supported platform MCP servers.
PLATFORM_MCP_PROVIDERS: dict[str, str] = {
    "opencti": "OpenCTI",
    "openaev": "OpenAEV",
    "opencrq": "OpenCRQ",
}

#: Provider key → short user-facing description of the exposed tool surface.
#: Single source of truth for every UI surface (Profile, Settings) via
#: ``GET /api/v1/platform/mcp-servers``.
PLATFORM_MCP_DESCRIPTIONS: dict[str, str] = {
    "opencti": (
        "Threat intelligence knowledge tools (search, entities, observables, "
        "relationships, indicators, containers)"
    ),
    "openaev": (
        "Adversarial exposure validation tools (scenarios, simulations, "
        "payloads, injects, atomic tests, findings)"
    ),
    "opencrq": (
        "Governance, risk & compliance tools (frameworks, control capabilities, "
        "compliance assessments, risk scenarios, assets, CVEs)"
    ),
}

# ── Tool allow-lists (fail closed) ───────────────────────────────────────────
#
# Only tools listed here are exposed over MCP.  Everything else — platform
# orchestration (users, groups, roles, playbooks, dashboards, notifications,
# customization, audit, PIRs, ingestion, data sharing, connectors, settings),
# draft management (the MCP transport is stateless and mutations run direct),
# file transfer (depends on the chat FileContext), and dev/debug tools — is
# excluded by construction.  A tool added to the integration providers later
# stays hidden from MCP until explicitly added here.

#: OpenCTI business / knowledge tools (STIX SDOs, SCOs, SROs, containers).
OPENCTI_MCP_TOOLS: frozenset[str] = frozenset(
    {
        # Read / search
        "search_opencti",
        "get_opencti_platform_version",
        "get_opencti_entity",
        "get_opencti_relationship",
        "list_opencti_entities",
        "list_opencti_observables",
        "list_opencti_relationships",
        "get_opencti_container_full",
        "get_opencti_entity_stats",
        "get_opencti_top_related",
        "get_opencti_entity_history",
        "validate_nlq_filter",
        # Create
        "create_opencti_entity",
        "create_opencti_relationship",
        "create_opencti_observable",
        "create_opencti_indicator",
        "create_opencti_container",
        "create_opencti_note",
        # Update
        "update_opencti_field",
        "update_opencti_relationship_field",
        # Delete
        "delete_opencti_entity",
        "delete_opencti_relationship",
        # Labels & markings
        "add_opencti_label",
        "remove_opencti_label",
        "add_opencti_marking",
        "remove_opencti_marking",
        "list_opencti_markings",
        # Containers
        "add_opencti_to_container",
        "remove_opencti_from_container",
        # Advanced
        "promote_opencti_observable",
    }
)

#: OpenAEV business / knowledge tools (scenarios, simulations, payloads,
#: injects, atomic tests, findings, assets, content).  People / group
#: management (users, teams, players, organizations), taxonomy writes and
#: platform administration stay excluded.
OPENAEV_MCP_TOOLS: frozenset[str] = frozenset(
    {
        # Search / read (core)
        "search_openaev_assets",
        "get_openaev_asset",
        "search_openaev_asset_groups",
        "get_openaev_asset_group",
        "search_openaev_teams",
        "get_openaev_team",
        "search_openaev_players",
        "search_openaev_attack_patterns",
        "get_openaev_attack_pattern",
        "search_openaev_scenarios",
        "get_openaev_scenario",
        "search_openaev_exercises",
        "get_openaev_exercise",
        "search_openaev_payloads",
        "get_openaev_payload",
        "search_openaev_injector_contracts",
        "get_openaev_injector_contract",
        "search_openaev_findings",
        "get_openaev_vulnerability",
        "full_text_search_openaev",
        "list_openaev_kill_chain_phases",
        "list_openaev_tags",
        "list_openaev_domains",
        # Create / mutate (core)
        "create_openaev_scenario",
        "update_openaev_scenario",
        "delete_openaev_scenario",
        "create_openaev_payload",
        "add_openaev_inject_to_scenario",
        "create_openaev_atomic_testing",
        # Business domain managers
        "manage_openaev_simulation",
        "manage_openaev_scenario",
        "manage_openaev_inject",
        "manage_openaev_expectation",
        "manage_openaev_atomic_testing",
        "manage_openaev_payload",
        "manage_openaev_threat_arsenal",
        "manage_openaev_finding",
        "manage_openaev_vulnerability",
        "manage_openaev_asset",
        "manage_openaev_asset_group",
        "manage_openaev_document",
        "manage_openaev_channel",
        "manage_openaev_article",
        "manage_openaev_challenge",
        "manage_openaev_lessons",
        "manage_openaev_reporting",
        # Phishing components (landing pages + email templates). Business content
        # management (direct CRUD, no orchestration); OpenAEV enforces the phishing
        # capabilities per calling user, so a caller without them gets a clean 403.
        "manage_openaev_phishing",
        # Analytics + preview read surfaces (business knowledge, no
        # orchestration): ad-hoc widget queries, saved dashboards, the
        # feature-flagged attack-path and chaining reads.
        "query_openaev_analytics",
        "manage_openaev_dashboard",
        "query_openaev_attack_path",
        "query_openaev_chaining",
        "manage_openaev_variable",
    }
)

#: OpenCRQ business / knowledge tools (frameworks, control capabilities,
#: compliance assessments, risk scenarios, CRQ, assets, findings, MITRE, CVEs,
#: job tracking, threats, treatment plans, budget reviews, proven control
#: posture, risk dashboard). Tenant/user administration (users, groups,
#: roles, invitations, tenant and organization lifecycle, service accounts)
#: and platform / engine configuration (CRQ and threat relevance settings,
#: job cancel/retry, connectors) stay excluded.
OPENCRQ_MCP_TOOLS: frozenset[str] = frozenset(
    {
        # Tenancy / catalog (read)
        "list_opencrq_tenants",
        "get_opencrq_platform_version",
        "list_opencrq_organizations",
        "list_opencrq_frameworks",
        "get_opencrq_framework",
        "list_opencrq_framework_requirements",
        "list_opencrq_drata_controls",
        # Controls
        "list_opencrq_control_capabilities",
        "get_opencrq_control_capability",
        "update_opencrq_control_capability_assessment",
        "manage_opencrq_scope_control_posture",
        "manage_opencrq_capability_techniques",
        "get_opencrq_control_capability_history",
        # Assessments
        "list_opencrq_compliance_assessments",
        "get_opencrq_compliance_assessment",
        "create_opencrq_compliance_assessment",
        "manage_opencrq_compliance_assessment",
        "update_opencrq_requirement_answer",
        # Risk
        "list_opencrq_risk_scenarios",
        "get_opencrq_risk_scenario",
        "create_opencrq_risk_scenario",
        "update_opencrq_risk_scenario",
        "delete_opencrq_risk_scenarios",
        "update_opencrq_risk_tolerance",
        "get_opencrq_scenario_exposure",
        "list_opencrq_attack_outcomes",
        "get_opencrq_attack_outcome",
        # CRQ
        "get_opencrq_financial_summary",
        "run_opencrq_crq_simulation",
        "get_opencrq_crq_run",
        "list_opencrq_crq_run_history",
        "get_opencrq_crq_what_if",
        "get_opencrq_tef_input",
        "update_opencrq_tef_input",
        "list_opencrq_loss_drivers",
        "get_opencrq_loss_driver",
        "update_opencrq_loss_estimates",
        "get_opencrq_financial_profile",
        "update_opencrq_financial_profile",
        # Assets & vulnerabilities
        "list_opencrq_primary_assets",
        "list_opencrq_business_value_categories",
        "create_opencrq_primary_asset",
        "update_opencrq_primary_asset",
        "delete_opencrq_primary_assets",
        "list_opencrq_supporting_assets",
        "get_opencrq_supporting_asset",
        "create_opencrq_supporting_asset",
        "update_opencrq_supporting_asset",
        "delete_opencrq_supporting_assets",
        "reset_opencrq_supporting_asset_to_connectors",
        "list_opencrq_asset_dependencies",
        "create_opencrq_asset_dependency",
        "update_opencrq_asset_dependency_roles",
        "delete_opencrq_asset_dependencies",
        "create_opencrq_asset_evidence",
        "update_opencrq_evidence_state",
        "delete_opencrq_asset_evidence",
        "reset_opencrq_evidence_state",
        "get_opencrq_evidence",
        "list_opencrq_cve_evidences",
        "list_opencrq_unique_findings",
        "search_opencrq_cves",
        "list_opencrq_cves",
        "get_opencrq_cve",
        # MITRE ATT&CK reference data
        "list_opencrq_mitre_tactics",
        "search_opencrq_mitre_techniques",
        "get_opencrq_mitre_technique",
        "list_opencrq_mitre_mitigations",
        # Job tracking (read-only - needed for CRQ simulation polling)
        "list_opencrq_jobs",
        "get_opencrq_job",
        # Findings, threats, risk treatment, proven posture, dashboard (read)
        "get_opencrq_findings_summary",
        "get_opencrq_top_vulnerabilities",
        "list_opencrq_findings",
        "get_opencrq_cve_finding",
        "get_opencrq_asset_posture",
        "get_opencrq_technique_test_posture",
        "list_opencrq_threats",
        "get_opencrq_threat",
        "list_opencrq_scenario_threats",
        "list_opencrq_cve_threats",
        "list_opencrq_treatment_plans",
        "get_opencrq_treatment_plan",
        "list_opencrq_budget_reviews",
        "get_opencrq_budget_review",
        "get_opencrq_control_validation",
        "get_opencrq_risk_dashboard",
    }
)

#: Provider key → allow-listed tool names.
MCP_TOOL_ALLOW_LISTS: dict[str, frozenset[str]] = {
    "opencti": OPENCTI_MCP_TOOLS,
    "openaev": OPENAEV_MCP_TOOLS,
    "opencrq": OPENCRQ_MCP_TOOLS,
}

# ── Read lists: what a read-only API key may call ───────────────────
#
# Reviewed by hand, tool by tool, from what each tool does: a read changes
# nothing on the platform. Fail closed - a tool absent here is a write for a
# read-only key, including every action-dispatched ``manage_*`` tool, whose
# actions mix reads and writes. ``tests/test_api_key_access.py`` holds each
# list inside its allow-list and checks it against the integration catalog's
# read / write classifier.

OPENCTI_MCP_READ_TOOLS: frozenset[str] = frozenset(
    {
        "search_opencti",
        "get_opencti_platform_version",
        "get_opencti_entity",
        "get_opencti_relationship",
        "list_opencti_entities",
        "list_opencti_observables",
        "list_opencti_relationships",
        "get_opencti_container_full",
        "get_opencti_entity_stats",
        "get_opencti_top_related",
        "get_opencti_entity_history",
        "validate_nlq_filter",
    }
)

OPENAEV_MCP_READ_TOOLS: frozenset[str] = frozenset(
    {
        "search_openaev_assets",
        "get_openaev_asset",
        "search_openaev_asset_groups",
        "get_openaev_asset_group",
        "search_openaev_teams",
        "get_openaev_team",
        "search_openaev_players",
        "search_openaev_attack_patterns",
        "get_openaev_attack_pattern",
        "search_openaev_scenarios",
        "get_openaev_scenario",
        "search_openaev_exercises",
        "get_openaev_exercise",
        "search_openaev_payloads",
        "get_openaev_payload",
        "search_openaev_injector_contracts",
        "get_openaev_injector_contract",
        "search_openaev_findings",
        "get_openaev_vulnerability",
        "full_text_search_openaev",
        "list_openaev_kill_chain_phases",
        "list_openaev_tags",
        "list_openaev_domains",
        "query_openaev_analytics",
        "query_openaev_attack_path",
        "query_openaev_chaining",
    }
)

OPENCRQ_MCP_READ_TOOLS: frozenset[str] = frozenset(
    {
        "list_opencrq_tenants",
        "get_opencrq_platform_version",
        "list_opencrq_organizations",
        "list_opencrq_frameworks",
        "get_opencrq_framework",
        "list_opencrq_framework_requirements",
        "list_opencrq_drata_controls",
        "list_opencrq_control_capabilities",
        "get_opencrq_control_capability",
        "get_opencrq_control_capability_history",
        "list_opencrq_compliance_assessments",
        "get_opencrq_compliance_assessment",
        "list_opencrq_risk_scenarios",
        "get_opencrq_risk_scenario",
        "get_opencrq_scenario_exposure",
        "list_opencrq_attack_outcomes",
        "get_opencrq_attack_outcome",
        "get_opencrq_financial_summary",
        "get_opencrq_crq_run",
        "list_opencrq_crq_run_history",
        "get_opencrq_crq_what_if",
        "get_opencrq_tef_input",
        "list_opencrq_loss_drivers",
        "get_opencrq_loss_driver",
        "get_opencrq_financial_profile",
        "list_opencrq_primary_assets",
        "list_opencrq_business_value_categories",
        "list_opencrq_supporting_assets",
        "get_opencrq_supporting_asset",
        "list_opencrq_asset_dependencies",
        "get_opencrq_evidence",
        "list_opencrq_cve_evidences",
        "list_opencrq_unique_findings",
        "search_opencrq_cves",
        "list_opencrq_cves",
        "get_opencrq_cve",
        "list_opencrq_mitre_tactics",
        "search_opencrq_mitre_techniques",
        "get_opencrq_mitre_technique",
        "list_opencrq_mitre_mitigations",
        "list_opencrq_jobs",
        "get_opencrq_job",
        "get_opencrq_findings_summary",
        "get_opencrq_top_vulnerabilities",
        "list_opencrq_findings",
        "get_opencrq_cve_finding",
        "get_opencrq_asset_posture",
        "get_opencrq_technique_test_posture",
        "list_opencrq_threats",
        "get_opencrq_threat",
        "list_opencrq_scenario_threats",
        "list_opencrq_cve_threats",
        "list_opencrq_treatment_plans",
        "get_opencrq_treatment_plan",
        "list_opencrq_budget_reviews",
        "get_opencrq_budget_review",
        "get_opencrq_control_validation",
        "get_opencrq_risk_dashboard",
    }
)

#: Provider key → the tools a read-only API key may call.
MCP_TOOL_READ_LISTS: dict[str, frozenset[str]] = {
    "opencti": OPENCTI_MCP_READ_TOOLS,
    "openaev": OPENAEV_MCP_READ_TOOLS,
    "opencrq": OPENCRQ_MCP_READ_TOOLS,
}


# ── Gates ────────────────────────────────────────────────────────────────────


async def _platform_mcp_enabled(db: AsyncSession) -> bool:
    """Whether the platform MCP servers are globally enabled (admin toggle)."""
    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )

    ps = await _load_ps(db)
    return bool(getattr(ps, "platform_mcp_enabled", True))


async def _get_active_registration(
    db: AsyncSession, provider: str
) -> PlatformRegistration | None:
    """Return the active registration for *provider* (with linked integration).

    ``platform_identifier`` also accepts the legacy identifier a platform
    registered under before its product was renamed — ``openbas`` for
    OpenAEV, ``opengrc`` for OpenCRQ.
    """
    identifiers = [provider]
    if provider == "openaev":
        identifiers.append("openbas")
    if provider == "opencrq":
        identifiers.append("opengrc")
    result = await db.execute(
        select(PlatformRegistration).where(
            PlatformRegistration.platform_identifier.in_(identifiers),
            PlatformRegistration.status == "active",
            PlatformRegistration.integration_id.isnot(None),
        )
    )
    return result.scalars().first()


# ── Tool building ────────────────────────────────────────────────────────────


async def _build_platform_tools(
    db: AsyncSession,
    provider: str,
    registration: PlatformRegistration,
    user: User,
) -> tuple[list[ToolDefinition], ScopedToolExecutor]:
    """Build the allow-listed MCP tool surface for *provider*.

    Delegates to the native integration tool builder (per-user platform JWT
    minted by ``_get_token``), then filters the definitions through the
    fail-closed allow-list.  The returned executor is scoped to the allowed
    definitions so a non-listed tool can never execute — even if a client
    guesses its registered name. A read-only API key keeps the read list.
    """
    from app.services.integration_tools import BindingBasis, ToolBindings, build_integration_tools

    definitions, executor = await build_integration_tools(
        db,
        ToolBindings(
            {provider: [str(registration.integration_id)]},
            basis=BindingBasis.CALLER_CHECKED,
            reason="the platform integration acts with a JWT minted for the caller",
        ),
        context={
            "source": "mcp_platform",
            "user_id": str(user.id),
            # OpenCTI: stateless transport — mutations bypass the draft gate.
            "direct_mutations": True,
        },
    )
    allow = MCP_TOOL_ALLOW_LISTS.get(provider, frozenset())
    allowed = mcp_tools_for_key(
        user,
        [d for d in definitions if d.name in allow],
        MCP_TOOL_READ_LISTS.get(provider, frozenset()),
    )
    return allowed, ScopedToolExecutor(executor, allowed)


async def _track_platform_tool_call(registration: PlatformRegistration) -> None:
    """Bump usage counters on the platform-managed integration.

    Uses its own short-lived session so the UPDATE row lock never spans the
    (potentially long) outbound platform API call of the next tool.
    Best-effort — tracking failures never fail the MCP request.
    """
    from app.core.database import async_session

    try:
        async with async_session() as tracking_db:
            await tracking_db.execute(
                update(Integration)
                .where(Integration.id == registration.integration_id)
                .values(
                    call_count=Integration.call_count + 1,
                    last_called_at=datetime.now(UTC),
                )
            )
            await tracking_db.commit()
    except Exception as exc:
        logger.warning(
            "Platform MCP usage tracking failed for integration %s: %s",
            registration.integration_id,
            format_exception_message(exc),
        )


# ── MCP method handlers ──────────────────────────────────────────────────────


def _handle_initialize(
    req_id: int | str | None, provider: str, params: dict | None = None
) -> dict[str, Any]:
    """Handle MCP initialize – identifies the server as the platform bridge."""
    identity = _identity(provider)
    return initialize_result(req_id, params, name=identity.name, description=identity.description)


def _identity(provider: str) -> ServerIdentity:
    """A platform server's identity: the platform, reached through the brand."""
    display = PLATFORM_MCP_PROVIDERS.get(provider, provider)
    return ServerIdentity(
        name=f"{display} (via {get_cached_branding().name})",
        description=PLATFORM_MCP_DESCRIPTIONS.get(provider, display),
    )


async def _handle_tools_call(
    req_id: int | str | None,
    params: dict | None,
    definitions: list[ToolDefinition],
    executor: ScopedToolExecutor,
    registration: PlatformRegistration,
) -> dict[str, Any]:
    """Handle MCP tools/call – executes the underlying integration tool.

    A call to no tool is a protocol error; invalid arguments and a failed call
    are tool execution errors, in the result the model reads.
    """
    if not params:
        return _jsonrpc_error(req_id, INVALID_PARAMS, "Missing params")

    tool_name = params.get("name")
    # Some clients send an absent optional field as null.
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not tool_name:
        return _jsonrpc_error(req_id, INVALID_PARAMS, "Missing tool name")

    if not any(d.name == tool_name for d in definitions):
        return _jsonrpc_error(req_id, INVALID_PARAMS, f"Unknown tool: {tool_name}")
    if not isinstance(arguments, dict):
        return tool_error_result(req_id, "Tool arguments must be an object.")

    call = ToolCall(
        id=str(req_id) if req_id is not None else str(_uuid.uuid4()),
        name=tool_name,
        arguments=arguments,
    )
    try:
        # The tool's result names another tool only when this server serves it.
        with offering_tools(definitions):
            result = await executor.execute_tool(call)
    except Exception as exc:
        logger.error(
            "Platform MCP tool execution error for %s: %s",
            tool_name,
            format_exception_message(exc),
            exc_info=True,
        )
        return tool_error_result(req_id, "Tool execution failed. Please try again.")

    await _track_platform_tool_call(registration)

    # Tool-level failures are surfaced in-band (MCP ``isError``) so the
    # client sees the actionable message instead of a JSON-RPC fault.
    return tool_result(req_id, result.content or "", is_error=bool(result.is_error))


async def _dispatch_platform_mcp(
    body: MCPRequest,
    provider: str,
    db: AsyncSession,
    registration: PlatformRegistration,
    user: User,
) -> dict[str, Any] | None:
    """Route a platform MCP JSON-RPC request to the appropriate handler."""
    method = body.method

    if method == "initialize":
        return _handle_initialize(body.id, provider, body.params)

    if method == "ping":
        return _jsonrpc_response(body.id, {})

    if method in ("tools/list", "tools/call"):
        definitions, executor = await _build_platform_tools(
            db, provider, registration, user
        )
        if method == "tools/list":
            return _jsonrpc_response(
                body.id,
                {"tools": tools_to_mcp(definitions)},
            )
        return await _handle_tools_call(
            body.id, body.params, definitions, executor, registration
        )

    return _jsonrpc_error(body.id, METHOD_NOT_FOUND, f"Method not found: {method}")


# ── Shared endpoint implementation ───────────────────────────────────────────


async def _platform_mcp_endpoint(
    provider: str,
    request: Request,
    db: AsyncSession,
):
    """Common implementation behind ``/mcp/opencti`` and ``/mcp/openaev``."""
    resource_path = f"mcp/{provider}"

    # 0) A web page of another origin (DNS rebinding) never gets this far.
    require_valid_origin(request)

    # 1) Authenticate (fcp- API key or OAuth 2.1 token — RFC 9728 on 401).
    user = await authenticate_user(request, db, resource_path)
    # A key restricted to agents reaches agents only, and this server has none.
    require_configuration_surface(user)

    # 1b) AI disabled — refuse entirely (FR-029). The tools behind this are
    # product APIs rather than model calls, but an MCP endpoint exists to be
    # consumed by a model: on a deployment chosen because model access is
    # prohibited, leaving it live keeps open the path the mode closes. Refused
    # after authentication and still routed, so it states why rather than
    # disappearing or 401-ing a client that worked yesterday.
    from app.services.agentic_gate import refuse_ai_disabled
    from app.services.ai_state import is_ai_enabled

    if not await is_ai_enabled(db):
        refuse_ai_disabled()

    # 2) Admin toggle — platform MCP servers can be disabled in Settings.
    if not await _platform_mcp_enabled(db):
        raise HTTPException(
            status_code=403,
            detail=(
                "Platform MCP servers are disabled on this deployment. "
                "An administrator can enable them in Settings > Integrations > MCP Endpoint."
            ),
        )

    # 3) Platform must be connected (registered and active).
    registration = await _get_active_registration(db, provider)
    if registration is None:
        display = PLATFORM_MCP_PROVIDERS.get(provider, provider)
        raise HTTPException(
            status_code=404,
            detail=(
                f"No {display} platform is connected. Register the platform "
                "with XTM One to activate this MCP server."
            ),
        )

    # 4) Read the JSON-RPC message(s).
    incoming = await read_messages(request)
    if isinstance(incoming, JSONResponse):
        # The API key's usage was recorded during authentication.
        await db.commit()
        return incoming

    answers = await dispatch_all(
        incoming,
        lambda body: _dispatch_platform_mcp(body, provider, db, registration, user),
        identity=_identity(provider),
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return respond(incoming, answers)


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post("/opencti")
async def mcp_opencti_endpoint(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """OpenCTI MCP server – MCP Streamable HTTP endpoint.

    Available as soon as an OpenCTI platform is registered with XTM One.
    Authenticated with the user's personal API key (``fcp-...``) or an
    OAuth 2.1 access token.  Exposes the business / knowledge tool surface
    of the native OpenCTI integration (STIX entities, observables,
    relationships, indicators, containers, labels, markings).
    """
    return await _platform_mcp_endpoint("opencti", request, db)


@router.post("/openaev")
async def mcp_openaev_endpoint(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """OpenAEV MCP server – MCP Streamable HTTP endpoint.

    Available as soon as an OpenAEV platform is registered with XTM One.
    Authenticated with the user's personal API key (``fcp-...``) or an
    OAuth 2.1 access token.  Exposes the business / knowledge tool surface
    of the native OpenAEV integration (scenarios, simulations, payloads,
    injects, atomic tests, findings, assets).
    """
    return await _platform_mcp_endpoint("openaev", request, db)


@router.post("/opencrq")
async def mcp_opencrq_endpoint(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """OpenCRQ MCP server – MCP Streamable HTTP endpoint.

    Available as soon as an OpenCRQ platform is registered with XTM One.
    Authenticated with the user's personal API key (``fcp-...``) or an
    OAuth 2.1 access token.  Exposes the business / knowledge tool surface
    of the native OpenCRQ integration (frameworks, controls, assessments,
    risk scenarios, assets, CVEs).
    """
    return await _platform_mcp_endpoint("opencrq", request, db)


add_unsupported_method_routes(router, "/opencti", "/openaev", "/opencrq")
