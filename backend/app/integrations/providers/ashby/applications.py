"""Applications: a candidate considered for a job, its stage history and AI evaluations."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    CURSOR_SCHEMA,
    DEFAULT_PAGE,
    MAX_LOOKUPS,
    MAX_PAGE,
    SINCE,
    AshbyContext,
    clamp,
    contact,
    custom_field_lines,
    definition,
    hiring_team_lines,
    id_schema,
    limit_schema,
    page_footer,
    set_if,
    to_epoch_ms,
    truncate,
    ts,
    user_label,
    warnings_note,
)

APPLICATION_STATUSES = ["Active", "Hired", "Archived", "Lead"]


def _history(application: dict[str, Any]) -> list[dict[str, Any]]:
    events = [h for h in application.get("applicationHistory") or [] if isinstance(h, dict)]
    return sorted(events, key=lambda h: h.get("stageNumber") or 0)


def hired_at(application: dict[str, Any]) -> str:
    """When a hired application entered its hired stage, read off its history.

    Ashby has no hire timestamp on an application: a hired application sits in
    its plan's hired stage, and the history event of that stage (the current
    one, not left yet) carries the time it was entered.
    """
    if application.get("status") != "Hired":
        return ""
    history = _history(application)
    stage_id = (application.get("currentInterviewStage") or {}).get("id")
    current = [h for h in history if h.get("stageId") == stage_id and not h.get("leftStageAt")]
    event = current[-1] if current else (history[-1] if history else None)
    return ts(event.get("enteredStageAt")) if event else ""


def _stage_label(stage: Any) -> str:
    if not isinstance(stage, dict) or not stage:
        return ""
    kind = f" [{stage['type']}]" if stage.get("type") else ""
    return f"{stage.get('title') or 'Unnamed stage'}{kind} (ID: {stage.get('id', '?')})"


def fmt_application(a: dict[str, Any], *, detailed: bool = False) -> str:
    lines = [f"**Application** (ID: {a.get('id', '?')}) - Status: {a.get('status') or 'unknown'}"]
    candidate = a.get("candidate") or {}
    if candidate:
        email = contact(candidate.get("primaryEmailAddress"))
        lines.append(
            f"  Candidate: {candidate.get('name') or 'Unnamed'} (ID: {candidate.get('id', '?')})"
            + (f" <{email}>" if email else "")
        )
    job = a.get("job") or {}
    if job:
        lines.append(f"  Job: {job.get('title') or 'Untitled job'} (ID: {job.get('id', '?')})")
    stage = _stage_label(a.get("currentInterviewStage"))
    if stage:
        lines.append(f"  Current stage: {stage}")
    source = a.get("source") or {}
    if source.get("title"):
        kind = (source.get("sourceType") or {}).get("title")
        lines.append(f"  Source: {source['title']}{f' ({kind})' if kind else ''} (ID: {source.get('id', '?')})")
    if a.get("creditedToUser"):
        lines.append(f"  Credited to: {user_label(a['creditedToUser'])}")
    reason = a.get("archiveReason") or {}
    if reason or a.get("archivedAt"):
        text = reason.get("text") or "no reason recorded"
        kind = f" [{reason['reasonType']}]" if reason.get("reasonType") else ""
        when = f" on {ts(a['archivedAt'])}" if a.get("archivedAt") else ""
        lines.append(f"  Archived{when}: {text}{kind}")
    dates = [f"Created: {ts(a.get('createdAt'))}"] if a.get("createdAt") else []
    if a.get("updatedAt"):
        dates.append(f"Updated: {ts(a['updatedAt'])}")
    hire = hired_at(a)
    if hire:
        dates.append(f"Hired at: {hire}")
    if dates:
        lines.append("  " + " | ".join(dates))
    if not detailed:
        return "\n".join(lines)
    if a.get("appliedViaJobPostingId"):
        lines.append(f"  Applied via job posting: {a['appliedViaJobPostingId']}")
    lines.extend(hiring_team_lines(a.get("hiringTeam")))
    openings = [o for o in a.get("openings") or [] if isinstance(o, dict)]
    for opening in openings:
        version = opening.get("latestVersion") or {}
        label = version.get("identifier") or opening.get("id")
        lines.append(f"  Opening: {label} [{opening.get('openingState') or '?'}] (ID: {opening.get('id', '?')})")
    history = _history(a)
    if history:
        trail = " -> ".join(f"{h.get('title') or '?'} ({ts(h.get('enteredStageAt'))[:10]})" for h in history[-6:])
        earlier = f"{len(history) - 6} earlier, " if len(history) > 6 else ""
        lines.append(f"  Stage history ({earlier}latest last): {trail}")
    lines.extend(custom_field_lines(a.get("customFields")))
    resume = a.get("resumeFileHandle")
    if isinstance(resume, dict) and resume.get("id"):
        lines.append(f"  Resume: {resume.get('name') or 'resume'} (file id: {resume['id']})")
    return "\n".join(lines)


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration(start: Any, end: Any) -> str:
    began, ended = _parse_ts(start), _parse_ts(end)
    if not began or not ended:
        return ""
    days = (ended - began).total_seconds() / 86400
    return f"{days:.1f} days" if days >= 1 else f"{days * 24:.1f} hours"


def register(ctx: AshbyContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    async def list_ashby_applications(
        status: str = "",
        job_id: str = "",
        created_after: str = "",
        created_before: str = "",
        limit: int = DEFAULT_PAGE,
        cursor: str = "",
        include_hire_dates: bool = False,
    ) -> str:
        body: dict[str, Any] = {}
        if status:
            wanted = {s.lower(): s for s in APPLICATION_STATUSES}.get(status.strip().lower())
            if not wanted:
                return f"Error: status must be one of {', '.join(APPLICATION_STATUSES)}."
            body["status"] = wanted
        set_if(body, "jobId", job_id)
        for key, value, name in (
            ("createdAfter", created_after, "created_after"),
            ("createdBefore", created_before, "created_before"),
        ):
            ms, err = to_epoch_ms(value, name)
            if err:
                return err
            set_if(body, key, ms)
        async with ctx.client() as client:
            page, err = await ctx.page(client, "application.list", body, limit=clamp(limit), cursor=cursor)
            if err:
                return err
            items = page.items
            if include_hire_dates:
                hired = [a["id"] for a in items if a.get("status") == "Hired" and a.get("id")]
                full = await ctx.applications(client, hired)
                items = [{**a, **full.get(a.get("id"), {})} if a.get("id") in full else a for a in items]
        filters = ", ".join(f"{k}={v}" for k, v in (("status", status), ("job", job_id)) if v)
        empty = f"No applications{f' with {filters}' if filters else ''}."
        return format_entity_list(items, fmt_application, noun="application", empty_msg=empty) + page_footer(
            page, noun="application"
        )

    async def get_ashby_application(application_id: str) -> str:
        async with ctx.client() as client:
            application, err = await ctx.call(
                client, "application.info", {"applicationId": application_id, "expand": ["openings"]}
            )
        if err:
            return err
        if not isinstance(application, dict) or not application.get("id"):
            return f"Error: Ashby returned no application for {application_id}."
        return fmt_application(application, detailed=True)

    async def list_ashby_candidate_applications(candidate_id: str) -> str:
        async with ctx.client() as client:
            candidate, err = await ctx.call(client, "candidate.info", {"id": candidate_id})
            if err:
                return err
            candidate = candidate if isinstance(candidate, dict) else {}
            ids = [i for i in candidate.get("applicationIds") or [] if i]
            applications = await ctx.applications(client, ids)
        name = candidate.get("name") or candidate_id
        if not ids:
            return f"Candidate {name} (ID: {candidate_id}) has no applications."
        lines = [f"**Applications of {name}** (candidate ID: {candidate_id}) - {len(ids)} in total"]
        unread = []
        for application_id in ids:
            application = applications.get(application_id)
            if application:
                lines.extend(["", fmt_application(application)])
            else:
                unread.append(application_id)
        if unread:
            shown = ", ".join(unread[:MAX_LOOKUPS])
            lines.append(f"\nNot read (get_ashby_application reads one): {shown}")
        return "\n".join(lines)

    async def create_ashby_application(
        candidate_id: str,
        job_id: str,
        source_id: str = "",
        credited_to_user_id: str = "",
        interview_plan_id: str = "",
        interview_stage_id: str = "",
    ) -> str:
        body: dict[str, Any] = {"candidateId": candidate_id, "jobId": job_id}
        set_if(body, "sourceId", source_id)
        set_if(body, "creditedToUserId", credited_to_user_id)
        set_if(body, "interviewPlanId", interview_plan_id)
        set_if(body, "interviewStageId", interview_stage_id)
        async with ctx.client() as client:
            data, err = await ctx.post(client, "application.create", body)
        if err:
            return err
        application = data.get("results") if isinstance(data.get("results"), dict) else {}
        return "Application created:\n" + fmt_application(application) + warnings_note(data)

    async def change_ashby_application_stage(
        application_id: str, interview_stage_id: str, archive_reason_id: str = ""
    ) -> str:
        body: dict[str, Any] = {"applicationId": application_id, "interviewStageId": interview_stage_id}
        set_if(body, "archiveReasonId", archive_reason_id)
        async with ctx.client() as client:
            application, err = await ctx.call(client, "application.changeStage", body)
        if err:
            return err
        if not isinstance(application, dict):
            return f"Application {application_id} moved to stage {interview_stage_id}."
        return "Stage changed:\n" + fmt_application(application)

    async def update_ashby_application(
        application_id: str,
        source_id: str = "",
        credited_to_user_id: str = "",
        send_notifications: bool = False,
    ) -> str:
        body: dict[str, Any] = {"applicationId": application_id}
        set_if(body, "sourceId", source_id)
        set_if(body, "creditedToUserId", credited_to_user_id)
        if len(body) == 1:
            return "Error: give a source_id or a credited_to_user_id to update."
        body["sendNotifications"] = bool(send_notifications)
        async with ctx.client() as client:
            application, err = await ctx.call(client, "application.update", body)
        if err:
            return err
        if not isinstance(application, dict):
            return f"Application {application_id} updated."
        return "Application updated:\n" + fmt_application(application)

    async def transfer_ashby_application(
        application_id: str,
        job_id: str,
        interview_plan_id: str,
        interview_stage_id: str,
        start_automatic_activities: bool = True,
    ) -> str:
        body = {
            "applicationId": application_id,
            "jobId": job_id,
            "interviewPlanId": interview_plan_id,
            "interviewStageId": interview_stage_id,
            "startAutomaticActivities": bool(start_automatic_activities),
        }
        async with ctx.client() as client:
            application, err = await ctx.call(client, "application.transfer", body)
        if err:
            return err
        if not isinstance(application, dict):
            return f"Application {application_id} transferred to job {job_id}."
        return "Application transferred:\n" + fmt_application(application)

    async def list_ashby_application_history(application_id: str, limit: int = MAX_PAGE, cursor: str = "") -> str:
        async with ctx.client() as client:
            page, err = await ctx.page(
                client,
                "application.listHistory",
                {"applicationId": application_id},
                limit=clamp(limit, default=MAX_PAGE),
                cursor=cursor,
            )
            if err:
                return err
            events = sorted(page.items, key=lambda h: h.get("stageNumber") or 0)
            actors = await ctx.users(client, (e.get("actorId") for e in events))
            stages = await ctx.stages(client, (e.get("stageId") for e in events))
        if not events:
            return f"No history for application {application_id}."
        lines = [f"**History of application {application_id}** ({len(events)} events, oldest first):"]
        previous: dict[str, Any] | None = None
        for event in events:
            stage = stages.get(event.get("stageId")) or {}
            kind = f" [{stage['type']}]" if stage.get("type") else ""
            if previous is not None:
                origin = f'from "{previous.get("title") or "?"}"'
                change = "Stage change"
            elif event.get("stageNumber") == 0:
                origin, change = "first stage", "Application created"
            else:
                origin, change = "from an earlier page", "Stage change"
            lines.append(
                f"{event.get('stageNumber', '?')}. {ts(event.get('enteredStageAt'))} - {change}: "
                f'entered "{event.get("title") or "?"}"{kind} ({origin})'
            )
            details = []
            if event.get("leftStageAt"):
                spent = _duration(event.get("enteredStageAt"), event.get("leftStageAt"))
                details.append(f"left {ts(event['leftStageAt'])}" + (f" after {spent}" if spent else ""))
            else:
                details.append("current stage")
            actor_id = event.get("actorId")
            if actor_id:
                details.append(f"by {user_label(actors.get(actor_id)) or f'user {actor_id}'}")
            details.append(f"stage ID: {event.get('stageId', '?')}")
            details.append(f"event ID: {event.get('id', '?')}")
            lines.append("   " + " | ".join(details))
            previous = event
        return "\n".join(lines) + page_footer(page, noun="event")

    async def list_ashby_application_criteria_evaluations(
        application_id: str, limit: int = MAX_PAGE, cursor: str = ""
    ) -> str:
        async with ctx.client() as client:
            page, err = await ctx.page(
                client,
                "application.listCriteriaEvaluations",
                {"applicationId": application_id},
                limit=clamp(limit, default=MAX_PAGE),
                cursor=cursor,
            )
        if err:
            return err
        if not page.items:
            return f"No AI criteria evaluations for application {application_id}."
        lines = [f"**AI criteria evaluations of application {application_id}** ({len(page.items)}):"]
        for e in page.items:
            criterion = e.get("criterion") or {}
            score = f" ({e['outcomeNumber']})" if e.get("outcomeNumber") is not None else ""
            lines.append(
                f"- [{e.get('status') or '?'}] {criterion.get('title') or 'Criterion'}"
                f" [{criterion.get('type') or '?'}]: {e.get('outcome') or 'no outcome'}{score}"
            )
            if e.get("reasoning"):
                lines.append(f"    Reasoning: {truncate(e['reasoning'], 800)}")
            if e.get("skipReason"):
                lines.append(f"    Skipped: {e['skipReason']}")
            if e.get("evaluatedAt"):
                lines.append(f"    Evaluated: {ts(e['evaluatedAt'])}")
        return "\n".join(lines) + page_footer(page, noun="evaluation")

    async def delete_ashby_application(application_id: str) -> str:
        async with ctx.client() as client:
            _, err = await ctx.call(client, "application.delete", {"applicationId": application_id})
        if err:
            return err
        return f"Application {application_id} deleted from Ashby. This cannot be undone."

    for fn in (
        list_ashby_applications,
        get_ashby_application,
        list_ashby_candidate_applications,
        create_ashby_application,
        change_ashby_application_stage,
        update_ashby_application,
        transfer_ashby_application,
        list_ashby_application_history,
        list_ashby_application_criteria_evaluations,
        delete_ashby_application,
    ):
        executor.register(fn.__name__, fn)

    application_id = id_schema("application")
    stage_hint = "list_ashby_interview_config(config_type='stages', job_id=...) lists them"
    return [
        definition(
            "list_ashby_applications",
            "List Ashby applications (a candidate considered for a job), newest first, with "
            "candidate, job, current stage, source, archive reason and dates. Filter by "
            "status (e.g. 'Hired' to enumerate hires), job_id and creation time - Ashby "
            "filters server-side, so hires are found without paging through archives. "
            "include_hire_dates reads each hired application's hire date (one extra read "
            "per hire).",
            {
                "status": {
                    "type": "string",
                    "enum": APPLICATION_STATUSES,
                    "description": "Only applications in this status",
                },
                "job_id": id_schema("job", "only applications for this job"),
                "created_after": {"type": "string", "description": f"Created at or after: {SINCE}"},
                "created_before": {"type": "string", "description": f"Created at or before: {SINCE}"},
                "limit": limit_schema(),
                "cursor": CURSOR_SCHEMA,
                "include_hire_dates": {
                    "type": "boolean",
                    "description": "Add the hire date of each hired application",
                    "default": False,
                },
            },
            [],
            0,
        ),
        definition(
            "get_ashby_application",
            "Get one Ashby application in full: status, candidate (name, email), job, current "
            "stage and its type, source, credited user, archive reason and date, created / "
            "updated / hired-at dates, hiring team, openings, the latest stage history, custom "
            "fields and the resume file id.",
            {"application_id": application_id},
            ["application_id"],
            0,
        ),
        definition(
            "list_ashby_candidate_applications",
            "List every application of one Ashby candidate (each job they were considered "
            "for) with status, current stage, source and dates - read from the candidate's "
            "application ids.",
            {"candidate_id": id_schema("candidate")},
            ["candidate_id"],
            0,
        ),
        definition(
            "create_ashby_application",
            "Consider an Ashby candidate for a job (create an application), optionally with a "
            "source, a credited user and a starting interview plan / stage.",
            {
                "candidate_id": id_schema("candidate"),
                "job_id": id_schema("job"),
                "source_id": id_schema("source", "list_ashby_sources"),
                "credited_to_user_id": id_schema("user", "credited with the application"),
                "interview_plan_id": id_schema("interview plan", "default: the job's default plan"),
                "interview_stage_id": id_schema("interview stage", "default: the plan's first stage"),
            },
            ["candidate_id", "job_id"],
            1,
        ),
        definition(
            "change_ashby_application_stage",
            "Move an Ashby application to another interview stage of its plan. Moving to an "
            "'Archived' stage needs archive_reason_id (list_ashby_reference_data(kind="
            "'archive_reasons')). Stage ids: " + stage_hint + ".",
            {
                "application_id": application_id,
                "interview_stage_id": id_schema("interview stage", stage_hint),
                "archive_reason_id": id_schema("archive reason", "required for an Archived stage"),
            },
            ["application_id", "interview_stage_id"],
            1,
        ),
        definition(
            "update_ashby_application",
            "Change an Ashby application's source or credited user.",
            {
                "application_id": application_id,
                "source_id": id_schema("source", "list_ashby_sources"),
                "credited_to_user_id": id_schema("user", "list_ashby_users"),
                "send_notifications": {
                    "type": "boolean",
                    "description": "Notify users subscribed to the application",
                    "default": False,
                },
            },
            ["application_id"],
            1,
        ),
        definition(
            "transfer_ashby_application",
            "Transfer an Ashby application to another job, into a given interview plan and "
            "stage of that job (" + stage_hint + ").",
            {
                "application_id": application_id,
                "job_id": id_schema("job", "the job to transfer to"),
                "interview_plan_id": id_schema("interview plan", "of the target job"),
                "interview_stage_id": id_schema("interview stage", "of that plan"),
                "start_automatic_activities": {
                    "type": "boolean",
                    "description": "Start the target stage's automatic activities",
                    "default": True,
                },
            },
            ["application_id", "job_id", "interview_plan_id", "interview_stage_id"],
            1,
        ),
        definition(
            "list_ashby_application_history",
            "The stage history of an Ashby application, oldest first: each stage change with "
            "the stage it came from and the one it entered (and that stage's type), when it "
            "entered and left (time spent), who moved it, and the event and stage ids.",
            {
                "application_id": application_id,
                "limit": limit_schema(default=MAX_PAGE),
                "cursor": CURSOR_SCHEMA,
            },
            ["application_id"],
            0,
        ),
        definition(
            "list_ashby_application_criteria_evaluations",
            "The AI criteria evaluations Ashby ran on an application: each criterion, its "
            "status, outcome, score and reasoning.",
            {
                "application_id": application_id,
                "limit": limit_schema(default=MAX_PAGE),
                "cursor": CURSOR_SCHEMA,
            },
            ["application_id"],
            1,
        ),
        definition(
            "delete_ashby_application",
            "Delete an Ashby application. Permanent, cannot be undone (prefer archiving with "
            "change_ashby_application_stage). Needs the key's Candidates Delete permission.",
            {"application_id": application_id},
            ["application_id"],
            2,
        ),
    ]
