"""Candidates: profiles, search, notes, tags, projects and anonymization."""

from __future__ import annotations

from typing import Any

from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    CURSOR_SCHEMA,
    DEFAULT_PAGE,
    MAX_PAGE,
    SINCE,
    AshbyContext,
    clamp,
    contact,
    custom_field_lines,
    definition,
    file_handle_list,
    fmt_project,
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


def _location(candidate: dict[str, Any]) -> str:
    location = candidate.get("location")
    if not isinstance(location, dict):
        return ""
    summary = location.get("locationSummary")
    if summary:
        return str(summary)
    parts = location.get("locationComponents") or []
    return ", ".join(str(p.get("name")) for p in parts if isinstance(p, dict) and p.get("name"))


def fmt_candidate(c: dict[str, Any], *, detailed: bool = False) -> str:
    """A candidate profile; *detailed* adds every contact, link, field and file."""
    lines = [f"**{c.get('name') or 'Unnamed candidate'}** (ID: {c.get('id', '?')})"]
    primary_email = contact(c.get("primaryEmailAddress"))
    emails = [contact(e) for e in c.get("emailAddresses") or [] if contact(e)]
    if detailed and emails:
        lines.append(f"  Emails: {', '.join(emails)}")
    elif primary_email or emails:
        lines.append(f"  Email: {primary_email or emails[0]}")
    phones = [contact(p) for p in c.get("phoneNumbers") or [] if contact(p)]
    primary_phone = contact(c.get("primaryPhoneNumber"))
    if detailed and phones:
        lines.append(f"  Phones: {', '.join(phones)}")
    elif primary_phone:
        lines.append(f"  Phone: {primary_phone}")
    role = " at ".join(str(v) for v in (c.get("position"), c.get("company")) if v)
    if role:
        lines.append(f"  Current role: {role}")
    if detailed and c.get("school"):
        lines.append(f"  School: {c['school']}")
    location = _location(c)
    if location:
        lines.append(f"  Location: {location}")
    if detailed and c.get("timezone"):
        lines.append(f"  Timezone: {c['timezone']}")
    tags = [t.get("title") for t in c.get("tags") or [] if isinstance(t, dict) and t.get("title")]
    if tags:
        lines.append(f"  Tags: {', '.join(tags)}")
    source = c.get("source")
    if isinstance(source, dict) and source.get("title"):
        lines.append(f"  Source: {source['title']} (ID: {source.get('id', '?')})")
    if detailed and c.get("creditedToUser"):
        lines.append(f"  Credited to: {user_label(c['creditedToUser'])}")
    application_ids = [a for a in c.get("applicationIds") or [] if a]
    if application_ids:
        shown = ", ".join(application_ids[:10])
        more = f" (+{len(application_ids) - 10} more)" if len(application_ids) > 10 else ""
        lines.append(f"  Applications ({len(application_ids)}): {shown}{more}")
    if c.get("fraudStatus") and c["fraudStatus"] not in ("Unreviewed", "NotFraudulent"):
        lines.append(f"  Fraud status: {c['fraudStatus']}")
    if detailed:
        links = [
            f"{s.get('type')}: {s.get('url')}"
            for s in c.get("socialLinks") or []
            if isinstance(s, dict) and s.get("url")
        ]
        if links:
            lines.append(f"  Links: {', '.join(links)}")
        lines.extend(custom_field_lines(c.get("customFields")))
    if c.get("createdAt"):
        updated = f" | Updated: {ts(c['updatedAt'])}" if detailed and c.get("updatedAt") else ""
        lines.append(f"  Created: {ts(c['createdAt'])}{updated}")
    resume = c.get("resumeFileHandle")
    if isinstance(resume, dict) and resume.get("id"):
        lines.append(f"  Resume: {resume.get('name') or 'resume'} (file id: {resume['id']})")
    files = file_handle_list(c.get("fileHandles"))
    if files:
        lines.append(f"  Files: {files}")
    if c.get("profileUrl"):
        lines.append(f"  Profile: {c['profileUrl']}")
    return "\n".join(lines)


def _location_body(city: str, region: str, country: str) -> dict[str, str] | None:
    location = {k: v.strip() for k, v in (("city", city), ("region", region), ("country", country)) if v and v.strip()}
    return location or None


