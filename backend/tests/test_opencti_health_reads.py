"""The OpenCTI state reads of the Module Analyst (#101 S14a-2b-3, platform-health.md 12.3):
registered, read-only by name and by query, and answering what the module holds."""

from __future__ import annotations

import pytest

from app.integrations.providers.opencti import build_tools, health_reads
from app.services.health import module_analyst
from app.services.tool_classification import infer_tool_type
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.opencti.io||test-token"
NAMES = ("list_opencti_connector_states", "list_opencti_feed_states", "list_opencti_playbook_states")


class _FakeGraphQL:
    def __init__(self, answers):
        self.answers = answers
        self.queries: list[str] = []

    async def __call__(self, client, base_url, headers, query, variables=None):
        self.queries.append(query)
        for marker, answer in self.answers.items():
            if marker in query:
                return answer
        return {}, None


async def _call(monkeypatch, fake, name):
    monkeypatch.setattr(health_reads, "_graphql", fake)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    result = await executor.execute_tool(ToolCall(id="c", name=name, arguments={}))
    assert not result.is_error, result.content
    return result.content


def test_the_reads_are_registered_and_only_the_playbook_read_is_allow_listed() -> None:
    """S15b-1 (section 13.5): the connector and feed reads stay tools of the provider
    for other agents, but a module checker no longer reads them (13.4 scope)."""
    executor = CallableToolExecutor()
    names = {t.name for t in build_tools(TOKEN, executor)}
    assert set(NAMES) <= names
    for name in NAMES:
        assert infer_tool_type(name) == "read", name
        assert name in executor._handlers
    allowed = {name for name in NAMES if module_analyst.refuse_module_tool("opencti", name) is None}
    assert allowed == {"list_opencti_playbook_states"}


def test_no_query_of_the_reads_is_a_mutation():
    queries = [
        health_reads._CONNECTORS_QUERY,
        health_reads._CONNECTORS_QUERY_NO_QUEUE,
        health_reads._PLAYBOOKS_QUERY,
        *(health_reads._feed_query(root) for root in health_reads._FEED_ROOTS.values()),
    ]
    assert all(q.startswith("query ") and "mutation" not in q for q in queries)


async def test_connector_states_name_the_inactive_one_and_its_queue(monkeypatch):
    fake = _FakeGraphQL(
        {
            "HealthConnectors": (
                {
                    "connectors": [
                        {
                            "id": "c1",
                            "name": "MITRE",
                            "active": False,
                            "connector_type": "EXTERNAL_IMPORT",
                            "updated_at": "2026-11-20T10:00:00Z",
                            "connector_queue_details": {"messages_number": 42},
                        },
                    ]
                },
                None,
            )
        }
    )
    out = await _call(monkeypatch, fake, "list_opencti_connector_states")
    assert "MITRE [inactive]" in out and "queue 42" in out


async def test_connector_states_fall_back_without_the_queue_field(monkeypatch):
    calls = []

    async def fake(client, base_url, headers, query, variables=None):
        calls.append(query)
        if "connector_queue_details" in query:
            return {}, "Cannot query field connector_queue_details on type Connector"
        return {"connectors": [{"id": "c1", "name": "MITRE", "active": True}]}, None

    out = await _call(monkeypatch, fake, "list_opencti_connector_states")
    assert len(calls) == 2 and "MITRE [active]" in out


async def test_feed_states_keep_going_past_a_refused_root(monkeypatch):
    fake = _FakeGraphQL(
        {
            "ingestionRsss": ({}, "Forbidden access"),
            "ingestionTaxiis": (
                {
                    "ingestionTaxiis": {
                        "edges": [
                            {
                                "node": {
                                    "id": "t1",
                                    "name": "Feed",
                                    "ingestion_running": True,
                                    "last_execution_date": "2026-11-21T08:00:00Z",
                                }
                            }
                        ]
                    }
                },
                None,
            ),
        }
    )
    out = await _call(monkeypatch, fake, "list_opencti_feed_states")
    assert "rss feeds: Forbidden access" in out
    assert "taxii Feed [running]" in out
    assert len(fake.queries) == len(health_reads._FEED_ROOTS)


@pytest.mark.parametrize("running,label", [(True, "running"), (False, "stopped")])
async def test_playbook_states(monkeypatch, running, label):
    fake = _FakeGraphQL(
        {
            "HealthPlaybooks": (
                {
                    "playbooks": {
                        "edges": [
                            {"node": {"id": "p1", "name": "Enrich", "playbook_running": running, "queue_messages": 3}}
                        ]
                    }
                },
                None,
            )
        }
    )
    out = await _call(monkeypatch, fake, "list_opencti_playbook_states")
    assert f"Enrich [{label}], queue 3" in out
