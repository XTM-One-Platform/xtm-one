"""Structural tests for the domain-split Notion provider package.

Guards the refactor (single file -> package): the package must build the full
tool surface, expose stable tool names with no duplicates, tag every tool with
the canonical source, register a handler for every advertised tool, and keep
exporting the symbols external consumers + the test-suite import.
"""

import app.integrations.providers.notion as notion
from app.services.integration_tools import _SafeIntegrationExecutor

# Every tool the package must expose (a guard against accidental removal during
# future refactors). 28 total: the original surface + the gap-closers
# (restore_notion_page, get_notion_me) + the file uploader.
_REQUIRED_TOOLS = {
    "search_notion",
    "list_notion_children",
    "get_notion_page",
    "get_notion_page_properties",
    "create_notion_page",
    "update_notion_page",
    "move_notion_page",
    "trash_notion_page",
    "restore_notion_page",
    "get_notion_page_property",
    "query_notion_database",
    "list_notion_databases",
    "get_notion_database",
    "list_notion_templates",
    "create_notion_database",
    "update_notion_database",
    "get_notion_block",
    "list_notion_block_children",
    "append_notion_blocks",
    "append_notion_blocks_json",
    "update_notion_block",
    "delete_notion_block",
    "upload_notion_file",
    "download_notion_file",
    "list_notion_comments",
    "add_notion_comment",
    "list_notion_users",
    "get_notion_user",
    "get_notion_me",
}


def _build():
    executor = _SafeIntegrationExecutor()
    return notion.build_tools("dummy-token", executor), executor


def test_notion_package_builds_full_surface_without_duplicates():
    defs, _ = _build()
    names = [d.name for d in defs]
    assert len(names) == len(set(names)), "duplicate tool names detected"
    assert len(names) == 29


def test_notion_required_tools_present():
    defs, _ = _build()
    names = {d.name for d in defs}
    missing = _REQUIRED_TOOLS - names
    assert not missing, f"missing expected tools: {sorted(missing)}"


def test_notion_all_tools_tagged_with_source():
    defs, _ = _build()
    assert all(d.source == "integration:notion" for d in defs)


def test_notion_every_tool_has_a_registered_handler():
    defs, executor = _build()
    for d in defs:
        assert executor._handlers.get(d.name) is not None, f"no handler for {d.name}"


def test_notion_build_tools_degrades_without_raising_on_empty_token():
    executor = _SafeIntegrationExecutor()
    # Notion does not gate tools on the token (calls would 401 at runtime);
    # building must still not raise and must keep the full surface.
    defs = notion.build_tools("", executor)
    assert len(defs) == 29


def test_notion_package_reexports_consumer_surface():
    # External importers rely on these names off the package root.
    assert notion.NOTION_API.endswith("/v1")
    assert isinstance(notion.NOTION_VERSION, str) and notion.NOTION_VERSION
    # kb_sync imports _rich_text_to_plain; integration_testing imports the
    # constants above; the test-suite imports these helpers.
    for attr in (
        "_rich_text_to_plain",
        "_parse_json_arg",
        "_text_to_blocks",
        "_parse_inline_markdown",
        "_sanitize_rich_text",
        "_notion_file_block_type",
        "safe_request",
        "AutoRefreshClient",
    ):
        assert hasattr(notion, attr), f"package no longer re-exports {attr}"
