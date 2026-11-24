"""Scheduled syncs of the organization profile sections (#11913).

The schedule arithmetic and the definition checks are pure; a run is covered
with the import's reader and the profile store stubbed, so the rules hold
without a database or a network: a run saves only when something changed, an
owner who cannot read pauses the sync with its reason, a second run of the same
sync is skipped, and Sync all runs every section sync.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import organization_context as oc
from app.services import organization_context_import as imp
from app.services import organization_context_sync as osync


def test_each_location_lists_only_its_sections() -> None:
    # #1134 S2 (Sam, 22 Nov 2026): Settings shows the business profile, CTEM > Inventory the inventory.
    profile, inventory = osync.SECTION_SCOPES["profile"], osync.SECTION_SCOPES["inventory"]
    assert set(profile) | set(inventory) == set(osync.SYNCABLE_SECTIONS)
    assert not set(profile) & set(inventory)
    # Primary assets are an Inventory source (Sam, 24 Nov 2026): their OpenCRQ sync sits with the asset sources.
    assert osync.in_scope("crown_jewels", "inventory") and not osync.in_scope("crown_jewels", "profile")
    assert osync.in_scope("assets", "inventory") and not osync.in_scope("assets", "profile")
    assert osync.in_scope(osync.ALL_SECTIONS, "profile") and osync.in_scope(osync.ALL_SECTIONS, "inventory")
    assert all(osync.in_scope(s, "") for s in osync.SYNCABLE_SECTIONS)


def _sync(**kwargs) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "section": "assets",
        "source": "openaev",
        "filters": {"tags": ["prod"]},
        "batch_type": "",
        "remove_gone": False,
        "frequency": "daily",
        "hour": 6,
        "minute": 30,
        "weekday": 0,
        "timezone": "Europe/Paris",
        "enabled": True,
        "paused_reason": "",
        "owner_user_id": uuid.uuid4(),
        "last_run_at": None,
        "last_result": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_syncable_sections_are_the_import_sections_only():
    assert set(osync.SYNCABLE_SECTIONS) == set(imp.SECTION_SOURCES)
    for typed in ("name", "sector", "regions", "size", "currency", "risk_appetite"):
        with pytest.raises(osync.SyncRefusedError):
            osync.validate(
                section=typed, source="openaev", frequency="daily", hour=6, minute=0, weekday=0, timezone="UTC"
            )


def test_validate_refuses_a_source_that_cannot_feed_the_section_and_a_bad_schedule():
    ok = {"frequency": "daily", "hour": 6, "minute": 0, "weekday": 0, "timezone": "UTC"}
    osync.validate(section="pirs", source="opencti", **ok)
    osync.validate(section="all", source="", **ok)
    with pytest.raises(osync.SyncRefusedError, match="cannot feed"):
        osync.validate(section="pirs", source="openaev", **ok)
    with pytest.raises(osync.SyncRefusedError, match="time zone"):
        osync.validate(section="pirs", source="opencti", **{**ok, "timezone": "Mars/Olympus"})
    with pytest.raises(osync.SyncRefusedError, match="hour"):
        osync.validate(section="pirs", source="opencti", **{**ok, "hour": 24})


def test_next_run_daily_in_the_sync_time_zone():
    sync = _sync(frequency="daily", hour=6, minute=30, timezone="Europe/Paris")
    # 14 Nov 2026 is CET (UTC+1): 06:30 Paris is 05:30Z.
    assert osync.next_run(sync, datetime(2026, 11, 14, 4, 0, tzinfo=UTC)) == datetime(2026, 11, 14, 5, 30, tzinfo=UTC)
    assert osync.next_run(sync, datetime(2026, 11, 14, 5, 30, tzinfo=UTC)) == datetime(2026, 11, 15, 5, 30, tzinfo=UTC)


def test_next_run_hourly_and_weekly():
    hourly = _sync(frequency="hourly", minute=15, timezone="UTC")
    assert osync.next_run(hourly, datetime(2026, 11, 14, 10, 20, tzinfo=UTC)) == datetime(
        2026, 11, 14, 11, 15, tzinfo=UTC
    )
    # 14 Nov 2026 is a Saturday; weekday 0 is Monday 16 Nov.
    weekly = _sync(frequency="weekly", weekday=0, hour=8, minute=0, timezone="UTC")
    assert osync.next_run(weekly, datetime(2026, 11, 14, 9, 0, tzinfo=UTC)) == datetime(2026, 11, 16, 8, 0, tzinfo=UTC)
    monday_late = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
    assert osync.next_run(weekly, monday_late) == datetime(2026, 11, 23, 8, 0, tzinfo=UTC)


def test_an_unknown_time_zone_reads_utc():
    assert str(osync.zone("Nowhere/Land")) == "UTC"


class _Db:
    """The session a run uses: ``get`` answers the owner."""

    def __init__(self, owner):
        self.owner = owner

    async def get(self, _model, _id):
        return self.owner


@pytest.fixture
def stubs(monkeypatch):
    calls: dict = {"saved": [], "audit": [], "locks": set()}

    async def take(sync_id):
        if sync_id in calls["locks"]:
            return False
        calls["locks"].add(sync_id)
        return True

    async def release(sync_id):
        calls["locks"].discard(sync_id)

    async def save_version(_db, profile, **kwargs):
        calls["saved"].append(kwargs)
        return SimpleNamespace(version=8)

    async def log_audit(_db, **kwargs):
        calls["audit"].append(kwargs)

    async def current(_db):
        return SimpleNamespace(profile={}, version=7)

    async def no_records(*_a: object, **_k: object) -> None:
        return None  # no organization records: the profile path these runs test

    from app.services import audit

    monkeypatch.setattr(imp, "_plan_on_records", no_records)
    monkeypatch.setattr(osync, "_take_lock", take)
    monkeypatch.setattr(osync, "_release_lock", release)
    monkeypatch.setattr(oc, "save_version", save_version)
    monkeypatch.setattr(oc, "current_version", current)
    monkeypatch.setattr(audit, "log_audit", log_audit)
    return calls


def _admin(**kwargs):
    return SimpleNamespace(id=uuid.uuid4(), email="admin@example.com", is_active=True, is_admin=True, **kwargs)


def _reader(rows=None, error=None):
    async def read(_db, _user, section, filters, batch_type):
        return imp.ReadResult(rows=rows or [], total=len(rows or []), error=error)

    return read


@pytest.mark.asyncio
async def test_a_run_saves_a_version_with_the_sync_provenance(monkeypatch, stubs):
    monkeypatch.setattr(imp, "_read_openaev", _reader([imp.openaev_asset({"asset_id": "a1", "asset_name": "web-01"})]))
    sync = _sync()
    result = await osync.run_sync(_Db(_admin()), sync, trigger="schedule")
    assert (result["added"], result["version"], result["error"]) == (1, 8, None)
    basis = stubs["saved"][0]["sources"]["assets_exposure"]["basis"]
    assert basis.startswith(f"scheduled sync {sync.id} saved by admin@example.com; import of assets")
    assert stubs["saved"][0]["source"] == "draft"
    assert stubs["audit"][0]["entity_type"] == "organization_context_sync"
    assert sync.last_result["added"] == 1 and sync.last_run_at is not None
    assert stubs["locks"] == set()


@pytest.mark.asyncio
async def test_a_run_with_nothing_new_saves_nothing(monkeypatch, stubs):
    monkeypatch.setattr(imp, "_read_openaev", _reader([]))
    result = await osync.run_sync(_Db(_admin()), _sync(), trigger="schedule")
    assert result["version"] is None and stubs["saved"] == [] and stubs["audit"] == []


@pytest.mark.asyncio
async def test_a_missing_token_pauses_the_sync_with_its_reason(monkeypatch, stubs):
    monkeypatch.setattr(
        imp, "_read_openaev", _reader(error="OpenAEV: not connected, or no token for this administrator")
    )
    sync = _sync()
    result = await osync.run_sync(_Db(_admin()), sync, trigger="schedule")
    assert "no token" in result["error"] and "no token" in sync.paused_reason
    assert stubs["saved"] == [] and stubs["audit"][0]["summary"].endswith(result["error"])


@pytest.mark.asyncio
async def test_a_run_that_only_drops_superseded_rows_saves_the_profile(monkeypatch, stubs):
    # #1591 review: an older build's supporting-asset crown jewel is dropped even when OpenCRQ
    # has no primary asset yet; the run saves that cleanup and reports it as removed.
    async def current(_db):
        jewel = {"name": "Mail server", "source": "opencrq", "external_id": "s1"}
        return SimpleNamespace(profile={"crown_jewels": [jewel]}, version=7)

    async def read(_db, _user, _filters):
        return imp.ReadResult(rows=[], total=0, superseded={"s1"})

    async def no_links(*_a, **_k):
        return {}

    from app.services import organization_context_opencrq_links as opencrq_links

    monkeypatch.setattr(oc, "current_version", current)
    monkeypatch.setattr(imp, "_read_opencrq", read)
    monkeypatch.setattr(opencrq_links, "sync_links_for", no_links)
    sync = _sync(section="crown_jewels", source="opencrq", filters={})
    result = await osync.run_sync(_Db(_admin()), sync, trigger="schedule")
    assert (result["added"], result["updated"], result["removed"], result["version"]) == (0, 0, 1, 8)
    assert len(stubs["saved"]) == 1


@pytest.mark.asyncio
async def test_an_owner_no_longer_admin_pauses_without_reading(monkeypatch, stubs):
    async def never(*_a, **_k):
        raise AssertionError("read without an administrator")

    monkeypatch.setattr(imp, "_read_openaev", never)
    sync = _sync()
    owner = SimpleNamespace(id=uuid.uuid4(), email="x@example.com", is_active=True, is_admin=False)
    result = await osync.run_sync(_Db(owner), sync, trigger="schedule")
    assert "no longer an administrator" in sync.paused_reason and result["error"] == sync.paused_reason


@pytest.mark.asyncio
async def test_a_second_run_of_the_same_sync_is_skipped(monkeypatch, stubs):
    monkeypatch.setattr(imp, "_read_openaev", _reader([]))
    sync = _sync()
    stubs["locks"].add(sync.id)
    result = await osync.run_sync(_Db(_admin()), sync, trigger="schedule")
    assert result["skipped"] and stubs["saved"] == []


class _Found:
    def __init__(self, hit: bool):
        self.hit = hit

    def first(self):
        return (uuid.uuid4(),) if self.hit else None


class _DefaultDb:
    """Answers the two reads of the default source in order: existing sync, deleted by hand."""

    def __init__(self, *, exists=False, deleted=False):
        self.answers = [exists, deleted]
        self.added: list = []

    async def execute(self, _stmt):
        return _Found(self.answers.pop(0))

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_opencrq_connected_creates_the_daily_primary_asset_source_once(monkeypatch, stubs):
    owner = _admin()

    async def default_owner(_db):
        return owner

    monkeypatch.setattr(osync, "_default_owner", default_owner)
    db = _DefaultDb()
    row = await osync.ensure_default_primary_asset_sync(db)
    assert db.added == [row]
    assert (row.section, row.source, row.frequency, row.enabled) == ("crown_jewels", "opencrq", "daily", True)
    assert row.owner_user_id == owner.id and row.next_run_at is not None and row.direction == "inbound"
    # The supporting-asset links of #1591 are on by default: no filter turns them off.
    from app.services import organization_context_opencrq_links as opencrq_links

    assert opencrq_links.link_option_on(row.filters)
    assert osync.in_scope(row.section, "inventory")
    assert stubs["audit"][0]["action"] == "create" and stubs["audit"][0]["details"]["default"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [{"exists": True}, {"deleted": True}],
    ids=["already-saved-or-turned-off", "deleted-by-an-administrator"],
)
async def test_the_default_primary_asset_source_is_never_forced(monkeypatch, stubs, state):
    async def never(_db):
        raise AssertionError("no owner is looked up when nothing is created")

    monkeypatch.setattr(osync, "_default_owner", never)
    db = _DefaultDb(**state)
    assert await osync.ensure_default_primary_asset_sync(db) is None and db.added == []


@pytest.mark.asyncio
async def test_opencrq_not_connected_for_any_administrator_creates_nothing(monkeypatch, stubs):
    async def nobody(_db):
        return None

    monkeypatch.setattr(osync, "_default_owner", nobody)
    db = _DefaultDb()
    assert await osync.ensure_default_primary_asset_sync(db) is None and db.added == []


@pytest.mark.asyncio
async def test_a_scheduled_primary_asset_run_counts_and_keeps_manual_rows(monkeypatch, stubs):
    # A manual primary asset survives a scheduled OpenCRQ run (#1591 rule: manual wins); the run counts what it added.
    async def current(_db):
        manual = {"name": "Payroll", "criticality": "critical", "source": "manual"}
        return SimpleNamespace(profile={"crown_jewels": [manual]}, version=7)

    async def read(_db, _user, _filters):
        return imp.ReadResult(
            rows=[imp.CrownJewel(name="Billing", criticality="high", source="opencrq", external_id="p1")], total=1
        )

    async def links(*_a, **_k):
        return {"added": 2}

    from app.services import organization_context_opencrq_links as opencrq_links

    monkeypatch.setattr(oc, "current_version", current)
    monkeypatch.setattr(imp, "_read_opencrq", read)
    monkeypatch.setattr(opencrq_links, "sync_links_for", links)
    saved_profiles: list = []

    async def save_version(_db, profile, **kwargs):
        saved_profiles.append(profile)
        return SimpleNamespace(version=8)

    monkeypatch.setattr(oc, "save_version", save_version)
    sync = _sync(section="crown_jewels", source="opencrq", filters={}, remove_gone=True)
    result = await osync.run_sync(_Db(_admin()), sync, trigger="schedule")
    assert (result["added"], result["removed"], result["version"], result["links"]) == (1, 0, 8, {"added": 2})
    names = {(j.name, j.source) for j in saved_profiles[0].crown_jewels}
    assert names == {("Payroll", "manual"), ("Billing", "opencrq")}
    assert sync.last_result["added"] == 1


@pytest.mark.asyncio
async def test_run_due_syncs_creates_the_default_source_before_reading_due_ones(monkeypatch):
    calls: list[str] = []

    async def ensure(_db):
        calls.append("ensure")
        return SimpleNamespace()

    class _Due:
        def scalars(self):
            return self

        def all(self):
            return []

    class _TickDb:
        async def commit(self):
            calls.append("commit")

        async def rollback(self):
            calls.append("rollback")

        async def execute(self, _stmt):
            calls.append("due")
            return _Due()

    monkeypatch.setattr(osync, "ensure_default_primary_asset_sync", ensure)
    assert await osync.run_due_syncs(_TickDb()) == 0
    assert calls == ["ensure", "commit", "due"]


@pytest.mark.asyncio
async def test_sync_all_runs_every_section_sync_and_sums_them(monkeypatch, stubs):
    members = [_sync(), _sync(section="tech_stack")]
    seen = []

    async def section_syncs(_db):
        return members

    async def run_sync(_db, member, *, trigger):
        seen.append((member.section, trigger))
        return {"added": 2, "updated": 1, "removed": 0, "version": 9, "error": None}

    monkeypatch.setattr(osync, "section_syncs", section_syncs)
    monkeypatch.setattr(osync, "run_sync", run_sync)
    everything = _sync(section="all", source="")
    totals = await osync.run_now(None, everything, trigger="schedule")
    assert [s for s, _ in seen] == ["assets", "tech_stack"] and seen[0][1] == "schedule, sync all"
    assert (totals["runs"], totals["added"], totals["updated"], totals["version"]) == (2, 4, 2, 9)
    assert everything.last_result == totals