def register(ctx: AshbyContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    async def list_ashby_candidates(
        limit: int = DEFAULT_PAGE,
        cursor: str = "",
        created_after: str = "",
        created_before: str = "",
    ) -> str:
        body: dict[str, Any] = {}
        for key, value, name in (
            ("createdAfter", created_after, "created_after"),
            ("createdBefore", created_before, "created_before"),
        ):
            ms, err = to_epoch_ms(value, name)
            if err:
                return err
            set_if(body, key, ms)
        async with ctx.client() as client:
            page, err = await ctx.page(client, "candidate.list", body, limit=clamp(limit), cursor=cursor)
        if err:
            return err
        return format_entity_list(page.items, fmt_candidate, noun="candidate") + page_footer(page, noun="candidate")

    async def get_ashby_candidate(candidate_id: str = "", external_mapping_id: str = "") -> str:
        body: dict[str, Any] = {}
        set_if(body, "id", candidate_id)
        set_if(body, "externalMappingId", external_mapping_id)
        if not body:
            return "Error: give the candidate_id (or an external_mapping_id)."
        async with ctx.client() as client:
            candidate, err = await ctx.call(client, "candidate.info", body)
        if err:
            return err
        if not isinstance(candidate, dict):
            return f"Error: Ashby returned no candidate for {candidate_id or external_mapping_id}."
        return fmt_candidate(candidate, detailed=True)

    async def search_ashby_candidates(
        name: str = "", email: str = "", term: str = "", limit: int = DEFAULT_PAGE
    ) -> str:
        name, email, term = name.strip(), email.strip(), term.strip()
        if term and not (name or email):
            if "@" in term:
                email = term
            else:
                name = term
        if not (name or email):
            return "Error: give a name or an email to search candidates."
        queries = [("name", name), ("email", email)]
        found: dict[str, dict[str, Any]] = {}
        more = False
        counts: list[str] = []
        async with ctx.client() as client:
            for key, value in queries:
                if not value:
                    continue
                data, err = await ctx.post(client, "candidate.search", {key: value, "limit": clamp(limit)})
                if err:
                    return err
                results = [r for r in data.get("results") or [] if isinstance(r, dict)]
                more = more or bool(data.get("moreDataAvailable"))
                counts.append(f"{key} '{value}': {len(results)}")
                for record in results:
                    found.setdefault(str(record.get("id")), record)
        if not found:
            return (
                f"No Ashby candidate matches ({'; '.join(counts)}). Ashby compares the "
                "name stored on the profile and the candidate's email addresses: try "
                "the full name or the email, or list recent candidates with "
                "list_ashby_candidates(created_after=...). Candidates who only applied "
                "to confidential jobs are visible only to a key with 'Allow access to "
                "confidential jobs and projects'."
            )
        header = f"Matches - {'; '.join(counts)}"
        footer = "\n[Ashby has more matches than shown - refine the name or search by email]" if more else ""
        return f"{header}\n" + format_entity_list(list(found.values()), fmt_candidate, noun="candidate") + footer

    async def create_ashby_candidate(
        name: str = "",
        first_name: str = "",
        last_name: str = "",
        email: str = "",
        phone: str = "",
        linkedin_url: str = "",
        github_url: str = "",
        website: str = "",
        alternate_emails: str = "",
        source_id: str = "",
        credited_to_user_id: str = "",
        city: str = "",
        region: str = "",
        country: str = "",
    ) -> str:
        full_name = name.strip() or " ".join(p.strip() for p in (first_name, last_name) if p.strip())
        if not full_name:
            return "Error: give the candidate's name (or first_name and last_name)."
        body: dict[str, Any] = {"name": full_name}
        set_if(body, "email", email)
        set_if(body, "phoneNumber", phone)
        set_if(body, "linkedInUrl", linkedin_url)
        set_if(body, "githubUrl", github_url)
        set_if(body, "website", website)
        set_if(body, "sourceId", source_id)
        set_if(body, "creditedToUserId", credited_to_user_id)
        if id_list(alternate_emails):
            body["alternateEmailAddresses"] = id_list(alternate_emails)
        set_if(body, "location", _location_body(city, region, country))
        async with ctx.client() as client:
            candidate, err = await ctx.call(client, "candidate.create", body)
        if err:
            return err
        if not isinstance(candidate, dict):
            return f"Candidate '{full_name}' created."
        return "Created candidate:\n" + fmt_candidate(candidate)

    async def update_ashby_candidate(
        candidate_id: str,
        name: str = "",
        email: str = "",
        phone: str = "",
        linkedin_url: str = "",
        github_url: str = "",
        website_url: str = "",
        alternate_email: str = "",
        source_id: str = "",
        credited_to_user_id: str = "",
        city: str = "",
        region: str = "",
        country: str = "",
        send_notifications: bool = False,
    ) -> str:
        body: dict[str, Any] = {"candidateId": candidate_id}
        set_if(body, "name", name)
        set_if(body, "email", email)
        set_if(body, "phoneNumber", phone)
        set_if(body, "linkedInUrl", linkedin_url)
        set_if(body, "githubUrl", github_url)
        set_if(body, "websiteUrl", website_url)
        set_if(body, "alternateEmail", alternate_email)
        set_if(body, "sourceId", source_id)
        set_if(body, "creditedToUserId", credited_to_user_id)
        set_if(body, "location", _location_body(city, region, country))
        if len(body) == 1:
            return "Error: give at least one field to update."
        body["sendNotifications"] = bool(send_notifications)
        async with ctx.client() as client:
            candidate, err = await ctx.call(client, "candidate.update", body)
        if err:
            return err
        if not isinstance(candidate, dict):
            return f"Candidate {candidate_id} updated."
        return "Updated candidate:\n" + fmt_candidate(candidate)

    async def create_ashby_candidate_note(
        candidate_id: str,
        note: str,
        note_type: str = "text",
        is_private: bool = False,
        send_notifications: bool = False,
    ) -> str:
        if not (note or "").strip():
            return "Error: the note is empty."
        content: Any = note
        if (note_type or "").strip().lower() in ("html", "text/html"):
            content = {"type": "text/html", "value": note}
        body = {
            "candidateId": candidate_id,
            "note": content,
            "isPrivate": bool(is_private),
            "sendNotifications": bool(send_notifications),
        }
        async with ctx.client() as client:
            created, err = await ctx.call(client, "candidate.createNote", body)
        if err:
            return err
        note_id = created.get("id", "?") if isinstance(created, dict) else "?"
        private = " (private)" if is_private else ""
        return f"Note{private} added to candidate {candidate_id} (note ID: {note_id})."

    async def list_ashby_candidate_notes(candidate_id: str, limit: int = DEFAULT_PAGE, cursor: str = "") -> str:
        async with ctx.client() as client:
            page, err = await ctx.page(
                client, "candidate.listNotes", {"candidateId": candidate_id}, limit=clamp(limit), cursor=cursor
            )
        if err:
            return err
        if not page.items:
            return f"No notes on candidate {candidate_id}."
        lines = [f"**Notes on candidate {candidate_id}** ({len(page.items)}, newest first):"]
        for n in page.items:
            author = user_label(n.get("author"), with_id=False) or "unknown author"
            private = " [private]" if n.get("isPrivate") else ""
            lines.append(f"- [{ts(n.get('createdAt'))}] {author}{private} (note ID: {n.get('id', '?')})")
            text = truncate(strip_html(n.get("content")))
            lines.extend(f"    {line}" for line in text.splitlines())
        return "\n".join(lines) + page_footer(page, noun="note")

    async def _tag_call(endpoint: str, candidate_id: str, tag_id: str, verb: str) -> str:
        async with ctx.client() as client:
            candidate, err = await ctx.call(client, endpoint, {"candidateId": candidate_id, "tagId": tag_id})
        if err:
            return err
        tags = []
        if isinstance(candidate, dict):
            tags = [t.get("title") for t in candidate.get("tags") or [] if isinstance(t, dict)]
        current = f" Tags now: {', '.join(t for t in tags if t) or 'none'}." if candidate else ""
        return f"Tag {tag_id} {verb} candidate {candidate_id}.{current}"

    async def add_ashby_candidate_tag(candidate_id: str, tag_id: str) -> str:
        return await _tag_call("candidate.addTag", candidate_id, tag_id, "added to")

    async def remove_ashby_candidate_tag(candidate_id: str, tag_id: str) -> str:
        return await _tag_call("candidate.removeTag", candidate_id, tag_id, "removed from")

    async def _project_call(endpoint: str, candidate_id: str, project_id: str, verb: str) -> str:
        async with ctx.client() as client:
            _, err = await ctx.call(client, endpoint, {"candidateId": candidate_id, "projectId": project_id})
        if err:
            return err
        return f"Candidate {candidate_id} {verb} project {project_id}."

    async def add_ashby_candidate_to_project(candidate_id: str, project_id: str) -> str:
        return await _project_call("candidate.addProject", candidate_id, project_id, "added to")

    async def remove_ashby_candidate_from_project(candidate_id: str, project_id: str) -> str:
        return await _project_call("candidate.removeProject", candidate_id, project_id, "removed from")

    async def list_ashby_candidate_projects(candidate_id: str) -> str:
        async with ctx.client() as client:
            projects, err = await ctx.collect_all(
                client, "candidate.listProjects", {"candidateId": candidate_id}, max_items=MAX_PAGE * 5
            )
        if err:
            return err
        return format_entity_list(
            projects,
            fmt_project,
            noun="project",
            empty_msg=f"Candidate {candidate_id} is in no project.",
        )

    async def anonymize_ashby_candidate(candidate_id: str) -> str:
        async with ctx.client() as client:
            _, err = await ctx.call(client, "candidate.anonymize", {"candidateId": candidate_id})
        if err:
            return err
        return (
            f"Candidate {candidate_id} anonymized: Ashby removed their personally "
            "identifiable information. This cannot be undone."
        )

    for fn in (
        list_ashby_candidates,
        get_ashby_candidate,
        search_ashby_candidates,
        create_ashby_candidate,
        update_ashby_candidate,
        create_ashby_candidate_note,
        list_ashby_candidate_notes,
        add_ashby_candidate_tag,
        remove_ashby_candidate_tag,
        add_ashby_candidate_to_project,
        remove_ashby_candidate_from_project,
        list_ashby_candidate_projects,
        anonymize_ashby_candidate,
    ):
        executor.register(fn.__name__, fn)

    candidate_id = id_schema("candidate")
    limit = limit_schema()
    cursor = CURSOR_SCHEMA
    since = SINCE
    contact_fields = {
        "email": {"type": "string", "description": "Primary personal email"},
        "phone": {"type": "string", "description": "Primary phone number"},
        "linkedin_url": {"type": "string", "description": "LinkedIn profile URL"},
        "github_url": {"type": "string", "description": "GitHub profile URL"},
        "source_id": {"type": "string", "description": "Source ID (list_ashby_sources)"},
        "credited_to_user_id": {
            "type": "string",
            "description": "User credited with the candidate (list_ashby_users)",
        },
        "city": {"type": "string", "description": "Location city"},
        "region": {"type": "string", "description": "Location region / state"},
        "country": {"type": "string", "description": "Location country"},
    }
    tool = definition

    return [
        tool(
            "list_ashby_candidates",
            "List Ashby candidates, most recently created first, with email, current role, "
            "location, tags, source and application ids. Filter by creation time; page with "
            "cursor. To find a person by name or email use search_ashby_candidates.",
            {
                "limit": limit,
                "cursor": cursor,
                "created_after": {"type": "string", "description": f"Created at or after: {since}"},
                "created_before": {"type": "string", "description": f"Created at or before: {since}"},
            },
            [],
            0,
        ),
        tool(
            "get_ashby_candidate",
            "Get one Ashby candidate in full: every email and phone, current role, school, "
            "location, links, tags, source, credited user, custom fields, application ids "
            "(list_ashby_candidate_applications reads them), resume and file ids "
            "(download_ashby_candidate_file fetches one) and the Ashby profile link.",
            {
                "candidate_id": candidate_id,
                "external_mapping_id": {
                    "type": "string",
                    "description": "Instead of candidate_id: the id another system assigned",
                    "default": "",
                },
            },
            [],
            0,
        ),
        tool(
            "search_ashby_candidates",
            "Find Ashby candidates by name and/or email (Ashby's candidate.search). With both, "
            "each is searched and the matches merged. 'term' is a shortcut: an address with '@' "
            "is searched as an email, anything else as a name. Returns full profiles with ids.",
            {
                "name": {"type": "string", "description": "Candidate name, e.g. 'Ada Lovelace'", "default": ""},
                "email": {"type": "string", "description": "Candidate email address", "default": ""},
                "term": {"type": "string", "description": "A name or an email", "default": ""},
                "limit": {**limit, "description": "Most matches per search (1-100)"},
            },
            [],
            0,
        ),
        tool(
            "create_ashby_candidate",
            "Create an Ashby candidate. Give the full name (or first_name + last_name) and "
            "optionally email, phone, LinkedIn / GitHub / website, alternate emails, source, "
            "credited user and location. Then create_ashby_application considers them for a job.",
            {
                "name": {"type": "string", "description": "Full name"},
                "first_name": {"type": "string", "description": "First name (when name is not given)"},
                "last_name": {"type": "string", "description": "Last name (when name is not given)"},
                **contact_fields,
                "website": {"type": "string", "description": "Personal website URL"},
                "alternate_emails": {
                    "type": "string",
                    "description": "Other email addresses, comma separated",
                },
            },
            [],
            1,
        ),
        tool(
            "update_ashby_candidate",
            "Update an Ashby candidate's name, email, phone, links, an alternate email, "
            "source, credited user or location. Only the fields given change.",
            {
                "candidate_id": candidate_id,
                "name": {"type": "string", "description": "New full name"},
                **contact_fields,
                "website_url": {"type": "string", "description": "Personal website URL"},
                "alternate_email": {"type": "string", "description": "An email address to add"},
                "send_notifications": {
                    "type": "boolean",
                    "description": "Notify users subscribed to the candidate",
                    "default": False,
                },
            },
            ["candidate_id"],
            1,
        ),
        tool(
            "create_ashby_candidate_note",
            "Add a note to an Ashby candidate's profile (recruiter observations, debrief "
            "summaries). Plain text by default; note_type 'html' posts HTML.",
            {
                "candidate_id": candidate_id,
                "note": {"type": "string", "description": "The note"},
                "note_type": {
                    "type": "string",
                    "enum": ["text", "html"],
                    "description": "Format of the note",
                    "default": "text",
                },
                "is_private": {
                    "type": "boolean",
                    "description": "Mark the note private (the key needs private field access)",
                    "default": False,
                },
                "send_notifications": {
                    "type": "boolean",
                    "description": "Notify users subscribed to the candidate",
                    "default": False,
                },
            },
            ["candidate_id", "note"],
            1,
        ),
        tool(
            "list_ashby_candidate_notes",
            "List the notes on an Ashby candidate, newest first, with author, date, privacy and the note text.",
            {"candidate_id": candidate_id, "limit": limit, "cursor": cursor},
            ["candidate_id"],
            0,
        ),
        tool(
            "add_ashby_candidate_tag",
            "Add a tag to an Ashby candidate (tag ids: list_ashby_candidate_tags; "
            "create_ashby_candidate_tag makes a new one).",
            {"candidate_id": candidate_id, "tag_id": {"type": "string", "description": "Tag ID"}},
            ["candidate_id", "tag_id"],
            1,
        ),
        tool(
            "remove_ashby_candidate_tag",
            "Remove a tag from an Ashby candidate.",
            {"candidate_id": candidate_id, "tag_id": {"type": "string", "description": "Tag ID"}},
            ["candidate_id", "tag_id"],
            1,
        ),
        tool(
            "add_ashby_candidate_to_project",
            "Add an Ashby candidate to a project (talent pool); project ids from "
            "list_ashby_projects or search_ashby_projects.",
            {"candidate_id": candidate_id, "project_id": {"type": "string", "description": "Project ID"}},
            ["candidate_id", "project_id"],
            1,
        ),
        tool(
            "remove_ashby_candidate_from_project",
            "Remove an Ashby candidate from a project (talent pool).",
            {"candidate_id": candidate_id, "project_id": {"type": "string", "description": "Project ID"}},
            ["candidate_id", "project_id"],
            1,
        ),
        tool(
            "list_ashby_candidate_projects",
            "List the projects (talent pools) an Ashby candidate belongs to.",
            {"candidate_id": candidate_id},
            ["candidate_id"],
            1,
        ),
        tool(
            "anonymize_ashby_candidate",
            "Anonymize an Ashby candidate: Ashby permanently removes their personally "
            "identifiable information (GDPR erasure requests). Destructive - cannot be undone.",
            {"candidate_id": candidate_id},
            ["candidate_id"],
            2,
        ),
    ]
