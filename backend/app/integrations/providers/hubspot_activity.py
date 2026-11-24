# SPDX-License-Identifier: Apache-2.0

"""The HubSpot activity tools: engagements, the record timeline, forms and
their submissions, property definitions and labels, batch reads, teams, sequences
and the inbound engagement report.

``hubspot.build_tools`` builds one :class:`HubSpotActivity` per toolbox around its
rate-limited request function. The record tools of ``hubspot.py`` share its
lookups - owners, property definitions, pipelines, forms - each fetched once for
the life of the toolbox (one run, one chat turn), so labelling a hundred records
costs the same few calls as labelling one.

What the tools return is read by ``hubspot_records``: an engagement always comes
with its time, type, direction, owner, subject and body, never as a bare id.
"""

from __future__ import annotations

import re
import time
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.http import format_pagination_hint
from app.integrations.providers.hubspot_records import (
    ENGAGEMENT_SPECS,
    ENGAGEMENT_TYPES,
    REPORT_GROUPS,
    Engagement,
    FormSubmission,
    InboundContact,
    TimelineEvent,
    aggregate_inbound,
    assess_inbound,
    associations_of,
    epoch_ms,
    format_engagement,
    format_group,
    format_outcome,
    format_submission,
    is_uuid,
    iso_utc,
    merge_timeline,
    normalize_engagement,
    one_line,
    parse_hubspot_time,
    parse_time_bound,
    property_changes,
    record_properties,
    split_ids,
    split_names,
    submission_from_form,
    submissions_from_history,
    submissions_from_profile,
    within,
)
from app.integrations.providers.hubspot_records import (
    engagement_type as as_engagement_type,
)
from app.integrations.providers.hubspot_scope import api_object_type, canonical_object_type
from app.tools.base import ToolDefinition, ToolResult, is_auth_or_permission_error

_BASE_URL = "https://api.hubapi.com"

#: The rate-limited request of ``hubspot.build_tools`` (``safe_request``'s contract).
Request = Callable[..., Awaitable[tuple[httpx.Response | None, str | None]]]

#: Records per CRM search page (HubSpot's maximum) and per search tool call.
SEARCH_PAGE = 200
SEARCH_MAX_RESULTS = 500
#: HubSpot's CRM search answers at most this many records per query.
SEARCH_OFFSET_CEILING = 10_000
#: Inputs per batch request (HubSpot's batch read limit).
BATCH_SIZE = 100
_BATCH_MAX_IDS = 500
_ASSOCIATION_MAX_IDS = 1000
_ASSOCIATED_ENGAGEMENTS_SHOWN = 20
_TIMELINE_MAX_PER_TYPE = 500
_FORM_PAGE = 50
_FORM_SCAN_PAGES = 10
_FORMS_MAX_PAGES = 20
_FORM_TYPES = ("hubspot", "captured", "flow")
_OWNER_PAGES = 10
_SEQUENCE_USERS_MAX = 50
#: Seconds between a contact's recorded conversion and the form submission it is.
_SUBMISSION_MATCH_SECONDS = 600
#: The inbound report stops fetching after this long (the tool timeout is 300 s).
_REPORT_SECONDS = 240.0
_REPORT_MAX_CONTACTS = 2000
_REPORT_MAX_ENGAGEMENTS = 8000
_REPORT_MAX_LISTED = 300

#: Properties whose values are pipeline or stage ids when the options do not name them.
_STAGE_PROPERTIES = frozenset({"dealstage", "pipeline", "hs_pipeline", "hs_pipeline_stage", "lifecyclestage"})

#: The property changes a timeline shows, per object type.
_HISTORY_PROPERTIES = {
    "contacts": ("lifecyclestage", "hubspot_owner_id", "hs_lead_status"),
    "companies": ("lifecyclestage", "hubspot_owner_id"),
    "deals": ("dealstage", "pipeline", "hubspot_owner_id"),
    "tickets": ("hs_pipeline_stage", "hs_pipeline", "hubspot_owner_id"),
}
_SEQUENCE_HISTORY = ("hs_latest_sequence_enrolled", "hs_latest_sequence_unenrolled_date", "hs_latest_sequence_finished_date")
_SEQUENCE_PROPERTIES = (
    "email",
    "hs_sequences_is_enrolled",
    "hs_sequences_enrolled_count",
    "hs_sequences_actively_enrolled_count",
    "hs_latest_sequence_enrolled",
    "hs_latest_sequence_enrolled_date",
    "hs_latest_sequence_unenrolled_date",
    "hs_latest_sequence_finished_date",
    "hs_sales_email_last_replied",
    "hs_sales_email_last_opened",
)
#: What a timeline reads besides the engagements, and what each spelling names.
_TIMELINE_SOURCES = {
    "form_submissions": "form_submissions",
    "form_submission": "form_submissions",
    "forms": "form_submissions",
    "form": "form_submissions",
    "sequences": "sequences",
    "sequence": "sequences",
    "property_changes": "property_changes",
    "property_change": "property_changes",
    "changes": "property_changes",
    "properties": "property_changes",
}
#: Pseudo-property of a CRM search filtering on the records an object is associated with.
_ASSOCIATION_FILTER_TYPES = {"contacts": "contact", "companies": "company", "deals": "deal", "tickets": "ticket"}

_REPORT_CONTACT_PROPERTIES = (
    "email",
    "firstname",
    "lastname",
    "company",
    "country",
    "ip_country",
    "hubspot_owner_id",
    "hubspot_owner_assigneddate",
    "createdate",
    "lifecyclestage",
    "hs_lead_status",
    "first_conversion_event_name",
    "first_conversion_date",
    "recent_conversion_event_name",
    "recent_conversion_date",
    "hs_latest_sequence_enrolled_date",
    "hs_sales_email_last_replied",
)
#: The few properties the report reads of each engagement (no bodies).
_REPORT_ENGAGEMENT_PROPERTIES = {
    "emails": ["hs_timestamp", "hs_createdate", "hubspot_owner_id", "hs_email_direction", "hs_email_status", "hs_sequence_id"],
    "calls": ["hs_timestamp", "hs_createdate", "hubspot_owner_id", "hs_call_direction", "hs_call_status"],
    "meetings": ["hs_timestamp", "hs_createdate", "hubspot_owner_id", "hs_meeting_outcome", "hs_meeting_source"],
    "communications": ["hs_timestamp", "hs_createdate", "hubspot_owner_id", "hs_communication_channel_type"],
}
_REPORT_METHOD = (
    "How this is read: a touch is an outgoing email (manual, or sent by a sequence), a call, a meeting or a logged "
    "LinkedIn / SMS / WhatsApp message on the contact after its submission (a meeting counts when it was booked); the "
    "first-touch delay runs from the submission to the first touch. Replied: an incoming email after the submission, "
    "or the contact's last sales email reply after it. Meeting held: a meeting with the outcome COMPLETED. Deal "
    "created: an associated deal created after the submission. Times are UTC."
)

#: The scopes of each engagement type, as HubSpot names them in a 403.
ENGAGEMENT_SCOPES = {
    "emails": ("sales-email-read", "crm.objects.emails.read"),
    "notes": ("crm.objects.notes.read",),
    "calls": ("crm.objects.calls.read",),
    "meetings": ("crm.objects.meetings.read",),
    "tasks": ("crm.objects.tasks.read",),
    "communications": ("crm.objects.contacts.read",),
}
_ALL_ENGAGEMENT_SCOPES = tuple(dict.fromkeys(s for scopes in ENGAGEMENT_SCOPES.values() for s in scopes))
_RECORD_READ_SCOPES = ("crm.objects.contacts.read", "crm.objects.companies.read", "crm.objects.deals.read", "tickets")
_RECORD_WRITE_SCOPES = (
    "crm.objects.contacts.write",
    "crm.objects.companies.write",
    "crm.objects.deals.write",
    "tickets",
)
_SCHEMA_SCOPES = ("crm.schemas.contacts.read", "crm.schemas.companies.read", "crm.schemas.deals.read")

#: The private-app scopes each HubSpot tool may need: its description states them
#: and a 403 names them when HubSpot does not.
TOOL_SCOPES: dict[str, tuple[str, ...]] = {
    "get_hubspot_contact": ("crm.objects.contacts.read", "crm.objects.owners.read", "crm.schemas.contacts.read"),
    "search_hubspot_contacts": ("crm.objects.contacts.read", "crm.objects.owners.read", "crm.schemas.contacts.read"),
    "create_hubspot_contact": ("crm.objects.contacts.write",),
    "update_hubspot_contact": ("crm.objects.contacts.write",),
    "get_hubspot_company": ("crm.objects.companies.read", "crm.objects.owners.read", "crm.schemas.companies.read"),
    "search_hubspot_companies": ("crm.objects.companies.read", "crm.objects.owners.read", "crm.schemas.companies.read"),
    "create_hubspot_company": ("crm.objects.companies.write",),
    "update_hubspot_company": ("crm.objects.companies.write",),
    "list_hubspot_deals": ("crm.objects.deals.read",),
    "get_hubspot_deal": ("crm.objects.deals.read", "crm.objects.owners.read", "crm.schemas.deals.read"),
    "search_hubspot_deals": ("crm.objects.deals.read", "crm.objects.owners.read", "crm.schemas.deals.read"),
    "create_hubspot_deal": ("crm.objects.deals.write",),
    "update_hubspot_deal": ("crm.objects.deals.write",),
    "list_hubspot_campaigns": ("content",),
    "get_hubspot_campaign": ("content",),
    "list_hubspot_pipelines": ("crm.objects.deals.read", "tickets"),
    "list_hubspot_owners": ("crm.objects.owners.read",),
    "search_hubspot_owners": ("crm.objects.owners.read",),
    "get_hubspot_owner": ("crm.objects.owners.read",),
    "get_hubspot_associations": (*_RECORD_READ_SCOPES, *_ALL_ENGAGEMENT_SCOPES),
    "create_hubspot_association": ("crm.objects.contacts.write", "crm.objects.companies.write", "crm.objects.deals.write", "tickets"),
    "delete_hubspot_association": ("crm.objects.contacts.write", "crm.objects.companies.write", "crm.objects.deals.write", "tickets"),
    "delete_hubspot_record": ("crm.objects.contacts.write", "crm.objects.companies.write", "crm.objects.deals.write", "tickets"),
    "list_hubspot_line_items": ("crm.objects.deals.read", "crm.objects.line_items.read"),
    "create_hubspot_line_item": ("crm.objects.line_items.write", "crm.objects.deals.write"),
    "update_hubspot_line_item": ("crm.objects.line_items.write",),
    "delete_hubspot_line_item": ("crm.objects.line_items.write",),
    "search_hubspot_tickets": ("tickets",),
    "get_hubspot_ticket": ("tickets", "crm.objects.owners.read"),
    "create_hubspot_ticket": ("tickets",),
    "update_hubspot_ticket": ("tickets",),
    "search_hubspot_files": ("files",),
    "list_hubspot_attachments": ("files", "files.ui_hidden.read", *_ALL_ENGAGEMENT_SCOPES),
    "download_hubspot_file": ("files", "files.ui_hidden.read", *_ALL_ENGAGEMENT_SCOPES),
    "upload_hubspot_file": ("files",),
    "upload_hubspot_attachment": ("files", "crm.objects.contacts.write", "crm.objects.companies.write", "crm.objects.deals.write", "tickets"),
    "create_hubspot_note": ("crm.objects.notes.write", *_RECORD_WRITE_SCOPES),
    "create_hubspot_task": ("crm.objects.tasks.write", *_RECORD_WRITE_SCOPES),
    "get_hubspot_engagement": (*_ALL_ENGAGEMENT_SCOPES, "crm.objects.owners.read"),
    "search_hubspot_engagements": (*_ALL_ENGAGEMENT_SCOPES, "crm.objects.owners.read"),
    "get_hubspot_record_timeline": (
        *_RECORD_READ_SCOPES,
        *_ALL_ENGAGEMENT_SCOPES,
        "forms",
        "automation.sequences.read",
        "crm.objects.owners.read",
    ),
    "list_hubspot_forms": ("forms",),
    "list_hubspot_form_submissions": ("forms", "crm.objects.contacts.read"),
    "get_hubspot_contact_form_submissions": ("crm.objects.contacts.read", "forms"),
    "list_hubspot_properties": _SCHEMA_SCOPES,
    "batch_get_hubspot_records": (*_RECORD_READ_SCOPES, *_ALL_ENGAGEMENT_SCOPES, "crm.objects.owners.read"),
    "batch_get_hubspot_associations": (*_RECORD_READ_SCOPES, *_ALL_ENGAGEMENT_SCOPES),
    "list_hubspot_teams": ("settings.users.teams.read", "crm.objects.owners.read"),
    "list_hubspot_sequences": ("automation.sequences.read", "crm.objects.owners.read"),
    "get_hubspot_sequence_enrollments": ("automation.sequences.read", "crm.objects.contacts.read", "sales-email-read"),
    "get_hubspot_inbound_engagement_report": (
        "forms",
        "crm.objects.contacts.read",
        "crm.objects.deals.read",
        "crm.objects.owners.read",
        *_ALL_ENGAGEMENT_SCOPES,
    ),
}
#: Tools that take an object type: the scopes they need are those of the types they read.
PER_OBJECT_TYPE_SCOPES = frozenset(
    {
        "get_hubspot_associations",
        "create_hubspot_association",
        "delete_hubspot_association",
        "delete_hubspot_record",
        "list_hubspot_pipelines",
        "list_hubspot_attachments",
        "download_hubspot_file",
        "upload_hubspot_attachment",
        "create_hubspot_note",
        "create_hubspot_task",
        "get_hubspot_engagement",
        "search_hubspot_engagements",
        "get_hubspot_record_timeline",
        "list_hubspot_properties",
        "batch_get_hubspot_records",
        "batch_get_hubspot_associations",
    }
)


