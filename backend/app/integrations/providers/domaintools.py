# SPDX-License-Identifier: Apache-2.0

"""DomainTools integration - best-in-class agentic domain intelligence.

Full-fledged DomainTools integration spanning the three API families that share
the ``https://api.domaintools.com`` host, designed for agentic CTI / SOC / brand
protection workflows:

  - Iris Investigate (*investigation*): domain enrichment for 1-100 domains
    (risk score + components, WHOIS/RDAP, IPs, name/mail servers, SSL, guided
    pivot counts) and pivot/reverse search across infrastructure and identity
    attributes (IP, email, name server, registrant, SSL hash, trackers, tags,
    Iris UI search hashes).
  - Iris Enrich (*bulk enrichment*): the same domain attributes at high volume
    on an independent quota - built for enriching alert/log domains.
  - Lookups (*core intelligence*): domain risk score (+ evidence), reputation,
    domain profile, WHOIS + parsed WHOIS (domain or IP), WHOIS history,
    hosting history, granular domain change history, and the reverse suite
    (reverse IP, reverse IP WHOIS, reverse name server, reverse WHOIS).
  - Monitors (*daily alerting*): brand monitor, registrant monitor, and the
    infrastructure monitors (IP / name server / IP registrant).
  - Iris Detect (*lookalike-domain defense*): list monitors and discovered /
    watched / ignored domains, plus a consolidated ``manage`` tool to create /
    update / delete monitors, triage domains (watch / ignore) and escalate
    them (internal blocking or Google Web Risk).

Authentication
--------------
DomainTools authenticates with an **API username + API key**. The integration
stores the key in ``api_key`` and the username in ``extra_config.api_username``;
``integration_tools._get_token`` assembles the compound
``api_username||api_key`` this builder parses. Every request is signed with
**HMAC-SHA256** (DomainTools' recommended scheme): the signature covers
``api_username + timestamp + uri_path`` and is keyed by the API key, so the
key itself never travels on the wire and requests expire quickly.

Error envelope
--------------
DomainTools sometimes returns an error INSIDE a 200 body (e.g. Iris code 413
"more than 10000 matched"), so every tool checks the in-body
``{"error": {"code", "message"}}`` envelope in addition to the HTTP status
handled by ``safe_request``. HTTP 206 (partial content) is valid, usable data.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import (
    format_entity_list,
    format_pagination_hint,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "DomainTools"
_SOURCE = "integration:domaintools"
_BASE_URL = "https://api.domaintools.com"
_DEFAULT_TIMEOUT = 30
_IRIS_TIMEOUT = 60

# Iris Detect state -> domains endpoint path.
_DETECT_STATES: dict[str, str] = {
    "new": "/v1/iris-detect/domains/new/",
    "watched": "/v1/iris-detect/domains/watched/",
    "ignored": "/v1/iris-detect/domains/ignored/",
}

# Infrastructure monitor type -> endpoint path.
_INFRA_MONITORS: dict[str, str] = {
    "ip": "/v1/ip-monitor/",
    "name_server": "/v1/name-server-monitor/",
    "ip_registrant": "/v1/ip-registrant-monitor/",
}

_DETECT_ACTIONS: frozenset[str] = frozenset(
    {
        "create_monitor",
        "update_monitor",
        "delete_monitor",
        "watch_domains",
        "ignore_domains",
        "escalate_domains",
    }
)
_ESCALATION_TYPES: frozenset[str] = frozenset({"blocked", "google_safe"})


# ---------------------------------------------------------------------------
# Auth + formatting helpers (signing shared with the connectivity test)
# ---------------------------------------------------------------------------


def sign_domaintools_params(
    api_username: str, api_key: str, uri_path: str
) -> dict[str, str]:
    """Build the HMAC-SHA256 signed auth query params for a request.

    DomainTools' recommended scheme: sign ``api_username + timestamp +
    uri_path`` with the API key. The key never travels on the wire and the
    signature expires after a brief period. Credentials are stripped so a
    value pasted with trailing whitespace still authenticates.
    """
    username = (api_username or "").strip()
    key = (api_key or "").strip()
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    signature = hmac.new(
        key.encode("utf-8"),
        f"{username}{timestamp}{uri_path}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return {"api_username": username, "timestamp": timestamp, "signature": signature}


def _parse_token(token: str) -> tuple[str, str]:
    """Split the compound ``api_username||api_key`` token.

    ``greedy_last=True`` keeps an API key that legitimately contains ``||``
    intact, and each segment is stripped so pasted whitespace never breaks
    the HMAC signature.
    """
    api_username, api_key = (
        part.strip() for part in parse_compound_token(token, 2, greedy_last=True)
    )
    return api_username, api_key


def _is_unsafe_path_segment(value: Any) -> bool:
    """True if *value* would alter the URL path structure if interpolated.

    Domains / IPs / hostnames are dropped into request paths (e.g.
    ``/v1/{domain}/whois/``), so a value containing a path separator, a
    traversal token, a query/fragment character, whitespace, or an ASCII
    control character could escape the intended endpoint (or desync the
    HMAC signature, which covers the raw path). Non-ASCII is rejected
    too: httpx percent-encodes it on the wire while the signature covers
    the raw string, so a Unicode IDN would fail with a misleading auth
    error - DomainTools expects the punycode (``xn--``) form. Reject
    those (and empty) so a crafted/hallucinated value can't widen what a
    tool calls.
    """
    s = str(value or "").strip()
    return (
        not s
        or s in (".", "..")
        or any(c in s for c in "/\\?#%")
        or any(c.isspace() or ord(c) < 32 or ord(c) > 126 for c in s)
    )


def _v(node: Any, default: Any = "") -> Any:
    """Unwrap Iris ``{"value": ..., "count": ...}`` attribute nodes."""
    if isinstance(node, dict) and "value" in node:
        return node.get("value", default)
    return node if node is not None else default


def _count(node: Any) -> int | None:
    """Return the guided-pivot count of an Iris attribute node, if present."""
    if isinstance(node, dict) and "count" in node:
        try:
            return int(node["count"])
        except (TypeError, ValueError):
            return None
    return None


def _trunc(text: Any, max_len: int = 200) -> str:
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _body_error(data: Any) -> str | None:
    """Extract an in-body DomainTools error envelope, if any.

    The envelope may live at the top level (Iris Detect) or inside
    ``response`` (Iris / Lookups, e.g. the code 413 "more than 10000
    matched" error that rides on an HTTP 200). The standard format carries
    ``message``; the Iris Detect validation format carries ``summary`` plus
    per-field ``messages`` instead.
    """
    if not isinstance(data, dict):
        return None
    for container in (data, data.get("response") or {}):
        err = container.get("error") if isinstance(container, dict) else None
        if isinstance(err, dict) and (err.get("message") or err.get("summary")):
            code = err.get("code", "?")
            detail = err.get("message") or err.get("summary")
            messages = err.get("messages")
            if isinstance(messages, dict) and messages:
                fields = "; ".join(
                    f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}"
                    for k, v in messages.items()
                )
                detail = f"{detail} ({fields})"
            return f"Error: {_PROVIDER} returned code {code}: {detail}"
    return None


def _risk_summary(result: dict) -> str:
    """Format the Iris ``domain_risk`` block into ``risk 72 (phishing 72, ...)``."""
    risk = result.get("domain_risk") or {}
    score = risk.get("risk_score")
    if score is None:
        return "risk N/A"
    parts = []
    for comp in risk.get("components") or []:
        name = str(comp.get("name", "")).replace("threat_profile_", "")
        if name and comp.get("risk_score") is not None:
            parts.append(f"{name} {comp['risk_score']}")
    detail = f" ({', '.join(parts[:4])})" if parts else ""
    return f"risk {score}{detail}"


def _fmt_iris_domain(result: dict) -> str:
    """Compact one-entry summary of an Iris Investigate/Enrich domain result."""
    ips = [
        str(_v(ip.get("address")))
        for ip in (result.get("ip") or [])[:3]
        if _v(ip.get("address"))
    ]
    ns = [
        str(_v(n.get("host")))
        for n in (result.get("name_server") or [])[:2]
        if _v(n.get("host"))
    ]
    mx = [
        str(_v(m.get("host")))
        for m in (result.get("mx") or [])[:2]
        if _v(m.get("host"))
    ]
    active = "active" if result.get("active") else "inactive"
    lines = [
        f"  {result.get('domain', '?')} [{active}] {_risk_summary(result)}",
        "    "
        + " | ".join(
            filter(
                None,
                [
                    f"created: {_v(result.get('create_date')) or 'N/A'}",
                    f"expires: {_v(result.get('expiration_date'))}"
                    if _v(result.get("expiration_date"))
                    else "",
                    f"registrar: {_trunc(_v(result.get('registrar')), 40)}"
                    if _v(result.get("registrar"))
                    else "",
                    f"registrant: {_trunc(_v(result.get('registrant_org')) or _v(result.get('registrant_name')), 40)}"
                    if _v(result.get("registrant_org")) or _v(result.get("registrant_name"))
                    else "",
                ],
            )
        ),
    ]
    infra = " | ".join(
        filter(
            None,
            [
                f"IPs: {', '.join(ips)}" if ips else "",
                f"NS: {', '.join(ns)}" if ns else "",
                f"MX: {', '.join(mx)}" if mx else "",
            ],
        )
    )
    if infra:
        lines.append(f"    {infra}")
    title = _v(result.get("website_title"))
    if title:
        lines.append(f"    title: {_trunc(title, 80)}")
    return "\n".join(lines)


def _pivot_hints(result: dict, threshold: int = 500) -> list[str]:
    """Surface low-count guided pivots (attributes shared by few domains)."""
    hints: list[str] = []

    def _check(label: str, node: Any) -> None:
        count = _count(node)
        value = _v(node)
        if value and count is not None and 0 < count <= threshold:
            hints.append(f"{label}={value} ({count} domains)")

    for ip in result.get("ip") or []:
        _check("ip", ip.get("address"))
    for ns_node in result.get("name_server") or []:
        _check("nameserver_host", ns_node.get("host"))
    for ssl in result.get("ssl_info") or []:
        _check("ssl_hash", ssl.get("hash"))
    _check("registrant", result.get("registrant_name"))
    _check("registrant_org", result.get("registrant_org"))
    _check("google_analytics", result.get("google_analytics"))
    _check("adsense", result.get("adsense"))
    return hints[:6]


def _fmt_detect_domain(d: dict) -> str:
    """Compact one-entry summary of an Iris Detect watchlist domain."""
    escalations = ", ".join(
        e.get("escalation_type", "?") for e in (d.get("escalations") or [])
    )
    bits = [
        f"  [{d.get('id', '?')}] {d.get('domain', '?')} ({d.get('state', '?')})"
        f" risk {d.get('risk_score', 'N/A')}"
        + (" (provisional)" if d.get("risk_score_status") == "provisional" else ""),
        "    "
        + " | ".join(
            filter(
                None,
                [
                    f"discovered: {str(d.get('discovered_date', ''))[:19]}",
                    f"changed: {str(d.get('changed_date', ''))[:19]}"
                    if d.get("changed_date")
                    else "",
                    f"tld: {d.get('tld', '?')}",
                    "MX: yes" if d.get("mx_exists") else "",
                    f"escalations: {escalations}" if escalations else "",
                ],
            )
        ),
    ]
    return "\n".join(bits)


# ---------------------------------------------------------------------------
# Connectivity validation (shared with integration_testing.py)
# ---------------------------------------------------------------------------


async def validate_connection(api_username: str, api_key: str) -> tuple[bool, str]:
    """Verify DomainTools credentials via the Account Information endpoint."""
    if not (api_username or "").strip() or not (api_key or "").strip():
        return False, "Missing API username or API key."
    path = "/v1/account/"
    params = sign_domaintools_params(api_username, api_key, path)
    try:
        async with egress_client(timeout=20) as client:
            resp = await client.get(f"{_BASE_URL}{path}", params=params)
    except httpx.HTTPError as exc:
        return False, f"Connection failed: {exc}"
    if resp.status_code in (200, 206):
        try:
            products = (resp.json().get("response") or {}).get("products") or []
        except Exception:
            products = []
        names = ", ".join(str(p.get("id", "?")) for p in products[:8])
        suffix = f" Licensed products: {names}." if names else ""
        return True, f"DomainTools is connected and authenticated.{suffix}"
    if resp.status_code in (401, 403):
        return False, (
            "Authentication failed - check the API username and API key "
            "(both come from the DomainTools API dashboard), and verify the "
            "account is active."
        )
    return False, f"DomainTools returned HTTP {resp.status_code}."


class DomainToolsIntegration(BaseIntegration):
    """DomainTools domain intelligence / internet intelligence integration."""

    provider_name = "domaintools"
    display_name = "DomainTools"
    icon = "Globe"
    description = (
        "Connect DomainTools to enrich and pivot on domains (Iris Investigate/"
        "Enrich), score domain risk, pull WHOIS and hosting history, run "
        "reverse lookups, and manage Iris Detect lookalike-domain monitors."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        api_username, api_key = _parse_token(token)
        ok, _ = await validate_connection(api_username, api_key)
        return ok

    async def get_user_info(self, token: str) -> dict:
        api_username, api_key = _parse_token(token)
        path = "/v1/account/"
        params = sign_domaintools_params(api_username, api_key, path)
        async with egress_client(timeout=20) as client:
            resp = await client.get(f"{_BASE_URL}{path}", params=params)
            resp.raise_for_status()
            return resp.json()


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build DomainTools tools from the compound ``api_username||api_key`` token."""
    api_username, api_key = _parse_token(token)

    def _client(timeout: int = _DEFAULT_TIMEOUT) -> httpx.AsyncClient:
        return egress_client(timeout=timeout)

    async def _api(
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> tuple[dict | None, str | None]:
        """Signed request returning ``(json, None)`` or ``(None, error)``."""
        if not api_username or not api_key:
            return None, (
                "Error: DomainTools credentials are not configured. Set the "
                "API username (extra config) and API key on the integration."
            )
        query: dict[str, Any] = {k: v for k, v in (params or {}).items() if v not in ("", None)}
        query.update(sign_domaintools_params(api_username, api_key, path))
        async with _client(timeout) as client:
            resp, err = await safe_request(
                client,
                method,
                f"{_BASE_URL}{path}",
                _PROVIDER,
                action,
                params=query,
                **({"json": json_body} if json_body is not None else {}),
            )
            if err:
                return None, err
            try:
                data = resp.json() or {}
            except Exception:
                return None, f"Error: {_PROVIDER} returned a non-JSON response while trying to {action}."
        body_err = _body_error(data)
        if body_err:
            return None, body_err
        return data, None

    # ══════════════════════════════════════════════════════════════════════
    # 0. ACCOUNT
    # ══════════════════════════════════════════════════════════════════════

    async def get_domaintools_account() -> str:
        """Report licensed API products, quotas, and usage for the account."""
        data, err = await _api("get", "/v1/account/", "get account information")
        if err:
            return err
        response = data.get("response") or {}
        account = response.get("account") or {}
        products = response.get("products") or []

        def _fmt(p: dict) -> str:
            usage = p.get("usage") or {}
            return (
                f"  {p.get('id', '?')} - month: {usage.get('month', '?')}"
                f"/{p.get('per_month_limit') or 'unlimited'}"
                f" | today: {usage.get('today', '?')}"
                f" | expires: {p.get('expiration_date', 'N/A')}"
            )

        header = (
            f"DomainTools account {account.get('api_username', '?')} "
            f"({'active' if account.get('active') else 'inactive'})\n"
        )
        return header + format_entity_list(products, _fmt, noun="licensed product")

    # ══════════════════════════════════════════════════════════════════════
    # 1. IRIS INVESTIGATE + ENRICH
    # ══════════════════════════════════════════════════════════════════════

    async def get_domaintools_domain_intel(domains: str, position: str = "") -> str:
        """Enrich 1-100 domains via Iris Investigate (risk, WHOIS, infra, pivots)."""
        domain_list = [d.strip() for d in (domains or "").split(",") if d.strip()]
        if not domain_list:
            return "Error: provide at least one domain (comma-separated, up to 100)."
        if len(domain_list) > 100:
            return "Error: Iris Investigate accepts at most 100 domains per call."
        params: dict[str, Any] = {"domain": ",".join(domain_list)}
        if position:
            params["position"] = position
        data, err = await _api(
            "get",
            "/v1/iris-investigate/",
            "investigate domains",
            params=params,
            timeout=_IRIS_TIMEOUT,
        )
        if err:
            return err
        response = data.get("response") or {}
        results = response.get("results") or []
        out = format_entity_list(
            results,
            _fmt_iris_domain,
            noun="domain result",
            total=response.get("total_count"),
            empty_msg="No Iris data found for the requested domain(s).",
        )
        missing = response.get("missing_domains") or []
        if missing:
            out += f"\nNo records for: {', '.join(map(str, missing[:20]))}"
        if len(results) == 1:
            hints = _pivot_hints(results[0])
            if hints:
                out += (
                    "\nGuided pivots (shared by few domains - pass to "
                    "search_domaintools_iris): " + "; ".join(hints)
                )
        if response.get("has_more_results") and response.get("position"):
            out += format_pagination_hint(response["position"], "position")
        return out

    async def search_domaintools_iris(
        ip: str = "",
        email: str = "",
        email_domain: str = "",
        nameserver_host: str = "",
        nameserver_domain: str = "",
        mailserver_host: str = "",
        mailserver_domain: str = "",
        registrant: str = "",
        registrant_org: str = "",
        registrar: str = "",
        ssl_hash: str = "",
        redirect_domain: str = "",
        google_analytics: str = "",
        whois: str = "",
        tagged_with_any: str = "",
        search_hash: str = "",
        tld: str = "",
        create_date: str = "",
        active: str = "",
        risk_score_min: int = 0,
        first_seen_since: str = "",
        first_seen_within: int = 0,
        ip_country_code: str = "",
        sort_by: str = "",
        sort_direction: str = "",
        page_size: int = 25,
        position: str = "",
    ) -> str:
        """Pivot search Iris Investigate by infrastructure/identity attributes."""
        base_params = {
            "ip": ip,
            "email": email,
            "email_domain": email_domain,
            "nameserver_host": nameserver_host,
            "nameserver_domain": nameserver_domain,
            "mailserver_host": mailserver_host,
            "mailserver_domain": mailserver_domain,
            "registrant": registrant,
            "registrant_org": registrant_org,
            "registrar": registrar,
            "ssl_hash": ssl_hash,
            "redirect_domain": redirect_domain,
            "google_analytics": google_analytics,
            "whois": whois,
            "tagged_with_any": tagged_with_any,
            "search_hash": search_hash,
        }
        params: dict[str, Any] = {k: v for k, v in base_params.items() if v}
        if not params:
            return (
                "Error: provide at least one base search parameter (e.g. ip, "
                "email, nameserver_domain, registrant, ssl_hash, search_hash)."
            )
        if tld:
            params["tld"] = tld.lstrip(".")
        if create_date:
            params["create_date"] = create_date
        if active in ("true", "false"):
            params["active"] = active
        if risk_score_min:
            params["risk_score"] = max(0, min(int(risk_score_min), 100))
        if first_seen_since:
            params["first_seen_since"] = first_seen_since
        if first_seen_within:
            params["first_seen_within"] = max(1, int(first_seen_within))
        if ip_country_code:
            params["ip_country_code"] = ip_country_code
        if sort_by:
            params["sort_by"] = sort_by
        if sort_direction:
            # The API expects the non-standard "dsc" token for descending
            # (docs: &sort_direction=dsc); accept the common "desc" spelling
            # and normalize so LLM-generated calls never send a bad value.
            direction = sort_direction.strip().lower()
            if direction == "desc":
                direction = "dsc"
            if direction not in ("asc", "dsc"):
                return "Error: sort_direction must be 'asc' or 'desc'."
            params["sort_direction"] = direction
        params["page_size"] = max(1, min(int(page_size or 25), 500))
        if position:
            params["position"] = position
        data, err = await _api(
            "get",
            "/v1/iris-investigate/",
            "pivot search Iris",
            params=params,
            timeout=_IRIS_TIMEOUT,
        )
        if err:
            return err
        response = data.get("response") or {}
        results = response.get("results") or []
        out = format_entity_list(
            results,
            _fmt_iris_domain,
            noun="matching domain",
            total=response.get("total_count"),
            empty_msg="No domains matched the pivot search.",
        )
        if response.get("has_more_results") and response.get("position"):
            out += format_pagination_hint(response["position"], "position")
        return out

    async def get_domaintools_enrichment(domains: str) -> str:
        """Bulk-enrich 1-100 domains via Iris Enrich (independent quota)."""
        domain_list = [d.strip() for d in (domains or "").split(",") if d.strip()]
        if not domain_list:
            return "Error: provide at least one domain (comma-separated, up to 100)."
        if len(domain_list) > 100:
            return "Error: Iris Enrich accepts at most 100 domains per call."
        data, err = await _api(
            "get",
            "/v1/iris-enrich/",
            "enrich domains",
            params={"domain": ",".join(domain_list)},
            timeout=_IRIS_TIMEOUT,
        )
        if err:
            return err
        response = data.get("response") or {}
        results = response.get("results") or []
        out = format_entity_list(
            results,
            _fmt_iris_domain,
            noun="enriched domain",
            total=response.get("results_count"),
            empty_msg="No enrichment data found for the requested domain(s).",
        )
        missing = response.get("missing_domains") or []
        if missing:
            out += f"\nNo records for: {', '.join(map(str, missing[:20]))}"
        return out

    # ══════════════════════════════════════════════════════════════════════
    # 2. RISK + REPUTATION
    # ══════════════════════════════════════════════════════════════════════

    async def get_domaintools_risk_score(
        domain: str, include_evidence: bool = False
    ) -> str:
        """Get a domain's risk score and component scores (optionally evidence)."""
        if not (domain or "").strip():
            return "Error: 'domain' is required."
        path = "/v1/risk/evidence/" if include_evidence else "/v1/risk/"
        data, err = await _api(
            "get", path, f"get risk score for {domain}", params={"domain": domain.strip()}
        )
        if err:
            return err
        response = data.get("response") or {}
        lines = [
            f"{response.get('domain', domain)}: risk score "
            f"{response.get('risk_score', 'N/A')} (0=benign, 100=known threat)"
        ]
        for comp in response.get("components") or []:
            bits = [f"  {comp.get('name', '?')}: {comp.get('risk_score', '?')}"]
            if comp.get("threats"):
                bits.append(f"threats: {', '.join(comp['threats'])}")
            if comp.get("evidence"):
                bits.append(f"evidence: {', '.join(comp['evidence'])}")
            lines.append(" | ".join(bits))
        return "\n".join(lines)

    async def get_domaintools_reputation(
        domain: str, include_reasons: bool = True
    ) -> str:
        """Get a domain's reputation risk score with contributing reasons."""
        if not (domain or "").strip():
            return "Error: 'domain' is required."
        params = {"domain": domain.strip()}
        if include_reasons:
            params["include_reasons"] = "true"
        data, err = await _api(
            "get", "/v1/reputation/", f"get reputation for {domain}", params=params
        )
        if err:
            return err
        response = data.get("response") or {}
        out = (
            f"{response.get('domain', domain)}: reputation risk score "
            f"{response.get('risk_score', 'N/A')} (0=least risk, 100=known risk)"
        )
        reasons = response.get("reasons") or []
        if reasons:
            out += f"\nReasons: {', '.join(map(str, reasons))}"
        return out

    # ══════════════════════════════════════════════════════════════════════
    # 3. WHOIS + PROFILE + HISTORY
    # ══════════════════════════════════════════════════════════════════════

    async def get_domaintools_domain_profile(domain: str) -> str:
        """Get a domain's registration snapshot (registrant, server, history counts)."""
        d = (domain or "").strip().lower()
        if _is_unsafe_path_segment(d):
            return "Error: 'domain' must be a bare domain name (e.g. example.com)."
        data, err = await _api("get", f"/v1/{d}/", f"get domain profile for {d}")
        if err:
            return err
        response = data.get("response") or {}
        registration = response.get("registration") or {}
        registrant = response.get("registrant") or {}
        server = response.get("server") or {}
        history = response.get("history") or {}
        website = response.get("website_data") or {}
        lines = [f"Domain profile: {d}"]
        if registrant:
            lines.append(
                f"Registrant: {registrant.get('name', 'N/A')} "
                f"(owns {registrant.get('domains', '?')} domains)"
            )
        if registration:
            lines.append(
                f"Registration: created {registration.get('created', 'N/A')} | "
                f"expires {registration.get('expires', 'N/A')} | "
                f"registrar {registration.get('registrar', 'N/A')} | "
                f"statuses: {', '.join(registration.get('statuses') or []) or 'N/A'}"
            )
        if server:
            lines.append(
                f"Server: {server.get('ip_address', 'N/A')} "
                f"({server.get('other_domains', '?')} other domains on this IP)"
            )
        name_servers = [
            ns.get("server", "?") for ns in (response.get("name_servers") or [])[:4]
        ]
        if name_servers:
            lines.append(f"Name servers: {', '.join(name_servers)}")
        if history:
            whois_hist = history.get("whois") or {}
            ip_hist = history.get("ip_address") or {}
            lines.append(
                f"History: {whois_hist.get('records', '?')} WHOIS records "
                f"(earliest {whois_hist.get('earliest_event', 'N/A')}) | "
                f"{ip_hist.get('events', '?')} IP events"
            )
        if website:
            lines.append(
                f"Website: HTTP {website.get('response_code', '?')} | "
                f"title: {_trunc(website.get('title'), 80) or 'N/A'}"
            )
        return "\n".join(lines)

    async def get_domaintools_whois(query: str) -> str:
        """Get the most recent WHOIS record for a domain or IP address."""
        q = (query or "").strip().lower()
        if _is_unsafe_path_segment(q):
            return "Error: 'query' must be a bare domain name or IP address."
        data, err = await _api("get", f"/v1/{q}/whois/", f"get WHOIS for {q}")
        if err:
            return err
        response = data.get("response") or {}
        registration = response.get("registration") or {}
        whois_node = response.get("whois") or {}
        lines = [
            f"WHOIS for {q} (record date: {whois_node.get('date', 'N/A')})",
            f"Registrant: {response.get('registrant', 'N/A')}",
        ]
        if registration:
            lines.append(
                f"Created: {registration.get('created', 'N/A')} | "
                f"Expires: {registration.get('expires', 'N/A')} | "
                f"Updated: {registration.get('updated', 'N/A')} | "
                f"Registrar: {registration.get('registrar', 'N/A')}"
            )
        name_servers = response.get("name_servers") or []
        if name_servers:
            lines.append(f"Name servers: {', '.join(map(str, name_servers[:6]))}")
        record = whois_node.get("record")
        if record:
            lines.append(f"\nRaw record (truncated):\n{_trunc(record, 1500)}")
        return "\n".join(lines)

    async def get_domaintools_parsed_whois(query: str) -> str:
        """Get parsed + normalized WHOIS fields for a domain or IP address."""
        q = (query or "").strip().lower()
        if _is_unsafe_path_segment(q):
            return "Error: 'query' must be a bare domain name or IP address."
        data, err = await _api(
            "get", f"/v1/{q}/whois/parsed", f"get parsed WHOIS for {q}"
        )
        if err:
            return err
        response = data.get("response") or {}
        parsed = response.get("parsed_whois") or {}
        if not parsed:
            return (
                f"No parsed WHOIS data for {q}. The raw record may still be "
                "available via get_domaintools_whois."
            )
        lines = [f"Parsed WHOIS for {q}"]
        if parsed.get("domain"):
            registrar = parsed.get("registrar") or {}
            lines.append(
                f"Created: {parsed.get('created_date', 'N/A')} | "
                f"Updated: {parsed.get('updated_date', 'N/A')} | "
                f"Expires: {parsed.get('expired_date', 'N/A')}"
            )
            lines.append(
                f"Registrar: {registrar.get('name', 'N/A')} "
                f"(IANA {registrar.get('iana_id') or 'N/A'})"
            )
            ns = parsed.get("name_servers") or []
            if ns:
                lines.append(f"Name servers: {', '.join(map(str, ns[:6]))}")
            for role, contact in (parsed.get("contacts") or {}).items():
                if not isinstance(contact, dict) or not any(contact.values()):
                    continue
                lines.append(
                    f"{role.capitalize()}: {contact.get('name') or 'N/A'} | "
                    f"{contact.get('org') or 'N/A'} | {contact.get('email') or 'N/A'} | "
                    f"{contact.get('country') or 'N/A'}"
                )
        networks = parsed.get("networks") or []
        for net in networks[:3]:
            rng = net.get("range") or {}
            lines.append(
                f"Network {net.get('id', '?')}: {rng.get('from', '?')}-{rng.get('to', '?')} "
                f"({', '.join(rng.get('cidr') or [])}) | org: {net.get('org', 'N/A')} | "
                f"status: {net.get('status', 'N/A')}"
            )
        return "\n".join(lines)

    async def get_domaintools_whois_history(
        domain: str,
        sort: str = "date_desc",
        limit: int = 10,
        offset: int = 0,
        mode: str = "list",
    ) -> str:
        """Get historical WHOIS records for a domain (ownership timeline)."""
        d = (domain or "").strip().lower()
        if _is_unsafe_path_segment(d):
            return "Error: 'domain' must be a bare domain name (e.g. example.com)."
        if mode not in ("list", "count", "check_existence"):
            return "Error: mode must be one of list, count, check_existence."
        params: dict[str, Any] = {"mode": mode}
        if mode == "list":
            params["sort"] = sort if sort in ("date_asc", "date_desc") else "date_desc"
            params["limit"] = max(1, min(int(limit or 10), 100))
            if offset:
                params["offset"] = max(0, int(offset))
        data, err = await _api(
            "get", f"/v1/{d}/whois/history/", f"get WHOIS history for {d}", params=params
        )
        if err:
            return err
        response = data.get("response") or {}
        if mode != "list":
            return (
                f"WHOIS history for {d}: record_count="
                f"{response.get('record_count', response.get('has_whois_history', 'N/A'))}"
            )
        records = response.get("history") or []

        def _fmt(rec: dict) -> str:
            w = rec.get("whois") or {}
            reg = w.get("registration") or {}
            private = " [redacted]" if rec.get("is_private") else ""
            return (
                f"  {rec.get('date', '?')}{private} - registrant: "
                f"{_trunc(w.get('registrant'), 50) or 'N/A'} | registrar: "
                f"{_trunc(reg.get('registrar'), 40) or 'N/A'} | NS: "
                f"{', '.join(map(str, (w.get('name_servers') or [])[:3])) or 'N/A'}"
            )

        out = format_entity_list(
            records,
            _fmt,
            noun="historical WHOIS record",
            total=response.get("record_count"),
        )
        total = response.get("record_count") or 0
        if total > offset + len(records):
            out += f"\n[More records available - pass offset={offset + len(records)}]"
        return out

    async def get_domaintools_hosting_history(domain: str) -> str:
        """Get a domain's IP address and name server change events."""
        d = (domain or "").strip().lower()
        if _is_unsafe_path_segment(d):
            return "Error: 'domain' must be a bare domain name (e.g. example.com)."
        data, err = await _api(
            "get", f"/v1/{d}/hosting-history/", f"get hosting history for {d}"
        )
        if err:
            return err
        response = data.get("response") or {}
        lines = [f"Hosting history for {response.get('domain_name', d)}"]
        ip_history = response.get("ip_history") or []
        if ip_history:
            lines.append(f"IP events ({len(ip_history)}):")
            for e in ip_history[-15:]:
                lines.append(
                    f"  {e.get('actiondate', '?')} [{e.get('action_in_words', '?')}] "
                    f"{e.get('pre_ip') or '-'} -> {e.get('post_ip') or '-'}"
                )
        ns_history = response.get("nameserver_history") or []
        if ns_history:
            lines.append(f"Name server events ({len(ns_history)}):")
            for e in ns_history[-15:]:
                lines.append(
                    f"  {e.get('actiondate', '?')} [{e.get('action_in_words', '?')}] "
                    f"{e.get('pre_mns') or '-'} -> {e.get('post_mns') or '-'}"
                )
        if len(lines) == 1:
            return f"No hosting history events found for {d}."
        return "\n".join(lines)

    async def get_domaintools_domain_history(
        domain: str,
        include_fields: str = "",
        exclude_fields: str = "",
        page_size: int = 25,
        offset: int = 0,
    ) -> str:
        """Get granular change events for a domain (what/when/before/after)."""
        d = (domain or "").strip().lower()
        if not d:
            return "Error: 'domain' is required."
        if include_fields and exclude_fields:
            return "Error: use include_fields OR exclude_fields, not both."
        params: dict[str, Any] = {
            "domain": d,
            "page_size": max(1, min(int(page_size or 25), 100)),
        }
        if include_fields:
            params["include_fields"] = include_fields
        if exclude_fields:
            params["exclude_fields"] = exclude_fields
        if offset:
            params["offset"] = max(0, int(offset))
        data, err = await _api(
            "get", "/v1/domain-history/", f"get domain history for {d}", params=params
        )
        if err:
            return err
        response = data.get("response") or {}
        changes = response.get("changes") or []

        def _state(node: Any, field: str) -> str:
            if not isinstance(node, dict):
                return "-"
            value = node.get(field)
            if value in (None, "", [], {}):
                # Aggregate fields (all_ssl, ...) do not map 1:1 - show a hint.
                return "(see full record)"
            if isinstance(value, list):
                return _trunc(
                    ", ".join(str(_v(x.get("host", x)) if isinstance(x, dict) else x) for x in value[:3]),
                    80,
                )
            return _trunc(_v(value), 80)

        def _fmt(c: dict) -> str:
            field = c.get("field", "?")
            return (
                f"  {str(c.get('timestamp', ''))[:19]} [{field}] "
                f"{_state(c.get('before'), field)} -> {_state(c.get('after'), field)}"
            )

        out = format_entity_list(
            changes,
            _fmt,
            noun="change event",
            total=response.get("count"),
            empty_msg=f"No change events found for {d} (history starts October 2021).",
        )
        total = response.get("count") or 0
        if total > offset + len(changes):
            out += f"\n[More events available - pass offset={offset + len(changes)}]"
        return out

    # ══════════════════════════════════════════════════════════════════════
    # 4. REVERSE LOOKUPS
    # ══════════════════════════════════════════════════════════════════════

    async def search_domaintools_reverse_ip(query: str, limit: int = 50) -> str:
        """List domains sharing an IP address (input: IP or domain)."""
        q = (query or "").strip().lower()
        if _is_unsafe_path_segment(q):
            return "Error: 'query' must be a bare IP address or domain name."
        # An explicit IP gives more predictable results (host-domains endpoint).
        try:
            ipaddress.ip_address(q)
            is_ip = True
        except ValueError:
            is_ip = False
        path = f"/v1/{q}/host-domains/" if is_ip else f"/v1/{q}/reverse-ip/"
        data, err = await _api(
            "get",
            path,
            f"reverse IP lookup for {q}",
            params={"limit": max(1, min(int(limit or 50), 1000))},
        )
        if err:
            return err
        response = data.get("response") or {}
        ip_addresses = response.get("ip_addresses")
        if isinstance(ip_addresses, dict):
            ip_addresses = [ip_addresses]
        lines = []
        for entry in ip_addresses or []:
            names = entry.get("domain_names") or []
            total = entry.get("domain_count", len(names))
            lines.append(
                f"IP {entry.get('ip_address', '?')} - {total} domain(s) known"
                + (f" (showing {len(names)})" if total > len(names) else "")
            )
            for name in names[: max(1, int(limit or 50))]:
                lines.append(f"  {str(name).lower()}")
        return "\n".join(lines) if lines else f"No shared-hosting domains found for {q}."

    async def search_domaintools_reverse_ip_whois(
        query: str = "",
        ip: str = "",
        country: str = "",
        server: str = "",
        page: int = 1,
    ) -> str:
        """Find IP ranges owned by an organization, or the IP WHOIS of one IP."""
        if not query and not ip:
            return "Error: provide 'query' (organization terms) or 'ip' (single IP)."
        params: dict[str, Any] = {}
        if ip:
            params["ip"] = ip.strip()
        else:
            params["query"] = query.strip()
            params["page"] = max(1, min(int(page or 1), 5))
            if country:
                params["country"] = country
            if server:
                params["server"] = server
        data, err = await _api(
            "get", "/v1/reverse-ip-whois/", "reverse IP WHOIS lookup", params=params
        )
        if err:
            return err
        response = data.get("response") or {}
        if ip:
            return (
                f"IP WHOIS for {ip}: range {response.get('range', 'N/A')} "
                f"({response.get('ip_from', '?')}-{response.get('ip_to', '?')})\n"
                f"Organization: {response.get('organization', 'N/A')} | "
                f"Country: {response.get('country', 'N/A')} | "
                f"Server: {response.get('server', 'N/A')} | "
                f"Record date: {response.get('record_date', 'N/A')}"
            )
        records = response.get("records") or []

        def _fmt(r: dict) -> str:
            return (
                f"  {r.get('range', '?')} ({r.get('ip_from', '?')}-{r.get('ip_to', '?')}) - "
                f"{_trunc(r.get('organization'), 60)} | {r.get('country', '?')} | "
                f"{r.get('server', '?')}"
            )

        out = format_entity_list(
            records, _fmt, noun="IP range", total=response.get("total_count")
        )
        if response.get("has_more_pages"):
            out += f"\n[More pages available - pass page={int(page or 1) + 1} (max 5)]"
        return out

    async def search_domaintools_reverse_nameserver(
        hostname: str, limit: int = 50
    ) -> str:
        """List domains that share a name server (primary or secondary)."""
        h = (hostname or "").strip().lower()
        if _is_unsafe_path_segment(h):
            return "Error: 'hostname' must be a bare name server hostname."
        data, err = await _api(
            "get",
            f"/v1/{h}/name-server-domains/",
            f"reverse name server lookup for {h}",
            params={"limit": max(1, min(int(limit or 50), 1000))},
        )
        if err:
            return err
        response = data.get("response") or {}
        ns = response.get("name_server") or {}
        primary = response.get("primary_domains") or []
        secondary = response.get("secondary_domains") or []
        lines = [
            f"Name server {ns.get('hostname', h)}: {ns.get('total', '?')} domain(s) "
            f"({ns.get('primary', '?')} primary, {ns.get('secondary', '?')} secondary)"
        ]
        if primary:
            lines.append("Primary domains:")
            lines.extend(f"  {d}" for d in primary[: max(1, int(limit or 50))])
        if secondary:
            lines.append("Secondary domains:")
            lines.extend(f"  {d}" for d in secondary[: max(1, int(limit or 50))])
        return "\n".join(lines)

    async def search_domaintools_reverse_whois(
        terms: str,
        exclude: str = "",
        scope: str = "current",
        mode: str = "purchase",
    ) -> str:
        """Find domains whose WHOIS records contain the given owner terms."""
        if not (terms or "").strip():
            return "Error: 'terms' is required (pipe-separated, e.g. 'Acme Corp|Seattle')."
        if scope not in ("current", "historic"):
            return "Error: scope must be 'current' or 'historic'."
        if mode not in ("quote", "purchase"):
            return "Error: mode must be 'quote' (count/price only) or 'purchase' (domain list)."
        params: dict[str, Any] = {"terms": terms.strip(), "scope": scope, "mode": mode}
        if exclude:
            params["exclude"] = exclude
        data, err = await _api(
            "get", "/v1/reverse-whois/", "reverse WHOIS search", params=params
        )
        if err:
            return err
        response = data.get("response") or {}
        counts = response.get("domain_count") or {}
        lines = [
            f"Reverse WHOIS for terms '{terms}': "
            f"{counts.get('current', '?')} current / {counts.get('historic', '?')} historic domains"
        ]
        domains_found = response.get("domains") or []
        if domains_found:
            shown = domains_found[:100]
            lines.append(f"Domains (showing {len(shown)} of {len(domains_found)}):")
            lines.extend(f"  {d}" for d in shown)
        elif mode == "quote":
            price = response.get("report_price") or {}
            lines.append(
                f"Report price: current {price.get('current', '?')} / "
                f"historic {price.get('historic', '?')} (mode=purchase returns the list)"
            )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 5. MONITORS (Lookups & Monitors daily alerts)
    # ══════════════════════════════════════════════════════════════════════

    async def check_domaintools_brand_monitor(
        query: str,
        exclude: str = "",
        domain_status: str = "",
        days_back: int = 0,
    ) -> str:
        """Check today's new domain registrations containing a brand term."""
        if not (query or "").strip():
            return "Error: 'query' is required (use | as logical AND, e.g. 'domain|tools')."
        params: dict[str, Any] = {"query": query.strip()}
        if exclude:
            params["exclude"] = exclude
        if domain_status in ("new", "on-hold"):
            params["domain_status"] = domain_status
        if days_back:
            params["days_back"] = max(1, min(int(days_back), 6))
        data, err = await _api(
            "get", "/v1/mark-alert/", "check brand monitor", params=params
        )
        if err:
            return err
        response = data.get("response") or {}
        alerts = response.get("alerts") or []

        def _fmt(a: dict) -> str:
            return f"  {a.get('domain', '?')} ({a.get('status', '?')})"

        header = f"Brand monitor '{query}' for {response.get('date', 'today')}:\n"
        return header + format_entity_list(
            alerts,
            _fmt,
            noun="matching registration",
            total=response.get("total"),
            empty_msg="No new registrations matched the term today.",
        )

    async def check_domaintools_registrant_monitor(
        query: str,
        exclude: str = "",
        days_back: int = 0,
        limit: int = 50,
    ) -> str:
        """Check today's WHOIS ownership changes matching a registrant term."""
        if not (query or "").strip():
            return "Error: 'query' is required (pipe-separated terms)."
        params: dict[str, Any] = {"query": query.strip(), "limit": max(1, int(limit or 50))}
        if exclude:
            params["exclude"] = exclude
        if days_back:
            params["days_back"] = max(1, min(int(days_back), 6))
        data, err = await _api(
            "get", "/v1/registrant-alert/", "check registrant monitor", params=params
        )
        if err:
            return err
        response = data.get("response") or {}
        alerts = response.get("alerts") or []

        def _fmt(a: dict) -> str:
            return (
                f"  {a.get('domain', '?')} [{a.get('match_type', '?')}] - "
                f"owner: {_trunc(a.get('current_owner'), 50) or 'N/A'}"
                + (
                    f" (was: {_trunc(a.get('last_owner'), 50)})"
                    if a.get("last_owner")
                    else ""
                )
            )

        header = f"Registrant monitor '{query}' for {response.get('date', 'today')}:\n"
        return header + format_entity_list(
            alerts,
            _fmt,
            noun="ownership alert",
            total=response.get("total"),
            empty_msg="No WHOIS changes matched the term today.",
        )

    async def check_domaintools_infrastructure_monitor(
        monitor_type: str,
        query: str,
        days_back: int = 0,
        page: int = 1,
    ) -> str:
        """Check daily domain changes on an IP, name server, or IP registrant."""
        mtype = (monitor_type or "").strip().lower()
        if mtype not in _INFRA_MONITORS:
            return (
                "Error: monitor_type must be one of "
                f"{', '.join(sorted(_INFRA_MONITORS))}."
            )
        if not (query or "").strip():
            return "Error: 'query' is required (IP address, name server hostname, or registrant terms)."
        params: dict[str, Any] = {"query": query.strip(), "page": max(1, int(page or 1))}
        if days_back:
            params["days_back"] = max(1, min(int(days_back), 6))
        data, err = await _api(
            "get",
            _INFRA_MONITORS[mtype],
            f"check {mtype} monitor",
            params=params,
        )
        if err:
            return err
        response = data.get("response") or {}
        if mtype == "ip_registrant":
            added = response.get("added") or []
            removed = response.get("removed") or []
            modified = response.get("modified") or []
            lines = [
                f"IP registrant monitor '{query}' for {response.get('date', 'today')}: "
                f"{response.get('record_count', '?')} record(s)"
            ]
            for label, items in (("Added", added), ("Removed", removed), ("Modified", modified)):
                if items:
                    lines.append(f"{label}:")
                    lines.extend(
                        f"  {r.get('range', '?')} - {_trunc(r.get('organization'), 50)} "
                        f"({r.get('country', '?')})"
                        for r in items[:25]
                    )
            if response.get("has_more_pages"):
                lines.append(f"[More pages available - pass page={int(page or 1) + 1}]")
            return "\n".join(lines)

        alerts = response.get("alerts") or []

        def _fmt(a: dict) -> str:
            before = a.get("previous_ip_address") or a.get("old_name_server") or "-"
            after = a.get("new_ip_address") or a.get("new_name_server") or "-"
            return f"  {a.get('domain', '?')} [{a.get('action', '?')}] {before} -> {after}"

        out = (
            f"{mtype} monitor '{query}' for {response.get('date', 'today')}:\n"
            + format_entity_list(
                alerts,
                _fmt,
                noun="domain change",
                total=int(response.get("total") or 0) or None,
                empty_msg="No domain changes matched today.",
            )
        )
        page_count = int(response.get("page_count") or 1)
        if page_count > int(page or 1):
            out += f"\n[More pages available - pass page={int(page or 1) + 1} of {page_count}]"
        return out

    # ══════════════════════════════════════════════════════════════════════
    # 6. IRIS DETECT (lookalike-domain defense)
    # ══════════════════════════════════════════════════════════════════════

    async def list_domaintools_detect_monitors(
        include_counts: bool = False,
        datetime_counts_since: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> str:
        """List Iris Detect monitors (lookalike-domain terms) with their IDs."""
        params: dict[str, Any] = {
            "offset": max(0, int(offset or 0)),
            "limit": max(1, min(int(limit or 50), 100 if include_counts else 500)),
        }
        if include_counts:
            if not datetime_counts_since:
                return (
                    "Error: datetime_counts_since is required when "
                    "include_counts=true (ISO-8601 datetime or negative seconds)."
                )
            params["include_counts"] = "true"
            params["datetime_counts_since"] = datetime_counts_since
        data, err = await _api(
            "get", "/v1/iris-detect/monitors/", "list Detect monitors", params=params
        )
        if err:
            return err
        monitors = data.get("monitors") or []

        def _fmt(m: dict) -> str:
            counts = m.get("domain_counts") or {}
            counts_str = (
                f" | new: {counts.get('new', 0)}, watched: {counts.get('watched', 0)}, "
                f"changed: {counts.get('changed', 0)}, escalated: {counts.get('escalated', 0)}"
                if counts
                else ""
            )
            return (
                f"  [{m.get('id', '?')}] '{m.get('term', '?')}' "
                f"({m.get('state', '?')}/{m.get('status', '?')})"
                f" | substrings: {m.get('match_substring_variations', False)}{counts_str}"
            )

        out = format_entity_list(
            monitors,
            _fmt,
            noun="Detect monitor",
            total=data.get("total_count"),
            empty_msg="No Iris Detect monitors configured.",
        )
        total = data.get("total_count") or 0
        if total > offset + len(monitors):
            out += f"\n[More monitors available - pass offset={offset + len(monitors)}]"
        return out

    async def list_domaintools_detect_domains(
        state: str = "new",
        monitor_id: str = "",
        risk_score_ranges: str = "",
        tlds: str = "",
        mx_exists: str = "",
        discovered_since: str = "",
        changed_since: str = "",
        escalated_since: str = "",
        search: str = "",
        sort: str = "",
        order: str = "",
        include_domain_data: bool = False,
        offset: int = 0,
        limit: int = 25,
        preview: bool = False,
    ) -> str:
        """List Iris Detect lookalike domains (new, watched, or ignored)."""
        st = (state or "new").strip().lower()
        if st not in _DETECT_STATES:
            return f"Error: state must be one of {', '.join(sorted(_DETECT_STATES))}."
        max_limit = 50 if include_domain_data else 100
        params: dict[str, Any] = {
            "offset": max(0, int(offset or 0)),
            "limit": max(1, min(int(limit or 25), max_limit)),
        }
        if monitor_id:
            params["monitor_id"] = monitor_id
        if risk_score_ranges:
            params["risk_score_ranges[]"] = [
                r.strip() for r in risk_score_ranges.split(",") if r.strip()
            ]
        if tlds:
            params["tlds[]"] = [t.strip().lstrip(".") for t in tlds.split(",") if t.strip()]
        if mx_exists in ("true", "false"):
            params["mx_exists"] = mx_exists
        if discovered_since:
            params["discovered_since"] = discovered_since
        if changed_since:
            params["changed_since"] = changed_since
        if escalated_since:
            params["escalated_since"] = escalated_since
        if search:
            params["search"] = search
        if sort:
            params["sort[]"] = sort
        if order in ("asc", "desc"):
            params["order"] = order
        if include_domain_data:
            params["include_domain_data"] = "true"
        if preview:
            params["preview"] = 1
        data, err = await _api(
            "get",
            _DETECT_STATES[st],
            f"list Detect {st} domains",
            params=params,
            timeout=_IRIS_TIMEOUT,
        )
        if err:
            return err
        domains_found = data.get("watchlist_domains") or []
        out = format_entity_list(
            domains_found,
            _fmt_detect_domain,
            noun=f"{st} lookalike domain",
            total=data.get("total_count"),
            empty_msg=f"No {st} lookalike domains found.",
        )
        total = data.get("total_count") or 0
        if total > offset + len(domains_found):
            out += f"\n[More domains available - pass offset={offset + len(domains_found)}]"
        return out

    async def manage_domaintools_detect(
        action: str,
        term: str = "",
        monitor_id: str = "",
        domain_ids: str = "",
        escalation_type: str = "",
        match_substring_variations: bool | None = None,
        text_exclusions: str = "",
        nameserver_exclusions: str = "",
    ) -> str:
        """Manage Iris Detect: monitors CRUD, domain triage, and escalations."""
        act = (action or "").strip().lower()
        if act not in _DETECT_ACTIONS:
            return (
                f"Error: unknown action '{action}'. Supported actions: "
                f"{', '.join(sorted(_DETECT_ACTIONS))}."
            )

        if act == "create_monitor":
            if len((term or "").strip()) < 3:
                return "Error: create_monitor requires 'term' (at least 3 characters)."
            body: dict[str, Any] = {"term": term.strip()}
            if match_substring_variations is not None:
                body["match_substring_variations"] = match_substring_variations
            if text_exclusions:
                body["text_exclusions"] = [
                    t.strip() for t in text_exclusions.split(",") if t.strip()
                ]
            if nameserver_exclusions:
                body["nameserver_exclusions"] = [
                    n.strip() for n in nameserver_exclusions.split(",") if n.strip()
                ]
            data, err = await _api(
                "post", "/v1/iris-detect/monitors/", "create Detect monitor", json_body=body
            )
            if err:
                return err
            monitor = data.get("monitor") or {}
            return (
                f"Detect monitor created: [{monitor.get('id', '?')}] "
                f"'{monitor.get('term', term)}' (status: {monitor.get('status', '?')}; "
                "initial discovery runs shortly)."
            )

        if act == "update_monitor":
            if not monitor_id.strip():
                return "Error: update_monitor requires 'monitor_id'."
            # Tri-state: only an explicitly provided value is sent, so
            # update_monitor can also DISABLE substring matching (False must
            # be distinguishable from "not provided").
            body = {}
            if match_substring_variations is not None:
                body["match_substring_variations"] = match_substring_variations
            if text_exclusions:
                body["text_exclusions"] = [
                    t.strip() for t in text_exclusions.split(",") if t.strip()
                ]
            if nameserver_exclusions:
                body["nameserver_exclusions"] = [
                    n.strip() for n in nameserver_exclusions.split(",") if n.strip()
                ]
            if not body:
                return (
                    "Error: update_monitor requires at least one of "
                    "match_substring_variations, text_exclusions, nameserver_exclusions."
                )
            data, err = await _api(
                "put",
                "/v1/iris-detect/monitors/",
                f"update Detect monitor {monitor_id}",
                params={"monitor_id": monitor_id.strip()},
                json_body=body,
            )
            if err:
                return err
            return f"Detect monitor {monitor_id} updated."

        if act == "delete_monitor":
            if not monitor_id.strip():
                return "Error: delete_monitor requires 'monitor_id'."
            _, err = await _api(
                "delete",
                "/v1/iris-detect/monitors/",
                f"delete Detect monitor {monitor_id}",
                params={"monitor_id": monitor_id.strip()},
            )
            if err:
                return err
            return (
                f"Detect monitor {monitor_id} permanently deleted - its domain "
                "tracking has stopped (this cannot be undone)."
            )

        ids = [i.strip() for i in (domain_ids or "").split(",") if i.strip()]
        if not ids:
            return f"Error: {act} requires 'domain_ids' (comma-separated Detect domain IDs)."

        if act in ("watch_domains", "ignore_domains"):
            new_state = "watched" if act == "watch_domains" else "ignored"
            data, err = await _api(
                "patch",
                "/v1/iris-detect/domains/",
                f"set Detect domains to {new_state}",
                json_body={"watchlist_domain_ids": ids, "state": new_state},
            )
            if err:
                return err
            updated = data.get("watchlist_domains") or []
            names = ", ".join(d.get("domain", d.get("id", "?")) for d in updated[:20])
            return f"{len(updated)} domain(s) set to '{new_state}': {names}"

        # escalate_domains
        esc = (escalation_type or "").strip().lower()
        if esc not in _ESCALATION_TYPES:
            return (
                "Error: escalate_domains requires escalation_type "
                f"({' or '.join(sorted(_ESCALATION_TYPES))}). 'blocked' marks the "
                "domains for internal blocking; 'google_safe' submits them to "
                "Google Web Risk (max 10 per request)."
            )
        if esc == "google_safe" and len(ids) > 10:
            return "Error: Google Web Risk escalations accept at most 10 domains per request."
        if len(ids) > 100:
            return "Error: escalations accept at most 100 domains per request."
        data, err = await _api(
            "post",
            "/v1/iris-detect/escalations/",
            f"escalate Detect domains ({esc})",
            json_body={"watchlist_domain_ids": ids, "escalation_type": esc},
        )
        if err:
            return err
        escalations = data.get("escalations") or []
        return (
            f"{len(escalations)} domain(s) escalated as '{esc}'. Escalated domains "
            "are also added to the watchlist."
        )

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("get_domaintools_account", get_domaintools_account)
    executor.register("get_domaintools_domain_intel", get_domaintools_domain_intel)
    executor.register("search_domaintools_iris", search_domaintools_iris)
    executor.register("get_domaintools_enrichment", get_domaintools_enrichment)
    executor.register("get_domaintools_risk_score", get_domaintools_risk_score)
    executor.register("get_domaintools_reputation", get_domaintools_reputation)
    executor.register("get_domaintools_domain_profile", get_domaintools_domain_profile)
    executor.register("get_domaintools_whois", get_domaintools_whois)
    executor.register("get_domaintools_parsed_whois", get_domaintools_parsed_whois)
    executor.register("get_domaintools_whois_history", get_domaintools_whois_history)
    executor.register("get_domaintools_hosting_history", get_domaintools_hosting_history)
    executor.register("get_domaintools_domain_history", get_domaintools_domain_history)
    executor.register("search_domaintools_reverse_ip", search_domaintools_reverse_ip)
    executor.register(
        "search_domaintools_reverse_ip_whois", search_domaintools_reverse_ip_whois
    )
    executor.register(
        "search_domaintools_reverse_nameserver", search_domaintools_reverse_nameserver
    )
    executor.register("search_domaintools_reverse_whois", search_domaintools_reverse_whois)
    executor.register("check_domaintools_brand_monitor", check_domaintools_brand_monitor)
    executor.register(
        "check_domaintools_registrant_monitor", check_domaintools_registrant_monitor
    )
    executor.register(
        "check_domaintools_infrastructure_monitor",
        check_domaintools_infrastructure_monitor,
    )
    executor.register(
        "list_domaintools_detect_monitors", list_domaintools_detect_monitors
    )
    executor.register("list_domaintools_detect_domains", list_domaintools_detect_domains)
    executor.register("manage_domaintools_detect", manage_domaintools_detect)

    return [
        ToolDefinition(
            name="get_domaintools_account",
            description=(
                "Report the DomainTools API products licensed on this account with "
                "their monthly quotas, usage, and expiration dates. Use first when "
                "other DomainTools tools fail (403 usually means the product is "
                "not licensed)."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="get_domaintools_domain_intel",
            description=(
                "Enrich one or more domains (comma-separated, up to 100) via Iris "
                "Investigate: risk score with threat components (phishing/malware/"
                "spam/proximity), registration data, IPs, name/mail servers, SSL, "
                "and website title. For a single domain it also surfaces guided "
                "pivots (attributes shared by few other domains) to feed into "
                "search_domaintools_iris. The primary domain investigation tool."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "domains": {
                        "type": "string",
                        "description": "Comma-separated apex domains (e.g. 'example.com,example.net'), max 100",
                    },
                    "position": {
                        "type": "string",
                        "description": "Pagination cursor from a previous response",
                    },
                },
                "required": ["domains"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_domaintools_iris",
            description=(
                "Pivot/reverse search Iris Investigate: find domains connected to "
                "an IP, email, name server, mail server, registrant, registrar, "
                "SSL hash, redirect target, Google Analytics code, WHOIS text, "
                "Iris tag, or an Iris UI search_hash. Multiple parameters combine "
                "as AND. Refine with tld/create_date/active/risk_score_min/"
                "first_seen filters; paginate with position. Use after "
                "get_domaintools_domain_intel surfaces a low-count pivot, or to "
                "monitor newly active domains (first_seen_within in seconds)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ip": {"type": "string", "description": "IPv4 address the domains resolve to"},
                    "email": {"type": "string", "description": "Email from WHOIS, DNS SOA, or SSL"},
                    "email_domain": {"type": "string", "description": "Apex domain of a WHOIS/SOA email"},
                    "nameserver_host": {"type": "string", "description": "Full name server hostname (ns1.example.net)"},
                    "nameserver_domain": {"type": "string", "description": "Registered domain of the name server (example.net)"},
                    "mailserver_host": {"type": "string", "description": "Full mail server hostname"},
                    "mailserver_domain": {"type": "string", "description": "Registered domain of the mail server"},
                    "registrant": {"type": "string", "description": "Substring match on the WHOIS registrant"},
                    "registrant_org": {"type": "string", "description": "Substring match on the registrant organization"},
                    "registrar": {"type": "string", "description": "Exact match on the WHOIS registrar"},
                    "ssl_hash": {"type": "string", "description": "SSL certificate SHA-1 hash"},
                    "redirect_domain": {"type": "string", "description": "Domains observed redirecting to this domain"},
                    "google_analytics": {"type": "string", "description": "Google Analytics tracking code"},
                    "whois": {"type": "string", "description": "Free-text search of the most recent WHOIS record"},
                    "tagged_with_any": {"type": "string", "description": "Comma-separated Iris tags (OR logic)"},
                    "search_hash": {"type": "string", "description": "Encoded search exported from the Iris Investigate UI"},
                    "tld": {"type": "string", "description": "Limit to a TLD (e.g. 'com')"},
                    "create_date": {"type": "string", "description": "Only domains created on this date (YYYY-MM-DD)"},
                    "active": {"type": "string", "enum": ["true", "false"], "description": "Filter by active DNS/registry status"},
                    "risk_score_min": {"type": "integer", "default": 0, "description": "Only domains with risk score above this value (0-100)"},
                    "first_seen_since": {"type": "string", "description": "Domains first seen on/after this ISO-8601 datetime"},
                    "first_seen_within": {"type": "integer", "default": 0, "description": "Domains first seen within the last N seconds (e.g. 86400)"},
                    "ip_country_code": {"type": "string", "description": "Country code of the IP A-record"},
                    "sort_by": {
                        "type": "string",
                        "enum": ["first_seen_since", "create_date", "domain", "risk_score"],
                        "description": "Sort field (default: first_seen_since descending)",
                    },
                    "sort_direction": {
                        "type": "string",
                        "enum": ["asc", "desc"],
                        "description": "Sort order (normalized to the API's 'dsc' token for descending)",
                    },
                    "page_size": {"type": "integer", "default": 25, "description": "Results per page (max 500)"},
                    "position": {"type": "string", "description": "Pagination cursor from a previous response"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_domaintools_enrichment",
            description=(
                "Bulk-enrich up to 100 domains via Iris Enrich (same attributes "
                "as Iris Investigate but on an independent high-volume quota, no "
                "pivot counts). Prefer this over get_domaintools_domain_intel "
                "when enriching many domains from alerts/logs and Iris Enrich is "
                "licensed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "domains": {
                        "type": "string",
                        "description": "Comma-separated apex domains, max 100",
                    },
                },
                "required": ["domains"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="get_domaintools_risk_score",
            description=(
                "Get a domain's Domain Risk Score (0=benign, 100=known threat) "
                "with component scores (proximity + threat profile classifiers "
                "for phishing/malware/spam). Set include_evidence=true for the "
                "evidence behind each component (slower, lower rate limit). Use "
                "for fast triage of a suspicious domain."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "Apex domain (e.g. example.com)"},
                    "include_evidence": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include per-component evidence (uses /v1/risk/evidence/)",
                    },
                },
                "required": ["domain"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_domaintools_reputation",
            description=(
                "Get a domain's legacy Domain Reputation score with reasons "
                "(blocklist, dns, realtime, registrant, zerolist). Use "
                "get_domaintools_risk_score first - this endpoint is the older "
                "reputation product some accounts are licensed for instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "Apex domain"},
                    "include_reasons": {"type": "boolean", "default": True},
                },
                "required": ["domain"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="get_domaintools_domain_profile",
            description=(
                "Get a domain's registration snapshot: registrant (+ how many "
                "domains they own), registration dates/registrar/statuses, "
                "hosting IP (+ co-hosted domain count), name servers, history "
                "record counts, and basic website data. A quick orientation "
                "lookup - use get_domaintools_domain_intel for full intel."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "Apex domain (e.g. example.com)"},
                },
                "required": ["domain"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="get_domaintools_whois",
            description=(
                "Get the most recent WHOIS record for a domain name or IP "
                "address: registrant, registration dates, registrar, statuses, "
                "name servers, and the raw record text."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Apex domain or IP address"},
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_domaintools_parsed_whois",
            description=(
                "Get parsed and normalized WHOIS fields for a domain or IP: "
                "registrar detail, per-role contacts (registrant/admin/tech/"
                "billing) with emails, and for IPs the network ranges and "
                "contacts. Use when you need structured WHOIS fields instead of "
                "the raw record."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Apex domain or IP address"},
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="get_domaintools_whois_history",
            description=(
                "Get historical WHOIS records for a domain, newest first by "
                "default - the ownership timeline (registrant/registrar/name "
                "server changes across registration lifecycles). Records are "
                "billed per record retrieved: keep limit small and use "
                "mode='count' to check volume for free first."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "Apex domain"},
                    "sort": {"type": "string", "enum": ["date_asc", "date_desc"], "default": "date_desc"},
                    "limit": {"type": "integer", "default": 10, "description": "Records per page (max 100)"},
                    "offset": {"type": "integer", "default": 0, "description": "Records to skip (pagination)"},
                    "mode": {
                        "type": "string",
                        "enum": ["list", "count", "check_existence"],
                        "default": "list",
                        "description": "'count'/'check_existence' are free and do not return records",
                    },
                },
                "required": ["domain"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_domaintools_hosting_history",
            description=(
                "Get a domain's hosting change events: IP address changes and "
                "name server transfers with before/after values and dates. Use "
                "to spot infrastructure moves (e.g. a domain switching to "
                "bulletproof hosting before a campaign)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "Apex domain"},
                },
                "required": ["domain"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_domaintools_domain_history",
            description=(
                "Get granular change events for a domain (what changed, when, "
                "before/after) across infrastructure, registration, SSL, web "
                "content, and trackers - since October 2021. Filter with "
                "include_fields OR exclude_fields (comma-separated exact field "
                "names; aggregates like all_ssl, all_ip, all_nameserver, "
                "all_web_trackers are supported). Complements hosting history "
                "with much finer granularity."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "Apex domain"},
                    "include_fields": {
                        "type": "string",
                        "description": "Only these change fields (e.g. 'ip,registrar' or 'all_ssl')",
                    },
                    "exclude_fields": {
                        "type": "string",
                        "description": "Exclude these change fields (e.g. 'all_web_trackers')",
                    },
                    "page_size": {"type": "integer", "default": 25, "description": "Events per page (max 100)"},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": ["domain"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="search_domaintools_reverse_ip",
            description=(
                "List domains sharing an IP address (reverse IP). Pass an IP "
                "address for predictable results, or a domain to look up the "
                "domains co-hosted on its IPs. Results are a tracked subset - "
                "not exhaustive for large CDN/cloud IPs (use search_domaintools_"
                "iris with the ip parameter for current, comprehensive data)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "IP address (preferred) or apex domain"},
                    "limit": {"type": "integer", "default": 50, "description": "Max domains per IP"},
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_domaintools_reverse_ip_whois",
            description=(
                "Find the IP ranges owned by an organization (query='google inc' "
                "with +required/-excluded/prefix* term modifiers), or get the "
                "cached IP WHOIS record for a single IP (ip=...). Use for "
                "attribution and mapping an organization's address space."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Free-text org terms (for range lists)"},
                    "ip": {"type": "string", "description": "Single IP (returns its allocated range's WHOIS)"},
                    "country": {"type": "string", "description": "ISO 3166-1 two-letter country filter"},
                    "server": {
                        "type": "string",
                        "enum": [
                            "whois.arin.net",
                            "whois.apnic.net",
                            "whois.ripe.net",
                            "whois.lacnic.net",
                            "whois.afrinic.net",
                        ],
                        "description": "Limit to one regional WHOIS server",
                    },
                    "page": {"type": "integer", "default": 1, "description": "Page (1000 ranges/page, max 5)"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="search_domaintools_reverse_nameserver",
            description=(
                "List domains that share a name server (reverse NS) - domains "
                "pointing at the same primary/secondary name server hostname. "
                "Use to expand from a suspicious name server to its client "
                "domains."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hostname": {"type": "string", "description": "Name server hostname or its registered domain"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["hostname"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_domaintools_reverse_whois",
            description=(
                "Find domains whose WHOIS records contain the given owner terms "
                "(reverse WHOIS) - e.g. a company name, email address, or "
                "address fragment. Separate multiple required terms with the "
                "pipe character ('Acme Corp|Seattle'). scope='historic' also "
                "searches past records; mode='quote' returns only the count "
                "(useful to gauge size before pulling the full list)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "terms": {"type": "string", "description": "Pipe-separated required terms"},
                    "exclude": {"type": "string", "description": "Pipe-separated exclusion terms"},
                    "scope": {"type": "string", "enum": ["current", "historic"], "default": "current"},
                    "mode": {"type": "string", "enum": ["quote", "purchase"], "default": "purchase"},
                },
                "required": ["terms"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="check_domaintools_brand_monitor",
            description=(
                "Check today's new domain registrations (and on-hold deletions) "
                "containing a brand term across all monitored TLDs (Brand "
                "Monitor). Use the pipe character as logical AND ('domain|"
                "tools'). days_back (1-6) catches up on missed days. Each call "
                "consumes quota even with zero matches - ideal for a daily "
                "scheduled assignment."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Brand term(s); | = logical AND"},
                    "exclude": {"type": "string", "description": "Terms to exclude (| = logical AND)"},
                    "domain_status": {"type": "string", "enum": ["new", "on-hold"], "description": "Limit to one status (default: both)"},
                    "days_back": {"type": "integer", "default": 0, "description": "Search up to 6 days back"},
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="check_domaintools_registrant_monitor",
            description=(
                "Check today's WHOIS ownership changes matching a registrant "
                "term (Registrant Monitor) - alerts when the term appears in "
                "(added) or disappears from (removed) newly updated WHOIS "
                "records. Use for monitoring an organization's domain "
                "registrations or an actor's registrant fingerprints."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Registrant term(s), pipe-separated"},
                    "exclude": {"type": "string"},
                    "days_back": {"type": "integer", "default": 0, "description": "Search up to 6 days back"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="check_domaintools_infrastructure_monitor",
            description=(
                "Check today's domain changes on monitored infrastructure: "
                "monitor_type='ip' (domains added/removed/transferred on an IP "
                "address), 'name_server' (same for a name server hostname), or "
                "'ip_registrant' (IP ranges whose WHOIS matches registrant "
                "terms). days_back (1-6) catches up on missed days; page for "
                ">1000 results."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "monitor_type": {
                        "type": "string",
                        "enum": ["ip", "name_server", "ip_registrant"],
                    },
                    "query": {
                        "type": "string",
                        "description": "IP address, name server hostname, or registrant terms (per monitor_type)",
                    },
                    "days_back": {"type": "integer", "default": 0, "description": "Search up to 6 days back"},
                    "page": {"type": "integer", "default": 1},
                },
                "required": ["monitor_type", "query"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_domaintools_detect_monitors",
            description=(
                "List Iris Detect monitors (the brand/keyword terms watched for "
                "lookalike domains) with their IDs, states, and optional per-"
                "monitor domain counts. Monitor IDs feed "
                "list_domaintools_detect_domains and manage_domaintools_detect."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "include_counts": {"type": "boolean", "default": False, "description": "Include new/watched/changed/escalated counts"},
                    "datetime_counts_since": {
                        "type": "string",
                        "description": "Required with include_counts: ISO-8601 datetime OR negative seconds (e.g. '-86400')",
                    },
                    "offset": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_domaintools_detect_domains",
            description=(
                "List Iris Detect lookalike domains by state: 'new' (recently "
                "discovered, awaiting triage), 'watched' (tracked for DNS/WHOIS "
                "changes), or 'ignored' (false positives). Filter by monitor_id, "
                "risk score ranges, TLDs, MX presence, discovery/change/"
                "escalation windows, or a name substring. NOTE: the new/watched "
                "endpoints are rate-limited to 1 query/hour (pagination requests "
                "exempt; preview=true allows testing). Returns the domain IDs "
                "needed by manage_domaintools_detect."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "state": {"type": "string", "enum": ["new", "watched", "ignored"], "default": "new"},
                    "monitor_id": {"type": "string", "description": "Limit to one monitor (from list_domaintools_detect_monitors)"},
                    "risk_score_ranges": {
                        "type": "string",
                        "description": "Comma-separated ranges: 0-0, 1-39, 40-69, 70-99, 100-100",
                    },
                    "tlds": {"type": "string", "description": "Comma-separated TLD filters (e.g. 'com,net')"},
                    "mx_exists": {"type": "string", "enum": ["true", "false"], "description": "Filter on MX record presence (phishing-readiness signal)"},
                    "discovered_since": {"type": "string", "description": "ISO-8601 datetime or negative seconds"},
                    "changed_since": {"type": "string", "description": "ISO-8601 datetime or negative seconds (watched domains)"},
                    "escalated_since": {"type": "string", "description": "ISO-8601 datetime or negative seconds"},
                    "search": {"type": "string", "description": "Substring match on the domain name"},
                    "sort": {"type": "string", "enum": ["discovered_date", "changed_date", "risk_score"]},
                    "order": {"type": "string", "enum": ["asc", "desc"]},
                    "include_domain_data": {"type": "boolean", "default": False, "description": "Include DNS + WHOIS/RDAP detail (halves the max limit)"},
                    "offset": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": 25, "description": "Max 100 (50 with include_domain_data)"},
                    "preview": {"type": "boolean", "default": False, "description": "Testing only: 2 results, bypasses the hourly limit"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_domaintools_detect",
            description=(
                "Manage Iris Detect lookalike-domain defense. Actions: "
                "'create_monitor' (term, optional match_substring_variations/"
                "text_exclusions/nameserver_exclusions), 'update_monitor' "
                "(monitor_id + fields to change), 'delete_monitor' (monitor_id - "
                "PERMANENT, stops tracking, cannot be undone), 'watch_domains' / "
                "'ignore_domains' (domain_ids - triage new discoveries), and "
                "'escalate_domains' (domain_ids + escalation_type: 'blocked' "
                "marks for internal blocking, 'google_safe' submits to Google "
                "Web Risk for global browser blocking - max 10 per request). "
                "Requires the corresponding Detect permissions on the API "
                "account."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(_DETECT_ACTIONS),
                    },
                    "term": {"type": "string", "description": "create_monitor: keyword to monitor (min 3 chars)"},
                    "monitor_id": {"type": "string", "description": "update_monitor / delete_monitor target"},
                    "domain_ids": {
                        "type": "string",
                        "description": "Comma-separated Detect domain IDs (from list_domaintools_detect_domains)",
                    },
                    "escalation_type": {"type": "string", "enum": ["blocked", "google_safe"]},
                    "match_substring_variations": {
                        "type": "boolean",
                        "description": "Enable or disable substring-variation matching; omit to leave the monitor's current setting unchanged",
                    },
                    "text_exclusions": {"type": "string", "description": "Comma-separated strings to exclude from matches"},
                    "nameserver_exclusions": {"type": "string", "description": "Comma-separated NS patterns to exclude (wildcards allowed)"},
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
