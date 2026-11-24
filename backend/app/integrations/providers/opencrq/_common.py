"""OpenCRQ integration – Governance, Risk & Compliance platform.

Provides tools for exploring and managing OpenCRQ frameworks, controls,
assessments, risk scenarios, assets, and cyber risk quantification via
the OpenCRQ tRPC-over-HTTP API.

Token format passed by the dispatcher: ``base_url||api_token`` (the
extra_config base_url is prepended by ``_get_token`` in
``integration_tools.py``).  For platform-managed integrations the
api_token is a short-lived per-user Ed25519 JWT minted by XTM One and
verified by OpenCRQ against the XTM One JWKS.

OpenCRQ is multi-tenant: every call is scoped by an ``x-tenant-id``
header.  Tools accept an optional ``tenant_id`` argument and fall back
to the calling user's sole tenant when omitted (see ``_resolve_tenant``).

That per-user JWT lives ten minutes, so every client in this provider is
opened through :func:`_client` and every call goes out through :func:`_trpc`,
which re-mints the token before it expires and retries once if OpenCRQ
rejects it anyway.

What OpenCRQ answers is third-party text: threat and CVE feeds, connector
inventories (Microsoft Graph, OpenAEV, Drata) and what its users typed. A
read tool returns the records it formats inside a ``wrap_untrusted`` block
(:func:`_quarantined`), each free-text value on one line and length-capped
(:func:`_one_line`); only the counts, paging hints and lookup failures this
package writes sit outside it.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from contextlib import AsyncExitStack, suppress
from typing import Any, Literal

import httpx

from app.core.egress import egress_client
from app.integrations.http import (
    AutoRefreshClient,
    RefreshableHeaders,
    RefreshFn,
    ensure_fresh_token,
    safe_request,
)
from app.services.content_safety import prompt_label, wrap_untrusted

from .input_contract import InputContract
from .procedure_manifest import ManifestProbe

logger = logging.getLogger(__name__)

_PROVIDER = "OpenCRQ"

#: An OpenCRQ entity id, as opposed to an ATT&CK external id such as ``T1059``.
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)

# Map (entity_type → UI path builder) for OpenCRQ entities that have a
# known canonical UI route.  Routes mirror the TanStack Router file tree
# in OpenCRQ's ``front/src/routes/_auth/$tenantId/{-$organizationId}``:
# the organization segment is optional, so a tenant-wide page is
# ``/{tenant}/<path>`` and an organization's own page is
# ``/{tenant}/{organization}/<path>``.  Entities without a stable detail
# page get no ``Link:`` line — no link is better than a wrong link.
_GRC_UI_PATHS: dict[str, str] = {
    "framework": "/compliance/{id}",
    "control": "/controls/{id}/overview",
    "organization": "/{id}/details/overview",
    "supporting_asset": "/assets/{id}/overview",
    "loss_driver": "/settings/taxonomies/loss-drivers/{id}",
    # Keyed by the CVE name (``CVE-2024-3400``), not the row's UUID.
    "cve": "/settings/taxonomies/cve/{id}",
    # Keyed by the ATT&CK external id (``T1566.001``, ``M1017``), not the UUID.
    "mitre_technique": "/settings/taxonomies/mitre-attack/techniques/{id}/overview",
    "mitre_mitigation": "/settings/taxonomies/mitre-attack/mitigations/{id}",
    "user": "/settings/security/users/{id}",
    "service_account": "/settings/security/service-accounts/{id}",
    "threat": "/threat-center/threats/{id}/overview",
    "finding": "/findings/{id}",
    "cve_finding": "/findings/cve/{id}/overview",
    "control_validation": "/controls/{id}/validation",
    # Both detail pages redirect to the entity's own organization.
    "treatment_plan": "/treatment-plans/{id}",
    "budget_review": "/budget-reviews/{id}",
    # Keyed by the connector source, ``_`` written ``-``.
    "connector": "/integrations/connectors/{id}/overview",
    "dashboard": "/dashboards/{id}",
}


#: One path segment of a link. Ids reach a link from tool arguments, so one
#: carrying ``/``, ``)`` or a leading dot would break or redirect the path.
_LINK_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _plain_segments(*values: object) -> bool:
    return all(isinstance(v, str) and _LINK_SEGMENT.fullmatch(v) for v in values)


def _grc_link(
    base_url: str,
    tenant_id: str,
    entity_type: str,
    entity_id: str | None,
    *,
    organization_id: str | None = None,
) -> str:
    """Return a markdown ``Link:`` line for the entity, or ``""``.

    Only for the tenant-scoped pages of ``_GRC_UI_PATHS``; a page under
    an organization has its own helper (:func:`_grc_scenario_link`,
    :func:`_grc_primary_asset_link`).

    Returns an empty string when ``base_url`` / ``tenant_id`` /
    ``entity_id`` are missing or not plain path segments, or the entity
    type has no canonical UI route (never emit a bare path the chat UI
    would mis-resolve). *organization_id* puts the page under that
    organization, for a page the organization narrows.
    """
    if not base_url or not entity_id or entity_id == "N/A":
        return ""
    path = _GRC_UI_PATHS.get(entity_type)
    if not path:
        return ""
    segments = [tenant_id, organization_id] if organization_id else [tenant_id]
    if not _plain_segments(*segments, str(entity_id)):
        return ""
    return (
        f"  Link: [Open in OpenCRQ]"
        f"({base_url}/{'/'.join(segments)}{path.format(id=entity_id)})"
    )


def _grc_scenario_link(
    base_url: str, tenant_id: str, organization_id: str | None, scenario_id: str | None
) -> str:
    """Risk scenarios live under their organization in the OpenCRQ UI.

    Same "no link is better than a wrong link" contract as
    :func:`_grc_link` — including the ``"N/A"`` sentinel the formatters
    use when the API response lacks an id.
    """
    if not base_url or not _plain_segments(tenant_id, organization_id, scenario_id):
        return ""
    return (
        f"  Link: [Open in OpenCRQ]({base_url}/{tenant_id}/"
        f"{organization_id}/risk-scenarios/{scenario_id}/overview)"
    )


def _grc_primary_asset_link(
    base_url: str, tenant_id: str, organization_id: str | None, asset_id: str | None
) -> str:
    """Primary assets live under their organization in the OpenCRQ UI.

    Same "no link is better than a wrong link" contract as
    :func:`_grc_scenario_link`.
    """
    if not base_url or not _plain_segments(tenant_id, organization_id, asset_id):
        return ""
    return (
        f"  Link: [Open in OpenCRQ]({base_url}/{tenant_id}/"
        f"{organization_id}/primary-assets/{asset_id})"
    )


# ---------------------------------------------------------------------------
# Enums (matching OpenCRQ API constants)
# ---------------------------------------------------------------------------

STRENGTH_RUNGS = ["L0", "L1", "L2", "L3"]

COVERAGE_BUCKETS = ["L0", "L1", "L2", "L3"]

#: Supporting-asset exposure (3-value list used by supportingAssets.*).
ASSET_EXPOSURES = ["internet_facing", "internal", "unknown"]

ASSET_CATEGORIES = [
    "host",
    "container_workload",
    "cloud_resource",
    "web_application",
    "network_device",
    "mobile_device",
    "iot_ot_device",
    "identity",
    "saas_application",
    "ai_target",
    "security_platform",
    "source_code",
    "generic_asset",
]

#: Category -> the subcategories it holds (SUBCATEGORY_TO_CATEGORY in OpenCRQ's
#: supporting-assets/constants.ts, inverted); OpenCRQ refuses any other pair.
ASSET_SUBCATEGORIES: dict[str, tuple[str, ...]] = {
    "host": (
        "server", "workstation", "laptop", "virtual_machine", "hypervisor",
        "mainframe", "thin_client",
    ),
    "container_workload": (
        "container", "container_image", "kubernetes_pod", "kubernetes_cluster",
        "kubernetes_node", "serverless_function",
    ),
    "cloud_resource": (
        "compute", "storage", "database", "networking", "serverless",
        "container_registry", "kubernetes", "iam_principal", "secrets_key_mgmt",
        "messaging_queue", "analytics_data", "ai_ml_service", "iac_template",
        "cloud_other",
    ),
    "web_application": (
        "website", "web_api", "single_page_app", "graphql_api", "web_service",
        "microservice",
    ),
    "network_device": (
        "router", "switch", "firewall", "load_balancer", "vpn_gateway",
        "wireless_ap", "proxy", "dns_server", "dhcp_server", "san_nas",
        "network_other",
    ),
    "mobile_device": ("smartphone", "tablet"),
    "iot_ot_device": (
        "iot_sensor", "ip_camera", "gateway", "point_of_sale", "media_device",
        "plc", "rtu", "hmi", "scada_historian", "medical_device",
        "printer_peripheral", "building_mgmt",
    ),
    "identity": (
        "user_account", "service_account", "group", "role", "shared_mailbox",
        "non_human_identity",
    ),
    "saas_application": ("saas_app", "saas_tenant"),
    "ai_target": ("llm_model", "ai_agent", "mcp_server", "rag_pipeline"),
    "security_platform": (
        "edr",
        "xdr",
        "siem",
        "soar",
        "ndr",
        "ispm",
        "email_security",
        "llm_firewall",
        "ai_gateway",
        "vulnerability_scanner",
    ),
    "source_code": (
        "code_repository",
        "ci_cd_pipeline",
        "package_registry",
        "software_package",
        "code_other",
    ),
    "generic_asset": (),
}

ASSET_CRITICALITIES = ["unknown", "low", "medium", "high", "very_high"]

EVIDENCE_STATES = ["applicable", "dismissed"]

JOB_STATUSES = ["queued", "running", "succeeded", "cancelled", "dlq"]

#: Shared ``tenant_id`` input-schema fragment for tool definitions.
TENANT_PROP = {
    "tenant_id": {
        "type": "string",
        "description": (
            "OpenCRQ tenant ID. Optional when the account has a single "
            "tenant (use list_opencrq_tenants to discover)."
        ),
    }
}


#: Shared ``organization_id`` input-schema fragment for the tools reading a
#: risk scope's estate (findings, dashboards, plans, budget reviews).
ORGANIZATION_SCOPE_PROP = {
    "organization_id": {
        "type": "string",
        "description": (
            "Organization (risk scope) UUID to narrow the answer to. Omit for "
            "the whole tenant (use list_opencrq_organizations to discover). "
            "The answer names the scope it was read for."
        ),
    }
}

#: A CVE identifier, as OpenCRQ's ``CVE_ID_PATTERN`` accepts it.
CVE_ID_PATTERN = re.compile(r"^CVE-\d{4}-\d{4,}$")


def _text(value: Any) -> str:
    """A string tool argument, stripped; a client's JSON ``null`` reads as empty."""
    return "" if value is None else str(value).strip()


