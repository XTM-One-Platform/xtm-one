# SPDX-License-Identifier: Apache-2.0

"""HackerOne integration - best-in-class agentic access to the HackerOne API.

Full-fledged HackerOne customer/program API integration covering the vulnerability
disclosure / bug bounty / pentest / attack-surface-management lifecycle, designed
as a building block for an agentic security team.

Tool categories:
  - Reports: query/list (rich filters), get, **generate & download a report PDF**,
    create (import external findings), comment (public/internal activity), change
    state, assign, set severity, award bounty, plus a consolidated ``manage`` tool
    for the long tail (tags, inboxes, participants, retests, disclosure, redact,
    CVEs, custom fields, structured scope, transfer, summaries, escalation, ...).
  - Activities: get one, incremental activity feed (audit-style streaming).
  - Programs: list mine, get, weaknesses, structured scopes, members, common
    responses, billing balance & transactions, plus a consolidated ``manage`` tool
    (policy, external bounty, team messages, swag, thanks, audit log, bounty table,
    CVE requests, allowed reporters, hacker invitations, scope exclusions,
    campaigns, ...).
  - Organizations: list mine, list programs, plus a consolidated ``manage`` tool
    (members, groups, inboxes, audit log, eligibility settings, invitations, ...).
  - Assets (ASM): list, get, plus a consolidated ``manage`` tool (create/update/
    archive, scopes, tags, imports, ports, reachability, scanner config, ...).
  - Automations: a consolidated ``manage`` tool (list/get/create/update, runs,
    run logs, trigger).
  - Findings workboards & views: a consolidated ``manage`` tool (CRUD + view reports).
  - Analytics: query predefined analytics data.
  - Users: look up a user by username.

Authentication is **HTTP Basic** using an API token: the *API identifier*
(username) and the *API token value* (password). The integration stores the
token value in ``api_key`` and the identifier in ``extra_config.api_username``;
``integration_tools._get_token`` assembles the compound
``api_username||api_token`` that this builder parses.

The report-PDF tool persists the generated PDF as a real downloadable file
attachment (via the injected ``persist_file_fn``) and returns a ``[[FILE:id]]``
marker so it renders as a download card in chat. When no persistence callback is
available (older callers / channel-bot mode) it gracefully falls back to an
inline file reference so the LLM can still read the PDF.
"""

from __future__ import annotations

import base64
import json as _json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import (
    format_entity_list,
    format_network_error,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_PROVIDER = "HackerOne"
_BASE_URL = "https://api.hackerone.com/v1"
_DEFAULT_TIMEOUT = 30
_PDF_TIMEOUT = 120
_MAX_PAGE_SIZE = 100

# Persist callback signature: (filename, raw_bytes, content_type) -> FileRef
PersistFileFn = Callable[[str, bytes, str], Awaitable[Any]]

_STATE_ICONS: dict[str, str] = {
    "new": "🆕",
    "triaged": "🩺",
    "needs-more-info": "❓",
    "resolved": "✅",
    "not-applicable": "⚪",
    "informative": "ℹ️",
    "duplicate": "🔁",
    "spam": "🗑️",
    "retesting": "🔬",
}

_SEVERITY_ICONS: dict[str, str] = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🔵",
    "none": "⚪",
}


# ---------------------------------------------------------------------------
# Auth + formatting helpers
# ---------------------------------------------------------------------------


def _basic_auth_header(api_username: str, api_token: str) -> str:
    """Build the HTTP Basic ``Authorization`` header value for the API token.

    The identifier and token are stripped: a value pasted with a trailing
    newline/space would otherwise fail authentication.
    """
    raw = f"{api_username.strip()}:{api_token.strip()}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _is_unsafe_path_id(value: Any) -> bool:
    """True if *value* would alter the URL path structure if interpolated.

    Caller-supplied ids (report/program/org/... ids, usernames) are dropped into
    request paths, so an id containing a path separator or a ``.``/``..``
    traversal token could escape the intended endpoint. Reject those (and empty)
    so a crafted/hallucinated id can't widen what a tool calls.
    """
    s = str(value or "").strip()
    return not s or s in (".", "..") or any(c in s for c in "/\\\x00")


def _trunc(text: Any, max_len: int = 200) -> str:
    if not text:
        return ""
    text = str(text)
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _dt(value: Any) -> str:
    """Trim an ISO timestamp to minute precision for display."""
    return (str(value) if value else "")[:16] or "N/A"


def _attrs(obj: dict) -> dict:
    return obj.get("attributes") or {} if isinstance(obj, dict) else {}


def _rels(obj: dict) -> dict:
    return obj.get("relationships") or {} if isinstance(obj, dict) else {}


def _rel_attr(obj: dict, rel: str, attr: str, default: str = "") -> str:
    """Pull a single attribute out of a JSON:API relationship (data.attributes)."""
    node = (_rels(obj).get(rel) or {}).get("data") or {}
    return str(_attrs(node).get(attr, default) or default)


def _fmt_report(obj: dict) -> str:
    a = _attrs(obj)
    state = (a.get("state") or "?").lower()
    sev = _rel_attr(obj, "severity", "rating").lower()
    program = _rel_attr(obj, "program", "handle") or _rel_attr(obj, "team", "handle")
    bits = [
        f"  {_STATE_ICONS.get(state, '•')} #{obj.get('id', '?')} [{state}] "
        f"{_SEVERITY_ICONS.get(sev, '') if sev else ''}{(' ' + sev) if sev else ''} "
        f"— {_trunc(a.get('title'), 120) or 'Untitled'}".rstrip(),
    ]
    meta = []
    if program:
        meta.append(f"program: {program}")
    reporter = _rel_attr(obj, "reporter", "username")
    if reporter:
        meta.append(f"reporter: {reporter}")
    if a.get("created_at"):
        meta.append(f"created: {_dt(a.get('created_at'))}")
    if a.get("last_activity_at"):
        meta.append(f"last activity: {_dt(a.get('last_activity_at'))}")
    if meta:
        bits.append("    " + " | ".join(meta))
    return "\n".join(bits)


def _fmt_generic(noun: str) -> Callable[[dict], str]:
    """Generic JSON:API object formatter: id + a few salient attributes."""

    def _f(obj: dict) -> str:
        a = _attrs(obj)
        label = (
            a.get("name")
            or a.get("handle")
            or a.get("title")
            or a.get("username")
            or a.get("identifier")
            or a.get("email")
            or obj.get("type")
            or noun
        )
        extra = []
        for key in ("state", "status", "asset_type", "type", "permissions"):
            if key in a and a[key] not in (None, ""):
                extra.append(f"{key}={a[key]}")
        tail = f"  ({', '.join(extra)})" if extra else ""
        return f"  • [{obj.get('id', '?')}] {_trunc(label, 120)}{tail}"

    return _f


