# SPDX-License-Identifier: Apache-2.0

"""Elastic Security integration – Agentic SOC for the SIEM + endpoint stack.

Full-fledged Elastic Security integration covering the detection →
investigation → response → intelligence loop on top of the Elastic Stack
(Kibana detection engine + Elastic Defend endpoint + Elasticsearch hunting),
designed as a best-in-class building block for an agentic security team.

Tool categories (≈26 tools):
  - Detection alerts (Kibana signals API): list, get, set status (open /
    acknowledged / closed), tag, assign
  - Detection rules: list, get, enable / disable
  - Cases: list, get, create, comment, update status
  - Exception lists & value lists: list exception lists / items, list value
    lists, add a value-list item (IOC enforcement primitive)
  - Endpoint response actions (Elastic Defend): list endpoints, isolate /
    release, run running-processes action, list / get response actions
  - Hunting (Elasticsearch): ES|QL (`_query`), EQL (`_eql/search`), and raw
    Query DSL (`_search`)

Authentication is an **Elasticsearch API key** (``Authorization: ApiKey
<encoded-key>`` plus the ``kbn-xsrf`` header for Kibana). The integration
stores the Kibana URL, an optional Elasticsearch URL (for hunting), an optional
Kibana space, and the API key; the compound
``kibana_url||es_url||api_key||space`` token is assembled by
``integration_tools._get_token``. Security operations target the Kibana APIs
(space-aware) and hunting targets the Elasticsearch APIs.
"""

from __future__ import annotations

import json as _json
import logging
from typing import Any
from urllib.parse import quote

from app.core.egress import egress_client
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "Elastic Security"
_DEFAULT_TIMEOUT = 30
_HUNT_TIMEOUT = 120
_ACTION_TIMEOUT = 60

_ALERT_STATUSES: frozenset[str] = frozenset({"open", "acknowledged", "closed"})
_ALERT_SEVERITIES: frozenset[str] = frozenset({"low", "medium", "high", "critical"})
_CASE_STATUSES: frozenset[str] = frozenset({"open", "in-progress", "closed"})
_CASE_SEVERITIES: frozenset[str] = frozenset({"low", "medium", "high", "critical"})
_OWNER = "securitySolution"

_ES_URL_MISSING = (
    "Error: this hunting tool requires the Elasticsearch URL to be configured "
    "on the integration (the 'es_url' field). Add it and try again."
)

_SEVERITY_ICONS: dict[str, str] = {
    "critical": "🔴",
    "high": "🔴",
    "medium": "🟠",
    "low": "🟡",
    "informational": "🔵",
}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _trunc(text: Any, max_len: int = 300) -> str:
    # Only None / empty-string are "empty"; a falsy-but-meaningful value such as
    # a numeric 0 or False (e.g. an ES|QL COUNT cell) must still render.
    if text is None or text == "":
        return ""
    text = str(text)
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _dt(value: Any) -> str:
    return str(value or "")[:19]


def _sev(severity: str | None) -> str:
    return _SEVERITY_ICONS.get((severity or "").lower(), "⚪")


def _field(source: dict, dotted: str) -> Any:
    """Read an ECS/alert field that may be stored flat (``a.b.c``) or nested.

    Elastic alert documents return ``kibana.alert.*`` fields as nested objects
    in ``_source`` but some pipelines flatten them, so try the flat key first
    then walk the nested path.
    """
    if not isinstance(source, dict):
        return None
    if dotted in source:
        return source[dotted]
    cur: Any = source
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _hits(resp: dict | None) -> list:
    if not isinstance(resp, dict):
        return []
    hits = resp.get("hits")
    if isinstance(hits, dict):
        inner = hits.get("hits")
        if isinstance(inner, list):
            return inner
    return []


def _total_hits(resp: dict | None) -> int | None:
    if not isinstance(resp, dict):
        return None
    total = (resp.get("hits") or {}).get("total")
    if isinstance(total, dict):
        return total.get("value")
    if isinstance(total, int):
        return total
    return None


