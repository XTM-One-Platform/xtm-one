"""Jobs, their public postings and their openings (headcount)."""

from __future__ import annotations

from typing import Any

from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    CURSOR_SCHEMA,
    DEFAULT_PAGE,
    SINCE,
    AshbyContext,
    clamp,
    custom_field_lines,
    definition,
    describe_form,
    form_fields,
    hiring_team_lines,
    id_list,
    id_schema,
    limit_schema,
    page_footer,
    set_if,
    strip_html,
    to_epoch_ms,
    truncate,
    ts,
    user_label,
)

JOB_STATUSES = ["Draft", "Open", "Closed", "Archived"]
EMPLOYMENT_TYPES = ["FullTime", "PartTime", "Intern", "Contract", "Temporary"]
WORKPLACE_TYPES = ["OnSite", "Hybrid", "Remote"]
OPENING_STATES = ["Draft", "Approved", "Open", "Closed"]
_OPENING_ACTIONS = [
    "create",
    "update",
    "set_state",
    "archive",
    "unarchive",
    "add_job",
    "remove_job",
    "add_location",
    "remove_location",
]
_MAX_POSTINGS = 200
_MAX_DESCRIPTION = 4000


def _named(record: Any, key: str = "name") -> str:
    if not isinstance(record, dict) or not record:
        return ""
    return f"{record.get(key) or record.get('title') or 'Unnamed'} (ID: {record.get('id', '?')})"


def _location_label(location: Any) -> str:
    if not isinstance(location, dict) or not location:
        return ""
    kind = f" [{location['workplaceType']}]" if location.get("workplaceType") else ""
    remote = " (remote)" if location.get("isRemote") and not kind else ""
    return f"{location.get('name') or 'Unnamed'}{kind}{remote} (ID: {location.get('id', '?')})"


def _compensation_lines(compensation: Any) -> list[str]:
    tiers = [t for t in (compensation or {}).get("compensationTiers") or [] if isinstance(t, dict)]
    lines = []
    for tier in tiers:
        summary = tier.get("tierSummary") or "; ".join(
            str(c.get("summary")) for c in tier.get("components") or [] if isinstance(c, dict) and c.get("summary")
        )
        title = f"{tier['title']}: " if tier.get("title") else ""
        if summary:
            lines.append(f"    - {title}{summary}")
    return ["  Compensation:", *lines] if lines else []


def fmt_job(
    j: dict[str, Any],
    *,
    departments: dict[str, dict[str, Any]],
    locations: dict[str, dict[str, Any]],
    detailed: bool = False,
) -> str:
    lines = [
        f"**{j.get('title') or 'Untitled job'}** (ID: {j.get('id', '?')}) - Status: {j.get('status') or 'unknown'}"
    ]
    facts = []
    department_id = j.get("departmentId")
    if department_id:
        department = departments.get(department_id)
        facts.append(f"Department: {_named(department) if department else department_id}")
    location = j.get("location") or locations.get(j.get("locationId") or "")
    if location:
        facts.append(f"Location: {_location_label(location)}")
    elif j.get("locationId"):
        facts.append(f"Location: {j['locationId']}")
    if j.get("employmentType"):
        facts.append(f"Employment: {j['employmentType']}")
    if j.get("customRequisitionId"):
        facts.append(f"Requisition: {j['customRequisitionId']}")
    if j.get("confidential"):
        facts.append("Confidential")
    if facts:
        lines.append("  " + " | ".join(facts))
    lines.extend(hiring_team_lines(j.get("hiringTeam")))
    dates = [
        f"{label}: {ts(j[key])}"
        for label, key in (("Created", "createdAt"), ("Opened", "openedAt"), ("Closed", "closedAt"))
        if j.get(key)
    ]
    if dates:
        lines.append("  " + " | ".join(dates))
    if not detailed:
        return "\n".join(lines)
    plans = [p for p in j.get("interviewPlanIds") or [] if p]
    if j.get("defaultInterviewPlanId") or plans:
        others = [p for p in plans if p != j.get("defaultInterviewPlanId")]
        lines.append(
            f"  Interview plan: {j.get('defaultInterviewPlanId') or 'none'} (default)"
            + (f"; also {', '.join(others)}" if others else "")
        )
    postings = [p for p in j.get("jobPostingIds") or [] if p]
    if postings:
        lines.append(f"  Job postings: {', '.join(postings)}")
    openings = [o for o in j.get("openings") or [] if isinstance(o, dict)]
    if openings:
        lines.append(f"  Openings ({len(openings)}):")
        for opening in openings:
            version = opening.get("latestVersion") or {}
            target = f", target hire {version['targetHireDate']}" if version.get("targetHireDate") else ""
            lines.append(
                f"    - {version.get('identifier') or 'opening'} [{opening.get('openingState') or '?'}{target}]"
                f" (ID: {opening.get('id', '?')})"
            )
    lines.extend(_compensation_lines(j.get("compensation")))
    lines.extend(custom_field_lines(j.get("customFields")))
    if j.get("author"):
        lines.append(f"  Author: {user_label(j['author'])}")
    if j.get("brandId"):
        lines.append(f"  Brand: {j['brandId']}")
    return "\n".join(lines)


