"""Defender XDR alert events read from the tenant's Event Hub (Streaming API)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.services import defender_stream as ds
from app.services.integration_event_filters import event_filter_reason


def _alert_record(alert_id: str = "da637", severity: str = "High", category: str = "Malware") -> dict:
    return {
        "time": "2026-11-25T18:00:00Z",
        "tenantId": "t-1",
        "category": "AdvancedHunting-AlertInfo",
        "properties": {
            "Timestamp": "2026-11-25T17:59:58Z",
            "AlertId": alert_id,
            "Title": "Suspicious PowerShell",
            "Category": category,
            "Severity": severity,
            "ServiceSource": "Microsoft Defender for Endpoint",
            "DetectionSource": "EDR",
            "AttackTechniques": '["T1059.001"]',
        },
    }


def _evidence_record(alert_id: str = "da637", sha: str = "abc") -> dict:
    return {
        "category": "AdvancedHunting-AlertEvidence",
        "properties": {"AlertId": alert_id, "EntityType": "File", "EvidenceRole": "Impacted", "SHA256": sha},
    }


def test_alert_record_maps_to_alert_created():
    payload = ds.record_payload(_alert_record())
    assert payload["event"] == "alert_created"
    assert payload["alert_id"] == "da637"
    assert payload["severity"] == "High"
    assert payload["attack_techniques"] == ["T1059.001"]
    assert payload["message_id"] == "defender:alert:da637"


def test_evidence_rows_get_distinct_ids_and_the_same_row_the_same_id():
    a = ds.record_payload(_evidence_record(sha="a"))
    b = ds.record_payload(_evidence_record(sha="b"))
    again = ds.record_payload(_evidence_record(sha="a"))
    assert a["event"] == "alert_evidence"
    assert a["entity"]["SHA256"] == "a"
    assert a["message_id"] != b["message_id"]
    assert a["message_id"] == again["message_id"]


def test_other_tables_and_bad_bodies_are_ignored():
    assert ds.record_payload({"category": "AdvancedHunting-DeviceEvents", "properties": {"AlertId": "x"}}) is None
    assert ds.record_payload({"category": "AdvancedHunting-AlertInfo", "properties": {}}) is None
    assert ds.records_of("not json") == []
    assert ds.records_of(json.dumps({"records": [_alert_record(), 3]})) == [_alert_record()]


def test_filter_keys():
    payload = ds.record_payload(_alert_record(severity="Low"))
    assert ds.filter_reason(payload, {"severity": ["high", "medium"]}) == "severity is not in severity"
    assert ds.filter_reason(payload, {"severity": "low", "category": ["malware"]}) is None
    assert event_filter_reason("alert_created", payload, {"service_source": ["Microsoft Defender for Office 365"]})


def test_catalog_lists_the_stream_events_and_filter_keys():
    events = {e["event"]: e for e in PROVIDER_CATALOG["defender"]["available_events"]}
    assert set(events) == {"alert_created", "alert_evidence"}
    for entry in events.values():
        assert entry["mechanism"] == "stream"
        assert set(entry["filter_keys"]) == {"severity", "category", "service_source"}
    assert "event_hub_namespace" in PROVIDER_CATALOG["defender"]["extra_fields"]


def test_stream_settings_name_what_is_missing():
    with pytest.raises(ds.StreamConfigError, match="event_hub_namespace"):
        ds.stream_settings({"tenant_id": "t", "event_hub_name": "h"}, "cid", "secret")
    settings = ds.stream_settings(
        {"tenant_id": "t", "event_hub_name": "h", "event_hub_namespace": "sb://ns/"}, "cid", "secret"
    )
    assert settings["namespace"] == "ns.servicebus.windows.net"
    assert settings["consumer_group"] == "$Default"
    cs = ds.stream_settings({"event_hub_connection_string": "Endpoint=sb://x/;EntityPath=h"}, "", "")
    assert cs["connection_string"].startswith("Endpoint=")
    with pytest.raises(ds.StreamConfigError, match="EntityPath"):
        ds.stream_settings({"event_hub_connection_string": "Endpoint=sb://x/"}, "", "")


class _FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    async def eval(self, script, numkeys, key, token, *args):
        if self.data.get(key) != token:
            return 0
        if "del" in script:
            self.data.pop(key)
        return 1


class _FakeEvent:
    def __init__(self, body: dict, seq: int):
        self._body = json.dumps(body)
        self.sequence_number = seq

    def body_as_str(self, encoding="UTF-8"):
        return self._body


class _FakeClient:
    def __init__(self, batches):
        self.batches = batches
        self.starting = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_partition_ids(self):
        return ["0", "1"]

    async def receive_batch(self, on_event_batch, starting_position, **kwargs):
        self.starting = starting_position
        for pid, events in self.batches:
            await on_event_batch(SimpleNamespace(partition_id=pid), events)


def test_consume_dispatches_then_checkpoints_and_resumes_after_it(monkeypatch):
    redis = _FakeRedis()
    redis.data[ds.checkpoint_key("i-1", "1")] = "41"
    seen: list[list] = []

    async def fake_dispatch(integration_id, records):
        seen.append(list(records))
        return []

    monkeypatch.setattr(ds, "dispatch_records", fake_dispatch)
    client = _FakeClient([("0", [_FakeEvent({"records": [_alert_record()]}, 7)]), ("1", [])])
    asyncio.run(ds.consume("i-1", {}, redis, client_factory=lambda _s: client))
    assert client.starting == {"0": "@latest", "1": 41}
    assert seen == [[_alert_record()]]
    assert redis.data[ds.checkpoint_key("i-1", "0")] == "7"


def test_a_failed_dispatch_does_not_move_the_checkpoint(monkeypatch):
    redis = _FakeRedis()

    async def failing(integration_id, records):
        raise RuntimeError("db down")

    monkeypatch.setattr(ds, "dispatch_records", failing)
    client = _FakeClient([("0", [_FakeEvent({"records": [_alert_record()]}, 9)])])
    with pytest.raises(RuntimeError):
        asyncio.run(ds.consume("i-1", {}, redis, client_factory=lambda _s: client))
    assert ds.checkpoint_key("i-1", "0") not in redis.data


def test_lease_is_held_by_one_reader():
    redis = _FakeRedis()

    async def scenario():
        assert await ds.take_lease(redis, "i-1", "a")
        assert not await ds.take_lease(redis, "i-1", "b")
        assert await ds.renew_lease(redis, "i-1", "a")
        assert not await ds.renew_lease(redis, "i-1", "b")
        await ds.release_lease(redis, "i-1", "b")
        assert redis.data[ds.lease_key("i-1")] == "a"
        await ds.release_lease(redis, "i-1", "a")
        assert ds.lease_key("i-1") not in redis.data

    asyncio.run(scenario())


def test_a_relay_routed_integration_gets_no_reader():
    redis = _FakeRedis()
    integration = SimpleNamespace(id="i-1", relay_group_id="g-1")
    asyncio.run(ds._run_leased(integration, redis))
    assert ds.lease_key("i-1") not in redis.data
