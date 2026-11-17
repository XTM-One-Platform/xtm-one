"""The Security Audit's relay findings."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.api.v1 import security_audit
from app.core.config import settings
from app.services import relays

GROUP_A = uuid.uuid4()
GROUP_B = uuid.uuid4()


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
    """Answers the relay query, then the group query."""

    def __init__(self, relays_rows, groups_rows):
        self._answers = [relays_rows, groups_rows]

    async def execute(self, _statement):
        return _Result(self._answers.pop(0))


def _relay(name, group, *, version="1.261002.1", allows_all=False, online=True):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        group_id=group,
        agent_version=version,
        allows_all_destinations=allows_all,
        is_active=True,
        status="online" if online else "offline",
        last_seen_at=datetime.now(UTC) - (timedelta(seconds=5) if online else timedelta(hours=2)),
    )


def _group(gid, name):
    return SimpleNamespace(id=gid, name=name)


@pytest.fixture
def audit(monkeypatch):
    monkeypatch.setattr(relays, "relays_active", lambda: True)
    monkeypatch.setattr(settings, "relay_min_supported_version", "")

    def run(relay_rows, group_rows, *, counts, routed):
        async def _counts(db, ids):
            return counts

        async def _routed(db, ids):
            return routed

        monkeypatch.setattr(relays, "group_relay_counts", _counts)
        monkeypatch.setattr(relays, "routed_resource_counts", _routed)
        findings: list = []
        import asyncio

        asyncio.run(security_audit._check_relays(_FakeDB(relay_rows, group_rows), findings))
        return {f.id: f for f in findings}

    return run


def test_nothing_is_reported_while_relays_are_off(monkeypatch):
    import asyncio

    monkeypatch.setattr(relays, "relays_active", lambda: False)
    findings: list = []
    asyncio.run(security_audit._check_relays(_FakeDB([], []), findings))
    assert findings == []


def test_a_relay_that_allows_every_destination_is_a_warning(audit):
    found = audit(
        [_relay("dc1", GROUP_A, allows_all=True), _relay("dc2", GROUP_A)],
        [_group(GROUP_A, "Paris")],
        counts={GROUP_A: (2, 2)},
        routed={},
    )
    assert found["network.relay_allows_all_destinations"].severity == "warning"
    assert "dc1 (Paris)" in found["network.relay_allows_all_destinations"].description
    assert "network.relay_destinations_restricted" not in found


def test_restricted_relays_and_an_online_group_in_use_pass(audit, monkeypatch):
    monkeypatch.setattr(settings, "relay_min_supported_version", "1.260901.0")
    found = audit(
        [_relay("dc1", GROUP_A), _relay("dc2", GROUP_A)],
        [_group(GROUP_A, "Paris")],
        counts={GROUP_A: (2, 2)},
        routed={GROUP_A: 3},
    )
    assert found["network.relay_destinations_restricted"].severity == "pass"
    assert found["network.relays_current"].severity == "pass"
    assert found["network.relays_current"].control == "network.relay_version_floor"
    assert found["network.relay_groups_online"].severity == "pass"
    assert "network.relay_single_instance_group" not in found


def test_an_unset_version_floor_is_named_and_an_outdated_relay_warned(audit, monkeypatch):
    found = audit([_relay("dc1", GROUP_A, version="1.250101.0")], [_group(GROUP_A, "Paris")], counts={}, routed={})
    assert found["network.relay_version_floor_unset"].severity == "info"

    monkeypatch.setattr(settings, "relay_min_supported_version", "1.260901.0")
    found = audit([_relay("dc1", GROUP_A, version="1.250101.0")], [_group(GROUP_A, "Paris")], counts={}, routed={})
    assert found["network.relay_outdated"].severity == "warning"
    assert found["network.relay_outdated"].control == "network.relay_version_floor"


def test_a_group_in_use_with_no_relay_online_is_a_warning_and_one_relay_is_noted(audit):
    found = audit(
        [_relay("dc1", GROUP_A), _relay("lyon", GROUP_B, online=False)],
        [_group(GROUP_A, "Paris"), _group(GROUP_B, "Lyon")],
        counts={GROUP_A: (1, 1), GROUP_B: (0, 1)},
        routed={GROUP_A: 1, GROUP_B: 2},
    )
    assert found["network.relay_group_offline_in_use"].severity == "warning"
    assert "Lyon" in found["network.relay_group_offline_in_use"].description
    assert found["network.relay_single_instance_group"].severity == "info"
    assert "Paris" in found["network.relay_single_instance_group"].description


def test_groups_nothing_is_routed_through_report_nothing_about_availability(audit):
    found = audit([], [_group(GROUP_A, "Paris")], counts={GROUP_A: (0, 0)}, routed={})
    assert not any(key.startswith("network.relay_group") for key in found)
