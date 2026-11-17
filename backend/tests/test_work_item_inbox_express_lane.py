"""Work-item inbox express lane: ``trigger_config.express_lane`` (#11082).

Items whose correlation key starts with a lane prefix may run up to ``slots``
runs above ``max_concurrent_runs``, so a short critical-path item never waits
behind long runs holding every regular slot; every other item still stops at
the regular cap.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.services.autonomous as autonomous_mod
import app.services.work_item as wi_service
from app.services import assignment_dispatch
from app.services.builtin_tools.agent_builder import work_item_trigger_error


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _QueueSession:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, *_a, **_k):
        return self._results.pop(0)

    async def commit(self):
        pass

    async def rollback(self):
        pass


def _db(trigger_config):
    agent_id = uuid.uuid4()
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        enabled=True,
        name="Inbox",
        trigger_type="work_item",
        trigger_config=trigger_config,
    )
    agent = SimpleNamespace(id=agent_id, name="Agent", enabled=True, is_company_managed=False)
    return _QueueSession([_ScalarsResult([assignment]), _ScalarsResult([agent])])


class _Runs:
    def __init__(self, active=0):
        self.active = active
        self.triggered: list[str] = []
        self.caps: list[int] = []

    async def count(self, _db, _assignment_id):
        return self.active

    async def has_active(self, _db, _assignment_id):
        return self.active > 0

    async def trigger(self, **kwargs):
        cap = kwargs.get("max_active_runs", 1)
        self.caps.append(cap)
        if self.active >= cap:
            return None
        self.active += 1
        self.triggered.append(kwargs["payload"]["work_item_id"])
        return f"run-{len(self.triggered)}"


def _patch(monkeypatch, runs, items):
    claimed: set[str] = set()
    listings: list[str | None] = []

    async def _list(**kwargs):
        prefix = kwargs.get("correlation_prefix")
        listings.append(prefix)
        rows = [i for i in items if i["id"] not in claimed]
        if prefix:
            rows = [i for i in rows if (i["correlation_key"] or "").lower().startswith(prefix.lower())]
        return rows[: kwargs.get("limit", 100)]

    async def _claim(work_item_id, **_k):
        if work_item_id in claimed:
            return {"claimed": False}
        claimed.add(work_item_id)
        return {"claimed": True}

    async def _release(*, work_item_id, **_k):
        claimed.discard(work_item_id)

    async def _record(*_a, **_k):
        return None

    async def _no_lease(_item):
        return False

    monkeypatch.setattr(autonomous_mod, "has_active_run", runs.has_active)
    monkeypatch.setattr(autonomous_mod, "trigger_assignment", runs.trigger)
    monkeypatch.setattr(autonomous_mod, "_held_by_a_live_lease", _no_lease)
    monkeypatch.setattr("app.services.assignment_engine.count_active_runs", runs.count)
    monkeypatch.setattr(wi_service, "list_dispatchable_work_items", _list)
    monkeypatch.setattr(wi_service, "claim_work_item", _claim)
    monkeypatch.setattr(wi_service, "release_work_item_claim", _release)
    monkeypatch.setattr(wi_service, "record_claim_run", _record)
    return claimed, listings


def _item(item_id, key):
    return {"id": item_id, "correlation_key": key, "title": item_id}


LANE = {"correlation_prefixes": ["dev:ux-verdict-"], "slots": 2}


@pytest.mark.asyncio
async def test_full_inbox_still_claims_an_express_item(monkeypatch):
    """Every regular slot is taken: the verdict item starts, the coding item waits."""
    runs = _Runs(active=10)
    items = [_item("code", "dev:long-feature"), _item("verdict", "dev:ux-verdict-11018-comment")]
    claimed, _ = _patch(monkeypatch, runs, items)

    db = _db({"max_concurrent_runs": 10, "express_lane": LANE})
    assert await autonomous_mod._process_work_item_assignments(db) == 1
    assert runs.triggered == ["verdict"]
    assert runs.caps == [12]
    assert claimed == {"verdict"}


@pytest.mark.asyncio
async def test_express_slots_are_bounded(monkeypatch):
    """Two express slots: a third verdict item waits for one of them."""
    runs = _Runs(active=10)
    items = [_item(f"v{i}", f"dev:ux-verdict-{i}-comment") for i in range(3)]
    _patch(monkeypatch, runs, items)

    db = _db({"max_concurrent_runs": 10, "express_lane": LANE})
    assert await autonomous_mod._process_work_item_assignments(db) == 2
    assert runs.triggered == ["v0", "v1"]
    assert runs.active == 12


@pytest.mark.asyncio
async def test_free_regular_slots_keep_the_usual_order(monkeypatch):
    """With a free regular slot, the first item in dispatch order takes it, express or not."""
    runs = _Runs(active=9)
    items = [_item("code", "dev:long-feature"), _item("verdict", "dev:ux-verdict-1-comment")]
    _patch(monkeypatch, runs, items)

    db = _db({"max_concurrent_runs": 10, "express_lane": LANE})
    assert await autonomous_mod._process_work_item_assignments(db) == 2
    assert runs.triggered == ["code", "verdict"]
    assert runs.caps == [10, 12]


@pytest.mark.asyncio
async def test_no_lane_full_inbox_claims_nothing(monkeypatch):
    runs = _Runs(active=10)
    claimed, _ = _patch(monkeypatch, runs, [_item("verdict", "dev:ux-verdict-1-comment")])

    db = _db({"max_concurrent_runs": 10})
    assert await autonomous_mod._process_work_item_assignments(db) == 0
    assert claimed == set()


@pytest.mark.asyncio
async def test_serial_inbox_with_a_lane(monkeypatch):
    """A serial inbox (cap 1) busy on a long run still starts an express item."""
    runs = _Runs(active=1)
    items = [_item("code", "dev:long"), _item("verdict", "dev:ux-verdict-2-comment")]
    _patch(monkeypatch, runs, items)

    db = _db({"express_lane": {"correlation_prefixes": ["dev:ux-verdict-"], "slots": 1}})
    assert await autonomous_mod._process_work_item_assignments(db) == 1
    assert runs.triggered == ["verdict"]
    assert runs.caps == [2]


@pytest.mark.asyncio
async def test_express_listing_stays_inside_the_inbox_prefix(monkeypatch):
    """An inbox limited to 'ops:' never lists a lane prefix outside it."""
    runs = _Runs(active=2)
    items = [_item("verdict", "dev:ux-verdict-3-comment")]
    claimed, listings = _patch(monkeypatch, runs, items)

    db = _db({"max_concurrent_runs": 2, "correlation_prefix": "ops:", "express_lane": LANE})
    assert await autonomous_mod._process_work_item_assignments(db) == 0
    assert claimed == set()
    assert listings == []


@pytest.mark.parametrize(
    ("lane", "inbox", "expected"),
    [
        ("dev:ux-verdict-", None, "dev:ux-verdict-"),
        ("dev:ux-verdict-", "dev:", "dev:ux-verdict-"),
        ("dev:", "dev:ux-", "dev:ux-"),
        ("dev:ux-verdict-", "ops:", None),
    ],
)
def test_express_prefix(lane, inbox, expected):
    assert autonomous_mod._express_prefix(lane, inbox) == expected


def test_lane_matches_case_insensitively():
    lane = assignment_dispatch.work_item_express_lane({"express_lane": LANE})
    assert lane is not None and lane.slots == 2
    assert lane.matches("DEV:UX-VERDICT-1-comment")
    assert not lane.matches("dev:long")
    assert not lane.matches(None)


@pytest.mark.parametrize(
    "lane",
    [
        {"correlation_prefixes": ["dev:ux-verdict-"], "slots": 1},
        {"correlation_prefixes": ["a", "b"], "slots": 5},
        None,
    ],
)
def test_valid_express_lane(lane):
    config = {"max_concurrent_runs": 10, "express_lane": lane}
    assert assignment_dispatch.work_item_concurrency_error(config) is None
    assert work_item_trigger_error("work_item", config) is None


@pytest.mark.parametrize(
    "lane",
    [
        "dev:",
        [],
        {"correlation_prefixes": [], "slots": 1},
        {"correlation_prefixes": ["dev:"], "slots": 0},
        {"correlation_prefixes": ["dev:"], "slots": 6},
        {"correlation_prefixes": ["dev:"], "slots": True},
        {"correlation_prefixes": ["dev:"], "slots": "2"},
        {"correlation_prefixes": [" "], "slots": 1},
        {"correlation_prefixes": "dev:", "slots": 1},
        {"correlation_prefixes": [f"p{i}" for i in range(11)], "slots": 1},
        {"correlation_prefixes": ["dev:"], "slots": 1, "other": 1},
    ],
)
def test_invalid_express_lane_is_refused(lane):
    config = {"express_lane": lane}
    error = assignment_dispatch.work_item_concurrency_error(config)
    assert error is not None and "express_lane" in error
    assert assignment_dispatch.work_item_express_lane(config) is None
    assert "express_lane" in (work_item_trigger_error("work_item", config) or "")


def test_rest_create_refuses_a_bad_express_lane():
    from pydantic import ValidationError

    from app.api.v1.schemas.assignment import CreateAssignmentRequest

    with pytest.raises(ValidationError, match="express_lane"):
        CreateAssignmentRequest(
            name="Inbox",
            agent_id=str(uuid.uuid4()),
            prompt="Work the item.",
            trigger_type="work_item",
            trigger_config={"express_lane": {"correlation_prefixes": ["dev:"], "slots": 9}},
        )


def test_express_lane_travels_with_a_copy():
    from app.models.agent_copy import TRIGGER_CONFIG_PORTABLE_KEYS

    assert "express_lane" in TRIGGER_CONFIG_PORTABLE_KEYS
