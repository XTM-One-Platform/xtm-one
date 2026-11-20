"""``integration.version.changed``: a connected platform's upgrade, raised by its heartbeat.

Raised once per platform and version change, after the registration commit;
never on a first registration, a blank version or an unchanged one. The
catalogue lists it platform-wide with its filter keys, and the registration
handler calls it with the version read under the registration lock.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest

from app.services import integration_versions
from app.services.integration_versions import (
    INTEGRATION_VERSION_CHANGED_EVENT,
    publish_version_change,
    version_change,
    version_changed_data,
    version_changed_event_id,
)


@pytest.mark.parametrize(
    ("previous", "current", "changed"),
    [
        ("6.8.1", "6.8.2", True),
        ("6.8.2", "6.8.1", True),
        (" 6.8.1 ", "6.8.1", False),
        ("6.8.1", "6.8.1", False),
        (None, "6.8.1", False),
        ("", "6.8.1", False),
        ("6.8.1", "", False),
        ("6.8.1", None, False),
    ],
)
def test_only_a_known_version_moving_to_another_is_a_change(previous, current, changed):
    assert version_change(previous, current) is changed


def test_the_payload_names_the_platform_and_both_versions():
    seen = datetime(2026, 11, 20, 1, 0, tzinfo=UTC)
    data = version_changed_data(
        provider="opencti",
        platform_identifier="opencti",
        integration_id="i-1",
        previous_version=" 6.8.1",
        version="6.8.2 ",
        first_seen_at=seen,
    )
    assert data["provider"] == "opencti"
    assert data["integration_id"] == "i-1"
    assert (data["previous_version"], data["version"]) == ("6.8.1", "6.8.2")
    assert data["first_seen_at"] == seen.isoformat()
    assert data["object_type"] == "integration" and data["object_id"] == "i-1"
    assert data["participant_agent_ids"] == []


def test_the_event_id_is_stable_per_platform_and_upgrade():
    one = version_changed_event_id("opencti", "6.8.1", "6.8.2")
    assert one == version_changed_event_id("opencti", " 6.8.1", "6.8.2 ")
    assert one != version_changed_event_id("openaev", "6.8.1", "6.8.2")
    assert one != version_changed_event_id("opencti", "6.8.2", "6.8.1")
    assert len(one) <= 64


@pytest.fixture
def published(monkeypatch):
    from app.services import platform_events

    events: list[tuple[str, dict, str | None]] = []

    async def publish(name, data, *, event_id=None):
        events.append((name, dict(data), event_id))
        return event_id

    monkeypatch.setattr(platform_events, "publish", publish)
    return events


@pytest.mark.asyncio
async def test_an_upgrade_publishes_one_event(published):
    event_id = await publish_version_change(
        provider="openaev",
        platform_identifier="openaev",
        integration_id="i-2",
        previous_version="1.18.0",
        version="1.19.0",
    )
    assert [(name, data["version"], eid) for name, data, eid in published] == [
        (INTEGRATION_VERSION_CHANGED_EVENT, "1.19.0", event_id)
    ]
    assert event_id == version_changed_event_id("openaev", "1.18.0", "1.19.0")


@pytest.mark.asyncio
@pytest.mark.parametrize(("previous", "current"), [(None, "1.19.0"), ("1.19.0", "1.19.0"), ("1.19.0", "")])
async def test_a_first_registration_or_an_unchanged_version_publishes_nothing(published, previous, current):
    assert (
        await publish_version_change(
            provider="opencrq",
            platform_identifier="opencrq",
            integration_id="i-3",
            previous_version=previous,
            version=current,
        )
        is None
    )
    assert published == []


def test_the_catalogue_lists_it_platform_wide_with_its_filter_keys():
    from app.services import platform_events

    entry = platform_events.PLATFORM_EVENTS[INTEGRATION_VERSION_CHANGED_EVENT]
    assert {"provider", "platform_identifier", "integration_id", "previous_version", "version"} <= set(
        entry["filter_keys"]
    )
    assert INTEGRATION_VERSION_CHANGED_EVENT in platform_events.PLATFORM_WIDE_EVENTS


def test_the_registration_heartbeat_compares_the_version_read_under_its_lock():
    from app.api.v1 import platform

    source = inspect.getsource(platform.register_platform)
    read = source.index("previous_platform_version = reg.platform_version if reg else None")
    assert source.index("await _lock_platform_registration(") < read
    assert read < source.index("reg.platform_version = body.platform_version")
    call = source.index("await publish_version_change(")
    assert source.index("await db.commit()") < call
    assert "published_default" in source[source.rindex("registration_token_state()", 0, call) : call]
    assert integration_versions.publish_version_change.__name__ in source
