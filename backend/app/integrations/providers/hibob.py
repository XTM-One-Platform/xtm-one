# SPDX-License-Identifier: Apache-2.0

"""HiBob integration — HR, People, Time Off, Tasks, Reports, Documents,
Goals, Job Catalog, Attendance, Workforce Planning, Custom Tables, Metadata.

API docs: https://apidocs.hibob.com/reference/getting-started-with-bob-api
Auth: Basic Auth (Service User ID : API Token, base64-encoded)
Base URL: https://api.hibob.com/v1
"""

from __future__ import annotations

import base64
import contextlib
import json as _json
import logging
from datetime import date as _date
from datetime import timedelta
from typing import Any
from urllib.parse import unquote

import httpx

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.directory import MAX_NAMED_REPORTS, DirectoryProfile
from app.integrations.file_delivery import (
    deliver_file,
    fetch_file,
    file_content_type,
    read_pipe_file,
    uploaded_file_answer,
    with_extension,
)
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_PROVIDER = "HiBob"
_BASE_URL = "https://api.hibob.com/v1"
_NAME_LOOKUP_LIMIT = 200
_MIN_PARTIAL_MATCH_LENGTH = 3
#: Largest employee document downloaded or uploaded.
_MAX_DOCUMENT_BYTES = 25 * 1024 * 1024


