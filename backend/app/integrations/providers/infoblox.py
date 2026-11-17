# SPDX-License-Identifier: Apache-2.0

"""Infoblox (BloxOne) integration - Agentic DDI + Threat Defense.

Full-fledged Infoblox BloxOne / Cloud Services Portal (CSP) integration covering
the DDI (DNS / DHCP / IPAM) source-of-truth plus BloxOne Threat Defense, designed
as a best-in-class building block for an agentic network-security team:

  - IPAM (*network source of truth*): IP spaces, subnets (+ detail), address
    records, ranges, and IPAM hosts
  - DNS: authoritative zones, resource records, and DNS views
  - Threat Defense (*detection + intelligence*): TIDE threat-intel IOC lookup
    (host / ip / url), security policies, custom/named lists, and a consolidated
    custom-list manager to create lists and add/remove IOCs for blocking
  - Connectivity / status

Authentication
--------------
Infoblox CSP authenticates with a user/service **API key** sent in the
``Authorization`` header with the ``Token`` prefix (``Authorization: Token <key>``
- note: NOT ``Bearer``). ``integration_tools._get_token`` assembles the compound
``base_url||api_key`` token this builder parses (base URL = the CSP host, default
``https://csp.infoblox.com``). DDI calls target ``{base_url}/api/ddi/v1``, Threat
Defense firewall calls ``{base_url}/api/atcfw/v1``, and TIDE threat intel
``{base_url}/tide/api/data``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.services.branding import get_cached_branding
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "Infoblox"
_SOURCE = "integration:infoblox"

# Default Infoblox Cloud Services Portal host. A stored base_url overrides this
# (e.g. a dedicated CSP tenant URL).
_DEFAULT_BASE_URL = "https://csp.infoblox.com"

# API path prefixes (all relative to the CSP host root).
_DDI = "/api/ddi/v1"  # Universal DDI: IPAM + DNS
_ATCFW = "/api/atcfw/v1"  # BloxOne Threat Defense cloud firewall
_TIDE = "/tide/api/data"  # Threat Intelligence Data Exchange

# Upper bound on the number of items requested in a single paginated call. Caps
# a caller-supplied limit so an unbounded value can't request an enormous
# response or stress the CSP.
_MAX_LIMIT = 1000

# Valid TIDE indicator data types.
_TIDE_TYPES: frozenset[str] = frozenset({"host", "ip", "url"})

# Valid actions for the consolidated custom-list manager.
_LIST_ACTIONS: frozenset[str] = frozenset({"create", "add_items", "remove_items"})

# TIDE threat_level is 0..100; map it to a named band + icon for at-a-glance triage.
_THREAT_ICONS: dict[str, str] = {
    "critical": "\U0001f534",  # red circle
    "high": "\U0001f7e0",  # orange circle
    "medium": "\U0001f7e1",  # yellow circle
    "low": "\U0001f535",  # blue circle
    "info": "\u26aa",  # white circle
}


def _tl_band(level: Any) -> str:
    try:
        n = int(level)
    except (TypeError, ValueError):
        return "info"
    if n >= 80:
        return "critical"
    if n >= 60:
        return "high"
    if n >= 40:
        return "medium"
    if n >= 1:
        return "low"
    return "info"


def _tl_icon(level: Any) -> str:
    return _THREAT_ICONS.get(_tl_band(level), "\u26aa")


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("\u2026" if len(text) > max_len else "")


def _tags(value: Any, limit: int = 5) -> str:
    """Render an Infoblox ``tags`` dict (``{key: value}``) compactly."""
    if not isinstance(value, dict) or not value:
        return ""
    items = [f"{k}={v}" for k, v in list(value.items())[:limit]]
    extra = f" (+{len(value) - limit})" if len(value) > limit else ""
    return ", ".join(items) + extra


def _results(data: Any) -> list:
    """Return the ``results`` collection from a Universal DDI / atcfw envelope."""
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        return data["results"]
    return []


def _clamp(limit: Any, default: int = 25) -> int:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = default
    return max(1, min(limit, _MAX_LIMIT))


def _eq_filter(pairs: dict[str, str]) -> str:
    """Build a DDI ``_filter`` expression of AND-joined equality clauses.

    Infoblox ``_filter`` uses expressions like ``address=="10.0.0.1"``; string
    literals are double-quoted. Embedded quotes are stripped defensively so a
    value can't break out of the clause.
    """
    clauses = [f'{field}=="{str(value).replace(chr(34), "")}"' for field, value in pairs.items() if value]
    return " and ".join(clauses)


# ---------------------------------------------------------------------------
# Auth + URL helpers (shared with integration_testing.py)
# ---------------------------------------------------------------------------


def infoblox_api_base(base_url: str) -> str:
    """Return the Infoblox CSP base URL (host root, no trailing slash).

    Per-API prefixes (``/api/ddi/v1``, ``/api/atcfw/v1``, ``/tide/api/data``) are
    appended by the individual tools, so this returns just the host.
    """
    return (base_url or _DEFAULT_BASE_URL).rstrip("/")


def build_infoblox_headers(api_key: str | None) -> dict[str, str]:
    """Build Infoblox CSP request headers.

    Infoblox authenticates with the API key in the ``Authorization`` header using
    the ``Token`` prefix (NOT ``Bearer``). The key is stripped and the
    ``Authorization`` header omitted entirely when blank/whitespace-only:
    building ``"Token "`` (with a trailing space) yields an httpx/h11
    ``LocalProtocolError: Illegal header value`` that surfaces as an opaque 500
    instead of a clean 401 - the same failure mode the shared
    ``bearer_auth_headers`` guard protects against for ``Bearer`` tokens. With
    the header omitted, a missing key cleanly falls through to the API's 401.
    """
    headers: dict[str, str] = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    key = (api_key or "").strip()
    if key:
        headers["Authorization"] = f"Token {key}"
    return headers


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Infoblox tools from a ``base_url||api_key`` compound token."""
    base_url, api_key = parse_compound_token(token, 2)
    # ``parse_compound_token`` does not strip its segments, so a whitespace-only
    # api_key would otherwise pass the emptiness check below and register
    # unusable tools (and build an illegal ``Token `` header). Normalise both and
    # re-check so a blank credential yields no tools.
    base_url = (base_url or "").strip()
    api_key = (api_key or "").strip()
    if not api_key:
        return []

    api = infoblox_api_base(base_url)
    headers = build_infoblox_headers(api_key)

    def _client(timeout: int = 30) -> httpx.AsyncClient:
        return egress_client(timeout=timeout)

    def _page(
        limit: int,
        offset: int = 0,
        filter_expr: str = "",
        tag_filter: str = "",
        **extra: Any,
    ) -> dict[str, Any]:
        """Build the standard Universal DDI query params (paging + filtering)."""
        params: dict[str, Any] = {"_limit": _clamp(limit), "_offset": max(0, int(offset or 0))}
        if filter_expr:
            params["_filter"] = filter_expr
        if tag_filter:
            params["_tfilter"] = tag_filter
        for key, value in extra.items():
            if value:
                params[key] = value
        return params

    # =====================================================================
    # 0. CONNECTION
    # =====================================================================

    async def get_infoblox_status() -> str:
        """Validate the Infoblox CSP connection and list visible IP spaces."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}{_DDI}/ipam/ip_space",
                _PROVIDER,
                "get IP spaces",
                headers=headers,
                params={"_limit": 5},
            )
            if err:
                return err
            spaces = _results(resp.json())
        names = ", ".join(s.get("name", "?") for s in spaces[:5]) or "none visible"
        return f"Connected to Infoblox CSP at {api}.\n  IP spaces visible: {names}"

    # =====================================================================
    # 1. IPAM (network source of truth)
    # =====================================================================

    async def list_infoblox_ip_spaces(limit: int = 50) -> str:
        """List IPAM IP spaces (top-level address containers / network views)."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}{_DDI}/ipam/ip_space",
                _PROVIDER,
                "list IP spaces",
                headers=headers,
                params=_page(limit),
            )
            if err:
                return err
            spaces = _results(resp.json())

        def _fmt(s: dict) -> str:
            tags = _tags(s.get("tags"))
            return (
                f"  {s.get('name', '?')} (id: {s.get('id', '?')})"
                f"{' - ' + _trunc(s.get('comment'), 80) if s.get('comment') else ''}"
                f"{chr(10) + '    Tags: ' + tags if tags else ''}"
            )

        return format_entity_list(spaces, _fmt, noun="IP space")

    async def list_infoblox_subnets(address: str = "", subnet_filter: str = "", limit: int = 25) -> str:
        """List IPAM subnets. Filter by network address or a raw Infoblox _filter."""
        filter_expr = subnet_filter or _eq_filter({"address": address})
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}{_DDI}/ipam/subnet",
                _PROVIDER,
                "list subnets",
                headers=headers,
                params=_page(limit, filter_expr=filter_expr),
            )
            if err:
                return err
            subnets = _results(resp.json())

        def _fmt(s: dict) -> str:
            cidr = s.get("cidr")
            net = f"{s.get('address', '?')}/{cidr}" if cidr else s.get("address", "?")
            util = s.get("utilization") or {}
            used = util.get("used")
            total = util.get("total")
            usage = f" | Used: {used}/{total}" if total is not None else ""
            return (
                f"  {net} {('- ' + s.get('name')) if s.get('name') else ''}\n"
                f"    id: {s.get('id', '?')}{usage}"
                f"{' | ' + _trunc(s.get('comment'), 70) if s.get('comment') else ''}"
            )

        return format_entity_list(subnets, _fmt, noun="subnet")

    async def get_infoblox_subnet(subnet_id: str) -> str:
        """Get full detail of an IPAM subnet by its id (or ``ipam/subnet/UUID`` path)."""
        path = subnet_id if subnet_id.startswith("ipam/") else f"ipam/subnet/{subnet_id}"
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}{_DDI}/{path}",
                _PROVIDER,
                f"get subnet {subnet_id}",
                headers=headers,
            )
            if err:
                return err
            s = (resp.json() or {}).get("result", {})
        if not s:
            return f"Subnet {subnet_id} not found."
        cidr = s.get("cidr")
        net = f"{s.get('address', '?')}/{cidr}" if cidr else s.get("address", "?")
        util = s.get("utilization") or {}
        dhcp = s.get("dhcp_utilization") or {}
        lines = [
            f"Subnet {net}: {s.get('name') or '(unnamed)'}",
            f"id: {s.get('id', subnet_id)} | Space: {s.get('space', '?')}",
            f"Utilization: used {util.get('used', '?')} / total {util.get('total', '?')}"
            f" ({util.get('utilization', '?')}%)",
        ]
        if dhcp:
            lines.append(f"DHCP utilization: {dhcp.get('dhcp_used', '?')} / {dhcp.get('dhcp_total', '?')}")
        if s.get("comment"):
            lines.append(f"Comment: {_trunc(s.get('comment'), 200)}")
        tags = _tags(s.get("tags"), limit=10)
        if tags:
            lines.append(f"Tags: {tags}")
        return "\n".join(lines)

    async def list_infoblox_addresses(
        address: str = "", state: str = "", address_filter: str = "", limit: int = 25
    ) -> str:
        """List IPAM address records. Filter by exact address, state, or a raw _filter.

        state examples: used, free, allocated, assigned, reserved.
        """
        filter_expr = address_filter or _eq_filter({"address": address, "state": state})
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}{_DDI}/ipam/address",
                _PROVIDER,
                "list addresses",
                headers=headers,
                params=_page(limit, filter_expr=filter_expr),
            )
            if err:
                return err
            addresses = _results(resp.json())

        def _fmt(a: dict) -> str:
            names = a.get("names") or []
            name = ""
            if names:
                first = names[0]
                name = first.get("name", "") if isinstance(first, dict) else str(first)
            return (
                f"  {a.get('address', '?')} [{a.get('state', '?')}]"
                f"{' ' + name if name else ''}"
                f"{' - usage ' + ','.join(a.get('usage') or []) if a.get('usage') else ''}"
                f"{' | ' + _trunc(a.get('comment'), 60) if a.get('comment') else ''}"
            )

        return format_entity_list(addresses, _fmt, noun="address")

    async def list_infoblox_ranges(range_filter: str = "", limit: int = 25) -> str:
        """List IPAM DHCP ranges (start/end pools). Optionally pass a raw _filter."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}{_DDI}/ipam/range",
                _PROVIDER,
                "list ranges",
                headers=headers,
                params=_page(limit, filter_expr=range_filter),
            )
            if err:
                return err
            ranges = _results(resp.json())

        def _fmt(r: dict) -> str:
            return (
                f"  {r.get('start', '?')} - {r.get('end', '?')}"
                f"{' (' + r.get('name') + ')' if r.get('name') else ''}\n"
                f"    id: {r.get('id', '?')} | Space: {r.get('space', '?')}"
                f"{' | ' + _trunc(r.get('comment'), 60) if r.get('comment') else ''}"
            )

        return format_entity_list(ranges, _fmt, noun="range")

    async def list_infoblox_hosts(name: str = "", host_filter: str = "", limit: int = 25) -> str:
        """List IPAM hosts (named host objects with their associated addresses)."""
        filter_expr = host_filter or _eq_filter({"name": name})
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}{_DDI}/ipam/host",
                _PROVIDER,
                "list hosts",
                headers=headers,
                params=_page(limit, filter_expr=filter_expr),
            )
            if err:
                return err
            hosts = _results(resp.json())

        def _fmt(h: dict) -> str:
            addrs = h.get("addresses") or []
            ips = ", ".join(a.get("address", "?") if isinstance(a, dict) else str(a) for a in addrs[:5])
            return (
                f"  {h.get('name', '?')} (id: {h.get('id', '?')})\n"
                f"    Addresses: {ips or 'none'}"
                f"{' | ' + _trunc(h.get('comment'), 60) if h.get('comment') else ''}"
            )

        return format_entity_list(hosts, _fmt, noun="host")

    # =====================================================================
    # 2. DNS
    # =====================================================================

    async def list_infoblox_dns_zones(zone_filter: str = "", limit: int = 25) -> str:
        """List authoritative DNS zones. Optionally pass a raw Infoblox _filter."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}{_DDI}/dns/auth_zone",
                _PROVIDER,
                "list DNS zones",
                headers=headers,
                params=_page(limit, filter_expr=zone_filter),
            )
            if err:
                return err
            zones = _results(resp.json())

        def _fmt(z: dict) -> str:
            return (
                f"  {z.get('fqdn', z.get('protocol_fqdn', '?'))} "
                f"[{z.get('primary_type', '?')}]\n"
                f"    id: {z.get('id', '?')} | View: {z.get('view', '?')}"
                f"{' | ' + _trunc(z.get('comment'), 60) if z.get('comment') else ''}"
            )

        return format_entity_list(zones, _fmt, noun="DNS zone")

    async def list_infoblox_dns_records(
        name: str = "",
        record_type: str = "",
        record_filter: str = "",
        limit: int = 25,
    ) -> str:
        """List DNS resource records. Filter by name, type (A/AAAA/CNAME/...), or raw _filter."""
        filter_expr = record_filter or _eq_filter(
            {"name_in_zone": name, "type": record_type.upper() if record_type else ""}
        )
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}{_DDI}/dns/record",
                _PROVIDER,
                "list DNS records",
                headers=headers,
                params=_page(limit, filter_expr=filter_expr),
            )
            if err:
                return err
            records = _results(resp.json())

        def _fmt(r: dict) -> str:
            fqdn = r.get("absolute_name_spec") or r.get("name_in_zone") or "?"
            rdata = r.get("dns_rdata") or r.get("rdata") or ""
            if isinstance(rdata, dict):
                rdata = ", ".join(f"{k}={v}" for k, v in rdata.items())
            return (
                f"  {fqdn} {r.get('type', '?')} -> {_trunc(rdata, 80)}\n"
                f"    id: {r.get('id', '?')} | TTL: {r.get('ttl', 'inherited')}"
            )

        return format_entity_list(records, _fmt, noun="DNS record")

    async def list_infoblox_dns_views(limit: int = 25) -> str:
        """List DNS views (namespaces that scope which records a client resolves)."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}{_DDI}/dns/view",
                _PROVIDER,
                "list DNS views",
                headers=headers,
                params=_page(limit),
            )
            if err:
                return err
            views = _results(resp.json())

        def _fmt(v: dict) -> str:
            return (
                f"  {v.get('name', '?')} (id: {v.get('id', '?')})"
                f"{' - ' + _trunc(v.get('comment'), 80) if v.get('comment') else ''}"
            )

        return format_entity_list(views, _fmt, noun="DNS view")

    # =====================================================================
    # 3. BLOXONE THREAT DEFENSE (detection + intelligence)
    # =====================================================================

    async def lookup_infoblox_threat(indicator: str, indicator_type: str = "host", limit: int = 20) -> str:
        """Look up an IOC in Infoblox TIDE threat intelligence.

        indicator_type: host (domain/FQDN), ip, or url. Returns the threat
        records Infoblox knows for the indicator (class, property, threat level,
        first/last seen) - use to enrich an investigation or confirm a domain/IP
        is malicious before blocking it.
        """
        itype = (indicator_type or "host").strip().lower()
        if itype not in _TIDE_TYPES:
            return f"Error: indicator_type must be one of {', '.join(sorted(_TIDE_TYPES))}."
        if not indicator or not indicator.strip():
            return "Error: an indicator value is required."
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}{_TIDE}/threats/{itype}",
                _PROVIDER,
                f"lookup {itype} threat",
                headers=headers,
                params={itype: indicator.strip(), "rlimit": _clamp(limit, 20)},
            )
            if err:
                return err
            data = resp.json() or {}
        threats = data.get("threat") or []
        if not threats:
            return f"No TIDE threat records found for {itype} '{indicator}'."

        def _fmt(t: dict) -> str:
            value = t.get("host") or t.get("ip") or t.get("url") or indicator
            return (
                f"  {_tl_icon(t.get('threat_level'))} {value} "
                f"[{t.get('class', '?')}/{t.get('property', '?')}]\n"
                f"    Threat level: {t.get('threat_level', '?')} | "
                f"Type: {t.get('type', '?')} | Profile: {t.get('profile', '?')}\n"
                f"    Detected: {str(t.get('detected', ''))[:19]} | "
                f"Expiration: {str(t.get('expiration', ''))[:19]}"
            )

        return format_entity_list(
            threats,
            _fmt,
            noun="threat record",
            total=data.get("record_count"),
        )

    async def list_infoblox_security_policies(limit: int = 50) -> str:
        """List BloxOne Threat Defense security policies (DNS firewall rule sets)."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}{_ATCFW}/security_policies",
                _PROVIDER,
                "list security policies",
                headers=headers,
                params={"_limit": _clamp(limit, 50)},
            )
            if err:
                return err
            policies = _results(resp.json())

        def _fmt(p: dict) -> str:
            rules = p.get("rules") or []
            return (
                f"  {p.get('name', '?')} (id: {p.get('id', '?')})\n"
                f"    Precedence: {p.get('precedence', '?')} | "
                f"Default action: {p.get('default_action', '?')} | "
                f"Rules: {len(rules)}"
                f"{' | ' + _trunc(p.get('description'), 70) if p.get('description') else ''}"
            )

        return format_entity_list(policies, _fmt, noun="security policy")

    async def list_infoblox_custom_lists(limit: int = 50) -> str:
        """List BloxOne Threat Defense custom/named lists (IOC allow/block lists)."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}{_ATCFW}/named_lists",
                _PROVIDER,
                "list custom lists",
                headers=headers,
                params={"_limit": _clamp(limit, 50)},
            )
            if err:
                return err
            lists = _results(resp.json())

        def _fmt(nl: dict) -> str:
            return (
                f"  {nl.get('name', '?')} (id: {nl.get('id', '?')}) "
                f"[{nl.get('type', '?')}]\n"
                f"    Items: {nl.get('item_count', '?')} | "
                f"Threat level: {nl.get('threat_level', '?')}"
                f"{' | ' + _trunc(nl.get('description'), 70) if nl.get('description') else ''}"
            )

        return format_entity_list(lists, _fmt, noun="custom list")

    async def manage_infoblox_custom_list(
        action: str,
        list_id: str = "",
        name: str = "",
        items: str = "",
        description: str = "",
    ) -> str:
        """Create a custom/named list or add/remove IOCs from one (for blocking).

        action:
          - create: create a new custom list (needs name; optional comma-separated
            items to seed it).
          - add_items: add comma-separated IOCs (domains/IPs/CIDRs) to list_id.
          - remove_items: remove comma-separated IOCs from list_id.

        Adding indicators to a custom list referenced by a block policy is how the
        agent operationalises intelligence into DNS-layer blocking.
        """
        act = (action or "").strip().lower()
        if act not in _LIST_ACTIONS:
            return f"Error: action must be one of {', '.join(sorted(_LIST_ACTIONS))}."
        item_list = [i.strip() for i in items.split(",") if i.strip()]
        desc = description or f"Managed via {get_cached_branding().name}"

        if act == "create":
            if not name:
                return "Error: 'name' is required to create a custom list."
            body: dict[str, Any] = {
                "name": name,
                "description": desc,
                "type": "custom_list",
            }
            if item_list:
                body["items_described"] = [{"item": i, "description": desc} for i in item_list]
            async with _client() as client:
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{api}{_ATCFW}/named_lists",
                    _PROVIDER,
                    f"create custom list {name}",
                    headers=headers,
                    json=body,
                )
                if err:
                    return err
                created = resp.json() or {}
            obj = created.get("results") or created.get("result") or created
            new_id = obj.get("id", "?") if isinstance(obj, dict) else "?"
            return f"Custom list '{name}' created (id: {new_id}) with {len(item_list)} item(s)."

        # add_items / remove_items both target the /items sub-resource.
        if not list_id:
            return f"Error: 'list_id' is required to {act}."
        if not item_list:
            return f"Error: provide comma-separated 'items' to {act}."
        if act == "add_items":
            method = "post"
            body = {"items_described": [{"item": i, "description": desc} for i in item_list]}
        else:  # remove_items
            method = "delete"
            body = {"items": item_list}
        async with _client() as client:
            _, err = await safe_request(
                client,
                method,
                f"{api}{_ATCFW}/named_lists/{list_id}/items",
                _PROVIDER,
                f"{act} on custom list {list_id}",
                headers=headers,
                json=body,
            )
            if err:
                return err
        verb = "added to" if act == "add_items" else "removed from"
        return f"{len(item_list)} item(s) {verb} custom list {list_id}."

    # =====================================================================
    # REGISTER ALL TOOLS
    # =====================================================================

    executor.register("get_infoblox_status", get_infoblox_status)
    executor.register("list_infoblox_ip_spaces", list_infoblox_ip_spaces)
    executor.register("list_infoblox_subnets", list_infoblox_subnets)
    executor.register("get_infoblox_subnet", get_infoblox_subnet)
    executor.register("list_infoblox_addresses", list_infoblox_addresses)
    executor.register("list_infoblox_ranges", list_infoblox_ranges)
    executor.register("list_infoblox_hosts", list_infoblox_hosts)
    executor.register("list_infoblox_dns_zones", list_infoblox_dns_zones)
    executor.register("list_infoblox_dns_records", list_infoblox_dns_records)
    executor.register("list_infoblox_dns_views", list_infoblox_dns_views)
    executor.register("lookup_infoblox_threat", lookup_infoblox_threat)
    executor.register("list_infoblox_security_policies", list_infoblox_security_policies)
    executor.register("list_infoblox_custom_lists", list_infoblox_custom_lists)
    executor.register("manage_infoblox_custom_list", manage_infoblox_custom_list)

    return [
        # -- 0. Connection --------------------------------------------------
        ToolDefinition(
            name="get_infoblox_status",
            description=(
                "Validate the Infoblox CSP connection and list visible IP spaces. "
                "Use first if other Infoblox tools fail."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        # -- 1. IPAM --------------------------------------------------------
        ToolDefinition(
            name="list_infoblox_ip_spaces",
            description=(
                "List IPAM IP spaces (top-level address containers / network views) with their ids, comments, and tags."
            ),
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 50}},
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_infoblox_subnets",
            description=(
                "List IPAM subnets with their CIDR, name, space, and utilization. "
                "Filter by exact network address or pass a raw Infoblox _filter "
                "expression (e.g. 'name~\"prod\"')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": "Exact network address, e.g. 10.0.0.0",
                    },
                    "subnet_filter": {
                        "type": "string",
                        "description": "Raw Infoblox _filter (overrides address)",
                    },
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_infoblox_subnet",
            description=(
                "Get full detail of an IPAM subnet by its id (accepts the bare "
                "UUID or the full 'ipam/subnet/UUID' resource path): "
                "utilization, DHCP usage, space, comment, and tags."
            ),
            input_schema={
                "type": "object",
                "properties": {"subnet_id": {"type": "string"}},
                "required": ["subnet_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_infoblox_addresses",
            description=(
                "List IPAM address records. Filter by exact address, by state "
                "(used, free, allocated, assigned, reserved), or a raw _filter."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "Exact IP, e.g. 10.0.0.5"},
                    "state": {
                        "type": "string",
                        "description": "used, free, allocated, assigned, reserved",
                    },
                    "address_filter": {
                        "type": "string",
                        "description": "Raw Infoblox _filter (overrides address/state)",
                    },
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_infoblox_ranges",
            description="List IPAM DHCP ranges (start/end address pools). Optionally pass a raw _filter.",
            input_schema={
                "type": "object",
                "properties": {
                    "range_filter": {"type": "string", "description": "Raw Infoblox _filter"},
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_infoblox_hosts",
            description=(
                "List IPAM hosts (named host objects) and their associated addresses. Filter by name or a raw _filter."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "host_filter": {"type": "string", "description": "Raw Infoblox _filter"},
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        # -- 2. DNS ---------------------------------------------------------
        ToolDefinition(
            name="list_infoblox_dns_zones",
            description="List authoritative DNS zones (fqdn, primary type, view). Optionally pass a raw _filter.",
            input_schema={
                "type": "object",
                "properties": {
                    "zone_filter": {"type": "string", "description": "Raw Infoblox _filter"},
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_infoblox_dns_records",
            description=(
                "List DNS resource records with their type and rdata. Filter by "
                "name (relative name in zone), record_type (A, AAAA, CNAME, MX, "
                "TXT, ...), or a raw _filter."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Relative name in the zone"},
                    "record_type": {
                        "type": "string",
                        "description": "A, AAAA, CNAME, MX, TXT, NS, PTR, SRV",
                    },
                    "record_filter": {
                        "type": "string",
                        "description": "Raw Infoblox _filter (overrides name/type)",
                    },
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_infoblox_dns_views",
            description="List DNS views (namespaces that scope which records a client resolves).",
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 25}},
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        # -- 3. BloxOne Threat Defense -------------------------------------
        ToolDefinition(
            name="lookup_infoblox_threat",
            description=(
                "Look up an IOC in Infoblox TIDE threat intelligence. "
                "indicator_type: host (domain/FQDN), ip, or url. Returns known "
                "threat records (class, property, threat level, detected/expiry) "
                "to enrich an investigation or confirm an indicator is malicious "
                "before blocking it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "indicator": {
                        "type": "string",
                        "description": "The domain, IP, or URL to look up",
                    },
                    "indicator_type": {
                        "type": "string",
                        "enum": ["host", "ip", "url"],
                        "default": "host",
                    },
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["indicator"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_infoblox_security_policies",
            description="List BloxOne Threat Defense security policies (DNS firewall rule sets) with precedence, default action, and rule count.",
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 50}},
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_infoblox_custom_lists",
            description=(
                "List BloxOne Threat Defense custom/named lists (IOC allow/block "
                "lists) with their id, type, item count, and threat level. Use the "
                "id with manage_infoblox_custom_list to add/remove indicators."
            ),
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 50}},
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_infoblox_custom_list",
            description=(
                "Create a custom/named list or add/remove IOCs from one for "
                "DNS-layer blocking. action: 'create' (needs name; optional "
                "comma-separated items to seed), 'add_items' (add comma-separated "
                "domains/IPs/CIDRs to list_id), or 'remove_items' (remove them). "
                "This operationalises intelligence into blocking via a policy that "
                "references the list."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "add_items", "remove_items"],
                    },
                    "list_id": {
                        "type": "string",
                        "description": "Custom list id (required for add_items/remove_items)",
                    },
                    "name": {"type": "string", "description": "List name (required for create)"},
                    "items": {
                        "type": "string",
                        "description": "Comma-separated IOCs (domains, IPs, CIDRs)",
                    },
                    "description": {"type": "string"},
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
