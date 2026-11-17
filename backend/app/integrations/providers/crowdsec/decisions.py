"""CrowdSec Service API tools - console decisions (fleet-wide remediation).

Console decisions push remediation (ban / captcha / throttle) to the
console-managed fleet: an organization, a tag of engines, or a single engine.

* ``list_crowdsec_decisions``   - GET /decisions (or /decisions/aggregated)
* ``create_crowdsec_decision``  - POST /decisions (org target auto-resolved
                                  from GET /info when omitted)
* ``delete_crowdsec_decision``  - DELETE /decisions/{uuid} or
                                  /decisions/aggregated/{id} (destructive)
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
    is_valid_decision_duration,
    is_valid_ip,
    page_footer,
)

_DECISION_TYPES = ("ban", "captcha", "throttle")
_TARGET_TYPES = ("org", "tag", "entity")


def _format_decision(d: dict[str, Any]) -> str:
    target = d.get("target") or {}
    return (
        f"  [{d.get('uuid') or d.get('id', '?')}] {d.get('type', '?')} "
        f"{d.get('scope', '?')}:{d.get('value', '?')} for {d.get('duration', '?')}"
        f" | target: {target.get('type', '?')}={target.get('value', '?')}"
        f" | scenario: {d.get('scenario', '?')} | origin: {d.get('origin', '?')}"
        + (f" | created: {dt(d.get('created_at'))}" if d.get("created_at") else "")
        + (f" | {d['country']}" if d.get("country") else "")
    )


def _format_aggregated(d: dict[str, Any]) -> str:
    target = d.get("target") or {}
    machines = d.get("machines") or {}
    return (
        f"  [{d.get('id', '?')}] {d.get('type', '?')} "
        f"{d.get('scope', '?')}:{d.get('value', '?')}"
        f" | {d.get('count', '?')} decision(s) across {len(machines)} machine(s)"
        f" | target: {target.get('type', '?')}={target.get('value', '?')}"
        f" | scenario: {d.get('scenario', '?')}"
        + (
            f" | first: {dt(d.get('first_created_at'))} / last expires: "
            f"{dt(d.get('last_expiration'))}"
            if d.get("first_created_at")
            else ""
        )
    )


def register(
    service_headers: dict[str, str] | None,
    executor: CallableToolExecutor,
) -> list[ToolDefinition]:
    """Register the console decision tools."""

    async def list_crowdsec_decisions(
        ips: list[str] | None = None,
        remediation_types: list[str] | None = None,
        created_at_from: str = "",
        aggregated: bool = False,
        page: int = 1,
        size: int = 50,
    ) -> str:
        """Review console decisions (bans/captchas/throttles) across the fleet."""
        if service_headers is None:
            return MISSING_SERVICE_KEY_MSG
        params: dict[str, Any] = {
            "page": clamp(page, default=1, hi=10_000),
            "size": clamp(size, default=50, hi=100),
        }
        cleaned_ips = [str(i).strip() for i in (ips or []) if str(i or "").strip()]
        invalid = [i for i in cleaned_ips if not is_valid_ip(i)]
        if invalid:
            return f"Error: invalid IP filter value(s): {', '.join(invalid[:10])}."
        if cleaned_ips:
            params["ips"] = cleaned_ips
        cleaned_types = [
            str(t).strip().lower()
            for t in (remediation_types or [])
            if str(t or "").strip()
        ]
        unknown_types = [t for t in cleaned_types if t not in _DECISION_TYPES]
        if unknown_types:
            return (
                f"Error: unknown remediation type(s): {', '.join(unknown_types)}. "
                f"Valid types: {', '.join(_DECISION_TYPES)}."
            )
        if cleaned_types:
            params["remediation_types"] = cleaned_types
        if str(created_at_from or "").strip():
            params["created_at_from"] = str(created_at_from).strip()
        path = "/decisions/aggregated" if aggregated else "/decisions"
        async with client() as http:
            resp, err = await safe_request(
                http,
                "get",
                f"{SERVICE_API}{path}",
                _PROVIDER,
                "list console decisions",
                headers=service_headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        items = data.get("items") or []
        formatter = _format_aggregated if aggregated else _format_decision
        out = format_entity_list(
            items,
            formatter,
            noun="console decision",
            total=data.get("total"),
            empty_msg="No console decisions match the given filters.",
        )
        return out + page_footer(data)

    async def create_crowdsec_decision(
        value: str,
        duration: str = "24h",
        decision_type: str = "ban",
        scenario: str = "manual",
        target_type: str = "org",
        target_value: str = "",
    ) -> str:
        """Push a console decision (e.g. an org-wide IP ban) to the fleet."""
        if service_headers is None:
            return MISSING_SERVICE_KEY_MSG
        value = str(value or "").strip()
        if not is_valid_ip(value):
            return f"Error: '{value}' is not a valid IP address to remediate."
        duration = str(duration or "24h").strip()
        if not is_valid_decision_duration(duration):
            return (
                f"Error: duration must be like '24h', '30m', '1h30m' or '7d' - "
                f"got '{duration}'."
            )
        decision_type = str(decision_type or "ban").strip().lower()
        if decision_type not in _DECISION_TYPES:
            return f"Error: decision_type must be one of {', '.join(_DECISION_TYPES)}."
        target_type = str(target_type or "org").strip().lower()
        if target_type not in _TARGET_TYPES:
            return f"Error: target_type must be one of {', '.join(_TARGET_TYPES)}."
        target_value = str(target_value or "").strip()
        scenario = str(scenario or "manual").strip()

        async with client() as http:
            if not target_value:
                if target_type != "org":
                    return (
                        f"Error: target_value is required for target_type "
                        f"'{target_type}' (a tag id or engine/entity id)."
                    )
                # Resolve the caller's organization id so an org-wide decision
                # works without the agent knowing the org id up front.
                info_resp, info_err = await safe_request(
                    http,
                    "get",
                    f"{SERVICE_API}/info",
                    _PROVIDER,
                    "resolve organization id",
                    headers=service_headers,
                )
                if info_err:
                    return info_err
                target_value = (info_resp.json() or {}).get("organization_id") or ""
                if not target_value:
                    return (
                        "Error: could not resolve the organization id from "
                        "GET /info - pass target_value explicitly."
                    )
            body = {
                "duration": duration,
                "origin": "console",
                "scenario": scenario,
                "scope": "ip",
                "type": decision_type,
                "value": value,
                "target": {"type": target_type, "value": target_value},
            }
            resp, err = await safe_request(
                http,
                "post",
                f"{SERVICE_API}/decisions",
                _PROVIDER,
                f"create console decision for {value}",
                headers=service_headers,
                json=body,
            )
            if err:
                return err
            created = resp.json() or {}
        return (
            f"Console decision created: {decision_type} {value} for {duration} "
            f"(target {target_type}={target_value}).\n"
            f"  Decision uuid: {created.get('uuid') or created.get('id', '?')}\n"
            "Engines pick the decision up on their next console sync."
        )

    async def delete_crowdsec_decision(
        decision_uuid: str, aggregated: bool = False
    ) -> str:
        """Revoke a console decision (permanent, cannot be undone)."""
        if service_headers is None:
            return MISSING_SERVICE_KEY_MSG
        decision_uuid = str(decision_uuid or "").strip()
        # Decision uuids/ids are opaque strings; forbid path-breaking chars.
        if not decision_uuid or any(c in decision_uuid for c in "/?#% \t"):
            return "Error: a valid decision_uuid is required (from list_crowdsec_decisions)."
        path = (
            f"/decisions/aggregated/{decision_uuid}"
            if aggregated
            else f"/decisions/{decision_uuid}"
        )
        async with client() as http:
            _, err = await safe_request(
                http,
                "delete",
                f"{SERVICE_API}{path}",
                _PROVIDER,
                f"delete console decision {decision_uuid}",
                headers=service_headers,
            )
            if err:
                return err
        return (
            f"Console decision {decision_uuid} permanently deleted - engines "
            "drop it on their next sync."
        )

    executor.register("list_crowdsec_decisions", list_crowdsec_decisions)
    executor.register("create_crowdsec_decision", create_crowdsec_decision)
    executor.register("delete_crowdsec_decision", delete_crowdsec_decision)

    return [
        ToolDefinition(
            name="list_crowdsec_decisions",
            description=(
                "List console decisions - the remediation (ban/captcha/"
                "throttle) currently pushed to the console-managed CrowdSec "
                "fleet. Filter by IPs, remediation types, or creation date; "
                "set aggregated=true to group identical decisions across "
                "machines (with per-machine state counts). Returns the "
                "decision uuid/id needed by delete_crowdsec_decision."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ips": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Only decisions on these IPs",
                    },
                    "remediation_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Only these remediation types, e.g. ['ban', 'captcha']",
                    },
                    "created_at_from": {
                        "type": "string",
                        "description": "Only decisions created after this ISO datetime (inclusive)",
                    },
                    "aggregated": {
                        "type": "boolean",
                        "default": False,
                        "description": "Group identical decisions across machines",
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
            name="create_crowdsec_decision",
            description=(
                "Push a console decision to the CrowdSec fleet: ban, captcha, "
                "or throttle an IP org-wide, per tag, or per engine. The "
                "decision propagates to every targeted Security Engine on its "
                "next console sync (and from there to bouncers/firewalls). "
                "Defaults: 24h ban targeting the whole organization (the org "
                "id is auto-resolved). Use for immediate fleet-wide "
                "containment of a confirmed-malicious IP."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "IP address to remediate",
                    },
                    "duration": {
                        "type": "string",
                        "default": "24h",
                        "description": "Decision duration, e.g. 30m, 24h, 1h30m, 7d",
                    },
                    "decision_type": {
                        "type": "string",
                        "enum": list(_DECISION_TYPES),
                        "default": "ban",
                        "description": "Remediation to apply",
                    },
                    "scenario": {
                        "type": "string",
                        "default": "manual",
                        "description": "Scenario label recorded on the decision (shown in the console)",
                    },
                    "target_type": {
                        "type": "string",
                        "enum": list(_TARGET_TYPES),
                        "default": "org",
                        "description": (
                            "Fleet scope: org (whole organization), tag (a tag "
                            "of engines), or entity (one engine)"
                        ),
                    },
                    "target_value": {
                        "type": "string",
                        "description": (
                            "Org id / tag id / engine id - optional for org "
                            "(auto-resolved from the Service API key)"
                        ),
                    },
                },
                "required": ["value"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="delete_crowdsec_decision",
            description=(
                "Revoke a console decision by uuid (from "
                "list_crowdsec_decisions). Permanent, cannot be undone - "
                "engines lift the remediation on their next console sync. Set "
                "aggregated=true when the id came from an aggregated listing "
                "to delete the whole group."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "decision_uuid": {
                        "type": "string",
                        "description": "Decision uuid (or aggregated group id)",
                    },
                    "aggregated": {
                        "type": "boolean",
                        "default": False,
                        "description": "The id refers to an aggregated decision group",
                    },
                },
                "required": ["decision_uuid"],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
