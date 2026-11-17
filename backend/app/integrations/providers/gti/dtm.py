"""Google TI Digital Threat Monitoring (DTM) tools.

DTM (the former Mandiant Digital Threat Monitoring) watches the open, deep,
and dark web for organisation-specific threats. All endpoints live under
``/dtm`` on the core host with the same ``x-apikey`` key; pagination uses an
opaque ``page`` token carried in the response ``Link: <...>; rel="next"``
header (NOT a body cursor).

* ``list_gti_dtm_alerts`` - ``GET /alerts`` with status / severity /
  monitor / date / search filters (``size`` max 25).
* ``get_gti_dtm_alert`` - ``GET /alerts/{id}``.
* ``update_gti_dtm_alert`` - ``PATCH /alerts/{id}`` (status transitions +
  tags) - write.
* ``search_gti_dtm_docs`` - ``POST /docs/search`` (Lucene query over
  deep/dark-web documents).
* ``list_gti_dtm_monitors`` / ``get_gti_dtm_monitor`` - ``GET /monitors``.
* ``manage_gti_dtm_monitor`` - create / update / enable / disable / delete
  monitors - write + destructive.

DTM requires the Digital Threat Monitoring module on the Google TI license -
permission errors are surfaced verbatim.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from app.integrations.http import format_pagination_hint, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _PROVIDER,
    _SOURCE,
    clamp,
    client,
    csv_list,
    next_page_token,
    trunc,
)

_ALERT_STATUSES = (
    "new",
    "read",
    "escalated",
    "in_progress",
    "closed",
    "no_action_required",
    "duplicate",
    "not_relevant",
    "tracked_external",
)
_ALERT_SEVERITIES = ("high", "medium", "low")
_MONITOR_ACTIONS = frozenset({"create", "update", "enable", "disable", "delete"})


def _format_alert(alert: dict[str, Any]) -> str:
    title = trunc(alert.get("title") or alert.get("alert_summary"), 120) or "?"
    bits: list[str] = []
    status = alert.get("status")
    if status:
        bits.append(trunc(status, 30))
    severity = alert.get("alert_severity") or alert.get("severity")
    if severity:
        bits.append(f"severity {trunc(severity, 20)}")
    alert_type = alert.get("alert_type")
    if alert_type:
        bits.append(trunc(alert_type, 40))
    created = alert.get("created_at")
    if created:
        bits.append(trunc(created, 25))
    monitor = alert.get("monitor_name") or alert.get("monitor_id")
    if monitor:
        bits.append(f"monitor {trunc(monitor, 50)}")
    suffix = f" [{', '.join(bits)}]" if bits else ""
    return f"  {title}{suffix} (id: {trunc(alert.get('id'), 60) or '?'})"


def register(
    api: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    """Register DTM tools and return their definitions.

    ``api`` is the DTM base (``.../api/v3/dtm``).
    """

    async def list_gti_dtm_alerts(
        status: str = "",
        severity: str = "",
        monitor_id: str = "",
        search: str = "",
        since: str = "",
        until: str = "",
        size: int = 10,
        page: str = "",
    ) -> str:
        """List DTM deep/dark-web alerts with status/severity/monitor/date filters."""
        params: list[tuple[str, Any]] = [("size", clamp(size, hi=25))]
        for value in csv_list(status):
            value = value.lower()
            if value not in _ALERT_STATUSES:
                return f"Error: status must be from {', '.join(_ALERT_STATUSES)}."
            params.append(("status", value))
        for value in csv_list(severity):
            value = value.lower()
            if value not in _ALERT_SEVERITIES:
                return f"Error: severity must be from {', '.join(_ALERT_SEVERITIES)}."
            params.append(("severity", value))
        if monitor_id.strip():
            params.append(("monitor_id", monitor_id.strip()))
        if search.strip():
            params.append(("search", search.strip()))
        if since.strip():
            params.append(("since", since.strip()))
        if until.strip():
            params.append(("until", until.strip()))
        params.append(("monitor_name", "true"))
        # Doc refs bloat responses massively; the compact list only needs
        # alert-level fields.
        params.append(("refs", "false"))
        if page.strip():
            # The API forbids combining page with other query parameters.
            params = [("page", page.strip())]
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/alerts",
                _PROVIDER,
                "list DTM alerts",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
            token = next_page_token(resp)
        alerts = payload.get("alerts") if isinstance(payload, dict) else None
        alerts = alerts if isinstance(alerts, list) else []
        if not alerts:
            return "No DTM alerts match the given filters."
        lines = [f"DTM alerts ({len(alerts)} shown):"]
        lines.extend(_format_alert(a) for a in alerts if isinstance(a, dict))
        return "\n".join(lines) + format_pagination_hint(token, "page")

    async def get_gti_dtm_alert(alert_id: str) -> str:
        """Read one DTM alert: summary, severity, status, monitor, matched doc excerpt."""
        alert_id = (alert_id or "").strip()
        if not alert_id:
            return "Error: alert_id is required (from list_gti_dtm_alerts)."
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/alerts/{quote(alert_id, safe='')}",
                _PROVIDER,
                f"get DTM alert {trunc(alert_id, 60)}",
                headers=headers,
                # refs=true includes the triggering doc (for the excerpt);
                # sanitize strips malicious HTML; truncate caps doc fields
                # server-side (client-side trunc caps the rendered lines).
                params={"refs": "true", "sanitize": "true", "truncate": 800},
                expected_statuses={404},
            )
            if err:
                if resp is not None and resp.status_code == 404:
                    return f"DTM alert '{trunc(alert_id, 80)}' not found."
                return err
            alert = resp.json() or {}
        if not isinstance(alert, dict):
            return f"DTM alert '{trunc(alert_id, 80)}' not found."
        lines = [f"DTM alert {trunc(alert.get('id'), 60) or alert_id}:"]
        title = alert.get("title") or alert.get("alert_summary")
        if title:
            lines.append(f"Title: {trunc(title, 200)}")
        for key, label in (
            ("status", "Status"),
            ("alert_severity", "Severity"),
            ("alert_type", "Type"),
            ("created_at", "Created"),
            ("updated_at", "Updated"),
            ("monitor_name", "Monitor"),
            ("monitor_id", "Monitor id"),
        ):
            value = alert.get(key)
            if value:
                lines.append(f"{label}: {trunc(value, 120)}")
        tags = alert.get("tags")
        if isinstance(tags, list) and tags:
            lines.append(f"Tags: {trunc(', '.join(str(t) for t in tags[:10]), 300)}")
        summary = alert.get("email_body_text") or alert.get("analysis_text")
        if summary:
            lines.append(f"Analysis: {trunc(summary, 600)}")
        doc = alert.get("doc")
        if isinstance(doc, dict):
            body = doc.get("body") or doc.get("text") or doc.get("title")
            if body:
                lines.append(f"Matched document excerpt: {trunc(body, 500)}")
        return "\n".join(lines)

    async def update_gti_dtm_alert(
        alert_id: str,
        status: str = "",
        tags: str = "",
    ) -> str:
        """Update a DTM alert's status and/or tags (triage workflow)."""
        alert_id = (alert_id or "").strip()
        if not alert_id:
            return "Error: alert_id is required."
        body: dict[str, Any] = {}
        status = (status or "").strip().lower()
        if status:
            if status not in _ALERT_STATUSES:
                return f"Error: status must be one of {', '.join(_ALERT_STATUSES)}."
            body["status"] = status
        tag_values = csv_list(tags)
        if tag_values:
            body["tags"] = tag_values
        if not body:
            return "Error: update requires status and/or tags."
        async with client() as c:
            _resp, err = await safe_request(
                c,
                "patch",
                f"{api}/alerts/{quote(alert_id, safe='')}",
                _PROVIDER,
                f"update DTM alert {trunc(alert_id, 60)}",
                headers=headers,
                json=body,
            )
            if err:
                return err
        changed = " and ".join(k for k in ("status", "tags") if k in body)
        return f"DTM alert '{trunc(alert_id, 80)}' updated ({changed})."

    async def search_gti_dtm_docs(
        query: str,
        doc_types: str = "",
        since: str = "",
        until: str = "",
        size: int = 10,
        page: str = "",
    ) -> str:
        """Search deep/dark-web documents with a Lucene query (forums, pastes, shops, chats)."""
        query = (query or "").strip()
        if not query:
            return "Error: a Lucene query string is required (e.g. 'acme.com AND password')."
        params: list[tuple[str, Any]] = [
            ("size", clamp(size, hi=25)),
            ("truncate", 400),
            ("sanitize", "true"),
        ]
        for value in csv_list(doc_types):
            params.append(("doc_type", value))
        if since.strip():
            params.append(("since", since.strip()))
        if until.strip():
            params.append(("until", until.strip()))
        if page.strip():
            params = [("page", page.strip())]
        async with client(timeout=60) as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/docs/search",
                _PROVIDER,
                f"search DTM documents for {trunc(query, 80)}",
                headers=headers,
                params=params,
                json={"query": query},
            )
            if err:
                return err
            payload = resp.json() or {}
            token = next_page_token(resp)
        docs = payload.get("docs") if isinstance(payload, dict) else None
        docs = docs if isinstance(docs, list) else []
        if not docs:
            return f"No deep/dark-web documents match '{trunc(query, 120)}'."
        lines = [f"DTM documents matching '{trunc(query, 100)}' ({len(docs)} shown):"]
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            inner = doc.get("__doc") if isinstance(doc.get("__doc"), dict) else doc
            doc_type = trunc(inner.get("__type") or inner.get("doc_type"), 40) or "?"
            doc_id = trunc(inner.get("__id") or inner.get("id"), 60) or "?"
            title = trunc(inner.get("title") or inner.get("subject"), 100)
            body = trunc(inner.get("body") or inner.get("text"), 200)
            when = trunc(inner.get("timestamp") or inner.get("__ts"), 25)
            head = f"  [{doc_type}] {title or body or '?'}"
            meta_bits = [b for b in (when, f"id: {doc_id}") if b]
            lines.append(f"{head} ({', '.join(meta_bits)})")
        return "\n".join(lines) + format_pagination_hint(token, "page")

    def _format_monitor(monitor: dict[str, Any]) -> str:
        enabled = "enabled" if monitor.get("enabled") else "disabled"
        return (
            f"  {trunc(monitor.get('name'), 100) or '?'} [{enabled}] "
            f"(id: {trunc(monitor.get('id'), 60) or '?'})"
        )

    async def list_gti_dtm_monitors(size: int = 10, page: str = "") -> str:
        """List the DTM monitors of your organisation."""
        params: list[tuple[str, Any]] = [("size", clamp(size, hi=50))]
        if page.strip():
            params = [("page", page.strip())]
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/monitors",
                _PROVIDER,
                "list DTM monitors",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
            token = next_page_token(resp)
        monitors = payload.get("monitors") if isinstance(payload, dict) else None
        monitors = monitors if isinstance(monitors, list) else []
        if not monitors:
            return (
                "No DTM monitors configured. Create one with "
                "manage_gti_dtm_monitor(action='create')."
            )
        lines = [f"DTM monitors ({len(monitors)} shown):"]
        lines.extend(_format_monitor(m) for m in monitors if isinstance(m, dict))
        return "\n".join(lines) + format_pagination_hint(token, "page")

    async def get_gti_dtm_monitor(monitor_id: str) -> str:
        """Read one DTM monitor: name, state, description, matching condition."""
        monitor_id = (monitor_id or "").strip()
        if not monitor_id:
            return "Error: monitor_id is required (from list_gti_dtm_monitors)."
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/monitors/{quote(monitor_id, safe='')}",
                _PROVIDER,
                f"get DTM monitor {trunc(monitor_id, 60)}",
                headers=headers,
                expected_statuses={404},
            )
            if err:
                if resp is not None and resp.status_code == 404:
                    return f"DTM monitor '{trunc(monitor_id, 80)}' not found."
                return err
            monitor = resp.json() or {}
        if not isinstance(monitor, dict):
            return f"DTM monitor '{trunc(monitor_id, 80)}' not found."
        lines = [f"DTM monitor {trunc(monitor.get('id'), 60) or monitor_id}:"]
        for key, label in (
            ("name", "Name"),
            ("description", "Description"),
            ("created_at", "Created"),
            ("updated_at", "Updated"),
        ):
            value = monitor.get(key)
            if value:
                lines.append(f"{label}: {trunc(value, 200)}")
        lines.append(f"Enabled: {'yes' if monitor.get('enabled') else 'no'}")
        condition = monitor.get("doc_condition")
        if isinstance(condition, dict):
            lines.append(f"Condition: {trunc(json.dumps(condition), 600)}")
        return "\n".join(lines)

    async def manage_gti_dtm_monitor(
        action: str,
        monitor_id: str = "",
        name: str = "",
        description: str = "",
        doc_condition: str = "",
    ) -> str:
        """Create, update, enable/disable, or permanently delete a DTM monitor."""
        action = (action or "").strip().lower()
        if action not in _MONITOR_ACTIONS:
            return f"Error: action must be one of {', '.join(sorted(_MONITOR_ACTIONS))}."
        monitor_id = (monitor_id or "").strip()
        if action != "create" and not monitor_id:
            return f"Error: monitor_id is required for action '{action}'."
        condition: dict[str, Any] | None = None
        if doc_condition.strip():
            try:
                condition = json.loads(doc_condition)
            except ValueError:
                return (
                    "Error: doc_condition must be a JSON Condition object, "
                    'e.g. {"operator": "all", "topic": "match_conditions", '
                    '"match": [{"operator": "must_equal", "topic": "keyword", '
                    '"match": ["acme.com"]}]}.'
                )
            if not isinstance(condition, dict):
                return "Error: doc_condition must be a JSON object."
        async with client() as c:
            if action == "create":
                if not name.strip():
                    return "Error: name is required to create a monitor."
                if condition is None:
                    return (
                        "Error: doc_condition (JSON) is required to create a "
                        "monitor - it defines what the monitor matches."
                    )
                body: dict[str, Any] = {
                    "name": name.strip(),
                    "enabled": True,
                    "doc_condition": condition,
                }
                if description.strip():
                    body["description"] = description.strip()
                resp, err = await safe_request(
                    c,
                    "post",
                    f"{api}/monitors",
                    _PROVIDER,
                    f"create DTM monitor '{trunc(name, 80)}'",
                    headers=headers,
                    json=body,
                )
                if err:
                    return err
                created = resp.json() or {}
                new_id = trunc(created.get("id"), 60) if isinstance(created, dict) else ""
                return f"DTM monitor '{trunc(name, 100)}' created (id: {new_id or '?'})."
            base_url = f"{api}/monitors/{quote(monitor_id, safe='')}"
            if action == "delete":
                _resp, err = await safe_request(
                    c,
                    "delete",
                    base_url,
                    _PROVIDER,
                    f"delete DTM monitor {trunc(monitor_id, 60)}",
                    headers=headers,
                )
                if err:
                    return err
                return (
                    f"DTM monitor '{trunc(monitor_id, 80)}' permanently "
                    "deleted (its alerts remain)."
                )
            body = {}
            if action in {"enable", "disable"}:
                body["enabled"] = action == "enable"
            else:  # update
                if name.strip():
                    body["name"] = name.strip()
                if description.strip():
                    body["description"] = description.strip()
                if condition is not None:
                    body["doc_condition"] = condition
                if not body:
                    return (
                        "Error: update requires name, description, and/or "
                        "doc_condition."
                    )
            _resp, err = await safe_request(
                c,
                "patch",
                base_url,
                _PROVIDER,
                f"{action} DTM monitor {trunc(monitor_id, 60)}",
                headers=headers,
                json=body,
            )
            if err:
                return err
        return f"DTM monitor '{trunc(monitor_id, 80)}' {action}d."

    executor.register("list_gti_dtm_alerts", list_gti_dtm_alerts)
    executor.register("get_gti_dtm_alert", get_gti_dtm_alert)
    executor.register("update_gti_dtm_alert", update_gti_dtm_alert)
    executor.register("search_gti_dtm_docs", search_gti_dtm_docs)
    executor.register("list_gti_dtm_monitors", list_gti_dtm_monitors)
    executor.register("get_gti_dtm_monitor", get_gti_dtm_monitor)
    executor.register("manage_gti_dtm_monitor", manage_gti_dtm_monitor)

    return [
        ToolDefinition(
            name="list_gti_dtm_alerts",
            description=(
                "List Google TI Digital Threat Monitoring alerts - "
                "deep/dark-web hits from your monitors (compromised "
                "credentials, domain discovery, forum posts, pastes, shop "
                "listings, messages). Filter by status, severity "
                "(high/medium/low), monitor_id, a Lucene search string, and "
                "an RFC3339 since/until window. Returns alert ids for "
                "get_gti_dtm_alert. Paginate with the returned page token "
                "(page replaces all other filters)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": (
                            "Comma-separated statuses: "
                            + ", ".join(_ALERT_STATUSES)
                        ),
                    },
                    "severity": {
                        "type": "string",
                        "description": "Comma-separated severities: high, medium, low",
                    },
                    "monitor_id": {
                        "type": "string",
                        "description": "Only alerts from this monitor",
                    },
                    "search": {
                        "type": "string",
                        "description": "Lucene query over alert and document contents",
                    },
                    "since": {
                        "type": "string",
                        "description": "Start of the date range, RFC3339 (e.g. 2026-07-01T00:00:00Z)",
                    },
                    "until": {
                        "type": "string",
                        "description": "End of the date range, RFC3339",
                    },
                    "size": {
                        "type": "integer",
                        "default": 10,
                        "description": "Alerts per page (1-25)",
                    },
                    "page": {
                        "type": "string",
                        "description": "Opaque page token from a previous call (used alone)",
                    },
                },
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_gti_dtm_alert",
            description=(
                "Read one DTM alert in full: title, status, severity, type, "
                "monitor, tags, analyst analysis, and a sanitized excerpt of "
                "the matched deep/dark-web document. Use after "
                "list_gti_dtm_alerts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {
                        "type": "string",
                        "description": "Alert id from list_gti_dtm_alerts",
                    },
                },
                "required": ["alert_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="update_gti_dtm_alert",
            description=(
                "Update a DTM alert as part of triage: change its status "
                "(new, read, escalated, in_progress, closed, "
                "no_action_required, duplicate, not_relevant, "
                "tracked_external) and/or set tags (comma-separated, "
                "replaces existing tags)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {
                        "type": "string",
                        "description": "Alert id from list_gti_dtm_alerts",
                    },
                    "status": {
                        "type": "string",
                        "enum": list(_ALERT_STATUSES),
                        "description": "New alert status",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags to set on the alert",
                    },
                },
                "required": ["alert_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="search_gti_dtm_docs",
            description=(
                "Search the raw deep/dark-web document corpus behind DTM "
                "with a Lucene query - forums, marketplaces, pastes, chats, "
                "web content - independent of your monitors. E.g. "
                "'acme.com AND (password OR credential)', "
                "'\"CVE-2026-1234\"'. Filter by doc_types (comma-separated, "
                "e.g. forum_post, paste, shop_listing, tweet, web_content) "
                "and an RFC3339 since/until window. Bodies are sanitized and "
                "truncated. Use for proactive hunting beyond configured "
                "monitors."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Lucene query string",
                    },
                    "doc_types": {
                        "type": "string",
                        "description": "Comma-separated document types to search",
                    },
                    "since": {
                        "type": "string",
                        "description": "Start timestamp (RFC3339)",
                    },
                    "until": {
                        "type": "string",
                        "description": "End timestamp (RFC3339)",
                    },
                    "size": {
                        "type": "integer",
                        "default": 10,
                        "description": "Documents per page (1-25)",
                    },
                    "page": {
                        "type": "string",
                        "description": "Opaque page token from a previous call (used alone)",
                    },
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_gti_dtm_monitors",
            description=(
                "List your organisation's DTM monitors (the saved conditions "
                "that generate alerts): name, enabled state, and id for "
                "get_gti_dtm_monitor / manage_gti_dtm_monitor."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "size": {
                        "type": "integer",
                        "default": 10,
                        "description": "Monitors per page (1-50)",
                    },
                    "page": {
                        "type": "string",
                        "description": "Opaque page token from a previous call (used alone)",
                    },
                },
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_gti_dtm_monitor",
            description=(
                "Read one DTM monitor: name, description, enabled state, "
                "dates, and the JSON matching condition that defines what it "
                "watches for."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "monitor_id": {
                        "type": "string",
                        "description": "Monitor id from list_gti_dtm_monitors",
                    },
                },
                "required": ["monitor_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_gti_dtm_monitor",
            description=(
                "Manage DTM monitors: 'create' (name + doc_condition JSON - "
                "a Condition object with operator/topic/match, e.g. keyword "
                "or credential_domain topics), 'update' (name / description "
                "/ doc_condition), 'enable' / 'disable', or 'delete' the "
                "monitor permanently (cannot be undone; existing alerts "
                "remain). Read get_gti_dtm_monitor first when editing a "
                "condition."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(_MONITOR_ACTIONS),
                    },
                    "monitor_id": {
                        "type": "string",
                        "description": "Monitor id (all actions except create)",
                    },
                    "name": {
                        "type": "string",
                        "description": "Monitor name (create) or new name (update)",
                    },
                    "description": {
                        "type": "string",
                        "description": "Monitor description",
                    },
                    "doc_condition": {
                        "type": "string",
                        "description": (
                            'JSON Condition object, e.g. {"operator": "all", '
                            '"topic": "match_conditions", "match": '
                            '[{"operator": "must_equal", "topic": '
                            '"credential_domain", "match": ["acme.com"]}]}'
                        ),
                    },
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