def fmt_posting(p: dict[str, Any], *, detailed: bool = False) -> str:
    listed = "listed" if p.get("isListed") else "unlisted"
    lines = [
        f"**{p.get('title') or 'Untitled posting'}** (posting ID: {p.get('id', '?')}) - {p.get('status') or '?'}, {listed}"
    ]
    facts = [f"Job: {p['jobId']}"] if p.get("jobId") else []
    for label, key in (("Department", "departmentName"), ("Team", "teamName"), ("Location", "locationName")):
        if p.get(key):
            facts.append(f"{label}: {p[key]}")
    if p.get("workplaceType"):
        facts.append(f"Workplace: {p['workplaceType']}")
    if p.get("employmentType"):
        facts.append(f"Employment: {p['employmentType']}")
    if facts:
        lines.append("  " + " | ".join(facts))
    dates = [
        f"{label}: {ts(p[key])}"
        for label, key in (
            ("Published", "publishedDate"),
            ("Deadline", "applicationDeadline"),
            ("Updated", "updatedAt"),
        )
        if p.get(key)
    ]
    if dates:
        lines.append("  " + " | ".join(dates))
    links = [f"{label}: {p[key]}" for label, key in (("Apply", "applyLink"), ("Posting", "externalLink")) if p.get(key)]
    if links:
        lines.append("  " + " | ".join(links))
    compensation = p.get("compensationTierSummary") or (p.get("compensation") or {}).get("compensationTierSummary")
    if compensation:
        lines.append(f"  Compensation: {compensation}")
    if detailed:
        if p.get("locationAddress"):
            lines.append(f"  Address: {p['locationAddress']}")
        boards = [
            f"{b.get('jobBoardId')} ({'listed' if b.get('isListed') else 'unlisted'})"
            for b in p.get("jobBoardAssociations") or []
            if isinstance(b, dict)
        ]
        if boards:
            lines.append(f"  Job boards: {', '.join(boards)}")
        form = p.get("applicationFormDefinition")
        if form_fields(form):
            lines.append(f"  Application form ({p.get('applicationFormDefinitionId', '?')}):")
            lines.append(describe_form(form, "    "))
        description = p.get("descriptionPlain") or strip_html(p.get("descriptionHtml"))
        if description:
            lines.append("  Description:\n    " + truncate(description, _MAX_DESCRIPTION).replace("\n", "\n    "))
    return "\n".join(lines)


def fmt_opening(o: dict[str, Any]) -> str:
    version = o.get("latestVersion") or {}
    archived = " [archived]" if o.get("isArchived") else ""
    name = f"Opening {version['identifier']}" if version.get("identifier") else "Opening"
    lines = [f"**{name}** (ID: {o.get('id', '?')}) - State: {o.get('openingState') or '?'}{archived}"]
    facts = []
    if version.get("jobIds"):
        facts.append(f"Jobs: {', '.join(version['jobIds'])}")
    if version.get("locationIds"):
        facts.append(f"Locations: {', '.join(version['locationIds'])}")
    if version.get("teamId"):
        facts.append(f"Department: {version['teamId']}")
    if version.get("employmentType"):
        facts.append(f"Employment: {version['employmentType']}")
    if version.get("isBackfill"):
        facts.append("Backfill")
    if facts:
        lines.append("  " + " | ".join(facts))
    dates = [
        f"{label}: {value}"
        for label, value in (
            ("Target hire", version.get("targetHireDate")),
            ("Target start", version.get("targetStartDate")),
            ("Opened", ts(o.get("openedAt"))),
            ("Closed", ts(o.get("closedAt"))),
        )
        if value
    ]
    if dates:
        lines.append("  " + " | ".join(dates))
    if o.get("closeReasonId"):
        lines.append(f"  Close reason: {o['closeReasonId']}")
    lines.extend(hiring_team_lines(version.get("hiringTeam")))
    if version.get("description"):
        lines.append(f"  Description: {truncate(strip_html(version['description']), 500)}")
    lines.extend(custom_field_lines(version.get("customFields")))
    return "\n".join(lines)


