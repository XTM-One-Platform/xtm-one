# SPDX-License-Identifier: Apache-2.0

"""Outbound organization syncs: the XTM One list is the master (#1134 S3)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import organization_context as oc
from app.services import organization_context_import as imp
from app.services import organization_context_outbound as outbound
from app.services import organization_context_sync as osync

OK: dict[str, Any] = {"frequency": "daily", "hour": 6, "minute": 0, "weekday": 0, "timezone": "UTC"}


def _profile(*jewels: dict) -> oc.OrganizationProfile:
    return oc.parse_profile({"name": "Acme", "crown_jewels": list(jewels)})


def test_every_import_pairing_names_its_directions_and_inbound_is_always_offered():
    for section, sources in imp.SECTION_SOURCES.items():
        for source in sources:
            supported, reason = outbound.directions(section, source)
            assert outbound.INBOUND in supported
            assert (outbound.OUTBOUND in supported) == (reason == ""), (section, source)
    # #1591: OpenCRQ primary assets are read only; the old push wrote them as supporting assets.
    assert not outbound.supports("crown_jewels", "opencrq", outbound.OUTBOUND)
    assert "primary assets" in outbound.directions("crown_jewels", "opencrq")[1]


def test_validate_refuses_outbound_where_the_platform_cannot_be_written():
    osync.validate(section="crown_jewels", source="opencrq", direction="inbound", **OK)
    with pytest.raises(osync.SyncRefusedError, match="cannot be outbound"):
        osync.validate(section="crown_jewels", source="opencrq", direction="outbound", **OK)
    with pytest.raises(osync.SyncRefusedError, match="cannot be outbound"):
        osync.validate(section="assets", source="opencti", direction="outbound", **OK)
    with pytest.raises(osync.SyncRefusedError, match="cannot be outbound"):
        osync.validate(section="all", source="", direction="outbound", **OK)
    with pytest.raises(osync.SyncRefusedError, match="inbound or outbound"):
        osync.validate(section="crown_jewels", source="opencrq", direction="both", **OK)


def test_define_keeps_the_stored_direction_when_omitted_and_a_new_direction_forgets_the_created_rows():
    inbound = SimpleNamespace(direction="inbound", outbound_ids={})
    # A save that omits the direction (a PUT from an older client) keeps the stored one.
    osync.define(inbound, owner_id=uuid.uuid4(), section="crown_jewels", source="opencrq")
    assert inbound.direction == "inbound" and inbound.outbound_ids == {}
    # #1591: a stored outbound OpenCRQ sync is refused until saved inbound, which forgets the created rows.
    sync = SimpleNamespace(direction="outbound", outbound_ids={"crm": "a1"})
    with pytest.raises(osync.SyncRefusedError, match="cannot be outbound"):
        osync.define(sync, owner_id=uuid.uuid4(), section="crown_jewels", source="opencrq")
    assert sync.outbound_ids == {"crm": "a1"}
    osync.define(sync, owner_id=uuid.uuid4(), section="crown_jewels", source="opencrq", direction="inbound")
    assert sync.direction == "inbound" and sync.outbound_ids == {}
    new = SimpleNamespace()
    osync.define(new, owner_id=uuid.uuid4(), section="crown_jewels", source="opencrq")
    assert new.direction == "inbound" and new.outbound_ids == {}


def test_plan_creates_updates_and_deletes_only_what_the_sync_created():
    profile = _profile(
        {"name": "CRM", "criticality": "critical"},
        {"name": "Vault", "criticality": "high"},
        {"name": "Billing", "criticality": "medium", "opencrq_asset_id": "linked"},
    )
    assets = [
        {"id": "made-crm", "name": "CRM", "criticality": "high"},
        {"id": "made-old", "name": "Old", "criticality": "high"},
        {"id": "linked", "name": "Billing system", "criticality": "medium"},
        {"id": "theirs", "name": "Payroll", "criticality": "very_high"},
    ]
    plan = outbound.plan_outbound(profile, assets, {"crm": "made-crm", "old": "made-old"})
    assert plan.create == [("Vault", "high")]
    assert plan.update == [("made-crm", "CRM", "very_high")]
    assert plan.delete == [("old", "made-old")]
    # The platform's own asset and the linked asset in step are never touched.
    assert all("theirs" not in str(x) for x in (plan.create, plan.update, plan.delete))


def test_plan_never_pushes_a_platform_row_back_and_keeps_a_linked_asset_name():
    profile = _profile(
        {"name": "Mail", "criticality": "high", "source": "opencrq", "external_id": "m1"},
        {"name": "Other", "criticality": "low", "source": "openaev", "external_id": "x"},
    )
    assets = [{"id": "m1", "name": "Mail (M365)", "criticality": "very_high"}]
    plan = outbound.plan_outbound(profile, assets, {})
    # The OpenCRQ row reads as linked (legacy id): only its criticality follows the profile.
    assert plan.update == [("m1", "Mail (M365)", "high")]
    assert plan.skipped == ["Other"] and plan.create == []


def test_a_linked_jewel_whose_asset_is_gone_is_not_recreated():
    plan = outbound.plan_outbound(_profile({"name": "CRM", "opencrq_asset_id": "gone"}), [], {})
    assert plan.create == [] and plan.update == [] and plan.delete == []


@pytest.mark.asyncio
async def test_an_outbound_run_records_the_created_rows_and_audits(monkeypatch):
    calls: dict = {"audit": []}

    async def take(_id):
        return True

    async def release(_id):
        return None

    async def run_outbound(_db, sync, *, user_id, apply):
        assert apply
        return outbound.OutboundPlan(create=[("CRM", "very_high")]), {"crm": "new-id"}

    async def log_audit(_db, **kwargs):
        calls["audit"].append(kwargs)

    from app.services import audit

    monkeypatch.setattr(osync, "_take_lock", take)
    monkeypatch.setattr(osync, "_release_lock", release)
    monkeypatch.setattr(outbound, "run_outbound", run_outbound)
    monkeypatch.setattr(audit, "log_audit", log_audit)

    owner = SimpleNamespace(id=uuid.uuid4(), email="admin@example.com", is_active=True, is_admin=True)

    class _Db:
        async def get(self, _model, _id):
            return owner

    sync = SimpleNamespace(
        id=uuid.uuid4(),
        section="crown_jewels",
        source="opencrq",
        direction="outbound",
        outbound_ids={},
        filters={},
        owner_user_id=owner.id,
        paused_reason="",
        last_run_at=None,
        last_result=None,
    )
    result = await osync.run_sync(_Db(), sync, trigger="schedule")
    assert (result["direction"], result["added"], result["error"]) == ("outbound", 1, None)
    assert sync.outbound_ids == {"crm": "new-id"}
    assert calls["audit"][0]["summary"].startswith("Outbound organization sync of crown_jewels to opencrq")
