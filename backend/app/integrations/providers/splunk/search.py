"""Splunk core search, saved/correlation searches, and data context tools.

Covers the threat-hunting heart of the integration (the asynchronous Search Job
API: create -> poll -> fetch, mirroring QRadar's AQL flow), the saved-search /
correlation-search lifecycle (ES detection rules are saved searches), and the
data context an analyst needs (indexes, fired alerts, server status).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    DEFAULT_SEARCH_EARLIEST,
    DEFAULT_SEARCH_LATEST,
    MAX_PAGE,
    MAX_RESULTS,
    SEARCH_TIMEOUT,
    SOURCE,
    ApiFn,
    content,
    dt,
    entries,
    execute_search,
    fetch_job_results,
    format_results_table,
    safe_int,
    trunc,
    wait_for_job,
)

_SAVED_SEARCH_ACTIONS = ("create", "update", "enable", "disable", "delete")


def register(base_url: str, api: ApiFn, executor: CallableToolExecutor) -> list[ToolDefinition]:
    """Register the Splunk core search + context tools."""

    # ══════════════════════════════════════════════════════════════════════
    # 0. CONNECTION / STATUS
    # ══════════════════════════════════════════════════════════════════════

    async def get_splunk_status() -> str:
        """Validate the Splunk connection and report version + whether ES is installed."""
        data, err = await api("get", "/services/server/info", "get server info")
        if err:
            return err
        ents = entries(data)
        c = content(ents[0]) if ents else {}
        version = c.get("version", "?")
        server = c.get("serverName", "?")
        # Best-effort ES detection via a filtered apps listing (avoids a 404 when
        # the ES app is absent).
        es_line = "Enterprise Security: not detected"
        apps, aerr = await api(
            "get",
            "/services/apps/local",
            "check Enterprise Security app",
            params={"search": "name=SplunkEnterpriseSecuritySuite", "count": 1},
        )
        if not aerr and entries(apps):
            ec = content(entries(apps)[0])
            es_line = f"Enterprise Security: installed (v{ec.get('version', '?')})"
        return (
            f"Connected to Splunk at {base_url}.\n"
            f"  Version: {version} | Server: {server}\n  {es_line}"
        )

    # ══════════════════════════════════════════════════════════════════════
    # 1. SEARCH (threat hunting)
    # ══════════════════════════════════════════════════════════════════════

    async def run_splunk_search(
        query: str,
        earliest_time: str = DEFAULT_SEARCH_EARLIEST,
        latest_time: str = DEFAULT_SEARCH_LATEST,
        limit: int = 50,
    ) -> str:
        """Run an SPL search (threat hunting) and wait for the results."""
        q = (query or "").strip()
        if not q:
            return "Error: a Splunk search query (SPL) is required."
        limit = safe_int(limit, 50, lo=1, hi=MAX_RESULTS)
        results, status = await execute_search(api, q, earliest_time, latest_time, limit)
        if results is None:
            return status or "Error: the Splunk search returned no readable results."
        return format_results_table(results, max_rows=limit)

    async def get_splunk_search_results(sid: str, limit: int = 50) -> str:
        """Fetch results for an existing Splunk search job by its sid."""
        if not sid or not sid.strip():
            return "Error: a Splunk search job id (sid) is required."
        sid = sid.strip()
        poll, err = await api(
            "get",
            f"/services/search/jobs/{quote(sid, safe='')}",
            f"get search job {sid}",
        )
        if err:
            return err
        ents = entries(poll)
        if not ents:
            return f"Splunk search job {sid} not found."
        c = content(ents[0])
        if not bool(c.get("isDone")):
            return (
                f"Splunk search job {sid} is not finished "
                f"(dispatchState {c.get('dispatchState', '?')}). Try again shortly."
            )
        limit = safe_int(limit, 50, lo=1, hi=MAX_RESULTS)
        results, ferr = await fetch_job_results(api, sid, limit)
        if results is None:
            return ferr or "Error: could not fetch the search results."
        return format_results_table(results, max_rows=limit)

    # ══════════════════════════════════════════════════════════════════════
    # 2. SAVED / CORRELATION SEARCHES
    # ══════════════════════════════════════════════════════════════════════

    def _is_true(value: Any) -> bool:
        return str(value).strip().lower() in ("1", "true", "yes")

    def _fmt_saved_search(e: dict) -> str:
        c = content(e)
        flags = []
        if _is_true(c.get("disabled")):
            flags.append("disabled")
        if _is_true(c.get("is_scheduled")):
            flags.append("scheduled")
        if _is_true(c.get("action.correlationsearch.enabled")):
            flags.append("correlation")
        return (
            f"  {e.get('name', '?')}"
            + (f" [{', '.join(flags)}]" if flags else "")
            + (f" | cron: {c.get('cron_schedule')}" if c.get("cron_schedule") else "")
            + f"\n    SPL: {trunc(c.get('search'), 160) or 'N/A'}"
        )

    async def list_splunk_saved_searches(
        name_filter: str = "",
        correlation_only: bool = False,
        limit: int = 25,
    ) -> str:
        """List saved searches (includes ES correlation searches / detection rules)."""
        params: dict[str, Any] = {"count": safe_int(limit, 25, lo=1, hi=MAX_PAGE)}
        if correlation_only:
            params["search"] = "action.correlationsearch.enabled=1"
        elif name_filter:
            params["search"] = f"name=*{name_filter}*"
        data, err = await api(
            "get", "/services/saved/searches", "list saved searches", params=params
        )
        if err:
            return err
        return format_entity_list(entries(data), _fmt_saved_search, noun="saved search")

    async def get_splunk_saved_search(name: str) -> str:
        """Get full detail of a saved search by name (SPL, schedule, alert actions)."""
        if not name or not name.strip():
            return "Error: a saved search name is required."
        data, err = await api(
            "get",
            f"/services/saved/searches/{quote(name.strip(), safe='')}",
            f"get saved search {name}",
        )
        if err:
            return err
        ents = entries(data)
        if not ents:
            return f"Saved search '{name}' not found."
        c = content(ents[0])
        lines = [
            f"Saved search: {ents[0].get('name', name)}",
            f"SPL: {trunc(c.get('search'), 400) or 'N/A'}",
            f"Disabled: {c.get('disabled', '?')} | Scheduled: {c.get('is_scheduled', '?')} | "
            f"Cron: {c.get('cron_schedule') or 'N/A'}",
            f"Time range: {c.get('dispatch.earliest_time') or '?'} -> "
            f"{c.get('dispatch.latest_time') or '?'}",
        ]
        if _is_true(c.get("action.correlationsearch.enabled")):
            lines.append(
                "Correlation search: YES | "
                f"Severity: {c.get('alert.severity') or '?'} | "
                f"Label: {trunc(c.get('action.correlationsearch.label') or c.get('description'), 160)}"
            )
        if c.get("actions"):
            lines.append(f"Alert actions: {trunc(c.get('actions'), 200)}")
        return "\n".join(lines)

    async def run_splunk_saved_search(
        name: str, trigger_actions: bool = False, limit: int = 50
    ) -> str:
        """Dispatch (run) a saved or correlation search and return the results.

        Set trigger_actions=true to also fire the search's alert actions (e.g.
        create notable events). Default false runs it read-only for inspection.
        """
        if not name or not name.strip():
            return "Error: a saved search name is required."
        limit = safe_int(limit, 50, lo=1, hi=MAX_RESULTS)
        create, err = await api(
            "post",
            f"/services/saved/searches/{quote(name.strip(), safe='')}/dispatch",
            f"dispatch saved search {name}",
            data={"trigger_actions": "1" if trigger_actions else "0"},
            timeout=SEARCH_TIMEOUT,
        )
        if err:
            return err
        sid = create.get("sid") if isinstance(create, dict) else None
        results, status = await wait_for_job(api, sid or "", limit)
        if results is None:
            return status or "Error: the saved search returned no readable results."
        return format_results_table(results, max_rows=limit)

    async def manage_splunk_saved_search(
        action: str,
        name: str,
        search: str = "",
        cron_schedule: str = "",
        is_scheduled: bool | None = None,
        earliest_time: str = "",
        latest_time: str = "",
    ) -> str:
        """Create, update, enable, disable, or delete a saved search (long tail)."""
        act = (action or "").strip().lower()
        if act not in _SAVED_SEARCH_ACTIONS:
            return f"Error: action must be one of {', '.join(_SAVED_SEARCH_ACTIONS)}."
        if not name or not name.strip():
            return "Error: a saved search name is required."
        name = name.strip()
        encoded = quote(name, safe="")

        if act == "delete":
            _, err = await api(
                "delete",
                f"/services/saved/searches/{encoded}",
                f"delete saved search {name}",
            )
            return err or f"Saved search '{name}' deleted."

        if act in ("enable", "disable"):
            _, err = await api(
                "post",
                f"/services/saved/searches/{encoded}",
                f"{act} saved search {name}",
                data={"disabled": "0" if act == "enable" else "1"},
            )
            return err or f"Saved search '{name}' {act}d."

        body: dict[str, Any] = {}
        if search:
            body["search"] = search
        if cron_schedule:
            body["cron_schedule"] = cron_schedule
        if is_scheduled is not None:
            body["is_scheduled"] = "1" if is_scheduled else "0"
        if earliest_time:
            body["dispatch.earliest_time"] = earliest_time
        if latest_time:
            body["dispatch.latest_time"] = latest_time

        if act == "create":
            if not search:
                return "Error: creating a saved search requires the search (SPL)."
            body["name"] = name
            _, err = await api(
                "post", "/services/saved/searches", f"create saved search {name}", data=body
            )
            return err or f"Saved search '{name}' created."

        # update
        if not body:
            return "Error: no fields provided to update."
        _, err = await api(
            "post",
            f"/services/saved/searches/{encoded}",
            f"update saved search {name}",
            data=body,
        )
        return err or f"Saved search '{name}' updated."

    # ══════════════════════════════════════════════════════════════════════
    # 3. DATA CONTEXT — indexes, fired alerts
    # ══════════════════════════════════════════════════════════════════════

    async def list_splunk_indexes(limit: int = 50) -> str:
        """List Splunk indexes with event counts and sizes (data coverage)."""
        data, err = await api(
            "get",
            "/services/data/indexes",
            "list indexes",
            params={"count": safe_int(limit, 50, lo=1, hi=MAX_PAGE), "datatype": "all"},
        )
        if err:
            return err

        def _fmt(e: dict) -> str:
            c = content(e)
            return (
                f"  {e.get('name', '?')} — events: {c.get('totalEventCount', 0)} | "
                f"size: {c.get('currentDBSizeMB', 0)} MB | disabled: {c.get('disabled', '?')}\n"
                f"    earliest: {dt(c.get('minTime'))} | latest: {dt(c.get('maxTime'))}"
            )

        return format_entity_list(entries(data), _fmt, noun="index")

    async def list_splunk_fired_alerts(limit: int = 25) -> str:
        """List recently fired alerts (triggered scheduled searches / core SIEM alerts)."""
        data, err = await api(
            "get",
            "/services/alerts/fired_alerts",
            "list fired alerts",
            params={"count": safe_int(limit, 25, lo=1, hi=MAX_PAGE)},
        )
        if err:
            return err
        # The ``-`` entry is a synthetic "all alerts" aggregate; drop it.
        groups = [e for e in entries(data) if e.get("name") != "-"]

        def _fmt(e: dict) -> str:
            c = content(e)
            return (
                f"  {e.get('name', '?')} — triggered: {c.get('triggered_alert_count', 0)}"
                + (f" | severity: {c.get('severity')}" if c.get("severity") else "")
            )

        return format_entity_list(groups, _fmt, noun="fired alert")

    # ── register ────────────────────────────────────────────────────────
    executor.register("get_splunk_status", get_splunk_status)
    executor.register("run_splunk_search", run_splunk_search)
    executor.register("get_splunk_search_results", get_splunk_search_results)
    executor.register("list_splunk_saved_searches", list_splunk_saved_searches)
    executor.register("get_splunk_saved_search", get_splunk_saved_search)
    executor.register("run_splunk_saved_search", run_splunk_saved_search)
    executor.register("manage_splunk_saved_search", manage_splunk_saved_search)
    executor.register("list_splunk_indexes", list_splunk_indexes)
    executor.register("list_splunk_fired_alerts", list_splunk_fired_alerts)

    return [
        ToolDefinition(
            name="get_splunk_status",
            description=(
                "Validate the Splunk connection and report the version and whether "
                "Enterprise Security is installed. Use first if other Splunk tools fail."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="run_splunk_search",
            description=(
                "Run an SPL (Search Processing Language) search against Splunk for "
                "threat hunting and wait for the results (asynchronous Search Job API: "
                "create -> poll -> fetch). A bare search like 'index=main error' is "
                "auto-prefixed with 'search '; pipelines ('| tstats ...'), macros "
                "('`notable`'), and 'search ...' are used as-is. Set the lookback with "
                "earliest_time/latest_time (Splunk relative time, e.g. '-24h', '-7d@d', "
                "'now'). Aggregations (e.g. '... | stats count by user') return a table."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "SPL search string"},
                    "earliest_time": {
                        "type": "string",
                        "default": "-24h",
                        "description": "Splunk relative/absolute start time",
                    },
                    "latest_time": {
                        "type": "string",
                        "default": "now",
                        "description": "Splunk relative/absolute end time",
                    },
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["query"],
            },
            source=SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_splunk_search_results",
            description=(
                "Fetch results for an existing Splunk search job by its sid (use when "
                "run_splunk_search reported the job was still running)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "sid": {"type": "string", "description": "Search job id"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["sid"],
            },
            source=SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_splunk_saved_searches",
            description=(
                "List saved searches. These include Enterprise Security correlation "
                "searches (detection rules) — set correlation_only=true to list only "
                "those. Optionally filter by a name substring."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name_filter": {"type": "string", "description": "Title substring filter"},
                    "correlation_only": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_splunk_saved_search",
            description=(
                "Get full detail of a saved search by name: the SPL, schedule, dispatch "
                "time range, alert actions, and correlation-search metadata."
            ),
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            source=SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="run_splunk_saved_search",
            description=(
                "Dispatch (run) a saved or correlation search and return its results. "
                "Set trigger_actions=true to also fire the search's alert actions (e.g. "
                "generate notable events); default false runs it read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "trigger_actions": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["name"],
            },
            source=SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_splunk_saved_search",
            description=(
                "Create, update, enable, disable, or delete a saved search "
                "(consolidated long-tail management). action='create' needs name + "
                "search (SPL). 'enable'/'disable' toggle the search. 'delete' is "
                "permanent and cannot be undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(_SAVED_SEARCH_ACTIONS)},
                    "name": {"type": "string"},
                    "search": {"type": "string", "description": "SPL (create/update)"},
                    "cron_schedule": {"type": "string", "description": "e.g. '*/10 * * * *'"},
                    "is_scheduled": {"type": "boolean"},
                    "earliest_time": {"type": "string"},
                    "latest_time": {"type": "string"},
                },
                "required": ["action", "name"],
            },
            source=SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_splunk_indexes",
            description=(
                "List Splunk indexes with event counts, sizes, and time coverage — use "
                "to discover where data lives before hunting."
            ),
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 50}},
                "required": [],
            },
            source=SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_splunk_fired_alerts",
            description=(
                "List recently fired alerts (triggered scheduled searches) — the core "
                "Splunk alerting surface for deployments without Enterprise Security."
            ),
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 25}},
                "required": [],
            },
            source=SOURCE,
            priority=2,
        ),
    ]
