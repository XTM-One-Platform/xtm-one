"""manage_opencti_ingestion names every field the action does not apply as ignored; the action runs.

A partner listed RSS feeds with ``uri`` set and got the whole list back as if it
had been filtered on that URL. Each action now names the fields it ignores,
undeclared arguments included, and writes nothing.
"""

import pytest

import app.integrations.providers.opencti.ingestion as ingestion_mod
import app.integrations.providers.opencti.knowledge as knowledge_mod
import app.integrations.providers.opencti.markings as markings_mod
from app.integrations.providers.opencti import build_tools
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.opencti.io||test-token"


class _Stub:
    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, client, base_url, headers, query, variables=None):
        self.calls.append(query)
        if "ingestionRsss(" in query:
            return {"ingestionRsss": {"edges": [{"node": {"id": "f1", "name": "A", "ingestion_running": True}}]}}, None
        if "ingestionRssFieldPatch" in query:
            return {"ingestionRssFieldPatch": {"id": "f1", "name": "A", "ingestion_running": False}}, None
        if "ingestionRssDelete" in query:
            return {"ingestionRssDelete": "f1"}, None
        return {}, None


def _build(monkeypatch):
    stub = _Stub()
    for module in (ingestion_mod, markings_mod, knowledge_mod):
        monkeypatch.setattr(module, "_graphql", stub)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor, scope_headers={})
    return executor, stub


async def _run(executor, arguments):
    return await executor.execute_tool(
        ToolCall(id="call-ingestion", name="manage_opencti_ingestion", arguments=arguments)
    )


@pytest.mark.parametrize(
    ("arguments", "named"),
    [
        ({"action": "list", "feed_type": "rss", "uri": "https://x.test/feed"}, "uri"),
        ({"action": "list", "feed_type": "rss", "feed_id": "f1"}, "feed_id"),
        ({"action": "get", "feed_type": "rss", "feed_id": "f1", "name": "A"}, "name"),
        ({"action": "delete", "feed_type": "rss", "feed_id": "f1", "config": '{"a": 1}'}, "config"),
        ({"action": "toggle", "feed_type": "rss", "feed_id": "f1", "running": False, "name": "A"}, "name"),
        ({"action": "update", "feed_type": "rss", "feed_id": "f1", "name": "B", "user_id": "u1"}, "user_id"),
        ({"action": "update", "feed_type": "rss", "feed_id": "f1", "name": "B", "running": False}, "running"),
        ({"action": "list", "feed_type": "rss", "label_import": True}, "label_import"),
    ],
)
async def test_unapplied_field_is_named_as_ignored_and_the_action_runs(monkeypatch, arguments, named):
    executor, _stub = _build(monkeypatch)
    result = await _run(executor, arguments)
    assert f"action '{arguments['action']}' does not apply" in result.content, result.content
    assert named in result.content
    assert "ignored and the rest ran" in result.content


async def test_list_with_its_own_fields_runs(monkeypatch):
    executor, stub = _build(monkeypatch)
    result = await _run(executor, {"action": "list", "feed_type": "rss", "search": "A", "limit": 10})
    assert "does not apply" not in result.content
    assert "rss feeds (1)" in result.content
    assert stub.calls


async def test_defaults_sent_explicitly_are_not_refused(monkeypatch):
    executor, _ = _build(monkeypatch)
    result = await _run(
        executor,
        {
            "action": "list",
            "feed_type": "rss",
            "limit": 50,
            "running": True,
            "authentication_type": "none",
            "uri": "",
        },
    )
    assert "does not apply" not in result.content, result.content


async def test_toggle_with_running_false_runs(monkeypatch):
    executor, _ = _build(monkeypatch)
    result = await _run(executor, {"action": "toggle", "feed_type": "rss", "feed_id": "f1", "running": False})
    assert "does not apply" not in result.content, result.content
    assert "is now stopped" in result.content