def scopes_sentence(tool: str) -> str:
    """The sentence of *tool*'s description that names the scopes it needs."""
    scopes = TOOL_SCOPES.get(tool)
    if not scopes:
        return ""
    qualifier = " (those of the object types it reads)" if tool in PER_OBJECT_TYPE_SCOPES else ""
    return f" HubSpot private app scopes it uses{qualifier}: {', '.join(dict.fromkeys(scopes))}."


def _refuse(text: str) -> ToolResult:
    return ToolResult(tool_call_id="", content=text, is_error=True)


def _structured(text: str, records: list[dict[str, Any]]) -> ToolResult:
    """A search answer whose records are its structured value, so a gate check can test it (``[]`` = none)."""
    structured = [
        {"id": r.get("id"), "properties": dict(r.get("properties") or {})} for r in records if isinstance(r, dict)
    ]
    return ToolResult(tool_call_id="", content=text, structured=structured)


def _first_line(err: str) -> str:
    return (err or "").strip().splitlines()[0] if (err or "").strip() else "unknown error"


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _body(resp: httpx.Response | None) -> dict[str, Any]:
    if resp is None:
        return {}
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _next_after(data: Mapping[str, Any]) -> str:
    paging = data.get("paging")
    nxt = paging.get("next") if isinstance(paging, Mapping) else None
    return str(nxt.get("after") or "") if isinstance(nxt, Mapping) else ""


def search_total(results: Sequence[Any], total: int) -> int:
    """The match count a search header shows: never below what it lists (a missing ``total``)."""
    return max(total, len(results))


def search_shown_note(results: Sequence[Any], total: int, after: str) -> str:
    """``\\nShowing N of TOTAL ...`` when a search lists fewer records than match, else ``""``."""
    shown, total = len(results), search_total(results, total)
    if not shown or shown >= total:
        return ""
    if after:
        return f"\nShowing {shown} of {total}: pass the after cursor below for the next page."
    return f"\nShowing {shown} of {total}: HubSpot gave no next page; narrow the filters to read the rest."


