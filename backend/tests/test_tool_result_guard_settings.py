"""The settings behind tool-result screening: the options, their controls and audit findings.

* ``prompt_guard_scanned_tools`` (a ``tools`` list) and
  ``prompt_guard_block_tool_results`` (a switch) are Settings > Access and security
  options: empty and off by default, validated like their variables;
* each is a security control: the scope ``enforced`` once it lists a tool,
  blocking ``monitoring`` while tools are screened and it is off;
* the Security Audit says, while a guard is connected, whether tool results
  are screened and whether a block rule withholds them (log-only is a
  finding of its own, recommending the field);
* the scope editor's candidates come from an admin-only endpoint.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.core.config as config_module
from app.api.v1 import security_audit
from app.api.v1 import settings as settings_api
from app.core import security_options as so
from app.core.config import MAX_SCANNED_TOOL_CHARS, MAX_SCANNED_TOOLS, Settings
from app.core.security_controls import control_statuses

APP_DIR = Path(__file__).resolve().parents[1] / "app"


# ---------------------------------------------------------------------------
# The options
# ---------------------------------------------------------------------------


def test_nothing_is_screened_or_withheld_by_default():
    assert so.SECURITY_OPTIONS["prompt_guard_scanned_tools"].default() == ()
    assert so.SECURITY_OPTIONS["prompt_guard_block_tool_results"].default() is False
    assert so.security_option("prompt_guard_scanned_tools") == ()
    assert so.security_option("prompt_guard_block_tool_results") is False


def test_a_stored_scope_is_sorted_each_entry_once():
    option = so.SECURITY_OPTIONS["prompt_guard_scanned_tools"]
    assert option.normalize([" web_fetch", "search_jira_issues", "web_fetch", ""]) == (
        "search_jira_issues",
        "web_fetch",
    )
    assert option.normalize("web_fetch, search_jira_issues") == ("search_jira_issues", "web_fetch")
    assert option.to_wire(("search_jira_issues", "web_fetch")) == ["search_jira_issues", "web_fetch"]


def test_a_custom_tool_entry_is_its_id_in_canonical_form():
    option = so.SECURITY_OPTIONS["prompt_guard_scanned_tools"]
    tool_id = "0F3B3A5E-3C5D-4D1E-9D0B-2D1B6F4F7A10"
    assert option.normalize([f"custom:{tool_id}", f"CUSTOM:{{{tool_id.lower()}}}", "web_fetch"]) == (
        f"custom:{tool_id.lower()}",
        "web_fetch",
    )


@pytest.mark.parametrize("entry", ["custom:crm_lookup", "custom:", "custom:0f3b3a5e"])
def test_a_custom_tool_entry_must_name_an_id(entry):
    with pytest.raises(ValueError, match="names no custom tool"):
        so.SECURITY_OPTIONS["prompt_guard_scanned_tools"].normalize([entry])


@pytest.mark.parametrize(
    "value",
    [
        ["web\nfetch"],
        ["web\x00fetch"],
        ["a" * (MAX_SCANNED_TOOL_CHARS + 1)],
        [f"tool_{i}" for i in range(MAX_SCANNED_TOOLS + 1)],
        [1],
        {"web_fetch": True},
        True,
    ],
)
def test_a_malformed_scope_is_refused(value):
    with pytest.raises(ValueError):
        so.SECURITY_OPTIONS["prompt_guard_scanned_tools"].normalize(value)


def test_the_blocking_switch_is_never_coerced():
    option = so.SECURITY_OPTIONS["prompt_guard_block_tool_results"]
    assert option.normalize(True) is True
    for value in ("true", 1):
        with pytest.raises(ValueError):
            option.normalize(value)


def test_the_variable_is_parsed_like_the_field(monkeypatch):
    monkeypatch.setenv("PROMPT_GUARD_SCANNED_TOOLS", "web_fetch, search_jira_issues,web_fetch")
    assert Settings().prompt_guard_scanned_tools == ["search_jira_issues", "web_fetch"]
    monkeypatch.setenv("PROMPT_GUARD_SCANNED_TOOLS", "web_fetch,search\tjira")
    with pytest.raises(ValueError):
        Settings()


def test_a_stored_scope_applies_from_the_snapshot():
    so.publish_stored({"prompt_guard_scanned_tools": ("web_fetch",), "prompt_guard_block_tool_results": True})
    assert so.security_option("prompt_guard_scanned_tools") == ("web_fetch",)
    assert so.security_option("prompt_guard_block_tool_results") is True


def test_an_emptied_scope_is_recommended_from_its_field():
    text = so.where_to_set("prompt_guard_scanned_tools", "")
    assert text.startswith('empty the "Tools screened by the prompt guard" field')


# ---------------------------------------------------------------------------
# The controls
# ---------------------------------------------------------------------------


def _states() -> dict[str, str]:
    return {status.id: status.state for status in control_statuses(None, None)}


def test_the_controls_follow_the_scope_and_the_switch(monkeypatch):
    assert _states()["agents.tool_result_guard_scope"] == "off"
    assert _states()["agents.tool_result_guard_blocking"] == "off"

    monkeypatch.setattr(config_module.settings, "prompt_guard_scanned_tools", ["web_fetch"])
    assert _states()["agents.tool_result_guard_scope"] == "enforced"
    # Matches recorded, nothing withheld: it observes what it would block.
    assert _states()["agents.tool_result_guard_blocking"] == "monitoring"

    monkeypatch.setattr(config_module.settings, "prompt_guard_block_tool_results", True)
    assert _states()["agents.tool_result_guard_blocking"] == "enforced"

    # Blocking with nothing screened withholds nothing.
    monkeypatch.setattr(config_module.settings, "prompt_guard_scanned_tools", [])
    assert _states()["agents.tool_result_guard_blocking"] == "off"


def test_both_controls_are_opt_in():
    statuses = {status.id: status for status in control_statuses(None, None)}
    assert statuses["agents.tool_result_guard_scope"].opt_in is True
    assert statuses["agents.tool_result_guard_blocking"].opt_in is True


# ---------------------------------------------------------------------------
# The audit
# ---------------------------------------------------------------------------


async def _findings(monkeypatch, *, connected: bool) -> list[security_audit.AuditFinding]:
    async def _connected(_db, provider):
        assert provider == "nova"
        return connected

    monkeypatch.setattr("app.services.integration_state.any_provider_connected", _connected)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_tool_result_guard(SimpleNamespace(), findings)
    return findings


async def test_no_guard_and_no_scope_is_nothing_to_say(monkeypatch):
    assert await _findings(monkeypatch, connected=False) == []


async def test_a_scope_without_a_guard_says_nothing_is_screened(monkeypatch):
    so.publish_stored({"prompt_guard_scanned_tools": ("web_fetch",)})
    [finding] = await _findings(monkeypatch, connected=False)
    assert (finding.id, finding.severity, finding.control) == (
        "agents.tool_result_guard_not_connected",
        "info",
        "agents.tool_result_guard_scope",
    )


async def test_a_connected_guard_with_no_scope_screens_no_tool_result(monkeypatch):
    # In use and off: every tool result reaches agents unscreened.
    [finding] = await _findings(monkeypatch, connected=True)
    assert (finding.id, finding.severity) == ("agents.tool_result_guard_unscoped", "warning")
    # The field, linked from the finding, and the variable that wins over it.
    assert finding.recommendation.startswith(
        'Screen the tools that return outside content: for fetched pages, set "Tools screened by the prompt guard" '
        "in Settings > Access and security > Security options to web_fetch (or set PROMPT_GUARD_SCANNED_TOOLS=web_fetch"
    )


async def test_log_only_screening_is_reported_with_the_field_that_blocks(monkeypatch):
    so.publish_stored({"prompt_guard_scanned_tools": ("web_fetch", "search_jira_issues")})
    [finding] = await _findings(monkeypatch, connected=True)
    assert (finding.id, finding.severity, finding.control) == (
        "agents.tool_result_guard_log_only",
        "info",
        "agents.tool_result_guard_blocking",
    )
    assert finding.title == "The prompt guard screens 2 tool(s) in log-only mode"
    assert finding.recommendation.startswith(
        'Turn on "Withhold tool results a guard rule blocks" in Settings > Access and security > Security options'
    )


async def test_blocking_is_reported_as_a_pass(monkeypatch):
    so.publish_stored({"prompt_guard_scanned_tools": ("web_fetch",), "prompt_guard_block_tool_results": True})
    [finding] = await _findings(monkeypatch, connected=True)
    assert (finding.id, finding.severity, finding.control) == (
        "agents.tool_result_guard_blocking",
        "pass",
        "agents.tool_result_guard_blocking",
    )


# ---------------------------------------------------------------------------
# The editor's candidates
# ---------------------------------------------------------------------------


def test_the_candidates_are_for_admins_only():
    [route] = [
        route
        for route in settings_api.router.routes
        if getattr(route, "path", "") == "/security-options/scannable-tools"
    ]
    dependencies = [dependency.call for dependency in route.dependant.dependencies]
    from app.api.deps import require_admin

    assert require_admin in dependencies
    assert route.methods == {"GET"}


def test_every_agent_wired_candidate_is_a_tool_the_platform_registers():
    sources = "\n".join(path.read_text(encoding="utf-8") for path in (APP_DIR / "services").rglob("*.py"))
    for name in settings_api._SCANNABLE_AGENT_TOOLS:
        assert re.search(rf'name="{name}"', sources), name


class _Catalog:
    """Answers the custom tool query with *custom_tools* and the MCP server query with *servers*."""

    def __init__(self, *, servers, custom_tools=()):
        self._servers = servers
        self._custom_tools = list(custom_tools)

    async def execute(self, statement):
        tables = {table.name for table in statement.get_final_froms()}
        rows = self._custom_tools if tables == {"custom_tools"} else self._servers
        assert tables in ({"custom_tools"}, {"mcp_servers"}), tables
        return SimpleNamespace(all=lambda: rows)


async def test_the_candidates_offer_built_ins_mcp_tools_and_integration_tools(monkeypatch):
    server_id = "0c1b0c55-7c42-4b8f-9b0e-3f5a1b7f8c01"
    db = _Catalog(
        servers=[
            (
                server_id,
                "Docs",
                [
                    {"name": "search_docs", "description": "Search the documentation.\n  Fast."},
                    {"name": "search_docs", "description": "a duplicate"},
                    {"name": ""},
                    "not a tool",
                ],
            ),
            ("another", "Empty", None),
        ]
    )

    groups = {group.key: group for group in await settings_api._scannable_tool_groups(db)}

    builtin = {tool.entry for tool in groups["builtin"].tools}
    assert {"web_fetch", "search_knowledge", "consult_agent"} <= builtin
    assert [tool.model_dump() for tool in groups[f"mcp:{server_id}"].tools] == [
        {"entry": "search_docs", "name": "search_docs", "description": "Search the documentation. Fast."}
    ]
    assert groups[f"mcp:{server_id}"].label == "MCP: Docs"
    assert "mcp:another" not in groups
    # Every integration tool is offered under its own name.
    assert any(tool.entry == tool.name for key, group in groups.items() if key not in ("builtin",) for tool in group.tools)
    assert all(len(tool.description) <= 160 for group in groups.values() for tool in group.tools)
    # No custom tool: no group for them.
    assert "custom" not in groups


async def test_custom_tools_are_offered_by_id_under_their_current_name():
    first, second = "5d7c3a64-2c1f-4f55-a3f0-6c0f7a9e2b11", "1b2c3d4e-5f60-4718-8293-a4b5c6d7e8f9"
    db = _Catalog(
        servers=[],
        custom_tools=[(second, "zendesk_notes", None), (first, "crm_lookup_v2", "Look a customer up in the CRM.")],
    )

    groups = {group.key: group for group in await settings_api._scannable_tool_groups(db)}

    assert groups["custom"].label == "Custom tools"
    assert [tool.model_dump() for tool in groups["custom"].tools] == [
        {"entry": f"custom:{first}", "name": "crm_lookup_v2", "description": "Look a customer up in the CRM."},
        {"entry": f"custom:{second}", "name": "zendesk_notes", "description": ""},
    ]
    # What the option accepts, as the editor stores it.
    option = so.SECURITY_OPTIONS["prompt_guard_scanned_tools"]
    assert option.normalize([tool.entry for tool in groups["custom"].tools]) == (f"custom:{second}", f"custom:{first}")
