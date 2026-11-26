"""The Wordsmith public API, one row per documented operation.

The single source of truth of the provider: every grouped tool is built
from these rows, the coverage matrix in
``dev-docs/reference/wordsmith-api-coverage.md`` is checked against them by
``tests/test_wordsmith_provider.py``, and a route Wordsmith adds later stays
reachable through ``wordsmith_api_request`` until it gets a row.

A row is ``(operation, method, path, summary, doc)``: ``operation`` is the
value the agent passes as ``operation`` to the tool of its group, ``path`` is
relative to ``/api/v1`` with ``{name}`` path parameters, ``doc`` is the page
under https://wordsmithai.mintlify.app.

Groups are split by effect, never mixed: a ``read`` group only reads (a POST
that searches or validates without changing anything is a read), a ``write``
group only changes, so a read-only binding withholds every write by the name
of its tool (``get_*`` / ``search_*`` read, ``manage_*`` write).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Operation:
    operation: str
    method: str
    path: str
    summary: str
    doc: str

    @property
    def path_params(self) -> tuple[str, ...]:
        params: list[str] = []
        rest = self.path
        while "{" in rest:
            start = rest.index("{")
            end = rest.index("}", start)
            params.append(rest[start + 1 : end])
            rest = rest[end + 1 :]
        return tuple(params)


@dataclass(frozen=True)
class Group:
    tool: str
    access: str  # "read" or "write"
    title: str
    operations: tuple[Operation, ...]


def _ops(*rows: tuple[str, str, str, str, str]) -> tuple[Operation, ...]:
    return tuple(Operation(*row) for row in rows)


_R = "api-reference/"

GROUPS: tuple[Group, ...] = (
    Group(
        "get_wordsmith_agents",
        "read",
        "agents (legal AI specialists) and the status and answer of a question",
        _ops(
            (
                "list_agents",
                "GET",
                "/agents",
                "List the agents the user can read; query task_assignable.",
                _R + "agents/list-agents",
            ),
            (
                "get_agent",
                "GET",
                "/agents/{agent_id}",
                "Read one agent; agent_id may be 'default'.",
                _R + "agents/get-agent",
            ),
            (
                "get_question",
                "GET",
                "/agents/{agent_id}/questions/{question_id}",
                "Status and answer of a question (in_progress, completed, error, filtered: the last three are final).",
                _R + "agents/get-question-status",
            ),
        ),
    ),
    Group(
        "get_wordsmith_sessions",
        "read",
        "agent sessions (conversations) and their messages",
        _ops(
            (
                "list_sessions",
                "GET",
                "/sessions",
                "List sessions; filters in query (agent_id, cursor, limit).",
                _R + "sessions/list-sessions",
            ),
            ("get_session", "GET", "/sessions/{session_id}", "Read one session.", _R + "sessions/get-session"),
            (
                "list_messages",
                "GET",
                "/sessions/{session_id}/messages",
                "Messages of a session, oldest first.",
                _R + "sessions/list-messages",
            ),
        ),
    ),
    Group(
        "get_wordsmith_operations",
        "read",
        "long-running operations (questions, document actions, report runs and exports)",
        _ops(
            (
                "list_operations",
                "GET",
                "/operations",
                "List operations; filters in query.",
                _R + "operations/list-operations",
            ),
            (
                "get_operation",
                "GET",
                "/operations/{operation_id}",
                "Read one operation; query wait (0-20 s) holds the answer until it finishes.",
                _R + "operations/get-operation",
            ),
        ),
    ),
    Group(
        "manage_wordsmith_operations",
        "write",
        "long-running operations",
        _ops(
            (
                "cancel_operation",
                "POST",
                "/operations/{operation_id}/cancel",
                "Cancel a running operation.",
                _R + "operations/cancel-operation",
            ),
        ),
    ),
    Group(
        "get_wordsmith_files",
        "read",
        "files: metadata, text by page and the short-lived download link",
        _ops(
            ("get_file", "GET", "/files/{file_id}", "File metadata.", _R + "files/get-file"),
            (
                "get_file_text",
                "GET",
                "/files/{file_id}/text",
                "Extracted text; query page for long files.",
                _R + "files/get-file-text",
            ),
            (
                "get_file_download",
                "GET",
                "/files/{file_id}/download",
                "Signed link (5 minutes) and web_url; download_wordsmith_file stores the file instead.",
                _R + "files/get-file-download",
            ),
        ),
    ),
    Group(
        "manage_wordsmith_files",
        "write",
        "files and document actions (compare, merge, redline, translate, redact, convert, accept changes) and drafts",
        _ops(
            ("create_file", "POST", "/files", "Create a file from text or base64 content.", _R + "files/create-file"),
            ("import_file", "POST", "/files/import", "Import a file from a public URL.", _R + "files/import-file"),
            (
                "create_upload_url",
                "POST",
                "/files/upload-url",
                "Presigned upload URL; upload_wordsmith_file does the whole upload from a conversation file.",
                _R + "files/upload-url",
            ),
            (
                "complete_upload",
                "POST",
                "/files/uploads/{upload_id}/complete",
                "Turn an uploaded job into a file.",
                _R + "files/complete-upload",
            ),
            ("compare_files", "POST", "/files/compare", "Compare two files (operation).", _R + "documents/compare"),
            ("merge_files", "POST", "/files/merge", "Merge files into one (operation).", _R + "documents/merge"),
            (
                "accept_changes",
                "POST",
                "/files/{file_id}/accept-changes",
                "Accept tracked changes (operation).",
                _R + "documents/accept-changes",
            ),
            (
                "convert_file",
                "POST",
                "/files/{file_id}/convert",
                "Convert a file to another format (operation).",
                _R + "documents/convert",
            ),
            ("redact_file", "POST", "/files/{file_id}/redact", "Redact a file (operation).", _R + "documents/redact"),
            (
                "redline_file",
                "POST",
                "/files/{file_id}/redline",
                "Redline a file against instructions (operation).",
                _R + "documents/redline",
            ),
            (
                "translate_file",
                "POST",
                "/files/{file_id}/translate",
                "Translate a file (operation).",
                _R + "documents/translate",
            ),
            ("create_draft", "POST", "/drafts", "Draft a document (operation).", _R + "drafts/create-draft"),
        ),
    ),
    Group(
        "get_wordsmith_reviews",
        "read",
        "contract reviews and their findings",
        _ops(
            ("list_reviews", "GET", "/reviews", "List reviews.", _R + "reviews/list-reviews"),
            ("get_review", "GET", "/reviews/{review_id}", "Read one review.", _R + "reviews/get-review"),
            (
                "list_findings",
                "GET",
                "/reviews/{review_id}/findings",
                "Findings of a review.",
                _R + "reviews/list-findings",
            ),
        ),
    ),
    Group(
        "manage_wordsmith_reviews",
        "write",
        "contract reviews",
        _ops(
            (
                "create_review",
                "POST",
                "/reviews",
                "Review a file against a playbook (operation).",
                _R + "reviews/create-review",
            ),
            (
                "generate_redline",
                "POST",
                "/reviews/{review_id}/redline",
                "Redline from a review's findings (operation).",
                _R + "reviews/generate-redline",
            ),
        ),
    ),
    Group(
        "get_wordsmith_playbooks",
        "read",
        "review playbooks and their checks",
        _ops(
            ("list_playbooks", "GET", "/playbooks", "List playbooks.", _R + "playbooks/list-playbooks"),
            (
                "get_playbook",
                "GET",
                "/playbooks/{playbook_id}",
                "Read one playbook with its checks.",
                _R + "playbooks/get-playbook",
            ),
        ),
    ),
    Group(
        "manage_wordsmith_playbooks",
        "write",
        "review playbooks and their checks",
        _ops(
            ("create_playbook", "POST", "/playbooks", "Create a playbook.", _R + "playbooks/create-playbook"),
            (
                "generate_playbook",
                "POST",
                "/playbooks/generate",
                "Generate a playbook from documents (operation).",
                _R + "playbooks/generate-playbook",
            ),
            (
                "update_playbook",
                "PATCH",
                "/playbooks/{playbook_id}",
                "Update a playbook.",
                _R + "playbooks/update-playbook",
            ),
            (
                "delete_playbook",
                "DELETE",
                "/playbooks/{playbook_id}",
                "Delete a playbook.",
                _R + "playbooks/delete-playbook",
            ),
            ("add_check", "POST", "/playbooks/{playbook_id}/checks", "Add a check.", _R + "playbooks/add-check"),
            (
                "update_check",
                "PATCH",
                "/playbooks/{playbook_id}/checks/{check_id}",
                "Update a check.",
                _R + "playbooks/update-check",
            ),
            (
                "delete_check",
                "DELETE",
                "/playbooks/{playbook_id}/checks/{check_id}",
                "Delete a check.",
                _R + "playbooks/delete-check",
            ),
        ),
    ),
    Group(
        "get_wordsmith_blueprints",
        "read",
        "drafting blueprints (the former templates)",
        _ops(
            ("list_blueprints", "GET", "/blueprints", "List blueprints.", _R + "blueprints/list-blueprints"),
            (
                "get_blueprint",
                "GET",
                "/blueprints/{blueprint_id}",
                "Read one blueprint.",
                _R + "blueprints/get-blueprint",
            ),
        ),
    ),
    Group(
        "get_wordsmith_skills",
        "read",
        "skills",
        _ops(
            ("list_skills", "GET", "/skills", "List skills.", _R + "skills/list-skills"),
            ("get_skill", "GET", "/skills/{skill_id}", "Read one skill.", _R + "skills/get-skill"),
        ),
    ),
    Group(
        "manage_wordsmith_skills",
        "write",
        "skills",
        _ops(
            (
                "create_skill",
                "POST",
                "/skills",
                "Create a skill (SKILL.md content and files).",
                _R + "skills/create-skill",
            ),
            ("update_skill", "PATCH", "/skills/{skill_id}", "Update a skill.", _R + "skills/update-skill"),
            ("delete_skill", "DELETE", "/skills/{skill_id}", "Delete a skill.", _R + "skills/delete-skill"),
        ),
    ),
    Group(
        "get_wordsmith_reports",
        "read",
        "tabular reports: rows, cells",
        _ops(
            ("list_reports", "GET", "/reports", "List reports.", _R + "reports/list-reports"),
            (
                "get_report",
                "GET",
                "/reports/{report_id}",
                "Read one report with its columns.",
                _R + "reports/get-report",
            ),
            ("list_rows", "GET", "/reports/{report_id}/rows", "Rows of a report.", _R + "reports/list-rows"),
            ("list_cells", "GET", "/reports/{report_id}/cells", "Cells of a report.", _R + "reports/list-cells"),
        ),
    ),
    Group(
        "manage_wordsmith_reports",
        "write",
        "tabular reports: columns, rows, runs, exports",
        _ops(
            ("create_report", "POST", "/reports", "Create a report.", _R + "reports/create-report"),
            ("update_report", "PATCH", "/reports/{report_id}", "Update a report.", _R + "reports/update-report"),
            ("delete_report", "DELETE", "/reports/{report_id}", "Delete a report.", _R + "reports/delete-report"),
            ("add_columns", "POST", "/reports/{report_id}/columns", "Add columns.", _R + "reports/add-columns"),
            (
                "update_column",
                "PATCH",
                "/reports/{report_id}/columns/{column_id}",
                "Update a column.",
                _R + "reports/update-column",
            ),
            (
                "delete_column",
                "DELETE",
                "/reports/{report_id}/columns/{column_id}",
                "Delete a column.",
                _R + "reports/delete-column",
            ),
            ("add_rows", "POST", "/reports/{report_id}/rows", "Add rows (files).", _R + "reports/add-rows"),
            ("delete_row", "DELETE", "/reports/{report_id}/rows/{row_id}", "Delete a row.", _R + "reports/delete-row"),
            (
                "run_report",
                "POST",
                "/reports/{report_id}/runs",
                "Run the report (operation).",
                _R + "reports/run-report",
            ),
            (
                "export_report",
                "POST",
                "/reports/{report_id}/exports",
                "Export the report to a file (operation).",
                _R + "reports/export-report",
            ),
        ),
    ),
    Group(
        "get_wordsmith_repositories",
        "read",
        "document repositories: documents, folders, search",
        _ops(
            ("list_repositories", "GET", "/repositories", "List repositories.", _R + "repositories/list-repositories"),
            (
                "get_repository",
                "GET",
                "/repositories/{repository_id}",
                "Read one repository.",
                _R + "repositories/get-repository",
            ),
            (
                "list_documents",
                "GET",
                "/repositories/{repository_id}/documents",
                "Documents of a repository.",
                _R + "repositories/list-documents",
            ),
            (
                "get_document",
                "GET",
                "/repositories/{repository_id}/documents/{document_id}",
                "Read one document.",
                _R + "repositories/get-document",
            ),
            (
                "list_folders",
                "GET",
                "/repositories/{repository_id}/folders",
                "Folders of a repository.",
                _R + "repositories/list-folders",
            ),
            (
                "search_repository",
                "POST",
                "/repositories/{repository_id}/search",
                "Search one repository (reads only).",
                _R + "repositories/search-repository",
            ),
        ),
    ),
    Group(
        "manage_wordsmith_repositories",
        "write",
        "document repositories",
        _ops(
            (
                "create_repository",
                "POST",
                "/repositories",
                "Create a repository.",
                _R + "repositories/create-repository",
            ),
            (
                "update_repository",
                "PATCH",
                "/repositories/{repository_id}",
                "Update a repository.",
                _R + "repositories/update-repository",
            ),
            (
                "delete_repository",
                "DELETE",
                "/repositories/{repository_id}",
                "Delete a repository.",
                _R + "repositories/delete-repository",
            ),
            (
                "add_documents",
                "POST",
                "/repositories/{repository_id}/documents",
                "Add documents (operation).",
                _R + "repositories/add-documents",
            ),
            (
                "remove_document",
                "DELETE",
                "/repositories/{repository_id}/documents/{document_id}",
                "Remove a document.",
                _R + "repositories/remove-document",
            ),
            (
                "create_folder",
                "POST",
                "/repositories/{repository_id}/folders",
                "Create a folder.",
                _R + "repositories/create-folder",
            ),
        ),
    ),
    Group(
        "search_wordsmith_knowledge",
        "read",
        "knowledge search across the organisation and legal research sources",
        _ops(
            (
                "search_knowledge",
                "POST",
                "/search",
                "Search the organisation's knowledge.",
                _R + "search/search-knowledge",
            ),
            ("search_research", "POST", "/research/search", "Search legal research sources.", _R + "research/search"),
            (
                "get_research_document",
                "GET",
                "/research/documents/{source}",
                "Read one research document; query id.",
                _R + "research/get-document",
            ),
        ),
    ),
    Group(
        "get_wordsmith_tasks",
        "read",
        "legal tasks: comments, thread with the requester, files, follow status, sharing, subtasks",
        _ops(
            ("list_tasks", "GET", "/tasks", "List tasks; filters in query.", _R + "tasks/list-tasks"),
            ("get_task", "GET", "/tasks/{task_id}", "Read one task.", _R + "tasks/get-task"),
            ("list_comments", "GET", "/tasks/{task_id}/comments", "Comments of a task.", _R + "tasks/list-comments"),
            (
                "list_thread",
                "GET",
                "/tasks/{task_id}/thread",
                "Messages exchanged with the requester.",
                _R + "tasks/list-thread",
            ),
            ("list_files", "GET", "/tasks/{task_id}/files", "Files of a task.", _R + "tasks/list-files"),
            (
                "get_file_download",
                "GET",
                "/tasks/{task_id}/files/{file_id}/download",
                "Signed link of a task file.",
                _R + "tasks/get-file-download",
            ),
            (
                "get_file_text",
                "GET",
                "/tasks/{task_id}/files/{file_id}/text",
                "Text of a task file.",
                _R + "tasks/get-file-text",
            ),
            (
                "get_follow",
                "GET",
                "/tasks/{task_id}/follow",
                "Whether the user follows the task.",
                _R + "tasks/get-follow",
            ),
            (
                "get_sharing",
                "GET",
                "/tasks/{task_id}/sharing",
                "Who the task is shared with.",
                _R + "tasks/get-sharing",
            ),
            ("list_subtasks", "GET", "/tasks/{task_id}/subtasks", "Subtasks of a task.", _R + "tasks/list-subtasks"),
        ),
    ),
    Group(
        "manage_wordsmith_tasks",
        "write",
        "legal tasks",
        _ops(
            (
                "create_task",
                "POST",
                "/tasks",
                "Create a task, optionally assigned to a person or an agent.",
                _R + "tasks/create-task",
            ),
            (
                "update_task",
                "PATCH",
                "/tasks/{task_id}",
                "Update a task (status, assignee, due date...).",
                _R + "tasks/update-task",
            ),
            ("delete_task", "DELETE", "/tasks/{task_id}", "Delete a task.", _R + "tasks/delete-task"),
            ("add_comment", "POST", "/tasks/{task_id}/comments", "Comment on a task.", _R + "tasks/add-comment"),
            (
                "update_comment",
                "PATCH",
                "/tasks/{task_id}/comments/{comment_id}",
                "Edit a comment.",
                _R + "tasks/update-comment",
            ),
            (
                "delete_comment",
                "DELETE",
                "/tasks/{task_id}/comments/{comment_id}",
                "Delete a comment.",
                _R + "tasks/delete-comment",
            ),
            (
                "reply_comment",
                "POST",
                "/tasks/{task_id}/comments/{comment_id}/replies",
                "Reply to a comment.",
                _R + "tasks/reply-comment",
            ),
            (
                "reply_thread",
                "POST",
                "/tasks/{task_id}/thread",
                "Reply to the requester.",
                _R + "tasks/add-thread-reply",
            ),
            ("attach_files", "POST", "/tasks/{task_id}/files", "Attach files.", _R + "tasks/attach-files"),
            ("remove_file", "DELETE", "/tasks/{task_id}/files/{file_id}", "Remove a file.", _R + "tasks/remove-file"),
            (
                "add_links",
                "POST",
                "/tasks/{task_id}/links",
                "Link items (sessions, files, tasks...).",
                _R + "tasks/add-links",
            ),
            (
                "update_link",
                "PATCH",
                "/tasks/{task_id}/links/{link_type}/{item_id}",
                "Update a link.",
                _R + "tasks/update-link",
            ),
            (
                "remove_link",
                "DELETE",
                "/tasks/{task_id}/links/{link_type}/{item_id}",
                "Unlink an item.",
                _R + "tasks/remove-link",
            ),
            ("follow_task", "PUT", "/tasks/{task_id}/follow", "Follow the task.", _R + "tasks/follow-task"),
            ("unfollow_task", "DELETE", "/tasks/{task_id}/follow", "Unfollow the task.", _R + "tasks/unfollow-task"),
            (
                "update_sharing",
                "PUT",
                "/tasks/{task_id}/sharing",
                "Replace who the task is shared with.",
                _R + "tasks/update-sharing",
            ),
        ),
    ),
    Group(
        "get_wordsmith_sharing",
        "read",
        "sharing of a repository, report, playbook, blueprint or session",
        _ops(
            (
                "get_sharing",
                "GET",
                "/{resource_type}/{resource_id}/sharing",
                "resource_type: repositories, reports, playbooks, blueprints or sessions.",
                _R + "sharing/get-sharing",
            ),
        ),
    ),
    Group(
        "manage_wordsmith_sharing",
        "write",
        "sharing of a repository, report, playbook, blueprint or session",
        _ops(
            (
                "update_sharing",
                "PUT",
                "/{resource_type}/{resource_id}/sharing",
                "Replace visibility, users and teams; resource_type as for reading.",
                _R + "sharing/update-sharing",
            ),
        ),
    ),
    Group(
        "get_wordsmith_directory",
        "read",
        "the organisation: users, teams, invites, roles, company profiles, access review",
        _ops(
            ("list_users", "GET", "/users", "List users.", _R + "directory/list-users"),
            ("get_user", "GET", "/users/{user_id}", "Read one member.", _R + "organisation/get-member"),
            ("list_teams", "GET", "/teams", "List teams.", _R + "directory/list-teams"),
            ("get_team", "GET", "/teams/{team_id}", "Read one team.", _R + "organisation/get-team"),
            ("list_invites", "GET", "/invites", "Pending invites.", _R + "organisation/list-invites"),
            ("list_roles", "GET", "/roles", "Roles.", _R + "organisation/list-roles"),
            (
                "list_company_profiles",
                "GET",
                "/company-profiles",
                "Company profiles.",
                _R + "organisation/list-company-profiles",
            ),
            (
                "get_company_profile",
                "GET",
                "/company-profiles/{profile_id}",
                "Read one company profile.",
                _R + "organisation/get-company-profile",
            ),
            (
                "get_access_review",
                "GET",
                "/access-review",
                "Who can access what.",
                _R + "organisation/get-access-review",
            ),
        ),
    ),
    Group(
        "manage_wordsmith_directory",
        "write",
        "the organisation (directory:admin scope)",
        _ops(
            (
                "update_user",
                "PATCH",
                "/users/{user_id}",
                "Update a member (role, status).",
                _R + "organisation/update-member",
            ),
            ("create_team", "POST", "/teams", "Create a team.", _R + "organisation/create-team"),
            ("update_team", "PATCH", "/teams/{team_id}", "Update a team.", _R + "organisation/update-team"),
            ("delete_team", "DELETE", "/teams/{team_id}", "Delete a team.", _R + "organisation/delete-team"),
            (
                "add_team_member",
                "POST",
                "/teams/{team_id}/members",
                "Add a team member.",
                _R + "organisation/add-team-member",
            ),
            (
                "update_team_member",
                "PATCH",
                "/teams/{team_id}/members/{user_id}",
                "Change a member's team role.",
                _R + "organisation/update-team-member",
            ),
            (
                "remove_team_member",
                "DELETE",
                "/teams/{team_id}/members/{user_id}",
                "Remove a team member.",
                _R + "organisation/remove-team-member",
            ),
            ("create_invite", "POST", "/invites", "Invite a person.", _R + "organisation/create-invite"),
            (
                "resend_invite",
                "POST",
                "/invites/{invite_id}/resend",
                "Resend an invite.",
                _R + "organisation/resend-invite",
            ),
            ("revoke_invite", "DELETE", "/invites/{invite_id}", "Revoke an invite.", _R + "organisation/revoke-invite"),
            (
                "update_company_profile",
                "PATCH",
                "/company-profiles/{profile_id}",
                "Update a company profile.",
                _R + "organisation/update-company-profile",
            ),
        ),
    ),
    Group(
        "get_wordsmith_events",
        "read",
        "events (what webhooks deliver), event types and the audit log",
        _ops(
            ("list_event_types", "GET", "/event_types", "Every event type.", _R + "events/list-event-types"),
            (
                "list_events",
                "GET",
                "/events",
                "Events, filters in query (type, created_after, cursor).",
                _R + "events/list-events",
            ),
            ("get_event", "GET", "/events/{event_id}", "Read one event.", _R + "events/get-event"),
            (
                "list_audit_events",
                "GET",
                "/audit/events",
                "Audit log, oldest first; query start_time, end_time, cursor, limit, categories (audit:read).",
                _R + "audit/list-audit-events",
            ),
        ),
    ),
    Group(
        "get_wordsmith_usage",
        "read",
        "usage figures and the OData feed",
        _ops(
            (
                "get_usage_summary",
                "GET",
                "/usage/summary",
                "Usage summary for a period.",
                _R + "usage/get-usage-summary",
            ),
            (
                "get_usage_breakdown",
                "GET",
                "/usage/breakdown",
                "Usage by user, agent or feature.",
                _R + "usage/get-usage-breakdown",
            ),
            ("get_usage_timeseries", "GET", "/usage/timeseries", "Usage over time.", _R + "usage/get-usage-timeseries"),
            ("get_odata_feed", "GET", "/odata", "OData service document; query period, start, end.", "odata-feed"),
            ("get_odata_report", "GET", "/odata/reports/{report_id}", "One report as an OData feed.", "odata-feed"),
        ),
    ),
    Group(
        "get_wordsmith_webhook_endpoints",
        "read",
        "webhook endpoints and their deliveries",
        _ops(
            (
                "list_webhook_endpoints",
                "GET",
                "/webhook_endpoints",
                "List webhook endpoints.",
                _R + "webhooks/list-webhook-endpoints",
            ),
            (
                "get_webhook_endpoint",
                "GET",
                "/webhook_endpoints/{webhook_endpoint_id}",
                "Read one endpoint.",
                _R + "webhooks/get-webhook-endpoint",
            ),
            (
                "list_deliveries",
                "GET",
                "/webhook_endpoints/{webhook_endpoint_id}/deliveries",
                "Deliveries of an endpoint.",
                _R + "webhooks/list-webhook-deliveries",
            ),
        ),
    ),
    Group(
        "manage_wordsmith_webhook_endpoints",
        "write",
        "webhook endpoints (webhooks:write; the signing secret is never shown)",
        _ops(
            (
                "create_webhook_endpoint",
                "POST",
                "/webhook_endpoints",
                "Create an endpoint.",
                _R + "webhooks/create-webhook-endpoint",
            ),
            (
                "update_webhook_endpoint",
                "PATCH",
                "/webhook_endpoints/{webhook_endpoint_id}",
                "Update an endpoint.",
                _R + "webhooks/update-webhook-endpoint",
            ),
            (
                "delete_webhook_endpoint",
                "DELETE",
                "/webhook_endpoints/{webhook_endpoint_id}",
                "Delete an endpoint.",
                _R + "webhooks/delete-webhook-endpoint",
            ),
            (
                "retry_delivery",
                "POST",
                "/webhook_endpoints/{webhook_endpoint_id}/deliveries/{delivery_id}/retry",
                "Retry a delivery.",
                _R + "webhooks/retry-webhook-delivery",
            ),
            (
                "rotate_secret",
                "POST",
                "/webhook_endpoints/{webhook_endpoint_id}/rotate_secret",
                "Rotate the secret.",
                _R + "webhooks/rotate-webhook-secret",
            ),
            (
                "send_test_event",
                "POST",
                "/webhook_endpoints/{webhook_endpoint_id}/test",
                "Send a webhook_endpoint.test event.",
                _R + "webhooks/send-test-event",
            ),
        ),
    ),
    Group(
        "get_wordsmith_artifacts",
        "read",
        "artifacts (API key only)",
        _ops(
            (
                "list_artifacts",
                "GET",
                "/artifacts",
                "List artifacts; query query, mine, limit, cursor.",
                _R + "artifacts/list-artifacts",
            ),
            ("get_artifact", "GET", "/artifacts/{artifact_id}", "Read one artifact.", _R + "artifacts/get-artifact"),
            (
                "validate_layout",
                "POST",
                "/artifacts/validate",
                "Validate a layout without saving it.",
                _R + "artifacts/validate-layout",
            ),
        ),
    ),
    Group(
        "manage_wordsmith_artifacts",
        "write",
        "artifacts (API key only)",
        _ops(
            ("create_artifact", "POST", "/artifacts", "Create an artifact.", _R + "artifacts/create-artifact"),
            (
                "update_artifact",
                "PATCH",
                "/artifacts/{artifact_id}",
                "Update an artifact.",
                _R + "artifacts/update-artifact",
            ),
            (
                "delete_artifact",
                "DELETE",
                "/artifacts/{artifact_id}",
                "Delete an artifact.",
                _R + "artifacts/delete-artifact",
            ),
        ),
    ),
)

#: Operations served by a dedicated tool rather than a group:
#: ``(method, path, tool, doc)``.
DEDICATED: tuple[tuple[str, str, str, str], ...] = (
    ("GET", "/me", "get_wordsmith_me", "api-reference/user/get-me"),
    ("POST", "/agents/{agent_id}/questions", "send_wordsmith_agent_question", _R + "agents/create-question"),
)

#: Deprecated aliases Wordsmith still serves, mapped to the route their tool
#: calls (identical request and response, per the docs): never a second tool.
LEGACY_ALIASES: dict[tuple[str, str], tuple[str, str]] = {
    ("GET", "/assistants"): ("GET", "/agents"),
    ("GET", "/assistants/{assistant_id}/questions/{question_id}"): (
        "GET",
        "/agents/{agent_id}/questions/{question_id}",
    ),
    ("POST", "/assistants/{assistant_id}/questions"): ("POST", "/agents/{agent_id}/questions"),
    ("GET", "/templates"): ("GET", "/blueprints"),
}

#: The JSON body fields a write operation accepts, as its Wordsmith docs page
#: defines them (read 25 Nov 2026), with the accepted values of an enum: the
#: tool names them and refuses any other key before the call, since Wordsmith
#: answers 400 "Extra inputs are not permitted" one key at a time (#2183).
BODY_FIELDS: dict[str, dict[str, str]] = {
    "create_draft": {
        "instructions": "what to draft; required without blueprint_id",
        "blueprint_id": "the published blueprint to draft from",
        "values": "blueprint placeholder values keyed by placeholder key (get_blueprint)",
        "context": "free text",
        "file_ids": "up to 10 source file ids",
        "session_id": "a past session as context",
        "output_format": "docx or pdf",
        "file_name": "name of the new file, no extension",
        "wait": "seconds to wait, 0 to 20",
        "callback_url": "HTTPS URL posted the finished operation",
    },
    "create_task": {
        "title": "required",
        "description": "the instruction",
        "parent_task_id": "#123 or a task id",
        "assigned_to": "user id, email, name or me",
        "assigned_agent": "agent id, name or shortcut",
        "assigned_team_id": "team id or name",
        "matter_id": "matter to file it under",
        "status": "initial status",
        "priority": "no_priority, urgent, high, medium or low",
        "complete_by": "ISO 8601 due date",
        "tags": "labels",
        "linked_items": "items of {type, id, role?, context?}",
        "draft": "true to create a draft task",
    },
}

#: Starts of long work: the request carries an ``Idempotency-Key`` so a retry
#: (ours after a 5xx, or the agent's) never starts the work twice.
IDEMPOTENT_STARTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/agents/{agent_id}/questions"),
        ("POST", "/files/{file_id}/accept-changes"),
        ("POST", "/files/compare"),
        ("POST", "/files/{file_id}/convert"),
        ("POST", "/files/merge"),
        ("POST", "/files/{file_id}/redact"),
        ("POST", "/files/{file_id}/redline"),
        ("POST", "/files/{file_id}/translate"),
        ("POST", "/drafts"),
        ("POST", "/playbooks/generate"),
        ("POST", "/reports"),
        ("POST", "/reports/{report_id}/exports"),
        ("POST", "/reports/{report_id}/runs"),
        ("POST", "/repositories/{repository_id}/documents"),
        ("POST", "/reviews"),
        ("POST", "/reviews/{review_id}/redline"),
    }
)


def covered_routes() -> dict[tuple[str, str], str]:
    """Every documented ``(method, path)`` and the tool that reaches it."""
    routes: dict[tuple[str, str], str] = {}
    for group in GROUPS:
        for op in group.operations:
            routes[(op.method, op.path)] = f"{group.tool} operation={op.operation}"
    for method, path, tool, _doc in DEDICATED:
        routes[(method, path)] = tool
    for alias, target in LEGACY_ALIASES.items():
        routes[alias] = f"{routes[target]} (legacy alias)"
    return routes