class HiBobIntegration(BaseIntegration):
    provider_name = "hibob"
    display_name = "HiBob"
    icon = "Users"
    description = (
        "Connect HiBob for HR — employee data, time off, goals, job catalog, "
        "tasks, reports, documents, attendance, and more."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        headers = _build_headers(token)
        async with _client() as client:
            resp = await client.get(
                f"{_BASE_URL}/profiles",
                headers=headers,
                params={"showInactive": "false"},
            )
            return resp.status_code == 200

    async def get_user_info(self, token: str) -> dict:
        headers = _build_headers(token)
        async with _client() as client:
            resp = await client.get(f"{_BASE_URL}/profiles", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                employees = data.get("employees", [])
                return {"status": "connected", "employee_count": len(employees)}
            return {"status": "connected"}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _client(timeout: int = 30):
    async with egress_client(timeout=timeout) as c:
        yield c


def _safe_parse_json(resp: httpx.Response, action: str) -> tuple[Any, str | None]:
    """Parse a HiBob HTTP response body as JSON, returning ``(data, None)``
    on success or ``(None, error_message)`` on a degenerate response.

    HiBob occasionally returns HTTP 2xx with an empty body or with content
    that is not valid JSON (most commonly during partial outages or for
    seldom-used endpoints).  Calling :py:meth:`httpx.Response.json` on
    those responses raises :class:`json.JSONDecodeError`, which used to
    propagate out of :func:`get_hibob_timeoff_balance` as an unhandled
    exception.

    This helper is the canonical place to convert those edge cases into
    user-facing tool error strings — adopting it at a call site lets
    that tool degrade gracefully instead of crashing on a degenerate
    response.

    .. note::
        **Migration status.** Today only
        :func:`get_hibob_timeoff_balance` calls into this helper. The
        remaining ~46 ``resp.json()``
        call sites in this provider still parse directly and would
        crash identically on the same edge cases — migrating them is
        tracked as a follow-up sweep. New call sites should use this
        helper instead of re-deriving the parse semantics.

    *action* is interpolated into the error message so the LLM gets a
    self-describing failure (e.g. ``"… while fetching time-off
    balance"``).

    The returned error strings follow this provider's (and
    :func:`app.integrations.http.format_http_error`'s) established
    ``"Error: …"`` convention so downstream consumers (and the LLM)
    recognise them as failures alongside the other error paths in
    this module.
    """
    if not resp.content:
        return None, (
            f"Error: HiBob API returned an empty response while {action}."
        )
    try:
        return resp.json(), None
    except _json.JSONDecodeError:
        return None, (
            f"Error: HiBob API returned an invalid response while {action}."
        )


def _is_closed_goal_update_error(resp: httpx.Response | None) -> bool:
    """Detect HiBob's "goal is closed" rejection on a goal update.

    HiBob answers ``PATCH /v1/goals/goals/{id}`` with HTTP 400 and a body
    like ``Goal <id> is closed and cannot be updated`` when the target goal
    is already closed. We inspect the actual response (status code + body)
    rather than string-matching the human-formatted error, so detection
    survives wording/formatting changes elsewhere. ``closed`` is the stable
    signal in HiBob's message.
    """
    if resp is None or resp.status_code != 400:
        return False
    try:
        body = resp.text or ""
    except Exception:
        return False
    return "closed" in body.lower()


def _document_name(doc: dict) -> str:
    """An employee document's name: ``documentName`` in the Docs API."""
    return str(doc.get("documentName") or doc.get("name") or doc.get("title") or "").strip() or "Unnamed"


def _document_link(doc: dict) -> str:
    """An employee document's ``downloadLink`` (Docs API), or ``""``."""
    return str(doc.get("downloadLink") or doc.get("downloadUrl") or doc.get("url") or "").strip()


def _match_document(docs: list[dict], document: str) -> tuple[dict | None, str | None]:
    """The document *document* names: its id, its exact name or its ``#position``."""
    wanted = str(document or "").strip()
    if not wanted:
        return None, (
            "Error: document is required - its id, exact name or #position from "
            "list_hibob_employee_documents."
        )
    for doc in docs:
        if doc.get("id") is not None and str(doc["id"]) == wanted:
            return doc, None
    named = [d for d in docs if _document_name(d).casefold() == wanted.casefold()]
    if len(named) == 1:
        return named[0], None
    if named:
        return None, (
            f"Error: {len(named)} documents are named '{wanted}' - name one by its id "
            "or #position from list_hibob_employee_documents."
        )
    position = wanted.removeprefix("#")
    if position.isdigit() and 1 <= int(position) <= len(docs):
        return docs[int(position) - 1], None
    names = ", ".join(_document_name(d) for d in docs[:20])
    return None, f"Error: no document '{wanted}' for this employee. Documents: {names}."


def _upload_endpoint(employee_id: str, folder_type: str, folder_id: str) -> tuple[str, str | None]:
    """The Docs API multipart upload endpoint of a folder, or the error."""
    kind = (folder_type or "shared").strip().lower()
    base = f"{_BASE_URL}/docs/people/{employee_id}"
    if kind == "shared":
        return f"{base}/shared/upload", None
    if kind == "confidential":
        return f"{base}/confidential/upload", None
    if kind == "custom":
        if not (folder_id or "").strip():
            return "", "Error: folder_id is required for folder_type=custom (see list_hibob_doc_folders)."
        return f"{base}/folders/{folder_id.strip()}/upload", None
    return "", f"Error: invalid folder_type '{folder_type}'. Must be one of: shared, confidential, custom."


def _named_like(name: str, original: str) -> str:
    """*name* when given (keeping *original*'s extension if it has none), else *original*."""
    name = (name or "").strip()
    if not name:
        return original
    if "." not in name and "." in original:
        return f"{name}.{original.rsplit('.', 1)[1]}"
    return name


def _uploaded_document_id(resp: httpx.Response) -> str:
    """The document id an upload answered with (a JSON object or a bare id)."""
    try:
        data = resp.json()
    except ValueError:
        return (resp.text or "").strip()[:64]
    if isinstance(data, dict):
        return str(data.get("id") or data.get("docId") or "")
    if isinstance(data, int | str):
        return str(data)
    return ""


def _url_file_name(url: str) -> str:
    try:
        return unquote(httpx.URL(url).path.rsplit("/", 1)[-1])
    except (httpx.InvalidURL, TypeError):
        return ""


def _build_headers(token: str) -> dict[str, str]:
    """Build Basic Auth headers from compound token ``service_user_id||api_key``."""
    service_user_id, api_key = parse_compound_token(token, 2)
    credentials = base64.b64encode(f"{service_user_id}:{api_key}".encode()).decode()
    return {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _normalize_for_name_match(value: str | None) -> str:
    """Return ``value`` casefolded with whitespace runs collapsed to a single space.

    The HiBob ``search_hibob_employees`` tool does client-side substring
    matching against a fetched name list.  The query the LLM sends is
    normalised once at the call site (``" ".join(query.strip().split()).casefold()``)
    but the candidate names the API returns can also contain weird
    whitespace — multiple spaces, leading/trailing spaces, non-breaking
    spaces — that ``.casefold()`` alone does not normalise.  Without
    matching normalisation on both sides, an upstream display name like
    ``"Jane   Doe"`` would silently fail to match the perfectly
    reasonable query ``"Jane Doe"``.

    This helper is the single source of truth for the normalisation rule
    used in the search filter (matching the existing
    ``" ".join(x.split()).casefold()`` shape used elsewhere in this
    module).  Returns an empty string for ``None`` so callers can pipe
    optional fields straight in without a guard.
    """
    if not value:
        return ""
    return " ".join(value.split()).casefold()


def _extract_field(emp: dict, *paths: str, default: str = "N/A") -> str:
    """Extract a field from a HiBob employee object, handling multiple formats.

    The HiBob API returns data in different structures depending on the endpoint
    and the ``humanReadable`` flag:
      - Flat nested: ``{"work": {"title": "Engineer"}}``
      - Slash-path with value wrapper: ``{"/work/title": {"value": "Engineer"}}``
      - Slash-path direct: ``{"/work/title": "Engineer"}``
      - humanReadable section: ``{"humanReadable": {"work": {"title": "Engineer"}}}``

    ``paths`` should be dot-separated like ``"work.title"`` or ``"root.email"``.
    """
    for path in paths:
        parts = path.replace("/", ".").strip(".").split(".")

        # 1) Slash-path key: /work/title
        slash_key = "/" + "/".join(parts)
        if slash_key in emp:
            val = emp[slash_key]
            if isinstance(val, dict):
                return str(val.get("value", val.get("humanReadable", default)))
            if val is not None:
                return str(val)

        # 2) Nested dict walk: work -> title
        node: Any = emp
        for p in parts:
            if p == "root":
                continue
            if isinstance(node, dict):
                node = node.get(p)
            else:
                node = None
                break
        if node is not None and not isinstance(node, (dict, list)):
            return str(node)
        if isinstance(node, dict):
            display = node.get("value") or node.get("displayName") or node.get("name")
            if display is not None:
                return str(display)

        # 3) humanReadable sub-object
        hr = emp.get("humanReadable", {})
        if isinstance(hr, dict):
            node = hr
            for p in parts:
                if p == "root":
                    continue
                if isinstance(node, dict):
                    node = node.get(p)
                else:
                    node = None
                    break
            if node is not None and not isinstance(node, (dict, list)):
                return str(node)

    return default


async def _resolve_employee_id(headers: dict, identifier: str) -> tuple[str, str]:
    """Resolve an identifier (ID, email, or display name) to a HiBob employee ID.

    Returns ``(employee_id, error_message)``.  If resolution succeeds,
    ``error_message`` is empty.
    """
    if not identifier:
        return "", "No employee identifier provided."
    stripped = identifier.strip()

    if stripped.isdigit():
        return stripped, ""

    body: dict[str, Any] = {
        "showInactive": True,
        "humanReadable": "REPLACE",
        "fields": ["root.id", "root.displayName", "root.email"],
    }
    is_email_lookup = "@" in stripped
    if is_email_lookup:
        body["filters"] = [
            {"fieldPath": "root.email", "operator": "equals", "values": [stripped]}
        ]
    else:
        body["limit"] = _NAME_LOOKUP_LIMIT
    async with _client() as client:
        resp, err = await safe_request(
            client,
            "post",
            f"{_BASE_URL}/people/search",
            _PROVIDER,
            f"resolve employee '{stripped}'",
            headers=headers,
            json=body,
        )
        if err:
            return "", err
        employees = resp.json().get("employees", [])
        if not employees:
            return "", f"No employee found matching '{stripped}'."

        if is_email_lookup:
            first_employee = employees[0]
            if not isinstance(first_employee, dict):
                return "", f"Malformed employee record returned for '{stripped}'."
            employee_id = _extract_field(
                first_employee,
                "root.id",
                default=str(first_employee.get("id", "")),
            ).strip()
            if not employee_id:
                return "", f"Employee record for '{stripped}' is missing an ID."
            return employee_id, ""

        normalized = " ".join(stripped.split()).casefold()
        allow_partial_match = len(normalized) >= _MIN_PARTIAL_MATCH_LENGTH
        exact_matches: list[tuple[str, str, str]] = []
        partial_matches: list[tuple[str, str, str]] = []

        for employee in employees:
            if not isinstance(employee, dict):
                continue

            employee_id = _extract_field(
                employee, "root.id", default=str(employee.get("id", ""))
            ).strip()
            if not employee_id:
                continue

            employee_email = _extract_field(
                employee, "root.email", default=str(employee.get("email", ""))
            ).strip()
            display_name = _extract_field(
                employee, "root.displayName", "root.fullName", default=""
            ).strip()
            if not display_name:
                first_name = str(employee.get("firstName", "")).strip()
                last_name = str(employee.get("surname", "")).strip()
                display_name = " ".join(p for p in [first_name, last_name] if p).strip()

            identity_tokens = {
                " ".join(employee_id.split()).casefold(),
                " ".join(employee_email.split()).casefold(),
                " ".join(display_name.split()).casefold(),
            }
            identity_tokens.discard("")
            if not identity_tokens:
                continue

            candidate = (employee_id, display_name or "Unknown", employee_email or "N/A")
            if normalized in identity_tokens:
                exact_matches.append(candidate)
                continue

            if allow_partial_match and any(normalized in token for token in identity_tokens):
                partial_matches.append(candidate)

        matches = exact_matches or partial_matches
        if not matches:
            if len(employees) >= _NAME_LOOKUP_LIMIT:
                return (
                    "",
                    f"No employee found matching '{stripped}' in the first {_NAME_LOOKUP_LIMIT} results. "
                    "Please retry with an exact email or employee ID.",
                )
            return "", f"No employee found matching '{stripped}'."
        if len(matches) > 1:
            options = ", ".join(
                f"{name} ({email}) [ID: {employee_id}]"
                for employee_id, name, email in matches[:5]
            )
            suffix = "..." if len(matches) > 5 else ""
            return (
                "",
                f"Multiple employees found matching '{stripped}': {options}{suffix}. "
                "Please retry with an exact email or employee ID.",
            )
        return matches[0][0], ""


# Well-known employee fields surfaced with friendly labels in the detailed
# view.  Everything else returned by the API (including custom fields in any
# category) is appended afterwards under its canonical dotted path so nothing
# is ever silently dropped.
_EMPLOYEE_LABELLED_FIELDS: list[tuple[str, str]] = [
    ("work.title", "Title"),
    ("work.department", "Department"),
    ("work.site", "Site"),
    ("work.startDate", "Start Date"),
    ("work.manager", "Manager"),
    ("work.reportsTo", "Reports To"),
    ("work.seniority", "Seniority"),
    ("work.tenure", "Tenure"),
    ("about.gender", "Gender"),
    ("about.nationality", "Nationality"),
    ("about.birthday", "Birthday"),
    ("personal.shortBirthDate", "Birth Date"),
    ("internal.status", "Status"),
    ("internal.lifecycleStatus", "Lifecycle Status"),
]

# Identity / structural top-level keys already rendered explicitly, or that add
# no value to a flattened dump (avatars, raw timestamps, the duplicate
# ``humanReadable`` branch produced by ``humanReadable="APPEND"``).
_EMPLOYEE_FLATTEN_SKIP_ROOTS: frozenset[str] = frozenset(
    {
        "id",
        "displayName",
        "fullName",
        "firstName",
        "surname",
        "secondName",
        "email",
        "avatarUrl",
        "creationDateTime",
        "humanReadable",
    }
)


def _coerce_scalar(value: Any) -> str | None:
    """Render a HiBob value-wrapper / scalar to a display string.

    HiBob scalars arrive either bare (``"Engineer"``) or wrapped as
    ``{"value": ..., "humanReadable": ...}``.  The human-readable form is
    preferred when present and non-empty.  Returns ``None`` for empty values
    or containers that should be walked instead of rendered.
    """
    if isinstance(value, dict):
        if "humanReadable" in value or "value" in value:
            human = value.get("humanReadable")
            if human not in (None, "") and not isinstance(human, (dict, list)):
                return str(human)
            # ``humanReadable`` is missing, empty, or itself a container we
            # cannot render as a scalar — fall back to the raw value so a
            # usable scalar is never silently dropped (matches the "nothing is
            # ever silently dropped" contract of the employee flattener).
            raw = value.get("value")
            if raw in (None, ""):
                return None
            return None if isinstance(raw, (dict, list)) else str(raw)
        return None
    if isinstance(value, list):
        return None
    if value in (None, ""):
        return None
    return str(value)


def _slash_value(fields_obj: Any, *field_ids: str, default: str = "N/A") -> str:
    """Read a slash-keyed value-wrapper from an object-search response.

    Object/search endpoints (positions, attendance) return each row as
    ``{"/object/fieldId": {"value": ..., "humanReadable": ...}}``.  This reads
    the first present *field_id*, preferring the human-readable form.
    """
    if not isinstance(fields_obj, dict):
        return default
    for field_id in field_ids:
        if field_id in fields_obj:
            scalar = _coerce_scalar(fields_obj[field_id])
            if scalar is not None:
                return scalar
    return default


def _is_value_wrapper(node: dict) -> bool:
    """True when *node* is a HiBob ``{"value"/"humanReadable": ...}`` scalar
    leaf rather than a nested category object.

    Only nodes whose keys are a subset of ``{"value", "humanReadable"}`` are
    treated as leaves; objects like a currency ``{"value": 32, "currency":
    "USD"}`` are walked so the sibling data (currency) is not lost.
    """
    if "value" not in node and "humanReadable" not in node:
        return False
    return all(key in ("value", "humanReadable") for key in node)


def _flatten_employee_fields(emp: dict) -> dict[str, str]:
    """Flatten a HiBob employee object to ``{dotted.path: display_value}``.

    Walks nested category objects (``work``, ``about``, custom ``category_*``
    …), descends into ``custom`` sub-objects, and normalises slash-path keys
    (``/work/custom/field_x`` → ``work.custom.field_x``).  Value-wrappers are
    collapsed via :func:`_coerce_scalar`.  List values (historical tables)
    are skipped — those have dedicated table tools.

    This is the single source of truth that lets the employee read tools
    surface *every* returned field, including custom fields such as an Equity
    / Option Grant column, instead of silently dropping anything that is not
    in a hard-coded map.
    """
    out: dict[str, str] = {}

    def _walk(node: Any, prefix: str) -> None:
        if isinstance(node, dict):
            if prefix and _is_value_wrapper(node):
                scalar = _coerce_scalar(node)
                if scalar is not None:
                    out[prefix] = scalar
                return
            for key, val in node.items():
                norm = str(key).strip("/").replace("/", ".")
                if not norm:
                    continue
                if not prefix and norm in _EMPLOYEE_FLATTEN_SKIP_ROOTS:
                    continue
                child = f"{prefix}.{norm}" if prefix else norm
                _walk(val, child)
        elif isinstance(node, list):
            return
        elif prefix:
            scalar = _coerce_scalar(node)
            if scalar is not None:
                out[prefix] = scalar

    _walk(emp, "")
    return out


#: Most requested-but-absent field ids listed by name in an employee read.
_MAX_WITHHELD_LISTED = 60


def _returned_paths(emp: dict[str, Any]) -> set[str]:
    """Every dotted path that carries a non-null value in *emp*, any depth.

    Unlike :func:`_flatten_employee_fields` it keeps dict and list nodes, so a
    table or an object field counts as returned. Slash-path keys
    (``/work/title``) are normalised to dotted paths.
    """
    paths: set[str] = set()

    def _walk(node: Any, prefix: str) -> None:
        if node is None:
            return
        if prefix:
            paths.add(prefix)
        if isinstance(node, dict):
            if prefix and _is_value_wrapper(node) and _coerce_scalar(node) is None:
                paths.discard(prefix)
                return
            for key, val in node.items():
                norm = str(key).strip("/").replace("/", ".")
                if norm:
                    _walk(val, f"{prefix}.{norm}" if prefix else norm)

    _walk(emp, "")
    return paths


def _withheld_field_ids(emp: dict[str, Any], requested: list[str]) -> list[str]:
    """The requested field ids the People read did not return.

    HiBob drops a field the service user may not read without an error, and an
    empty field the same way: the caller names both so an absent indicator is
    never read as "not set". ``root.`` ids are matched on the top-level key
    the API answers them under.
    """
    present = _returned_paths(emp)
    missing: list[str] = []
    for fid in requested:
        if not isinstance(fid, str) or not fid:
            continue
        bare = fid.removeprefix("root.")
        if fid in present or bare in present:
            continue
        missing.append(fid)
    return missing


def _withheld_lines(missing: list[str]) -> list[str]:
    """Render the withheld-field footer of an employee read."""
    if not missing:
        return []
    shown = missing[:_MAX_WITHHELD_LISTED]
    more = len(missing) - len(shown)
    lines = [
        f"Requested fields not returned ({len(missing)}): withheld by the "
        "service user's permissions, or empty for this employee:",
        "  " + ", ".join(shown),
    ]
    if more:
        lines.append(f"  ... and {more} more")
    return lines


def _custom_field_lines(emp: dict) -> list[str]:
    """Return ``"path: value"`` strings for any custom fields/columns present
    in *emp*.  Custom data uses the ``…custom.field_*`` /
    ``…customColumns.column_*`` id patterns (see Fields metadata)."""
    return [
        f"{path}: {val}"
        for path, val in _flatten_employee_fields(emp).items()
        if ".custom." in path or ".customColumns." in path
    ]


def _format_employee(emp: dict) -> str:
    """Format a single employee record to a readable one-liner.

    Any custom fields present in the record (returned only when explicitly
    requested via ``fields_json``) are appended so a search that asks for a
    custom column — e.g. an Option Grant — actually shows its value instead
    of silently dropping it.
    """
    eid = _extract_field(emp, "root.id", default=str(emp.get("id", "N/A")))
    name = (
        _extract_field(emp, "root.displayName", "root.fullName", default="")
        or f"{emp.get('firstName', '')} {emp.get('surname', '')}".strip()
        or "Unknown"
    )
    email = _extract_field(emp, "root.email", default=str(emp.get("email", "N/A")))
    title = _extract_field(emp, "work.title")
    department = _extract_field(emp, "work.department")
    site = _extract_field(emp, "work.site")
    start = _extract_field(emp, "work.startDate")

    line = (
        f"{name} | Email: {email} | Title: {title} "
        f"| Department: {department} | Site: {site} "
        f"| Start Date: {start} | ID: {eid}"
    )
    custom = _custom_field_lines(emp)
    if custom:
        line += " | " + " | ".join(custom)
    return line


def _format_employee_detail(emp: dict) -> str:
    """Format a single employee with all available scalar fields.

    Renders the well-known fields with friendly labels first, then appends
    every remaining scalar field (including custom fields in any category)
    under its canonical dotted path.  No scalar field is silently dropped —
    this is what makes custom data such as an Equity / Option Grant field
    visible in the read tools.  List-valued fields (historical tables) are
    intentionally skipped by ``_flatten_employee_fields``; those have
    dedicated table tools.
    """
    eid = _extract_field(emp, "root.id", default=str(emp.get("id", "N/A")))
    name = (
        _extract_field(emp, "root.displayName", "root.fullName", default="")
        or f"{emp.get('firstName', '')} {emp.get('surname', '')}".strip()
        or "Unknown"
    )
    email = _extract_field(emp, "root.email", default=str(emp.get("email", "N/A")))

    lines = [f"Employee ID: {eid}", f"Name: {name}", f"Email: {email}"]
    shown: set[str] = {
        "root.id",
        "id",
        "root.displayName",
        "root.fullName",
        "displayName",
        "fullName",
        "root.email",
        "email",
    }

    for path, label in _EMPLOYEE_LABELLED_FIELDS:
        val = _extract_field(emp, path, default="")
        if val:
            lines.append(f"{label}: {val}")
            # Only suppress this path from the ``extras`` dump when it was
            # actually rendered here.  ``_extract_field`` and
            # ``_flatten_employee_fields`` can disagree (e.g. a value-wrapper
            # with only ``humanReadable`` populated yields "" from the former
            # but a scalar from the latter); marking it shown unconditionally
            # would silently drop that scalar.
            shown.add(path)

    # Address — composed from individual lines.  As above, only mark a
    # sub-field shown when it actually contributed, so a value the flattener
    # surfaces (but ``_extract_field`` missed) still appears under ``extras``.
    addr_fields = ("line1", "city", "state", "country", "postCode")
    addr_parts: list[str] = []
    for f in addr_fields:
        val = _extract_field(emp, f"address.{f}", default="")
        if val:
            addr_parts.append(val)
            shown.add(f"address.{f}")
    addr_str = ", ".join(addr_parts)
    if addr_str:
        lines.append(f"Address: {addr_str}")

    # Append every other scalar field returned by the API — including custom
    # fields in any category — so nothing is silently dropped.
    extras = [
        f"{path}: {val}"
        for path, val in sorted(_flatten_employee_fields(emp).items())
        if path not in shown
    ]
    if extras:
        lines.append("Additional fields:")
        lines.extend(f"  {extra}" for extra in extras)

    return "\n".join(lines)


def _format_timeoff(entry: dict) -> str:
    emp_name = entry.get("employeeName", entry.get("employeeDisplayName", "Unknown"))
    emp_id = entry.get("employeeId", "N/A")
    policy = entry.get("policyTypeDisplayName", entry.get("policyType", "N/A"))
    start = entry.get("startDate", "N/A")
    end = entry.get("endDate", "N/A")
    status = entry.get("status", "N/A")
    request_id = entry.get("requestId", entry.get("id", "N/A"))
    return (
        f"{emp_name} | Policy: {policy} | {start} → {end} "
        f"| Status: {status} | Employee ID: {emp_id} | Request ID: {request_id}"
    )


def _fmt_task_person(val: Any) -> str:
    """Render a Task ``owner`` / ``requestedFor`` sub-object (a HiBob
    ``TaskEmployee``) as ``Name (email, id=...)``.

    Falls back to a bare id/string for the truncated shapes HiBob sometimes
    returns (e.g. a raw employee id in a webhook-style payload).
    """
    if isinstance(val, dict):
        name = val.get("displayName") or " ".join(
            p for p in (val.get("firstName"), val.get("surname")) if p
        ).strip()
        extra: list[str] = []
        if val.get("email"):
            extra.append(str(val["email"]))
        if val.get("id"):
            extra.append(f"id={val['id']}")
        if name:
            return f"{name} ({', '.join(extra)})" if extra else name
        # No name on this (truncated) TaskEmployee - fall back to the bare
        # email/id so the subject/owner stays actionable, rather than emitting
        # a useless "Unknown (id=...)".
        if extra:
            return ", ".join(extra)
        return "Unknown"
    if val in (None, ""):
        return "N/A"
    return str(val)


def _task_person_haystack(val: Any) -> str:
    """Lower-cased blob of every identifying field on a ``TaskEmployee``.

    Used for client-side ``requested_for`` / ``assignee`` substring filtering,
    since HiBob's Tasks API exposes no such query parameters.
    """
    if isinstance(val, dict):
        return " ".join(
            str(val.get(k, ""))
            for k in ("id", "displayName", "firstName", "surname", "email")
        ).lower()
    return str(val or "").lower()


def _format_task(t: Any) -> str:
    """Format a single HiBob general task as a readable one-liner.

    Surfaces every meaningful field from the Tasks API ``Task`` schema - most
    importantly ``requestedFor`` (the subject employee a task is *for*), which
    the previous formatter dropped entirely. Also reads the *correct* field
    names (``due`` and the ``owner`` object, not the non-existent ``dueDate`` /
    ``employeeId`` the old code probed).
    """
    if not isinstance(t, dict):
        return f"  {t}"
    tid = t.get("id", "N/A")
    title = t.get("title") or t.get("description") or "Untitled"
    parts = [
        f"[{tid}] {title}",
        f"Requested for: {_fmt_task_person(t.get('requestedFor'))}",
        f"Assignee: {_fmt_task_person(t.get('owner'))}",
        f"Workflow: {t.get('workflow') or t.get('set') or 'N/A'}",
        f"Due: {t.get('due') or 'N/A'}",
        f"Status: {t.get('status') or 'N/A'}",
    ]
    if t.get("completionDate"):
        parts.append(f"Completed: {t['completionDate']}")
    if t.get("linkInBob"):
        parts.append(f"Link: {t['linkInBob']}")
    return "  " + " | ".join(parts)


def _filter_tasks(
    tasks: list[Any],
    *,
    requested_for: str = "",
    assignee: str = "",
    workflow: str = "",
    status: str = "",
) -> list[Any]:
    """Apply the client-side filters the HiBob Tasks API does not support.

    HiBob's ``GET /tasks`` and ``GET /tasks/people/{id}`` return whole task
    lists with no ``requestedFor`` / assignee / workflow query parameters (only
    ``task_status`` on the per-employee route). These filters let an agent
    answer "which offboarding tasks are *requested for* employee X" - the exact
    gap that previously forced guesswork. Matching is case-insensitive
    substring against the relevant field(s).
    """
    rf = requested_for.strip().lower()
    asg = assignee.strip().lower()
    wf = workflow.strip().lower()
    st = status.strip().lower()
    if not any((rf, asg, wf, st)):
        return tasks
    out: list[Any] = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        if rf and rf not in _task_person_haystack(t.get("requestedFor")):
            continue
        if asg and asg not in _task_person_haystack(t.get("owner")):
            continue
        if wf and wf not in str(t.get("workflow") or t.get("set") or "").lower():
            continue
        if st and st not in str(t.get("status") or "").lower():
            continue
        out.append(t)
    return out


def _json_dump(data: Any, limit: int = 3000) -> str:
    return _json.dumps(data, indent=2, default=str)[:limit]


def _parse_json_param(value: str) -> Any | None:
    if not value:
        return None
    with contextlib.suppress(Exception):
        return _json.loads(value)
    return None


# Cap on field IDs requested in one People search/read call. HiBob allows up
# to 400 field IDs in ``fields[]`` (see People read API contract).
_MAX_PEOPLE_FIELD_IDS = 400


async def _all_employee_field_ids(headers: dict) -> list[str]:
    """Return employee field ids from ``GET /company/people/fields``.

    Used to request custom fields (e.g. an Equity / Option Grant column) that
    live outside the default root/about/employment/work categories and are
    therefore never returned by a fieldless People read.  The result is capped
    at ``_MAX_PEOPLE_FIELD_IDS`` (400) — the maximum number of field ids the
    People read API accepts in one ``fields[]`` request — so in unusually large
    schemas the tail of the field list is not requested.  Returns ``[]`` on any
    failure so callers degrade to the API's default field set instead of
    crashing.  Calling this metadata endpoint requires no special permissions;
    the actual values are still subject to the service user's category
    permissions (silently omitted otherwise).
    """
    async with _client() as client:
        resp, err = await safe_request(
            client,
            "get",
            f"{_BASE_URL}/company/people/fields",
            _PROVIDER,
            "list employee field ids",
            headers=headers,
        )
        if err:
            return []
        data, parse_err = _safe_parse_json(resp, "listing employee field ids")
        if parse_err or data is None:
            return []
    fields = data if isinstance(data, list) else data.get("fields", [])
    ids: list[str] = []
    for field in fields:
        if isinstance(field, dict):
            fid = field.get("id")
            if isinstance(fid, str) and fid:
                ids.append(fid)
    return ids[:_MAX_PEOPLE_FIELD_IDS]


#: Field types whose stored value is the id of a company list item.
_LIST_FIELD_TYPES = frozenset({"list", "multi-list", "hierarchy-list"})
#: Longest range ``get_hibob_whos_out`` reads for one employee.
_WHOS_OUT_EMPLOYEE_MAX_DAYS = 731


def _list_ids_by_column(fields: list[Any], category: str, columns: set[str]) -> dict[str, str]:
    """Map each table column holding a list item id to its company list id.

    *fields* is the ``GET /company/people/fields`` answer: a list-type field
    carries ``typeData.listId``, the id ``GET /company/named-lists/{id}``
    reads.  A column is matched on ``<category>.<column>`` first (``work.title``
    for the work table), then on any field id ending in ``.<column>``.
    """
    exact: dict[str, str] = {}
    loose: dict[str, str] = {}
    for field in fields:
        if not isinstance(field, dict) or field.get("type") not in _LIST_FIELD_TYPES:
            continue
        type_data = field.get("typeData")
        list_id = type_data.get("listId") if isinstance(type_data, dict) else None
        fid = field.get("id")
        if not isinstance(list_id, str) or not list_id or not isinstance(fid, str):
            continue
        column = fid.rsplit(".", 1)[-1]
        if column not in columns:
            continue
        if fid == f"{category}.{column}":
            exact[column] = list_id
        else:
            loose.setdefault(column, list_id)
    return {**loose, **exact}


def _list_item_names(items: Any) -> dict[str, str]:
    """Flatten a named list's items (nested children included) to id -> name."""
    names: dict[str, str] = {}
    stack = list(items) if isinstance(items, list) else []
    while stack:
        item = stack.pop()
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("value")
        if item.get("id") is not None and isinstance(name, str) and name:
            names[str(item["id"])] = name
        children = item.get("children")
        if isinstance(children, list):
            stack.extend(children)
    return names


async def _list_column_names(headers: dict[str, str], category: str, entries: list[Any]) -> dict[str, dict[str, str]]:
    """Resolve the list-typed columns of employee table *entries* to names.

    Answers ``{column: {item id: item name}}`` for the columns the field
    metadata types as a list; one metadata read and one read per list.  Any
    failure answers what was resolved so far: the caller then prints the raw
    id, as before.
    """
    columns = {
        str(key)
        for entry in entries
        if isinstance(entry, dict)
        for key, value in entry.items()
        if isinstance(value, (str, int)) and not isinstance(value, bool)
    }
    if not columns:
        return {}
    async with _client() as client:
        resp, err = await safe_request(
            client,
            "get",
            f"{_BASE_URL}/company/people/fields",
            _PROVIDER,
            "list employee fields",
            headers=headers,
        )
        if err or resp is None:
            return {}
        data, parse_err = _safe_parse_json(resp, "listing employee fields")
        if parse_err or data is None:
            return {}
        fields = data if isinstance(data, list) else data.get("fields", [])
        list_ids = _list_ids_by_column(fields if isinstance(fields, list) else [], category, columns)
        resolved: dict[str, dict[str, str]] = {}
        by_list: dict[str, dict[str, str]] = {}
        for column, list_id in list_ids.items():
            if list_id not in by_list:
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/company/named-lists/{list_id}",
                    _PROVIDER,
                    f"get company list '{list_id}'",
                    headers=headers,
                    params={"includeArchived": "true"},
                )
                if err or resp is None:
                    by_list[list_id] = {}
                else:
                    body, parse_err = _safe_parse_json(resp, "reading a company list")
                    items = body.get("items") if isinstance(body, dict) else body
                    by_list[list_id] = {} if parse_err else _list_item_names(items)
            if by_list[list_id]:
                resolved[column] = by_list[list_id]
    return resolved


def _whos_out_range_error(from_date: str, to_date: str) -> str:
    """Check the range of an employee's time-off read; '' when it is valid."""
    if not from_date or not to_date:
        return "Error: from_date and to_date are required when employee is given."
    try:
        start = _date.fromisoformat(from_date[:10])
        end = _date.fromisoformat(to_date[:10])
    except ValueError:
        return "Error: from_date and to_date must be ISO dates (YYYY-MM-DD)."
    if end < start:
        return "Error: to_date is before from_date."
    if (end - start).days > _WHOS_OUT_EMPLOYEE_MAX_DAYS:
        return "Error: the range of an employee's time off is at most 24 months."
    return ""


#: Days a performance snapshot reads by default (the last twelve months).
_SNAPSHOT_DEFAULT_DAYS = 365

#: Why a snapshot has no review section: the public Bob API has no reviews module.
_REVIEWS_NOT_IN_PUBLIC_API = (
    "Not available: the public Bob API exposes no performance review cycles, "
    "answers or ratings. They are read through Bob MCP (an MCP server at "
    "https://mcp.hibob.com/mcp/public, connected with OAuth by a Bob user)."
)


def _snapshot_range(from_date: str, to_date: str) -> tuple[str, str]:
    """The snapshot's time-off range: the given dates, else the last twelve months."""
    to_date = to_date.strip() or _date.today().isoformat()
    if from_date.strip():
        return from_date.strip(), to_date
    try:
        end = _date.fromisoformat(to_date[:10])
    except ValueError:
        return "", to_date
    return (end - timedelta(days=_SNAPSHOT_DEFAULT_DAYS)).isoformat(), to_date


def _timeoff_by_policy_lines(outs: list[dict[str, Any]], from_date: str, to_date: str) -> list[str]:
    """One employee's time-off entries, a total of entries and days per policy first."""
    if not outs:
        return [f"No time off from {from_date} to {to_date}."]
    totals: dict[str, list[int]] = {}
    for entry in outs:
        policy = str(entry.get("policyTypeDisplayName") or entry.get("policyType") or "Unknown policy")
        try:
            days = (
                _date.fromisoformat(str(entry.get("endDate", ""))[:10])
                - _date.fromisoformat(str(entry.get("startDate", ""))[:10])
            ).days + 1
        except ValueError:
            days = 0
        bucket = totals.setdefault(policy, [0, 0])
        bucket[0] += 1
        bucket[1] += max(days, 0)
    lines = ["By policy (calendar days, half days counted whole):"]
    lines.extend(f"  {policy}: {n} request(s), {days} day(s)" for policy, (n, days) in sorted(totals.items()))
    lines.append("Entries:")
    lines.extend(f"  {_format_timeoff(entry)}" for entry in outs)
    return lines


#: Default fields of a goal search (``progress`` is the goal's average KR progress).
_GOAL_FIELDS: tuple[str, ...] = (
    "id",
    "title",
    "status",
    "progress",
    "owner",
    "dueDate",
    "isPrivate",
    "typeId",
    "cycleId",
    "description",
    "startDate",
    "alignedGoal",
)
#: Fields read for each key result.
_KEY_RESULT_FIELDS: tuple[str, ...] = (
    "id",
    "title",
    "measureType",
    "startValue",
    "target",
    "currentValue",
    "currency",
    "currencySymbol",
    "progress",
    "dueDate",
)
#: Pages a goal search walks when it filters client-side (owner, cycle).
_GOAL_SEARCH_MAX_PAGES = 50


def _goal_value(fields: Any, key: str, default: Any = None) -> Any:
    """A value of a Goals API ``fields`` dict: ``{"key": {"value": ...}}``."""
    entry = fields.get(key) if isinstance(fields, dict) else None
    if isinstance(entry, dict):
        return entry.get("value", default)
    return default


def _format_goal_line(fields: Any) -> str:
    """One goal of a search, on one line."""
    gid = _goal_value(fields, "id", "N/A")
    title = _goal_value(fields, "title", "Untitled")
    status = _goal_value(fields, "status", "N/A")
    owner = _goal_value(fields, "owner", "N/A")
    due = _goal_value(fields, "dueDate", "N/A")
    parts = [f"[{gid}] {title}", f"Status: {status}"]
    progress = _goal_value(fields, "progress")
    if progress is not None:
        parts.append(f"Progress: {progress}%")
    parts += [f"Owner: {owner}", f"Due: {due}"]
    cycle = _goal_value(fields, "cycleId")
    if cycle is not None:
        parts.append(f"CycleID: {cycle}")
    type_id = _goal_value(fields, "typeId")
    if type_id is not None:
        parts.append(f"TypeID: {type_id}")
    return " | ".join(parts)


def _format_key_result(fields: Any) -> str:
    """One key result, on one line."""
    kid = _goal_value(fields, "id", "N/A")
    title = _goal_value(fields, "title", "Untitled")
    measure = _goal_value(fields, "measureType", "N/A")
    target = _goal_value(fields, "target", "N/A")
    current = _goal_value(fields, "currentValue", 0)
    currency = _goal_value(fields, "currency")
    unit = f" {currency}" if currency else ""
    parts = [f"[{kid}] {title}", f"Measure: {measure}"]
    start = _goal_value(fields, "startValue")
    if start is not None:
        parts.append(f"Start: {start}{unit}")
    parts += [f"Target: {target}{unit}", f"Current: {current}{unit}"]
    progress = _goal_value(fields, "progress")
    if progress is not None:
        parts.append(f"Progress: {progress}%")
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Directory lookup (services/sender_directory.py)
# ---------------------------------------------------------------------------

#: The only fields a directory lookup requests. Anything else the service
#: user could read (pay, performance, personal contact, health, equity) is
#: never requested, so it never reaches the platform. A field id the account
#: does not hold is silently omitted by the API.
_DIRECTORY_FIELDS: tuple[str, ...] = (
    "root.id",
    "root.email",
    "root.displayName",
    "work.title",
    "work.department",
    "work.division",
    "work.site",
    "work.siteTimezone",
    "work.startDate",
    "work.reportsTo",
)
#: The fields read of every employee to name a manager and direct reports.
_DIRECTORY_ROSTER_FIELDS: tuple[str, ...] = (
    "root.id",
    "root.displayName",
    "work.title",
    "work.reportsTo",
)
_DIRECTORY_TIMEOUT_SECONDS = 10


def _directory_value(emp: dict[str, Any], path: str) -> str:
    value = _extract_field(emp, path, default="").strip()
    return "" if value in {"N/A", "None"} else value


def _manager_id(emp: dict[str, Any]) -> str:
    """The id of the employee *emp* reports to, in any of the API's shapes."""
    for key in ("/work/reportsTo", "/work/reportsTo/id"):
        value = emp.get(key)
        if isinstance(value, dict):
            inner = value.get("value", value)
            value = inner.get("id") if isinstance(inner, dict) else inner
        if value:
            return str(value)
    work = emp.get("work")
    if isinstance(work, dict):
        reports_to = work.get("reportsTo")
        if isinstance(reports_to, dict) and reports_to.get("id"):
            return str(reports_to["id"])
        if isinstance(reports_to, str) and reports_to:
            return reports_to
    return ""


async def _directory_search(
    headers: dict[str, str], fields: tuple[str, ...], email: str = ""
) -> list[dict[str, Any]]:
    body: dict[str, Any] = {"showInactive": False, "fields": list(fields)}
    if email:
        body["filters"] = [{"fieldPath": "root.email", "operator": "equals", "values": [email]}]
    async with _client(_DIRECTORY_TIMEOUT_SECONDS) as client:
        resp, err = await safe_request(
            client, "post", f"{_BASE_URL}/people/search", _PROVIDER, "read the directory", headers=headers, json=body
        )
    if err or resp is None:
        raise RuntimeError(err or "HiBob directory read failed")
    employees = resp.json().get("employees", [])
    return [emp for emp in employees if isinstance(emp, dict)]


async def lookup_directory_profile(token: str, email: str) -> DirectoryProfile | None:
    """The directory profile of *email* (``app.integrations.directory``).

    ``None`` when no active employee has that email; raises on any API
    failure. Two reads: the person, then the roster (id, name, title,
    manager) for their manager's title and their direct reports.
    """
    headers = _build_headers(token)
    matches = await _directory_search(headers, _DIRECTORY_FIELDS, email=email.strip())
    if not matches:
        return None
    person = matches[0]
    person_id = _directory_value(person, "root.id") or str(person.get("id") or "")
    manager_id = _manager_id(person)
    roster = await _directory_search(headers, _DIRECTORY_ROSTER_FIELDS)
    manager_name = manager_title = ""
    reports: list[str] = []
    for emp in roster:
        emp_id = _directory_value(emp, "root.id") or str(emp.get("id") or "")
        if manager_id and emp_id == manager_id:
            manager_name = _directory_value(emp, "root.displayName")
            manager_title = _directory_value(emp, "work.title")
        if person_id and emp_id != person_id and _manager_id(emp) == person_id:
            reports.append(_directory_value(emp, "root.displayName"))
    named = tuple(sorted(name for name in reports if name)) if len(reports) <= MAX_NAMED_REPORTS else ()
    return DirectoryProfile(
        display_name=_directory_value(person, "root.displayName"),
        title=_directory_value(person, "work.title"),
        department=_directory_value(person, "work.department"),
        sub_department=_directory_value(person, "work.division"),
        site=_directory_value(person, "work.site"),
        timezone=_directory_value(person, "work.siteTimezone"),
        manager_name=manager_name,
        manager_title=manager_title,
        direct_report_count=len(reports),
        direct_report_names=named,
        start_date=_directory_value(person, "work.startDate"),
    )


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    """Build HiBob integration tools."""
    headers = _build_headers(token)
    persist_file_fn = _kwargs.get("persist_file_fn")

    # ── People / Employees ────────────────────────────────────────────────

    async def search_hibob_employees(
        email: str = "",
        employee_id: str = "",
        show_inactive: bool = False,
        fields_json: str = "",
        count: int = 50,
        query: str = "",
    ) -> str:
        # ``query`` is a convenience alias accepted when the LLM sends a generic
        # search term. Route it to the appropriate filter:
        #   - Contains "@"  → treat as an exact email filter
        #   - Otherwise     → fetch up to _NAME_LOOKUP_LIMIT employees, then apply
        #                     the client-side name filter; results may miss matches
        #                     outside that capped subset
        name_filter: str = ""
        if query and not email and not employee_id:
            if "@" in query:
                email = query.strip()
            else:
                # Apply ``_normalize_for_name_match`` on BOTH sides of
                # the substring check below — the candidate must be
                # normalised the same way as the query, otherwise an
                # upstream display name like ``"Jane   Doe"`` (multiple
                # spaces) or ``"Jane\u00a0Doe"`` (non-breaking space)
                # would never match a query of ``"Jane Doe"``.  The
                # helper is also reused as a single source of truth so
                # a future tweak to the normalisation rules doesn't
                # have to be replicated in every call site.
                name_filter = _normalize_for_name_match(query)

        body: dict[str, Any] = {
            "showInactive": show_inactive,
            "humanReadable": "REPLACE",
        }
        parsed_fields = _parse_json_param(fields_json)
        if parsed_fields:
            body["fields"] = parsed_fields
        if name_filter:
            # Cap the API fetch to avoid pulling the entire directory for name searches,
            # mirroring the _NAME_LOOKUP_LIMIT guard in _resolve_employee_id.
            body["limit"] = _NAME_LOOKUP_LIMIT
        filters: list[dict] = []
        if email:
            filters.append(
                {"fieldPath": "root.email", "operator": "equals", "values": [email]}
            )
        if employee_id:
            filters.append(
                {"fieldPath": "root.id", "operator": "equals", "values": [employee_id]}
            )
        if filters:
            body["filters"] = filters

        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/people/search",
                _PROVIDER,
                "search employees",
                headers=headers,
                json=body,
            )
            if err:
                return err
            employees = resp.json().get("employees", [])
            raw_count = len(employees)
            if name_filter:
                employees = [
                    emp
                    for emp in employees
                    if name_filter
                    in _normalize_for_name_match(
                        _extract_field(emp, "root.displayName", "root.fullName", default="")
                        or f"{emp.get('firstName', '')} {emp.get('surname', '')}".strip()
                    )
                ]
            if not employees:
                if name_filter and raw_count >= _NAME_LOOKUP_LIMIT:
                    return (
                        f"No employees found matching '{query}' in the first {_NAME_LOOKUP_LIMIT} results. "
                        "The directory may contain more records — retry with an exact email or employee ID for a precise match."
                    )
                return "No employees found matching the criteria."
            total = len(employees)
            limited = employees[:count]
            result = format_entity_list(
                limited, _format_employee, noun="employee", total=total
            )
            if total > count:
                result += f"\n(Showing {count} of {total} — increase count or add filters to see more)"
            return result

    async def get_hibob_employee(
        identifier: str, fields_json: str = "", include_custom_fields: bool = True
    ) -> str:
        resolved_id, err = await _resolve_employee_id(headers, identifier)
        if err:
            return err

        body: dict[str, Any] = {"humanReadable": "REPLACE"}
        parsed_fields = _parse_json_param(fields_json)
        if parsed_fields:
            body["fields"] = parsed_fields
        elif include_custom_fields:
            # A fieldless People read only returns the default categories
            # (root/about/employment/work), so custom fields living in other
            # categories — e.g. an Equity / Option Grant field — are silently
            # omitted. Request every field id from metadata so custom data is
            # included by default. Falls back to the API default set if the
            # metadata lookup yields nothing.
            all_ids = await _all_employee_field_ids(headers)
            if all_ids:
                body["fields"] = all_ids

        async with _client() as client:
            resp, read_err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/people/{resolved_id}",
                _PROVIDER,
                f"get employee {resolved_id}",
                headers=headers,
                json=body,
            )
            if read_err or resp is None:
                return read_err or f"Error: HiBob API returned no response while getting employee {resolved_id}."
            data, parse_err = _safe_parse_json(resp, f"getting employee {resolved_id}")
            if parse_err:
                return parse_err
            emp = data if isinstance(data, dict) else {}
            lines = [_format_employee_detail(emp)]
            requested = body.get("fields")
            if isinstance(requested, list):
                lines.extend(_withheld_lines(_withheld_field_ids(emp, requested)))
            return "\n".join(lines)

    async def list_hibob_profiles(count: int = 100) -> str:
        body: dict[str, Any] = {
            "showInactive": False,
            "humanReadable": "REPLACE",
            "fields": [
                "root.id",
                "root.displayName",
                "root.email",
                "work.title",
                "work.department",
                "work.site",
                "work.startDate",
            ],
        }
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/people/search",
                _PROVIDER,
                "list employee profiles",
                headers=headers,
                json=body,
            )
            if err:
                return err
            employees = resp.json().get("employees", [])
            if not employees:
                return "No active employee profiles found."
            total = len(employees)
            limited = employees[:count]
            result = format_entity_list(
                limited, _format_employee, noun="active employee", total=total
            )
            if total > count:
                result += f"\n(Showing {count} of {total} — increase count to see more)"
            return result

    async def create_hibob_employee(
        first_name: str,
        surname: str,
        email: str,
        site: str,
        start_date: str,
        department: str = "",
        title: str = "",
        extra_fields_json: str = "",
    ) -> str:
        body: dict[str, Any] = {
            "firstName": first_name,
            "surname": surname,
            "email": email,
            "work": {"site": site, "startDate": start_date},
        }
        if department:
            body["work"]["department"] = department
        if title:
            body["work"]["title"] = title
        extra = _parse_json_param(extra_fields_json)
        if isinstance(extra, dict):
            body.update(extra)

        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/people",
                _PROVIDER,
                "create employee",
                headers=headers,
                json=body,
            )
            if err:
                return err
            return (
                f"Employee created successfully. ID: {resp.json().get('id', 'unknown')}"
            )

    async def update_hibob_employee(employee_id: str, fields_json: str) -> str:
        resolved_id, err = await _resolve_employee_id(headers, employee_id)
        if err:
            return err
        fields = _parse_json_param(fields_json)
        if not isinstance(fields, dict):
            return 'Error: Invalid JSON for fields. Example: {"work": {"title": "CTO"}}'

        async with _client() as client:
            resp, err = await safe_request(
                client,
                "put",
                f"{_BASE_URL}/people/{resolved_id}",
                _PROVIDER,
                f"update employee {resolved_id}",
                headers=headers,
                json=fields,
                expected_statuses={304},
            )
            if err:
                if resp is not None and resp.status_code == 304:
                    return f"Employee {resolved_id} record is already up to date."
                return err
            return f"Employee {resolved_id} updated successfully."

    async def terminate_hibob_employee(
        employee_id: str,
        termination_date: str,
        termination_reason: str = "",
        notice_period_start: str = "",
    ) -> str:
        resolved_id, err = await _resolve_employee_id(headers, employee_id)
        if err:
            return err
        body: dict[str, Any] = {"terminationDate": termination_date}
        if termination_reason:
            body["terminationReason"] = termination_reason
        if notice_period_start:
            body["noticePeriodStartDate"] = notice_period_start

        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/employees/{resolved_id}/terminate",
                _PROVIDER,
                f"terminate employee {resolved_id}",
                headers=headers,
                json=body,
            )
            if err:
                return err
            return f"Employee {resolved_id} terminated. Effective date: {termination_date}."

    # ── Employee Tables ───────────────────────────────────────────────────

    _table_endpoints: dict[str, str] = {
        "work": "work",
        "employment": "employment",
        "lifecycle": "lifecycle",
        "salary": "salaries",
        "equity": "equities",
        "variable_payments": "variable",
        "training": "training",
    }
    _table_labels: dict[str, str] = {
        "work": "Work history",
        "employment": "Employment history",
        "lifecycle": "Lifecycle history",
        "salary": "Salary history",
        "equity": "Equity grants",
        "variable_payments": "Variable payments",
        "training": "Training records",
    }

    async def get_hibob_employee_table(employee_id: str, table_name: str) -> str:
        endpoint = _table_endpoints.get(table_name)
        if not endpoint:
            return f"Invalid table_name '{table_name}'. Must be one of: {', '.join(_table_endpoints)}."
        label = _table_labels[table_name]

        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/people/{employee_id}/{endpoint}",
                _PROVIDER,
                f"get {label.lower()} for {employee_id}",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            entries = data.get("values", data) if isinstance(data, dict) else data
            if not entries:
                return f"No {label.lower()} found for employee {employee_id}."
            if isinstance(entries, list):
                names = await _list_column_names(headers, table_name, entries)
                lines = [
                    f"{label} for employee {employee_id} ({len(entries)} entries):"
                ]
                for e in entries:
                    if not isinstance(e, dict):
                        continue
                    parts: list[str] = []
                    for k, v in e.items():
                        if k == "id":
                            continue
                        if isinstance(v, dict):
                            v = v.get("displayName", str(v))
                        elif k in names and str(v) in names[k]:
                            v = f"{names[k][str(v)]} (id {v})"
                        if v is not None and v != "":
                            parts.append(f"{k}: {v}")
                    lines.append(f"  {' | '.join(parts)}")
                return "\n".join(lines)
            return _json_dump(entries)

    async def add_hibob_employee_table_entry(
        employee_id: str,
        table_name: str,
        entry_json: str,
    ) -> str:
        endpoint = _table_endpoints.get(table_name)
        if not endpoint:
            return f"Invalid table_name '{table_name}'. Must be one of: {', '.join(_table_endpoints)}."
        entry = _parse_json_param(entry_json)
        if not isinstance(entry, dict):
            return "Error: entry_json must be a valid JSON object."

        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/people/{employee_id}/{endpoint}",
                _PROVIDER,
                f"add {_table_labels[table_name].lower()} entry for {employee_id}",
                headers=headers,
                json=entry,
            )
            if err:
                return err
            return f"Entry added to {_table_labels[table_name].lower()} for employee {employee_id}."

    # ── Time Off ──────────────────────────────────────────────────────────

    async def get_hibob_whos_out(
        from_date: str = "",
        to_date: str = "",
        include_pending: bool = False,
        employee: str = "",
    ) -> str:
        employee_id = ""
        if employee.strip():
            range_error = _whos_out_range_error(from_date, to_date)
            if range_error:
                return range_error
            employee_id, err = await _resolve_employee_id(headers, employee)
            if err:
                return err
        params: dict[str, str] = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if include_pending:
            params["includePending"] = "true"

        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/timeoff/whosout",
                _PROVIDER,
                "get who's out",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
            outs = data.get("outs", data) if isinstance(data, dict) else data
            if employee_id and isinstance(outs, list):
                outs = [o for o in outs if isinstance(o, dict) and str(o.get("employeeId", "")) == employee_id]
                if not outs:
                    return f"No time off for employee {employee_id} from {from_date} to {to_date}."
            if not outs or (isinstance(outs, list) and len(outs) == 0):
                return "No one is out of office for the specified period."
            if isinstance(outs, list):
                return format_entity_list(outs, _format_timeoff, noun="time-off entry")
            return _json_dump(outs)

    async def get_hibob_out_today(date: str = "") -> str:
        params: dict[str, str] = {}
        if date:
            params["date"] = date
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/timeoff/outtoday",
                _PROVIDER,
                "get who's out today",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
            outs = data.get("outs", data) if isinstance(data, dict) else data
            if not outs or (isinstance(outs, list) and len(outs) == 0):
                return "No one is out of office today."
            if isinstance(outs, list):
                return format_entity_list(outs, _format_timeoff, noun="time-off entry")
            return _json_dump(outs)

    async def get_hibob_timeoff_balance(
        employee_id: str,
        policy_type: str = "",
        date: str = "",
    ) -> str:
        # ``policy_type`` keeps a default (even though the JSON schema marks it
        # required) so an LLM call that omits it routes through the friendly
        # check below rather than raising a TypeError: the tool executor only
        # filters kwargs by signature and does not enforce schema ``required``,
        # so a required positional param would surface a raw exception string
        # instead of this actionable message. policyType is required by the
        # API; use list_hibob_timeoff_policy_types to discover valid values.
        if not policy_type:
            return (
                "Error: policy_type is required. Use list_hibob_timeoff_policy_types "
                "to discover valid policy type names (e.g. 'Holiday')."
            )
        params: dict[str, str] = {"policyType": policy_type}
        if date:
            params["date"] = date

        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/timeoff/employees/{employee_id}/balance",
                _PROVIDER,
                f"get time-off balance for {employee_id}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data, parse_err = _safe_parse_json(
                resp,
                f"fetching time-off balance for employee {employee_id}",
            )
            if parse_err:
                return parse_err
            # A list of balances (legacy/aggregate shape).
            balances = data.get("balances") if isinstance(data, dict) else None
            if isinstance(balances, list):
                if not balances:
                    return f"No time-off balance data found for employee {employee_id}."
                lines = [f"Time-off balances for employee {employee_id}:"]
                for b in balances:
                    policy = b.get("policyType", b.get("policyName", "N/A"))
                    balance = b.get("balance", "N/A")
                    used = b.get("used", "N/A")
                    pending = b.get("pending", "N/A")
                    lines.append(
                        f"  {policy} | Balance: {balance} | Used: {used} | Pending: {pending}"
                    )
                return "\n".join(lines)
            # The documented single-object BalanceResult shape.
            if isinstance(data, dict) and data:
                total = data.get("totalBalanceAsOfDate")
                rounded = data.get("totalRoundedBalanceAsOfDate")
                point = data.get("pointInTime", date or "today")
                if total is not None or rounded is not None:
                    shown = rounded if rounded is not None else total
                    return (
                        f"Time-off balance for employee {employee_id} "
                        f"(policy '{policy_type}', as of {point}): {shown}"
                    )
            # Detect API-level error fields returned inside an otherwise-valid
            # JSON body (HiBob occasionally responds HTTP 2xx with an error
            # object such as {"errorMessage": "…"} instead of the expected
            # balance schema — returning a clear error here stops the agent
            # from retrying a call that will never succeed).  Checked only
            # after the recognized balance shapes above so a valid payload
            # that happens to also carry a benign informational key is never
            # misclassified as a failure.
            if isinstance(data, dict):
                api_err = next(
                    (
                        v
                        for v in (
                            data.get("errorMessage"),
                            data.get("message"),
                            data.get("error"),
                        )
                        if isinstance(v, str) and v
                    ),
                    None,
                )
                if api_err:
                    return (
                        f"Error: HiBob returned an error for employee {employee_id} "
                        f"time-off balance (policy: {policy_type}): {api_err}"
                    )
            # Guard: empty or completely unrecognized shape — return a clear
            # terminal error so the agent does not loop retrying a call whose
            # response shape is never going to match the expected schema.
            if not data:
                return (
                    f"Error: HiBob returned no time-off balance data for employee "
                    f"{employee_id} (policy: {policy_type}). "
                    f"The employee ID or policy type may be invalid."
                )
            return _json_dump(data)

    async def submit_hibob_timeoff_request(
        employee_id: str,
        policy_type: str,
        start_date: str,
        end_date: str,
        request_type: str = "days",
        description: str = "",
        duration: int = 0,
        start_date_portion: str = "",
        end_date_portion: str = "",
    ) -> str:
        body: dict[str, Any] = {
            "policyType": policy_type,
            "startDate": start_date,
            "endDate": end_date,
            "type": request_type,
        }
        if description:
            body["description"] = description
        if duration:
            body["duration"] = duration
        if start_date_portion:
            body["startDatePortion"] = start_date_portion
        if end_date_portion:
            body["endDatePortion"] = end_date_portion

        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/timeoff/employees/{employee_id}/requests",
                _PROVIDER,
                f"submit time-off request for {employee_id}",
                headers=headers,
                json=body,
            )
            if err:
                return err
            return (
                f"Time-off request submitted successfully for employee {employee_id}."
            )

    async def get_hibob_timeoff_request(employee_id: str, request_id: str) -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/timeoff/employees/{employee_id}/requests/{request_id}",
                _PROVIDER,
                f"get time-off request {request_id}",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            lines = [
                f"Request ID: {data.get('id', request_id)}",
                f"Employee ID: {data.get('employeeId', employee_id)}",
                f"Policy: {data.get('policyTypeDisplayName', data.get('policyType', 'N/A'))}",
                f"Status: {data.get('status', 'N/A')}",
                f"Start: {data.get('startDate', 'N/A')}",
                f"End: {data.get('endDate', 'N/A')}",
                f"Type: {data.get('type', 'N/A')}",
            ]
            if data.get("description"):
                lines.append(f"Description: {data['description']}")
            if data.get("totalCost") is not None:
                lines.append(f"Total Cost: {data['totalCost']}")
            return "\n".join(lines)

    async def cancel_hibob_timeoff_request(employee_id: str, request_id: str) -> str:
        async with _client() as client:
            _, err = await safe_request(
                client,
                "delete",
                f"{_BASE_URL}/timeoff/employees/{employee_id}/requests/{request_id}",
                _PROVIDER,
                f"cancel time-off request {request_id}",
                headers=headers,
            )
            if err:
                return err
            return f"Time-off request {request_id} cancelled successfully."

    async def list_hibob_timeoff_policy_types() -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/timeoff/policy-types",
                _PROVIDER,
                "list time-off policy types",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            types = data.get("types", data) if isinstance(data, dict) else data
            if isinstance(types, list):
                if not types:
                    return "No time-off policy types found."
                lines = [f"Found {len(types)} time-off policy type(s):"]
                for t in types:
                    name = (
                        t.get("name", t.get("policyType", "N/A"))
                        if isinstance(t, dict)
                        else str(t)
                    )
                    lines.append(f"  - {name}")
                return "\n".join(lines)
            return _json_dump(data)

    async def get_hibob_timeoff_policy_details(policy_type: str) -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/timeoff/policy-types/{policy_type}",
                _PROVIDER,
                f"get policy type details for '{policy_type}'",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            lines = [f"Policy type: {policy_type}"]
            for k, v in data.items():
                if v is not None and not isinstance(v, (dict, list)):
                    lines.append(f"  {k}: {v}")
            for p in data.get("policies", []):
                if isinstance(p, dict):
                    lines.append(
                        f"  Policy: [{p.get('id', 'N/A')}] {p.get('name', p.get('policyName', 'N/A'))}"
                    )
            return "\n".join(lines)

    async def list_hibob_timeoff_policies() -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/timeoff/policies",
                _PROVIDER,
                "list time-off policies",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            policies = data.get("policies", data) if isinstance(data, dict) else data
            if isinstance(policies, list):
                if not policies:
                    return "No time-off policies found."
                lines = [f"Found {len(policies)} time-off policy(ies):"]
                for p in policies:
                    if isinstance(p, dict):
                        lines.append(
                            f"  [{p.get('id', 'N/A')}] {p.get('name', 'N/A')} "
                            f"| Type: {p.get('policyType', 'N/A')} | Unit: {p.get('unit', 'N/A')}"
                        )
                return "\n".join(lines)
            return _json_dump(data)

    async def list_hibob_timeoff_reason_codes(policy_type: str) -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/timeoff/policy-types/{policy_type}/reason-codes",
                _PROVIDER,
                f"get reason codes for '{policy_type}'",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            codes = data.get("reasonCodes", data) if isinstance(data, dict) else data
            if isinstance(codes, list):
                if not codes:
                    return f"No reason codes found for policy type '{policy_type}'."
                lines = [f"Reason codes for '{policy_type}' ({len(codes)}):"]
                for c in codes:
                    if isinstance(c, dict):
                        lines.append(
                            f"  [{c.get('id', 'N/A')}] {c.get('name', c.get('code', 'N/A'))}"
                        )
                    else:
                        lines.append(f"  - {c}")
                return "\n".join(lines)
            return _json_dump(data)

    async def add_hibob_timeoff_reason_code(
        policy_type: str, name: str, code: str = ""
    ) -> str:
        body: dict[str, Any] = {"name": name}
        if code:
            body["code"] = code
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/timeoff/policy-types/{policy_type}/reason-codes",
                _PROVIDER,
                f"add reason code to '{policy_type}'",
                headers=headers,
                json=body,
            )
            if err:
                return err
            return f"Reason code '{name}' added to policy type '{policy_type}'."

    async def get_hibob_timeoff_changes(since: str = "", until: str = "") -> str:
        params: dict[str, str] = {}
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/timeoff/requests/changes",
                _PROVIDER,
                "get time-off changes",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
            changes = data.get("changes", data) if isinstance(data, dict) else data
            if isinstance(changes, list):
                return format_entity_list(
                    changes,
                    _format_timeoff,
                    noun="time-off change",
                    empty_msg="No time-off changes found for the specified period.",
                )
            return _json_dump(data)

    async def create_hibob_timeoff_balance_adjustment(
        employee_id: str,
        policy_type: str,
        amount: float,
        effective_date: str = "",
        reason: str = "",
    ) -> str:
        body: dict[str, Any] = {"policyType": policy_type, "amount": amount}
        if effective_date:
            body["effectiveDate"] = effective_date
        if reason:
            body["reason"] = reason
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/timeoff/employees/{employee_id}/adjustments",
                _PROVIDER,
                f"create balance adjustment for {employee_id}",
                headers=headers,
                json=body,
            )
            if err:
                return err
            return f"Balance adjustment of {amount} for policy '{policy_type}' created for employee {employee_id}."

    # ── Tasks ─────────────────────────────────────────────────────────────

    async def list_hibob_tasks(
        requested_for: str = "",
        assignee: str = "",
        workflow: str = "",
    ) -> str:
        """List OPEN company-wide tasks (``GET /v1/tasks``).

        HiBob's ``/tasks`` route only ever returns *open* tasks for the whole
        company and accepts no query parameters, so ``requested_for`` /
        ``assignee`` / ``workflow`` are applied client-side. For *closed* tasks
        (e.g. completed offboarding steps) use ``get_hibob_employee_tasks``
        with ``task_status="closed"``.
        """
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/tasks",
                _PROVIDER,
                "list tasks",
                headers=headers,
            )
            if err:
                return err
            data, parse_err = _safe_parse_json(resp, "listing tasks")
            if parse_err:
                return parse_err
            tasks = data.get("tasks", data) if isinstance(data, dict) else data
            if not isinstance(tasks, list):
                return _json_dump(data)
            filtered = _filter_tasks(
                tasks,
                requested_for=requested_for,
                assignee=assignee,
                workflow=workflow,
            )
            has_filter = bool(
                requested_for.strip() or assignee.strip() or workflow.strip()
            )
            if has_filter:
                empty = (
                    "No OPEN tasks matched the given filter(s). Note: /tasks "
                    "returns OPEN tasks only - for closed tasks call "
                    "get_hibob_employee_tasks with task_status='closed'."
                )
            else:
                empty = "No open tasks found."
            result = format_entity_list(
                filtered, _format_task, noun="open task", empty_msg=empty
            )
            if filtered and len(filtered) != len(tasks):
                result = (
                    f"(Filtered to {len(filtered)} of {len(tasks)} open "
                    f"company task(s).)\n{result}"
                )
            return result

    async def get_hibob_employee_tasks(
        employee_id: str,
        task_status: str = "",
        requested_for: str = "",
        workflow: str = "",
    ) -> str:
        """Read tasks for a specific employee (``GET /v1/tasks/people/{id}``).

        ``employee_id`` accepts a HiBob ID, an email, or a display name. This
        route is keyed to the task **owner/assignee**, so to find every task
        *requested for* someone (e.g. a leaver) pull the assignee's list and
        pass ``requested_for`` to filter by the subject employee.

        ``task_status`` maps to HiBob's only native task filter
        (``open`` / ``closed``; ``all`` or omit returns everything). HiBob's
        enum is open/closed only, so ``all`` is honoured by simply not sending
        the param. ``requested_for`` / ``workflow`` are applied client-side.
        """
        status = task_status.strip().lower()
        if status and status not in ("open", "closed", "all"):
            return "Error: task_status must be 'open', 'closed', 'all', or empty."
        resolved_id, err = await _resolve_employee_id(headers, employee_id)
        if err:
            return err
        # HiBob's task_status enum is open/closed only, and omitting it returns
        # all tasks. Treat "all" (and empty) as "return everything" by NOT
        # sending the param - forwarding a non-enum value risks a 400.
        params: dict[str, str] = {}
        if status in ("open", "closed"):
            params["task_status"] = status
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/tasks/people/{resolved_id}",
                _PROVIDER,
                f"get tasks for {resolved_id}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data, parse_err = _safe_parse_json(resp, "reading employee tasks")
            if parse_err:
                return parse_err
            tasks = data.get("tasks", data) if isinstance(data, dict) else data
            if not isinstance(tasks, list):
                return _json_dump(data)
            scope = f"employee {resolved_id}"
            if status:
                scope += f" ({status})"
            filtered = _filter_tasks(
                tasks, requested_for=requested_for, workflow=workflow
            )
            has_filter = bool(requested_for.strip() or workflow.strip())
            empty = (
                f"No tasks matched the given filter(s) for {scope}."
                if has_filter
                else f"No tasks found for {scope}."
            )
            result = format_entity_list(
                filtered, _format_task, noun="task", empty_msg=empty
            )
            if filtered and len(filtered) != len(tasks):
                result = (
                    f"(Filtered to {len(filtered)} of {len(tasks)} task(s) for "
                    f"{scope}.)\n{result}"
                )
            return result

    async def complete_hibob_task(task_id: str) -> str:
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/tasks/{task_id}/complete",
                _PROVIDER,
                f"complete task {task_id}",
                headers=headers,
            )
            if err:
                return err
            return f"Task {task_id} marked as complete."

    # ── Reports ───────────────────────────────────────────────────────────

    async def list_hibob_reports() -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/company/reports",
                _PROVIDER,
                "list reports",
                headers=headers,
            )
            if err:
                return err
            data, parse_err = _safe_parse_json(resp, "listing reports")
            if parse_err:
                return parse_err
            # The Read company reports endpoint returns {"views": [...]} — each
            # report has id/name/type/description/createdBy/creationDate/
            # folderId (there is no "category" field). The old code looked for a
            # non-existent "reports" key, fell back to JSON-dumping the whole
            # truncated payload, and never surfaced the report IDs.
            if isinstance(data, dict):
                reports = data.get("views", data.get("reports", []))
            elif isinstance(data, list):
                reports = data
            else:
                reports = []
            if not isinstance(reports, list) or not reports:
                return "No reports found."
            lines = [f"Found {len(reports)} report(s):"]
            for r in reports:
                if not isinstance(r, dict):
                    lines.append(f"  {r}")
                    continue
                rid = r.get("id", "N/A")
                name = r.get("name", "Unnamed")
                detail = []
                if r.get("type"):
                    detail.append(f"Type: {r['type']}")
                if r.get("folderId"):
                    detail.append(f"Folder: {r['folderId']}")
                if r.get("creationDate"):
                    detail.append(f"Created: {r['creationDate']}")
                if r.get("createdBy"):
                    detail.append(f"By: {r['createdBy']}")
                suffix = f" | {' | '.join(detail)}" if detail else ""
                lines.append(f"  [{rid}] {name}{suffix}")
                if r.get("description"):
                    lines.append(f"      {r['description']}")
            return "\n".join(lines)

    async def download_hibob_report(
        report_id: str, report_format: str = "csv", human_readable: bool = True
    ) -> str | ToolResult:
        # ``format`` is a REQUIRED query parameter on the download endpoint;
        # omitting it returns HTTP 400. Allowed values are csv, xlsx and json.
        fmt = (report_format or "csv").strip().lower()
        if fmt not in ("csv", "xlsx", "json"):
            return (
                f"Error: invalid report_format '{report_format}'. "
                "Must be one of: csv, xlsx, json."
            )
        params: dict[str, str] = {"format": fmt}
        # humanReadable is only honoured for the json format (resolves list
        # IDs to their display values).
        if fmt == "json" and human_readable:
            params["humanReadable"] = "REPLACE"
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/company/reports/{report_id}/download",
                _PROVIDER,
                f"download report {report_id}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            ct = resp.headers.get("content-type", "")
            raw = resp.content or b""
            if fmt == "xlsx":
                # A spreadsheet reads as a file: stored for the run, its
                # extracted cells come with the result.
                return await deliver_file(
                    data=raw,
                    filename=f"hibob-report-{report_id}.xlsx",
                    content_type=(
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    ),
                    source="HiBob",
                    persist_file_fn=persist_file_fn,
                    details=[f"Report {report_id} downloaded as XLSX ({len(raw)} bytes)."],
                )
            stored = ""
            if raw and persist_file_fn is not None:
                ext = "json" if fmt == "json" or "json" in ct else "csv"
                try:
                    ref = await persist_file_fn(
                        f"hibob-report-{report_id}.{ext}", raw, "application/json" if ext == "json" else "text/csv"
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("HiBob report persistence failed: %s", format_exception_message(exc))
                    ref = None
                if ref is not None:
                    stored = (
                        f"\n\nThe whole report is stored as a reusable file: [[FILE:{ref.file_id}]] "
                        f"- file_id: {ref.file_id}; forward it to any tool that takes a file, and "
                        "include the marker verbatim so the user gets a download card."
                    )
            if fmt == "json" or "json" in ct:
                data, parse_err = _safe_parse_json(
                    resp, f"downloading report {report_id}"
                )
                if parse_err:
                    return parse_err
                return _json_dump(data, 5000) + stored
            text = resp.text[:5000]
            return (
                f"Report {report_id} content (CSV):\n{text}{stored}"
                if text.strip()
                else f"Report {report_id} downloaded but content is empty."
            )

    # ── Documents ─────────────────────────────────────────────────────────

    async def _employee_documents(employee_id: str) -> tuple[list[dict] | None, Any, str | None]:
        """``(documents, raw answer, error)`` of ``GET /docs/people/{id}``."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/docs/people/{employee_id}",
                _PROVIDER,
                f"list documents for {employee_id}",
                headers=headers,
            )
        if err:
            return None, None, err
        data, parse_err = _safe_parse_json(resp, f"listing documents for {employee_id}")
        if parse_err:
            return None, None, parse_err
        docs = (
            data.get("documents", data.get("docs", data))
            if isinstance(data, dict)
            else data
        )
        if not isinstance(docs, list):
            return None, data, None
        return [d for d in docs if isinstance(d, dict)], data, None

    async def list_hibob_employee_documents(employee_id: str) -> str:
        docs, raw, err = await _employee_documents(employee_id)
        if err:
            return err
        if docs is None:
            return _json_dump(raw)
        if not docs:
            return f"No documents found for employee {employee_id}."
        lines = [f"Documents for employee {employee_id} ({len(docs)} doc(s)):"]
        for position, d in enumerate(docs, start=1):
            detail = [f"#{position}"]
            if d.get("id") is not None:
                detail.append(f"id: {d['id']}")
            folder = d.get("folderName") or d.get("folder") or d.get("folderType")
            if folder:
                detail.append(f"Folder: {folder}")
            uploaded = d.get("creationDate") or d.get("uploadedAt") or d.get("createdDate")
            if uploaded:
                detail.append(f"Uploaded: {uploaded}")
            lines.append(f"  {_document_name(d)} | {' | '.join(detail)}")
            link = _document_link(d)
            if link:
                lines.append(f"      Download URL: {link}")
        lines.append(
            "\nTip: download_hibob_document(employee_id, document) stores one as a file "
            "(by id, exact name or #position)."
        )
        return "\n".join(lines)

    async def download_hibob_document(employee_id: str, document: str) -> str | ToolResult:
        docs, _raw, err = await _employee_documents(employee_id)
        if err:
            return err
        if not docs:
            return f"Error: employee {employee_id} has no documents."
        doc, match_err = _match_document(docs, document)
        if match_err:
            return match_err
        name = _document_name(doc)
        link = _document_link(doc)
        if not link:
            return f"Error: HiBob gave no download link for '{name}'."
        # The Basic credential goes to HiBob's own hosts only; a signed
        # storage link is fetched without it.
        try:
            host = (httpx.URL(link).host or "").lower()
        except httpx.InvalidURL:
            return f"Error: HiBob gave an invalid download link for '{name}': {link}"
        fetch_headers = (
            {"Authorization": headers["Authorization"]}
            if host == "hibob.com" or host.endswith(".hibob.com")
            else {}
        )
        async with _client(timeout=60) as client:
            fetched = await fetch_file(
                client,
                "get",
                link,
                provider=_PROVIDER,
                action=f"download document '{name}'",
                max_bytes=_MAX_DOCUMENT_BYTES,
                headers=fetch_headers,
                follow_redirects=True,
            )
        if isinstance(fetched, str):
            return f"{fetched}\nDownload URL: {link}"
        filename = fetched.filename or name
        content_type = file_content_type(fetched.content_type, filename)
        return await deliver_file(
            data=fetched.data,
            filename=with_extension(filename, content_type),
            content_type=content_type,
            source="HiBob",
            persist_file_fn=persist_file_fn,
            links=[("Download URL", link)],
            details=[f"Document of employee {employee_id}."],
        )

    async def upload_hibob_document(
        employee_id: str,
        file_id: str,
        folder_type: str = "shared",
        folder_id: str = "",
        document_name: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        endpoint, folder_err = _upload_endpoint(employee_id, folder_type, folder_id)
        if folder_err:
            return ToolResult(tool_call_id="", content=folder_err, is_error=True)
        piped = await read_pipe_file(
            file_context, file_id, max_bytes=_MAX_DOCUMENT_BYTES, target="HiBob"
        )
        if isinstance(piped, str):
            return ToolResult(tool_call_id="", content=piped, is_error=True)
        data, piped_name, content_type = piped
        file_id = file_id.strip()
        filename = _named_like(document_name, piped_name)
        upload_headers = {k: v for k, v in headers.items() if k != "Content-Type"}
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                endpoint,
                _PROVIDER,
                f"upload document for {employee_id}",
                headers=upload_headers,
                files={"file": (filename, data, content_type)},
            )
        if err:
            return err
        doc_id = _uploaded_document_id(resp)
        return uploaded_file_answer(
            f"Uploaded file {file_id} to the {folder_type} folder of employee {employee_id}"
            + (f". Doc ID: {doc_id}" if doc_id else "."),
            target=_PROVIDER,
            filename=filename,
        )

    async def list_hibob_doc_folders() -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/docs/folders/metadata",
                _PROVIDER,
                "list document folders",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            folders = data if isinstance(data, list) else data.get("folders", [])
            if not folders:
                return "No document folders found."
            lines = [f"Found {len(folders)} document folder(s):"]
            for f in folders:
                if isinstance(f, dict):
                    lines.append(
                        f"  [{f.get('id', 'N/A')}] {f.get('name', 'Unnamed')} | Type: {f.get('type', 'N/A')}"
                    )
            return "\n".join(lines)

    async def upload_hibob_document_url(
        employee_id: str,
        url: str,
        folder_type: str = "shared",
        folder_id: str = "",
        document_name: str = "",
    ) -> str:
        if folder_type == "confidential":
            endpoint = f"{_BASE_URL}/docs/people/{employee_id}/confidential"
        elif folder_type == "custom" and folder_id:
            endpoint = f"{_BASE_URL}/docs/people/{employee_id}/custom/{folder_id}"
        else:
            endpoint = f"{_BASE_URL}/docs/people/{employee_id}/shared"
        name = (document_name or "").strip() or _url_file_name(url) or "Document"

        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                endpoint,
                _PROVIDER,
                f"upload document for {employee_id}",
                headers=headers,
                json={"documentName": name, "documentUrl": url},
            )
            if err:
                return err
            doc_id = _uploaded_document_id(resp) or "unknown"
            return f"Document '{name}' uploaded to {folder_type} folder for employee {employee_id}. Doc ID: {doc_id}"

    async def delete_hibob_document(
        employee_id: str,
        doc_id: str,
        folder_type: str = "shared",
        folder_id: str = "",
    ) -> str:
        if folder_type == "confidential":
            endpoint = f"{_BASE_URL}/docs/people/{employee_id}/confidential/{doc_id}"
        elif folder_type == "custom" and folder_id:
            endpoint = (
                f"{_BASE_URL}/docs/people/{employee_id}/folders/{folder_id}/{doc_id}"
            )
        else:
            endpoint = f"{_BASE_URL}/docs/people/{employee_id}/shared/{doc_id}"

        async with _client() as client:
            _, err = await safe_request(
                client,
                "delete",
                endpoint,
                _PROVIDER,
                f"delete document {doc_id} for {employee_id}",
                headers=headers,
            )
            if err:
                return err
            return f"Document {doc_id} deleted from {folder_type} folder for employee {employee_id}."

    # ── Metadata / Fields ─────────────────────────────────────────────────

    async def get_hibob_employee_fields_metadata() -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/company/people/fields",
                _PROVIDER,
                "get employee fields metadata",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            fields = data if isinstance(data, list) else data.get("fields", [])
            if not fields:
                return "No employee fields found."
            lines = [f"Found {len(fields)} employee field(s):"]
            for f in fields:
                if isinstance(f, dict):
                    fid = f.get("id", "N/A")
                    name = f.get("name", "N/A")
                    ftype = f.get("type", "N/A")
                    cat = f.get("categoryDisplayName", f.get("category", "N/A"))
                    lines.append(f"  {fid} | {name} | Type: {ftype} | Category: {cat}")
            return "\n".join(lines)

    async def create_hibob_field(
        name: str, category: str, field_type: str, description: str = ""
    ) -> str:
        body: dict[str, Any] = {"name": name, "category": category, "type": field_type}
        if description:
            body["description"] = description
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/company/people/fields",
                _PROVIDER,
                f"create field '{name}'",
                headers=headers,
                json=body,
            )
            if err:
                return err
            fid = (
                resp.json().get("id", "unknown")
                if resp.status_code < 300
                else "unknown"
            )
            return f"Field '{name}' created successfully. ID: {fid}"

    async def list_hibob_company_lists() -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/company/named-lists",
                _PROVIDER,
                "list company lists",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            lists = data if isinstance(data, list) else data.get("lists", [])
            if not lists:
                return "No company lists found."
            lines = [f"Found {len(lists)} company list(s):"]
            for lst in lists:
                if isinstance(lst, dict):
                    lines.append(
                        f"  {lst.get('name', 'N/A')} ({len(lst.get('items', []))} items)"
                    )
                else:
                    lines.append(f"  {lst}")
            return "\n".join(lines)

    async def get_hibob_company_list(list_name: str) -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/company/named-lists/{list_name}",
                _PROVIDER,
                f"get company list '{list_name}'",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            items = (
                data.get("items", data.get("values", data))
                if isinstance(data, dict)
                else data
            )
            if isinstance(items, list):
                if not items:
                    return f"Company list '{list_name}' is empty."
                lines = [f"Company list '{list_name}' ({len(items)} items):"]
                for item in items:
                    if isinstance(item, dict):
                        lines.append(
                            f"  [{item.get('id', 'N/A')}] {item.get('name', item.get('value', 'N/A'))}"
                        )
                    else:
                        lines.append(f"  {item}")
                return "\n".join(lines)
            return _json_dump(data)

    async def add_hibob_list_item(
        list_name: str, name: str, parent_id: str = ""
    ) -> str:
        body: dict[str, Any] = {"name": name}
        if parent_id:
            body["parentId"] = parent_id
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/company/named-lists/{list_name}",
                _PROVIDER,
                f"add item to list '{list_name}'",
                headers=headers,
                json=body,
            )
            if err:
                return err
            return f"Item '{name}' added to company list '{list_name}'."

    async def update_hibob_list_item(
        list_name: str, item_id: str, new_name: str
    ) -> str:
        async with _client() as client:
            _, err = await safe_request(
                client,
                "put",
                f"{_BASE_URL}/company/named-lists/{list_name}/{item_id}",
                _PROVIDER,
                f"update item {item_id} in list '{list_name}'",
                headers=headers,
                json={"name": new_name},
            )
            if err:
                return err
            return f"Item {item_id} in list '{list_name}' renamed to '{new_name}'."

    # ── Workforce Planning ────────────────────────────────────────────────

    async def search_hibob_positions(
        filters_json: str = "", fields_json: str = ""
    ) -> str:
        # Workforce-planning positions live under the object-search endpoint
        # POST /objects/position/search (NOT /positions/search). Both `fields`
        # and `filters` are required by the API. Supported filter fields:
        # /position/status, /position/name, /position/hasOpenRequests,
        # /position/id (operators: equals, notEqual).
        parsed_fields = _parse_json_param(fields_json)
        fields = (
            parsed_fields
            if isinstance(parsed_fields, list) and parsed_fields
            else [
                "/position/id",
                "/position/name",
                "/position/status",
                "/position/employmentType",
                "/position/effectiveDate",
            ]
        )
        parsed_filters = _parse_json_param(filters_json)
        if isinstance(parsed_filters, list) and parsed_filters:
            filters = parsed_filters
        else:
            # `filters` is mandatory — default to everything that is not
            # cancelled so the call succeeds and returns live positions.
            filters = [
                {
                    "fieldId": "/position/status",
                    "operator": "notEqual",
                    "values": ["cancelled"],
                }
            ]
        body: dict[str, Any] = {
            "fields": fields,
            "filters": filters,
            "includeHumanReadable": True,
        }
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/objects/position/search",
                _PROVIDER,
                "search positions",
                headers=headers,
                json=body,
            )
            if err:
                return err
            data, parse_err = _safe_parse_json(resp, "searching positions")
            if parse_err:
                return parse_err
            if isinstance(data, dict):
                positions = data.get("values", data.get("positions", data.get("items", [])))
            elif isinstance(data, list):
                positions = data
            else:
                positions = []
            if not isinstance(positions, list) or not positions:
                return "No positions found."
            lines = [f"Found {len(positions)} position(s):"]
            for p in positions:
                if not isinstance(p, dict):
                    continue
                # Response objects key fields by slash-path value-wrappers
                # (e.g. {"/position/status": {"value": "vacant", ...}}).
                fields_obj = p.get("fields", p)
                pid = _slash_value(fields_obj, "/position/id", default="N/A")
                name = _slash_value(fields_obj, "/position/name", default="Unnamed")
                status = _slash_value(fields_obj, "/position/status")
                emp_type = _slash_value(fields_obj, "/position/employmentType")
                lines.append(
                    f"  [{pid}] {name} | Status: {status} | Type: {emp_type}"
                )
            return "\n".join(lines)

    # ── Attendance ────────────────────────────────────────────────────────

    async def get_hibob_attendance_summary(
        from_date: str,
        to_date: str,
        employee_ids_json: str = "",
    ) -> str:
        # The summaries search requires `fields` plus `filters` carrying both a
        # /attendanceSummary/dateRange (equals [from, to]) and a
        # /attendanceSummary/employeeId (equals [...]) filter — a plain
        # {"from","to"} body returns HTTP 400.
        employee_ids = _parse_json_param(employee_ids_json)
        if not isinstance(employee_ids, list) or not employee_ids:
            return (
                "Error: employee_ids_json is required — provide a JSON array of "
                "employee IDs (e.g. '[\"123456789\"]'). The attendance summaries "
                "endpoint requires both a date range and an employeeId filter."
            )
        body: dict[str, Any] = {
            "fields": [
                "/attendanceSummary/employeeId",
                "/attendanceSummary/dateRange",
                "/attendanceSummary/hoursWorked",
                "/attendanceSummary/payableHours",
                "/attendanceSummary/regularHours",
                "/attendanceSummary/overtimeHours",
            ],
            "filters": [
                {
                    "fieldId": "/attendanceSummary/dateRange",
                    "operator": "equals",
                    "values": [from_date, to_date],
                },
                {
                    "fieldId": "/attendanceSummary/employeeId",
                    "operator": "equals",
                    "values": [str(e) for e in employee_ids],
                },
            ],
            "limit": 200,
        }
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/attendance/summaries/search",
                _PROVIDER,
                "fetch attendance summaries",
                headers=headers,
                json=body,
            )
            if err:
                return err
            data, parse_err = _safe_parse_json(resp, "fetching attendance summaries")
            if parse_err:
                return parse_err
            items = data.get("items", []) if isinstance(data, dict) else []
            if not items:
                return "No attendance summaries found for the specified period."
            lines = [f"Attendance summaries ({len(items)} entries):"]
            for item in items:
                fields_obj = item.get("fields", item) if isinstance(item, dict) else {}
                emp = _slash_value(fields_obj, "/attendanceSummary/employeeId")
                worked = _slash_value(fields_obj, "/attendanceSummary/hoursWorked")
                payable = _slash_value(fields_obj, "/attendanceSummary/payableHours")
                overtime = _slash_value(fields_obj, "/attendanceSummary/overtimeHours")
                lines.append(
                    f"  Employee {emp} | Worked: {worked}h "
                    f"| Payable: {payable}h | Overtime: {overtime}h"
                )
            return "\n".join(lines)

    async def get_hibob_attendance_daily(
        from_date: str,
        to_date: str,
        employee_ids_json: str = "",
    ) -> str:
        # The daily-breakdown search requires `fields` plus `filters` carrying a
        # /dailyBreakdown/date range (from / to operators) and a
        # /dailyBreakdown/employeeId equals filter — a plain {"from","to"} body
        # returns HTTP 400.
        employee_ids = _parse_json_param(employee_ids_json)
        if not isinstance(employee_ids, list) or not employee_ids:
            return (
                "Error: employee_ids_json is required — provide a JSON array of "
                "employee IDs (e.g. '[\"123456789\"]'). The daily attendance "
                "breakdown endpoint requires both a date range and an employeeId "
                "filter."
            )
        body: dict[str, Any] = {
            "fields": [
                "/dailyBreakdown/employeeId",
                "/dailyBreakdown/date",
                "/dailyBreakdown/hoursWorked",
                "/dailyBreakdown/payableHours",
                "/dailyBreakdown/overtimeHours",
                "/dailyBreakdown/isApproved",
            ],
            "filters": [
                {
                    "fieldId": "/dailyBreakdown/date",
                    "operator": "from",
                    "values": [from_date],
                },
                {
                    "fieldId": "/dailyBreakdown/date",
                    "operator": "to",
                    "values": [to_date],
                },
                {
                    "fieldId": "/dailyBreakdown/employeeId",
                    "operator": "equals",
                    "values": [str(e) for e in employee_ids],
                },
            ],
            "limit": 200,
        }
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/attendance/daily-breakdown/search",
                _PROVIDER,
                "fetch daily attendance breakdown",
                headers=headers,
                json=body,
            )
            if err:
                return err
            data, parse_err = _safe_parse_json(
                resp, "fetching daily attendance breakdown"
            )
            if parse_err:
                return parse_err
            items = data.get("items", []) if isinstance(data, dict) else []
            if not items:
                return "No daily attendance data found."
            lines = [f"Daily attendance breakdown ({len(items)} entries):"]
            for item in items[:50]:
                fields_obj = item.get("fields", item) if isinstance(item, dict) else {}
                emp = _slash_value(fields_obj, "/dailyBreakdown/employeeId")
                date = _slash_value(fields_obj, "/dailyBreakdown/date")
                worked = _slash_value(fields_obj, "/dailyBreakdown/hoursWorked")
                approved = _slash_value(fields_obj, "/dailyBreakdown/isApproved")
                lines.append(
                    f"  Employee {emp} | Date: {date} "
                    f"| Worked: {worked}h | Approved: {approved}"
                )
            if len(items) > 50:
                lines.append(f"  ... and {len(items) - 50} more entries")
            return "\n".join(lines)

    async def import_hibob_attendance(
        entries_json: str, import_method: str = "aggregate"
    ) -> str:
        entries = _parse_json_param(entries_json)
        if not isinstance(entries, list):
            return "Error: entries_json must be a JSON array of attendance entries."
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/attendance/import/{import_method}",
                _PROVIDER,
                "import attendance data",
                headers=headers,
                json={"requests": entries},
            )
            if err:
                return err
            return f"Attendance data imported successfully ({len(entries)} entries, method: {import_method})."

    async def search_hibob_projects(filters_json: str = "") -> str:
        body: dict[str, Any] = {}
        parsed = _parse_json_param(filters_json)
        if parsed:
            body["filters"] = parsed
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/attendance/projects/search",
                _PROVIDER,
                "search attendance projects",
                headers=headers,
                json=body,
            )
            if err:
                return err
            data = resp.json()
            projects = data.get("projects", data) if isinstance(data, dict) else data
            if isinstance(projects, list):
                if not projects:
                    return "No attendance projects found."
                lines = [f"Found {len(projects)} project(s):"]
                for p in projects:
                    if isinstance(p, dict):
                        lines.append(
                            f"  [{p.get('id', 'N/A')}] {p.get('name', 'Unnamed')} "
                            f"| Billable: {p.get('billable', 'N/A')} | Status: {p.get('status', 'active')}"
                        )
                return "\n".join(lines)
            return _json_dump(data)

    async def create_hibob_project(
        name: str,
        description: str = "",
        billable: bool = False,
        member_ids_json: str = "",
    ) -> str:
        body: dict[str, Any] = {"name": name, "billable": billable}
        if description:
            body["description"] = description
        members = _parse_json_param(member_ids_json)
        if members:
            body["members"] = members
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/attendance/projects",
                _PROVIDER,
                f"create project '{name}'",
                headers=headers,
                json=body,
            )
            if err:
                return err
            pid = (
                resp.json().get("id", "unknown")
                if resp.status_code < 300
                else "unknown"
            )
            return f"Project '{name}' created successfully. ID: {pid}"

    async def update_hibob_project(project_id: str, fields_json: str) -> str:
        fields = _parse_json_param(fields_json)
        if not isinstance(fields, dict):
            return "Error: fields_json must be a valid JSON object."
        async with _client() as client:
            _, err = await safe_request(
                client,
                "patch",
                f"{_BASE_URL}/attendance/projects/{project_id}",
                _PROVIDER,
                f"update project {project_id}",
                headers=headers,
                json=fields,
            )
            if err:
                return err
            return f"Project {project_id} updated successfully."

    async def search_hibob_project_tasks(filters_json: str = "") -> str:
        body: dict[str, Any] = {}
        parsed = _parse_json_param(filters_json)
        if parsed:
            body["filters"] = parsed
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/attendance/project-tasks/search",
                _PROVIDER,
                "search project tasks",
                headers=headers,
                json=body,
            )
            if err:
                return err
            data = resp.json()
            tasks = data.get("tasks", data) if isinstance(data, dict) else data
            if isinstance(tasks, list):
                if not tasks:
                    return "No project tasks found."
                lines = [f"Found {len(tasks)} project task(s):"]
                for t in tasks:
                    if isinstance(t, dict):
                        lines.append(
                            f"  [{t.get('id', 'N/A')}] {t.get('name', 'Unnamed')} "
                            f"| Billable: {t.get('billable', 'N/A')}"
                        )
                return "\n".join(lines)
            return _json_dump(data)

    # ── Custom Tables ─────────────────────────────────────────────────────

    async def list_hibob_custom_tables() -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/people/custom-tables/metadata",
                _PROVIDER,
                "list custom tables",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            tables = data if isinstance(data, list) else data.get("tables", [])
            if not tables:
                return "No custom tables found."
            lines = [f"Found {len(tables)} custom table(s):"]
            for t in tables:
                if isinstance(t, dict):
                    lines.append(f"  [{t.get('id', 'N/A')}] {t.get('name', 'Unnamed')}")
                else:
                    lines.append(f"  {t}")
            return "\n".join(lines)

    async def get_hibob_custom_table_entries(employee_id: str, table_id: str) -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/people/{employee_id}/custom-tables/{table_id}",
                _PROVIDER,
                f"get custom table {table_id} for {employee_id}",
                headers=headers,
            )
            if err:
                return err
            data, parse_err = _safe_parse_json(
                resp,
                f"fetching custom table {table_id} entries for employee {employee_id}",
            )
            if parse_err:
                return parse_err
            entries = data.get("values", data) if isinstance(data, dict) else data
            if isinstance(entries, list):
                if not entries:
                    return f"No entries in custom table {table_id} for employee {employee_id}."
                lines = [
                    f"Custom table '{table_id}' for employee {employee_id} ({len(entries)} entries):"
                ]
                for e in entries:
                    eid = e.get("id", "N/A")
                    summary = " | ".join(
                        f"{k}: {v}"
                        for k, v in e.items()
                        if k != "id"
                        and v is not None
                        and not isinstance(v, (dict, list))
                    )
                    lines.append(f"  [{eid}] {summary}")
                return "\n".join(lines)
            return _json_dump(data)

    async def create_hibob_custom_table_entry(
        employee_id: str, table_id: str, entry_json: str
    ) -> str:
        entry = _parse_json_param(entry_json)
        if not isinstance(entry, dict):
            return "Error: entry_json must be a valid JSON object."
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/people/{employee_id}/custom-tables/{table_id}",
                _PROVIDER,
                f"create custom table entry for {employee_id}",
                headers=headers,
                json=entry,
            )
            if err:
                return err
            return (
                f"Entry added to custom table '{table_id}' for employee {employee_id}."
            )

    async def update_hibob_custom_table_entry(
        employee_id: str, table_id: str, entry_id: str, entry_json: str
    ) -> str:
        entry = _parse_json_param(entry_json)
        if not isinstance(entry, dict):
            return "Error: entry_json must be a valid JSON object."
        async with _client() as client:
            _, err = await safe_request(
                client,
                "put",
                f"{_BASE_URL}/people/{employee_id}/custom-tables/{table_id}/{entry_id}",
                _PROVIDER,
                f"update custom table entry {entry_id}",
                headers=headers,
                json=entry,
            )
            if err:
                return err
            return f"Entry {entry_id} in custom table '{table_id}' updated for employee {employee_id}."

    async def delete_hibob_custom_table_entry(
        employee_id: str, table_id: str, entry_id: str
    ) -> str:
        async with _client() as client:
            _, err = await safe_request(
                client,
                "delete",
                f"{_BASE_URL}/people/{employee_id}/custom-tables/{table_id}/{entry_id}",
                _PROVIDER,
                f"delete custom table entry {entry_id}",
                headers=headers,
            )
            if err:
                return err
            return f"Entry {entry_id} deleted from custom table '{table_id}' for employee {employee_id}."

    # ── Employee Lifecycle ────────────────────────────────────────────────

    async def revoke_hibob_employee_access(employee_id: str) -> str:
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/employees/{employee_id}/uninvite",
                _PROVIDER,
                f"revoke access for employee {employee_id}",
                headers=headers,
            )
            if err:
                return err
            return f"Access to Bob revoked for employee {employee_id}."

    async def invite_hibob_employee(employee_id: str, wizard_id: str) -> str:
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/employees/{employee_id}/invitations",
                _PROVIDER,
                f"invite employee {employee_id}",
                headers=headers,
                json={"welcomeWizardId": wizard_id},
            )
            if err:
                return err
            return (
                f"Employee {employee_id} invited successfully with wizard {wizard_id}."
            )

    async def set_hibob_employee_start_date(
        employee_id: str, start_date: str, reason: str = ""
    ) -> str:
        body: dict[str, Any] = {"startDate": start_date}
        if reason:
            body["reason"] = reason
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/employees/{employee_id}/start-date",
                _PROVIDER,
                f"set start date for employee {employee_id}",
                headers=headers,
                json=body,
            )
            if err:
                return err
            return f"Start date for employee {employee_id} set to {start_date}."

    # ── Goals (OKR) ──────────────────────────────────────────────────────

    def _extract_goal_field(fields: dict, key: str, default: Any = None) -> Any:
        """Extract a value from a goal's fields dict: {"key": {"value": ...}}."""
        entry = fields.get(key)
        if isinstance(entry, dict):
            return entry.get("value", default)
        return default

    async def search_hibob_goal_types(
            fields_json: str = "",
            limit: int = 50,
            cursor: str = "",
    ) -> str:
        parsed_fields = _parse_json_param(fields_json) if fields_json else []
        if fields_json and not isinstance(parsed_fields, list):
            return "Error: fields_json must be a JSON array of field IDs."
        body: dict[str, Any] = {
            "fields": parsed_fields if parsed_fields else [
                "id", "name", "category", "order", "hierarchyLevel"
            ],
        }
        # NOTE: filters are NOT supported for goal-types — omit entirely
        if limit > 0:
            body["limit"] = limit
        if cursor:
            body["cursor"] = cursor

        async with _client() as client:
            resp, err = await safe_request(
                client, "post",
                f"{_BASE_URL}/goals/goal-types/search",
                _PROVIDER, "search goal types",
                headers=headers, json=body,
            )
            if err:
                return err
            data = resp.json()
            types = data.get("items", []) if isinstance(data, dict) else []
            if not types:
                return "No goal types found."
            lines = [f"Found {len(types)} goal type(s):"]
            for t in types:
                if not isinstance(t, dict):
                    continue
                f = t.get("fields", {})
                tid = _extract_goal_field(f, "id", "N/A")
                tname = _extract_goal_field(f, "name", "N/A")
                torder = _extract_goal_field(f, "order", "N/A")
                tcategory = _extract_goal_field(f, "category", "N/A")
                tlevel = _extract_goal_field(f, "hierarchyLevel", "")
                level_str = f" | HierarchyLevel: {tlevel}" if tlevel != "" else ""
                lines.append(
                    f"  [{tid}] {tname} | Category: {tcategory} | Order: {torder}{level_str}"
                )
            meta = data.get("responseMetadata", {})
            if meta.get("totalCount"):
                lines.append(f"Total: {meta['totalCount']}")
            return "\n".join(lines)

    async def search_hibob_goal_cycles(
            fields_json: str = "",
            limit: int = 50,
            cursor: str = "",
    ) -> str:
        parsed_fields = _parse_json_param(fields_json) if fields_json else []
        if fields_json and not isinstance(parsed_fields, list):
            return "Error: fields_json must be a JSON array of field IDs."
        body: dict[str, Any] = {
            "fields": parsed_fields if parsed_fields else [
                "id", "name", "startDate", "endDate"
            ],
            # NOTE: filters are NOT supported — passing them causes 400
        }
        if limit > 0:
            body["limit"] = limit
        if cursor:
            body["cursor"] = cursor

        async with _client() as client:
            resp, err = await safe_request(
                client, "post",
                f"{_BASE_URL}/goals/goal-cycles/search",
                _PROVIDER, "search goal cycles",
                headers=headers, json=body,
            )
            if err:
                return err
            data = resp.json()
            cycles = data.get("items", []) if isinstance(data, dict) else []
            if not cycles:
                return "No goal cycles found."
            lines = [f"Found {len(cycles)} goal cycle(s):"]
            for c in cycles:
                if not isinstance(c, dict):
                    continue
                f = c.get("fields", {})
                cid = _extract_goal_field(f, "id", "N/A")
                cname = _extract_goal_field(f, "name", "N/A")
                cstart = _extract_goal_field(f, "startDate", "N/A")
                cend = _extract_goal_field(f, "endDate", "N/A")
                lines.append(f"  [{cid}] {cname} | {cstart} → {cend}")
            meta = data.get("responseMetadata", {})
            if meta.get("totalCount"):
                lines.append(f"Total: {meta['totalCount']}")
            return "\n".join(lines)

    async def _key_result_lines(client: Any, goal_id: str) -> list[str]:
        """One goal's key results, indented, for ``include_key_results``."""
        resp, err = await safe_request(
            client,
            "post",
            f"{_BASE_URL}/goals/goals/key-results/search",
            _PROVIDER,
            f"search key results for goal {goal_id}",
            headers=headers,
            json={
                "fields": list(_KEY_RESULT_FIELDS),
                "filters": [{"fieldId": "goalId", "operator": "equals", "values": [str(goal_id)]}],
            },
        )
        if err or resp is None:
            return [f"      Key results not read: {err or 'no response'}"]
        data, parse_err = _safe_parse_json(resp, f"reading key results of goal {goal_id}")
        if parse_err:
            return [f"      Key results not read: {parse_err}"]
        krs = data.get("items", []) if isinstance(data, dict) else []
        if not krs:
            return ["      No key results."]
        return [f"      KR {_format_key_result(kr.get('fields', {}))}" for kr in krs if isinstance(kr, dict)]

    async def search_hibob_goals(
            status: str = "",
            type_id: str = "",
            is_private: str = "",
            fields_json: str = "",
            filters_json: str = "",
            limit: int = 50,
            cursor: str = "",
            count: int = 50,
        owner: str = "",
        cycle_id: str = "",
        include_key_results: bool = False,
    ) -> str:
        """
        Search goals. Supported server-side filter fields: id, status,
        isPrivate, typeId. ``owner`` and ``cycle_id`` are applied client-side
        over every page, since the Goals API cannot filter on them.
        """
        parsed_fields = _parse_json_param(fields_json) if fields_json else []
        if fields_json and not isinstance(parsed_fields, list):
            return "Error: fields_json must be a JSON array of field IDs."

        owner_id = ""
        if owner.strip():
            owner_id, err = await _resolve_employee_id(headers, owner)
            if err:
                return err

        fields = list(parsed_fields) if parsed_fields else list(_GOAL_FIELDS)
        # The client-side filters and the KR expansion read these fields.
        for needed in ("id", "owner", "cycleId"):
            if needed not in fields:
                fields.append(needed)
        body: dict[str, Any] = {"fields": fields}

        filters: list[dict[str, Any]] = []

        # Extra filters from caller
        extra = _parse_json_param(filters_json) if filters_json else []
        if filters_json and not isinstance(extra, list):
            return "Error: filters_json must be a JSON array."
        if isinstance(extra, list):
            filters.extend(f for f in extra if isinstance(f, dict))

        # Convenience params — only supported fieldIds
        if status:
            filters.append({"fieldId": "status", "operator": "equals", "values": [status]})
        if type_id:
            filters.append({"fieldId": "typeId", "operator": "equals", "values": [str(type_id)]})
        if is_private:
            filters.append({
                "fieldId": "isPrivate",
                "operator": "equals",
                "values": ["true" if is_private.strip().lower() == "true" else "false"],
            })

        if filters:
            body["filters"] = filters
        if limit > 0:
            body["limit"] = limit
        if cursor:
            body["cursor"] = cursor
        # A client-side filter must see every goal, not the first page.
        walk_pages = bool(owner_id or cycle_id.strip())

        async with _client() as client:
            goals: list[Any] = []
            server_total: Any = None
            next_cursor: str | None = None
            for _page in range(_GOAL_SEARCH_MAX_PAGES):
                resp, search_err = await safe_request(
                    client,
                    "post",
                    f"{_BASE_URL}/goals/goals/search",
                    _PROVIDER,
                    "search goals",
                    headers=headers,
                    json=body,
                )
                if search_err or resp is None:
                    return search_err or "Error: HiBob API returned no response while searching goals."
                data, parse_err = _safe_parse_json(resp, "searching goals")
                if parse_err:
                    return parse_err
                page = data.get("items", []) if isinstance(data, dict) else []
                goals.extend(g for g in page if isinstance(g, dict))
                meta = data.get("responseMetadata", {}) if isinstance(data, dict) else {}
                meta = meta if isinstance(meta, dict) else {}
                server_total = meta.get("totalCount", server_total)
                next_cursor = meta.get("nextCursor") or None
                if not walk_pages or not next_cursor or not page:
                    break
                body["cursor"] = next_cursor
            if owner_id:
                goals = [g for g in goals if str(_extract_goal_field(g.get("fields", {}), "owner", "")) == owner_id]
            if cycle_id.strip():
                goals = [
                    g for g in goals if str(_extract_goal_field(g.get("fields", {}), "cycleId", "")) == cycle_id.strip()
                ]
            if not goals:
                scope = f" for owner {owner_id}" if owner_id else ""
                return f"No goals found matching the criteria{scope}."
            total = len(goals)
            limited = goals[:count]
            lines = [f"Found {total} goal(s){f' (showing {count})' if total > count else ''}:"]
            for g in limited:
                f = g.get("fields", {})
                lines.append(f"  {_format_goal_line(f)}")
                if include_key_results:
                    gid = _extract_goal_field(f, "id")
                    if gid is not None:
                        lines.extend(await _key_result_lines(client, str(gid)))
            if server_total and not (owner_id or cycle_id.strip()):
                lines.append(f"Total (server): {server_total}")
            if walk_pages and next_cursor:
                lines.append(
                    f"Stopped after {_GOAL_SEARCH_MAX_PAGES} pages: goals past them were not filtered."
                )
            return "\n".join(lines)

    async def get_hibob_goal(goal_id: str, fields_json: str = "") -> str:
        """Retrieve a single goal by id (detailed view).

        HiBob has no direct GET-by-id for goals, so this queries the goals
        search endpoint with an ``id`` equality filter. ``fields_json``
        (JSON array of field ids) overrides the default field set.
        """
        parsed_fields = _parse_json_param(fields_json) if fields_json else []
        if fields_json and not isinstance(parsed_fields, list):
            return "Error: fields_json must be a JSON array of field IDs."

        fields = list(parsed_fields) if parsed_fields else [
            "id", "title", "status", "owner", "dueDate",
            "isPrivate", "typeId", "cycleId", "description",
            "startDate", "alignedGoal",
        ]
        # ``id`` must always be requested: the result is matched client-side by
        # id below, so a fields_json override that omits ``id`` would otherwise
        # make every goal fail to match and report a spurious "not found".
        if "id" not in fields:
            fields.append("id")

        body: dict[str, Any] = {
            "fields": fields,
            "filters": [
                {"fieldId": "id", "operator": "equals", "values": [str(goal_id)]}
            ],
            # Safety net: if a tenant ignores the id filter we still match
            # client-side within the returned page rather than trusting the
            # first arbitrary row.
            "limit": 50,
        }
        async with _client() as client:
            resp, err = await safe_request(
                client, "post",
                f"{_BASE_URL}/goals/goals/search",
                _PROVIDER, f"get goal {goal_id}",
                headers=headers, json=body,
            )
            if err:
                return err
            # Use the canonical safe parser so a degenerate 2xx body (empty or
            # non-JSON, e.g. during a HiBob partial outage) degrades to a clear
            # tool error instead of raising.
            data, parse_err = _safe_parse_json(resp, f"getting goal {goal_id}")
            if parse_err:
                return parse_err
            goals = data.get("items", []) if isinstance(data, dict) else []
            target = next(
                (
                    g
                    for g in goals
                    if isinstance(g, dict)
                    and str(_extract_goal_field(g.get("fields", {}), "id", ""))
                    == str(goal_id)
                ),
                None,
            )
            if target is None:
                return f"Goal {goal_id} not found."

            f = target.get("fields", {})
            title = _extract_goal_field(f, "title", "Untitled")
            status = _extract_goal_field(f, "status", "N/A")
            owner = _extract_goal_field(f, "owner", "N/A")
            due = _extract_goal_field(f, "dueDate", "N/A")
            start = _extract_goal_field(f, "startDate")
            is_private = _extract_goal_field(f, "isPrivate")
            type_id = _extract_goal_field(f, "typeId")
            cycle_id = _extract_goal_field(f, "cycleId")
            aligned = _extract_goal_field(f, "alignedGoal")
            description = _extract_goal_field(f, "description")

            lines = [
                f"Goal [{goal_id}]: {title}",
                f"Status: {status}",
                f"Owner: {owner}",
                f"Start: {start if start is not None else 'N/A'} | Due: {due}",
            ]
            if type_id is not None:
                lines.append(f"TypeID: {type_id}")
            if cycle_id is not None:
                lines.append(f"CycleID: {cycle_id}")
            if is_private is not None:
                lines.append(f"Private: {is_private}")
            if aligned is not None:
                lines.append(f"Aligned goal: {aligned}")
            if description:
                lines.append(f"Description: {description}")
            return "\n".join(lines)

    async def search_hibob_key_results(
            goal_id: str,
            fields_json: str = "",
            limit: int = 50,
            cursor: str = "",
    ) -> str:
        """
        Search key results for a specific goal.
        goalId filter is mandatory — the API returns 400 without it.
        """
        parsed_fields = _parse_json_param(fields_json) if fields_json else []
        if fields_json and not isinstance(parsed_fields, list):
            return "Error: fields_json must be a JSON array of field IDs."

        body: dict[str, Any] = {
            "fields": parsed_fields if parsed_fields else list(_KEY_RESULT_FIELDS),
            "filters": [
                {"fieldId": "goalId", "operator": "equals", "values": [str(goal_id)]}
            ],
        }
        if limit > 0:
            body["limit"] = limit
        if cursor:
            body["cursor"] = cursor

        async with _client() as client:
            resp, err = await safe_request(
                client, "post",
                f"{_BASE_URL}/goals/goals/key-results/search",
                _PROVIDER, f"search key results for goal {goal_id}",
                headers=headers, json=body,
            )
            if err:
                return err
            data = resp.json()
            krs = data.get("items", []) if isinstance(data, dict) else []
            if not krs:
                return f"No key results found for goal {goal_id}."
            lines = [f"Key results for goal {goal_id} ({len(krs)} KR(s)):"]
            for kr in krs:
                if not isinstance(kr, dict):
                    continue
                lines.append(f"  {_format_key_result(kr.get('fields', {}))}")
            meta = data.get("responseMetadata", {})
            if meta.get("totalCount"):
                lines.append(f"Total: {meta['totalCount']}")
            return "\n".join(lines)

    async def create_hibob_goal(
            title: str,
            type_id: str,
            owner: str,
            start_date: str,
            due_date: str,
            description: str = "",
            is_private: bool = False,
            aligned_goal: str = "",
            type_list_item: str = "",
    ) -> str:
        try:
            type_id_int = int(type_id)
        except (ValueError, TypeError):
            return f"Error: type_id must be a numeric value, got '{type_id}'"
        fields: dict[str, Any] = {
            "title":     {"value": title},
            "typeId":    {"value": type_id_int},
            "owner":     {"value": owner},
            "startDate": {"value": start_date},
            "dueDate":   {"value": due_date},
            "isPrivate": {"value": is_private},
        }
        if description:
            fields["description"] = {"value": description}
        if aligned_goal:
            try:
                aligned_goal_int = int(aligned_goal)
            except (ValueError, TypeError):
                return f"Error: aligned_goal must be a numeric value, got '{aligned_goal}'"
            fields["alignedGoal"] = {"value": aligned_goal_int}
        if type_list_item:
            fields["typeListItem"] = {"value": type_list_item}

        body: dict[str, Any] = {
            "items": [{"objectType": "goal", "fields": fields}]
        }
        async with _client() as client:
            resp, err = await safe_request(
                client, "post",
                f"{_BASE_URL}/goals/goals",
                _PROVIDER, f"create goal '{title}'",
                headers=headers, json=body,
            )
            if err:
                return err
            gid = "unknown"
            if resp.status_code < 300:
                payload = resp.json()
                ids = payload.get("ids") or payload.get("goalIds")
                if isinstance(ids, list) and ids:
                    gid = ids[0]
            return f"Goal '{title}' created successfully. ID: {gid}"

    async def update_hibob_goal(goal_id: str, fields_json: str) -> str:
        """
        fields_json: flat dict of fields to update, e.g.:
          {"title": "New title", "dueDate": "2026-06-30", "cycleId": null}
        Pass null explicitly to unassign cycleId.
        Updatable fields: title, description, dueDate, startDate, status,
          isPrivate, cycleId, typeId, alignedGoal, typeListItem.
        ``owner`` is refused upfront: HiBob answers 400 "Goal owner ...
        cannot be changed on update" and its Goals API has no other route
        that reassigns a goal (#10858).
        """
        updatable_goal_fields = {
            "title", "description", "dueDate", "startDate", "status",
            "isPrivate", "cycleId", "typeId", "alignedGoal", "typeListItem",
        }
        raw = _parse_json_param(fields_json)
        if not isinstance(raw, dict):
            return "Error: fields_json must be a valid JSON object."
        if "owner" in raw:
            return (
                f"Error: the owner of goal {goal_id} cannot be changed through "
                "the HiBob API: HiBob refuses an owner change on update and "
                "has no endpoint that reassigns a goal. Reassign it in the "
                "HiBob UI, or create a new goal with the new owner "
                "(create_hibob_goal) and delete this one, which loses its "
                "check-ins and comments. Nothing was sent."
            )

        unknown = set(raw.keys()) - updatable_goal_fields
        if unknown:
            return (
                f"Error: unknown field(s): {sorted(unknown)}. "
                f"Updatable fields are: {sorted(updatable_goal_fields)}"
            )

        wrapped: dict[str, Any] = {}
        for k, v in raw.items():
            if v is None:
                wrapped[k] = {"value": None}
            elif k in ("typeId", "alignedGoal") and isinstance(v, (str, int)):
                try:
                    wrapped[k] = {"value": int(v)}
                except (ValueError, TypeError):
                    return f"Error: '{k}' must be a numeric value, got '{v}'"
            else:
                wrapped[k] = {"value": v}

        body: dict[str, Any] = {
            "items": [{"objectType": "goal", "fields": wrapped}]
        }
        async with _client() as client:
            # ``expected_statuses={400}`` opts into receiving the failed
            # response (so we can inspect it for the closed-goal signal) and
            # suppresses ``safe_request``'s per-attempt warning for 400s.
            # Agents commonly hit a batch of closed goals in one run (x8 in a
            # row has been seen). We re-emit the warning below for any *non*-
            # closed 400 so genuine validation failures stay visible — only
            # the benign closed-goal case is silenced.
            resp, err = await safe_request(
                client, "patch",
                f"{_BASE_URL}/goals/goals/{goal_id}",
                _PROVIDER, f"update goal {goal_id}",
                headers=headers, json=body,
                expected_statuses={400},
            )
            if err:
                # A closed goal can't be updated — surface a clear, actionable
                # message so the agent skips it (or reopens it first) instead
                # of blindly retrying the same 400.
                if _is_closed_goal_update_error(resp):
                    return (
                        f"Goal {goal_id} is closed and cannot be updated. "
                        "Skip it, or reopen it first with "
                        "update_hibob_goal_status, then retry."
                    )
                # Restore the warning that ``expected_statuses={400}`` suppressed
                # for non-closed 400s — these are real validation errors and
                # should remain visible in the warning stream (other statuses
                # were already logged by ``safe_request``).
                if resp is not None and resp.status_code == 400:
                    logger.warning("HiBob update goal %s failed: %s", goal_id, err)
                return err
            return f"Goal {goal_id} updated successfully."

    async def delete_hibob_goal(goal_id: str) -> str:
        async with _client() as client:
            _, err = await safe_request(
                client, "delete",
                f"{_BASE_URL}/goals/goals/{goal_id}",
                _PROVIDER, f"delete goal {goal_id}",
                headers=headers,
            )
            if err:
                return err
            return f"Goal {goal_id} deleted successfully."

    async def update_hibob_goal_status(
            goal_id: str, status: str, comment: str = ""
    ) -> str:
        body: dict[str, Any] = {"status": status}
        if comment:
            body["comment"] = comment
        async with _client() as client:
            _, err = await safe_request(
                client, "patch",
                f"{_BASE_URL}/goals/goals/{goal_id}/status",
                _PROVIDER, f"update goal {goal_id} status to {status}",
                headers=headers, json=body,
            )
            if err:
                return err
            return f"Goal {goal_id} status updated to '{status}'."

    async def create_hibob_key_results(goal_id: str, key_results_json: str) -> str:
        """
        key_results_json: JSON array of KR objects, e.g.:
          [
            {"title": "Ship v1", "measureType": "boolean"},
            {"title": "Adoption rate", "measureType": "percentage", "target": 100},
            {"title": "Alternative key", "measure": "numeric", "target": 2},
            {"title": "Revenue", "measureType": "currency", "target": 50000,
             "currency": "USD", "currencySymbol": "$"}
          ]
        NOTE: both 'measureType' and 'measure' are accepted.
        For non-boolean measures (numeric, percentage, currency), 'target' is required.
        """
        krs = _parse_json_param(key_results_json)
        if not isinstance(krs, list):
            return "Error: key_results_json must be a JSON array."

        items: list[dict[str, Any]] = []
        for i, kr in enumerate(krs):
            measure_type = kr.get("measureType", kr.get("measure", "boolean"))
            target = kr.get("target")
            if measure_type == "boolean":
                if target is None:
                    target = True
            else:
                if target is None:
                    return (
                        f"Error: KR #{i + 1} ('{kr.get('title', '')}') "
                        f"requires an explicit 'target' for measure type '{measure_type}'."
                    )
            fields: dict[str, Any] = {
                "title":       {"value": kr["title"]},
                "measureType": {"value": measure_type},
                "target":      {"value": target},
            }
            if kr.get("description"):
                fields["description"] = {"value": kr["description"]}
            if kr.get("currentValue") is not None:
                fields["currentValue"] = {"value": kr["currentValue"]}
            if kr.get("currency"):
                fields["currency"] = {"value": kr["currency"]}
            if kr.get("currencySymbol"):
                fields["currencySymbol"] = {"value": kr["currencySymbol"]}
            items.append({"objectType": "keyResult", "fields": fields})

        async with _client() as client:
            resp, err = await safe_request(
                client, "post",
                f"{_BASE_URL}/goals/goals/{goal_id}/key-results",
                _PROVIDER, f"create key results for goal {goal_id}",
                headers=headers, json={"items": items},
            )
            if err:
                return err
            kr_ids = []
            if resp.status_code < 300:
                kr_ids = resp.json().get("keyResultIds", [])
            return (
                f"{len(items)} key result(s) created for goal {goal_id}. "
                f"IDs: {kr_ids}"
            )

    def _is_fractional(value: Any) -> bool:
        """True for a JSON number with a fractional part (0.5, 99.995)."""
        return (
            isinstance(value, float)
            and not isinstance(value, bool)
            and not value.is_integer()
        )

    def _same_stored_value(sent: Any, got: Any) -> bool:
        """True when the value HiBob reads back is the value written."""
        if got is None:
            return False
        numeric = (int, float)
        if (
            isinstance(sent, numeric) and not isinstance(sent, bool)
            and not isinstance(got, bool)
        ):
            try:
                return abs(float(got) - sent) < 1e-9
            except (TypeError, ValueError):
                return False
        if isinstance(sent, str) and isinstance(got, str):
            return sent.strip().lower() == got.strip().lower()
        return sent == got

    async def _kr_value_mismatches(
            client: Any, goal_id: str, sent: dict[str, dict[str, Any]],
    ) -> list[str]:
        """Read the goal's key results back and list every value not stored.

        HiBob answers 2xx yet truncates some decimal ``target`` /
        ``currentValue`` writes to integers, and leaves other detail
        fields (``measureType``) unchanged. ``sent`` maps a key result id
        to the values written; each value HiBob stored differently is
        returned as a readable line. An unreadable read-back returns one
        line saying the values could not be verified.
        """
        read_fields = ["id", "measureType", "target", "currentValue"]
        for values in sent.values():
            for field in values:
                if field not in read_fields:
                    read_fields.append(field)
        body = {
            "fields": read_fields,
            "filters": [
                {"fieldId": "goalId", "operator": "equals", "values": [str(goal_id)]}
            ],
            "limit": 100,
        }
        resp, err = await safe_request(
            client, "post",
            f"{_BASE_URL}/goals/goals/key-results/search",
            _PROVIDER, f"read back key results for goal {goal_id}",
            headers=headers, json=body,
        )
        if err or resp is None:
            return [f"values could not be verified: {err or 'no response'}"]
        data = resp.json()
        stored: dict[str, dict] = {}
        for kr in data.get("items", []) if isinstance(data, dict) else []:
            if isinstance(kr, dict):
                f = kr.get("fields", {})
                stored[str(_extract_goal_field(f, "id"))] = f
        problems: list[str] = []
        for kr_id, values in sent.items():
            f = stored.get(kr_id)
            if f is None:
                problems.append(f"key result {kr_id}: not found on read-back")
                continue
            measure = _extract_goal_field(f, "measureType", "unknown")
            for field, value in values.items():
                got = _extract_goal_field(f, field)
                if not _same_stored_value(value, got):
                    problems.append(
                        f"key result {kr_id} ({measure}): {field} sent {value}, "
                        f"HiBob stored {got}"
                    )
        return problems

    def _decimal_error(goal_id: str, problems: list[str]) -> str:
        return (
            f"Error: HiBob accepted the update of goal {goal_id} but did not "
            "store every decimal value: " + "; ".join(problems) + ". "
            "HiBob truncates decimals for this field and measure; write a "
            "value it can store (e.g. scale the target) or keep the integer."
        )

    def _kr_update_refusals(resp: Any) -> list[str]:
        """Key results the PATCH answer marks failed.

        HiBob answers 200 with ``{success, goalId, results: [{keyResultId,
        success, error}]}`` and tracks each key result on its own, so a
        refused key result never surfaces as an HTTP error.
        """
        try:
            data = resp.json() if resp is not None else None
        except ValueError:
            return []
        results = data.get("results") if isinstance(data, dict) else None
        refusals: list[str] = []
        for entry in results if isinstance(results, list) else []:
            if isinstance(entry, dict) and entry.get("success") is False:
                reason = entry.get("error") or "no reason given"
                refusals.append(
                    f"key result {entry.get('keyResultId')}: HiBob refused the update ({reason})"
                )
        return refusals

    async def update_hibob_key_results(goal_id: str, key_results_json: str) -> str:
        """
        key_results_json: JSON array with keyResultId + fields to update, e.g.:
          [{"keyResultId": 200, "title": "New title", "target": 5}]
        Updatable fields: title, measureType, target, currency, currencySymbol.
        NOTE: 'measure' is accepted as an alias of 'measureType'.
        Every field sent is read back; a field HiBob did not store is an error.
        """
        krs = _parse_json_param(key_results_json)
        if not isinstance(krs, list):
            return "Error: key_results_json must be a JSON array."

        items: list[dict[str, Any]] = []
        requested: dict[str, dict[str, Any]] = {}
        for kr in krs:
            kr_id = kr.get("keyResultId") or kr.get("id")
            if not kr_id:
                return "Error: each KR update must include 'keyResultId'."
            fields: dict[str, Any] = {"keyResultId": {"value": int(kr_id)}}
            if "measure" in kr and "measureType" not in kr:
                kr["measureType"] = kr["measure"]
            for field in ("title", "measureType", "target", "currency", "currencySymbol"):
                if field in kr:
                    fields[field] = {"value": kr[field]}
                    requested.setdefault(str(int(kr_id)), {})[field] = kr[field]
            items.append({"objectType": "keyResult", "fields": fields})

        async with _client() as client:
            resp, err = await safe_request(
                client, "patch",
                f"{_BASE_URL}/goals/goals/{goal_id}/key-results",
                _PROVIDER, f"update key results for goal {goal_id}",
                headers=headers, json={"items": items},
            )
            if err:
                return err
            problems = _kr_update_refusals(resp)
            if requested:
                problems += await _kr_value_mismatches(client, goal_id, requested)
            if problems:
                hint = ""
                if any("measureType" in p for p in problems):
                    hint = (
                        " To change a key result's measure, create a key result "
                        "with the new measure (create_hibob_key_results), check "
                        "its progress in, then delete_hibob_key_result the old one."
                    )
                return (
                    f"Error: HiBob accepted the update of goal {goal_id} but did "
                    "not store every requested field: " + "; ".join(problems)
                    + "." + hint
                )
            return f"Key results updated for goal {goal_id}."

    async def delete_hibob_key_result(goal_id: str, key_result_id: str) -> str:
        async with _client() as client:
            _, err = await safe_request(
                client, "delete",
                f"{_BASE_URL}/goals/goals/{goal_id}/key-results/{key_result_id}",
                _PROVIDER, f"delete key result {key_result_id} of goal {goal_id}",
                headers=headers,
            )
            if err:
                return err
            return f"Key result {key_result_id} deleted from goal {goal_id}."

    async def update_hibob_key_results_progress(
            goal_id: str, progress_json: str
    ) -> str:
        """
        progress_json: plain array or {"keyResults": [...], "comment": "..."}
          [{"keyResultId": 200, "currentValue": 75, "notes": "On track"}]
        No value-wrapping — currentValue is passed directly.
        """
        parsed = _parse_json_param(progress_json)
        if isinstance(parsed, list):
            key_results = parsed
            comment: str | None = None
        elif isinstance(parsed, dict):
            key_results = parsed.get("keyResults", [])
            comment = parsed.get("comment")
        else:
            return "Error: progress_json must be a JSON array or object."

        if not isinstance(key_results, list):
            return "Error: 'keyResults' must be a JSON array."

        items: list[dict[str, Any]] = []
        decimals: dict[str, dict[str, float]] = {}
        for entry in key_results:
            kr_id = entry.get("keyResultId") or entry.get("id")
            if not kr_id:
                return "Error: each progress entry must include 'keyResultId'."
            item: dict[str, Any] = {
                "keyResultId": int(kr_id),
                "currentValue": entry["currentValue"],
            }
            if entry.get("notes"):
                item["notes"] = entry["notes"]
            if _is_fractional(entry["currentValue"]):
                decimals.setdefault(str(int(kr_id)), {})["currentValue"] = entry["currentValue"]
            items.append(item)

        body: dict[str, Any] = {"keyResults": items}
        if comment:
            body["comment"] = comment

        async with _client() as client:
            _, err = await safe_request(
                client, "patch",
                f"{_BASE_URL}/goals/goals/{goal_id}/key-results/progress",
                _PROVIDER, f"update key results progress for goal {goal_id}",
                headers=headers, json=body,
            )
            if err:
                return err
            if decimals:
                problems = await _kr_value_mismatches(client, goal_id, decimals)
                if problems:
                    return _decimal_error(goal_id, problems)
            return f"Key results progress updated for goal {goal_id} ({len(items)} KR(s))."

    # ── Job Catalog ──────────────────────────────────────────────────────

    async def search_hibob_job_profiles(
        status: str = "",
        role_id: str = "",
        family_id: str = "",
    ) -> str:
        body: dict[str, Any] = {
            "includeHumanReadable": True,
            "fields": ["jobProfile.id", "jobProfile.title", "jobProfile.status"],
        }
        filters: dict[str, Any] = {}
        if status:
            filters["status"] = status
        if role_id:
            filters["jobRoleId"] = role_id
        if family_id:
            filters["jobFamilyId"] = family_id
        if filters:
            body["filters"] = filters
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/job-catalog/job-profiles/search",
                _PROVIDER,
                "search job profiles",
                headers=headers,
                json=body,
            )
            if err:
                return err
            data = resp.json()
            profiles = (
                data.get("profiles", data.get("results", data))
                if isinstance(data, dict)
                else data
            )
            if isinstance(profiles, list):
                if not profiles:
                    return "No job profiles found."
                lines = [f"Found {len(profiles)} job profile(s):"]
                for p in profiles:
                    if isinstance(p, dict):
                        title_obj = p.get("/jobProfile/title", {})
                        title_val = (
                            title_obj.get(
                                "humanReadable", title_obj.get("value", "N/A")
                            )
                            if isinstance(title_obj, dict)
                            else str(title_obj) if title_obj else "N/A"
                        )
                        status_obj = p.get("/jobProfile/status", {})
                        status_val = (
                            status_obj.get(
                                "humanReadable", status_obj.get("value", "N/A")
                            )
                            if isinstance(status_obj, dict)
                            else "N/A"
                        )
                        pid_obj = p.get("/jobProfile/id", {})
                        pid_val = (
                            pid_obj.get("value", "N/A")
                            if isinstance(pid_obj, dict)
                            else "N/A"
                        )
                        lines.append(
                            f"  [{pid_val}] {title_val} | Status: {status_val}"
                        )
                return "\n".join(lines)
            return _json_dump(data)

    async def list_hibob_job_roles() -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/job-catalog/job-roles",
                _PROVIDER,
                "list job roles",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            roles = (
                data
                if isinstance(data, list)
                else data.get("roles", data.get("results", []))
            )
            if isinstance(roles, list):
                if not roles:
                    return "No job roles found."
                lines = [f"Found {len(roles)} job role(s):"]
                for r in roles:
                    if isinstance(r, dict):
                        lines.append(f"  [{r.get('id', 'N/A')}] {r.get('name', 'N/A')}")
                return "\n".join(lines)
            return _json_dump(data)

    async def list_hibob_job_families() -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/job-catalog/job-families",
                _PROVIDER,
                "list job families",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            families = (
                data
                if isinstance(data, list)
                else data.get("families", data.get("results", []))
            )
            if isinstance(families, list):
                if not families:
                    return "No job families found."
                lines = [f"Found {len(families)} job family(ies):"]
                for f in families:
                    if isinstance(f, dict):
                        lines.append(f"  [{f.get('id', 'N/A')}] {f.get('name', 'N/A')}")
                return "\n".join(lines)
            return _json_dump(data)

    async def list_hibob_job_family_groups() -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/job-catalog/job-family-groups",
                _PROVIDER,
                "list job family groups",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            groups = (
                data
                if isinstance(data, list)
                else data.get("familyGroups", data.get("results", []))
            )
            if isinstance(groups, list):
                if not groups:
                    return "No job family groups found."
                lines = [f"Found {len(groups)} job family group(s):"]
                for g in groups:
                    if isinstance(g, dict):
                        lines.append(f"  [{g.get('id', 'N/A')}] {g.get('name', 'N/A')}")
                return "\n".join(lines)
            return _json_dump(data)

    async def get_hibob_job_catalog_metadata() -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/job-catalog/job-profiles/metadata",
                _PROVIDER,
                "get job catalog metadata",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            fields = data if isinstance(data, list) else data.get("fields", [])
            if isinstance(fields, list) and fields:
                lines = [f"Job profile fields ({len(fields)}):"]
                for f in fields:
                    if isinstance(f, dict):
                        lines.append(
                            f"  {f.get('id', 'N/A')} | {f.get('name', 'N/A')} | Type: {f.get('type', 'N/A')}"
                        )
                return "\n".join(lines)
            return _json_dump(data)

    # ── Performance snapshot ─────────────────────────────────────────────

    async def _employee_time_off(employee_id: str, from_date: str, to_date: str) -> str:
        """Time off of one employee over a range, totalled by policy."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/timeoff/whosout",
                _PROVIDER,
                "get who's out",
                headers=headers,
                params={"from": from_date, "to": to_date},
            )
        if err or resp is None:
            return err or "Error: HiBob API returned no response while reading time off."
        data, parse_err = _safe_parse_json(resp, "reading time off")
        if parse_err:
            return parse_err
        outs = data.get("outs", data) if isinstance(data, dict) else data
        mine = [
            o
            for o in (outs if isinstance(outs, list) else [])
            if isinstance(o, dict) and str(o.get("employeeId", "")) == employee_id
        ]
        return "\n".join(_timeoff_by_policy_lines(mine, from_date, to_date))

    async def get_hibob_performance_snapshot(
        employee: str,
        from_date: str = "",
        to_date: str = "",
        include_goals: bool = True,
        include_documents: bool = True,
    ) -> str:
        employee_id, err = await _resolve_employee_id(headers, employee)
        if err:
            return err
        from_date, to_date = _snapshot_range(from_date, to_date)
        range_error = _whos_out_range_error(from_date, to_date)
        if range_error:
            return range_error

        sections: list[tuple[str, Any]] = [
            ("Profile", get_hibob_employee(employee_id)),
            ("Work history (titles, departments, managers)", get_hibob_employee_table(employee_id, "work")),
            ("Lifecycle history", get_hibob_employee_table(employee_id, "lifecycle")),
            (f"Time off {from_date} to {to_date}", _employee_time_off(employee_id, from_date, to_date)),
        ]
        if include_goals:
            sections.append(
                ("Goals and key results", search_hibob_goals(owner=employee_id, include_key_results=True, count=100))
            )
        if include_documents:
            sections.append(("Documents", list_hibob_employee_documents(employee_id)))

        lines = [f"Performance snapshot of employee {employee_id}"]
        for title, pending in sections:
            try:
                body = await pending
            except Exception as exc:  # one section failing must not hide the others
                body = f"Error: {format_exception_message(exc)}"
            lines.append(f"\n## {title}\n{body}")
        lines.append(f"\n## Performance reviews\n{_REVIEWS_NOT_IN_PUBLIC_API}")
        return "\n".join(lines)

    # ── Register all handlers ────────────────────────────────────────────

    _tools: dict[str, Any] = {
        "search_hibob_employees": search_hibob_employees,
        "get_hibob_employee": get_hibob_employee,
        "list_hibob_profiles": list_hibob_profiles,
        "create_hibob_employee": create_hibob_employee,
        "update_hibob_employee": update_hibob_employee,
        "terminate_hibob_employee": terminate_hibob_employee,
        "get_hibob_employee_table": get_hibob_employee_table,
        "add_hibob_employee_table_entry": add_hibob_employee_table_entry,
        "get_hibob_whos_out": get_hibob_whos_out,
        "get_hibob_out_today": get_hibob_out_today,
        "get_hibob_timeoff_balance": get_hibob_timeoff_balance,
        "submit_hibob_timeoff_request": submit_hibob_timeoff_request,
        "get_hibob_timeoff_request": get_hibob_timeoff_request,
        "cancel_hibob_timeoff_request": cancel_hibob_timeoff_request,
        "list_hibob_timeoff_policy_types": list_hibob_timeoff_policy_types,
        "get_hibob_timeoff_policy_details": get_hibob_timeoff_policy_details,
        "list_hibob_timeoff_policies": list_hibob_timeoff_policies,
        "list_hibob_timeoff_reason_codes": list_hibob_timeoff_reason_codes,
        "add_hibob_timeoff_reason_code": add_hibob_timeoff_reason_code,
        "get_hibob_timeoff_changes": get_hibob_timeoff_changes,
        "create_hibob_timeoff_balance_adjustment": create_hibob_timeoff_balance_adjustment,
        "list_hibob_tasks": list_hibob_tasks,
        "get_hibob_employee_tasks": get_hibob_employee_tasks,
        "complete_hibob_task": complete_hibob_task,
        "list_hibob_reports": list_hibob_reports,
        "download_hibob_report": download_hibob_report,
        "list_hibob_employee_documents": list_hibob_employee_documents,
        "download_hibob_document": download_hibob_document,
        "list_hibob_doc_folders": list_hibob_doc_folders,
        "upload_hibob_document": upload_hibob_document,
        "upload_hibob_document_url": upload_hibob_document_url,
        "delete_hibob_document": delete_hibob_document,
        "get_hibob_employee_fields_metadata": get_hibob_employee_fields_metadata,
        "create_hibob_field": create_hibob_field,
        "list_hibob_company_lists": list_hibob_company_lists,
        "get_hibob_company_list": get_hibob_company_list,
        "add_hibob_list_item": add_hibob_list_item,
        "update_hibob_list_item": update_hibob_list_item,
        "search_hibob_positions": search_hibob_positions,
        "get_hibob_attendance_summary": get_hibob_attendance_summary,
        "get_hibob_attendance_daily": get_hibob_attendance_daily,
        "import_hibob_attendance": import_hibob_attendance,
        "search_hibob_projects": search_hibob_projects,
        "create_hibob_project": create_hibob_project,
        "update_hibob_project": update_hibob_project,
        "search_hibob_project_tasks": search_hibob_project_tasks,
        "list_hibob_custom_tables": list_hibob_custom_tables,
        "get_hibob_custom_table_entries": get_hibob_custom_table_entries,
        "create_hibob_custom_table_entry": create_hibob_custom_table_entry,
        "update_hibob_custom_table_entry": update_hibob_custom_table_entry,
        "delete_hibob_custom_table_entry": delete_hibob_custom_table_entry,
        "revoke_hibob_employee_access": revoke_hibob_employee_access,
        "invite_hibob_employee": invite_hibob_employee,
        "set_hibob_employee_start_date": set_hibob_employee_start_date,
        "search_hibob_goal_types": search_hibob_goal_types,
        "search_hibob_goals": search_hibob_goals,
        "get_hibob_goal": get_hibob_goal,
        "search_hibob_key_results": search_hibob_key_results,
        "create_hibob_goal": create_hibob_goal,
        "update_hibob_goal": update_hibob_goal,
        "delete_hibob_goal": delete_hibob_goal,
        "update_hibob_goal_status": update_hibob_goal_status,
        "create_hibob_key_results": create_hibob_key_results,
        "update_hibob_key_results": update_hibob_key_results,
        "delete_hibob_key_result": delete_hibob_key_result,
        "update_hibob_key_results_progress": update_hibob_key_results_progress,
        "search_hibob_goal_cycles": search_hibob_goal_cycles,
        "search_hibob_job_profiles": search_hibob_job_profiles,
        "list_hibob_job_roles": list_hibob_job_roles,
        "list_hibob_job_families": list_hibob_job_families,
        "list_hibob_job_family_groups": list_hibob_job_family_groups,
        "get_hibob_job_catalog_metadata": get_hibob_job_catalog_metadata,
        "get_hibob_performance_snapshot": get_hibob_performance_snapshot,
    }
    for name, fn in _tools.items():
        executor.register(name, fn)

    # ── Tool definitions ─────────────────────────────────────────────────

    src = "integration:hibob"

    return [
        # ── People / Employees ────────────────────────────────────────
        ToolDefinition(
            name="search_hibob_employees",
            source=src,
            priority=0,
            description='Search for employees in HiBob. The HiBob search API only supports server-side filtering by exact email or employee ID. Without filters, returns employees with human-readable titles and departments. To include CUSTOM fields (e.g. an Equity / Option Grant field) in the output, pass their exact field IDs in fields_json — custom fields are NOT returned by default and are silently omitted otherwise. Discover field IDs with get_hibob_employee_fields_metadata. Use the query parameter to find employees by name (client-side match against the capped name-lookup result set, not exhaustive). If email or employee_id is provided, query is ignored.',
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-text search term. If it contains '@', it is used as an exact email filter when email/employee_id are not provided; otherwise it is used for client-side display-name filtering only within the capped name-lookup result set (limit=_NAME_LOOKUP_LIMIT). If email or employee_id is provided, query is ignored.",
                        "default": "",
                    },
                    "email": {
                        "type": "string",
                        "description": "Filter by exact employee email address",
                        "default": "",
                    },
                    "employee_id": {
                        "type": "string",
                        "description": "Filter by exact employee ID",
                        "default": "",
                    },
                    "show_inactive": {
                        "type": "boolean",
                        "description": "Include inactive/terminated employees",
                        "default": False,
                    },
                    "fields_json": {
                        "type": "string",
                        "description": 'JSON array of field IDs to return in dot notation (e.g. \'["root.displayName", "work.title", "equity.custom.field_1768430212695"]\'). Required to surface custom fields — use get_hibob_employee_fields_metadata to find their IDs.',
                        "default": "",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Maximum number of employees to return",
                        "default": 50,
                    },
                },
                "required": [],
            },
        ),
        ToolDefinition(
            name="get_hibob_employee",
            source=src,
            priority=0,
            description="Get detailed information about a specific HiBob employee. Accepts an employee ID, email address, or display name — automatically resolves to the correct employee. By default (include_custom_fields=true) it requests all field IDs from metadata (up to the API's 400-field limit), so CUSTOM fields in any category (e.g. an Equity / Option Grant field) are included alongside the standard fields. Values a field's category permission does not allow are silently omitted by HiBob. Pass fields_json to request a specific subset instead.",
            input_schema={
                "type": "object",
                "properties": {
                    "identifier": {
                        "type": "string",
                        "description": "Employee ID, email address, or display name",
                    },
                    "fields_json": {
                        "type": "string",
                        "description": "JSON array of specific field IDs (dot notation) to return. Overrides include_custom_fields. Discover IDs with get_hibob_employee_fields_metadata.",
                        "default": "",
                    },
                    "include_custom_fields": {
                        "type": "boolean",
                        "description": "When true (default) and no fields_json is given, fetch all field IDs from metadata so custom fields (outside root/about/employment/work) are included. Set false for a faster default-fields-only read.",
                        "default": True,
                    },
                },
                "required": ["identifier"],
            },
        ),
        ToolDefinition(
            name="list_hibob_profiles",
            source=src,
            priority=1,
            description="List all active employees with human-readable names, titles, departments, and sites. Uses the search API with humanReadable mode so all list-type fields (titles, departments) are resolved to display names instead of numeric IDs.",
            input_schema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Maximum number of profiles to return",
                        "default": 100,
                    }
                },
                "required": [],
            },
        ),
        ToolDefinition(
            name="create_hibob_employee",
            source=src,
            priority=0,
            description="Create a new employee in HiBob. Requires first name, surname, email, site, and start date. Use get_hibob_company_list to discover valid values for site and department.",
            input_schema={
                "type": "object",
                "properties": {
                    "first_name": {
                        "type": "string",
                        "description": "Employee first name",
                    },
                    "surname": {"type": "string", "description": "Employee surname"},
                    "email": {
                        "type": "string",
                        "description": "Employee email address",
                    },
                    "site": {
                        "type": "string",
                        "description": "Work site/office location",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Start date (ISO format, e.g. 2026-03-01)",
                    },
                    "department": {
                        "type": "string",
                        "description": "Department name",
                        "default": "",
                    },
                    "title": {
                        "type": "string",
                        "description": "Job title",
                        "default": "",
                    },
                    "extra_fields_json": {
                        "type": "string",
                        "description": "JSON object with additional fields",
                        "default": "",
                    },
                },
                "required": ["first_name", "surname", "email", "site", "start_date"],
            },
        ),
        ToolDefinition(
            name="update_hibob_employee",
            source=src,
            priority=1,
            description='Update an employee\'s fields. Accepts employee ID, email, or name. Pass a JSON object with fields to update. Use get_hibob_company_list to discover valid list values. Example: {"work": {"title": "Senior Engineer"}}.',
            input_schema={
                "type": "object",
                "properties": {
                    "employee_id": {
                        "type": "string",
                        "description": "Employee ID, email, or name",
                    },
                    "fields_json": {
                        "type": "string",
                        "description": "JSON object with fields to update",
                    },
                },
                "required": ["employee_id", "fields_json"],
            },
        ),
        ToolDefinition(
            name="terminate_hibob_employee",
            source=src,
            priority=1,
            description="Terminate an employee in HiBob. Accepts employee ID, email, or name.",
            input_schema={
                "type": "object",
                "properties": {
                    "employee_id": {
                        "type": "string",
                        "description": "Employee ID, email, or name",
                    },
                    "termination_date": {
                        "type": "string",
                        "description": "Termination date (ISO format)",
                    },
                    "termination_reason": {
                        "type": "string",
                        "description": "Reason for termination",
                        "default": "",
                    },
                    "notice_period_start": {
                        "type": "string",
                        "description": "Notice period start date",
                        "default": "",
                    },
                },
                "required": ["employee_id", "termination_date"],
            },
        ),
        ToolDefinition(
            name="get_hibob_employee_table",
            source=src,
            priority=0,
            description="Read historical data from an employee's HR table: work (roles, departments), employment (contracts, FTE), lifecycle (status changes), salary (payroll), equity (stock/RSUs), variable_payments (bonuses), training (certifications). List columns (title, department, site...) are shown as 'name (id N)', resolved from the company lists.",
            input_schema={
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Employee ID"},
                    "table_name": {
                        "type": "string",
                        "description": "Table to retrieve",
                        "enum": [
                            "work",
                            "employment",
                            "lifecycle",
                            "salary",
                            "equity",
                            "variable_payments",
                            "training",
                        ],
                    },
                },
                "required": ["employee_id", "table_name"],
            },
        ),
        ToolDefinition(
            name="add_hibob_employee_table_entry",
            source=src,
            priority=1,
            description="Add a new entry to an employee's HR table (work, employment, lifecycle, salary, equity, variable_payments, training). Pass the entry data as a JSON object.",
            input_schema={
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Employee ID"},
                    "table_name": {
                        "type": "string",
                        "description": "Table to add entry to",
                        "enum": [
                            "work",
                            "employment",
                            "lifecycle",
                            "salary",
                            "equity",
                            "variable_payments",
                            "training",
                        ],
                    },
                    "entry_json": {
                        "type": "string",
                        "description": "JSON object with the entry fields",
                    },
                },
                "required": ["employee_id", "table_name", "entry_json"],
            },
        ),
        # ── Time Off ──────────────────────────────────────────────────
        ToolDefinition(
            name="get_hibob_whos_out",
            source=src,
            priority=0,
            description="Get employees who are out of office in a date range. Great for coverage planning. Pass employee (ID, email or name) with from_date and to_date (at most 24 months) for one person's time-off history.",
            input_schema={
                "type": "object",
                "properties": {
                    "from_date": {
                        "type": "string",
                        "description": "Start date (ISO format)",
                        "default": "",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "End date (ISO format)",
                        "default": "",
                    },
                    "include_pending": {
                        "type": "boolean",
                        "description": "Include pending requests",
                        "default": False,
                    },
                    "employee": {
                        "type": "string",
                        "description": "Only this employee's time off (ID, email or display name); needs from_date and to_date",
                        "default": "",
                    },
                },
                "required": [],
            },
        ),
        ToolDefinition(
            name="get_hibob_out_today",
            source=src,
            priority=0,
            description="Get employees who are out of office today (or on a specific date). Quicker than get_hibob_whos_out for same-day queries.",
            input_schema={
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date to check (ISO format). Empty = today.",
                        "default": "",
                    }
                },
                "required": [],
            },
        ),
        ToolDefinition(
            name="get_hibob_timeoff_balance",
            source=src,
            priority=1,
            description="Get an employee's time-off balance for a given policy type as of a date. policy_type is required by HiBob — use list_hibob_timeoff_policy_types to find valid names.",
            input_schema={
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Employee ID"},
                    "policy_type": {
                        "type": "string",
                        "description": "Policy type name, e.g. 'Holiday' (required). Discover values with list_hibob_timeoff_policy_types.",
                    },
                    "date": {
                        "type": "string",
                        "description": "Balance as of this date (YYYY-MM-DD). Empty = current.",
                        "default": "",
                    },
                },
                "required": ["employee_id", "policy_type"],
            },
        ),
        ToolDefinition(
            name="submit_hibob_timeoff_request",
            source=src,
            priority=1,
            description="Submit a time-off request. Use list_hibob_timeoff_policy_types first to find valid policy types. Supports request types: days, hours, portionOnRange, hoursOnRange, percentageOnRange, openEnded, differentDayDurations.",
            input_schema={
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Employee ID"},
                    "policy_type": {
                        "type": "string",
                        "description": "Policy type name",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Start date (ISO format)",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date (ISO format)",
                    },
                    "request_type": {
                        "type": "string",
                        "description": "Request type",
                        "default": "days",
                    },
                    "description": {
                        "type": "string",
                        "description": "Reason",
                        "default": "",
                    },
                    "duration": {
                        "type": "integer",
                        "description": "Duration in hours (for hours type)",
                        "default": 0,
                    },
                    "start_date_portion": {
                        "type": "string",
                        "description": "morning or afternoon",
                        "default": "",
                    },
                    "end_date_portion": {
                        "type": "string",
                        "description": "morning or afternoon",
                        "default": "",
                    },
                },
                "required": ["employee_id", "policy_type", "start_date", "end_date"],
            },
        ),
        ToolDefinition(
            name="get_hibob_timeoff_request",
            source=src,
            priority=1,
            description="Get details of an existing time-off request.",
            input_schema={
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Employee ID"},
                    "request_id": {"type": "string", "description": "Request ID"},
                },
                "required": ["employee_id", "request_id"],
            },
        ),
        ToolDefinition(
            name="cancel_hibob_timeoff_request",
            source=src,
            priority=1,
            description="Cancel an existing time-off request.",
            input_schema={
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Employee ID"},
                    "request_id": {
                        "type": "string",
                        "description": "Request ID to cancel",
                    },
                },
                "required": ["employee_id", "request_id"],
            },
        ),
        ToolDefinition(
            name="list_hibob_timeoff_policy_types",
            source=src,
            priority=2,
            description="List all time-off policy types (e.g. Holiday, Sick Leave). Call this before submit_hibob_timeoff_request or create_hibob_timeoff_balance_adjustment to discover valid types.",
            input_schema={"type": "object", "properties": {}, "required": []},
        ),
        ToolDefinition(
            name="get_hibob_timeoff_policy_details",
            source=src,
            priority=1,
            description="Get details of a specific time-off policy type including associated policies.",
            input_schema={
                "type": "object",
                "properties": {
                    "policy_type": {"type": "string", "description": "Policy type name"}
                },
                "required": ["policy_type"],
            },
        ),
        ToolDefinition(
            name="list_hibob_timeoff_policies",
            source=src,
            priority=2,
            description="List all time-off policies with their names, types, and units. Provides more detail than list_hibob_timeoff_policy_types.",
            input_schema={"type": "object", "properties": {}, "required": []},
        ),
        ToolDefinition(
            name="list_hibob_timeoff_reason_codes",
            source=src,
            priority=2,
            description="List reason codes for a time-off policy type (e.g. sick leave reasons).",
            input_schema={
                "type": "object",
                "properties": {
                    "policy_type": {"type": "string", "description": "Policy type name"}
                },
                "required": ["policy_type"],
            },
        ),
        ToolDefinition(
            name="add_hibob_timeoff_reason_code",
            source=src,
            priority=2,
            description="Add a new reason code to a time-off policy type.",
            input_schema={
                "type": "object",
                "properties": {
                    "policy_type": {
                        "type": "string",
                        "description": "Policy type name",
                    },
                    "name": {"type": "string", "description": "Reason code name"},
                    "code": {
                        "type": "string",
                        "description": "Optional short code",
                        "default": "",
                    },
                },
                "required": ["policy_type", "name"],
            },
        ),
        ToolDefinition(
            name="get_hibob_timeoff_changes",
            source=src,
            priority=1,
            description="Get time-off request changes (created, approved, cancelled) in a date range. Useful for monitoring.",
            input_schema={
                "type": "object",
                "properties": {
                    "since": {
                        "type": "string",
                        "description": "Since date (ISO format)",
                        "default": "",
                    },
                    "until": {
                        "type": "string",
                        "description": "Until date (ISO format)",
                        "default": "",
                    },
                },
                "required": [],
            },
        ),
        ToolDefinition(
            name="create_hibob_timeoff_balance_adjustment",
            source=src,
            priority=1,
            description="Adjust an employee's time-off balance. Positive = add, negative = deduct. Use list_hibob_timeoff_policy_types first.",
            input_schema={
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Employee ID"},
                    "policy_type": {
                        "type": "string",
                        "description": "Policy type name",
                    },
                    "amount": {"type": "number", "description": "Adjustment amount"},
                    "effective_date": {
                        "type": "string",
                        "description": "Effective date (ISO format)",
                        "default": "",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason",
                        "default": "",
                    },
                },
                "required": ["employee_id", "policy_type", "amount"],
            },
        ),
        # ── Tasks ─────────────────────────────────────────────────────
        ToolDefinition(
            name="list_hibob_tasks",
            source=src,
            priority=1,
            description=(
                "List OPEN company-wide tasks (onboarding, offboarding, HR, "
                "compliance). Each task shows who it is 'Requested for' (the "
                "subject employee), the assignee, workflow, due date, status "
                "and link. Optional client-side filters: requested_for, "
                "assignee, workflow. Note: this route returns OPEN tasks only - "
                "for closed tasks use get_hibob_employee_tasks with "
                "task_status='closed'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "requested_for": {
                        "type": "string",
                        "description": (
                            "Filter to tasks requested for this employee "
                            "(matches id, display name or email)."
                        ),
                        "default": "",
                    },
                    "assignee": {
                        "type": "string",
                        "description": (
                            "Filter to tasks owned by / assigned to this "
                            "employee (matches id, display name or email)."
                        ),
                        "default": "",
                    },
                    "workflow": {
                        "type": "string",
                        "description": (
                            "Filter to a workflow / task-list name "
                            "(e.g. 'Offboarding')."
                        ),
                        "default": "",
                    },
                },
                "required": [],
            },
        ),
        ToolDefinition(
            name="get_hibob_employee_tasks",
            source=src,
            priority=1,
            description=(
                "Read tasks for a specific employee (by HiBob ID, email or "
                "name). This route is keyed to the task ASSIGNEE/owner - to "
                "find tasks requested for a leaver, pull the assignee's list "
                "and filter with requested_for. Supports task_status "
                "(open/closed/all; 'all' or omit returns everything) - HiBob's "
                "only native task filter - plus client-side requested_for and "
                "workflow filters. Output includes the 'Requested for' subject "
                "employee."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "employee_id": {
                        "type": "string",
                        "description": (
                            "Employee ID, email, or display name (the task "
                            "assignee/owner)."
                        ),
                    },
                    "task_status": {
                        "type": "string",
                        "description": (
                            "Filter by status: 'open', 'closed', or 'all'. "
                            "Use 'all' or omit to return everything "
                            "(open + closed)."
                        ),
                        "default": "",
                    },
                    "requested_for": {
                        "type": "string",
                        "description": (
                            "Filter to tasks requested for this subject "
                            "employee (matches id, display name or email)."
                        ),
                        "default": "",
                    },
                    "workflow": {
                        "type": "string",
                        "description": (
                            "Filter to a workflow / task-list name "
                            "(e.g. 'Offboarding')."
                        ),
                        "default": "",
                    },
                },
                "required": ["employee_id"],
            },
        ),
        ToolDefinition(
            name="complete_hibob_task",
            source=src,
            priority=1,
            description="Mark a task as complete.",
            input_schema={
                "type": "object",
                "properties": {"task_id": {"type": "string", "description": "Task ID"}},
                "required": ["task_id"],
            },
        ),
        # ── Reports ───────────────────────────────────────────────────
        ToolDefinition(
            name="list_hibob_reports",
            source=src,
            priority=2,
            description="List all company reports in HiBob with their IDs, names, types, and descriptions. Use the report ID with download_hibob_report. The public Bob API has no performance-review endpoint: review cycles, answers and ratings are read from a Bob report built on them, when the service user may see it.",
            input_schema={"type": "object", "properties": {}, "required": []},
        ),
        ToolDefinition(
            name="download_hibob_report",
            workspace_ingest=True,
            source=src,
            priority=1,
            description="Download a HiBob report by ID. The format query parameter is required by the API (csv, xlsx, or json). CSV and JSON are returned as text (the first 5,000 characters) and the whole report is stored as a file of the conversation (a download card and a file_id to forward); XLSX is stored the same way and its readable cells come with the result.",
            input_schema={
                "type": "object",
                "properties": {
                    "report_id": {
                        "type": "string",
                        "description": "Report ID (from list_hibob_reports)",
                    },
                    "report_format": {
                        "type": "string",
                        "enum": ["csv", "xlsx", "json"],
                        "description": "File format to download. csv (default) or json are human-readable here; xlsx is binary.",
                        "default": "csv",
                    },
                    "human_readable": {
                        "type": "boolean",
                        "description": "For json format only: resolve list IDs to their display values.",
                        "default": True,
                    },
                },
                "required": ["report_id"],
            },
        ),
        # ── Documents ─────────────────────────────────────────────────
        ToolDefinition(
            name="list_hibob_employee_documents",
            source=src,
            priority=1,
            description=(
                "List all documents for an employee (contracts, offer letters, certifications) "
                "with each document's download URL."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Employee ID"}
                },
                "required": ["employee_id"],
            },
        ),
        ToolDefinition(
            name="download_hibob_document",
            workspace_ingest=True,
            source=src,
            priority=1,
            description=(
                "Download an employee document into the conversation: a download card, its "
                "download URL and a file_id to forward to any tool that takes a file; its "
                "readable content comes with the result. Use list_hibob_employee_documents to "
                "find documents. Include the returned [[FILE:...]] marker verbatim so the user "
                "gets a download card."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Employee ID"},
                    "document": {
                        "type": "string",
                        "description": "The document's id, exact name, or #position in list_hibob_employee_documents",
                    },
                },
                "required": ["employee_id", "document"],
            },
        ),
        ToolDefinition(
            name="upload_hibob_document",
            source=src,
            priority=1,
            description=(
                "Upload a file of this conversation to an employee's document folder "
                "byte-for-byte: any file attached by the user, generated, or downloaded by "
                "another tool (a signed contract, a certificate...), by its file_id. Use "
                "list_hibob_doc_folders to find custom folder IDs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Employee ID"},
                    "file_id": {"type": "string", "description": "file_id of a file of this conversation"},
                    "folder_type": {
                        "type": "string",
                        "description": "Folder type: shared, confidential, or custom",
                        "default": "shared",
                    },
                    "folder_id": {
                        "type": "string",
                        "description": "Custom folder ID (required when folder_type=custom)",
                        "default": "",
                    },
                    "document_name": {
                        "type": "string",
                        "description": "Optional document name (default: the file's own name)",
                        "default": "",
                    },
                },
                "required": ["employee_id", "file_id"],
            },
        ),
        ToolDefinition(
            name="list_hibob_doc_folders",
            source=src,
            priority=2,
            description="List document folder metadata (shared, confidential, custom folders). Use to discover folder IDs for upload/delete.",
            input_schema={"type": "object", "properties": {}, "required": []},
        ),
        ToolDefinition(
            name="upload_hibob_document_url",
            source=src,
            priority=1,
            description=(
                "Upload a document to an employee's folder from a public URL HiBob fetches. For "
                "a file of this conversation use upload_hibob_document. Use "
                "list_hibob_doc_folders to find custom folder IDs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Employee ID"},
                    "url": {"type": "string", "description": "Document URL to upload"},
                    "folder_type": {
                        "type": "string",
                        "description": "Folder type: shared, confidential, or custom",
                        "default": "shared",
                    },
                    "folder_id": {
                        "type": "string",
                        "description": "Custom folder ID (required when folder_type=custom)",
                        "default": "",
                    },
                    "document_name": {
                        "type": "string",
                        "description": "Optional document name (default: the URL's file name)",
                        "default": "",
                    },
                },
                "required": ["employee_id", "url"],
            },
        ),
        ToolDefinition(
            name="delete_hibob_document",
            source=src,
            priority=1,
            description="Delete a document from an employee's folder.",
            input_schema={
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Employee ID"},
                    "doc_id": {"type": "string", "description": "Document ID"},
                    "folder_type": {
                        "type": "string",
                        "description": "Folder type: shared, confidential, or custom",
                        "default": "shared",
                    },
                    "folder_id": {
                        "type": "string",
                        "description": "Custom folder ID (required when folder_type=custom)",
                        "default": "",
                    },
                },
                "required": ["employee_id", "doc_id"],
            },
        ),
        # ── Metadata / Fields ─────────────────────────────────────────
        ToolDefinition(
            name="get_hibob_employee_fields_metadata",
            source=src,
            priority=2,
            description="Get all employee field definitions — IDs, names, types, categories. Call this first to discover available fields (including custom fields) before querying employee data with specific field IDs.",
            input_schema={"type": "object", "properties": {}, "required": []},
        ),
        ToolDefinition(
            name="create_hibob_field",
            source=src,
            priority=2,
            description="Create a new custom employee field.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Field display name"},
                    "category": {
                        "type": "string",
                        "description": "Category ID (e.g. 'work', 'about')",
                    },
                    "field_type": {
                        "type": "string",
                        "description": "Field type (text, number, date, list, boolean, etc.)",
                    },
                    "description": {
                        "type": "string",
                        "description": "Field description",
                        "default": "",
                    },
                },
                "required": ["name", "category", "field_type"],
            },
        ),
        ToolDefinition(
            name="list_hibob_company_lists",
            source=src,
            priority=2,
            description="List all company named lists (departments, sites, teams, etc.) with item counts. Reference tool — use to discover valid values for list-type fields.",
            input_schema={"type": "object", "properties": {}, "required": []},
        ),
        ToolDefinition(
            name="get_hibob_company_list",
            source=src,
            priority=2,
            description="Get all items in a company list by name. Use to find valid values for fields like department, site, title.",
            input_schema={
                "type": "object",
                "properties": {
                    "list_name": {
                        "type": "string",
                        "description": "List name (e.g. 'department', 'site')",
                    }
                },
                "required": ["list_name"],
            },
        ),
        ToolDefinition(
            name="add_hibob_list_item",
            source=src,
            priority=2,
            description="Add a new item to a company named list.",
            input_schema={
                "type": "object",
                "properties": {
                    "list_name": {"type": "string", "description": "List name"},
                    "name": {"type": "string", "description": "Item name/value"},
                    "parent_id": {
                        "type": "string",
                        "description": "Parent item ID (for hierarchical lists)",
                        "default": "",
                    },
                },
                "required": ["list_name", "name"],
            },
        ),
        ToolDefinition(
            name="update_hibob_list_item",
            source=src,
            priority=2,
            description="Rename an item in a company named list.",
            input_schema={
                "type": "object",
                "properties": {
                    "list_name": {"type": "string", "description": "List name"},
                    "item_id": {"type": "string", "description": "Item ID to update"},
                    "new_name": {"type": "string", "description": "New display name"},
                },
                "required": ["list_name", "item_id", "new_name"],
            },
        ),
        # ── Workforce Planning ────────────────────────────────────────
        ToolDefinition(
            name="search_hibob_positions",
            source=src,
            priority=2,
            description="Search company positions in workforce planning (POST /objects/position/search). By default returns non-cancelled positions. Supported filter fields: /position/status, /position/name, /position/hasOpenRequests, /position/id (operators: equals, notEqual).",
            input_schema={
                "type": "object",
                "properties": {
                    "filters_json": {
                        "type": "string",
                        "description": 'JSON array of filter objects, e.g. \'[{"fieldId": "/position/status", "operator": "equals", "values": ["vacant"]}]\'. Empty = all non-cancelled positions.',
                        "default": "",
                    },
                    "fields_json": {
                        "type": "string",
                        "description": 'JSON array of position field IDs to return, e.g. \'["/position/name", "/position/status"]\'. Empty = a sensible default set.',
                        "default": "",
                    },
                },
                "required": [],
            },
        ),
        # ── Attendance ────────────────────────────────────────────────
        ToolDefinition(
            name="get_hibob_attendance_summary",
            source=src,
            priority=2,
            description="Fetch aggregated attendance summaries (worked/payable/overtime hours) for one or more employees over a date range. The HiBob API requires both a date range and an employee filter, so employee_ids_json is mandatory.",
            input_schema={
                "type": "object",
                "properties": {
                    "from_date": {
                        "type": "string",
                        "description": "Start date (YYYY-MM-DD)",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "End date (YYYY-MM-DD)",
                    },
                    "employee_ids_json": {
                        "type": "string",
                        "description": 'JSON array of employee IDs (required), e.g. \'["123456789"]\'.',
                    },
                },
                "required": ["from_date", "to_date", "employee_ids_json"],
            },
        ),
        ToolDefinition(
            name="get_hibob_attendance_daily",
            source=src,
            priority=2,
            description="Fetch daily attendance breakdown — per-day worked/payable/overtime hours and approval status. The HiBob API requires both a date range and an employee filter, so employee_ids_json is mandatory.",
            input_schema={
                "type": "object",
                "properties": {
                    "from_date": {
                        "type": "string",
                        "description": "Start date (YYYY-MM-DD)",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "End date (YYYY-MM-DD)",
                    },
                    "employee_ids_json": {
                        "type": "string",
                        "description": 'JSON array of employee IDs (required), e.g. \'["123456789"]\'.',
                    },
                },
                "required": ["from_date", "to_date", "employee_ids_json"],
            },
        ),
        ToolDefinition(
            name="import_hibob_attendance",
            source=src,
            priority=2,
            description="Import attendance entries (clock-in/out) into HiBob. For integrating physical time clocks.",
            input_schema={
                "type": "object",
                "properties": {
                    "entries_json": {
                        "type": "string",
                        "description": "JSON array of attendance entries (employeeId, clockIn, clockOut, type)",
                    },
                    "import_method": {
                        "type": "string",
                        "description": "Import method: aggregate or immediate",
                        "default": "aggregate",
                    },
                },
                "required": ["entries_json"],
            },
        ),
        ToolDefinition(
            name="search_hibob_projects",
            source=src,
            priority=2,
            description="Search attendance projects for time tracking.",
            input_schema={
                "type": "object",
                "properties": {
                    "filters_json": {
                        "type": "string",
                        "description": "JSON filters",
                        "default": "",
                    }
                },
                "required": [],
            },
        ),
        ToolDefinition(
            name="create_hibob_project",
            source=src,
            priority=2,
            description="Create an attendance project for time tracking.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Project name"},
                    "description": {
                        "type": "string",
                        "description": "Description",
                        "default": "",
                    },
                    "billable": {
                        "type": "boolean",
                        "description": "Is project billable",
                        "default": False,
                    },
                    "member_ids_json": {
                        "type": "string",
                        "description": "JSON array of employee IDs to assign",
                        "default": "",
                    },
                },
                "required": ["name"],
            },
        ),
        ToolDefinition(
            name="update_hibob_project",
            source=src,
            priority=2,
            description="Update an attendance project.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID"},
                    "fields_json": {
                        "type": "string",
                        "description": "JSON object with fields to update",
                    },
                },
                "required": ["project_id", "fields_json"],
            },
        ),
        ToolDefinition(
            name="search_hibob_project_tasks",
            source=src,
            priority=2,
            description="Search project tasks (activity types for time tracking).",
            input_schema={
                "type": "object",
                "properties": {
                    "filters_json": {
                        "type": "string",
                        "description": "JSON filters",
                        "default": "",
                    }
                },
                "required": [],
            },
        ),
        # ── Custom Tables ─────────────────────────────────────────────
        ToolDefinition(
            name="list_hibob_custom_tables",
            source=src,
            priority=2,
            description="List all custom table definitions. Custom tables hold additional structured data per employee.",
            input_schema={"type": "object", "properties": {}, "required": []},
        ),
        ToolDefinition(
            name="get_hibob_custom_table_entries",
            source=src,
            priority=1,
            description="Read entries of a custom table for a specific employee.",
            input_schema={
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Employee ID"},
                    "table_id": {
                        "type": "string",
                        "description": "Custom table ID (from list_hibob_custom_tables)",
                    },
                },
                "required": ["employee_id", "table_id"],
            },
        ),
        ToolDefinition(
            name="create_hibob_custom_table_entry",
            source=src,
            priority=1,
            description="Add a new entry to an employee's custom table.",
            input_schema={
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Employee ID"},
                    "table_id": {"type": "string", "description": "Custom table ID"},
                    "entry_json": {
                        "type": "string",
                        "description": "JSON object with entry fields",
                    },
                },
                "required": ["employee_id", "table_id", "entry_json"],
            },
        ),
        ToolDefinition(
            name="update_hibob_custom_table_entry",
            source=src,
            priority=1,
            description="Update an existing entry in an employee's custom table.",
            input_schema={
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Employee ID"},
                    "table_id": {"type": "string", "description": "Custom table ID"},
                    "entry_id": {"type": "string", "description": "Entry ID to update"},
                    "entry_json": {
                        "type": "string",
                        "description": "JSON object with updated fields",
                    },
                },
                "required": ["employee_id", "table_id", "entry_id", "entry_json"],
            },
        ),
        ToolDefinition(
            name="delete_hibob_custom_table_entry",
            source=src,
            priority=1,
            description="Delete an entry from an employee's custom table.",
            input_schema={
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Employee ID"},
                    "table_id": {"type": "string", "description": "Custom table ID"},
                    "entry_id": {"type": "string", "description": "Entry ID to delete"},
                },
                "required": ["employee_id", "table_id", "entry_id"],
            },
        ),
        # ── Lifecycle ─────────────────────────────────────────────────
        ToolDefinition(
            name="revoke_hibob_employee_access",
            source=src,
            priority=1,
            description="Revoke an employee's access to Bob (uninvite).",
            input_schema={
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Employee ID"}
                },
                "required": ["employee_id"],
            },
        ),
        ToolDefinition(
            name="invite_hibob_employee",
            source=src,
            priority=1,
            description="Invite an employee to Bob with a welcome wizard.",
            input_schema={
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Employee ID"},
                    "wizard_id": {"type": "string", "description": "Welcome wizard ID"},
                },
                "required": ["employee_id", "wizard_id"],
            },
        ),
        ToolDefinition(
            name="set_hibob_employee_start_date",
            source=src,
            priority=1,
            description="Set or update an employee's start date.",
            input_schema={
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Employee ID"},
                    "start_date": {
                        "type": "string",
                        "description": "Start date (ISO format)",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason for change",
                        "default": "",
                    },
                },
                "required": ["employee_id", "start_date"],
            },
        ),
        # ── Goals (OKR) ──────────────────────────────────────────────
        ToolDefinition(
            name="search_hibob_goal_types",
            source=src,
            priority=2,
            description="List all goal types (Company, Department, Team, Individual) and their hierarchy. Call this before creating goals.",
            input_schema={"type": "object", "properties": {}, "required": []},
        ),
        ToolDefinition(
            name="search_hibob_goals",
            source=src,
            priority=1,
            description=(
                "Search goals by status, type, privacy, owner or cycle, with "
                "progress; include_key_results adds each goal's KRs. Check-ins "
                "and comments are not in the public Goals API."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {
                        "type": "string",
                        "description": "Goal owner: employee ID, email or name (filtered over all pages)",
                        "default": "",
                    },
                    "cycle_id": {
                        "type": "string",
                        "description": "Goal cycle ID (from search_hibob_goal_cycles)",
                        "default": "",
                    },
                    "include_key_results": {
                        "type": "boolean",
                        "description": "Add each listed goal's key results",
                        "default": False,
                    },
                    "status": {
                        "type": "string",
                        "description": "Filter: onTrack, offTrack, completed, incomplete, other",
                        "default": "",
                    },
                    "type_id": {
                        "type": "string",
                        "description": "Filter by goal type ID (from search_hibob_goal_types)",
                        "default": "",
                    },
                    "is_private": {
                        "type": "string",
                        "description": "Filter: 'true' or 'false'",
                        "default": "",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max results",
                        "default": 50,
                    },
                },
                "required": [],
            },
        ),
        ToolDefinition(
            name="get_hibob_goal",
            source=src,
            priority=1,
            description=(
                "Retrieve a single HiBob goal by id (detailed view: status, "
                "owner, dates, type, cycle, description). Use after "
                "search_hibob_goals to read a specific goal."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "goal_id": {
                        "type": "string",
                        "description": "Goal ID to retrieve",
                    },
                    "fields_json": {
                        "type": "string",
                        "description": "Optional JSON array of field IDs to return",
                        "default": "",
                    },
                },
                "required": ["goal_id"],
            },
        ),
        ToolDefinition(
            name="search_hibob_key_results",
            source=src,
            priority=1,
            description="Search key results (KRs) for a specific goal. goal_id is required — the API returns 400 without it.",
            input_schema={
                "type": "object",
                "properties": {
                    "goal_id": {
                        "type": "string",
                        "description": "Goal ID to fetch KRs for",
                    },
                    "fields_json": {
                        "type": "string",
                        "description": "Optional JSON array of field IDs to return",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max KRs per page",
                        "default": 50,
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Pagination cursor",
                        "default": "",
                    },
                },
                "required": ["goal_id"],
            },
        ),
        ToolDefinition(
            name="create_hibob_goal",
            source=src,
            priority=1,
            description="Create a goal. Use search_hibob_goal_types first to get type IDs. Goals can be aligned to higher-order goals.",
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Goal title"},
                    "type_id": {"type": "string", "description": "Goal type ID"},
                    "owner": {"type": "string", "description": "Owner employee ID"},
                    "start_date": {
                        "type": "string",
                        "description": "Start date (ISO format)",
                    },
                    "due_date": {
                        "type": "string",
                        "description": "Due date (ISO format)",
                    },
                    "description": {
                        "type": "string",
                        "description": "Description",
                        "default": "",
                    },
                    "is_private": {
                        "type": "boolean",
                        "description": "Private goal",
                        "default": False,
                    },
                    "aligned_goal": {
                        "type": "string",
                        "description": "ID of higher-order goal to align with",
                        "default": "",
                    },
                    "type_list_item": {
                        "type": "string",
                        "description": "List item ID for custom goal types",
                        "default": "",
                    },
                },
                "required": ["title", "type_id", "owner", "start_date", "due_date"],
            },
        ),
        ToolDefinition(
            name="update_hibob_goal",
            source=src,
            priority=1,
            description="Update a goal's fields (title, dates, description, etc.). Does NOT update status (use update_hibob_goal_status) or owner (HiBob refuses it on update).",
            input_schema={
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string", "description": "Goal ID"},
                    "fields_json": {
                        "type": "string",
                        "description": "JSON object with fields to update",
                    },
                },
                "required": ["goal_id", "fields_json"],
            },
        ),
        ToolDefinition(
            name="delete_hibob_goal",
            source=src,
            priority=1,
            description="Delete a goal.",
            input_schema={
                "type": "object",
                "properties": {"goal_id": {"type": "string", "description": "Goal ID"}},
                "required": ["goal_id"],
            },
        ),
        ToolDefinition(
            name="update_hibob_goal_status",
            source=src,
            priority=1,
            description="Update a goal's status. Valid values: onTrack, offTrack, completed, incomplete, other.",
            input_schema={
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string", "description": "Goal ID"},
                    "status": {"type": "string", "description": "New status"},
                    "comment": {
                        "type": "string",
                        "description": "Status update comment",
                        "default": "",
                    },
                },
                "required": ["goal_id", "status"],
            },
        ),
        ToolDefinition(
            name="create_hibob_key_results",
            source=src,
            priority=1,
            description="Create key results for a goal. Pass a JSON array of key result objects with title, measureType (or measure: percentage/numeric/currency/boolean), and target.",
            input_schema={
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string", "description": "Goal ID"},
                    "key_results_json": {
                        "type": "string",
                        "description": "JSON array of key result objects (accepts measureType or measure)",
                    },
                },
                "required": ["goal_id", "key_results_json"],
            },
        ),
        ToolDefinition(
            name="update_hibob_key_results",
            source=src,
            priority=1,
            description=(
                "Update key result details (title, target, measureType/measure, etc.). "
                "Every field sent is read back and an error names any value HiBob did not store."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string", "description": "Goal ID"},
                    "key_results_json": {
                        "type": "string",
                        "description": "JSON array of key result update objects (accepts measureType or measure)",
                    },
                },
                "required": ["goal_id", "key_results_json"],
            },
        ),
        ToolDefinition(
            name="delete_hibob_key_result",
            source=src,
            priority=1,
            description=(
                "Delete one key result of a goal. Used to replace a key result whose "
                "measure HiBob will not change: create the new one first, then delete the old."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string", "description": "Goal ID"},
                    "key_result_id": {"type": "string", "description": "Key result ID"},
                },
                "required": ["goal_id", "key_result_id"],
            },
        ),
        ToolDefinition(
            name="update_hibob_key_results_progress",
            source=src,
            priority=1,
            description=(
                "Check in on key results progress (update currentValue). Does NOT change goal status. "
                "A decimal currentValue is read back and an error names any value HiBob did not store."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string", "description": "Goal ID"},
                    "progress_json": {
                        "type": "string",
                        "description": "JSON array of progress update objects with keyResultId and currentValue",
                    },
                },
                "required": ["goal_id", "progress_json"],
            },
        ),
        ToolDefinition(
            name="search_hibob_goal_cycles",
            source=src,
            priority=2,
            description="List goal cycles (timeframes for goals).",
            input_schema={"type": "object", "properties": {}, "required": []},
        ),
        # ── Job Catalog ──────────────────────────────────────────────
        ToolDefinition(
            name="search_hibob_job_profiles",
            source=src,
            priority=1,
            description="Search job profiles with optional filters by status, role, or family. Returns titles with human-readable values.",
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter: active, draft, inactive",
                        "default": "",
                    },
                    "role_id": {
                        "type": "string",
                        "description": "Filter by role ID (from list_hibob_job_roles)",
                        "default": "",
                    },
                    "family_id": {
                        "type": "string",
                        "description": "Filter by family ID (from list_hibob_job_families)",
                        "default": "",
                    },
                },
                "required": [],
            },
        ),
        ToolDefinition(
            name="list_hibob_job_roles",
            source=src,
            priority=2,
            description="List all job roles in the job catalog.",
            input_schema={"type": "object", "properties": {}, "required": []},
        ),
        ToolDefinition(
            name="list_hibob_job_families",
            source=src,
            priority=2,
            description="List all job families in the job catalog.",
            input_schema={"type": "object", "properties": {}, "required": []},
        ),
        ToolDefinition(
            name="list_hibob_job_family_groups",
            source=src,
            priority=2,
            description="List all job family groups (top-level domains) in the job catalog.",
            input_schema={"type": "object", "properties": {}, "required": []},
        ),
        ToolDefinition(
            name="get_hibob_job_catalog_metadata",
            source=src,
            priority=2,
            description="Get job profile field definitions (metadata). Discover available fields including custom fields.",
            input_schema={"type": "object", "properties": {}, "required": []},
        ),
        # ── Performance snapshot ───────────────────────────────────────
        ToolDefinition(
            name="get_hibob_performance_snapshot",
            source=src,
            priority=1,
            description=(
                "One employee's assessment file in one call: profile (withheld fields named), "
                "work and lifecycle history, time off by policy over a range, goals with key "
                "results, documents. Reviews are not in the public API (Bob MCP)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "employee": {"type": "string", "description": "Employee ID, email or name"},
                    "from_date": {
                        "type": "string",
                        "description": "Time-off range start, YYYY-MM-DD (default: 365 days before to_date)",
                        "default": "",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "Time-off range end, YYYY-MM-DD (default: today); 24 months at most",
                        "default": "",
                    },
                    "include_goals": {"type": "boolean", "description": "Add goals and key results", "default": True},
                    "include_documents": {"type": "boolean", "description": "Add the documents list", "default": True},
                },
                "required": ["employee"],
            },
        ),
    ]
