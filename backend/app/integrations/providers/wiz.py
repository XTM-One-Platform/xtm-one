# SPDX-License-Identifier: Apache-2.0

"""Wiz integration - cloud security / CNAPP (Cloud Native Application Protection).

Full-fledged Wiz integration exposing the vendor's cloud risk findings to an
agentic cloud-security team. Wiz continuously scans connected cloud
environments (AWS, Azure, GCP, OCI, Alibaba Cloud, Kubernetes, ...) and
correlates misconfigurations, vulnerabilities, exposed secrets, identities and
network exposure on the Wiz Security Graph into prioritised Issues.

Tool surface (this module):
  - Issues: list with rich filters (``list_wiz_issues``), read one with its
    remediation guidance, affected resource, projects and notes
    (``get_wiz_issue``), update status / due date / resolution
    (``update_wiz_issue``), and append an investigation note
    (``add_wiz_issue_note``).
  - Posture: open-issue counts by severity plus the CISA KEV vulnerability
    count in one call (``get_wiz_posture_summary``).
  - Vulnerabilities: query vulnerability findings with CVE / severity /
    exploitability / fixability filters (``list_wiz_vulnerability_findings``).
  - Inventory: search the Wiz Security Graph for cloud resources by entity
    type, cloud platform, subscription and name
    (``search_wiz_cloud_resources``), and list connected cloud accounts
    (``list_wiz_cloud_accounts``).
  - Governance: list Wiz projects (``list_wiz_projects``) and security
    frameworks with their categories (``list_wiz_security_frameworks``).

The Wiz API is **GraphQL-only**: every call is a POST to the tenant-specific
endpoint ``https://api.REGION.app.wiz.io/graphql``. All queries in this
module use explicit field selections so results stay compact and readable for
the agent.

Authentication
--------------
Wiz authenticates with an **OAuth2 client-credentials** grant. The customer
creates a service account (type "Custom Integration (GraphQL API)") in the Wiz
portal under Settings > Access Management > Service Accounts, which yields a
client ID + client secret. The compound
``api_endpoint_url||client_id||client_secret`` token is assembled by
``integration_tools._get_token`` (client secret in ``api_key_enc``; API
endpoint URL and client ID in ``extra_config``). The client secret is the
FINAL, greedy segment so a secret containing ``||`` still round-trips.

The credentials are exchanged at the Wiz identity provider for a short-lived
bearer access token. Wiz tenants sit on one of two IdPs with different token
endpoints and audiences (Amazon Cognito: ``https://auth.app.wiz.io/oauth/token``
with audience ``wiz-api``; legacy Auth0: ``https://auth.wiz.io/oauth/token``
with audience ``beyond-api``) - the exchange tries Cognito first and falls
back to Auth0, and the minted token is cached in-process (keyed by a hash of
the credentials) so a multi-tool agentic run reuses one token instead of
re-minting on every call (mirrors the Picus / Cybersixgill app-token cache).

The stored ``api_endpoint_url`` is the API host (``https://api.us1.app.wiz.io``),
NOT the portal UI host (``https://app.wiz.io``), so it is intentionally
excluded from ``_USER_FACING_BASE_URL_PROVIDERS``.

Rate limits: Wiz enforces per-service-account rate limits (guideline: stay
under ~3 requests/second) and returns HTTP 429 when exceeded - requests ride
the shared ``safe_request`` retry with decorrelated jitter.

Exports ``resolve_api_endpoint()`` and ``validate_connection()`` shared with
the connectivity test.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import (
    app_token_cache_key,
    format_entity_list,
    format_pagination_hint,
    get_cached_app_token,
    parse_compound_token,
    safe_request,
    set_cached_app_token,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "Wiz"
_SOURCE = "integration:wiz"
_DEFAULT_TIMEOUT = 60

# Wiz tenants authenticate against one of two identity providers. Cognito is
# the current default for commercial tenants; Auth0 is the legacy IdP. Each
# has its own token endpoint AND audience value, so the exchange tries them
# in order until one succeeds.
_TOKEN_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("https://auth.app.wiz.io/oauth/token", "wiz-api"),
    ("https://auth.wiz.io/oauth/token", "beyond-api"),
)

_AUTH_ERROR = (
    "Error: Failed to authenticate with Wiz. Check that the service account "
    "client ID and client secret are correct and active (create them in the "
    "Wiz portal under Settings > Access Management > Service Accounts, type "
    "'Custom Integration (GraphQL API)'), and that the service account's API "
    "scopes cover this operation (read:issues, read:vulnerabilities, "
    "read:resources, read:cloud_accounts, read:projects, "
    "read:security_frameworks, update:issues)."
)

_SEVERITIES: frozenset[str] = frozenset(
    {"CRITICAL", "HIGH", "MEDIUM", "LOW", "INFORMATIONAL"}
)
_ISSUE_STATUSES: frozenset[str] = frozenset(
    {"OPEN", "IN_PROGRESS", "RESOLVED", "REJECTED"}
)
_ISSUE_TYPES: frozenset[str] = frozenset(
    {"TOXIC_COMBINATION", "THREAT_DETECTION", "CLOUD_CONFIGURATION"}
)
# Manual resolution reasons: standard Issues take the first block
# (reject/resolve workflow); Threat Detection Issues take the *_THREAT block.
_RESOLUTION_REASONS: frozenset[str] = frozenset(
    {
        "OBJECT_DELETED",
        "ISSUE_FIXED",
        "FALSE_POSITIVE",
        "EXCEPTION",
        "WONT_FIX",
        "MALICIOUS_THREAT",
        "NOT_MALICIOUS_THREAT",
        "SECURITY_TEST_THREAT",
        "PLANNED_ACTION_THREAT",
        "INCONCLUSIVE_THREAT",
    }
)
_VULN_STATUSES: frozenset[str] = frozenset({"OPEN", "RESOLVED"})

_SEVERITY_ICONS: dict[str, str] = {
    "CRITICAL": "\U0001f534",
    "HIGH": "\U0001f7e0",
    "MEDIUM": "\U0001f7e1",
    "LOW": "\U0001f535",
    "INFORMATIONAL": "\u26aa",
    "NONE": "\u26aa",
}


# ---------------------------------------------------------------------------
# GraphQL documents (explicit field selections keep results compact)
# ---------------------------------------------------------------------------

_ISSUES_QUERY = """
query CopilotIssues($first: Int, $after: String, $filterBy: IssueFilters) {
  issues: issuesV2(first: $first, after: $after, filterBy: $filterBy) {
    nodes {
      id
      type
      severity
      status
      createdAt
      dueAt
      resolvedAt
      statusChangedAt
      sourceRule { id name }
      entitySnapshot {
        id
        name
        type
        nativeType
        cloudPlatform
        subscriptionExternalId
        region
      }
      projects { id name }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_ISSUE_DETAIL_QUERY = """
query CopilotIssueDetail($id: ID!) {
  issue(id: $id) {
    id
    type
    severity
    status
    createdAt
    updatedAt
    dueAt
    resolvedAt
    statusChangedAt
    resolutionReason
    sourceRule {
      id
      name
      __typename
      ... on Control {
        controlDescription: description
        resolutionRecommendation
        securitySubCategories {
          title
          category { name framework { name } }
        }
      }
      ... on CloudConfigurationRule {
        cloudConfigurationRuleDescription: description
        remediationInstructions
      }
      ... on CloudEventRule {
        cloudEventRuleDescription: description
      }
    }
    entitySnapshot {
      id
      name
      type
      nativeType
      externalId
      cloudPlatform
      cloudProviderURL
      subscriptionName
      subscriptionExternalId
      region
      status
      tags
    }
    projects { id name slug }
    notes {
      text
      createdAt
      user { name email }
      serviceAccount { name }
    }
    serviceTickets { externalId name url }
  }
}
"""

_POSTURE_QUERY = """
query CopilotPosture {
  critical: issuesV2(
    first: 1
    filterBy: { status: [OPEN], severity: [CRITICAL] }
  ) { totalCount }
  high: issuesV2(
    first: 1
    filterBy: { status: [OPEN], severity: [HIGH] }
  ) { totalCount }
  medium: issuesV2(
    first: 1
    filterBy: { status: [OPEN], severity: [MEDIUM] }
  ) { totalCount }
  low: issuesV2(
    first: 1
    filterBy: { status: [OPEN], severity: [LOW] }
  ) { totalCount }
  inProgress: issuesV2(
    first: 1
    filterBy: { status: [IN_PROGRESS] }
  ) { totalCount }
  toxic: issuesV2(
    first: 1
    filterBy: { status: [OPEN], type: [TOXIC_COMBINATION] }
  ) { totalCount }
  threats: issuesV2(
    first: 1
    filterBy: { status: [OPEN], type: [THREAT_DETECTION] }
  ) { totalCount }
  kev: vulnerabilityFindings(
    first: 1
    filterBy: { hasCisaKevExploit: true }
  ) { totalCount }
}
"""

_VULN_FINDINGS_QUERY = """
query CopilotVulnerabilityFindings(
  $first: Int
  $after: String
  $filterBy: VulnerabilityFindingFilters
) {
  vulnerabilityFindings(first: $first, after: $after, filterBy: $filterBy) {
    nodes {
      id
      name
      CVEDescription
      CVSSSeverity
      vendorSeverity
      score
      exploitabilityScore
      hasExploit
      hasCisaKevExploit
      status
      detectionMethod
      detailedName
      version
      fixedVersion
      firstDetectedAt
      lastDetectedAt
      vulnerableAsset {
        ... on VulnerableAssetBase {
          id
          type
          name
          cloudPlatform
          subscriptionExternalId
          region
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_GRAPH_SEARCH_QUERY = """
query CopilotGraphSearch(
  $query: GraphEntityQueryInput
  $projectId: String!
  $first: Int
  $after: String
) {
  graphSearch(query: $query, projectId: $projectId, first: $first, after: $after) {
    totalCount
    maxCountReached
    pageInfo { hasNextPage endCursor }
    nodes { entities { id name type properties } }
  }
}
"""

_CLOUD_ACCOUNTS_QUERY = """
query CopilotCloudAccounts(
  $first: Int
  $after: String
  $filterBy: CloudAccountFilters
) {
  cloudAccounts(first: $first, after: $after, filterBy: $filterBy) {
    nodes {
      id
      name
      externalId
      cloudProvider
      status
      linkedProjects { id name }
      lastScannedAt
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_PROJECTS_QUERY = """
query CopilotProjects($first: Int, $after: String, $filterBy: ProjectFilters) {
  projects(first: $first, after: $after, filterBy: $filterBy) {
    nodes {
      id
      name
      slug
      isFolder
      archived
      description
      businessUnit
      riskProfile { businessImpact }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_FRAMEWORKS_QUERY = """
query CopilotSecurityFrameworks(
  $first: Int
  $after: String
  $filterBy: SecurityFrameworkFilters
) {
  securityFrameworks(first: $first, after: $after, filterBy: $filterBy) {
    nodes {
      id
      name
      description
      builtin
      enabled
      categories { id name subCategories { id title } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_UPDATE_ISSUE_MUTATION = """
mutation CopilotUpdateIssue($issueId: ID!, $patch: UpdateIssuePatch!) {
  updateIssue(input: { id: $issueId, patch: $patch }) {
    issue { id status dueAt resolutionReason updatedAt }
  }
}
"""

_CREATE_ISSUE_NOTE_MUTATION = """
mutation CopilotCreateIssueNote($input: CreateIssueNoteInput!) {
  createIssueNote(input: $input) {
    issueNote { id createdAt }
  }
}
"""


# ---------------------------------------------------------------------------
# API endpoint + auth helpers (shared with the connectivity test)
# ---------------------------------------------------------------------------


def resolve_api_endpoint(api_endpoint_url: str) -> str:
    """Normalise a tenant-specific Wiz API endpoint URL.

    Accepts the value shown in the Wiz portal under Profile > User Settings >
    API Endpoint URL (``https://api.REGION.app.wiz.io``). A trailing
    ``/graphql`` path or slash is stripped (the GraphQL path is appended at
    request time), a raw ``http://`` URL is upgraded to ``https://`` (requests
    carry the bearer token and must never travel in cleartext), and a bare
    host gains the scheme. There is NO default - the endpoint is
    tenant-specific, so an empty value stays empty and surfaces a clear
    call-time configuration error.
    """
    value = (api_endpoint_url or "").strip()
    if not value:
        return ""
    lowered = value.lower()
    if lowered.startswith("http://"):
        logger.warning(
            "Wiz API endpoint URL was supplied over http://; upgrading to "
            "https:// so the bearer token is not sent in cleartext."
        )
        value = "https://" + value[len("http://") :]
    elif not lowered.startswith("https://"):
        value = "https://" + value
    value = value.rstrip("/")
    if value.lower().endswith("/graphql"):
        value = value[: -len("/graphql")]
    return value.rstrip("/")


async def _request_access_token(
    client_id: str, client_secret: str
) -> tuple[str | None, int]:
    """Exchange service-account credentials for a short-lived bearer token.

    Tries the Cognito token endpoint (audience ``wiz-api``) first and falls
    back to the legacy Auth0 endpoint (audience ``beyond-api``). Returns
    ``(access_token, expires_in_seconds)`` on success, or ``(None, 0)`` on
    failure. The token endpoints take no prior authentication.
    """
    if not (client_id and client_secret):
        return None, 0
    last_error = ""
    for token_url, audience in _TOKEN_ENDPOINTS:
        try:
            async with egress_client(timeout=20) as client:
                resp = await client.post(
                    token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "audience": audience,
                    },
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "application/json",
                    },
                )
                if resp.is_success:
                    try:
                        data = resp.json()
                    except ValueError:
                        logger.warning(
                            "Wiz token response was not valid JSON (status %s)",
                            resp.status_code,
                        )
                        continue
                    if not isinstance(data, dict):
                        continue
                    token = data.get("access_token")
                    if not token or not isinstance(token, str):
                        logger.warning("Wiz token response contained no token")
                        continue
                    # ``expires_in`` is in seconds when present (Wiz tokens
                    # live ~24 h). Default to a conservative 50 minutes so a
                    # token is never cached past its true lifetime; the shared
                    # cache applies its own expiry skew.
                    expires_in = data.get("expires_in")
                    ttl = (
                        expires_in
                        if isinstance(expires_in, int) and expires_in > 0
                        else 3000
                    )
                    return token, ttl
                last_error = f"{resp.status_code}: {resp.text[:200]}"
        except httpx.HTTPError as exc:
            last_error = format_exception_message(exc)
    if last_error:
        logger.warning("Wiz token request failed (%s)", last_error)
    return None, 0


async def validate_connection(
    api_endpoint_url: str, client_id: str, client_secret: str
) -> tuple[bool, str]:
    """Verify Wiz credentials and the tenant API endpoint.

    Returns ``(ok, message)``. Performs the OAuth2 client-credentials token
    exchange, then a trivial no-scope GraphQL probe (``{ __typename }``)
    against the tenant endpoint so a wrong region/endpoint URL is caught too.
    The probe response must be a JSON GraphQL envelope without ``errors`` -
    a 2xx with non-JSON content (a portal/proxy HTML page) or a GraphQL
    error envelope is a misconfigured endpoint, not a success. Used by the
    integration connectivity test.
    """
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    endpoint = resolve_api_endpoint(api_endpoint_url)
    if not (client_id and client_secret):
        return False, (
            "Missing service account client ID or client secret. Enter the "
            "client ID in extra config and the client secret in the API Key "
            "field."
        )
    if not endpoint:
        return False, (
            "Missing API endpoint URL. Enter your tenant-specific endpoint "
            "(e.g. https://api.us1.app.wiz.io - shown in the Wiz portal under "
            "Profile > User Settings)."
        )
    token, _ = await _request_access_token(client_id, client_secret)
    if not token:
        return False, (
            "Authentication failed - could not obtain a Wiz access token. "
            "Check the service account client ID and client secret."
        )
    try:
        async with egress_client(timeout=20) as client:
            resp = await client.post(
                f"{endpoint}/graphql",
                json={"query": "query { __typename }"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            if not resp.is_success:
                return False, (
                    f"Authenticated, but the API endpoint returned HTTP "
                    f"{resp.status_code} - check that the API endpoint URL "
                    "matches your tenant region (e.g. "
                    "https://api.us1.app.wiz.io)."
                )
            try:
                body = resp.json()
            except ValueError:
                return False, (
                    "Authenticated, but the API endpoint returned a non-JSON "
                    "response - check that the API endpoint URL points at the "
                    "tenant GraphQL API (https://api.REGION.app.wiz.io), "
                    "not the portal UI (https://app.wiz.io)."
                )
            gql_err = _graphql_errors(body)
            if gql_err:
                return False, (
                    f"Authenticated, but the GraphQL probe failed: {gql_err}"
                )
            if not isinstance(body, dict) or not isinstance(
                body.get("data"), dict
            ):
                return False, (
                    "Authenticated, but the API endpoint did not return a "
                    "GraphQL response - check that the API endpoint URL "
                    "points at the tenant GraphQL API "
                    "(https://api.REGION.app.wiz.io)."
                )
            return True, "Wiz is connected and authenticated."
    except httpx.HTTPError as exc:
        return False, (
            "Authenticated, but the API endpoint is unreachable "
            f"({format_exception_message(exc)}) - check the API endpoint URL."
        )


# ---------------------------------------------------------------------------
# Integration class
# ---------------------------------------------------------------------------


class WizIntegration(BaseIntegration):
    """Wiz cloud security (CNAPP) integration."""

    provider_name = "wiz"
    display_name = "Wiz"
    icon = "CloudCog"
    description = (
        "Connect Wiz for cloud security posture - triage and remediate Issues, "
        "query vulnerability findings, search the Security Graph for cloud "
        "resources, and review projects and compliance frameworks."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        endpoint, client_id, client_secret = parse_compound_token(
            token, 3, greedy_last=True
        )
        ok, _ = await validate_connection(endpoint, client_id, client_secret)
        return ok

    async def get_user_info(self, token: str) -> dict:
        # Wiz exposes no service-account whoami endpoint; a successful token
        # exchange is the identity signal. Return the (non-secret) client ID
        # so the UI can display which service account is connected.
        _endpoint, client_id, _secret = parse_compound_token(
            token, 3, greedy_last=True
        )
        return {"client_id": client_id.strip()} if client_id.strip() else {}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _sev(severity: Any) -> str:
    return _SEVERITY_ICONS.get(str(severity or "").upper(), "\u26aa")


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text)
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _dt(value: Any) -> str:
    """Render an ISO 8601 timestamp compactly (date + time, minute precision)."""
    if not value:
        return "N/A"
    return str(value)[:16].replace("T", " ")


def _clamp(value: Any, default: int, lo: int = 1, hi: int = 100) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(lo, min(value, hi))


def _csv_list(value: str) -> list[str]:
    """Split a comma-separated string into a list of trimmed values."""
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def _upper_enum(
    value: str, allowed: frozenset[str], param: str
) -> tuple[list[str], str | None]:
    """Parse a comma-separated enum filter, validating against *allowed*.

    Returns ``(values, None)`` or ``([], error_message)``.
    """
    values = [v.upper().replace("-", "_").replace(" ", "_") for v in _csv_list(value)]
    invalid = [v for v in values if v not in allowed]
    if invalid:
        return [], (
            f"Error: {param} must be one of {', '.join(sorted(allowed))} "
            f"(got: {', '.join(invalid)})."
        )
    return values, None


def _graphql_errors(body: Any) -> str:
    """Extract a readable message from a GraphQL ``errors`` array, if any."""
    if not isinstance(body, dict):
        return ""
    errors = body.get("errors")
    if not isinstance(errors, list) or not errors:
        return ""
    parts: list[str] = []
    for e in errors[:3]:
        if not isinstance(e, dict):
            continue
        msg = str(e.get("message") or "")[:200]
        path = e.get("path")
        if isinstance(path, list) and path:
            msg += f" (at {'.'.join(str(p) for p in path)})"
        if msg:
            parts.append(msg)
    return "; ".join(parts)[:400]


def _entity_line(entity: dict) -> str:
    """Format one graph entity with its most useful inventory properties."""
    props = entity.get("properties") or {}
    if not isinstance(props, dict):
        props = {}
    detail_parts = [
        str(props.get(key))
        for key in ("cloudPlatform", "subscriptionExternalId", "region", "nativeType")
        if props.get(key)
    ]
    detail = " | ".join(detail_parts) if detail_parts else "N/A"
    return (
        f"  {entity.get('name', 'Unnamed')} [{entity.get('type', '?')}] "
        f"(id {entity.get('id', '?')})\n    {detail}"
    )


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Wiz tools from a compound credential token.

    ``token`` is ``api_endpoint_url||client_id||client_secret`` (the client
    secret is the final, greedy segment). Missing credentials still build the
    full tool surface - ``integration_tools._get_token`` returns a partial
    compound token so a misconfiguration surfaces as a clear call-time error
    instead of the tools silently disappearing (mirrors cybersixgill / picus).
    """
    raw_endpoint, client_id, client_secret = (
        part.strip() for part in parse_compound_token(token, 3, greedy_last=True)
    )
    api_endpoint = resolve_api_endpoint(raw_endpoint)
    cache_key = app_token_cache_key("wiz", api_endpoint, client_id, client_secret)

    def _creds_error() -> str | None:
        if not (api_endpoint and client_id and client_secret):
            return (
                "Error: Wiz credentials are not configured. Enter the "
                "tenant-specific API endpoint URL (e.g. "
                "https://api.us1.app.wiz.io) and the service account client ID "
                "in extra config, and the client secret in the API Key field."
            )
        return None

    async def _token() -> str | None:
        cached = get_cached_app_token(cache_key)
        if cached:
            return cached
        access, ttl = await _request_access_token(client_id, client_secret)
        if access:
            set_cached_app_token(cache_key, access, ttl)
        return access

    async def _gql(
        query: str,
        variables: dict[str, Any],
        action: str,
        *,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> tuple[dict, str | None]:
        """Execute a GraphQL request, returning ``(data, error)``.

        GraphQL transports application errors in a 200 response's ``errors``
        array, so both the HTTP layer (via ``safe_request``) and the GraphQL
        envelope are checked before data is handed back to a tool.
        """
        creds_err = _creds_error()
        if creds_err:
            return {}, creds_err
        access = await _token()
        if not access:
            return {}, _AUTH_ERROR
        headers = {
            "Authorization": f"Bearer {access}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        async with egress_client(timeout=timeout) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api_endpoint}/graphql",
                _PROVIDER,
                action,
                headers=headers,
                json={"query": query, "variables": variables},
            )
            if err:
                return {}, err
            try:
                body = resp.json()
            except ValueError:
                logger.warning(
                    "Wiz returned a non-JSON body for %s (status %s)",
                    action,
                    resp.status_code,
                )
                return {}, (
                    f"Error: {_PROVIDER} returned a non-JSON response for "
                    f"'{action}'. Check that the API endpoint URL points at "
                    "the tenant GraphQL API (https://api.REGION.app.wiz.io)."
                )
            gql_err = _graphql_errors(body)
            if gql_err:
                return {}, (
                    f"Error: {_PROVIDER} rejected the request to {action}: "
                    f"{gql_err}"
                )
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, dict):
                # A JSON body without a GraphQL ``data`` envelope (and no
                # ``errors``) is a misconfigured endpoint or an intercepting
                # proxy - surface it instead of returning empty results.
                return {}, (
                    f"Error: {_PROVIDER} returned an unexpected response for "
                    f"'{action}' (no GraphQL data envelope). Check that the "
                    "API endpoint URL points at the tenant GraphQL API "
                    "(https://api.REGION.app.wiz.io)."
                )
            return data, None

    # =====================================================================
    # 0. ISSUES (read)
    # =====================================================================

    async def list_wiz_issues(
        severity: str = "",
        status: str = "OPEN",
        issue_type: str = "",
        search: str = "",
        project_id: str = "",
        framework_category_id: str = "",
        related_entity_type: str = "",
        created_after: str = "",
        created_before: str = "",
        limit: int = 25,
        after: str = "",
    ) -> str:
        """List Wiz Issues with severity/status/type/project/entity filters."""
        filter_by: dict[str, Any] = {}
        if severity:
            values, err = _upper_enum(severity, _SEVERITIES, "severity")
            if err:
                return err
            filter_by["severity"] = values
        if status:
            values, err = _upper_enum(status, _ISSUE_STATUSES, "status")
            if err:
                return err
            filter_by["status"] = values
        if issue_type:
            values, err = _upper_enum(issue_type, _ISSUE_TYPES, "issue_type")
            if err:
                return err
            filter_by["type"] = values
        if search:
            filter_by["search"] = search
        if project_id:
            filter_by["project"] = _csv_list(project_id)
        if framework_category_id:
            filter_by["frameworkCategory"] = _csv_list(framework_category_id)
        if related_entity_type:
            filter_by["relatedEntity"] = {"type": _csv_list(related_entity_type)}
        if created_after or created_before:
            window: dict[str, str] = {}
            if created_after:
                window["after"] = created_after
            if created_before:
                window["before"] = created_before
            filter_by["createdAt"] = window
        variables: dict[str, Any] = {
            "first": _clamp(limit, 25),
            "filterBy": filter_by,
        }
        if after:
            variables["after"] = after
        data, err = await _gql(_ISSUES_QUERY, variables, "list issues")
        if err:
            return err
        conn = data.get("issues") or {}
        nodes = conn.get("nodes") or []

        def _fmt(issue: dict) -> str:
            rule = issue.get("sourceRule") or {}
            snap = issue.get("entitySnapshot") or {}
            projects = ", ".join(
                p.get("name", "?") for p in (issue.get("projects") or [])[:3]
            )
            return (
                f"  {_sev(issue.get('severity'))} [{issue.get('severity', '?')}] "
                f"{rule.get('name', 'Unnamed rule')} "
                f"({issue.get('status', '?')}, {issue.get('type', '?')})\n"
                f"    Issue ID: {issue.get('id', '?')}\n"
                f"    Resource: {snap.get('name', 'N/A')} "
                f"[{snap.get('type', '?')}] on {snap.get('cloudPlatform', 'N/A')} "
                f"({snap.get('subscriptionExternalId', 'N/A')}, "
                f"{snap.get('region') or 'no region'})\n"
                f"    Created: {_dt(issue.get('createdAt'))} | "
                f"Due: {_dt(issue.get('dueAt'))} | "
                f"Projects: {projects or 'N/A'}"
            )

        out = format_entity_list(nodes, _fmt, noun="issue")
        page = conn.get("pageInfo") or {}
        if page.get("hasNextPage"):
            out += format_pagination_hint(page.get("endCursor"), "after")
        return out

    async def get_wiz_issue(issue_id: str = "") -> str:
        """Get a Wiz Issue's full detail: remediation, resource, notes, tickets."""
        if not issue_id:
            return "Error: issue_id is required."
        data, err = await _gql(
            _ISSUE_DETAIL_QUERY, {"id": issue_id}, f"get issue {issue_id}"
        )
        if err:
            return err
        issue = data.get("issue")
        if not isinstance(issue, dict) or not issue.get("id"):
            return f"Issue {issue_id} not found."
        rule = issue.get("sourceRule") or {}
        snap = issue.get("entitySnapshot") or {}
        lines = [
            f"Issue {issue.get('id')}: {rule.get('name', 'Unnamed rule')}",
            f"Severity: {_sev(issue.get('severity'))} {issue.get('severity', '?')} | "
            f"Status: {issue.get('status', '?')} | Type: {issue.get('type', '?')}",
            f"Created: {_dt(issue.get('createdAt'))} | "
            f"Due: {_dt(issue.get('dueAt'))} | "
            f"Resolved: {_dt(issue.get('resolvedAt'))}"
            + (
                f" ({issue.get('resolutionReason')})"
                if issue.get("resolutionReason")
                else ""
            ),
        ]
        projects = issue.get("projects") or []
        if projects:
            lines.append(
                "Projects: "
                + ", ".join(
                    f"{p.get('name', '?')} (id {p.get('id', '?')})"
                    for p in projects[:5]
                )
            )
        description = (
            rule.get("controlDescription")
            or rule.get("cloudConfigurationRuleDescription")
            or rule.get("cloudEventRuleDescription")
        )
        if description:
            lines.append(f"\nRule ({rule.get('__typename', '?')} {rule.get('id', '?')}):")
            lines.append(_trunc(description, 600))
        remediation = rule.get("resolutionRecommendation") or rule.get(
            "remediationInstructions"
        )
        if remediation:
            lines.append(f"\nRemediation:\n{_trunc(remediation, 800)}")
        subcategories = rule.get("securitySubCategories") or []
        if subcategories:
            lines.append(
                "\nCompliance mappings: "
                + "; ".join(
                    f"{(sc.get('category') or {}).get('framework', {}).get('name', '?')}"
                    f" > {(sc.get('category') or {}).get('name', '?')}"
                    f" > {sc.get('title', '?')}"
                    for sc in subcategories[:5]
                )
            )
        if snap:
            lines.append(
                f"\nAffected resource: {snap.get('name', 'N/A')} "
                f"[{snap.get('type', '?')} / {snap.get('nativeType', '?')}]\n"
                f"  Cloud: {snap.get('cloudPlatform', 'N/A')} | "
                f"Subscription: {snap.get('subscriptionName', 'N/A')} "
                f"({snap.get('subscriptionExternalId', 'N/A')}) | "
                f"Region: {snap.get('region') or 'N/A'}\n"
                f"  External ID: {snap.get('externalId', 'N/A')} | "
                f"Status: {snap.get('status', 'N/A')}"
            )
            if snap.get("cloudProviderURL"):
                lines.append(f"  Cloud console: {snap.get('cloudProviderURL')}")
            tags = snap.get("tags")
            if isinstance(tags, dict) and tags:
                lines.append(
                    "  Tags: "
                    + ", ".join(f"{k}={v}" for k, v in list(tags.items())[:10])
                )
        tickets = issue.get("serviceTickets") or []
        if tickets:
            lines.append(
                "\nService tickets: "
                + "; ".join(
                    f"{t.get('name', '?')} ({t.get('externalId', '?')})"
                    for t in tickets[:5]
                )
            )
        notes = issue.get("notes") or []
        if notes:
            lines.append(f"\nNotes ({len(notes)}):")
            for note in notes[:10]:
                author = (note.get("user") or {}).get("name") or (
                    note.get("serviceAccount") or {}
                ).get("name") or "unknown"
                lines.append(
                    f"  - [{_dt(note.get('createdAt'))}] {author}: "
                    f"{_trunc(note.get('text'), 200)}"
                )
        return "\n".join(lines)

    async def get_wiz_posture_summary() -> str:
        """Get open-issue counts by severity/type + the CISA KEV vuln count."""
        data, err = await _gql(_POSTURE_QUERY, {}, "get posture summary")
        if err:
            return err

        def _count(key: str) -> Any:
            node = data.get(key)
            return node.get("totalCount", "N/A") if isinstance(node, dict) else "N/A"

        return (
            "Wiz cloud security posture summary:\n"
            f"\nOpen issues by severity:\n"
            f"  {_sev('CRITICAL')} Critical: {_count('critical')}\n"
            f"  {_sev('HIGH')} High: {_count('high')}\n"
            f"  {_sev('MEDIUM')} Medium: {_count('medium')}\n"
            f"  {_sev('LOW')} Low: {_count('low')}\n"
            f"\nOpen issues by type:\n"
            f"  Toxic combinations: {_count('toxic')}\n"
            f"  Threat detections: {_count('threats')}\n"
            f"\nIn progress (all severities): {_count('inProgress')}\n"
            f"Vulnerability findings on the CISA KEV list: {_count('kev')}"
        )

    # =====================================================================
    # 1. ISSUES (write)
    # =====================================================================

    async def update_wiz_issue(
        issue_id: str = "",
        status: str = "",
        due_at: str = "",
        resolution_reason: str = "",
        resolution_note: str = "",
    ) -> str:
        """Update a Wiz Issue's status, due date, or resolution."""
        if not issue_id:
            return "Error: issue_id is required."
        patch: dict[str, Any] = {}
        if status:
            new_status = status.upper().replace("-", "_").replace(" ", "_")
            if new_status not in _ISSUE_STATUSES:
                return (
                    f"Error: status must be one of "
                    f"{', '.join(sorted(_ISSUE_STATUSES))}."
                )
            patch["status"] = new_status
            if new_status in ("RESOLVED", "REJECTED") and not resolution_reason:
                return (
                    f"Error: resolution_reason is required when setting status "
                    f"to {new_status}. Use one of: "
                    f"{', '.join(sorted(_RESOLUTION_REASONS))}."
                )
        if resolution_reason:
            reason = resolution_reason.upper().replace("-", "_").replace(" ", "_")
            if reason not in _RESOLUTION_REASONS:
                return (
                    f"Error: resolution_reason must be one of "
                    f"{', '.join(sorted(_RESOLUTION_REASONS))}."
                )
            patch["resolutionReason"] = reason
        if resolution_note:
            if patch.get("status") == "REJECTED":
                patch["note"] = resolution_note
            else:
                patch["resolutionNote"] = resolution_note
        if due_at:
            patch["dueAt"] = due_at
        if not patch:
            return (
                "Error: provide at least one field to update (status, due_at, "
                "resolution_reason, resolution_note)."
            )
        data, err = await _gql(
            _UPDATE_ISSUE_MUTATION,
            {"issueId": issue_id, "patch": patch},
            f"update issue {issue_id}",
        )
        if err:
            return err
        issue = ((data.get("updateIssue") or {}).get("issue")) or {}
        return (
            f"Updated issue {issue.get('id', issue_id)}: "
            f"status={issue.get('status', 'unchanged')}, "
            f"dueAt={issue.get('dueAt') or 'unset'}"
            + (
                f", resolutionReason={issue.get('resolutionReason')}"
                if issue.get("resolutionReason")
                else ""
            )
            + "."
        )

    async def add_wiz_issue_note(issue_id: str = "", text: str = "") -> str:
        """Append an investigation note to a Wiz Issue."""
        if not issue_id or not text:
            return "Error: both issue_id and text are required."
        data, err = await _gql(
            _CREATE_ISSUE_NOTE_MUTATION,
            {"input": {"issueId": issue_id, "text": text}},
            f"add note to issue {issue_id}",
        )
        if err:
            return err
        note = ((data.get("createIssueNote") or {}).get("issueNote")) or {}
        return (
            f"Note added to issue {issue_id} "
            f"(note id {note.get('id', '?')}, {_dt(note.get('createdAt'))})."
        )

    # =====================================================================
    # 2. VULNERABILITIES (read)
    # =====================================================================

    async def list_wiz_vulnerability_findings(
        cve: str = "",
        vendor_severity: str = "",
        status: str = "",
        has_fix: bool | None = None,
        has_exploit: bool | None = None,
        has_cisa_kev_exploit: bool | None = None,
        asset_id: str = "",
        project_id: str = "",
        detection_method: str = "",
        first_seen_after: str = "",
        first_seen_before: str = "",
        limit: int = 25,
        after: str = "",
    ) -> str:
        """List Wiz vulnerability findings with CVE/severity/exploit filters."""
        filter_by: dict[str, Any] = {}
        if cve:
            filter_by["vulnerabilityExternalId"] = [
                c.upper() for c in _csv_list(cve)
            ]
        if vendor_severity:
            values, err = _upper_enum(
                vendor_severity, _SEVERITIES, "vendor_severity"
            )
            if err:
                return err
            filter_by["vendorSeverity"] = values
        if status:
            values, err = _upper_enum(status, _VULN_STATUSES, "status")
            if err:
                return err
            filter_by["status"] = values
        if has_fix is not None:
            filter_by["hasFix"] = bool(has_fix)
        if has_exploit is not None:
            filter_by["hasExploit"] = bool(has_exploit)
        if has_cisa_kev_exploit is not None:
            filter_by["hasCisaKevExploit"] = bool(has_cisa_kev_exploit)
        if asset_id:
            filter_by["assetId"] = _csv_list(asset_id)
        if project_id:
            filter_by["projectId"] = _csv_list(project_id)
        if detection_method:
            filter_by["detectionMethod"] = [
                m.upper() for m in _csv_list(detection_method)
            ]
        if first_seen_after or first_seen_before:
            window: dict[str, str] = {}
            if first_seen_after:
                window["after"] = first_seen_after
            if first_seen_before:
                window["before"] = first_seen_before
            filter_by["firstSeenAt"] = window
        variables: dict[str, Any] = {
            "first": _clamp(limit, 25),
            "filterBy": filter_by,
        }
        if after:
            variables["after"] = after
        data, err = await _gql(
            _VULN_FINDINGS_QUERY, variables, "list vulnerability findings"
        )
        if err:
            return err
        conn = data.get("vulnerabilityFindings") or {}
        nodes = conn.get("nodes") or []

        def _fmt(finding: dict) -> str:
            asset = finding.get("vulnerableAsset") or {}
            flags = []
            if finding.get("hasCisaKevExploit"):
                flags.append("CISA KEV")
            if finding.get("hasExploit"):
                flags.append("exploit available")
            if finding.get("fixedVersion"):
                flags.append(f"fix: {finding.get('fixedVersion')}")
            return (
                f"  {_sev(finding.get('vendorSeverity'))} "
                f"{finding.get('name', '?')} "
                f"[{finding.get('vendorSeverity', '?')}] "
                f"({finding.get('status', '?')}, finding {finding.get('id', '?')})\n"
                f"    Component: {finding.get('detailedName') or 'N/A'} "
                f"{finding.get('version') or ''} | "
                f"CVSS: {finding.get('score', 'N/A')} | "
                f"{' | '.join(flags) if flags else 'no exploit/fix flags'}\n"
                f"    Asset: {asset.get('name', 'N/A')} "
                f"[{asset.get('type', '?')}] on {asset.get('cloudPlatform', 'N/A')} "
                f"({asset.get('subscriptionExternalId', 'N/A')}, "
                f"{asset.get('region') or 'no region'}, "
                f"asset id {asset.get('id', '?')})\n"
                f"    First seen: {_dt(finding.get('firstDetectedAt'))} | "
                f"Last seen: {_dt(finding.get('lastDetectedAt'))}"
            )

        out = format_entity_list(nodes, _fmt, noun="vulnerability finding")
        page = conn.get("pageInfo") or {}
        if page.get("hasNextPage"):
            out += format_pagination_hint(page.get("endCursor"), "after")
        return out

    # =====================================================================
    # 3. INVENTORY (read)
    # =====================================================================

    async def search_wiz_cloud_resources(
        entity_type: str = "",
        cloud_platform: str = "",
        subscription_external_id: str = "",
        name_contains: str = "",
        project_id: str = "",
        limit: int = 25,
        after: str = "",
    ) -> str:
        """Search the Wiz Security Graph for cloud resources by type/cloud/subscription."""
        if not entity_type:
            return (
                "Error: entity_type is required (a Wiz graph entity type such "
                "as VIRTUAL_MACHINE, CONTAINER, BUCKET, DATABASE, "
                "SERVICE_ACCOUNT, USER_ACCOUNT, SECRET, KUBERNETES_CLUSTER, "
                "SUBSCRIPTION)."
            )
        types = [
            t.upper().replace("-", "_").replace(" ", "_")
            for t in _csv_list(entity_type)
        ]
        where: dict[str, Any] = {}
        if cloud_platform:
            where["cloudPlatform"] = {"EQUALS": _csv_list(cloud_platform)}
        if subscription_external_id:
            where["subscriptionExternalId"] = {
                "EQUALS": _csv_list(subscription_external_id)
            }
        if name_contains:
            where["name"] = {"CONTAINS": [name_contains]}
        query_input: dict[str, Any] = {"type": types, "select": True}
        if where:
            query_input["where"] = where
        variables: dict[str, Any] = {
            "query": query_input,
            "projectId": project_id or "*",
            "first": _clamp(limit, 25),
        }
        if after:
            variables["after"] = after
        data, err = await _gql(
            _GRAPH_SEARCH_QUERY, variables, "search cloud resources"
        )
        if err:
            return err
        conn = data.get("graphSearch") or {}
        nodes = conn.get("nodes") or []
        entities = [
            (node.get("entities") or [None])[0]
            for node in nodes
            if isinstance(node, dict) and node.get("entities")
        ]
        entities = [e for e in entities if isinstance(e, dict)]
        total = conn.get("totalCount")
        out = format_entity_list(
            entities,
            _entity_line,
            noun="cloud resource",
            total=total if isinstance(total, int) else None,
        )
        if conn.get("maxCountReached"):
            out += "\n[Total count capped by Wiz - narrow the filters for exact numbers]"
        page = conn.get("pageInfo") or {}
        if page.get("hasNextPage"):
            out += format_pagination_hint(page.get("endCursor"), "after")
        return out

    async def list_wiz_cloud_accounts(
        search: str = "",
        cloud_provider: str = "",
        limit: int = 50,
        after: str = "",
    ) -> str:
        """List cloud accounts/subscriptions connected to Wiz."""
        filter_by: dict[str, Any] = {}
        if search:
            # Unlike IssueFilters.search / ProjectFilters.search (String),
            # CloudAccountFilters.search is [String!] - a list of name /
            # external-ID terms.
            filter_by["search"] = _csv_list(search)
        if cloud_provider:
            filter_by["cloudProvider"] = _csv_list(cloud_provider)
        variables: dict[str, Any] = {
            "first": _clamp(limit, 50),
            "filterBy": filter_by,
        }
        if after:
            variables["after"] = after
        data, err = await _gql(
            _CLOUD_ACCOUNTS_QUERY, variables, "list cloud accounts"
        )
        if err:
            return err
        conn = data.get("cloudAccounts") or {}
        nodes = conn.get("nodes") or []

        def _fmt(account: dict) -> str:
            projects = ", ".join(
                p.get("name", "?") for p in (account.get("linkedProjects") or [])[:3]
            )
            return (
                f"  {account.get('name', 'Unnamed')} "
                f"({account.get('cloudProvider', '?')} "
                f"{account.get('externalId', '?')}) - "
                f"{account.get('status', '?')}\n"
                f"    Account ID: {account.get('id', '?')} | "
                f"Last scanned: {_dt(account.get('lastScannedAt'))} | "
                f"Projects: {projects or 'N/A'}"
            )

        out = format_entity_list(nodes, _fmt, noun="cloud account")
        page = conn.get("pageInfo") or {}
        if page.get("hasNextPage"):
            out += format_pagination_hint(page.get("endCursor"), "after")
        return out

    # =====================================================================
    # 4. GOVERNANCE (read)
    # =====================================================================

    async def list_wiz_projects(
        search: str = "",
        include_archived: bool = False,
        limit: int = 50,
        after: str = "",
    ) -> str:
        """List Wiz projects with business context and risk profile."""
        filter_by: dict[str, Any] = {}
        if search:
            filter_by["search"] = search
        if include_archived:
            filter_by["includeArchived"] = True
        variables: dict[str, Any] = {
            "first": _clamp(limit, 50),
            "filterBy": filter_by,
        }
        if after:
            variables["after"] = after
        data, err = await _gql(_PROJECTS_QUERY, variables, "list projects")
        if err:
            return err
        conn = data.get("projects") or {}
        nodes = conn.get("nodes") or []

        def _fmt(project: dict) -> str:
            risk = (project.get("riskProfile") or {}).get("businessImpact", "N/A")
            marker = " [folder]" if project.get("isFolder") else ""
            marker += " [archived]" if project.get("archived") else ""
            return (
                f"  {project.get('name', 'Unnamed')}{marker} "
                f"(id {project.get('id', '?')}, slug {project.get('slug', '?')})\n"
                f"    Business unit: {project.get('businessUnit') or 'N/A'} | "
                f"Business impact: {risk}"
                + (
                    f"\n    {_trunc(project.get('description'), 150)}"
                    if project.get("description")
                    else ""
                )
            )

        out = format_entity_list(nodes, _fmt, noun="project")
        page = conn.get("pageInfo") or {}
        if page.get("hasNextPage"):
            out += format_pagination_hint(page.get("endCursor"), "after")
        return out

    async def list_wiz_security_frameworks(
        enabled_only: bool = True,
        limit: int = 25,
        after: str = "",
    ) -> str:
        """List Wiz security/compliance frameworks with their control categories."""
        filter_by: dict[str, Any] = {}
        if enabled_only:
            filter_by["enabled"] = True
        variables: dict[str, Any] = {
            "first": _clamp(limit, 25, hi=50),
            "filterBy": filter_by,
        }
        if after:
            variables["after"] = after
        data, err = await _gql(
            _FRAMEWORKS_QUERY, variables, "list security frameworks"
        )
        if err:
            return err
        conn = data.get("securityFrameworks") or {}
        nodes = conn.get("nodes") or []

        def _fmt(framework: dict) -> str:
            # GraphQL list elements are nullable - drop nulls before formatting.
            categories = [
                c
                for c in (framework.get("categories") or [])
                if isinstance(c, dict)
            ]
            subcategory_count = sum(
                len(c.get("subCategories") or []) for c in categories
            )
            category_names = ", ".join(
                c.get("name", "?") for c in categories[:6]
            )
            if len(categories) > 6:
                category_names += ", ..."
            return (
                f"  {framework.get('name', 'Unnamed')} "
                f"(id {framework.get('id', '?')}) - "
                f"{'enabled' if framework.get('enabled') else 'disabled'}"
                f"{', built-in' if framework.get('builtin') else ', custom'}\n"
                f"    Categories: {len(categories)} "
                f"({subcategory_count} controls): {category_names or 'N/A'}"
            )

        out = format_entity_list(nodes, _fmt, noun="security framework")
        page = conn.get("pageInfo") or {}
        if page.get("hasNextPage"):
            out += format_pagination_hint(page.get("endCursor"), "after")
        return out

    # =====================================================================
    # REGISTER ALL TOOLS
    # =====================================================================

    executor.register("list_wiz_issues", list_wiz_issues)
    executor.register("get_wiz_issue", get_wiz_issue)
    executor.register("get_wiz_posture_summary", get_wiz_posture_summary)
    executor.register("update_wiz_issue", update_wiz_issue)
    executor.register("add_wiz_issue_note", add_wiz_issue_note)
    executor.register(
        "list_wiz_vulnerability_findings", list_wiz_vulnerability_findings
    )
    executor.register("search_wiz_cloud_resources", search_wiz_cloud_resources)
    executor.register("list_wiz_cloud_accounts", list_wiz_cloud_accounts)
    executor.register("list_wiz_projects", list_wiz_projects)
    executor.register("list_wiz_security_frameworks", list_wiz_security_frameworks)

    return [
        # -- 0. Issues (read) ----------------------------------------------
        ToolDefinition(
            name="list_wiz_issues",
            description=(
                "List Wiz cloud security Issues (correlated risk findings). "
                "Filter by severity (CRITICAL, HIGH, MEDIUM, LOW, "
                "INFORMATIONAL), status (OPEN, IN_PROGRESS, RESOLVED, "
                "REJECTED - defaults to OPEN), issue_type (TOXIC_COMBINATION, "
                "THREAT_DETECTION, CLOUD_CONFIGURATION), free-text search, "
                "project, security-framework category, related entity type "
                "(e.g. VIRTUAL_MACHINE, BUCKET), and creation window. Returns "
                "issue IDs for get_wiz_issue / update_wiz_issue. "
                "Comma-separate multiple filter values."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "severity": {
                        "type": "string",
                        "description": (
                            "Comma-separated severities (CRITICAL, HIGH, "
                            "MEDIUM, LOW, INFORMATIONAL)"
                        ),
                    },
                    "status": {
                        "type": "string",
                        "default": "OPEN",
                        "description": (
                            "Comma-separated statuses (OPEN, IN_PROGRESS, "
                            "RESOLVED, REJECTED); empty = all"
                        ),
                    },
                    "issue_type": {
                        "type": "string",
                        "description": (
                            "Comma-separated types (TOXIC_COMBINATION, "
                            "THREAT_DETECTION, CLOUD_CONFIGURATION)"
                        ),
                    },
                    "search": {
                        "type": "string",
                        "description": "Free-text search on issue title / resource name",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Comma-separated Wiz project IDs (see list_wiz_projects)",
                    },
                    "framework_category_id": {
                        "type": "string",
                        "description": (
                            "Comma-separated security-framework category IDs "
                            "(see list_wiz_security_frameworks)"
                        ),
                    },
                    "related_entity_type": {
                        "type": "string",
                        "description": (
                            "Comma-separated graph entity types of the "
                            "affected resource (e.g. VIRTUAL_MACHINE, BUCKET)"
                        ),
                    },
                    "created_after": {
                        "type": "string",
                        "description": "ISO 8601 lower bound, e.g. 2026-01-01T00:00:00Z",
                    },
                    "created_before": {
                        "type": "string",
                        "description": "ISO 8601 upper bound",
                    },
                    "limit": {"type": "integer", "default": 25},
                    "after": {
                        "type": "string",
                        "description": "Pagination cursor from a previous call",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_wiz_issue",
            description=(
                "Get a Wiz Issue's full detail and evidence: the source rule "
                "with its description and remediation recommendation, "
                "compliance framework mappings, the affected cloud resource "
                "snapshot (cloud, subscription, region, tags, console link), "
                "linked projects, service tickets, and investigation notes."
            ),
            input_schema={
                "type": "object",
                "properties": {"issue_id": {"type": "string"}},
                "required": ["issue_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_wiz_posture_summary",
            description=(
                "Get a one-shot Wiz cloud security posture summary: open "
                "issue counts by severity (critical/high/medium/low), open "
                "toxic-combination and threat-detection counts, in-progress "
                "issues, and the number of vulnerability findings on the "
                "CISA KEV (Known Exploited Vulnerabilities) list. Use this "
                "first for status reports before drilling into "
                "list_wiz_issues."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
        # -- 1. Issues (write) ----------------------------------------------
        ToolDefinition(
            name="update_wiz_issue",
            description=(
                "Update a Wiz Issue: change status (OPEN, IN_PROGRESS, "
                "RESOLVED, REJECTED), set a due date, or record the "
                "resolution. Setting RESOLVED or REJECTED requires "
                "resolution_reason (standard issues: OBJECT_DELETED, "
                "ISSUE_FIXED, FALSE_POSITIVE, EXCEPTION, WONT_FIX; threat "
                "detection issues: MALICIOUS_THREAT, NOT_MALICIOUS_THREAT, "
                "SECURITY_TEST_THREAT, PLANNED_ACTION_THREAT, "
                "INCONCLUSIVE_THREAT). Note: only Threat Detection Issues can "
                "be manually RESOLVED - Wiz auto-resolves other issue types "
                "on the next scan; use REJECTED to dismiss them. For plain "
                "comments use add_wiz_issue_note instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["OPEN", "IN_PROGRESS", "RESOLVED", "REJECTED"],
                    },
                    "due_at": {
                        "type": "string",
                        "description": "ISO 8601 due date, e.g. 2026-08-01T00:00:00Z",
                    },
                    "resolution_reason": {
                        "type": "string",
                        "description": (
                            "Required with RESOLVED/REJECTED; see the tool "
                            "description for valid values per issue type"
                        ),
                    },
                    "resolution_note": {
                        "type": "string",
                        "description": "Free-text note explaining the status change",
                    },
                },
                "required": ["issue_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="add_wiz_issue_note",
            description=(
                "Append an investigation note (comment) to a Wiz Issue. "
                "Notes are visible to the security team in the Wiz portal - "
                "use for triage findings, context, and remediation progress."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["issue_id", "text"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # -- 2. Vulnerabilities (read) ---------------------------------------
        ToolDefinition(
            name="list_wiz_vulnerability_findings",
            description=(
                "List Wiz vulnerability findings (CVE detections on cloud "
                "workloads). Filter by CVE ID(s), vendor_severity (CRITICAL, "
                "HIGH, MEDIUM, LOW), status (OPEN, RESOLVED), exploitability "
                "flags (has_exploit, has_cisa_kev_exploit), fix availability "
                "(has_fix), asset ID, project, detection method, and first "
                "seen window. Returns the vulnerable component/version, fixed "
                "version, CVSS score, and the affected asset with its cloud "
                "and subscription."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cve": {
                        "type": "string",
                        "description": "Comma-separated CVE IDs, e.g. CVE-2021-44228",
                    },
                    "vendor_severity": {
                        "type": "string",
                        "description": (
                            "Comma-separated severities (CRITICAL, HIGH, "
                            "MEDIUM, LOW, INFORMATIONAL)"
                        ),
                    },
                    "status": {
                        "type": "string",
                        "description": "Comma-separated statuses (OPEN, RESOLVED)",
                    },
                    "has_fix": {
                        "type": "boolean",
                        "description": "Only findings with an available fix",
                    },
                    "has_exploit": {
                        "type": "boolean",
                        "description": "Only findings with a known exploit",
                    },
                    "has_cisa_kev_exploit": {
                        "type": "boolean",
                        "description": "Only findings on the CISA KEV list",
                    },
                    "asset_id": {
                        "type": "string",
                        "description": "Comma-separated Wiz asset IDs",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Comma-separated Wiz project IDs",
                    },
                    "detection_method": {
                        "type": "string",
                        "description": (
                            "Comma-separated detection methods (e.g. PACKAGE, "
                            "LIBRARY, FILE_PATH)"
                        ),
                    },
                    "first_seen_after": {
                        "type": "string",
                        "description": "ISO 8601 lower bound on first detection",
                    },
                    "first_seen_before": {
                        "type": "string",
                        "description": "ISO 8601 upper bound on first detection",
                    },
                    "limit": {"type": "integer", "default": 25},
                    "after": {
                        "type": "string",
                        "description": "Pagination cursor from a previous call",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        # -- 3. Inventory (read) ----------------------------------------------
        ToolDefinition(
            name="search_wiz_cloud_resources",
            description=(
                "Search the Wiz Security Graph for cloud resources by graph "
                "entity type - e.g. VIRTUAL_MACHINE, CONTAINER, BUCKET, "
                "DATABASE, SERVICE_ACCOUNT, USER_ACCOUNT, SECRET, "
                "KUBERNETES_CLUSTER, SUBSCRIPTION. Refine by cloud platform "
                "(AWS, GCP, Azure, OCI, Alibaba), subscription external ID, "
                "resource name substring, or a Wiz project. Returns each "
                "resource's graph ID (usable as asset_id in "
                "list_wiz_vulnerability_findings), cloud, subscription, and "
                "region."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_type": {
                        "type": "string",
                        "description": (
                            "Comma-separated Wiz graph entity types, e.g. "
                            "VIRTUAL_MACHINE, BUCKET, DATABASE"
                        ),
                    },
                    "cloud_platform": {
                        "type": "string",
                        "description": "Comma-separated platforms (AWS, GCP, Azure, OCI, Alibaba)",
                    },
                    "subscription_external_id": {
                        "type": "string",
                        "description": (
                            "Comma-separated cloud account IDs (AWS account "
                            "ID, Azure subscription ID, GCP project ID)"
                        ),
                    },
                    "name_contains": {
                        "type": "string",
                        "description": "Substring match on the resource name",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Scope to one Wiz project ID (default: all projects)",
                    },
                    "limit": {"type": "integer", "default": 25},
                    "after": {
                        "type": "string",
                        "description": "Pagination cursor from a previous call",
                    },
                },
                "required": ["entity_type"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_wiz_cloud_accounts",
            description=(
                "List the cloud accounts/subscriptions connected to Wiz with "
                "their provider, external ID, connection status, linked "
                "projects, and last scan time. Use to confirm coverage scope "
                "before analysing issues or vulnerabilities."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Comma-separated name/external-ID search terms",
                    },
                    "cloud_provider": {
                        "type": "string",
                        "description": "Comma-separated providers (AWS, GCP, Azure, OCI, Alibaba)",
                    },
                    "limit": {"type": "integer", "default": 50},
                    "after": {
                        "type": "string",
                        "description": "Pagination cursor from a previous call",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        # -- 4. Governance (read) ---------------------------------------------
        ToolDefinition(
            name="list_wiz_projects",
            description=(
                "List Wiz projects (the organizational grouping of cloud "
                "resources) with business unit, business impact rating, and "
                "description. Returns project IDs used to scope "
                "list_wiz_issues, list_wiz_vulnerability_findings, and "
                "search_wiz_cloud_resources."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Free-text search on project name",
                    },
                    "include_archived": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "default": 50},
                    "after": {
                        "type": "string",
                        "description": "Pagination cursor from a previous call",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_wiz_security_frameworks",
            description=(
                "List Wiz security/compliance frameworks (CIS, NIST, "
                "PCI DSS, SOC 2, Wiz built-ins, custom frameworks) with "
                "their control categories and control counts. Framework "
                "category IDs can be used to filter list_wiz_issues for "
                "compliance-focused triage."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "enabled_only": {"type": "boolean", "default": True},
                    "limit": {"type": "integer", "default": 25},
                    "after": {
                        "type": "string",
                        "description": "Pagination cursor from a previous call",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
