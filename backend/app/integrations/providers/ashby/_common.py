"""What every Ashby domain module shares.

Ashby is an RPC API: every endpoint is ``POST https://api.ashbyhq.com/<category>.<method>``
with a JSON body and HTTP Basic auth (the API key as the username, no password).
Two properties of its answers decide how a tool reads them:

* A refused request is HTTP 200 with ``{"success": false, "errors": [...],
  "errorInfo": {...}}`` - a wrong parameter, an unknown id, a validation
  error. :meth:`AshbyContext.post` turns that envelope into an ``Error:``
  string; a tool never formats it as if it were the record it asked for.
* A missing API key is HTTP 401; a wrong or deactivated key HTTP 403; a key
  without the module permission an endpoint needs HTTP 403 with
  ``missing_endpoint_permission``. The last one names the permission to grant
  (:func:`required_permission`, from Ashby's permission table).

List endpoints page with ``cursor`` / ``nextCursor`` / ``moreDataAvailable``
(at most 100 per page). Ashby allows 1,000 requests per minute per API key;
every request takes a slot of one limiter per key, so a tool that resolves
names (users, stages, applications) for a whole page stays under it.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import httpx

from app.core.egress import egress_client
from app.core.utils import parse_delay
from app.integrations.http import (
    TRANSIENT_RETRY_STATUSES,
    app_token_cache_key,
    format_pagination_hint,
    get_shared_rate_limiter,
    safe_request,
)
from app.tools.base import ToolDefinition

BASE_URL = "https://api.ashbyhq.com"
PROVIDER = "Ashby"
SOURCE = "integration:ashby"
#: Largest page Ashby serves, and the most items one tool call returns.
MAX_PAGE = 100
#: Default page size of the list tools.
DEFAULT_PAGE = 25
#: How many records a client-side filter reads before it stops looking.
MAX_SCAN = 1000
#: Concurrent lookups when one answer needs several reads (names, stages).
FAN_OUT = 6
#: Most distinct records one answer resolves (the rest keep their ids).
MAX_LOOKUPS = 50
#: Longest free text (notes, feedback answers, descriptions) one value shows.
MAX_TEXT = 2000
TIMEOUT = 30

_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}
#: 1,000 requests per minute per key: 15 a second sustained, short bursts.
_RATE_PER_SECOND = 15.0
_BURST = 20.0
_MAX_IN_FLIGHT = 8

# ---------------------------------------------------------------------------
# Permissions (https://developers.ashbyhq.com/reference/authentication)
# ---------------------------------------------------------------------------

_MODULE_BY_CATEGORY: dict[str, str] = {
    "job": "Jobs",
    "jobBoard": "Jobs",
    "jobInterviewPlan": "Jobs",
    "jobPosting": "Jobs",
    "jobTemplate": "Jobs",
    "opening": "Jobs",
    "application": "Candidates",
    "applicationFeedback": "Candidates",
    "applicationFeedbackRequest": "Candidates",
    "applicationForm": "Candidates",
    "applicationHiringTeamRole": "Candidates",
    "assessment": "Candidates",
    "candidate": "Candidates",
    "file": "Candidates",
    "referral": "Candidates",
    "surveyRequest": "Candidates",
    "surveySubmission": "Candidates",
    "project": "Projects",
    "interview": "Interviews",
    "interviewBriefing": "Interviews",
    "interviewEvent": "Interviews",
    "interviewPlan": "Interviews",
    "interviewSchedule": "Interviews",
    "interviewStage": "Interviews",
    "interviewStageGroup": "Interviews",
    "takeHomeAssignment": "Interviews",
    "archiveReason": "Hiring Process",
    "candidateTag": "Hiring Process",
    "closeReason": "Hiring Process",
    "communicationTemplate": "Hiring Process",
    "customField": "Hiring Process",
    "feedbackFormDefinition": "Hiring Process",
    "interviewerPool": "Hiring Process",
    "referralForm": "Hiring Process",
    "source": "Hiring Process",
    "sourceTrackingLink": "Hiring Process",
    "surveyFormDefinition": "Hiring Process",
    "brand": "Organization",
    "department": "Organization",
    "hiringTeam": "Organization",
    "hiringTeamRole": "Organization",
    "location": "Organization",
    "user": "Organization",
    "offer": "Offers",
    "offerProcess": "Offers",
    "apiKey": "API Keys",
    "version": "API Keys",
    "webhook": "API Keys",
    "approval": "Approvals",
    "report": "Reports",
    "notetakerTranscript": "Notetaker",
}
#: Endpoints whose permission is not their category's.
_PERMISSION_OVERRIDES: dict[str, str] = {
    "application.delete": "Candidates Delete",
    "customField.setValue": "Candidates Write",
    "customField.setValues": "Candidates Write",
    "file.createFileUploadHandle": "Candidates Write",
    "job.startApprovalProcess": "Approvals Write",
    "candidate.getRecentEmailMessages": "Emails Read",
}
_READ_ONLY_CATEGORIES = {"report", "notetakerTranscript", "approval"}
_READ_METHOD_RE = re.compile(r"^(info|list|search|get)")


def is_read_endpoint(endpoint: str) -> bool:
    category, _, method = endpoint.partition(".")
    return category in _READ_ONLY_CATEGORIES or bool(_READ_METHOD_RE.match(method))


def required_permission(endpoint: str) -> str:
    """The Ashby API key permission *endpoint* needs, e.g. ``Candidates Read``."""
    if endpoint in _PERMISSION_OVERRIDES:
        return _PERMISSION_OVERRIDES[endpoint]
    category = endpoint.partition(".")[0]
    module = _MODULE_BY_CATEGORY.get(category)
    if module is None:
        return "the module permission for this endpoint"
    access = "Read" if is_read_endpoint(endpoint) else "Write"
    if module == "Projects" and access == "Read":
        return "Projects Read (or Candidates Read)"
    return f"{module} {access}"


def _body_of(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _error_codes(data: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    info = data.get("errorInfo")
    if isinstance(info, dict) and info.get("code"):
        codes.append(str(info["code"]))
    for item in data.get("errors") or []:
        if isinstance(item, str):
            codes.append(item)
        elif isinstance(item, dict) and item.get("code"):
            codes.append(str(item["code"]))
    return codes


def ashby_error(data: Any, endpoint: str) -> str | None:
    """The ``Error:`` string of an Ashby ``success: false`` answer, or ``None``."""
    if not isinstance(data, dict) or data.get("success") is not False:
        return None
    messages: list[str] = []
    info = data.get("errorInfo")
    if isinstance(info, dict) and info.get("message"):
        code = f" ({info['code']})" if info.get("code") else ""
        messages.append(f"{info['message']}{code}")
    for item in data.get("errors") or []:
        if isinstance(item, dict):
            text = str(item.get("message") or item.get("code") or "").strip()
            if item.get("parameter"):
                text = f"{text} (parameter: {item['parameter']})"
        else:
            text = str(item).strip()
        if text and not any(text in m for m in messages):
            messages.append(text)
    detail = "; ".join(messages) or "no reason given"
    hint = ""
    if any(code.endswith("_not_found") for code in _error_codes(data)):
        hint = (
            " If the record exists, the API key may lack the permission for it or "
            "the 'Allow access to confidential jobs and projects' setting."
        )
    return f"Error: Ashby refused {endpoint}: {detail}.{hint}"


def warnings_note(data: dict[str, Any] | None) -> str:
    """The ``warnings`` Ashby attached to a successful answer, as one line."""
    warnings = (data or {}).get("warnings") or []
    if not warnings:
        return ""
    return "\nAshby warnings: " + "; ".join(str(w) for w in warnings)


def _auth_error(resp: httpx.Response, endpoint: str) -> str:
    body = _body_of(resp)
    codes = _error_codes(body)
    if resp.status_code == 403 and "missing_endpoint_permission" in codes:
        return (
            f"Error: the Ashby API key may not call {endpoint}: it needs the "
            f"'{required_permission(endpoint)}' permission. An Ashby admin grants it "
            "under Admin > Integrations > API Keys (get_ashby_api_key_info lists "
            "what the key holds)."
        )
    if resp.status_code == 401:
        return "Error: Ashby received no API key. Paste the key in the integration settings."
    return (
        "Error: Ashby rejected the API key (wrong or deactivated). An Ashby admin "
        "can check it under Admin > Integrations > API Keys."
    )


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------


@dataclass
class Page:
    items: list[dict[str, Any]]
    next_cursor: str | None = None
    #: Records read to fill the page when a client-side filter applied.
    scanned: int = 0
    #: The client-side filter stopped at :data:`MAX_SCAN` before the end.
    scan_capped: bool = False


def clamp(value: Any, default: int = DEFAULT_PAGE, maximum: int = MAX_PAGE) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, maximum))


def page_footer(page: Page, *, noun: str) -> str:
    """The pagination hint and, for a filtered scan, what was read."""
    footer = ""
    if page.scan_capped:
        footer += (
            f"\n[Read {page.scanned} {noun}s without reaching the end; narrow the "
            "filters or continue with the cursor below]"
        )
    return footer + format_pagination_hint(page.next_cursor, param_name="cursor")


# ---------------------------------------------------------------------------
# Context: HTTP, paging and per-run lookups
# ---------------------------------------------------------------------------


@dataclass
class AshbyContext:
    api_key: str
    persist_file_fn: Any = None
    _caches: dict[str, dict[str, Any]] = field(default_factory=dict)

    def client(self) -> Any:
        return egress_client(timeout=TIMEOUT)

    def _limiter(self):
        return get_shared_rate_limiter(
            f"ashby:{app_token_cache_key('ashby', self.api_key)}",
            rate=_RATE_PER_SECOND,
            capacity=_BURST,
            max_concurrency=_MAX_IN_FLIGHT,
        )

    async def post(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        body: dict[str, Any] | None = None,
        *,
        files: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """POST *endpoint*; ``(answer, None)`` or ``(None, "Error: ...")``.

        With *files* the request is multipart: *body* is sent as form fields.
        """
        retry = TRANSIENT_RETRY_STATUSES if is_read_endpoint(endpoint) else None
        if files:
            payload: dict[str, Any] = {"data": body or {}, "files": files, "headers": {"Accept": "application/json"}}
        else:
            payload = {"json": body or {}, "headers": _HEADERS}
        async with self._limiter().slot():
            resp, err = await safe_request(
                client,
                "post",
                f"{BASE_URL}/{endpoint}",
                PROVIDER,
                f"call {endpoint}",
                expected_statuses={401, 403},
                retry_statuses=retry,
                auth=(self.api_key, ""),
                **payload,
            )
        if resp is not None and resp.status_code in (401, 403):
            return None, _auth_error(resp, endpoint)
        if err or resp is None:
            return None, err or f"Error: Ashby did not answer {endpoint}."
        data = _body_of(resp)
        if not data:
            return None, f"Error: Ashby answered {endpoint} with something that is not a JSON object."
        api_err = ashby_error(data, endpoint)
        if api_err:
            return None, api_err
        return data, None

    async def call(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        body: dict[str, Any] | None = None,
    ) -> tuple[Any, str | None]:
        """POST *endpoint*; ``(results, None)`` or ``(None, "Error: ...")``."""
        data, err = await self.post(client, endpoint, body)
        if err:
            return None, err
        return data.get("results"), None

    async def page(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        body: dict[str, Any] | None = None,
        *,
        limit: int = DEFAULT_PAGE,
        cursor: str = "",
        keep: Callable[[dict[str, Any]], bool] | None = None,
        max_scan: int = MAX_SCAN,
    ) -> tuple[Page | None, str | None]:
        """Up to *limit* records of a paged list, from *cursor* on.

        With *keep*, records it refuses are skipped and pages are read until
        *limit* records are kept or *max_scan* were read. A page never asks
        for more records than there is room left for, so the cursor handed
        back resumes right after the last record read: none is skipped.
        """
        items: list[dict[str, Any]] = []
        next_cursor = (cursor or "").strip() or None
        scanned = 0
        while True:
            payload = {**(body or {}), "limit": max(1, min(MAX_PAGE, limit - len(items)))}
            if next_cursor:
                payload["cursor"] = next_cursor
            data, err = await self.post(client, endpoint, payload)
            if err:
                return None, err
            results = [r for r in data.get("results") or [] if isinstance(r, dict)]
            scanned += len(results)
            more = bool(data.get("moreDataAvailable")) and bool(data.get("nextCursor"))
            next_cursor = data.get("nextCursor") if more else None
            items.extend(results if keep is None else [r for r in results if keep(r)])
            if not more or len(items) >= limit:
                break
            if keep is not None and scanned >= max_scan:
                return Page(items, next_cursor, scanned, scan_capped=True), None
        return Page(items[:limit], next_cursor, scanned), None

    async def collect_all(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        body: dict[str, Any] | None = None,
        *,
        max_items: int = MAX_SCAN,
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        """Every record of a list endpoint, up to *max_items*."""
        page, err = await self.page(client, endpoint, body, limit=max_items)
        if err:
            return None, err
        return page.items, None

    # -- per-run lookups -------------------------------------------------

    async def lookup(
        self,
        client: httpx.AsyncClient,
        kind: str,
        ids: Iterable[str | None],
        fetch: Callable[[httpx.AsyncClient, str], Awaitable[tuple[Any, str | None]]],
    ) -> dict[str, dict[str, Any]]:
        """Records of *ids*, read once per run; a failed read is an empty dict."""
        cache = self._caches.setdefault(kind, {})
        wanted = [i for i in dict.fromkeys(i for i in ids if i)]
        missing = [i for i in wanted if i not in cache][:MAX_LOOKUPS]
        if missing:
            gate = asyncio.Semaphore(FAN_OUT)

            async def one(record_id: str) -> None:
                async with gate:
                    record, err = await fetch(client, record_id)
                cache[record_id] = record if not err and isinstance(record, dict) else {}

            await asyncio.gather(*(one(i) for i in missing))
        return {i: cache.get(i) or {} for i in wanted}

    async def users(self, client: httpx.AsyncClient, ids: Iterable[str | None]) -> dict[str, dict[str, Any]]:
        return await self.lookup(client, "user", ids, lambda c, i: self.call(c, "user.info", {"userId": i}))

    async def stages(self, client: httpx.AsyncClient, ids: Iterable[str | None]) -> dict[str, dict[str, Any]]:
        return await self.lookup(
            client,
            "stage",
            ids,
            lambda c, i: self.call(c, "interviewStage.info", {"interviewStageId": i}),
        )

    async def interview_definitions(
        self, client: httpx.AsyncClient, ids: Iterable[str | None]
    ) -> dict[str, dict[str, Any]]:
        return await self.lookup(client, "interview", ids, lambda c, i: self.call(c, "interview.info", {"id": i}))

    async def applications(self, client: httpx.AsyncClient, ids: Iterable[str | None]) -> dict[str, dict[str, Any]]:
        return await self.lookup(
            client,
            "application",
            ids,
            lambda c, i: self.call(c, "application.info", {"applicationId": i}),
        )

    async def feedback_forms(self, client: httpx.AsyncClient, ids: Iterable[str | None]) -> dict[str, dict[str, Any]]:
        return await self.lookup(
            client,
            "feedback_form",
            ids,
            lambda c, i: self.call(c, "feedbackFormDefinition.info", {"feedbackFormDefinitionId": i}),
        )

    async def _directory(self, client: httpx.AsyncClient, kind: str, endpoint: str) -> dict[str, dict[str, Any]]:
        """Every record of a small list (departments, locations) by id, once per run."""
        key = f"directory:{kind}"
        if key not in self._caches:
            records, _err = await self.collect_all(client, endpoint, {"includeArchived": True})
            self._caches[key] = {str(r["id"]): r for r in records or [] if r.get("id")}
        return self._caches[key]

    async def departments(self, client: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
        return await self._directory(client, "department", "department.list")

    async def locations(self, client: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
        return await self._directory(client, "location", "location.list")

    def remember_form(self, form_id: str, form_definition: Any) -> None:
        if form_id and isinstance(form_definition, dict):
            self._caches.setdefault("form", {})[form_id] = form_definition

    def remembered_form(self, form_id: str) -> dict[str, Any] | None:
        return self._caches.get("form", {}).get(form_id)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

SINCE = "a window back from now ('30d', '12h'), an ISO date or date-time ('2026-01-31'), or a Unix timestamp"
CURSOR_SCHEMA = {
    "type": "string",
    "description": "Cursor from a previous call's 'More results available' hint",
    "default": "",
}


def limit_schema(default: int = DEFAULT_PAGE, maximum: int = MAX_PAGE) -> dict[str, Any]:
    return {
        "type": "integer",
        "description": f"Number of records to return (1-{maximum})",
        "default": default,
    }


def id_schema(what: str, hint: str = "") -> dict[str, Any]:
    return {"type": "string", "description": f"Ashby {what} ID{f' ({hint})' if hint else ''}"}


def definition(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: Iterable[str] = (),
    priority: int = 1,
) -> ToolDefinition:
    """One Ashby ``ToolDefinition``: 0 core reads, 1 writes, 2 long tail and destructive."""
    return ToolDefinition(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": properties, "required": list(required)},
        source=SOURCE,
        priority=priority,
    )


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def to_epoch_ms(value: Any, name: str) -> tuple[int | None, str | None]:
    """Read a time bound Ashby takes in epoch milliseconds.

    Accepts a relative window back from now (``30d``, ``12h``, ``90m``), an
    ISO date or date-time, or a Unix timestamp in seconds or milliseconds.
    """
    raw = str(value if value is not None else "").strip()
    if not raw:
        return None, None
    if raw.isdigit() and 9 <= len(raw) <= 14:
        number = int(raw)
        return (number if len(raw) >= 12 else number * 1000), None
    seconds = parse_delay(raw)
    if seconds:
        return int((time.time() - seconds) * 1000), None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            parsed = datetime.combine(date.fromisoformat(raw), datetime.min.time(), UTC)
        else:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
    except ValueError:
        return None, (
            f"Error: {name} '{raw}' is not a time. Use a window back from now "
            "('30d', '12h'), an ISO date ('2026-01-31') or date-time, or a Unix timestamp."
        )
    return int(parsed.timestamp() * 1000), None


def parse_json_arg(value: Any, name: str) -> tuple[Any, str | None]:
    """A JSON object or array given as a string (or already parsed)."""
    if value is None or value == "":
        return None, None
    if isinstance(value, (dict, list)):
        return value, None
    try:
        return json.loads(str(value)), None
    except json.JSONDecodeError as exc:
        return None, f"Error: {name} is not valid JSON ({exc.msg} at position {exc.pos})."


def id_list(value: Any) -> list[str]:
    """Ids given as a list or a comma / newline separated string."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,\n;]", str(value))
    return [str(i).strip() for i in items if str(i).strip()]


