"""Interviews: scheduled interviews, their events, plans and stages, take-homes.

Ashby keeps three different things under "interview":

* an interview *definition* (``interview.list`` / ``interview.info``): a
  template - title, type, instructions, feedback form;
* an interview *schedule* (``interviewSchedule.list``): the interviews booked
  for one application in one stage - status, who scheduled it;
* an interview *event* of a schedule: one slot - start and end, interviewers,
  meeting link, whether feedback was submitted.

``list_ashby_interviews`` lists schedules with their events; the definitions are
``list_ashby_interview_config(config_type='interviews')``.
"""

from __future__ import annotations

import asyncio
import mimetypes
import time
from datetime import datetime
from typing import Any

from app.integrations.file_delivery import deliver_file, fetch_file, file_content_type
from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

from ._common import (
    CURSOR_SCHEMA,
    FAN_OUT,
    MAX_PAGE,
    PROVIDER,
    SINCE,
    AshbyContext,
    clamp,
    definition,
    id_list,
    id_schema,
    limit_schema,
    page_footer,
    parse_json_arg,
    set_if,
    strip_html,
    to_epoch_ms,
    truncate,
    ts,
    user_label,
)

SCHEDULE_STATUSES = [
    "NeedsScheduling",
    "SchedulingRequested",
    "WaitingOnCandidateBooking",
    "WaitingOnCandidateAvailability",
    "CandidateAvailabilitySubmitted",
    "Scheduled",
    "WaitingOnFeedback",
    "Complete",
    "Cancelled",
    "OnHold",
    "Unknown",
]
CONFIG_TYPES = ["all", "stages", "plans", "stage_groups", "interviews", "interviewer_pools"]
_SCHEDULE_ACTIONS = ["add_event", "update_event", "cancel_events", "cancel"]
#: Largest notetaker transcript downloaded.
_MAX_TRANSCRIPT_BYTES = 25 * 1024 * 1024
#: Most interview plans whose stages one call lists.
_MAX_PLANS = 50
#: Most schedules read to find the one an event belongs to.
_MAX_SCHEDULE_SCAN = 300


