# SPDX-License-Identifier: Apache-2.0

"""Sumo Logic integration - Agentic log analytics + Cloud SIEM for the SecOps team.

Full-fledged Sumo Logic integration covering the detection -> investigation ->
hunting -> response -> intelligence loop, designed as a best-in-class building
block for an agentic security team:

  - Search Job API (*log hunting*): run an asynchronous search (create job ->
    poll until DONE GATHERING RESULTS -> page through messages or aggregation
    records), mirroring QRadar's bounded-poll pattern. The create -> poll ->
    fetch -> delete flow reuses a single HTTP client so the session-affinity
    cookie keeps the job alive.
  - Cloud SIEM (CSE) insights (*triage / response*): list (filterable), get,
    set status (close / progress with resolution), and add comments
  - Cloud SIEM signals + entities (*investigation*): list and get
  - Threat intelligence (*creating intelligence*): list sources, and a
    consolidated tool to list or create threat-intel indicators (IOCs)
  - Monitoring + data collection context: monitors and collectors
  - Connectivity / status

Authentication
--------------
Sumo Logic authenticates with an **Access ID + Access Key pair** sent as HTTP
Basic auth (``Authorization: Basic base64(accessId:accessKey)``). There is no
OAuth. ``integration_tools._get_token`` assembles the compound
``base_url||access_id||access_key`` token this builder parses. ``base_url`` is
the region-specific API endpoint ending in ``/api`` (e.g.
``https://api.sumologic.com/api`` for US1, ``https://api.eu.sumologic.com/api``
for EU). Core endpoints live under ``{base_url}/v1`` and Cloud SIEM endpoints
under ``{base_url}/sec/v1``.

Scope
-----
The Search Job API requires an Enterprise account; the Cloud SIEM (CSE) tools
require a Cloud SIEM Enterprise subscription and a role with Cloud SIEM
capabilities. Tools degrade gracefully to a formatted error string when the
account lacks the relevant entitlement.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

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

_PROVIDER = "Sumo Logic"
_SOURCE = "integration:sumologic"

# Default Sumo Logic API endpoint (US1). The stored base_url overrides this with
# the customer's region (e.g. https://api.eu.sumologic.com/api).
_DEFAULT_BASE_URL = "https://api.sumologic.com/api"

# Search Job polling budget (create job -> poll status -> fetch results). Sumo
# cancels an idle search job if it is not polled every 20-30s, so the 3s
# interval comfortably keeps the job alive while bounding total wait time.
_SEARCH_POLL_INTERVAL = 3.0
_SEARCH_MAX_POLLS = 20
_DEFAULT_SEARCH_HOURS = 24

# Upper bound on rows fetched in a single search page (the API caps messages at
# 10,000 / records at 10,000; this keeps tool output and latency sane).
_MAX_SEARCH_RESULTS = 1000
# Upper bound on a Cloud SIEM / monitors page.
_MAX_PAGE = 100
# Upper bound on a collectors page (the Collector Management API caps at 1000).
_MAX_COLLECTOR_PAGE = 1000
_INDICATOR_DEFAULT_TTL_DAYS = 90

# Cloud SIEM insight severities -> icon.
_SEVERITY_ICONS: dict[str, str] = {
    "CRITICAL": "🔴",
    "HIGH": "🟠",
    "MEDIUM": "🟡",
    "LOW": "🔵",
    "INFO": "⚪",
}

_INSIGHT_STATUSES: frozenset[str] = frozenset({"new", "inprogress", "closed"})
_INDICATOR_ACTIONS: frozenset[str] = frozenset({"list", "create"})

# Monitor status filter values map lowercase input -> the canonical CamelCase the
# monitors search DSL expects (monitorStatus:Critical, etc.).
_MONITOR_STATUSES: dict[str, str] = {
    s.lower(): s
    for s in ("Normal", "Critical", "Warning", "MissingData", "Disabled", "AllTriggered")
}
# Collector state filter values accepted by the Collector Management API.
_COLLECTOR_FILTERS: frozenset[str] = frozenset({"installed", "hosted", "dead", "alive"})


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _sev_icon(severity: Any) -> str:
    return _SEVERITY_ICONS.get(str(severity or "").upper(), "⚪")


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text)
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _dt(value: Any) -> str:
    """Trim an ISO timestamp to second precision for display."""
    return str(value or "")[:19]


def _cell(value: Any, max_len: int = 160) -> str:
    """Render a value safe for a single-line summary (strips newlines/tabs)."""
    if value is None:
        return "null"
    text = str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return text[:max_len]


def _epoch_ms(value: Any) -> str:
    """Render an epoch-milliseconds timestamp as a UTC ISO second-precision string."""
    if value in (None, "", 0, "0"):
        return ""
    try:
        return datetime.fromtimestamp(int(value) / 1000, UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (TypeError, ValueError, OSError):
        return str(value)


def _entity_name(entity: Any) -> str:
    """Cloud SIEM entity fields are objects (``{"name": ...}``) or bare strings."""
    if isinstance(entity, dict):
        return str(entity.get("name") or entity.get("value") or entity.get("id") or "?")
    return str(entity or "?")


def _status_name(status: Any) -> str:
    """Cloud SIEM insight status is an object (``{"displayName": ...}``) or a string."""
    if isinstance(status, dict):
        return str(status.get("displayName") or status.get("name") or "?")
    return str(status or "?")


def _cse_data(resp: dict | None) -> dict:
    """Return the ``data`` envelope from a Cloud SIEM response."""
    if isinstance(resp, dict):
        d = resp.get("data")
        if isinstance(d, dict):
            return d
    return {}


def _cse_objects(resp: dict | None) -> list:
    """Return the ``data.objects`` list from a Cloud SIEM search response."""
    objects = _cse_data(resp).get("objects")
    return objects if isinstance(objects, list) else []


def _cse_has_next(resp: dict | None) -> bool:
    return bool(_cse_data(resp).get("hasNextPage"))


def _safe_json(resp: httpx.Response) -> Any:
    """Parse a response body as JSON, returning ``None`` if it is not decodable.

    The shared ``_api`` helper already guards ``.json()``; the Search Job flow
    issues ``safe_request`` calls directly, so it needs the same guard to avoid
    crashing on a 2xx response that carries a non-JSON / malformed body.
    """
    try:
        return resp.json()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Auth + URL helpers (shared with integration_testing.py)
# ---------------------------------------------------------------------------


def sumologic_api_base(base_url: str) -> str:
    """Return the Sumo Logic API base URL (the ``.../api`` root, no trailing slash).

    Defaults to the US1 endpoint and tolerates a base entered without the
    trailing ``/api`` segment (so ``https://api.eu.sumologic.com`` resolves to
    ``https://api.eu.sumologic.com/api``).
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return _DEFAULT_BASE_URL
    if not base.endswith("/api"):
        base = f"{base}/api"
    return base


def build_sumologic_headers(access_id: str, access_key: str) -> dict[str, str]:
    """Build Sumo Logic request headers (HTTP Basic with the access id/key pair)."""
    raw = f"{access_id}:{access_key}".encode()
    token = base64.b64encode(raw).decode("ascii")
    return {
        "Authorization": f"Basic {token}",
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
    """Build Sumo Logic tools from a ``base_url||access_id||access_key`` compound token."""
    base_url, access_id, access_key = parse_compound_token(token, 3, greedy_last=True)
    api = sumologic_api_base(base_url)
    headers = build_sumologic_headers(access_id, access_key)

    def _creds_error() -> str | None:
        """Validate credentials at call time (tools are always built for the catalog)."""
        if not access_id or not access_key:
            return (
                "Error: Sumo Logic credentials are not configured. Set the "
                "Access ID (extra config) and the Access Key (API Key field)."
            )
        return None

    async def _api(
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        timeout: int = 30,
    ) -> tuple[Any, str | None]:
        """Authenticated one-shot request helper returning ``(json, error)``."""
        err = _creds_error()
        if err:
            return None, err
        async with egress_client(timeout=timeout) as client:
            resp, rerr = await safe_request(
                client,
                method,
                f"{api}{path}",
                _PROVIDER,
                action,
                headers=headers,
                params=params,
                json=json_body,
            )
            if rerr:
                return None, rerr
            try:
                return resp.json(), None
            except Exception:
                return {}, None

    # ══════════════════════════════════════════════════════════════════════
    # 0. CONNECTION
    # ══════════════════════════════════════════════════════════════════════

    async def get_sumologic_status() -> str:
        """Validate the Sumo Logic connection (cheap authenticated collectors probe)."""
        data, err = await _api(
            "get", "/v1/collectors", "verify connection", params={"limit": 1}
        )
        if err:
            return err
        collectors = (data or {}).get("collectors", []) if isinstance(data, dict) else []
        return (
            f"Connected to Sumo Logic at {api}.\n"
            f"  Credentials valid (collector probe returned {len(collectors)} collector(s))."
        )

    # ══════════════════════════════════════════════════════════════════════
    # 1. SEARCH JOB API (log hunting)
    # ══════════════════════════════════════════════════════════════════════

    async def _delete_search_job(client: httpx.AsyncClient, job_id: str) -> None:
        """Best-effort cancel of a finished search job (keeps the concurrent-job count low)."""
        try:
            await safe_request(
                client,
                "delete",
                f"{api}/v1/search/jobs/{job_id}",
                _PROVIDER,
                "delete search job",
                headers=headers,
            )
        except Exception:  # pragma: no cover - cleanup is best-effort
            logger.debug("Sumo Logic search job %s cleanup failed", job_id)

    def _format_search_page(payload: dict, kind: str, max_rows: int) -> str:
        rows = (payload or {}).get(kind) or []
        if not rows:
            return f"Search completed with 0 {kind}."
        shown = rows[:max_rows]
        lines = [f"Search returned {len(rows)} {kind} (showing {len(shown)}):"]
        for row in shown:
            m = row.get("map") or {}
            if kind == "messages":
                ts = _epoch_ms(m.get("_messagetime") or m.get("_messageTime"))
                category = m.get("_sourcecategory") or m.get("_sourceCategory") or "?"
                host = m.get("_sourcehost") or m.get("_sourceHost") or "?"
                lines.append(f"  [{ts}] {category} @ {host}: {_cell(m.get('_raw'))}")
            else:
                pairs = ", ".join(f"{k}={_cell(v, 60)}" for k, v in m.items())
                lines.append(f"  {pairs}")
        if len(rows) > len(shown):
            lines.append(f"\n[Showing {len(shown)} of {len(rows)}; raise limit for more]")
        return "\n".join(lines)

    async def run_sumologic_search(
        query: str,
        hours: int = _DEFAULT_SEARCH_HOURS,
        limit: int = 50,
        time_zone: str = "UTC",
    ) -> str:
        """Run a Sumo Logic log search (threat hunting) and wait for the results.

        Uses the asynchronous Search Job API: creates the job, polls until it is
        DONE GATHERING RESULTS, then returns aggregation records when the job
        produced any (aggregation queries such as ``... | count by user`` yield
        records) and raw messages otherwise. ``query`` is a Sumo Logic search
        expression (e.g. ``_sourceCategory=prod/auth error | count by user``).
        ``hours`` is the lookback window from now.
        """
        err = _creds_error()
        if err:
            return err
        if not query or not query.strip():
            return "Error: a Sumo Logic search query is required."
        hours = max(1, int(hours or _DEFAULT_SEARCH_HOURS))
        limit = max(1, min(int(limit or 50), _MAX_SEARCH_RESULTS))
        now = datetime.now(UTC)
        body = {
            "query": query,
            "from": (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S"),
            "to": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": time_zone or "UTC",
        }
        # A single client preserves the session-affinity cookie across the
        # create -> poll -> fetch -> delete flow (a follow-up request without it
        # gets a 404 - see the Search Job API docs).
        async with egress_client(timeout=60) as client:
            start_resp, start_err = await safe_request(
                client,
                "post",
                f"{api}/v1/search/jobs",
                _PROVIDER,
                "create search job",
                headers=headers,
                json=body,
            )
            if start_err:
                return start_err
            start_json = _safe_json(start_resp)
            if not isinstance(start_json, dict):
                return (
                    "Error: Sumo Logic returned an unreadable response when "
                    "creating the search job."
                )
            job_id = start_json.get("id")
            if not job_id:
                return "Error: Sumo Logic did not return a search job id."

            state = ""
            record_count = 0
            for _ in range(_SEARCH_MAX_POLLS):
                await asyncio.sleep(_SEARCH_POLL_INTERVAL)
                poll_resp, poll_err = await safe_request(
                    client,
                    "get",
                    f"{api}/v1/search/jobs/{job_id}",
                    _PROVIDER,
                    f"poll search job {job_id}",
                    headers=headers,
                )
                if poll_err:
                    await _delete_search_job(client, job_id)
                    return poll_err
                status = _safe_json(poll_resp)
                if not isinstance(status, dict):
                    await _delete_search_job(client, job_id)
                    return (
                        "Error: Sumo Logic returned an unreadable response while "
                        f"polling search job {job_id}."
                    )
                state = status.get("state", "")
                record_count = status.get("recordCount", 0) or 0
                if state in ("DONE GATHERING RESULTS", "CANCELLED"):
                    break

            if state == "CANCELLED":
                await _delete_search_job(client, job_id)
                return f"Sumo Logic search job {job_id} was cancelled before completing."
            if state != "DONE GATHERING RESULTS":
                await _delete_search_job(client, job_id)
                return (
                    f"Sumo Logic search job {job_id} is still '{state or 'processing'}' "
                    "after the polling window. Narrow the time range or simplify the "
                    "query and retry."
                )

            # Select the result type from the job's recordCount: aggregation
            # queries populate records (recordCount > 0); raw searches leave it
            # at 0 and we page the messages instead.
            kind = "records" if record_count else "messages"
            page_resp, page_err = await safe_request(
                client,
                "get",
                f"{api}/v1/search/jobs/{job_id}/{kind}",
                _PROVIDER,
                f"fetch search {kind}",
                headers=headers,
                params={"offset": 0, "limit": limit},
            )
            await _delete_search_job(client, job_id)
            if page_err:
                return page_err
            page_json = _safe_json(page_resp)
            if not isinstance(page_json, dict):
                return (
                    "Error: Sumo Logic returned an unreadable response when "
                    f"fetching search {kind}."
                )
            return _format_search_page(page_json, kind, limit)

    # ══════════════════════════════════════════════════════════════════════
    # 2. CLOUD SIEM - INSIGHTS (triage / response)
    # ══════════════════════════════════════════════════════════════════════

    def _format_insight(i: dict) -> str:
        return (
            f"  {_sev_icon(i.get('severity'))} [{i.get('severity', '?')}] "
            f"{i.get('readableId') or i.get('id', '?')}: {_trunc(i.get('name'), 90)}\n"
            f"    id: {i.get('id', '?')} | Status: {_status_name(i.get('status'))} | "
            f"Assignee: {_entity_name(i.get('assignee')) if i.get('assignee') else 'Unassigned'} | "
            f"Entity: {_entity_name(i.get('entity'))}\n"
            f"    Created: {_dt(i.get('timestamp') or i.get('created'))} | "
            f"Last updated: {_dt(i.get('lastUpdated'))}"
        )

    async def list_sumologic_insights(
        query: str = "",
        status: str = "",
        offset: int = 0,
        limit: int = 20,
    ) -> str:
        """List Cloud SIEM insights (triage queue). Filter by status and/or a raw query.

        ``status`` is a comma-separated subset of new, inprogress, closed
        (case-insensitive). ``query`` is the Cloud SIEM search DSL (e.g.
        ``severity:>"MEDIUM" entity.ip:"1.2.3.4"``); it is combined with the
        status filter.
        """
        clauses: list[str] = []
        if query:
            clauses.append(query.strip())
        if status:
            statuses = [s.strip().lower() for s in status.split(",") if s.strip()]
            invalid = [s for s in statuses if s not in _INSIGHT_STATUSES]
            if invalid:
                return (
                    f"Error: invalid insight status filter: {', '.join(invalid)}. "
                    f"Valid values are {', '.join(sorted(_INSIGHT_STATUSES))}."
                )
            if statuses:
                joined = ",".join(f'"{s}"' for s in statuses)
                clauses.append(f"status:in({joined})")
        params: dict[str, Any] = {
            "offset": max(0, int(offset or 0)),
            "limit": max(1, min(int(limit or 20), _MAX_PAGE)),
        }
        if clauses:
            params["q"] = " ".join(clauses)
        data, err = await _api("get", "/sec/v1/insights", "list insights", params=params)
        if err:
            return err
        insights = _cse_objects(data)
        out = format_entity_list(insights, _format_insight, noun="insight")
        if _cse_has_next(data):
            out += f"\n[More results available - pass offset={params['offset'] + len(insights)}]"
        return out

    async def get_sumologic_insight(insight_id: str) -> str:
        """Get full detail of a Cloud SIEM insight by id, including its signals."""
        data, err = await _api(
            "get",
            f"/sec/v1/insights/{insight_id}",
            f"get insight {insight_id}",
        )
        if err:
            return err
        i = _cse_data(data)
        if not i:
            return f"Insight {insight_id} not found."
        signals = i.get("signals") or []
        lines = [
            f"Insight {i.get('readableId') or i.get('id', insight_id)}: "
            f"{_trunc(i.get('name'), 200)}",
            f"Severity: {_sev_icon(i.get('severity'))} {i.get('severity', '?')} | "
            f"Status: {_status_name(i.get('status'))} | "
            f"Resolution: {i.get('resolution') or 'N/A'}",
            f"Assignee: {_entity_name(i.get('assignee')) if i.get('assignee') else 'Unassigned'} | "
            f"Entity: {_entity_name(i.get('entity'))} | Source: {i.get('source', '?')}",
            f"Created: {_dt(i.get('timestamp') or i.get('created'))} | "
            f"Last updated: {_dt(i.get('lastUpdated'))} | Closed: {_dt(i.get('closed'))}",
        ]
        if i.get("description"):
            lines.append(f"\nDescription: {_trunc(i.get('description'), 400)}")
        if signals:
            lines.append(f"\nSignals ({len(signals)}):")
            for s in signals[:15]:
                lines.append(
                    f"  - [{s.get('severity', '?')}] {_trunc(s.get('name'), 90)} "
                    f"(id: {s.get('id', '?')} | rule: {s.get('ruleId', '?')} | "
                    f"stage: {s.get('stage', '?')})"
                )
        return "\n".join(lines)

    async def update_sumologic_insight_status(
        insight_id: str, status: str, resolution: str = ""
    ) -> str:
        """Set the status of a Cloud SIEM insight (new, inprogress, closed).

        Closing an insight (status=closed) records a resolution - one of
        Resolved, "False Positive", "No Action", Duplicate (defaults to
        Resolved when omitted).
        """
        status = (status or "").strip().lower()
        if status not in _INSIGHT_STATUSES:
            return f"Error: status must be one of {', '.join(sorted(_INSIGHT_STATUSES))}."
        body: dict[str, Any] = {"status": status}
        if status == "closed":
            body["resolution"] = resolution or "Resolved"
        data, err = await _api(
            "put",
            f"/sec/v1/insights/{insight_id}/status",
            f"update insight {insight_id} status",
            json_body=body,
        )
        if err:
            return err
        i = _cse_data(data)
        return (
            f"Insight {insight_id} status set to '{_status_name(i.get('status')) or status}'"
            + (f" (resolution: {i.get('resolution')})" if i.get("resolution") else "")
            + "."
        )

    async def add_sumologic_insight_comment(insight_id: str, comment: str) -> str:
        """Add an investigation comment to a Cloud SIEM insight (audit trail)."""
        if not comment or not comment.strip():
            return "Error: a non-empty comment is required."
        _, err = await _api(
            "post",
            f"/sec/v1/insights/{insight_id}/comments",
            f"comment on insight {insight_id}",
            json_body={"body": comment},
        )
        if err:
            return err
        return f"Comment added to insight {insight_id}."

    # ══════════════════════════════════════════════════════════════════════
    # 3. CLOUD SIEM - SIGNALS + ENTITIES (investigation)
    # ══════════════════════════════════════════════════════════════════════

    def _format_signal(s: dict) -> str:
        return (
            f"  {_sev_icon(s.get('severity'))} [sev {s.get('severity', '?')}] "
            f"{_trunc(s.get('name'), 90)}\n"
            f"    id: {s.get('id', '?')} | Rule: {s.get('ruleId', '?')} | "
            f"Stage: {s.get('stage', '?')} | Type: {s.get('contentType', '?')} | "
            f"Entity: {_entity_name(s.get('entity'))} | Records: {s.get('recordCount', 0)}\n"
            f"    Created: {_dt(s.get('timestamp') or s.get('created'))}"
        )

    async def list_sumologic_signals(
        query: str = "", offset: int = 0, limit: int = 20
    ) -> str:
        """List Cloud SIEM signals (the building blocks of insights).

        ``query`` is the Cloud SIEM search DSL (e.g.
        ``contentType:"RULE" name:contains("Internal")``).
        """
        params: dict[str, Any] = {
            "offset": max(0, int(offset or 0)),
            "limit": max(1, min(int(limit or 20), _MAX_PAGE)),
        }
        if query:
            params["q"] = query.strip()
        data, err = await _api("get", "/sec/v1/signals", "list signals", params=params)
        if err:
            return err
        signals = _cse_objects(data)
        out = format_entity_list(signals, _format_signal, noun="signal")
        if _cse_has_next(data):
            out += f"\n[More results available - pass offset={params['offset'] + len(signals)}]"
        return out

    async def get_sumologic_signal(signal_id: str) -> str:
        """Get full detail of a Cloud SIEM signal by id."""
        data, err = await _api(
            "get", f"/sec/v1/signals/{signal_id}", f"get signal {signal_id}"
        )
        if err:
            return err
        s = _cse_data(data)
        if not s:
            return f"Signal {signal_id} not found."
        lines = [
            f"Signal {s.get('id', signal_id)}: {_trunc(s.get('name'), 200)}",
            f"Severity: {_sev_icon(s.get('severity'))} {s.get('severity', '?')} | "
            f"Stage: {s.get('stage', '?')} | Content type: {s.get('contentType', '?')}",
            f"Rule: {s.get('ruleId', '?')} | Entity: {_entity_name(s.get('entity'))} | "
            f"Records: {s.get('recordCount', 0)} | Suppressed: {s.get('suppressed', False)}",
            f"Created: {_dt(s.get('timestamp') or s.get('created'))}",
        ]
        if s.get("description"):
            lines.append(f"\nDescription: {_trunc(s.get('description'), 500)}")
        return "\n".join(lines)

    def _format_entity(e: dict) -> str:
        return (
            f"  {e.get('name', '?')} [{_entity_name(e.get('entityType')) if e.get('entityType') else (e.get('type') or '?')}]\n"
            f"    id: {e.get('id', '?')} | Activity score: {e.get('activityScore', 'N/A')} | "
            f"Whitelisted: {e.get('isWhitelisted', e.get('whitelisted', False))} | "
            f"OS: {_trunc(e.get('operatingSystem'), 40) or 'N/A'}\n"
            f"    First seen: {_dt(e.get('firstSeen'))} | Last seen: {_dt(e.get('lastSeen'))}"
        )

    async def list_sumologic_entities(
        query: str = "", offset: int = 0, limit: int = 20
    ) -> str:
        """List Cloud SIEM entities (hosts, users, IPs) with their activity scores.

        ``query`` is the Cloud SIEM search DSL (e.g.
        ``type:"ip" activityScore:>=3``).
        """
        params: dict[str, Any] = {
            "offset": max(0, int(offset or 0)),
            "limit": max(1, min(int(limit or 20), _MAX_PAGE)),
        }
        if query:
            params["q"] = query.strip()
        data, err = await _api("get", "/sec/v1/entities", "list entities", params=params)
        if err:
            return err
        entities = _cse_objects(data)
        out = format_entity_list(entities, _format_entity, noun="entity")
        if _cse_has_next(data):
            out += f"\n[More results available - pass offset={params['offset'] + len(entities)}]"
        return out

    async def get_sumologic_entity(entity_id: str) -> str:
        """Get full detail of a Cloud SIEM entity by id (with inventory enrichment)."""
        data, err = await _api(
            "get",
            f"/sec/v1/entities/{entity_id}",
            f"get entity {entity_id}",
            params={"expand": "inventory"},
        )
        if err:
            return err
        e = _cse_data(data)
        if not e:
            return f"Entity {entity_id} not found."
        lines = [
            f"Entity {e.get('id', entity_id)}: {e.get('name', '?')}",
            f"Activity score: {e.get('activityScore', 'N/A')} | "
            f"Whitelisted: {e.get('isWhitelisted', e.get('whitelisted', False))} | "
            f"Inventory data: {e.get('inventoryData', e.get('hasInventoryData', False))}",
            f"Hostname: {e.get('hostname') or 'N/A'} | "
            f"OS: {_trunc(e.get('operatingSystem'), 80) or 'N/A'}",
            f"First seen: {_dt(e.get('firstSeen'))} | Last seen: {_dt(e.get('lastSeen'))}",
        ]
        tags = e.get("tags") or []
        if tags:
            rendered = ", ".join(
                f"{t.get('key')}={t.get('value')}" if isinstance(t, dict) else str(t)
                for t in tags[:8]
            )
            lines.append(f"Tags: {rendered}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 4. CLOUD SIEM - THREAT INTELLIGENCE (creating intelligence)
    # ══════════════════════════════════════════════════════════════════════

    async def list_sumologic_threat_intel_sources(
        offset: int = 0, limit: int = 20
    ) -> str:
        """List Cloud SIEM threat-intel sources (needed to create indicators)."""
        params = {
            "offset": max(0, int(offset or 0)),
            "limit": max(1, min(int(limit or 20), _MAX_PAGE)),
        }
        data, err = await _api(
            "get", "/sec/v1/threat-intel-sources", "list threat intel sources", params=params
        )
        if err:
            return err
        sources = _cse_objects(data)

        def _fmt(s: dict) -> str:
            return (
                f"  #{s.get('id', '?')} {s.get('name', 'Unnamed')} "
                f"[{s.get('sourceType', '?')}] - {_trunc(s.get('description'), 100) or 'no description'}"
            )

        return format_entity_list(sources, _fmt, noun="threat intel source")

    async def manage_sumologic_threat_indicators(
        action: str,
        value: str = "",
        source_id: str = "",
        query: str = "",
        description: str = "",
        expiration: str = "",
        active: bool = True,
        offset: int = 0,
        limit: int = 20,
    ) -> str:
        """List or create Cloud SIEM threat-intel indicators (IOCs).

        action="list": search indicators (optionally filter by exact ``value``
        or a raw ``query``). action="create": add an indicator ``value`` to the
        threat-intel ``source_id`` (see list_sumologic_threat_intel_sources);
        ``expiration`` is an ISO-8601 timestamp (defaults to 90 days out).
        """
        act = (action or "").strip().lower()
        if act not in _INDICATOR_ACTIONS:
            return f"Error: action must be one of {', '.join(sorted(_INDICATOR_ACTIONS))}."

        if act == "list":
            params: dict[str, Any] = {
                "offset": max(0, int(offset or 0)),
                "limit": max(1, min(int(limit or 20), _MAX_PAGE)),
            }
            if value:
                params["value"] = value
            if query:
                params["q"] = query.strip()
            data, err = await _api(
                "get",
                "/sec/v1/threat-intel-indicators",
                "list threat intel indicators",
                params=params,
            )
            if err:
                return err
            indicators = _cse_objects(data)

            def _fmt(i: dict) -> str:
                meta = i.get("meta") or {}
                return (
                    f"  {i.get('value', '?')} [{i.get('type', meta.get('type', '?'))}]\n"
                    f"    id: {i.get('id', '?')} | Active: {i.get('active', '?')} | "
                    f"Expiration: {_dt(i.get('expiration'))}"
                )

            return format_entity_list(indicators, _fmt, noun="threat intel indicator")

        # create
        if not source_id or not value:
            return (
                "Error: creating an indicator requires source_id (from "
                "list_sumologic_threat_intel_sources) and value."
            )
        default_expiry = (
            datetime.now(UTC) + timedelta(days=_INDICATOR_DEFAULT_TTL_DAYS)
        ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        indicator = {
            "value": value,
            "active": bool(active),
            "expiration": expiration or default_expiry,
            "description": description or f"Added via {get_cached_branding().name}",
        }
        _, err = await _api(
            "post",
            f"/sec/v1/threat-intel-sources/{source_id}/items",
            f"create threat intel indicator in source {source_id}",
            json_body={"indicators": [indicator]},
        )
        if err:
            return err
        return f"Threat intel indicator '{value}' added to source {source_id}."

    # ══════════════════════════════════════════════════════════════════════
    # 5. MONITORING + DATA COLLECTION (context)
    # ══════════════════════════════════════════════════════════════════════

    async def list_sumologic_monitors(
        query: str = "", monitor_status: str = "", limit: int = 25, offset: int = 0
    ) -> str:
        """List Sumo Logic monitors (alerting rules). Optionally filter by status.

        ``monitor_status`` is one of Normal, Critical, Warning, MissingData,
        Disabled, AllTriggered (case-insensitive). ``query`` is free text / the
        monitors search DSL. An empty query lists all monitors and folders.
        """
        clauses: list[str] = []
        if query:
            clauses.append(query.strip())
        if monitor_status:
            canonical = _MONITOR_STATUSES.get(monitor_status.strip().lower())
            if not canonical:
                return (
                    f"Error: invalid monitor_status '{monitor_status}'. Valid values "
                    f"are {', '.join(_MONITOR_STATUSES.values())}."
                )
            clauses.append(f"monitorStatus:{canonical}")
        params = {
            "query": " ".join(clauses),
            "limit": max(1, min(int(limit or 25), _MAX_PAGE)),
            "offset": max(0, int(offset or 0)),
        }
        data, err = await _api(
            "get", "/v1/monitors/search", "list monitors", params=params
        )
        if err:
            return err
        rows = data if isinstance(data, list) else []

        def _fmt(row: dict) -> str:
            item = row.get("item") if isinstance(row, dict) else None
            item = item if isinstance(item, dict) else (row if isinstance(row, dict) else {})
            content_type = item.get("contentType", "?")
            status = item.get("status")
            status_str = ", ".join(status) if isinstance(status, list) else (status or "N/A")
            return (
                f"  {item.get('name', '?')} [{content_type}"
                + (f"/{item.get('monitorType')}" if item.get("monitorType") else "")
                + f"]\n    id: {item.get('id', '?')} | "
                f"Enabled: {not item.get('isDisabled', False)} | Status: {status_str}"
                + (f" | Path: {_trunc(row.get('path'), 60)}" if isinstance(row, dict) and row.get("path") else "")
            )

        return format_entity_list(rows, _fmt, noun="monitor")

    async def list_sumologic_collectors(
        collector_filter: str = "", limit: int = 25, offset: int = 0
    ) -> str:
        """List Sumo Logic collectors (data inputs). Optionally filter by state.

        ``collector_filter`` is one of installed, hosted, dead, alive
        (case-insensitive).
        """
        params: dict[str, Any] = {
            "limit": max(1, min(int(limit or 25), _MAX_COLLECTOR_PAGE)),
            "offset": max(0, int(offset or 0)),
        }
        if collector_filter:
            normalized = collector_filter.strip().lower()
            if normalized not in _COLLECTOR_FILTERS:
                return (
                    f"Error: invalid collector_filter '{collector_filter}'. Valid "
                    f"values are {', '.join(sorted(_COLLECTOR_FILTERS))}."
                )
            params["filter"] = normalized
        data, err = await _api(
            "get", "/v1/collectors", "list collectors", params=params
        )
        if err:
            return err
        collectors = (data or {}).get("collectors", []) if isinstance(data, dict) else []

        def _fmt(c: dict) -> str:
            return (
                f"  #{c.get('id', '?')} {c.get('name', 'Unnamed')} "
                f"[{c.get('collectorType', '?')}] - alive: {c.get('alive', '?')} | "
                f"category: {c.get('category') or 'N/A'} | "
                f"last seen: {_epoch_ms(c.get('lastSeenAlive'))}"
            )

        return format_entity_list(collectors, _fmt, noun="collector")

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("get_sumologic_status", get_sumologic_status)
    executor.register("run_sumologic_search", run_sumologic_search)
    executor.register("list_sumologic_insights", list_sumologic_insights)
    executor.register("get_sumologic_insight", get_sumologic_insight)
    executor.register("update_sumologic_insight_status", update_sumologic_insight_status)
    executor.register("add_sumologic_insight_comment", add_sumologic_insight_comment)
    executor.register("list_sumologic_signals", list_sumologic_signals)
    executor.register("get_sumologic_signal", get_sumologic_signal)
    executor.register("list_sumologic_entities", list_sumologic_entities)
    executor.register("get_sumologic_entity", get_sumologic_entity)
    executor.register(
        "list_sumologic_threat_intel_sources", list_sumologic_threat_intel_sources
    )
    executor.register(
        "manage_sumologic_threat_indicators", manage_sumologic_threat_indicators
    )
    executor.register("list_sumologic_monitors", list_sumologic_monitors)
    executor.register("list_sumologic_collectors", list_sumologic_collectors)

    return [
        # ── 0. Connection ──────────────────────────────────────────────────
        ToolDefinition(
            name="get_sumologic_status",
            description=(
                "Validate the Sumo Logic connection (cheap authenticated probe). "
                "Use first if other Sumo Logic tools fail."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        # ── 1. Search Job API (log hunting) ────────────────────────────────
        ToolDefinition(
            name="run_sumologic_search",
            description=(
                "Run a Sumo Logic log search for threat hunting and wait for the "
                "results (asynchronous Search Job API: create -> poll -> fetch). "
                "Returns aggregation records when the search produced any "
                "(aggregation queries like '... | count by user' yield records), "
                "otherwise raw messages. Put the lookback in 'hours'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Sumo Logic search expression",
                    },
                    "hours": {
                        "type": "integer",
                        "default": 24,
                        "description": "Lookback window in hours from now",
                    },
                    "limit": {"type": "integer", "default": 50},
                    "time_zone": {
                        "type": "string",
                        "default": "UTC",
                        "description": "IANA time zone for the search window",
                    },
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 2. Cloud SIEM insights ─────────────────────────────────────────
        ToolDefinition(
            name="list_sumologic_insights",
            description=(
                "List Cloud SIEM (CSE) insights - the triage queue of correlated "
                "threats. Filter by status (comma-separated: new, inprogress, "
                "closed) and/or a raw Cloud SIEM query (e.g. 'severity:>\"MEDIUM\"'). "
                "Requires Cloud SIEM Enterprise."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Cloud SIEM search DSL"},
                    "status": {
                        "type": "string",
                        "description": "Comma-separated: new, inprogress, closed",
                    },
                    "offset": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_sumologic_insight",
            description=(
                "Get full detail of a Cloud SIEM insight by id (severity, status, "
                "resolution, entity, timeline, and contributing signals)."
            ),
            input_schema={
                "type": "object",
                "properties": {"insight_id": {"type": "string"}},
                "required": ["insight_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="update_sumologic_insight_status",
            description=(
                "Set a Cloud SIEM insight's status: new, inprogress, or closed. "
                "Closing records a resolution (Resolved, 'False Positive', "
                "'No Action', Duplicate; defaults to Resolved)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "insight_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["new", "inprogress", "closed"],
                    },
                    "resolution": {
                        "type": "string",
                        "description": "Resolution when closing the insight",
                    },
                },
                "required": ["insight_id", "status"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="add_sumologic_insight_comment",
            description="Add an investigation comment to a Cloud SIEM insight (audit trail).",
            input_schema={
                "type": "object",
                "properties": {
                    "insight_id": {"type": "string"},
                    "comment": {"type": "string"},
                },
                "required": ["insight_id", "comment"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 3. Cloud SIEM signals + entities ───────────────────────────────
        ToolDefinition(
            name="list_sumologic_signals",
            description=(
                "List Cloud SIEM signals (the detections that compose insights). "
                "Filter with the Cloud SIEM query DSL (e.g. 'contentType:\"RULE\"')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Cloud SIEM search DSL"},
                    "offset": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_sumologic_signal",
            description="Get full detail of a Cloud SIEM signal by id.",
            input_schema={
                "type": "object",
                "properties": {"signal_id": {"type": "string"}},
                "required": ["signal_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_sumologic_entities",
            description=(
                "List Cloud SIEM entities (hosts, users, IPs) and their activity "
                "scores. Filter with the Cloud SIEM query DSL (e.g. "
                "'type:\"ip\" activityScore:>=3')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Cloud SIEM search DSL"},
                    "offset": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_sumologic_entity",
            description=(
                "Get full detail of a Cloud SIEM entity by id (activity score, "
                "first/last seen, OS, inventory tags)."
            ),
            input_schema={
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 4. Cloud SIEM threat intelligence ──────────────────────────────
        ToolDefinition(
            name="list_sumologic_threat_intel_sources",
            description=(
                "List Cloud SIEM threat-intel sources and their ids (needed to "
                "create indicators with manage_sumologic_threat_indicators)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "offset": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_sumologic_threat_indicators",
            description=(
                "List or create Cloud SIEM threat-intel indicators (IOCs). "
                "action='list' searches indicators (filter by exact value or a raw "
                "query); action='create' adds an indicator value to a threat-intel "
                "source (source_id from list_sumologic_threat_intel_sources)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create"]},
                    "value": {
                        "type": "string",
                        "description": "Indicator value (IP, domain, hash, URL)",
                    },
                    "source_id": {
                        "type": "string",
                        "description": "Threat-intel source id (required for create)",
                    },
                    "query": {"type": "string", "description": "Search DSL for list"},
                    "description": {"type": "string"},
                    "expiration": {
                        "type": "string",
                        "description": "ISO-8601 expiry (create; defaults to 90 days)",
                    },
                    "active": {"type": "boolean", "default": True},
                    "offset": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 5. Monitoring + data collection ────────────────────────────────
        ToolDefinition(
            name="list_sumologic_monitors",
            description=(
                "List Sumo Logic monitors (alerting rules) and folders. Filter by "
                "monitor_status (Normal, Critical, Warning, MissingData, Disabled, "
                "AllTriggered) and/or a free-text query. An empty query lists all."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "monitor_status": {
                        "type": "string",
                        "enum": [
                            "Normal",
                            "Critical",
                            "Warning",
                            "MissingData",
                            "Disabled",
                            "AllTriggered",
                        ],
                    },
                    "limit": {"type": "integer", "default": 25},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_sumologic_collectors",
            description=(
                "List Sumo Logic collectors (data inputs) with their state and "
                "last-seen time. Filter by collector_filter: installed, hosted, "
                "dead, or alive."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "collector_filter": {
                        "type": "string",
                        "enum": ["installed", "hosted", "dead", "alive"],
                    },
                    "limit": {"type": "integer", "default": 25},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
