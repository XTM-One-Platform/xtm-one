"""Structural tests for the domain-split Jira provider package.

Guards the refactor: the package must build the full tool surface, expose stable
tool names with no duplicates, tag every tool with the canonical source, and
degrade gracefully (no exception, no tools) when the credential is incomplete.
"""

from app.integrations.providers.jira import JiraIntegration, build_tools
from app.services.integration_tools import _SafeIntegrationExecutor

# High-value tools that must always be present (a guard against accidental
# removal during future refactors). Includes the custom-field discovery tools
# that close the original gap.
_REQUIRED_TOOLS = {
    "search_jira_issues",
    "count_jira_issues",
    "get_jira_issue",
    "create_jira_issue",
    "update_jira_issue",
    "transition_jira_issue",
    "list_jira_fields",
    "get_jira_create_fields",
    "get_jira_edit_fields",
    "list_jira_comments",
    "add_jira_comment",
    "list_jira_attachments",
    "download_jira_attachment",
    "list_jira_boards",
    "manage_jira_sprint",
    "manage_jira_epic",
    "list_jira_projects",
    "list_jira_metadata",
    "get_jira_myself",
    "find_jira_assignable_users",
    "link_jira_issues",
    "list_jira_filters",
    "list_jira_service_desks",
    "get_jira_request_type_fields",
    "manage_jira_service_request",
    "list_jira_service_desk_queues",
}


def _build():
    executor = _SafeIntegrationExecutor()
    return build_tools("cloud-123||token", executor), executor


def test_jira_provider_identity():
    integ = JiraIntegration()
    assert integ.provider_name == "jira"
    assert integ.auth_types == ["oauth"]


def test_jira_package_builds_full_surface_without_duplicates():
    defs, _ = _build()
    names = [d.name for d in defs]
    assert len(names) == len(set(names)), "duplicate tool names detected"
    # The refactor expands the surface well beyond the original 25 tools.
    assert len(names) >= 50


def test_jira_required_tools_present():
    defs, _ = _build()
    names = {d.name for d in defs}
    missing = _REQUIRED_TOOLS - names
    assert not missing, f"missing expected tools: {sorted(missing)}"


def test_jira_all_tools_tagged_with_source():
    defs, _ = _build()
    assert all(d.source == "integration:jira" for d in defs)


def test_jira_every_tool_is_registered_on_executor():
    defs, executor = _build()
    for d in defs:
        assert d.name in executor._handlers, f"{d.name} has no registered handler"


def test_jira_build_tools_degrades_without_credentials():
    executor = _SafeIntegrationExecutor()
    # No cloud_id (the access token alone) -> no site to target -> no tools.
    assert build_tools("plain-token-no-delimiter", executor) == []
    # cloud_id present but empty access token -> no valid bearer -> no tools.
    assert build_tools("cloud-123||", executor) == []
