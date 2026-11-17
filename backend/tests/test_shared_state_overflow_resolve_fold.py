"""Overflow pointers resolved on read and duplicate list items folded on write (#12320).

A retention ``max_item_chars`` moves long fields to its ``overflow_key`` and
leaves ``overflow://KEY/LIST/ITEM#FIELD`` in the item; ``get_shared_state``
with ``resolve_overflow`` follows those pointers. ``set_shared_state``
``fold_json`` merges the items of a list sharing an identity on the server.
"""

from __future__ import annotations

import uuid

import pytest

import app.services.builtin_tools as builtin_tools_module
from app.services.shared_state import (
    _fold_shape,
    apply_list_ops,
    fold_list_items,
    overflow_pointer_keys,
    resolve_overflow_pointers,
    spill_items,
    split_overflow_pointer,
)
from app.tools.base import ToolCall

# ── pointers ──────────────────────────────────────────────────────────────


def test_a_pointer_parses_into_key_list_item_and_field():
    assert split_overflow_pointer("overflow://gaps-overflow/gaps/g1#evidence") == (
        "gaps-overflow",
        "gaps",
        "g1",
        "evidence",
    )
    # A key holding a slash still parses: list and item are the last segments.
    assert split_overflow_pointer("overflow://ws:1/a/gaps/g1#evidence")[0] == "ws:1/a"


@pytest.mark.parametrize(
    "value",
    ["plain text", "overflow://key/gaps#evidence", "overflow://key/gaps/g1", "overflow://key/gaps/g1#", 12, None],
)
def test_a_value_that_is_not_a_whole_pointer_parses_to_none(value):
    assert split_overflow_pointer(value) is None


def test_the_pointer_a_spill_writes_resolves_back_to_the_moved_field():
    long_text = "x" * 900
    value = {"gaps": [{"id": "g1", "evidence": long_text, "count": 2}]}
    spilled = spill_items(value, {"max_item_chars": 600, "overflow_key": "gaps-overflow"})
    assert value["gaps"][0]["evidence"].startswith("overflow://")
    assert overflow_pointer_keys(value) == ["gaps-overflow"]

    resolved, done, missing = resolve_overflow_pointers(value, {"gaps-overflow": spilled})
    assert resolved == {"gaps": [{"id": "g1", "evidence": long_text, "count": 2}]}
    assert (done, missing) == (1, 0)
    # The stored value is never changed by a read.
    assert value["gaps"][0]["evidence"].startswith("overflow://")


def test_a_pointer_whose_key_item_or_field_is_absent_stays_a_pointer_and_is_counted():
    value = {
        "gaps": [
            {"id": "g1", "evidence": "overflow://o/gaps/g1#evidence"},
            {"id": "g2", "evidence": "overflow://o/gaps/g2#evidence"},
            {"id": "g3", "evidence": "overflow://gone/gaps/g3#evidence"},
        ]
    }
    resolved, done, missing = resolve_overflow_pointers(value, {"o": {"gaps": {"g1": {"evidence": "found"}}}})
    assert [g["evidence"] for g in resolved["gaps"]] == [
        "found",
        "overflow://o/gaps/g2#evidence",
        "overflow://gone/gaps/g3#evidence",
    ]
    assert (done, missing) == (1, 2)


# ── fold ──────────────────────────────────────────────────────────────────


def test_duplicates_fold_into_the_first_item_with_lists_unioned_and_sums_added():
    items = [
        {"id": "a", "count": 2, "runs": ["r1", "r2"], "state": "queued"},
        {"id": "b", "count": 1},
        {"id": "a", "count": 3, "runs": ["r2", "r3"], "state": "merged", "pr": 12},
        "a scalar is kept",
        {"no_id": True},
    ]
    out, folded = fold_list_items(items, ("id",), ("count",))
    assert folded == 1
    assert out == [
        {"id": "a", "count": 5, "runs": ["r1", "r2", "r3"], "state": "queued", "pr": 12},
        {"id": "b", "count": 1},
        "a scalar is kept",
        {"no_id": True},
    ]


