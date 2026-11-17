"""The relay Prometheus series."""

from __future__ import annotations

import asyncio
import uuid

import httpx
from prometheus_client import REGISTRY

from app.api.v1 import metrics
from app.services import relay_transport, relays

GROUP = uuid.uuid4()


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)


class _FakeDB:
    async def execute(self, _statement):
        return _Result([GROUP])

    async def rollback(self):
        return None


def _sample(name: str, **labels: str) -> float | None:
    return REGISTRY.get_sample_value(name, labels)


def test_the_relay_gauges_are_read_at_scrape_time(monkeypatch):
    async def _counts(db, ids):
        return {GROUP: (1, 2)}

    async def _routed(db, ids):
        return {GROUP: 3}

    monkeypatch.setattr(relays, "relays_active", lambda: True)
    monkeypatch.setattr(relays, "group_relay_counts", _counts)
    monkeypatch.setattr(relays, "routed_resource_counts", _routed)
    asyncio.run(metrics._read_relays(_FakeDB(), "p1"))

    labels = {"group_id": str(GROUP), "platform_id": "p1"}
    assert _sample("xtmone_relay_group_relays", state="online", **labels) == 1
    assert _sample("xtmone_relay_group_relays", state="enrolled", **labels) == 2
    assert _sample("xtmone_relay_group_routed_resources", **labels) == 3


def test_nothing_is_reported_while_relays_are_off(monkeypatch):
    metrics.RELAY_GROUP_RELAYS_GAUGE.labels(group_id="stale", state="online", platform_id="p1").set(1)
    monkeypatch.setattr(relays, "relays_active", lambda: False)
    asyncio.run(metrics._read_relays(_FakeDB(), "p1"))
    assert _sample("xtmone_relay_group_relays", group_id="stale", state="online", platform_id="p1") is None


def test_the_tool_call_a_request_serves_names_the_group_in_its_span():
    from app.services import langfuse_tracing

    tool_span = {"local_span_type": "TOOL", "local_metadata": {"tool_name": "splunk_search"}}
    token = langfuse_tracing.set_current_trace_ctx(tool_span)
    try:
        relay_transport._RequestRecord(
            relay_transport.RelayTransport(group_id=GROUP, group_name="Paris"),
            httpx.Request("GET", "https://splunk.corp.local:8089/"),
        )
    finally:
        langfuse_tracing._current_trace_ctx.reset(token)
    expected = {"egress.relay_group": str(GROUP), "egress.relay_group_name": "Paris"}
    assert tool_span["local_metadata"] == {"tool_name": "splunk_search", **expected}
    assert tool_span["added_metadata"] == expected


def test_a_request_outside_a_tool_call_annotates_nothing():
    from app.services import langfuse_tracing

    iteration_span = {"local_span_type": "SPAN", "local_metadata": {}}
    token = langfuse_tracing.set_current_trace_ctx(iteration_span)
    try:
        relay_transport._RequestRecord(
            relay_transport.RelayTransport(group_id=GROUP, group_name="Paris"),
            httpx.Request("GET", "https://splunk.corp.local:8089/"),
        )
    finally:
        langfuse_tracing._current_trace_ctx.reset(token)
    assert iteration_span == {"local_span_type": "SPAN", "local_metadata": {}}


def test_a_finished_request_is_counted_and_timed_by_group_and_outcome():
    transport = relay_transport.RelayTransport(group_id=GROUP, group_name="Paris")
    group = str(GROUP)
    before = _sample("xtm_one_relay_requests_total", group_id=group, outcome="timeout") or 0
    record = relay_transport._RequestRecord(transport, httpx.Request("GET", "https://splunk.corp.local:8089/"))
    record.entry["bytes_received"] = 42
    record.finish(error="timeout")
    record.finish(error="timeout")

    assert _sample("xtm_one_relay_requests_total", group_id=group, outcome="timeout") == before + 1
    assert _sample("xtm_one_relay_request_duration_seconds_count", group_id=group, outcome="timeout") >= 1
    assert _sample("xtm_one_relay_bytes_total", group_id=group, direction="received") >= 42