def _results(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [r for r in data.get("results") or [] if isinstance(r, dict)]


def _chunks(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _is_owner_property(name: str, definition: Mapping[str, Any] | None) -> bool:
    return name == "hubspot_owner_id" or (definition or {}).get("referencedObjectType") == "OWNER"


def _options(definition: Mapping[str, Any] | None) -> dict[str, str]:
    return {
        str(o.get("value")): str(o.get("label") or o.get("value"))
        for o in (definition or {}).get("options") or []
        if isinstance(o, Mapping) and o.get("value") is not None
    }


def _bounds(from_date: Any, to_date: Any) -> tuple[datetime | None, datetime | None, str]:
    """``(start, end, error)`` of a ``from_date`` / ``to_date`` pair."""
    start = parse_time_bound(from_date)
    end = parse_time_bound(to_date, end=True)
    for name, raw, parsed in (("from_date", from_date, start), ("to_date", to_date, end)):
        if parsed is None and str(raw or "").strip():
            return None, None, (
                f"Error: {name} '{raw}' is not a time: pass a date (YYYY-MM-DD, a whole UTC day), an ISO-8601 "
                "time (2026-10-28T09:00:00+02:00) or epoch milliseconds."
            )
    if start and end and start > end:
        return None, None, "Error: from_date is after to_date."
    return start, end, ""


def _range_text(start: datetime | None, end: datetime | None) -> str:
    if start and end:
        return f" from {iso_utc(start)} to {iso_utc(end)}"
    if start:
        return f" since {iso_utc(start)}"
    if end:
        return f" until {iso_utc(end)}"
    return ""


class HubSpotActivity:
    """The activity tools and the lookups the HubSpot tools share, for one toolbox."""

    def __init__(
        self,
        request: Request,
        *,
        headers: Mapping[str, str],
        search_body: Callable[..., dict[str, Any]],
        default_properties: Mapping[str, str],
        object_type_scope: frozenset[str] | None,
    ) -> None:
        self._request = request
        self._headers = dict(headers)
        self._search_body = search_body
        self._default_properties = dict(default_properties)
        self._scope = object_type_scope
        self._owners: dict[str, dict[str, Any]] | None = None
        self._owners_error = ""
        self._property_defs: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self._unknown_properties: dict[str, set[str]] = defaultdict(set)
        self._definitions_error: dict[str, str] = {}
        self._all_properties: dict[str, list[dict[str, Any]]] = {}
        self._pipelines: dict[str, dict[str, str]] = {}
        self._forms: list[dict[str, Any]] | None = None

    # ── Requests ────────────────────────────────────────────────────────

    async def _get(
        self, client: httpx.AsyncClient, path: str, action: str, params: Mapping[str, Any] | None = None
    ) -> tuple[dict[str, Any], str | None]:
        resp, err = await self._request(
            client, "get", f"{_BASE_URL}{path}", "HubSpot", action, headers=self._headers, params=dict(params or {})
        )
        return ({}, err) if err else (_body(resp), None)

    async def _post(
        self, client: httpx.AsyncClient, path: str, action: str, body: Mapping[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        resp, err = await self._request(
            client, "post", f"{_BASE_URL}{path}", "HubSpot", action, headers=self._headers, json=dict(body)
        )
        return ({}, err) if err else (_body(resp), None)

    # ── Owners ──────────────────────────────────────────────────────────

    async def owners(self, client: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
        """Every owner, active and deactivated, by owner id (fetched once)."""
        if self._owners is not None:
            return self._owners
        owners: dict[str, dict[str, Any]] = {}
        for archived in ("false", "true"):
            after = ""
            for _ in range(_OWNER_PAGES):
                params = {"limit": "100", "archived": archived}
                if after:
                    params["after"] = after
                data, err = await self._get(client, "/crm/v3/owners", "list owners", params)
                if err:
                    if archived == "false":
                        self._owners_error = err
                    break
                for owner in _results(data):
                    if owner.get("id") is not None:
                        owners.setdefault(str(owner["id"]), {**owner, "archived": archived == "true"})
                after = _next_after(data)
                if not after:
                    break
            if self._owners_error:
                break
        self._owners = owners
        return owners

    def owner_label(self, owner_id: Any) -> str:
        """``Jane Doe <jane@acme.com>``, or ``owner <id>`` when the owner is unknown."""
        owner = (self._owners or {}).get(str(owner_id))
        if not owner:
            return f"owner {owner_id}"
        name = f"{owner.get('firstName') or ''} {owner.get('lastName') or ''}".strip()
        email = str(owner.get("email") or "")
        label = f"{name} <{email}>" if name and email else (name or email or f"owner {owner_id}")
        return label + (" (deactivated)" if owner.get("archived") else "")

    def owner_teams(self, owner_id: Any) -> list[tuple[str, str, bool]]:
        """``[(team id, team name, primary)]`` of an owner, its primary team first."""
        owner = (self._owners or {}).get(str(owner_id)) or {}
        teams = [
            (str(t.get("id") or ""), str(t.get("name") or ""), bool(t.get("primary")))
            for t in owner.get("teams") or []
            if isinstance(t, Mapping)
        ]
        return sorted(teams, key=lambda t: not t[2])

    def user_label(self, user_id: Any) -> str:
        """The owner a HubSpot *user* id belongs to (property history names users, not owners)."""
        for owner_id, owner in (self._owners or {}).items():
            if str(owner.get("userId") or "") == str(user_id):
                return self.owner_label(owner_id)
        return f"user {user_id}"

    def owner_lines(self, owner_id: str) -> str:
        teams = self.owner_teams(owner_id)
        text = f"{self.owner_label(owner_id)} (owner {owner_id})"
        if teams:
            text += " | team(s) " + ", ".join(f"{name}{' (primary)' if primary else ''}" for _, name, primary in teams)
        return text

    # ── Property definitions, labels, pipelines ─────────────────────────

    async def all_properties(self, client: httpx.AsyncClient, object_type: str) -> tuple[list[dict[str, Any]], str | None]:
        if object_type in self._all_properties:
            return self._all_properties[object_type], None
        data, err = await self._get(client, f"/crm/v3/properties/{object_type}", f"list {object_type} properties")
        if err:
            return [], err
        defs = [d for d in _results(data) if isinstance(d.get("name"), str)]
        self._all_properties[object_type] = defs
        self._property_defs[object_type].update({d["name"]: d for d in defs})
        return defs, None

    async def property_definitions(
        self, client: httpx.AsyncClient, object_type: str, names: Iterable[str]
    ) -> dict[str, dict[str, Any]]:
        """The definitions of *names* (cached; one batch read for the missing ones)."""
        cache = self._property_defs[object_type]
        wanted = list(dict.fromkeys(names))
        missing = [n for n in wanted if n not in cache and n not in self._unknown_properties[object_type]]
        if missing and object_type not in self._definitions_error:
            data, err = await self._post(
                client,
                f"/crm/v3/properties/{object_type}/batch/read",
                f"read {object_type} property definitions",
                {"archived": False, "inputs": [{"name": n} for n in missing]},
            )
            if err:
                _defs, err = await self.all_properties(client, object_type)
                if err:
                    self._definitions_error[object_type] = _first_line(err)
            else:
                cache.update({d["name"]: d for d in _results(data) if isinstance(d.get("name"), str)})
            self._unknown_properties[object_type].update(n for n in missing if n not in cache)
        return {n: cache[n] for n in wanted if n in cache}

    def property_label(self, object_type: str, name: str) -> str:
        definition = self._property_defs.get(object_type, {}).get(name) or {}
        return str(definition.get("label") or name)

    async def pipeline_labels(self, client: httpx.AsyncClient, object_type: str) -> dict[str, str]:
        """``{pipeline or stage id: label}``; a stage names its pipeline when there are several."""
        if object_type in self._pipelines:
            return self._pipelines[object_type]
        data, err = await self._get(client, f"/crm/v3/pipelines/{object_type}", f"list {object_type} pipelines")
        labels: dict[str, str] = {}
        if not err:
            pipelines = _results(data)
            for pipeline in pipelines:
                pipeline_label = str(pipeline.get("label") or pipeline.get("id") or "")
                labels[str(pipeline.get("id"))] = pipeline_label
                for stage in pipeline.get("stages") or []:
                    if isinstance(stage, Mapping) and stage.get("id") is not None:
                        label = str(stage.get("label") or stage["id"])
                        labels[str(stage["id"])] = f"{label} ({pipeline_label})" if len(pipelines) > 1 else label
        self._pipelines[object_type] = labels
        return labels

    async def prepare_labels(
        self,
        client: httpx.AsyncClient,
        object_type: str,
        records: Iterable[Mapping[str, str]],
        names: Iterable[str],
    ) -> None:
        """Fetch what labelling *names* in *records* takes: definitions, owners, pipelines."""
        rows = list(records)
        present = [n for n in dict.fromkeys(names) if any(r.get(n) for r in rows)]
        if not present:
            return
        defs = await self.property_definitions(client, object_type, present)
        if any(_is_owner_property(n, defs.get(n)) for n in present):
            await self.owners(client)
        unlabelled = any(
            n in _STAGE_PROPERTIES and str(r.get(n)) not in _options(defs.get(n)) for n in present for r in rows if r.get(n)
        )
        if unlabelled:
            await self.pipeline_labels(client, object_type)

    def label(self, object_type: str, name: str, raw: str) -> str:
        """*raw*'s label from what :meth:`prepare_labels` fetched; *raw* when none names it."""
        if raw is None or raw == "":
            return raw
        definition = self._property_defs.get(object_type, {}).get(name) or {}
        if _is_owner_property(name, definition):
            known = (self._owners or {}).get(str(raw))
            return self.owner_label(raw) if known else raw
        if definition.get("type") == "datetime":
            moment = parse_hubspot_time(raw)
            return iso_utc(moment) if moment else raw
        options = _options(definition)
        stages = self._pipelines.get(object_type, {}) if name in _STAGE_PROPERTIES else {}
        parts = raw.split(";") if definition.get("type") == "enumeration" and ";" in raw else [raw]
        return ";".join(options.get(p) or stages.get(p) or p for p in parts)

    def shown(self, object_type: str, name: str, raw: str, *, raw_values: bool = False) -> str:
        """*raw* as a tool shows it: ``Label (raw)`` when a label differs from it."""
        if raw_values or raw is None or raw == "":
            return raw
        label = self.label(object_type, name, raw)
        if label == raw:
            return raw
        definition = self._property_defs.get(object_type, {}).get(name)
        if _is_owner_property(name, definition):
            return f"{label} (owner {raw})"
        if definition and definition.get("type") == "datetime":
            return label
        return f"{label} ({raw})"

    def labels_note(self, object_type: str) -> str:
        """Why labels of *object_type* are missing, when they are."""
        notes = []
        if object_type in self._definitions_error:
            notes.append(f"enumeration labels not resolved ({self._definitions_error[object_type]})")
        if self._owners_error:
            notes.append(f"owner names not resolved ({_first_line(self._owners_error)})")
        return "Note: " + "; ".join(notes) if notes else ""

    # ── Searches, associations, engagements ─────────────────────────────

    async def search_pages(
        self,
        client: httpx.AsyncClient,
        object_type: str,
        body: Mapping[str, Any],
        count: Any,
        *,
        action: str,
        cap: int = SEARCH_MAX_RESULTS,
    ) -> tuple[list[dict[str, Any]], int, str, str | None]:
        """``(results, total, next cursor, error)``: up to *count* results, paged 200 at a time.

        HubSpot sometimes ends a page without ``paging.next`` while its
        ``total`` says more records match (a page of 100 out of 200 asked):
        a search cursor is the record offset, so the next page is read from
        the offset reached instead of stopping short and silent.
        """
        want = max(1, min(_int(count, 10), cap))
        results: list[dict[str, Any]] = []
        total = 0
        after = str(body.get("after") or "")
        offset = _int(after or 0, -1)
        while True:
            page = {k: v for k, v in body.items() if k != "after"}
            page["limit"] = min(SEARCH_PAGE, want - len(results))
            if after:
                page["after"] = after
            data, err = await self._post(client, f"/crm/v3/objects/{object_type}/search", action, page)
            if err:
                return results, total, after, err
            total = _int(data.get("total"), 0)
            batch = _results(data)
            results.extend(batch)
            after = _next_after(data)
            if offset >= 0:
                offset += len(batch)
            if not after and batch and offset >= 0 and offset < min(total, SEARCH_OFFSET_CEILING):
                after = str(offset)
            if not after or not batch or len(results) >= want:
                return results, total, after, None
            offset = _int(after, -1)

    async def associated_ids(
        self, client: httpx.AsyncClient, from_type: str, from_id: str, to_type: str, *, cap: int
    ) -> tuple[list[str], bool, str | None]:
        """``(ids, more than cap, error)`` of the *to_type* records associated with one record."""
        ids: list[str] = []
        after = ""
        while True:
            params = {"limit": "500"}
            if after:
                params["after"] = after
            data, err = await self._get(
                client,
                f"/crm/v4/objects/{from_type}/{from_id}/associations/{to_type}",
                f"list the {to_type} of {from_type} {from_id}",
                params,
            )
            if err:
                return ids, False, err
            ids.extend(str(r["toObjectId"]) for r in _results(data) if r.get("toObjectId") is not None)
            after = _next_after(data)
            if not after or len(ids) >= cap:
                break
        ids = split_ids(ids)
        return ids[:cap], bool(after) or len(ids) > cap, None

    async def batch_association_ids(
        self, client: httpx.AsyncClient, from_type: str, ids: Sequence[str], to_type: str
    ) -> tuple[dict[str, list[tuple[str, list[str]]]], bool, str | None]:
        """``({from id: [(to id, labels)]}, some lists cut, error)`` through the v4 batch read."""
        out: dict[str, list[tuple[str, list[str]]]] = {}
        cut = False
        for chunk in _chunks(list(ids), BATCH_SIZE):
            data, err = await self._post(
                client,
                f"/crm/v4/associations/{from_type}/{to_type}/batch/read",
                f"read the {to_type} of {len(chunk)} {from_type}",
                {"inputs": [{"id": i} for i in chunk]},
            )
            if err:
                return out, cut, err
            for result in _results(data):
                source = result.get("from")
                from_id = str(source.get("id") or "") if isinstance(source, Mapping) else ""
                if not from_id:
                    continue
                for target in result.get("to") or []:
                    if not isinstance(target, Mapping) or target.get("toObjectId") is None:
                        continue
                    labels = [
                        str(t["label"])
                        for t in target.get("associationTypes") or []
                        if isinstance(t, Mapping) and t.get("label")
                    ]
                    out.setdefault(from_id, []).append((str(target["toObjectId"]), labels))
                cut = cut or bool(_next_after(result))
        return out, cut, None

    async def read_engagements(
        self,
        client: httpx.AsyncClient,
        type_: str,
        ids: Sequence[str],
        *,
        properties: Sequence[str] | None = None,
    ) -> tuple[list[Engagement], str | None]:
        """Engagements of one type by id, read and labelled (100 per request)."""
        objects: list[dict[str, Any]] = []
        error = None
        for chunk in _chunks(list(ids), BATCH_SIZE):
            data, err = await self._post(
                client,
                f"/crm/v3/objects/{type_}/batch/read",
                f"read {len(chunk)} {type_}",
                {"properties": list(properties or ENGAGEMENT_SPECS[type_].properties), "inputs": [{"id": i} for i in chunk]},
            )
            if err:
                error = err
                break
            objects.extend(_results(data))
        await self._prepare_engagement_labels(client, type_, objects)
        return [normalize_engagement(type_, o, label=self.label) for o in objects], error

    async def _prepare_engagement_labels(self, client: httpx.AsyncClient, type_: str, objects: list[dict[str, Any]]) -> None:
        if not objects:
            return
        rows = [record_properties(o) for o in objects]
        spec = ENGAGEMENT_SPECS[type_]
        names = [*spec.status, *(n for n in spec.extra if n in ("hs_activity_type", "hs_meeting_source", "hs_task_type", "hs_task_priority"))]
        await self.prepare_labels(client, type_, rows, names)
        if any(r.get("hubspot_owner_id") for r in rows):
            await self.owners(client)

    async def describe_engagements(
        self, client: httpx.AsyncClient, type_: str, ids: Sequence[str], *, body_chars: int = 200
    ) -> list[str]:
        """Readable lines of the most recent engagements among *ids* (never bare ids)."""
        shown = list(ids)[-_ASSOCIATED_ENGAGEMENTS_SHOWN * 3 :]
        engagements, err = await self.read_engagements(client, type_, shown)
        engagements.sort(key=lambda e: (e.timestamp is not None, e.timestamp or datetime.min.replace(tzinfo=UTC)), reverse=True)
        lines = [
            "  " + format_engagement(e, owner=self.owner_label, body_chars=body_chars).replace("\n", "\n  ")
            for e in engagements[:_ASSOCIATED_ENGAGEMENTS_SHOWN]
        ]
        if len(ids) > len(lines):
            lines.append(
                f"  ({len(ids) - len(lines)} more {type_} not shown: get_hubspot_record_timeline or "
                f"search_hubspot_engagements reads them all, in order)"
            )
        if err:
            lines.append(f"  ({type_} not read: {_first_line(err)})")
        return lines

    # ── Forms ───────────────────────────────────────────────────────────

    async def forms(self, client: httpx.AsyncClient) -> tuple[list[dict[str, Any]], str | None]:
        """Every form of the account (fetched once)."""
        if self._forms is not None:
            return self._forms, None
        forms: list[dict[str, Any]] = []
        after = ""
        for _ in range(_FORMS_MAX_PAGES):
            params: dict[str, Any] = {"limit": str(_FORM_PAGE), "formTypes": list(_FORM_TYPES)}
            if after:
                params["after"] = after
            data, err = await self._get(client, "/marketing/v3/forms", "list forms", params)
            if err:
                return forms, err
            forms.extend(f for f in _results(data) if f.get("id"))
            after = _next_after(data)
            if not after:
                break
        self._forms = forms
        return forms, None

    async def match_forms(self, client: httpx.AsyncClient, ref: str) -> tuple[list[tuple[str, str]], str]:
        """``([(form id, name)], error)`` of the forms *ref* names: an id, else an exact
        name, else every form whose name contains it (case-insensitive)."""
        ref = ref.strip()
        forms, err = await self.forms(client)
        if is_uuid(ref):
            name = next((str(f.get("name") or "") for f in forms if str(f.get("id")) == ref), "")
            return [(ref, name)], ""
        if err:
            return [], err
        lowered = ref.lower()
        exact = [f for f in forms if str(f.get("name") or "").strip().lower() == lowered]
        partial = [f for f in forms if lowered in str(f.get("name") or "").lower()]
        matches = exact or partial
        if not matches:
            known = ", ".join(sorted(f"'{f.get('name')}'" for f in forms)[:30])
            return [], f"Error: no HubSpot form is named '{ref}'. Forms: {known or 'none'}."
        return [(str(f["id"]), str(f.get("name") or "")) for f in matches], ""

    async def form_submissions(
        self,
        client: httpx.AsyncClient,
        form_id: str,
        form_name: str,
        *,
        start: datetime | None,
        end: datetime | None,
        limit: int,
        after: str = "",
        deadline: float | None = None,
    ) -> tuple[list[FormSubmission], str, str | None]:
        """``(submissions, next cursor, error)`` of one form in ``[start, end]``, newest
        first, whole pages of 50 until *limit* (the cursor is empty past *start*)."""
        collected: list[FormSubmission] = []
        cursor = after
        while len(collected) < limit:
            if deadline is not None and time.monotonic() > deadline:
                return collected, cursor, None
            params = {"limit": str(_FORM_PAGE)}
            if cursor:
                params["after"] = cursor
            data, err = await self._get(
                client, f"/form-integrations/v1/submissions/forms/{form_id}", f"list the submissions of form {form_id}", params
            )
            if err:
                return collected, cursor, err
            items = _results(data)
            for item in items:
                submission = submission_from_form(form_id, item, form_name=form_name)
                if start and submission.timestamp and submission.timestamp < start:
                    return collected, "", None
                if within(submission.timestamp, start, end):
                    collected.append(submission)
            cursor = _next_after(data)
            if not cursor or not items:
                return collected, "", None
        return collected, cursor, None

    async def contacts_by_email(
        self, client: httpx.AsyncClient, emails: Sequence[str], properties: Sequence[str]
    ) -> tuple[dict[str, dict[str, Any]], str | None]:
        """``{email: contact}`` of the contacts whose primary email is one of *emails*."""
        found: dict[str, dict[str, Any]] = {}
        for chunk in _chunks(list(dict.fromkeys(e.lower() for e in emails if e)), BATCH_SIZE):
            data, err = await self._post(
                client,
                "/crm/v3/objects/contacts/batch/read",
                f"find the contacts of {len(chunk)} email address(es)",
                {"idProperty": "email", "properties": list(dict.fromkeys(["email", *properties])), "inputs": [{"id": e} for e in chunk]},
            )
            if err:
                return found, err
            for contact in _results(data):
                email = record_properties(contact).get("email", "").strip().lower()
                if email:
                    found[email] = contact
        return found, None

    async def contact_submissions(
        self,
        client: httpx.AsyncClient,
        contact_id: str,
        *,
        with_values: bool,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[list[FormSubmission], list[str], str | None]:
        """``(submissions, notes, error)``: a contact's whole form history, oldest first.

        The legacy contact profile is the one HubSpot API that lists a contact's
        submissions; when it does not answer, the history of the contact's
        ``recent_conversion_event_name`` (one entry per conversion) stands in.
        """
        notes: list[str] = []
        profile, err = await self._get(
            client,
            f"/contacts/v1/contact/vid/{contact_id}/profile",
            f"read the form submissions of contact {contact_id}",
            {"formSubmissionMode": "all", "showListMemberships": "false", "propertyMode": "value_only", "property": "email"},
        )
        if err is None:
            submissions = submissions_from_profile(profile, contact_id)
        else:
            data, history_err = await self._get(
                client,
                f"/crm/v3/objects/contacts/{contact_id}",
                f"read the conversion history of contact {contact_id}",
                {"properties": "email", "propertiesWithHistory": "recent_conversion_event_name"},
            )
            if history_err:
                return [], notes, err
            email = record_properties(data).get("email", "").lower()
            submissions = submissions_from_history(data.get("propertiesWithHistory"), contact_id, email)
            notes.append(
                "form submissions read from the contact's conversion history (the contact profile did not answer: "
                f"{_first_line(err)}): form names and times; page and values are looked up in the forms' submissions"
            )
        submissions = [s for s in submissions if within(s.timestamp, start, end)]
        if with_values and submissions:
            await self.fill_submission_values(client, submissions, notes)
        return submissions, notes, None

    async def fill_submission_values(
        self, client: httpx.AsyncClient, submissions: Sequence[FormSubmission], notes: list[str]
    ) -> None:
        """Give each submission the values (and page) of its form's matching submission."""
        pending = [s for s in submissions if not s.values]
        unnamed = [s for s in pending if not s.form_id]
        if unnamed:
            forms, err = await self.forms(client)
            if err:
                notes.append(f"forms not listed ({_first_line(err)}): submission values not read")
            for s in unnamed:
                # A conversion is named ``<form name>: <page title>``: the longest form name it starts with.
                candidates = [
                    f for f in forms if f.get("name") and s.form_name.lower().startswith(str(f["name"]).lower())
                ]
                if candidates:
                    best = max(candidates, key=lambda f: len(str(f["name"])))
                    s.form_id = str(best["id"])
        by_form: dict[str, list[FormSubmission]] = defaultdict(list)
        for s in pending:
            if s.form_id:
                by_form[s.form_id].append(s)
        for form_id, items in by_form.items():
            dated = [s.timestamp for s in items if s.timestamp]
            oldest = min(dated) if dated else None
            cursor = ""
            pages = 0
            while any(not s.values for s in items) and pages < _FORM_SCAN_PAGES:
                params = {"limit": str(_FORM_PAGE)}
                if cursor:
                    params["after"] = cursor
                data, err = await self._get(
                    client, f"/form-integrations/v1/submissions/forms/{form_id}", f"list the submissions of form {form_id}", params
                )
                if err:
                    notes.append(f"values of form {form_id} not read ({_first_line(err)})")
                    break
                pages += 1
                candidates = [submission_from_form(form_id, item) for item in _results(data)]
                for s in items:
                    if s.values:
                        continue
                    match = next((c for c in candidates if _same_submission(s, c)), None)
                    if match:
                        s.values = match.values
                        s.page_url = s.page_url or match.page_url
                last = candidates[-1].timestamp if candidates else None
                cursor = _next_after(data)
                if not cursor or (oldest and last and (oldest - last).total_seconds() > _SUBMISSION_MATCH_SECONDS):
                    break
            missing = sum(1 for s in items if not s.values)
            if missing:
                notes.append(
                    f"{missing} submission(s) of form {form_id}: values not found in its {pages * _FORM_PAGE} most "
                    "recent submissions (list_hubspot_form_submissions reads further back)"
                )

    # ── Tools: engagements ──────────────────────────────────────────────

    async def get_hubspot_engagement(
        self, engagement_type: str, engagement_id: str, properties: str = "", max_body_chars: int = 20000
    ) -> str | ToolResult:
        """Read one engagement in full."""
        type_ = as_engagement_type(engagement_type)
        if not type_:
            return _refuse(f"Error: engagement_type must be one of {', '.join(ENGAGEMENT_TYPES)}.")
        eid = str(engagement_id or "").strip()
        if not eid.isdigit():
            return _refuse(f"Error: '{engagement_id}' is not a HubSpot engagement id (digits).")
        extra = split_names(properties)
        associated = ["contacts", "companies", "deals", "tickets"]
        if self._scope is not None:
            associated = list(dict.fromkeys([*associated, *(api_object_type(t) for t in self._scope)]))
        async with egress_client(timeout=30) as client:
            data, err = await self._get(
                client,
                f"/crm/v3/objects/{type_}/{eid}",
                f"read {type_} {eid}",
                {
                    "properties": ",".join(dict.fromkeys([*ENGAGEMENT_SPECS[type_].properties, *extra])),
                    "associations": ",".join(associated),
                },
            )
            if err:
                return err
            if self._scope is not None and not any(
                canonical_object_type(t) in self._scope for t in associations_of(data)
            ):
                return _refuse(
                    f"Refused: {type_} {eid} is not associated with a record of an object type the Scope(s) set for "
                    "this agent or assignment allows. Read the activity of an allowed record with "
                    "get_hubspot_record_timeline."
                )
            await self._prepare_engagement_labels(client, type_, [data])
            if extra:
                await self.prepare_labels(client, type_, [record_properties(data)], extra)
        engagement = normalize_engagement(type_, data, label=self.label)
        text = format_engagement(
            engagement,
            owner=self.owner_label,
            body_chars=max(200, min(_int(max_body_chars, 20000), 100_000)),
            with_associations=True,
            indent="  ",
        )
        props = record_properties(data)
        extras = [f"{name}: {self.shown(type_, name, props[name])}" for name in extra if props.get(name)]
        if extras:
            text += "\nRequested properties:\n  " + "\n  ".join(extras)
        if engagement.attachment_ids:
            text += (
                f"\nAttachments: download_hubspot_file(file_id, engagement_type='{type_}', engagement_id='{eid}') "
                "downloads one."
            )
        return text

    async def search_hubspot_engagements(
        self,
        engagement_type: str,
        associated_object_type: str = "",
        associated_object_id: str = "",
        from_date: str = "",
        to_date: str = "",
        query: str = "",
        filters_json: str = "",
        sort_direction: str = "DESCENDING",
        count: int = 50,
        after: str = "",
        properties: str = "",
        max_body_chars: int = 300,
    ) -> str | ToolResult:
        """Search one engagement type by record, date range, text and property filters."""
        type_ = as_engagement_type(engagement_type)
        if not type_:
            return _refuse(f"Error: engagement_type must be one of {', '.join(ENGAGEMENT_TYPES)}.")
        start, end, bounds_err = _bounds(from_date, to_date)
        if bounds_err:
            return _refuse(bounds_err)
        filters: list[dict[str, Any]] = []
        assoc_type = str(associated_object_type or "").strip()
        assoc_id = str(associated_object_id or "").strip()
        if assoc_type or assoc_id:
            pseudo = _ASSOCIATION_FILTER_TYPES.get(api_object_type(assoc_type))
            if not pseudo or not assoc_id:
                return _refuse(
                    "Error: pass associated_object_type (contacts, companies, deals or tickets) and "
                    "associated_object_id together."
                )
            filters.append({"propertyName": f"associations.{pseudo}", "operator": "EQ", "value": assoc_id})
        if start and end:
            filters.append(
                {"propertyName": "hs_timestamp", "operator": "BETWEEN", "value": str(epoch_ms(start)), "highValue": str(epoch_ms(end))}
            )
        elif start:
            filters.append({"propertyName": "hs_timestamp", "operator": "GTE", "value": str(epoch_ms(start))})
        elif end:
            filters.append({"propertyName": "hs_timestamp", "operator": "LTE", "value": str(epoch_ms(end))})
        extra = split_names(properties)
        body = self._search_body(
            properties=list(dict.fromkeys([*ENGAGEMENT_SPECS[type_].properties, *extra])),
            query=query,
            filters_json=filters_json,
            sort_by="hs_timestamp",
            sort_direction=sort_direction,
            count=SEARCH_PAGE,
            after=after,
        )
        groups = body.get("filterGroups") or [{"filters": []}]
        body["filterGroups"] = [{"filters": [*filters, *g["filters"]]} for g in groups if filters or g["filters"]]
        if not body["filterGroups"]:
            body.pop("filterGroups")
        async with egress_client(timeout=60) as client:
            results, total, next_after, err = await self.search_pages(client, type_, body, count, action=f"search {type_}")
            if err and not results:
                return err
            await self._prepare_engagement_labels(client, type_, results)
            if extra:
                await self.prepare_labels(client, type_, [record_properties(r) for r in results], extra)
        if not results:
            return _structured(f"No {type_} found{_range_text(start, end)}.", [])
        lines = [f"Found {total} {type_}{_range_text(start, end)}; showing {len(results)}:"]
        for obj in results:
            engagement = normalize_engagement(type_, obj, label=self.label)
            line = format_engagement(engagement, owner=self.owner_label, body_chars=_int(max_body_chars, 300))
            props = record_properties(obj)
            extras = [f"{n}: {self.shown(type_, n, props[n])}" for n in extra if props.get(n)]
            if extras:
                line += "\n    " + " | ".join(extras)
            lines.append(line)
        text = "\n".join(lines) + format_pagination_hint(next_after)
        if err:
            text += f"\nStopped early: {_first_line(err)}"
        return _structured(text, results)

    # ── Tools: timeline ─────────────────────────────────────────────────

    async def get_hubspot_record_timeline(
        self,
        object_type: str,
        record_id: str,
        from_date: str = "",
        to_date: str = "",
        types: str = "",
        limit: int = 100,
        order: str = "asc",
        body_chars: int = 400,
        properties: str = "",
    ) -> str | ToolResult:
        """The merged, chronological activity of one record."""
        api_type = api_object_type(object_type)
        rid = str(record_id or "").strip()
        if not api_type or not rid:
            return _refuse("Error: pass object_type and record_id.")
        start, end, bounds_err = _bounds(from_date, to_date)
        if bounds_err:
            return _refuse(bounds_err)
        wanted: list[str] = []
        for name in split_names(types) or [*ENGAGEMENT_TYPES, "form_submissions", "sequences", "property_changes"]:
            source = as_engagement_type(name) or _TIMELINE_SOURCES.get(name.strip().lower(), "")
            if not source:
                return _refuse(
                    f"Error: unknown timeline type '{name}'. Types: {', '.join(ENGAGEMENT_TYPES)}, form_submissions, "
                    "sequences, property_changes."
                )
            wanted.append(source)
        extra_history, extra_err = _history_property_names(properties)
        if extra_err:
            return _refuse(extra_err)
        if extra_history:
            wanted.append("property_changes")
        wanted = list(dict.fromkeys(wanted))
        is_contact = api_type == "contacts"
        events: list[TimelineEvent] = []
        notes: list[str] = []
        counts: Counter[str] = Counter()
        body_limit = max(0, min(_int(body_chars, 400), 20_000))
        async with egress_client(timeout=60) as client:
            for type_ in (t for t in wanted if t in ENGAGEMENT_SPECS):
                ids, more, err = await self.associated_ids(client, api_type, rid, type_, cap=_TIMELINE_MAX_PER_TYPE)
                if err:
                    notes.append(f"{type_} not read: {_first_line(err)}")
                    continue
                if more:
                    notes.append(f"only the first {_TIMELINE_MAX_PER_TYPE} {type_} read")
                engagements, err = await self.read_engagements(client, type_, ids)
                if err:
                    notes.append(f"{type_} partly read: {_first_line(err)}")
                for e in engagements:
                    if within(e.timestamp, start, end):
                        counts[type_] += 1
                        text = format_engagement(e, owner=self.owner_label, body_chars=body_limit)
                        events.append(TimelineEvent(e.timestamp, type_, e.id, text))
            if is_contact and "form_submissions" in wanted:
                submissions, sub_notes, err = await self.contact_submissions(
                    client, rid, with_values=True, start=start, end=end
                )
                notes.extend(sub_notes)
                if err:
                    notes.append(f"form submissions not read: {_first_line(err)}")
                for s in submissions:
                    counts["form submissions"] += 1
                    events.append(TimelineEvent(s.timestamp, "form submission", s.conversion_id, format_submission(s)))
            history_names = list(
                dict.fromkeys(
                    [
                        *(
                            _HISTORY_PROPERTIES.get(api_type, ("hubspot_owner_id",))
                            if "property_changes" in wanted
                            else ()
                        ),
                        *extra_history,
                        *(_SEQUENCE_HISTORY if is_contact and "sequences" in wanted else ()),
                    ]
                )
            )
            if history_names:
                events.extend(await self._history_events(client, api_type, rid, history_names, start, end, counts, notes))
            if is_contact and "sequences" in wanted:
                events.extend(await self._enrollment_events(client, rid, start, end, events, counts, notes))
        ordered = merge_timeline(events, newest_first=str(order).strip().lower() == "desc")
        cap = max(1, min(_int(limit, 100), 1000))
        summary = ", ".join(f"{k}: {n}" for k, n in counts.most_common()) or "none"
        lines = [f"Timeline of {api_type} {rid}{_range_text(start, end)}: {len(ordered)} event(s) ({summary})."]
        if len(ordered) > cap:
            which = "latest" if str(order).strip().lower() == "desc" else "first"
            lines.append(
                f"Showing the {which} {cap}; narrow from_date / to_date, pick types, or raise limit for the rest."
            )
        if not ordered:
            lines.append("No activity is logged on this record in that range.")
        lines.extend(e.text for e in ordered[:cap])
        if notes:
            lines.append("Not read or partly read: " + "; ".join(notes))
        return "\n".join(lines)

    async def _history_events(
        self,
        client: httpx.AsyncClient,
        api_type: str,
        rid: str,
        names: Sequence[str],
        start: datetime | None,
        end: datetime | None,
        counts: Counter[str],
        notes: list[str],
    ) -> list[TimelineEvent]:
        data, err = await self._get(
            client,
            f"/crm/v3/objects/{api_type}/{rid}",
            f"read the property history of {api_type} {rid}",
            {"propertiesWithHistory": ",".join(names)},
        )
        if err:
            notes.append(f"property history not read: {_first_line(err)}")
            return []
        changes = property_changes(data.get("propertiesWithHistory"), names)
        await self.prepare_labels(client, api_type, [{c.name: c.value} for c in changes], names)
        if any(c.user_id for c in changes):
            await self.owners(client)
        events = []
        for change in changes:
            if not within(change.timestamp, start, end):
                continue
            if change.name in _SEQUENCE_HISTORY:
                if not change.value:
                    continue
                what = {
                    "hs_latest_sequence_enrolled": f"enrolled in sequence {change.value}",
                    "hs_latest_sequence_unenrolled_date": "unenrolled from its latest sequence",
                    "hs_latest_sequence_finished_date": "finished its latest sequence",
                }[change.name]
                counts["sequence events"] += 1
                events.append(TimelineEvent(change.timestamp, "sequence", f"{change.name}-{change.value}", f"{iso_utc(change.timestamp)} | SEQUENCE {what}"))
                continue
            counts["property changes"] += 1
            value = self.shown(api_type, change.name, change.value) if change.value else "(cleared)"
            parts = [iso_utc(change.timestamp), f"PROPERTY CHANGE {self.property_label(api_type, change.name)}: {value}"]
            if change.source:
                parts.append(f"source {change.source}")
            if change.user_id:
                parts.append(f"by {self.user_label(change.user_id)}")
            events.append(TimelineEvent(change.timestamp, "property change", f"{change.name}-{change.value}", " | ".join(parts)))
        return events

    async def _enrollment_events(
        self,
        client: httpx.AsyncClient,
        contact_id: str,
        start: datetime | None,
        end: datetime | None,
        events: list[TimelineEvent],
        counts: Counter[str],
        notes: list[str],
    ) -> list[TimelineEvent]:
        enrollment, err = await self.current_enrollment(client, contact_id)
        if err:
            notes.append(f"sequence enrollment not read: {_first_line(err)}")
            return []
        if not enrollment:
            return []
        enrolled_at = parse_hubspot_time(enrollment.get("enrolledAt"))
        sequence_id = str(enrollment.get("sequenceId") or "")
        name = str(enrollment.get("sequenceName") or "")
        by = str(enrollment.get("enrolledByEmail") or enrollment.get("enrolledBy") or "")
        text = f"{iso_utc(enrolled_at)} | SEQUENCE enrolled in '{name}' (sequence {sequence_id}) by {by} - still enrolled"
        for event in events:
            if event.source == "sequence" and event.key == f"hs_latest_sequence_enrolled-{sequence_id}":
                event.text = text
                return []
        if not within(enrolled_at, start, end):
            return []
        counts["sequence events"] += 1
        return [TimelineEvent(enrolled_at, "sequence", f"current-{sequence_id}", text)]

    async def current_enrollment(self, client: httpx.AsyncClient, contact_id: str) -> tuple[dict[str, Any], str | None]:
        """The sequence enrollment a contact is in now (``{}`` when none)."""
        resp, err = await self._request(
            client,
            "get",
            f"{_BASE_URL}/automation/v4/sequences/enrollments/contact/{contact_id}",
            "HubSpot",
            f"read the sequence enrollment of contact {contact_id}",
            headers=self._headers,
            expected_statuses={404},
        )
        if resp is not None and resp.status_code == 404:
            return {}, None
        if err:
            return {}, err
        return _body(resp), None

    # ── Tools: forms ────────────────────────────────────────────────────

    async def list_hubspot_forms(self, query: str = "", count: int = 100) -> str:
        """List the account's forms."""
        async with egress_client(timeout=60) as client:
            forms, err = await self.forms(client)
        if err and not forms:
            return err
        q = str(query or "").strip().lower()
        matched = [f for f in forms if not q or q in str(f.get("name") or "").lower()]
        matched.sort(key=lambda f: str(f.get("name") or "").lower())
        if not matched:
            return f"No HubSpot form matches '{query}'." if q else "No HubSpot forms found."
        cap = max(1, min(_int(count, 100), 1000))
        lines = [f"Found {len(matched)} form(s):"]
        for form in matched[:cap]:
            parts = [str(form.get("name") or "Unnamed"), f"form id {form.get('id')}"]
            if form.get("formType"):
                parts.append(f"type {form['formType']}")
            for key, label in (("createdAt", "created"), ("updatedAt", "updated")):
                moment = parse_hubspot_time(form.get(key))
                if moment:
                    parts.append(f"{label} {iso_utc(moment)[:10]}")
            if form.get("archived"):
                parts.append("archived")
            lines.append(" | ".join(parts))
        if len(matched) > cap:
            lines.append(f"({len(matched) - cap} more: narrow with query)")
        if err:
            lines.append(f"Listing stopped early: {_first_line(err)}")
        return "\n".join(lines)

    async def list_hubspot_form_submissions(
        self,
        form_id: str,
        from_date: str = "",
        to_date: str = "",
        count: int = 50,
        after: str = "",
        resolve_contacts: bool = True,
        value_chars: int = 300,
    ) -> str | ToolResult:
        """List one form's submissions with their values."""
        start, end, bounds_err = _bounds(from_date, to_date)
        if bounds_err:
            return _refuse(bounds_err)
        ref = str(form_id or "").strip()
        if not ref:
            return _refuse("Error: pass form_id (a form id or name; list_hubspot_forms lists them).")
        async with egress_client(timeout=60) as client:
            matches, err = await self.match_forms(client, ref)
            if err:
                return _refuse(err) if err.startswith("Error: no HubSpot form") else err
            if len(matches) > 1:
                listed = "; ".join(f"'{name}' ({fid})" for fid, name in matches[:15])
                return _refuse(f"Error: '{ref}' names several forms: {listed}. Pass one form id.")
            fid, name = matches[0]
            limit = max(1, min(_int(count, 50), 500))
            submissions, cursor, err = await self.form_submissions(
                client, fid, name, start=start, end=end, limit=limit, after=str(after or "")
            )
            if err and not submissions:
                return err
            if resolve_contacts and submissions:
                contacts, contact_err = await self.contacts_by_email(
                    client, [s.email for s in submissions if s.email], ["email"]
                )
                for s in submissions:
                    contact = contacts.get(s.email)
                    if contact:
                        s.contact_id = str(contact.get("id") or "")
                if contact_err:
                    err = err or contact_err
        title = f"form '{name}' ({fid})" if name else f"form {fid}"
        if not submissions:
            return f"No submissions of {title}{_range_text(start, end)}."
        lines = [f"{len(submissions)} submission(s) of {title}{_range_text(start, end)}, newest first:"]
        lines.extend(format_submission(s, value_chars=max(50, _int(value_chars, 300))) for s in submissions)
        text = "\n".join(lines) + format_pagination_hint(cursor)
        if err:
            text += f"\nNote: {_first_line(err)}"
        return text

    async def get_hubspot_contact_form_submissions(
        self, contact_id: str, include_values: bool = True, value_chars: int = 300
    ) -> str | ToolResult:
        """A contact's whole form-submission history."""
        cid = str(contact_id or "").strip()
        if not cid.isdigit():
            return _refuse(f"Error: '{contact_id}' is not a HubSpot contact id (digits).")
        async with egress_client(timeout=60) as client:
            submissions, notes, err = await self.contact_submissions(client, cid, with_values=bool(include_values))
        if err:
            return err
        if not submissions:
            text = f"Contact {cid} has no form submission."
        else:
            lines = [f"{len(submissions)} form submission(s) of contact {cid}, oldest first:"]
            lines.extend(format_submission(s, value_chars=max(50, _int(value_chars, 300))) for s in submissions)
            text = "\n".join(lines)
        if notes:
            text += "\nNotes: " + "; ".join(notes)
        return text

    # ── Tools: properties and batch reads ───────────────────────────────

    async def list_hubspot_properties(self, object_type: str, query: str = "", include_options: bool = True, count: int = 200) -> str | ToolResult:
        """List an object type's property definitions."""
        api_type = as_engagement_type(object_type) or api_object_type(object_type)
        if not api_type:
            return _refuse("Error: pass object_type (e.g. contacts, companies, deals, tickets, emails, calls).")
        async with egress_client(timeout=60) as client:
            defs, err = await self.all_properties(client, api_type)
        if err:
            return err
        q = str(query or "").strip().lower()
        matched = [
            d for d in defs if not q or q in str(d.get("name", "")).lower() or q in str(d.get("label", "")).lower()
        ]
        matched.sort(key=lambda d: (str(d.get("groupName") or ""), str(d.get("name"))))
        if not matched:
            return f"No {api_type} property matches '{query}'."
        cap = max(1, min(_int(count, 200), 1000))
        lines = [f"{len(matched)} {api_type} propert{'y' if len(matched) == 1 else 'ies'}:"]
        for d in matched[:cap]:
            parts = [str(d.get("name")), str(d.get("label") or ""), f"{d.get('type')}/{d.get('fieldType')}"]
            if d.get("groupName"):
                parts.append(f"group {d['groupName']}")
            if _is_owner_property(str(d.get("name")), d):
                parts.append("owner id (shown as the owner's name)")
            if d.get("calculated"):
                parts.append("calculated")
            metadata = d.get("modificationMetadata")
            if isinstance(metadata, Mapping) and metadata.get("readOnlyValue"):
                parts.append("read-only")
            options = _options(d)
            if include_options and options:
                shown = [f"{label}={value}" if label != value else value for value, label in list(options.items())[:15]]
                more = f" +{len(options) - 15} more" if len(options) > 15 else ""
                parts.append("options " + ", ".join(shown) + more)
            lines.append(" | ".join(parts))
        if len(matched) > cap:
            lines.append(f"({len(matched) - cap} more: narrow with query)")
        lines.append("Pass property names to the properties argument of the search, get and batch tools.")
        return "\n".join(lines)

    async def batch_get_hubspot_records(
        self, object_type: str, ids: list[str] | str, properties: str = "", id_property: str = "", raw_values: bool = False
    ) -> str | ToolResult:
        """Read up to 500 records (or engagements) of one type by id."""
        wanted = split_ids(ids)
        if not wanted:
            return _refuse("Error: pass ids (a list of record ids).")
        dropped = max(0, len(wanted) - _BATCH_MAX_IDS)
        wanted = wanted[:_BATCH_MAX_IDS]
        type_ = as_engagement_type(object_type)
        async with egress_client(timeout=60) as client:
            if type_:
                engagements, err = await self.read_engagements(client, type_, wanted)
                if err and not engagements:
                    return err
                found = {e.id for e in engagements}
                lines = [f"{len(engagements)} of {len(wanted)} {type_}:"]
                lines.extend(format_engagement(e, owner=self.owner_label, body_chars=300) for e in engagements)
                missing = [i for i in wanted if i not in found]
                if missing:
                    lines.append(f"Not found: {', '.join(missing)}")
                if err:
                    lines.append(f"Stopped early: {_first_line(err)}")
                return "\n".join(lines)
            api_type = api_object_type(object_type)
            if not api_type:
                return _refuse("Error: pass object_type.")
            names = list(dict.fromkeys([*split_names(self._default_properties.get(api_type, "")), *split_names(properties)]))
            id_prop = str(id_property or "").strip()
            records: list[dict[str, Any]] = []
            error = None
            for chunk in _chunks(wanted, BATCH_SIZE):
                body: dict[str, Any] = {"properties": names, "inputs": [{"id": i} for i in chunk]}
                if id_prop:
                    body["idProperty"] = id_prop
                data, err = await self._post(client, f"/crm/v3/objects/{api_type}/batch/read", f"read {len(chunk)} {api_type}", body)
                if err:
                    error = err
                    break
                records.extend(_results(data))
            if error and not records:
                return error
            if not raw_values:
                await self.prepare_labels(client, api_type, [record_properties(r) for r in records], names)
        found_keys = {str(r.get("id")) for r in records} | {
            record_properties(r).get(id_prop, "").lower() for r in records if id_prop
        }
        lines = [f"{len(records)} of {len(wanted)} {api_type}:"]
        for record in records:
            props = record_properties(record)
            values = [f"{n}: {self.shown(api_type, n, props[n], raw_values=raw_values)}" for n in names if props.get(n)]
            lines.append(f"{api_type} {record.get('id')} | " + " | ".join(values))
        missing = [i for i in wanted if i not in found_keys and i.lower() not in found_keys]
        if missing:
            lines.append(f"Not found: {', '.join(missing)}")
        if dropped:
            lines.append(f"{dropped} more id(s) not read: pass at most {_BATCH_MAX_IDS} per call.")
        if error:
            lines.append(f"Stopped early: {_first_line(error)}")
        note = self.labels_note(api_type) if not raw_values else ""
        if note:
            lines.append(note)
        return "\n".join(lines)

    async def batch_get_hubspot_associations(
        self, object_type: str, ids: list[str] | str, to_object_type: str, include_details: bool = True
    ) -> str | ToolResult:
        """The associations of up to 1000 records to another type (engagements read, not bare ids)."""
        wanted = split_ids(ids)
        if not wanted:
            return _refuse("Error: pass ids (a list of record ids).")
        dropped = max(0, len(wanted) - _ASSOCIATION_MAX_IDS)
        wanted = wanted[:_ASSOCIATION_MAX_IDS]
        from_type = api_object_type(object_type)
        to_engagement = as_engagement_type(to_object_type)
        to_type = to_engagement or api_object_type(to_object_type)
        if not from_type or not to_type:
            return _refuse("Error: pass object_type and to_object_type.")
        async with egress_client(timeout=60) as client:
            mapping, cut, err = await self.batch_association_ids(client, from_type, wanted, to_type)
            if err and not mapping:
                return err
            engagements: dict[str, Engagement] = {}
            if to_engagement and include_details:
                all_ids = list(dict.fromkeys(i for targets in mapping.values() for i, _ in targets))[:_ASSOCIATION_MAX_IDS]
                read, read_err = await self.read_engagements(client, to_engagement, all_ids)
                engagements = {e.id: e for e in read}
                err = err or read_err
        lines = [f"{to_type} associated with {len(wanted)} {from_type}:"]
        empty = []
        for source in wanted:
            targets = mapping.get(source) or []
            if not targets:
                empty.append(source)
                continue
            if to_engagement and engagements:
                items = sorted(
                    (engagements[i] for i, _ in targets if i in engagements),
                    key=lambda e: e.timestamp or datetime.min.replace(tzinfo=UTC),
                    reverse=True,
                )
                lines.append(f"{from_type} {source}: {len(targets)} {to_type}")
                for e in items[:_ASSOCIATED_ENGAGEMENTS_SHOWN]:
                    summary = one_line(e.subject or e.body, 100)
                    lines.append(
                        f"  {iso_utc(e.timestamp)} | {e.kind}{' ' + e.direction if e.direction else ''} | "
                        f"{self.owner_label(e.owner_id) if e.owner_id else 'no owner'} | {summary} | id {e.id}"
                    )
                if len(items) > _ASSOCIATED_ENGAGEMENTS_SHOWN:
                    lines.append(f"  ({len(items) - _ASSOCIATED_ENGAGEMENTS_SHOWN} more)")
            else:
                listed = ", ".join(f"{i} ({', '.join(labels)})" if labels else i for i, labels in targets)
                lines.append(f"{from_type} {source} -> {listed}")
        if empty:
            lines.append(f"No {to_type}: {', '.join(empty)}")
        if cut:
            lines.append("Some records have more than 500 associations of that type: only the first 500 are listed.")
        if dropped:
            lines.append(f"{dropped} more id(s) not read: pass at most {_ASSOCIATION_MAX_IDS} per call.")
        if err:
            lines.append(f"Stopped early: {_first_line(err)}")
        return "\n".join(lines)

    # ── Tools: teams and sequences ──────────────────────────────────────

    async def list_hubspot_teams(self) -> str:
        """List the account's teams and their members."""
        async with egress_client(timeout=30) as client:
            data, err = await self._get(client, "/settings/v3/users/teams", "list teams")
            owners = await self.owners(client)
        by_user = {str(o.get("userId")): oid for oid, o in owners.items() if o.get("userId") is not None}

        def member(user_id: Any) -> str:
            owner_id = by_user.get(str(user_id))
            return f"{self.owner_label(owner_id)} (owner {owner_id})" if owner_id else f"user {user_id}"

        lines: list[str] = []
        if not err:
            teams = _results(data)
            if not teams:
                return "No teams are set up in this HubSpot account."
            lines.append(f"{len(teams)} team(s):")
            for team in sorted(teams, key=lambda t: str(t.get("name") or "").lower()):
                members = [member(u) for u in team.get("userIds") or []]
                secondary = [member(u) for u in team.get("secondaryUserIds") or []]
                line = f"{team.get('name')} (team id {team.get('id')}): {len(members)} member(s)"
                if members:
                    line += ": " + "; ".join(members)
                if secondary:
                    line += " | secondary: " + "; ".join(secondary)
                lines.append(line)
            return "\n".join(lines)
        teams_of: dict[tuple[str, str], list[str]] = defaultdict(list)
        for owner_id in owners:
            for team_id, name, _primary in self.owner_teams(owner_id):
                teams_of[(team_id, name)].append(f"{self.owner_label(owner_id)} (owner {owner_id})")
        if not teams_of:
            return err if not owners else f"No owner belongs to a team. (Teams not listed: {_first_line(err)})"
        lines.append(f"{len(teams_of)} team(s), read from the owners (the teams list did not answer: {_first_line(err)}):")
        for (team_id, name), members in sorted(teams_of.items(), key=lambda kv: kv[0][1].lower()):
            lines.append(f"{name} (team id {team_id}): {len(members)} member(s): " + "; ".join(members))
        return "\n".join(lines)

    async def list_hubspot_sequences(self, user: str = "", query: str = "", count: int = 100) -> str | ToolResult:
        """List sequences, of one user or of every active owner."""
        async with egress_client(timeout=60) as client:
            owners = await self.owners(client)
            if self._owners_error and not owners:
                return self._owners_error
            ref = str(user or "").strip().lower()
            candidates = [
                (str(o.get("userId")), oid)
                for oid, o in owners.items()
                if o.get("userId") is not None and not o.get("archived")
                and (not ref or ref in (oid, str(o.get("userId")), str(o.get("email") or "").lower()))
            ]
            if ref and not candidates:
                return _refuse(f"Error: no active HubSpot owner has the owner id, user id or email '{user}'.")
            sequences: dict[str, dict[str, Any]] = {}
            notes = []
            for user_id, _owner_id in candidates[:_SEQUENCE_USERS_MAX]:
                after = ""
                for _ in range(5):
                    params = {"userId": user_id, "limit": "100"}
                    if query:
                        params["name"] = str(query)
                    if after:
                        params["after"] = after
                    data, err = await self._get(client, "/automation/v4/sequences", f"list the sequences of user {user_id}", params)
                    if err:
                        if is_auth_or_permission_error(err):
                            return err
                        notes.append(f"user {user_id}: {_first_line(err)}")
                        break
                    for sequence in _results(data):
                        sequences.setdefault(str(sequence.get("id")), sequence)
                    after = _next_after(data)
                    if not after:
                        break
        if len(candidates) > _SEQUENCE_USERS_MAX:
            notes.append(f"only the sequences of the first {_SEQUENCE_USERS_MAX} owners are listed; pass user")
        q = str(query or "").strip().lower()
        listed = [s for s in sequences.values() if not q or q in str(s.get("name") or "").lower()]
        listed.sort(key=lambda s: str(s.get("name") or "").lower())
        if not listed:
            text = "No sequences found."
        else:
            cap = max(1, min(_int(count, 100), 1000))
            lines = [f"{len(listed)} sequence(s):"]
            for sequence in listed[:cap]:
                parts = [str(sequence.get("name") or "Unnamed"), f"sequence id {sequence.get('id')}"]
                if sequence.get("userId") is not None:
                    parts.append(f"owned by {self.user_label(sequence['userId'])}")
                for key, label in (("createdAt", "created"), ("updatedAt", "updated")):
                    moment = parse_hubspot_time(sequence.get(key))
                    if moment:
                        parts.append(f"{label} {iso_utc(moment)[:10]}")
                lines.append(" | ".join(parts))
            text = "\n".join(lines)
        if notes:
            text += "\nNotes: " + "; ".join(notes)
        return text

    async def get_hubspot_sequence_enrollments(self, contact_id: str) -> str | ToolResult:
        """A contact's sequence enrollments: now, in its history, the emails they sent and the replies."""
        cid = str(contact_id or "").strip()
        if not cid.isdigit():
            return _refuse(f"Error: '{contact_id}' is not a HubSpot contact id (digits).")
        notes: list[str] = []
        async with egress_client(timeout=60) as client:
            current, current_err = await self.current_enrollment(client, cid)
            if current_err:
                notes.append(f"current enrollment not read: {_first_line(current_err)}")
            data, err = await self._get(
                client,
                f"/crm/v3/objects/contacts/{cid}",
                f"read the sequence history of contact {cid}",
                {"properties": ",".join(_SEQUENCE_PROPERTIES), "propertiesWithHistory": ",".join(_SEQUENCE_HISTORY)},
            )
            if err:
                return err
            body = self._search_body(
                properties=ENGAGEMENT_SPECS["emails"].properties,
                filters_json=[
                    {"filters": [{"propertyName": "associations.contact", "operator": "EQ", "value": cid}, {"propertyName": "hs_sequence_id", "operator": "HAS_PROPERTY"}]},
                    {"filters": [{"propertyName": "associations.contact", "operator": "EQ", "value": cid}, {"propertyName": "hs_email_direction", "operator": "EQ", "value": "INCOMING_EMAIL"}]},
                ],
                sort_by="hs_timestamp",
                sort_direction="ASCENDING",
                count=SEARCH_PAGE,
            )
            emails, _total, _after, err = await self.search_pages(client, "emails", body, SEARCH_PAGE, action=f"search the sequence emails of contact {cid}")
            if err:
                notes.append(f"emails not read: {_first_line(err)}")
            await self._prepare_engagement_labels(client, "emails", emails)
        props = record_properties(data)
        changes = property_changes(data.get("propertiesWithHistory"), _SEQUENCE_HISTORY)
        normalized = [normalize_engagement("emails", e, label=self.label) for e in emails]
        sent = [e for e in normalized if e.sequence_id]
        replies = [e for e in normalized if e.direction == "incoming"]
        lines = [f"Sequence enrollments of contact {cid}{' (' + props['email'] + ')' if props.get('email') else ''}:"]
        if current:
            lines.append(
                f"Now enrolled in '{current.get('sequenceName') or ''}' (sequence {current.get('sequenceId')}) since "
                f"{iso_utc(parse_hubspot_time(current.get('enrolledAt')))} by "
                f"{current.get('enrolledByEmail') or current.get('enrolledBy') or 'unknown'}."
            )
        else:
            lines.append("Current enrollment unknown." if current_err else "Not enrolled in a sequence now.")
        counters = [
            f"{label} {props[name]}"
            for name, label in (
                ("hs_sequences_enrolled_count", "enrollments"),
                ("hs_sequences_actively_enrolled_count", "active"),
                ("hs_sales_email_last_opened", "last sales email opened"),
                ("hs_sales_email_last_replied", "last sales email reply"),
            )
            if props.get(name)
        ]
        if counters:
            lines.append("Contact record: " + " | ".join(counters))
        starts = [c for c in changes if c.name == "hs_latest_sequence_enrolled" and c.value and c.timestamp]
        ends = [c for c in changes if c.name != "hs_latest_sequence_enrolled" and c.value and c.timestamp]
        if starts:
            lines.append("Enrollments, from the contact's sequence history:")
        for i, begin in enumerate(starts):
            next_start = starts[i + 1].timestamp if i + 1 < len(starts) else None
            finish = next(
                (c for c in ends if c.timestamp and begin.timestamp and c.timestamp >= begin.timestamp and (next_start is None or c.timestamp <= next_start)),
                None,
            )
            stop = finish.timestamp if finish else next_start
            steps = [e for e in sent if e.sequence_id == begin.value and within(e.timestamp, begin.timestamp, stop)]
            replied = [e for e in replies if within(e.timestamp, begin.timestamp, stop)]
            state = (
                f"{'unenrolled' if finish.name == 'hs_latest_sequence_unenrolled_date' else 'finished'} {iso_utc(finish.timestamp)}"
                if finish
                else ("still enrolled" if current and str(current.get("sequenceId")) == begin.value else "end not recorded")
            )
            name = f" '{current.get('sequenceName')}'" if current and str(current.get("sequenceId")) == begin.value else ""
            line = f"  sequence {begin.value}{name} | enrolled {iso_utc(begin.timestamp)} | {state} | {len(steps)} email(s) sent"
            line += f" | replied {iso_utc(replied[0].timestamp)}" if replied else " | no reply"
            lines.append(line)
            for step, e in enumerate(steps, 1):
                opened = next((v for n, v in e.details if n == "opens"), "0")
                lines.append(f"    email {step}: {iso_utc(e.timestamp)} | '{one_line(e.subject, 120)}' | opens {opened} | email id {e.id}")
        if not starts and not current:
            lines.append("No sequence enrollment is recorded on this contact.")
        lines.append(
            "HubSpot's API does not expose the step a contact is on nor why it was unenrolled: the emails a sequence "
            "sent are its steps, and a reply or a booked meeting is what usually ends an enrollment."
        )
        if notes:
            lines.append("Notes: " + "; ".join(notes))
        return "\n".join(lines)

    # ── Tool: inbound engagement report ─────────────────────────────────

    async def get_hubspot_inbound_engagement_report(
        self,
        from_date: str,
        to_date: str = "",
        forms: list[str] | str = "",
        group_by: str = "form",
        region_property: str = "",
        detail: str = "untouched",
        max_contacts: int = 1000,
    ) -> str | ToolResult:
        """Who submitted, who was touched, how fast, how, and what came of it."""
        start, end, bounds_err = _bounds(from_date, to_date)
        if bounds_err:
            return _refuse(bounds_err)
        if start is None:
            return _refuse("Error: pass from_date (the start of the submission range).")
        now = datetime.now(UTC)
        end = end or now
        grouping = str(group_by or "form").strip().lower()
        if grouping not in REPORT_GROUPS:
            return _refuse(f"Error: group_by must be one of {', '.join(REPORT_GROUPS)}.")
        level = str(detail or "untouched").strip().lower()
        if level not in ("summary", "untouched", "all"):
            return _refuse("Error: detail must be summary, untouched or all.")
        region_prop = str(region_property or "").strip()
        cap = max(1, min(_int(max_contacts, 1000), _REPORT_MAX_CONTACTS))
        deadline = time.monotonic() + _REPORT_SECONDS
        notes: list[str] = []
        properties = [*_REPORT_CONTACT_PROPERTIES, *([region_prop] if region_prop else [])]
        form_refs = split_names(forms)
        async with egress_client(timeout=60) as client:
            if form_refs:
                population = await self._inbound_from_forms(client, form_refs, start, end, cap, properties, deadline, notes)
            else:
                population = await self._inbound_from_conversions(client, start, end, cap, properties, notes)
            if isinstance(population, (str, ToolResult)):
                return population
            records, anchors, header = population
            if not anchors:
                return "\n".join([*header, "No inbound contact in that range."])
            engagements = await self._contact_engagements(client, list(anchors), deadline, notes)
            deals = await self._contact_deals(client, list(anchors), deadline, notes)
            await self.owners(client)
            if region_prop:
                await self.prepare_labels(client, "contacts", [record_properties(r) for r in records.values()], [region_prop])
        contacts = []
        for cid, (submitted_at, form) in anchors.items():
            p = record_properties(records.get(cid, {}))
            owner_id = p.get("hubspot_owner_id", "")
            if region_prop:
                region = self.label("contacts", region_prop, p.get(region_prop, ""))
            elif owner_id:
                teams = self.owner_teams(owner_id)
                region = teams[0][1] if teams else "(owner without team)"
            else:
                region = "(no owner)"
            contacts.append(
                InboundContact(
                    contact_id=cid,
                    name=f"{p.get('firstname', '')} {p.get('lastname', '')}".strip(),
                    email=p.get("email", ""),
                    company=p.get("company", ""),
                    country=p.get("country") or p.get("ip_country", ""),
                    region=region,
                    owner_id=owner_id,
                    owner=self.owner_label(owner_id) if owner_id else "",
                    form=form,
                    submitted_at=submitted_at,
                    assigned_at=parse_hubspot_time(p.get("hubspot_owner_assigneddate")),
                    engagements=engagements.get(cid, []),
                    deals_created=deals.get(cid, []),
                    sequence_enrolled_at=parse_hubspot_time(p.get("hs_latest_sequence_enrolled_date")),
                    replied_at=parse_hubspot_time(p.get("hs_sales_email_last_replied")),
                )
            )
        outcomes = [assess_inbound(c) for c in contacts]
        total, groups = aggregate_inbound(outcomes, grouping)
        lines = [
            f"Inbound engagement report: submissions from {iso_utc(start)} to {iso_utc(end)} (UTC), by {grouping}.",
            *header,
            format_group(total),
            f"By {grouping}:",
            *("  " + format_group(g) for g in groups),
        ]
        untouched = sorted((o for o in outcomes if not o.first_touch), key=lambda o: o.contact.submitted_at or now)
        if level in ("untouched", "all"):
            lines.append(f"Untouched contacts ({len(untouched)}), oldest submission first:")
            lines.extend("  " + format_outcome(o, now=now) for o in untouched[:_REPORT_MAX_LISTED])
            if len(untouched) > _REPORT_MAX_LISTED:
                lines.append(f"  ({len(untouched) - _REPORT_MAX_LISTED} more untouched contacts)")
        if level == "all":
            lines.append("Every contact, by submission time:")
            ordered = sorted(outcomes, key=lambda o: o.contact.submitted_at or now)
            lines.extend("  " + format_outcome(o, now=now) for o in ordered)
        lines.append(_REPORT_METHOD)
        if notes:
            lines.append("Notes: " + "; ".join(notes))
        return "\n".join(lines)

    async def _inbound_from_forms(
        self,
        client: httpx.AsyncClient,
        refs: Sequence[str],
        start: datetime,
        end: datetime,
        cap: int,
        properties: Sequence[str],
        deadline: float,
        notes: list[str],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[datetime | None, str]], list[str]] | str | ToolResult:
        selected: dict[str, str] = {}
        for ref in refs:
            matches, err = await self.match_forms(client, ref)
            if err:
                return _refuse(err) if err.startswith("Error: no HubSpot form") else err
            selected.update(dict(matches))
        submissions: list[FormSubmission] = []
        for form_id, name in selected.items():
            got, cursor, err = await self.form_submissions(
                client, form_id, name, start=start, end=end, limit=cap * 5, deadline=deadline
            )
            submissions.extend(got)
            if err:
                notes.append(f"form '{name}' partly read: {_first_line(err)}")
            elif cursor:
                notes.append(f"form '{name}': submissions read up to the limit or the time budget, not to from_date")
        contacts, err = await self.contacts_by_email(client, [s.email for s in submissions if s.email], properties)
        if err:
            notes.append(f"contacts partly matched: {_first_line(err)}")
        anchors: dict[str, tuple[datetime | None, str]] = {}
        records: dict[str, dict[str, Any]] = {}
        unmatched = 0
        beyond_cap: set[str] = set()
        for s in sorted(submissions, key=lambda s: s.timestamp or end):
            contact = contacts.get(s.email)
            if not contact:
                unmatched += 1
                continue
            cid = str(contact.get("id"))
            if cid in anchors:
                continue
            if len(anchors) >= cap:
                beyond_cap.add(cid)
                continue
            records[cid] = contact
            anchors[cid] = (s.timestamp, s.form_name)
        header = [
            "Forms: " + ", ".join(f"'{name}'" for name in selected.values()) + ".",
            f"Submissions in range: {len(submissions)}; contacts: {len(anchors)} (a contact counts once, at its first "
            f"submission in range); submissions whose email matches no contact: {unmatched}.",
        ]
        if beyond_cap:
            notes.append(f"{len(beyond_cap)} later contacts not reported: raise max_contacts (max {_REPORT_MAX_CONTACTS})")
        return records, anchors, header

    async def _inbound_from_conversions(
        self,
        client: httpx.AsyncClient,
        start: datetime,
        end: datetime,
        cap: int,
        properties: Sequence[str],
        notes: list[str],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[datetime | None, str]], list[str]] | str:
        window = {"operator": "BETWEEN", "value": str(epoch_ms(start)), "highValue": str(epoch_ms(end))}
        body = self._search_body(
            properties=list(properties),
            filters_json=[
                {"filters": [{"propertyName": "first_conversion_date", **window}]},
                {"filters": [{"propertyName": "recent_conversion_date", **window}]},
            ],
            sort_by="createdate",
            sort_direction="ASCENDING",
            count=SEARCH_PAGE,
        )
        results, total, _after, err = await self.search_pages(
            client, "contacts", body, cap, action="search the contacts converted in range", cap=_REPORT_MAX_CONTACTS
        )
        if err and not results:
            return err
        if err:
            notes.append(f"contact search stopped early: {_first_line(err)}")
        if total > len(results):
            notes.append(f"{total - len(results)} more contacts converted in range: raise max_contacts or narrow the range")
        anchors: dict[str, tuple[datetime | None, str]] = {}
        records: dict[str, dict[str, Any]] = {}
        for contact in results:
            p = record_properties(contact)
            first = parse_hubspot_time(p.get("first_conversion_date"))
            recent = parse_hubspot_time(p.get("recent_conversion_date"))
            if within(first, start, end):
                anchor = (first, p.get("first_conversion_event_name", ""))
            elif within(recent, start, end):
                anchor = (recent, p.get("recent_conversion_event_name", ""))
            else:
                continue
            cid = str(contact.get("id"))
            records[cid] = contact
            anchors[cid] = anchor
        header = [
            "Forms: every form conversion (pass forms to read named forms' submissions instead).",
            f"Contacts converted in range: {len(anchors)} (a contact counts once, at its first conversion in range).",
        ]
        return records, anchors, header

    async def _contact_engagements(
        self, client: httpx.AsyncClient, contact_ids: Sequence[str], deadline: float, notes: list[str]
    ) -> dict[str, list[Engagement]]:
        out: dict[str, list[Engagement]] = defaultdict(list)
        budget = _REPORT_MAX_ENGAGEMENTS
        for type_, properties in _REPORT_ENGAGEMENT_PROPERTIES.items():
            if time.monotonic() > deadline:
                notes.append(f"time budget reached: {type_} and later types not read")
                break
            mapping, cut, err = await self.batch_association_ids(client, "contacts", contact_ids, type_)
            if err:
                notes.append(f"{type_} not read: {_first_line(err)}")
                if not mapping:
                    continue
            if cut:
                notes.append(f"some contacts have more than 500 {type_}: the first 500 are read")
            ids = list(dict.fromkeys(i for targets in mapping.values() for i, _ in targets))
            if len(ids) > budget:
                notes.append(f"{len(ids) - budget} {type_} not read (engagement budget)")
                ids = ids[:budget]
            budget -= len(ids)
            engagements, err = await self.read_engagements(client, type_, ids, properties=properties)
            if err:
                notes.append(f"{type_} partly read: {_first_line(err)}")
            by_id = {e.id: e for e in engagements}
            for contact_id, targets in mapping.items():
                out[contact_id].extend(by_id[i] for i, _ in targets if i in by_id)
        return out

    async def _contact_deals(
        self, client: httpx.AsyncClient, contact_ids: Sequence[str], deadline: float, notes: list[str]
    ) -> dict[str, list[datetime]]:
        out: dict[str, list[datetime]] = defaultdict(list)
        if time.monotonic() > deadline:
            notes.append("time budget reached: deals not read")
            return out
        mapping, _cut, err = await self.batch_association_ids(client, "contacts", contact_ids, "deals")
        if err:
            notes.append(f"deals not read: {_first_line(err)}")
        deal_ids = list(dict.fromkeys(i for targets in mapping.values() for i, _ in targets))
        created: dict[str, datetime] = {}
        for chunk in _chunks(deal_ids, BATCH_SIZE):
            data, err = await self._post(
                client,
                "/crm/v3/objects/deals/batch/read",
                f"read {len(chunk)} deals",
                {"properties": ["createdate"], "inputs": [{"id": i} for i in chunk]},
            )
            if err:
                notes.append(f"deals partly read: {_first_line(err)}")
                break
            for deal in _results(data):
                moment = parse_hubspot_time(record_properties(deal).get("createdate"))
                if moment:
                    created[str(deal.get("id"))] = moment
        for contact_id, targets in mapping.items():
            out[contact_id].extend(created[i] for i, _ in targets if i in created)
        return out

    # ── Registration ────────────────────────────────────────────────────

    def handlers(self) -> dict[str, Callable[..., Awaitable[Any]]]:
        return {
            "get_hubspot_engagement": self.get_hubspot_engagement,
            "search_hubspot_engagements": self.search_hubspot_engagements,
            "get_hubspot_record_timeline": self.get_hubspot_record_timeline,
            "list_hubspot_forms": self.list_hubspot_forms,
            "list_hubspot_form_submissions": self.list_hubspot_form_submissions,
            "get_hubspot_contact_form_submissions": self.get_hubspot_contact_form_submissions,
            "list_hubspot_properties": self.list_hubspot_properties,
            "batch_get_hubspot_records": self.batch_get_hubspot_records,
            "batch_get_hubspot_associations": self.batch_get_hubspot_associations,
            "list_hubspot_teams": self.list_hubspot_teams,
            "list_hubspot_sequences": self.list_hubspot_sequences,
            "get_hubspot_sequence_enrollments": self.get_hubspot_sequence_enrollments,
            "get_hubspot_inbound_engagement_report": self.get_hubspot_inbound_engagement_report,
        }


def _same_submission(recorded: FormSubmission, candidate: FormSubmission) -> bool:
    """Whether *candidate* (a form's submission) is the submission *recorded* on a contact."""
    if recorded.conversion_id and candidate.conversion_id:
        return recorded.conversion_id == candidate.conversion_id
    if not recorded.email or recorded.email != candidate.email:
        return False
    if recorded.timestamp is None or candidate.timestamp is None:
        return False
    return abs((recorded.timestamp - candidate.timestamp).total_seconds()) <= _SUBMISSION_MATCH_SECONDS


_ENGAGEMENT_TYPE_ENUM = list(ENGAGEMENT_TYPES)
_DATE_HELP = "a date (YYYY-MM-DD, a whole UTC day), an ISO-8601 time or epoch milliseconds"

#: The most extra properties one timeline reads the history of.
_MAX_HISTORY_PROPERTIES = 50
_PROPERTY_NAME = re.compile(r"^[a-z0-9_]+$")


def _history_property_names(raw: str) -> tuple[list[str], str]:
    """The extra property names a timeline reads the history of, or a refusal."""
    names = list(dict.fromkeys(n.strip().lower() for n in str(raw or "").split(",") if n.strip()))
    bad = [n for n in names if not _PROPERTY_NAME.match(n)]
    if bad:
        return [], f"Error: invalid property name(s): {', '.join(bad)}. Pass internal names (list_hubspot_properties)."
    if len(names) > _MAX_HISTORY_PROPERTIES:
        return [], f"Error: {len(names)} properties passed; at most {_MAX_HISTORY_PROPERTIES} per call."
    return names, ""


def activity_definitions(source: str) -> list[ToolDefinition]:
    """The activity tools' definitions (``hubspot.build_tools`` adds their scopes)."""
    return [
        ToolDefinition(
            name="get_hubspot_record_timeline",
            description=(
                "The activity feed of one HubSpot record (contact, company, deal, ticket) in one call, in chronological "
                "order: every email (sender, recipients, subject, opens, sequence or manual), call (direction, outcome, "
                "duration), meeting (start, outcome, booked by the contact or not), note, task and LinkedIn / SMS / "
                "WhatsApp message, each with its time, owner (name and email) and body (HTML stripped, cut at "
                "body_chars); for a contact also its form submissions (form, page, submitted values) and sequence "
                "enrollments; and the changes of lifecycle stage, owner and lead status (deal stage, ticket stage) "
                "with who made them, plus any properties named. Restrict with from_date / to_date and types. A type "
                "the private app cannot read is named with the scope it misses; the rest is still returned."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": {"type": "string", "description": "contacts, companies, deals or tickets"},
                    "record_id": {"type": "string", "description": "Record ID"},
                    "from_date": {"type": "string", "description": f"Start of the range: {_DATE_HELP}", "default": ""},
                    "to_date": {"type": "string", "description": f"End of the range: {_DATE_HELP}", "default": ""},
                    "types": {
                        "type": "string",
                        "description": (
                            "Comma-separated sources (default all): emails, calls, meetings, notes, tasks, "
                            "communications, form_submissions, sequences, property_changes"
                        ),
                        "default": "",
                    },
                    "limit": {"type": "integer", "description": "Max events shown (default 100, max 1000)", "default": 100},
                    "order": {
                        "type": "string",
                        "enum": ["asc", "desc"],
                        "description": "asc: oldest first (default); desc: newest first",
                        "default": "asc",
                    },
                    "body_chars": {
                        "type": "integer",
                        "description": "Characters of each body shown (default 400, 0 for none, max 20000)",
                        "default": 400,
                    },
                    "properties": {
                        "type": "string",
                        "description": "Comma-separated extra properties whose change history is shown (max 50)",
                        "default": "",
                    },
                },
                "required": ["object_type", "record_id"],
            },
            source=source,
            priority=0,
        ),
        ToolDefinition(
            name="get_hubspot_engagement",
            description=(
                "Read one HubSpot engagement (email, note, call, meeting, task or LinkedIn / SMS / WhatsApp "
                "communication) in full: time, direction, owner (name and email), sender and recipients, subject, "
                "body (HTML stripped, up to max_body_chars), status and outcome labels, attachment file IDs and the "
                "contacts, companies, deals and tickets it is associated with."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "engagement_type": {"type": "string", "enum": _ENGAGEMENT_TYPE_ENUM, "description": "Engagement type"},
                    "engagement_id": {"type": "string", "description": "Engagement ID"},
                    "properties": {
                        "type": "string",
                        "description": "Comma-separated extra properties to include (list_hubspot_properties lists them)",
                        "default": "",
                    },
                    "max_body_chars": {
                        "type": "integer",
                        "description": "Characters of the body shown (default 20000, max 100000)",
                        "default": 20000,
                    },
                },
                "required": ["engagement_type", "engagement_id"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="search_hubspot_engagements",
            description=(
                "Search one engagement type: e.g. every email associated with contact X between two dates, the calls "
                "of a deal, notes containing a word. Filters: the associated record, a time range on the engagement "
                "time, free text, property filters (filters_json, as in search_hubspot_contacts). Sorted by time. "
                "Each result is readable (time, direction, owner, subject, body preview), up to 500 per call."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "engagement_type": {"type": "string", "enum": _ENGAGEMENT_TYPE_ENUM, "description": "Engagement type"},
                    "associated_object_type": {
                        "type": "string",
                        "description": "Type of the record the engagements are associated with: contacts, companies, deals or tickets",
                        "default": "",
                    },
                    "associated_object_id": {"type": "string", "description": "ID of that record", "default": ""},
                    "from_date": {"type": "string", "description": f"Start of the range: {_DATE_HELP}", "default": ""},
                    "to_date": {"type": "string", "description": f"End of the range: {_DATE_HELP}", "default": ""},
                    "query": {"type": "string", "description": "Free-text search", "default": ""},
                    "filters_json": {
                        "type": "string",
                        "description": 'JSON array of property filters, e.g. [{"propertyName": "hs_email_direction", "operator": "EQ", "value": "INCOMING_EMAIL"}]',
                        "default": "",
                    },
                    "sort_direction": {
                        "type": "string",
                        "enum": ["ASCENDING", "DESCENDING"],
                        "description": "By engagement time (default newest first)",
                        "default": "DESCENDING",
                    },
                    "count": {"type": "integer", "description": "Max results (default 50, max 500; paged 200 per request)", "default": 50},
                    "after": {"type": "string", "description": "Pagination cursor from a previous call", "default": ""},
                    "properties": {"type": "string", "description": "Comma-separated extra properties to include", "default": ""},
                    "max_body_chars": {
                        "type": "integer",
                        "description": "Characters of each body shown (default 300, 0 for none)",
                        "default": 300,
                    },
                },
                "required": ["engagement_type"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="list_hubspot_forms",
            description="List the HubSpot account's forms (name, form ID, type), optionally filtered by a name substring.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Text the form name contains", "default": ""},
                    "count": {"type": "integer", "description": "Max forms (default 100)", "default": 100},
                },
                "required": [],
            },
            source=source,
            priority=2,
        ),
        ToolDefinition(
            name="list_hubspot_form_submissions",
            description=(
                "List the submissions of one HubSpot form, newest first, optionally between two dates: submission "
                "time, page URL, every submitted value, and the contact ID matched by the submitted email. Up to 500 "
                "per call in pages of 50; pass after to continue."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "form_id": {"type": "string", "description": "Form ID, or its exact name"},
                    "from_date": {"type": "string", "description": f"Oldest submission time: {_DATE_HELP}", "default": ""},
                    "to_date": {"type": "string", "description": f"Newest submission time: {_DATE_HELP}", "default": ""},
                    "count": {"type": "integer", "description": "Max submissions (default 50, max 500)", "default": 50},
                    "after": {"type": "string", "description": "Pagination cursor from a previous call", "default": ""},
                    "resolve_contacts": {
                        "type": "boolean",
                        "description": "Match each submission to its contact by email (default true)",
                        "default": True,
                    },
                    "value_chars": {"type": "integer", "description": "Characters of each value shown (default 300)", "default": 300},
                },
                "required": ["form_id"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="get_hubspot_contact_form_submissions",
            description=(
                "Every form a HubSpot contact submitted, oldest first (not only the latest conversion): form name and "
                "ID, time, page URL and title, and the submitted values."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "contact_id": {"type": "string", "description": "HubSpot contact ID"},
                    "include_values": {
                        "type": "boolean",
                        "description": "Look up the submitted values in each form's submissions (default true)",
                        "default": True,
                    },
                    "value_chars": {"type": "integer", "description": "Characters of each value shown (default 300)", "default": 300},
                },
                "required": ["contact_id"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="list_hubspot_properties",
            description=(
                "List the property definitions of a HubSpot object type (contacts, companies, deals, tickets, emails, "
                "calls...): internal name, label, type, group and, for an enumeration, its options (label=value). "
                "Use it to find the names to pass to the properties argument of the search, get and batch tools."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": {"type": "string", "description": "Object type"},
                    "query": {"type": "string", "description": "Text the name or label contains", "default": ""},
                    "include_options": {"type": "boolean", "description": "Show enumeration options (default true)", "default": True},
                    "count": {"type": "integer", "description": "Max properties (default 200)", "default": 200},
                },
                "required": ["object_type"],
            },
            source=source,
            priority=2,
        ),
        ToolDefinition(
            name="batch_get_hubspot_records",
            description=(
                "Read up to 500 HubSpot records of one type by ID in one call (batch endpoint, 100 per request): "
                "contacts, companies, deals, tickets, or engagements (emails, calls, meetings, notes, tasks, "
                "communications, returned readable). Enumerations and owners are shown with their labels unless "
                "raw_values. id_property reads by another unique property (e.g. email for contacts)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": {"type": "string", "description": "Object type"},
                    "ids": {"type": "array", "items": {"type": "string"}, "description": "Record IDs (max 500)"},
                    "properties": {"type": "string", "description": "Comma-separated extra properties to include", "default": ""},
                    "id_property": {"type": "string", "description": "Unique property the ids are values of (default: record ID)", "default": ""},
                    "raw_values": {"type": "boolean", "description": "Show raw values instead of labels", "default": False},
                },
                "required": ["object_type", "ids"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="batch_get_hubspot_associations",
            description=(
                "The associations of up to 1000 HubSpot records to another object type in one call (batch endpoint, "
                "100 per request): e.g. the companies of 300 contacts, or the emails of 300 contacts. Engagements are "
                "read and summarised (time, type, direction, owner, subject), newest first."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": {"type": "string", "description": "Source object type"},
                    "ids": {"type": "array", "items": {"type": "string"}, "description": "Source record IDs (max 1000)"},
                    "to_object_type": {"type": "string", "description": "Target object type (a record or engagement type)"},
                    "include_details": {
                        "type": "boolean",
                        "description": "Read and summarise engagement targets (default true)",
                        "default": True,
                    },
                },
                "required": ["object_type", "ids", "to_object_type"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="list_hubspot_teams",
            description="List the HubSpot account's teams with their members (owner name, email and owner ID) and secondary members.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=source,
            priority=2,
        ),
        ToolDefinition(
            name="list_hubspot_sequences",
            description=(
                "List HubSpot sales sequences (name, ID, owner, dates): those of one user, or of every active owner "
                "when user is empty."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user": {"type": "string", "description": "Owner ID, user ID or email of the owner (default: everyone)", "default": ""},
                    "query": {"type": "string", "description": "Text the sequence name contains", "default": ""},
                    "count": {"type": "integer", "description": "Max sequences (default 100)", "default": 100},
                },
                "required": [],
            },
            source=source,
            priority=2,
        ),
        ToolDefinition(
            name="get_hubspot_sequence_enrollments",
            description=(
                "A HubSpot contact's sequence enrollments: the one it is in now (sequence, since when, by whom), each "
                "past enrollment with its end (unenrolled or finished), the emails each sent (time, subject, opens) "
                "and the first reply."
            ),
            input_schema={
                "type": "object",
                "properties": {"contact_id": {"type": "string", "description": "HubSpot contact ID"}},
                "required": ["contact_id"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="get_hubspot_inbound_engagement_report",
            description=(
                "Audit inbound follow-up from HubSpot alone, computed server-side in one call: every contact that "
                "submitted the named forms (or, with no forms, made any form conversion) in the range, then per "
                "region, country, owner or form: submitted, assigned, touched (outgoing email, sequence email, call, "
                "meeting, LinkedIn / SMS / WhatsApp), touched within 24 h, first-touch delay (median, mean), first "
                "touch channel, replied, meeting booked and held, deal created; plus the untouched contacts (detail="
                "'untouched', default) or every contact's line (detail='all'). Region is the owner's primary team "
                "unless region_property names a contact property. Reads up to 2000 contacts within 4 minutes; "
                "anything cut short is stated."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "from_date": {"type": "string", "description": f"Start of the submission range: {_DATE_HELP}"},
                    "to_date": {"type": "string", "description": f"End of the range (default now): {_DATE_HELP}", "default": ""},
                    "forms": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Form IDs or names (a name matches every form containing it); empty: every conversion",
                        "default": [],
                    },
                    "group_by": {"type": "string", "enum": list(REPORT_GROUPS), "description": "Grouping (default form)", "default": "form"},
                    "region_property": {
                        "type": "string",
                        "description": "Contact property holding the region (default: the owner's primary team)",
                        "default": "",
                    },
                    "detail": {
                        "type": "string",
                        "enum": ["summary", "untouched", "all"],
                        "description": "summary: aggregates; untouched (default): plus the untouched contacts; all: plus every contact",
                        "default": "untouched",
                    },
                    "max_contacts": {"type": "integer", "description": "Max contacts (default 1000, max 2000)", "default": 1000},
                },
                "required": ["from_date"],
            },
            source=source,
            priority=1,
        ),
    ]

