"""Recorded Future case management tools (Case API ``/case``).

Cases are Recorded Future's triage-and-investigation primitive built on top of
classic alerts: an analyst opens a case around an alert notification, works it
through New / InProgress / Resolved / Dismissed, and records activity in the
case log.

* ``search_rf_cases`` - ``POST /case/search`` (ids) hydrated in the same call
  via ``POST /case/lookup`` (summary panel), so the agent gets titles and
  statuses instead of bare ``task:`` ids.
* ``get_rf_case``     - ``POST /case/lookup`` with all panels for one case.
* ``create_rf_case``  - ``POST /case/create`` (from an alert notification).
* ``update_rf_case``  - ``POST /case/update`` (status / priority / assignee /
  title / description).
"""

from __future__ import annotations

from typing import Any

from app.integrations.http import safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _PROVIDER, _SOURCE, clamp, client, csv_list, dt, trunc

# Case workflow vocabularies (Case API OpenAPI).
_CASE_STATUSES: frozenset[str] = frozenset(
    {"New", "InProgress", "Dismissed", "Resolved"}
)
_CASE_PRIORITIES: frozenset[str] = frozenset({"Informational", "Moderate", "High"})
# Case create context types: reference_alert (document-based) or signal_alert
# (entity risk-based).
_CASE_CONTEXT_TYPES: frozenset[str] = frozenset({"reference_alert", "signal_alert"})


