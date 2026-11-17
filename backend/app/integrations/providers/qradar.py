# SPDX-License-Identifier: Apache-2.0

"""IBM QRadar integration – Agentic SOC SIEM for the SecOps team.

Full-fledged IBM QRadar SIEM integration covering the detection → investigation
→ hunting → response → intelligence loop, designed as a best-in-class building
block for an agentic security team:

  - Offenses (*gathering incidents*): list (filterable), get, update (status /
    close / assign / follow-up / protect), notes (list/add), closing reasons,
    and source/destination address enrichment (IOC extraction)
  - Ariel / AQL (*threat hunting*): run an AQL query (async search + poll +
    results), fetch results for an existing search, list saved searches
  - Reference data (*creating intelligence*): list/get reference sets, add and
    delete values (IOC management), create a reference set
  - Context: assets, rules (CRE/ADE/building blocks), and log sources
  - Connectivity / system info

Authentication
--------------
QRadar authenticates with an **authorized service token** sent in the ``SEC`` HTTP
header, plus a ``Version`` header pinning the API version (recommended by IBM so
upgrades don't break clients). ``integration_tools._get_token`` assembles the
compound ``base_url||token||api_version`` token this builder parses (base URL =
the QRadar console host, e.g. ``https://qradar.example.com``). All calls target
``{base_url}/api``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.core.egress import egress_client
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.services.branding import get_cached_branding
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "IBM QRadar"
_SOURCE = "integration:qradar"

# Default QRadar API version. IBM recommends always sending a Version header;
# requesting a version newer than the console supports safely falls back to the
# latest available version of each endpoint, so a recent default is safe.
_DEFAULT_API_VERSION = "20.0"

# AQL search polling budget (start search -> poll status -> fetch results).
_SEARCH_POLL_INTERVAL = 3.0
_SEARCH_MAX_POLLS = 10

# Upper bound on the number of items requested in a single paginated call via the
# QRadar ``Range`` header. Caps caller-supplied limits so an unbounded value can't
# request an enormous response or stress the console.
_MAX_RANGE = 1000


def _mag_icon(value: Any) -> str:
    try:
        m = int(value)
    except (TypeError, ValueError):
        return "⚪"
    if m >= 8:
        return "🔴"
    if m >= 6:
        return "🟠"
    if m >= 4:
        return "🟡"
    return "🔵"


def _ts(value: Any) -> str:
    """Render an epoch-milliseconds timestamp as a UTC ISO second-precision string.

    Formats directly to ``YYYY-MM-DDTHH:MM:SSZ``; the value is always UTC, so the
    trailing ``Z`` is retained (formatting via ``.isoformat().replace(...)[:19]``
    truncated it off, dropping the timezone indicator and producing ambiguous
    timestamps).
    """
    if value in (None, "", 0):
        return ""
    try:
        return datetime.fromtimestamp(int(value) / 1000, UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (TypeError, ValueError, OSError):
        return str(value)


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text)
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _cell(value: Any, max_len: int = 80) -> str:
    """Render a query-result cell value safe for a pipe-delimited table.

    Strips embedded newlines/tabs (common in AQL string fields like payloads and
    command lines) that would otherwise break the single-row-per-record layout.
    """
    if value is None:
        return "null"
    text = str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return text[:max_len]


# ---------------------------------------------------------------------------
# Auth + URL helpers (shared with integration_testing.py)
# ---------------------------------------------------------------------------


def qradar_api_base(base_url: str) -> str:
    """Return the QRadar REST API base URL (``{base_url}/api``)."""
    return f"{base_url.rstrip('/')}/api"


def build_qradar_headers(token: str, api_version: str = "") -> dict[str, str]:
    """Build QRadar request headers (SEC token + pinned API Version)."""
    return {
        "SEC": token,
        "Version": api_version or _DEFAULT_API_VERSION,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build IBM QRadar tools from a ``base_url||token||api_version`` compound token."""
    base_url, sec_token, api_version = parse_compound_token(token, 3)
    if not base_url or not sec_token:
        return []

    api = qradar_api_base(base_url)
    headers = build_qradar_headers(sec_token, api_version)

    def _range(limit: int, offset: int = 0) -> dict[str, str]:
        """QRadar paginates via the Range header (items=start-end, inclusive).

        The caller-supplied limit/offset are clamped: the tools expose ``limit``
        without an upper bound, and an excessively large value would request a
        huge response (memory/latency) and stress the QRadar console. Clamp the
        window to ``_MAX_RANGE`` items and floor the offset at 0.
        """
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 1
        try:
            offset = int(offset)
        except (TypeError, ValueError):
            offset = 0
        limit = max(1, min(limit, _MAX_RANGE))
        offset = max(0, offset)
        return {**headers, "Range": f"items={offset}-{offset + limit - 1}"}

    # ══════════════════════════════════════════════════════════════════════
    # 0. CONNECTION
    # ══════════════════════════════════════════════════════════════════════

    async def get_qradar_status() -> str:
        """Validate the QRadar connection and report console version info."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/system/about",
                _PROVIDER,
                "get system info",
                headers=headers,
            )
            if err:
                return err
            data = resp.json() or {}
            return (
                f"Connected to IBM QRadar at {base_url}.\n"
                f"  Version: {data.get('external_version', data.get('release_version', '?'))} | "
                f"Build: {data.get('build_version', '?')}"
            )

    # ══════════════════════════════════════════════════════════════════════
    # 1. OFFENSES (gathering incidents)
    # ══════════════════════════════════════════════════════════════════════

    async def list_qradar_offenses(
        status: str = "",
        min_magnitude: int = 0,
        assigned_to: str = "",
        offense_filter: str = "",
        limit: int = 25,
    ) -> str:
        """List QRadar offenses. Filter by status (OPEN/HIDDEN/CLOSED), magnitude, assignee, or a raw QRadar filter."""
        clauses: list[str] = []
        if offense_filter:
            clauses.append(offense_filter)
        else:
            if status:
                clauses.append(f'status="{status.upper()}"')
            if min_magnitude > 0:
                clauses.append(f"magnitude>={min_magnitude}")
            if assigned_to:
                clauses.append(f'assigned_to="{assigned_to}"')
        params: dict[str, Any] = {"sort": "-magnitude"}
        if clauses:
            params["filter"] = " and ".join(clauses)

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/siem/offenses",
                _PROVIDER,
                "list offenses",
                headers=_range(limit),
                params=params,
            )
            if err:
                return err
            offenses = resp.json() or []

            def _fmt(o: dict) -> str:
                return (
                    f"  {_mag_icon(o.get('magnitude'))} offense #{o.get('id', '?')} "
                    f"[mag {o.get('magnitude', '?')}/sev {o.get('severity', '?')}] "
                    f"{o.get('status', '?')} — {_trunc(o.get('description'), 90)}\n"
                    f"    Source: {o.get('offense_source', '?')} | "
                    f"Events: {o.get('event_count', 0)} | Flows: {o.get('flow_count', 0)} | "
                    f"Assigned: {o.get('assigned_to') or 'Unassigned'}\n"
                    f"    Start: {_ts(o.get('start_time'))} | "
                    f"Last updated: {_ts(o.get('last_updated_time'))}"
                )

            return format_entity_list(offenses, _fmt, noun="offense")

    async def get_qradar_offense(offense_id: int) -> str:
        """Get full details of a QRadar offense by id."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/siem/offenses/{offense_id}",
                _PROVIDER,
                f"get offense {offense_id}",
                headers=headers,
            )
            if err:
                return err
            o = resp.json() or {}
            rules = ", ".join(str(r.get("id")) for r in (o.get("rules") or [])[:10])
            sources = ", ".join(
                ls.get("name", "?") for ls in (o.get("log_sources") or [])[:8]
            )
            lines = [
                f"Offense #{o.get('id', offense_id)}: {_trunc(o.get('description'), 200)}",
                f"Status: {o.get('status', '?')} | "
                f"Magnitude: {_mag_icon(o.get('magnitude'))} {o.get('magnitude', '?')} | "
                f"Severity: {o.get('severity', '?')} | Credibility: {o.get('credibility', '?')} | "
                f"Relevance: {o.get('relevance', '?')}",
                f"Offense source: {o.get('offense_source', '?')} "
                f"(type id {o.get('offense_type', '?')})",
                f"Assigned to: {o.get('assigned_to') or 'Unassigned'} | "
                f"Follow-up: {o.get('follow_up', False)} | Protected: {o.get('protected', False)}",
                f"Events: {o.get('event_count', 0)} | Flows: {o.get('flow_count', 0)} | "
                f"Sources: {o.get('source_count', 0)} | "
                f"Local dest: {o.get('local_destination_count', 0)}",
                f"Start: {_ts(o.get('start_time'))} | "
                f"Last updated: {_ts(o.get('last_updated_time'))} | "
                f"Close: {_ts(o.get('close_time'))}",
                f"Domain id: {o.get('domain_id', '?')} | "
                f"Closing reason id: {o.get('closing_reason_id') or 'N/A'}",
            ]
            if rules:
                lines.append(f"Contributing rule ids: {rules}")
            if sources:
                lines.append(f"Log sources: {sources}")
            categories = o.get("categories") or []
            if categories:
                lines.append(f"Categories: {', '.join(categories[:12])}")
            return "\n".join(lines)

    async def update_qradar_offense(
        offense_id: int,
        status: str = "",
        closing_reason_id: int = 0,
        assigned_to: str = "",
        follow_up: bool | None = None,
        protected: bool | None = None,
    ) -> str:
        """Update a QRadar offense: status (OPEN/HIDDEN/CLOSED), close reason, assignee, follow-up, protect.

        Closing an offense (status=CLOSED) requires a valid closing_reason_id
        (see list_qradar_closing_reasons).
        """
        params: dict[str, Any] = {}
        if status:
            params["status"] = status.upper()
        if closing_reason_id:
            params["closing_reason_id"] = closing_reason_id
        if assigned_to:
            params["assigned_to"] = assigned_to
        if follow_up is not None:
            params["follow_up"] = str(follow_up).lower()
        if protected is not None:
            params["protected"] = str(protected).lower()
        if not params:
            return "Error: No fields provided to update."
        if params.get("status") == "CLOSED" and "closing_reason_id" not in params:
            return (
                "Error: closing an offense requires closing_reason_id. "
                "Call list_qradar_closing_reasons to find a valid id."
            )

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/siem/offenses/{offense_id}",
                _PROVIDER,
                f"update offense {offense_id}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            o = resp.json() or {}
            return (
                f"Offense {offense_id} updated — status: {o.get('status', '?')}, "
                f"assigned: {o.get('assigned_to') or 'Unassigned'}, "
                f"follow_up: {o.get('follow_up', False)}, protected: {o.get('protected', False)}"
            )

    async def list_qradar_offense_notes(offense_id: int) -> str:
        """List investigation notes on a QRadar offense."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/siem/offenses/{offense_id}/notes",
                _PROVIDER,
                f"list notes for offense {offense_id}",
                headers=_range(100),
            )
            if err:
                return err
            notes = resp.json() or []
            if not notes:
                return f"No notes on offense {offense_id}."
            lines = [f"Notes on offense {offense_id} ({len(notes)}):"]
            for n in notes:
                lines.append(
                    f"\n  [{_ts(n.get('create_time'))}] {n.get('username', '?')}:\n"
                    f"  {_trunc(n.get('note_text'), 400)}"
                )
            return "\n".join(lines)

    async def add_qradar_offense_note(offense_id: int, note_text: str) -> str:
        """Add an investigation note to a QRadar offense."""
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "post",
                f"{api}/siem/offenses/{offense_id}/notes",
                _PROVIDER,
                f"add note to offense {offense_id}",
                headers=headers,
                params={"note_text": note_text},
            )
            if err:
                return err
            return f"Note added to offense {offense_id}."

    async def list_qradar_closing_reasons() -> str:
        """List QRadar offense closing reasons (needed to close an offense)."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/siem/offense_closing_reasons",
                _PROVIDER,
                "list closing reasons",
                headers=headers,
            )
            if err:
                return err
            reasons = resp.json() or []

            def _fmt(r: dict) -> str:
                return f"  #{r.get('id', '?')} {r.get('text', 'Unnamed')}"

            return format_entity_list(reasons, _fmt, noun="closing reason")

    async def get_qradar_offense_addresses(offense_id: int) -> str:
        """Resolve a QRadar offense's source and local destination IP addresses (IOC extraction)."""
        async with egress_client(timeout=30) as client:
            off_resp, off_err = await safe_request(
                client,
                "get",
                f"{api}/siem/offenses/{offense_id}",
                _PROVIDER,
                f"get offense {offense_id} for addresses",
                headers=headers,
                params={"fields": "source_address_ids,local_destination_address_ids"},
            )
            if off_err:
                return off_err
            o = off_resp.json() or {}
            src_ids = o.get("source_address_ids") or []
            dst_ids = o.get("local_destination_address_ids") or []

            async def _resolve(kind: str, ids: list[int], ip_field: str) -> list[str]:
                if not ids:
                    return []
                id_list = ",".join(str(i) for i in ids[:50])
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{api}/siem/{kind}",
                    _PROVIDER,
                    f"resolve {kind}",
                    headers=headers,
                    params={"filter": f"id in ({id_list})"},
                )
                if err or not resp:
                    return []
                return [a.get(ip_field, "?") for a in (resp.json() or [])]

            sources = await _resolve(
                "source_addresses", src_ids, "source_ip"
            )
            dests = await _resolve(
                "local_destination_addresses", dst_ids, "local_destination_ip"
            )

        lines = [f"Addresses for offense {offense_id}:"]
        lines.append(f"  Source IPs ({len(sources)}): {', '.join(sources) or 'None'}")
        lines.append(
            f"  Local destination IPs ({len(dests)}): {', '.join(dests) or 'None'}"
        )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 2. ARIEL / AQL (threat hunting)
    # ══════════════════════════════════════════════════════════════════════

    def _format_search_results(payload: dict, *, max_rows: int = 50) -> str:
        # The results object has a single key named after the queried database
        # (events, flows, …) whose value is the list of rows.
        rows: list[dict] = []
        for value in (payload or {}).values():
            if isinstance(value, list):
                rows = value
                break
        if not rows:
            return "Query completed: 0 rows."
        columns = list(rows[0].keys())
        lines = [f"AQL results ({len(rows)} rows):", " | ".join(columns)]
        lines.append("-" * min(len(" | ".join(columns)), 120))
        for row in rows[:max_rows]:
            lines.append(" | ".join(_cell(row.get(c)) for c in columns))
        if len(rows) > max_rows:
            lines.append(f"\n... [{len(rows) - max_rows} more rows not shown]")
        return "\n".join(lines)

    async def _fetch_results(
        client: httpx.AsyncClient, search_id: str, limit: int
    ) -> tuple[dict | None, str | None]:
        resp, err = await safe_request(
            client,
            "get",
            f"{api}/ariel/searches/{search_id}/results",
            _PROVIDER,
            f"get search results {search_id}",
            headers=_range(limit),
        )
        if err:
            return None, err
        return resp.json() or {}, None

    async def run_qradar_aql_query(query: str, limit: int = 50) -> str:
        """Run an AQL query against QRadar (threat hunting) and wait for results.

        Example: `SELECT sourceip, destinationip, username FROM events WHERE
        magnitude >= 5 LAST 24 HOURS`. AQL must be a SELECT; specify the time
        window in the query (e.g. `LAST 24 HOURS` / `START ... STOP ...`).
        """
        async with egress_client(timeout=60) as client:
            start_resp, start_err = await safe_request(
                client,
                "post",
                f"{api}/ariel/searches",
                _PROVIDER,
                "start AQL search",
                headers=headers,
                params={"query_expression": query},
            )
            if start_err:
                return start_err
            search = start_resp.json() or {}
            search_id = search.get("search_id")
            if not search_id:
                return "Error: QRadar did not return a search id."

            status = search.get("status", "")
            for _ in range(_SEARCH_MAX_POLLS):
                if status in ("COMPLETED", "ERROR", "CANCELED"):
                    break
                await asyncio.sleep(_SEARCH_POLL_INTERVAL)
                poll_resp, poll_err = await safe_request(
                    client,
                    "get",
                    f"{api}/ariel/searches/{search_id}",
                    _PROVIDER,
                    f"poll search {search_id}",
                    headers=headers,
                )
                if poll_err:
                    return poll_err
                status = (poll_resp.json() or {}).get("status", "")

            if status == "ERROR":
                return f"AQL search {search_id} failed (status ERROR)."
            if status != "COMPLETED":
                return (
                    f"AQL search {search_id} is still running (status {status}). "
                    f"Use get_qradar_search_results with this id shortly."
                )

            results, res_err = await _fetch_results(client, search_id, limit)
            if res_err:
                return res_err
            return f"AQL search {search_id}:\n{_format_search_results(results, max_rows=limit)}"

    async def get_qradar_search_results(search_id: str, limit: int = 50) -> str:
        """Fetch results for an existing QRadar Ariel search by its search id."""
        async with egress_client(timeout=60) as client:
            status_resp, status_err = await safe_request(
                client,
                "get",
                f"{api}/ariel/searches/{search_id}",
                _PROVIDER,
                f"get search {search_id}",
                headers=headers,
            )
            if status_err:
                return status_err
            status = (status_resp.json() or {}).get("status", "")
            if status != "COMPLETED":
                return f"Search {search_id} status: {status} (not yet complete)."
            results, res_err = await _fetch_results(client, search_id, limit)
            if res_err:
                return res_err
            return _format_search_results(results, max_rows=limit)

    async def list_qradar_saved_searches(limit: int = 30) -> str:
        """List QRadar Ariel saved searches (reusable AQL queries)."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/ariel/saved_searches",
                _PROVIDER,
                "list saved searches",
                headers=_range(limit),
            )
            if err:
                return err
            searches = resp.json() or []

            def _fmt(s: dict) -> str:
                return (
                    f"  #{s.get('id', '?')} {s.get('name', 'Unnamed')} "
                    f"({s.get('database', '?')})\n"
                    f"    {_trunc(s.get('aql'), 140) or 'N/A'}"
                )

            return format_entity_list(searches, _fmt, noun="saved search")

    # ══════════════════════════════════════════════════════════════════════
    # 3. REFERENCE DATA (creating intelligence / IOC management)
    # ══════════════════════════════════════════════════════════════════════

    async def list_qradar_reference_sets(limit: int = 50) -> str:
        """List QRadar reference sets (named IOC/value collections used by rules)."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/reference_data/sets",
                _PROVIDER,
                "list reference sets",
                headers=_range(limit),
            )
            if err:
                return err
            sets = resp.json() or []

            def _fmt(s: dict) -> str:
                return (
                    f"  {s.get('name', '?')} [{s.get('element_type', '?')}] — "
                    f"{s.get('number_of_elements', 0)} elements "
                    f"(namespace: {s.get('namespace', '?')})"
                )

            return format_entity_list(sets, _fmt, noun="reference set")

    async def get_qradar_reference_set(name: str, limit: int = 100) -> str:
        """Get a QRadar reference set and a sample of its values."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/reference_data/sets/{quote(name, safe='')}",
                _PROVIDER,
                f"get reference set {name}",
                headers=headers,
            )
            if err:
                return err
            s = resp.json() or {}
            data = s.get("data") or []
            lines = [
                f"Reference set '{s.get('name', name)}' "
                f"[{s.get('element_type', '?')}]",
                f"Elements: {s.get('number_of_elements', len(data))} | "
                f"Namespace: {s.get('namespace', '?')} | TTL: {s.get('time_to_live') or 'none'}",
            ]
            if data:
                values = [str(d.get("value", d)) for d in data[:limit]]
                lines.append(f"\nValues ({len(values)} shown):")
                lines.append(", ".join(values))
            return "\n".join(lines)

    async def add_qradar_reference_set_value(name: str, value: str) -> str:
        """Add a value (e.g. an IOC) to a QRadar reference set."""
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "post",
                f"{api}/reference_data/sets/{quote(name, safe='')}",
                _PROVIDER,
                f"add value to reference set {name}",
                headers=headers,
                params={"value": value, "source": get_cached_branding().name},
            )
            if err:
                return err
            return f"Value '{value}' added to reference set '{name}'."

    async def delete_qradar_reference_set_value(name: str, value: str) -> str:
        """Remove a value from a QRadar reference set."""
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "delete",
                f"{api}/reference_data/sets/{quote(name, safe='')}/{quote(value, safe='')}",
                _PROVIDER,
                f"delete value from reference set {name}",
                headers=headers,
            )
            if err:
                return err
            return f"Value '{value}' removed from reference set '{name}'."

    async def create_qradar_reference_set(
        name: str, element_type: str = "ALNIC"
    ) -> str:
        """Create a new QRadar reference set.

        element_type: ALN (alphanumeric), ALNIC (alphanumeric, case-insensitive),
        NUM (numeric), IP, PORT, or DATE.
        """
        valid = {"ALN", "ALNIC", "NUM", "IP", "PORT", "DATE"}
        if element_type.upper() not in valid:
            return f"Error: element_type must be one of {', '.join(sorted(valid))}."
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "post",
                f"{api}/reference_data/sets",
                _PROVIDER,
                f"create reference set {name}",
                headers=headers,
                params={"name": name, "element_type": element_type.upper()},
            )
            if err:
                return err
            return f"Reference set '{name}' created (type {element_type.upper()})."

    # ══════════════════════════════════════════════════════════════════════
    # 4. CONTEXT — assets, rules, log sources
    # ══════════════════════════════════════════════════════════════════════

    async def list_qradar_assets(asset_filter: str = "", limit: int = 25) -> str:
        """List QRadar assets (asset model). Optionally pass a raw QRadar filter."""
        params: dict[str, Any] = {}
        if asset_filter:
            params["filter"] = asset_filter
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/asset_model/assets",
                _PROVIDER,
                "list assets",
                headers=_range(limit),
                params=params or None,
            )
            if err:
                return err
            assets = resp.json() or []

            def _fmt(a: dict) -> str:
                interfaces = a.get("interfaces") or []
                ips: list[str] = []
                for iface in interfaces:
                    for ip in iface.get("ip_addresses") or []:
                        if ip.get("value"):
                            ips.append(ip["value"])
                props = {
                    p.get("name"): p.get("value")
                    for p in (a.get("properties") or [])
                }
                hostname = props.get("Unified Name") or props.get("Name") or "?"
                return (
                    f"  asset #{a.get('id', '?')} {hostname} — "
                    f"IPs: {', '.join(ips[:5]) or 'N/A'} | "
                    f"Domain: {a.get('domain_id', '?')}"
                )

            return format_entity_list(assets, _fmt, noun="asset")

    async def list_qradar_rules(
        enabled_only: bool = False, rule_filter: str = "", limit: int = 30
    ) -> str:
        """List QRadar detection rules (CRE/ADE/building blocks)."""
        clauses: list[str] = []
        if rule_filter:
            clauses.append(rule_filter)
        elif enabled_only:
            clauses.append("enabled=true")
        params: dict[str, Any] = {}
        if clauses:
            params["filter"] = " and ".join(clauses)
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/analytics/rules",
                _PROVIDER,
                "list rules",
                headers=_range(limit),
                params=params or None,
            )
            if err:
                return err
            rules = resp.json() or []

            def _fmt(r: dict) -> str:
                state = "✓ enabled" if r.get("enabled") else "✗ disabled"
                return (
                    f"  #{r.get('id', '?')} {r.get('name', 'Unnamed')} "
                    f"[{r.get('type', '?')}] — {state} | "
                    f"Origin: {r.get('origin', '?')}"
                )

            return format_entity_list(rules, _fmt, noun="rule")

    async def list_qradar_log_sources(limit: int = 30) -> str:
        """List QRadar log sources (data inputs)."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/config/event_sources/log_source_management/log_sources",
                _PROVIDER,
                "list log sources",
                headers=_range(limit),
            )
            if err:
                return err
            sources = resp.json() or []

            def _fmt(s: dict) -> str:
                status = (s.get("status") or {}).get("status", "?")
                return (
                    f"  #{s.get('id', '?')} {s.get('name', 'Unnamed')} "
                    f"(type {s.get('type_id', '?')}) — "
                    f"enabled: {s.get('enabled', '?')} | status: {status} | "
                    f"last event: {_ts(s.get('last_event_time'))}"
                )

            return format_entity_list(sources, _fmt, noun="log source")

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("get_qradar_status", get_qradar_status)
    executor.register("list_qradar_offenses", list_qradar_offenses)
    executor.register("get_qradar_offense", get_qradar_offense)
    executor.register("update_qradar_offense", update_qradar_offense)
    executor.register("list_qradar_offense_notes", list_qradar_offense_notes)
    executor.register("add_qradar_offense_note", add_qradar_offense_note)
    executor.register("list_qradar_closing_reasons", list_qradar_closing_reasons)
    executor.register("get_qradar_offense_addresses", get_qradar_offense_addresses)
    executor.register("run_qradar_aql_query", run_qradar_aql_query)
    executor.register("get_qradar_search_results", get_qradar_search_results)
    executor.register("list_qradar_saved_searches", list_qradar_saved_searches)
    executor.register("list_qradar_reference_sets", list_qradar_reference_sets)
    executor.register("get_qradar_reference_set", get_qradar_reference_set)
    executor.register("add_qradar_reference_set_value", add_qradar_reference_set_value)
    executor.register(
        "delete_qradar_reference_set_value", delete_qradar_reference_set_value
    )
    executor.register("create_qradar_reference_set", create_qradar_reference_set)
    executor.register("list_qradar_assets", list_qradar_assets)
    executor.register("list_qradar_rules", list_qradar_rules)
    executor.register("list_qradar_log_sources", list_qradar_log_sources)

    return [
        # ── 0. Connection ──────────────────────────────────────────────────
        ToolDefinition(
            name="get_qradar_status",
            description=(
                "Validate the IBM QRadar connection and report console version. "
                "Use first if other QRadar tools fail."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        # ── 1. Offenses ────────────────────────────────────────────────────
        ToolDefinition(
            name="list_qradar_offenses",
            description=(
                "List QRadar offenses (gathering incidents). Filter by status "
                "(OPEN/HIDDEN/CLOSED), min_magnitude (1-10), assignee, or a raw "
                "QRadar filter expression (e.g. 'categories contains \"Malware\"'). "
                "Sorted by magnitude descending."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["OPEN", "HIDDEN", "CLOSED"],
                    },
                    "min_magnitude": {
                        "type": "integer",
                        "description": "Minimum magnitude (1-10)",
                        "default": 0,
                    },
                    "assigned_to": {"type": "string"},
                    "offense_filter": {
                        "type": "string",
                        "description": "Raw QRadar filter (overrides the other filters)",
                    },
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_qradar_offense",
            description="Get full details of a QRadar offense by id (magnitude, status, contributing rules, log sources, categories, timeline).",
            input_schema={
                "type": "object",
                "properties": {"offense_id": {"type": "integer"}},
                "required": ["offense_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="update_qradar_offense",
            description=(
                "Update a QRadar offense: status (OPEN/HIDDEN/CLOSED), "
                "closing_reason_id (required to CLOSE — see list_qradar_closing_reasons), "
                "assigned_to, follow_up, or protected."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "offense_id": {"type": "integer"},
                    "status": {
                        "type": "string",
                        "enum": ["OPEN", "HIDDEN", "CLOSED"],
                    },
                    "closing_reason_id": {"type": "integer"},
                    "assigned_to": {"type": "string"},
                    "follow_up": {"type": "boolean"},
                    "protected": {"type": "boolean"},
                },
                "required": ["offense_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_qradar_offense_notes",
            description="List investigation notes on a QRadar offense.",
            input_schema={
                "type": "object",
                "properties": {"offense_id": {"type": "integer"}},
                "required": ["offense_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="add_qradar_offense_note",
            description="Add an investigation note to a QRadar offense for the audit trail.",
            input_schema={
                "type": "object",
                "properties": {
                    "offense_id": {"type": "integer"},
                    "note_text": {"type": "string"},
                },
                "required": ["offense_id", "note_text"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_qradar_closing_reasons",
            description="List QRadar offense closing reasons and their ids (needed to close an offense).",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="get_qradar_offense_addresses",
            description="Resolve a QRadar offense's source and local destination IP addresses for IOC extraction and enrichment.",
            input_schema={
                "type": "object",
                "properties": {"offense_id": {"type": "integer"}},
                "required": ["offense_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 2. Ariel / AQL (threat hunting) ────────────────────────────────
        ToolDefinition(
            name="run_qradar_aql_query",
            description=(
                "Run an AQL (Ariel Query Language) query against QRadar for threat "
                "hunting and wait for results. Must be a SELECT; put the time window "
                "in the query. Example: 'SELECT sourceip, destinationip, username, "
                "\"Magnitude\" FROM events WHERE magnitude >= 5 LAST 24 HOURS'. "
                "Common tables: events, flows."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "AQL SELECT query"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_qradar_search_results",
            description="Fetch results for an existing QRadar Ariel search by id (use when run_qradar_aql_query was still running).",
            input_schema={
                "type": "object",
                "properties": {
                    "search_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["search_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_qradar_saved_searches",
            description="List QRadar Ariel saved searches (reusable AQL queries with their query text).",
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 30}},
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
        # ── 3. Reference data (creating intelligence) ──────────────────────
        ToolDefinition(
            name="list_qradar_reference_sets",
            description="List QRadar reference sets (named IOC/value collections that rules and AQL reference).",
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 50}},
                "required": [],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_qradar_reference_set",
            description="Get a QRadar reference set by name with a sample of its values.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "limit": {"type": "integer", "default": 100},
                },
                "required": ["name"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="add_qradar_reference_set_value",
            description=(
                "Add a value (e.g. a malicious IP/domain/hash IOC) to a QRadar "
                "reference set so rules acting on that set take effect (CREATING "
                "INTELLIGENCE)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Reference set name"},
                    "value": {"type": "string", "description": "Value/IOC to add"},
                },
                "required": ["name", "value"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="delete_qradar_reference_set_value",
            description="Remove a value from a QRadar reference set.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["name", "value"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="create_qradar_reference_set",
            description="Create a new QRadar reference set. element_type: ALN, ALNIC (case-insensitive), NUM, IP, PORT, or DATE.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "element_type": {
                        "type": "string",
                        "enum": ["ALN", "ALNIC", "NUM", "IP", "PORT", "DATE"],
                        "default": "ALNIC",
                    },
                },
                "required": ["name"],
            },
            source=_SOURCE,
            priority=2,
        ),
        # ── 4. Context ─────────────────────────────────────────────────────
        ToolDefinition(
            name="list_qradar_assets",
            description="List QRadar assets (asset model) with their IPs. Optionally pass a raw QRadar filter.",
            input_schema={
                "type": "object",
                "properties": {
                    "asset_filter": {"type": "string"},
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_qradar_rules",
            description="List QRadar detection rules (CRE/ADE/building blocks). Filter to enabled only or pass a raw QRadar filter.",
            input_schema={
                "type": "object",
                "properties": {
                    "enabled_only": {"type": "boolean", "default": False},
                    "rule_filter": {"type": "string"},
                    "limit": {"type": "integer", "default": 30},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_qradar_log_sources",
            description="List QRadar log sources (data inputs) with status and last event time.",
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 30}},
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
