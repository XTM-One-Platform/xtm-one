"""CrowdSec Service API tools - integration endpoints + remediation metrics.

Integration endpoints are the Blocklist-as-a-Service delivery mechanism:
firewalls and remediation components pull their subscribed blocklists from a
per-endpoint URL. Remediation metrics quantify what the fleet dropped.

* ``list_crowdsec_integrations``        - GET /integrations
* ``manage_crowdsec_integration``       - get/create/update/delete + content
                                          preview
* ``get_crowdsec_remediation_metrics``  - GET /metrics/remediation
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
    page_footer,
    preview_text,
    trunc,
    validate_iso_datetime,
)

_ACTIONS = ("get", "create", "update", "delete", "content")

_INTEGRATION_TYPES = ("firewall_integration", "remediation_component_integration")

_OUTPUT_FORMATS = (
    "plain_text",
    "f5",
    "remediation_component",
    "fortigate",
    "paloalto",
    "checkpoint",
    "cisco",
    "juniper",
    "mikrotik",
    "pfsense",
    "opnsense",
    "sophos",
)

def _format_integration(i: dict[str, Any]) -> str:
    blocklists = i.get("blocklists") or []
    allowlists = i.get("allowlists") or []
    return (
        f"  {i.get('name', '?')} [{i.get('id', '?')}]"
        f" | type: {i.get('entity_type', '?')}"
        f" | format: {i.get('output_format', '?')}"
        + (f" | blocklists: {len(blocklists)}" if blocklists else "")
        + (f" | allowlists: {len(allowlists)}" if allowlists else "")
        + (f" | last pull: {dt(i.get('last_pull'))}" if i.get("last_pull") else " | never pulled")
        + (f"\n    {trunc(i.get('description'), 120)}" if i.get("description") else "")
    )


def _format_metric_block(label: str, metrics: list[Any]) -> list[str]:
    lines: list[str] = []
    for m in metrics or []:
        if not isinstance(m, dict):
            continue
        unit = m.get("unit")
        unit_str = unit if isinstance(unit, str) else (unit or {}).get("name", "")
        progression = m.get("progression")
        lines.append(
            f"  {label}"
            + (f" ({m['label']})" if m.get("label") else "")
            + f": {m.get('total', 0)} {unit_str}".rstrip()
            + (
                f" | vs previous period: {'+' if progression >= 0 else ''}{progression}%"
                if isinstance(progression, (int, float))
                else ""
            )
        )
    return lines


def register(
    service_headers: dict[str, str] | None,
    executor: CallableToolExecutor,
) -> list[ToolDefinition]:
    """Register the integration-endpoint + metrics tools."""

    async def list_crowdsec_integrations(page: int = 1, size: int = 50) -> str:
        """Firewall/remediation integration endpoints + their subscriptions."""
        if service_headers is None:
            return MISSING_SERVICE_KEY_MSG
        async with client() as http:
            resp, err = await safe_request(
                http,
                "get",
                f"{SERVICE_API}/integrations",
                _PROVIDER,
                "list integration endpoints",
                headers=service_headers,
                params={
                    "page": clamp(page, default=1, hi=10_000),
                    "size": clamp(size, default=50, hi=100),
                },
            )
            if err:
                return err
            data = resp.json() or {}
        items = data.get("items") or []
        out = format_entity_list(
            items,
            _format_integration,
            noun="integration endpoint",
            total=data.get("total"),
            empty_msg=(
                "No integration endpoints exist yet - create one with "
                "manage_crowdsec_integration action='create'."
            ),
        )
        return out + page_footer(data)

    async def manage_crowdsec_integration(
        action: str,
        integration_id: str = "",
        name: str = "",
        description: str = "",
        entity_type: str = "firewall_integration",
        output_format: str = "plain_text",
        pull_limit: int = 0,
        force: bool = False,
    ) -> str:
        """Integration endpoint lifecycle: get/create/update/delete/content."""
        if service_headers is None:
            return MISSING_SERVICE_KEY_MSG
        act = str(action or "").strip().lower()
        if act not in _ACTIONS:
            return f"Error: action must be one of {', '.join(_ACTIONS)}."
        integration_id = str(integration_id or "").strip()
        if act != "create" and (
            not integration_id or any(c in integration_id for c in "/?#% \t")
        ):
            return (
                "Error: integration_id (from list_crowdsec_integrations) is "
                f"required for action '{act}'."
            )
        base = f"{SERVICE_API}/integrations"

        async with client(timeout=60) as http:
            if act == "get":
                resp, err = await safe_request(
                    http,
                    "get",
                    f"{base}/{integration_id}",
                    _PROVIDER,
                    f"get integration endpoint {integration_id}",
                    headers=service_headers,
                )
                if err:
                    return err
                i = resp.json() or {}
                lines = [_format_integration(i).strip()]
                for kind in ("blocklists", "allowlists", "cves", "fingerprints", "vendors"):
                    subs = i.get(kind) or []
                    if subs:
                        shown = ", ".join(
                            str(s.get("name") or s.get("id") or "?")
                            for s in subs[:8]
                            if isinstance(s, dict)
                        )
                        lines.append(f"  Subscribed {kind}: {shown}")
                return "\n".join(lines)

            if act == "create":
                name = str(name or "").strip()
                description = str(description or "").strip()
                if not name or not description:
                    return "Error: 'name' and 'description' are required for 'create'."
                etype = str(entity_type or "firewall_integration").strip().lower()
                if etype not in _INTEGRATION_TYPES:
                    return (
                        f"Error: entity_type must be one of {', '.join(_INTEGRATION_TYPES)}."
                    )
                fmt = str(output_format or "plain_text").strip().lower()
                if fmt not in _OUTPUT_FORMATS:
                    return (
                        f"Error: output_format must be one of {', '.join(_OUTPUT_FORMATS)}."
                    )
                body: dict[str, Any] = {
                    "name": name,
                    "description": description,
                    "entity_type": etype,
                    "output_format": fmt,
                }
                if pull_limit:
                    body["pull_limit"] = max(1, int(pull_limit))
                resp, err = await safe_request(
                    http,
                    "post",
                    base,
                    _PROVIDER,
                    f"create integration endpoint '{name}'",
                    headers=service_headers,
                    json=body,
                )
                if err:
                    return err
                created = resp.json() or {}
                out = (
                    f"Integration endpoint created: {name} "
                    f"[{created.get('id', '?')}] ({etype}, {fmt})."
                )
                creds = created.get("credentials") or {}
                if creds:
                    out += (
                        "\nEndpoint credentials were generated - retrieve them "
                        "from the CrowdSec Console (Integrations page) to "
                        "configure the firewall."
                    )
                out += (
                    "\nSubscribe blocklists to it with manage_crowdsec_blocklist "
                    "action='subscribe' entity_type='" + etype + "'."
                )
                return out

            if act == "update":
                body = {}
                if str(name or "").strip():
                    body["name"] = str(name).strip()
                if str(description or "").strip():
                    body["description"] = str(description).strip()
                fmt = str(output_format or "").strip().lower()
                if fmt:
                    if fmt not in _OUTPUT_FORMATS:
                        return (
                            "Error: output_format must be one of "
                            f"{', '.join(_OUTPUT_FORMATS)}."
                        )
                    body["output_format"] = fmt
                if pull_limit:
                    body["pull_limit"] = max(1, int(pull_limit))
                if not body:
                    return (
                        "Error: nothing to update - pass name, description, "
                        "output_format, or pull_limit."
                    )
                _, err = await safe_request(
                    http,
                    "patch",
                    f"{base}/{integration_id}",
                    _PROVIDER,
                    f"update integration endpoint {integration_id}",
                    headers=service_headers,
                    json=body,
                )
                if err:
                    return err
                return f"Integration endpoint {integration_id} updated."

            if act == "delete":
                _, err = await safe_request(
                    http,
                    "delete",
                    f"{base}/{integration_id}",
                    _PROVIDER,
                    f"delete integration endpoint {integration_id}",
                    headers=service_headers,
                    params={"force": bool(force)},
                )
                if err:
                    return err
                return (
                    f"Integration endpoint {integration_id} permanently deleted"
                    + (" (subscriptions force-removed)" if force else "")
                    + "."
                )

            # content preview
            resp, err = await safe_request(
                http,
                "get",
                f"{base}/{integration_id}/content",
                _PROVIDER,
                f"preview integration content {integration_id}",
                headers=service_headers,
            )
            if err:
                return err
            return preview_text(
                resp.text or "",
                f"Integration endpoint {integration_id} would serve",
            )

    async def get_crowdsec_remediation_metrics(
        start_date: str = "", end_date: str = ""
    ) -> str:
        """How much malicious traffic the blocklists dropped (ROI reporting)."""
        if service_headers is None:
            return MISSING_SERVICE_KEY_MSG
        params: dict[str, Any] = {}
        for value, param in ((start_date, "start_date"), (end_date, "end_date")):
            value = str(value or "").strip()
            if value:
                date_err = validate_iso_datetime(value, param)
                if date_err:
                    return date_err
                params[param] = value
        async with client(timeout=60) as http:
            resp, err = await safe_request(
                http,
                "get",
                f"{SERVICE_API}/metrics/remediation",
                _PROVIDER,
                "get remediation metrics",
                headers=service_headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        raw = data.get("raw") or {}
        computed = data.get("computed") or {}
        stats = data.get("stats") or {}
        lines = ["CrowdSec remediation metrics"]
        if params:
            lines[0] += (
                f" ({params.get('start_date', 'default')} -> "
                f"{params.get('end_date', 'now')})"
            )
        if isinstance(stats, dict) and stats:
            dropped = stats.get("dropped_rate")
            allowed = stats.get("allowed_rate")
            if dropped is not None or allowed is not None:
                lines.append(
                    f"  Dropped rate: {dropped if dropped is not None else '?'}%"
                    f" | allowed rate: {allowed if allowed is not None else '?'}%"
                )
        if isinstance(raw, dict):
            for key in ("dropped", "processed", "blocked"):
                lines += _format_metric_block(key, raw.get(key) or [])
        if isinstance(computed, dict):
            lines += _format_metric_block(
                "estimated dropped", computed.get("dropped") or []
            )
            saved = computed.get("saved") or {}
            if isinstance(saved, dict) and saved:
                saved_bits: list[str] = []
                for key in ("egress_traffic", "log_lines", "storage"):
                    metric = saved.get(key)
                    if isinstance(metric, dict) and metric.get("total") is not None:
                        unit = metric.get("unit")
                        unit_str = (
                            unit if isinstance(unit, str) else (unit or {}).get("name", "")
                        )
                        saved_bits.append(f"{key}: {metric['total']} {unit_str}".rstrip())
                if saved_bits:
                    lines.append("  Estimated savings - " + " | ".join(saved_bits))
            prevented = computed.get("prevented") or []
            lines += _format_metric_block("prevented attacks", prevented)
        if len(lines) == 1:
            lines.append("  No remediation metrics reported for this window.")
        return "\n".join(lines)

    executor.register("list_crowdsec_integrations", list_crowdsec_integrations)
    executor.register("manage_crowdsec_integration", manage_crowdsec_integration)
    executor.register(
        "get_crowdsec_remediation_metrics", get_crowdsec_remediation_metrics
    )

    return [
        ToolDefinition(
            name="list_crowdsec_integrations",
            description=(
                "List the organization's CrowdSec integration endpoints - the "
                "pull URLs firewalls and remediation components use to consume "
                "subscribed blocklists (Blocklist as a Service). Shows type, "
                "output format, subscription counts, and last pull time (a "
                "'never pulled' endpoint is not wired to its device yet)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "default": 1},
                    "size": {"type": "integer", "default": 50, "description": "Page size (max 100)"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_crowdsec_integration",
            description=(
                "Manage a CrowdSec integration endpoint. Actions: 'get' (full "
                "detail incl. subscribed blocklists/allowlists/CVE feeds), "
                "'create' (name + description + entity_type + output_format "
                "matching the target device: fortigate, paloalto, checkpoint, "
                "cisco, pfsense, ... - the endpoint URL/credentials are then "
                "shown in the CrowdSec Console), 'update' (rename, change "
                "format, set pull_limit), 'delete' (permanent, cannot be "
                "undone; force=true also removes active subscriptions), "
                "'content' (preview exactly what the device would pull)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": list(_ACTIONS),
                        "description": "Operation to perform",
                    },
                    "integration_id": {
                        "type": "string",
                        "description": "Endpoint id (all actions except create)",
                    },
                    "name": {"type": "string", "description": "Endpoint name (create/update)"},
                    "description": {
                        "type": "string",
                        "description": "Endpoint description (create/update)",
                    },
                    "entity_type": {
                        "type": "string",
                        "enum": list(_INTEGRATION_TYPES),
                        "default": "firewall_integration",
                        "description": "Kind of consumer device (create)",
                    },
                    "output_format": {
                        "type": "string",
                        "enum": list(_OUTPUT_FORMATS),
                        "default": "plain_text",
                        "description": "Format the device expects (create/update)",
                    },
                    "pull_limit": {
                        "type": "integer",
                        "description": "Max entries served per pull (create/update)",
                    },
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": "Delete even with active subscriptions (delete)",
                    },
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_crowdsec_remediation_metrics",
            description=(
                "CrowdSec remediation metrics for ROI reporting: how much "
                "malicious traffic the fleet dropped vs allowed, raw and "
                "computed drop counts with period-over-period progression, "
                "estimated savings (egress traffic, log lines, storage), and "
                "prevented attacks. Defaults to the last day - pass ISO "
                "start_date/end_date for a custom window."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": "ISO 8601 window start, e.g. 2026-06-01T00:00:00Z",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "ISO 8601 window end (defaults to now)",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
