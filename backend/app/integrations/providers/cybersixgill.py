# SPDX-License-Identifier: Apache-2.0

"""Cybersixgill (a Bitsight company) integration - deep and dark web threat intelligence.

Full-fledged Cybersixgill integration exposing the vendor's real-time deep and
dark web intelligence to an agentic SOC / CTI team. Cybersixgill continuously
collects intelligence from underground forums, marketplaces, paste sites,
messaging platforms (Telegram) and other closed sources, then correlates it
into IOC enrichment, the Dynamic Vulnerability Exploit (DVE) score, dark web
search, and per-organization actionable alerts.

Tool surface (this module):
  - Vulnerability intelligence (DVE): enrich a CVE with the Dynamic
    Vulnerability Exploit score + underground context (``enrich_cybersixgill_cve``)
    and pull remediation guidance (``get_cybersixgill_cve_remediation``).
  - IOC enrichment: enrich an IP / domain / URL / file hash with underground
    context (``enrich_cybersixgill_ioc``), and pivot on the actor
    (``enrich_cybersixgill_actor``) or source post
    (``enrich_cybersixgill_post``) surfaced by an enrichment.
  - Dark web search: a simple full-text query over collected intel items
    (``search_cybersixgill_intel_items``), an advanced query with filters /
    date range / sort / scroll pagination
    (``search_cybersixgill_intel_items_advanced``), and the scroll continuation
    (``get_cybersixgill_intel_items_next``).
  - Actionable alerts: list per-organization alerts with rich filters
    (``list_cybersixgill_alerts``), read one (``get_cybersixgill_alert``) plus
    its matched underground content (``get_cybersixgill_alert_content``),
    triage one (``update_cybersixgill_alert`` - status / read / threat level),
    and permanently delete alerts (``delete_cybersixgill_alert``).
  - Reports: list finished-intelligence reports (``list_cybersixgill_reports``),
    read one (``get_cybersixgill_report``), and store its rendered HTML as a
    file of the run (``get_cybersixgill_report_html``).
  - Organization assets: read the monitored asset set
    (``get_cybersixgill_organization_assets``) and add/replace it
    (``manage_cybersixgill_organization_assets``) - the assets that drive
    actionable-alert matching.

Authentication
--------------
Cybersixgill authenticates with an **OAuth2 client-credentials** grant. The
customer generates an API client ID + client secret in the Cybersixgill /
Bitsight developer portal (https://developer.cybersixgill.com). The compound
``base_url||client_id||organization_id||client_secret`` token is assembled by
``integration_tools._get_token`` (client secret in ``api_key_enc``; base URL,
client ID and optional MSSP organization ID in ``extra_config``). The client
secret is the FINAL, greedy segment so a secret containing ``||`` still
round-trips. The credentials are exchanged at ``POST {base_url}/auth/token``
for a short-lived bearer access token, cached in-process (keyed by a hash of
the credentials) so a multi-tool agentic run reuses one token instead of
re-minting on every call (mirrors the Picus / ThreatQ app-token cache).

Every request also carries the ``X-Channel-Id`` header identifying the
consuming application (the fixed ``DEFAULT_CHANNEL_ID`` constant - Filigran's
registered channel, shared with the OpenCTI Cybersixgill connector).

The stored ``base_url`` is the API host (``https://api.cybersixgill.com``), NOT
the investigative portal UI host, so it is intentionally excluded from
``_USER_FACING_BASE_URL_PROVIDERS``. Portal deep links are built from the fixed
public ``PORTAL_BASE_URL`` constant instead.

Exports ``cybersixgill_api_base()`` and ``validate_connection()`` shared with
the connectivity test.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.credential_exposure import scrub_exposure_text
from app.integrations.file_delivery import PersistFileFn, deliver_file, fetch_file
from app.integrations.http import (
    app_token_cache_key,
    format_entity_list,
    get_cached_app_token,
    parse_compound_token,
    safe_request,
    set_cached_app_token,
)
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_PROVIDER = "Cybersixgill"
_SOURCE = "integration:cybersixgill"
_DEFAULT_TIMEOUT = 30
# A report's rendered HTML stored as a file of the run.
_MAX_REPORT_BYTES = 25 * 1024 * 1024
_DEFAULT_BASE_URL = "https://api.cybersixgill.com"

# The investigative portal UI host (fixed, public) used to build deep links.
# This is a constant vendor URL, never a stored/interpolated base_url, so it is
# safe to surface directly in agent output.
PORTAL_BASE_URL = "https://portal.cybersixgill.com"

# ``X-Channel-Id`` identifies the consuming application to Cybersixgill. This
# is Filigran's registered channel (shared with the OpenCTI Cybersixgill
# connector) and is sent as a fixed constant on every request.
DEFAULT_CHANNEL_ID = "a7405f61ea64113b9de457dc952bd133"

# The IOC types Cybersixgill accepts on the ``ioc/enrich`` endpoint.
_IOC_TYPES: frozenset[str] = frozenset({"ip", "domain", "url", "hash"})

# CVE identifiers are interpolated into the ``/dve_enrich/{cve_id}`` URL path,
# so they are validated against the official format first (CVE-YYYY-NNNN...).
_CVE_RE = re.compile(r"^CVE-\d{4}-\d{4,}$")

# Actionable-alert filter/update enums (from the vendor API).
_ALERT_THREAT_LEVELS: frozenset[str] = frozenset({"imminent", "emerging"})
_ALERT_STATUSES: frozenset[str] = frozenset(
    {"treatment_required", "in_treatment", "resolved"}
)
_ALERT_READ_STATES: frozenset[str] = frozenset({"read", "unread"})
# manage_cybersixgill_organization_assets actions: add (POST) / replace (PUT).
_ORG_ASSET_ACTIONS: frozenset[str] = frozenset({"add", "replace"})

_AUTH_ERROR = (
    "Error: Failed to authenticate with Cybersixgill. Check that the API "
    "client ID and client secret are correct and active (generate them in the "
    "Cybersixgill / Bitsight developer portal at "
    "https://developer.cybersixgill.com), and that the client's permission "
    "scopes cover this operation (Darkfeed Enrichment for IOC/DVE enrichment "
    "and intel search; Actionable Alerts for the alert, report, and "
    "organization-asset tools)."
)


# ---------------------------------------------------------------------------
# Base URL + auth helpers (shared with integration_testing / integrations.py)
# ---------------------------------------------------------------------------


def cybersixgill_api_base(base_url: str) -> str:
    """Normalise a Cybersixgill API base URL.

    An empty value falls back to the SaaS API host. A raw ``http://`` URL is
    upgraded to ``https://`` (the token exchange carries the client secret and
    must never travel in cleartext). The trailing slash is stripped.
    """
    value = (base_url or "").strip()
    if not value:
        return _DEFAULT_BASE_URL
    lowered = value.lower()
    if lowered.startswith("http://"):
        logger.warning(
            "Cybersixgill base URL was supplied over http://; upgrading to "
            "https:// so the client secret is not sent in cleartext."
        )
        return ("https://" + value[len("http://") :]).rstrip("/")
    if not lowered.startswith("https://"):
        value = "https://" + value
    return value.rstrip("/")


async def _request_access_token(
    base_url: str, client_id: str, client_secret: str
) -> tuple[str | None, int]:
    """Exchange client credentials for a short-lived bearer access token.

    Returns ``(access_token, expires_in_seconds)`` on success, or ``(None, 0)``
    on failure. The token endpoint takes no prior authentication.
    """
    if not (client_id and client_secret):
        return None, 0
    url = f"{base_url}/auth/token"
    try:
        async with egress_client(timeout=20) as client:
            resp = await client.post(
                url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "X-Channel-Id": DEFAULT_CHANNEL_ID,
                    "Cache-Control": "no-cache",
                },
            )
            if resp.is_success:
                try:
                    data = resp.json()
                except ValueError:
                    logger.warning(
                        "Cybersixgill token response was not valid JSON "
                        "(status %s)",
                        resp.status_code,
                    )
                    return None, 0
                if not isinstance(data, dict):
                    return None, 0
                token = data.get("access_token")
                if not token or not isinstance(token, str):
                    logger.warning("Cybersixgill token response contained no token")
                    return None, 0
                # ``expires_in`` is in seconds when present. Default to a
                # conservative 25 minutes so a token is never cached past its
                # true lifetime; the shared cache applies its own expiry skew.
                expires_in = data.get("expires_in")
                ttl = expires_in if isinstance(expires_in, int) and expires_in > 0 else 1500
                return token, ttl
            logger.warning(
                "Cybersixgill token request failed (%s): %s",
                resp.status_code,
                resp.text[:300],
            )
    except httpx.HTTPError as exc:
        logger.warning("Cybersixgill token request error: %s", format_exception_message(exc))
    return None, 0


async def validate_connection(
    base_url: str, client_id: str, client_secret: str
) -> tuple[bool, str]:
    """Verify Cybersixgill credentials via a client-credentials token exchange.

    Returns ``(ok, message)``. A successful token exchange confirms the client
    ID and secret are valid. Used by the integration connectivity test.
    """
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    if not (client_id and client_secret):
        return False, (
            "Missing API client ID or client secret. Enter the client ID in "
            "extra config and the client secret in the API Key field."
        )
    resolved = cybersixgill_api_base(base_url)
    token, _ = await _request_access_token(resolved, client_id, client_secret)
    if token:
        return True, "Cybersixgill is connected and authenticated."
    return False, (
        "Authentication failed - could not obtain a Cybersixgill access token. "
        "Check the API client ID and client secret (and the base URL if you "
        "overrode the default)."
    )


# ---------------------------------------------------------------------------
# Integration class
# ---------------------------------------------------------------------------


class CybersixgillIntegration(BaseIntegration):
    """Cybersixgill deep and dark web threat intelligence integration."""

    provider_name = "cybersixgill"
    display_name = "Cybersixgill"
    icon = "Radar"
    description = (
        "Connect Cybersixgill (a Bitsight company) for deep and dark web threat "
        "intelligence - CVE Dynamic Vulnerability Exploit (DVE) scoring, IOC "
        "enrichment, dark web search, and actionable alerts."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        base_url, client_id, _org, client_secret = parse_compound_token(
            token, 4, greedy_last=True
        )
        ok, _ = await validate_connection(base_url, client_id, client_secret)
        return ok

    async def get_user_info(self, token: str) -> dict:
        # Cybersixgill exposes no whoami endpoint; a successful token exchange
        # is the identity signal. Return the (non-secret) client ID so the UI
        # can display which API client is connected.
        _base, client_id, _org, _secret = parse_compound_token(
            token, 4, greedy_last=True
        )
        return {"client_id": client_id} if client_id else {}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text)
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _clamp(value: Any, default: int, lo: int = 1, hi: int = 200) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(lo, min(value, hi))


def _cve_portal_link(cve_id: str) -> str:
    return f"{PORTAL_BASE_URL}/#/cve/{cve_id}"


def _alert_portal_link(alert_id: str, organization_id: str = "") -> str:
    link = f"{PORTAL_BASE_URL}/#/?actionable_alert={alert_id}"
    if organization_id:
        link += f"&org={organization_id}"
    return link


def _to_int(value: Any, default: int = -1) -> int:
    """Coerce a tool argument to int, falling back to ``default`` on garbage."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_json_arg(value: Any) -> Any:
    """Accept a dict/list as-is, or parse a JSON string into one."""
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip():
        import json

        try:
            return json.loads(value)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    persist_file_fn: PersistFileFn | None = None,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Cybersixgill tools from a compound credential token.

    ``token`` is ``base_url||client_id||organization_id||client_secret`` (the
    client secret is the final, greedy segment). Missing credentials still
    build the full tool surface - ``integration_tools._get_token`` returns a
    partial compound token so a misconfiguration surfaces as a clear call-time
    error instead of the tools silently disappearing (mirrors picus / threatq).
    ``persist_file_fn`` (injected by ``integration_tools``) stores a report's
    HTML as a file of the run.
    """
    raw_base_url, client_id, organization_id, client_secret = (
        part.strip() for part in parse_compound_token(token, 4, greedy_last=True)
    )
    base_url = cybersixgill_api_base(raw_base_url)
    cache_key = app_token_cache_key(
        "cybersixgill", base_url, client_id, client_secret
    )

    def _creds_error() -> str | None:
        if not (client_id and client_secret):
            return (
                "Error: Cybersixgill credentials are not configured. Enter the "
                "API client ID in extra config and the client secret in the "
                "API Key field."
            )
        return None

    async def _token() -> str | None:
        cached = get_cached_app_token(cache_key)
        if cached:
            return cached
        access, ttl = await _request_access_token(base_url, client_id, client_secret)
        if access:
            set_cached_app_token(cache_key, access, ttl)
        return access

    async def _request(
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> tuple[httpx.Response | None, str | None]:
        """Authenticated ``safe_request`` against ``{base_url}{path}``."""
        creds_err = _creds_error()
        if creds_err:
            return None, creds_err
        access = await _token()
        if not access:
            return None, _AUTH_ERROR
        headers = {
            "Authorization": f"Bearer {access}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Channel-Id": DEFAULT_CHANNEL_ID,
            "Cache-Control": "no-cache",
        }
        async with egress_client(timeout=timeout) as client:
            return await safe_request(
                client,
                method,
                f"{base_url}{path}",
                _PROVIDER,
                action,
                headers=headers,
                params=params,
                json=json_body,
            )

    def _org_params(extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Seed a params dict with the MSSP organization ID when configured."""
        params: dict[str, Any] = dict(extra or {})
        if organization_id:
            params["organization_id"] = organization_id
        return params

    # =====================================================================
    # 1. VULNERABILITY INTELLIGENCE - Dynamic Vulnerability Exploit (DVE)
    # =====================================================================

    async def enrich_cybersixgill_cve(cve_id: str) -> str:
        """Enrich a CVE with the Cybersixgill Dynamic Vulnerability Exploit (DVE) score.

        Use this to prioritise vulnerabilities by their REAL-WORLD exploitation
        likelihood from underground chatter (not just CVSS). Returns the current
        DVE score (0-10), the highest DVE score ever recorded, attributes
        (e.g. is-trending, has-PoC-exploit, exploited-in-the-wild), and NVD
        context. ``cve_id`` is a single CVE identifier, e.g. "CVE-2021-44228".
        """
        cve = (cve_id or "").strip().upper()
        if not _CVE_RE.match(cve):
            return (
                "Error: cve_id must be a CVE identifier in the form "
                "CVE-YYYY-NNNN (e.g. CVE-2021-44228)."
            )
        resp, err = await _request(
            "get", f"/dve_enrich/{cve}", f"enrich CVE {cve}"
        )
        if err:
            return err
        data = resp.json() if resp is not None else {}
        if not isinstance(data, dict) or not data:
            return f"No Cybersixgill DVE intelligence found for {cve}."
        lines = [f"Cybersixgill DVE intelligence for {cve}:"]
        score = data.get("score") or data.get("current_score") or {}
        if isinstance(score, dict):
            lines.append(
                f"  DVE score: {score.get('value', 'N/A')} "
                f"(as of {score.get('date', 'N/A')})"
            )
        elif score not in (None, ""):
            lines.append(f"  DVE score: {score}")
        highest = data.get("score_highest_ever") or {}
        if isinstance(highest, dict) and highest:
            lines.append(
                f"  Highest DVE score ever: {highest.get('value', 'N/A')} "
                f"(on {highest.get('date', 'N/A')})"
            )
        rating = data.get("rating") or data.get("severity")
        if rating:
            lines.append(f"  Rating: {rating}")
        attributes = data.get("attributes")
        if isinstance(attributes, list) and attributes:
            active = [
                a.get("description") or a.get("name")
                for a in attributes
                if isinstance(a, dict) and a.get("value")
            ]
            if active:
                lines.append("  Attributes: " + ", ".join(str(a) for a in active))
        description = data.get("description") or data.get("Description")
        if description:
            lines.append(f"  Description: {_trunc(description, 500)}")
        lines.append(f"  Portal: {_cve_portal_link(cve)}")
        return "\n".join(lines)

    async def get_cybersixgill_cve_remediation(cve_id: str) -> str:
        """Get Cybersixgill remediation guidance for a CVE.

        Returns the recommended remediation / mitigation steps Cybersixgill
        publishes for the vulnerability. Pair with ``enrich_cybersixgill_cve``
        (which returns the DVE exploitation score) to both prioritise and act.
        ``cve_id`` is a single CVE identifier, e.g. "CVE-2021-44228".
        """
        cve = (cve_id or "").strip().upper()
        if not _CVE_RE.match(cve):
            return (
                "Error: cve_id must be a CVE identifier in the form "
                "CVE-YYYY-NNNN (e.g. CVE-2021-44228)."
            )
        resp, err = await _request(
            "get", f"/dve_enrich/{cve}/remediation", f"get remediation for {cve}"
        )
        if err:
            return err
        data = resp.json() if resp is not None else {}
        if not data:
            return f"No Cybersixgill remediation guidance found for {cve}."
        lines = [f"Cybersixgill remediation guidance for {cve}:"]
        if isinstance(data, dict):
            solutions = data.get("Solutions") or data.get("solutions") or []
            description = data.get("Description") or data.get("description")
            if description:
                lines.append(f"  {_trunc(description, 500)}")
            if isinstance(solutions, list) and solutions:
                lines.append("  Solutions:")
                lines.extend(f"    - {_trunc(s, 400)}" for s in solutions)
            elif isinstance(solutions, str) and solutions:
                lines.append(f"  Solutions: {_trunc(solutions, 500)}")
        elif isinstance(data, list):
            lines.extend(f"  - {_trunc(item, 400)}" for item in data)
        lines.append(f"  Portal: {_cve_portal_link(cve)}")
        return "\n".join(lines)

    # =====================================================================
    # 2. IOC ENRICHMENT
    # =====================================================================

    def _format_ioc_items(items: list[dict[str, Any]], total: int, skip: int) -> str:
        def _fmt(item: dict[str, Any]) -> str:
            pattern = item.get("pattern") or item.get("value") or ""
            labels = item.get("labels") or item.get("x_sixgill_feedname") or []
            if isinstance(labels, list):
                labels = ", ".join(str(label) for label in labels)
            created = item.get("created") or item.get("valid_from") or ""
            desc = item.get("description") or ""
            line = f"  - {_trunc(pattern, 200)}"
            meta: list[str] = []
            if labels:
                meta.append(f"labels: {labels}")
            if created:
                meta.append(f"created: {created}")
            if meta:
                line += "\n    " + " | ".join(meta)
            if desc:
                line += f"\n    {_trunc(desc, 200)}"
            return line

        out = format_entity_list(
            items, _fmt, noun="enrichment item", total=total or len(items)
        )
        if total and skip + len(items) < total:
            out += (
                f"\n[More results available - pass skip={skip + len(items)} "
                f"({total} total)]"
            )
        return out

    async def enrich_cybersixgill_ioc(
        ioc_type: str, ioc_value: str, skip: int = 0
    ) -> str:
        """Enrich an IOC (IP, domain, URL, or file hash) with underground context.

        Returns Cybersixgill Darkfeed enrichment items for the observable:
        the associated actor(s), source post(s), threat labels, and STIX
        pattern(s) tying the IOC to underground activity. Only IOCs Cybersixgill
        has seen return items. After an enrichment, pivot with
        ``enrich_cybersixgill_actor`` (on a surfaced actor name) or
        ``enrich_cybersixgill_post`` (on a surfaced post ID) to widen the
        investigation. ``ioc_type`` is one of ip, domain, url, hash.
        ``skip`` pages through large result sets.
        """
        ioc_type = (ioc_type or "").strip().lower()
        ioc_value = (ioc_value or "").strip()
        if ioc_type not in _IOC_TYPES:
            return (
                f"Error: ioc_type must be one of {', '.join(sorted(_IOC_TYPES))} "
                f"(got '{ioc_type}')."
            )
        if not ioc_value:
            return "Error: ioc_value is required."
        skip = max(int(skip) if str(skip).lstrip('-').isdigit() else 0, 0)
        resp, err = await _request(
            "post",
            "/ioc/enrich",
            f"enrich {ioc_type} IOC",
            json_body={"ioc_type": ioc_type, "ioc_value": ioc_value, "skip": skip},
        )
        if err:
            return err
        data = resp.json() if resp is not None else {}
        items = data.get("items", []) if isinstance(data, dict) else []
        total = data.get("total", len(items)) if isinstance(data, dict) else len(items)
        if not items:
            return (
                f"No Cybersixgill underground intelligence found for "
                f"{ioc_type} '{ioc_value}'."
            )
        header = f"Cybersixgill enrichment for {ioc_type} '{ioc_value}':\n"
        return header + _format_ioc_items(items, total, skip)

    async def enrich_cybersixgill_actor(actor_name: str, skip: int = 0) -> str:
        """Enrich a threat actor with the intelligence items they are tied to.

        Given an actor name (obtain one from ``enrich_cybersixgill_ioc``),
        returns the other IOC patterns, posts, and intelligence associated with
        that actor - the pivot that widens an investigation from one IOC to an
        actor's wider infrastructure. ``skip`` pages through large result sets.
        """
        actor = (actor_name or "").strip()
        if not actor:
            return "Error: actor_name is required."
        skip = max(int(skip) if str(skip).lstrip('-').isdigit() else 0, 0)
        resp, err = await _request(
            "post",
            "/ioc/enrich",
            "enrich actor",
            json_body={
                "sixgill_field": "actor",
                "sixgill_field_value": actor,
                "skip": skip,
            },
        )
        if err:
            return err
        data = resp.json() if resp is not None else {}
        items = data.get("items", []) if isinstance(data, dict) else []
        total = data.get("total", len(items)) if isinstance(data, dict) else len(items)
        if not items:
            return f"No Cybersixgill intelligence found for actor '{actor}'."
        header = f"Cybersixgill intelligence for actor '{actor}':\n"
        return header + _format_ioc_items(items, total, skip)

    async def enrich_cybersixgill_post(post_id: str, skip: int = 0) -> str:
        """Enrich a source post with the intelligence items it produced.

        Given a Cybersixgill post ID (obtain one from
        ``enrich_cybersixgill_ioc``), returns the IOC patterns and intelligence
        extracted from that underground post - the pivot from a single IOC back
        to its originating post and the other observables it yielded. ``skip``
        pages through large result sets.
        """
        post = (post_id or "").strip()
        if not post:
            return "Error: post_id is required."
        skip = max(int(skip) if str(skip).lstrip('-').isdigit() else 0, 0)
        resp, err = await _request(
            "post",
            "/ioc/enrich",
            "enrich post",
            json_body={
                "sixgill_field": "post_id",
                "sixgill_field_value": post,
                "skip": skip,
            },
        )
        if err:
            return err
        data = resp.json() if resp is not None else {}
        items = data.get("items", []) if isinstance(data, dict) else []
        total = data.get("total", len(items)) if isinstance(data, dict) else len(items)
        if not items:
            return f"No Cybersixgill intelligence found for post '{post}'."
        header = f"Cybersixgill intelligence for post '{post}':\n"
        return header + _format_ioc_items(items, total, skip)

    # =====================================================================
    # 3. DARK WEB SEARCH - intel items
    # =====================================================================

    def _format_intel_items(
        items: list[dict[str, Any]], scroll_id: str | None = None
    ) -> str:
        def _fmt(item: dict[str, Any]) -> str:
            title = item.get("title") or item.get("post_title") or "Untitled"
            site = item.get("site") or item.get("source") or "?"
            creator = item.get("creator") or item.get("actor") or "?"
            date = item.get("date") or item.get("collection_date") or ""
            item_id = item.get("id") or item.get("_id") or ""
            content = item.get("content") or ""
            line = (
                f"  - {_trunc(title, 160)}\n"
                f"    Site: {site} | Actor: {creator}"
                + (f" | {date}" if date else "")
            )
            if item_id:
                line += f" | post_id: {item_id}"
            if content:
                line += f"\n    {_trunc(content, 240)}"
            return line

        out = format_entity_list(items, _fmt, noun="intel item")
        if scroll_id:
            out += (
                f"\n[More results available - pass scroll_id=\"{scroll_id}\" to "
                f"get_cybersixgill_intel_items_next]"
            )
        return out

    async def search_cybersixgill_intel_items(
        query: str,
        results_size: int = 25,
        recent_items: bool = False,
        highlight: bool = False,
    ) -> str:
        """Search collected dark/deep web intel items with a full-text query.

        Runs a simple full-text search across Cybersixgill's collected
        underground content (forum/marketplace/paste/Telegram posts). Use this
        for a quick lookup by keyword, brand, domain, actor handle, or leaked
        artifact. For structured filtering (date range, site, sort, deep
        pagination) use ``search_cybersixgill_intel_items_advanced`` instead.
        Returns matching posts with title, site, actor, date, and a post_id you
        can feed to ``enrich_cybersixgill_post``. Set ``recent_items`` to limit
        to the last ~2 days.
        """
        query = (query or "").strip()
        if not query:
            return "Error: query is required."
        params: dict[str, Any] = {
            "query": query,
            "results_size": _clamp(results_size, 25),
        }
        if recent_items:
            params["recent_items"] = True
        if highlight:
            params["highlight"] = True
        resp, err = await _request(
            "get", "/intel/intel_items", "search intel items", params=params
        )
        if err:
            return err
        data = resp.json() if resp is not None else {}
        items = data.get("intel_items", []) if isinstance(data, dict) else []
        if not items:
            return f"No Cybersixgill intel items found for query '{query}'."
        return (
            f"Cybersixgill intel items for '{query}':\n"
            + _format_intel_items(items)
        )

    async def search_cybersixgill_intel_items_advanced(
        query: str,
        date_range_start: str = "",
        date_range_end: str = "",
        sort: str = "",
        sort_type: str = "desc",
        results_size: int = 25,
        recent_items: bool = False,
        highlight: bool = False,
    ) -> str:
        """Search dark/deep web intel items with filters, a date range, and scroll paging.

        The advanced counterpart to ``search_cybersixgill_intel_items``: same
        underground corpus, but with structured controls. ``query`` accepts
        Cybersixgill's query language. Scope collection time with
        ``date_range_start`` / ``date_range_end`` (ISO-8601 UTC, e.g.
        "2026-01-01T00:00:00"). Order with ``sort`` (a field name, e.g. "date")
        and ``sort_type`` (asc / desc). When more results exist, a scroll_id is
        returned - pass it to ``get_cybersixgill_intel_items_next`` to page on.
        """
        query = (query or "").strip()
        if not query:
            return "Error: query is required."
        body: dict[str, Any] = {
            "query": query,
            "results_size": _clamp(results_size, 25),
            "partial_content": True,
            "scroll": True,
        }
        if date_range_start or date_range_end:
            date_range: dict[str, str] = {}
            if date_range_start:
                date_range["start"] = date_range_start.strip()
            if date_range_end:
                date_range["end"] = date_range_end.strip()
            body["date_range"] = date_range
        if sort:
            body["sort"] = sort.strip()
            body["sort_type"] = (sort_type or "desc").strip().lower()
        if recent_items:
            body["recent_items"] = True
        if highlight:
            body["highlight"] = True
        resp, err = await _request(
            "post",
            "/intel/intel_items",
            "advanced search intel items",
            json_body=body,
        )
        if err:
            return err
        data = resp.json() if resp is not None else {}
        items = data.get("intel_items", []) if isinstance(data, dict) else []
        scroll_id = data.get("scroll_id") if isinstance(data, dict) else None
        if not items:
            return f"No Cybersixgill intel items found for query '{query}'."
        return (
            f"Cybersixgill intel items for '{query}':\n"
            + _format_intel_items(items, scroll_id)
        )

    async def get_cybersixgill_intel_items_next(
        scroll_id: str, recent_items: bool = False
    ) -> str:
        """Fetch the next page of an advanced intel-item search.

        Continues a ``search_cybersixgill_intel_items_advanced`` result set
        using the ``scroll_id`` it returned. Returns the next batch of posts
        plus a fresh scroll_id while more results remain.
        """
        scroll_id = (scroll_id or "").strip()
        if not scroll_id:
            return (
                "Error: scroll_id is required (obtain it from "
                "search_cybersixgill_intel_items_advanced)."
            )
        body: dict[str, Any] = {"scroll_id": scroll_id}
        if recent_items:
            body["recent_items"] = True
        resp, err = await _request(
            "post",
            "/intel/intel_items/next",
            "get next intel items",
            json_body=body,
        )
        if err:
            return err
        data = resp.json() if resp is not None else {}
        items = data.get("intel_items", []) if isinstance(data, dict) else []
        next_scroll = data.get("scroll_id") if isinstance(data, dict) else None
        if not items:
            return "No more Cybersixgill intel items in this result set."
        return _format_intel_items(items, next_scroll)

    # =====================================================================
    # 4. ACTIONABLE ALERTS (read / write / destructive)
    # =====================================================================

    def _format_alert_row(a: dict[str, Any]) -> str:
        alert_id = a.get("id") or a.get("alert_id") or "?"
        title = a.get("title") or a.get("alert_name") or "Untitled"
        level = a.get("threat_level", "?")
        threats = a.get("threats") or []
        if isinstance(threats, list):
            threats = ", ".join(str(t) for t in threats)
        status = a.get("status")
        if isinstance(status, dict):
            status = status.get("name")
        date = a.get("date") or ""
        read = a.get("read")
        line = (
            f"  - #{alert_id} {_trunc(title, 160)}\n"
            f"    Threat level: {level}"
            + (f" | Threats: {threats}" if threats else "")
            + (f" | Status: {status}" if status else "")
            + (f" | Site: {a.get('site')}" if a.get("site") else "")
        )
        meta: list[str] = []
        if date:
            meta.append(f"date: {date}")
        if read is not None:
            meta.append("read" if read else "unread")
        if meta:
            line += "\n    " + " | ".join(meta)
        return line

    async def list_cybersixgill_alerts(
        from_date: str = "",
        to_date: str = "",
        threat_level: str = "",
        threat_type: str = "",
        is_read: str = "",
        sort_by: str = "date",
        sort_order: str = "desc",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """List Cybersixgill actionable alerts for your organization's assets.

        Actionable alerts are the curated, per-organization notifications
        Cybersixgill raises when underground activity matches your monitored
        assets (brand, domains, VIPs, ...). Filter by ``from_date`` / ``to_date``
        (UTC "YYYY-MM-DD HH:MM:SS"), ``threat_level`` (imminent / emerging),
        ``threat_type`` (e.g. Data Leak, Phishing, Compromised Accounts, Malware,
        Vulnerability Exploit, ...), and ``is_read`` (read / unread). Returns
        alert IDs for get_cybersixgill_alert / update_cybersixgill_alert. Use
        get_cybersixgill_alert_content for the matched underground content.
        """
        params = _org_params(
            {
                "fetch_size": _clamp(limit, 25, hi=200),
                "offset": max(int(offset) if str(offset).lstrip("-").isdigit() else 0, 0),
                "sort_by": (sort_by or "date").strip(),
                "sort_order": (sort_order or "desc").strip().lower(),
            }
        )
        if from_date:
            params["from_date"] = from_date.strip()
        if to_date:
            params["to_date"] = to_date.strip()
        if threat_level:
            tl = threat_level.strip().lower()
            if tl not in _ALERT_THREAT_LEVELS:
                return (
                    f"Error: threat_level must be one of "
                    f"{', '.join(sorted(_ALERT_THREAT_LEVELS))}."
                )
            params["threat_level"] = tl
        if threat_type:
            params["threat_type"] = threat_type.strip()
        if is_read:
            ir = is_read.strip().lower()
            if ir not in _ALERT_READ_STATES:
                return "Error: is_read must be 'read' or 'unread'."
            params["is_read"] = ir
        resp, err = await _request(
            "get", "/alerts/actionable-alert", "list actionable alerts", params=params
        )
        if err:
            return err
        data = resp.json() if resp is not None else []
        if isinstance(data, list):
            alerts = data
        elif isinstance(data, dict):
            alerts = data.get("items", [])
        else:
            alerts = []
        if not alerts:
            return "No Cybersixgill actionable alerts match the given filters."
        return format_entity_list(
            alerts, _format_alert_row, noun="actionable alert"
        )

    async def get_cybersixgill_alert(alert_id: str) -> str:
        """Get the full detail of a Cybersixgill actionable alert by ID.

        Returns the alert's title, threat level/type, status, assessment,
        recommendations, matched assets, and actor information. Obtain
        ``alert_id`` from list_cybersixgill_alerts. For the underlying
        underground content items, use get_cybersixgill_alert_content.
        """
        alert_id = (alert_id or "").strip()
        if not alert_id:
            return "Error: alert_id is required."
        resp, err = await _request(
            "get",
            f"/alerts/actionable_alert/{alert_id}",
            f"get actionable alert {alert_id}",
            params=_org_params(),
        )
        if err:
            return err
        a = resp.json() if resp is not None else {}
        # A 404 already surfaced via format_http_error above; an empty body is
        # the only remaining "not found" signal. Do NOT require the response to
        # echo the id - the detail payload may omit it.
        if not isinstance(a, dict) or not a:
            return f"Actionable alert {alert_id} not found."
        status = a.get("status")
        if isinstance(status, dict):
            status = status.get("name")
        lines = [
            f"Actionable alert #{a.get('id') or a.get('alert_id') or alert_id}: "
            f"{a.get('title') or a.get('alert_name') or 'Untitled'}",
            f"Threat level: {a.get('threat_level', '?')} | Status: {status or '?'} | "
            f"Site: {a.get('site', 'N/A')}",
            f"Date: {a.get('date', 'N/A')} | Read: {a.get('read', '?')}",
        ]
        threats = a.get("threats") or []
        if isinstance(threats, list) and threats:
            lines.append("Threats: " + ", ".join(str(t) for t in threats))
        if a.get("assessment"):
            lines.append(f"Assessment: {_trunc(a.get('assessment'), 500)}")
        recs = a.get("recommendations") or []
        if isinstance(recs, list) and recs:
            lines.append("Recommendations:")
            lines.extend(f"  - {_trunc(r, 300)}" for r in recs)
        actor = a.get("actor_information") or {}
        if isinstance(actor, dict) and actor:
            lines.append(
                f"Actor: {actor.get('actor_name', 'N/A')} "
                f"(site {actor.get('site', 'N/A')}, "
                f"reputation {actor.get('reputation', 'N/A')})"
            )
        additional = a.get("additional_info") or {}
        if isinstance(additional, dict) and additional.get("cve_id"):
            lines.append(f"CVE: {additional.get('cve_id')}")
        lines.append(
            f"Portal: "
            f"{_alert_portal_link(str(a.get('id') or a.get('alert_id') or alert_id), organization_id)}"
        )
        return "\n".join(lines)

    async def get_cybersixgill_alert_content(
        alert_id: str,
        limit: int = 25,
        highlight: bool = False,
        aggregate_alert_id: int = -1,
    ) -> str:
        """Get the matched underground content items behind an actionable alert.

        Returns the actual deep/dark web posts / leaked records that triggered
        the alert (title, content, creator, site). This is the "why" behind the
        alert - use it to validate and investigate. ``alert_id`` comes from
        list_cybersixgill_alerts. Set ``highlight`` to mark the matched terms;
        pass ``aggregate_alert_id`` only when drilling into a specific sub-alert.
        """
        alert_id = (alert_id or "").strip()
        if not alert_id:
            return "Error: alert_id is required."
        params = _org_params(
            {"limit": _clamp(limit, 25, hi=100), "highlight": bool(highlight)}
        )
        sub_alert = _to_int(aggregate_alert_id)
        if sub_alert >= 0:
            params["aggregate_alert_id"] = sub_alert
        resp, err = await _request(
            "get",
            f"/alerts/actionable_alert_content/{alert_id}",
            f"get actionable alert {alert_id} content",
            params=params,
        )
        if err:
            return err
        data = resp.json() if resp is not None else {}
        content = data.get("content") if isinstance(data, dict) else None
        items = (content or {}).get("items", []) if isinstance(content, dict) else []
        if not items:
            # Some responses nest items directly under the body.
            items = data.get("items", []) if isinstance(data, dict) else []
        if not items:
            return f"No content items found for actionable alert {alert_id}."

        def _fmt(item: dict[str, Any]) -> str:
            src = item.get("_source") or item
            title = src.get("title") or item.get("title") or "Untitled"
            creator = src.get("creator") or item.get("creator") or "?"
            site = src.get("site") or item.get("site") or "?"
            body = src.get("content") or item.get("content") or ""
            # A leaked record (a "Compromised Accounts" alert) carries the
            # secret itself: the shared exposure scrub masks it (#5686).
            return (
                f"  - {_trunc(scrub_exposure_text(title), 160)}\n"
                f"    Actor: {creator} | Site: {site}\n"
                f"    {_trunc(scrub_exposure_text(body), 300)}"
            )

        return format_entity_list(
            items, _fmt, noun="content item"
        )

    async def update_cybersixgill_alert(
        alert_id: str,
        status: str = "",
        read: str = "",
        threat_level: str = "",
        aggregate_alert_id: int = -1,
    ) -> str:
        """Triage a Cybersixgill actionable alert: set its status, read flag, or level.

        Updates one actionable alert in place. ``status`` is one of
        treatment_required, in_treatment, resolved. ``read`` is read / unread.
        ``threat_level`` is imminent / emerging. Provide only the fields you
        want to change. Pass ``aggregate_alert_id`` to update a specific
        sub-alert. Obtain ``alert_id`` from list_cybersixgill_alerts.
        """
        alert_id = (alert_id or "").strip()
        if not alert_id:
            return "Error: alert_id is required."
        body: dict[str, Any] = {}
        if status:
            st = status.strip().lower()
            if st not in _ALERT_STATUSES:
                return (
                    f"Error: status must be one of "
                    f"{', '.join(sorted(_ALERT_STATUSES))}."
                )
            body["status"] = {"status": st}
        if read:
            rd = read.strip().lower()
            if rd not in _ALERT_READ_STATES:
                return "Error: read must be 'read' or 'unread'."
            body["read"] = rd == "read"
        if threat_level:
            tl = threat_level.strip().lower()
            if tl not in _ALERT_THREAT_LEVELS:
                return (
                    f"Error: threat_level must be one of "
                    f"{', '.join(sorted(_ALERT_THREAT_LEVELS))}."
                )
            body["threat_level"] = tl
        if not body:
            return (
                "Error: provide at least one field to update (status, read, or "
                "threat_level)."
            )
        params = _org_params({"consumer": DEFAULT_CHANNEL_ID})
        sub_alert = _to_int(aggregate_alert_id)
        if sub_alert >= 0:
            params["sub_alert_indexes"] = [sub_alert]
        _resp, err = await _request(
            "patch",
            f"/alerts/actionable_alert/{alert_id}",
            f"update actionable alert {alert_id}",
            params=params,
            json_body=body,
        )
        if err:
            return err
        changed = ", ".join(
            f"{k}={v['status'] if isinstance(v, dict) else v}" for k, v in body.items()
        )
        return f"Updated actionable alert {alert_id} ({changed})."

    async def delete_cybersixgill_alert(alert_ids: str) -> str:
        """Permanently delete one or more Cybersixgill actionable alerts.

        DESTRUCTIVE and cannot be undone: the alert(s) are removed from your
        Cybersixgill organization. ``alert_ids`` is a comma-separated list of
        actionable alert IDs (obtain them from list_cybersixgill_alerts).
        """
        ids = [i.strip() for i in (alert_ids or "").split(",") if i.strip()]
        if not ids:
            return "Error: alert_ids is required (comma-separated alert IDs)."
        _resp, err = await _request(
            "delete",
            "/alerts/actionable-alert",
            "delete actionable alerts",
            params=_org_params(),
            json_body=ids,
        )
        if err:
            return err
        return (
            f"Permanently deleted {len(ids)} actionable alert"
            f"{'s' if len(ids) != 1 else ''}: {', '.join(ids)}."
        )

    # =====================================================================
    # 5. REPORTS (finished intelligence, read)
    # =====================================================================

    async def list_cybersixgill_reports(
        threat_level: str = "",
        is_read: str = "",
        sort_by: str = "",
        sort_order: str = "desc",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """List Cybersixgill finished-intelligence reports.

        Returns the analyst reports available to your organization with their
        IDs, titles, and threat levels. Filter by ``threat_level`` (imminent /
        emerging) and ``is_read`` (read / unread). Use get_cybersixgill_report
        for the structured detail or get_cybersixgill_report_html for the
        rendered report.
        """
        body: dict[str, Any] = {
            "fetch_size": _clamp(limit, 25, hi=200),
            "offset": max(int(offset) if str(offset).lstrip("-").isdigit() else 0, 0),
            "sort_order": (sort_order or "desc").strip().lower(),
        }
        if sort_by:
            body["sort_by"] = sort_by.strip()
        if threat_level:
            tl = threat_level.strip().lower()
            if tl not in _ALERT_THREAT_LEVELS:
                return (
                    f"Error: threat_level must be one of "
                    f"{', '.join(sorted(_ALERT_THREAT_LEVELS))}."
                )
            body["threat_level"] = tl
        if is_read:
            ir = is_read.strip().lower()
            if ir not in _ALERT_READ_STATES:
                return "Error: is_read must be 'read' or 'unread'."
            body["is_read"] = ir
        resp, err = await _request(
            "post",
            "/reports/reports",
            "list reports",
            params=_org_params(),
            json_body=body,
        )
        if err:
            return err
        data = resp.json() if resp is not None else {}
        if isinstance(data, list):
            reports = data
        elif isinstance(data, dict):
            reports = data.get("reports") or data.get("items") or []
        else:
            reports = []
        if not reports:
            return "No Cybersixgill reports match the given filters."

        def _fmt(r: dict[str, Any]) -> str:
            return (
                f"  - #{r.get('id', '?')} {_trunc(r.get('title', 'Untitled'), 180)}"
                + (
                    f"\n    Threat level: {r.get('threat_level')}"
                    if r.get("threat_level")
                    else ""
                )
                + (f" | {r.get('date')}" if r.get("date") else "")
            )

        return format_entity_list(reports, _fmt, noun="report")

    async def get_cybersixgill_report(report_id: str) -> str:
        """Get the structured detail of a Cybersixgill finished-intelligence report.

        Returns the report's title, threat level, publication date, summary, and
        body sections. ``report_id`` comes from list_cybersixgill_reports. For
        the fully rendered version, use get_cybersixgill_report_html.
        """
        report_id = (report_id or "").strip()
        if not report_id:
            return "Error: report_id is required."
        resp, err = await _request(
            "get",
            f"/reports/report/{report_id}",
            f"get report {report_id}",
            params=_org_params(),
        )
        if err:
            return err
        r = resp.json() if resp is not None else {}
        if not isinstance(r, dict) or not r:
            return f"Report {report_id} not found."
        lines = [
            f"Report #{r.get('id', report_id)}: {r.get('title', 'Untitled')}",
            f"Threat level: {r.get('threat_level', 'N/A')} | Date: {r.get('date', 'N/A')}",
        ]
        summary = r.get("summary") or r.get("description")
        if summary:
            lines.append(f"Summary: {_trunc(summary, 800)}")
        body = r.get("content") or r.get("body")
        if isinstance(body, str) and body:
            lines.append(f"Content: {_trunc(body, 1200)}")
        return "\n".join(lines)

    async def get_cybersixgill_report_html(
        report_id: str, html_format: str = "html"
    ) -> str | ToolResult:
        """Get the rendered HTML of a Cybersixgill report as a file of the run.

        The whole report is stored (a download card and a file_id) and carried
        on the result, which the model reads as its rendered text. Use
        this when you need the full narrative rendering rather than the
        structured fields from get_cybersixgill_report. ``report_id`` comes
        from list_cybersixgill_reports.
        """
        report_id = (report_id or "").strip()
        if not report_id:
            return "Error: report_id is required."
        creds_err = _creds_error()
        if creds_err:
            return creds_err
        access = await _token()
        if not access:
            return _AUTH_ERROR
        async with egress_client(timeout=_DEFAULT_TIMEOUT) as client:
            fetched = await fetch_file(
                client,
                "get",
                f"{base_url}/reports/report/{report_id}/html",
                provider=_PROVIDER,
                action=f"get report {report_id} HTML",
                max_bytes=_MAX_REPORT_BYTES,
                headers={
                    "Authorization": f"Bearer {access}",
                    "Accept": "text/html, */*",
                    "X-Channel-Id": DEFAULT_CHANNEL_ID,
                    "Cache-Control": "no-cache",
                },
                params=_org_params({"html_format": (html_format or "html").strip()}),
            )
        if isinstance(fetched, str):
            return fetched
        if not fetched.data.strip():
            return f"Report {report_id} returned no HTML content."
        safe_id = "".join(c if c.isalnum() or c in "-_" else "-" for c in report_id)
        return await deliver_file(
            data=fetched.data,
            filename=f"cybersixgill-report-{safe_id}.html",
            content_type="text/html",
            source=_PROVIDER,
            persist_file_fn=persist_file_fn,
            links=[("Download URL", f"{base_url}/reports/report/{report_id}/html")],
        )

    # =====================================================================
    # 6. ORGANIZATION ASSETS (read / write)
    # =====================================================================

    async def get_cybersixgill_organization_assets(
        upgraded_assets: bool = False,
    ) -> str:
        """Get the organization assets Cybersixgill monitors for you.

        Returns the configured monitored assets (domains, IPs, brand names,
        VIPs, BINs, ...) that drive actionable-alert matching. Set
        ``upgraded_assets`` to return the upgraded (structured) asset schema.
        """
        resp, err = await _request(
            "get",
            "/assets/organization",
            "get organization assets",
            params=_org_params({"upgraded_assets": bool(upgraded_assets)}),
        )
        if err:
            return err
        data = resp.json() if resp is not None else {}
        if not data:
            return "No Cybersixgill organization assets are configured."
        import json as _json

        return (
            "Cybersixgill organization assets:\n"
            + _trunc(_json.dumps(data, indent=2, ensure_ascii=False), 3000)
        )

    async def manage_cybersixgill_organization_assets(
        action: str,
        assets: Any,
        upgraded_assets: bool = False,
    ) -> str:
        """Add to or replace the Cybersixgill monitored organization assets.

        WRITE operation - changes what Cybersixgill monitors and therefore which
        actionable alerts you receive. ``action='add'`` appends to the current
        asset set (POST); ``action='replace'`` overwrites it (PUT). ``assets``
        is the asset payload as a JSON object (a dict keyed by asset category,
        e.g. {"domain_names": ["acme.com"], "ip_addresses": ["1.2.3.4"]}) -
        obtain the current shape from get_cybersixgill_organization_assets first.
        """
        act = (action or "").strip().lower()
        if act not in _ORG_ASSET_ACTIONS:
            return (
                f"Error: action must be one of "
                f"{', '.join(sorted(_ORG_ASSET_ACTIONS))}."
            )
        payload = _parse_json_arg(assets)
        if not isinstance(payload, dict) or not payload:
            return (
                "Error: assets must be a non-empty JSON object keyed by asset "
                "category (e.g. {\"domain_names\": [\"acme.com\"]})."
            )
        method = "post" if act == "add" else "put"
        _resp, err = await _request(
            method,
            "/assets/organization",
            f"{act} organization assets",
            params=_org_params({"upgraded_assets": bool(upgraded_assets)}),
            json_body=payload,
        )
        if err:
            return err
        verb = "Added to" if act == "add" else "Replaced"
        return f"{verb} Cybersixgill organization assets."

    # -- register --------------------------------------------------------
    executor.register("enrich_cybersixgill_cve", enrich_cybersixgill_cve)
    executor.register(
        "get_cybersixgill_cve_remediation", get_cybersixgill_cve_remediation
    )
    executor.register("enrich_cybersixgill_ioc", enrich_cybersixgill_ioc)
    executor.register("enrich_cybersixgill_actor", enrich_cybersixgill_actor)
    executor.register("enrich_cybersixgill_post", enrich_cybersixgill_post)
    executor.register(
        "search_cybersixgill_intel_items", search_cybersixgill_intel_items
    )
    executor.register(
        "search_cybersixgill_intel_items_advanced",
        search_cybersixgill_intel_items_advanced,
    )
    executor.register(
        "get_cybersixgill_intel_items_next", get_cybersixgill_intel_items_next
    )
    executor.register("list_cybersixgill_alerts", list_cybersixgill_alerts)
    executor.register("get_cybersixgill_alert", get_cybersixgill_alert)
    executor.register(
        "get_cybersixgill_alert_content", get_cybersixgill_alert_content
    )
    executor.register("update_cybersixgill_alert", update_cybersixgill_alert)
    executor.register("delete_cybersixgill_alert", delete_cybersixgill_alert)
    executor.register("list_cybersixgill_reports", list_cybersixgill_reports)
    executor.register("get_cybersixgill_report", get_cybersixgill_report)
    executor.register("get_cybersixgill_report_html", get_cybersixgill_report_html)
    executor.register(
        "get_cybersixgill_organization_assets",
        get_cybersixgill_organization_assets,
    )
    executor.register(
        "manage_cybersixgill_organization_assets",
        manage_cybersixgill_organization_assets,
    )

    src = _SOURCE
    return [
        ToolDefinition(
            name="enrich_cybersixgill_cve",
            description=(
                "Enrich a CVE with the Cybersixgill Dynamic Vulnerability "
                "Exploit (DVE) score - the real-world exploitation likelihood "
                "derived from underground chatter, not just CVSS. Returns the "
                "current and highest-ever DVE score, attributes (trending, "
                "PoC/exploit availability, exploited-in-the-wild), and a portal "
                "link. Use to prioritise patching. cve_id is a single CVE, e.g. "
                "'CVE-2021-44228'. Pair with get_cybersixgill_cve_remediation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cve_id": {
                        "type": "string",
                        "description": "A single CVE identifier, e.g. CVE-2021-44228",
                    }
                },
                "required": ["cve_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_cybersixgill_cve_remediation",
            description=(
                "Get Cybersixgill remediation / mitigation guidance for a CVE. "
                "Returns the recommended fixes for the vulnerability. Pair with "
                "enrich_cybersixgill_cve (which returns the DVE exploitation "
                "score) to both prioritise and act. cve_id is a single CVE, "
                "e.g. 'CVE-2021-44228'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cve_id": {
                        "type": "string",
                        "description": "A single CVE identifier, e.g. CVE-2021-44228",
                    }
                },
                "required": ["cve_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="enrich_cybersixgill_ioc",
            description=(
                "Enrich an IOC (IP, domain, URL, or file hash) with Cybersixgill "
                "deep/dark web context: associated threat actors, source posts, "
                "threat labels, and STIX patterns tying the observable to "
                "underground activity. Only IOCs Cybersixgill has observed "
                "return items. After enriching, pivot with "
                "enrich_cybersixgill_actor (on a surfaced actor) or "
                "enrich_cybersixgill_post (on a surfaced post_id). ioc_type is "
                "one of ip, domain, url, hash."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ioc_type": {
                        "type": "string",
                        "enum": ["ip", "domain", "url", "hash"],
                    },
                    "ioc_value": {
                        "type": "string",
                        "description": "The observable value, e.g. 1.2.3.4 or evil.test",
                    },
                    "skip": {
                        "type": "integer",
                        "description": "Number of items to skip (pagination)",
                        "default": 0,
                    },
                },
                "required": ["ioc_type", "ioc_value"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="enrich_cybersixgill_actor",
            description=(
                "Enrich a threat actor with the intelligence items tied to them "
                "- the other IOC patterns, posts, and infrastructure associated "
                "with that actor. This is the pivot that widens an "
                "investigation from a single IOC to an actor's wider footprint. "
                "Obtain an actor_name from enrich_cybersixgill_ioc."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "actor_name": {"type": "string"},
                    "skip": {
                        "type": "integer",
                        "description": "Number of items to skip (pagination)",
                        "default": 0,
                    },
                },
                "required": ["actor_name"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="enrich_cybersixgill_post",
            description=(
                "Enrich a source post with the intelligence extracted from it - "
                "the IOC patterns and observables that underground post yielded. "
                "This is the pivot from a single IOC back to its originating "
                "post. Obtain a post_id from enrich_cybersixgill_ioc or a dark "
                "web search."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "post_id": {"type": "string"},
                    "skip": {
                        "type": "integer",
                        "description": "Number of items to skip (pagination)",
                        "default": 0,
                    },
                },
                "required": ["post_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="search_cybersixgill_intel_items",
            description=(
                "Search Cybersixgill's collected deep/dark web intel items with "
                "a simple full-text query (forum, marketplace, paste, and "
                "Telegram posts). Use for a quick lookup by keyword, brand, "
                "domain, actor handle, or leaked artifact. Returns posts with "
                "title, site, actor, date, and a post_id for "
                "enrich_cybersixgill_post. For date-range/sort/deep pagination "
                "use search_cybersixgill_intel_items_advanced. Set recent_items "
                "to limit to the last ~2 days."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "results_size": {"type": "integer", "default": 25},
                    "recent_items": {"type": "boolean", "default": False},
                    "highlight": {"type": "boolean", "default": False},
                },
                "required": ["query"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="search_cybersixgill_intel_items_advanced",
            description=(
                "Search Cybersixgill deep/dark web intel items with structured "
                "controls: Cybersixgill query-language query, a collection-time "
                "date range (date_range_start / date_range_end, ISO-8601 UTC), "
                "and sort (field + asc/desc). When more results exist a "
                "scroll_id is returned - pass it to "
                "get_cybersixgill_intel_items_next to page on. Use this over the "
                "simple search when you need filtering or deep pagination."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "date_range_start": {
                        "type": "string",
                        "description": "ISO-8601 UTC start, e.g. 2026-01-01T00:00:00",
                    },
                    "date_range_end": {
                        "type": "string",
                        "description": "ISO-8601 UTC end, e.g. 2026-02-01T00:00:00",
                    },
                    "sort": {
                        "type": "string",
                        "description": "Field to sort by, e.g. 'date'",
                    },
                    "sort_type": {
                        "type": "string",
                        "enum": ["asc", "desc"],
                        "default": "desc",
                    },
                    "results_size": {"type": "integer", "default": 25},
                    "recent_items": {"type": "boolean", "default": False},
                    "highlight": {"type": "boolean", "default": False},
                },
                "required": ["query"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_cybersixgill_intel_items_next",
            description=(
                "Fetch the next page of an advanced dark web search using the "
                "scroll_id returned by search_cybersixgill_intel_items_advanced. "
                "Returns the next batch of posts plus a fresh scroll_id while "
                "more results remain."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "scroll_id": {"type": "string"},
                    "recent_items": {"type": "boolean", "default": False},
                },
                "required": ["scroll_id"],
            },
            source=src,
            priority=0,
        ),
        # -- 4. Actionable alerts ----------------------------------------
        ToolDefinition(
            name="list_cybersixgill_alerts",
            description=(
                "List Cybersixgill actionable alerts - the curated, "
                "per-organization notifications raised when underground "
                "activity matches your monitored assets (brand, domains, VIPs). "
                "Filter by from_date/to_date (UTC 'YYYY-MM-DD HH:MM:SS'), "
                "threat_level (imminent/emerging), threat_type (Data Leak, "
                "Phishing, Compromised Accounts, Malware, Vulnerability Exploit, "
                "Brand Protection, Fraud, ...), and is_read (read/unread). "
                "Returns alert IDs for get_cybersixgill_alert and "
                "update_cybersixgill_alert."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "from_date": {
                        "type": "string",
                        "description": "UTC start, 'YYYY-MM-DD HH:MM:SS'",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "UTC end, 'YYYY-MM-DD HH:MM:SS'",
                    },
                    "threat_level": {
                        "type": "string",
                        "enum": ["imminent", "emerging"],
                    },
                    "threat_type": {"type": "string"},
                    "is_read": {"type": "string", "enum": ["read", "unread"]},
                    "sort_by": {"type": "string", "default": "date"},
                    "sort_order": {
                        "type": "string",
                        "enum": ["asc", "desc"],
                        "default": "desc",
                    },
                    "limit": {"type": "integer", "default": 25},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_cybersixgill_alert",
            description=(
                "Get the full detail of a Cybersixgill actionable alert: title, "
                "threat level/type, status, assessment, recommendations, matched "
                "assets, and actor information. Obtain alert_id from "
                "list_cybersixgill_alerts. For the underlying underground "
                "content, use get_cybersixgill_alert_content."
            ),
            input_schema={
                "type": "object",
                "properties": {"alert_id": {"type": "string"}},
                "required": ["alert_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_cybersixgill_alert_content",
            description=(
                "Get the matched underground content items behind an actionable "
                "alert - the actual deep/dark web posts or leaked records that "
                "triggered it (title, content, actor, site). This is the 'why' "
                "behind the alert. alert_id comes from list_cybersixgill_alerts. "
                "Set highlight to mark matched terms; pass aggregate_alert_id "
                "only to drill into a specific sub-alert."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 25},
                    "highlight": {"type": "boolean", "default": False},
                    "aggregate_alert_id": {
                        "type": "integer",
                        "description": "Sub-alert index; omit for the whole alert",
                        "default": -1,
                    },
                },
                "required": ["alert_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="update_cybersixgill_alert",
            description=(
                "Triage a Cybersixgill actionable alert in place: set its status "
                "(treatment_required, in_treatment, resolved), read flag "
                "(read/unread), and/or threat_level (imminent/emerging). Provide "
                "only the fields to change. Pass aggregate_alert_id to update a "
                "specific sub-alert. alert_id comes from list_cybersixgill_alerts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["treatment_required", "in_treatment", "resolved"],
                    },
                    "read": {"type": "string", "enum": ["read", "unread"]},
                    "threat_level": {
                        "type": "string",
                        "enum": ["imminent", "emerging"],
                    },
                    "aggregate_alert_id": {
                        "type": "integer",
                        "description": "Sub-alert index; omit for the whole alert",
                        "default": -1,
                    },
                },
                "required": ["alert_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="delete_cybersixgill_alert",
            description=(
                "Permanently delete one or more Cybersixgill actionable alerts. "
                "DESTRUCTIVE and cannot be undone - the alert(s) are removed from "
                "your Cybersixgill organization. alert_ids is a comma-separated "
                "list of alert IDs from list_cybersixgill_alerts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_ids": {
                        "type": "string",
                        "description": "Comma-separated actionable alert IDs",
                    }
                },
                "required": ["alert_ids"],
            },
            source=src,
            priority=2,
        ),
        # -- 5. Reports ---------------------------------------------------
        ToolDefinition(
            name="list_cybersixgill_reports",
            description=(
                "List Cybersixgill finished-intelligence (analyst) reports with "
                "their IDs, titles, and threat levels. Filter by threat_level "
                "(imminent/emerging) and is_read (read/unread). Use "
                "get_cybersixgill_report for structured detail or "
                "get_cybersixgill_report_html for the rendered report."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "threat_level": {
                        "type": "string",
                        "enum": ["imminent", "emerging"],
                    },
                    "is_read": {"type": "string", "enum": ["read", "unread"]},
                    "sort_by": {"type": "string"},
                    "sort_order": {
                        "type": "string",
                        "enum": ["asc", "desc"],
                        "default": "desc",
                    },
                    "limit": {"type": "integer", "default": 25},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_cybersixgill_report",
            description=(
                "Get the structured detail of a Cybersixgill finished-"
                "intelligence report: title, threat level, date, summary, and "
                "body. report_id comes from list_cybersixgill_reports. For the "
                "rendered version use get_cybersixgill_report_html."
            ),
            input_schema={
                "type": "object",
                "properties": {"report_id": {"type": "string"}},
                "required": ["report_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_cybersixgill_report_html",
            description=(
                "Get the rendered HTML of a Cybersixgill report as a file of the "
                "conversation: the whole report stored (download card + file_id "
                "to forward, e.g. to publish it) and read inline as its rendered "
                "text. Use when you need the full narrative rendering rather "
                "than the structured fields. report_id comes from "
                "list_cybersixgill_reports."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "report_id": {"type": "string"},
                    "html_format": {"type": "string", "default": "html"},
                },
                "required": ["report_id"],
            },
            source=src,
            priority=0,
        ),
        # -- 6. Organization assets --------------------------------------
        ToolDefinition(
            name="get_cybersixgill_organization_assets",
            description=(
                "Get the organization assets Cybersixgill monitors for you "
                "(domains, IPs, brand names, VIPs, BINs, ...) - the assets that "
                "drive actionable-alert matching. Set upgraded_assets for the "
                "structured asset schema. Read this before "
                "manage_cybersixgill_organization_assets to learn the payload "
                "shape."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "upgraded_assets": {"type": "boolean", "default": False}
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="manage_cybersixgill_organization_assets",
            description=(
                "Add to or replace the Cybersixgill monitored organization "
                "assets. WRITE - changes what Cybersixgill monitors and which "
                "actionable alerts you receive. action='add' appends (POST); "
                "action='replace' overwrites (PUT). 'assets' is the asset "
                "payload as a JSON object keyed by asset category, e.g. "
                "{\"domain_names\": [\"acme.com\"], \"ip_addresses\": "
                "[\"1.2.3.4\"]}. Fetch the current shape from "
                "get_cybersixgill_organization_assets first."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "replace"]},
                    "assets": {
                        "type": ["object", "string"],
                        "description": (
                            "Asset payload keyed by category, as a JSON "
                            "object or a JSON-encoded string of that object"
                        ),
                    },
                    "upgraded_assets": {"type": "boolean", "default": False},
                },
                "required": ["action", "assets"],
            },
            source=src,
            priority=1,
        ),
    ]
