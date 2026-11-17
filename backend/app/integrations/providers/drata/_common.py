"""Drata integration - shared helpers, constants, pagination and base-URL routing.

Region routing: the dispatcher prepends the v2 base URL via the compound
``base_url||api_key`` token. A handful of control external-evidence endpoints
live on the v1 API; ``to_v1_base_url`` derives that host from the v2 one.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from app.integrations.file_delivery import uploaded_file_answer
from app.integrations.http import (
    safe_request,
)
from app.tools.base import FileContext

logger = logging.getLogger(__name__)

_PROVIDER = "Drata"

# ---------------------------------------------------------------------------
# Region → base-URL mapping
# ---------------------------------------------------------------------------

REGION_URLS: dict[str, str] = {
    "us": "https://public-api.drata.com/public/v2",
    "eu": "https://public-api.eu.drata.com/public/v2",
    "apac": "https://public-api.apac.drata.com/public/v2",
}

DEFAULT_BASE_URL = REGION_URLS["us"]

#: Stat scopes accepted by the v2 ``GET /vendors-stats`` endpoint
#: (VendorStatsExpandEnum). At least one is required or the API returns 400.
_VENDOR_STATS_SCOPES: tuple[str, ...] = (
    "reminder",
    "hasPii",
    "businessUnits",
    "passwordPolicy",
    "status",
    "isCritical",
    "isSubProcessor",
    "type",
    "risk",
    "impactLevel",
)

#: Risk groups accepted by the v2 Risk Library bulk-copy endpoint
#: (``POST /risk-library/action-copy`` with bulkActionType=COPY_BY_GROUPS).
_RISK_LIBRARY_GROUPS: tuple[str, ...] = (
    "WIZARD_CORE",
    "AI_DEVELOPMENT",
    "AI_USE",
    "PHYSICAL_SITE",
    "CLOUD_ENVIRONMENT",
    "UNSECURE_DEVICES",
    "DEVICE_DELIVERY",
    "SOFTWARE_DEVELOPMENT",
    "REGULATORY_REQUIREMENTS",
)

#: Vendor Security Review enums (VendorSecurityReview*Enum). Used by the review
#: list filters, create payloads, and tool schemas.
_VENDOR_SR_STATUSES: tuple[str, ...] = (
    "NOT_YET_STARTED",
    "IN_PROGRESS",
    "COMPLETED",
    "NOT_REQUIRED",
)
_VENDOR_SR_TYPES: tuple[str, ...] = ("SECURITY", "SOC_REPORT", "UPLOAD_REPORT")
_VENDOR_SR_DECISIONS: tuple[str, ...] = (
    "PENDING",
    "APPROVED",
    "APPROVED_WITH_CONDITIONS",
    "REJECTED",
)
_VENDOR_SR_ACTIONS: tuple[str, ...] = ("finalize", "reopen")

#: Vendor Document types (VendorDocumentTypeEnum) accepted by the document
#: list filter and upload endpoint.
_VENDOR_DOCUMENT_TYPES: tuple[str, ...] = (
    "COMPLIANCE_REPORT",
    "COMPLIANCE_REPORT_REVIEW",
    "BRIDGE_LETTER",
    "UPLOADED_COMPLIANCE_REPORT_REVIEW",
    "QUESTIONNAIRE_ATTACHMENT",
    "SOC_DOCUMENT",
    "QUESTIONNAIRE_REPORT",
)


def resolve_base_url(region: str | None = None) -> str:
    """Return the Drata API base URL for a given region key.

    Falls back to the US endpoint when *region* is ``None`` or unknown.
    """
    if region:
        return REGION_URLS.get(region.lower().strip(), DEFAULT_BASE_URL)
    return DEFAULT_BASE_URL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _upload_headers(token: str) -> dict[str, str]:
    """Auth headers for ``multipart/form-data`` uploads.

    Deliberately omits ``Content-Type`` so httpx sets the
    ``multipart/form-data; boundary=…`` header itself when ``files=`` is
    supplied — forcing ``application/json`` here would corrupt the request.
    """
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def _full_name(person: dict[str, Any] | None) -> str:
    if not person:
        return "N/A"
    first = (person.get("firstName") or "").strip()
    last = (person.get("lastName") or "").strip()
    name = f"{first} {last}".strip()
    return name or person.get("email") or "N/A"


def _ids_from_csv(value: str) -> list[dict[str, int]]:
    """Convert a comma-separated string of integers into ``[{"id": n}, …]``.

    Drata's write endpoints expect arrays of objects of the form ``{"id": N}``
    for owners / reviewers / controls / categories. Accepting a CSV string
    keeps the tool signature flat for the LLM.
    """
    out: list[dict[str, int]] = []
    for chunk in (value or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append({"id": int(chunk)})
        except ValueError:
            continue
    return out


def _int_csv(value: str) -> list[int]:
    """Convert a comma-separated string of integers into ``[n, …]``.

    Used for the Evidence Library ``controlIds`` array — the v2 API expects a
    flat list of numeric control IDs there, unlike the ``[{"id": n}]`` shape
    other Drata write endpoints take (see :func:`_ids_from_csv`).
    """
    out: list[int] = []
    for chunk in (value or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError:
            continue
    return out


def _coerce_int(value: Any) -> int | None:
    """Best-effort int coercion; ``None`` when the value isn't int-like.

    Tool arguments are not type-coerced on every execution path: the
    integration tool-test endpoint dispatches straight to the handler and
    bypasses the agent loop's jsonschema pre-validation, so a schema integer
    can arrive as a JSON string (or other non-int). Coerce defensively rather
    than letting ``int()``/comparisons raise ``TypeError``.

    JSON booleans are rejected (``None``): ``bool`` is a subclass of ``int`` so
    ``int(True) == 1`` / ``int(False) == 0`` would otherwise silently treat a
    malformed ``{"control_id": true}`` as control 1 and act on the wrong
    control/owner instead of skipping it as invalid input.
    """
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_str(value: Any) -> str:
    """Best-effort string coercion; ``""`` when the value isn't a string.

    The string analogue of ``_coerce_int``: tool arguments are not type-coerced
    on every execution path (the executor dispatches straight to the handler),
    so a schema string can arrive as a JSON object / number / boolean. Returning
    ``""`` for a non-string lets the existing required-field and enum guards
    return a friendly error instead of ``.strip()`` / ``.split()`` raising
    ``AttributeError``.
    """
    return value if isinstance(value, str) else ""


def _coerce_bool(value: Any) -> bool | None:
    """Best-effort bool coercion; ``None`` when the value isn't bool-like.

    The boolean analogue of ``_coerce_int``: a schema boolean can arrive as the
    JSON string ``"true"`` / ``"false"`` on the paths that skip schema
    validation. Anything else (a number, an object, another string) is
    ``None`` so the caller refuses it instead of guessing a toggle direction.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return None