def _format_object(obj: dict, *, indent: str = "") -> str:
    """Render a single JSON:API object as a compact, readable block."""
    if not isinstance(obj, dict):
        return str(obj)
    lines = [f"{indent}id: {obj.get('id', '?')}  type: {obj.get('type', '?')}"]
    for key, value in (_attrs(obj)).items():
        if value in (None, "", [], {}):
            continue
        value = (
            _trunc(_json.dumps(value, default=str), 400)
            if isinstance(value, dict | list)
            else _trunc(value, 400)
        )
        lines.append(f"{indent}  {key}: {value}")
    rels = _rels(obj)
    if rels:
        rel_names = ", ".join(sorted(rels.keys()))
        lines.append(f"{indent}  relationships: {rel_names}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Connectivity validation (used by the integration test endpoint via
# ``test_provider_connectivity``; kept here for completeness/reuse).
# ---------------------------------------------------------------------------


async def validate_connection(api_username: str, api_token: str) -> tuple[bool, str]:
    """Verify HackerOne credentials by listing the programs the token can access."""
    if not api_username or not api_token:
        return False, "Missing API username (identifier) or API token."
    headers = {
        "Authorization": _basic_auth_header(api_username, api_token),
        "Accept": "application/json",
    }
    try:
        async with egress_client(timeout=20) as client:
            resp = await client.get(f"{_BASE_URL}/me/programs", headers=headers)
    except httpx.HTTPError as exc:
        return False, f"Connection failed: {exc}"
    if resp.status_code == 200:
        return True, "HackerOne is connected and authenticated."
    if resp.status_code in (401, 403):
        return False, (
            "Authentication failed — check the API username (token identifier, "
            "NOT your email) and the API token value."
        )
    return False, f"HackerOne returned HTTP {resp.status_code}."


class HackerOneIntegration(BaseIntegration):
    """HackerOne vulnerability disclosure / bug bounty platform integration."""

    provider_name = "hackerone"
    display_name = "HackerOne"
    icon = "ShieldCheck"
    description = (
        "Connect HackerOne to triage vulnerability reports, download report PDFs, "
        "manage programs, assets, and bounties across the disclosure lifecycle."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        api_username, api_token = parse_compound_token(token, 2)
        ok, _ = await validate_connection(api_username, api_token)
        return ok

    async def get_user_info(self, token: str) -> dict:
        api_username, api_token = parse_compound_token(token, 2)
        headers = {
            "Authorization": _basic_auth_header(api_username, api_token),
            "Accept": "application/json",
        }
        async with egress_client(timeout=20) as client:
            resp = await client.get(f"{_BASE_URL}/me/organizations", headers=headers)
            resp.raise_for_status()
            return resp.json()


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    *,
    persist_file_fn: PersistFileFn | None = None,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build HackerOne tools.

    ``token`` is the compound ``api_username||api_token`` assembled by
    ``integration_tools._get_token`` from the integration's stored API token
    identifier (username) and value. ``persist_file_fn`` (injected by the
    integration-tools loop) persists generated files — used by the report-PDF
    download tool to surface a real downloadable artifact.
    """
    api_username, api_token = parse_compound_token(token, 2)
    auth_header = _basic_auth_header(api_username, api_token)

    def _headers(
        accept: str = "application/json", *, with_body: bool = False
    ) -> dict[str, str]:
        """Build request headers.

        ``Content-Type: application/json`` is attached **only** when a JSON body
        is actually sent (``with_body``). Sending it on a bodyless GET is
        meaningless and a strict ``Accept`` is what triggers the report-PDF 406,
        so headers stay minimal unless there is a payload.
        """
        headers = {"Authorization": auth_header, "Accept": accept}
        if with_body:
            headers["Content-Type"] = "application/json"
        return headers

    async def _api(
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> tuple[Any, str | None]:
        """Authenticated JSON request helper returning ``(json, error)``."""
        # Central traversal backstop: never send a path with a '.'/'..' segment,
        # so a caller-supplied id interpolated into the path can't escape the
        # intended endpoint (defends every tool that routes through _api).
        if any(seg in (".", "..") for seg in path.split("/")):
            return None, (
                f"Error: refusing to call '{action}' — the request path contains "
                "a traversal segment (check the id arguments)."
            )
        # Only attach a body (and its Content-Type) when there is one. Forwarding
        # ``json=None`` to a GET previously crashed with
        # ``AsyncClient.get() got an unexpected keyword argument 'json'``; a body
        # is now sent only for write verbs that actually carry one, while bodied
        # DELETEs (e.g. cancel-disclosure) ride through ``safe_request``'s
        # ``request()`` dispatch.
        has_body = json is not None
        request_kwargs: dict[str, Any] = {"headers": _headers(with_body=has_body)}
        if params is not None:
            request_kwargs["params"] = params
        if has_body:
            request_kwargs["json"] = json
        async with egress_client(timeout=timeout) as client:
            resp, err = await safe_request(
                client,
                method,
                f"{_BASE_URL}{path}",
                _PROVIDER,
                action,
                **request_kwargs,
            )
            if err:
                return None, err
            try:
                return resp.json(), None
            except Exception:
                # Empty body (e.g. 204) — treat as success with no payload.
                return {}, None

    def _render_list(data: Any, formatter: Callable[[dict], str], noun: str) -> str:
        """Render a JSON:API list response with pagination hints."""
        items = (data or {}).get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return f"No {noun}s found."
        out = format_entity_list(items, formatter, noun=noun, total=len(items))
        links = (data or {}).get("links") or {}
        if links.get("next"):
            out += "\n\n(More results available — increase page_number to paginate.)"
        return out

    def _render_one(data: Any, noun: str) -> str:
        obj = (data or {}).get("data") if isinstance(data, dict) else None
        if not isinstance(obj, dict):
            return f"{noun} not found."
        return _format_object(obj)

    def _page_params(page_size: int, page_number: int) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if page_size:
            params["page[size]"] = max(1, min(page_size, _MAX_PAGE_SIZE))
        if page_number:
            params["page[number]"] = max(1, page_number)
        return params

    # ══════════════════════════════════════════════════════════════════════
    # REPORTS
    # ══════════════════════════════════════════════════════════════════════

    async def list_hackerone_reports(
        program: str = "",
        state: str = "",
        severity: str = "",
        assignee: str = "",
        keyword: str = "",
        created_after: str = "",
        created_before: str = "",
        last_activity_after: str = "",
        sort: str = "last_activity_at",
        order: str = "desc",
        page_size: int = 25,
        page_number: int = 1,
    ) -> str:
        """List/query vulnerability reports with rich filters.

        ``program`` accepts one or more comma-separated program handles. ``state``
        and ``severity`` accept comma-separated values (e.g. ``new,triaged``).
        """
        params: dict[str, Any] = _page_params(page_size, page_number)
        for raw, key in (
            (program, "filter[program][]"),
            (state, "filter[state][]"),
            (severity, "filter[severity][]"),
        ):
            values = [v.strip() for v in str(raw).split(",") if v.strip()]
            if values:
                params[key] = values
        if assignee:
            params["filter[assignee][]"] = [
                v.strip() for v in assignee.split(",") if v.strip()
            ]
        if keyword:
            params["filter[keyword]"] = keyword
        if created_after:
            params["filter[created_at__gt]"] = created_after
        if created_before:
            params["filter[created_at__lt]"] = created_before
        if last_activity_after:
            params["filter[last_activity_at__gt]"] = last_activity_after
        if sort:
            direction = "-" if (order or "").lower() == "desc" else ""
            params["sort"] = f"{direction}reports.{sort}"
        if "filter[program][]" not in params:
            return (
                "A program handle is required to list reports. Use "
                "list_hackerone_programs to find your program handles, then pass "
                "one (or several, comma-separated) as `program`."
            )
        data, err = await _api("get", "/reports", "list reports", params=params)
        if err:
            return err
        return _render_list(data, _fmt_report, "report")

    async def get_hackerone_report(report_id: str) -> str:
        """Get full details of a single vulnerability report by its numeric ID."""
        data, err = await _api(
            "get", f"/reports/{report_id}", f"get report {report_id}"
        )
        if err:
            return err
        return _render_one(data, "Report")

    async def download_hackerone_report_pdf(
        report_id: str,
        pdf_type: str = "full",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Generate and download a report as a PDF.

        Calls ``GET /reports/{id}/pdf`` and persists the PDF as a downloadable
        file attachment so the user gets a proper download card. ``pdf_type``
        selects which rendering of the report to generate: ``reporter`` (the
        reporter-facing view), ``full`` (the complete report — best for evidence
        collection), or ``triage``.
        """
        from app.llm.base import FileRef

        # This tool builds its own request URL (not via _api), so validate the
        # id here too: reject path separators / traversal so it can't escape the
        # /reports/{id}/pdf endpoint.
        if _is_unsafe_path_id(report_id):
            return (
                "Error: report_id must be a plain report id (no path separators "
                "or traversal sequences)."
            )
        pdf_type = (pdf_type or "full").strip().lower()
        if pdf_type not in ("reporter", "full", "triage"):
            return (
                "Error: pdf_type must be one of 'reporter', 'full', or 'triage'."
            )
        url = f"{_BASE_URL}/reports/{report_id}/pdf"
        # A strict ``Accept: application/pdf`` makes HackerOne answer HTTP 406
        # (the endpoint redirects to a pre-signed download whose own response is
        # not ``application/pdf``). Send a permissive Accept — matching the
        # official API samples — so content negotiation always succeeds.
        pdf_headers = {"Authorization": auth_header, "Accept": "application/pdf, */*"}
        try:
            # Do NOT auto-follow redirects while the Basic-auth header is set: a
            # PDF download commonly 3xx-redirects to a pre-signed CDN/S3 URL, and
            # forwarding the HackerOne token to a third-party host would leak the
            # credential. Follow a single redirect manually WITHOUT auth headers
            # (the pre-signed URL carries its own authorization).
            async with egress_client(timeout=_PDF_TIMEOUT) as client:
                resp = await client.get(
                    url, headers=pdf_headers, params={"pdf_type": pdf_type}
                )
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        return (
                            "HackerOne returned a redirect without a location "
                            f"for the report #{report_id} PDF."
                        )
                    resp = await client.get(
                        httpx.URL(url).join(location), follow_redirects=True
                    )
        except httpx.HTTPError as exc:
            return format_network_error(exc, _PROVIDER, "download report PDF")
        if resp.status_code == 404:
            return (
                f"Report #{report_id} was not found, or your API token lacks "
                "permission to access it."
            )
        if resp.status_code in (401, 403):
            return (
                "HackerOne denied the PDF request (HTTP "
                f"{resp.status_code}). The API token needs report access on the "
                "program that owns this report."
            )
        if resp.status_code == 406:
            return (
                f"HackerOne could not produce a '{pdf_type}' PDF for report "
                f"#{report_id} (HTTP 406 Not Acceptable). Try pdf_type='reporter'."
            )
        if not resp.is_success:
            return f"HackerOne returned HTTP {resp.status_code} for the report PDF."
        data = resp.content
        if not data:
            return f"HackerOne returned an empty PDF for report #{report_id}."

        filename = f"hackerone-report-{report_id}.pdf"
        file_ref = None
        persisted = False
        if persist_file_fn is not None:
            try:
                file_ref = await persist_file_fn(filename, data, "application/pdf")
                persisted = file_ref is not None
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("HackerOne PDF persistence failed: %s", format_exception_message(exc))
                file_ref = None
        if file_ref is None:
            # Fallback: inline reference (readable by the LLM, no download card).
            file_ref = FileRef.from_bytes(
                data=data, filename=filename, content_type="application/pdf"
            )
        if file_context is not None:
            file_context.add(file_ref)

        size_kb = max(1, len(data) // 1024)
        links = (
            f"Web URL: https://hackerone.com/reports/{report_id}\n"
            f"file_id: {file_ref.file_id} - forward it to any tool that takes a file."
        )
        if persisted:
            content = (
                f"Generated the PDF for HackerOne report #{report_id} "
                f"({size_kb:,} KB). [[FILE:{file_ref.file_id}]]\n{links}\n\n"
                "Include the [[FILE:...]] marker verbatim in your reply so the "
                "user gets a download card."
            )
        else:
            content = (
                f"Generated the PDF for HackerOne report #{report_id} "
                f"({size_kb:,} KB). It is attached to this conversation for "
                f"analysis.\n{links}"
            )
        return ToolResult(tool_call_id="", content=content, files=[file_ref])

    async def create_hackerone_report(
        team_handle: str,
        title: str,
        vulnerability_information: str,
        impact: str,
        severity_rating: str = "",
        weakness_id: str = "",
        structured_scope_id: str = "",
        source: str = "",
    ) -> str:
        """Create (import) a report into a program (e.g. an external finding)."""
        attributes: dict[str, Any] = {
            "team_handle": team_handle,
            "title": title,
            "vulnerability_information": vulnerability_information,
            "impact": impact,
        }
        if severity_rating:
            attributes["severity_rating"] = severity_rating
        if weakness_id:
            attributes["weakness_id"] = weakness_id
        if structured_scope_id:
            attributes["structured_scope_id"] = structured_scope_id
        if source:
            attributes["source"] = source
        data, err = await _api(
            "post",
            "/reports",
            "create report",
            json={"data": {"type": "report", "attributes": attributes}},
        )
        if err:
            return err
        return "Report created:\n" + _render_one(data, "Report")

    async def comment_hackerone_report(
        report_id: str, message: str, internal: bool = True
    ) -> str:
        """Post a comment/activity on a report (internal by default, or public)."""
        _data, err = await _api(
            "post",
            f"/reports/{report_id}/activities",
            "comment on report",
            json={
                "data": {
                    "type": "activity-comment",
                    "attributes": {"message": message, "internal": bool(internal)},
                }
            },
        )
        if err:
            return err
        scope = "internal" if internal else "public"
        return f"Posted {scope} comment on report #{report_id}."

    async def change_hackerone_report_state(
        report_id: str, state: str, message: str = ""
    ) -> str:
        """Change a report's state (e.g. triaged, resolved, informative, duplicate)."""
        attributes: dict[str, Any] = {"state": state}
        if message:
            attributes["message"] = message
        _data, err = await _api(
            "post",
            f"/reports/{report_id}/state_changes",
            "change report state",
            json={"data": {"type": "state-change", "attributes": attributes}},
        )
        if err:
            return err
        return f"Report #{report_id} state changed to '{state}'."

    async def assign_hackerone_report(
        report_id: str,
        assignee_type: str = "user",
        assignee_id: str = "",
        message: str = "",
    ) -> str:
        """Assign a report to a user or group (``assignee_type`` = user|group|nobody)."""
        body: dict[str, Any] = {"data": {"type": assignee_type}}
        if assignee_id:
            body["data"]["id"] = assignee_id
        if message:
            body["message"] = message
        _data, err = await _api(
            "put", f"/reports/{report_id}/assignee", "assign report", json=body
        )
        if err:
            return err
        return f"Report #{report_id} assigned ({assignee_type} {assignee_id})."

    async def award_hackerone_bounty(
        report_id: str,
        amount: str,
        bonus_amount: str = "",
        message: str = "",
    ) -> str:
        """Award a bounty on a report (amount in the program's currency)."""
        body: dict[str, Any] = {"data": {"amount": str(amount)}}
        if bonus_amount:
            body["data"]["bonus_amount"] = str(bonus_amount)
        if message:
            body["data"]["message"] = message
        _data, err = await _api(
            "post", f"/reports/{report_id}/bounties", "award bounty", json=body
        )
        if err:
            return err
        return f"Awarded a bounty of {amount} on report #{report_id}."

    # ══════════════════════════════════════════════════════════════════════
    # PROGRAMS
    # ══════════════════════════════════════════════════════════════════════

    async def list_hackerone_programs() -> str:
        """List the programs the API token can access (with their IDs and handles)."""
        data, err = await _api("get", "/me/programs", "list programs")
        if err:
            return err
        return _render_list(data, _fmt_generic("program"), "program")

    async def get_hackerone_program(program_id: str) -> str:
        """Get a program's details (members, groups, custom fields, settings)."""
        data, err = await _api(
            "get", f"/programs/{program_id}", f"get program {program_id}"
        )
        if err:
            return err
        return _render_one(data, "Program")

    async def list_hackerone_program_weaknesses(program_id: str) -> str:
        """List the weakness (CWE) types configured for a program."""
        data, err = await _api(
            "get", f"/programs/{program_id}/weaknesses", "list weaknesses"
        )
        if err:
            return err
        return _render_list(data, _fmt_generic("weakness"), "weakness")

    async def list_hackerone_structured_scopes(program_id: str) -> str:
        """List a program's structured scopes (assets in scope)."""
        data, err = await _api(
            "get", f"/programs/{program_id}/structured_scopes", "list structured scopes"
        )
        if err:
            return err
        return _render_list(data, _fmt_generic("scope"), "structured scope")

    async def list_hackerone_program_members(program_id: str) -> str:
        """List the organization members who have access to a program."""
        data, err = await _api(
            "get", f"/programs/{program_id}/members", "list program members"
        )
        if err:
            return err
        return _render_list(data, _fmt_generic("member"), "member")

    async def list_hackerone_common_responses(program_id: str) -> str:
        """List a program's saved common (canned) responses."""
        data, err = await _api(
            "get", f"/programs/{program_id}/common_responses", "list common responses"
        )
        if err:
            return err
        return _render_list(data, _fmt_generic("response"), "common response")

    async def get_hackerone_program_balance(program_id: str) -> str:
        """Get a program's available balance for bounty payments."""
        data, err = await _api(
            "get", f"/programs/{program_id}/billing/balance", "get balance"
        )
        if err:
            return err
        return _render_one(data, "Balance")

    async def list_hackerone_program_transactions(
        program_id: str, month: int = 0, year: int = 0
    ) -> str:
        """List a program's billing transactions, optionally for a given month/year."""
        params: dict[str, Any] = {}
        if month:
            params["month"] = month
        if year:
            params["year"] = year
        data, err = await _api(
            "get",
            f"/programs/{program_id}/billing/transactions",
            "list transactions",
            params=params or None,
        )
        if err:
            return err
        return _render_list(data, _fmt_generic("transaction"), "transaction")

    # ══════════════════════════════════════════════════════════════════════
    # ORGANIZATIONS & ASSETS (ASM)
    # ══════════════════════════════════════════════════════════════════════

    async def list_hackerone_organizations() -> str:
        """List the organizations the API token can access (with their IDs)."""
        data, err = await _api("get", "/me/organizations", "list organizations")
        if err:
            return err
        return _render_list(data, _fmt_generic("organization"), "organization")

    async def list_hackerone_organization_programs(organization_id: str) -> str:
        """List all programs that belong to an organization."""
        data, err = await _api(
            "get", f"/organizations/{organization_id}/programs", "list org programs"
        )
        if err:
            return err
        return _render_list(data, _fmt_generic("program"), "program")

    async def list_hackerone_assets(
        organization_id: str,
        state: str = "",
        page_size: int = 25,
        page_number: int = 1,
    ) -> str:
        """List an organization's assets (attack surface inventory)."""
        params = _page_params(page_size, page_number)
        if state:
            params["filter[state]"] = state
        data, err = await _api(
            "get",
            f"/organizations/{organization_id}/assets",
            "list assets",
            params=params,
        )
        if err:
            return err
        return _render_list(data, _fmt_generic("asset"), "asset")

    async def get_hackerone_asset(organization_id: str, asset_id: str) -> str:
        """Get full details of a single asset, including ports and reachability."""
        data, err = await _api(
            "get",
            f"/organizations/{organization_id}/assets/{asset_id}",
            "get asset",
        )
        if err:
            return err
        return _render_one(data, "Asset")

    # ══════════════════════════════════════════════════════════════════════
    # ANALYTICS, ACTIVITIES, USERS
    # ══════════════════════════════════════════════════════════════════════

    async def get_hackerone_analytics(
        key: str,
        start_at: str = "",
        end_at: str = "",
        interval: str = "month",
        team_id: str = "",
        organization_id: str = "",
    ) -> str:
        """Query predefined program analytics (e.g. submissions, bounty-awarded, sla_statistics).

        ``start_at``, ``end_at`` (both ``YYYY-MM-DD``) and ``organization_id`` are
        required by the API, and ``interval`` must be ``month``, ``quarter`` or
        ``year``.
        """
        interval = (interval or "month").strip().lower()
        if interval not in ("month", "quarter", "year"):
            return "Error: interval must be one of 'month', 'quarter', or 'year'."
        missing = [
            name
            for name, value in (
                ("start_at", start_at),
                ("end_at", end_at),
                ("organization_id", organization_id),
            )
            if not value
        ]
        if missing:
            return (
                "HackerOne analytics requires " + ", ".join(missing) + ". Provide an "
                "organization_id (see list_hackerone_organizations), a start_at and "
                "end_at date (YYYY-MM-DD, end is exclusive), and an interval "
                "(month, quarter, or year)."
            )
        params: dict[str, Any] = {
            "key": key,
            "interval": interval,
            "start_at": start_at,
            "end_at": end_at,
            "organization_id": organization_id,
        }
        if team_id:
            params["team_id"] = team_id
        data, err = await _api("get", "/analytics", "get analytics", params=params)
        if err:
            return err
        return "Analytics result:\n" + _trunc(
            _json.dumps(data, indent=2, default=str), 4000
        )

    async def get_hackerone_activity(activity_id: str) -> str:
        """Get a single activity (timeline event) by its ID."""
        data, err = await _api(
            "get", f"/activities/{activity_id}", "get activity"
        )
        if err:
            return err
        return _render_one(data, "Activity")

    async def list_hackerone_incremental_activities(
        handle: str,
        updated_at_after: str = "",
        report_id: str = "",
        page_size: int = 25,
        page_number: int = 1,
    ) -> str:
        """Stream a program's activities incrementally by time (audit-style feed)."""
        params: dict[str, Any] = _page_params(page_size, page_number)
        params["handle"] = handle
        if updated_at_after:
            params["updated_at_after"] = updated_at_after
        if report_id:
            params["report_id"] = report_id
        data, err = await _api(
            "get", "/incremental/activities", "list activities", params=params
        )
        if err:
            return err
        return _render_list(data, _fmt_generic("activity"), "activity")

    async def get_hackerone_user(username: str) -> str:
        """Look up a HackerOne user by username (reputation, participating programs)."""
        data, err = await _api("get", f"/users/{username}", "get user")
        if err:
            return err
        return _render_one(data, "User")

    # ══════════════════════════════════════════════════════════════════════
    # Consolidated managers (full coverage of the long-tail endpoints)
    # ══════════════════════════════════════════════════════════════════════

    async def _manage(
        action: str,
        table: dict[str, tuple[str, str, str | None]],
        ids: dict[str, str],
        *,
        attributes: dict | None,
        params: dict | None,
        raw_body: dict | None,
        noun: str,
    ) -> str:
        spec = table.get(action)
        if not spec:
            return (
                f"Unknown action '{action}'. Supported actions: "
                f"{', '.join(sorted(table))}."
            )
        method, path_tmpl, data_type = spec
        # Validate every {placeholder} id used in the path: it must be present
        # and free of path separators / '.'/'..' traversal tokens, since ids are
        # interpolated into the request URL and an unsafe id could escape the
        # intended action -> endpoint mapping.
        for key in ids:
            if "{" + key + "}" not in path_tmpl:
                continue
            raw = (ids.get(key) or "").strip()
            if not raw:
                return f"Action '{action}' requires '{key}'."
            if _is_unsafe_path_id(raw):
                return (
                    f"Error: invalid '{key}' for action '{action}' — ids must not "
                    "contain path separators or traversal sequences."
                )
        try:
            path = path_tmpl.format(**{k: (v or "").strip() for k, v in ids.items()})
        except KeyError as exc:
            return f"Missing required id {exc} for action '{action}'."
        json_body: Any = None
        if raw_body is not None:
            json_body = raw_body
        elif method in ("post", "put", "patch", "delete") and attributes is not None:
            # ``delete`` is included because some HackerOne endpoints take a body
            # on DELETE (e.g. cancel-disclosure expects a ``message``); the body
            # now rides on the verb via safe_request's ``request()`` dispatch.
            data_obj: dict[str, Any] = {"attributes": attributes}
            if data_type:
                data_obj["type"] = data_type
            json_body = {"data": data_obj}
        data, err = await _api(
            method, path, f"{noun} {action}", params=params, json=json_body
        )
        if err:
            return err
        payload = (data or {}).get("data") if isinstance(data, dict) else None
        if isinstance(payload, list):
            return _render_list(data, _fmt_generic(noun), noun)
        if isinstance(payload, dict):
            return f"'{action}' succeeded:\n" + _format_object(payload)
        return f"'{action}' completed successfully."

    report_actions: dict[str, tuple[str, str, str | None]] = {
        "update_title": ("put", "/reports/{report_id}/title", "report"),
        "set_weakness": ("put", "/reports/{report_id}/weakness", "weakness"),
        "set_tags": ("put", "/reports/{report_id}/report_tags", "report-tag"),
        "update_inboxes": ("post", "/reports/{report_id}/inboxes", None),
        "add_participant": ("post", "/reports/{report_id}/participants", "participant"),
        "request_retest": ("post", "/reports/{report_id}/retests", "retest"),
        "approve_retest": ("post", "/reports/{report_id}/retests/approve", "retest"),
        "reject_retest": ("post", "/reports/{report_id}/retests/reject", "retest"),
        "cancel_retest": ("delete", "/reports/{report_id}/retests/cancel", None),
        "request_disclosure": (
            "post",
            "/reports/{report_id}/disclosure_requests",
            "disclosure-request",
        ),
        "cancel_disclosure": (
            "delete",
            "/reports/{report_id}/disclosure_requests",
            None,
        ),
        "redact": ("put", "/reports/{report_id}/redact", "report"),
        "set_cves": ("put", "/reports/{report_id}/cves", "report"),
        "mark_ineligible_for_bounty": (
            "put",
            "/reports/{report_id}/ineligible_for_bounty",
            "report",
        ),
        "update_structured_scope": (
            "put",
            "/reports/{report_id}/structured_scope",
            "report",
        ),
        "transfer": ("put", "/reports/{report_id}/transfer", "report"),
        "update_custom_fields": (
            "post",
            "/reports/{report_id}/custom_field_values",
            "custom-field-value",
        ),
        "add_summary": ("post", "/reports/{report_id}/summaries", "summary"),
        "set_close_comments": (
            "put",
            "/reports/{report_id}/close_comments",
            "report",
        ),
        "set_issue_tracker_reference": (
            "post",
            "/reports/{report_id}/issue_tracker_reference_id",
            "issue-tracker-reference-id",
        ),
        "suggest_bounty": (
            "post",
            "/reports/{report_id}/bounty_suggestions",
            "bounty-suggestion",
        ),
        "list_bounty_suggestions": (
            "get",
            "/reports/{report_id}/bounty_suggestions",
            None,
        ),
        "award_swag": ("post", "/reports/{report_id}/swags", "swag"),
        "escalate": ("post", "/reports/{report_id}/escalate", None),
        "cancel_escalation": ("delete", "/reports/{report_id}/escalate", None),
        "delete_attachments": ("delete", "/reports/{report_id}/attachments", None),
    }

    async def manage_hackerone_report(
        report_id: str,
        action: str,
        attributes: dict | None = None,
        params: dict | None = None,
        raw_body: dict | None = None,
    ) -> str:
        """Long-tail report operations not covered by the dedicated report tools.

        Supply ``attributes`` (JSON:API attributes) for write actions, or
        ``raw_body`` to send an exact request body verbatim. See the HackerOne
        API docs for each action's payload.
        """
        return await _manage(
            action,
            report_actions,
            {"report_id": report_id},
            attributes=attributes,
            params=params,
            raw_body=raw_body,
            noun="report",
        )

    program_actions: dict[str, tuple[str, str, str | None]] = {
        "update_policy": ("put", "/programs/{program_id}/policy", "program-policy"),
        "award_external_bounty": ("post", "/programs/{program_id}/bounties", "bounty"),
        "send_team_message": (
            "post",
            "/programs/{program_id}/team_messages",
            "team-message",
        ),
        "notify_external_platform": (
            "post",
            "/programs/{program_id}/notify_external_platform",
            None,
        ),
        "list_swag": ("get", "/programs/{program_id}/swag", None),
        "list_thanks": ("get", "/programs/{program_id}/thanks", None),
        "get_audit_log": ("get", "/programs/{program_id}/audit_log", None),
        "get_bounty_table": ("get", "/programs/{program_id}/bounty_table", None),
        "list_integrations": ("get", "/programs/{program_id}/integrations", None),
        "list_triage_reviews": ("get", "/programs/{program_id}/triage_reviews", None),
        "list_reporters": ("get", "/programs/{program_id}/reporters", None),
        "list_cve_requests": ("get", "/programs/{program_id}/cve_requests", None),
        "create_cve_request": (
            "post",
            "/programs/{program_id}/cve_requests",
            "cve-request",
        ),
        "list_credential_inquiries": (
            "get",
            "/programs/{program_id}/credential_inquiries",
            None,
        ),
        "list_allowed_reporters": (
            "get",
            "/programs/{program_id}/allowed_reporters",
            None,
        ),
        "list_allowed_reporters_history": (
            "get",
            "/programs/{program_id}/allowed_reporters_history",
            None,
        ),
        "list_hacker_invitations": (
            "get",
            "/programs/{program_id}/hacker_invitations",
            None,
        ),
        "invite_hackers": (
            "post",
            "/programs/{program_id}/hacker_invitations",
            "hacker-invitation",
        ),
        "list_scope_exclusions": (
            "get",
            "/programs/{program_id}/scope_exclusions",
            None,
        ),
        "create_scope_exclusion": (
            "post",
            "/programs/{program_id}/scope_exclusions",
            "scope-exclusion",
        ),
        "update_scope_exclusion": (
            "put",
            "/programs/{program_id}/scope_exclusions/{resource_id}",
            "scope-exclusion",
        ),
        "delete_scope_exclusion": (
            "delete",
            "/programs/{program_id}/scope_exclusions/{resource_id}",
            None,
        ),
        "list_campaigns": ("get", "/programs/{program_id}/campaigns", None),
        "create_campaign": ("post", "/programs/{program_id}/campaigns", "campaign"),
        "get_campaign": (
            "get",
            "/programs/{program_id}/campaigns/{resource_id}",
            None,
        ),
        "update_campaign": (
            "patch",
            "/programs/{program_id}/campaigns/{resource_id}",
            "campaign",
        ),
        "launch_campaign": (
            "post",
            "/programs/{program_id}/campaigns/{resource_id}/launch",
            None,
        ),
        "end_campaign": (
            "post",
            "/programs/{program_id}/campaigns/{resource_id}/end_prematurely",
            None,
        ),
    }

    async def manage_hackerone_program(
        program_id: str,
        action: str,
        resource_id: str = "",
        attributes: dict | None = None,
        params: dict | None = None,
        raw_body: dict | None = None,
    ) -> str:
        """Program-level operations: policy, payouts, swag, audit log, CVE requests,
        allowed reporters, hacker invitations, scope exclusions, and campaigns.

        ``resource_id`` identifies a sub-resource (e.g. a campaign or scope
        exclusion ID) for actions that target one.
        """
        return await _manage(
            action,
            program_actions,
            {"program_id": program_id, "resource_id": resource_id},
            attributes=attributes,
            params=params,
            raw_body=raw_body,
            noun="program",
        )

    org_actions: dict[str, tuple[str, str, str | None]] = {
        "list_members": ("get", "/organizations/{organization_id}/members", None),
        "update_member": (
            "put",
            "/organizations/{organization_id}/members/{resource_id}",
            "organization-member",
        ),
        "remove_member": (
            "delete",
            "/organizations/{organization_id}/members/{resource_id}",
            None,
        ),
        "list_groups": ("get", "/organizations/{organization_id}/groups", None),
        "get_group": (
            "get",
            "/organizations/{organization_id}/groups/{resource_id}",
            None,
        ),
        "create_group": (
            "post",
            "/organizations/{organization_id}/groups",
            "organization-member-group",
        ),
        "update_group": (
            "put",
            "/organizations/{organization_id}/groups/{resource_id}",
            "organization-member-group",
        ),
        "list_inboxes": ("get", "/organizations/{organization_id}/inboxes", None),
        "get_audit_log": ("get", "/organizations/{organization_id}/audit_log", None),
        "list_eligibility_settings": (
            "get",
            "/organizations/{organization_id}/eligibility_settings",
            None,
        ),
        "get_eligibility_setting": (
            "get",
            "/organizations/{organization_id}/eligibility_settings/{resource_id}",
            None,
        ),
        "list_pending_invitations": (
            "get",
            "/organizations/{organization_id}/pending_invitations",
            None,
        ),
        "create_invitation": (
            "post",
            "/organizations/{organization_id}/invitations",
            "invitation-organization-member",
        ),
        "get_user_by_id": ("get", "/user_by_id/{resource_id}", None),
    }

    async def manage_hackerone_organization(
        organization_id: str,
        action: str,
        resource_id: str = "",
        attributes: dict | None = None,
        params: dict | None = None,
        raw_body: dict | None = None,
    ) -> str:
        """Organization administration: members, groups, inboxes, audit log,
        eligibility settings, and invitations."""
        return await _manage(
            action,
            org_actions,
            {"organization_id": organization_id, "resource_id": resource_id},
            attributes=attributes,
            params=params,
            raw_body=raw_body,
            noun="organization",
        )

    asset_actions: dict[str, tuple[str, str, str | None]] = {
        "create_asset": ("post", "/organizations/{organization_id}/assets", "asset"),
        "update_asset": (
            "put",
            "/organizations/{organization_id}/assets/{asset_id}",
            "asset",
        ),
        "archive_assets": (
            "post",
            "/organizations/{organization_id}/assets/archive",
            None,
        ),
        "add_to_scope": (
            "post",
            "/organizations/{organization_id}/assets/{asset_id}/scopes",
            "structured-scope",
        ),
        "archive_scope": (
            "post",
            "/organizations/{organization_id}/assets/{asset_id}/scopes/archive",
            None,
        ),
        "update_scope": (
            "put",
            "/organizations/{organization_id}/assets/{asset_id}/scopes/{resource_id}",
            "structured-scope",
        ),
        "list_tags": ("get", "/organizations/{organization_id}/asset_tags", None),
        "list_tag_categories": (
            "get",
            "/organizations/{organization_id}/asset_tag_categories",
            None,
        ),
        "create_tag": (
            "post",
            "/organizations/{organization_id}/asset_tags",
            "asset-tag",
        ),
        "create_tag_category": (
            "post",
            "/organizations/{organization_id}/asset_tag_categories",
            "asset-tag-category",
        ),
        "list_ports": (
            "get",
            "/organizations/{organization_id}/assets/{asset_id}/ports",
            None,
        ),
        "add_ports": (
            "post",
            "/organizations/{organization_id}/assets/{asset_id}/ports",
            "asset-port",
        ),
        "delete_port": (
            "delete",
            "/organizations/{organization_id}/assets/{asset_id}/ports/{resource_id}",
            None,
        ),
        "get_reachability": (
            "get",
            "/organizations/{organization_id}/assets/{asset_id}/reachability",
            None,
        ),
        "check_reachability": (
            "post",
            "/organizations/{organization_id}/assets/{asset_id}/reachability/check",
            None,
        ),
        "get_scanner": (
            "get",
            "/organizations/{organization_id}/assets/{asset_id}/scanner",
            None,
        ),
        "update_scanner": (
            "patch",
            "/organizations/{organization_id}/assets/{asset_id}/scanner",
            "asset-scanner",
        ),
        "get_asset_import": (
            "get",
            "/organizations/{organization_id}/asset_imports/{resource_id}",
            None,
        ),
    }

    async def manage_hackerone_assets(
        organization_id: str,
        action: str,
        asset_id: str = "",
        resource_id: str = "",
        attributes: dict | None = None,
        params: dict | None = None,
        raw_body: dict | None = None,
    ) -> str:
        """Attack-surface management: create/update/archive assets, manage scopes,
        tags, ports, reachability checks, and scanner configuration."""
        return await _manage(
            action,
            asset_actions,
            {
                "organization_id": organization_id,
                "asset_id": asset_id,
                "resource_id": resource_id,
            },
            attributes=attributes,
            params=params,
            raw_body=raw_body,
            noun="asset",
        )

    automation_actions: dict[str, tuple[str, str, str | None]] = {
        "list": ("get", "/organizations/{organization_id}/automations", None),
        "get": (
            "get",
            "/organizations/{organization_id}/automations/{automation_id}",
            None,
        ),
        "create": (
            "post",
            "/organizations/{organization_id}/automations",
            "automation",
        ),
        "update": (
            "patch",
            "/organizations/{organization_id}/automations/{automation_id}",
            "automation",
        ),
        "list_runs": (
            "get",
            "/organizations/{organization_id}/automations/{automation_id}/runs",
            None,
        ),
        "get_run": (
            "get",
            "/organizations/{organization_id}/automations/{automation_id}/runs/{resource_id}",
            None,
        ),
        "get_run_logs": (
            "get",
            "/organizations/{organization_id}/automations/{automation_id}/runs/{resource_id}/logs",
            None,
        ),
        "trigger": (
            "post",
            "/organizations/{organization_id}/automations/{automation_id}/runs",
            "automation-run",
        ),
    }

    async def manage_hackerone_automations(
        organization_id: str,
        action: str,
        automation_id: str = "",
        resource_id: str = "",
        attributes: dict | None = None,
        params: dict | None = None,
        raw_body: dict | None = None,
    ) -> str:
        """Manage organization automations: list/get/create/update, inspect runs and
        run logs, and trigger a run. ``resource_id`` is the run ID for run actions."""
        return await _manage(
            action,
            automation_actions,
            {
                "organization_id": organization_id,
                "automation_id": automation_id,
                "resource_id": resource_id,
            },
            attributes=attributes,
            params=params,
            raw_body=raw_body,
            noun="automation",
        )

    findings_actions: dict[str, tuple[str, str, str | None]] = {
        "list_workboards": (
            "get",
            "/organizations/{organization_id}/findings/workboards",
            None,
        ),
        "get_workboard": (
            "get",
            "/organizations/{organization_id}/findings/workboards/{workboard_id}",
            None,
        ),
        "create_workboard": (
            "post",
            "/organizations/{organization_id}/findings/workboards",
            "workboard",
        ),
        "update_workboard": (
            "patch",
            "/organizations/{organization_id}/findings/workboards/{workboard_id}",
            "workboard",
        ),
        "delete_workboard": (
            "delete",
            "/organizations/{organization_id}/findings/workboards/{workboard_id}",
            None,
        ),
        "list_views": (
            "get",
            "/organizations/{organization_id}/findings/workboards/{workboard_id}/views",
            None,
        ),
        "get_view": (
            "get",
            "/organizations/{organization_id}/findings/workboards/{workboard_id}/views/{resource_id}",
            None,
        ),
        "create_view": (
            "post",
            "/organizations/{organization_id}/findings/workboards/{workboard_id}/views",
            "view",
        ),
        "update_view": (
            "patch",
            "/organizations/{organization_id}/findings/workboards/{workboard_id}/views/{resource_id}",
            "view",
        ),
        "delete_view": (
            "delete",
            "/organizations/{organization_id}/findings/workboards/{workboard_id}/views/{resource_id}",
            None,
        ),
        "get_view_reports": (
            "get",
            "/organizations/{organization_id}/findings/workboards/{workboard_id}/views/{resource_id}/reports",
            None,
        ),
    }

    async def manage_hackerone_findings(
        organization_id: str,
        action: str,
        workboard_id: str = "",
        resource_id: str = "",
        attributes: dict | None = None,
        params: dict | None = None,
        raw_body: dict | None = None,
    ) -> str:
        """Manage findings workboards and views (CRUD) and run a view's report query.

        ``resource_id`` is the view ID for view actions."""
        return await _manage(
            action,
            findings_actions,
            {
                "organization_id": organization_id,
                "workboard_id": workboard_id,
                "resource_id": resource_id,
            },
            attributes=attributes,
            params=params,
            raw_body=raw_body,
            noun="finding",
        )

    # ── Registration ──────────────────────────────────────────────────────
    executor.register("list_hackerone_reports", list_hackerone_reports)
    executor.register("get_hackerone_report", get_hackerone_report)
    executor.register("download_hackerone_report_pdf", download_hackerone_report_pdf)
    executor.register("create_hackerone_report", create_hackerone_report)
    executor.register("comment_hackerone_report", comment_hackerone_report)
    executor.register("change_hackerone_report_state", change_hackerone_report_state)
    executor.register("assign_hackerone_report", assign_hackerone_report)
    executor.register("award_hackerone_bounty", award_hackerone_bounty)
    executor.register("manage_hackerone_report", manage_hackerone_report)
    executor.register("list_hackerone_programs", list_hackerone_programs)
    executor.register("get_hackerone_program", get_hackerone_program)
    executor.register(
        "list_hackerone_program_weaknesses", list_hackerone_program_weaknesses
    )
    executor.register(
        "list_hackerone_structured_scopes", list_hackerone_structured_scopes
    )
    executor.register("list_hackerone_program_members", list_hackerone_program_members)
    executor.register(
        "list_hackerone_common_responses", list_hackerone_common_responses
    )
    executor.register("get_hackerone_program_balance", get_hackerone_program_balance)
    executor.register(
        "list_hackerone_program_transactions", list_hackerone_program_transactions
    )
    executor.register("manage_hackerone_program", manage_hackerone_program)
    executor.register("list_hackerone_organizations", list_hackerone_organizations)
    executor.register(
        "list_hackerone_organization_programs", list_hackerone_organization_programs
    )
    executor.register("list_hackerone_assets", list_hackerone_assets)
    executor.register("get_hackerone_asset", get_hackerone_asset)
    executor.register("manage_hackerone_assets", manage_hackerone_assets)
    executor.register("manage_hackerone_organization", manage_hackerone_organization)
    executor.register("manage_hackerone_automations", manage_hackerone_automations)
    executor.register("manage_hackerone_findings", manage_hackerone_findings)
    executor.register("get_hackerone_analytics", get_hackerone_analytics)
    executor.register("get_hackerone_activity", get_hackerone_activity)
    executor.register(
        "list_hackerone_incremental_activities", list_hackerone_incremental_activities
    )
    executor.register("get_hackerone_user", get_hackerone_user)

    src = "integration:hackerone"
    return _build_definitions(src)


# ---------------------------------------------------------------------------
# Tool definitions (static metadata; no credentials required)
# ---------------------------------------------------------------------------

# Shared JSON:API manager parameter fragments reused across the manage_* tools.
_MANAGE_COMMON_PROPS: dict[str, Any] = {
    "action": {
        "type": "string",
        "description": "The operation to perform (see the tool description).",
    },
    "attributes": {
        "type": "object",
        "description": (
            "JSON:API attributes for write actions (wrapped as "
            "{'data': {'type': ..., 'attributes': ...}} automatically)."
        ),
    },
    "params": {
        "type": "object",
        "description": "Optional query-string parameters.",
    },
    "raw_body": {
        "type": "object",
        "description": (
            "Optional exact request body sent verbatim — use when the endpoint "
            "needs a non-standard payload (overrides 'attributes')."
        ),
    },
}


def _build_definitions(src: str) -> list[ToolDefinition]:
    """Build the static HackerOne ToolDefinition list."""

    def _td(
        name: str,
        description: str,
        properties: dict[str, Any],
        required: list[str],
        priority: int = 2,
    ) -> ToolDefinition:
        return ToolDefinition(
            name=name,
            description=description,
            input_schema={
                "type": "object",
                "properties": properties,
                "required": required,
            },
            source=src,
            priority=priority,
        )

    return [
        # ── Reports ───────────────────────────────────────────────────────
        _td(
            "list_hackerone_reports",
            "List/query HackerOne vulnerability reports for a program. Filter by "
            "program handle(s), state (new, triaged, needs-more-info, resolved, "
            "informative, duplicate, not-applicable, spam, retesting), severity "
            "(none, low, medium, high, critical), assignee, keyword, and date "
            "ranges. A program handle is required.",
            {
                "program": {
                    "type": "string",
                    "description": "Program handle(s), comma-separated (required).",
                },
                "state": {
                    "type": "string",
                    "description": "State filter(s), comma-separated.",
                },
                "severity": {
                    "type": "string",
                    "description": "Severity filter(s), comma-separated.",
                },
                "assignee": {"type": "string"},
                "keyword": {"type": "string"},
                "created_after": {
                    "type": "string",
                    "description": "ISO datetime lower bound (created_at).",
                },
                "created_before": {
                    "type": "string",
                    "description": "ISO datetime upper bound (created_at).",
                },
                "last_activity_after": {"type": "string"},
                "sort": {
                    "type": "string",
                    "description": "Sort field, e.g. last_activity_at, created_at.",
                    "default": "last_activity_at",
                },
                "order": {"type": "string", "enum": ["asc", "desc"], "default": "desc"},
                "page_size": {"type": "integer", "default": 25},
                "page_number": {"type": "integer", "default": 1},
            },
            ["program"],
            priority=1,
        ),
        _td(
            "get_hackerone_report",
            "Get full details of a single HackerOne report by its numeric ID.",
            {"report_id": {"type": "string"}},
            ["report_id"],
            priority=1,
        ),
        _td(
            "download_hackerone_report_pdf",
            "Generate and download a HackerOne report as a PDF (the report's full "
            "timeline and metadata). Returns a downloadable PDF file — ideal for "
            "fetching engagement reports as evidence. Include the returned "
            "[[FILE:...]] marker verbatim so the user gets a download card.",
            {
                "report_id": {"type": "string"},
                "pdf_type": {
                    "type": "string",
                    "enum": ["reporter", "full", "triage"],
                    "default": "full",
                    "description": (
                        "Which rendering to generate: 'reporter' (reporter-facing "
                        "view), 'full' (complete report, best for evidence), or "
                        "'triage'."
                    ),
                },
            },
            ["report_id"],
            priority=1,
        ),
        _td(
            "create_hackerone_report",
            "Create (import) a report into a program — e.g. an external finding "
            "from a scanner or pentest, for central tracking and dedup.",
            {
                "team_handle": {"type": "string"},
                "title": {"type": "string"},
                "vulnerability_information": {"type": "string"},
                "impact": {"type": "string"},
                "severity_rating": {
                    "type": "string",
                    "enum": ["none", "low", "medium", "high", "critical"],
                },
                "weakness_id": {"type": "string"},
                "structured_scope_id": {"type": "string"},
                "source": {"type": "string"},
            },
            ["team_handle", "title", "vulnerability_information", "impact"],
        ),
        _td(
            "comment_hackerone_report",
            "Post a comment on a report. Internal comments are visible only to the "
            "team; set internal=false for a comment visible to the reporter.",
            {
                "report_id": {"type": "string"},
                "message": {"type": "string"},
                "internal": {"type": "boolean", "default": True},
            },
            ["report_id", "message"],
        ),
        _td(
            "change_hackerone_report_state",
            "Change a report's state (e.g. triaged, needs-more-info, resolved, "
            "informative, duplicate, not-applicable, spam).",
            {
                "report_id": {"type": "string"},
                "state": {"type": "string"},
                "message": {"type": "string"},
            },
            ["report_id", "state"],
        ),
        _td(
            "assign_hackerone_report",
            "Assign a report to a user or group (assignee_type = user, group, or "
            "nobody to unassign).",
            {
                "report_id": {"type": "string"},
                "assignee_type": {
                    "type": "string",
                    "enum": ["user", "group", "nobody"],
                    "default": "user",
                },
                "assignee_id": {"type": "string"},
                "message": {"type": "string"},
            },
            ["report_id"],
        ),
        _td(
            "award_hackerone_bounty",
            "Award a bounty on a report (amount and optional bonus in the "
            "program's currency).",
            {
                "report_id": {"type": "string"},
                "amount": {"type": "string"},
                "bonus_amount": {"type": "string"},
                "message": {"type": "string"},
            },
            ["report_id", "amount"],
        ),
        _td(
            "manage_hackerone_report",
            "Long-tail report operations. Actions: update_title, set_weakness, "
            "set_tags, update_inboxes, add_participant, request_retest, "
            "approve_retest, reject_retest, cancel_retest, request_disclosure, "
            "cancel_disclosure, redact, set_cves, mark_ineligible_for_bounty, "
            "update_structured_scope, transfer, update_custom_fields, add_summary, "
            "set_close_comments, set_issue_tracker_reference, suggest_bounty, "
            "list_bounty_suggestions, award_swag, escalate, cancel_escalation, "
            "delete_attachments.",
            {"report_id": {"type": "string"}, **_MANAGE_COMMON_PROPS},
            ["report_id", "action"],
        ),
        # ── Programs ──────────────────────────────────────────────────────
        _td(
            "list_hackerone_programs",
            "List the programs the API token can access, with their IDs and "
            "handles. Use this first to discover program handles/IDs.",
            {},
            [],
            priority=1,
        ),
        _td(
            "get_hackerone_program",
            "Get a program's details (members, groups, custom fields, settings).",
            {"program_id": {"type": "string"}},
            ["program_id"],
        ),
        _td(
            "list_hackerone_program_weaknesses",
            "List the weakness (CWE) types configured for a program.",
            {"program_id": {"type": "string"}},
            ["program_id"],
        ),
        _td(
            "list_hackerone_structured_scopes",
            "List a program's structured scopes (assets in scope).",
            {"program_id": {"type": "string"}},
            ["program_id"],
        ),
        _td(
            "list_hackerone_program_members",
            "List the organization members who have access to a program.",
            {"program_id": {"type": "string"}},
            ["program_id"],
        ),
        _td(
            "list_hackerone_common_responses",
            "List a program's saved common (canned) responses.",
            {"program_id": {"type": "string"}},
            ["program_id"],
        ),
        _td(
            "get_hackerone_program_balance",
            "Get a program's available balance for bounty payments.",
            {"program_id": {"type": "string"}},
            ["program_id"],
        ),
        _td(
            "list_hackerone_program_transactions",
            "List a program's billing transactions, optionally for a month/year.",
            {
                "program_id": {"type": "string"},
                "month": {"type": "integer"},
                "year": {"type": "integer"},
            },
            ["program_id"],
        ),
        _td(
            "manage_hackerone_program",
            "Program operations. Actions: update_policy, award_external_bounty, "
            "send_team_message, notify_external_platform, list_swag, list_thanks, "
            "get_audit_log, get_bounty_table, list_integrations, list_triage_reviews, "
            "list_reporters, list_cve_requests, create_cve_request, "
            "list_credential_inquiries, list_allowed_reporters, "
            "list_allowed_reporters_history, list_hacker_invitations, invite_hackers, "
            "list_scope_exclusions, create_scope_exclusion, update_scope_exclusion, "
            "delete_scope_exclusion, list_campaigns, create_campaign, get_campaign, "
            "update_campaign, launch_campaign, end_campaign. Pass resource_id for "
            "sub-resource actions (e.g. a campaign or scope exclusion ID).",
            {
                "program_id": {"type": "string"},
                "resource_id": {"type": "string"},
                **_MANAGE_COMMON_PROPS,
            },
            ["program_id", "action"],
        ),
        # ── Organizations & Assets ────────────────────────────────────────
        _td(
            "list_hackerone_organizations",
            "List the organizations the API token can access (with their IDs). Use "
            "this first to discover the organization ID for asset/automation tools.",
            {},
            [],
            priority=1,
        ),
        _td(
            "list_hackerone_organization_programs",
            "List all programs that belong to an organization.",
            {"organization_id": {"type": "string"}},
            ["organization_id"],
        ),
        _td(
            "list_hackerone_assets",
            "List an organization's assets (attack-surface inventory).",
            {
                "organization_id": {"type": "string"},
                "state": {"type": "string"},
                "page_size": {"type": "integer", "default": 25},
                "page_number": {"type": "integer", "default": 1},
            },
            ["organization_id"],
        ),
        _td(
            "get_hackerone_asset",
            "Get full details of a single asset, including ports and reachability.",
            {
                "organization_id": {"type": "string"},
                "asset_id": {"type": "string"},
            },
            ["organization_id", "asset_id"],
        ),
        _td(
            "manage_hackerone_assets",
            "Attack-surface management. Actions: create_asset, update_asset, "
            "archive_assets, add_to_scope, archive_scope, update_scope, list_tags, "
            "list_tag_categories, create_tag, create_tag_category, list_ports, "
            "add_ports, delete_port, get_reachability, check_reachability, "
            "get_scanner, update_scanner, get_asset_import. Pass asset_id for "
            "asset-scoped actions and resource_id for sub-resources (scope/port/"
            "import IDs).",
            {
                "organization_id": {"type": "string"},
                "asset_id": {"type": "string"},
                "resource_id": {"type": "string"},
                **_MANAGE_COMMON_PROPS,
            },
            ["organization_id", "action"],
        ),
        _td(
            "manage_hackerone_organization",
            "Organization administration. Actions: list_members, update_member, "
            "remove_member, list_groups, get_group, create_group, update_group, "
            "list_inboxes, get_audit_log, list_eligibility_settings, "
            "get_eligibility_setting, list_pending_invitations, create_invitation, "
            "get_user_by_id. Pass resource_id for member/group/setting/user IDs.",
            {
                "organization_id": {"type": "string"},
                "resource_id": {"type": "string"},
                **_MANAGE_COMMON_PROPS,
            },
            ["organization_id", "action"],
        ),
        _td(
            "manage_hackerone_automations",
            "Manage organization automations. Actions: list, get, create, update, "
            "list_runs, get_run, get_run_logs, trigger. Pass automation_id for a "
            "specific automation and resource_id for a run ID.",
            {
                "organization_id": {"type": "string"},
                "automation_id": {"type": "string"},
                "resource_id": {"type": "string"},
                **_MANAGE_COMMON_PROPS,
            },
            ["organization_id", "action"],
        ),
        _td(
            "manage_hackerone_findings",
            "Manage findings workboards and views. Actions: list_workboards, "
            "get_workboard, create_workboard, update_workboard, delete_workboard, "
            "list_views, get_view, create_view, update_view, delete_view, "
            "get_view_reports. Pass workboard_id and resource_id (view ID) as needed.",
            {
                "organization_id": {"type": "string"},
                "workboard_id": {"type": "string"},
                "resource_id": {"type": "string"},
                **_MANAGE_COMMON_PROPS,
            },
            ["organization_id", "action"],
        ),
        # ── Analytics, Activities, Users ──────────────────────────────────
        _td(
            "get_hackerone_analytics",
            "Query predefined program analytics (e.g. submissions, "
            "submissions-by-severity, bounty-awarded, sla_statistics). Requires a "
            "date range (start_at/end_at as YYYY-MM-DD, end exclusive), an "
            "interval, and an organization_id; optionally scope to one program "
            "with team_id.",
            {
                "key": {
                    "type": "string",
                    "description": (
                        "Analytics query key (e.g. submissions, "
                        "submissions-by-severity, bounty-awarded, sla_statistics)."
                    ),
                },
                "start_at": {
                    "type": "string",
                    "description": "Start date (YYYY-MM-DD, inclusive).",
                },
                "end_at": {
                    "type": "string",
                    "description": "End date (YYYY-MM-DD, exclusive).",
                },
                "interval": {
                    "type": "string",
                    "enum": ["month", "quarter", "year"],
                    "default": "month",
                },
                "organization_id": {
                    "type": "string",
                    "description": "Organization ID (see list_hackerone_organizations).",
                },
                "team_id": {
                    "type": "string",
                    "description": "Optional program/team ID to scope to one program.",
                },
            },
            ["key", "start_at", "end_at", "organization_id"],
        ),
        _td(
            "get_hackerone_activity",
            "Get a single activity (timeline event) by its ID.",
            {"activity_id": {"type": "string"}},
            ["activity_id"],
        ),
        _td(
            "list_hackerone_incremental_activities",
            "Stream a program's activities incrementally by time (audit-style feed). "
            "Provide the program handle and optionally an updated_at_after cutoff.",
            {
                "handle": {"type": "string"},
                "updated_at_after": {"type": "string"},
                "report_id": {"type": "string"},
                "page_size": {"type": "integer", "default": 25},
                "page_number": {"type": "integer", "default": 1},
            },
            ["handle"],
        ),
        _td(
            "get_hackerone_user",
            "Look up a HackerOne user by username (reputation, signal, and the "
            "private programs they participate in that you can see).",
            {"username": {"type": "string"}},
            ["username"],
        ),
    ]
