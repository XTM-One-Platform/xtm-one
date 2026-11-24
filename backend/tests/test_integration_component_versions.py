"""Component upgrades of a connected platform raise ``integration.version.changed``.

The autonomous tick reads each registered OpenCTI's connectors while a
subscriber exists; a known connector reporting another version raises one event
with a component object; a first sight or a blank version raises nothing.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest

from app.services import integration_component_versions as icv
from app.services.integration_versions import INTEGRATION_VERSION_CHANGED_EVENT


def _component(cid: str, version: str, name: str = "Connector") -> dict[str, str]:
    return {"kind": icv.COMPONENT_CONNECTOR, "id": cid, "name": name, "slug": name.lower(), "version": version}


def test_a_first_sight_is_recorded_and_raises_nothing():
    record, moved = icv.component_changes({}, [_component("c-1", "rolling-abc")])
    assert record == {"c-1": "rolling-abc"}
    assert moved == []


def test_a_known_component_with_another_version_moves():
    record, moved = icv.component_changes({"c-1": "6.8.1"}, [_component("c-1", " 6.8.2 ")])
    assert record == {"c-1": "6.8.2"}
    assert [(c["id"], previous) for c, previous in moved] == [("c-1", "6.8.1")]


def test_an_unchanged_or_blank_version_is_neither_recorded_nor_raised():
    record, moved = icv.component_changes(
        {"c-1": "6.8.1", "c-2": "6.8.1"}, [_component("c-1", "6.8.1"), _component("c-2", ""), {"version": "1"}]
    )
    assert record == {}
    assert moved == []


def test_the_opencti_answer_maps_connectors_to_components():
    data = {"connectors": [{"id": "x", "name": "MISP", "slug": "misp", "version": "rolling"}, {"name": "no id"}, "bad"]}
    assert icv.opencti_components(data) == [
        {"kind": "connector", "id": "x", "name": "MISP", "slug": "misp", "version": "rolling"}
    ]
    assert icv.opencti_components({}) == []


def test_the_payload_carries_the_component_and_the_event_id_is_stable():
    data = icv.component_changed_data(
        provider="opencti",
        platform_identifier="opencti",
        integration_id="i-1",
        component=_component("c-1", "2", name="MISP"),
        previous_version="1",
        version="2",
    )
    assert (data["component_kind"], data["component_id"], data["component_name"], data["component_slug"]) == (
        "connector",
        "c-1",
        "MISP",
        "misp",
    )
    assert (data["previous_version"], data["version"], data["object_id"]) == ("1", "2", "i-1")
    one = icv.component_event_id("opencti", "c-1", "1", "2")
    assert one == icv.component_event_id("opencti", "c-1", " 1", "2 ")
    assert one != icv.component_event_id("opencti", "c-2", "1", "2")
    assert len(one) <= 64


def test_the_catalogue_lists_the_component_filter_keys():
    from app.services import platform_events

    keys = set(platform_events.PLATFORM_EVENTS[INTEGRATION_VERSION_CHANGED_EVENT]["filter_keys"])
    assert {"component_kind", "component_id", "component_name", "component_slug"} <= keys


@pytest.mark.asyncio
async def test_nothing_is_read_without_a_subscriber(monkeypatch):
    from app.services import platform_events

    async def no_subs(name):
        return []

    def fail(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("read without a subscriber")

    monkeypatch.setattr(platform_events, "live_subscriptions", no_subs)
    monkeypatch.setattr(icv, "_platform_tick", fail)
    assert await icv.component_versions_tick() == 0


class _FakeRedis:
    def __init__(self, seen: dict[str, str], slot: bool = True) -> None:
        self.seen = dict(seen)
        self.slot = slot

    async def set(self, key, value, nx=False, ex=None):
        return self.slot

    async def hgetall(self, key):
        return {k.encode(): v.encode() for k, v in self.seen.items()}

    async def hset(self, key, mapping):
        self.seen.update(mapping)


@pytest.mark.asyncio
async def test_a_platform_tick_records_and_publishes_one_event_per_upgrade(monkeypatch):
    from app.core import redis as redis_module
    from app.services import platform_events

    fake = _FakeRedis({"c-1": "rolling-old"})

    async def client():
        return fake

    published: list[tuple[str, dict, str | None]] = []

    async def publish(name, data, *, event_id=None):
        published.append((name, dict(data), event_id))
        return event_id

    async def read(db, integration, user_ids):
        return [_component("c-1", "rolling-new"), _component("c-2", "6.8.2")]

    monkeypatch.setattr(redis_module, "redis_client", client)
    monkeypatch.setattr(platform_events, "publish", publish)
    monkeypatch.setattr(icv, "_read_opencti", read)
    reg = SimpleNamespace(platform_identifier="opencti")
    integration = SimpleNamespace(id=uuid.uuid4())

    assert await icv._platform_tick(None, reg, integration, [uuid.uuid4()]) == 1
    assert fake.seen == {"c-1": "rolling-new", "c-2": "6.8.2"}
    [(name, data, event_id)] = published
    assert name == INTEGRATION_VERSION_CHANGED_EVENT
    assert (data["component_id"], data["previous_version"], data["version"]) == ("c-1", "rolling-old", "rolling-new")
    assert event_id == icv.component_event_id("opencti", "c-1", "rolling-old", "rolling-new")


@pytest.mark.asyncio
async def test_a_slot_held_by_another_replica_reads_nothing(monkeypatch):
    from app.core import redis as redis_module

    async def client():
        return _FakeRedis({}, slot=False)

    async def read(*args):  # pragma: no cover - must not be reached
        raise AssertionError("read without the slot")

    monkeypatch.setattr(redis_module, "redis_client", client)
    monkeypatch.setattr(icv, "_read_opencti", read)
    reg = SimpleNamespace(platform_identifier="opencti")
    assert await icv._platform_tick(None, reg, SimpleNamespace(id=uuid.uuid4()), [uuid.uuid4()]) == 0


@pytest.mark.asyncio
async def test_the_read_goes_through_the_person_scoped_resolver(monkeypatch):
    """A user the integration is not visible to is skipped; the next one reads."""
    from app.integrations.providers.opencti import _common
    from app.services import platform_access

    refused, allowed = uuid.uuid4(), uuid.uuid4()
    asked: list[uuid.UUID] = []

    async def resolve(db, user_id, integration_id, provider):
        asked.append(user_id)
        assert provider == "opencti"
        if user_id == refused:
            raise platform_access.PlatformAccessError()
        return SimpleNamespace(base_url="https://cti.example", headers={"Authorization": "Bearer t"})

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def graphql(client, base, headers, query):
        assert base == "https://cti.example" and headers["Authorization"] == "Bearer t"
        return {"connectors": [{"id": "c-1", "name": "A", "slug": "a", "version": "7.1"}]}, None

    monkeypatch.setattr(platform_access, "resolve_platform_client", resolve)
    monkeypatch.setattr(_common, "_client", lambda headers, timeout=30: _Client())
    monkeypatch.setattr(_common, "_graphql", graphql)
    integration = SimpleNamespace(id=uuid.uuid4(), auth_type="api_key")
    components = await icv._read_opencti(None, integration, [refused, allowed])
    assert asked == [refused, allowed]
    assert [c["id"] for c in components or []] == ["c-1"]


def test_the_autonomous_tick_runs_the_step():
    from app.services import autonomous

    assert "await component_versions_tick()" in inspect.getsource(autonomous._run_autonomous_tick_inner)
