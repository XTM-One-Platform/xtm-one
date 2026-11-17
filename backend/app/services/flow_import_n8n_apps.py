# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The n8n app nodes the importer knows, and where each one lands.

n8n ships hundreds of app nodes (``n8n-nodes-base.github``, ``.googleSheets``,
...). The importer in :mod:`app.services.flow_import_n8n` reads this table to
decide, for each of them:

- **an XTM One integration provider** — the node becomes an Integration node
  (``integration``), its trigger an Integration event or a Webhook trigger, its
  ``*Tool`` variant an Integration capability. ``operations`` names the
  provider's tool for each n8n ``resource:operation``; an operation missing
  from it leaves the Integration node's operation for someone to pick;
- **no provider** — the node stays unresolved, with the CTEM verdict of
  :data:`CTEM_CANDIDATES` in its hint: either a catalog candidate (an
  exposure-management use: assets, findings, remediation tickets, alerting),
  or outside the catalog (marketing, commerce, consumer apps), where an HTTP
  request reproduces the call when the step matters.

Pure data: no import of the importer, so both modules stay readable alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class N8nApp:
    """One n8n app node family and its XTM One provider."""

    provider: str
    label: str
    # n8n leaves a parameter out of the export at its default: the resource,
    # and per resource the operation, the node used when none is written.
    default_resource: str = ""
    default_operations: dict[str, str] = field(default_factory=dict)
    # "resource:operation" → the provider's tool doing the same.
    operations: dict[str, str] = field(default_factory=dict)
    # The integration event its trigger maps to, when the provider polls one.
    event: str = ""


def _ops(resource: str, table: dict[str, str]) -> dict[str, str]:
    return {f"{resource}:{operation}": tool for operation, tool in table.items()}


_THEHIVE_OPERATIONS = {
    **_ops(
        "alert",
        {
            "create": "create_thehive_alert",
            "get": "get_thehive_alert",
            "getAll": "search_thehive_alerts",
            "search": "search_thehive_alerts",
            "update": "update_thehive_alert",
            "promote": "promote_thehive_alert",
            "merge": "merge_thehive_alert_into_case",
        },
    ),
    **_ops(
        "case",
        {
            "create": "create_thehive_case",
            "get": "get_thehive_case",
            "getAll": "search_thehive_cases",
            "search": "search_thehive_cases",
            "update": "update_thehive_case",
        },
    ),
    **_ops(
        "observable",
        {
            "create": "add_thehive_observable",
            "getAll": "list_thehive_observables",
            "search": "list_thehive_observables",
            "update": "update_thehive_observable",
            "executeAnalyzer": "run_thehive_analyzer",
        },
    ),
    **_ops(
        "task",
        {
            "create": "create_thehive_task",
            "getAll": "list_thehive_tasks",
            "search": "list_thehive_tasks",
            "update": "update_thehive_task",
        },
    ),
    **_ops("log", {"create": "add_thehive_task_log", "getAll": "list_thehive_task_logs"}),
    **_ops("comment", {"add": "add_thehive_comment", "search": "list_thehive_comments"}),
}

_THEHIVE_DEFAULTS = {
    "alert": "create",
    "case": "create",
    "observable": "create",
    "task": "create",
    "log": "create",
    "comment": "add",
}


