"""CrowdSec Service API tools - the CVE Live Exploit Tracker.

Which CVEs are being exploited in the wild RIGHT NOW, by which IPs, and what
to do about it:

* ``list_crowdsec_cves``               - GET /cves (exploitation phase filter)
* ``get_crowdsec_cve``                 - GET /cves/{id} (score, phase,
                                         CrowdSec analysis, CWEs, events)
* ``get_crowdsec_cve_ips``             - GET /cves/{id}/ips-details
* ``list_crowdsec_exploitation_events``- GET /tracker-events/
                                         exploitation-phase-change
* ``manage_crowdsec_cve_tracking``     - protect rules / timeline / indicators
                                         / IP stats / integration feed
                                         subscriptions per CVE
* ``manage_crowdsec_tracker``          - fingerprint rules, tracker tags,
                                         vendors, products (the same tracker
                                         surface keyed on other dimensions)
"""

from __future__ import annotations

from typing import Any

from app.integrations.http import format_entity_list, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _PROVIDER,
    _SOURCE,
    MISSING_SERVICE_KEY_MSG,
    SERVICE_API,
    clamp,
    client,
    dt,
    is_valid_cve_id,
    page_footer,
    quote_path,
    trunc,
    validate_service_duration,
)

_INVALID_CVE_MSG = "Error: a valid CVE id is required (e.g. CVE-2024-3400)."

_EXPLOITATION_PHASES = (
    "insufficient_data",
    "early_exploitation",
    "fresh_and_popular",
    "targeted_exploitation",
    "mass_exploitation",
    "background_noise",
    "unpopular",
    "wearing_out",
    "unclassified",
)

_CVE_SORTS = ("rule_release_date", "trending", "nb_ips", "name", "first_seen")

_CVE_TRACKING_ACTIONS = (
    "protect_rules",
    "timeline",
    "indicators",
    "ip_stats",
    "list_integrations",
    "subscribe_integration",
    "unsubscribe_integration",
)

_TRACKER_ACTIONS = (
    "list_fingerprints",
    "get_fingerprint",
    "get_fingerprint_ips",
    "list_vendors",
    "get_vendor",
    "get_vendor_stats",
    "get_vendor_ips",
    "list_products",
    "get_product",
    "list_tags",
    "get_tag",
    "list_tracker_tags",
    "get_tracker_tag",
    "subscribe_fingerprint_integration",
    "unsubscribe_fingerprint_integration",
    "subscribe_vendor_integration",
    "unsubscribe_vendor_integration",
)


def _phase(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("label") or value.get("name") or "?")
    return str(value or "?")


def _format_cve(c: dict[str, Any]) -> str:
    return (
        f"  {c.get('name') or c.get('id', '?')}: {trunc(c.get('title'), 80)}"
        f" | score: {c.get('crowdsec_score', '?')}/10"
        f" | phase: {_phase(c.get('exploitation_phase'))}"
        f" | IPs: {c.get('nb_ips', '?')}"
        + (
            f" | momentum: {c['momentum_score']}/5"
            if c.get("momentum_score") is not None
            else ""
        )
        + (" | public exploit" if c.get("has_public_exploit") else "")
        + (f" | last seen: {dt(c.get('last_seen'))}" if c.get("last_seen") else "")
    )


def _format_tracked_ip(i: dict[str, Any]) -> str:
    return (
        f"  {i.get('ip', '?')}"
        + (f" [{i['reputation']}]" if i.get("reputation") else "")
        + (f" | AS: {i['as_name']}" if i.get("as_name") else "")
        + (
            f" | {(i.get('location') or {}).get('country')}"
            if (i.get("location") or {}).get("country")
            else ""
        )
        + (f" | last seen: {dt(i.get('last_seen'))}" if i.get("last_seen") else "")
    )


