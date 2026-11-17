"""Interview feedback: scorecards on applications and the forms they fill."""

from __future__ import annotations

from typing import Any

from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    CURSOR_SCHEMA,
    SINCE,
    AshbyContext,
    build_field_submissions,
    clamp,
    definition,
    describe_form,
    form_fields,
    id_schema,
    limit_schema,
    page_footer,
    parse_json_arg,
    render_submitted_values,
    set_if,
    to_epoch_ms,
    ts,
    user_label,
    warnings_note,
)

_DEFAULT_FEEDBACK = 20


def _fmt_form(form: dict[str, Any]) -> str:
    flags = [f for f, on in (("default", form.get("isDefaultForm")), ("archived", form.get("isArchived"))) if on]
    line = f"**{form.get('title') or 'Untitled form'}** (ID: {form.get('id', '?')})"
    if flags:
        line += f" [{', '.join(flags)}]"
    fields = form_fields(form.get("formDefinition"))
    line += f"\n  {len(fields)} fields"
    if form.get("interviewId"):
        line += f" | interview: {form['interviewId']}"
    return line


def register(ctx: AshbyContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    async def list_ashby_application_feedback(
        application_id: str,
        limit: int = _DEFAULT_FEEDBACK,
        cursor: str = "",
        created_after: str = "",
    ) -> str:
        body: dict[str, Any] = {"applicationId": application_id}
        ms, err = to_epoch_ms(created_after, "created_after")
        if err:
            return err
        set_if(body, "createdAfter", ms)
        async with ctx.client() as client:
            page, err = await ctx.page(
                client, "applicationFeedback.list", body, limit=clamp(limit, default=_DEFAULT_FEEDBACK), cursor=cursor
            )
            if err:
                return err
            items = sorted(page.items, key=lambda f: f.get("submittedAt") or "")
            application = (await ctx.applications(client, [application_id])).get(application_id) or {}
            forms = await ctx.feedback_forms(client, (f.get("feedbackFormDefinitionId") for f in items))
            interviews = await ctx.interview_definitions(client, (f.get("interviewId") for f in items))
        if not items:
            return f"No feedback submitted on application {application_id}."
        stage_by_event = {
            h.get("id"): h.get("title") for h in application.get("applicationHistory") or [] if isinstance(h, dict)
        }
        candidate = (application.get("candidate") or {}).get("name")
        job = (application.get("job") or {}).get("title")
        context = f" - {candidate} for {job}" if candidate and job else ""
        count = f"{len(items)} submission{'s' if len(items) != 1 else ''}"
        lines = [f"**Feedback on application {application_id}**{context} ({count}, oldest first):"]
        for number, fb in enumerate(items, 1):
            author = user_label(fb.get("submittedByUser")) or "unknown author"
            credited = fb.get("creditedToUser") or {}
            credit = ""
            if credited and credited.get("id") != (fb.get("submittedByUser") or {}).get("id"):
                credit = f" (credited to {user_label(credited)})"
            lines.append("")
            lines.append(f"{number}. Submitted {ts(fb.get('submittedAt')) or '?'} by {author}{credit}")
            refs = [f"Feedback ID: {fb.get('id', '?')}"]
            form_id = fb.get("feedbackFormDefinitionId")
            if form_id:
                title = (forms.get(form_id) or {}).get("title")
                refs.append(f"Form: {title + ' ' if title else ''}(ID: {form_id})")
            interview_id = fb.get("interviewId")
            if interview_id:
                title = (interviews.get(interview_id) or {}).get("title")
                refs.append(f"Interview: {title + ' ' if title else ''}(ID: {interview_id})")
            stage = stage_by_event.get(fb.get("applicationHistoryId"))
            if stage:
                refs.append(f"Stage: {stage}")
            if fb.get("interviewEventId"):
                refs.append(f"Interview event: {fb['interviewEventId']}")
            lines.append("   " + " | ".join(refs))
            values = render_submitted_values(fb.get("formDefinition"), fb.get("submittedValues"), prefix="   ")
            lines.extend(values or ["   (no values submitted)"])
        return "\n".join(lines) + page_footer(page, noun="submission")

    async def _form_definition(client: Any, form_id: str) -> tuple[dict[str, Any] | None, str | None]:
        form, err = await ctx.call(client, "feedbackFormDefinition.info", {"feedbackFormDefinitionId": form_id})
        if err:
            return None, err
        return (form or {}).get("formDefinition") if isinstance(form, dict) else None, None

    async def submit_ashby_application_feedback(
        application_id: str,
        feedback_form_definition_id: str,
        submitted_values: Any = "",
        interview_event_id: str = "",
        user_id: str = "",
    ) -> str:
        values, err = parse_json_arg(submitted_values, "submitted_values")
        if err:
            return err
        async with ctx.client() as client:
            form_definition, err = await _form_definition(client, feedback_form_definition_id)
            if err:
                return err
            if not values:
                return (
                    f"No values given. The fields of feedback form {feedback_form_definition_id} "
                    "(pass submitted_values as a JSON object of path or title -> value):\n"
                    + describe_form(form_definition)
                )
            submissions, err = build_field_submissions(form_definition, values)
            if err:
                return err
            body: dict[str, Any] = {
                "applicationId": application_id,
                "formDefinitionId": feedback_form_definition_id,
                "feedbackForm": {"fieldSubmissions": submissions},
            }
            set_if(body, "interviewEventId", interview_event_id)
            set_if(body, "userId", user_id)
            data, err = await ctx.post(client, "applicationFeedback.submit", body)
        if err:
            return err
        instance = ((data.get("results") or {}).get("submittedFormInstance") or {}) if data else {}
        instance_id = instance.get("id", "?") if isinstance(instance, dict) else "?"
        return (
            f"Feedback submitted on application {application_id} ({len(submissions)} fields, "
            f"form instance ID: {instance_id})." + warnings_note(data)
        )

    async def request_ashby_application_feedback(
        application_id: str, interview_id: str, interviewer_user_id: str
    ) -> str:
        body = {
            "applicationId": application_id,
            "interviewId": interview_id,
            "interviewerUserId": interviewer_user_id,
        }
        async with ctx.client() as client:
            result, err = await ctx.call(client, "applicationFeedbackRequest.create", body)
        if err:
            return err
        event_id = (result or {}).get("interviewEventId", "?") if isinstance(result, dict) else "?"
        return (
            f"Feedback requested from user {interviewer_user_id} on application {application_id}. "
            f"Interview event ID: {event_id} (submit_ashby_application_feedback takes it as "
            "interview_event_id)."
        )

    async def list_ashby_feedback_forms(include_archived: bool = False, limit: int = 50, cursor: str = "") -> str:
        async with ctx.client() as client:
            page, err = await ctx.page(
                client,
                "feedbackFormDefinition.list",
                {"includeArchived": bool(include_archived)},
                limit=clamp(limit, default=50),
                cursor=cursor,
            )
        if err:
            return err
        return format_entity_list(page.items, _fmt_form, noun="feedback form") + page_footer(page, noun="feedback form")

    async def get_ashby_feedback_form(feedback_form_definition_id: str) -> str:
        async with ctx.client() as client:
            form, err = await ctx.call(
                client, "feedbackFormDefinition.info", {"feedbackFormDefinitionId": feedback_form_definition_id}
            )
        if err:
            return err
        if not isinstance(form, dict):
            return f"Error: Ashby returned no feedback form for {feedback_form_definition_id}."
        return _fmt_form(form) + "\n  Fields (path: title [type]):\n" + describe_form(form.get("formDefinition"), "  ")

    for fn in (
        list_ashby_application_feedback,
        submit_ashby_application_feedback,
        request_ashby_application_feedback,
        list_ashby_feedback_forms,
        get_ashby_feedback_form,
    ):
        executor.register(fn.__name__, fn)

    application_id = id_schema("application")
    form_id = id_schema("feedback form definition", "list_ashby_feedback_forms")
    return [
        definition(
            "list_ashby_application_feedback",
            "Every interview scorecard / feedback submitted on an Ashby application, oldest "
            "first: who submitted it (name, email) and when, the form, the interview and "
            "stage it was for, and every answer - ratings ('3/4'), selected options by their "
            "label (e.g. the overall recommendation) and the written comments.",
            {
                "application_id": application_id,
                "limit": limit_schema(default=_DEFAULT_FEEDBACK),
                "cursor": CURSOR_SCHEMA,
                "created_after": {"type": "string", "description": f"Submitted at or after: {SINCE}"},
            },
            ["application_id"],
            0,
        ),
        definition(
            "submit_ashby_application_feedback",
            "Submit an interview scorecard on an Ashby application. submitted_values is a JSON "
            "object of field -> value, a field named by its path or its title; a score is a "
            "number 1-4, a select option its label or value, rich text plain text. Call "
            "without submitted_values to see the form's fields. Feedback is credited to "
            "user_id (default: the API key's user).",
            {
                "application_id": application_id,
                "feedback_form_definition_id": form_id,
                "submitted_values": {
                    "type": "string",
                    "description": 'JSON object, e.g. {"overall_recommendation": "Hire", "Technical skills": 3}',
                    "default": "",
                },
                "interview_event_id": id_schema("interview event", "the interview the feedback is for"),
                "user_id": id_schema("user", "credited with the feedback"),
            },
            ["application_id", "feedback_form_definition_id"],
            1,
        ),
        definition(
            "request_ashby_application_feedback",
            "Ask an interviewer for feedback on an Ashby application without scheduling an "
            "interview; returns the interview event id the feedback will be submitted against.",
            {
                "application_id": application_id,
                "interview_id": id_schema("interview", "list_ashby_interview_config(config_type='interviews')"),
                "interviewer_user_id": id_schema("user", "the interviewer"),
            },
            ["application_id", "interview_id", "interviewer_user_id"],
            1,
        ),
        definition(
            "list_ashby_feedback_forms",
            "List Ashby feedback (scorecard) forms with their ids and field counts.",
            {
                "include_archived": {"type": "boolean", "default": False},
                "limit": limit_schema(default=50),
                "cursor": CURSOR_SCHEMA,
            },
            [],
            1,
        ),
        definition(
            "get_ashby_feedback_form",
            "Get one Ashby feedback form: each field's path, title, type, whether it is "
            "required and its options - what submit_ashby_application_feedback takes.",
            {"feedback_form_definition_id": form_id},
            ["feedback_form_definition_id"],
            1,
        ),
    ]
