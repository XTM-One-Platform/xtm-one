"""VirusTotal hunting tools - Livehunt (YARA rulesets + notifications) and
Retrohunt jobs. All endpoints live under ``/intelligence`` and require a
premium (VT Enterprise) API key with hunting privileges.

* Livehunt: rulesets CRUD (``/intelligence/hunting_rulesets``) + match
  notifications (``/intelligence/hunting_notifications``).
* Retrohunt: jobs (``/intelligence/retrohunt_jobs``) - scan the last year of
  the VirusTotal corpus with YARA rules - plus matching files.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from app.integrations.http import format_pagination_hint, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _PROVIDER,
    _SOURCE,
    as_int,
    attributes_of,
    clamp,
    client,
    csv_list,
    cursor_of,
    data_of,
    trunc,
    ts,
)

_RULESETS = "intelligence/hunting_rulesets"
_NOTIFICATIONS = "intelligence/hunting_notifications"
_RETROHUNT = "intelligence/retrohunt_jobs"

_MATCH_OBJECT_TYPES = frozenset({"file", "url", "domain", "ip"})
_RULESET_ACTIONS = frozenset({"update", "enable", "disable", "delete"})
_RETROHUNT_ACTIONS = frozenset({"abort", "delete"})
_RETROHUNT_CORPORA = frozenset({"main", "goodware"})


def _to_epoch(value: str) -> int | None:
    """Convert an epoch string or ISO date/datetime to a UNIX timestamp.

    The Retrohunt ``time_range`` fields must be UNIX epoch integers (UTC) -
    the API rejects date strings. Returns ``None`` for unparseable input.
    """
    raw = (value or "").strip()
    if raw.isdigit():
        return int(raw)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def _format_ruleset(obj: dict[str, Any]) -> str:
    attrs = attributes_of(obj)
    enabled = "enabled" if attrs.get("enabled") else "disabled"
    return (
        f"  {trunc(attrs.get('name'), 100) or '?'} [{enabled}, "
        f"match: {trunc(attrs.get('match_object_type'), 20) or 'file'}, "
        f"{trunc(attrs.get('number_of_rules'), 20) or '?'} rule(s), "
        f"limit {trunc(attrs.get('limit'), 20) or '?'}] (id: {trunc(obj.get('id'), 80) or '?'})"
    )


def _format_job(obj: dict[str, Any]) -> str:
    attrs = attributes_of(obj)
    progress = attrs.get("progress")
    progress_str = f", {progress:.0f}%" if isinstance(progress, (int, float)) else ""
    return (
        f"  {trunc(obj.get('id'), 80) or '?'}: {trunc(attrs.get('status'), 40) or '?'}{progress_str} - "
        f"{trunc(attrs.get('num_matches'), 20) or 0} match(es), "
        f"corpus {trunc(attrs.get('corpus'), 20) or 'main'}, "
        f"created {ts(attrs.get('creation_date')) or '?'}"
    )


def register(
    api: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    """Register Livehunt + Retrohunt tools and return their definitions."""

    # -- Livehunt rulesets ----------------------------------------------------
    async def list_vt_hunting_rulesets(
        filter: str = "",
        limit: int = 10,
        cursor: str = "",
    ) -> str:
        """List your Livehunt YARA rulesets (name, state, rule count, id)."""
        params: dict[str, Any] = {"limit": clamp(limit)}
        if filter.strip():
            params["filter"] = filter.strip()
        if cursor.strip():
            params["cursor"] = cursor.strip()
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/{_RULESETS}",
                _PROVIDER,
                "list Livehunt rulesets",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
        items = data_of(payload)
        items = items if isinstance(items, list) else []
        if not items:
            return "No Livehunt rulesets found."
        lines = [f"Livehunt rulesets ({len(items)} shown):"]
        lines.extend(_format_ruleset(o) for o in items if isinstance(o, dict))
        return "\n".join(lines) + format_pagination_hint(cursor_of(payload), "cursor")

    async def get_vt_hunting_ruleset(ruleset_id: str) -> str:
        """Read one Livehunt ruleset including its YARA source."""
        ruleset_id = (ruleset_id or "").strip()
        if not ruleset_id:
            return "Error: ruleset_id is required (see list_vt_hunting_rulesets)."
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/{_RULESETS}/{quote(ruleset_id, safe='')}",
                _PROVIDER,
                f"get Livehunt ruleset {trunc(ruleset_id, 60)}",
                headers=headers,
            )
            if err:
                return err
            payload = resp.json() or {}
        obj = data_of(payload)
        if not isinstance(obj, dict):
            return f"Livehunt ruleset '{trunc(ruleset_id, 60)}' not found."
        attrs = attributes_of(obj)
        lines = [
            f"Livehunt ruleset: {trunc(attrs.get('name'), 100) or '?'} "
            f"(id: {trunc(obj.get('id'), 80) or trunc(ruleset_id, 80)})",
            f"State: {'enabled' if attrs.get('enabled') else 'disabled'}",
            f"Match object type: {trunc(attrs.get('match_object_type'), 20) or 'file'}",
            f"Rules: {trunc(attrs.get('number_of_rules'), 20) or '?'} | "
            f"Daily notification limit: {trunc(attrs.get('limit'), 20) or '?'}",
        ]
        emails = attrs.get("notification_emails")
        if isinstance(emails, list) and emails:
            lines.append(f"Notification emails: {trunc(', '.join(str(e) for e in emails), 300)}")
        modified = ts(attrs.get("modification_date"))
        if modified:
            lines.append(f"Last modified: {modified} UTC")
        rules = attrs.get("rules")
        if rules:
            lines.append(f"\nYARA source:\n{str(rules)[:4000]}")
        return "\n".join(lines)

    async def create_vt_hunting_ruleset(
        name: str,
        rules: str,
        enabled: bool = True,
        limit: int = 100,
        match_object_type: str = "file",
        notification_emails: str = "",
    ) -> str:
        """Create (deploy) a new Livehunt YARA ruleset that matches against the live VirusTotal stream."""
        name = (name or "").strip()
        rules = (rules or "").strip()
        if not name or not rules:
            return "Error: name and rules (YARA source) are required."
        match_object_type = (match_object_type or "file").strip().lower()
        if match_object_type not in _MATCH_OBJECT_TYPES:
            return f"Error: match_object_type must be one of {', '.join(sorted(_MATCH_OBJECT_TYPES))}."
        attributes: dict[str, Any] = {
            "name": name,
            "rules": rules,
            "enabled": bool(enabled),
            "limit": clamp(limit, default=100, hi=10000),
            "match_object_type": match_object_type,
        }
        emails = csv_list(notification_emails)
        if emails:
            attributes["notification_emails"] = emails
        body = {"data": {"type": "hunting_ruleset", "attributes": attributes}}
        async with client() as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/{_RULESETS}",
                _PROVIDER,
                f"create Livehunt ruleset '{trunc(name, 80)}'",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        obj = data_of(payload)
        new_id = (trunc(obj.get("id"), 80) if isinstance(obj, dict) else "") or "?"
        return (
            f"Livehunt ruleset '{trunc(name, 100)}' created (id: {new_id}, "
            f"{'enabled' if enabled else 'disabled'}, matching {match_object_type} objects)."
        )

    async def manage_vt_hunting_ruleset(
        action: str,
        ruleset_id: str,
        name: str = "",
        rules: str = "",
        limit: int = 0,
    ) -> str:
        """Update, enable, disable, or delete a Livehunt ruleset."""
        action = (action or "").strip().lower()
        if action not in _RULESET_ACTIONS:
            return f"Error: action must be one of {', '.join(sorted(_RULESET_ACTIONS))}."
        ruleset_id = (ruleset_id or "").strip()
        if not ruleset_id:
            return "Error: ruleset_id is required (see list_vt_hunting_rulesets)."
        url = f"{api}/{_RULESETS}/{quote(ruleset_id, safe='')}"
        async with client() as c:
            if action == "delete":
                _resp, err = await safe_request(
                    c,
                    "delete",
                    url,
                    _PROVIDER,
                    f"delete Livehunt ruleset {trunc(ruleset_id, 60)}",
                    headers=headers,
                )
                if err:
                    return err
                return f"Livehunt ruleset '{trunc(ruleset_id, 80)}' permanently deleted."
            attributes: dict[str, Any] = {}
            if action == "enable":
                attributes["enabled"] = True
            elif action == "disable":
                attributes["enabled"] = False
            else:  # update
                if name.strip():
                    attributes["name"] = name.strip()
                if rules.strip():
                    attributes["rules"] = rules.strip()
                if limit:
                    attributes["limit"] = clamp(limit, default=100, hi=10000)
                if not attributes:
                    return "Error: update requires at least one of name, rules, or limit."
            body = {
                "data": {
                    "type": "hunting_ruleset",
                    "id": ruleset_id,
                    "attributes": attributes,
                }
            }
            _resp, err = await safe_request(
                c,
                "patch",
                url,
                _PROVIDER,
                f"{action} Livehunt ruleset {trunc(ruleset_id, 60)}",
                headers=headers,
                json=body,
            )
            if err:
                return err
        return f"Livehunt ruleset '{trunc(ruleset_id, 80)}' {action}d."

    # -- Livehunt notifications ------------------------------------------------
    async def list_vt_hunting_notifications(
        filter: str = "",
        limit: int = 10,
        cursor: str = "",
    ) -> str:
        """List Livehunt match notifications (which rule matched which file)."""
        params: dict[str, Any] = {"limit": clamp(limit)}
        if filter.strip():
            params["filter"] = filter.strip()
        if cursor.strip():
            params["cursor"] = cursor.strip()
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/{_NOTIFICATIONS}",
                _PROVIDER,
                "list Livehunt notifications",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
        items = data_of(payload)
        items = items if isinstance(items, list) else []
        if not items:
            return "No Livehunt notifications found."
        lines = [f"Livehunt notifications ({len(items)} shown):"]
        for obj in items:
            if not isinstance(obj, dict):
                continue
            attrs = attributes_of(obj)
            tags = attrs.get("tags")
            tag_str = (
                f" [{trunc(', '.join(str(t) for t in tags[:6]), 200)}]"
                if isinstance(tags, list) and tags
                else ""
            )
            lines.append(
                f"  rule '{trunc(attrs.get('rule_name'), 100) or '?'}'{tag_str} matched at "
                f"{ts(attrs.get('date')) or '?'} (notification id: {trunc(obj.get('id'), 80) or '?'})"
            )
        lines.append(
            "\nTip: get_vt_related_objects(entity_type='file', ...) on the "
            "matched hashes, or filter by tag with filter='tag:RULESET_NAME'."
        )
        return "\n".join(lines) + format_pagination_hint(cursor_of(payload), "cursor")

    async def delete_vt_hunting_notifications(tag: str) -> str:
        """Permanently delete Livehunt notifications carrying a tag (e.g. a ruleset name)."""
        tag = (tag or "").strip()
        if not tag:
            return (
                "Error: a tag is required (usually the ruleset or rule name) - "
                "deleting ALL notifications is not supported by this tool as a "
                "safety guard."
            )
        async with client() as c:
            _resp, err = await safe_request(
                c,
                "delete",
                f"{api}/{_NOTIFICATIONS}",
                _PROVIDER,
                f"delete Livehunt notifications tagged '{trunc(tag, 80)}'",
                headers=headers,
                params={"tag": tag},
            )
            if err:
                return err
        return f"Livehunt notifications tagged '{trunc(tag, 80)}' permanently deleted."

    # -- Retrohunt --------------------------------------------------------------
    async def list_vt_retrohunt_jobs(
        filter: str = "",
        limit: int = 10,
        cursor: str = "",
    ) -> str:
        """List your Retrohunt jobs with status and match counts."""
        params: dict[str, Any] = {"limit": clamp(limit)}
        if filter.strip():
            params["filter"] = filter.strip()
        if cursor.strip():
            params["cursor"] = cursor.strip()
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/{_RETROHUNT}",
                _PROVIDER,
                "list Retrohunt jobs",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
        items = data_of(payload)
        items = items if isinstance(items, list) else []
        if not items:
            return "No Retrohunt jobs found."
        lines = [f"Retrohunt jobs ({len(items)} shown):"]
        lines.extend(_format_job(o) for o in items if isinstance(o, dict))
        return "\n".join(lines) + format_pagination_hint(cursor_of(payload), "cursor")

    async def get_vt_retrohunt_job(job_id: str) -> str:
        """Check a Retrohunt job's status, progress, and match count."""
        job_id = (job_id or "").strip()
        if not job_id:
            return "Error: job_id is required (see list_vt_retrohunt_jobs)."
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/{_RETROHUNT}/{quote(job_id, safe='')}",
                _PROVIDER,
                f"get Retrohunt job {trunc(job_id, 60)}",
                headers=headers,
            )
            if err:
                return err
            payload = resp.json() or {}
        obj = data_of(payload)
        if not isinstance(obj, dict):
            return f"Retrohunt job '{trunc(job_id, 60)}' not found."
        attrs = attributes_of(obj)
        progress = attrs.get("progress")
        lines = [
            f"Retrohunt job {trunc(obj.get('id'), 80) or trunc(job_id, 80)}:",
            f"Status: {trunc(attrs.get('status'), 40) or '?'}"
            + (f" ({progress:.0f}%)" if isinstance(progress, (int, float)) else ""),
            f"Matches: {trunc(attrs.get('num_matches'), 20) or 0} | "
            f"Corpus: {trunc(attrs.get('corpus'), 20) or 'main'}",
            f"Created: {ts(attrs.get('creation_date')) or '?'} UTC",
        ]
        scanned = attrs.get("scanned_bytes")
        if scanned:
            lines.append(f"Scanned: {trunc(scanned, 20)} bytes")
        eta = attrs.get("eta_seconds")
        if eta:
            lines.append(f"ETA: ~{trunc(eta, 20)} seconds")
        if attrs.get("status") == "finished":
            lines.append("Fetch matches with get_vt_retrohunt_matches.")
        return "\n".join(lines)

    async def create_vt_retrohunt_job(
        rules: str,
        corpus: str = "main",
        start_time: str = "",
        end_time: str = "",
        notification_email: str = "",
    ) -> str:
        """Launch a Retrohunt job scanning the historical VirusTotal corpus with YARA rules."""
        rules = (rules or "").strip()
        if not rules:
            return "Error: rules (YARA source) are required."
        corpus = (corpus or "main").strip().lower()
        if corpus not in _RETROHUNT_CORPORA:
            return f"Error: corpus must be one of {', '.join(sorted(_RETROHUNT_CORPORA))}."
        attributes: dict[str, Any] = {"rules": rules, "corpus": corpus}
        # The API requires time_range bounds as UNIX epoch integers (UTC).
        time_range: dict[str, int] = {}
        for field, raw in (("start", start_time), ("end", end_time)):
            if not raw.strip():
                continue
            epoch = _to_epoch(raw)
            if epoch is None:
                return (
                    f"Error: {field}_time must be an ISO date/datetime "
                    "(e.g. 2025-01-01) or a UNIX epoch timestamp."
                )
            time_range[field] = epoch
        if time_range:
            attributes["time_range"] = time_range
        if notification_email.strip():
            attributes["notification_email"] = notification_email.strip()
        body = {"data": {"type": "retrohunt_job", "attributes": attributes}}
        async with client() as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/{_RETROHUNT}",
                _PROVIDER,
                "create Retrohunt job",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        obj = data_of(payload)
        new_id = (trunc(obj.get("id"), 80) if isinstance(obj, dict) else "") or "?"
        return (
            f"Retrohunt job started (id: {new_id}, corpus: {corpus}). Jobs "
            "typically take minutes to hours - poll get_vt_retrohunt_job."
        )

    async def get_vt_retrohunt_matches(job_id: str, limit: int = 10, cursor: str = "") -> str:
        """List the files matched by a finished Retrohunt job."""
        job_id = (job_id or "").strip()
        if not job_id:
            return "Error: job_id is required (see list_vt_retrohunt_jobs)."
        params: dict[str, Any] = {"limit": clamp(limit)}
        if cursor.strip():
            params["cursor"] = cursor.strip()
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/{_RETROHUNT}/{quote(job_id, safe='')}/matching_files",
                _PROVIDER,
                f"get Retrohunt matches for {trunc(job_id, 60)}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
        items = data_of(payload)
        items = items if isinstance(items, list) else []
        if not items:
            return f"No matching files for Retrohunt job '{trunc(job_id, 60)}' (yet)."
        lines = [f"Retrohunt matches for job {trunc(job_id, 60)} ({len(items)} shown):"]
        for obj in items:
            if not isinstance(obj, dict):
                continue
            attrs = attributes_of(obj)
            stats = attrs.get("last_analysis_stats")
            verdict = f" [{as_int(stats.get('malicious'))} malicious]" if isinstance(stats, dict) else ""
            context = obj.get("context_attributes")
            rule = ""
            if isinstance(context, dict) and context.get("rule_name"):
                rule = f" rule '{trunc(context['rule_name'], 100)}'"
            lines.append(
                f"  {trunc(attrs.get('meaningful_name'), 120) or '?'} "
                f"(sha256: {trunc(attrs.get('sha256'), 80) or trunc(obj.get('id'), 80) or '?'})"
                f"{verdict}{rule}"
            )
        return "\n".join(lines) + format_pagination_hint(cursor_of(payload), "cursor")

    async def manage_vt_retrohunt_job(action: str, job_id: str) -> str:
        """Abort a running Retrohunt job, or delete a job and its results."""
        action = (action or "").strip().lower()
        if action not in _RETROHUNT_ACTIONS:
            return f"Error: action must be one of {', '.join(sorted(_RETROHUNT_ACTIONS))}."
        job_id = (job_id or "").strip()
        if not job_id:
            return "Error: job_id is required (see list_vt_retrohunt_jobs)."
        async with client() as c:
            if action == "abort":
                _resp, err = await safe_request(
                    c,
                    "post",
                    f"{api}/{_RETROHUNT}/{quote(job_id, safe='')}/abort",
                    _PROVIDER,
                    f"abort Retrohunt job {trunc(job_id, 60)}",
                    headers=headers,
                )
                if err:
                    return err
                return f"Retrohunt job '{trunc(job_id, 80)}' aborted."
            _resp, err = await safe_request(
                c,
                "delete",
                f"{api}/{_RETROHUNT}/{quote(job_id, safe='')}",
                _PROVIDER,
                f"delete Retrohunt job {trunc(job_id, 60)}",
                headers=headers,
            )
            if err:
                return err
        return f"Retrohunt job '{trunc(job_id, 80)}' permanently deleted."

    executor.register("list_vt_hunting_rulesets", list_vt_hunting_rulesets)
    executor.register("get_vt_hunting_ruleset", get_vt_hunting_ruleset)
    executor.register("create_vt_hunting_ruleset", create_vt_hunting_ruleset)
    executor.register("manage_vt_hunting_ruleset", manage_vt_hunting_ruleset)
    executor.register("list_vt_hunting_notifications", list_vt_hunting_notifications)
    executor.register("delete_vt_hunting_notifications", delete_vt_hunting_notifications)
    executor.register("list_vt_retrohunt_jobs", list_vt_retrohunt_jobs)
    executor.register("get_vt_retrohunt_job", get_vt_retrohunt_job)
    executor.register("create_vt_retrohunt_job", create_vt_retrohunt_job)
    executor.register("get_vt_retrohunt_matches", get_vt_retrohunt_matches)
    executor.register("manage_vt_retrohunt_job", manage_vt_retrohunt_job)

    _page_props: dict[str, Any] = {
        "limit": {
            "type": "integer",
            "default": 10,
            "description": "Max items to return (1-40)",
        },
        "cursor": {
            "type": "string",
            "description": "Pagination cursor from a previous call",
        },
    }

    return [
        ToolDefinition(
            name="list_vt_hunting_rulesets",
            description=(
                "List your VirusTotal Livehunt YARA rulesets: name, "
                "enabled/disabled state, match object type, rule count, and "
                "the ruleset id used by the other hunting tools. Premium (VT "
                "Enterprise) feature. Filter e.g. 'enabled:true' or by name."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": "Optional filter, e.g. enabled:true or a name substring",
                    },
                    **_page_props,
                },
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_vt_hunting_ruleset",
            description=(
                "Read one Livehunt ruleset in full, including its YARA "
                "source, state, match object type, daily notification limit, "
                "and notification emails."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ruleset_id": {
                        "type": "string",
                        "description": "Ruleset id from list_vt_hunting_rulesets",
                    },
                },
                "required": ["ruleset_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_vt_hunting_ruleset",
            description=(
                "Deploy a new Livehunt YARA ruleset that matches every new "
                "object entering VirusTotal (files by default; also url, "
                "domain, or ip via match_object_type). Matches appear as "
                "hunting notifications. Premium feature. Provide the complete "
                "YARA source in 'rules'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Ruleset name"},
                    "rules": {
                        "type": "string",
                        "description": "Complete YARA ruleset source code",
                    },
                    "enabled": {
                        "type": "boolean",
                        "default": True,
                        "description": "Start matching immediately",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 100,
                        "description": "Max notifications per day (1-10000)",
                    },
                    "match_object_type": {
                        "type": "string",
                        "enum": sorted(_MATCH_OBJECT_TYPES),
                        "default": "file",
                        "description": "Entity kind the rules match against",
                    },
                    "notification_emails": {
                        "type": "string",
                        "description": "Comma-separated email addresses to notify (optional)",
                    },
                },
                "required": ["name", "rules"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_vt_hunting_ruleset",
            description=(
                "Manage an existing Livehunt ruleset: 'update' (change name, "
                "rules, or daily limit), 'enable' / 'disable' matching, or "
                "'delete' the ruleset permanently (cannot be undone)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(_RULESET_ACTIONS),
                    },
                    "ruleset_id": {
                        "type": "string",
                        "description": "Ruleset id from list_vt_hunting_rulesets",
                    },
                    "name": {"type": "string", "description": "New name (update)"},
                    "rules": {
                        "type": "string",
                        "description": "New complete YARA source (update)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "New daily notification limit (update)",
                    },
                },
                "required": ["action", "ruleset_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_vt_hunting_notifications",
            description=(
                "List Livehunt match notifications - which YARA rule matched "
                "which newly-submitted object and when. Filter with "
                "'tag:RULESET_OR_RULE_NAME' or a date filter. The matched "
                "file hashes can be enriched with get_vt_file_report."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": "Optional filter, e.g. tag:my_ruleset or date:2025-01-01+",
                    },
                    **_page_props,
                },
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="delete_vt_hunting_notifications",
            description=(
                "Permanently delete Livehunt notifications carrying a given "
                "tag (usually the ruleset or rule name). Use after processing "
                "a batch of matches. This cannot be undone; a tag is required "
                "as a safety guard."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tag": {
                        "type": "string",
                        "description": "Tag identifying the notifications, e.g. the ruleset name",
                    },
                },
                "required": ["tag"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_vt_retrohunt_jobs",
            description=(
                "List your Retrohunt jobs: status (starting / running / "
                "finished / aborted), progress, match counts, and job ids. "
                "Premium (VT Enterprise) feature."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": "Optional filter expression",
                    },
                    **_page_props,
                },
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_vt_retrohunt_job",
            description=(
                "Check one Retrohunt job: status, progress percentage, match "
                "count, corpus, and ETA. When finished, fetch the matched "
                "files with get_vt_retrohunt_matches."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job id from list_vt_retrohunt_jobs",
                    },
                },
                "required": ["job_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_vt_retrohunt_job",
            description=(
                "Launch a Retrohunt job: scan the historical VirusTotal file "
                "corpus (roughly the last 12 months, ~100TB) with YARA rules. "
                "Jobs run for minutes to hours - poll get_vt_retrohunt_job. "
                "corpus 'main' is the standard corpus, 'goodware' scans a "
                "known-clean set (for rule false-positive testing). Optional "
                "start_time/end_time (ISO dates or epoch) narrow the window."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "rules": {
                        "type": "string",
                        "description": "Complete YARA ruleset source code",
                    },
                    "corpus": {
                        "type": "string",
                        "enum": sorted(_RETROHUNT_CORPORA),
                        "default": "main",
                        "description": "Corpus to scan",
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Only scan files first seen after this time (optional)",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "Only scan files first seen before this time (optional)",
                    },
                    "notification_email": {
                        "type": "string",
                        "description": "Email to notify on completion (optional)",
                    },
                },
                "required": ["rules"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_vt_retrohunt_matches",
            description=(
                "List the files matched by a Retrohunt job (name, SHA-256, "
                "malicious-detection count, and which rule matched). Chain "
                "get_vt_file_report on interesting hashes. Paginate with "
                "limit + cursor."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job id from list_vt_retrohunt_jobs",
                    },
                    **_page_props,
                },
                "required": ["job_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_vt_retrohunt_job",
            description=(
                "Manage a Retrohunt job: 'abort' stops a running job (partial "
                "matches are kept), 'delete' permanently removes the job and "
                "its results (cannot be undone)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(_RETROHUNT_ACTIONS),
                    },
                    "job_id": {
                        "type": "string",
                        "description": "Job id from list_vt_retrohunt_jobs",
                    },
                },
                "required": ["action", "job_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
