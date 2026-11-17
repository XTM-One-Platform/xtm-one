"""A work item's fleet impact routes its completion to the agents it names."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import platform_events
from app.services import work_item as wi
from app.services import work_item_fleet_impact as fi
from app.services.integration_event_filters import payload_filter_reason

CREATOR = uuid.uuid4()
OWNER = uuid.uuid4()
TARGET = uuid.uuid4()
OUTSIDER = uuid.uuid4()
FLEET = uuid.uuid4()
OTHER_FLEET = uuid.uuid4()


def _item(**extra) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "correlation_key": "dev:x",
        "title": "Do x",
        "description": "Brief",
        "status": "done",
        "priority": "high",
        "owner_agent_id": OWNER,
        "created_by_agent_id": CREATOR,
        "claimed_by_agent_id": None,
        "claimed_by_run_id": None,
        "user_id": None,
        "is_company_managed": True,
        "workspace_id": None,
        "result": "merged abc123",
        "fleet_impact": None,
        "claim_notes": None,
    }
    base.update(extra)
    return SimpleNamespace(**base)


def _sub(agent: uuid.UUID, flt: dict | None = None) -> platform_events.Subscription:
    return platform_events.Subscription(
        assignment_id=uuid.uuid4(),
        agent_id=agent,
        events=("work_item.completed",),
        filter=flt or {},
        scope_all=False,
    )


def _completed(item) -> dict:
    events = wi.update_events(item, previous_status="in_progress", note=None)
    return dict(events)["work_item.completed"]


# -- Normalization ------------------------------------------------------------------


def test_normalize_keeps_the_stored_shape():
    impact = fi.normalize_fleet_impact(
        {"targets": [str(TARGET), str(TARGET).upper()], "kind": "skill", "instructions": "  rewrite s.20  "}
    )
    assert impact == {"targets": [str(TARGET)], "kind": "skill", "instructions": "rewrite s.20"}


def test_no_targets_is_no_impact():
    assert fi.normalize_fleet_impact({}) is None
    assert fi.normalize_fleet_impact({"targets": []}) is None
    assert fi.normalize_fleet_impact(None) is None


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ({"targets": [str(TARGET)]}, "kind is required"),
        ({"targets": [str(TARGET)], "kind": "mood"}, "is not one of"),
        ({"targets": ["not-a-uuid"], "kind": "skill"}, "is not an agent id"),
        ({"targets": [str(TARGET)], "kind": "skill", "extra": 1}, "unknown key"),
        ("text", "must be an object"),
    ],
)
def test_malformed_impact_is_refused_with_the_reason(raw, reason):
    with pytest.raises(ValueError, match=reason):
        fi.normalize_fleet_impact(raw)


# -- Fleet check --------------------------------------------------------------------


@pytest.fixture
def homes(monkeypatch):
    import app.services.fleets as fleets

    table = {
        OWNER: SimpleNamespace(fleet_id=FLEET),
        CREATOR: SimpleNamespace(fleet_id=FLEET),
        TARGET: SimpleNamespace(fleet_id=FLEET),
        OUTSIDER: SimpleNamespace(fleet_id=OTHER_FLEET),
    }

    async def home_fleets_of(_db, agent_ids):
        return {a: table[a] for a in agent_ids if a in table}

    monkeypatch.setattr(fleets, "home_fleets_of", home_fleets_of)
    return table


@pytest.mark.asyncio
async def test_a_target_of_the_owners_fleet_is_accepted(homes):
    impact = fi.normalize_fleet_impact({"targets": [str(TARGET)], "kind": "retest"})
    await fi.check_targets(None, impact, owner_agent_id=OWNER, created_by_agent_id=CREATOR)


@pytest.mark.asyncio
async def test_a_target_outside_the_fleet_is_refused_with_its_id(homes):
    impact = fi.normalize_fleet_impact({"targets": [str(TARGET), str(OUTSIDER)], "kind": "retest"})
    with pytest.raises(ValueError, match=str(OUTSIDER)):
        await fi.check_targets(None, impact, owner_agent_id=OWNER, created_by_agent_id=CREATOR)


@pytest.mark.asyncio
async def test_an_agent_in_no_fleet_is_refused(homes):
    impact = fi.normalize_fleet_impact({"targets": [str(uuid.uuid4())], "kind": "retest"})
    with pytest.raises(ValueError, match="outside the fleet"):
        await fi.check_targets(None, impact, owner_agent_id=OWNER, created_by_agent_id=None)


@pytest.mark.asyncio
async def test_an_item_with_no_fleet_anchor_cannot_impact(homes):
    impact = fi.normalize_fleet_impact({"targets": [str(TARGET)], "kind": "retest"})
    with pytest.raises(ValueError, match="needs an owner or a creator agent in a fleet"):
        await fi.check_targets(None, impact, owner_agent_id=None, created_by_agent_id=uuid.uuid4())


# -- Routing ------------------------------------------------------------------------


def test_completion_reaches_the_target_and_its_filter():
    impact = {"targets": [str(TARGET)], "kind": "skill", "instructions": "rewrite s.20"}
    data = _completed(_item(fleet_impact=impact))
    assert str(TARGET) in data["participant_agent_ids"]
    assert data["fleet_impact"]["kind"] == "skill"
    assert data["result"] == "merged abc123"
    listener = _sub(TARGET, {"fleet_impact.targets": {"contains": str(TARGET)}})
    assert platform_events.mismatch_reason(listener, "work_item.completed", data) is None
    assert payload_filter_reason(data, {"fleet_impact.kind": "skill"}) is None
    assert payload_filter_reason(data, {"fleet_impact.kind": "persona"}) is not None


def test_creator_and_owner_still_receive_it():
    data = _completed(_item(fleet_impact={"targets": [str(TARGET)], "kind": "skill", "instructions": None}))
    for agent in (CREATOR, OWNER):
        assert platform_events.mismatch_reason(_sub(agent), "work_item.completed", data) is None


def test_no_targets_starts_nothing_extra():
    data = _completed(_item())
    assert data["fleet_impact"] is None
    assert str(TARGET) not in data["participant_agent_ids"]
    assert platform_events.mismatch_reason(_sub(TARGET), "work_item.completed", data) is not None


def test_only_the_completion_is_routed_to_the_target():
    item = _item(fleet_impact={"targets": [str(TARGET)], "kind": "skill", "instructions": None})
    events = dict(wi.update_events(item, previous_status="in_progress", note=None))
    assert str(TARGET) not in events["work_item.status_changed"]["participant_agent_ids"]
    assert events["work_item.status_changed"]["fleet_impact"] is None


def test_the_catalog_names_the_filter_keys():
    keys = dict((e["event"], e["filter_keys"]) for e in platform_events.catalog())["work_item.completed"]
    assert "fleet_impact.targets" in keys and "fleet_impact.kind" in keys


# -- Temporary shim (remove with LEGACY_FOLLOW_THROUGH_HEADING) -----------------------


def test_a_follow_through_section_routes_to_the_creator_until_the_field_is_set():
    item = _item(description=f"Brief\n\n{fi.LEGACY_FOLLOW_THROUGH_HEADING}: apply it", owner_agent_id=OWNER)
    impact = fi.effective_fleet_impact(item)
    assert impact == {"targets": [str(CREATOR)], "kind": "capability", "instructions": None, "inferred": True}
    explicit = {"targets": [str(TARGET)], "kind": "skill", "instructions": None}
    assert fi.effective_fleet_impact(_item(description=item.description, fleet_impact=explicit)) == explicit


def test_the_impact_reads_from_a_serialized_item_too():
    """The Operations context reads the serialized dict, not the row."""
    explicit = {"targets": [str(TARGET)], "kind": "skill", "instructions": None}
    assert fi.effective_fleet_impact({"fleet_impact": explicit, "created_by_agent_id": str(CREATOR)}) == explicit
    legacy = {"fleet_impact": None, "created_by_agent_id": str(CREATOR), "description": f"{fi.LEGACY_FOLLOW_THROUGH_HEADING}: x"}
    assert fi.impacted_agent_ids(legacy) == [str(CREATOR)]
    assert fi.effective_fleet_impact({"fleet_impact": None, "description": "Brief"}) is None


# -- Activity note ------------------------------------------------------------------


class _Db:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _query):
        return SimpleNamespace(all=lambda: self.rows)


@pytest.mark.asyncio
async def test_the_completion_writes_one_dated_note_naming_the_agents():
    item = _item(fleet_impact={"targets": [str(TARGET)], "kind": "skill", "instructions": None})
    now = datetime(2026, 11, 4, 0, 30, tzinfo=UTC)
    await fi.note_impact_routed(_Db([(TARGET, "Executive Assistant")]), item, previous_status="in_progress", now=now)
    assert item.claim_notes[-1]["text"] == "impact routed to Executive Assistant at 2026-11-04T00:30:00Z"
    # A second terminal write (done to cancelled) does not repeat it.
    await fi.note_impact_routed(_Db([]), item, previous_status="done", now=now)
    assert len(item.claim_notes) == 1


@pytest.mark.asyncio
async def test_no_note_without_targets():
    item = _item()
    await fi.note_impact_routed(_Db([]), item, previous_status="in_progress", now=datetime.now(UTC))
    assert item.claim_notes is None