def set_if(body: dict[str, Any], key: str, value: Any) -> None:
    """Put *value* in *body* unless it is empty (``False`` and ``0`` count)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return
    body[key] = value.strip() if isinstance(value, str) else value


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"<\s*(br|/p|/div|/li|/h[1-6])\s*/?>", re.IGNORECASE)


def strip_html(text: Any) -> str:
    if not text:
        return ""
    value = str(text)
    if "<" in value:
        value = _BREAK_RE.sub("\n", value)
        value = _TAG_RE.sub("", value)
    value = html.unescape(value)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def truncate(text: Any, limit: int = MAX_TEXT) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return f"{value[:limit].rstrip()} [... {len(value) - limit} more characters]"


def indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def ts(value: Any) -> str:
    """An Ashby timestamp without its milliseconds (``2026-01-15T10:30:00Z``)."""
    if not value:
        return ""
    return re.sub(r"\.\d+Z$", "Z", str(value))


def person_name(person: Any) -> str:
    if not isinstance(person, dict):
        return ""
    name = person.get("name") or " ".join(str(p) for p in (person.get("firstName"), person.get("lastName")) if p)
    return str(name).strip() or str(person.get("email") or "").strip()


def user_label(person: Any, *, with_id: bool = True) -> str:
    """``Name <email> (ID: id)`` of an Ashby user (or hiring team member)."""
    if not isinstance(person, dict) or not person:
        return ""
    name = person_name(person) or "Unnamed user"
    email = person.get("email")
    label = f"{name} <{email}>" if email and email != name else name
    user_id = person.get("id") or person.get("userId")
    if with_id and user_id:
        label += f" (ID: {user_id})"
    return label


def hiring_team_lines(team: Any, prefix: str = "  ") -> list[str]:
    members = [m for m in team or [] if isinstance(m, dict)]
    if not members:
        return []
    lines = [f"{prefix}Hiring team:"]
    for member in members:
        role = f" - {member['role']}" if member.get("role") else ""
        lines.append(f"{prefix}  - {user_label(member)}{role}")
    return lines


def contact(info: Any) -> str:
    if isinstance(info, dict):
        value = info.get("value") or ""
        kind = info.get("type")
        return f"{value} ({kind})" if value and kind else str(value)
    return str(info or "")


def money(value: Any) -> str:
    if isinstance(value, dict) and "value" in value:
        return f"{value.get('value')} {value.get('currencyCode') or ''}".strip()
    return str(value)


def format_value(value: Any) -> str:
    """A custom field or form value as text."""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(format_value(v) for v in value)
    if isinstance(value, dict):
        if "score" in value:
            text = f"{value['score']}/4"
            comments = format_value(value.get("comments"))
            return f"{text} - {comments}" if comments else text
        if value.get("type") in ("PlainText", "text/plain") and "value" in value:
            return truncate(value.get("value"))
        if value.get("type") == "text/html" and "value" in value:
            return truncate(strip_html(value.get("value")))
        if "minValue" in value or "maxValue" in value:
            unit = f" {value['currencyCode']}" if value.get("currencyCode") else ""
            interval = f" per {value['interval']}" if value.get("interval") else ""
            return f"{value.get('minValue')} - {value.get('maxValue')}{unit}{interval}"
        if "currencyCode" in value and "value" in value:
            return money(value)
        if "value" in value and len(value) <= 2:
            return format_value(value.get("value"))
        return truncate(json.dumps(value, ensure_ascii=False), 500)
    return truncate(strip_html(value) if isinstance(value, str) else value)


def custom_field_lines(fields: Any, prefix: str = "  ") -> list[str]:
    entries = [f for f in fields or [] if isinstance(f, dict)]
    lines = []
    for entry in entries:
        label = entry.get("valueLabel")
        value = format_value(label if label not in (None, "", []) else entry.get("value"))
        if value:
            lines.append(f"{prefix}  - {entry.get('title') or entry.get('id')}: {value}")
    return [f"{prefix}Custom fields:", *lines] if lines else []


def file_handle_list(handles: Any) -> str:
    files = [h for h in handles or [] if isinstance(h, dict) and h.get("id")]
    return ", ".join(f"{h.get('name') or 'file'} (file id: {h['id']})" for h in files)


def fmt_project(p: dict[str, Any]) -> str:
    flags = [f for f, on in (("archived", p.get("isArchived")), ("confidential", p.get("confidential"))) if on]
    line = f"**{p.get('title') or 'Untitled project'}** (ID: {p.get('id', '?')})"
    if flags:
        line += f" [{', '.join(flags)}]"
    description = truncate(p.get("descriptionPlain") or strip_html(p.get("descriptionHtml")), 300)
    if description:
        line += f"\n  {description}"
    if p.get("createdAt"):
        line += f"\n  Created: {ts(p['createdAt'])}"
    return line


# ---------------------------------------------------------------------------
# Forms (feedback scorecards, offers, referrals, surveys)
# ---------------------------------------------------------------------------


def form_fields(form_definition: Any) -> list[dict[str, Any]]:
    """Each field of a form: ``{path, title, type, required, options, section}``."""
    fields: list[dict[str, Any]] = []
    if not isinstance(form_definition, dict):
        return fields
    for section in form_definition.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for entry in section.get("fields") or []:
            if not isinstance(entry, dict):
                continue
            definition = entry.get("field") if isinstance(entry.get("field"), dict) else entry
            if not definition.get("path"):
                continue
            fields.append(
                {
                    "path": definition["path"],
                    "title": definition.get("title") or definition.get("humanReadablePath") or definition["path"],
                    "type": definition.get("type") or "",
                    "required": bool(entry.get("isRequired")),
                    "options": [o for o in definition.get("selectableValues") or [] if isinstance(o, dict)],
                    "section": section.get("title") or "",
                }
            )
    return fields


def describe_form(form_definition: Any, prefix: str = "  ") -> str:
    """One line per field: path, title, type, required, and the options."""
    lines = []
    for f in form_fields(form_definition):
        options = ""
        if f["options"]:
            options = " | options: " + ", ".join(f"{o.get('label')}={o.get('value')}" for o in f["options"])
        required = ", required" if f["required"] else ""
        lines.append(f"{prefix}- {f['path']}: {f['title']} [{f['type']}{required}]{options}")
    return "\n".join(lines) if lines else f"{prefix}(no fields)"


def _option_value(field_def: dict[str, Any], given: Any) -> Any:
    """The stored value of a select option given by its value or its label."""
    text = str(given).strip()
    for option in field_def["options"]:
        if text == str(option.get("value")):
            return option.get("value")
    for option in field_def["options"]:
        if text.lower() == str(option.get("label", "")).strip().lower():
            return option.get("value")
    return None


def _coerce(field_def: dict[str, Any], value: Any) -> tuple[Any, str | None]:
    kind = field_def["type"]
    title = field_def["title"]
    if kind == "Score" and not isinstance(value, dict):
        try:
            score = int(str(value).strip())
        except ValueError:
            return None, f"'{title}' is a score from 1 to 4, not '{value}'"
        if not 1 <= score <= 4:
            return None, f"'{title}' is a score from 1 to 4, not {score}"
        return {"score": score}, None
    if kind == "ValueSelect" and field_def["options"] and not isinstance(value, dict):
        chosen = _option_value(field_def, value)
        if chosen is None:
            options = ", ".join(str(o.get("label")) for o in field_def["options"])
            return None, f"'{value}' is not an option of '{title}' ({options})"
        return chosen, None
    if kind == "MultiValueSelect" and field_def["options"]:
        values = value if isinstance(value, list) else id_list(value)
        chosen = [_option_value(field_def, v) for v in values]
        missing = [str(v) for v, c in zip(values, chosen, strict=True) if c is None]
        if missing:
            return None, f"{', '.join(missing)} not options of '{title}'"
        return chosen, None
    if kind == "RichText" and isinstance(value, str):
        return {"type": "PlainText", "value": value}, None
    if kind == "Boolean" and isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "1"):
            return True, None
        if lowered in ("false", "no", "0"):
            return False, None
    if kind == "Number" and isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip()), None
    return value, None


def build_field_submissions(form_definition: Any, values: Any) -> tuple[list[dict[str, Any]] | None, str | None]:
    """``[{path, value}]`` for a form submission.

    *values* is a ``{field: value}`` object - a field named by its path or its
    title - or a ``[{path, value}]`` list. With the form definition at hand,
    each field is checked against it and its value written the way Ashby
    takes it: a score as ``{"score": n}``, a select option by its stored value
    (its label is accepted), rich text as plain text.
    """
    if isinstance(values, list):
        pairs = []
        for item in values:
            if not isinstance(item, dict) or "path" not in item:
                return None, "Error: each submitted value is an object with 'path' and 'value'."
            pairs.append((str(item["path"]), item.get("value")))
    elif isinstance(values, dict):
        pairs = [(str(k), v) for k, v in values.items()]
    else:
        return None, "Error: give the values as a JSON object of field -> value."
    fields = form_fields(form_definition)
    if not fields:
        return [{"path": path, "value": value} for path, value in pairs], None
    by_path = {f["path"]: f for f in fields}
    by_title = {str(f["title"]).strip().lower(): f for f in fields}
    submissions: list[dict[str, Any]] = []
    problems: list[str] = []
    for key, value in pairs:
        field_def = by_path.get(key) or by_title.get(key.strip().lower())
        if field_def is None:
            problems.append(f"no field '{key}'")
            continue
        coerced, problem = _coerce(field_def, value)
        if problem:
            problems.append(problem)
            continue
        submissions.append({"path": field_def["path"], "value": coerced})
    if problems:
        return None, ("Error: " + "; ".join(problems) + ". The form's fields:\n" + describe_form(form_definition))
    return submissions, None


def render_submitted_values(form_definition: Any, submitted: Any, prefix: str = "    ") -> list[str]:
    """``Title: value`` for every submitted value, in the form's order."""
    if not isinstance(submitted, dict) or not submitted:
        return []
    lines: list[str] = []
    seen: set[str] = set()
    for f in form_fields(form_definition):
        if f["path"] not in submitted:
            continue
        seen.add(f["path"])
        raw = submitted[f["path"]]
        if f["options"]:
            labels = {str(o.get("value")): o.get("label") for o in f["options"]}
            if isinstance(raw, list):
                raw = [labels.get(str(v), v) for v in raw]
            elif isinstance(raw, (str, int)) and str(raw) in labels:
                raw = labels[str(raw)]
        text = format_value(raw)
        if text:
            lines.append(_value_line(prefix, f["title"], text))
    for path, raw in submitted.items():
        if path not in seen:
            text = format_value(raw)
            if text:
                lines.append(_value_line(prefix, path, text))
    return lines


def _value_line(prefix: str, title: str, text: str) -> str:
    if "\n" in text:
        return f"{prefix}{title}:\n{indent(text, prefix + '  ')}"
    return f"{prefix}{title}: {text}"
