# SPDX-License-Identifier: Apache-2.0

"""Shodan integration - the search engine for Internet-connected devices.

API docs: https://developer.shodan.io/api
Auth: API key passed as the ``key`` query parameter on every request.
Base URL: https://api.shodan.io

Shodan continuously scans the public Internet and indexes the banners of the
services it finds. For a security/CTI agent this integration is the external
attack-surface and OSINT lens: look up everything Shodan knows about an IP or
domain, search the banner database with Shodan's filter syntax, run on-demand
scans, and manage network-monitoring alerts + notifiers.

Endpoints wired here span every documented REST surface:
  - Search:       /shodan/host/{ip}, /shodan/host/search, /shodan/host/count,
                  /shodan/host/search/facets, /shodan/host/search/filters,
                  /shodan/host/search/tokens
  - Scanning:     /shodan/ports, /shodan/protocols, /shodan/scan,
                  /shodan/scans, /shodan/scan/{id}
  - Alerts:       /shodan/alert(+/{id}, /info, /triggers, trigger + notifier
                  sub-resources)
  - Notifiers:    /notifier(+/provider, /{id})
  - Directory:    /shodan/query/search, /shodan/query/tags
  - DNS:          /dns/resolve, /dns/reverse, /dns/domain/{domain}
  - Account/util: /account/profile, /api-info, /tools/myip, /org(+/member/{u})
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import (
    DEFAULT_TIMEOUT,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "Shodan"
_BASE_URL = "https://api.shodan.io"
_SOURCE = "integration:shodan"

# Shodan truncates large fields itself; we still cap banner output so a single
# host with dozens of services can't flood the agent context.
_MAX_ITEMS = 50
_MAX_BANNER_CHARS = 600


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _seg(value: Any, *, safe: str = "") -> str:
    """Percent-encode a value for safe interpolation into a URL path segment.

    Path parameters can contain reserved characters - IPv6 addresses carry
    colons, org members can be emails (``@``, ``+``), and services are
    ``ip:port`` - which must be encoded so they cannot alter the request path
    or inject additional path/query segments. ``safe`` keeps a delimiter
    literal where the API expects it (e.g. commas in a trigger list).
    """
    return quote(str(value), safe=safe)


def _truncate(text: str, limit: int = _MAX_BANNER_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + " ...(truncated)"


def _format_service(banner: dict[str, Any]) -> str:
    """Format a single Shodan service banner into a compact line block."""
    port = banner.get("port", "?")
    transport = banner.get("transport", "")
    proto = f"{port}/{transport}" if transport else str(port)
    product = banner.get("product") or banner.get("_shodan", {}).get("module") or ""
    lines = [f"- {proto}" + (f" [{product}]" if product else "")]
    if banner.get("version"):
        lines[0] += f" v{banner['version']}"
    hostnames = banner.get("hostnames") or []
    if hostnames:
        lines.append(f"    hostnames: {', '.join(hostnames[:5])}")
    if banner.get("cpe"):
        lines.append(f"    cpe: {', '.join(banner['cpe'][:5])}")
    vulns = banner.get("vulns")
    if vulns:
        keys = list(vulns.keys()) if isinstance(vulns, dict) else list(vulns)
        lines.append(f"    vulns: {', '.join(keys[:10])}")
    data = banner.get("data")
    if data:
        lines.append(f"    banner: {_truncate(str(data), 200)}")
    return "\n".join(lines)


def _format_match(match: dict[str, Any]) -> str:
    """Format one host search match into a readable block."""
    ip = match.get("ip_str") or match.get("ip", "?")
    port = match.get("port", "?")
    org = match.get("org") or match.get("isp") or "Unknown org"
    loc = match.get("location") or {}
    country = loc.get("country_code") or match.get("country_code") or ""
    city = loc.get("city") or match.get("city") or ""
    place = ", ".join(p for p in (city, country) if p)
    header = f"**{ip}:{port}** - {org}" + (f" ({place})" if place else "")
    parts = [header]
    product = match.get("product")
    if product:
        parts.append(f"    product: {product}" + (f" {match.get('version', '')}".rstrip()))
    hostnames = match.get("hostnames") or []
    if hostnames:
        parts.append(f"    hostnames: {', '.join(hostnames[:5])}")
    if match.get("vulns"):
        vulns = match["vulns"]
        keys = list(vulns.keys()) if isinstance(vulns, dict) else list(vulns)
        parts.append(f"    vulns: {', '.join(keys[:10])}")
    if match.get("timestamp"):
        parts.append(f"    seen: {match['timestamp']}")
    return "\n".join(parts)


def _format_facets(facets: dict[str, Any]) -> str:
    """Format a Shodan facet breakdown into readable lines."""
    if not facets:
        return ""
    blocks: list[str] = ["", "Facet breakdown:"]
    for prop, values in facets.items():
        blocks.append(f"  {prop}:")
        for entry in (values or [])[:10]:
            blocks.append(f"    {entry.get('value')}: {entry.get('count')}")
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# Integration class
# ---------------------------------------------------------------------------


class ShodanIntegration(BaseIntegration):
    """Shodan - search engine for Internet-connected devices and OSINT."""

    provider_name = "shodan"
    display_name = "Shodan"
    icon = "Radar"
    description = (
        "Connect Shodan to search the Internet-wide device/banner database, "
        "look up host and domain intelligence, run on-demand scans, and manage "
        "network monitoring alerts."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        api_key = (token or "").strip()
        if not api_key:
            return False
        # ``safe_request`` converts ``httpx.HTTPError`` into a ``(resp, err)``
        # tuple and never raises, so no surrounding try/except is needed - a
        # broad ``except Exception`` here would only mask programming errors
        # and swallow task cancellation.
        async with egress_client(timeout=DEFAULT_TIMEOUT) as client:
            _resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/api-info",
                _PROVIDER,
                "validate connection",
                params={"key": api_key},
            )
        return err is None

    async def get_user_info(self, token: str) -> dict:
        api_key = (token or "").strip()
        async with egress_client(timeout=DEFAULT_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/api-info",
                _PROVIDER,
                "get account info",
                params={"key": api_key},
            )
            if err or resp is None:
                return {}
            return resp.json()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    """Build Shodan integration tools.

    ``token`` is the Shodan API key, sent as the ``key`` query parameter on
    every request.
    """
    api_key = (token or "").strip()

    def _missing_key() -> str:
        return (
            "Error: Shodan API key is not configured. Add your API key to the "
            "integration (Account -> API in the Shodan dashboard)."
        )

    async def _request(
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        allow_text: bool = False,
    ) -> tuple[Any, str | None]:
        """Issue a Shodan request; return ``(parsed_json, None)`` or ``(None, error)``."""
        if not api_key:
            return None, _missing_key()
        query = dict(params or {})
        query["key"] = api_key
        kwargs: dict[str, Any] = {"params": query}
        if json_body is not None:
            kwargs["json"] = json_body
        if data is not None:
            kwargs["data"] = data
        # ``safe_request`` already catches ``httpx.HTTPError`` and returns it as
        # a formatted ``(None, err)`` tuple, so no local try/except is needed
        # here (a surrounding one would be unreachable and could re-surface raw
        # exception text).
        async with egress_client(timeout=DEFAULT_TIMEOUT) as client:
            resp, err = await safe_request(
                client, method, f"{_BASE_URL}{path}", _PROVIDER, action, **kwargs
            )
        if err:
            return None, err
        if resp is None:
            return None, f"Error: {_PROVIDER} returned no response while trying to {action}."
        try:
            return resp.json(), None
        except (ValueError, json.JSONDecodeError):
            # Every Shodan endpoint except /tools/myip returns JSON, and the
            # callers immediately index into a dict/list. Surface a non-JSON
            # body as an error instead of returning a raw string that would
            # crash the caller with an opaque AttributeError.
            if allow_text:
                return (resp.text or "").strip(), None
            return None, (
                f"Error: {_PROVIDER} returned an unexpected non-JSON response "
                f"while trying to {action}."
            )

    # ── Search & host intelligence ─────────────────────────────────────────

    async def get_shodan_host_info(
        ip: str, history: bool = False, minify: bool = False
    ) -> str:
        """Return all services Shodan has indexed for a single IP address."""
        params: dict[str, Any] = {}
        if history:
            params["history"] = "true"
        if minify:
            params["minify"] = "true"
        data, err = await _request(
            "get", f"/shodan/host/{_seg(ip)}", f"get host info for {ip}", params=params
        )
        if err:
            return err
        ports = data.get("ports") or []
        org = data.get("org") or data.get("isp") or "Unknown org"
        country = data.get("country_name") or data.get("country_code") or ""
        hostnames = data.get("hostnames") or []
        header = [
            f"Host {data.get('ip_str', ip)} - {org}" + (f" ({country})" if country else ""),
            f"Open ports ({len(ports)}): {', '.join(str(p) for p in ports) or 'none'}",
        ]
        if hostnames:
            header.append(f"Hostnames: {', '.join(hostnames[:10])}")
        if data.get("tags"):
            header.append(f"Tags: {', '.join(data['tags'][:10])}")
        if data.get("vulns"):
            header.append(f"Vulnerabilities: {', '.join(list(data['vulns'])[:20])}")
        services = data.get("data") or []
        if services and not minify:
            header.append("\nServices:")
            header.extend(_format_service(b) for b in services[:_MAX_ITEMS])
        return "\n".join(header)

    async def search_shodan_hosts(
        query: str,
        facets: str = "",
        page: int = 1,
        minify: bool = True,
        fields: str = "",
    ) -> str:
        """Search the Shodan banner database using its filter query syntax.

        Uses query credits for filtered searches and pages past the first.
        """
        page = max(page, 1)
        params: dict[str, Any] = {
            "query": query,
            "page": page,
            "minify": "true" if minify else "false",
        }
        if facets:
            params["facets"] = facets
        if fields:
            params["fields"] = fields
        data, err = await _request(
            "get", "/shodan/host/search", f"search hosts for '{query}'", params=params
        )
        if err:
            return err
        matches = data.get("matches") or []
        total = data.get("total", len(matches))
        if not matches:
            return f"No hosts found for '{query}' (total matches: {total})."
        blocks = [_format_match(m) for m in matches[:_MAX_ITEMS]]
        out = (
            f"Found {total} hosts for '{query}' (page {page}, showing {len(blocks)}):\n\n"
            + "\n\n".join(blocks)
        )
        out += _format_facets(data.get("facets") or {})
        # Shodan returns up to 100 matches per page; we cap the display at
        # ``_MAX_ITEMS`` so a single page can't flood the agent context. Those
        # extra matches are NOT reachable by paging (page N+1 starts at the next
        # 100-block), so flag the intra-page truncation explicitly rather than
        # letting the user assume they saw the whole page.
        if len(matches) > len(blocks):
            out += (
                f"\n\n[Note: this page returned {len(matches)} matches but only "
                f"the first {len(blocks)} are shown; refine the query to narrow "
                f"the results.]"
            )
        shown = (page - 1) * 100 + len(matches)
        if total > shown:
            out += f'\n\n[More results available - pass page={page + 1}]'
        return out

    async def count_shodan_hosts(query: str, facets: str = "") -> str:
        """Count hosts matching a Shodan query without returning results (no query credits)."""
        params: dict[str, Any] = {"query": query}
        if facets:
            params["facets"] = facets
        data, err = await _request(
            "get", "/shodan/host/count", f"count hosts for '{query}'", params=params
        )
        if err:
            return err
        total = data.get("total", 0)
        out = f"{total:,} hosts match '{query}'."
        out += _format_facets(data.get("facets") or {})
        return out

    async def list_shodan_search_facets() -> str:
        """List the facet properties usable to summarise search results."""
        data, err = await _request(
            "get", "/shodan/host/search/facets", "list search facets"
        )
        if err:
            return err
        facets = data if isinstance(data, list) else []
        return f"Available facets ({len(facets)}):\n" + ", ".join(str(f) for f in facets)

    async def list_shodan_search_filters() -> str:
        """List the search filters usable in Shodan query strings (e.g. port, country, org)."""
        data, err = await _request(
            "get", "/shodan/host/search/filters", "list search filters"
        )
        if err:
            return err
        filters = data if isinstance(data, list) else []
        return f"Available filters ({len(filters)}):\n" + ", ".join(str(f) for f in filters)

    async def parse_shodan_query_tokens(query: str) -> str:
        """Break a search query into its filters and parameters to validate/debug it."""
        data, err = await _request(
            "get",
            "/shodan/host/search/tokens",
            f"parse query '{query}'",
            params={"query": query},
        )
        if err:
            return err
        return (
            f"Parsed '{query}':\n"
            f"  Search string: {data.get('string', '')}\n"
            f"  Filters: {', '.join(data.get('filters') or []) or 'none'}\n"
            f"  Attributes: {json.dumps(data.get('attributes') or {})}\n"
            f"  Errors: {', '.join(data.get('errors') or []) or 'none'}"
        )

    # ── Scanning ───────────────────────────────────────────────────────────

    async def list_shodan_ports() -> str:
        """List the port numbers that Shodan's crawlers actively scan."""
        data, err = await _request("get", "/shodan/ports", "list crawled ports")
        if err:
            return err
        ports = data if isinstance(data, list) else []
        return f"Shodan crawls {len(ports)} ports:\n" + ", ".join(str(p) for p in ports)

    async def list_shodan_protocols() -> str:
        """List the protocols available for on-demand Internet scans."""
        data, err = await _request("get", "/shodan/protocols", "list scan protocols")
        if err:
            return err
        if not isinstance(data, dict) or not data:
            return "No protocols returned."
        lines = [f"- {name}: {desc}" for name, desc in list(data.items())[:_MAX_ITEMS]]
        return f"Available scan protocols ({len(data)}):\n" + "\n".join(lines)

    async def request_shodan_scan(ips: str) -> str:
        """Request Shodan to crawl one or more IPs/netblocks. Consumes scan credits (paid plans)."""
        if not ips.strip():
            return "Error: provide at least one IP or CIDR netblock to scan."
        data, err = await _request(
            "post", "/shodan/scan", f"request scan of {ips}", data={"ips": ips}
        )
        if err:
            return err
        return (
            f"Scan submitted for {data.get('count', '?')} IP(s). "
            f"Scan ID: {data.get('id')} (credits left: {data.get('credits_left', '?')}). "
            f"Poll status with get_shodan_scan_status."
        )

    async def list_shodan_scans(page: int = 1) -> str:
        """List the on-demand scans created on the account."""
        page = max(page, 1)
        data, err = await _request(
            "get", "/shodan/scans", "list scans", params={"page": page}
        )
        if err:
            return err
        matches = data.get("matches") or []
        total = data.get("total", len(matches))
        if not matches:
            return "No scans found on this account."
        lines = [
            f"- {m.get('id')}: {m.get('status')} ({m.get('size')} IPs, created {m.get('created')})"
            for m in matches[:_MAX_ITEMS]
        ]
        return f"Found {total} scans (page {page}):\n" + "\n".join(lines)

    async def get_shodan_scan_status(scan_id: str) -> str:
        """Check the progress of a submitted scan (SUBMITTING, QUEUE, PROCESSING, DONE)."""
        data, err = await _request(
            "get", f"/shodan/scan/{_seg(scan_id)}", f"get scan status {scan_id}"
        )
        if err:
            return err
        return (
            f"Scan {data.get('id', scan_id)}: status={data.get('status')}, "
            f"count={data.get('count')}, created={data.get('created')}"
        )

    # ── Network alerts ───────────────────────────────────────────────────

    async def list_shodan_alerts() -> str:
        """List all network monitoring alerts on the account."""
        data, err = await _request("get", "/shodan/alert/info", "list alerts")
        if err:
            return err
        alerts = data if isinstance(data, list) else []
        if not alerts:
            return "No network alerts configured."
        lines = []
        for a in alerts[:_MAX_ITEMS]:
            ips = ", ".join((a.get("filters") or {}).get("ip") or [])
            triggers = ", ".join((a.get("triggers") or {}).keys())
            lines.append(
                f"- {a.get('id')}: {a.get('name')} [{ips}]"
                + (f" triggers: {triggers}" if triggers else "")
            )
        return f"Found {len(alerts)} alerts:\n" + "\n".join(lines)

    async def get_shodan_alert(alert_id: str) -> str:
        """Get the full configuration of a single network alert."""
        data, err = await _request(
            "get", f"/shodan/alert/{_seg(alert_id)}/info", f"get alert {alert_id}"
        )
        if err:
            return err
        ips = ", ".join((data.get("filters") or {}).get("ip") or [])
        triggers = ", ".join((data.get("triggers") or {}).keys())
        return (
            f"Alert {data.get('id', alert_id)}: {data.get('name')}\n"
            f"  IPs: {ips}\n"
            f"  Size: {data.get('size')}\n"
            f"  Triggers: {triggers or 'none'}\n"
            f"  Expires: {data.get('expires')}"
        )

    async def create_shodan_alert(name: str, ips: str, expires: int = 0) -> str:
        """Create a network alert monitoring the given IPs/netblocks for changes."""
        ip_list = [x.strip() for x in ips.split(",") if x.strip()]
        if not name.strip() or not ip_list:
            return "Error: provide a name and at least one IP/netblock."
        body = {"name": name, "filters": {"ip": ip_list}, "expires": expires}
        data, err = await _request(
            "post", "/shodan/alert", f"create alert '{name}'", json_body=body
        )
        if err:
            return err
        return (
            f"Created alert '{data.get('name', name)}' (ID: {data.get('id')}) "
            f"monitoring {data.get('size', len(ip_list))} IP(s)."
        )

    async def update_shodan_alert(alert_id: str, ips: str) -> str:
        """Replace the IPs/netblocks monitored by an existing alert."""
        ip_list = [x.strip() for x in ips.split(",") if x.strip()]
        if not ip_list:
            return "Error: provide at least one IP/netblock."
        body = {"filters": {"ip": ip_list}}
        data, err = await _request(
            "post",
            f"/shodan/alert/{_seg(alert_id)}",
            f"update alert {alert_id}",
            json_body=body,
        )
        if err:
            return err
        return f"Updated alert {data.get('id', alert_id)} - now monitoring {', '.join(ip_list)}."

    async def delete_shodan_alert(alert_id: str) -> str:
        """Permanently delete a network alert. This cannot be undone."""
        _data, err = await _request(
            "delete", f"/shodan/alert/{_seg(alert_id)}", f"delete alert {alert_id}"
        )
        if err:
            return err
        return f"Deleted alert {alert_id}."

    async def list_shodan_alert_triggers() -> str:
        """List trigger types that can be enabled on network alerts (e.g. malware, vulnerable)."""
        data, err = await _request(
            "get", "/shodan/alert/triggers", "list alert triggers"
        )
        if err:
            return err
        triggers = data if isinstance(data, list) else []
        lines = [
            f"- {t.get('name')}: {t.get('description')} (rule: {t.get('rule')})"
            for t in triggers[:_MAX_ITEMS]
        ]
        return f"Available alert triggers ({len(triggers)}):\n" + "\n".join(lines)

    async def manage_shodan_alert_trigger(
        alert_id: str,
        action: str,
        trigger: str = "",
        service: str = "",
        notifier_id: str = "",
    ) -> str:
        """Manage triggers and notifiers on a network alert.

        Actions:
          - enable / disable: turn a trigger (comma-separated names) on or off
          - ignore / unignore: whitelist/unwhitelist a service ("ip:port") for a trigger
          - add_notifier / remove_notifier: attach/detach a notifier_id to the alert
        """
        action = action.lower().strip()
        base = f"/shodan/alert/{_seg(alert_id)}"
        if action in ("enable", "disable", "ignore", "unignore") and not trigger.strip():
            return f"Error: 'trigger' is required for {action}."
        # ``trigger`` may be a comma-separated list, so keep commas literal.
        trigger_seg = _seg(trigger, safe=",")
        if action == "enable":
            _, err = await _request("put", f"{base}/trigger/{trigger_seg}", "enable trigger")
            return err or f"Enabled trigger '{trigger}' on alert {alert_id}."
        if action == "disable":
            _, err = await _request("delete", f"{base}/trigger/{trigger_seg}", "disable trigger")
            return err or f"Disabled trigger '{trigger}' on alert {alert_id}."
        if action in ("ignore", "unignore"):
            if not service:
                return "Error: 'service' (ip:port) is required for ignore/unignore."
            verb = "put" if action == "ignore" else "delete"
            _, err = await _request(
                verb,
                f"{base}/trigger/{trigger_seg}/ignore/{_seg(service)}",
                f"{action} service",
            )
            return err or f"{action.title()}d service {service} for trigger '{trigger}'."
        if action in ("add_notifier", "remove_notifier"):
            if not notifier_id:
                return "Error: 'notifier_id' is required for add_notifier/remove_notifier."
            verb = "put" if action == "add_notifier" else "delete"
            _, err = await _request(
                verb, f"{base}/notifier/{_seg(notifier_id)}", f"{action}"
            )
            return err or f"{action.replace('_', ' ').title()} {notifier_id} on alert {alert_id}."
        return (
            "Error: unknown action. Use enable, disable, ignore, unignore, "
            "add_notifier, or remove_notifier."
        )

    # ── Notifiers ──────────────────────────────────────────────────────────

    async def list_shodan_notifiers() -> str:
        """List the notification services (email, Slack, webhook, ...) created by the user."""
        data, err = await _request("get", "/notifier", "list notifiers")
        if err:
            return err
        matches = data.get("matches") or []
        if not matches:
            return "No notifiers configured."
        lines = [
            f"- {n.get('id')}: {n.get('provider')} ({json.dumps(n.get('args') or {})})"
            for n in matches[:_MAX_ITEMS]
        ]
        return f"Found {data.get('total', len(matches))} notifiers:\n" + "\n".join(lines)

    async def list_shodan_notifier_providers() -> str:
        """List available notifier provider types and their required arguments."""
        data, err = await _request(
            "get", "/notifier/provider", "list notifier providers"
        )
        if err:
            return err
        if not isinstance(data, dict):
            return "No notifier providers returned."
        lines = [
            f"- {name}: requires {', '.join((cfg or {}).get('required') or []) or 'no args'}"
            for name, cfg in data.items()
        ]
        return f"Notifier providers ({len(data)}):\n" + "\n".join(lines)

    async def manage_shodan_notifier(
        action: str,
        notifier_id: str = "",
        provider: str = "",
        description: str = "",
        args: Any = None,
    ) -> str:
        """Manage notification services (email/Slack/webhook/telegram/...).

        Actions:
          - get: fetch a notifier by notifier_id
          - create: create a notifier (needs provider; args depend on the provider)
          - update: update a notifier's args (needs notifier_id + args)
          - delete: permanently delete a notifier (needs notifier_id)
        args is a JSON object of provider-specific parameters (optional; only
        the fields a given provider requires, e.g. {"to": "me@example.com"} for
        email). A JSON-encoded string is tolerated too.
        """
        action = action.lower().strip()
        parsed_args: dict[str, Any] = {}
        if isinstance(args, str) and args.strip():
            try:
                decoded = json.loads(args)
            except json.JSONDecodeError:
                return "Error: 'args' must be a JSON object or dict."
            # A valid JSON string can still decode to a non-object (list,
            # number, ...); reject it so the later ``**parsed_args`` spread
            # cannot raise TypeError with an unhelpful failure.
            if not isinstance(decoded, dict):
                return "Error: 'args' must be a JSON object or dict."
            parsed_args = decoded
        elif isinstance(args, dict):
            parsed_args = args

        if action == "get":
            if not notifier_id:
                return "Error: 'notifier_id' is required for get."
            data, err = await _request(
                "get", f"/notifier/{_seg(notifier_id)}", f"get notifier {notifier_id}"
            )
            if err:
                return err
            return (
                f"Notifier {data.get('id', notifier_id)}: {data.get('provider')} "
                f"- {data.get('description') or ''} ({json.dumps(data.get('args') or {})})"
            )
        if action == "create":
            if not provider:
                return "Error: 'provider' is required for create."
            form = {"provider": provider, "description": description, **parsed_args}
            data, err = await _request(
                "post", "/notifier", f"create {provider} notifier", data=form
            )
            if err:
                return err
            return f"Created notifier {data.get('id')} (provider: {provider})."
        if action == "update":
            if not notifier_id:
                return "Error: 'notifier_id' is required for update."
            if not parsed_args:
                return (
                    "Error: 'args' (a non-empty JSON object of the fields to "
                    "change) is required for update."
                )
            data, err = await _request(
                "put", f"/notifier/{_seg(notifier_id)}", "update notifier", data=parsed_args
            )
            if err:
                return err
            return f"Updated notifier {notifier_id}."
        if action == "delete":
            if not notifier_id:
                return "Error: 'notifier_id' is required for delete."
            _, err = await _request(
                "delete", f"/notifier/{_seg(notifier_id)}", "delete notifier"
            )
            return err or f"Deleted notifier {notifier_id}."
        return "Error: unknown action. Use get, create, update, or delete."

    # ── Directory of saved queries ─────────────────────────────────────────

    async def search_shodan_query_directory(
        query: str = "", page: int = 1, sort: str = "", order: str = ""
    ) -> str:
        """Search the community directory of saved Shodan search queries."""
        if query:
            path, params = "/shodan/query/search", {"query": query, "page": max(page, 1)}
        else:
            path, params = "/shodan/query", {"page": max(page, 1)}
            if sort:
                params["sort"] = sort
            if order:
                params["order"] = order
        data, err = await _request("get", path, "search query directory", params=params)
        if err:
            return err
        matches = data.get("matches") or []
        if not matches:
            return "No saved queries found."
        lines = [
            f"- {m.get('title')} (votes: {m.get('votes')}): {m.get('query')}"
            for m in matches[:_MAX_ITEMS]
        ]
        return f"Found {data.get('total', len(matches))} saved queries:\n" + "\n".join(lines)

    async def list_shodan_query_tags(size: int = 10) -> str:
        """List the most popular tags for saved search queries."""
        data, err = await _request(
            "get", "/shodan/query/tags", "list query tags", params={"size": size}
        )
        if err:
            return err
        matches = data.get("matches") or []
        lines = [f"- {t.get('value')} ({t.get('count')})" for t in matches[:_MAX_ITEMS]]
        return "Popular query tags:\n" + "\n".join(lines)

    # ── DNS ────────────────────────────────────────────────────────────────

    async def resolve_shodan_dns(hostnames: str) -> str:
        """Resolve a comma-separated list of hostnames to IP addresses."""
        data, err = await _request(
            "get", "/dns/resolve", "resolve hostnames", params={"hostnames": hostnames}
        )
        if err:
            return err
        if not isinstance(data, dict) or not data:
            return "No DNS resolution results."
        return "DNS resolution:\n" + "\n".join(f"- {h}: {ip}" for h, ip in data.items())

    async def reverse_shodan_dns(ips: str) -> str:
        """Reverse-resolve a comma-separated list of IPs to hostnames."""
        data, err = await _request(
            "get", "/dns/reverse", "reverse resolve IPs", params={"ips": ips}
        )
        if err:
            return err
        if not isinstance(data, dict) or not data:
            return "No reverse DNS results."
        lines = [f"- {ip}: {', '.join(names or []) or 'no PTR'}" for ip, names in data.items()]
        return "Reverse DNS:\n" + "\n".join(lines)

    async def get_shodan_domain_info(
        domain: str, history: bool = False, type: str = "", page: int = 1
    ) -> str:
        """Get subdomains and DNS records for a domain (uses 1 query credit per lookup)."""
        page = max(page, 1)
        params: dict[str, Any] = {"page": page}
        if history:
            params["history"] = "true"
        if type:
            params["type"] = type
        data, err = await _request(
            "get",
            f"/dns/domain/{_seg(domain)}",
            f"get domain info for {domain}",
            params=params,
        )
        if err:
            return err
        subdomains = data.get("subdomains") or []
        records = data.get("data") or []
        out = [
            f"Domain {data.get('domain', domain)}",
            f"Subdomains ({len(subdomains)}): {', '.join(subdomains[:40])}",
        ]
        if data.get("tags"):
            out.append(f"Tags: {', '.join(data['tags'][:10])}")
        if records:
            out.append("\nDNS records:")
            out.extend(
                f"- {r.get('subdomain') or '@'} {r.get('type')} -> {r.get('value')}"
                for r in records[:_MAX_ITEMS]
            )
        if data.get("more"):
            out.append(f"\n[More records available - pass page={page + 1}]")
        return "\n".join(out)

    # ── Account & utility ──────────────────────────────────────────────────

    async def get_shodan_account_profile() -> str:
        """Get the Shodan account profile linked to the API key."""
        data, err = await _request("get", "/account/profile", "get account profile")
        if err:
            return err
        return (
            f"Account: display_name={data.get('display_name')}, "
            f"member={data.get('member')}, credits={data.get('credits')}, "
            f"created={data.get('created')}"
        )

    async def get_shodan_api_info() -> str:
        """Get API plan details and remaining query/scan credits for the API key."""
        data, err = await _request("get", "/api-info", "get API info")
        if err:
            return err
        return (
            f"Plan: {data.get('plan')}\n"
            f"Query credits: {data.get('query_credits')}\n"
            f"Scan credits: {data.get('scan_credits')}\n"
            f"Monitored IPs: {data.get('monitored_ips')}\n"
            f"HTTPS: {data.get('https')}, Unlocked: {data.get('unlocked')}"
        )

    async def get_shodan_my_ip() -> str:
        """Return the current external IP address as seen from the Internet."""
        # /tools/myip is the one Shodan endpoint that may answer with a bare
        # text body instead of JSON.
        data, err = await _request(
            "get", "/tools/myip", "get external IP", allow_text=True
        )
        if err:
            return err
        return f"External IP: {data}"

    async def get_shodan_org_info() -> str:
        """Get the Shodan organization details (members, admins, domains). Enterprise."""
        data, err = await _request("get", "/org", "get organization info")
        if err:
            return err
        admins = ", ".join(a.get("username", "") for a in (data.get("admins") or []))
        members = ", ".join(m.get("username", "") for m in (data.get("members") or []))
        return (
            f"Organization: {data.get('name')} (id: {data.get('id')})\n"
            f"  Access: {data.get('upgrade_type')}\n"
            f"  Domains: {', '.join(data.get('domains') or [])}\n"
            f"  Admins: {admins or 'none'}\n"
            f"  Members: {members or 'none'}"
        )

    async def manage_shodan_org_member(
        action: str, user: str, notify: bool = False
    ) -> str:
        """Add or remove an organization member (Enterprise).

        Actions:
          - add: add a Shodan user (username or email) to the org and upgrade them
          - remove: remove and downgrade a member (this revokes their access)
        """
        action = action.lower().strip()
        if not user:
            return "Error: 'user' (username or email) is required."
        if action == "add":
            params = {"notify": "true"} if notify else None
            _data, err = await _request(
                "put", f"/org/member/{_seg(user)}", f"add org member {user}", params=params
            )
            return err or f"Added {user} to the organization."
        if action == "remove":
            _data, err = await _request(
                "delete", f"/org/member/{_seg(user)}", f"remove org member {user}"
            )
            return err or f"Removed {user} from the organization."
        return "Error: unknown action. Use add or remove."

    # ── Register ───────────────────────────────────────────────────────────

    _handlers = {
        "get_shodan_host_info": get_shodan_host_info,
        "search_shodan_hosts": search_shodan_hosts,
        "count_shodan_hosts": count_shodan_hosts,
        "list_shodan_search_facets": list_shodan_search_facets,
        "list_shodan_search_filters": list_shodan_search_filters,
        "parse_shodan_query_tokens": parse_shodan_query_tokens,
        "list_shodan_ports": list_shodan_ports,
        "list_shodan_protocols": list_shodan_protocols,
        "request_shodan_scan": request_shodan_scan,
        "list_shodan_scans": list_shodan_scans,
        "get_shodan_scan_status": get_shodan_scan_status,
        "list_shodan_alerts": list_shodan_alerts,
        "get_shodan_alert": get_shodan_alert,
        "create_shodan_alert": create_shodan_alert,
        "update_shodan_alert": update_shodan_alert,
        "delete_shodan_alert": delete_shodan_alert,
        "list_shodan_alert_triggers": list_shodan_alert_triggers,
        "manage_shodan_alert_trigger": manage_shodan_alert_trigger,
        "list_shodan_notifiers": list_shodan_notifiers,
        "list_shodan_notifier_providers": list_shodan_notifier_providers,
        "manage_shodan_notifier": manage_shodan_notifier,
        "search_shodan_query_directory": search_shodan_query_directory,
        "list_shodan_query_tags": list_shodan_query_tags,
        "resolve_shodan_dns": resolve_shodan_dns,
        "reverse_shodan_dns": reverse_shodan_dns,
        "get_shodan_domain_info": get_shodan_domain_info,
        "get_shodan_account_profile": get_shodan_account_profile,
        "get_shodan_api_info": get_shodan_api_info,
        "get_shodan_my_ip": get_shodan_my_ip,
        "get_shodan_org_info": get_shodan_org_info,
        "manage_shodan_org_member": manage_shodan_org_member,
    }
    for _name, _fn in _handlers.items():
        executor.register(_name, _fn)

    def _tool(
        name: str, description: str, schema: dict[str, Any], priority: int = 0
    ) -> ToolDefinition:
        return ToolDefinition(
            name=name,
            description=description,
            input_schema=schema,
            source=_SOURCE,
            priority=priority,
        )

    _obj = lambda props, required=None: {  # noqa: E731
        "type": "object",
        "properties": props,
        "required": required or [],
    }
    _str = {"type": "string"}
    _int = {"type": "integer"}
    _bool = {"type": "boolean"}

    return [
        _tool(
            "get_shodan_host_info",
            "Return everything Shodan knows about a single IP: open ports, service "
            "banners, hostnames, tags, and known vulnerabilities (CVEs). Use this to "
            "enrich an IP during triage/investigation. For a whole search use "
            "search_shodan_hosts instead.",
            _obj(
                {
                    "ip": {**_str, "description": "IP address to look up, e.g. 8.8.8.8"},
                    "history": {**_bool, "description": "Include historical banners (default false)", "default": False},
                    "minify": {**_bool, "description": "Only ports + host summary, no banners (default false)", "default": False},
                },
                ["ip"],
            ),
        ),
        _tool(
            "search_shodan_hosts",
            "Search Shodan's Internet-wide banner database using its filter syntax "
            "(e.g. 'apache country:DE', 'product:nginx port:443', 'vuln:CVE-2021-44228'). "
            "Returns matching hosts with IP, port, org, location, product and vulns. "
            "Use facets to summarise (e.g. facets='country,org'). Filtered queries and "
            "pages past the first consume query credits.",
            _obj(
                {
                    "query": {**_str, "description": "Shodan search query with optional filter:value terms"},
                    "facets": {**_str, "description": "Comma-separated facet properties for a summary breakdown, e.g. 'country,org'"},
                    "page": {**_int, "description": "Result page, 100 per page (default 1)", "default": 1},
                    "minify": {**_bool, "description": "Truncate large banner fields (default true)", "default": True},
                    "fields": {**_str, "description": "Comma-separated fields to return, e.g. 'ip_str,port,http.title'"},
                },
                ["query"],
            ),
        ),
        _tool(
            "count_shodan_hosts",
            "Count how many hosts match a Shodan query WITHOUT returning results and "
            "WITHOUT consuming query credits. Ideal for sizing an exposure ('how many "
            "hosts run this product?') or getting a facet breakdown cheaply.",
            _obj(
                {
                    "query": {**_str, "description": "Shodan search query"},
                    "facets": {**_str, "description": "Comma-separated facet properties for a summary breakdown"},
                },
                ["query"],
            ),
        ),
        _tool(
            "list_shodan_search_facets",
            "List the facet properties (asn, country, org, port, ...) that can be "
            "requested to summarise search or count results.",
            _obj({}),
        ),
        _tool(
            "list_shodan_search_filters",
            "List the search filters (port, country, org, product, vuln, net, ...) "
            "that can be used inside a Shodan query string.",
            _obj({}),
        ),
        _tool(
            "parse_shodan_query_tokens",
            "Break a search query into the filters and parameters Shodan detects. Use "
            "this to validate or debug a complex query before running search_shodan_hosts.",
            _obj({"query": {**_str, "description": "Query string to parse"}}, ["query"]),
        ),
        _tool(
            "list_shodan_ports",
            "List the port numbers that Shodan's crawlers actively scan on the Internet.",
            _obj({}),
        ),
        _tool(
            "list_shodan_protocols",
            "List the protocols that can be used when requesting an on-demand Internet scan.",
            _obj({}),
        ),
        _tool(
            "list_shodan_scans",
            "List the on-demand scans created on this account, with their status and size.",
            _obj({"page": {**_int, "description": "Result page (default 1)", "default": 1}}),
        ),
        _tool(
            "get_shodan_scan_status",
            "Check the progress of a submitted scan (SUBMITTING, QUEUE, PROCESSING, DONE). "
            "Use after request_shodan_scan to know when results are indexed.",
            _obj({"scan_id": {**_str, "description": "Scan ID returned by request_shodan_scan"}}, ["scan_id"]),
        ),
        _tool(
            "list_shodan_alerts",
            "List all network monitoring alerts on the account (IP ranges being watched "
            "and their enabled triggers).",
            _obj({}),
        ),
        _tool(
            "get_shodan_alert",
            "Get the full configuration of a single network alert (monitored IPs, "
            "triggers, expiry).",
            _obj({"alert_id": {**_str, "description": "Alert ID"}}, ["alert_id"]),
        ),
        _tool(
            "list_shodan_alert_triggers",
            "List the trigger types (malware, industrial_control_system, vulnerable, "
            "new_service, uncommon, ...) that can be enabled on network alerts.",
            _obj({}),
        ),
        _tool(
            "list_shodan_notifiers",
            "List the notification services (email, Slack, webhook, telegram, ...) the "
            "user has created for alert delivery.",
            _obj({}),
        ),
        _tool(
            "list_shodan_notifier_providers",
            "List the available notifier provider types and the arguments each requires.",
            _obj({}),
        ),
        _tool(
            "search_shodan_query_directory",
            "Search the community directory of saved Shodan search queries (great for "
            "discovering effective queries, e.g. 'webcam', 'ics'). Omit query to list "
            "recent/top saved queries.",
            _obj(
                {
                    "query": {**_str, "description": "Keyword to search the query directory (optional)"},
                    "page": {**_int, "description": "Result page, 10 per page (default 1)", "default": 1},
                    "sort": {**_str, "description": "Sort by 'votes' or 'timestamp' (only when query is empty)", "enum": ["votes", "timestamp"]},
                    "order": {**_str, "description": "'asc' or 'desc' (only when query is empty)", "enum": ["asc", "desc"]},
                }
            ),
        ),
        _tool(
            "list_shodan_query_tags",
            "List the most popular tags applied to saved search queries.",
            _obj({"size": {**_int, "description": "Number of tags to return (default 10)", "default": 10}}),
        ),
        _tool(
            "resolve_shodan_dns",
            "Resolve one or more hostnames to IP addresses (comma-separated).",
            _obj({"hostnames": {**_str, "description": "Comma-separated hostnames, e.g. 'google.com,bing.com'"}}, ["hostnames"]),
        ),
        _tool(
            "reverse_shodan_dns",
            "Reverse-resolve one or more IP addresses to their hostnames (comma-separated).",
            _obj({"ips": {**_str, "description": "Comma-separated IPs, e.g. '8.8.8.8,1.1.1.1'"}}, ["ips"]),
        ),
        _tool(
            "get_shodan_domain_info",
            "Get all known subdomains and DNS records for a domain (uses 1 query credit "
            "per lookup). Use for domain footprinting / attack-surface discovery.",
            _obj(
                {
                    "domain": {**_str, "description": "Domain name, e.g. 'cnn.com'"},
                    "history": {**_bool, "description": "Include historical DNS data (default false)", "default": False},
                    "type": {**_str, "description": "Filter by DNS record type", "enum": ["A", "AAAA", "CNAME", "NS", "SOA", "MX", "TXT"]},
                    "page": {**_int, "description": "Result page, 100 per page (default 1)", "default": 1},
                },
                ["domain"],
            ),
        ),
        _tool(
            "get_shodan_account_profile",
            "Get the Shodan account profile (display name, membership, credits) linked "
            "to the configured API key.",
            _obj({}),
        ),
        _tool(
            "get_shodan_api_info",
            "Get the API plan and remaining query/scan credits for the configured API "
            "key. Check this before running credit-consuming searches or scans.",
            _obj({}),
        ),
        _tool(
            "get_shodan_my_ip",
            "Return the current external IP address as seen from the Internet.",
            _obj({}),
        ),
        _tool(
            "get_shodan_org_info",
            "Get the Shodan organization details (name, members, admins, authorized "
            "domains). Requires an Enterprise organization.",
            _obj({}),
        ),
        # Writes (priority 1)
        _tool(
            "request_shodan_scan",
            "Request Shodan to crawl one or more IPs/netblocks on demand. Consumes scan "
            "credits (1 per IP) and requires a paid plan. Returns a scan ID to poll with "
            "get_shodan_scan_status.",
            _obj({"ips": {**_str, "description": "Comma-separated IPs or CIDR netblocks, e.g. '8.8.8.8,1.1.1.0/24'"}}, ["ips"]),
            priority=1,
        ),
        _tool(
            "create_shodan_alert",
            "Create a network alert that monitors the given IPs/netblocks for changes. "
            "Enable triggers afterwards with manage_shodan_alert_trigger.",
            _obj(
                {
                    "name": {**_str, "description": "Human-readable alert name"},
                    "ips": {**_str, "description": "Comma-separated IPs or CIDR netblocks to monitor"},
                    "expires": {**_int, "description": "Seconds until the alert expires (0 = never, default)", "default": 0},
                },
                ["name", "ips"],
            ),
            priority=1,
        ),
        _tool(
            "update_shodan_alert",
            "Replace the list of IPs/netblocks monitored by an existing network alert.",
            _obj(
                {
                    "alert_id": {**_str, "description": "Alert ID to update"},
                    "ips": {**_str, "description": "New comma-separated IPs or CIDR netblocks"},
                },
                ["alert_id", "ips"],
            ),
            priority=1,
        ),
        _tool(
            "manage_shodan_alert_trigger",
            "Manage triggers and notifiers on a network alert. action=enable/disable "
            "turns a trigger on/off; action=ignore/unignore whitelists a service "
            "('ip:port') for a trigger; action=add_notifier/remove_notifier attaches or "
            "detaches a notifier_id.",
            _obj(
                {
                    "alert_id": {**_str, "description": "Alert ID"},
                    "action": {**_str, "description": "enable, disable, ignore, unignore, add_notifier, or remove_notifier", "enum": ["enable", "disable", "ignore", "unignore", "add_notifier", "remove_notifier"]},
                    "trigger": {**_str, "description": "Trigger name(s), comma-separated - required for enable/disable/ignore/unignore"},
                    "service": {**_str, "description": "Service 'ip:port' for ignore/unignore"},
                    "notifier_id": {**_str, "description": "Notifier ID for add_notifier/remove_notifier"},
                },
                ["alert_id", "action"],
            ),
            priority=1,
        ),
        _tool(
            "manage_shodan_notifier",
            "Manage notification services used by alerts. action=get fetches one; "
            "action=create makes one (needs provider; args are provider-specific and "
            "only required by some providers); action=update changes its args; "
            "action=delete permanently removes it. args is a dict/JSON object of "
            "provider-specific parameters (e.g. {\"to\": \"me@example.com\"} for email).",
            _obj(
                {
                    "action": {**_str, "description": "get, create, update, or delete", "enum": ["get", "create", "update", "delete"]},
                    "notifier_id": {**_str, "description": "Notifier ID (get/update/delete)"},
                    "provider": {**_str, "description": "Provider name for create (email, slack, webhook, telegram, ...)"},
                    "description": {**_str, "description": "Description for the notifier (create)"},
                    "args": {"type": "object", "description": "Provider-specific arguments, e.g. {\"to\": \"me@example.com\"}", "additionalProperties": True},
                },
                ["action"],
            ),
            priority=1,
        ),
        _tool(
            "manage_shodan_org_member",
            "Add or remove an organization member (Enterprise). action=add adds and "
            "upgrades a Shodan user; action=remove removes and downgrades a member "
            "(revokes their org access).",
            _obj(
                {
                    "action": {**_str, "description": "add or remove", "enum": ["add", "remove"]},
                    "user": {**_str, "description": "Shodan username or email"},
                    "notify": {**_bool, "description": "Email the user when adding (default false)", "default": False},
                },
                ["action", "user"],
            ),
            priority=1,
        ),
        # Destructive (priority 2)
        _tool(
            "delete_shodan_alert",
            "Permanently delete a network alert. This cannot be undone.",
            _obj({"alert_id": {**_str, "description": "Alert ID to delete"}}, ["alert_id"]),
            priority=2,
        ),
    ]