def register(
    api: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    """Register case management tools and return their definitions."""

    async def _lookup_cases(
        c: Any, ids: list[str], panels: list[str]
    ) -> tuple[dict[str, Any], str | None]:
        resp, err = await safe_request(
            c,
            "post",
            f"{api}/case/lookup",
            _PROVIDER,
            "lookup cases",
            headers=headers,
            json={"ids": ids, "panels": panels},
        )
        if err:
            return {}, err
        payload = resp.json() or {}
        data = payload.get("data") or {}
        return (data if isinstance(data, dict) else {}), None

    def _fmt_case_summary(case_id: str, case: dict[str, Any]) -> str:
        summary = case.get("panel_summary") or case.get("summary") or case
        if not isinstance(summary, dict):
            summary = {}
        title = summary.get("title") or "?"
        status = summary.get("status", "?")
        priority = summary.get("priority", "?")
        assignee = (
            summary.get("assignee_name") or summary.get("assignee") or "Unassigned"
        )
        created = dt(summary.get("created"))
        return (
            f"  [{priority}/{status}] {trunc(title, 100)}\n"
            f"    id: {case_id} | assignee: {assignee}"
            + (f" | created: {created}" if created else "")
        )

    async def search_rf_cases(
        statuses: str = "",
        priorities: str = "",
        assignee: str = "",
        order_by: str = "modified",
        limit: int = 20,
    ) -> str:
        """Search Recorded Future cases and return their triage summaries.

        Filter by ``statuses`` (comma-separated New / InProgress / Dismissed /
        Resolved), ``priorities`` (Informational / Moderate / High), and
        ``assignee`` (a uhash id, or 'unassigned' to find unassigned cases).
        Returns case ids (task:...) for get_rf_case / update_rf_case.
        """
        body: dict[str, Any] = {
            "limit": clamp(limit, 20, hi=100),
            "order_by": "created" if order_by.strip().lower() == "created" else "modified",
            "direction": "desc",
        }
        raw_stat = csv_list(statuses)
        stat = [s for s in raw_stat if s in _CASE_STATUSES]
        if raw_stat and not stat:
            return f"Error: statuses must be a subset of {', '.join(sorted(_CASE_STATUSES))}."
        if stat:
            body["statuses"] = stat
        raw_prio = csv_list(priorities)
        prio = [p for p in raw_prio if p in _CASE_PRIORITIES]
        if raw_prio and not prio:
            return f"Error: priorities must be a subset of {', '.join(sorted(_CASE_PRIORITIES))}."
        if prio:
            body["priorities"] = prio
        assignee = assignee.strip()
        if assignee:
            # The API models "unassigned" as an empty-string assignee.
            body["assignees"] = [""] if assignee.lower() == "unassigned" else [assignee]
        async with client() as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/case/search",
                _PROVIDER,
                "search cases",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
            ids = [i for i in (payload.get("data") or []) if isinstance(i, str)]
            total = (payload.get("counts") or {}).get("total", len(ids))
            if not ids:
                return "No cases matched the filters."
            details, lerr = await _lookup_cases(c, ids, ["summary"])
            if lerr:
                # Search succeeded but hydration failed - still return the ids.
                id_lines = "\n".join(f"  {cid}" for cid in ids)
                return (
                    f"Found {total} case(s) (details lookup failed: {lerr}):\n{id_lines}"
                )
        lines = [_fmt_case_summary(cid, details.get(cid) or {}) for cid in ids]
        out = f"Found {total} case(s):\n" + "\n".join(lines)
        if payload.get("next_offset"):
            out += "\n[More cases available; narrow the filters or raise limit.]"
        return out

    async def get_rf_case(case_id: str) -> str:
        """Get one case by id: summary, alert context/enrichment details, and activity log."""
        cid = (case_id or "").strip()
        if not cid:
            return "Error: case_id is required."
        async with client() as c:
            details, err = await _lookup_cases(c, [cid], ["summary", "details", "log"])
            if err:
                return err
        case = details.get(cid)
        if not isinstance(case, dict):
            return f"No Recorded Future case found with id {cid}."
        lines = [_fmt_case_summary(cid, case).lstrip()]
        summary = case.get("panel_summary") or case.get("summary") or {}
        if isinstance(summary, dict) and summary.get("description"):
            lines.append(f"Description: {trunc(summary.get('description'), 500)}")
        detail = case.get("panel_details") or case.get("details") or {}
        if isinstance(detail, dict):
            context = detail.get("context") or {}
            if isinstance(context, dict) and context:
                bits = []
                if context.get("type"):
                    bits.append(f"type {context.get('type')}")
                if context.get("alert_notification"):
                    bits.append(f"alert {context.get('alert_notification')}")
                if context.get("alert_rule"):
                    bits.append(f"rule {context.get('alert_rule')}")
                if bits:
                    lines.append("Alert context: " + ", ".join(bits))
            entity = detail.get("entity") or {}
            if isinstance(entity, dict) and entity.get("name"):
                lines.append(
                    f"Entity: {entity.get('name')} ({entity.get('type', '?')}) "
                    f"- id: {entity.get('id', '?')}"
                )
        log = case.get("panel_log") or case.get("log") or []
        if isinstance(log, list) and log:
            lines.append(f"\nActivity log ({len(log)} entries, most recent first):")
            for entry in list(reversed(log))[:6]:
                if not isinstance(entry, dict):
                    continue
                author = entry.get("author_name") or entry.get("author") or "?"
                created = dt(entry.get("created"))
                changes = entry.get("changes")
                what = ""
                if isinstance(changes, list) and changes:
                    what = "; ".join(
                        f"{ch.get('type', 'change')}" for ch in changes[:3]
                        if isinstance(ch, dict)
                    )
                note = entry.get("message") or entry.get("comment") or ""
                text = what or trunc(note, 120) or "entry"
                lines.append(f"  - {created} {author}: {text}")
        return "\n".join(lines)

    async def create_rf_case(
        title: str,
        alert_notification: str,
        alert_rule: str,
        organization: str,
        context_type: str = "reference_alert",
        references: str = "",
        entity_id: str = "",
        description: str = "",
        priority: str = "",
        assignee: str = "",
    ) -> str:
        """Create a case around a classic alert for triage and investigation.

        Needs the alert notification id + alert rule id (from list_rf_alerts /
        list_rf_alert_rules) and your organization uhash (from the alert rule's
        owner). ``context_type`` is reference_alert (document-based; pass the
        triggering document ids in ``references``) or signal_alert (entity
        risk-based; pass ``entity_id`` and optionally ``references``). Returns
        the new case id (task:...).
        """
        title = (title or "").strip()
        if not title:
            return "Error: title is required."
        if not alert_notification.strip() or not alert_rule.strip():
            return "Error: alert_notification and alert_rule are both required."
        if not organization.strip():
            return "Error: organization (uhash:...) is required."
        context_type = (context_type or "reference_alert").strip().lower()
        if context_type not in _CASE_CONTEXT_TYPES:
            return f"Error: context_type must be one of {', '.join(sorted(_CASE_CONTEXT_TYPES))}."
        priority = priority.strip()
        if priority and priority not in _CASE_PRIORITIES:
            return f"Error: priority must be one of {', '.join(sorted(_CASE_PRIORITIES))}."
        refs = csv_list(references)
        context: dict[str, Any] = {
            "type": context_type,
            "alert_notification": alert_notification.strip(),
            "alert_rule": alert_rule.strip(),
        }
        if context_type == "reference_alert":
            if not refs:
                return (
                    "Error: references (comma-separated document ids) is required "
                    "when context_type='reference_alert'."
                )
            context["references"] = refs
        else:
            if not entity_id.strip():
                return "Error: entity_id is required when context_type='signal_alert'."
            context["entity"] = entity_id.strip()
            if refs:
                context["references"] = refs
        body: dict[str, Any] = {
            "context": context,
            "organization": organization.strip(),
            "title": title[:256],
        }
        if description.strip():
            body["description"] = description.strip()[:1000]
        if priority:
            body["priority"] = priority
        if assignee.strip():
            body["assignee"] = assignee.strip()
        async with client() as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/case/create",
                _PROVIDER,
                "create case",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        case_id = payload.get("data") or "?"
        return f"Created case '{trunc(title, 80)}' (id {case_id})."

    async def update_rf_case(
        case_id: str,
        status: str = "",
        priority: str = "",
        assignee: str = "",
        title: str = "",
        description: str = "",
    ) -> str:
        """Update a case: set status / priority / assignee / title / description.

        ``status`` is one of New / InProgress / Dismissed / Resolved; ``priority``
        is Informational / Moderate / High. Pass assignee='unassigned' to clear
        the assignee. Provide only the fields to change (at least one).
        """
        cid = (case_id or "").strip()
        if not cid:
            return "Error: case_id is required."
        status = status.strip()
        if status and status not in _CASE_STATUSES:
            return f"Error: status must be one of {', '.join(sorted(_CASE_STATUSES))}."
        priority = priority.strip()
        if priority and priority not in _CASE_PRIORITIES:
            return f"Error: priority must be one of {', '.join(sorted(_CASE_PRIORITIES))}."
        body: dict[str, Any] = {"id": cid}
        if status:
            body["status"] = status
        if priority:
            body["priority"] = priority
        assignee = assignee.strip()
        if assignee:
            # The API unassigns with an empty-string assignee.
            body["assignee"] = "" if assignee.lower() == "unassigned" else assignee
        if title.strip():
            body["title"] = title.strip()[:256]
        if description.strip():
            body["description"] = description.strip()[:1000]
        if len(body) == 1:
            return (
                "Error: provide at least one of status, priority, assignee, "
                "title, or description."
            )
        async with client() as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/case/update",
                _PROVIDER,
                f"update case {cid}",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        st = payload.get("status") or {}
        code = st.get("status_code", "OK") if isinstance(st, dict) else "OK"
        return f"Case {cid} updated ({code})."

    executor.register("search_rf_cases", search_rf_cases)
    executor.register("get_rf_case", get_rf_case)
    executor.register("create_rf_case", create_rf_case)
    executor.register("update_rf_case", update_rf_case)

    return [
        ToolDefinition(
            name="search_rf_cases",
            description=(
                "Search Recorded Future cases (the triage/investigation queue "
                "built on classic alerts). Filter by status (New/InProgress/"
                "Dismissed/Resolved), priority (Informational/Moderate/High), "
                "and assignee (uhash id, or 'unassigned'). Returns each case's "
                "title, status, priority, and id (task:...) for get_rf_case / "
                "update_rf_case."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "statuses": {
                        "type": "string",
                        "description": "Comma-separated statuses (New/InProgress/Dismissed/Resolved)",
                    },
                    "priorities": {
                        "type": "string",
                        "description": "Comma-separated priorities (Informational/Moderate/High)",
                    },
                    "assignee": {
                        "type": "string",
                        "description": "Assignee uhash id, or 'unassigned' for unassigned cases",
                    },
                    "order_by": {
                        "type": "string",
                        "enum": ["created", "modified"],
                        "default": "modified",
                    },
                    "limit": {"type": "integer", "default": 20},
                },
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_rf_case",
            description=(
                "Get one Recorded Future case by id (task:...): triage summary, "
                "originating alert context and enriched entity, and the case "
                "activity log."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string", "description": "Case id, e.g. task:96b1..."},
                },
                "required": ["case_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_rf_case",
            description=(
                "Create a Recorded Future case around a classic alert for triage "
                "and investigation. Requires the alert notification id and alert "
                "rule id (from list_rf_alerts / list_rf_alert_rules) plus your "
                "organization uhash. context_type reference_alert needs "
                "references (document ids); signal_alert needs entity_id. "
                "Returns the new case id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Case title (max 256 chars)"},
                    "alert_notification": {
                        "type": "string",
                        "description": "Alert notification id (from list_rf_alerts)",
                    },
                    "alert_rule": {
                        "type": "string",
                        "description": "Alert rule id (report:..., from list_rf_alert_rules)",
                    },
                    "organization": {
                        "type": "string",
                        "description": "Organization uhash id (uhash:..., from the alert rule owner)",
                    },
                    "context_type": {
                        "type": "string",
                        "enum": sorted(_CASE_CONTEXT_TYPES),
                        "default": "reference_alert",
                    },
                    "references": {
                        "type": "string",
                        "description": "Comma-separated document ids (required for reference_alert)",
                    },
                    "entity_id": {
                        "type": "string",
                        "description": "RF entity id (required for signal_alert)",
                    },
                    "description": {"type": "string", "description": "Case description (max 1000 chars)"},
                    "priority": {"type": "string", "enum": sorted(_CASE_PRIORITIES)},
                    "assignee": {"type": "string", "description": "Assignee uhash id"},
                },
                "required": ["title", "alert_notification", "alert_rule", "organization"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_rf_case",
            description=(
                "Update a Recorded Future case: set status (New/InProgress/"
                "Dismissed/Resolved), priority (Informational/Moderate/High), "
                "assignee ('unassigned' to clear), title, or description. "
                "Provide only the fields to change."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string", "description": "Case id (task:...)"},
                    "status": {"type": "string", "enum": sorted(_CASE_STATUSES)},
                    "priority": {"type": "string", "enum": sorted(_CASE_PRIORITIES)},
                    "assignee": {
                        "type": "string",
                        "description": "Assignee uhash id, or 'unassigned' to clear",
                    },
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["case_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