def _epoch(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _events(schedule: dict[str, Any]) -> list[dict[str, Any]]:
    events = [e for e in schedule.get("interviewEvents") or [] if isinstance(e, dict)]
    return sorted(events, key=lambda e: e.get("startTime") or "")


def _event_lines(
    event: dict[str, Any],
    interviews: dict[str, dict[str, Any]],
    prefix: str = "  ",
) -> list[str]:
    interview = event.get("interview") or interviews.get(event.get("interviewId")) or {}
    title = interview.get("title") or "Interview"
    start, end = ts(event.get("startTime")), ts(event.get("endTime"))
    when = f"{start} -> {end}" if start or end else "not scheduled yet"
    lines = [
        f"{prefix}- {title}: {when} (event ID: {event.get('id', '?')}, interview ID: "
        f"{event.get('interviewId') or interview.get('id') or '?'})"
    ]
    people = []
    for person in event.get("interviewers") or []:
        if not isinstance(person, dict):
            continue
        notes = ["feedback required"] if person.get("isFeedbackRequired") else []
        if person.get("trainingRole"):
            notes.append(f"training: {person['trainingRole']}")
        suffix = f" ({', '.join(notes)})" if notes else ""
        people.append(user_label(person) + suffix)
    if not people:
        people = [f"user {u}" for u in event.get("interviewerUserIds") or []]
    if people:
        lines.append(f"{prefix}    Interviewers: {'; '.join(people)}")
    where = [
        f"{label}: {event[key]}"
        for label, key in (("Meeting", "meetingLink"), ("Location", "location"))
        if event.get(key)
    ]
    if where:
        lines.append(f"{prefix}    " + " | ".join(where))
    feedback = "submitted" if event.get("hasSubmittedFeedback") else "not submitted yet"
    link = f" | Feedback link: {event['feedbackLink']}" if event.get("feedbackLink") else ""
    lines.append(f"{prefix}    Feedback: {feedback}{link}")
    if event.get("notetakerTranscriptId"):
        lines.append(
            f"{prefix}    Notetaker transcript: {event['notetakerTranscriptId']} (download_ashby_interview_transcript)"
        )
    return lines


def _fmt_schedule(
    schedule: dict[str, Any],
    *,
    stages: dict[str, dict[str, Any]],
    applications: dict[str, dict[str, Any]],
    interviews: dict[str, dict[str, Any]],
) -> str:
    lines = [f"**Interview schedule** (ID: {schedule.get('id', '?')}) - Status: {schedule.get('status') or 'unknown'}"]
    application_id = schedule.get("applicationId")
    application = applications.get(application_id) or {}
    candidate = application.get("candidate") or {}
    job = application.get("job") or {}
    who = [f"Application: {application_id or '?'}"]
    if candidate:
        who.insert(0, f"Candidate: {candidate.get('name') or 'Unnamed'} (ID: {candidate.get('id', '?')})")
    if job:
        who.append(f"Job: {job.get('title') or 'Untitled'} (ID: {job.get('id', '?')})")
    lines.append("  " + " | ".join(who))
    stage_id = schedule.get("interviewStageId")
    if stage_id:
        stage = stages.get(stage_id) or {}
        kind = f" [{stage['type']}]" if stage.get("type") else ""
        lines.append(f"  Stage: {stage.get('title') or 'stage'}{kind} (ID: {stage_id})")
    meta = []
    if schedule.get("scheduledBy"):
        meta.append(f"Scheduled by: {user_label(schedule['scheduledBy'])}")
    if schedule.get("createdAt"):
        meta.append(f"Created: {ts(schedule['createdAt'])}")
    if schedule.get("updatedAt"):
        meta.append(f"Updated: {ts(schedule['updatedAt'])}")
    if meta:
        lines.append("  " + " | ".join(meta))
    events = _events(schedule)
    if events:
        lines.append(f"  Events ({len(events)}):")
        for event in events:
            lines.extend(_event_lines(event, interviews, prefix="  "))
    else:
        lines.append("  No interview events yet.")
    return "\n".join(lines)


def _fmt_definition(interview: dict[str, Any], *, detailed: bool = False) -> str:
    flags = [
        label
        for label, key in (
            ("archived", "isArchived"),
            ("debrief", "isDebrief"),
            ("feedback required", "isFeedbackRequired"),
        )
        if interview.get(key)
    ]
    line = (
        f"**{interview.get('title') or 'Untitled interview'}** (interview ID: {interview.get('id', '?')})"
        f" - Type: {interview.get('type') or '?'}"
    )
    if flags:
        line += f" [{', '.join(flags)}]"
    extra = []
    if interview.get("externalTitle") and interview["externalTitle"] != interview.get("title"):
        extra.append(f"Candidate-facing title: {interview['externalTitle']}")
    if interview.get("jobId"):
        extra.append(f"Job: {interview['jobId']}")
    if interview.get("feedbackFormDefinitionId"):
        extra.append(f"Feedback form: {interview['feedbackFormDefinitionId']}")
    if extra:
        line += "\n  " + " | ".join(extra)
    instructions = interview.get("instructionsPlain") or strip_html(interview.get("instructionsHtml"))
    if detailed and instructions:
        line += "\n  Instructions:\n    " + truncate(instructions).replace("\n", "\n    ")
    return line


def _fmt_stage(stage: dict[str, Any], prefix: str = "  ") -> list[str]:
    order = stage.get("orderInInterviewPlan")
    head = f"{prefix}{order}. " if order is not None else f"{prefix}- "
    kind = f" [{stage['type']}]" if stage.get("type") else ""
    lines = [f"{head}{stage.get('title') or 'Unnamed stage'}{kind} (stage ID: {stage.get('id', '?')})"]
    for activity in stage.get("activities") or []:
        if not isinstance(activity, dict):
            continue
        interviews = []
        for item in activity.get("interviews") or []:
            if not isinstance(item, dict):
                continue
            duration = f", {item['interviewDurationMinutes']} min" if item.get("interviewDurationMinutes") else ""
            schedulable = "" if item.get("isSchedulable", True) else ", not schedulable"
            interviews.append(
                f"{item.get('title') or 'Interview'} (interview ID: {item.get('interviewId', '?')}{duration}{schedulable})"
            )
        detail = f": {'; '.join(interviews)}" if interviews else ""
        lines.append(f"{prefix}     Activity {activity.get('title') or ''}{detail}")
    return lines


def register(ctx: AshbyContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    async def _context(client: Any, schedules: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
        events = [e for s in schedules for e in _events(s)]
        missing = [e.get("interviewId") for e in events if not e.get("interview")]
        stages, applications, interviews = await asyncio.gather(
            ctx.stages(client, (s.get("interviewStageId") for s in schedules)),
            ctx.applications(client, (s.get("applicationId") for s in schedules)),
            ctx.interview_definitions(client, missing),
        )
        return {"stages": stages, "applications": applications, "interviews": interviews}

    async def list_ashby_interviews(
        application_id: str = "",
        interview_stage_id: str = "",
        created_after: str = "",
        status: str = "",
        upcoming_only: bool = False,
        limit: int = 25,
        cursor: str = "",
    ) -> str:
        body: dict[str, Any] = {}
        set_if(body, "applicationId", application_id)
        set_if(body, "interviewStageId", interview_stage_id)
        ms, err = to_epoch_ms(created_after, "created_after")
        if err:
            return err
        set_if(body, "createdAfter", ms)
        wanted = None
        if status:
            wanted = {s.lower(): s for s in SCHEDULE_STATUSES}.get(status.strip().lower())
            if not wanted:
                return f"Error: status must be one of {', '.join(SCHEDULE_STATUSES)}."
        now = time.time()

        def keep(schedule: dict[str, Any]) -> bool:
            if wanted and schedule.get("status") != wanted:
                return False
            if upcoming_only:
                return any((_epoch(e.get("startTime")) or 0) >= now for e in _events(schedule))
            return True

        async with ctx.client() as client:
            page, err = await ctx.page(
                client,
                "interviewSchedule.list",
                body,
                limit=clamp(limit),
                cursor=cursor,
                keep=keep if (wanted or upcoming_only) else None,
            )
            if err:
                return err
            schedules = page.items
            if upcoming_only:
                schedules = sorted(schedules, key=lambda s: next((e.get("startTime") for e in _events(s)), "") or "")
            lookups = await _context(client, schedules)
        filters = ", ".join(
            f"{k}={v}"
            for k, v in (("application", application_id), ("stage", interview_stage_id), ("status", wanted))
            if v
        )
        empty = (
            "No interviews" + (f" with {filters}" if filters else "") + (" coming up" if upcoming_only else "") + "."
        )
        text = format_entity_list(
            schedules, lambda s: _fmt_schedule(s, **lookups), noun="interview schedule", empty_msg=empty
        )
        return text + page_footer(page, noun="interview schedule")

    async def _find_schedule(
        client: Any, application_id: str, *, schedule_id: str = "", event_id: str = ""
    ) -> dict[str, Any] | None:
        page, err = await ctx.page(
            client,
            "interviewSchedule.list",
            {"applicationId": application_id},
            limit=_MAX_SCHEDULE_SCAN,
        )
        if err or not page:
            return None
        for schedule in page.items:
            if schedule_id and schedule.get("id") == schedule_id:
                return schedule
            if event_id and any(e.get("id") == event_id for e in _events(schedule)):
                return schedule
        return None

    async def _render_event(client: Any, briefing: dict[str, Any]) -> str:
        event_id = briefing.get("id")
        application_id = briefing.get("applicationId") or ""
        schedule = await _find_schedule(client, application_id, event_id=event_id) if application_id else None
        stages = await ctx.stages(client, [briefing.get("interviewStageId")])
        event = next((e for e in _events(schedule or {}) if e.get("id") == event_id), {})
        interview = briefing.get("interview") or event.get("interview") or {}
        candidate = briefing.get("candidate") or (briefing.get("application") or {}).get("candidate") or {}
        job = briefing.get("job") or (briefing.get("application") or {}).get("job") or {}
        lines = [
            f"**Interview: {interview.get('title') or 'Interview'}** (event ID: {event_id}, interview ID: "
            f"{briefing.get('interviewId') or interview.get('id') or '?'})"
        ]
        if schedule:
            lines.append(f"  Status: {schedule.get('status') or '?'} (schedule ID: {schedule.get('id', '?')})")
        start, end = ts(event.get("startTime")), ts(event.get("endTime"))
        if start or end:
            lines.append(f"  When: {start} -> {end}")
        who = [f"Application: {application_id or '?'}"]
        if candidate:
            who.insert(0, f"Candidate: {candidate.get('name') or 'Unnamed'} (ID: {candidate.get('id', '?')})")
        if job:
            who.append(f"Job: {job.get('title') or 'Untitled'} (ID: {job.get('id', '?')})")
        lines.append("  " + " | ".join(who))
        stage_id = briefing.get("interviewStageId")
        if stage_id:
            stage = stages.get(stage_id) or {}
            kind = f" [{stage['type']}]" if stage.get("type") else ""
            lines.append(f"  Stage: {stage.get('title') or 'stage'}{kind} (ID: {stage_id})")
        people = []
        for person in briefing.get("interviewers") or []:
            if not isinstance(person, dict):
                continue
            label = user_label(person.get("user")) or f"user {person.get('userId', '?')}"
            state = "feedback submitted" if person.get("hasSubmittedFeedback") else "feedback pending"
            if not person.get("isFeedbackRequired"):
                state += ", not required"
            people.append(f"{label} ({state})")
        if people:
            lines.append(f"  Interviewers: {'; '.join(people)}")
        where = [
            f"{label}: {event[key]}"
            for label, key in (("Meeting", "meetingLink"), ("Location", "location"), ("Feedback link", "feedbackLink"))
            if event.get(key)
        ]
        if where:
            lines.append("  " + " | ".join(where))
        form = briefing.get("feedbackFormDefinition") or {}
        if briefing.get("feedbackFormDefinitionId"):
            title = form.get("title")
            lines.append(f"  Feedback form: {title + ' ' if title else ''}(ID: {briefing['feedbackFormDefinitionId']})")
        if event.get("notetakerTranscriptId"):
            lines.append(
                f"  Notetaker transcript: {event['notetakerTranscriptId']} (download_ashby_interview_transcript)"
            )
        instructions = interview.get("instructionsPlain") or strip_html(interview.get("instructionsHtml"))
        if instructions:
            lines.append("  Instructions:\n    " + truncate(instructions).replace("\n", "\n    "))
        return "\n".join(lines)

    async def get_ashby_interview(interview_id: str) -> str:
        interview_id = (interview_id or "").strip()
        if not interview_id:
            return "Error: give an interview event, schedule or interview id."
        async with ctx.client() as client:
            briefing, first_err = await ctx.call(
                client,
                "interviewBriefing.info",
                {
                    "interviewEventId": interview_id,
                    "expand": [
                        "application",
                        "candidate",
                        "job",
                        "interview",
                        "interviewers",
                        "feedbackFormDefinition",
                    ],
                },
            )
            if isinstance(briefing, dict) and briefing.get("id"):
                return await _render_event(client, briefing)
            events, err = await ctx.collect_all(
                client,
                "interviewEvent.list",
                {"interviewScheduleId": interview_id, "expand": ["interview"]},
                max_items=MAX_PAGE,
            )
            if not err and events:
                first, _ = await ctx.call(client, "interviewBriefing.info", {"interviewEventId": events[0].get("id")})
                application_id = (first or {}).get("applicationId") if isinstance(first, dict) else None
                schedule = (
                    await _find_schedule(client, application_id, schedule_id=interview_id) if application_id else None
                ) or {"id": interview_id, "applicationId": application_id, "interviewEvents": events}
                lookups = await _context(client, [schedule])
                return _fmt_schedule(schedule, **lookups)
            interview, err = await ctx.call(client, "interview.info", {"id": interview_id})
            if not err and isinstance(interview, dict) and interview.get("id"):
                return _fmt_definition(interview, detailed=True)
        return (
            f"{first_err} ({interview_id} is not an Ashby interview event, interview schedule "
            "or interview definition the key can read.)"
        )

    async def _interviewers(
        client: Any, emails: Any, user_ids: Any, required: bool
    ) -> tuple[list[dict[str, Any]], str | None]:
        addresses = id_list(emails)
        ids = id_list(user_ids)
        if ids:
            users = await ctx.users(client, ids)
            unknown = [i for i in ids if not users.get(i, {}).get("email")]
            if unknown:
                return [], f"Error: no Ashby user email for {', '.join(unknown)} (search_ashby_users)."
            addresses.extend(users[i]["email"] for i in ids)
        return [{"email": a, "feedbackRequired": bool(required)} for a in dict.fromkeys(addresses)], None

    async def _event_body(client: Any, raw: dict[str, Any], required: bool) -> tuple[dict[str, Any] | None, str | None]:
        start = raw.get("start_time") or raw.get("startTime")
        end = raw.get("end_time") or raw.get("endTime")
        if not start or not end:
            return (
                None,
                "Error: each interview event needs a start_time and an end_time (ISO 8601 with Z or an offset).",
            )
        people = raw.get("interviewers")
        if isinstance(people, list) and people and isinstance(people[0], dict):
            interviewers = people
        else:
            interviewers, err = await _interviewers(
                client, raw.get("interviewer_emails") or people, raw.get("interviewer_user_ids"), required
            )
            if err:
                return None, err
        if not interviewers:
            return None, "Error: each interview event needs at least one interviewer (an email or a user id)."
        event: dict[str, Any] = {"startTime": start, "endTime": end, "interviewers": interviewers}
        set_if(event, "interviewId", raw.get("interview_id") or raw.get("interviewId"))
        set_if(event, "interviewEventId", raw.get("interview_event_id") or raw.get("interviewEventId"))
        return event, None

    async def schedule_ashby_interview(
        application_id: str,
        start_time: str = "",
        end_time: str = "",
        interviewer_emails: str = "",
        interviewer_user_ids: str = "",
        interview_id: str = "",
        feedback_required: bool = True,
        events: str = "",
    ) -> str:
        raw_events, err = parse_json_arg(events, "events")
        if err:
            return err
        if raw_events is not None and not isinstance(raw_events, list):
            return "Error: events is a JSON array of interview events."
        requested = raw_events or [
            {
                "start_time": start_time,
                "end_time": end_time,
                "interviewer_emails": interviewer_emails,
                "interviewer_user_ids": interviewer_user_ids,
                "interview_id": interview_id,
            }
        ]
        async with ctx.client() as client:
            built = []
            for raw in requested:
                if not isinstance(raw, dict):
                    return "Error: each interview event is a JSON object."
                event, err = await _event_body(client, raw, feedback_required)
                if err:
                    return err
                built.append(event)
            schedule, err = await ctx.call(
                client, "interviewSchedule.create", {"applicationId": application_id, "interviewEvents": built}
            )
            if err:
                return err
            schedule = schedule if isinstance(schedule, dict) else {}
            lookups = await _context(client, [schedule])
        return "Interview scheduled:\n" + _fmt_schedule(schedule, **lookups)

    async def manage_ashby_interview_schedule(
        action: str,
        interview_schedule_id: str,
        interview_event_id: str = "",
        start_time: str = "",
        end_time: str = "",
        interviewer_emails: str = "",
        interviewer_user_ids: str = "",
        interview_id: str = "",
        feedback_required: bool = True,
        cancel_event_ids: str = "",
        allow_feedback_deletion: bool = False,
        allow_reschedule: bool = False,
    ) -> str:
        action = (action or "").strip().lower()
        if action not in _SCHEDULE_ACTIONS:
            return f"Error: action must be one of {', '.join(_SCHEDULE_ACTIONS)}."
        async with ctx.client() as client:
            if action == "cancel":
                endpoint = "interviewSchedule.cancel"
                body: dict[str, Any] = {"id": interview_schedule_id, "allowReschedule": bool(allow_reschedule)}
            elif action == "cancel_events":
                ids = id_list(cancel_event_ids or interview_event_id)
                if not ids:
                    return "Error: give cancel_event_ids (the interview event ids to cancel)."
                endpoint = "interviewSchedule.update"
                body = {
                    "interviewScheduleId": interview_schedule_id,
                    "interviewEventIdToCancel": ids,
                    "allowFeedbackDeletion": bool(allow_feedback_deletion),
                }
            else:
                if action == "update_event" and not interview_event_id:
                    return "Error: update_event needs the interview_event_id to update."
                event, err = await _event_body(
                    client,
                    {
                        "start_time": start_time,
                        "end_time": end_time,
                        "interviewer_emails": interviewer_emails,
                        "interviewer_user_ids": interviewer_user_ids,
                        "interview_id": interview_id,
                        "interview_event_id": interview_event_id if action == "update_event" else "",
                    },
                    feedback_required,
                )
                if err:
                    return err
                endpoint = "interviewSchedule.update"
                body = {"interviewScheduleId": interview_schedule_id, "interviewEvent": event}
            schedule, err = await ctx.call(client, endpoint, body)
            if err:
                return err
            schedule = schedule if isinstance(schedule, dict) else {"id": interview_schedule_id}
            lookups = await _context(client, [schedule])
        return f"Interview schedule {action.replace('_', ' ')} done:\n" + _fmt_schedule(schedule, **lookups)

    async def _plan_stages(client: Any, plans: list[dict[str, Any]]) -> list[str]:
        gate = asyncio.Semaphore(FAN_OUT)

        async def stages_of(plan: dict[str, Any]) -> tuple[dict[str, Any], Any, str | None]:
            async with gate:
                stages, err = await ctx.call(client, "interviewStage.list", {"interviewPlanId": plan.get("id")})
            return plan, stages, err

        results = await asyncio.gather(*(stages_of(p) for p in plans[:_MAX_PLANS]))
        lines: list[str] = []
        for plan, stages, err in results:
            archived = " [archived]" if plan.get("isArchived") else ""
            lines.append(
                f"- Plan **{plan.get('title') or 'Untitled plan'}**{archived} (plan ID: {plan.get('id', '?')})"
            )
            if err:
                lines.append(f"    stages not read: {err}")
                continue
            ordered = sorted(
                (s for s in stages or [] if isinstance(s, dict)), key=lambda s: s.get("orderInInterviewPlan") or 0
            )
            lines.extend(line for s in ordered for line in _fmt_stage(s, prefix="    "))
            if not ordered:
                lines.append("    (no stages)")
        if len(plans) > _MAX_PLANS:
            lines.append(f"[{len(plans) - _MAX_PLANS} more plans not expanded - pass interview_plan_id]")
        return lines

    async def list_ashby_interview_config(
        config_type: str = "all",
        job_id: str = "",
        interview_plan_id: str = "",
        include_archived: bool = False,
        limit: int = MAX_PAGE,
        cursor: str = "",
    ) -> str:
        config_type = (config_type or "all").strip().lower()
        if config_type not in CONFIG_TYPES:
            return f"Error: config_type must be one of {', '.join(CONFIG_TYPES)}."
        async with ctx.client() as client:
            if config_type in ("stages", "all") and job_id:
                plan, err = await ctx.call(client, "jobInterviewPlan.info", {"jobId": job_id})
                if err:
                    return err
                plan = plan if isinstance(plan, dict) else {}
                stages = sorted(
                    (s for s in plan.get("stages") or [] if isinstance(s, dict)),
                    key=lambda s: s.get("orderInInterviewPlan") or 0,
                )
                lines = [
                    f"**Interview plan of job {job_id}** (plan ID: {plan.get('interviewPlanId', '?')}), {len(stages)} stages:"
                ]
                lines.extend(line for s in stages for line in _fmt_stage(s))
                return "\n".join(lines)
            if config_type in ("stages", "all") and interview_plan_id:
                stages, err = await ctx.call(client, "interviewStage.list", {"interviewPlanId": interview_plan_id})
                if err:
                    return err
                ordered = sorted(
                    (s for s in stages or [] if isinstance(s, dict)), key=lambda s: s.get("orderInInterviewPlan") or 0
                )
                lines = [f"**Stages of interview plan {interview_plan_id}** ({len(ordered)}):"]
                lines.extend(line for s in ordered for line in _fmt_stage(s))
                return "\n".join(lines)
            if config_type in ("stages", "plans", "all"):
                plans, err = await ctx.collect_all(
                    client, "interviewPlan.list", {"includeArchived": bool(include_archived)}, max_items=500
                )
                if err:
                    return err
                if config_type == "plans":
                    return format_entity_list(
                        plans,
                        lambda p: (
                            f"**{p.get('title') or 'Untitled plan'}** (plan ID: {p.get('id', '?')})"
                            + (" [archived]" if p.get("isArchived") else "")
                        ),
                        noun="interview plan",
                    )
                lines = [
                    f"**Interview plans and their stages** ({len(plans)} plans; Ashby lists published "
                    "plans only - a job's own plan: pass job_id):"
                ]
                lines.extend(await _plan_stages(client, plans))
                if config_type == "all":
                    groups, err = await ctx.call(client, "interviewStageGroup.list", {})
                    if not err and groups:
                        lines.append("\n**Stage groups:**")
                        lines.extend(
                            f"- {g.get('title') or 'Group'} [{g.get('stageType') or '?'}] (ID: {g.get('id', '?')})"
                            for g in groups
                            if isinstance(g, dict)
                        )
                return "\n".join(lines)
            if config_type == "stage_groups":
                groups, err = await ctx.call(client, "interviewStageGroup.list", {})
                if err:
                    return err
                return format_entity_list(
                    [g for g in groups or [] if isinstance(g, dict)],
                    lambda g: (
                        f"{g.get('order', '-')}. {g.get('title') or 'Group'} [{g.get('stageType') or '?'}] (ID: {g.get('id', '?')})"
                    ),
                    noun="stage group",
                )
            if config_type == "interviews":
                page, err = await ctx.page(
                    client,
                    "interview.list",
                    {"includeArchived": bool(include_archived), "includeNonSharedInterviews": True},
                    limit=clamp(limit, default=MAX_PAGE),
                    cursor=cursor,
                )
                if err:
                    return err
                return format_entity_list(page.items, _fmt_definition, noun="interview") + page_footer(
                    page, noun="interview"
                )
            page, err = await ctx.page(
                client,
                "interviewerPool.list",
                {"includeArchivedPools": bool(include_archived)},
                limit=clamp(limit, default=MAX_PAGE),
                cursor=cursor,
            )
        if err:
            return err

        def fmt_pool(pool: dict[str, Any]) -> str:
            archived = " [archived]" if pool.get("isArchived") else ""
            return f"**{pool.get('title') or 'Pool'}**{archived} (ID: {pool.get('id', '?')})"

        return format_entity_list(page.items, fmt_pool, noun="interviewer pool") + page_footer(
            page, noun="interviewer pool"
        )

    async def list_ashby_take_home_assignments(
        application_id: str = "", candidate_id: str = "", limit: int = 25, cursor: str = ""
    ) -> str:
        body: dict[str, Any] = {"expand": ["interview"]}
        set_if(body, "applicationId", application_id)
        set_if(body, "candidateId", candidate_id)
        async with ctx.client() as client:
            page, err = await ctx.page(client, "takeHomeAssignment.list", body, limit=clamp(limit), cursor=cursor)
        if err:
            return err

        def fmt(t: dict[str, Any]) -> str:
            title = (t.get("interview") or {}).get("title") or "Take-home"
            lines = [
                f"**{title}** (ID: {t.get('id', '?')}) - Status: {t.get('status') or '?'}",
                f"  Application: {t.get('applicationId', '?')} | Candidate: {t.get('candidateId', '?')} | "
                f"Created: {ts(t.get('createdAt'))}",
            ]
            submission = t.get("submission") or {}
            if submission:
                notes = truncate(submission.get("notesPlain") or strip_html(submission.get("notesHtml")), 500)
                lines.append(f"  Submitted: {ts(submission.get('submittedAt'))}" + (f" - {notes}" if notes else ""))
            reviewers = [
                f"{r.get('userId', '?')} ({'feedback submitted' if r.get('hasSubmittedFeedback') else 'feedback pending'})"
                for r in t.get("reviewers") or []
                if isinstance(r, dict)
            ]
            if reviewers:
                lines.append(f"  Reviewers: {'; '.join(reviewers)}")
            return "\n".join(lines)

        return format_entity_list(page.items, fmt, noun="take-home assignment") + page_footer(
            page, noun="take-home assignment"
        )

    async def download_ashby_interview_transcript(notetaker_transcript_id: str) -> str | ToolResult:
        async with ctx.client() as client:
            info, err = await ctx.call(
                client, "notetakerTranscript.info", {"notetakerTranscriptId": notetaker_transcript_id}
            )
        if err:
            return err
        info = info if isinstance(info, dict) else {}
        url = str(info.get("transcriptUrl") or "")
        if not url:
            return f"Error: Ashby returned no download URL for transcript {notetaker_transcript_id}."
        # The transcript URL is pre-signed: fetched without the API key.
        async with ctx.client() as storage:
            fetched = await fetch_file(
                storage,
                "get",
                url,
                provider=PROVIDER,
                action=f"download transcript {notetaker_transcript_id}",
                max_bytes=_MAX_TRANSCRIPT_BYTES,
                follow_redirects=True,
            )
        if isinstance(fetched, str):
            return f"{fetched}\nDownload URL: {url}"
        name = fetched.filename or (
            f"ashby-interview-transcript-{notetaker_transcript_id}"
            + (mimetypes.guess_extension((fetched.content_type or "").split(";")[0].strip()) or "")
        )
        participants = ", ".join(
            user_label(p, with_id=False) or str(p.get("name") or p.get("email") or "")
            for p in info.get("participants") or []
            if isinstance(p, dict)
        )
        details = [
            f"Notetaker transcript {notetaker_transcript_id} of interview event "
            f"{info.get('interviewEventId', '?')} (recorded {ts(info.get('createdAt'))}); the download URL is "
            "signed and short-lived."
        ]
        if participants:
            details.append(f"Participants: {participants}")
        return await deliver_file(
            data=fetched.data,
            filename=name,
            content_type=file_content_type(fetched.content_type, name),
            source="Ashby",
            persist_file_fn=ctx.persist_file_fn,
            links=[("Download URL", url)],
            details=details,
        )

    for fn in (
        list_ashby_interviews,
        get_ashby_interview,
        schedule_ashby_interview,
        manage_ashby_interview_schedule,
        list_ashby_interview_config,
        list_ashby_take_home_assignments,
        download_ashby_interview_transcript,
    ):
        executor.register(fn.__name__, fn)

    emails = {"type": "string", "description": "Interviewer email addresses, comma separated", "default": ""}
    user_ids = {
        "type": "string",
        "description": "Interviewer Ashby user ids, comma separated (their emails are read)",
        "default": "",
    }
    when = "ISO 8601 with Z or an offset, e.g. 2026-10-14T09:00:00Z"
    return [
        definition(
            "list_ashby_interviews",
            "List scheduled Ashby interviews (interview schedules) with their status, the "
            "candidate, job and application, the stage, and each event: interview title, start "
            "and end, interviewers, meeting link, feedback status. Filter by application_id "
            "(one candidate's interviews), interview_stage_id, created_after and status; "
            "upcoming_only keeps schedules with an event still to come, soonest first (pair it "
            "with created_after, e.g. '60d').",
            {
                "application_id": id_schema("application", "only this application's interviews"),
                "interview_stage_id": id_schema("interview stage"),
                "created_after": {"type": "string", "description": f"Scheduled (created) at or after: {SINCE}"},
                "status": {"type": "string", "enum": SCHEDULE_STATUSES, "description": "Only schedules in this status"},
                "upcoming_only": {
                    "type": "boolean",
                    "description": "Only schedules with an event to come",
                    "default": False,
                },
                "limit": limit_schema(),
                "cursor": CURSOR_SCHEMA,
            },
            [],
            0,
        ),
        definition(
            "get_ashby_interview",
            "Get one Ashby interview by any of its ids: an interview event id (from "
            "list_ashby_interviews) gives the slot - times, status, candidate, job, stage, "
            "each interviewer's feedback status, meeting link, feedback form, instructions; a "
            "schedule id gives the whole schedule; an interview definition id gives the "
            "template (type, instructions, feedback form).",
            {"interview_id": {"type": "string", "description": "Interview event, schedule or interview id"}},
            ["interview_id"],
            0,
        ),
        definition(
            "schedule_ashby_interview",
            "Book an interview for an Ashby application (in its current stage). Give "
            "start_time, end_time and the interviewers (emails, or user ids whose emails are "
            "read), optionally the interview definition (interview_id). For a panel of several "
            "slots pass events: a JSON array of {start_time, end_time, interviewer_emails, "
            "interview_id}.",
            {
                "application_id": id_schema("application"),
                "start_time": {"type": "string", "description": f"Start: {when}"},
                "end_time": {"type": "string", "description": f"End: {when}"},
                "interviewer_emails": emails,
                "interviewer_user_ids": user_ids,
                "interview_id": id_schema(
                    "interview definition", "list_ashby_interview_config(config_type='interviews')"
                ),
                "feedback_required": {
                    "type": "boolean",
                    "description": "Interviewers must submit feedback",
                    "default": True,
                },
                "events": {
                    "type": "string",
                    "description": "JSON array of events (instead of the single-event fields)",
                    "default": "",
                },
            },
            ["application_id"],
            1,
        ),
        definition(
            "manage_ashby_interview_schedule",
            "Change an Ashby interview schedule created through the API: add_event, "
            "update_event (interview_event_id + new times / interviewers), cancel_events "
            "(cancel_event_ids; events with submitted feedback need allow_feedback_deletion, "
            "which deletes that feedback), or cancel the whole schedule.",
            {
                "action": {"type": "string", "enum": _SCHEDULE_ACTIONS},
                "interview_schedule_id": id_schema("interview schedule"),
                "interview_event_id": id_schema("interview event", "for update_event"),
                "start_time": {"type": "string", "description": f"Start: {when}"},
                "end_time": {"type": "string", "description": f"End: {when}"},
                "interviewer_emails": emails,
                "interviewer_user_ids": user_ids,
                "interview_id": id_schema("interview definition"),
                "feedback_required": {"type": "boolean", "default": True},
                "cancel_event_ids": {"type": "string", "description": "Event ids to cancel, comma separated"},
                "allow_feedback_deletion": {"type": "boolean", "default": False},
                "allow_reschedule": {
                    "type": "boolean",
                    "description": "For cancel: allow rescheduling later",
                    "default": False,
                },
            },
            ["action", "interview_schedule_id"],
            1,
        ),
        definition(
            "list_ashby_interview_config",
            "Ashby interview configuration. 'stages' with job_id: that job's interview plan, "
            "its stages in order with their types and the interviews of each stage (the stage "
            "ids change_ashby_application_stage and transfer_ashby_application take); with "
            "interview_plan_id: that plan's stages; with neither: every published plan and its "
            "stages. 'plans', 'stage_groups', 'interviews' (interview definitions, paged) and "
            "'interviewer_pools' list those; 'all' = plans with stages and the stage groups.",
            {
                "config_type": {"type": "string", "enum": CONFIG_TYPES, "default": "all"},
                "job_id": id_schema("job", "its interview plan and stages"),
                "interview_plan_id": id_schema("interview plan"),
                "include_archived": {"type": "boolean", "default": False},
                "limit": limit_schema(default=MAX_PAGE),
                "cursor": CURSOR_SCHEMA,
            },
            [],
            0,
        ),
        definition(
            "list_ashby_take_home_assignments",
            "List Ashby take-home assignments of an application or candidate: status, the "
            "candidate's submission and notes, and each reviewer's feedback status.",
            {
                "application_id": id_schema("application"),
                "candidate_id": id_schema("candidate"),
                "limit": limit_schema(),
                "cursor": CURSOR_SCHEMA,
            },
            [],
            1,
        ),
        definition(
            "download_ashby_interview_transcript",
            "Download the AI Notetaker transcript of an Ashby interview (its id appears on the "
            "interview event in list_ashby_interviews / get_ashby_interview; needs Ashby's AI "
            "Notetaker add-on) into the conversation: a download card and a file_id, its "
            "readable content with the result. Include the returned [[FILE:...]] marker verbatim.",
            {"notetaker_transcript_id": id_schema("notetaker transcript")},
            ["notetaker_transcript_id"],
            1,
        ),
    ]