# n8n short type → its XTM One provider. A trigger (``githubTrigger``) or a tool
# variant (``githubTool``) reads its base app's entry.
N8N_APPS: dict[str, N8nApp] = {
    "github": N8nApp(
        "github",
        "GitHub",
        default_resource="issue",
        default_operations={
            "issue": "create",
            "file": "create",
            "repository": "get",
            "release": "create",
            "review": "create",
            "user": "getRepositories",
            "organization": "getRepositories",
        },
        operations={
            **_ops(
                "issue",
                {
                    "create": "create_github_issue",
                    "createComment": "add_github_comment",
                    "edit": "update_github_issue",
                    "get": "get_github_issue",
                    "lock": "manage_github_issue",
                },
            ),
            **_ops(
                "file",
                {
                    "create": "create_or_update_github_file",
                    "edit": "create_or_update_github_file",
                    "delete": "delete_github_file",
                    "get": "get_github_file",
                    "list": "get_github_repository_tree",
                },
            ),
            **_ops(
                "repository",
                {
                    "get": "get_github_repository",
                    "getIssues": "search_github_issues",
                    "getPullRequests": "search_github_issues",
                },
            ),
            **_ops(
                "release",
                {
                    "create": "manage_github_release",
                    "delete": "manage_github_release",
                    "update": "manage_github_release",
                    "get": "get_github_release",
                    "getAll": "list_github_releases",
                },
            ),
            "review:create": "create_github_pr_review",
            "user:getRepositories": "list_github_repos",
            "organization:getRepositories": "list_github_repos",
        },
    ),
    "jira": N8nApp(
        "jira",
        "Jira",
        default_resource="issue",
        default_operations={
            "issue": "create",
            "issueComment": "add",
            "issueAttachment": "add",
            "user": "get",
        },
        operations={
            **_ops(
                "issue",
                {
                    "create": "create_jira_issue",
                    "get": "get_jira_issue",
                    "getAll": "search_jira_issues",
                    "update": "update_jira_issue",
                    "delete": "delete_jira_issue",
                    "transitions": "get_jira_transitions",
                    "changelog": "get_jira_issue_changelog",
                },
            ),
            **_ops(
                "issueComment",
                {
                    "add": "add_jira_comment",
                    "get": "get_jira_comment",
                    "getAll": "list_jira_comments",
                    "update": "update_jira_comment",
                    "remove": "delete_jira_comment",
                },
            ),
            **_ops(
                "issueAttachment",
                {
                    "add": "upload_jira_attachment",
                    "get": "download_jira_attachment",
                    "getAll": "list_jira_attachments",
                    "remove": "delete_jira_attachment",
                },
            ),
            **_ops("user", {"get": "search_jira_users", "getAll": "search_jira_users"}),
        },
    ),
    "notion": N8nApp(
        "notion",
        "Notion",
        default_resource="page",
        default_operations={
            "block": "append",
            "database": "get",
            "databasePage": "create",
            "page": "create",
            "user": "get",
        },
        operations={
            **_ops("block", {"append": "append_notion_blocks", "getAll": "list_notion_block_children"}),
            **_ops(
                "database",
                {"get": "get_notion_database", "getAll": "list_notion_databases", "search": "search_notion"},
            ),
            **_ops(
                "databasePage",
                {
                    "create": "create_notion_page",
                    "get": "get_notion_page",
                    "getAll": "query_notion_database",
                    "update": "update_notion_page",
                },
            ),
            **_ops(
                "page",
                {
                    "archive": "trash_notion_page",
                    "create": "create_notion_page",
                    "get": "get_notion_page",
                    "search": "search_notion",
                },
            ),
            **_ops("user", {"get": "get_notion_user", "getAll": "list_notion_users"}),
        },
    ),
    "hubspot": N8nApp(
        "hubspot",
        "HubSpot",
        default_resource="deal",
        default_operations={"contact": "upsert", "company": "create", "deal": "create", "ticket": "create"},
        operations={
            **_ops(
                "contact",
                {
                    "get": "get_hubspot_contact",
                    "getAll": "search_hubspot_contacts",
                    "getRecentlyCreatedUpdated": "search_hubspot_contacts",
                    "search": "search_hubspot_contacts",
                    "delete": "delete_hubspot_record",
                },
            ),
            **_ops(
                "company",
                {
                    "create": "create_hubspot_company",
                    "get": "get_hubspot_company",
                    "getAll": "search_hubspot_companies",
                    "getRecentlyCreatedUpdated": "search_hubspot_companies",
                    "getRecentlyModified": "search_hubspot_companies",
                    "searchByDomain": "search_hubspot_companies",
                    "update": "update_hubspot_company",
                    "delete": "delete_hubspot_record",
                },
            ),
            **_ops(
                "deal",
                {
                    "create": "create_hubspot_deal",
                    "get": "get_hubspot_deal",
                    "getAll": "list_hubspot_deals",
                    "search": "search_hubspot_deals",
                    "update": "update_hubspot_deal",
                    "delete": "delete_hubspot_record",
                },
            ),
            **_ops(
                "ticket",
                {
                    "create": "create_hubspot_ticket",
                    "get": "get_hubspot_ticket",
                    "getAll": "search_hubspot_tickets",
                    "update": "update_hubspot_ticket",
                    "delete": "delete_hubspot_record",
                },
            ),
        },
    ),
    "gmail": N8nApp(
        "google",
        "Gmail",
        default_resource="message",
        default_operations={"message": "send", "draft": "create", "label": "getAll", "thread": "get"},
        operations={
            **_ops(
                "message",
                {
                    "get": "get_gmail_message",
                    "getAll": "search_gmail",
                    "reply": "reply_gmail",
                    "delete": "trash_gmail",
                    "addLabels": "modify_gmail_labels",
                    "removeLabels": "modify_gmail_labels",
                    "markAsRead": "modify_gmail_labels",
                    "markAsUnread": "modify_gmail_labels",
                },
            ),
            "draft:create": "create_gmail_draft",
            **_ops("label", {"get": "list_gmail_labels", "getAll": "list_gmail_labels"}),
            **_ops(
                "thread",
                {
                    "getAll": "search_gmail",
                    "reply": "reply_gmail",
                    "delete": "trash_gmail",
                    "trash": "trash_gmail",
                    "addLabels": "modify_gmail_labels",
                    "removeLabels": "modify_gmail_labels",
                },
            ),
            **_ops("messageLabel", {"add": "modify_gmail_labels", "remove": "modify_gmail_labels"}),
        },
        event="new_email",
    ),
    "googleDrive": N8nApp(
        "google",
        "Google Drive",
        default_resource="file",
        default_operations={"file": "upload", "fileFolder": "search", "folder": "create"},
        operations={
            **_ops(
                "file",
                {
                    "upload": "upload_google_drive_file",
                    "createFromText": "upload_google_drive_file",
                    "download": "download_google_drive_file",
                    "list": "list_google_drive",
                },
            ),
            "fileFolder:search": "search_google_drive",
        },
    ),
    "googleCalendar": N8nApp(
        "google",
        "Google Calendar",
        default_resource="event",
        default_operations={"event": "create", "calendar": "availability"},
        operations=_ops(
            "event",
            {
                "create": "create_google_event",
                "get": "get_google_event",
                "getAll": "read_google_calendar",
                "update": "update_google_event",
                "delete": "delete_google_event",
            },
        ),
    ),
    "googleDocs": N8nApp(
        "google",
        "Google Docs",
        default_resource="document",
        default_operations={"document": "create"},
        operations={"document:get": "read_google_doc"},
    ),
    "googleTasks": N8nApp(
        "google",
        "Google Tasks",
        default_resource="task",
        default_operations={"task": "create"},
        operations=_ops(
            "task",
            {
                "create": "create_google_task",
                "getAll": "list_google_tasks",
                "update": "update_google_task",
                "delete": "delete_google_task",
            },
        ),
    ),
    "googleChat": N8nApp(
        "google",
        "Google Chat",
        default_resource="message",
        default_operations={"message": "create", "space": "getAll", "member": "getAll"},
        operations={
            "message:create": "send_google_chat_message",
            "space:getAll": "list_google_chat_spaces",
            "member:getAll": "list_google_chat_members",
        },
    ),
    "microsoftOutlook": N8nApp(
        "microsoft",
        "Microsoft Outlook",
        default_resource="message",
        default_operations={"message": "send", "draft": "create", "event": "create", "messageAttachment": "get"},
        operations={
            **_ops(
                "message",
                {
                    "send": "send_microsoft_email",
                    "get": "get_microsoft_email",
                    "getAll": "list_microsoft_emails",
                    "reply": "reply_microsoft_email_in_thread",
                },
            ),
            **_ops("draft", {"create": "create_microsoft_draft_email", "send": "send_microsoft_draft"}),
            **_ops(
                "event",
                {
                    "create": "create_microsoft_calendar_event",
                    "get": "get_microsoft_calendar_event",
                    "getAll": "list_microsoft_calendar_events",
                    "update": "update_microsoft_calendar_event",
                    "delete": "delete_microsoft_calendar_event",
                },
            ),
            "messageAttachment:get": "get_microsoft_attachment",
        },
        event="new_email",
    ),
    "microsoftOneDrive": N8nApp(
        "microsoft",
        "Microsoft OneDrive",
        default_resource="file",
        operations={
            "file:download": "download_microsoft_file",
            "file:upload": "upload_microsoft_drive_file",
            "file:search": "search_microsoft_files",
            "folder:getChildren": "list_microsoft_drive_files",
            "folder:search": "search_microsoft_files",
        },
    ),
    "microsoftToDo": N8nApp(
        "microsoft",
        "Microsoft To Do",
        default_resource="task",
        default_operations={"task": "create"},
        operations=_ops(
            "task",
            {
                "create": "create_microsoft_task",
                "getAll": "list_microsoft_tasks",
                "update": "update_microsoft_task",
                "delete": "delete_microsoft_task",
            },
        ),
    ),
    "microsoftTeams": N8nApp(
        "microsoft",
        "Microsoft Teams",
        default_resource="channel",
        default_operations={"channelMessage": "create", "chatMessage": "create"},
        operations={
            "channelMessage:getAll": "list_microsoft_channel_messages",
            "chatMessage:getAll": "list_microsoft_chat_messages",
            "chatMessage:get": "get_microsoft_chat_message",
        },
        event="new_teams_message",
    ),
    "microsoftExcel": N8nApp("microsoft", "Microsoft Excel"),
    "microsoftEntra": N8nApp("entra", "Microsoft Entra ID"),
    "slack": N8nApp(
        "slack",
        "Slack",
        default_resource="message",
        default_operations={"message": "post", "channel": "create", "file": "upload", "reaction": "add"},
        operations={
            "message:search": "search_slack_messages",
            **_ops(
                "channel",
                {
                    "getAll": "list_slack_channels",
                    "history": "read_slack_messages",
                    "replies": "read_slack_thread",
                    "create": "create_slack_channel",
                    "invite": "invite_to_slack_channel",
                    "kick": "kick_from_slack_channel",
                    "archive": "archive_slack_channel",
                    "rename": "rename_slack_channel",
                    "setPurpose": "set_slack_channel_purpose",
                    "setTopic": "set_slack_channel_topic",
                    "open": "open_slack_dm",
                },
            ),
            **_ops("file", {"upload": "upload_slack_file", "getAll": "list_slack_files", "get": "get_slack_file_info"}),
            **_ops("reaction", {"add": "add_slack_reaction", "get": "get_slack_reactions"}),
            **_ops("user", {"info": "get_slack_user", "getAll": "list_slack_users"}),
        },
        event="new_slack_message",
    ),
    "theHive": N8nApp(
        "thehive",
        "TheHive",
        default_resource="alert",
        default_operations=_THEHIVE_DEFAULTS,
        operations=_THEHIVE_OPERATIONS,
    ),
    "theHiveProject": N8nApp(
        "thehive",
        "TheHive 5",
        default_resource="alert",
        default_operations=_THEHIVE_DEFAULTS,
        operations=_THEHIVE_OPERATIONS,
    ),
    "serviceNow": N8nApp(
        "servicenow",
        "ServiceNow",
        default_resource="user",
        operations={
            **_ops(
                "incident",
                {
                    "create": "create_servicenow_incident",
                    "get": "get_servicenow_incident",
                    "getAll": "list_servicenow_incidents",
                    "update": "update_servicenow_incident",
                },
            ),
            **_ops(
                "tableRecord",
                {
                    "create": "manage_servicenow_record",
                    "update": "manage_servicenow_record",
                    "delete": "manage_servicenow_record",
                    "get": "get_servicenow_record",
                    "getAll": "query_servicenow_table",
                },
            ),
            "user:getAll": "search_servicenow_users",
            "userGroup:getAll": "search_servicenow_groups",
            "configurationItems:getAll": "search_servicenow_cmdb_ci",
            "businessService:getAll": "search_servicenow_cmdb_ci",
        },
    ),
    "n8n": N8nApp(
        "n8n",
        "n8n",
        default_resource="workflow",
        operations={
            **_ops(
                "workflow",
                {
                    "activate": "set_n8n_workflow_active",
                    "deactivate": "set_n8n_workflow_active",
                    "create": "create_n8n_workflow",
                    "delete": "delete_n8n_workflow",
                    "get": "get_n8n_workflow",
                    "getAll": "list_n8n_workflows",
                    "update": "update_n8n_workflow",
                },
            ),
            **_ops(
                "execution",
                {
                    "get": "get_n8n_execution",
                    "getAll": "list_n8n_executions",
                    "delete": "delete_n8n_execution",
                },
            ),
        },
    ),
    "supabase": N8nApp(
        "supabase",
        "Supabase",
        default_resource="row",
        default_operations={"row": "create"},
        operations=_ops(
            "row",
            {
                "create": "execute_supabase_sql",
                "update": "execute_supabase_sql",
                "delete": "execute_supabase_sql",
                "get": "query_supabase_sql",
                "getAll": "query_supabase_sql",
            },
        ),
    ),
    "urlScanIo": N8nApp(
        "urlscan",
        "urlscan.io",
        default_resource="scan",
        default_operations={"scan": "perform"},
        operations=_ops(
            "scan",
            {"perform": "submit_urlscan_scan", "get": "get_urlscan_result", "getAll": "search_urlscan_scans"},
        ),
    ),
    "misp": N8nApp(
        "misp",
        "MISP",
        default_resource="attribute",
        default_operations={"attribute": "create", "event": "create", "eventTag": "add"},
        operations={
            **_ops(
                "attribute",
                {
                    "create": "add_misp_attribute",
                    "get": "get_misp_attribute",
                    "getAll": "search_misp_attributes",
                    "search": "search_misp_attributes",
                    "update": "update_misp_attribute",
                    "delete": "delete_misp_attribute",
                },
            ),
            **_ops(
                "event",
                {
                    "create": "create_misp_event",
                    "get": "get_misp_event",
                    "getAll": "search_misp_events",
                    "search": "search_misp_events",
                    "update": "update_misp_event",
                    "delete": "delete_misp_event",
                    "publish": "publish_misp_event",
                },
            ),
            **_ops("eventTag", {"add": "add_misp_tag", "remove": "remove_misp_tag"}),
            "tag:getAll": "list_misp_tags",
            "feed:getAll": "list_misp_feeds",
            "galaxy:getAll": "list_misp_galaxies",
            "organisation:getAll": "list_misp_organisations",
            "user:getAll": "list_misp_users",
            "warninglist:getAll": "list_misp_warninglists",
        },
    ),
    "splunk": N8nApp(
        "splunk",
        "Splunk",
        default_resource="search",
        operations={
            "search:create": "run_splunk_search",
            "search:getResult": "get_splunk_search_results",
            "firedAlert:getReport": "list_splunk_fired_alerts",
        },
    ),
    "elasticSecurity": N8nApp(
        "elastic_security",
        "Elastic Security",
        default_resource="case",
        default_operations={"case": "create", "caseComment": "add"},
        operations={
            **_ops(
                "case",
                {
                    "create": "create_elastic_case",
                    "get": "get_elastic_case",
                    "getAll": "list_elastic_cases",
                },
            ),
            "caseComment:add": "comment_elastic_case",
        },
    ),
    "elasticsearch": N8nApp(
        "elastic_security",
        "Elasticsearch",
        default_resource="document",
        operations={"document:get": "search_elastic", "document:getAll": "search_elastic"},
    ),
    "linkedIn": N8nApp(
        "linkedin",
        "LinkedIn",
        default_resource="post",
        default_operations={"post": "create"},
        operations={"post:create": "create_linkedin_post"},
    ),
    "twitter": N8nApp(
        "x",
        "X (Twitter)",
        default_resource="tweet",
        default_operations={"tweet": "create", "directMessage": "create"},
        operations={
            **_ops(
                "tweet",
                {
                    "create": "create_x_tweet",
                    "search": "search_x_tweets",
                    "delete": "delete_x_tweet",
                    "like": "like_x_tweet",
                    "retweet": "retweet_x",
                },
            ),
            "directMessage:create": "send_x_dm",
            "user:searchUser": "get_x_user_profile",
        },
    ),
    "facebookGraphApi": N8nApp("facebook", "Facebook Graph API"),
    "bluesky": N8nApp(
        "bluesky",
        "Bluesky",
        default_resource="post",
        default_operations={"post": "create"},
        operations={"post:create": "create_bluesky_post", "post:delete": "delete_bluesky_post"},
    ),
    "awsSes": N8nApp(
        "ses",
        "AWS SES",
        default_resource="email",
        default_operations={"email": "send"},
        operations={"email:send": "send_ses_email", "email:sendTemplate": "send_ses_templated_email"},
    ),
    "rssFeedRead": N8nApp("rss", "RSS"),
    "metabase": N8nApp("metabase", "Metabase"),
    "grafana": N8nApp("grafana", "Grafana"),
    "figma": N8nApp("figma", "Figma"),
    "googleSheets": N8nApp(
        "google",
        "Google Sheets",
        default_resource="sheet",
        default_operations={"sheet": "read"},
        operations=_ops(
            "sheet",
            {
                "read": "get_google_sheet_values",
                "lookup": "get_google_sheet_values",
                "getAll": "get_google_sheet_values",
                "append": "append_google_sheet_rows",
                "appendOrUpdate": "append_or_update_google_sheet_rows",
                "upsert": "append_or_update_google_sheet_rows",
                "update": "update_google_sheet_rows",
                "clear": "clear_google_sheet_values",
                # n8n's `delete` removes rows or columns: left for someone to pick.
            },
        ),
    ),
    "gitlab": N8nApp(
        "gitlab",
        "GitLab",
        default_resource="issue",
        default_operations={
            "issue": "create",
            "file": "create",
            "repository": "get",
            "release": "create",
            "user": "getRepositories",
        },
        operations={
            **_ops(
                "issue",
                {
                    "create": "create_gitlab_issue",
                    "createComment": "add_gitlab_issue_note",
                    "edit": "update_gitlab_issue",
                    "get": "get_gitlab_issue",
                    "lock": "update_gitlab_issue",
                },
            ),
            **_ops(
                "file",
                {
                    "create": "create_gitlab_file",
                    "edit": "update_gitlab_file",
                    "delete": "delete_gitlab_file",
                    "get": "get_gitlab_file",
                    "list": "list_gitlab_repository_tree",
                },
            ),
            **_ops(
                "release",
                {
                    "create": "create_gitlab_release",
                    "delete": "delete_gitlab_release",
                    "get": "get_gitlab_release",
                    "getAll": "list_gitlab_releases",
                    "update": "update_gitlab_release",
                },
            ),
            **_ops("repository", {"get": "get_gitlab_project", "getIssues": "list_gitlab_issues"}),
            **_ops("user", {"getRepositories": "list_gitlab_projects"}),
        },
    ),
    "zendesk": N8nApp(
        "zendesk",
        "Zendesk",
        default_resource="ticket",
        default_operations={"ticket": "create", "ticketField": "get", "user": "create", "organization": "create"},
        operations={
            **_ops(
                "ticket",
                {
                    "create": "create_zendesk_ticket",
                    "delete": "delete_zendesk_ticket",
                    "get": "get_zendesk_ticket",
                    "getAll": "search_zendesk_tickets",
                    "recover": "restore_zendesk_ticket",
                    "update": "update_zendesk_ticket",
                },
            ),
            **_ops("ticketField", {"get": "get_zendesk_ticket_field", "getAll": "list_zendesk_ticket_fields"}),
            **_ops(
                "user",
                {
                    "create": "create_zendesk_user",
                    "delete": "delete_zendesk_user",
                    "get": "get_zendesk_user",
                    "getAll": "search_zendesk_users",
                    "getOrganizations": "list_zendesk_user_organizations",
                    "search": "search_zendesk_users",
                    "update": "update_zendesk_user",
                },
            ),
            **_ops(
                "organization",
                {
                    "create": "create_zendesk_organization",
                    "delete": "delete_zendesk_organization",
                    "get": "get_zendesk_organization",
                    "getAll": "list_zendesk_organizations",
                    "update": "update_zendesk_organization",
                },
            ),
        },
    ),
    "pagerDuty": N8nApp(
        "pagerduty",
        "PagerDuty",
        default_resource="incident",
        default_operations={"incident": "create", "incidentNote": "create", "logEntry": "get", "user": "get"},
        operations={
            **_ops(
                "incident",
                {
                    "create": "create_pagerduty_incident",
                    "get": "get_pagerduty_incident",
                    "getAll": "list_pagerduty_incidents",
                    "update": "update_pagerduty_incident",
                },
            ),
            **_ops(
                "incidentNote",
                {"create": "add_pagerduty_incident_note", "getAll": "list_pagerduty_incident_notes"},
            ),
            **_ops("logEntry", {"get": "get_pagerduty_log_entry", "getAll": "list_pagerduty_log_entries"}),
            **_ops("user", {"get": "get_pagerduty_user"}),
        },
    ),
    "securityScorecard": N8nApp(
        "securityscorecard",
        "SecurityScorecard",
        default_resource="company",
        default_operations={"company": "getFactor"},
        operations={
            **_ops(
                "company",
                {
                    "getScorecard": "get_securityscorecard_company",
                    "getFactor": "list_securityscorecard_factors",
                    "getFactorHistorical": "get_securityscorecard_factor_history",
                    "getHistoricalScore": "get_securityscorecard_score_history",
                },
            ),
            **_ops("industry", {"getScore": "get_securityscorecard_industry_score"}),
            **_ops(
                "portfolio",
                {
                    "create": "create_securityscorecard_portfolio",
                    "delete": "delete_securityscorecard_portfolio",
                    "getAll": "list_securityscorecard_portfolios",
                },
            ),
            **_ops(
                "portfolioCompany",
                {
                    "add": "add_securityscorecard_portfolio_company",
                    "getAll": "list_securityscorecard_portfolio_companies",
                    "remove": "remove_securityscorecard_portfolio_company",
                },
            ),
            **_ops(
                "report",
                {"generate": "generate_securityscorecard_report", "getAll": "list_securityscorecard_reports"},
            ),
        },
    ),
    "mattermost": N8nApp(
        "mattermost",
        "Mattermost",
        default_resource="message",
        default_operations={"message": "post", "channel": "create", "reaction": "create", "user": "create"},
        operations={
            **_ops(
                "message",
                {
                    "post": "post_mattermost_message",
                    "postEphemeral": "post_mattermost_ephemeral_message",
                    "delete": "delete_mattermost_post",
                },
            ),
            **_ops(
                "reaction",
                {
                    "create": "add_mattermost_reaction",
                    "delete": "remove_mattermost_reaction",
                    "getAll": "list_mattermost_reactions",
                },
            ),
            **_ops(
                "channel",
                {
                    "addUser": "add_mattermost_channel_member",
                    "create": "create_mattermost_channel",
                    "delete": "archive_mattermost_channel",
                    "members": "list_mattermost_channel_members",
                    "restore": "restore_mattermost_channel",
                    "search": "search_mattermost_channels",
                },
            ),
            **_ops(
                "user",
                {
                    "deactive": "deactivate_mattermost_user",
                    "getAll": "list_mattermost_users",
                    "getById": "get_mattermost_user",
                    "invite": "invite_mattermost_users",
                },
            ),
        },
    ),
}