def _as_int(value: Any) -> int | None:
    """An integer tool argument (``"2"`` included), or ``None`` when it is not one."""
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _scoped(payload: dict, organization_id: str) -> dict:
    """Add ``organizationId`` to *payload* only when one was given.

    Some OpenCRQ inputs take it as optional but not nullable, so an empty
    scope is left out rather than sent as ``null``.
    """
    organization_id = (organization_id or "").strip()
    if organization_id:
        payload["organizationId"] = organization_id
    return payload


def _fmt_share(value: Any) -> str:
    """Render a 0..1 OpenCRQ ratio as a whole percentage."""
    if value is None:
        return "n/a"
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return str(value)


def _clean_ids(ids: list[str] | None) -> list[str]:
    """Normalise a UUID-list tool argument (strip blanks, drop empties).

    ``None`` entries are dropped first — stringifying them would forward
    the literal ``"None"`` to OpenCRQ as an ID and cause confusing
    downstream validation errors.
    """
    return [
        str(i).strip() for i in (ids or []) if i is not None and str(i).strip()
    ]


# ---------------------------------------------------------------------------
# tRPC-over-HTTP helpers
# ---------------------------------------------------------------------------


class OpenCrqHeaders(RefreshableHeaders):
    """The provider's headers, carrying the build's :class:`ManifestProbe` when the build has one.

    :func:`_trpc` holds a call's input against what the connected instance's manifest
    accepts; headers without a probe (a plain dict, a build made with no
    ``opencrq_manifest``) are never checked.
    """

    manifest_probe: ManifestProbe | None = None


