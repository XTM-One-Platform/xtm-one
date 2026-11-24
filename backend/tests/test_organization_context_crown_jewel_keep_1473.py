"""Hand-typed crown jewels survive the OpenCRQ reconcile and the crown-jewel sync (#1473).

A link set by the reconcile never hands a typed row to the sync; a sync only
removes the rows it wrote; a jewel whose criticality the reconcile pushes
below the sync's threshold stays in the profile; a stale save is refused and
a save says which rows it dropped; a past version can be read and restored.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.api.v1 import organization_context as api
from app.services import organization_context as oc
from app.services import organization_context_import as imp
from app.services.organization_context_opencrq import plan_sync

_SYNC = "sync-1"


def _typed(name: str, criticality: str) -> dict[str, Any]:
    return {"name": name, "criticality": criticality, "owner": f"{name} owner", "business_service": f"{name} svc"}


def _assets(levels: dict[str, str]) -> list[dict[str, Any]]:
    # OpenCRQ primary assets rated by one security need (#1591): the highest need is the criticality.
    return [{"id": f"a-{name}", "name": name, "availability": level} for name, level in levels.items()]


def _sync_read(assets: list[dict[str, Any]], min_level: str = "high") -> imp.ReadResult:
    rows = [j for j in (imp.opencrq_jewel(a, min_level) for a in assets) if j]
    return imp.ReadResult(rows=rows, total=len(rows))


def test_a_link_keeps_a_typed_row_typed() -> None:
    profile = oc.parse_profile(
        {"crown_jewels": [{**_typed("ERP", "medium"), "source": "manual", "opencrq_asset_id": "a-1"}]}
    )
    jewel = profile.crown_jewels[0]
    assert (jewel.source, jewel.external_id, jewel.opencrq_asset_id) == ("manual", "", "a-1")


def test_a_row_saved_before_provenance_still_reads_as_an_opencrq_row() -> None:
    jewel = oc.parse_profile({"crown_jewels": [{"name": "Payments", "opencrq_asset_id": "a-1"}]}).crown_jewels[0]
    assert (jewel.source, jewel.external_id) == ("opencrq", "a-1")


def test_three_typed_jewels_survive_the_reconcile_then_the_sync() -> None:
    """The acceptance scenario: critical, medium and low typed, OpenCRQ assets of the same names."""
    profile = oc.parse_profile(
        {"crown_jewels": [_typed("Core", "critical"), _typed("Billing", "medium"), _typed("Wiki", "low")]}
    )
    assets = _assets({"Core": "high", "Billing": "high", "Wiki": "high"})
    reconcile = plan_sync(profile, assets)
    assert sorted(j for j, _ in reconcile.linked) == ["Billing", "Core", "Wiki"]
    # Saved then read back, as the next request does.
    saved = oc.parse_profile(reconcile.profile.model_dump(mode="json"))
    assert {j.source for j in saved.crown_jewels} == {"manual"}
    # #1591: nothing is pushed to OpenCRQ; the sync reads the same primary assets (>= high).
    assert reconcile.pushed == []
    plan = imp.plan_import(saved, "crown_jewels", "opencrq", _sync_read(assets), remove_gone=True, sync_id=_SYNC)
    assert plan.profile is not None
    jewels = {j.name: j for j in plan.profile.crown_jewels}
    assert sorted(jewels) == ["Billing", "Core", "Wiki"]
    for name in ("Core", "Billing", "Wiki"):
        assert (jewels[name].owner, jewels[name].business_service) == (f"{name} owner", f"{name} svc")
        assert jewels[name].source == "manual"


def test_a_row_already_converted_by_an_older_build_is_kept_by_the_sync() -> None:
    """Rows stored as opencrq with no sync stamp (written by a link before #1473) are reported, never removed."""
    stored = oc.CrownJewel(name="Billing", criticality="medium", source="opencrq", external_id="a-Billing", owner="CFO")
    profile = oc.OrganizationProfile(crown_jewels=[stored])
    plan = imp.plan_import(profile, "crown_jewels", "opencrq", _sync_read([]), remove_gone=True, sync_id=_SYNC)
    assert plan.gone == ["Billing"] and plan.gone_kept == ["Billing"]
    assert plan.profile is not None and [j.owner for j in plan.profile.crown_jewels] == ["CFO"]
    assert plan.as_dict()["gone_kept"] == ["Billing"]


def test_a_sync_still_removes_the_rows_it_wrote() -> None:
    mine = oc.CrownJewel(name="Old", source="opencrq", external_id="a-Old", sync_id=_SYNC)
    plan = imp.plan_import(
        oc.OrganizationProfile(crown_jewels=[mine]),
        "crown_jewels",
        "opencrq",
        _sync_read([]),
        remove_gone=True,
        sync_id=_SYNC,
    )
    assert plan.gone == ["Old"] and plan.gone_kept == []
    assert plan.profile is not None and plan.profile.crown_jewels == []


def test_the_reconcile_never_releases_a_synced_row() -> None:
    # #1591: no criticality is pushed, so no synced row falls below the sync's threshold by the reconcile.
    synced = oc.CrownJewel(
        name="Billing", criticality="medium", source="opencrq", external_id="a-Billing", sync_id=_SYNC, owner="CFO"
    )
    plan = plan_sync(oc.OrganizationProfile(crown_jewels=[synced]), _assets({"Billing": "high"}))
    assert plan.released == [] and plan.pushed == []
    jewel = plan.profile.crown_jewels[0]
    assert (jewel.source, jewel.external_id, jewel.owner) == ("opencrq", "a-Billing", "CFO")
    assert plan.summary()["kept_below_sync_threshold"] == []


def test_removed_rows_names_what_a_save_dropped() -> None:
    before = oc.OrganizationProfile(
        crown_jewels=[oc.CrownJewel(name="A"), oc.CrownJewel(name="B", source="opencrq", external_id="x")],
        software=[oc.SoftwareEntry(product="Splunk")],
    ).model_dump(mode="json")
    after = oc.OrganizationProfile(
        crown_jewels=[oc.CrownJewel(name="A")], software=[oc.SoftwareEntry(product="Splunk")]
    )
    assert oc.removed_rows(before, after.model_dump(mode="json")) == {"crown_jewels": ["B"]}
    assert oc.removed_rows(None, after.model_dump(mode="json")) == {}


class _Db:
    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:  # pragma: no cover
        pass

    async def refresh(self, _row: Any) -> None:
        pass


def _row(version: int, profile: dict[str, Any], sources: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        version=version, profile=profile, source="api", sources=sources, created_at=None, author_user_id=None
    )


def _store(monkeypatch: pytest.MonkeyPatch, versions: dict[int, dict[str, Any]]) -> list[SimpleNamespace]:
    saved: list[SimpleNamespace] = []

    async def current_version(_db: Any, organization_id: Any = None) -> SimpleNamespace:
        latest = max(versions)
        return _row(latest, versions[latest])

    async def get_version(_db: Any, version: int, organization_id: Any = None) -> SimpleNamespace | None:
        return _row(version, versions[version]) if version in versions else None

    async def save_version(
        _db: Any, profile: Any, *, source: str, author_user_id: Any, sources: Any = None
    ) -> SimpleNamespace:
        row = _row(max(versions) + 1, profile.model_dump(mode="json"), sources)
        versions[row.version] = row.profile
        saved.append(row)
        return row

    monkeypatch.setattr(oc, "current_version", current_version)
    monkeypatch.setattr(oc, "get_version", get_version)
    monkeypatch.setattr(oc, "save_version", save_version)
    return saved


@pytest.mark.asyncio
async def test_a_save_based_on_a_stale_version_is_refused_and_nothing_is_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    """Client A added a jewel (v2); client B saves its copy based on v1: 409, v2 kept."""
    versions: dict[int, dict[str, Any]] = {
        1: {"crown_jewels": [{"name": "A"}]},
        2: {"crown_jewels": [{"name": "A"}, {"name": "B"}]},
    }
    saved = _store(monkeypatch, versions)
    body = api.SaveOrganizationContextRequest(profile={"crown_jewels": [{"name": "A"}]}, based_on_version=1)
    admin: Any = SimpleNamespace(id=uuid.uuid4())
    db: Any = _Db()
    with pytest.raises(HTTPException) as exc:
        await api.save_organization_context(body, db=db, admin=admin)
    assert exc.value.status_code == 409 and "version 1" in exc.value.detail
    assert saved == [] and max(versions) == 2


@pytest.mark.asyncio
async def test_a_version_reads_whole_and_a_restore_names_its_source(monkeypatch: pytest.MonkeyPatch) -> None:
    versions: dict[int, dict[str, Any]] = {
        1: {"name": "Acme", "crown_jewels": [{"name": "A", "owner": "CISO"}]},
        2: {"name": "Acme Corp", "crown_jewels": []},
    }
    saved = _store(monkeypatch, versions)
    admin: Any = SimpleNamespace(id=uuid.uuid4())
    reader: Any = _Db()
    past = await api.get_organization_context_version(1, db=reader, _admin=admin)
    assert past.version == 1 and past.profile["crown_jewels"][0]["owner"] == "CISO"
    with pytest.raises(HTTPException) as missing:
        await api.get_organization_context_version(9, db=reader, _admin=admin)
    assert missing.value.status_code == 404

    db: Any = _Db()
    body = api.RestoreOrganizationContextRequest(fields=["crown_jewels"], based_on_version=2)
    restored = await api.restore_organization_context_version(1, body, db=db, admin=admin)
    assert db.committed and restored.version == 3
    assert [j["name"] for j in restored.profile["crown_jewels"]] == ["A"]
    assert restored.profile["name"] == "Acme Corp"
    assert restored.sources == {
        "crown_jewels": {"source": "restored from v1", "basis": "restored from v1 by an administrator"}
    }
    assert len(saved) == 1

    with pytest.raises(HTTPException) as stale:
        await api.restore_organization_context_version(1, body, db=reader, admin=admin)
    assert stale.value.status_code == 409


@pytest.mark.asyncio
async def test_a_restore_linking_an_asset_gone_since_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A restore runs the checks of a form save: a jewel linking an asset no longer listed is a 422, nothing saved."""
    versions: dict[int, dict[str, Any]] = {
        1: {"assets_exposure": [{"name": "ERP"}], "crown_jewels": [{"name": "A", "asset_names": ["ERP"]}]},
        2: {"assets_exposure": [], "crown_jewels": []},
    }
    saved = _store(monkeypatch, versions)
    admin: Any = SimpleNamespace(id=uuid.uuid4())
    db: Any = _Db()
    body = api.RestoreOrganizationContextRequest(fields=["crown_jewels"], based_on_version=2)
    with pytest.raises(HTTPException) as exc:
        await api.restore_organization_context_version(1, body, db=db, admin=admin)
    assert exc.value.status_code == 422 and "ERP" in exc.value.detail
    assert saved == [] and not db.committed


@pytest.mark.asyncio
async def test_a_restore_rewriting_a_synced_row_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A restore cannot set a synced row's fields by hand (#1367 lock): 422, nothing saved."""
    synced = {"name": "S", "source": "opencrq", "external_id": "a-S"}
    versions: dict[int, dict[str, Any]] = {
        1: {"crown_jewels": [{**synced, "criticality": "medium"}]},
        2: {"crown_jewels": [{**synced, "criticality": "critical"}]},
    }
    saved = _store(monkeypatch, versions)
    admin: Any = SimpleNamespace(id=uuid.uuid4())
    db: Any = _Db()
    body = api.RestoreOrganizationContextRequest(fields=["crown_jewels"], based_on_version=2)
    with pytest.raises(HTTPException) as exc:
        await api.restore_organization_context_version(1, body, db=db, admin=admin)
    assert exc.value.status_code == 422 and "kept in sync from opencrq" in exc.value.detail
    assert saved == [] and not db.committed


def test_restored_profile_takes_the_named_fields_only() -> None:
    current = {"name": "Now", "crown_jewels": []}
    past = {"name": "Then", "crown_jewels": [{"name": "A"}]}
    merged, fields = oc.restored_profile(current, past, ["crown_jewels"])
    assert merged == {"name": "Now", "crown_jewels": [{"name": "A"}]} and fields == ["crown_jewels"]
    try:
        oc.restored_profile(current, past, ["nope"])
    except ValueError as exc:
        assert "nope" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an unknown field must be refused")


def test_ai_systems_is_not_a_restorable_field() -> None:
    """#1844: no ai_systems alias; a restore naming it is refused like any unknown field."""
    current = oc.parse_profile({"name": "Now"}).model_dump(mode="json")
    with pytest.raises(ValueError, match="ai_systems"):
        oc.restored_profile(current, current, ["ai_systems"])
