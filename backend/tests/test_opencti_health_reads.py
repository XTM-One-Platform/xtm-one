"""The OpenCTI state reads of the Module Analyst (#101 S14a-2b-3, platform-health.md 12.3):
registered, read-only by name and by query, and answering what the module holds."""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers.opencti import build_tools, health_reads
from app.services.health import module_analyst
from app.services.tool_classification import infer_tool_type
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.opencti.io||test-token"
NAMES = ("list_opencti_connector_states", "list_opencti_feed_states", "list_opencti_playbook_states")
#: The module health reads of S15b-2 (platform-health.md 13.5), all allow-listed.
HEALTH_NAMES = (
    "list_opencti_background_tasks",
    "get_opencti_managers",
    "get_opencti_platform_queues",
    "list_opencti_stream_consumers",
    "get_opencti_licence",
    "get_opencti_data_health",
    "get_opencti_error_rate",
)


class _FakeGraphQL:
    def __init__(self, answers: dict[str, tuple[dict[str, Any], str | None]]) -> None:
        self.answers = answers
        self.queries: list[str] = []

    async def __call__(self, client, base_url, headers, query, variables=None):
        self.queries.append(query)
        for marker, answer in self.answers.items():
            if marker in query:
                return answer
        return {}, None


async def _call(monkeypatch: pytest.MonkeyPatch, fake: Any, name: str) -> str:
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


def test_the_module_health_reads_are_registered_read_only_and_allow_listed() -> None:
    """S15b-2 (section 13.5): every new read is a tool of the provider, classifies
    as a read and is on the checker's allow-list; none takes an argument."""
    executor = CallableToolExecutor()
    defs = {t.name: t for t in build_tools(TOKEN, executor)}
    for name in HEALTH_NAMES:
        assert name in defs and name in executor._handlers, name
        assert infer_tool_type(name) == "read", name
        assert module_analyst.refuse_module_tool("opencti", name) is None, name
        assert defs[name].input_schema["properties"] == {}, name
        assert len(defs[name].description) <= 200, name


