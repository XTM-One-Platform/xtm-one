"""Shared-state lists stay usable at their caps (#1760).

A spill by ``max_item_chars`` never turns a number, a ``keep_fields`` field or
a field ``archive_when`` reads into an ``overflow://`` pointer, so an
``increment`` and a ``where_json`` keep working on the item; ``roll_open``
bounds a list of open items by rolling the oldest of them into the archive
once the terminal ones cannot bring it under its bound.
"""

import pytest

from app.services.shared_state import (
    OVERFLOW_POINTER,
    apply_list_ops,
    list_item_matches,
    retention_shape,
    roll_lists,
    spill_items,
    unmatched_archive_rule,
    value_size,
)


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "shared_state_value_soft_limit_chars",
        "shared_state_value_hard_limit_chars",
        "shared_state_list_max_items",
    ):
        monkeypatch.setattr(f"app.services.shared_state.settings.{name}", 0)


# ── a spill keeps what increments and filters read ────────────────────────


def test_a_spill_keeps_numbers_and_the_fields_archive_when_reads(limits: None) -> None:
    policy = retention_shape(
        {
            "max_item_chars": 120,
            "overflow_key": "gaps-overflow",
            "archive_when": {"state": "merged"},
        }
    )
    value = {"gaps": [{"id": "g1", "state": "queued " + "x" * 60, "count": 123456789, "what": "y" * 200}]}
    spilled = spill_items(value, policy)
    item = value["gaps"][0]
    assert item["count"] == 123456789
    assert not str(item["state"]).startswith(OVERFLOW_POINTER)
    assert str(item["what"]).startswith(OVERFLOW_POINTER)
    assert spilled == {"gaps": {"g1": {"what": "y" * 200}}}
    # The list ops still read the item.
    out, changes = apply_list_ops(value, update={"gaps": {"match": {"id": "g1"}, "increment": {"count": 1}}})
    assert out["gaps"][0]["count"] == 123456790
    assert changes["gaps"]["updated"] == 1
    assert list_item_matches(out["gaps"][0], {"state": {"starts_with": "queued"}})


def test_a_spill_keeps_the_keep_fields_and_a_field_shorter_than_its_pointer(limits: None) -> None:
    policy = retention_shape({"max_item_chars": 90, "overflow_key": "o", "max_chars": 100000, "keep_fields": ["title"]})
    value = {"items": [{"id": "a", "title": "t" * 80, "short": "s", "flag": True, "body": "b" * 80}]}
    spill_items(value, policy)
    item = value["items"][0]
    assert item["title"] == "t" * 80
    assert item["short"] == "s"
    assert item["flag"] is True
    assert str(item["body"]).startswith(OVERFLOW_POINTER)


# ── roll_open bounds a list of open items ─────────────────────────────────


def test_roll_open_needs_an_archive_key_and_is_a_boolean() -> None:
    assert retention_shape({"keep_last": 3, "archive_key": "a", "roll_open": True})["roll_open"] is True
    assert "roll_open" not in retention_shape({"keep_last": 3, "archive_key": "a", "roll_open": False})
    with pytest.raises(ValueError, match="archive_key"):
        retention_shape({"keep_last": 3, "roll_open": True})
    with pytest.raises(ValueError, match="true or false"):
        retention_shape({"keep_last": 3, "archive_key": "a", "roll_open": "yes"})


def test_without_roll_open_open_items_are_held_past_keep_last(limits: None) -> None:
    policy = retention_shape({"keep_last": 2, "archive_key": "a", "archive_when": {"state": "done"}})
    value = {"entries": [{"key": f"k{i}", "state": "open"} for i in range(4)]}
    held: dict[str, int] = {}
    assert roll_lists(value, policy, held=held) == {}
    assert len(value["entries"]) == 4
    assert held == {"entries": 2}


def test_roll_open_rolls_terminal_items_first_then_the_oldest_open(limits: None) -> None:
    policy = retention_shape({"keep_last": 2, "archive_key": "a", "archive_when": {"state": "done"}, "roll_open": True})
    value = {
        "entries": [
            {"key": "k0", "state": "open"},
            {"key": "k1", "state": "open"},
            {"key": "k2", "state": "done"},
            {"key": "k3", "state": "open"},
        ]
    }
    held: dict[str, int] = {}
    rolled = roll_lists(value, policy, held=held)
    assert [i["key"] for i in rolled["entries"]] == ["k2", "k0"]
    assert [i["key"] for i in value["entries"]] == ["k1", "k3"]
    assert held == {}


def test_roll_open_bounds_the_value_by_max_chars_and_keeps_the_appended_items(limits: None) -> None:
    items = [{"key": f"k{i}", "state": "open", "pad": "p" * 50} for i in range(10)]
    cap = value_size({"entries": items[:4]})
    policy = retention_shape(
        {"max_chars": cap, "archive_key": "a", "archive_when": {"state": "done"}, "roll_open": True}
    )
    value = {"entries": list(items)}
    rolled = roll_lists(value, policy, appended={"entries": 1})
    assert value_size(value) <= cap
    assert value["entries"][-1]["key"] == "k9"
    assert [i["key"] for i in rolled["entries"]] == [f"k{i}" for i in range(len(rolled["entries"]))]
    assert unmatched_archive_rule({"entries": items}, policy) is None