# ``%`` is deliberately excluded from the local part: the value is interpolated
# raw into the request path, so allowing ``%`` would let a caller smuggle a
# percent-encoded reserved character (e.g. ``%2F`` -> ``/``, ``%23`` -> ``#``)
# into ``/users/{ref}/documents``. Plain-addressing ``+``, ``.``, ``_`` and ``-``
# stay (they're literal and safe in a path segment).
_USER_EMAIL_RE = re.compile(r"^[A-Za-z0-9._+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _user_ref(user_id: Any) -> str:
    """Normalize and validate a ``/users/{userId}`` path identifier.

    The v2 User Documents endpoints accept either a numeric Drata user ID or an
    ``email:<address>`` path form. Agents reconciling leavers from an HRIS often
    have only the person's email, so a bare email is accepted and rewritten to
    the documented ``email:`` form (the ``email:`` prefix is normalized to
    lowercase). Returns ``""`` for anything that is neither a bare integer nor a
    well-formed email: the value is interpolated into the request path, so
    rejecting stray ``/`` / ``?`` / ``#`` / whitespace / other characters here
    prevents path- and query-injection into the Drata API URL (the caller then
    returns a friendly "user_id is required" error).
    """
    ref = _coerce_str(user_id).strip()
    if not ref:
        return ""
    body = ref[6:] if ref.lower().startswith("email:") else ref
    if "@" in body:
        return f"email:{body}" if _USER_EMAIL_RE.match(body) else ""
    return ref if ref.isdigit() else ""


# ---------------------------------------------------------------------------
# Control-note identifiers
# ---------------------------------------------------------------------------

#: Drata v2 control notes are identified by UUID - the ``id`` every note carries
#: in the list / create / get responses (e.g. ``81584418-17a7-44b1-b0f9-47d42b903812``).
#: RFC 4122 accepts either case on input, so the match is case-insensitive.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

#: A purely numeric identifier, ASCII digits only. ``str.isdigit`` would also
#: accept Unicode digits (superscripts, other scripts) that Drata never issues.
_NUMERIC_ID_RE = re.compile(r"^[0-9]+$")


def _note_ref(note_id: Any) -> str:
    """Normalize and validate a control-note ``{noteId}`` path identifier.

    Drata keys control notes by UUID, not by integer: the wrapper's former
    ``integer`` schema made the agent loop reject every real note id at
    validation time, so no note could ever be updated or deleted. A UUID is
    accepted in either case and forwarded unchanged. For backward compatibility
    a purely numeric value (an int, or a string of ASCII digits) is forwarded
    unchanged as well.

    Returns ``""`` for anything else. The value is interpolated raw into the
    request path, so refusing stray ``/`` / ``?`` / ``#`` / whitespace / other
    characters here prevents path- and query-injection into the Drata URL; the
    caller then returns a friendly error without issuing any HTTP request.
    """
    if isinstance(note_id, bool):
        return ""
    if isinstance(note_id, int):
        return str(note_id) if note_id >= 0 else ""
    ref = _coerce_str(note_id).strip()
    if not ref:
        return ""
    if _UUID_RE.match(ref) or _NUMERIC_ID_RE.match(ref):
        return ref
    return ""


# ---------------------------------------------------------------------------
# Verbatim error surfacing
# ---------------------------------------------------------------------------

#: Cap on the response body echoed verbatim into a tool error. Drata error
#: bodies are small (a validation constraint, a message, a code); the cap only
#: protects the agent's context against a pathological upstream response.
_ERROR_BODY_MAX_CHARS = 4000


def _verbatim_body(resp: httpx.Response) -> str:
    """Return the response body verbatim: compact JSON when it parses, raw text otherwise."""
    try:
        text = json.dumps(resp.json(), ensure_ascii=False, separators=(",", ":"))
    except Exception:
        text = resp.text or ""
    if len(text) > _ERROR_BODY_MAX_CHARS:
        text = text[:_ERROR_BODY_MAX_CHARS] + "... [truncated]"
    return text or "<empty body>"


def _format_drata_error(resp: httpx.Response, action: str, *, required_permission: str = "") -> str:
    """Build a tool error carrying Drata's HTTP status code and body verbatim.

    ``format_http_error`` (the cross-provider helper behind ``safe_request``)
    reduces a body to its ``message`` / ``error`` / ``errors[]`` detail and
    otherwise degrades to a generic "bad request". Drata's NestJS validation
    errors carry none of those keys (``{"property": "expand", "constraints":
    {"EnumValidator": "..."}}``), so the agent was told "bad request" with no
    way to self-correct. Echoing the body verbatim is what makes the failure
    actionable; the short hint per status only orients the reader.

    ``required_permission`` names the Drata API key permission the endpoint
    documents (e.g. ``Connections: Manage Version Control User Identities``);
    a 403 then tells the operator exactly which permission to grant.
    """
    status = resp.status_code
    if status == 401:
        hint = " Authentication failed: the API key may be expired or revoked; re-authorize the integration."
    elif status == 403 and required_permission:
        hint = (
            f" Access denied: the Drata API key lacks the '{required_permission}' permission; "
            "grant it to the key in Drata > Account Settings > API Keys."
        )
    elif status == 403:
        hint = " Access denied: the API key lacks the permission for this action or does not own the resource."
    elif status == 404:
        hint = " Resource not found."
    elif status == 409:
        hint = " Conflict: the request conflicts with the resource's current state in Drata."
    elif status == 412:
        hint = (
            " Precondition failed: the Drata terms and conditions must be accepted by the API key's user "
            "before the API can be used."
        )
    elif status == 429:
        hint = " Rate limit exceeded; retry later."
    elif status >= 500:
        hint = " Drata server error; the service may be temporarily unavailable."
    else:
        hint = ""
    return f"Error: Drata returned HTTP {status} while trying to {action}.{hint} Response body: {_verbatim_body(resp)}"


async def _request_verbatim(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    action: str,
    *,
    required_permission: str = "",
    **kwargs: Any,
) -> tuple[httpx.Response | None, str | None]:
    """``safe_request`` whose HTTP errors carry Drata's status code and body verbatim.

    Returns ``(response, None)`` on a 2xx, ``(response, error)`` on a non-2xx
    (the failed response is kept so the caller can still branch on
    ``response.status_code``, e.g. to explain a 403 as an owner mismatch) and
    ``(None, error)`` on a network error. The 429 retry and the warning log of
    ``safe_request`` are unchanged. ``required_permission`` is forwarded to
    :func:`_format_drata_error` so a 403 names the missing API key permission.
    """
    resp, err = await safe_request(client, method, url, _PROVIDER, action, expected_statuses=set(), **kwargs)
    if err is None or resp is None:
        return resp, err
    return resp, _format_drata_error(resp, action, required_permission=required_permission)


# ---------------------------------------------------------------------------
# Assigned-policy helpers
# ---------------------------------------------------------------------------


def _dedupe_assigned_policies(
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Collapse the per-(policy, version) assignment rows to one row per policy.

    ``GET /users/{id}/assigned-policies`` returns a row for every version a user
    was ever assigned - including archived / superseded versions - so a single
    policy can appear 4-5x. We keep the row that best represents the *current*
    compliance obligation: the current published version, else an accepted row,
    else the highest version id. When the kept row is unaccepted but a sibling
    row (an older version) was accepted, the latest such date is stashed on the
    kept row under the private ``_priorVersionAcceptedAt`` key so the formatter
    can flag a pending re-acknowledgment. Returns ``(deduped, dropped_count)``;
    input order of first-seen policies is preserved.
    """

    def _version_id(ap: dict[str, Any]) -> int:
        version = ap.get("policyVersion") or {}
        raw = ap.get("policyVersionId") or version.get("id") or 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    def _rank(ap: dict[str, Any]) -> tuple[bool, bool, int]:
        version = ap.get("policyVersion") or {}
        return (bool(version.get("current")), bool(ap.get("acceptedAt")), _version_id(ap))

    groups: dict[Any, list[dict[str, Any]]] = {}
    for ap in items:
        key = ap.get("policyId") or (ap.get("policy") or {}).get("id") or id(ap)
        groups.setdefault(key, []).append(ap)

    deduped: list[dict[str, Any]] = []
    dropped = 0
    for group in groups.values():
        best = max(group, key=_rank)
        if not best.get("acceptedAt"):
            prior = [ap.get("acceptedAt") for ap in group if ap.get("acceptedAt")]
            if prior:
                best["_priorVersionAcceptedAt"] = max(prior)
        deduped.append(best)
        dropped += len(group) - 1
    return deduped, dropped


def _fmt_assigned_policy(ap: dict[str, Any]) -> str:
    """Render one assigned policy with an acceptance-first verdict.

    ``acceptedAt`` is the single authoritative acceptance signal: a missing
    ``acceptedAt`` is ALWAYS reported as NOT ACCEPTED. We never collapse a
    pending assignment into a reassuring "no acknowledgment required" using the
    ``policyVersion.requiresAcknowledgment`` flag, which was observed to read
    ``False`` on archived / superseded version rows for genuinely-required,
    not-yet-acknowledged policies - that masked real non-compliance. The
    raw required flag and the version's current/superseded state are still shown
    so the reader has every datum.
    """
    policy = ap.get("policy") or {}
    version = ap.get("policyVersion") or {}
    name = policy.get("name") or "Untitled"
    policy_id = ap.get("policyId") or policy.get("id") or "?"
    accepted_at = ap.get("acceptedAt")

    status = f"ACCEPTED ({accepted_at})" if accepted_at else "NOT ACCEPTED"

    ver = version.get("version")
    ver_str = f"v{ver}.{version.get('subVersion', 0)}" if ver is not None else "N/A"
    current_str = "current" if version.get("current") else "superseded"

    required = version.get("requiresAcknowledgment")
    if required is True:
        required_str = "yes"
    elif required is False:
        required_str = "no (per policy version - verify via the policy's monitoring test)"
    else:
        required_str = "unknown"

    line = (
        f"[policy {policy_id}] {name} — {status} | "
        f"Version: {ver_str} ({current_str}) | "
        f"Acknowledgment required: {required_str}"
    )
    prior = ap.get("_priorVersionAcceptedAt")
    if not accepted_at and prior:
        line += f" | NOTE: a prior version was accepted on {prior}; re-acknowledgment of the current version is pending"
    return line


# ---------------------------------------------------------------------------
# Evidence Library artifact helpers
# ---------------------------------------------------------------------------

#: Artifact renewal schedule types accepted by the Evidence Library v2 API.
DRATA_RENEWAL_SCHEDULE_TYPES: frozenset[str] = frozenset(
    {
        "ONE_MONTH",
        "TWO_MONTHS",
        "THREE_MONTHS",
        "SIX_MONTHS",
        "ONE_YEAR",
        "CUSTOM",
        "NONE",
    }
)

#: File extensions Drata accepts as an Evidence Library artifact (v2 API).
DRATA_EVIDENCE_FILE_EXTENSIONS: frozenset[str] = frozenset(
    {
        "pdf",
        "docx",
        "odt",
        "doc",
        "xlsx",
        "ods",
        "pptx",
        "odp",
        "gif",
        "jpg",
        "jpeg",
        "png",
        "json",
        "csv",
        "md",
        "markdown",
        "txt",
        "html",
        "log",
        "zip",
        "msg",
        "mp4",
    }
)

#: The subset of accepted extensions that are plain text and can be uploaded
#: verbatim as UTF-8 from agent-authored ``file_content``. Everything else in
#: ``DRATA_EVIDENCE_FILE_EXTENSIONS`` is binary (``pdf`` / ``docx`` are rendered
#: server-side; the rest - images, Office/ODF docs, zip, msg, mp4 - must be
#: supplied pre-built via ``file_encoding='base64'``). Encoding agent text as
#: UTF-8 into a binary extension would upload a corrupt, unusable artifact.
DRATA_EVIDENCE_TEXT_EXTENSIONS: frozenset[str] = frozenset({"json", "csv", "md", "markdown", "txt", "html", "log"})

# ---------------------------------------------------------------------------
# User Document (personnel compliance evidence) helpers
# ---------------------------------------------------------------------------
# A User Document is manual evidence of a User/Personnel compliance check, filed
# against ``/users/{userId}/documents``. This is the ONLY write path for
# personnel offboarding evidence (``OFFBOARDING_EVIDENCE``) — a separate surface
# from the workspace Evidence Library, so an offboarding artifact must be filed
# here (not as an evidence-library item) to satisfy a leaver's OFFBOARDING
# personnel compliance check.

#: User Document types accepted by the v2 **Upload** endpoint
#: (``POST /users/{userId}/documents``). This *write* enum is deliberately
#: narrower than the list/filter enum below — Drata only lets the API file these
#: four manual-evidence types; the rest are device/agent-collected and synced
#: automatically.
DRATA_USER_DOCUMENT_UPLOAD_TYPES: frozenset[str] = frozenset(
    {"MFA_EVIDENCE", "SEC_TRAINING", "HIPAA_TRAINING_EVIDENCE", "OFFBOARDING_EVIDENCE"}
)

#: User Document types accepted by the v2 **List** ``type`` filter
#: (``GET /users/{userId}/documents``) — a superset of the upload enum that also
#: covers the device/agent-collected evidence Drata syncs automatically.
DRATA_USER_DOCUMENT_TYPES: frozenset[str] = frozenset(
    {
        "SEC_TRAINING",
        "PASSWORD_MANAGER_EVIDENCE",
        "AUTO_UPDATES_EVIDENCE",
        "HARD_DRIVE_ENCRYPTION_EVIDENCE",
        "ANTIVIRUS_EVIDENCE",
        "LOCK_SCREEN_EVIDENCE",
        "MFA_EVIDENCE",
        "HIPAA_TRAINING_EVIDENCE",
        "OFFBOARDING_EVIDENCE",
        "NIST_AI_TRAINING_EVIDENCE",
    }
)

#: File extensions Drata accepts for a User Document artifact (v2 Upload User
#: Document). A strict subset of ``DRATA_EVIDENCE_FILE_EXTENSIONS`` — no plain
#: text / markdown, zip, msg or mp4; ODF office formats are allowed. Agent
#: authored content therefore defaults to a rendered PDF, the universally
#: accepted offboarding-evidence format.
DRATA_USER_DOCUMENT_FILE_EXTENSIONS: frozenset[str] = frozenset(
    {
        "pdf",
        "docx",
        "odt",
        "doc",
        "xlsx",
        "ods",
        "pptx",
        "odp",
        "gif",
        "jpg",
        "jpeg",
        "png",
    }
)


def _evidence_artifact_meta(
    owner_id: int,
    filed_at: str,
    renewal_schedule_type: str,
    renewal_date: str,
    *,
    owner_required: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate + build the metadata Drata requires alongside an artifact.

    When an artifact source (``file`` / ``base64File`` / ``url`` / ``ticketUrl``)
    is attached to an Evidence Library Item, the v2 API requires ``ownerId``,
    ``filedAt`` and ``renewalScheduleType`` (``renewalDate`` only when the
    schedule is ``CUSTOM``). ``filedAt`` defaults to today and
    ``renewalScheduleType`` defaults to ``ONE_YEAR`` so the agent typically only
    has to supply an owner.

    Returns ``(meta, None)`` on success or ``(None, error_string)``.
    """
    meta: dict[str, Any] = {}
    if owner_id:
        meta["ownerId"] = int(owner_id)
    elif owner_required:
        return None, (
            "Error: Drata requires an evidence owner when attaching an artifact. "
            "Pass owner_id (a Drata user ID — find IDs via list_drata_users)."
        )

    meta["filedAt"] = (filed_at or "").strip() or datetime.now(UTC).date().isoformat()

    schedule = (renewal_schedule_type or "").strip().upper() or "ONE_YEAR"
    if schedule not in DRATA_RENEWAL_SCHEDULE_TYPES:
        return None, (
            f"Error: invalid renewal_schedule_type '{schedule}'. Use one of: "
            f"{', '.join(sorted(DRATA_RENEWAL_SCHEDULE_TYPES))}."
        )
    meta["renewalScheduleType"] = schedule
    if schedule == "CUSTOM":
        custom_date = (renewal_date or "").strip()
        if not custom_date:
            return None, ("Error: renewal_date (YYYY-MM-DD) is required when renewal_schedule_type is CUSTOM.")
        meta["renewalDate"] = custom_date
    return meta, None


def _evidence_write_summary(
    prefix: str,
    data: dict[str, Any] | None,
    *,
    workspace_id: int,
    fallback_id: Any,
) -> str:
    """Build a create/update success message that surfaces the new artifact version.

    The Evidence Library create/update responses already carry the authoritative
    post-write item body (including the ``versions`` array), so the newly filed
    artifact's version id can be reported immediately — a deterministic
    read-your-write confirmation that needs no re-read and is immune to any
    upstream read caching. When a new version is present the agent is pointed at
    ``get_drata_evidence_version`` to verify the stored file byte-for-byte (it
    returns the file name + a download URL).
    """
    data = data or {}
    ev_id = data.get("id")
    if ev_id is None:
        ev_id = fallback_id
    ev_str = ev_id if ev_id is not None else "?"
    parts = [f"{prefix} (ID: {ev_str})."]
    versions = data.get("versions") or []
    if versions:
        current = next((v for v in versions if v.get("current")), versions[-1])
        vid = current.get("id")
        if vid is not None:
            parts.append(
                f"Current artifact version_id={vid} ({len(versions)} total). "
                f"Verify the stored file with get_drata_evidence_version("
                f"workspace_id={workspace_id}, evidence_id={ev_str}, "
                f"version_id={vid})."
            )
    return " ".join(parts)


async def _render_evidence_file(
    file_content: str,
    file_name: str,
    encoding: str,
    *,
    fallback_name: str,
    accepted_extensions: frozenset[str] = DRATA_EVIDENCE_FILE_EXTENSIONS,
) -> tuple[bytes, str, str] | str:
    """Resolve agent-supplied content into ``(bytes, file_name, mime_type)``.

    LLMs cannot reliably emit valid PDF / DOCX binary inline, so the agent
    authors the *content* in Markdown / HTML and we convert it server-side —
    the same pipeline used by the ``generate_file`` builtin and OpenCTI's
    ``upload_opencti_file``:

    * ``.pdf``  → rendered to a real PDF via the shared Playwright renderer
      (unless ``encoding='base64'``).
    * ``.docx`` → rendered to a real Word document via python-docx (unless
      ``encoding='base64'``).
    * ``encoding='base64'`` → decoded as pre-built binary, no rendering.
    * text formats (``.md`` / ``.txt`` / ``.html`` / ``.csv`` / ``.json`` /
      ``.log``) → encoded verbatim as UTF-8.
    * any other (binary) accepted extension (images, Office/ODF docs, zip,
      msg, mp4) → rejected with a friendly error unless supplied as base64,
      since encoding agent text as UTF-8 there would upload a corrupt artifact.

    Returns the tuple on success, or an error string suitable for the agent.
    """
    encoding = (encoding or "text").strip().lower()
    explicit_name = (file_name or "").strip()
    if explicit_name:
        # Only an explicitly-supplied file_name is parsed for an extension.
        name = explicit_name
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    else:
        # No file_name: the evidence name is only the *base* filename and must
        # NOT be parsed for an extension — a name like "SOC2 v1.2 recap" would
        # otherwise yield a bogus ".2 recap" ext and be rejected. Default to PDF.
        # A blank/whitespace fallback must not collapse to a bare ".pdf" name.
        name = (fallback_name or "").strip() or "evidence"
        ext = ""
    if not ext:
        # No extension — default the artifact to a PDF, the most common
        # evidence format and what an agent means by "attach the recap".
        name = f"{name}.pdf"
        ext = "pdf"
    if ext not in accepted_extensions:
        accepted = ", ".join("." + e for e in sorted(accepted_extensions))
        return f"Error: '.{ext}' is not an accepted Drata evidence file type. Accepted: {accepted}."

    title = name.rsplit(".", 1)[0] or name
    # A render-failure fallback may suggest uploading the raw text file instead,
    # but only for the text extensions THIS surface accepts (User Documents
    # accept none, so the hint is dropped there - mirroring the binary branch).
    _accepted_text = sorted(accepted_extensions & DRATA_EVIDENCE_TEXT_EXTENSIONS)
    _text_fallback = (
        " or attach the content as a text file (" + " / ".join("." + e for e in _accepted_text) + ")"
        if _accepted_text
        else ""
    )
    if encoding == "base64":
        # base64 payloads are routinely whitespace-wrapped (newlines every 76
        # chars, copy/paste indentation), which validate=True would otherwise
        # reject. Strip ASCII whitespace first so formatting is tolerated while
        # genuinely invalid (non-alphabet) characters still raise instead of
        # being silently dropped and a corrupt file uploaded.
        cleaned = "".join((file_content or "").split())
        try:
            file_bytes = base64.b64decode(cleaned, validate=True)
        except Exception:
            return "Invalid base64 content — could not decode."
    elif ext == "pdf":
        from app.services.pdf_render import render_text_to_pdf

        try:
            file_bytes = await render_text_to_pdf(file_content, title=title)
        except Exception:
            logger.warning("Drata evidence: PDF rendering failed for %s", name, exc_info=True)
            return (
                "PDF rendering failed — the headless browser used for server-side "
                "PDF generation could not render the supplied content. Retry with "
                "file_encoding='base64' and pre-rendered PDF bytes" + _text_fallback + "."
            )
    elif ext == "docx":
        from app.services.docx_render import render_text_to_docx

        try:
            file_bytes = await render_text_to_docx(file_content, title=title)
        except Exception:
            logger.warning("Drata evidence: DOCX rendering failed for %s", name, exc_info=True)
            return (
                "DOCX rendering failed — the Word document renderer could not "
                "process the supplied content. Retry with file_encoding='base64' "
                "and pre-built DOCX bytes" + _text_fallback + "."
            )
    elif ext in DRATA_EVIDENCE_TEXT_EXTENSIONS:
        file_bytes = file_content.encode("utf-8")
    else:
        # A binary format (image, Office/ODF doc, zip, msg, mp4): encoding the
        # agent's text as UTF-8 would upload corrupt bytes. The agent can't emit
        # valid binary inline, so require pre-built base64 instead. Only suggest
        # text formats that this surface actually accepts (User Documents take
        # no text/markdown, so the hint is dropped there).
        accepted_text = sorted(accepted_extensions & DRATA_EVIDENCE_TEXT_EXTENSIONS)
        text_hint = " or a text format (" + " / ".join("." + e for e in accepted_text) + ")" if accepted_text else ""
        return (
            f"Error: '.{ext}' is a binary file type, so it cannot be authored as "
            "text. Provide the file as base64 with file_encoding='base64', or use "
            f"a PDF/DOCX (rendered from your content){text_hint}."
        )

    from app.services.file_storage import MAX_UPLOAD_SIZE_BYTES, guess_content_type

    if len(file_bytes) > MAX_UPLOAD_SIZE_BYTES:
        return f"File too large ({len(file_bytes):,} bytes). Maximum is {MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)} MB."
    return file_bytes, name, guess_content_type(name)


async def _resolve_uploaded_evidence_file(
    file_context: FileContext | None,
    source_file_id: str,
    override_name: str,
    *,
    accepted_extensions: frozenset[str] = DRATA_EVIDENCE_FILE_EXTENSIONS,
) -> tuple[bytes, str, str] | str:
    """Resolve a user-uploaded file into ``(bytes, file_name, mime_type)``.

    Lets an agent file a Drata evidence artifact from a file the user already
    attached to the conversation (or the assignment trigger) — uploaded
    **byte-for-byte**, with no re-authoring or re-encoding — instead of only
    being able to render agent-written Markdown / HTML. Reuses the same
    ``FileContext`` byte-loading path the code interpreter uses.

    ``override_name`` (the tool's ``file_name`` argument) wins when supplied so
    the agent can rename the artifact; otherwise the uploaded file's own name is
    used. The extension is validated against Drata's accepted evidence types.

    Returns the tuple on success, or a friendly error string for the agent.
    """
    from app.services.file_storage import MAX_UPLOAD_SIZE_BYTES, guess_content_type

    if file_context is None:
        return "Error: no uploaded files are available in this conversation to reference via source_file_id."
    try:
        resolved = await file_context.resolve_upload(source_file_id)
    except Exception:
        logger.warning(
            "Drata evidence: failed to read uploaded file %s",
            source_file_id,
            exc_info=True,
        )
        return (
            "Error: the referenced uploaded file could not be read from storage "
            "(it may have expired). Re-upload it and try again."
        )
    if resolved is None:
        return (
            f"Error: '{source_file_id}' is not an available uploaded file. Use a file_id from the Available Files list."
        )
    file_bytes, original_name, content_type = resolved

    # The agent may rename the artifact via file_name; otherwise keep the
    # uploaded file's own name. If the override dropped the extension, restore
    # it from the original name so the artifact keeps a valid Drata file type.
    name = (override_name or "").strip() or (original_name or "").strip()
    if not name:
        return "Error: could not determine a file name for the uploaded artifact."
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if not ext:
        orig_ext = original_name.rsplit(".", 1)[-1].lower() if original_name and "." in original_name else ""
        if orig_ext:
            name = f"{name}.{orig_ext}"
            ext = orig_ext
    if ext not in accepted_extensions:
        accepted = ", ".join("." + e for e in sorted(accepted_extensions))
        # Named by its id: the extension is cut from a name a third party may
        # have chosen, which reaches the model only in the block.
        return uploaded_file_answer(
            f"Error: file '{source_file_id}' is not an accepted Drata evidence file type "
            f"(its name is below). Accepted: {accepted}.",
            target=_PROVIDER,
            filename=name,
        )
    if len(file_bytes) > MAX_UPLOAD_SIZE_BYTES:
        return f"File too large ({len(file_bytes):,} bytes). Maximum is {MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)} MB."
    # Fall back to guessing from the (validated) extension when the uploaded
    # content type is missing or the generic octet-stream, so Drata receives a
    # meaningful MIME type — parity with the render path's guess_content_type.
    base_ct = (content_type or "").split(";", 1)[0].strip().lower()
    if not base_ct or base_ct == "application/octet-stream":
        content_type = guess_content_type(name)
    return file_bytes, name, content_type


#: Drata caps a single page at 500 results. The list tools fetch up to this
#: many records before reporting truncation so compliance reports don't
#: silently omit people / policies (the previous 200 cap could under-count a
#: roster without any warning).
_DRATA_MAX_PAGE_SIZE = 500
_DEFAULT_MAX_ITEMS = 500

#: Drata personnel compliance-check statuses (ComplianceCheckStatusEnum). A
#: check counts as a problem only when it FAILs or is MISCONFIGURED — ``PASS``
#: and ``EXCLUDED`` are healthy. (The old code compared against ``"PASSING"``,
#: which is not a valid Drata status, so every passing check was wrongly
#: counted as a compliance issue.)
_DRATA_NONCOMPLIANT_CHECK_STATUSES: frozenset[str] = frozenset({"FAIL", "MISCONFIGURED"})


async def _paginated_fetch_all(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    *,
    max_items: int = _DEFAULT_MAX_ITEMS,
    page_size: int = 100,
    action: str = "fetch page",
    verbatim_errors: bool = False,
) -> tuple[list[dict], str | None, bool]:
    """Fetch pages using Drata's cursor-based pagination.

    Returns ``(items, error, truncated)`` where *error* is a human-readable
    string on failure (``None`` on success) and *truncated* is ``True`` when
    the ``max_items`` cap was reached while Drata still had more pages - so
    callers can warn that the result set is incomplete instead of silently
    dropping records.

    ``action`` names the operation in the error string. With
    ``verbatim_errors=True`` a failed page goes through ``_request_verbatim``,
    so the error carries Drata's status code and response body verbatim
    instead of the generic ``format_http_error`` wording.
    """
    params = dict(params or {})
    items: list[dict] = []
    cursor: str | None = None
    truncated = False

    while True:
        # Size each page from the REMAINING budget, not a fixed page size: with
        # a max_items that isn't a multiple of page_size the final page would
        # otherwise over-fetch, and ``items[:max_items]`` would silently drop
        # the overshoot - possibly with ``truncated=False`` when that page also
        # had no cursor. Capping to the remaining budget makes the last fetched
        # record exactly ``max_items``, so Drata returns a cursor iff more data
        # genuinely exists and the truncation flag stays reliable.
        params["size"] = max(1, min(page_size, max_items - len(items), _DRATA_MAX_PAGE_SIZE))
        if cursor:
            params["cursor"] = cursor
        if verbatim_errors:
            resp, err = await _request_verbatim(client, "get", url, action, headers=headers, params=params)
        else:
            resp, err = await safe_request(
                client,
                "get",
                url,
                _PROVIDER,
                action,
                headers=headers,
                params=params,
            )
        if err:
            return [], err, False
        data = resp.json()
        items.extend(data.get("data", []))

        cursor = (data.get("pagination") or {}).get("cursor")
        if not cursor:
            break
        if len(items) >= max_items:
            truncated = True
            break

    return items[:max_items], None, truncated


async def _paginated_fetch(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    *,
    max_items: int = _DEFAULT_MAX_ITEMS,
    page_size: int = 100,
) -> tuple[list[dict], str | None]:
    """Backwards-compatible wrapper around :func:`_paginated_fetch_all`.

    Returns ``(items, error)`` for the many callers that don't need the
    truncation flag.
    """
    items, err, _ = await _paginated_fetch_all(client, url, headers, params, max_items=max_items, page_size=page_size)
    return items, err


#: Drata v1 caps a list ``limit`` at 50 (unlike v2's cursor ``size``).
_DRATA_V1_MAX_PAGE_SIZE = 50


async def _paginated_fetch_pages_all(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    *,
    max_items: int = _DEFAULT_MAX_ITEMS,
    page_size: int = _DRATA_V1_MAX_PAGE_SIZE,
    action: str = "fetch page",
    verbatim_errors: bool = False,
    required_permission: str = "",
) -> tuple[list[dict], str | None, bool]:
    """Fetch pages using Drata's **v1** ``page`` / ``limit`` pagination.

    The v1 API (control external evidence, user identities) paginates with
    ``page`` / ``limit`` and a top-level ``total`` rather than v2's
    ``pagination.cursor`` scheme. Returns ``(items, error, truncated)``; stops
    at ``max_items`` or the last page, and *truncated* is ``True`` when the cap
    was reached while Drata still reported more records.

    With ``verbatim_errors=True`` a failed page goes through
    ``_request_verbatim`` (status code + body verbatim, and the
    ``required_permission`` named on a 403) instead of the generic wording.
    """
    params = dict(params or {})
    items: list[dict] = []
    page = 1
    page_size = max(1, min(page_size, _DRATA_V1_MAX_PAGE_SIZE))
    truncated = False
    while True:
        params["page"] = page
        params["limit"] = page_size
        if verbatim_errors:
            resp, err = await _request_verbatim(
                client,
                "get",
                url,
                action,
                required_permission=required_permission,
                headers=headers,
                params=params,
            )
        else:
            resp, err = await safe_request(client, "get", url, _PROVIDER, action, headers=headers, params=params)
        if err:
            return [], err, False
        parsed = resp.json()
        data = parsed if isinstance(parsed, dict) else {}
        batch = data.get("data") or []
        items.extend(batch)
        total = data.get("total")
        if len(batch) < page_size:
            break
        if isinstance(total, int) and len(items) >= total:
            break
        if len(items) >= max_items:
            truncated = not isinstance(total, int) or total > max_items
            break
        page += 1
    if len(items) > max_items:
        truncated = True
    return items[:max_items], None, truncated


async def _paginated_fetch_pages(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    *,
    max_items: int = _DEFAULT_MAX_ITEMS,
    page_size: int = _DRATA_V1_MAX_PAGE_SIZE,
) -> tuple[list[dict], str | None]:
    """Backwards-compatible wrapper around :func:`_paginated_fetch_pages_all`.

    Returns ``(items, error)`` for the callers that don't need the truncation
    flag.
    """
    items, err, _ = await _paginated_fetch_pages_all(
        client, url, headers, params, max_items=max_items, page_size=page_size
    )
    return items, err


# ---------------------------------------------------------------------------
# Drata tools
# ---------------------------------------------------------------------------


_empty: dict[str, Any] = {"type": "object", "properties": {}, "required": []}


def _ws_only(default_ws: int = 1) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "workspace_id": {
                "type": "integer",
                "description": "Workspace ID",
                "default": default_ws,
            }
        },
        "required": [],
    }


def to_v1_base_url(base_url: str) -> str:
    """Return the Drata **v1** API base URL for a configured v2 base URL.

    A handful of control *external-evidence* endpoints exist only on Drata's
    legacy **v1** API. The critical gotcha: v1 is served at the UNVERSIONED
    ``/public`` base - NOT ``/public/v1`` (which returns a 404 "Cannot GET
    /public/v1/..."). v2 simply appends ``/v2`` to that same base, so the v1
    base is obtained by DROPPING the trailing ``/v2`` segment, not replacing it.
    Region/host are preserved.

    Example: ``https://public-api.eu.drata.com/public/v2`` ->
             ``https://public-api.eu.drata.com/public``.

    Confirmed against Drata's own published API samples, which call v1 endpoints
    like ``https://public-api.drata.com/public/personnel`` and
    ``.../public/users/{id}/documents`` (no version segment).
    """
    stripped = base_url.rstrip("/")
    if stripped.endswith("/v2"):
        return stripped[:-3].rstrip("/")
    return stripped


def resolve_base_url_v1(region: str | None = None) -> str:
    """Return the Drata **v1** API base URL for a region key."""
    return to_v1_base_url(resolve_base_url(region))
