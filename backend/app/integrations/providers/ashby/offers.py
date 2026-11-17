"""Offers: listing and reading them, and the start -> fill -> create flow.

Ashby creates an offer in three steps: ``offerProcess.start`` opens an offer
process on the application, ``offer.start`` returns the offer form to fill
(its id and fields), and ``offer.create`` submits the filled form.
``start_ashby_offer`` runs the first two and shows the fields;
``create_ashby_offer`` submits them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    CURSOR_SCHEMA,
    DEFAULT_PAGE,
    SINCE,
    AshbyContext,
    build_field_submissions,
    clamp,
    custom_field_lines,
    definition,
    describe_form,
    file_handle_list,
    id_list,
    id_schema,
    limit_schema,
    money,
    page_footer,
    parse_json_arg,
    set_if,
    to_epoch_ms,
    ts,
    user_label,
    warnings_note,
)

OFFER_STATUSES = [
    "WaitingOnOfferCreation",
    "WaitingOnApprovalStart",
    "WaitingOnOfferApproval",
    "WaitingOnApprovalDefinition",
    "WaitingOnCandidateResponse",
    "CandidateRejected",
    "CandidateAccepted",
    "OfferCancelled",
]
ACCEPTANCE_STATUSES = ["Accepted", "Declined", "Pending", "Created", "Cancelled"]
APPROVAL_STATUSES = ["Approved", "WaitingOnApprovals", "Declined"]
_OFFER_ACTIONS = ["set_status", "set_decided_at", "start_approval", "update"]


def _enum_list(value: str, allowed: list[str], name: str) -> tuple[list[str], str | None]:
    picked = []
    for item in id_list(value):
        match = {a.lower(): a for a in allowed}.get(item.lower())
        if not match:
            return [], f"Error: {name} must be among {', '.join(allowed)}."
        picked.append(match)
    return picked, None


def fmt_offer(o: dict[str, Any], applications: dict[str, dict[str, Any]]) -> str:
    lines = [
        f"**Offer** (ID: {o.get('id', '?')}) - Status: {o.get('offerStatus') or '?'} | "
        f"Acceptance: {o.get('acceptanceStatus') or '?'}"
    ]
    application_id = o.get("applicationId")
    application = applications.get(application_id) or {}
    who = [f"Application: {application_id or '?'}"]
    candidate = application.get("candidate") or {}
    if candidate:
        who.insert(0, f"Candidate: {candidate.get('name') or 'Unnamed'} (ID: {candidate.get('id', '?')})")
    job = application.get("job") or {}
    if job:
        who.append(f"Job: {job.get('title') or 'Untitled'} (ID: {job.get('id', '?')})")
    lines.append("  " + " | ".join(who))
    version = o.get("latestVersion") or {}
    if version:
        facts = []
        if version.get("startDate"):
            facts.append(f"start date {version['startDate']}")
        if version.get("salary"):
            facts.append(f"salary {money(version['salary'])}")
        if version.get("approvalStatus"):
            facts.append(f"approval {version['approvalStatus']}")
        if version.get("openingId"):
            facts.append(f"opening {version['openingId']}")
        lines.append(f"  Latest version (ID: {version.get('id', '?')}): " + (" | ".join(facts) or "no terms set"))
        if version.get("author"):
            lines.append(f"    Author: {user_label(version['author'])} | Created: {ts(version.get('createdAt'))}")
        lines.extend(custom_field_lines(version.get("customFields"), prefix="    "))
        files = file_handle_list(version.get("fileHandles"))
        if files:
            lines.append(f"    Files: {files}")
    versions = [v for v in o.get("versions") or [] if isinstance(v, dict)]
    meta = [f"Versions: {len(versions)}"] if versions else []
    if o.get("decidedAt"):
        meta.append(f"Decided: {ts(o['decidedAt'])}")
    if meta:
        lines.append("  " + " | ".join(meta))
    return "\n".join(lines)


def register(ctx: AshbyContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    async def list_ashby_offers(
        application_id: str = "",
        offer_status: str = "",
        acceptance_status: str = "",
        approval_status: str = "",
        created_after: str = "",
        limit: int = DEFAULT_PAGE,
        cursor: str = "",
    ) -> str:
        body: dict[str, Any] = {}
        set_if(body, "applicationId", application_id)
        for key, value, allowed, name in (
            ("offerStatus", offer_status, OFFER_STATUSES, "offer_status"),
            ("acceptanceStatus", acceptance_status, ACCEPTANCE_STATUSES, "acceptance_status"),
            ("approvalStatus", approval_status, APPROVAL_STATUSES, "approval_status"),
        ):
            picked, err = _enum_list(value, allowed, name)
            if err:
                return err
            if picked:
                body[key] = picked
        ms, err = to_epoch_ms(created_after, "created_after")
        if err:
            return err
        set_if(body, "createdAfter", ms)
        async with ctx.client() as client:
            page, err = await ctx.page(client, "offer.list", body, limit=clamp(limit), cursor=cursor)
            if err:
                return err
            applications = await ctx.applications(client, (o.get("applicationId") for o in page.items))
        if not page.items:
            return "No offers match." + page_footer(page, noun="offer")
        lines = [f"Found {len(page.items)} offer{'s' if len(page.items) != 1 else ''}:"]
        lines.extend(fmt_offer(o, applications) for o in page.items)
        return "\n".join(lines) + page_footer(page, noun="offer")

    async def get_ashby_offer(offer_id: str, include_signed_documents: bool = False) -> str:
        body: dict[str, Any] = {"offerId": offer_id, "excludeFormDefinition": True}
        if include_signed_documents:
            body["expand"] = {"individualSignedPacketDocuments": True}
        async with ctx.client() as client:
            offer, err = await ctx.call(client, "offer.info", body)
            if err:
                return err
            if not isinstance(offer, dict) or not offer.get("id"):
                return f"Error: Ashby returned no offer for {offer_id}."
            applications = await ctx.applications(client, [offer.get("applicationId")])
        return fmt_offer(offer, applications)

    async def start_ashby_offer(application_id: str) -> str:
        async with ctx.client() as client:
            process, err = await ctx.call(client, "offerProcess.start", {"applicationId": application_id})
            if err:
                return err
            process_id = (process or {}).get("id") if isinstance(process, dict) else None
            if not process_id:
                return f"Error: Ashby started no offer process on application {application_id}."
            form, err = await ctx.call(client, "offer.start", {"offerProcessId": process_id})
            if err:
                return f"{err}\n(The offer process {process_id} was opened.)"
        form = form if isinstance(form, dict) else {}
        form_id = form.get("id", "")
        ctx.remember_form(form_id, form.get("formDefinition"))
        return (
            f"Offer started on application {application_id}.\n"
            f"  offer_process_id: {process_id}\n  offer_form_id: {form_id}\n"
            "Fill these fields with create_ashby_offer (field_values: JSON object of path or "
            "title -> value):\n" + describe_form(form.get("formDefinition"))
        )

    async def create_ashby_offer(offer_process_id: str, offer_form_id: str, field_values: Any = "") -> str:
        values, err = parse_json_arg(field_values, "field_values")
        if err:
            return err
        if not values:
            return "Error: give field_values (start_ashby_offer lists the offer form's fields)."
        submissions, err = build_field_submissions(ctx.remembered_form(offer_form_id), values)
        if err:
            return err
        body = {
            "offerProcessId": offer_process_id,
            "offerFormId": offer_form_id,
            "offerForm": {"fieldSubmissions": submissions},
            "excludeFormDefinition": True,
        }
        async with ctx.client() as client:
            data, err = await ctx.post(client, "offer.create", body)
            if err:
                return err
            offer = data.get("results") if isinstance(data.get("results"), dict) else {}
            applications = await ctx.applications(client, [offer.get("applicationId")])
        return "Offer created:\n" + fmt_offer(offer, applications) + warnings_note(data)

    async def manage_ashby_offer(
        action: str,
        offer_id: str,
        acceptance_status: str = "",
        decided_at: str = "",
        offer_version_id: str = "",
        note: str = "",
        field_values: Any = "",
    ) -> str:
        action = (action or "").strip().lower()
        if action not in _OFFER_ACTIONS:
            return f"Error: action must be one of {', '.join(_OFFER_ACTIONS)}."
        async with ctx.client() as client:
            if action == "set_status":
                status = {s.lower(): s for s in ("Accepted", "Declined", "Cancelled")}.get(
                    acceptance_status.strip().lower()
                )
                if not status:
                    return "Error: acceptance_status must be Accepted, Declined or Cancelled."
                endpoint, body = "offer.setStatus", {"offerId": offer_id, "acceptanceStatus": status}
            elif action == "set_decided_at":
                ms, err = to_epoch_ms(decided_at, "decided_at")
                if err or ms is None:
                    return err or "Error: give decided_at (when the candidate accepted or declined)."
                when = datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
                endpoint, body = "offer.setDecidedAt", {"offerId": offer_id, "decidedAt": when}
            else:
                current, err = await ctx.call(client, "offer.info", {"offerId": offer_id})
                if err:
                    return err
                current = current if isinstance(current, dict) else {}
                if action == "start_approval":
                    version_id = offer_version_id or (current.get("latestVersion") or {}).get("id")
                    if not version_id:
                        return f"Error: offer {offer_id} has no version to send for approval."
                    body = {"offerVersionId": version_id}
                    set_if(body, "note", note)
                    endpoint = "offer.startApprovalProcess"
                else:
                    values, err = parse_json_arg(field_values, "field_values")
                    if err:
                        return err
                    if not values:
                        return "Error: give field_values. The offer form's fields:\n" + describe_form(
                            current.get("formDefinition")
                        )
                    submissions, err = build_field_submissions(current.get("formDefinition"), values)
                    if err:
                        return err
                    endpoint = "offer.update"
                    body = {"offerId": offer_id, "offerForm": {"fieldSubmissions": submissions}}
            body["excludeFormDefinition"] = True
            offer, err = await ctx.call(client, endpoint, body)
            if err:
                return err
            offer = offer if isinstance(offer, dict) else {"id": offer_id}
            applications = await ctx.applications(client, [offer.get("applicationId")])
        return f"Offer {action.replace('_', ' ')} done:\n" + fmt_offer(offer, applications)

    for fn in (list_ashby_offers, get_ashby_offer, start_ashby_offer, create_ashby_offer, manage_ashby_offer):
        executor.register(fn.__name__, fn)

    offer_id = id_schema("offer")
    statuses = "comma separated"
    return [
        definition(
            "list_ashby_offers",
            "List Ashby offers with their offer and acceptance status, candidate, job, and "
            "latest version (start date, salary, approval status, author, files). Filter by "
            "application, offer status, acceptance status, approval status, creation time.",
            {
                "application_id": id_schema("application", "only this application's offers"),
                "offer_status": {"type": "string", "description": f"{statuses}: {', '.join(OFFER_STATUSES)}"},
                "acceptance_status": {"type": "string", "description": f"{statuses}: {', '.join(ACCEPTANCE_STATUSES)}"},
                "approval_status": {"type": "string", "description": f"{statuses}: {', '.join(APPROVAL_STATUSES)}"},
                "created_after": {"type": "string", "description": f"Created at or after: {SINCE}"},
                "limit": limit_schema(),
                "cursor": CURSOR_SCHEMA,
            },
            [],
            0,
        ),
        definition(
            "get_ashby_offer",
            "Get one Ashby offer: status, acceptance, decision date, candidate and job, and its "
            "latest version (start date, salary, approval status, opening, custom fields, files).",
            {
                "offer_id": offer_id,
                "include_signed_documents": {
                    "type": "boolean",
                    "description": "List each signed packet document",
                    "default": False,
                },
            },
            ["offer_id"],
            0,
        ),
        definition(
            "start_ashby_offer",
            "Start an offer on an Ashby application: opens an offer process and returns the "
            "offer_process_id, the offer_form_id and the offer form's fields. Fill them with "
            "create_ashby_offer.",
            {"application_id": id_schema("application")},
            ["application_id"],
            1,
        ),
        definition(
            "create_ashby_offer",
            "Create the offer started by start_ashby_offer: field_values is a JSON object of "
            "field (path or title) -> value - a select option by label or value, a currency "
            'as {"value": 120000, "currencyCode": "EUR"}, a date as YYYY-MM-DD.',
            {
                "offer_process_id": id_schema("offer process", "from start_ashby_offer"),
                "offer_form_id": id_schema("offer form", "from start_ashby_offer"),
                "field_values": {"type": "string", "description": "JSON object of field -> value"},
            },
            ["offer_process_id", "offer_form_id", "field_values"],
            1,
        ),
        definition(
            "manage_ashby_offer",
            "Act on an Ashby offer: set_status (the candidate Accepted / Declined, or "
            "Cancelled), set_decided_at (when they decided), start_approval (send the latest "
            "or a given version to its approvers, with a note), update (change form values: "
            "field_values JSON; call without to see the fields).",
            {
                "action": {"type": "string", "enum": _OFFER_ACTIONS},
                "offer_id": offer_id,
                "acceptance_status": {"type": "string", "enum": ["Accepted", "Declined", "Cancelled"]},
                "decided_at": {"type": "string", "description": "ISO date-time of the decision"},
                "offer_version_id": id_schema("offer version", "start_approval; default the latest"),
                "note": {"type": "string", "description": "Note for the approvers"},
                "field_values": {"type": "string", "description": "update: JSON object of field -> value"},
            },
            ["action", "offer_id"],
            1,
        ),
    ]