def _headers(api_token: str, refresh_fn: RefreshFn | None = None) -> OpenCrqHeaders:
    """Build the provider-wide auth headers, carrying the token re-minter.

    A platform-managed OpenCRQ integration authenticates with the per-user
    Ed25519 JWT XTM One mints for it, whose TTL is ten minutes - far less than
    an assessment sweep or a CRQ run. Binding *refresh_fn* onto the headers
    (rather than onto each call site) is what lets :func:`_client` and
    :func:`_trpc` re-mint it mid-run: the new token is written back into THIS
    dict, so every later call on the shared instance picks it up. Passing no
    callback yields the plain-dict behaviour a static API key had.
    """
    return OpenCrqHeaders(
        {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
        refresh_fn=refresh_fn,
    )


def _client(
    headers: Any,
    timeout: int = 30,
    *,
    follow_redirects: bool = False,
) -> AutoRefreshClient | httpx.AsyncClient:
    """Open an HTTP client for one OpenCRQ call or multi-call handler.

    THE client factory for this provider - opening a bare
    ``httpx.AsyncClient`` anywhere else pins the token that existed at
    tool-build time, which is how a long run used to fail auth on every call
    once the ten-minute JWT expired and never recover.

    Headers carrying a re-minter get an :class:`AutoRefreshClient`, so a 401/403
    re-mints the JWT and retries once. Note the single-retry budget belongs to
    the client INSTANCE: a multi-call handler that shares one client across
    several ``_trpc`` calls therefore recovers reactively once, which is why
    :func:`_trpc` also refreshes proactively on every call. A static-API-key
    integration has no callback and keeps the plain short-lived client.
    """
    refresh_fn = getattr(headers, "refresh_fn", None)
    if refresh_fn is None:
        return egress_client(timeout=timeout, follow_redirects=follow_redirects)
    return AutoRefreshClient(
        headers,
        refresh_fn,
        timeout=timeout,
        follow_redirects=follow_redirects,
    )


async def _refresh_if_stale(headers: Any) -> None:
    """Proactively re-mint the JWT in *headers* when it is close to expiring.

    No-op unless *headers* carries a re-minter and its bearer token is a JWT
    inside the refresh skew, so a static API key is untouched. Concurrent
    callers coalesce onto a single re-mint.
    """
    refresh_fn = getattr(headers, "refresh_fn", None)
    if refresh_fn is not None:
        await ensure_fresh_token(headers, refresh_fn)


class TrpcError(str):
    """An error string that also carries OpenCRQ's stable machine code.

    ``reason`` is the envelope's ``error.data.reason`` (``STALE_WRITE``,
    ``ASSESSMENT_PUBLISHED``), ``None`` when OpenCRQ sent none or the failure
    never reached a tRPC envelope. A caller switches on it, never on the text.
    ``status`` is the HTTP status of the response, ``None`` when there was none
    (a network error).
    """

    reason: str | None
    status: int | None

    def __new__(
        cls, text: str, reason: str | None = None, status: int | None = None
    ) -> TrpcError:
        error = super().__new__(cls, text)
        error.reason = reason
        error.status = status
        return error


def _envelope_reason(payload: Any) -> str | None:
    error = payload.get("error") if isinstance(payload, dict) else None
    data = error.get("data") if isinstance(error, dict) else None
    reason = data.get("reason") if isinstance(data, dict) else None
    return reason if isinstance(reason, str) and reason else None


async def _refuse_unaccepted_input(headers: Any, procedure: str, input: Any, mutation: bool) -> str | None:
    """What the connected OpenCRQ's schema finds wrong with the input of this call, ``None`` when it takes it.

    Reads the manifest from the build's probe, which for an integration no
    registration owns is the ``procedureManifest`` read this first call may
    trigger: that read does not go through :func:`_trpc`, so nothing recurses.
    A build with no manifest checks nothing.
    """
    probe = getattr(headers, "manifest_probe", None)
    manifest = await probe.load() if probe is not None else None
    if manifest is None:
        return None
    refusal = InputContract(manifest).check(procedure, {} if input is None and mutation else input)
    if refusal is not None:
        logger.info("OpenCRQ call refused before it was sent: %s", refusal)
    return refusal


async def _trpc(
    base_url: str,
    headers: dict[str, str],
    procedure: str,
    action: str,
    *,
    input: Any | None = None,
    mutation: bool = False,
    tenant_id: str = "",
    client: httpx.AsyncClient | AutoRefreshClient | None = None,
) -> tuple[Any | None, str | None]:
    """Execute a single OpenCRQ tRPC procedure over HTTP.

    Queries use ``GET /trpc/{procedure}?input=<json>``; mutations use
    ``POST /trpc/{procedure}`` with the input as the JSON body.  Returns
    ``(result_data, None)`` on success or ``(None, error_string)`` on
    failure.  ``tenant_id`` is forwarded as the ``x-tenant-id`` header
    (OpenCRQ scopes every domain query by tenant).

    The short-lived platform-managed JWT is re-minted here if it is about to
    expire, so a long run keeps sending a valid token instead of waiting for
    OpenCRQ to reject an expired one. tRPC surfaces a rejected token as a real
    HTTP 401, which the :class:`AutoRefreshClient` from :func:`_client` retries
    once - there is no 200-with-auth-error case to detect, unlike OpenCTI's
    GraphQL.
    """
    # Before anything leaves, and before the re-mint below: a refused call costs no request.
    if (refusal := await _refuse_unaccepted_input(headers, procedure, input, mutation)) is not None:
        return None, TrpcError(refusal, None, None)

    # BEFORE the copy below: the re-mint updates the shared ``headers`` dict in
    # place, and ``call_headers`` must be snapshotted from the refreshed value.
    # (The copy is what drops the ``refresh_fn`` attribute, so it also has to be
    # ``headers`` - not ``call_headers`` - that every refresh path is handed.)
    await _refresh_if_stale(headers)

    call_headers = dict(headers)
    if tenant_id:
        call_headers["x-tenant-id"] = tenant_id

    url = f"{base_url}/trpc/{procedure}"
    params: dict[str, str] | None = None
    body: Any = None
    if mutation:
        body = input if input is not None else {}
    elif input is not None:
        params = {"input": json.dumps(input, default=str)}

    async with AsyncExitStack() as stack:
        c = client or await stack.enter_async_context(_client(headers))
        resp, err = await safe_request(
            c,
            "post" if mutation else "get",
            url,
            _PROVIDER,
            action,
            headers=call_headers,
            json=body if mutation else None,
            params=params,
            # The empty set returns the failed response without silencing its warning log.
            expected_statuses=set(),
        )
    if err:
        reason = None
        if resp is not None:
            with suppress(ValueError):
                reason = _envelope_reason(resp.json())
        status = resp.status_code if resp is not None else None
        return None, TrpcError(err, reason, status)
    if resp is None:
        return None, f"Error: OpenCRQ {action} returned no response."
    try:
        payload = resp.json()
    except ValueError:
        return None, f"Error: OpenCRQ {action} returned a non-JSON response."
    if isinstance(payload, dict) and "error" in payload:
        error = payload.get("error") or {}
        message = error.get("message") or json.dumps(error, default=str)[:300]
        return None, TrpcError(
            f"Error: OpenCRQ {action} failed: {message}",
            _envelope_reason(payload),
            resp.status_code,
        )
    if isinstance(payload, dict):
        return (payload.get("result") or {}).get("data"), None
    return payload, None


def _client_only_hint(err: str) -> str:
    """Append what an unmapped 500 means on a procedure OpenCRQ reserves to a user session.

    Switches on the HTTP status a ``TrpcError`` carries, so any other error (a 4xx, a
    network failure, a plain string) is returned as it came.
    """
    if getattr(err, "status", None) != 500:
        return err
    return (
        f"{err} If this connection uses a service account, note that OpenCRQ "
        "answers this action with a 500 there: it needs a user session."
    )


async def _resolve_tenant(
    base_url: str,
    headers: dict[str, str],
    tenant_id: str,
    *,
    client: httpx.AsyncClient | AutoRefreshClient | None = None,
) -> tuple[str | None, str | None]:
    """Return the effective tenant id for a call.

    When ``tenant_id`` is provided it is used as-is.  Otherwise the
    calling user's tenants are listed and the sole tenant is selected;
    with multiple tenants an explicit choice is required (the error
    message lists them so the agent can retry with ``tenant_id``).
    ``client`` lets multi-call handlers reuse one connection pool.
    """
    tenant_id = (tenant_id or "").strip()
    if tenant_id:
        return tenant_id, None
    tenants, err = await _trpc(
        base_url, headers, "tenants.getTenants", "list tenants", client=client
    )
    if err:
        return None, err
    tenants = tenants or []
    if not tenants:
        return None, "Error: No OpenCRQ tenants are accessible with this account."
    if len(tenants) == 1:
        sole_id = str(tenants[0].get("id") or "").strip()
        if not sole_id:
            # A blank id would silently drop the x-tenant-id header and make
            # every subsequent call fail confusingly - surface it instead.
            return None, (
                "Error: OpenCRQ returned a tenant without an id - cannot scope the "
                "call. Check the OpenCRQ tenants configuration."
            )
        return sole_id, None
    listing = ", ".join(
        f"{_one_line(t.get('name'), 120, '?')} (id: {t.get('id')})" for t in tenants[:20]
    )
    return None, (
        "Error: Multiple OpenCRQ tenants are accessible - specify tenant_id. "
        f"Available tenants: {listing}"
    )


async def _organization_scope(
    base_url: str,
    headers: dict[str, str],
    tenant_id: str,
    organization_id: str,
    *,
    estate: bool = False,
    client: httpx.AsyncClient | AutoRefreshClient | None = None,
) -> tuple[str | None, str | None]:
    """Name the scope a tool answered for: ``(label, None)`` or ``(None, error)``.

    With *estate*, the label says which assets were measured: OpenCRQ narrows
    an organization's findings to its mapped assets only when its estate is
    closed, and answers for the whole tenant when it is open.
    """
    if not organization_id:
        return "whole tenant", None
    try:
        uuid.UUID(organization_id)
    except ValueError:
        return None, f"organization_id must be an organization UUID, got {organization_id!r}."
    org, err = await _trpc(
        base_url, headers, "organizations.getOrganization", "get organization",
        input={"id": organization_id}, tenant_id=tenant_id, client=client,
    )
    if err:
        return None, err
    if not org:
        return None, f"Organization {organization_id} not found."
    name = org.get("name") or organization_id
    if not estate:
        return name, None
    mode = org.get("estateMode")
    if mode == "closed":
        return f"{name}, its {org.get('mappedAssetCount', 0)} mapped assets", None
    if mode == "open":
        return f"{name}, measured over the whole tenant estate", None
    return f"{name}, estate not reported by OpenCRQ", None


# ---------------------------------------------------------------------------
# Third-party text
# ---------------------------------------------------------------------------


def _one_line(value: Any, limit: int = 200, fallback: str = "") -> str:
    """A free-text value OpenCRQ answered, as one line of at most *limit* characters.

    A name, title, description or comment comes from a feed, a connector or a
    user of OpenCRQ: a line break in it could otherwise start a line that
    reads as another field of the record (``ID: ...``). *fallback* stands for
    a missing or blank value.
    """
    if value is None:
        return fallback
    return prompt_label(str(value), limit=limit, fallback=fallback)


def _untrusted(body: str, what: str) -> str:
    """*body*, records OpenCRQ answered, in a quarantine block whose source names *what*."""
    return wrap_untrusted(body, source=f"OpenCRQ {what}")


def _quarantined(header: str, body: str, what: str, *notes: str) -> str:
    """*header*, then *body* quarantined (:func:`_untrusted`), then *notes*.

    *header* and *notes* are this package's own words (counts, paging hints,
    a lookup that failed) and stay outside the block; an empty one is left out.
    """
    return "\n".join(part for part in (header, _untrusted(body, what), *notes) if part)


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _fmt_framework(
    f: dict,
    base_url: str = "",
    tenant_id: str = "",
    *,
    source: Literal["catalog", "drata"],
) -> str:
    """Format a framework of the assessment catalog or of the Drata mirror, which share no id space."""
    framework_id = f.get("id", "N/A")
    if source == "catalog":
        lines = [f"[Framework] {_one_line(f.get('title'), 200, '?')} (assessment catalog)"]
        lines.append(f"  ID: {framework_id}")
        lines.append(f"  Key: {_one_line(f.get('key'), 120)}")
        if f.get("version"):
            lines.append(f"  Version: {_one_line(f['version'], 60)}")
        return "\n".join(lines)
    lines = [f"[Framework] {_one_line(f.get('name'), 200, '?')} (Drata mirror)"]
    lines.append(f"  ID: {framework_id}")
    link = _grc_link(base_url, tenant_id, "framework", framework_id)
    if link:
        lines.append(link)
    if f.get("slug"):
        lines.append(f"  Slug: {_one_line(f['slug'], 120)}" + (f" (tag {_one_line(f['tag'], 60)})" if f.get("tag") else ""))
    if f.get("isReady") is not None:
        lines.append(f"  Ready: {'yes' if f['isReady'] else 'no'}")
    if f.get("numInScopeRequirements") is not None:
        lines.append(
            "  Requirements (Drata report): "
            f"{f['numInScopeRequirements']} in scope, "
            f"{f.get('numReadyInScopeRequirements', 0)} ready"
        )
    if f.get("derivedTotalRequirements") is not None:
        lines.append(
            "  Requirements (mirror): "
            f"{f['derivedTotalRequirements']} total, "
            f"{f.get('derivedInScopeRequirements', 0)} in scope, "
            f"{f.get('derivedReadyInScopeRequirements', 0)} ready"
        )
    if f.get("numInScopeControls") is not None:
        lines.append(f"  In-scope controls: {f['numInScopeControls']}")
    if f.get("lastReportedAt"):
        lines.append(f"  Last reported: {f['lastReportedAt']}")
    if f.get("description"):
        lines.append(f"  Description: {_one_line(f['description'], 300)}")
    return "\n".join(lines)


def _fmt_technique_overrides(edges: list[dict]) -> str | None:
    """What the tenant changed on a capability's catalogue techniques, or ``None`` when nothing."""
    overrides = [
        e for e in edges if e.get("source") == "tenant" or e.get("state") in ("added", "removed")
    ]
    if not overrides:
        return None
    shown = ", ".join(
        f"{e.get('state') or 'override'} "
        f"{(_one_line(e.get('techniqueExternalId'), 20, '?') + ' ' + _one_line(e.get('techniqueName'), 120)).strip()} "
        f"({e.get('function')})"
        for e in overrides[:15]
    )
    more = f" ... and {len(overrides) - 15} more" if len(overrides) > 15 else ""
    return f"  Tenant overrides ({len(overrides)}): {shown}{more}"


def _fmt_control(c: dict, base_url: str = "", tenant_id: str = "") -> str:
    """Format a control capability: a list row, or a detail (which adds its edges)."""
    capability_id = c.get("id", "N/A")
    ext = _one_line(c.get("externalId"), 40)
    name = _one_line(c.get("name"), 200, "?")
    lines = [f"[ControlCapability] {name} ({ext})" if ext else f"[ControlCapability] {name}"]
    lines.append(f"  ID: {capability_id}")
    link = _grc_link(base_url, tenant_id, "control", capability_id)
    if link:
        lines.append(link)
    if c.get("edges") is not None:
        functions = sorted(
            {
                e["function"]
                for e in c["edges"]
                if e.get("function") and e.get("state") != "removed"
            }
        )
        if functions:
            lines.append(f"  Functions: {', '.join(functions)}")
        overrides = _fmt_technique_overrides(c["edges"])
        if overrides:
            lines.append(overrides)
    a = c.get("assessment")
    if not a:
        lines.append("  Assessment: not assessed")
        return "\n".join(lines)
    coverage_how = ", ".join(
        part
        for part in (a.get("coverageMode"), a.get("coverageBasis"))
        if part and part != "declared"
    )
    lines.append(
        f"  Strength: {a.get('strengthRung', '?')} ({a.get('strengthMode', '?')}); "
        f"coverage: {a.get('coverageBucket', '?')} "
        f"({coverage_how or a.get('coverageMode', '?')})"
    )
    if c.get("lastAssessedAt"):
        assessor = c.get("lastAssessedBy")
        by = (
            _one_line(assessor.get("name"), 120, "a user")
            if assessor
            else _one_line(c.get("lastAssessedConnector"), 60)
        )
        lines.append(
            f"  Last assessed: {str(c['lastAssessedAt'])[:10]}"
            + (f" by {by}" if by else "")
        )
    if a.get("comment"):
        lines.append(f"  Comment: {_one_line(a['comment'], 300)}")
    return "\n".join(lines)


def _fmt_amount(value: float | None) -> str:
    """A loss figure with thousands separators, or ``not computed``."""
    return "not computed" if value is None else f"{value:,.0f}"


def _fmt_usd(value: float | None) -> str:
    """An amount OpenCRQ returns, always in US dollars, with its unit."""
    return "not computed" if value is None else f"{_fmt_amount(value)} USD"


def _fmt_rate(value: float | None) -> str:
    """A rate or a probability to three significant digits, or ``not computed``."""
    return "not computed" if value is None else f"{value:.3g}"


def _fmt_empty_page(nothing: str, page: int, total: int | None) -> str:
    """*nothing* when nothing matches, else a page past the last one."""
    if not total:
        return nothing
    return f"Page {page} is past the end: {total} in total."


#: Schema of the ``offset`` a tool takes when OpenCRQ answers a whole list it shows a page of.
OFFSET_PROP = {
    "offset": {
        "type": "integer",
        "minimum": 0,
        "default": 0,
        "description": "Index of the first item to show, to read past the first page.",
    }
}


def _page(items: list, offset: int, size: int) -> tuple[list, str]:
    """The page of *items* at *offset*, and the line saying how to reach the rest (empty when all show)."""
    page = items[offset : offset + size]
    total = len(items)
    if total and not page:
        return page, f"offset {offset} is past the end: {total} in total."
    end = offset + len(page)
    if offset == 0 and end == total:
        return page, ""
    more = f"; call again with offset={end} for the next ones" if end < total else ""
    return page, f"Showing {offset + 1}-{end} of {total}{more}."


def _fmt_risk_scenario(s: dict, base_url: str = "", tenant_id: str = "") -> str:
    """Format a risk scenario: an overview row (adds its latest run and readiness) or a detail."""
    scenario_id = s.get("id", "N/A")
    org = s.get("organization") or {}
    lines = [f"[RiskScenario] {_one_line(s.get('name'), 200, '?')}"]
    lines.append(f"  ID: {scenario_id}")
    link = _grc_scenario_link(base_url, tenant_id, org.get("id"), scenario_id)
    if link:
        lines.append(link)
    if org.get("name"):
        lines.append(f"  Organization: {_one_line(org['name'], 200)} (id: {org.get('id')})")
    outcome = s.get("attackOutcome") or {}
    if outcome.get("name"):
        lines.append(f"  Attack outcome: {_one_line(outcome['name'], 200)} (id: {outcome.get('id')})")
    owner = s.get("owner") or {}
    if owner.get("name"):
        lines.append(f"  Owner: {_one_line(owner['name'], 120)}")
    assets = s.get("primaryAssets") or []
    if assets:
        names = [_one_line(a.get("name"), 120) or str(a.get("id", "?")) for a in assets]
        lines.append(f"  Primary assets: {', '.join(names)}")
    categories = s.get("businessValueCategories") or []
    if categories:
        lines.append(
            "  Business value categories: "
            + ", ".join(_one_line(c.get("name"), 120) or _one_line(c.get("code"), 60, "?") for c in categories)
        )
    if s.get("description"):
        lines.append(f"  Description: {_one_line(s['description'], 300)}")
    if "latestRun" in s:
        run = s["latestRun"]
        if not run:
            lines.append("  Latest run: none")
        else:
            lines.append(
                f"  Latest run: ALE mean {_fmt_amount(run.get('aleMean'))}"
                + (
                    f" (P10 {_fmt_amount(run.get('aleP10'))} / "
                    f"P90 {_fmt_amount(run.get('aleP90'))})"
                    if run.get("aleMean") is not None
                    else ""
                )
                + (
                    f", LEF mean {_fmt_rate(run['lefMean'])}"
                    if run.get("lefMean") is not None
                    else ""
                )
                + f", {run.get('coveredTechniqueCount', 0)} of "
                f"{run.get('techniqueCount', 0)} techniques covered"
            )
    if s.get("noMappedAsset"):
        lines.append("  Estate: no asset of the risk scope is mapped to this scenario")
    readiness = s.get("readiness")
    if readiness:
        detail = "; ".join(
            f"{label} {', '.join(readiness[key])}"
            for key, label in (("missing", "missing"), ("outdated", "outdated"))
            if readiness.get(key)
        )
        lines.append(
            f"  Readiness: {readiness.get('status', 'unknown')}"
            + (f" ({detail})" if detail else "")
        )
    return "\n".join(lines)


def _fmt_actor(person: dict | None, service_account: dict | None) -> str:
    if person:
        return _one_line(person.get("name"), 120) or _one_line(person.get("email"), 120)
    if service_account:
        return f"{_one_line(service_account.get('name'), 120, 'unnamed')} (service account)"
    return ""


def _answer_summary(a: dict) -> dict | None:
    """The answer summary of a list row, or one computed from a detail's nodes and answers."""
    summary = a.get("maturity")
    if isinstance(summary, dict):
        return summary
    nodes, answers = a.get("nodes"), a.get("answers")
    if not isinstance(nodes, list) or not isinstance(answers, list):
        return None
    requirement_ids = {n.get("id") for n in nodes if n.get("isRequirement")}
    distribution = [0] * 6
    not_applicable = 0
    level_sum = 0
    for answer in answers:
        if answer.get("requirementId") not in requirement_ids:
            continue
        level = answer.get("level")
        if answer.get("notApplicable"):
            not_applicable += 1
        elif isinstance(level, int) and 0 <= level <= 5:
            distribution[level] += 1
            level_sum += level
    answered = sum(distribution)
    return {
        "requirementCount": len(requirement_ids),
        "answeredCount": answered,
        "notApplicableCount": not_applicable,
        "meanLevel": level_sum / answered if answered else None,
        "distribution": distribution,
    }


def _fmt_assessment(a: dict) -> str:
    lines = [f"[ComplianceAssessment] {_one_line(a.get('name'), 200, '?')}"]
    lines.append(f"  ID: {a.get('id', 'N/A')}")
    if a.get("status"):
        lines.append(f"  Status: {a['status']}")
    if a.get("frameworkTitle"):
        lines.append(
            f"  Framework: {_one_line(a['frameworkTitle'], 200)} (id: {a.get('frameworkId')})"
        )
    summary = _answer_summary(a)
    if summary:
        lines.append(
            f"  Answered on a level: {summary.get('answeredCount', 0)} of "
            f"{summary.get('requirementCount', 0)} requirements, "
            f"{summary.get('notApplicableCount', 0)} not applicable"
        )
        mean = summary.get("meanLevel")
        if mean is not None:
            lines.append(f"  Mean level (0-5): {mean:.2f}")
        distribution = summary.get("distribution")
        if distribution:
            lines.append(
                "  Requirements per level 0 to 5: "
                + ", ".join(str(n) for n in distribution)
            )
    if a.get("createdAt"):
        author = _fmt_actor(a.get("createdBy"), a.get("createdByServiceAccount"))
        lines.append(
            f"  Created: {str(a['createdAt'])[:10]}" + (f" by {author}" if author else "")
        )
    if a.get("publishedAt"):
        publisher = _fmt_actor(
            a.get("publishedBy"), a.get("publishedByServiceAccount")
        )
        lines.append(
            f"  Published: {str(a['publishedAt'])[:10]}"
            + (f" by {publisher}" if publisher else "")
        )
    if a.get("updatedAt"):
        lines.append(f"  Updated: {str(a['updatedAt'])[:10]}")
    return "\n".join(lines)


def _fmt_supporting_asset(a: dict, base_url: str = "", tenant_id: str = "") -> str:
    """Format a supporting asset: a list row, or a detail (which adds its properties)."""
    asset_id = a.get("id", "N/A")
    name = _one_line(a.get("name"), 200) or _one_line(a.get("derivedName"), 200, "unnamed")
    lines = [f"[SupportingAsset] {name}"]
    lines.append(f"  ID: {asset_id}")
    link = _grc_link(base_url, tenant_id, "supporting_asset", asset_id)
    if link:
        lines.append(link)
    for key, label in (
        ("categoryId", "Category"),
        ("subcategoryId", "Subcategory"),
        ("criticality", "Criticality"),
        ("exposure", "Exposure"),
        ("description", "Description"),
        ("url", "URL"),
        ("linkedPerson", "Linked person"),
    ):
        if a.get(key):
            lines.append(f"  {label}: {_one_line(a[key], 300)}")
    owner = a.get("owner") or {}
    if owner.get("name"):
        lines.append(f"  Owner: {_one_line(owner['name'], 120)}")
    tags = [
        f"{_one_line(t['name'], 80)} (hand-set)" if t.get("isManual") else _one_line(t["name"], 80)
        for t in a.get("tags") or []
        if t.get("name")
    ]
    if tags:
        lines.append(f"  Tags: {', '.join(tags)}")
    groups = [_one_line(g["name"], 80) for g in a.get("groups") or [] if g.get("name")]
    if groups:
        lines.append(f"  Groups: {', '.join(groups)}")
    if a.get("lastSeenAt"):
        lines.append(f"  Last seen: {str(a['lastSeenAt'])[:10]}")
    sources = sorted({_one_line(s["source"], 60) for s in a.get("sources") or [] if s.get("source")})
    if sources:
        lines.append(f"  Sources: {', '.join(sources)}")
    if a.get("applicableEvidenceCount") is not None:
        lines.append(f"  Applicable findings: {a['applicableEvidenceCount']}")
    properties = a.get("properties") or {}
    details = [
        f"{_one_line(key, 60)} {_one_line(value, 160)}"
        for key, value in properties.items()
        if key != "family"
        and value is not None
        and value != ""
        and not isinstance(value, (list, dict))
    ]
    if details:
        lines.append(
            f"  Properties ({_one_line(properties.get('family'), 40, 'unknown')}): "
            + ", ".join(details[:12])
        )
    return "\n".join(lines)


def _fmt_cve(c: dict, base_url: str = "", tenant_id: str = "") -> str:
    cve_name = c.get("cveId")
    lines = [f"[CVE] {_one_line(cve_name, 40, '?')}"]
    if c.get("id"):
        lines.append(f"  ID: {c['id']}")
    link = _grc_link(base_url, tenant_id, "cve", cve_name)
    if link:
        lines.append(link)
    if c.get("cveTitle"):
        lines.append(f"  Title: {_one_line(c['cveTitle'], 200)}")
    for key, label in (
        ("cvss", "CVSS"),
        ("epss", "EPSS"),
        ("kev", "CISA KEV"),
        ("cveDescription", "Description"),
    ):
        value = c.get(key)
        if value is None or value == "":
            continue
        if key == "kev":
            value = "yes" if value else "no"
        elif key == "cveDescription":
            value = _one_line(value, 300)
        lines.append(f"  {label}: {value}")
    return "\n".join(lines)


__all__ = [
    "ASSET_CATEGORIES",
    "ASSET_CRITICALITIES",
    "ASSET_EXPOSURES",
    "ASSET_SUBCATEGORIES",
    "COVERAGE_BUCKETS",
    "CVE_ID_PATTERN",
    "EVIDENCE_STATES",
    "JOB_STATUSES",
    "OFFSET_PROP",
    "ORGANIZATION_SCOPE_PROP",
    "STRENGTH_RUNGS",
    "TENANT_PROP",
    "_GRC_UI_PATHS",
    "_PROVIDER",
    "_UUID",
    "TrpcError",
    "_as_int",
    "_clean_ids",
    "_client",
    "_fmt_amount",
    "_fmt_assessment",
    "_fmt_control",
    "_fmt_cve",
    "_fmt_empty_page",
    "_fmt_framework",
    "_fmt_rate",
    "_fmt_risk_scenario",
    "_fmt_share",
    "_fmt_supporting_asset",
    "_fmt_usd",
    "_grc_link",
    "_grc_primary_asset_link",
    "_grc_scenario_link",
    "_headers",
    "_one_line",
    "_organization_scope",
    "_page",
    "_quarantined",
    "_refresh_if_stale",
    "_resolve_tenant",
    "_scoped",
    "_text",
    "_trpc",
    "_untrusted",
    "logger",
]