def test_a_count_is_not_added_unless_named_in_sum():
    out, _ = fold_list_items([{"id": "a", "count": 2}, {"id": "a", "count": 3}], ("id",))
    assert out == [{"id": "a", "count": 2}]


def test_fold_runs_after_the_appends_and_is_counted_in_the_changes():
    value = {"gaps": [{"id": "a", "count": 1, "runs": ["r1"]}, {"slug": "a", "count": 4}]}
    out, changes = apply_list_ops(
        value,
        append={"gaps": [{"id": "a", "count": 2, "runs": ["r9"]}]},
        fold={"gaps": {"by": "id", "sum": ["count"]}},
    )
    assert out["gaps"] == [{"id": "a", "count": 3, "runs": ["r1", "r9"]}, {"slug": "a", "count": 4}]
    assert changes["gaps"]["folded"] == 1
    assert changes["gaps"]["appended"] == 1


def test_fold_of_a_missing_list_folds_nothing_and_a_non_list_is_refused():
    out, changes = apply_list_ops({"x": 1}, fold={"gaps": "id"})
    assert changes["gaps"]["folded"] == 0 and "gaps" not in out
    with pytest.raises(ValueError, match="not a list"):
        apply_list_ops({"gaps": {"a": 1}}, fold={"gaps": "id"})


@pytest.mark.parametrize(
    "fold",
    [{"gaps": ""}, {"gaps": []}, {"gaps": {"by": "id", "sum": "count"}}, {"gaps": {"by": "id", "max": 1}}, ["gaps"]],
)
def test_a_malformed_fold_is_refused(fold):
    with pytest.raises(ValueError):
        _fold_shape(fold)


def test_a_checked_fold_checks_again_unchanged():
    once = _fold_shape({"gaps": {"by": ["repository", "number"], "sum": ["count"]}})
    assert _fold_shape(once) == once


# ── the tool ──────────────────────────────────────────────────────────────


async def test_get_shared_state_resolves_pointers_through_the_overflow_key(monkeypatch):
    from app.services import shared_state

    states = {
        "gaps": {
            "key": "gaps",
            "status": "active",
            "value": {"gaps": [{"id": "g1", "evidence": "overflow://gaps-overflow/gaps/g1#evidence"}]},
        },
        "gaps-overflow": {
            "key": "gaps-overflow",
            "status": "active",
            "value": {"gaps": {"g1": {"evidence": "the long evidence"}}},
        },
    }
    reads: list[str] = []

    async def _get_state(*, key, **_kwargs):
        reads.append(key)
        return states.get(key)

    monkeypatch.setattr(shared_state, "get_state", _get_state)
    _defs, executor = builtin_tools_module.build_shared_state_tools(None, str(uuid.uuid4()), str(uuid.uuid4()))

    plain = await executor.execute_tool(
        ToolCall(id="a", name="get_shared_state", arguments={"key": "gaps", "path": "gaps"})
    )
    assert "overflow://gaps-overflow" in plain.content
    assert reads == ["gaps"]

    resolved = await executor.execute_tool(
        ToolCall(id="b", name="get_shared_state", arguments={"key": "gaps", "path": "gaps", "resolve_overflow": True})
    )
    assert "the long evidence" in resolved.content
    assert "overflow://" not in resolved.content.split("\n", 1)[1]
    assert "1 pointer(s) resolved from 1 key(s)" in resolved.content
    assert reads[1:] == ["gaps", "gaps-overflow"]


async def test_set_shared_state_rejects_a_fold_json_that_is_not_an_object():
    _defs, executor = builtin_tools_module.build_shared_state_tools(None, str(uuid.uuid4()), str(uuid.uuid4()))
    result = await executor.execute_tool(
        ToolCall(id="c", name="set_shared_state", arguments={"key": "gaps", "fold_json": '["gaps"]'})
    )
    text = result.content if hasattr(result, "content") else str(result)
    assert "fold_json must be a JSON object" in text