def register(ctx: AshbyContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    async def _render_jobs(client: Any, jobs: list[dict[str, Any]], *, detailed: bool = False) -> list[str]:
        departments = await ctx.departments(client)
        locations = (
            {} if all(j.get("location") or not j.get("locationId") for j in jobs) else await ctx.locations(client)
        )
        return [fmt_job(j, departments=departments, locations=locations, detailed=detailed) for j in jobs]

    async def list_ashby_jobs(
        status: str = "",
        created_after: str = "",
        opened_after: str = "",
        opened_before: str = "",
        closed_after: str = "",
        closed_before: str = "",
        limit: int = DEFAULT_PAGE,
        cursor: str = "",
    ) -> str:
        body: dict[str, Any] = {"expand": ["location"]}
        statuses = []
        for value in id_list(status):
            match = {s.lower(): s for s in JOB_STATUSES}.get(value.lower())
            if not match:
                return f"Error: status must be among {', '.join(JOB_STATUSES)}."
            statuses.append(match)
        if statuses:
            body["status"] = statuses
        for key, value, name in (
            ("createdAfter", created_after, "created_after"),
            ("openedAfter", opened_after, "opened_after"),
            ("openedBefore", opened_before, "opened_before"),
            ("closedAfter", closed_after, "closed_after"),
            ("closedBefore", closed_before, "closed_before"),
        ):
            ms, err = to_epoch_ms(value, name)
            if err:
                return err
            set_if(body, key, ms)
        async with ctx.client() as client:
            page, err = await ctx.page(client, "job.list", body, limit=clamp(limit), cursor=cursor)
            if err:
                return err
            rendered = await _render_jobs(client, page.items)
        if not rendered:
            return f"No jobs{' with status ' + ', '.join(statuses) if statuses else ''}." + page_footer(
                page, noun="job"
            )
        return (
            f"Found {len(rendered)} job{'s' if len(rendered) != 1 else ''}:\n"
            + "\n".join(rendered)
            + page_footer(page, noun="job")
        )

    async def get_ashby_job(job_id: str, include_unpublished_postings: bool = False) -> str:
        body = {
            "id": job_id,
            "expand": ["location", "openings", "compensation"],
            "includeUnpublishedJobPostingsIds": bool(include_unpublished_postings),
        }
        async with ctx.client() as client:
            job, err = await ctx.call(client, "job.info", body)
            if err:
                return err
            if not isinstance(job, dict) or not job.get("id"):
                return f"Error: Ashby returned no job for {job_id}."
            return (await _render_jobs(client, [job], detailed=True))[0]

    async def search_ashby_jobs(
        title: str = "", requisition_id: str = "", term: str = "", limit: int = DEFAULT_PAGE
    ) -> str:
        title = title.strip() or term.strip()
        body: dict[str, Any] = {"limit": clamp(limit)}
        set_if(body, "title", title)
        set_if(body, "requisitionId", requisition_id)
        if len(body) == 1:
            return "Error: give a title (or part of one) or a requisition_id to search jobs."
        async with ctx.client() as client:
            jobs, err = await ctx.call(client, "job.search", body)
            if err:
                return err
            rendered = await _render_jobs(client, [j for j in jobs or [] if isinstance(j, dict)])
        if not rendered:
            return f"No Ashby job matches {title or requisition_id!r}."
        return f"Found {len(rendered)} job{'s' if len(rendered) != 1 else ''}:\n" + "\n".join(rendered)

    async def create_ashby_job(
        title: str,
        department_id: str,
        location_id: str,
        default_interview_plan_id: str = "",
        job_template_id: str = "",
        employment_type: str = "",
        brand_id: str = "",
    ) -> str:
        body: dict[str, Any] = {"title": title, "teamId": department_id, "locationId": location_id}
        set_if(body, "defaultInterviewPlanId", default_interview_plan_id)
        set_if(body, "jobTemplateId", job_template_id)
        set_if(body, "employmentType", employment_type)
        set_if(body, "brandId", brand_id)
        async with ctx.client() as client:
            job, err = await ctx.call(client, "job.create", body)
            if err:
                return err
            if not isinstance(job, dict):
                return f"Created job '{title}'."
            return (
                "Created job (in Draft until set_ashby_job_status opens it):\n" + (await _render_jobs(client, [job]))[0]
            )

    async def update_ashby_job(
        job_id: str,
        title: str = "",
        department_id: str = "",
        location_id: str = "",
        default_interview_plan_id: str = "",
        employment_type: str = "",
        custom_requisition_id: str = "",
    ) -> str:
        body: dict[str, Any] = {"jobId": job_id}
        set_if(body, "title", title)
        set_if(body, "teamId", department_id)
        set_if(body, "locationId", location_id)
        set_if(body, "defaultInterviewPlanId", default_interview_plan_id)
        set_if(body, "employmentType", employment_type)
        set_if(body, "customRequisitionId", custom_requisition_id)
        if len(body) == 1:
            return "Error: give at least one field to update."
        async with ctx.client() as client:
            job, err = await ctx.call(client, "job.update", body)
            if err:
                return err
            if not isinstance(job, dict):
                return f"Job {job_id} updated."
            return "Job updated:\n" + (await _render_jobs(client, [job]))[0]

    async def set_ashby_job_status(job_id: str, status: str, close_reason_id: str = "") -> str:
        match = {s.lower(): s for s in JOB_STATUSES}.get((status or "").strip().lower())
        if not match:
            return f"Error: status must be one of {', '.join(JOB_STATUSES)}."
        body: dict[str, Any] = {"jobId": job_id, "status": match}
        if close_reason_id and match == "Closed":
            body["closeReasonId"] = close_reason_id.strip()
        async with ctx.client() as client:
            _, err = await ctx.call(client, "job.setStatus", body)
        if err:
            return err
        return f"Job {job_id} is now {match}."

    async def list_ashby_job_postings(
        include_unpublished: bool = False,
        listed_only: bool = False,
        job_board_id: str = "",
        location: str = "",
        department: str = "",
        job_id: str = "",
    ) -> str:
        body: dict[str, Any] = {
            "includeUnpublishedJobPostings": bool(include_unpublished),
            "listedOnly": bool(listed_only),
        }
        set_if(body, "jobBoardId", job_board_id)
        set_if(body, "location", location)
        set_if(body, "department", department)
        async with ctx.client() as client:
            postings, err = await ctx.call(client, "jobPosting.list", body)
        if err:
            return err
        items = [p for p in postings or [] if isinstance(p, dict)]
        if job_id:
            items = [p for p in items if p.get("jobId") == job_id.strip()]
        shown = items[:_MAX_POSTINGS]
        more = (
            f"\n[{len(items) - len(shown)} more postings - narrow with location / department / job_id]"
            if len(items) > len(shown)
            else ""
        )
        return format_entity_list(shown, fmt_posting, noun="job posting", total=len(items)) + more

    async def get_ashby_job_posting(
        job_posting_id: str, include_unpublished: bool = True, job_board_id: str = ""
    ) -> str:
        body: dict[str, Any] = {
            "jobPostingId": job_posting_id,
            "includeUnpublishedJobPostings": bool(include_unpublished),
        }
        set_if(body, "jobBoardId", job_board_id)
        async with ctx.client() as client:
            posting, err = await ctx.call(client, "jobPosting.info", body)
        if err:
            return err
        if not isinstance(posting, dict):
            return f"Error: Ashby returned no job posting for {job_posting_id}."
        return fmt_posting(posting, detailed=True)

    async def update_ashby_job_posting(
        job_posting_id: str,
        title: str = "",
        description_html: str = "",
        workplace_type: str = "",
        location_id: str = "",
        status: str = "",
    ) -> str:
        body: dict[str, Any] = {"jobPostingId": job_posting_id, "includeUnpublishedJobPostings": True}
        set_if(body, "title", title)
        set_if(body, "workplaceType", workplace_type)
        set_if(body, "locationId", location_id)
        if description_html.strip():
            body["description"] = {"type": "text/html", "value": description_html}
        wanted = None
        if status:
            wanted = {"published": "Published", "draft": "Draft"}.get(status.strip().lower())
            if not wanted:
                return "Error: status must be 'Published' or 'Draft'."
        if len(body) == 2 and not wanted:
            return "Error: give at least one field to change (or a status)."
        posting: Any = None
        async with ctx.client() as client:
            if len(body) > 2:
                posting, err = await ctx.call(client, "jobPosting.update", body)
                if err:
                    return err
            if wanted:
                posting, err = await ctx.call(
                    client, "jobPosting.setStatus", {"jobPostingId": job_posting_id, "status": wanted}
                )
                if err:
                    return err
        if not isinstance(posting, dict):
            return f"Job posting {job_posting_id} updated."
        return "Job posting updated:\n" + fmt_posting(posting)

    async def list_ashby_openings(created_after: str = "", limit: int = DEFAULT_PAGE, cursor: str = "") -> str:
        body: dict[str, Any] = {}
        ms, err = to_epoch_ms(created_after, "created_after")
        if err:
            return err
        set_if(body, "createdAfter", ms)
        async with ctx.client() as client:
            page, err = await ctx.page(client, "opening.list", body, limit=clamp(limit), cursor=cursor)
        if err:
            return err
        return format_entity_list(page.items, fmt_opening, noun="opening") + page_footer(page, noun="opening")

    async def get_ashby_opening(opening_id: str) -> str:
        async with ctx.client() as client:
            opening, err = await ctx.call(client, "opening.info", {"openingId": opening_id})
        if err:
            return err
        if not isinstance(opening, dict):
            return f"Error: Ashby returned no opening for {opening_id}."
        return fmt_opening(opening)

    async def search_ashby_openings(identifier: str = "", job_id: str = "") -> str:
        if not (identifier.strip() or job_id.strip()):
            return "Error: give an opening identifier or a job_id."
        async with ctx.client() as client:
            if identifier.strip():
                openings, err = await ctx.call(client, "opening.search", {"identifier": identifier.strip()})
                if err:
                    return err
                openings = [o for o in openings or [] if isinstance(o, dict)]
                if job_id.strip():
                    openings = [
                        o for o in openings if job_id.strip() in ((o.get("latestVersion") or {}).get("jobIds") or [])
                    ]
            else:
                job, err = await ctx.call(client, "job.info", {"id": job_id.strip(), "expand": ["openings"]})
                if err:
                    return err
                openings = [o for o in (job or {}).get("openings") or [] if isinstance(o, dict)]
        return format_entity_list(openings, fmt_opening, noun="opening", empty_msg="No opening matches.")

    async def manage_ashby_opening(
        action: str,
        opening_id: str = "",
        identifier: str = "",
        description: str = "",
        department_id: str = "",
        location_ids: str = "",
        job_ids: str = "",
        job_id: str = "",
        location_id: str = "",
        target_hire_date: str = "",
        target_start_date: str = "",
        is_backfill: bool | None = None,
        employment_type: str = "",
        opening_state: str = "",
        close_reason_id: str = "",
    ) -> str:
        action = (action or "").strip().lower()
        if action not in _OPENING_ACTIONS:
            return f"Error: action must be one of {', '.join(_OPENING_ACTIONS)}."
        if action != "create" and not opening_id:
            return f"Error: {action} needs the opening_id."
        body: dict[str, Any] = {} if action == "create" else {"openingId": opening_id}
        if action in ("create", "update"):
            set_if(body, "identifier", identifier)
            set_if(body, "description", description)
            set_if(body, "teamId", department_id)
            set_if(body, "targetHireDate", target_hire_date)
            set_if(body, "targetStartDate", target_start_date)
            set_if(body, "isBackfill", is_backfill)
            set_if(body, "employmentType", employment_type)
            if action == "create":
                if id_list(location_ids):
                    body["locationIds"] = id_list(location_ids)
                if id_list(job_ids):
                    body["jobIds"] = id_list(job_ids)
                set_if(body, "openingState", opening_state)
            endpoint = f"opening.{action}"
        elif action == "set_state":
            state = {s.lower(): s for s in OPENING_STATES}.get(opening_state.strip().lower())
            if not state:
                return f"Error: opening_state must be one of {', '.join(OPENING_STATES)}."
            body["openingState"] = state
            set_if(body, "closeReasonId", close_reason_id if state == "Closed" else "")
            endpoint = "opening.setOpeningState"
        elif action in ("archive", "unarchive"):
            body["archive"] = action == "archive"
            endpoint = "opening.setArchived"
        elif action in ("add_job", "remove_job"):
            if not job_id:
                return f"Error: {action} needs the job_id."
            body["jobId"] = job_id
            endpoint = "opening.addJob" if action == "add_job" else "opening.removeJob"
        else:
            if not location_id:
                return f"Error: {action} needs the location_id."
            body["locationId"] = location_id
            endpoint = "opening.addLocation" if action == "add_location" else "opening.removeLocation"
        async with ctx.client() as client:
            opening, err = await ctx.call(client, endpoint, body)
        if err:
            return err
        if not isinstance(opening, dict):
            return f"Opening {action.replace('_', ' ')} done."
        return f"Opening {action.replace('_', ' ')} done:\n" + fmt_opening(opening)

    for fn in (
        list_ashby_jobs,
        get_ashby_job,
        search_ashby_jobs,
        create_ashby_job,
        update_ashby_job,
        set_ashby_job_status,
        list_ashby_job_postings,
        get_ashby_job_posting,
        update_ashby_job_posting,
        list_ashby_openings,
        get_ashby_opening,
        search_ashby_openings,
        manage_ashby_opening,
    ):
        executor.register(fn.__name__, fn)

    job_id = id_schema("job")
    department = id_schema("department", "list_ashby_departments")
    location = id_schema("location", "list_ashby_locations")
    since = {"type": "string", "description": f"Time bound: {SINCE}"}
    return [
        definition(
            "list_ashby_jobs",
            "List Ashby jobs with status, department and location names, employment type, "
            "requisition id, hiring team (names and roles) and created / opened / closed dates. "
            "Filter by status (one or several, comma separated) and by creation, opening or "
            "closing time; page with cursor.",
            {
                "status": {"type": "string", "description": f"Statuses, comma separated: {', '.join(JOB_STATUSES)}"},
                "created_after": since,
                "opened_after": since,
                "opened_before": since,
                "closed_after": since,
                "closed_before": since,
                "limit": limit_schema(),
                "cursor": CURSOR_SCHEMA,
            },
            [],
            0,
        ),
        definition(
            "get_ashby_job",
            "Get one Ashby job in full: title, status, department and location (names), "
            "employment type, requisition id, hiring team with roles, interview plans, job "
            "posting ids, openings with their state and target hire date, compensation tiers, "
            "custom fields, author and dates.",
            {
                "job_id": job_id,
                "include_unpublished_postings": {
                    "type": "boolean",
                    "description": "Also list draft posting ids",
                    "default": False,
                },
            },
            ["job_id"],
            0,
        ),
        definition(
            "search_ashby_jobs",
            "Find Ashby jobs by title (or part of it) or by custom requisition id.",
            {
                "title": {"type": "string", "description": "Job title or part of it", "default": ""},
                "requisition_id": {
                    "type": "string",
                    "description": "Custom requisition id or part of it",
                    "default": "",
                },
                "term": {"type": "string", "description": "Shortcut for title", "default": ""},
                "limit": limit_schema(),
            },
            [],
            0,
        ),
        definition(
            "create_ashby_job",
            "Create an Ashby job (in Draft) in a department and a location, optionally from a "
            "job template, with a default interview plan (a job cannot open without one), an "
            "employment type and a brand.",
            {
                "title": {"type": "string", "description": "Job title"},
                "department_id": department,
                "location_id": location,
                "default_interview_plan_id": id_schema(
                    "interview plan", "list_ashby_interview_config(config_type='plans')"
                ),
                "job_template_id": id_schema("job template", "list_ashby_reference_data(kind='job_templates')"),
                "employment_type": {"type": "string", "enum": EMPLOYMENT_TYPES},
                "brand_id": id_schema("brand", "list_ashby_reference_data(kind='brands')"),
            },
            ["title", "department_id", "location_id"],
            1,
        ),
        definition(
            "update_ashby_job",
            "Update an Ashby job's title, department, location, default interview plan, "
            "employment type or custom requisition id.",
            {
                "job_id": job_id,
                "title": {"type": "string"},
                "department_id": department,
                "location_id": location,
                "default_interview_plan_id": id_schema("interview plan"),
                "employment_type": {"type": "string", "enum": EMPLOYMENT_TYPES},
                "custom_requisition_id": {"type": "string"},
            },
            ["job_id"],
            1,
        ),
        definition(
            "set_ashby_job_status",
            "Open, close, archive or return an Ashby job to draft. Closing can name a close "
            "reason (list_ashby_reference_data(kind='close_reasons')).",
            {
                "job_id": job_id,
                "status": {"type": "string", "enum": JOB_STATUSES},
                "close_reason_id": id_schema("close reason", "only when closing"),
            },
            ["job_id", "status"],
            1,
        ),
        definition(
            "list_ashby_job_postings",
            "List Ashby job postings (what candidates see on the careers page): title, status, "
            "listed or not, job, department, team, location, workplace and employment type, "
            "publication date, deadline, apply link, compensation summary. Published only "
            "unless include_unpublished.",
            {
                "include_unpublished": {"type": "boolean", "default": False},
                "listed_only": {"type": "boolean", "description": "Leave out unlisted postings", "default": False},
                "job_board_id": id_schema("job board", "list_ashby_reference_data(kind='job_boards')"),
                "location": {"type": "string", "description": "Location name (case sensitive)"},
                "department": {"type": "string", "description": "Department name (case sensitive)"},
                "job_id": id_schema("job", "only this job's postings"),
            },
            [],
            1,
        ),
        definition(
            "get_ashby_job_posting",
            "Get one Ashby job posting: its description, address, job boards, application "
            "form fields, apply link and compensation.",
            {
                "job_posting_id": id_schema("job posting"),
                "include_unpublished": {"type": "boolean", "description": "Allow draft postings", "default": True},
                "job_board_id": id_schema("job board"),
            },
            ["job_posting_id"],
            1,
        ),
        definition(
            "update_ashby_job_posting",
            "Edit an Ashby job posting (title, HTML description, workplace type, primary "
            "location) and/or publish or unpublish it (status 'Published' / 'Draft'). "
            "Publishing makes it visible to candidates.",
            {
                "job_posting_id": id_schema("job posting"),
                "title": {"type": "string"},
                "description_html": {"type": "string", "description": "Full HTML description (replaces it)"},
                "workplace_type": {"type": "string", "enum": WORKPLACE_TYPES},
                "location_id": location,
                "status": {"type": "string", "enum": ["Published", "Draft"]},
            },
            ["job_posting_id"],
            1,
        ),
        definition(
            "list_ashby_openings",
            "List Ashby openings (headcount): identifier, state, jobs, locations, department, "
            "employment type, backfill, target hire and start dates, hiring team.",
            {"created_after": since, "limit": limit_schema(), "cursor": CURSOR_SCHEMA},
            [],
            1,
        ),
        definition(
            "get_ashby_opening",
            "Get one Ashby opening (headcount position) in full.",
            {"opening_id": id_schema("opening")},
            ["opening_id"],
            1,
        ),
        definition(
            "search_ashby_openings",
            "Find Ashby openings by identifier, or list the openings of a job (job_id).",
            {
                "identifier": {"type": "string", "description": "Opening identifier"},
                "job_id": id_schema("job", "the job's openings"),
            },
            [],
            1,
        ),
        definition(
            "manage_ashby_opening",
            "Manage Ashby openings (headcount): create, update (identifier, description, "
            "department, target dates, backfill, employment type), set_state (Draft / Approved "
            "/ Open / Closed, with a close reason), archive / unarchive, add_job / remove_job, "
            "add_location / remove_location.",
            {
                "action": {"type": "string", "enum": _OPENING_ACTIONS},
                "opening_id": id_schema("opening", "every action but create"),
                "identifier": {"type": "string", "description": "Human-readable identifier"},
                "description": {"type": "string"},
                "department_id": department,
                "location_ids": {"type": "string", "description": "For create: location ids, comma separated"},
                "job_ids": {"type": "string", "description": "For create: job ids, comma separated"},
                "job_id": id_schema("job", "add_job / remove_job"),
                "location_id": id_schema("location", "add_location / remove_location"),
                "target_hire_date": {"type": "string", "description": "YYYY-MM-DD"},
                "target_start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "is_backfill": {"type": "boolean"},
                "employment_type": {"type": "string", "enum": EMPLOYMENT_TYPES},
                "opening_state": {"type": "string", "enum": OPENING_STATES},
                "close_reason_id": id_schema("close reason"),
            },
            ["action"],
            2,
        ),
    ]