def test_no_query_of_the_reads_is_a_mutation():
    queries = [
        health_reads._CONNECTORS_QUERY,
        health_reads._CONNECTORS_QUERY_NO_QUEUE,
        health_reads._PLAYBOOKS_QUERY,
        health_reads._BACKGROUND_TASKS_QUERY,
        health_reads._MANAGERS_QUERY,
        health_reads._QUEUES_QUERY,
        health_reads._STREAMS_QUERY,
        health_reads._STREAMS_QUERY_NO_CONSUMERS,
        health_reads._LICENCE_QUERY,
        health_reads._SERIES_QUERY,
        health_reads._DATA_TOTALS_QUERY,
        health_reads._WORKS_QUERY,
        health_reads._ERROR_RATE_QUERY,
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


async def test_background_tasks_count_the_running_and_the_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    nodes = [
        {
            "id": "b1",
            "type": "QUERY",
            "completed": False,
            "task_expected_number": 100,
            "task_processed_number": 40,
            "errors": [{"timestamp": "2026-11-21T08:00:00Z", "message": "Element not found"}],
        },
        {"id": "b2", "type": "RULE", "completed": True, "errors": []},
    ]
    fake = _FakeGraphQL({"HealthBackgroundTasks": ({"backgroundTasks": {"edges": [{"node": n} for n in nodes]}}, None)})
    out = await _call(monkeypatch, fake, "list_opencti_background_tasks")
    assert "newest 2; 1 in progress, 1 with errors" in out
    assert "QUERY [in progress], 40 of 100 processed" in out
    assert "Element not found" in out


class _SequenceGraphQL:
    """Answers the managers query with one module list per read, the last one repeated."""

    def __init__(self, reads: list[list[dict[str, Any]]]) -> None:
        self.reads = reads
        self.calls = 0

    async def __call__(self, client, base_url, headers, query, variables=None):
        modules = self.reads[min(self.calls, len(self.reads) - 1)]
        self.calls += 1
        return {"settings": {"platform_modules": modules}}, None


def _healthy_modules() -> list[dict[str, Any]]:
    """Every known manager enabled: stream managers running, cron managers idle between cycles."""
    return [
        {"id": mid, "enable": True, "running": kind == "stream", "warning": False}
        for mid, kind in health_reads.MANAGER_KINDS.items()
    ]


@pytest.fixture
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(health_reads.asyncio, "sleep", _sleep)
    return slept


def test_every_manager_is_classed_and_an_unknown_id_is_unknown() -> None:
    kinds = health_reads.MANAGER_KINDS
    assert set(kinds.values()) == {"stream", "cron"}
    assert {m for m, k in kinds.items() if k == "stream"} == {
        "RULE_ENGINE",
        "HISTORY_MANAGER",
        "SYNC_MANAGER",
        "PUBLISHER_MANAGER",
        "NOTIFICATION_MANAGER",
        "ACTIVITY_MANAGER",
        "PLAYBOOK_MANAGER",
        "TELEMETRY_MANAGER",
    }
    for cron in ("TASK_MANAGER", "RETENTION_MANAGER", "EXPIRATION_SCHEDULER", "INGESTION_MANAGER"):
        assert health_reads.manager_kind(cron) == "cron"
    assert health_reads.manager_kind("SOME_FUTURE_MANAGER") == "unknown"


async def test_idle_cron_managers_are_never_counted_down(
    monkeypatch: pytest.MonkeyPatch, _no_sleep: list[float]
) -> None:
    """#1640: all managers enabled, stream running, cron idle: nothing is down, one read only."""
    fake = _SequenceGraphQL([_healthy_modules()])
    out = await _call(monkeypatch, fake, "get_opencti_managers")
    assert "Enabled stream managers not running on all 3 reads: none." in out
    assert "enabled and not running" not in out
    assert "intermittent for cron managers" in out
    assert "- TASK_MANAGER (cron) [enabled, not running]" in out
    assert "- RULE_ENGINE (stream) [enabled, running]" in out
    assert fake.calls == 1 and _no_sleep == []


async def test_a_stream_manager_down_on_three_reads_is_named(
    monkeypatch: pytest.MonkeyPatch, _no_sleep: list[float]
) -> None:
    down = [{**m, "running": False} if m["id"] == "RULE_ENGINE" else m for m in _healthy_modules()]
    fake = _SequenceGraphQL([down, down, down])
    out = await _call(monkeypatch, fake, "get_opencti_managers")
    assert "Enabled stream managers not running on all 3 reads, 60 s apart: RULE_ENGINE." in out
    assert fake.calls == 3 and _no_sleep == [60, 60]


async def test_a_stream_manager_down_on_one_read_recovers_with_a_note(
    monkeypatch: pytest.MonkeyPatch, _no_sleep: list[float]
) -> None:
    down = [{**m, "running": False} if m["id"] == "HISTORY_MANAGER" else m for m in _healthy_modules()]
    fake = _SequenceGraphQL([down, _healthy_modules()])
    out = await _call(monkeypatch, fake, "get_opencti_managers")
    assert "Enabled stream managers not running on all 3 reads: none." in out
    assert "Not running on the first read, running again within 2 reads: HISTORY_MANAGER." in out
    assert fake.calls == 2


async def test_a_disabled_or_unknown_manager_is_never_judged(
    monkeypatch: pytest.MonkeyPatch, _no_sleep: list[float]
) -> None:
    modules = [
        {"id": "SYNC_MANAGER", "enable": False, "running": False},
        {"id": "SOME_FUTURE_MANAGER", "enable": True, "running": False, "warning": True},
    ]
    fake = _SequenceGraphQL([modules])
    out = await _call(monkeypatch, fake, "get_opencti_managers")
    assert "Enabled stream managers not running on all 3 reads: none." in out
    assert "- SYNC_MANAGER (stream) [disabled, not running]" in out
    assert "- SOME_FUTURE_MANAGER (unknown) [enabled, not running, warning]" in out
    assert fake.calls == 1


async def test_platform_queues_answer_totals_and_never_a_queue_name(monkeypatch: pytest.MonkeyPatch) -> None:
    metrics = {
        "consumers": "4",
        "overview": {"queue_totals": {"messages": "1200", "messages_ready": "1000", "messages_unacknowledged": "200"}},
    }
    fake = _FakeGraphQL({"HealthPlatformQueues": ({"rabbitMQMetrics": metrics}, None)})
    out = await _call(monkeypatch, fake, "get_opencti_platform_queues")
    assert "consumers 4, messages 1200, ready 1000, unacknowledged 200" in out
    assert "queues {" not in health_reads._QUEUES_QUERY.replace("queue_totals {", "")


async def test_stream_consumers_show_the_lag_and_fall_back_without_consumers(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake(
        client: Any, base_url: str, headers: Any, query: str, variables: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], str | None]:
        calls.append(query)
        if "consumers" in query:
            return {}, "Cannot query field consumers on type StreamCollection"
        node = {"id": "s1", "name": "Live", "stream_live": True}
        return {"streamCollections": {"edges": [{"node": node}]}}, None

    out = await _call(monkeypatch, fake, "list_opencti_stream_consumers")
    assert len(calls) == 2 and "Live [live]" in out and "consumers" not in out.split("Live")[1]

    node = {
        "id": "s1",
        "name": "Live",
        "stream_live": True,
        "consumers": [{"connectionId": "k1", "timeLag": 7200.5, "lastEventDate": "2026-11-21T08:00:00Z"}],
    }
    fake2 = _FakeGraphQL({"HealthStreams": ({"streamCollections": {"edges": [{"node": node}]}}, None)})
    out = await _call(monkeypatch, fake2, "list_opencti_stream_consumers")
    assert "consumers 1" in out and "consumer k1: time lag 7200.5" in out


async def test_licence_answers_expiry_or_its_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    ee = {
        "license_type": "standard",
        "license_validated": True,
        "license_expired": False,
        "license_expiration_date": "2026-12-01T00:00:00Z",
    }
    fake = _FakeGraphQL({"HealthLicence": ({"settings": {"platform_enterprise_edition": ee}}, None)})
    out = await _call(monkeypatch, fake, "get_opencti_licence")
    assert "validated True, expired False, expires 2026-12-01T00:00:00Z" in out

    refused = _FakeGraphQL({"HealthLicence": ({}, "Forbidden access")})
    assert await _call(monkeypatch, refused, "get_opencti_licence") == "Licence: Forbidden access"


async def test_data_health_counts_per_day_totals_and_works_in_error(monkeypatch: pytest.MonkeyPatch) -> None:
    series = [{"date": "2026-11-20T00:00:00.000Z", "value": 120}, {"date": "2026-11-21T00:00:00.000Z", "value": 0}]
    totals = {
        "stixCoreObjectsNumber": {"total": 5400, "count": 5400},
        "stixCoreObjects": {"edges": [{"node": {"created_at": "2026-11-20T09:00:00Z"}}]},
    }
    works = [
        {"status": "complete", "errors": [{"timestamp": "2026-11-21T01:00:00Z"}]},
        {"status": "timeout", "errors": []},
        {"status": "complete", "errors": []},
    ]
    fake = _FakeGraphQL(
        {
            "HealthDataSeries": ({"stixCoreObjectsTimeSeries": series}, None),
            "HealthDataTotals": (totals, None),
            "HealthWorks": ({"works": {"edges": [{"node": w} for w in works]}}, None),
        }
    )
    out = await _call(monkeypatch, fake, "get_opencti_data_health")
    assert "Objects created per day (120 in 2 days): 2026-11-20 120, 2026-11-21 0" in out
    assert "Objects updated per day (120 in 2 days)" in out
    assert "Objects in total: 5400, newest created 2026-11-20T09:00:00Z" in out
    assert "Works (newest 3, platform-wide): 1 with errors, 1 timed out" in out
    assert len(fake.queries) == 4
    # Platform-wide: the works read never asks for the connector of a work.
    assert "connector" not in health_reads._WORKS_QUERY


async def test_data_health_keeps_going_past_a_refused_read(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGraphQL({"HealthWorks": ({}, "Forbidden access")})
    out = await _call(monkeypatch, fake, "get_opencti_data_health")
    assert "Works: Forbidden access" in out
    assert "Objects created per day (0 in 0 days): none" in out


async def test_error_rate_reads_activity_errors_per_day(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any] | None] = []

    async def fake(
        client: Any, base_url: str, headers: Any, query: str, variables: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], str | None]:
        seen.append(variables)
        return {"auditsTimeSeries": [{"date": "2026-11-21T00:00:00.000Z", "value": 7}]}, None

    out = await _call(monkeypatch, fake, "get_opencti_error_rate")
    assert out == "OpenCTI activity entries in error per day (7 in 1 days): 2026-11-21 7"
    assert seen[0] is not None and seen[0]["filters"] == health_reads._ERROR_FILTER
    assert set(seen[0]) == {"startDate", "endDate", "filters"}

    refused = _FakeGraphQL({"HealthErrorRate": ({}, "Forbidden access")})
    assert await _call(monkeypatch, refused, "get_opencti_error_rate") == "Error rate: Forbidden access"
