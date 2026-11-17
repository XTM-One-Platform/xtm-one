"""A per-item size cap on the lists of a shared state retention (#12177).

``max_item_chars`` bounds one item of a governed list: with ``overflow_key``
its longest fields move to that key, keyed by the list and the item's id, and
the item keeps a pointer; without it the write is refused with the size.
``overflow_fields`` moves named fields of every item in one call. A write
whose ``archive_when`` matches no item while the value is over ``max_chars``
warns, and every read-back states the size against the soft and hard limits.
"""

import uuid

import pytest

import app.services.shared_state as shared_state_service
from app.services.shared_state import (
    OVERFLOW_POINTER,
    append_at_path,
    apply_list_ops,
    parse_overflow_pointer,
    retention_shape,
    size_caps,
    spill_items,
    unmatched_archive_rule,
    value_size,
)
from tests.test_shared_state_list_ops import COMPANY_AGENT, _Session, _state


@pytest.fixture
def limits(monkeypatch):
    monkeypatch.setattr(shared_state_service.settings, "shared_state_value_soft_limit_chars", 0)
    monkeypatch.setattr(shared_state_service.settings, "shared_state_value_hard_limit_chars", 0)
    monkeypatch.setattr(shared_state_service.settings, "shared_state_list_max_items", 0)


def _sessions(monkeypatch, *sessions):
    queue = list(sessions)
    monkeypatch.setattr("app.core.database.async_session", lambda: queue.pop(0))


# ── the policy shape ───────────────────────────────────────────────────────


def test_the_item_cap_fields_are_checked():
    policy = retention_shape({"max_item_chars": 600, "overflow_key": " gaps-overflow ", "item_key": "slug"})
    assert policy == {"max_item_chars": 600, "overflow_key": "gaps-overflow", "item_key": "slug"}
    assert retention_shape({"overflow_fields": ["evidence"], "overflow_key": "o"})["overflow_fields"] == ["evidence"]
    for bad, word in (
        ({"max_item_chars": 0}, "max_item_chars"),
        ({"max_item_chars": True}, "max_item_chars"),
        ({"overflow_fields": ["evidence"]}, "overflow_key"),
        ({"overflow_fields": []}, "overflow_fields"),
        ({"max_item_chars": 600, "overflow_key": ""}, "overflow_key"),
        ({"max_item_chars": 600, "item_key": " "}, "item_key"),
    ):
        with pytest.raises(ValueError, match=word):
            retention_shape(bad)


# ── spill_items ────────────────────────────────────────────────────────────


def test_a_long_field_moves_to_the_overflow_and_the_item_keeps_a_pointer():
    value = {"gaps": [{"id": "g1", "state": "queued", "evidence": "e" * 5000}, {"id": "g2", "state": "queued"}]}
    spilled = spill_items(value, {"max_item_chars": 600, "overflow_key": "gaps-overflow"})
    assert spilled == {"gaps": {"g1": {"evidence": "e" * 5000}}}
    item = value["gaps"][0]
    assert item["evidence"] == f"{OVERFLOW_POINTER}gaps-overflow/gaps/g1#evidence"
    assert item["state"] == "queued" and value_size(item) <= 600
    assert value["gaps"][1] == {"id": "g2", "state": "queued"}


def test_the_longest_fields_move_first_and_only_until_the_item_fits():
    item = {"id": 7, "a": "a" * 400, "b": "b" * 900, "c": "c" * 50}
    value = {"runs": [item]}
    spilled = spill_items(value, {"max_item_chars": 600, "overflow_key": "o"})
    assert list(spilled["runs"]["7"]) == ["b"]
    assert value["runs"][0]["a"] == "a" * 400


def test_a_pointer_is_never_moved_again():
    value = {"gaps": [{"id": "g1", "evidence": "e" * 2000}]}
    policy = {"max_item_chars": 600, "overflow_key": "o"}
    spill_items(value, policy)
    assert spill_items(value, policy) == {}


def test_without_an_overflow_key_an_item_over_the_cap_is_refused_with_its_size():
    value = {"gaps": [{"id": "g1", "evidence": "e" * 5000}]}
    with pytest.raises(ValueError, match="over the retention max_item_chars of 600"):
        spill_items(value, {"max_item_chars": 600})


def test_an_item_without_its_id_cannot_be_spilled():
    value = {"gaps": [{"slug": "g1", "evidence": "e" * 5000}]}
    with pytest.raises(ValueError, match="no 'id' value"):
        spill_items(value, {"max_item_chars": 600, "overflow_key": "o"})
    spilled = spill_items(value, {"max_item_chars": 600, "overflow_key": "o", "item_key": "slug"})
    assert list(spilled["gaps"]) == ["g1"]


