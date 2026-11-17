# SPDX-License-Identifier: Apache-2.0

"""Tanium integration – Agentic SOC for converged endpoint operations.

Full-fledged Tanium integration spanning the three Tanium integration surfaces,
designed as a best-in-class building block for an agentic security team and the
"detection → investigation → threat hunting → response → intelligence" loop:

  - **Threat Response REST API** (``/plugin/products/threat-response/api/v1``):
    alerts (list/get/update state), intel documents (list/get/create - also
    from a file of the run - /deploy — *creating intelligence*), live-response
    actions, endpoint connections, and file evidence (listed with its download
    URL, stored as Tanium's password-protected ZIP).
  - **Core Platform REST API** (``/api/v2``): Interact questions (ask + poll for
    results — *threat hunting*), question parsing, saved questions, sensors,
    computer groups, packages, action groups, and action deployment
    (*remediation*).
  - **API Gateway (GraphQL)** (``/plugin/products/gateway/graphql``): endpoint /
    asset inventory plus a raw GraphQL passthrough for the full Gateway surface.

Authentication
--------------
Tanium authenticates with an **API token** passed in the ``session`` HTTP header
(not ``Authorization``/``Bearer``). The token is created in the Tanium Console
under *Administration → Permissions → API Tokens*. ``integration_tools._get_token``
assembles the compound ``base_url||api_token`` token this builder parses, where
``base_url`` is the Tanium Cloud (``https://<customer>-api.cloud.tanium.com``) or
on-prem (``https://<server>``) host.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

from app.core.egress import egress_client
from app.integrations.file_delivery import (
    PersistFileFn,
    deliver_live_artefact,
    fetch_file,
    read_pipe_file,
    uploaded_file_answer,
)
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.services.branding import get_cached_branding
from app.services.content_safety import single_line
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_PROVIDER = "Tanium"
_SOURCE = "integration:tanium"

# Relative API roots appended to the configured base URL.
_CORE_API = "/api/v2"
_TR_API = "/plugin/products/threat-response/api/v1"
_GATEWAY_API = "/plugin/products/gateway/graphql"

# How long ``ask_tanium_question`` waits for endpoints to report, and how often
# it polls. Tanium questions are answered asynchronously as the linear chain of
# endpoints reports in, so we issue the question and then poll the result-data
# endpoint until the cohort has converged (or the budget is exhausted).
_QUESTION_POLL_INTERVAL = 3.0
_QUESTION_MAX_POLLS = 8

# File evidence comes as Tanium's ZIP encrypted with the password "infected"
# (https://docs.tanium.com/trace/trace/saving_evidence_for_analysis.html).
_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
# An intel document read from a file of the run (YARA / STIX / OpenIOC text).
_MAX_INTEL_BYTES = 10 * 1024 * 1024

_SEVERITY_ICONS: dict[str, str] = {
    "critical": "🔴",
    "high": "🔴",
    "medium": "🟠",
    "low": "🟡",
    "info": "🔵",
    "informational": "🔵",
}

_STATE_ICONS: dict[str, str] = {
    "unresolved": "🆕",
    "inprogress": "⚡",
    "in progress": "⚡",
    "resolved": "✅",
    "ignored": "🚫",
    "dismissed": "🚫",
}

# Tanium intel document type → request Content-Type. The Threat Response intel
# API infers the document type from the request Content-Type, so creating intel
# means POSTing the raw document with the right media type.
_INTEL_CONTENT_TYPES: dict[str, str] = {
    "yara": "application/x-yara",
    "openioc": "application/xml",
    "stix": "application/xml",
    "cybox": "application/xml",
    "tanium_signal": "application/json",
    "json": "application/json",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(token: str) -> tuple[str, str]:
    base_url, api_token = parse_compound_token(token, 2)
    return base_url.rstrip("/"), api_token


def _headers(api_token: str) -> dict[str, str]:
    return {
        "session": api_token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _sev(value: str | None) -> str:
    return _SEVERITY_ICONS.get((value or "").lower(), "⚪")


def _state(value: str | None) -> str:
    return _STATE_ICONS.get((value or "").lower(), "❓")


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text)
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _dt(value: str | None) -> str:
    """Trim an ISO timestamp to second precision for display."""
    return (value or "")[:19]


def _cell(value: Any, max_len: int = 80) -> str:
    """Render a result cell safe for a pipe-delimited table.

    Strips embedded newlines/tabs (common in Tanium sensor output like process
    command lines and file paths) that would otherwise break the
    single-row-per-record layout and misalign the table.
    """
    if value is None:
        return "null"
    text = str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return text[:max_len]


def _result_set_progress(result_set: dict[str, Any]) -> tuple[int, int]:
    """Return ``(estimated_total, machines_tested)`` for a question result set."""
    estimated = int(result_set.get("estimated_total") or 0)
    tested = int(result_set.get("mr_tested") or result_set.get("tested") or 0)
    return estimated, tested


def _format_question_results(payload: dict[str, Any], *, max_rows: int = 50) -> str:
    """Render a Tanium question ``result_data`` payload as a compact table."""
    data = payload.get("data") or {}
    result_sets = data.get("result_sets") or []
    if not result_sets:
        return "Question returned no result sets yet."

    rs = result_sets[0]
    columns = [c.get("name", "?") for c in rs.get("columns", [])]
    rows = rs.get("rows", []) or []
    estimated, tested = _result_set_progress(rs)

    if not rows:
        header = (
            f"Question returned 0 rows so far "
            f"(endpoints reported: {tested}/{estimated or '?'})."
        )
        return f"{header} Columns: {', '.join(columns) or 'N/A'}"

    lines = [
        f"Question results — {len(rows)} row(s), "
        f"endpoints reported {tested}/{estimated or '?'}:"
    ]
    lines.append(" | ".join(columns))
    lines.append("-" * min(len(" | ".join(columns)), 120))

    for row in rows[:max_rows]:
        cells = row.get("data", []) if isinstance(row, dict) else []
        values: list[str] = []
        for cell in cells:
            if isinstance(cell, list):
                texts = [str(c.get("text", "")) for c in cell if isinstance(c, dict)]
                values.append(", ".join(t for t in texts if t) or "—")
            elif isinstance(cell, dict):
                values.append(str(cell.get("text", "")))
            else:
                values.append(str(cell))
        lines.append(" | ".join(_cell(v) for v in values))

    if len(rows) > max_rows:
        lines.append(f"\n... [{len(rows) - max_rows} more rows not shown]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    persist_file_fn: PersistFileFn | None = None,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Tanium tools from a ``base_url||api_token`` compound token.

    ``persist_file_fn`` (injected by ``integration_tools``) stores downloaded
    file evidence as a file of the run.
    """
    base_url, api_token = _parse(token)
    if not base_url or not api_token:
        return []

    core = f"{base_url}{_CORE_API}"
    tr = f"{base_url}{_TR_API}"
    gateway = f"{base_url}{_GATEWAY_API}"
    headers = _headers(api_token)

    # ══════════════════════════════════════════════════════════════════════
    # 0. CONNECTION
    # ══════════════════════════════════════════════════════════════════════

    async def get_tanium_session_info() -> str:
        """Return the authenticated Tanium session / user (validate the token)."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{core}/session/info",
                _PROVIDER,
                "get session info",
                headers=headers,
            )
            if err:
                return err
            data = resp.json().get("data", {}) or {}
            return (
                f"Tanium connected at {base_url}.\n"
                f"  User: {data.get('user_name', data.get('name', '?'))} "
                f"(id: {data.get('user_id', '?')})\n"
                f"  Roles: {', '.join(str(r) for r in data.get('roles', [])) or 'N/A'}"
            )

    # ══════════════════════════════════════════════════════════════════════
    # 1. THREAT RESPONSE — ALERTS (gathering incidents)
    # ══════════════════════════════════════════════════════════════════════

    async def list_tanium_alerts(
        state: str = "",
        priority: str = "",
        severity: str = "",
        intel_doc_id: int = 0,
        computer_name: str = "",
        computer_ip: str = "",
        mitre_id: str = "",
        alert_type: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """List Tanium Threat Response alerts with optional filters."""
        params: dict[str, Any] = {
            "limit": min(max(limit, 1), 100),
            "offset": max(offset, 0),
        }
        if state:
            params["state"] = state
        if priority:
            params["priority"] = priority
        if severity:
            params["severity"] = severity
        if intel_doc_id:
            params["intelDocId"] = intel_doc_id
        if computer_name:
            params["computerName"] = computer_name
        if computer_ip:
            params["computerIpAddress"] = computer_ip
        if mitre_id:
            params["mitreId"] = mitre_id
        if alert_type:
            params["type"] = alert_type

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{tr}/alerts",
                _PROVIDER,
                "list alerts",
                headers=headers,
                params=params,
            )
            if err:
                return err
            body = resp.json()
            alerts = body.get("data", []) or []
            meta = body.get("meta", {}) or {}
            total = meta.get("filteredCount") or meta.get("totalCount")

            def _fmt(a: dict) -> str:
                return (
                    f"  {_sev(a.get('severity'))} alert #{a.get('id', '?')} "
                    f"[{a.get('severity', '?')}/{a.get('priority', '?')}] "
                    f"{_state(a.get('state'))} {a.get('state', '?')} — "
                    f"{a.get('type', 'alert')}\n"
                    f"    Host: {a.get('computerName', '?')} "
                    f"({a.get('computerIpAddress', '?')}) | "
                    f"Intel: {a.get('intelDocId', '—')} | "
                    f"GUID: {a.get('guid', '?')}\n"
                    f"    Alerted: {_dt(a.get('alertedAt'))}"
                )

            return format_entity_list(alerts, _fmt, noun="alert", total=total)

    async def get_tanium_alert(alert_id: int) -> str:
        """Get full details (including match evidence) of a Threat Response alert."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{tr}/alerts/{alert_id}",
                _PROVIDER,
                f"get alert {alert_id}",
                headers=headers,
            )
            if err:
                return err
            a = resp.json().get("data", {}) or {}
            lines = [
                f"Alert #{a.get('id', alert_id)} — {a.get('type', 'alert')}",
                f"State: {_state(a.get('state'))} {a.get('state', '?')}",
                f"Severity: {_sev(a.get('severity'))} {a.get('severity', '?')} | "
                f"Priority: {a.get('priority', '?')}",
                f"Host: {a.get('computerName', '?')} ({a.get('computerIpAddress', '?')})",
                f"Intel doc: {a.get('intelDocId', '—')} "
                f"(revision {a.get('intelDocRevisionId', '—')})",
                f"GUID: {a.get('guid', '?')}",
                f"Path: {a.get('path', '—')} | Match type: {a.get('matchType', '—')}",
                f"Alerted: {_dt(a.get('alertedAt'))} | "
                f"Updated: {_dt(a.get('updatedAt'))}",
            ]
            details = a.get("details")
            if details:
                lines.append(f"\nMatch details:\n{_trunc(details, 2000)}")
            return "\n".join(lines)

    async def update_tanium_alert_state(alert_id: int, state: str) -> str:
        """Update a Threat Response alert state (Unresolved/InProgress/Resolved/Ignored)."""
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "put",
                f"{tr}/alerts/{alert_id}",
                _PROVIDER,
                f"update alert {alert_id} state",
                headers=headers,
                json={"state": state},
            )
            if err:
                return err
            return f"Alert {alert_id} state set to '{state}'."

    # ══════════════════════════════════════════════════════════════════════
    # 2. THREAT RESPONSE — INTEL (creating intelligence)
    # ══════════════════════════════════════════════════════════════════════

    async def list_tanium_intel_docs(limit: int = 25, offset: int = 0) -> str:
        """List Threat Response intel documents (detection content)."""
        params = {"limit": min(max(limit, 1), 100), "offset": max(offset, 0)}
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{tr}/intels",
                _PROVIDER,
                "list intel documents",
                headers=headers,
                params=params,
            )
            if err:
                return err
            body = resp.json()
            docs = body.get("data", []) or []

            def _fmt(d: dict) -> str:
                return (
                    f"  intel #{d.get('id', '?')} [{d.get('type', '?')}] "
                    f"{d.get('name', 'Unnamed')}\n"
                    f"    Alerts: {d.get('alertCount', 0)} "
                    f"(unresolved: {d.get('unresolvedAlertCount', 0)}) | "
                    f"Updated: {_dt(d.get('updatedAt'))}"
                )

            return format_entity_list(docs, _fmt, noun="intel document")

    async def get_tanium_intel_doc(intel_doc_id: int) -> str:
        """Get a Threat Response intel document (including its contents)."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{tr}/intels/{intel_doc_id}",
                _PROVIDER,
                f"get intel document {intel_doc_id}",
                headers=headers,
            )
            if err:
                return err
            d = resp.json().get("data", {}) or {}
            lines = [
                f"Intel doc #{d.get('id', intel_doc_id)}: {d.get('name', 'Unnamed')}",
                f"Type: {d.get('type', '?')} (v{d.get('typeVersion', '?')}) | "
                f"Revision: {d.get('revisionId', '?')}",
                f"Description: {_trunc(d.get('description'), 400) or 'N/A'}",
                f"Alerts: {d.get('alertCount', 0)} "
                f"(unresolved: {d.get('unresolvedAlertCount', 0)}) | "
                f"MD5: {d.get('md5', '?')}",
                f"Created: {_dt(d.get('createdAt'))} | "
                f"Updated: {_dt(d.get('updatedAt'))}",
            ]
            contents = d.get("contents")
            if contents:
                lines.append(f"\nContents:\n{_trunc(contents, 3000)}")
            return "\n".join(lines)

    async def create_tanium_intel_doc(
        contents: str = "",
        intel_type: str = "yara",
        name: str = "",
        file_id: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Create a Threat Response intel document (YARA, STIX, OpenIOC, Tanium Signal).

        ``contents`` is the raw detection content, or ``file_id`` a file of the
        run holding it; ``intel_type`` selects the media type Tanium uses to
        parse it.
        """
        content_type = _INTEL_CONTENT_TYPES.get(intel_type.lower())
        if not content_type:
            return (
                f"Error: unsupported intel_type '{intel_type}'. "
                f"Supported: {', '.join(sorted(_INTEL_CONTENT_TYPES))}."
            )
        if bool(file_id) == bool(contents):
            return "Error: provide exactly one of 'contents' or 'file_id'."
        if file_id:
            piped = await read_pipe_file(
                file_context, file_id, max_bytes=_MAX_INTEL_BYTES, target=_PROVIDER
            )
            if isinstance(piped, str):
                return ToolResult(tool_call_id="", content=piped, is_error=True)
            body, filename, _ = piped
            name = name or filename
        else:
            body = contents.encode("utf-8")

        post_headers = {
            "session": api_token,
            "Content-Type": content_type,
            "Accept": "application/json",
        }
        params: dict[str, Any] = {}
        if name:
            params["name"] = name
        # Self-document provenance for Tanium Signal / JSON intel created by the
        # platform so analysts can trace it back to its source.
        if content_type == "application/json" and "name" not in params:
            params["name"] = f"Created via {get_cached_branding().name}"

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{tr}/intels",
                _PROVIDER,
                "create intel document",
                headers=post_headers,
                params=params or None,
                content=body,
            )
            if err:
                return err
            d = resp.json().get("data", {}) or resp.json()
            created = f"Intel document created (id: {d.get('id', '?')}, type: {d.get('type', intel_type)})"
            intel_name = str(d.get("name", name or "Unnamed"))
            if not file_id:
                return f"{created}: {intel_name}"
            # Named after the stored name of a file of the run unless the
            # call named it: the name goes in the block.
            return uploaded_file_answer(
                f"{created} from file {file_id.strip()}.",
                target=_PROVIDER,
                filename=filename,
                details=[f"Intel name: {single_line(intel_name)}"],
            )

    async def deploy_tanium_intel() -> str:
        """Deploy/redeploy Threat Response intel to all configured endpoints."""
        async with egress_client(timeout=60) as client:
            _, err = await safe_request(
                client,
                "post",
                f"{tr}/intel/deploy",
                _PROVIDER,
                "deploy intel",
                headers=headers,
                json={},
            )
            if err:
                return err
            return "Intel deployment triggered to endpoints."

    # ══════════════════════════════════════════════════════════════════════
    # 3. INTERACT — QUESTIONS & SENSORS (threat hunting)
    # ══════════════════════════════════════════════════════════════════════

    async def ask_tanium_question(query_text: str, max_wait_polls: int = 0) -> str:
        """Ask a Tanium question in natural Tanium syntax and wait for results.

        Example: ``Get Computer Name and IP Address from all machines with
        Running Processes contains "powershell.exe"``. Results stream in from
        endpoints, so the answer reflects the cohort that reported within the
        polling budget.
        """
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{core}/questions",
                _PROVIDER,
                "ask question",
                headers=headers,
                json={"query_text": query_text},
            )
            if err:
                return err
            question = resp.json().get("data", {}) or {}
            question_id = question.get("id")
            if not question_id:
                return "Error: Tanium did not return a question id."

        polls = max_wait_polls if max_wait_polls > 0 else _QUESTION_MAX_POLLS
        last_payload: dict[str, Any] = {}
        async with egress_client(timeout=30) as client:
            for attempt in range(polls):
                await asyncio.sleep(_QUESTION_POLL_INTERVAL)
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{core}/result_data/question/{question_id}",
                    _PROVIDER,
                    f"get results for question {question_id}",
                    headers=headers,
                )
                if err:
                    return err
                last_payload = resp.json()
                result_sets = (last_payload.get("data") or {}).get("result_sets") or []
                if result_sets:
                    estimated, tested = _result_set_progress(result_sets[0])
                    # Converged: all (estimated) endpoints reported, and we have
                    # rows — stop early instead of burning the whole budget.
                    if estimated and tested >= estimated and result_sets[0].get("rows"):
                        break
                    if attempt >= polls - 1:
                        break

        formatted = _format_question_results(last_payload)
        return f"Question #{question_id}: {query_text}\n{formatted}"

    async def get_tanium_question_results(question_id: int) -> str:
        """Get the current results for an already-asked Tanium question by id."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{core}/result_data/question/{question_id}",
                _PROVIDER,
                f"get results for question {question_id}",
                headers=headers,
            )
            if err:
                return err
            return _format_question_results(resp.json())

    async def parse_tanium_question(text: str) -> str:
        """Parse free text into valid Tanium question syntax suggestions."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{core}/parse_questions",
                _PROVIDER,
                "parse question",
                headers=headers,
                json={"text": text},
            )
            if err:
                return err
            parsed = resp.json().get("data", []) or []
            if not parsed:
                return f"No question suggestions found for '{text}'."
            lines = [f"Parsed question suggestions for '{text}':"]
            for p in parsed[:15]:
                lines.append(
                    f"  • {p.get('question_text', '?')} "
                    f"(score: {p.get('score', '?')})"
                )
            return "\n".join(lines)

    async def list_tanium_saved_questions(limit: int = 50) -> str:
        """List saved questions available in the Tanium environment."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{core}/saved_questions",
                _PROVIDER,
                "list saved questions",
                headers=headers,
            )
            if err:
                return err
            saved = resp.json().get("data", []) or []

            def _fmt(s: dict) -> str:
                return (
                    f"  #{s.get('id', '?')} {s.get('name', 'Unnamed')} — "
                    f"{_trunc(s.get('query_text'), 120) or 'N/A'}"
                )

            return format_entity_list(
                saved[: max(limit, 1)], _fmt, noun="saved question"
            )

    async def ask_tanium_saved_question(saved_question_id: int) -> str:
        """Run a saved question and return its current results."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{core}/result_data/saved_question/{saved_question_id}",
                _PROVIDER,
                f"get results for saved question {saved_question_id}",
                headers=headers,
            )
            if err:
                return err
            return _format_question_results(resp.json())

    async def list_tanium_sensors(search: str = "", limit: int = 30) -> str:
        """List/search Tanium sensors (the building blocks of questions)."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{core}/sensors",
                _PROVIDER,
                "list sensors",
                headers=headers,
            )
            if err:
                return err
            sensors = resp.json().get("data", []) or []
            if search:
                needle = search.lower()
                sensors = [
                    s
                    for s in sensors
                    if needle in (s.get("name", "").lower())
                    or needle in (s.get("description", "") or "").lower()
                ]

            def _fmt(s: dict) -> str:
                return (
                    f"  #{s.get('id', '?')} {s.get('name', 'Unnamed')} — "
                    f"{_trunc(s.get('description'), 120) or 'N/A'}"
                )

            return format_entity_list(sensors[: max(limit, 1)], _fmt, noun="sensor")

    # ══════════════════════════════════════════════════════════════════════
    # 4. ENDPOINTS / ASSETS (API Gateway — GraphQL)
    # ══════════════════════════════════════════════════════════════════════

    async def _graphql(query: str, variables: dict[str, Any] | None = None) -> Any:
        async with egress_client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                gateway,
                _PROVIDER,
                "run GraphQL query",
                headers=headers,
                json={"query": query, "variables": variables or {}},
            )
            if err:
                return err
            return resp.json()

    async def list_tanium_endpoints(first: int = 25) -> str:
        """List endpoints (asset inventory) via the Tanium API Gateway."""
        query = (
            "query Endpoints($first: Int) {\n"
            "  endpoints(first: $first) {\n"
            "    edges { node { id name ipAddress os { name platform } "
            "eidLastSeen } }\n"
            "  }\n"
            "}"
        )
        result = await _graphql(query, {"first": min(max(first, 1), 200)})
        if isinstance(result, str):
            return result
        if result.get("errors"):
            return f"Error: Tanium GraphQL error: {result['errors']}"
        edges = (
            ((result.get("data") or {}).get("endpoints") or {}).get("edges") or []
        )
        nodes = [e.get("node", {}) for e in edges if isinstance(e, dict)]

        def _fmt(n: dict) -> str:
            os_info = n.get("os") or {}
            return (
                f"  {n.get('name', '?')} ({n.get('ipAddress', '?')}) — "
                f"{os_info.get('platform', '?')}/{os_info.get('name', '?')} | "
                f"id: {n.get('id', '?')} | Last seen: {_dt(n.get('eidLastSeen'))}"
            )

        return format_entity_list(nodes, _fmt, noun="endpoint")

    async def run_tanium_graphql(query: str, variables: str = "") -> str:
        """Run a raw GraphQL query against the Tanium API Gateway.

        ``variables`` is an optional JSON object string. Use this for any Gateway
        capability not covered by a dedicated tool (filtered endpoint queries,
        actions, asset records, etc.).
        """
        parsed_vars: dict[str, Any] = {}
        if variables.strip():
            import json

            try:
                parsed_vars = json.loads(variables)
            except json.JSONDecodeError as exc:
                return f"Error: variables must be a valid JSON object — {exc}"

        result = await _graphql(query, parsed_vars)
        if isinstance(result, str):
            return result
        import json

        out = json.dumps(result, indent=2, default=str)
        return out[:6000] + ("\n… [truncated]" if len(out) > 6000 else "")

    # ══════════════════════════════════════════════════════════════════════
    # 5. COMPUTER GROUPS
    # ══════════════════════════════════════════════════════════════════════

    async def list_tanium_computer_groups() -> str:
        """List Tanium computer groups (used to target questions and actions)."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{core}/computer_groups",
                _PROVIDER,
                "list computer groups",
                headers=headers,
            )
            if err:
                return err
            groups = resp.json().get("data", []) or []

            def _fmt(g: dict) -> str:
                return (
                    f"  #{g.get('id', '?')} {g.get('name', 'Unnamed')} "
                    f"(type: {g.get('type', '?')})"
                )

            return format_entity_list(groups, _fmt, noun="computer group")

    # ══════════════════════════════════════════════════════════════════════
    # 6. PACKAGES & ACTIONS (remediation)
    # ══════════════════════════════════════════════════════════════════════

    async def list_tanium_packages(limit: int = 30) -> str:
        """List Tanium packages (deployable remediation/response content)."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{core}/packages",
                _PROVIDER,
                "list packages",
                headers=headers,
            )
            if err:
                return err
            packages = resp.json().get("data", []) or []

            def _fmt(p: dict) -> str:
                return (
                    f"  #{p.get('id', '?')} {p.get('name', 'Unnamed')} — "
                    f"{_trunc(p.get('command'), 100) or 'N/A'}"
                )

            return format_entity_list(packages[: max(limit, 1)], _fmt, noun="package")

    async def list_tanium_action_groups() -> str:
        """List Tanium action groups (the targeting scope for action deployment)."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{core}/action_groups",
                _PROVIDER,
                "list action groups",
                headers=headers,
            )
            if err:
                return err
            groups = resp.json().get("data", []) or []

            def _fmt(g: dict) -> str:
                return f"  #{g.get('id', '?')} {g.get('name', 'Unnamed')}"

            return format_entity_list(groups, _fmt, noun="action group")

    async def deploy_tanium_action(
        package_id: int,
        action_group_id: int,
        target_computer_group_id: int,
        name: str = "",
        expire_seconds: int = 600,
        package_parameters: dict[str, str] | None = None,
    ) -> str:
        """Deploy a package to a computer group (a Tanium action — remediation).

        Targets are addressed by id: use ``list_tanium_packages``,
        ``list_tanium_action_groups`` and ``list_tanium_computer_groups`` to
        resolve them. This executes content on endpoints — use deliberately.
        """
        package_spec: dict[str, Any] = {"source_id": package_id}
        if package_parameters:
            package_spec["parameters"] = [
                {"key": k, "value": v} for k, v in package_parameters.items()
            ]
        payload: dict[str, Any] = {
            "name": name or f"{get_cached_branding().name} action",
            "action_group": {"id": action_group_id},
            "package_spec": package_spec,
            "target_group": {"id": target_computer_group_id},
            "expire_seconds": expire_seconds,
        }
        async with egress_client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{core}/actions",
                _PROVIDER,
                "deploy action",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            a = resp.json().get("data", {}) or {}
            return (
                f"Action deployed (id: {a.get('id', '?')}, name: "
                f"{a.get('name', name)}), targeting computer group "
                f"{target_computer_group_id}."
            )

    async def list_tanium_actions(limit: int = 25) -> str:
        """List recent Tanium actions and their status."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{core}/actions",
                _PROVIDER,
                "list actions",
                headers=headers,
            )
            if err:
                return err
            actions = resp.json().get("data", []) or []

            def _fmt(a: dict) -> str:
                pkg = (a.get("package_spec") or {}).get("name", "?")
                return (
                    f"  #{a.get('id', '?')} {a.get('name', 'Unnamed')} — "
                    f"status: {a.get('status', '?')} | package: {pkg} | "
                    f"created: {_dt(a.get('creation_time'))}"
                )

            # The actions endpoint returns most-recent last; show the tail.
            # Clamp to a positive count so limit<=0 can't fall back to the whole
            # list (actions[-0:] == actions[:]).
            return format_entity_list(
                actions[-max(limit, 1):], _fmt, noun="action"
            )

    async def get_tanium_action(action_id: int) -> str:
        """Get the status and details of a specific Tanium action."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{core}/actions/{action_id}",
                _PROVIDER,
                f"get action {action_id}",
                headers=headers,
            )
            if err:
                return err
            a = resp.json().get("data", {}) or {}
            pkg = (a.get("package_spec") or {}) or {}
            lines = [
                f"Action #{a.get('id', action_id)}: {a.get('name', 'Unnamed')}",
                f"Status: {a.get('status', '?')}",
                f"Package: {pkg.get('name', '?')} — {_trunc(pkg.get('command'), 150)}",
                f"Target group: {(a.get('target_group') or {}).get('id', '?')}",
                f"Approved: {a.get('approved_flag', '?')} | "
                f"Expire seconds: {a.get('expire_seconds', '?')}",
                f"Created: {_dt(a.get('creation_time'))} | "
                f"Start: {_dt(a.get('start_time'))}",
            ]
            return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 7. THREAT RESPONSE — LIVE RESPONSE & CONNECTIONS (investigation)
    # ══════════════════════════════════════════════════════════════════════

    async def list_tanium_connections() -> str:
        """List active Threat Response endpoint connections (live investigation)."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{tr}/conns",
                _PROVIDER,
                "list connections",
                headers=headers,
            )
            if err:
                return err
            body = resp.json()
            conns = body.get("data", body) if isinstance(body, dict) else body
            if not isinstance(conns, list):
                conns = []

            def _fmt(c: dict) -> str:
                return (
                    f"  {c.get('name', c.get('id', '?'))} — "
                    f"status: {c.get('status', c.get('state', '?'))} | "
                    f"host: {c.get('hostname', '?')} ({c.get('ip', '?')})"
                )

            return format_entity_list(conns, _fmt, noun="connection")

    async def create_tanium_connection(
        hostname: str,
        client_id: str = "",
        platform: str = "",
        ip: str = "",
    ) -> str:
        """Open a Threat Response live connection to an endpoint for investigation."""
        target: dict[str, Any] = {"hostname": hostname}
        if client_id:
            target["clientId"] = client_id
        if platform:
            target["platform"] = platform
        if ip:
            target["ip"] = ip
        async with egress_client(timeout=60) as client:
            _, err = await safe_request(
                client,
                "post",
                f"{tr}/conns/connect",
                _PROVIDER,
                f"create connection to {hostname}",
                headers=headers,
                json={"target": target},
            )
            if err:
                return err
            return f"Live response connection requested to {hostname}."

    async def list_tanium_response_actions(limit: int = 25, offset: int = 0) -> str:
        """List Threat Response live-response actions (e.g. forensic collection)."""
        params = {"limit": min(max(limit, 1), 100), "offset": max(offset, 0)}
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{tr}/response-actions",
                _PROVIDER,
                "list response actions",
                headers=headers,
                params=params,
            )
            if err:
                return err
            actions = resp.json().get("data", []) or []

            def _fmt(a: dict) -> str:
                opts = a.get("options") or {}
                return (
                    f"  #{a.get('id', '?')} [{a.get('type', '?')}] "
                    f"status: {a.get('status', '?')} — {a.get('computerName', '?')}\n"
                    f"    Package: {opts.get('packageName', '?')} | "
                    f"Created: {_dt(a.get('createdAt'))}"
                )

            return format_entity_list(actions, _fmt, noun="response action")

    async def list_tanium_file_evidence(limit: int = 25, offset: int = 0) -> str:
        """List collected file evidence from Threat Response investigations."""
        params = {"limit": min(max(limit, 1), 100), "offset": max(offset, 0)}
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{tr}/filedownload",
                _PROVIDER,
                "list file evidence",
                headers=headers,
                params=params,
            )
            if err:
                return err
            body = resp.json()
            evidence = body.get("fileEvidence", []) or []
            total = body.get("totalCount")

            def _fmt(e: dict) -> str:
                return (
                    f"  {e.get('path', '?')} on {e.get('hostname', '?')}\n"
                    f"    Evidence id: {e.get('uuid', '?')} | "
                    f"Hash: {e.get('hash', '?')} | "
                    f"Size: {e.get('size', '?')} | "
                    f"Downloaded: {_dt(e.get('downloaded'))}\n"
                    f"    Download URL: {tr}/filedownload/data/{quote(str(e.get('uuid') or ''), safe='')}"
                )

            out = format_entity_list(
                evidence, _fmt, noun="file evidence record", total=total
            )
            if evidence:
                out += (
                    "\n[download_tanium_file_evidence(evidence_id) stores one as "
                    "Tanium's password-protected ZIP]"
                )
            return out

    async def download_tanium_file_evidence(evidence_id: str) -> str | ToolResult:
        """Store collected file evidence as Tanium's password-protected ZIP."""
        evidence_id = (evidence_id or "").strip()
        if not evidence_id:
            return "Error: evidence_id is required (from list_tanium_file_evidence)."
        path_id = quote(evidence_id, safe="")
        data_url = f"{tr}/filedownload/data/{path_id}"
        async with egress_client(timeout=120) as client:
            meta_resp, err = await safe_request(
                client,
                "get",
                f"{tr}/filedownload/{path_id}",
                _PROVIDER,
                f"get file evidence {evidence_id}",
                headers=headers,
            )
            if err:
                return err
            evidence = (meta_resp.json() or {}).get("evidence") or {}
            size = evidence.get("size")
            if isinstance(size, int) and size > _MAX_DOWNLOAD_BYTES:
                return (
                    f"File evidence {evidence_id} is {round(size / 1024 / 1024, 1)} MB, "
                    f"over the {_MAX_DOWNLOAD_BYTES // 1024 // 1024} MB download limit.\n"
                    f"Download URL: {data_url}"
                )
            fetched = await fetch_file(
                client,
                "get",
                data_url,
                provider=_PROVIDER,
                action=f"download file evidence {evidence_id}",
                max_bytes=_MAX_DOWNLOAD_BYTES,
                headers={"session": api_token, "Accept": "*/*"},
            )
        if isinstance(fetched, str):
            return f"{fetched}\nDownload URL: {data_url}"
        original = str(evidence.get("path") or evidence_id).replace("\\", "/").rsplit("/", 1)[-1]
        details = [
            f"Evidence: {evidence.get('path', '?')} on {evidence.get('hostname', '?')} "
            f"(hash {evidence.get('hash', '?')})"
        ]
        return await deliver_live_artefact(
            data=fetched.data,
            filename=f"{original or evidence_id}.zip",
            source=_PROVIDER,
            persist_file_fn=persist_file_fn,
            links=[("Download URL", data_url)],
            details=details,
        )

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("get_tanium_session_info", get_tanium_session_info)
    executor.register("list_tanium_alerts", list_tanium_alerts)
    executor.register("get_tanium_alert", get_tanium_alert)
    executor.register("update_tanium_alert_state", update_tanium_alert_state)
    executor.register("list_tanium_intel_docs", list_tanium_intel_docs)
    executor.register("get_tanium_intel_doc", get_tanium_intel_doc)
    executor.register("create_tanium_intel_doc", create_tanium_intel_doc)
    executor.register("deploy_tanium_intel", deploy_tanium_intel)
    executor.register("ask_tanium_question", ask_tanium_question)
    executor.register("get_tanium_question_results", get_tanium_question_results)
    executor.register("parse_tanium_question", parse_tanium_question)
    executor.register("list_tanium_saved_questions", list_tanium_saved_questions)
    executor.register("ask_tanium_saved_question", ask_tanium_saved_question)
    executor.register("list_tanium_sensors", list_tanium_sensors)
    executor.register("list_tanium_endpoints", list_tanium_endpoints)
    executor.register("run_tanium_graphql", run_tanium_graphql)
    executor.register("list_tanium_computer_groups", list_tanium_computer_groups)
    executor.register("list_tanium_packages", list_tanium_packages)
    executor.register("list_tanium_action_groups", list_tanium_action_groups)
    executor.register("deploy_tanium_action", deploy_tanium_action)
    executor.register("list_tanium_actions", list_tanium_actions)
    executor.register("get_tanium_action", get_tanium_action)
    executor.register("list_tanium_connections", list_tanium_connections)
    executor.register("create_tanium_connection", create_tanium_connection)
    executor.register("list_tanium_response_actions", list_tanium_response_actions)
    executor.register("list_tanium_file_evidence", list_tanium_file_evidence)
    executor.register("download_tanium_file_evidence", download_tanium_file_evidence)

    return [
        # ── 0. Connection ──────────────────────────────────────────────────
        ToolDefinition(
            name="get_tanium_session_info",
            description=(
                "Validate the Tanium connection and return the authenticated "
                "user, id, and roles. Use this first if other Tanium tools fail."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        # ── 1. Threat Response — Alerts ────────────────────────────────────
        ToolDefinition(
            name="list_tanium_alerts",
            description=(
                "List Tanium Threat Response alerts (endpoint detections / "
                "incidents). Filter by state (Unresolved, In Progress, Resolved, "
                "Ignored), priority, severity, intel_doc_id, computer_name, "
                "computer_ip, mitre_id (ATT&CK technique), or alert_type. Core "
                "tool for gathering incidents."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "description": "Unresolved | In Progress | Resolved | Ignored",
                    },
                    "priority": {"type": "string"},
                    "severity": {"type": "string"},
                    "intel_doc_id": {"type": "integer"},
                    "computer_name": {"type": "string"},
                    "computer_ip": {"type": "string"},
                    "mitre_id": {
                        "type": "string",
                        "description": "MITRE ATT&CK technique id, e.g. T1059",
                    },
                    "alert_type": {"type": "string"},
                    "limit": {"type": "integer", "default": 25},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_tanium_alert",
            description=(
                "Get full details of a Threat Response alert by numeric id, "
                "including the match details/evidence used for IOC extraction."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {"type": "integer", "description": "Tanium alert id"}
                },
                "required": ["alert_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="update_tanium_alert_state",
            description=(
                "Update a Threat Response alert's state. Valid states: "
                "Unresolved, In Progress, Resolved, Ignored."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {"type": "integer"},
                    "state": {
                        "type": "string",
                        "description": "Unresolved | In Progress | Resolved | Ignored",
                    },
                },
                "required": ["alert_id", "state"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 2. Threat Response — Intel ─────────────────────────────────────
        ToolDefinition(
            name="list_tanium_intel_docs",
            description=(
                "List Threat Response intel documents (detection content: YARA, "
                "STIX, OpenIOC, Tanium Signals) with their alert counts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 25},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_tanium_intel_doc",
            description=(
                "Get a Threat Response intel document by id, including its raw "
                "detection contents."
            ),
            input_schema={
                "type": "object",
                "properties": {"intel_doc_id": {"type": "integer"}},
                "required": ["intel_doc_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="create_tanium_intel_doc",
            description=(
                "Create Threat Response intel (CREATING INTELLIGENCE). Provide "
                "the raw detection content (or the file_id of a file of the "
                "conversation holding it, e.g. a .yar) and its type: 'yara', "
                "'stix', 'openioc', or 'tanium_signal'. The new intel can then be "
                "deployed to endpoints with deploy_tanium_intel."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "contents": {
                        "type": "string",
                        "description": "Raw detection content (YARA rule, STIX/OpenIOC XML, or Tanium Signal JSON)",
                    },
                    "file_id": {
                        "type": "string",
                        "description": "A file attached, generated or downloaded in this conversation holding the detection content (instead of contents)",
                    },
                    "intel_type": {
                        "type": "string",
                        "enum": [
                            "yara",
                            "stix",
                            "openioc",
                            "cybox",
                            "tanium_signal",
                            "json",
                        ],
                        "default": "yara",
                    },
                    "name": {"type": "string", "description": "Optional intel name"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="deploy_tanium_intel",
            description=(
                "Deploy/redeploy all Threat Response intel to configured "
                "endpoints so newly created detections take effect."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=1,
        ),
        # ── 3. Interact — Questions & Sensors ──────────────────────────────
        ToolDefinition(
            name="ask_tanium_question",
            description=(
                "Ask a Tanium question in Tanium question syntax and wait for "
                "endpoints to report (THREAT HUNTING across the live fleet). "
                "Example: 'Get Computer Name and IP Address from all machines "
                "with Running Processes contains \"powershell.exe\"'. Target a "
                "subset by adding 'from all machines with ...' clauses. Use "
                "parse_tanium_question first if unsure of the exact syntax."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query_text": {
                        "type": "string",
                        "description": "Question in Tanium syntax (e.g. 'Get Computer Name from all machines')",
                    },
                    "max_wait_polls": {
                        "type": "integer",
                        "description": "Optional: number of 3s poll cycles to wait for results (default 8)",
                        "default": 0,
                    },
                },
                "required": ["query_text"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_tanium_question_results",
            description="Get the latest results for an already-asked Tanium question by id.",
            input_schema={
                "type": "object",
                "properties": {"question_id": {"type": "integer"}},
                "required": ["question_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="parse_tanium_question",
            description=(
                "Parse free text into valid Tanium question-syntax suggestions "
                "(ranked). Use to convert a natural-language hunt idea into a "
                "question you can pass to ask_tanium_question."
            ),
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_tanium_saved_questions",
            description="List saved questions available in the Tanium environment.",
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 50}},
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="ask_tanium_saved_question",
            description="Run a saved question by id and return its current results.",
            input_schema={
                "type": "object",
                "properties": {"saved_question_id": {"type": "integer"}},
                "required": ["saved_question_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_tanium_sensors",
            description=(
                "List or search Tanium sensors (the data collectors that "
                "questions select). Use to discover available data points for "
                "hunting."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Filter by name/description"},
                    "limit": {"type": "integer", "default": 30},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
        # ── 4. Endpoints / Assets (Gateway) ────────────────────────────────
        ToolDefinition(
            name="list_tanium_endpoints",
            description=(
                "List endpoints / asset inventory (name, IP, OS, last seen) via "
                "the Tanium API Gateway. For name/attribute searches across the "
                "live fleet prefer ask_tanium_question."
            ),
            input_schema={
                "type": "object",
                "properties": {"first": {"type": "integer", "default": 25}},
                "required": [],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="run_tanium_graphql",
            description=(
                "Run a raw GraphQL query against the Tanium API Gateway. Use for "
                "any Gateway capability without a dedicated tool (filtered "
                "endpoint/asset queries, software inventory, etc.). 'variables' "
                "is an optional JSON object string."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "GraphQL query/mutation"},
                    "variables": {
                        "type": "string",
                        "description": "Optional JSON object string of GraphQL variables",
                    },
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=2,
        ),
        # ── 5. Computer Groups ─────────────────────────────────────────────
        ToolDefinition(
            name="list_tanium_computer_groups",
            description=(
                "List computer groups used to scope questions and target "
                "actions."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        # ── 6. Packages & Actions (remediation) ────────────────────────────
        ToolDefinition(
            name="list_tanium_packages",
            description=(
                "List Tanium packages — the deployable remediation/response "
                "content (e.g. kill process, isolate, collect). Resolve the "
                "package id before deploy_tanium_action."
            ),
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 30}},
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_tanium_action_groups",
            description="List action groups (the deployment targeting scope for actions).",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="deploy_tanium_action",
            description=(
                "Deploy a package to a computer group as a Tanium action "
                "(REMEDIATION / RESPONSE — executes content on endpoints). "
                "Requires package_id (list_tanium_packages), action_group_id "
                "(list_tanium_action_groups) and target_computer_group_id "
                "(list_tanium_computer_groups). Use deliberately."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "package_id": {"type": "integer"},
                    "action_group_id": {"type": "integer"},
                    "target_computer_group_id": {"type": "integer"},
                    "name": {"type": "string"},
                    "expire_seconds": {"type": "integer", "default": 600},
                    "package_parameters": {
                        "type": "object",
                        "description": 'Package parameter map, e.g. {"$1": "value"}',
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": [
                    "package_id",
                    "action_group_id",
                    "target_computer_group_id",
                ],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_tanium_actions",
            description="List recent Tanium actions and their deployment status.",
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 25}},
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="get_tanium_action",
            description="Get the status and details of a specific Tanium action by id.",
            input_schema={
                "type": "object",
                "properties": {"action_id": {"type": "integer"}},
                "required": ["action_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        # ── 7. Threat Response — Live response & connections ───────────────
        ToolDefinition(
            name="list_tanium_connections",
            description="List active Threat Response endpoint connections (live investigation sessions).",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="create_tanium_connection",
            description=(
                "Open a Threat Response live connection to an endpoint to enable "
                "deep investigation (timeline, file/registry/process inspection)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hostname": {"type": "string"},
                    "client_id": {"type": "string"},
                    "platform": {"type": "string", "description": "Windows | Linux | Mac"},
                    "ip": {"type": "string"},
                },
                "required": ["hostname"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_tanium_response_actions",
            description="List Threat Response live-response actions (e.g. forensic data collection).",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 25},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_tanium_file_evidence",
            description=(
                "List file evidence collected during Threat Response "
                "investigations, with each record's evidence id and download URL."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 25},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="download_tanium_file_evidence",
            workspace_ingest=True,
            description=(
                "Store one piece of Threat Response file evidence as a file of "
                "the conversation (download card + file_id to forward, e.g. to a "
                "sandbox). Evidence taken from an endpoint can be live malware: "
                "it is stored only as Tanium's ZIP encrypted with the password "
                "'infected', exactly as it came, and never read inline."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "evidence_id": {
                        "type": "string",
                        "description": "Evidence id (uuid) from list_tanium_file_evidence",
                    },
                },
                "required": ["evidence_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