def _err_detail(resp: dict | None) -> str:
    """Surface a Kibana/ES error message from a (possibly 200) response body."""
    if not isinstance(resp, dict):
        return ""
    msg = resp.get("message") or resp.get("error")
    if isinstance(msg, dict):
        msg = msg.get("reason") or _json.dumps(msg, default=str)
    return str(msg)[:400] if msg else ""


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Elastic Security tools.

    ``token`` is the compound ``kibana_url||es_url||api_key||space`` assembled
    by ``integration_tools._get_token`` from the integration's stored URLs, API
    key, and Kibana space.
    """
    kibana_url, es_url, api_key, space = parse_compound_token(token, 4)
    kibana_url = (kibana_url or "").rstrip("/")
    es_url = (es_url or "").rstrip("/")
    space = (space or "default").strip() or "default"
    # The space id is interpolated into the Kibana path (``/s/{space}``); encode
    # it so a value containing ``/`` or other reserved characters can't alter
    # the request path.
    space_prefix = "" if space == "default" else f"/s/{quote(space, safe='')}"
    kibana_base = f"{kibana_url}{space_prefix}"

    kbn_headers = {
        "Authorization": f"ApiKey {api_key}",
        "kbn-xsrf": "true",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    es_headers = {
        "Authorization": f"ApiKey {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    async def _kbn(
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> tuple[dict | None, str | None]:
        """Authenticated Kibana request returning ``(json, error)``."""
        if not kibana_url or not api_key:
            return None, (
                "Error: the Elastic Security integration is missing the Kibana "
                "URL or API key."
            )
        async with egress_client(timeout=timeout) as client:
            resp, err = await safe_request(
                client,
                method,
                f"{kibana_base}{path}",
                _PROVIDER,
                action,
                headers=kbn_headers,
                params=params,
                json=json,
            )
            if err:
                return None, err
            try:
                return resp.json(), None
            except Exception:
                return {}, None

    async def _es(
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        timeout: int = _HUNT_TIMEOUT,
    ) -> tuple[dict | None, str | None]:
        """Authenticated Elasticsearch request returning ``(json, error)``."""
        if not es_url:
            return None, _ES_URL_MISSING
        async with egress_client(timeout=timeout) as client:
            resp, err = await safe_request(
                client,
                method,
                f"{es_url}{path}",
                _PROVIDER,
                action,
                headers=es_headers,
                params=params,
                json=json,
            )
            if err:
                return None, err
            try:
                return resp.json(), None
            except Exception:
                return {}, None

    # ══════════════════════════════════════════════════════════════════════
    # 0. DETECTION ALERTS
    # ══════════════════════════════════════════════════════════════════════

    def _fmt_alert(hit: dict) -> str:
        s = hit.get("_source", {}) or {}
        rule = _field(s, "kibana.alert.rule.name") or "Unknown rule"
        sev = _field(s, "kibana.alert.severity") or "N/A"
        status = _field(s, "kibana.alert.workflow_status") or "open"
        risk = _field(s, "kibana.alert.risk_score")
        host = _field(s, "host.name") or "N/A"
        user = _field(s, "user.name")
        alert_id = _field(s, "kibana.alert.uuid") or hit.get("_id", "?")
        ts = _field(s, "@timestamp")
        line = (
            f"  {_sev(sev)} [{sev}] {status} — {rule}\n"
            f"    id: {alert_id} | Host: {host}"
        )
        if user:
            line += f" | User: {user}"
        line += f" | Risk: {risk if risk is not None else 'N/A'} | {_dt(ts)}"
        reason = _field(s, "kibana.alert.reason")
        if reason:
            line += f"\n    {_trunc(reason, 220)}"
        return line

    async def list_elastic_alerts(
        status: str = "open",
        severity: str = "",
        query: str = "",
        days: int = 7,
        count: int = 25,
    ) -> str:
        """List Elastic Security detection alerts.

        Filter by status (open, acknowledged, closed), severity (low, medium,
        high, critical), a free-text KQL/Lucene ``query``, and a look-back
        window in ``days``. Pass an empty status to match every status.
        """
        status = (status or "").strip().lower()
        if status and status not in _ALERT_STATUSES:
            return (
                f"Error: invalid status '{status}'. Use one of: "
                f"{', '.join(sorted(_ALERT_STATUSES))} (or empty for any)."
            )
        severity = (severity or "").strip().lower()
        if severity and severity not in _ALERT_SEVERITIES:
            return (
                f"Error: invalid severity '{severity}'. Use one of: "
                f"{', '.join(sorted(_ALERT_SEVERITIES))}."
            )
        must: list[dict[str, Any]] = []
        if status:
            must.append({"term": {"kibana.alert.workflow_status": status}})
        if severity:
            must.append({"term": {"kibana.alert.severity": severity}})
        if query:
            must.append({"query_string": {"query": query}})
        must.append({"range": {"@timestamp": {"gte": f"now-{max(days, 1)}d"}}})

        body = {
            "query": {"bool": {"must": must}},
            "size": min(count, 100),
            "sort": [{"@timestamp": {"order": "desc"}}],
        }
        resp, err = await _kbn(
            "post", "/api/detection_engine/signals/search", "search alerts", json=body
        )
        if err:
            return err
        hits = _hits(resp)
        return format_entity_list(
            hits, _fmt_alert, noun="alert", total=_total_hits(resp)
        )

    async def get_elastic_alert(alert_id: str) -> str:
        """Get full details of a detection alert by its alert UUID (or _id)."""
        body = {
            "query": {
                "bool": {
                    "should": [
                        {"term": {"kibana.alert.uuid": alert_id}},
                        {"term": {"_id": alert_id}},
                    ],
                    "minimum_should_match": 1,
                }
            },
            "size": 1,
        }
        resp, err = await _kbn(
            "post",
            "/api/detection_engine/signals/search",
            f"get alert {alert_id}",
            json=body,
        )
        if err:
            return err
        hits = _hits(resp)
        if not hits:
            return f"Alert {alert_id} not found."
        s = hits[0].get("_source", {}) or {}
        rule = _field(s, "kibana.alert.rule.name") or "Unknown rule"
        lines = [
            f"Alert: {rule}",
            f"ID: {_field(s, 'kibana.alert.uuid') or hits[0].get('_id', '?')}",
            f"Severity: {_sev(_field(s, 'kibana.alert.severity'))} "
            f"{_field(s, 'kibana.alert.severity') or 'N/A'} | "
            f"Risk score: {_field(s, 'kibana.alert.risk_score') or 'N/A'}",
            f"Status: {_field(s, 'kibana.alert.workflow_status') or 'open'}",
            f"Host: {_field(s, 'host.name') or 'N/A'} "
            f"({_field(s, 'host.os.name') or 'N/A'})",
            f"User: {_field(s, 'user.name') or 'N/A'}",
            f"Tactic: {_field(s, 'kibana.alert.rule.threat') or 'N/A'}",
            f"Tags: {', '.join(_field(s, 'kibana.alert.workflow_tags') or []) or 'None'}",
            f"Timestamp: {_dt(_field(s, '@timestamp'))}",
        ]
        reason = _field(s, "kibana.alert.reason")
        if reason:
            lines.append(f"\nReason:\n{_trunc(reason, 800)}")
        proc = _field(s, "process.command_line")
        if proc:
            lines.append(f"\nProcess: {_trunc(proc, 400)}")
        return "\n".join(lines)

    async def set_elastic_alert_status(
        alert_id: str, status: str, reason: str = ""
    ) -> str:
        """Set the workflow status of a detection alert.

        ``status``: open, acknowledged, closed. ``reason`` (optional, for
        closing): false_positive, duplicate, true_positive, benign_positive,
        automated_closure, other.
        """
        status = (status or "").strip().lower()
        if status not in _ALERT_STATUSES:
            return (
                f"Error: invalid status '{status}'. Use one of: "
                f"{', '.join(sorted(_ALERT_STATUSES))}."
            )
        body: dict[str, Any] = {"status": status, "signal_ids": [alert_id]}
        if reason:
            body["reason"] = reason
        _, err = await _kbn(
            "post",
            "/api/detection_engine/signals/status",
            f"set status on alert {alert_id}",
            json=body,
        )
        if err:
            return err
        return f"Alert {alert_id} status set to '{status}'."

    async def tag_elastic_alert(
        alert_id: str, add_tags: str = "", remove_tags: str = ""
    ) -> str:
        """Add and/or remove workflow tags on a detection alert (comma-separated)."""
        to_add = [t.strip() for t in add_tags.split(",") if t.strip()]
        to_remove = [t.strip() for t in remove_tags.split(",") if t.strip()]
        if not to_add and not to_remove:
            return "Error: provide add_tags and/or remove_tags."
        body = {
            "tags": {"tags_to_add": to_add, "tags_to_remove": to_remove},
            "ids": [alert_id],
        }
        _, err = await _kbn(
            "post",
            "/api/detection_engine/signals/tags",
            f"tag alert {alert_id}",
            json=body,
        )
        if err:
            return err
        return f"Alert {alert_id} tags updated (added: {to_add}, removed: {to_remove})."

    async def assign_elastic_alert(
        alert_id: str, add_assignees: str = "", remove_assignees: str = ""
    ) -> str:
        """Assign/unassign users on a detection alert by user profile UID.

        ``add_assignees`` / ``remove_assignees`` are comma-separated user
        profile UIDs (not emails).
        """
        to_add = [a.strip() for a in add_assignees.split(",") if a.strip()]
        to_remove = [a.strip() for a in remove_assignees.split(",") if a.strip()]
        if not to_add and not to_remove:
            return "Error: provide add_assignees and/or remove_assignees (profile UIDs)."
        body = {
            "assignees": {"add": to_add, "remove": to_remove},
            "ids": [alert_id],
        }
        _, err = await _kbn(
            "post",
            "/api/detection_engine/signals/assignees",
            f"assign alert {alert_id}",
            json=body,
        )
        if err:
            return err
        return f"Alert {alert_id} assignees updated."

    # ══════════════════════════════════════════════════════════════════════
    # 1. DETECTION RULES
    # ══════════════════════════════════════════════════════════════════════

    async def list_elastic_rules(
        query: str = "",
        enabled_only: bool = False,
        count: int = 25,
    ) -> str:
        """List Elastic Security detection rules.

        ``query`` is an optional KQL filter (e.g. ``alert.attributes.name:*ransom*``).
        Set ``enabled_only`` to only return enabled rules.
        """
        params: dict[str, Any] = {
            "page": 1,
            "per_page": min(count, 100),
            "sort_field": "name",
            "sort_order": "asc",
        }
        filters: list[str] = []
        if query:
            filters.append(query)
        if enabled_only:
            filters.append("alert.attributes.enabled: true")
        if filters:
            params["filter"] = " and ".join(filters)

        resp, err = await _kbn(
            "get", "/api/detection_engine/rules/_find", "list rules", params=params
        )
        if err:
            return err
        rules = (resp or {}).get("data", []) if isinstance(resp, dict) else []

        def _fmt(r: dict) -> str:
            enabled = "✓ enabled" if r.get("enabled") else "✗ disabled"
            return (
                f"  {_sev(r.get('severity'))} {r.get('name', 'Unnamed')} — {enabled}\n"
                f"    id: {r.get('id', '?')} | Type: {r.get('type', '?')} | "
                f"Severity: {r.get('severity', 'N/A')} | "
                f"Risk: {r.get('risk_score', 'N/A')} | "
                f"Tags: {', '.join(r.get('tags', []) or []) or 'none'}"
            )

        total = (resp or {}).get("total") if isinstance(resp, dict) else None
        return format_entity_list(rules, _fmt, noun="rule", total=total)

    async def get_elastic_rule(rule_id: str) -> str:
        """Get full details of a detection rule by its object ID."""
        resp, err = await _kbn(
            "get",
            "/api/detection_engine/rules",
            f"get rule {rule_id}",
            params={"id": rule_id},
        )
        if err:
            return err
        if not isinstance(resp, dict) or not resp.get("id"):
            return f"Rule {rule_id} not found."
        lines = [
            f"Rule: {resp.get('name', 'Unnamed')}",
            f"ID: {resp.get('id', '?')} | rule_id: {resp.get('rule_id', 'N/A')}",
            f"Enabled: {resp.get('enabled', '?')} | Type: {resp.get('type', '?')}",
            f"Severity: {resp.get('severity', 'N/A')} | "
            f"Risk score: {resp.get('risk_score', 'N/A')}",
            f"Interval: {resp.get('interval', 'N/A')} | "
            f"From: {resp.get('from', 'N/A')}",
            f"Tags: {', '.join(resp.get('tags', []) or []) or 'none'}",
        ]
        if resp.get("query"):
            lines.append(f"Query: {_trunc(resp.get('query'), 400)}")
        if resp.get("description"):
            lines.append(f"\nDescription:\n{_trunc(resp.get('description'), 600)}")
        return "\n".join(lines)

    async def toggle_elastic_rule(rule_id: str, enable: bool = True) -> str:
        """Enable or disable a detection rule by its object ID."""
        resp, err = await _kbn(
            "post",
            "/api/detection_engine/rules/_bulk_action",
            f"{'enable' if enable else 'disable'} rule {rule_id}",
            json={"action": "enable" if enable else "disable", "ids": [rule_id]},
        )
        if err:
            return err
        detail = _err_detail(resp)
        if detail and isinstance(resp, dict) and resp.get("success") is False:
            return f"Error toggling rule {rule_id}: {detail}"
        return f"Rule {rule_id} {'enabled' if enable else 'disabled'}."

    # ══════════════════════════════════════════════════════════════════════
    # 2. CASES
    # ══════════════════════════════════════════════════════════════════════

    async def list_elastic_cases(
        search: str = "",
        status: str = "",
        severity: str = "",
        count: int = 25,
    ) -> str:
        """List Elastic Security cases. Filter by search text, status, severity.

        ``status`` (open, in-progress, closed) and ``severity`` (low, medium,
        high, critical) are validated against the Kibana Cases allowlists; pass
        an empty value to skip that filter.
        """
        status = (status or "").strip().lower()
        if status and status not in _CASE_STATUSES:
            return (
                f"Error: invalid case status '{status}'. Use one of: "
                f"{', '.join(sorted(_CASE_STATUSES))} (or empty for any)."
            )
        severity = (severity or "").strip().lower()
        if severity and severity not in _CASE_SEVERITIES:
            return (
                f"Error: invalid severity '{severity}'. Use one of: "
                f"{', '.join(sorted(_CASE_SEVERITIES))} (or empty for any)."
            )
        params: dict[str, Any] = {
            "owner": _OWNER,
            "page": 1,
            "perPage": min(count, 100),
            "sortField": "createdAt",
            "sortOrder": "desc",
        }
        if search:
            params["search"] = search
        if status:
            params["status"] = status
        if severity:
            params["severity"] = severity

        resp, err = await _kbn("get", "/api/cases/_find", "list cases", params=params)
        if err:
            return err
        cases = (resp or {}).get("cases", []) if isinstance(resp, dict) else []

        def _fmt(c: dict) -> str:
            return (
                f"  {_sev(c.get('severity'))} [{c.get('severity', 'N/A')}] "
                f"{c.get('status', '?')} — {c.get('title', 'Untitled')}\n"
                f"    id: {c.get('id', '?')} | "
                f"Assignees: {len(c.get('assignees', []) or [])} | "
                f"Comments: {c.get('totalComment', 0)} | "
                f"Alerts: {c.get('totalAlerts', 0)} | "
                f"Created: {_dt(c.get('created_at'))}"
            )

        total = (resp or {}).get("total") if isinstance(resp, dict) else None
        return format_entity_list(cases, _fmt, noun="case", total=total)

    async def get_elastic_case(case_id: str) -> str:
        """Get full details of a case by ID."""
        resp, err = await _kbn(
            "get", f"/api/cases/{case_id}", f"get case {case_id}"
        )
        if err:
            return err
        if not isinstance(resp, dict) or not resp.get("id"):
            return f"Case {case_id} not found."
        lines = [
            f"Case: {resp.get('title', 'Untitled')}",
            f"ID: {resp.get('id', '?')}",
            f"Status: {resp.get('status', '?')} | "
            f"Severity: {resp.get('severity', 'N/A')}",
            f"Tags: {', '.join(resp.get('tags', []) or []) or 'none'}",
            f"Total comments: {resp.get('totalComment', 0)} | "
            f"Total alerts: {resp.get('totalAlerts', 0)}",
            f"Created: {_dt(resp.get('created_at'))} by "
            f"{(resp.get('created_by') or {}).get('username', 'N/A')}",
        ]
        if resp.get("description"):
            lines.append(f"\nDescription:\n{_trunc(resp.get('description'), 800)}")
        return "\n".join(lines)

    async def create_elastic_case(
        title: str,
        description: str,
        *,
        severity: str = "low",
        tags: str = "",
    ) -> str:
        """Create a new Elastic Security case.

        ``severity``: low, medium, high, critical (defaults to low when
        omitted; an explicit invalid value is rejected).
        """
        severity = (severity or "").strip().lower()
        if not severity:
            severity = "low"
        elif severity not in _CASE_SEVERITIES:
            return (
                f"Error: invalid severity '{severity}'. Use one of: "
                f"{', '.join(sorted(_CASE_SEVERITIES))}."
            )
        body = {
            "title": title,
            "description": description,
            "tags": [t.strip() for t in tags.split(",") if t.strip()],
            "severity": severity,
            "owner": _OWNER,
            "connector": {
                "id": "none",
                "name": "none",
                "type": ".none",
                "fields": None,
            },
            "settings": {"syncAlerts": False},
        }
        resp, err = await _kbn("post", "/api/cases", "create case", json=body)
        if err:
            return err
        if not isinstance(resp, dict) or not resp.get("id"):
            return f"Case creation failed: {_err_detail(resp) or 'unknown error'}"
        return f"Case created: '{title}' (id: {resp.get('id')})."

    async def comment_elastic_case(case_id: str, comment: str) -> str:
        """Add a user comment to a case."""
        body = {"type": "user", "owner": _OWNER, "comment": comment}
        _, err = await _kbn(
            "post",
            f"/api/cases/{case_id}/comments",
            f"comment on case {case_id}",
            json=body,
        )
        if err:
            return err
        return f"Comment added to case {case_id}."

    async def update_elastic_case_status(case_id: str, status: str) -> str:
        """Update a case status: open, in-progress, or closed."""
        status = (status or "").strip().lower()
        if status not in _CASE_STATUSES:
            return (
                f"Error: invalid case status '{status}'. Use one of: "
                f"{', '.join(sorted(_CASE_STATUSES))}."
            )
        # The bulk case update requires the current document version.
        current, gerr = await _kbn(
            "get", f"/api/cases/{case_id}", f"load case {case_id} for update"
        )
        if gerr:
            return gerr
        version = current.get("version") if isinstance(current, dict) else None
        if not version:
            return f"Could not resolve the current version for case {case_id}."
        body = {"cases": [{"id": case_id, "version": version, "status": status}]}
        _, err = await _kbn("patch", "/api/cases", f"update case {case_id}", json=body)
        if err:
            return err
        return f"Case {case_id} status set to '{status}'."

    # ══════════════════════════════════════════════════════════════════════
    # 3. EXCEPTION LISTS + VALUE LISTS
    # ══════════════════════════════════════════════════════════════════════

    async def list_elastic_exception_lists(count: int = 25) -> str:
        """List exception list containers (including endpoint artifact lists)."""
        resp, err = await _kbn(
            "get",
            "/api/exception_lists/_find",
            "list exception lists",
            params={
                "namespace_type": ["single", "agnostic"],
                "page": 1,
                "per_page": min(count, 100),
            },
        )
        if err:
            return err
        data = (resp or {}).get("data", []) if isinstance(resp, dict) else []

        def _fmt(e: dict) -> str:
            return (
                f"  {e.get('name', 'Unnamed')} "
                f"(list_id: {e.get('list_id', '?')})\n"
                f"    Type: {e.get('type', 'N/A')} | "
                f"Namespace: {e.get('namespace_type', 'N/A')} | "
                f"{_trunc(e.get('description'), 120) or 'No description'}"
            )

        total = (resp or {}).get("total") if isinstance(resp, dict) else None
        return format_entity_list(data, _fmt, noun="exception list", total=total)

    async def list_elastic_exception_items(
        list_id: str,
        namespace_type: str = "single",
        count: int = 25,
    ) -> str:
        """List the items within an exception list by its ``list_id``."""
        resp, err = await _kbn(
            "get",
            "/api/exception_lists/items/_find",
            f"list exception items for {list_id}",
            params={
                "list_id": list_id,
                "namespace_type": namespace_type,
                "page": 1,
                "per_page": min(count, 100),
            },
        )
        if err:
            return err
        data = (resp or {}).get("data", []) if isinstance(resp, dict) else []

        def _fmt(i: dict) -> str:
            entries = i.get("entries", []) or []
            entry_str = "; ".join(
                f"{e.get('field', '?')} {e.get('operator', '')} "
                f"{e.get('value', e.get('list', ''))}"
                for e in entries
            )
            return (
                f"  {i.get('name', 'Unnamed')} (item_id: {i.get('item_id', '?')})\n"
                f"    {_trunc(entry_str, 220)}"
            )

        total = (resp or {}).get("total") if isinstance(resp, dict) else None
        return format_entity_list(data, _fmt, noun="exception item", total=total)

    async def list_elastic_value_lists(count: int = 25) -> str:
        """List value lists (used by detection rules for IOC matching)."""
        resp, err = await _kbn(
            "get",
            "/api/lists/_find",
            "list value lists",
            params={"page": 1, "per_page": min(count, 100)},
        )
        if err:
            return err
        data = (resp or {}).get("data", []) if isinstance(resp, dict) else []

        def _fmt(v: dict) -> str:
            return (
                f"  {v.get('name', 'Unnamed')} (id: {v.get('id', '?')}) — "
                f"Type: {v.get('type', 'N/A')} | "
                f"{_trunc(v.get('description'), 120) or 'No description'}"
            )

        total = (resp or {}).get("total") if isinstance(resp, dict) else None
        return format_entity_list(data, _fmt, noun="value list", total=total)

    async def add_elastic_value_list_item(list_id: str, value: str) -> str:
        """Add a value (IP, hash, domain, …) to an existing value list.

        The value's type is inherited from the list. Useful for IOC
        enforcement: add an indicator to a value list referenced by detection
        rules so future matches generate alerts.
        """
        resp, err = await _kbn(
            "post",
            "/api/lists/items",
            f"add value '{value}' to list {list_id}",
            json={"list_id": list_id, "value": value},
        )
        if err:
            return err
        if not isinstance(resp, dict) or not resp.get("id"):
            return f"Failed to add value: {_err_detail(resp) or 'unknown error'}"
        return f"Value '{value}' added to value list {list_id}."

    # ══════════════════════════════════════════════════════════════════════
    # 4. ENDPOINT RESPONSE ACTIONS (Elastic Defend)
    # ══════════════════════════════════════════════════════════════════════

    async def list_elastic_endpoints(
        kuery: str = "", count: int = 25
    ) -> str:
        """List Elastic Defend endpoints (hosts) with status and metadata."""
        params: dict[str, Any] = {"page": 0, "pageSize": min(count, 100)}
        if kuery:
            params["kuery"] = kuery
        resp, err = await _kbn(
            "get", "/api/endpoint/metadata", "list endpoints", params=params
        )
        if err:
            return err
        data = (resp or {}).get("data", []) if isinstance(resp, dict) else []

        def _fmt(item: dict) -> str:
            meta = item.get("metadata") or item
            host = meta.get("host") or {}
            agent = meta.get("agent") or {}
            os_info = host.get("os") or {}
            ips = host.get("ip") or []
            return (
                f"  {host.get('name', '?')} "
                f"[{os_info.get('name', 'N/A')}] — {item.get('host_status', 'N/A')}\n"
                f"    endpoint_id (agent.id): {agent.get('id', '?')} | "
                f"IP: {(ips[0] if ips else 'N/A')} | "
                f"Agent: {agent.get('version', 'N/A')}"
            )

        total = (resp or {}).get("total") if isinstance(resp, dict) else None
        return format_entity_list(data, _fmt, noun="endpoint", total=total)

    async def _endpoint_action(
        path: str, endpoint_id: str, verb: str, comment: str = ""
    ) -> tuple[dict | None, str | None]:
        body: dict[str, Any] = {"endpoint_ids": [endpoint_id]}
        if comment:
            body["comment"] = comment
        return await _kbn(
            "post",
            f"/api/endpoint/action/{path}",
            f"{verb} endpoint {endpoint_id}",
            json=body,
            timeout=_ACTION_TIMEOUT,
        )

    async def isolate_elastic_endpoint(endpoint_id: str, comment: str = "") -> str:
        """Network-isolate an Elastic Defend endpoint (by agent.id)."""
        resp, err = await _endpoint_action(
            "isolate", endpoint_id, "isolate", comment
        )
        if err:
            return err
        action_id = ((resp or {}).get("data") or {}).get("id") if isinstance(
            resp, dict
        ) else None
        return (
            f"Isolation requested for endpoint {endpoint_id} "
            f"(action id: {action_id or 'N/A'})."
        )

    async def release_elastic_endpoint(endpoint_id: str, comment: str = "") -> str:
        """Release (un-isolate) an Elastic Defend endpoint (by agent.id)."""
        resp, err = await _endpoint_action(
            "unisolate", endpoint_id, "release", comment
        )
        if err:
            return err
        action_id = ((resp or {}).get("data") or {}).get("id") if isinstance(
            resp, dict
        ) else None
        return (
            f"Release requested for endpoint {endpoint_id} "
            f"(action id: {action_id or 'N/A'})."
        )

    async def run_elastic_endpoint_processes(
        endpoint_id: str, comment: str = ""
    ) -> str:
        """Request the running-processes list from an endpoint (async action).

        Returns an action ID — poll it with ``get_elastic_endpoint_action`` to
        retrieve the process list once the agent reports back.
        """
        resp, err = await _endpoint_action(
            "running_procs", endpoint_id, "list running processes on", comment
        )
        if err:
            return err
        action_id = ((resp or {}).get("data") or {}).get("id") if isinstance(
            resp, dict
        ) else None
        if not action_id:
            return (
                "Running-processes action did not return an action ID "
                f"({_err_detail(resp) or 'unknown error'})."
            )
        return (
            f"Running-processes action started for endpoint {endpoint_id} "
            f"(action id: {action_id}). Use get_elastic_endpoint_action with this "
            f"ID to fetch the results once complete."
        )

    async def list_elastic_endpoint_actions(count: int = 25) -> str:
        """List recent Elastic Defend response actions (history)."""
        resp, err = await _kbn(
            "get",
            "/api/endpoint/action",
            "list endpoint actions",
            params={"page": 1, "pageSize": min(count, 100)},
        )
        if err:
            return err
        data = (resp or {}).get("data", []) if isinstance(resp, dict) else []

        def _fmt(a: dict) -> str:
            return (
                f"  {a.get('command', '?')} — "
                f"{'completed' if a.get('isCompleted') else 'pending'} "
                f"({'success' if a.get('wasSuccessful') else 'incomplete'})\n"
                f"    id: {a.get('id', '?')} | "
                f"Agents: {len(a.get('agents', []) or [])} | "
                f"By: {a.get('createdBy', 'N/A')} | "
                f"Started: {_dt(a.get('startedAt'))}"
            )

        total = (resp or {}).get("total") if isinstance(resp, dict) else None
        return format_entity_list(data, _fmt, noun="endpoint action", total=total)

    async def get_elastic_endpoint_action(action_id: str) -> str:
        """Get an endpoint response action's status and outputs by action ID."""
        resp, err = await _kbn(
            "get",
            f"/api/endpoint/action/{action_id}",
            f"get endpoint action {action_id}",
        )
        if err:
            return err
        data = (resp or {}).get("data") if isinstance(resp, dict) else None
        if not isinstance(data, dict):
            return f"Action {action_id} not found."
        lines = [
            f"Endpoint action: {data.get('command', '?')} (id: {action_id})",
            f"Completed: {data.get('isCompleted', '?')} | "
            f"Successful: {data.get('wasSuccessful', '?')}",
            f"Agents: {len(data.get('agents', []) or [])} | "
            f"Started: {_dt(data.get('startedAt'))} | "
            f"Completed at: {_dt(data.get('completedAt'))}",
        ]
        outputs = data.get("outputs") or {}
        if outputs:
            lines.append("\nOutputs:")
            for agent_id, out in list(outputs.items())[:5]:
                content = out.get("content") if isinstance(out, dict) else out
                lines.append(
                    f"  [{agent_id}] {_trunc(_json.dumps(content, default=str), 1200)}"
                )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 5. HUNTING (Elasticsearch)
    # ══════════════════════════════════════════════════════════════════════

    async def run_elastic_esql_query(query: str) -> str:
        """Run an ES|QL query against Elasticsearch for threat hunting.

        Example: ``FROM logs-* | WHERE event.category == "process" | LIMIT 20``.
        Requires the Elasticsearch URL to be configured.
        """
        if not query or not query.strip():
            return "Error: an ES|QL query string is required."
        resp, err = await _es("post", "/_query", "run ES|QL query", json={"query": query})
        if err:
            return err
        columns = (resp or {}).get("columns", []) if isinstance(resp, dict) else []
        values = (resp or {}).get("values", []) if isinstance(resp, dict) else []
        if not values:
            return "ES|QL query returned no rows."
        col_names = [c.get("name", "?") for c in columns]
        lines = [f"ES|QL results ({len(values)} rows):", " | ".join(col_names)]
        lines.append("-" * min(len(" | ".join(col_names)), 120))
        for row in values[:50]:
            lines.append(
                " | ".join(
                    _trunc(v, 80) if v is not None else "null" for v in row
                )
            )
        if len(values) > 50:
            lines.append(f"\n... [{len(values) - 50} more rows not shown]")
        return "\n".join(lines)

    async def run_elastic_eql_query(
        index: str, query: str, size: int = 25
    ) -> str:
        """Run an EQL query against an index/data stream for event hunting.

        Example query: ``process where process.name == "regsvr32.exe"``.
        Requires the Elasticsearch URL to be configured.
        """
        if not query or not query.strip():
            return "Error: an EQL query string is required."
        resp, err = await _es(
            "post",
            f"/{index}/_eql/search",
            "run EQL query",
            json={"query": query, "size": min(size, 100)},
        )
        if err:
            return err
        hits = (resp or {}).get("hits", {}) if isinstance(resp, dict) else {}
        events = hits.get("events") or []
        sequences = hits.get("sequences") or []
        if not events and not sequences:
            return "EQL query returned no matches."
        lines = []
        if events:
            lines.append(f"EQL events ({len(events)}):")
            for e in events[:size]:
                s = e.get("_source", {}) or {}
                lines.append(
                    f"  [{_dt(_field(s, '@timestamp'))}] "
                    f"{_field(s, 'event.category') or 'event'} on "
                    f"{_field(s, 'host.name') or 'N/A'} — "
                    f"{_trunc(_field(s, 'process.name') or _field(s, 'message'), 160)}"
                )
        if sequences:
            lines.append(f"\nEQL sequences: {len(sequences)} matched.")
        return "\n".join(lines)

    async def search_elastic(
        index: str, query: str = "", size: int = 25
    ) -> str:
        """Run a raw Elasticsearch _search over an index/data stream.

        ``query`` is a Query DSL JSON object (string). When empty, matches all.
        Requires the Elasticsearch URL to be configured.
        """
        body: dict[str, Any] = {"size": min(size, 100)}
        if query and query.strip():
            try:
                parsed = _json.loads(query)
            except (ValueError, TypeError):
                return (
                    "Error: 'query' must be a valid Query DSL JSON object, e.g. "
                    '{"match": {"host.name": "web-01"}}.'
                )
            body["query"] = parsed
        else:
            body["query"] = {"match_all": {}}

        resp, err = await _es(
            "post", f"/{index}/_search", "run _search query", json=body
        )
        if err:
            return err
        hits = _hits(resp)
        if not hits:
            return "Search returned no documents."
        lines = [f"Search results ({_total_hits(resp)} total, showing {len(hits)}):"]
        for h in hits[:size]:
            s = h.get("_source", {}) or {}
            lines.append(
                f"  [{_dt(_field(s, '@timestamp'))}] _id={h.get('_id', '?')} — "
                f"{_trunc(_json.dumps(s, default=str), 220)}"
            )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("list_elastic_alerts", list_elastic_alerts)
    executor.register("get_elastic_alert", get_elastic_alert)
    executor.register("set_elastic_alert_status", set_elastic_alert_status)
    executor.register("tag_elastic_alert", tag_elastic_alert)
    executor.register("assign_elastic_alert", assign_elastic_alert)
    executor.register("list_elastic_rules", list_elastic_rules)
    executor.register("get_elastic_rule", get_elastic_rule)
    executor.register("toggle_elastic_rule", toggle_elastic_rule)
    executor.register("list_elastic_cases", list_elastic_cases)
    executor.register("get_elastic_case", get_elastic_case)
    executor.register("create_elastic_case", create_elastic_case)
    executor.register("comment_elastic_case", comment_elastic_case)
    executor.register("update_elastic_case_status", update_elastic_case_status)
    executor.register("list_elastic_exception_lists", list_elastic_exception_lists)
    executor.register("list_elastic_exception_items", list_elastic_exception_items)
    executor.register("list_elastic_value_lists", list_elastic_value_lists)
    executor.register("add_elastic_value_list_item", add_elastic_value_list_item)
    executor.register("list_elastic_endpoints", list_elastic_endpoints)
    executor.register("isolate_elastic_endpoint", isolate_elastic_endpoint)
    executor.register("release_elastic_endpoint", release_elastic_endpoint)
    executor.register(
        "run_elastic_endpoint_processes", run_elastic_endpoint_processes
    )
    executor.register(
        "list_elastic_endpoint_actions", list_elastic_endpoint_actions
    )
    executor.register("get_elastic_endpoint_action", get_elastic_endpoint_action)
    executor.register("run_elastic_esql_query", run_elastic_esql_query)
    executor.register("run_elastic_eql_query", run_elastic_eql_query)
    executor.register("search_elastic", search_elastic)

    src = "integration:elastic_security"
    return [
        # ── 0. Detection alerts ───────────────────────────────────────────
        ToolDefinition(
            name="list_elastic_alerts",
            description=(
                "List Elastic Security detection alerts. Filter by status (open, "
                "acknowledged, closed), severity (low, medium, high, critical), a "
                "free-text KQL/Lucene query, and a look-back window in days."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "default": "open"},
                    "severity": {"type": "string"},
                    "query": {"type": "string"},
                    "days": {"type": "integer", "default": 7},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_elastic_alert",
            description=(
                "Get full details of a detection alert by its alert UUID: rule, "
                "severity, risk score, status, host/user, reason, and process."
            ),
            input_schema={
                "type": "object",
                "properties": {"alert_id": {"type": "string"}},
                "required": ["alert_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="set_elastic_alert_status",
            description=(
                "Set the workflow status of a detection alert: open, "
                "acknowledged, or closed. Optional reason when closing "
                "(false_positive, duplicate, true_positive, benign_positive, …)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {"type": "string"},
                    "status": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["alert_id", "status"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="tag_elastic_alert",
            description=(
                "Add and/or remove workflow tags on a detection alert "
                "(comma-separated tag lists)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {"type": "string"},
                    "add_tags": {"type": "string"},
                    "remove_tags": {"type": "string"},
                },
                "required": ["alert_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="assign_elastic_alert",
            description=(
                "Assign or unassign users on a detection alert by user profile "
                "UID (comma-separated). Profile UIDs, not emails."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {"type": "string"},
                    "add_assignees": {"type": "string"},
                    "remove_assignees": {"type": "string"},
                },
                "required": ["alert_id"],
            },
            source=src,
            priority=3,
        ),
        # ── 1. Detection rules ────────────────────────────────────────────
        ToolDefinition(
            name="list_elastic_rules",
            description=(
                "List Elastic Security detection rules. Optional KQL query "
                "filter and enabled_only flag. Returns name, enabled state, "
                "type, severity, and risk score."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "enabled_only": {"type": "boolean", "default": False},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_elastic_rule",
            description="Get full details of a detection rule by its object ID.",
            input_schema={
                "type": "object",
                "properties": {"rule_id": {"type": "string"}},
                "required": ["rule_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="toggle_elastic_rule",
            description="Enable or disable a detection rule by its object ID.",
            input_schema={
                "type": "object",
                "properties": {
                    "rule_id": {"type": "string"},
                    "enable": {"type": "boolean", "default": True},
                },
                "required": ["rule_id"],
            },
            source=src,
            priority=2,
        ),
        # ── 2. Cases ──────────────────────────────────────────────────────
        ToolDefinition(
            name="list_elastic_cases",
            description=(
                "List Elastic Security cases. Filter by search text, status "
                "(open, in-progress, closed), and severity."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string"},
                    "status": {"type": "string"},
                    "severity": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_elastic_case",
            description="Get full details of a case by ID.",
            input_schema={
                "type": "object",
                "properties": {"case_id": {"type": "string"}},
                "required": ["case_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="create_elastic_case",
            description=(
                "Create a new Elastic Security case. severity: low, medium, "
                "high, critical. tags is a comma-separated list."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "severity": {"type": "string", "default": "low"},
                    "tags": {"type": "string"},
                },
                "required": ["title", "description"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="comment_elastic_case",
            description="Add a user comment to a case.",
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "comment": {"type": "string"},
                },
                "required": ["case_id", "comment"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="update_elastic_case_status",
            description="Update a case status: open, in-progress, or closed.",
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "status": {"type": "string"},
                },
                "required": ["case_id", "status"],
            },
            source=src,
            priority=2,
        ),
        # ── 3. Exception lists + value lists ───────────────────────────────
        ToolDefinition(
            name="list_elastic_exception_lists",
            description=(
                "List exception list containers, including endpoint artifact "
                "lists (trusted apps, event filters, host isolation exceptions, "
                "blocklists)."
            ),
            input_schema={
                "type": "object",
                "properties": {"count": {"type": "integer", "default": 25}},
                "required": [],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="list_elastic_exception_items",
            description=(
                "List the items within an exception list by its list_id. "
                "namespace_type is 'single' (space-aware) or 'agnostic'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "list_id": {"type": "string"},
                    "namespace_type": {"type": "string", "default": "single"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": ["list_id"],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="list_elastic_value_lists",
            description=(
                "List value lists (collections of IPs / hashes / domains used by "
                "detection rules for IOC matching)."
            ),
            input_schema={
                "type": "object",
                "properties": {"count": {"type": "integer", "default": 25}},
                "required": [],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="add_elastic_value_list_item",
            description=(
                "Add a value (IP, hash, domain, …) to an existing value list for "
                "IOC enforcement — future matches generate alerts. The value's "
                "type is inherited from the list."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "list_id": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["list_id", "value"],
            },
            source=src,
            priority=2,
        ),
        # ── 4. Endpoint response actions ──────────────────────────────────
        ToolDefinition(
            name="list_elastic_endpoints",
            description=(
                "List Elastic Defend endpoints (hosts) with host status, OS, IP, "
                "and the endpoint_id (agent.id) used for response actions. "
                "Optional KQL kuery filter."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "kuery": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="isolate_elastic_endpoint",
            description=(
                "Network-isolate an Elastic Defend endpoint (by agent.id) to "
                "contain an active threat. Optional comment."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "endpoint_id": {"type": "string"},
                    "comment": {"type": "string"},
                },
                "required": ["endpoint_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="release_elastic_endpoint",
            description="Release (un-isolate) an Elastic Defend endpoint (by agent.id).",
            input_schema={
                "type": "object",
                "properties": {
                    "endpoint_id": {"type": "string"},
                    "comment": {"type": "string"},
                },
                "required": ["endpoint_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="run_elastic_endpoint_processes",
            description=(
                "Request the running-processes list from an Elastic Defend "
                "endpoint (async). Returns an action ID — fetch results with "
                "get_elastic_endpoint_action."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "endpoint_id": {"type": "string"},
                    "comment": {"type": "string"},
                },
                "required": ["endpoint_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="list_elastic_endpoint_actions",
            description="List recent Elastic Defend response actions (history).",
            input_schema={
                "type": "object",
                "properties": {"count": {"type": "integer", "default": 25}},
                "required": [],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="get_elastic_endpoint_action",
            description=(
                "Get an endpoint response action's status and outputs by action "
                "ID (e.g. to read running-processes results)."
            ),
            input_schema={
                "type": "object",
                "properties": {"action_id": {"type": "string"}},
                "required": ["action_id"],
            },
            source=src,
            priority=2,
        ),
        # ── 5. Hunting (Elasticsearch) ────────────────────────────────────
        ToolDefinition(
            name="run_elastic_esql_query",
            description=(
                "Run an ES|QL query against Elasticsearch for threat hunting "
                "(e.g. 'FROM logs-* | WHERE event.category == \"process\" | "
                "LIMIT 20'). Requires the Elasticsearch URL to be configured."
            ),
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            source=src,
            priority=1,
            # "run" reads as a write to the name classifier; ES|QL (_query) has no mutating command.
            read_only=True,
        ),
        ToolDefinition(
            name="run_elastic_eql_query",
            description=(
                "Run an EQL query against an index/data stream for event-based "
                "hunting (e.g. 'process where process.name == \"regsvr32.exe\"'). "
                "Requires the Elasticsearch URL to be configured."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "index": {
                        "type": "string",
                        "description": "Index/data stream, e.g. logs-* or .alerts-security.alerts-default",
                    },
                    "query": {"type": "string"},
                    "size": {"type": "integer", "default": 25},
                },
                "required": ["index", "query"],
            },
            source=src,
            priority=2,
            # "run" reads as a write to the name classifier; EQL goes to the _eql/search API.
            read_only=True,
        ),
        ToolDefinition(
            name="search_elastic",
            description=(
                "Run a raw Elasticsearch _search over an index/data stream. "
                "query is a Query DSL JSON object (string); empty matches all. "
                "Requires the Elasticsearch URL to be configured."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "index": {"type": "string"},
                    "query": {
                        "type": "string",
                        "description": 'Query DSL JSON, e.g. {"match": {"host.name": "web-01"}}',
                    },
                    "size": {"type": "integer", "default": 25},
                },
                "required": ["index"],
            },
            source=src,
            priority=2,
        ),
    ]