def test_overflow_fields_move_from_every_item_whatever_its_size():
    value = {"gaps": [{"id": "a", "runs": ["r1"]}, {"id": "b", "runs": ["r2"], "state": "x"}, {"id": "c"}]}
    spilled = spill_items(value, {"overflow_fields": ["runs"], "overflow_key": "o"})
    assert spilled == {"gaps": {"a": {"runs": ["r1"]}, "b": {"runs": ["r2"]}}}
    assert value["gaps"][1]["runs"].startswith(OVERFLOW_POINTER)
    assert value["gaps"][2] == {"id": "c"}


def test_lists_outside_the_policy_are_left_alone():
    value = {"gaps": [{"id": "g", "e": "e" * 900}], "notes": [{"id": "n", "e": "e" * 900}]}
    spill_items(value, {"max_item_chars": 600, "overflow_key": "o", "lists": ["gaps"]})
    assert value["notes"][0]["e"] == "e" * 900


# ── the unmatched rule and the caps ─────────────────────────────────────────


def test_an_archive_when_matching_nothing_over_the_cap_is_named():
    value = {"gaps": [{"id": i, "status": "parked, waiting for the queue"} for i in range(20)]}
    rule = {"status": "merged"}
    assert unmatched_archive_rule(value, {"max_chars": 100, "archive_when": rule}) == rule
    assert unmatched_archive_rule(value, {"max_chars": 100_000, "archive_when": rule}) is None
    value["gaps"][0]["status"] = "merged"
    assert unmatched_archive_rule(value, {"max_chars": 100, "archive_when": rule}) is None
    assert unmatched_archive_rule(value, {"max_chars": 100}) is None


def test_size_caps_reads_the_platform_limits(monkeypatch):
    monkeypatch.setattr(shared_state_service.settings, "shared_state_value_soft_limit_chars", 32768)
    monkeypatch.setattr(shared_state_service.settings, "shared_state_value_hard_limit_chars", 65536)
    caps = size_caps({"a": [1]})
    assert caps == {"size_chars": value_size({"a": [1]}), "soft_limit_chars": 32768, "hard_limit_chars": 65536}


# ── the write ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_append_over_the_item_cap_lands_in_the_overflow_key(monkeypatch, limits):
    source = _state({"gaps": [{"id": "old", "state": "queued"}]})
    source.retention = {"max_item_chars": 600, "overflow_key": "gaps-overflow"}
    overflow = _state({})
    overflow.key = "gaps-overflow"
    _sessions(monkeypatch, _Session(source), _Session(overflow))
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="harness-gaps",
        append={"gaps": [{"id": "new", "state": "queued", "evidence": "e" * 5000}]},
        writer=COMPANY_AGENT,
        is_company_managed=True,
    )
    assert out["value"]["gaps"][1]["evidence"] == f"{OVERFLOW_POINTER}gaps-overflow/gaps/new#evidence"
    assert overflow.value == {"gaps": {"new": {"evidence": "e" * 5000}}}
    assert out["spilled"] == {"gaps": 1} and out["spilled_to"] == "gaps-overflow"
    assert out["size_caps"]["size_chars"] == value_size(out["value"])


@pytest.mark.asyncio
async def test_setting_the_policy_compacts_the_items_already_stored(monkeypatch, limits):
    source = _state({"gaps": [{"id": "a", "evidence": "e" * 3000}, {"id": "b", "evidence": "short"}]})
    overflow = _state({})
    overflow.key = "gaps-overflow"
    _sessions(monkeypatch, _Session(source), _Session(overflow))
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="harness-gaps",
        retention={"max_item_chars": 600, "overflow_key": "gaps-overflow"},
        writer=COMPANY_AGENT,
        is_company_managed=True,
    )
    assert out["value"]["gaps"][0]["evidence"].startswith(OVERFLOW_POINTER)
    assert out["value"]["gaps"][1]["evidence"] == "short"
    assert overflow.value == {"gaps": {"a": {"evidence": "e" * 3000}}}


@pytest.mark.asyncio
async def test_an_item_over_the_cap_without_overflow_is_refused_and_nothing_written(monkeypatch, limits):
    source = _state({"gaps": []})
    source.retention = {"max_item_chars": 600}
    _sessions(monkeypatch, _Session(source))
    with pytest.raises(ValueError, match="max_item_chars of 600"):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="harness-gaps",
            append={"gaps": [{"id": "x", "evidence": "e" * 5000}]},
            writer=COMPANY_AGENT,
            is_company_managed=True,
        )
    assert source.value == {"gaps": []}