# Apps with no provider that an exposure-management platform has a use for:
# each is a catalog candidate, and the reason is the CTEM use it serves.
# Every other app with no provider is outside the catalog.
CTEM_CANDIDATES: dict[str, tuple[str, str]] = {
    "postgres": ("PostgreSQL", "asset inventories, CMDB exports and scoring tables kept in SQL"),
    "mySql": ("MySQL", "asset inventories, CMDB exports and scoring tables kept in SQL"),
    "microsoftSql": ("Microsoft SQL Server", "asset inventories, CMDB exports and scoring tables kept in SQL"),
    "mongoDb": ("MongoDB", "asset and finding stores kept in a document database"),
    "snowflake": ("Snowflake", "security data lake queries"),
    "googleBigQuery": ("BigQuery", "security data lake queries"),
    "awsS3": ("Amazon S3", "scan reports and evidence exchanged as files"),
    "ftp": ("FTP / SFTP", "scan reports and evidence exchanged as files"),
    "linear": ("Linear", "remediation tickets"),
    "zammad": ("Zammad", "remediation and incident tickets for CERT teams"),
    "freshdesk": ("Freshdesk", "remediation and incident tickets"),
    "signl4": ("SIGNL4", "on-call escalation of critical exposures"),
    "rocketchat": ("Rocket.Chat", "the self-hosted chat many SOC teams are notified in"),
    "matrix": ("Matrix", "the self-hosted chat many SOC teams are notified in"),
    "cortex": ("Cortex", "observable enrichment through TheHive's analyzers"),
    "venafiTlsProtectCloud": ("Venafi TLS Protect", "certificate exposure: expiring and weak certificates"),
    "venafiTlsProtectDatacenter": ("Venafi TLS Protect", "certificate exposure: expiring and weak certificates"),
    "bitwarden": ("Bitwarden", "secret hygiene, beside the 1Password integration"),
    "uptimeRobot": ("UptimeRobot", "availability of exposed services"),
    "kafka": ("Kafka", "the event bus SIEM and scanner pipelines publish findings on"),
    "amqp": ("AMQP", "the event bus SIEM and scanner pipelines publish findings on"),
    "rabbitmq": ("RabbitMQ", "the event bus SIEM and scanner pipelines publish findings on"),
    "mqtt": ("MQTT", "the event bus SIEM and scanner pipelines publish findings on"),
}

