"""Integration and knowledge base data sources: a read tool's answer and a document's table, drawn.

An integration source calls one tool that only reads, through an integration
the viewer may use, with fixed arguments, and draws its JSON answer; a tool
that may change something is never called. A knowledge source reads a table
written as JSON, JSON lines, CSV or Markdown, whole or one heading section.
"""

from __future__ import annotations

import pytest

from app.services import dashboard_sources
from app.services.dashboard_definition import validate_definition
from app.services.dashboard_live import LiveError
from app.services.dashboard_sources import table_value
from app.tools.base import ToolDefinition, ToolResult

INTEGRATION = "11111111-1111-4111-8111-111111111111"
USER = "22222222-2222-4222-8222-222222222222"


@pytest.mark.parametrize(
    ("text", "table_format"),
    [
        ('[{"severity": "high", "count": 3}, {"severity": "low", "count": 7}]', "auto"),
        ('{"severity": "high", "count": 3}\n{"severity": "low", "count": 7}', "auto"),
        ("severity,count\nhigh,3\nlow,7", "auto"),
        ("| severity | count |\n|---|---:|\n| high | 3 |\n| low | 7 |", "auto"),
        ("Intro text.\n\n| severity | count |\n| --- | --- |\n| high | 3 |\n| low | 7 |\n", "markdown"),
    ],
    ids=["json", "jsonl", "csv", "markdown", "markdown-in-prose"],
)
def test_a_document_s_table_is_read_in_every_format(text, table_format):
    assert table_value(text, table_format) == [{"severity": "high", "count": 3}, {"severity": "low", "count": 7}]


def test_a_document_without_a_table_reads_nothing():
    with pytest.raises(LiveError):
        table_value("Just a paragraph of prose.")


def _definition(source: dict) -> dict:
    return validate_definition({
        "version": 2, "title": "T", "dataSources": {"s": source}, "visualizations": {},
        "layout": {"tabs": {"items": [{"layoutId": "main", "label": "Main"}]}, "layoutDefinitions": {"main": {"structure": []}}},
    })  # fmt: skip


def test_the_new_sources_are_validated_like_the_others():
    integration = _definition({"type": "integration", "options": {"integration_id": INTEGRATION, "tool": "list_jira_issues",
                                                                   "arguments": {"jql": "project = SOC"}, "path": "issues", "refresh": "15m"}})  # fmt: skip
    assert integration["dataSources"]["s"]["options"]["shape"] == "rows"
    knowledge = _definition({"type": "knowledge", "options": {"knowledge_base_id": INTEGRATION, "document_id": USER, "section": "Weekly"}})
    assert knowledge["dataSources"]["s"]["options"]["format"] == "auto"
    with pytest.raises(Exception, match="tool"):
        _definition({"type": "integration", "options": {"integration_id": INTEGRATION, "tool": "rm -rf /"}})


class _Executor:
    def __init__(self, content: str, *, error: bool = False):
        self.calls: list = []
        self.content, self.error = content, error

    async def execute_tool(self, call):
        self.calls.append((call.name, call.arguments))
        return ToolResult(tool_call_id="x", content=self.content, is_error=self.error)


@pytest.fixture
def no_cache(monkeypatch):
    from app.services import dashboard_live

    async def _none(*_a, **_k):
        return None

    async def _pass(*_a, **_k):
        return None

    monkeypatch.setattr(dashboard_live, "_cache_get", _none)
    monkeypatch.setattr(dashboard_live, "_cache_set", _pass)
    monkeypatch.setattr(dashboard_live, "_enforce_rate", _pass)


async def test_an_integration_source_draws_its_read_tool_s_answer(monkeypatch, no_cache):
    import uuid

    executor = _Executor('{"issues": [{"key": "SOC-1", "priority": "High"}, {"key": "SOC-2", "priority": "Low"}]}')
    definition = ToolDefinition(name="list_jira_issues", description="", input_schema={"type": "object", "properties": {"jql": {"type": "string"}}})

    async def _tool(_db, _user, _integration, tool):
        assert tool == "list_jira_issues"
        return definition, executor

    monkeypatch.setattr(dashboard_sources, "_read_tool", _tool)
    options = {"integration_id": INTEGRATION, "tool": "list_jira_issues", "arguments": {"jql": "project = SOC"}, "shape": "rows", "path": "issues"}
    read = await dashboard_sources._integration(None, uuid.UUID(USER), options)
    assert read["status"] == "ok" and read["shape"] == "rows"
    assert [row["key"] for row in read["data"]["rows"]] == ["SOC-1", "SOC-2"]
    assert executor.calls == [("list_jira_issues", {"jql": "project = SOC"})]


