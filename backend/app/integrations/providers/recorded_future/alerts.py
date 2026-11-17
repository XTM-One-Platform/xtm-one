"""Recorded Future alert tools - classic alerts and Playbook Alerts.

Gathering and triaging what Recorded Future is telling you:

* Classic Alerts (Alert API ``/alert``) - keyword / entity-watchlist alerts:
  ``list_rf_alerts`` (search), ``get_rf_alert`` (detail + hits),
  ``update_rf_alerts`` (batch set status / assignee / note), and
  ``list_rf_alert_rules`` (discover rule ids to filter by).
* Playbook Alerts (Playbook Alert API ``/playbook-alert``) - high-fidelity,
  enriched alerts (domain abuse, vulnerability, identity exposure, code-repo
  leakage, third-party risk, ...): ``search_rf_playbook_alerts``,
  ``get_rf_playbook_alert`` (preview), ``get_rf_playbook_alert_details``
  (category-specific evidence panels), and ``update_rf_playbook_alert``.
"""

from __future__ import annotations

from typing import Any

from app.integrations.http import format_entity_list, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _PROVIDER, _SOURCE, clamp, client, csv_list, dt, trunc

# Classic alert workflow statuses (Alert API v3 statusInPortal vocabulary).
_ALERT_STATUSES: frozenset[str] = frozenset(
    {"New", "Resolved", "Pending", "Dismissed", "Flag for Tuning"}
)
# Playbook Alert generic status / priority vocabularies.
_PA_STATUSES: frozenset[str] = frozenset(
    {"New", "InProgress", "Dismissed", "Resolved"}
)
_PA_PRIORITIES: frozenset[str] = frozenset({"High", "Moderate", "Informational"})
# Playbook Alert categories with a category-specific detail endpoint
# (``POST /playbook-alert/{category}/{id}``) - the Playbook Alert API's
# PlaybookAlertCategory enum.
_PA_CATEGORIES: frozenset[str] = frozenset(
    {
        "domain_abuse",
        "malicious_sites",
        "cyber_vulnerability",
        "code_repo_leakage",
        "third_party_risk",
        "identity_novel_exposures",
        "geopolitics_facility",
    }
)


