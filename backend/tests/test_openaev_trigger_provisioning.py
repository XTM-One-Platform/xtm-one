"""OpenAEV trigger provisioning (#8991, CTEM S2c) and the OpenAEV receiver path."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.api.v1 import webhooks
from app.services import openaev_trigger_provisioning as prov
from app.services import trigger_provisioning

NOW = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
URL = "https://xtm.example/api/hooks/integrations/i1/assignments/a1"
SPEC = {"fleet": "Exposure validation", "purpose": "Results", "events": ["finding_created", "simulation_finished"]}


class FakeOpenAEV:
    """An in-memory /api/notifiers and /api/notification-triggers."""

    def __init__(self) -> None:
        self.notifiers: dict[str, dict] = {}
        self.triggers: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return f"id-{self._n}"

    async def __call__(self, method: str, path: str, body):
        self.calls.append((method, path))
        store, key = (
            (self.triggers, "notification_trigger_id")
            if path.startswith("/api/notification-triggers")
            else (self.notifiers, "notifier_id")
        )
        parts = path.rstrip("/").split("/")
        oid = parts[3] if len(parts) > 3 else None
        if method == "post":
            new_id = self._id()
            store[new_id] = {**body, key: new_id}
            return store[new_id]
        if method == "get":
            return store.get(oid)
        if method == "put":
            store[oid] = {**body, key: oid}
            return store[oid]
        if method == "delete":
            store.pop(oid, None)
            return {}
        raise AssertionError(method)


def test_the_provider_is_registered():
    assert trigger_provisioning._PROVISIONERS["openaev"] == "app.services.openaev_trigger_provisioning"


@pytest.mark.parametrize(
    "raw, error",
    [
        ("x", "object"),
        ({"fleet": "F", "events": ["finding_created"]}, "required"),
        ({"fleet": "F", "purpose": "P", "events": []}, "one or more"),
        ({"fleet": "F", "purpose": "P", "events": ["expectation_result"]}, "unknown"),
    ],
    ids=["not-object", "no-purpose", "no-events", "unknown-event"],
)
def test_a_bad_provision_block_is_refused(raw, error):
    with pytest.raises(prov.ProvisionSpecError, match=error):
        prov.read_provision_spec({"provision": raw})


def test_the_spec_is_normalised():
    spec = prov.read_provision_spec({"provision": {"fleet": " F ", "purpose": "P", "events": "finding_created"}})
    assert spec == {"fleet": "F", "purpose": "P", "events": ["finding_created"]}
    assert prov.read_provision_spec({"provider": "openaev"}) is None


def test_each_event_maps_to_its_openaev_trigger():
    body = prov.trigger_body(SPEC, "simulation_finished", "n1")
    assert body["notification_trigger_resource_type"] == "SIMULATION"
    assert body["notification_trigger_event_types"] == ["UPDATE"]
    assert body["notification_trigger_filters"]["filters"][0] == {
        "key": "exercise_status", "operator": "eq", "mode": "or", "values": ["FINISHED"],
    }
    inject = prov.trigger_body(SPEC, "atomic_test_finished", "n1")
    assert inject["notification_trigger_resource_type"] == "INJECT"
    assert inject["notification_trigger_filters"]["filters"][0]["values"] == ["EXECUTED", "PARTIAL", "ERROR"]
    finding = prov.trigger_body(SPEC, "finding_created", "n1")
    assert finding["notification_trigger_event_types"] == ["CREATE"]
    assert "notification_trigger_filters" not in finding
    assert finding["notification_trigger_name"] == "XTM One - Exposure validation - Results - Trigger - finding_created"


def test_the_notifier_carries_the_secret_as_a_static_header():
    body = prov.notifier_body("XTM One - F - P", URL, "s3cret")
    assert body["notifier_type"] == "WEBHOOK"
    assert body["notifier_configuration"] == {"url": URL, "verb": "POST", "headers": {"X-OpenAEV-Token": "s3cret"}}


async def test_reconcile_creates_then_is_idempotent():
    api = FakeOpenAEV()
    state = await prov.reconcile(api, SPEC, None, url=URL, secret="s", now=NOW)
    assert state["status"] == "provisioned"
    assert set(state["trigger_ids"]) == {"finding_created", "simulation_finished"}
    assert len(api.notifiers) == 1 and len(api.triggers) == 2
    api.calls.clear()
    again = await prov.reconcile(api, SPEC, state, url=URL, secret="s", now=NOW)
    assert again["trigger_ids"] == state["trigger_ids"]
    assert all(method == "get" for method, _ in api.calls)


async def test_reconcile_repairs_a_deleted_trigger_and_an_edited_notifier():
    api = FakeOpenAEV()
    state = await prov.reconcile(api, SPEC, None, url=URL, secret="s", now=NOW)
    api.triggers.pop(state["trigger_ids"]["finding_created"])
    api.notifiers[state["notifier_id"]]["notifier_configuration"] = {"url": "https://elsewhere"}
    repaired = await prov.reconcile(api, SPEC, state, url=URL, secret="s", now=NOW)
    assert repaired["trigger_ids"]["finding_created"] != state["trigger_ids"]["finding_created"]
    assert api.notifiers[state["notifier_id"]]["notifier_configuration"]["url"] == URL
    assert ("put", f"/api/notifiers/{state['notifier_id']}") in api.calls


async def test_an_event_dropped_from_the_spec_loses_its_trigger():
    api = FakeOpenAEV()
    state = await prov.reconcile(api, SPEC, None, url=URL, secret="s", now=NOW)
    narrowed = {**SPEC, "events": ["finding_created"]}
    after = await prov.reconcile(api, narrowed, state, url=URL, secret="s", now=NOW)
    assert set(after["trigger_ids"]) == {"finding_created"}
    assert len(api.triggers) == 1


async def test_an_api_error_is_recorded_not_raised():
    async def broken(method, path, body):
        raise prov.OpenAEVApiError("403 Forbidden")

    state = await prov.reconcile(broken, SPEC, {"notifier_id": None}, url=URL, secret="s", now=NOW)
    assert state["status"] == "error" and "403" in state["error"]


async def test_an_optional_event_an_older_openaev_refuses_keeps_the_other_triggers():
    # CTEM-13: an OpenAEV that predates CRITICAL_ASSET_REACHED refuses that trigger only.
    inner = FakeOpenAEV()

    async def older(method, path, body):
        if method == "post" and (body or {}).get("notification_trigger_event_types") == ["CRITICAL_ASSET_REACHED"]:
            raise prov.OpenAEVApiError("400 Bad Request")
        return await inner(method, path, body)

    spec = {**SPEC, "events": ["critical_asset_reached", "finding_created", "simulation_finished"]}
    state = await prov.reconcile(older, spec, None, url=URL, secret="s", now=NOW)
    assert state["status"] == "provisioned"
    assert set(state["trigger_ids"]) == {"finding_created", "simulation_finished"}
    assert state["unsupported_events"] == ["critical_asset_reached"]
    # A newer OpenAEV takes it at the next check.
    again = await prov.reconcile(inner, spec, state, url=URL, secret="s", now=NOW)
    assert set(again["trigger_ids"]) == set(spec["events"]) and "unsupported_events" not in again


async def test_a_required_event_refused_is_still_an_error():
    async def refuses_findings(method, path, body):
        if method == "post" and path == "/api/notification-triggers":
            raise prov.OpenAEVApiError("400 Bad Request")
        return {"notifier_id": "n1"}

    state = await prov.reconcile(refuses_findings, SPEC, None, url=URL, secret="s", now=NOW)
    assert state["status"] == "error"


def test_the_critical_asset_event_maps_to_its_openaev_trigger():
    body = prov.trigger_body(SPEC, "critical_asset_reached", "n1")
    assert body["notification_trigger_resource_type"] == "SIMULATION"
    assert body["notification_trigger_event_types"] == ["CRITICAL_ASSET_REACHED"]
    assert "notification_trigger_filters" not in body
    assert "critical_asset_reached" in prov.OPTIONAL_EVENTS


async def test_remove_deletes_triggers_then_notifier():
    api = FakeOpenAEV()
    state = await prov.reconcile(api, SPEC, None, url=URL, secret="s", now=NOW)
    assert await prov.remove(api, state) is None
    assert not api.notifiers and not api.triggers


async def test_objects_follow_the_naming_rule_and_are_renamed_in_place() -> None:
    # #2583: "XTM One - <Fleet> - <Path> - <Kind>", a description naming who it serves, ids kept on rename.
    api = FakeOpenAEV()
    spec = {**SPEC, "origin": {"assignment": "Results", "assignment_id": "a1"}}
    state = await prov.reconcile(api, spec, None, url=URL, secret="s", now=NOW)
    notifier = api.notifiers[state["notifier_id"]]
    assert notifier["notifier_name"] == "XTM One - Exposure validation - Results - Notifier"
    assert "Fleet: Exposure validation. Path: Results. Kind: Notifier." in notifier["notifier_description"]
    assert "Assignment: Results (a1)." in notifier["notifier_description"]
    names = {t["notification_trigger_name"] for t in api.triggers.values()}
    assert names == {
        "XTM One - Exposure validation - Results - Trigger - finding_created",
        "XTM One - Exposure validation - Results - Trigger - simulation_finished",
    }
    # An object of the former rule is renamed by the next check, its id kept.
    api.notifiers[state["notifier_id"]]["notifier_name"] = "XTM One - Exposure validation - Results"
    tid = state["trigger_ids"]["finding_created"]
    api.triggers[tid]["notification_trigger_name"] = "XTM One - Exposure validation - Results - finding_created"
    again = await prov.reconcile(api, spec, state, url=URL, secret="s", now=NOW)
    assert again["notifier_id"] == state["notifier_id"] and again["trigger_ids"] == state["trigger_ids"]
    assert api.notifiers[state["notifier_id"]]["notifier_name"].endswith(" - Notifier")
    assert api.triggers[tid]["notification_trigger_name"].endswith(" - Trigger - finding_created")


def test_due_for_check():
    assert prov.due_for_check(None, NOW)
    fresh = {"status": "provisioned", "checked_at": NOW.isoformat()}
    assert not prov.due_for_check(fresh, NOW + timedelta(seconds=60))
    assert prov.due_for_check(fresh, NOW + timedelta(seconds=prov.RECHECK_SECONDS))


@pytest.mark.parametrize(
    "name, event",
    [
        ("XTM One - F - P - finding_created", "finding_created"),
        ("XTM One - F - P - Trigger - finding_created", "finding_created"),
        ("XTM One - F - P - nope", None),
        ("My own trigger - finding_created", None),
        (None, None),
    ],
    ids=["provisioned", "named-2583", "unknown-suffix", "foreign", "absent"],
)
def test_the_event_is_read_from_the_trigger_name(name, event):
    assert prov.event_from_notification_name(name) == event


def test_the_receiver_normalises_a_default_openaev_body():
    payload = {
        "notification_name": "XTM One - F - P - simulation_finished",
        "notification_type": "live",
        "platform_url": "https://oaev.example/",
        "content": [{"title": "t", "events": [{
            "operation": "UPDATE", "message": "Simulation X updated",
            "resource_type": "SIMULATION", "resource_id": "sim-1",
        }]}],
    }
    assert webhooks._normalize_openaev_payload(payload) == "simulation_finished"
    meta = payload["_openaev_event"]
    assert meta["event_type"] == "simulation_finished"
    assert meta["resource_type"] == "SIMULATION" and meta["resource_id"] == "sim-1"
    assert meta["platform_url"] == "https://oaev.example"


def test_a_foreign_openaev_delivery_keeps_its_operation():
    payload = {"notification_name": "Mine", "content": [{"events": [{"operation": "CREATE"}]}]}
    assert webhooks._normalize_openaev_payload(payload) is None
    assert payload["_openaev_event"]["event_type"] == "create"


def test_the_openaev_token_is_never_persisted():
    assert "x-openaev-token" in webhooks._SENSITIVE_WEBHOOK_HEADERS


def test_the_catalog_lists_the_openaev_events():
    from app.api.v1.integration_catalog import PROVIDER_CATALOG

    entry = PROVIDER_CATALOG["openaev"]
    assert entry["supports_assignment_webhooks"] is True
    assert {e["event"] for e in entry["available_events"]} == set(prov.EVENTS)
    assert "webhook_secret" in entry["extra_fields"]
