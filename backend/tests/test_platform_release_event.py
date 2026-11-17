"""``platform.release.deployed``: the first boot of a build is an event."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.services import platform_builds as pb
from app.services import platform_events as pe
from app.services.flow_triggers import platform_event_in_flow_scope

NAME = "platform.release.deployed"


def _payload() -> dict:
    data = pb.release_deployed_data(
        version="1.261104.0-dev.abc1234",
        git_sha="abc1234def",
        first_seen_at=datetime(2026, 11, 4, 12, 0, tzinfo=UTC),
        previous_version="1.261104.0-dev.0001111",
        previous_git_sha="0001111aaa",
    )
    return {**data, "event": NAME, "event_id": pb.release_event_id("1.261104.0-dev.abc1234", "abc1234def")}


def test_the_event_is_listed_with_its_filter_keys():
    entry = next(e for e in pe.catalog() if e["event"] == NAME)
    assert {"version", "git_sha", "previous_version", "previous_git_sha", "first_seen_at"} <= set(entry["filter_keys"])
    assert pe.trigger_config_error("platform_event", {"event": NAME, "once": True}) is None


def test_the_payload_names_the_build_and_the_one_before():
    payload = _payload()
    assert payload["version"] == "1.261104.0-dev.abc1234" and payload["git_sha"] == "abc1234def"
    assert payload["previous_version"] == "1.261104.0-dev.0001111"
    assert payload["previous_git_sha"] == "0001111aaa"
    assert payload["first_seen_at"] == "2026-11-04T12:00:00+00:00"
    assert payload["object_type"] == "platform_release"
    assert not pe.touched_workspaces(payload) and not pe.personal_owners(payload)


def test_the_first_build_has_no_previous_one():
    data = pb.release_deployed_data(
        version="1.0.0", git_sha="", first_seen_at=datetime.now(UTC), previous_version=None, previous_git_sha=None
    )
    assert data["git_sha"] is None and data["previous_version"] is None and data["previous_git_sha"] is None


def test_the_event_id_is_stable_per_build_and_fits_a_watch():
    a = pb.release_event_id("1.0.0", "abc")
    assert a == pb.release_event_id("1.0.0", "abc")
    assert a != pb.release_event_id("1.0.0", "abd") and a != pb.release_event_id("1.0.1", "abc")
    assert len(pb.release_event_id("v" * 100, "f" * 64)) <= 64


def test_an_own_scope_subscriber_hears_it_without_taking_part():
    sub = pe.Subscription(
        assignment_id=uuid.uuid4(), agent_id=uuid.uuid4(), events=(NAME,), filter={}, scope_all=False
    )
    assert pe.mismatch_reason(sub, NAME, _payload()) is None
    assert pe.mismatch_reason(
        pe.Subscription(
            assignment_id=uuid.uuid4(), agent_id=uuid.uuid4(), events=(NAME,), filter={"version": "2.0.0"},
            scope_all=False,
        ),
        NAME,
        _payload(),
    ) is not None
    assert platform_event_in_flow_scope(owner_id=uuid.uuid4(), agent_ids=set(), payload=_payload(), scope_all=False)


def test_an_object_event_is_not_platform_wide():
    assert not pe.is_platform_wide({"event": "work_item.completed", "participant_agent_ids": []})


@pytest.mark.asyncio
async def test_a_first_boot_publishes_once_with_a_stable_id(monkeypatch):
    published: list = []

    async def upsert(db, *, now):
        return dict(_payload()) if not published else None

    async def publish(name, data, *, event_id=None):
        published.append((name, data, event_id))
        return event_id

    from app.services import work_item_blocked_refs as refs_mod

    checked: list = []
    monkeypatch.setattr(pb, "_upsert_running_build", upsert)
    monkeypatch.setattr(pe, "publish", publish)
    monkeypatch.setattr(refs_mod, "schedule_release_check", checked.append)
    await pb.record_running_build(object())
    await pb.record_running_build(object())
    assert len(published) == 1
    # The items blocked on a deployment are checked once, on the first boot.
    assert [c["git_sha"] for c in checked] == ["abc1234def"]
    name, data, event_id = published[0]
    assert name == NAME and event_id == pb.release_event_id("1.261104.0-dev.abc1234", "abc1234def")
    assert data["previous_git_sha"] == "0001111aaa"


@pytest.mark.asyncio
async def test_a_failed_record_publishes_nothing_and_never_raises(monkeypatch):
    published: list = []

    class _Db:
        async def rollback(self):
            return None

    async def upsert(db, *, now):
        raise RuntimeError("database down")

    async def publish(name, data, *, event_id=None):
        published.append(name)

    monkeypatch.setattr(pb, "_upsert_running_build", upsert)
    monkeypatch.setattr(pe, "publish", publish)
    await pb.record_running_build(_Db())
    assert published == []