# n8n triggers that poll their app (n8n reads it on a schedule) rather than
# receive its webhook: the counterpart is a schedule, not an inbound call.
POLLING_TRIGGERS = frozenset(
    {
        "gmailTrigger",
        "googleSheetsTrigger",
        "googleDriveTrigger",
        "googleCalendarTrigger",
        "googleBusinessProfileTrigger",
        "airtableTrigger",
        "notionTrigger",
        "microsoftOutlookTrigger",
        "microsoftOneDriveTrigger",
        "microsoftExcelTrigger",
        "rssFeedReadTrigger",
        "coinGeckoTrigger",
        "baserowTrigger",
        "nocoDbTrigger",
        "todoistTrigger",
        "clockifyTrigger",
        "stravaTrigger",
    }
)

# Triggers on a message bus or the n8n host itself: nothing calls a webhook.
HOST_TRIGGERS = frozenset(
    {
        "kafkaTrigger",
        "amqpTrigger",
        "rabbitmqTrigger",
        "mqttTrigger",
        "sseTrigger",
        "postgresTrigger",
        "redisTrigger",
        "localFileTrigger",
        "n8nTrigger",
        "workflowTrigger",
        "awsSqsTrigger",
    }
)


def app_key(short_type: str) -> str:
    """``githubTrigger`` / ``githubTool`` → ``github``; anything else unchanged."""
    for suffix in ("Trigger", "Tool"):
        if short_type.endswith(suffix) and len(short_type) > len(suffix):
            return short_type[: -len(suffix)]
    return short_type


def app_for(short_type: str) -> N8nApp | None:
    return N8N_APPS.get(short_type) or N8N_APPS.get(app_key(short_type))


def tool_for(app: N8nApp, resource: str, operation: str) -> str:
    """The provider tool for an n8n ``resource:operation`` ("" when none)."""
    return app.operations.get(f"{resource}:{operation}", "")


def outside_catalog_hint(short_type: str) -> str:
    """Why an app with no provider stays unresolved, and what to do instead."""
    key = app_key(short_type)
    candidate = CTEM_CANDIDATES.get(key)
    if candidate is not None:
        label, use = candidate
        return (
            f"XTM One has no {label} integration yet; it is a catalog candidate "
            f"for exposure management ({use}). Meanwhile, call its API with an "
            "HTTP request node."
        )
    named = " ".join(short_type.split())[:80]  # the type name comes from the document
    return (
        f"The n8n `{named}` app is outside XTM One's integration catalog: "
        "it has no exposure-management use. If the step matters, call the app's "
        "API with an HTTP request node; otherwise delete it."
    )