@pytest.mark.asyncio
async def test_a_write_whose_archive_when_matches_nothing_over_the_cap_warns(monkeypatch, limits):
    items = [{"id": i, "status": "parked", "text": "x" * 40} for i in range(10)]
    source = _state({"gaps": items})
    source.retention = {"max_chars": 200, "archive_when": {"status": "merged"}}
    _sessions(monkeypatch, _Session(source))
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="harness-gaps",
        append={"gaps": [{"id": 99, "status": "parked"}]},
        writer=COMPANY_AGENT,
        is_company_managed=True,
    )
    assert out["retention_unmatched"] == {"status": "merged"}
    assert out.get("rolled") is None


# ── an update append on a field moved to the overflow key (#12301) ─────────


def test_a_pointer_names_its_overflow_key_and_the_path_under_it() -> None:
    pointer = f"{OVERFLOW_POINTER}workspace:w1:dev:gaps/x/gaps/g1#runs"
    assert parse_overflow_pointer(pointer, "gaps") == ("workspace:w1:dev:gaps/x", ["gaps", "g1", "runs"])
    assert parse_overflow_pointer(pointer, "prs") is None
    assert parse_overflow_pointer("runs", "gaps") is None
    assert parse_overflow_pointer(f"{OVERFLOW_POINTER}k/gaps/g1", "gaps") is None


def test_append_at_path_adds_the_absent_items_and_creates_the_path() -> None:
    value, added = append_at_path({"gaps": {"g1": {"runs": ["r1"]}}}, ["gaps", "g1", "runs"], ["r1", "r2"])
    assert value == {"gaps": {"g1": {"runs": ["r1", "r2"]}}} and added == 1
    value, added = append_at_path({}, ["gaps", "g2", "runs"], ["r3"])
    assert value == {"gaps": {"g2": {"runs": ["r3"]}}} and added == 1
    with pytest.raises(ValueError, match="not a list"):
        append_at_path({"gaps": {"g1": {"runs": "text"}}}, ["gaps", "g1", "runs"], ["r"])


def test_an_update_append_on_a_pointer_keeps_it_and_hands_the_items_over() -> None:
    pointer = f"{OVERFLOW_POINTER}gaps-overflow/gaps/g1#runs"
    value = {"gaps": [{"id": "g1", "count": 3, "runs": pointer}]}
    handed: list[dict[str, object]] = []
    out, changes = apply_list_ops(
        value,
        update={"gaps": {"match": {"id": "g1"}, "increment": {"count": 1}, "append": {"runs": ["r9"]}}},
        overflow_appends=handed,
    )
    assert out["gaps"][0] == {"id": "g1", "count": 4, "runs": pointer}
    assert changes["gaps"]["updated"] == 1
    assert handed == [{"pointer": pointer, "list": "gaps", "field": "runs", "items": ["r9"]}]


@pytest.mark.asyncio
async def test_an_update_append_on_an_overflowed_field_lands_in_the_overflow_key(
    monkeypatch: pytest.MonkeyPatch, limits: None
) -> None:
    """The reproduction: before #12301 this write was refused ('is not a list') and the count was lost."""
    pointer = f"{OVERFLOW_POINTER}gaps-overflow/gaps/g1#runs"
    source = _state({"gaps": [{"id": "g1", "count": 3, "runs": pointer}]})
    source.retention = {"overflow_fields": ["runs"], "overflow_key": "gaps-overflow"}
    overflow = _state({"gaps": {"g1": {"runs": ["r1", "r2"]}}})
    overflow.key = "gaps-overflow"
    overflow_session = _Session(overflow)
    _sessions(monkeypatch, _Session(source), overflow_session)
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="harness-gaps",
        update={"gaps": {"match": {"id": "g1"}, "increment": {"count": 1}, "append": {"runs": ["r2", "r3"]}}},
        writer=COMPANY_AGENT,
        is_company_managed=True,
    )
    assert out["value"]["gaps"][0] == {"id": "g1", "count": 4, "runs": pointer}
    assert overflow.value == {"gaps": {"g1": {"runs": ["r1", "r2", "r3"]}}}
    assert overflow_session.committed
    assert out["overflow_appended"] == {"gaps-overflow": 1}


@pytest.mark.asyncio
async def test_a_pointer_to_the_entry_itself_is_refused_and_nothing_written(
    monkeypatch: pytest.MonkeyPatch, limits: None
) -> None:
    pointer = f"{OVERFLOW_POINTER}harness-gaps/gaps/g1#runs"
    source = _state({"gaps": [{"id": "g1", "count": 3, "runs": pointer}]})
    session = _Session(source)
    _sessions(monkeypatch, session)
    with pytest.raises(ValueError, match="cannot be appended to"):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="harness-gaps",
            update={"gaps": {"match": {"id": "g1"}, "increment": {"count": 1}, "append": {"runs": ["r3"]}}},
            writer=COMPANY_AGENT,
            is_company_managed=True,
        )
    assert not session.committed