def register(
    api: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    """Register classic + playbook alert tools and return their definitions."""

    # ══ classic alerts ════════════════════════════════════════════════════
    def _fmt_alert(a: dict[str, Any]) -> str:
        review = a.get("review") or {}
        rule = a.get("rule") or {}
        status = review.get("statusInPortal") or review.get("status") or "?"
        return (
            f"  [{status}] {trunc(a.get('title'), 100)}\n"
            f"    id: {a.get('id', '?')} | rule: {rule.get('name', '?')} | "
            f"triggered: {dt(a.get('triggered'))}"
        )

    async def list_rf_alerts(
        triggered: str = "",
        status: str = "",
        alert_rule: str = "",
        assignee: str = "",
        freetext: str = "",
        limit: int = 20,
        from_offset: int = 0,
    ) -> str:
        """List/search classic Recorded Future alerts (keyword & watchlist alerts).

        Filter by ``triggered`` time window (relative like '-24h' or a range
        like '[2024-01-01,2024-02-01]'), ``status`` (New / Resolved / Pending /
        Dismissed / Flag for Tuning), ``alert_rule`` (a rule id from
        list_rf_alert_rules), ``assignee``, or ``freetext``. Returns alert ids
        for get_rf_alert / update_rf_alerts.
        """
        params: dict[str, Any] = {
            "limit": clamp(limit, 20, hi=100),
            "from": max(0, int(from_offset) if str(from_offset).isdigit() else 0),
            "orderby": "triggered",
            "direction": "desc",
        }
        if triggered.strip():
            params["triggered"] = triggered.strip()
        status = status.strip()
        if status and status not in _ALERT_STATUSES:
            # Match the vocabulary update_rf_alerts enforces and the tool schema
            # enumerates, rather than forwarding an unknown status that yields an
            # API error or a confusingly empty result.
            return f"Error: status must be one of {', '.join(sorted(_ALERT_STATUSES))}."
        if status:
            params["statusInPortal"] = status
        if alert_rule.strip():
            params["alertRule"] = alert_rule.strip()
        if assignee.strip():
            params["assignee"] = assignee.strip()
        if freetext.strip():
            params["freetext"] = freetext.strip()
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/alert/v3",
                _PROVIDER,
                "list alerts",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
        results = ((payload.get("data") or {}).get("results")) or []
        total = (payload.get("counts") or {}).get("total", len(results))
        return format_entity_list(
            results, _fmt_alert, noun="alert", total=total, empty_msg="No alerts found."
        )

    async def get_rf_alert(alert_id: str) -> str:
        """Get a single classic alert by id: rule, status, and the triggering intelligence hits."""
        alert_id = (alert_id or "").strip()
        if not alert_id:
            return "Error: alert_id is required."
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/alert/v3/{alert_id}",
                _PROVIDER,
                f"get alert {alert_id}",
                headers=headers,
            )
            if err:
                return err
            payload = resp.json() or {}
        a = payload.get("data", payload) if isinstance(payload, dict) else {}
        review = a.get("review") or {}
        rule = a.get("rule") or {}
        lines = [
            f"Alert: {trunc(a.get('title'), 200)}",
            f"id: {a.get('id', alert_id)} | rule: {rule.get('name', '?')} ({rule.get('id', '?')})",
            f"Status: {review.get('statusInPortal') or review.get('status', '?')} | "
            f"Assignee: {review.get('assignee', 'Unassigned')} | "
            f"Triggered: {dt(a.get('triggered'))}",
        ]
        if a.get("url"):
            lines.append(f"Portal: {a.get('url')}")
        hits = a.get("hits")
        if isinstance(hits, list) and hits:
            lines.append(f"\nHits ({len(hits)}):")
            for hit in hits[:8]:
                if not isinstance(hit, dict):
                    continue
                entities = hit.get("entities") or []
                names = ", ".join(
                    e.get("name", "?") for e in entities[:4] if isinstance(e, dict)
                )
                doc = hit.get("document") or {}
                title = doc.get("title") or (doc.get("source") or {}).get("name") or ""
                lines.append(f"  - {trunc(title, 120)}" + (f" [{names}]" if names else ""))
        return "\n".join(lines)

    async def update_rf_alerts(
        alert_ids: str, status: str = "", assignee: str = "", note: str = ""
    ) -> str:
        """Batch-update classic alerts: set status, assignee, and/or add a note.

        ``alert_ids`` is a comma-separated list of alert ids. ``status`` is one
        of New / Resolved / Pending / Dismissed / Flag for Tuning. At least one
        of status / assignee / note must be provided.
        """
        ids = csv_list(alert_ids)
        if not ids:
            return "Error: provide at least one alert id in alert_ids."
        status = status.strip()
        if status and status not in _ALERT_STATUSES:
            return f"Error: status must be one of {', '.join(sorted(_ALERT_STATUSES))}."
        if not any([status, assignee.strip(), note.strip()]):
            return "Error: provide at least one of status, assignee, or note."
        entry: dict[str, Any] = {}
        if status:
            entry["statusInPortal"] = status
        if assignee.strip():
            entry["assignee"] = assignee.strip()
        if note.strip():
            entry["note"] = note.strip()
        body = [{"id": aid, **entry} for aid in ids]
        async with client() as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/alert/v2/update",
                _PROVIDER,
                "update alerts",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        ok = payload.get("success") or []
        errors = payload.get("error") or []
        out = f"Updated {len(ok)} alert(s)."
        if errors:
            details = "; ".join(
                f"{e.get('id', '?')}: {e.get('reason', 'failed')}" for e in errors[:5]
            )
            out += f" {len(errors)} failed: {details}"
        return out

    async def list_rf_alert_rules(freetext: str = "", limit: int = 25) -> str:
        """List classic alert rules (discover rule ids for list_rf_alerts' alert_rule filter)."""
        params: dict[str, Any] = {"limit": clamp(limit, 25, hi=100)}
        if freetext.strip():
            params["freetext"] = freetext.strip()
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/alert/v2/rule",
                _PROVIDER,
                "list alert rules",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
        results = ((payload.get("data") or {}).get("results")) or []
        total = (payload.get("counts") or {}).get("total", len(results))

        def _fmt(r: dict[str, Any]) -> str:
            state = "enabled" if r.get("enabled") else "disabled"
            prio = " priority" if r.get("priority") else ""
            return f"  {trunc(r.get('title'), 100)} - id: {r.get('id', '?')} ({state}{prio})"

        return format_entity_list(
            results, _fmt, noun="alert rule", total=total, empty_msg="No alert rules found."
        )

    # ══ playbook alerts ════════════════════════════════════════════════════
    def _fmt_pa(a: dict[str, Any]) -> str:
        return (
            f"  [{a.get('priority', '?')}/{a.get('status', '?')}] "
            f"{trunc(a.get('title'), 100)}\n"
            f"    id: {a.get('playbook_alert_id', '?')} | "
            f"category: {a.get('category', '?')} | updated: {dt(a.get('updated'))}"
        )

    async def search_rf_playbook_alerts(
        category: str = "",
        statuses: str = "",
        priority: str = "",
        limit: int = 20,
    ) -> str:
        """Search Recorded Future Playbook Alerts (high-fidelity enriched alerts).

        Optionally filter by ``category`` (comma-separated, e.g. 'domain_abuse,
        cyber_vulnerability,identity_novel_exposures,code_repo_leakage,
        third_party_risk'), ``statuses`` (New / InProgress / Dismissed /
        Resolved), and ``priority`` (High / Moderate / Informational). Returns
        playbook_alert_ids for get_rf_playbook_alert / update_rf_playbook_alert.
        """
        body: dict[str, Any] = {
            "limit": clamp(limit, 20, hi=100),
            "order_by": "updated",
            "direction": "desc",
        }
        cats = csv_list(category)
        if cats:
            body["category"] = cats
        # Reject a non-empty statuses/priority whose values are all invalid rather
        # than silently dropping the filter (which would return every alert).
        raw_stat = csv_list(statuses)
        stat = [s for s in raw_stat if s in _PA_STATUSES]
        if raw_stat and not stat:
            return f"Error: statuses must be a subset of {', '.join(sorted(_PA_STATUSES))}."
        if stat:
            body["statuses"] = stat
        raw_prio = csv_list(priority)
        prio = [p for p in raw_prio if p in _PA_PRIORITIES]
        if raw_prio and not prio:
            return f"Error: priority must be a subset of {', '.join(sorted(_PA_PRIORITIES))}."
        if prio:
            body["priority"] = prio
        async with client() as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/playbook-alert/search",
                _PROVIDER,
                "search playbook alerts",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        # The Playbook Alert search returns its results directly under ``data``.
        results = payload.get("data") or []
        if isinstance(results, dict):
            results = results.get("results", [])
        return format_entity_list(
            results,
            _fmt_pa,
            noun="playbook alert",
            total=(payload.get("counts") or {}).get("total", len(results)),
            empty_msg="No playbook alerts found.",
        )

    async def get_rf_playbook_alert(playbook_alert_id: str) -> str:
        """Get the common (preview) data for a Playbook Alert by id: status, priority, title, log."""
        pid = (playbook_alert_id or "").strip()
        if not pid:
            return "Error: playbook_alert_id is required."
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/playbook-alert/common/{pid}",
                _PROVIDER,
                f"get playbook alert {pid}",
                headers=headers,
            )
            if err:
                return err
            payload = resp.json() or {}
        a = payload.get("data", payload) if isinstance(payload, dict) else {}
        lines = [
            f"Playbook Alert: {trunc(a.get('title'), 200)}",
            f"id: {a.get('playbook_alert_id', pid)} | category: {a.get('category', '?')}",
            f"Status: {a.get('status', '?')} | Priority: {a.get('priority', '?')} | "
            f"Assignee: {a.get('assignee', 'Unassigned')}",
            f"Created: {dt(a.get('created'))} | Updated: {dt(a.get('updated'))}",
        ]
        log = a.get("log")
        if isinstance(log, list) and log:
            lines.append(f"Log entries: {len(log)} (most recent: {dt((log[-1] or {}).get('created'))})")
        return "\n".join(lines)

    def _render_panels(data: dict[str, Any]) -> list[str]:
        """Flatten a category detail response's ``panel_*`` blocks into text.

        Panels are UI-oriented nested JSON whose exact shape differs per
        category; render a bounded, human-readable outline instead of trying
        to model every category schema.
        """

        def _flatten(value: Any, depth: int) -> list[str]:
            pad = "  " * depth
            out: list[str] = []
            if isinstance(value, dict):
                for key, val in list(value.items())[:12]:
                    if val in (None, "", [], {}):
                        continue
                    if isinstance(val, (dict, list)):
                        out.append(f"{pad}{key}:")
                        out.extend(_flatten(val, depth + 1))
                    else:
                        out.append(f"{pad}{key}: {trunc(val, 160)}")
            elif isinstance(value, list):
                for item in value[:6]:
                    if isinstance(item, (dict, list)):
                        out.extend(_flatten(item, depth))
                    else:
                        out.append(f"{pad}- {trunc(item, 160)}")
                if len(value) > 6:
                    out.append(f"{pad}... and {len(value) - 6} more")
            return out[:40]

        lines: list[str] = []
        for key, val in data.items():
            if not key.startswith("panel_") or val in (None, "", [], {}):
                continue
            lines.append(f"\n{key}:")
            lines.extend(_flatten(val, 1))
        return lines

    async def get_rf_playbook_alert_details(
        playbook_alert_id: str, category: str, panels: str = ""
    ) -> str:
        """Get the category-specific evidence panels for a Playbook Alert.

        ``category`` must match the alert's category from
        search_rf_playbook_alerts (domain_abuse, malicious_sites,
        cyber_vulnerability, code_repo_leakage, third_party_risk,
        identity_novel_exposures, geopolitics_facility). Returns the full
        evidence detail - e.g. DNS/WHOIS records and remediation actions for
        domain abuse, exposure records for identity alerts - that the common
        preview (get_rf_playbook_alert) omits. ``panels`` optionally narrows
        to a comma-separated subset (e.g. 'status,summary,dns,whois,action,log').
        """
        pid = (playbook_alert_id or "").strip()
        if not pid:
            return "Error: playbook_alert_id is required."
        category = (category or "").strip().lower()
        if category not in _PA_CATEGORIES:
            return f"Error: category must be one of {', '.join(sorted(_PA_CATEGORIES))}."
        body: dict[str, Any] = {}
        wanted = csv_list(panels)
        if wanted:
            body["panels"] = wanted
        async with client(timeout=45) as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/playbook-alert/{category}/{pid}",
                _PROVIDER,
                f"get playbook alert details {pid}",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        a = payload.get("data", payload) if isinstance(payload, dict) else {}
        if not isinstance(a, dict) or not a:
            return f"No detail data returned for playbook alert {pid}."
        status = a.get("panel_status") or {}
        title = status.get("entity_name") or status.get("case_rule_label") or pid
        lines = [
            f"Playbook Alert detail ({category}): {trunc(title, 150)}",
            f"id: {a.get('playbook_alert_id', pid)}",
        ]
        if isinstance(status, dict) and status:
            head_bits = []
            for key in ("status", "priority", "assignee_name", "risk_score"):
                if status.get(key) not in (None, ""):
                    head_bits.append(f"{key}: {status.get(key)}")
            if head_bits:
                lines.append(" | ".join(head_bits))
        lines.extend(_render_panels(a))
        return "\n".join(lines)

    async def update_rf_playbook_alert(
        playbook_alert_id: str,
        status: str = "",
        priority: str = "",
        assignee: str = "",
        log_entry: str = "",
    ) -> str:
        """Update a Playbook Alert: set status / priority / assignee, or append a log entry.

        ``status`` is one of New / InProgress / Dismissed / Resolved; ``priority``
        is High / Moderate / Informational. Provide only the fields to change.
        """
        pid = (playbook_alert_id or "").strip()
        if not pid:
            return "Error: playbook_alert_id is required."
        status = status.strip()
        priority = priority.strip()
        if status and status not in _PA_STATUSES:
            return f"Error: status must be one of {', '.join(sorted(_PA_STATUSES))}."
        if priority and priority not in _PA_PRIORITIES:
            return f"Error: priority must be one of {', '.join(sorted(_PA_PRIORITIES))}."
        body: dict[str, Any] = {}
        if status:
            body["status"] = status
        if priority:
            body["priority"] = priority
        if assignee.strip():
            body["assignee"] = assignee.strip()
        if log_entry.strip():
            body["log_entry"] = log_entry.strip()[:5000]
        if not body:
            return "Error: provide at least one of status, priority, assignee, or log_entry."
        async with client() as c:
            resp, err = await safe_request(
                c,
                "put",
                f"{api}/playbook-alert/common/{pid}",
                _PROVIDER,
                f"update playbook alert {pid}",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        code = payload.get("status_code", "Ok")
        msg = payload.get("status_message", "")
        return f"Playbook alert {pid} updated ({code}). {msg}".strip()

    executor.register("list_rf_alerts", list_rf_alerts)
    executor.register("get_rf_alert", get_rf_alert)
    executor.register("update_rf_alerts", update_rf_alerts)
    executor.register("list_rf_alert_rules", list_rf_alert_rules)
    executor.register("search_rf_playbook_alerts", search_rf_playbook_alerts)
    executor.register("get_rf_playbook_alert", get_rf_playbook_alert)
    executor.register("get_rf_playbook_alert_details", get_rf_playbook_alert_details)
    executor.register("update_rf_playbook_alert", update_rf_playbook_alert)

    return [
        ToolDefinition(
            name="list_rf_alerts",
            description=(
                "List/search classic Recorded Future alerts (keyword & "
                "entity-watchlist alerts). Filter by triggered time window, "
                "status (New/Resolved/Pending/Dismissed/Flag for Tuning), a rule "
                "id, assignee, or freetext. Returns alert ids."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "triggered": {
                        "type": "string",
                        "description": "Time window, e.g. '-24h' or '[2024-01-01,2024-02-01]'",
                    },
                    "status": {
                        "type": "string",
                        "enum": sorted(_ALERT_STATUSES),
                    },
                    "alert_rule": {
                        "type": "string",
                        "description": "Alert rule id (from list_rf_alert_rules)",
                    },
                    "assignee": {"type": "string"},
                    "freetext": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                    "from_offset": {"type": "integer", "default": 0},
                },
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_rf_alert",
            description=(
                "Get a single classic Recorded Future alert by id, including the "
                "triggering rule, workflow status, and the intelligence hits that "
                "fired it."
            ),
            input_schema={
                "type": "object",
                "properties": {"alert_id": {"type": "string"}},
                "required": ["alert_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="update_rf_alerts",
            description=(
                "Batch-update classic Recorded Future alerts: set workflow status "
                "(New/Resolved/Pending/Dismissed/Flag for Tuning), assignee, "
                "and/or add a note. alert_ids is comma-separated."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_ids": {
                        "type": "string",
                        "description": "Comma-separated alert ids",
                    },
                    "status": {"type": "string", "enum": sorted(_ALERT_STATUSES)},
                    "assignee": {
                        "type": "string",
                        "description": "Assignee id, uhash, username, or email",
                    },
                    "note": {"type": "string"},
                },
                "required": ["alert_ids"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_rf_alert_rules",
            description=(
                "List classic alert rules to discover the rule ids used to filter "
                "list_rf_alerts. Optionally search by freetext."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "freetext": {"type": "string"},
                    "limit": {"type": "integer", "default": 25},
                },
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_rf_playbook_alerts",
            description=(
                "Search Recorded Future Playbook Alerts (high-fidelity enriched "
                "alerts: domain abuse, vulnerability, identity exposure, code-repo "
                "leakage, third-party risk, etc.). Filter by category, status "
                "(New/InProgress/Dismissed/Resolved), and priority "
                "(High/Moderate/Informational). Returns playbook_alert_ids."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": (
                            "Comma-separated categories, e.g. 'domain_abuse,"
                            "cyber_vulnerability,identity_novel_exposures'"
                        ),
                    },
                    "statuses": {
                        "type": "string",
                        "description": "Comma-separated statuses (New/InProgress/Dismissed/Resolved)",
                    },
                    "priority": {
                        "type": "string",
                        "description": "Comma-separated priorities (High/Moderate/Informational)",
                    },
                    "limit": {"type": "integer", "default": 20},
                },
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_rf_playbook_alert",
            description=(
                "Get the common preview data for a Recorded Future Playbook Alert "
                "by id: status, priority, assignee, category, and log summary."
            ),
            input_schema={
                "type": "object",
                "properties": {"playbook_alert_id": {"type": "string"}},
                "required": ["playbook_alert_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_rf_playbook_alert_details",
            description=(
                "Get the category-specific evidence panels for a Recorded "
                "Future Playbook Alert - the full detail the common preview "
                "omits (e.g. DNS/WHOIS records and remediation actions for "
                "domain_abuse, exposure records for identity_novel_exposures, "
                "affected products for cyber_vulnerability). category must "
                "match the alert's category from search_rf_playbook_alerts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "playbook_alert_id": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": sorted(_PA_CATEGORIES),
                    },
                    "panels": {
                        "type": "string",
                        "description": (
                            "Optional comma-separated panel subset, e.g. "
                            "'status,summary,dns,whois,action,log' (all when omitted)"
                        ),
                    },
                },
                "required": ["playbook_alert_id", "category"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="update_rf_playbook_alert",
            description=(
                "Update a Recorded Future Playbook Alert: set status "
                "(New/InProgress/Dismissed/Resolved), priority "
                "(High/Moderate/Informational), assignee, or append a log entry. "
                "Provide only the fields to change."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "playbook_alert_id": {"type": "string"},
                    "status": {"type": "string", "enum": sorted(_PA_STATUSES)},
                    "priority": {"type": "string", "enum": sorted(_PA_PRIORITIES)},
                    "assignee": {"type": "string", "description": "Assignee id (uhash:...)"},
                    "log_entry": {
                        "type": "string",
                        "description": "Free-text log message (max 5000 chars)",
                    },
                },
                "required": ["playbook_alert_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