def _format_facets(stats: dict[str, Any]) -> list[str]:
    lines: list[str] = [f"Total matching IPs: {stats.get('total', 0)}"]
    for key, label in (
        ("reputation", "By reputation"),
        ("country", "Top countries"),
        ("as_name", "Top AS"),
        ("cves", "Top CVEs"),
        ("classifications", "Top classifications"),
    ):
        rows = stats.get(key) or []
        if not rows:
            continue
        top = ", ".join(
            f"{r.get('value', r.get('key', '?'))} ({r.get('count', 0)})"
            for r in rows[:6]
            if isinstance(r, dict)
        )
        if top:
            lines.append(f"{label}: {top}")
    return lines


def register(
    service_headers: dict[str, str] | None,
    executor: CallableToolExecutor,
) -> list[ToolDefinition]:
    """Register the Live Exploit Tracker tools."""

    async def _get(
        http: Any, path: str, action: str, params: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str | None]:
        resp, err = await safe_request(
            http,
            "get",
            f"{SERVICE_API}{path}",
            _PROVIDER,
            action,
            headers=service_headers,
            params=params,
        )
        if err:
            return None, err
        return resp.json() or {}, None

    async def list_crowdsec_cves(
        query: str = "",
        exploitation_phase: str = "",
        sort_by: str = "trending",
        page: int = 1,
        size: int = 25,
    ) -> str:
        """Live Exploit Tracker: CVEs currently exploited in the wild."""
        if service_headers is None:
            return MISSING_SERVICE_KEY_MSG
        params: dict[str, Any] = {
            "page": clamp(page, default=1, hi=10_000),
            "size": clamp(size, default=25, hi=100),
        }
        if str(query or "").strip():
            params["query"] = str(query).strip()
        phase = str(exploitation_phase or "").strip().lower()
        if phase:
            if phase not in _EXPLOITATION_PHASES:
                return (
                    "Error: exploitation_phase must be one of "
                    f"{', '.join(_EXPLOITATION_PHASES)}."
                )
            params["exploitation_phase"] = phase
        sort = str(sort_by or "trending").strip().lower()
        if sort not in _CVE_SORTS:
            return f"Error: sort_by must be one of {', '.join(_CVE_SORTS)}."
        params["sort_by"] = sort
        async with client() as http:
            data, err = await _get(http, "/cves", "list tracked CVEs", params)
            if err:
                return err
        items = data.get("items") or []
        out = format_entity_list(
            items,
            _format_cve,
            noun="tracked CVE",
            total=data.get("total"),
            empty_msg="No tracked CVEs match the given filters.",
        )
        return out + page_footer(data)

    async def get_crowdsec_cve(cve_id: str) -> str:
        """One tracked CVE in full: scores, phase, analysis, components, events."""
        if service_headers is None:
            return MISSING_SERVICE_KEY_MSG
        cve_id = str(cve_id or "").strip().upper()
        if not is_valid_cve_id(cve_id):
            return _INVALID_CVE_MSG
        async with client() as http:
            data, err = await _get(
                http, f"/cves/{quote_path(cve_id)}", f"get CVE {cve_id}"
            )
            if err:
                return err
        lines = [
            f"{data.get('name') or cve_id}: {trunc(data.get('title'), 120)}",
            f"  CrowdSec score: {data.get('crowdsec_score', '?')}/10"
            f" | phase: {_phase(data.get('exploitation_phase'))}"
            f" | opportunity: {data.get('opportunity_score', '?')}/5"
            f" (0=opportunistic, 5=targeted)"
            f" | momentum: {data.get('momentum_score', '?')}/5",
            f"  Exploiting IPs: {data.get('nb_ips', '?')}"
            + (f" | CVSS: {data['cvss_score']}" if data.get("cvss_score") is not None else "")
            + (" | public exploit available" if data.get("has_public_exploit") else "")
            + (
                f" | seen: {dt(data.get('first_seen'))} -> {dt(data.get('last_seen'))}"
                if data.get("first_seen")
                else ""
            ),
        ]
        if data.get("description"):
            lines.append(f"  {trunc(data['description'], 400)}")
        if data.get("crowdsec_analysis"):
            lines.append(f"  CrowdSec analysis: {trunc(data['crowdsec_analysis'], 500)}")
        components = data.get("affected_components") or []
        if components:
            shown = ", ".join(
                f"{c.get('vendor', '?')}/{c.get('product', '?')}"
                for c in components[:6]
                if isinstance(c, dict)
            )
            lines.append(f"  Affected: {shown}")
        cwes = data.get("cwes") or []
        if cwes:
            lines.append(
                "  CWEs: "
                + ", ".join(
                    str(c.get("id") or c.get("name") or c) for c in cwes[:6]
                )
            )
        events = data.get("events") or []
        if events:
            lines.append("  Recent events:")
            for e in events[:5]:
                if isinstance(e, dict):
                    lines.append(
                        f"    {dt(e.get('date'))}: {trunc(e.get('label') or e.get('description'), 120)}"
                    )
        lines.append(
            "  Follow-ups: get_crowdsec_cve_ips (attacking IPs), "
            "manage_crowdsec_cve_tracking (protect rules, timeline, "
            "indicators, feed subscription)."
        )
        return "\n".join(lines)

    async def get_crowdsec_cve_ips(
        cve_id: str, since: str = "14d", page: int = 1, size: int = 50
    ) -> str:
        """The IPs actively exploiting a CVE (with reputation detail)."""
        if service_headers is None:
            return MISSING_SERVICE_KEY_MSG
        cve_id = str(cve_id or "").strip().upper()
        if not is_valid_cve_id(cve_id):
            return _INVALID_CVE_MSG
        since = str(since or "14d").strip()
        dur_err = validate_service_duration(since)
        if dur_err:
            return dur_err
        params = {
            "since": since,
            "page": clamp(page, default=1, hi=10_000),
            "size": clamp(size, default=50, hi=100),
        }
        async with client(timeout=60) as http:
            data, err = await _get(
                http,
                f"/cves/{quote_path(cve_id)}/ips-details",
                f"get IPs exploiting {cve_id}",
                params,
            )
            if err:
                return err
        items = data.get("items") or []
        out = format_entity_list(
            items,
            _format_tracked_ip,
            noun="exploiting IP",
            total=data.get("total"),
            empty_msg=f"No IPs observed exploiting {cve_id} in the last {since}.",
        )
        if items:
            out += (
                "\nUse lookup_crowdsec_ip for full context, or "
                "add_crowdsec_blocklist_ips to block them fleet-wide."
            )
        return out + page_footer(data)

    async def list_crowdsec_exploitation_events(
        since: str = "30d",
        cve_id: str = "",
        new_phase: str = "",
        page: int = 1,
        size: int = 50,
    ) -> str:
        """Phase-change feed: CVEs moving between exploitation phases."""
        if service_headers is None:
            return MISSING_SERVICE_KEY_MSG
        since = str(since or "30d").strip()
        dur_err = validate_service_duration(since)
        if dur_err:
            return dur_err
        params: dict[str, Any] = {
            "since": since,
            "page": clamp(page, default=1, hi=10_000),
            "size": clamp(size, default=50, hi=100),
        }
        if str(cve_id or "").strip():
            cid = str(cve_id).strip().upper()
            if not is_valid_cve_id(cid):
                return _INVALID_CVE_MSG
            params["cve_id"] = cid
        phase = str(new_phase or "").strip().lower()
        if phase:
            if phase not in _EXPLOITATION_PHASES:
                return (
                    "Error: new_phase must be one of "
                    f"{', '.join(_EXPLOITATION_PHASES)}."
                )
            params["new_phase"] = phase
        async with client() as http:
            data, err = await _get(
                http,
                "/tracker-events/exploitation-phase-change",
                "list exploitation phase changes",
                params,
            )
            if err:
                return err
        items = data.get("items") or []

        def _fmt(e: dict[str, Any]) -> str:
            return (
                f"  {dt(e.get('date'))}: {e.get('cve_id', '?')} "
                f"{e.get('previous_phase', '?')} -> {e.get('new_phase', '?')}"
                + (f" | {trunc(e.get('description'), 120)}" if e.get("description") else "")
            )

        out = format_entity_list(
            items,
            _fmt,
            noun="phase-change event",
            total=data.get("total"),
            empty_msg=f"No exploitation phase changes in the last {since}.",
        )
        return out + page_footer(data)

    async def manage_crowdsec_cve_tracking(
        action: str,
        cve_id: str,
        integration_name: str = "",
        since: str = "14d",
        timeline_days: int = 7,
        page: int = 1,
        size: int = 50,
    ) -> str:
        """Per-CVE tracker detail + firewall-integration feed subscriptions."""
        if service_headers is None:
            return MISSING_SERVICE_KEY_MSG
        act = str(action or "").strip().lower()
        if act not in _CVE_TRACKING_ACTIONS:
            return f"Error: action must be one of {', '.join(_CVE_TRACKING_ACTIONS)}."
        cve_id = str(cve_id or "").strip().upper()
        if not is_valid_cve_id(cve_id):
            return _INVALID_CVE_MSG
        base = f"/cves/{quote_path(cve_id)}"
        page_params = {
            "page": clamp(page, default=1, hi=10_000),
            "size": clamp(size, default=50, hi=100),
        }

        async with client(timeout=60) as http:
            if act == "protect_rules":
                data, err = await _get(
                    http, f"{base}/protect-rules", f"get protect rules for {cve_id}"
                )
                if err:
                    return err
                rules = data.get("items") or data.get("rules") or []
                if not rules:
                    return f"No protection rules published for {cve_id} yet."
                lines = [f"Protection rules for {cve_id} ({len(rules)}):"]
                for r in rules[:10]:
                    if isinstance(r, dict):
                        lines.append(
                            f"  {r.get('label') or r.get('name', '?')}"
                            + (f" | published: {dt(r.get('published_date'))}" if r.get("published_date") else "")
                            + (f" | {r['link']}" if r.get("link") else "")
                        )
                return "\n".join(lines)

            if act == "timeline":
                days = int(timeline_days or 7)
                if days not in (1, 7, 30):
                    return "Error: timeline_days must be 1, 7 or 30."
                data, err = await _get(
                    http,
                    f"{base}/timeline",
                    f"get timeline for {cve_id}",
                    {"since_days": days},
                )
                if err:
                    return err
                points = data.get("items") or data.get("timeline") or []
                if not points:
                    return f"No timeline activity for {cve_id} in the last {days} day(s)."
                lines = [f"Attack timeline for {cve_id} (last {days} day(s)):"]
                lines += [
                    f"  {dt(p.get('timestamp'))}: {p.get('count', 0)} signal(s)"
                    for p in points[:40]
                    if isinstance(p, dict)
                ]
                return "\n".join(lines)

            if act == "indicators":
                data, err = await _get(
                    http, f"{base}/indicators", f"get indicators for {cve_id}"
                )
                if err:
                    return err
                indicators = data.get("items") or data.get("indicators") or []
                if not indicators:
                    return f"No indicators published for {cve_id}."
                lines = [f"Top indicators for {cve_id}:"]
                for i in indicators[:15]:
                    if isinstance(i, dict):
                        lines.append(
                            f"  [{i.get('indicator_type', '?')}] "
                            f"{trunc(i.get('value'), 120)} | IPs: {i.get('nb_ips', 0)}"
                            + (f" | last seen: {dt(i.get('last_seen'))}" if i.get("last_seen") else "")
                        )
                return "\n".join(lines)

            if act == "ip_stats":
                since = str(since or "14d").strip()
                dur_err = validate_service_duration(since)
                if dur_err:
                    return dur_err
                data, err = await _get(
                    http,
                    f"{base}/ips-details-stats",
                    f"get IP stats for {cve_id}",
                    {"since": since},
                )
                if err:
                    return err
                return "\n".join(
                    [
                        f"Exploiting-IP stats for {cve_id} (last {since}):",
                        *_format_facets(data),
                    ]
                )

            if act == "list_integrations":
                data, err = await _get(
                    http,
                    f"{base}/integrations",
                    f"list integrations subscribed to {cve_id}",
                    page_params,
                )
                if err:
                    return err
                items = data.get("items") or []
                out = format_entity_list(
                    items,
                    lambda i: f"  {i.get('name', '?')}"
                    + (f" [{i.get('id')}]" if i.get("id") else ""),
                    noun="subscribed integration",
                    total=data.get("total"),
                    empty_msg=f"No integrations are subscribed to the {cve_id} feed.",
                )
                return out + page_footer(data)

            # subscribe_integration / unsubscribe_integration
            integration_name = str(integration_name or "").strip()
            if not integration_name:
                return (
                    "Error: integration_name is required (the firewall/"
                    "remediation integration endpoint name from "
                    "list_crowdsec_integrations)."
                )
            if act == "subscribe_integration":
                _, err = await safe_request(
                    http,
                    "post",
                    f"{SERVICE_API}{base}/integrations",
                    _PROVIDER,
                    f"subscribe integration to {cve_id}",
                    headers=service_headers,
                    json={"name": integration_name},
                )
                if err:
                    return err
                return (
                    f"Integration '{integration_name}' subscribed to the "
                    f"{cve_id} feed - IPs exploiting the CVE are now pushed to "
                    "it automatically."
                )
            if any(c in integration_name for c in "/?#%\t"):
                return "Error: invalid integration_name."
            _, err = await safe_request(
                http,
                "delete",
                f"{SERVICE_API}{base}/integrations/{quote_path(integration_name)}",
                _PROVIDER,
                f"unsubscribe integration from {cve_id}",
                headers=service_headers,
            )
            if err:
                return err
            return f"Integration '{integration_name}' unsubscribed from the {cve_id} feed."

    async def manage_crowdsec_tracker(
        action: str,
        name: str = "",
        query: str = "",
        integration_name: str = "",
        since: str = "14d",
        sort_by: str = "",
        page: int = 1,
        size: int = 25,
    ) -> str:
        """Tracker surface for fingerprints, vendors, products, and tags."""
        if service_headers is None:
            return MISSING_SERVICE_KEY_MSG
        act = str(action or "").strip().lower()
        if act not in _TRACKER_ACTIONS:
            return f"Error: action must be one of {', '.join(_TRACKER_ACTIONS)}."
        name = str(name or "").strip()
        needs_name = act not in (
            "list_fingerprints",
            "list_vendors",
            "list_products",
            "list_tags",
            "list_tracker_tags",
        )
        if needs_name and not name:
            return f"Error: 'name' is required for action '{act}'."
        page_params: dict[str, Any] = {
            "page": clamp(page, default=1, hi=10_000),
            "size": clamp(size, default=25, hi=100),
        }
        if str(query or "").strip():
            page_params["query"] = str(query).strip()
        if str(sort_by or "").strip():
            page_params["sort_by"] = str(sort_by).strip()

        list_paths = {
            "list_fingerprints": ("/fingerprints", "fingerprint rule"),
            "list_vendors": ("/vendors", "vendor"),
            "list_products": ("/products", "product"),
            "list_tags": ("/tags", "tag"),
            "list_tracker_tags": ("/tracker-tags", "tracker tag"),
        }
        impact_paths = {
            "get_vendor": "/vendors",
            "get_product": "/products",
            "get_tag": "/tags",
            "get_tracker_tag": "/tracker-tags",
        }

        async with client(timeout=60) as http:
            if act in list_paths:
                path, noun = list_paths[act]
                data, err = await _get(http, path, f"list {noun}s", page_params)
                if err:
                    return err
                items = data.get("items") or []

                def _fmt(item: dict[str, Any]) -> str:
                    if act == "list_fingerprints":
                        return _format_cve(item)
                    return (
                        f"  {item.get('value') or item.get('name', '?')}"
                        + (
                            f" | CVEs: {item['nb_cves']}"
                            if item.get("nb_cves") is not None
                            else ""
                        )
                        + (
                            f" | fingerprints: {item['nb_fingerprints']}"
                            if item.get("nb_fingerprints") is not None
                            else ""
                        )
                        + (
                            f" | IPs: {item['nb_ips']}"
                            if item.get("nb_ips") is not None
                            else ""
                        )
                    )

                out = format_entity_list(
                    items,
                    _fmt,
                    noun=noun,
                    total=data.get("total"),
                    empty_msg=f"No {noun}s found.",
                )
                return out + page_footer(data)

            if act == "get_fingerprint":
                data, err = await _get(
                    http,
                    f"/fingerprints/{quote_path(name)}",
                    f"get fingerprint rule {name}",
                )
                if err:
                    return err
                lines = [_format_cve(data).strip()]
                if data.get("description"):
                    lines.append(f"  {trunc(data['description'], 300)}")
                if data.get("crowdsec_analysis"):
                    lines.append(
                        f"  CrowdSec analysis: {trunc(data['crowdsec_analysis'], 400)}"
                    )
                return "\n".join(lines)

            if act == "get_fingerprint_ips":
                since = str(since or "14d").strip()
                dur_err = validate_service_duration(since)
                if dur_err:
                    return dur_err
                data, err = await _get(
                    http,
                    f"/fingerprints/{quote_path(name)}/ips-details",
                    f"get IPs for fingerprint {name}",
                    {**page_params, "since": since},
                )
                if err:
                    return err
                out = format_entity_list(
                    data.get("items") or [],
                    _format_tracked_ip,
                    noun="matching IP",
                    total=data.get("total"),
                    empty_msg=f"No IPs matched fingerprint rule {name} in the last {since}.",
                )
                return out + page_footer(data)

            if act == "get_vendor_stats":
                data, err = await _get(
                    http, f"/vendors/{quote_path(name)}/stats", f"get vendor stats {name}"
                )
                if err:
                    return err
                products = data.get("top_products") or []
                lines = [
                    f"Vendor {data.get('value', name)}: "
                    f"{data.get('nb_cves', 0)} tracked CVE(s), "
                    f"{data.get('nb_fingerprints', 0)} fingerprint rule(s), "
                    f"{data.get('nb_ips', 0)} attacking IP(s) "
                    f"({data.get('nb_ips_cves', 0)} via CVEs, "
                    f"{data.get('nb_ips_fingerprints', 0)} via fingerprints)."
                ]
                if products:
                    lines.append(
                        "Top products: "
                        + ", ".join(
                            f"{p.get('value', '?')} ({p.get('nb_ips', 0)} IPs)"
                            for p in products[:8]
                            if isinstance(p, dict)
                        )
                    )
                return "\n".join(lines)

            if act == "get_vendor_ips":
                since = str(since or "14d").strip()
                dur_err = validate_service_duration(since)
                if dur_err:
                    return dur_err
                data, err = await _get(
                    http,
                    f"/vendors/{quote_path(name)}/ips-details",
                    f"get IPs targeting vendor {name}",
                    {**page_params, "since": since},
                )
                if err:
                    return err
                out = format_entity_list(
                    data.get("items") or [],
                    _format_tracked_ip,
                    noun="attacking IP",
                    total=data.get("total"),
                    empty_msg=f"No IPs targeting vendor {name} in the last {since}.",
                )
                return out + page_footer(data)

            if act in impact_paths:
                data, err = await _get(
                    http,
                    f"{impact_paths[act]}/{quote_path(name)}",
                    f"get impact for {name}",
                    page_params,
                )
                if err:
                    return err
                items = data.get("items") or []
                out = format_entity_list(
                    items,
                    _format_cve,
                    noun="tracked CVE / fingerprint",
                    total=data.get("total"),
                    empty_msg=f"Nothing tracked for '{name}'.",
                )
                return out + page_footer(data)

            # fingerprint / vendor integration feed subscriptions
            integration_name = str(integration_name or "").strip()
            if not integration_name:
                return (
                    "Error: integration_name is required (from "
                    "list_crowdsec_integrations)."
                )
            domain = "fingerprints" if "fingerprint" in act else "vendors"
            if act.startswith("subscribe"):
                _, err = await safe_request(
                    http,
                    "post",
                    f"{SERVICE_API}/{domain}/{quote_path(name)}/integrations",
                    _PROVIDER,
                    f"subscribe integration to {name}",
                    headers=service_headers,
                    json={"name": integration_name},
                )
                if err:
                    return err
                return (
                    f"Integration '{integration_name}' subscribed to the "
                    f"{domain[:-1]} '{name}' feed."
                )
            _, err = await safe_request(
                http,
                "delete",
                f"{SERVICE_API}/{domain}/{quote_path(name)}/integrations/"
                f"{quote_path(integration_name)}",
                _PROVIDER,
                f"unsubscribe integration from {name}",
                headers=service_headers,
            )
            if err:
                return err
            return (
                f"Integration '{integration_name}' unsubscribed from the "
                f"{domain[:-1]} '{name}' feed."
            )

    executor.register("list_crowdsec_cves", list_crowdsec_cves)
    executor.register("get_crowdsec_cve", get_crowdsec_cve)
    executor.register("get_crowdsec_cve_ips", get_crowdsec_cve_ips)
    executor.register(
        "list_crowdsec_exploitation_events", list_crowdsec_exploitation_events
    )
    executor.register("manage_crowdsec_cve_tracking", manage_crowdsec_cve_tracking)
    executor.register("manage_crowdsec_tracker", manage_crowdsec_tracker)

    return [
        ToolDefinition(
            name="list_crowdsec_cves",
            description=(
                "Browse the CrowdSec Live Exploit Tracker: the CVEs currently "
                "being exploited in the wild, each with a CrowdSec score "
                "(0-10), exploitation phase (e.g. mass_exploitation, "
                "targeted_exploitation, background_noise), momentum, and the "
                "number of attacking IPs. Filter by free-text query (product/"
                "vendor, e.g. 'wordpress') or phase; sort by trending to see "
                "what is heating up. The vulnerability-prioritization signal: "
                "is anyone actually exploiting this?"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-text search, e.g. 'wordpress' or 'CVE-2024-3400'",
                    },
                    "exploitation_phase": {
                        "type": "string",
                        "enum": list(_EXPLOITATION_PHASES),
                        "description": "Only CVEs currently in this phase",
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": list(_CVE_SORTS),
                        "default": "trending",
                    },
                    "page": {"type": "integer", "default": 1},
                    "size": {"type": "integer", "default": 25, "description": "Page size (max 100)"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_crowdsec_cve",
            description=(
                "Full Live Exploit Tracker detail for one CVE: CrowdSec score, "
                "exploitation phase, opportunity score (opportunistic vs "
                "targeted), momentum, attacking-IP count, CVSS, public-exploit "
                "flag, CrowdSec's written analysis, affected vendor/product "
                "components, CWEs, and recent tracker events."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cve_id": {"type": "string", "description": "CVE id, e.g. CVE-2024-3400"},
                },
                "required": ["cve_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_crowdsec_cve_ips",
            description=(
                "List the IPs actively exploiting a CVE right now (from the "
                "Live Exploit Tracker), with reputation, AS, country, and last "
                "seen. Use during patch-gap windows to block attackers before "
                "patching completes - chain into add_crowdsec_blocklist_ips or "
                "create_crowdsec_decision."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cve_id": {"type": "string", "description": "CVE id, e.g. CVE-2024-3400"},
                    "since": {
                        "type": "string",
                        "default": "14d",
                        "description": "Only IPs seen within this window - duration in h/d (e.g. 24h, 7d)",
                    },
                    "page": {"type": "integer", "default": 1},
                    "size": {"type": "integer", "default": 50, "description": "Page size (max 100)"},
                },
                "required": ["cve_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_crowdsec_exploitation_events",
            description=(
                "The exploitation phase-change feed: which CVEs moved between "
                "Live Exploit Tracker phases (e.g. early_exploitation -> "
                "mass_exploitation) and when. The ideal monitoring trigger for "
                "'alert me when a CVE goes into mass exploitation' - filter by "
                "cve_id or by the new phase."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "since": {
                        "type": "string",
                        "default": "30d",
                        "description": "Event window - duration in h/d (e.g. 24h, 7d)",
                    },
                    "cve_id": {"type": "string", "description": "Only events for this CVE"},
                    "new_phase": {
                        "type": "string",
                        "enum": list(_EXPLOITATION_PHASES),
                        "description": "Only transitions INTO this phase",
                    },
                    "page": {"type": "integer", "default": 1},
                    "size": {"type": "integer", "default": 50, "description": "Page size (max 100)"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_crowdsec_cve_tracking",
            description=(
                "Per-CVE Live Exploit Tracker operations. Read actions: "
                "'protect_rules' (published detection/protection rules with "
                "links), 'timeline' (attack signal counts over 1/7/30 days), "
                "'indicators' (top IOCs like exploited HTTP paths), 'ip_stats' "
                "(faceted stats of the exploiting IPs: reputation/country/AS), "
                "'list_integrations' (which firewall integrations consume this "
                "CVE's IP feed). Write actions: 'subscribe_integration' / "
                "'unsubscribe_integration' - auto-push the IPs exploiting the "
                "CVE to a firewall/remediation integration endpoint (by name, "
                "from list_crowdsec_integrations)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": list(_CVE_TRACKING_ACTIONS),
                        "description": "Operation to perform",
                    },
                    "cve_id": {"type": "string", "description": "CVE id, e.g. CVE-2024-3400"},
                    "integration_name": {
                        "type": "string",
                        "description": "Integration endpoint name (subscribe/unsubscribe)",
                    },
                    "since": {
                        "type": "string",
                        "default": "14d",
                        "description": "Stats window - duration in h/d (ip_stats)",
                    },
                    "timeline_days": {
                        "type": "integer",
                        "enum": [1, 7, 30],
                        "default": 7,
                        "description": "Timeline range in days (timeline)",
                    },
                    "page": {"type": "integer", "default": 1},
                    "size": {"type": "integer", "default": 50},
                },
                "required": ["action", "cve_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_crowdsec_tracker",
            description=(
                "Explore the Live Exploit Tracker beyond CVEs. List/get "
                "actions: 'list_fingerprints'/'get_fingerprint'/"
                "'get_fingerprint_ips' (behavioral fingerprint rules for "
                "attacks without a CVE, same scoring as CVEs), 'list_vendors'/"
                "'get_vendor' (tracked CVEs+fingerprints for a vendor)/"
                "'get_vendor_stats' (impact summary + top products)/"
                "'get_vendor_ips' (IPs attacking that vendor's products - your "
                "supply-chain exposure), 'list_products'/'get_product', "
                "'list_tags'/'get_tag' and 'list_tracker_tags'/"
                "'get_tracker_tag' (thematic groupings). Write actions: "
                "'subscribe_fingerprint_integration'/'subscribe_vendor_"
                "integration' (+ matching unsubscribe) to auto-feed a firewall "
                "integration with the IPs behind a fingerprint rule or vendor. "
                "'name' is the rule/vendor/product/tag identifier."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": list(_TRACKER_ACTIONS),
                        "description": "Operation to perform",
                    },
                    "name": {
                        "type": "string",
                        "description": "Fingerprint rule / vendor / product / tag name (get_* and subscriptions)",
                    },
                    "query": {"type": "string", "description": "Free-text filter (list_* actions)"},
                    "integration_name": {
                        "type": "string",
                        "description": "Integration endpoint name (subscribe/unsubscribe)",
                    },
                    "since": {
                        "type": "string",
                        "default": "14d",
                        "description": "IP window - duration in h/d (get_*_ips)",
                    },
                    "sort_by": {
                        "type": "string",
                        "description": "Sort field for list actions (e.g. trending, nb_ips)",
                    },
                    "page": {"type": "integer", "default": 1},
                    "size": {"type": "integer", "default": 25},
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
