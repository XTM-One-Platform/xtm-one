"""The organization around the pipeline: users, departments, locations, sources,
tags, reference data, custom fields, hiring teams, projects, referrals, reports,
surveys, approvals and the API key itself."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    CURSOR_SCHEMA,
    DEFAULT_PAGE,
    MAX_SCAN,
    SINCE,
    AshbyContext,
    build_field_submissions,
    clamp,
    custom_field_lines,
    definition,
    describe_form,
    fmt_project,
    id_schema,
    limit_schema,
    page_footer,
    parse_json_arg,
    person_name,
    render_submitted_values,
    set_if,
    to_epoch_ms,
    ts,
    user_label,
)

REFERENCE_KINDS = [
    "archive_reasons",
    "close_reasons",
    "job_boards",
    "job_templates",
    "brands",
    "hiring_team_roles",
    "application_hiring_team_roles",
    "communication_templates",
    "custom_fields",
    "source_tracking_links",
    "survey_forms",
]
CUSTOM_FIELD_OBJECTS = ["Application", "Candidate", "Job", "Opening"]
SURVEY_TYPES = ["CandidateExperience", "Questionnaire", "HiringManagerExperience"]
APPROVAL_ENTITIES = ["offer", "job", "opening"]
_TEAM_ACTIONS = ["add", "remove"]
_PROJECT_ACTIONS = ["create", "update", "archive", "restore"]
_REPORT_POLL_SECONDS = 3.0
_REPORT_WAIT_SECONDS = 60.0
_MAX_REPORT_ROWS = 500
_MAX_CELL = 80


def fmt_user(u: dict[str, Any]) -> str:
    state = "" if u.get("isEnabled", True) else " [deactivated]"
    role = f" - {u['globalRole']}" if u.get("globalRole") else ""
    manager = f" | manager: {u['managerId']}" if u.get("managerId") else ""
    return f"  - {user_label(u)}{role}{state}{manager}"


def _fmt_location(loc: dict[str, Any], parents: dict[str, str]) -> str:
    flags = [f for f, on in (("remote", loc.get("isRemote")), ("archived", loc.get("isArchived"))) if on]
    kind = f" [{loc['workplaceType']}]" if loc.get("workplaceType") else ""
    line = f"  - {loc.get('name') or 'Unnamed'}{kind} (ID: {loc.get('id', '?')})"
    if loc.get("type") == "LocationHierarchy":
        line += " - region"
    if flags:
        line += f" ({', '.join(flags)})"
    address = ((loc.get("address") or {}).get("postalAddress")) or {}
    parts = [
        address.get(k) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry")
    ]
    if any(parts):
        line += f"\n      {', '.join(p for p in parts if p)}"
    parent = loc.get("parentLocationId")
    if parent:
        line += f"\n      in {parents.get(parent) or parent}"
    return line


def _fmt_reference(kind: str, r: dict[str, Any]) -> str:
    rid = r.get("id", "?")
    archived = " [archived]" if r.get("isArchived") else ""
    if kind == "archive_reasons":
        return f"  - {r.get('text') or 'Reason'} [{r.get('reasonType') or '?'}]{archived} (ID: {rid})"
    if kind == "close_reasons":
        return f"  - {r.get('reasonText') or 'Reason'}{archived} (ID: {rid})"
    if kind == "job_boards":
        internal = " [internal]" if r.get("isInternal") else ""
        return f"  - {r.get('title') or 'Board'}{internal} (ID: {rid})"
    if kind == "job_templates":
        return (
            f"  - {r.get('title') or 'Template'} [{r.get('status') or '?'}] (ID: {rid}) | department "
            f"{r.get('departmentId') or '-'} | location {r.get('locationId') or '-'} | default plan "
            f"{r.get('defaultInterviewPlanId') or '-'}"
        )
    if kind == "brands":
        slug = f" | jobs page: {r['hostedJobsPageSlug']}" if r.get("hostedJobsPageSlug") else ""
        return f"  - {r.get('name') or 'Brand'} (ID: {rid}){slug}"
    if kind in ("hiring_team_roles", "application_hiring_team_roles"):
        if isinstance(r, str):
            return f"  - {r}"
        return f"  - {r.get('title') or 'Role'} (ID: {rid})"
    if kind == "communication_templates":
        types = ", ".join(r.get("intendedTypes") or [])
        return f"  - {r.get('title') or 'Template'} (ID: {rid}){f' | {types}' if types else ''}"
    if kind == "custom_fields":
        flags = [
            f
            for f, on in (
                ("required", r.get("isRequired")),
                ("private", r.get("isPrivate")),
                ("API-only", r.get("isEditableOnlyViaPublicApi")),
            )
            if on
        ]
        line = (
            f"  - {r.get('title') or 'Field'} [{r.get('objectType') or '?'} / {r.get('fieldType') or '?'}]"
            f"{archived}{' (' + ', '.join(flags) + ')' if flags else ''} (ID: {rid})"
        )
        options = [o for o in r.get("selectableValues") or [] if isinstance(o, dict) and not o.get("isArchived")]
        if options:
            line += "\n      options: " + ", ".join(f"{o.get('label')}={o.get('value')}" for o in options[:30])
        return line
    if kind == "source_tracking_links":
        state = "" if r.get("enabled", True) else " [disabled]"
        return f"  - {r.get('code') or 'code'}{state}: {r.get('link') or '?'} (source: {r.get('sourceId', '?')}, ID: {rid})"
    return f"  - {r.get('title') or 'Survey'} [{r.get('surveyType') or '?'}]{archived} (ID: {rid})"


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("|", "/").replace("\n", " ")
    return text if len(text) <= _MAX_CELL else text[: _MAX_CELL - 3] + "..."


def register(ctx: AshbyContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    async def get_ashby_api_key_info() -> str:
        async with ctx.client() as client:
            info, err = await ctx.call(client, "apiKey.info", {})
        if err:
            return err
        info = info if isinstance(info, dict) else {}
        scopes = sorted(str(s) for s in info.get("scopes") or [])
        lines = [
            f"**Ashby API key: {info.get('title') or 'untitled'}**",
            f"  Created: {ts(info.get('createdAt'))} | API version: {info.get('version') or '?'}",
            f"  Permissions ({len(scopes)}): {', '.join(scopes) or 'none listed'}",
            "  A tool answering 'missing permission' names the one to add under Admin > "
            "Integrations > API Keys; confidential jobs and private fields are separate settings there.",
        ]
        return "\n".join(lines)

    async def list_ashby_departments(include_archived: bool = False) -> str:
        async with ctx.client() as client:
            departments, err = await ctx.collect_all(
                client, "department.list", {"includeArchived": bool(include_archived)}
            )
        if err:
            return err
        names = {d.get("id"): d.get("name") for d in departments}
        lines = []
        for d in departments:
            parent = f" (under {names.get(d['parentId']) or d['parentId']})" if d.get("parentId") else ""
            external = (
                f" - public name: {d['externalName']}"
                if d.get("externalName") and d["externalName"] != d.get("name")
                else ""
            )
            archived = " [archived]" if d.get("isArchived") else ""
            lines.append(f"  - {d.get('name') or 'Unnamed'}{parent}{archived} (ID: {d.get('id', '?')}){external}")
        if not lines:
            return "No departments found."
        return f"**Departments** ({len(lines)}):\n" + "\n".join(lines)

    async def list_ashby_locations(include_archived: bool = False) -> str:
        async with ctx.client() as client:
            locations, err = await ctx.collect_all(
                client,
                "location.list",
                {"includeArchived": bool(include_archived), "includeLocationHierarchy": True},
            )
        if err:
            return err
        parents = {loc.get("id"): loc.get("name") for loc in locations}
        if not locations:
            return "No locations found."
        return f"**Locations** ({len(locations)}):\n" + "\n".join(_fmt_location(loc, parents) for loc in locations)

    async def list_ashby_users(include_deactivated: bool = False, limit: int = 50, cursor: str = "") -> str:
        async with ctx.client() as client:
            page, err = await ctx.page(
                client,
                "user.list",
                {"includeDeactivated": bool(include_deactivated)},
                limit=clamp(limit, default=50),
                cursor=cursor,
            )
        if err:
            return err
        if not page.items:
            return "No users found."
        return (
            f"**Users** ({len(page.items)}):\n"
            + "\n".join(fmt_user(u) for u in page.items)
            + page_footer(page, noun="user")
        )

    async def search_ashby_users(term: str = "", email: str = "", name: str = "") -> str:
        email = email.strip() or (term.strip() if "@" in term else "")
        name = name.strip() or (term.strip() if term.strip() and "@" not in term else "")
        if not (email or name):
            return "Error: give an email or a name to search users."
        async with ctx.client() as client:
            if email:
                users, err = await ctx.call(client, "user.search", {"email": email})
                if err:
                    return err
                found = [u for u in users or [] if isinstance(u, dict)]
            else:
                users, err = await ctx.collect_all(
                    client, "user.list", {"includeDeactivated": True}, max_items=MAX_SCAN * 2
                )
                if err:
                    return err
                needle = name.casefold()
                found = [u for u in users if needle in person_name(u).casefold()]
        if not found:
            return f"No Ashby user matches {email or name!r}."
        return f"**Users matching {email or name!r}** ({len(found)}):\n" + "\n".join(fmt_user(u) for u in found)

    async def get_ashby_user(user_id: str) -> str:
        async with ctx.client() as client:
            user, err = await ctx.call(client, "user.info", {"userId": user_id})
        if err:
            return err
        if not isinstance(user, dict):
            return f"Error: Ashby returned no user for {user_id}."
        return "\n".join([fmt_user(user).strip(), *custom_field_lines(user.get("customFields"))])

    async def list_ashby_sources(include_archived: bool = False) -> str:
        async with ctx.client() as client:
            sources, err = await ctx.call(client, "source.list", {"includeArchived": bool(include_archived)})
        if err:
            return err
        lines = []
        for s in [s for s in sources or [] if isinstance(s, dict)]:
            kind = (s.get("sourceType") or {}).get("title")
            archived = " [archived]" if s.get("isArchived") else ""
            lines.append(
                f"  - {s.get('title') or 'Unnamed'}{f' ({kind})' if kind else ''}{archived} (ID: {s.get('id', '?')})"
            )
        return f"**Sources** ({len(lines)}):\n" + "\n".join(lines) if lines else "No sources found."

    async def list_ashby_candidate_tags(include_archived: bool = False) -> str:
        async with ctx.client() as client:
            tags, err = await ctx.collect_all(client, "candidateTag.list", {"includeArchived": bool(include_archived)})
        if err:
            return err
        lines = [
            f"  - {t.get('title') or 'Unnamed'}{' [archived]' if t.get('isArchived') else ''} (ID: {t.get('id', '?')})"
            for t in tags
        ]
        return f"**Candidate tags** ({len(lines)}):\n" + "\n".join(lines) if lines else "No candidate tags found."

    async def create_ashby_candidate_tag(title: str) -> str:
        async with ctx.client() as client:
            tag, err = await ctx.call(client, "candidateTag.create", {"title": title})
        if err:
            return err
        tag = tag if isinstance(tag, dict) else {}
        return f"Tag '{tag.get('title') or title}' ready (ID: {tag.get('id', '?')}; an existing tag of that title is reused)."

    async def list_ashby_reference_data(kind: str, include_archived: bool = False, source_id: str = "") -> str:
        kind = (kind or "").strip().lower()
        if kind not in REFERENCE_KINDS:
            return f"Error: kind must be one of {', '.join(REFERENCE_KINDS)}."
        archived = {"includeArchived": bool(include_archived)}
        endpoints: dict[str, tuple[str, dict[str, Any], bool]] = {
            "archive_reasons": ("archiveReason.list", archived, False),
            "close_reasons": ("closeReason.list", archived, False),
            "job_boards": ("jobBoard.list", {}, False),
            "job_templates": ("jobTemplate.list", {}, True),
            "brands": ("brand.list", {}, True),
            "hiring_team_roles": ("hiringTeamRole.list", {"namesOnly": False}, False),
            "application_hiring_team_roles": ("applicationHiringTeamRole.list", {}, False),
            "communication_templates": ("communicationTemplate.list", {}, False),
            "custom_fields": ("customField.list", archived, True),
            "source_tracking_links": ("sourceTrackingLink.list", {"includeDisabled": bool(include_archived)}, True),
            "survey_forms": ("surveyFormDefinition.list", {}, True),
        }
        endpoint, body, paged = endpoints[kind]
        if kind == "source_tracking_links":
            set_if(body, "sourceId", source_id)
        async with ctx.client() as client:
            if paged:
                records, err = await ctx.collect_all(client, endpoint, body)
            else:
                records, err = await ctx.call(client, endpoint, body)
        if err:
            return err
        records = [r for r in records or [] if isinstance(r, (dict, str))]
        if not records:
            return f"No {kind.replace('_', ' ')} found."
        title = kind.replace("_", " ").capitalize()
        return f"**{title}** ({len(records)}):\n" + "\n".join(_fmt_reference(kind, r) for r in records)

    async def set_ashby_custom_field_value(object_type: str, object_id: str, field_id: str, value: Any) -> str:
        kind = {o.lower(): o for o in CUSTOM_FIELD_OBJECTS}.get((object_type or "").strip().lower())
        if not kind:
            return f"Error: object_type must be one of {', '.join(CUSTOM_FIELD_OBJECTS)}."
        parsed = value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = value
        body = {"objectType": kind, "objectId": object_id, "fieldId": field_id, "fieldValue": parsed}
        async with ctx.client() as client:
            entry, err = await ctx.call(client, "customField.setValue", body)
        if err:
            return err
        entry = entry if isinstance(entry, dict) else {}
        shown = entry.get("valueLabel") or entry.get("value")
        cleared = parsed is None
        return f"Custom field {entry.get('title') or field_id} on {kind} {object_id} " + (
            "cleared." if cleared else f"set to {shown if shown not in (None, '') else parsed}."
        )

    async def manage_ashby_hiring_team(
        action: str,
        user_id: str,
        role_id: str,
        application_id: str = "",
        job_id: str = "",
        opening_id: str = "",
    ) -> str:
        action = (action or "").strip().lower()
        if action not in _TEAM_ACTIONS:
            return "Error: action must be 'add' or 'remove'."
        targets = [
            (k, v.strip())
            for k, v in (("applicationId", application_id), ("jobId", job_id), ("openingId", opening_id))
            if v and v.strip()
        ]
        if len(targets) != 1:
            return "Error: give exactly one of application_id, job_id or opening_id."
        key, target = targets[0]
        body = {key: target, "teamMemberId": user_id, "roleId": role_id}
        if key == "applicationId":
            endpoint = "application.addHiringTeamMember" if action == "add" else "application.removeHiringTeamMember"
        else:
            endpoint = "hiringTeam.addMember" if action == "add" else "hiringTeam.removeMember"
        async with ctx.client() as client:
            team, err = await ctx.call(client, endpoint, body)
        if err:
            return err
        members = team if isinstance(team, list) else [team] if isinstance(team, dict) else []
        roster = "; ".join(f"{user_label(m)} - {m.get('role') or '?'}" for m in members if isinstance(m, dict))
        verb = "added to" if action == "add" else "removed from"
        return f"User {user_id} {verb} the hiring team of {key[:-2]} {target}." + (f" Team: {roster}" if roster else "")

    async def list_ashby_projects(created_after: str = "", limit: int = DEFAULT_PAGE, cursor: str = "") -> str:
        body: dict[str, Any] = {}
        ms, err = to_epoch_ms(created_after, "created_after")
        if err:
            return err
        set_if(body, "createdAfter", ms)
        async with ctx.client() as client:
            page, err = await ctx.page(client, "project.list", body, limit=clamp(limit), cursor=cursor)
        if err:
            return err
        return format_entity_list(page.items, fmt_project, noun="project") + page_footer(page, noun="project")

    async def search_ashby_projects(title: str = "", term: str = "") -> str:
        title = title.strip() or term.strip()
        if not title:
            return "Error: give a project title (or part of one)."
        async with ctx.client() as client:
            projects, err = await ctx.call(client, "project.search", {"title": title})
        if err:
            return err
        return format_entity_list(
            [p for p in projects or [] if isinstance(p, dict)],
            fmt_project,
            noun="project",
            empty_msg=f"No project matches {title!r}.",
        )

    async def manage_ashby_project(
        action: str, project_id: str = "", title: str = "", description: str = "", confidential: bool = False
    ) -> str:
        action = (action or "").strip().lower()
        if action not in _PROJECT_ACTIONS:
            return f"Error: action must be one of {', '.join(_PROJECT_ACTIONS)}."
        if action != "create" and not project_id:
            return f"Error: {action} needs the project_id."
        body: dict[str, Any] = {} if action == "create" else {"projectId": project_id}
        if action in ("create", "update"):
            set_if(body, "title", title)
            if description.strip():
                body["description"] = {"type": "text/plain", "value": description}
            if action == "create":
                if not title.strip():
                    return "Error: a new project needs a title."
                body["confidential"] = bool(confidential)
            elif len(body) == 1:
                return "Error: give a title or a description to update."
        async with ctx.client() as client:
            project, err = await ctx.call(client, f"project.{action}", body)
        if err:
            return err
        project = project if isinstance(project, dict) else {"id": project_id, "title": title}
        return f"Project {action} done:\n" + fmt_project(project)

    async def list_ashby_referrals(
        application_id: str = "", created_after: str = "", limit: int = DEFAULT_PAGE, cursor: str = ""
    ) -> str:
        body: dict[str, Any] = {}
        set_if(body, "applicationId", application_id)
        ms, err = to_epoch_ms(created_after, "created_after")
        if err:
            return err
        set_if(body, "createdAfter", ms)
        async with ctx.client() as client:
            page, err = await ctx.page(client, "referral.list", body, limit=clamp(limit), cursor=cursor)
        if err:
            return err

        def fmt(r: dict[str, Any]) -> str:
            lines = [
                f"**Referral** (ID: {r.get('id', '?')}) - {r.get('referralType') or '?'} by "
                f"{user_label(r.get('referrer')) or 'unknown'} on {ts(r.get('createdAt'))}",
                f"  Candidate: {r.get('candidateId', '?')} | Application: {r.get('applicationId', '?')} | Job: {r.get('jobId', '?')}",
            ]
            lines.extend(render_submitted_values(r.get("formDefinition"), r.get("submittedValues"), prefix="  "))
            return "\n".join(lines)

        return format_entity_list(page.items, fmt, noun="referral") + page_footer(page, noun="referral")

    async def create_ashby_referral(credited_to_user_id: str, field_values: Any = "") -> str:
        values, err = parse_json_arg(field_values, "field_values")
        if err:
            return err
        async with ctx.client() as client:
            form, err = await ctx.call(client, "referralForm.info", {})
            if err:
                return err
            form = form if isinstance(form, dict) else {}
            if not values:
                return (
                    f"No values given. The referral form '{form.get('title') or 'Referral'}' fields "
                    "(pass field_values as a JSON object of path or title -> value):\n"
                    + describe_form(form.get("formDefinition"))
                )
            submissions, err = build_field_submissions(form.get("formDefinition"), values)
            if err:
                return err
            application, err = await ctx.call(
                client,
                "referral.create",
                {"id": form.get("id"), "creditedToUserId": credited_to_user_id, "fieldSubmissions": submissions},
            )
        if err:
            return err
        application = application if isinstance(application, dict) else {}
        candidate = application.get("candidate") or {}
        job = application.get("job") or {}
        return (
            f"Referral created: application {application.get('id', '?')} for "
            f"{candidate.get('name') or 'the candidate'} (ID: {candidate.get('id', '?')}) on "
            f"{job.get('title') or 'the job'} (ID: {job.get('id', '?')})."
        )

    async def get_ashby_report(report_id: str, max_rows: int = 100) -> str:
        rows = clamp(max_rows, default=100, maximum=_MAX_REPORT_ROWS)
        async with ctx.client() as client:
            result, err = await ctx.call(
                client,
                "report.synchronous",
                {"reportId": report_id, "includeHeadersInData": False, "resultStyle": "inline"},
            )
            if err:
                return err
            result = result if isinstance(result, dict) else {}
            waited = 0.0
            while result.get("status") in ("requested", "in_progress") and waited < _REPORT_WAIT_SECONDS:
                await asyncio.sleep(_REPORT_POLL_SECONDS)
                waited += _REPORT_POLL_SECONDS
                polled, err = await ctx.call(
                    client,
                    "report.generate",
                    {"reportId": report_id, "requestId": result.get("requestId"), "includeHeadersInData": False},
                )
                if err:
                    return err
                result = polled if isinstance(polled, dict) else {}
        status = result.get("status")
        if status == "failed":
            return f"Error: Ashby could not generate report {report_id}: {result.get('failureReason') or 'no reason given'}."
        if status != "complete":
            return (
                f"Report {report_id} is still being generated (request ID: {result.get('requestId', '?')}). "
                "Call get_ashby_report again in a minute."
            )
        data = result.get("reportData") or {}
        meta = data.get("metadata") or {}
        header = f"**Report: {meta.get('title') or report_id}** (generated {ts(meta.get('reportGeneratedAt'))})"
        if data.get("url"):
            return f"{header}\n  The report is served as a file: {data['url']}"
        columns = [str(c) for c in data.get("columnNames") or []]
        table = [r for r in data.get("data") or [] if isinstance(r, list)]
        lines = [
            header,
            f"  {len(table)} rows x {len(columns)} columns" + (f" (first {rows} shown)" if len(table) > rows else ""),
        ]
        if columns:
            lines.append("| " + " | ".join(_cell(c) for c in columns) + " |")
            lines.append("|" + "---|" * len(columns))
        lines.extend("| " + " | ".join(_cell(v) for v in row) + " |" for row in table[:rows])
        return "\n".join(lines)

    async def list_ashby_survey_submissions(
        survey_type: str, created_after: str = "", limit: int = DEFAULT_PAGE, cursor: str = ""
    ) -> str:
        kind = {s.lower(): s for s in SURVEY_TYPES}.get((survey_type or "").strip().lower())
        if not kind:
            return f"Error: survey_type must be one of {', '.join(SURVEY_TYPES)}."
        body: dict[str, Any] = {"surveyType": kind}
        ms, err = to_epoch_ms(created_after, "created_after")
        if err:
            return err
        set_if(body, "createdAfter", ms)
        async with ctx.client() as client:
            page, err = await ctx.page(client, "surveySubmission.list", body, limit=clamp(limit), cursor=cursor)
        if err:
            return err

        def fmt(s: dict[str, Any]) -> str:
            lines = [
                f"**Survey submission** (ID: {s.get('id', '?')}) - {ts(s.get('submittedAt'))}",
                f"  Candidate: {s.get('candidateId') or '-'} | Application: {s.get('applicationId') or '-'}",
            ]
            lines.extend(render_submitted_values(s.get("formDefinition"), s.get("submittedValues"), prefix="  "))
            return "\n".join(lines)

        return format_entity_list(page.items, fmt, noun="survey submission") + page_footer(
            page, noun="survey submission"
        )

    async def list_ashby_approvals(
        entity_type: str = "", entity_id: str = "", limit: int = DEFAULT_PAGE, cursor: str = ""
    ) -> str:
        body: dict[str, Any] = {}
        if entity_type:
            kind = entity_type.strip().lower()
            if kind not in APPROVAL_ENTITIES:
                return f"Error: entity_type must be one of {', '.join(APPROVAL_ENTITIES)}."
            body["entityType"] = kind
        set_if(body, "entityId", entity_id)
        async with ctx.client() as client:
            page, err = await ctx.page(client, "approval.list", body, limit=clamp(limit), cursor=cursor)
            if err:
                return err
            approver_ids = [
                a.get("userId")
                for item in page.items
                for step in item.get("steps") or []
                for a in step.get("approvers") or []
                if isinstance(a, dict)
            ]
            users = await ctx.users(client, approver_ids)

        def fmt(item: dict[str, Any]) -> str:
            lines = [
                f"**Approval** (ID: {item.get('id', '?')}) - {item.get('status') or '?'} | "
                f"{item.get('entityType') or '?'} {item.get('entityId', '?')}",
                "  "
                + " | ".join(
                    f"{label}: {ts(item[key])}"
                    for label, key in (
                        ("Created", "createdAt"),
                        ("Submitted", "submittedAt"),
                        ("Completed", "completedAt"),
                    )
                    if item.get(key)
                ),
            ]
            for number, step in enumerate(item.get("steps") or [], 1):
                approvers = [
                    f"{user_label(users.get(a.get('userId'))) or a.get('userId')}: {a.get('decision') or 'pending'}"
                    for a in step.get("approvers") or []
                    if isinstance(a, dict)
                ]
                done = f", completed {ts(step['completedAt'])}" if step.get("completedAt") else ""
                lines.append(
                    f"  Step {number} ({step.get('approvalsRequired', '?')} required{done}): {'; '.join(approvers) or 'no approvers'}"
                )
            return "\n".join(lines)

        return format_entity_list(page.items, fmt, noun="approval") + page_footer(page, noun="approval")

    for fn in (
        get_ashby_api_key_info,
        list_ashby_departments,
        list_ashby_locations,
        list_ashby_users,
        search_ashby_users,
        get_ashby_user,
        list_ashby_sources,
        list_ashby_candidate_tags,
        create_ashby_candidate_tag,
        list_ashby_reference_data,
        set_ashby_custom_field_value,
        manage_ashby_hiring_team,
        list_ashby_projects,
        search_ashby_projects,
        manage_ashby_project,
        list_ashby_referrals,
        create_ashby_referral,
        get_ashby_report,
        list_ashby_survey_submissions,
        list_ashby_approvals,
    ):
        executor.register(fn.__name__, fn)

    archived = {"type": "boolean", "description": "Include archived records", "default": False}
    since = {"type": "string", "description": f"Created at or after: {SINCE}"}
    return [
        definition(
            "get_ashby_api_key_info",
            "Show the Ashby API key in use: its name, API version and the permissions "
            "(scopes) it holds. Use it when a tool answers that a permission is missing, or "
            "a search returns nothing for a record that exists.",
            {},
            [],
            2,
        ),
        definition(
            "list_ashby_departments",
            "List Ashby departments (teams) with their parent department and ids - the "
            "department_id the job and opening tools take.",
            {"include_archived": archived},
            [],
            1,
        ),
        definition(
            "list_ashby_locations",
            "List Ashby locations and regions with workplace type, remote flag, address and parent region.",
            {"include_archived": archived},
            [],
            1,
        ),
        definition(
            "list_ashby_users",
            "List Ashby users (recruiters, hiring managers, interviewers) with email, global "
            "role, manager and id. Active users only unless include_deactivated.",
            {
                "include_deactivated": {"type": "boolean", "default": False},
                "limit": limit_schema(default=50),
                "cursor": CURSOR_SCHEMA,
            },
            [],
            1,
        ),
        definition(
            "search_ashby_users",
            "Find an Ashby user by email (exact) or by name (part of it, across every user). "
            "'term' is a shortcut: an address with '@' is an email, anything else a name.",
            {
                "term": {"type": "string", "description": "An email or a name", "default": ""},
                "email": {"type": "string", "default": ""},
                "name": {"type": "string", "default": ""},
            },
            [],
            0,
        ),
        definition(
            "get_ashby_user",
            "Get one Ashby user by id: name, email, global role, manager, status, custom fields.",
            {"user_id": id_schema("user")},
            ["user_id"],
            1,
        ),
        definition(
            "list_ashby_sources",
            "List Ashby candidate sources (LinkedIn, Referral, Careers page...) with their type "
            "and ids - the source_id the candidate and application tools take.",
            {"include_archived": archived},
            [],
            1,
        ),
        definition(
            "list_ashby_candidate_tags",
            "List Ashby candidate tags with their ids.",
            {"include_archived": archived},
            [],
            1,
        ),
        definition(
            "create_ashby_candidate_tag",
            "Create an Ashby candidate tag (an existing tag of the same title is returned instead).",
            {"title": {"type": "string", "description": "Tag title"}},
            ["title"],
            2,
        ),
        definition(
            "list_ashby_reference_data",
            "Ashby reference lists by kind: archive_reasons (for archiving an application), "
            "close_reasons (closing a job or opening), job_boards, job_templates, brands, "
            "hiring_team_roles (jobs, openings), application_hiring_team_roles, "
            "communication_templates, custom_fields (with object type, field type, options), "
            "source_tracking_links, survey_forms.",
            {
                "kind": {"type": "string", "enum": REFERENCE_KINDS},
                "include_archived": {**archived, "description": "Include archived (or disabled) records"},
                "source_id": id_schema("source", "source_tracking_links of one source"),
            },
            ["kind"],
            1,
        ),
        definition(
            "set_ashby_custom_field_value",
            "Set (or clear with null) a custom field on an Ashby application, candidate, job or "
            "opening. value is JSON when it parses (number, true/false, a list of option values, "
            '{"value": 1000, "currencyCode": "EUR"}, null), else the text as given. Field '
            "ids and options: list_ashby_reference_data(kind='custom_fields').",
            {
                "object_type": {"type": "string", "enum": CUSTOM_FIELD_OBJECTS},
                "object_id": {"type": "string", "description": "Id of the application, candidate, job or opening"},
                "field_id": id_schema("custom field"),
                "value": {"type": "string", "description": "The value (JSON or text); 'null' clears it"},
            },
            ["object_type", "object_id", "field_id", "value"],
            1,
        ),
        definition(
            "manage_ashby_hiring_team",
            "Add a user to, or remove them from, the hiring team of an Ashby application, job "
            "or opening, in a role (roles: list_ashby_reference_data(kind='hiring_team_roles'), "
            "or 'application_hiring_team_roles' for an application).",
            {
                "action": {"type": "string", "enum": _TEAM_ACTIONS},
                "user_id": id_schema("user"),
                "role_id": id_schema("hiring team role"),
                "application_id": id_schema("application"),
                "job_id": id_schema("job"),
                "opening_id": id_schema("opening"),
            },
            ["action", "user_id", "role_id"],
            1,
        ),
        definition(
            "list_ashby_projects",
            "List Ashby projects (talent pools / sourcing pipelines) with description and flags.",
            {"created_after": since, "limit": limit_schema(), "cursor": CURSOR_SCHEMA},
            [],
            1,
        ),
        definition(
            "search_ashby_projects",
            "Find Ashby projects (talent pools) by title (up to 100 matches).",
            {
                "title": {"type": "string", "description": "Project title or part of it", "default": ""},
                "term": {"type": "string", "description": "Shortcut for title", "default": ""},
            },
            [],
            1,
        ),
        definition(
            "manage_ashby_project",
            "Create an Ashby project (talent pool), update its title or description, archive or restore it.",
            {
                "action": {"type": "string", "enum": _PROJECT_ACTIONS},
                "project_id": id_schema("project", "every action but create"),
                "title": {"type": "string"},
                "description": {"type": "string", "description": "Plain text description"},
                "confidential": {"type": "boolean", "description": "create: confidential project", "default": False},
            },
            ["action"],
            2,
        ),
        definition(
            "list_ashby_referrals",
            "List Ashby referrals (of one application or all those the key may see): who "
            "referred whom for which job, and the referral form answers.",
            {
                "application_id": id_schema("application"),
                "created_after": since,
                "limit": limit_schema(),
                "cursor": CURSOR_SCHEMA,
            },
            [],
            1,
        ),
        definition(
            "create_ashby_referral",
            "Submit a referral through Ashby's referral form, credited to a user. Call without "
            "field_values to see the form's fields; then pass a JSON object of field (path or "
            "title) -> value.",
            {
                "credited_to_user_id": id_schema("user", "the referrer"),
                "field_values": {"type": "string", "description": "JSON object of field -> value", "default": ""},
            },
            ["credited_to_user_id"],
            1,
        ),
        definition(
            "get_ashby_report",
            "Run a saved Ashby report by its id (from the report's URL in Ashby) and return its "
            "rows as a table. Ashby allows 15 report runs a minute per organization; a long "
            "report is polled for up to a minute.",
            {
                "report_id": id_schema("report"),
                "max_rows": {"type": "integer", "description": f"Rows to show (1-{_MAX_REPORT_ROWS})", "default": 100},
            },
            ["report_id"],
            1,
        ),
        definition(
            "list_ashby_survey_submissions",
            "List Ashby survey answers of one type (candidate experience, questionnaire, hiring "
            "manager experience) with the candidate, application and every answer.",
            {
                "survey_type": {"type": "string", "enum": SURVEY_TYPES},
                "created_after": since,
                "limit": limit_schema(),
                "cursor": CURSOR_SCHEMA,
            },
            ["survey_type"],
            2,
        ),
        definition(
            "list_ashby_approvals",
            "List Ashby approvals (offers, jobs, openings): status, dates, and each step's "
            "approvers with their decision.",
            {
                "entity_type": {"type": "string", "enum": APPROVAL_ENTITIES},
                "entity_id": {"type": "string", "description": "Only the approvals of this offer, job or opening"},
                "limit": limit_schema(),
                "cursor": CURSOR_SCHEMA,
            },
            [],
            2,
        ),
    ]