async def test_an_integration_source_refused_or_failing_answers_a_fixed_code(monkeypatch, no_cache):
    import uuid

    async def _write_tool(*_a):
        raise LiveError("unsupported")

    monkeypatch.setattr(dashboard_sources, "_read_tool", _write_tool)
    options = {"integration_id": INTEGRATION, "tool": "create_jira_issue", "arguments": {}, "shape": "rows"}
    assert await dashboard_sources._integration(None, uuid.UUID(USER), options) == {"status": "error", "code": "unsupported"}

    definition = ToolDefinition(name="list_jira_issues", description="", input_schema={"type": "object"})

    async def _failing(*_a):
        return definition, _Executor("401 Unauthorized", error=True)

    monkeypatch.setattr(dashboard_sources, "_read_tool", _failing)
    options = {**options, "tool": "list_jira_issues"}
    assert await dashboard_sources._integration(None, uuid.UUID(USER), options) == {
        "status": "error",
        "code": "upstream_error",
        "detail": "401 Unauthorized",
    }


async def test_what_a_failing_tool_said_goes_with_the_code(monkeypatch, no_cache):
    """A search with nothing to find answered 404 on Ransomware.live: the agent, given only ``upstream_error``,
    retried it some twenty times. What the tool said tells "nothing found" from "wrong argument"."""
    import uuid

    definition = ToolDefinition(name="search_victims", description="", input_schema={"type": "object"})

    async def _not_found(*_a):
        return definition, _Executor("Error: Victims resource not found while trying to list victims for group x. " + "y" * 400, error=True)

    monkeypatch.setattr(dashboard_sources, "_read_tool", _not_found)
    options = {"integration_id": INTEGRATION, "tool": "search_victims", "arguments": {}, "shape": "rows"}
    answer = await dashboard_sources._integration(None, uuid.UUID(USER), options)
    assert answer["code"] == "upstream_error"
    assert answer["detail"].startswith("Victims resource not found while trying to list victims for group x.")
    assert len(answer["detail"]) == dashboard_sources.DETAIL_CHARS


async def test_a_tool_answering_in_words_reads_as_an_answer_that_does_not_fit(monkeypatch, no_cache):
    """A source's own codes survive: words where JSON was expected is ``invalid_result`` (its remedy: a text
    shape or a feed), never ``upstream_error`` - a platform that failed."""
    import uuid

    definition = ToolDefinition(name="list_recent_victims", description="", input_schema={"type": "object"})

    async def _words(*_a):
        return definition, _Executor("Found 100 recent victims, showing 2:\n- ACME [group: x] | US\n- Initech [group: y] | FR")

    monkeypatch.setattr(dashboard_sources, "_read_tool", _words)
    options = {"integration_id": INTEGRATION, "tool": "list_recent_victims", "arguments": {}, "shape": "rows"}
    assert await dashboard_sources._integration(None, uuid.UUID(USER), options) == {"status": "error", "code": "invalid_result"}
    text = await dashboard_sources._integration(None, uuid.UUID(USER), {**options, "shape": "text"})
    assert text["status"] == "ok" and "ACME" in text["data"]["text"]


def test_a_table_is_drawn_as_a_distribution_by_its_columns():
    from app.services.dashboard_live import closed_static_data

    rows = table_value("severity,count\ncritical,3\nhigh,7")
    assert closed_static_data("distribution", dashboard_sources._as_shape_input("distribution", rows)) == {
        "items": [{"label": "critical", "value": 3.0}, {"label": "high", "value": 7.0}]
    }
    # Its numbers stay numbers in a table.
    table = closed_static_data("rows", dashboard_sources._as_shape_input("rows", rows))
    assert table["columns"][1] == {"key": "count", "label": "count", "kind": "number"} and table["rows"][0]["count"] == 3.0
    # Items written as such are kept; a table with no text and number pair reads nothing.
    assert dashboard_sources._as_shape_input("distribution", [{"label": "a", "value": 1}]) == {"items": [{"label": "a", "value": 1}]}
    with pytest.raises(LiveError) as refused:
        dashboard_sources._as_shape_input("distribution", [{"name": "a"}, {"name": "b"}])
    assert refused.value.code == "invalid_result"


def test_a_whole_number_in_a_text_cell_is_written_without_a_decimal():
    from app.services.dashboard_live import rows_data

    drawn = rows_data([("ctx", "Context"), ("ratio", "Ratio")], [{"ctx": 131072.0, "ratio": 0.5}])
    assert drawn["rows"] == [{"ctx": "131072", "ratio": "0.5"}]
