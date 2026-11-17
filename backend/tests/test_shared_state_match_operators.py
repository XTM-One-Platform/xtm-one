"""Operator predicates in shared state list matches (issue #9228).

A match value of ``get_shared_state`` ``where_json``, ``set_shared_state``
``remove_json`` and the ``match`` of ``update_json`` may be an operator object
of the event filter grammar (``integration_event_filters``): ``eq``, ``ne``,
``in``, ``not_in``, ``starts_with``, ``ends_with``, ``contains``,
``matches``, ``exists``. A plain value keeps meaning equality on a present
field; a bad operator object is refused before anything is written.
"""

import pytest

from app.services.shared_state import apply_list_ops, list_item_matches, list_match_error

from .test_shared_state import _call, _open_prs_executor

WATCHES = [
    {"key": "a", "retest_status": "PASS on build 1", "tags": ["ui", "deploy"], "n": 1},
    {"key": "b", "retest_status": "PENDING", "retest": "open it", "tags": ["deploy"], "n": 2},
    {"key": "c", "retest_status": "PASS", "n": 3},
    {"key": "d", "retest_status": "FAIL: PASS expected", "retest": "x", "n": 4},
    {"key": "e", "retest_status": None, "retest": "y", "n": 5},
]


def _keys(match: dict) -> list[str]:
    return [w["key"] for w in WATCHES if list_item_matches(w, match)]


@pytest.mark.parametrize(
    ("match", "expected"),
    [
        ({"retest_status": {"eq": "PASS"}}, ["c"]),
        ({"retest_status": {"ne": "PENDING"}}, ["a", "c", "d", "e"]),
        ({"n": {"in": [1, 4]}}, ["a", "d"]),
        ({"n": {"not_in": [1, 4]}}, ["b", "c", "e"]),
        ({"retest_status": {"starts_with": "PASS"}}, ["a", "c"]),
        ({"retest_status": {"starts_with": ["PEND", "FAIL"]}}, ["b", "d"]),
        ({"retest_status": {"ends_with": "expected"}}, ["d"]),
        ({"retest_status": {"contains": "PASS"}}, ["a", "c", "d"]),
        ({"tags": {"contains": "ui"}}, ["a"]),
        ({"retest_status": {"matches": "PASS( on build \\d+)?"}}, ["a", "c"]),
        ({"retest": {"exists": False}}, ["a", "c"]),
        ({"retest": {"exists": True}}, ["b", "d", "e"]),
        # null counts as absent, as in a trigger filter.
        ({"retest_status": {"exists": False}}, ["e"]),
        # Several operators and several fields must all hold.
        ({"retest_status": {"starts_with": "PASS", "ne": "PASS"}}, ["a"]),
        ({"retest_status": {"starts_with": "PASS"}, "retest": {"exists": False}}, ["a", "c"]),
    ],
)
def test_each_operator_selects_the_items_it_names(match, expected):
    assert _keys(match) == expected


def test_a_plain_value_keeps_meaning_equality():
    assert _keys({"retest_status": "PASS"}) == ["c"]
    # A plain list is equality with that list, never any-of.
    assert _keys({"tags": ["deploy"]}) == ["b"]
    # A plain object that is not an operator object is equality too.
    item = {"meta": {"a": 1, "b": 2}}
    assert list_item_matches(item, {"meta": {"a": 1, "b": 2}})
    assert not list_item_matches(item, {"meta": {"a": 1}})
    # A missing field never equals a value, None included.
    assert not list_item_matches({"x": 1}, {"y": None})
    # A scalar item never matches an object match.
    assert not list_item_matches("PASS", {"retest_status": {"exists": False}})


@pytest.mark.parametrize(
    "match",
    [
        {"s": {"startswith": "PASS"}},
        {"s": {"starts_with": "PASS", "other": 1}},
        {"s": {"in": "PASS"}},
        {"s": {"exists": "no"}},
        {"s": {"matches": "(a+)+"}},
    ],
)
def test_a_bad_operator_object_is_refused_naming_the_operators(match):
    error = list_match_error(match)
    assert error is not None
    assert "starts_with" in error and "exists" in error


def test_remove_with_starts_with_drops_only_the_matching_items_and_counts_them():
    out, changes = apply_list_ops(
        {"entries": list(WATCHES)},
        remove={"entries": [{"retest_status": {"starts_with": "PASS"}}, {"retest": {"exists": False}}]},
    )
    assert [w["key"] for w in out["entries"]] == ["b", "d", "e"]
    assert changes["entries"]["removed"] == 2


def test_update_match_takes_operators():
    out, changes = apply_list_ops(
        {"entries": list(WATCHES)},
        update={"entries": {"match": {"retest_status": {"starts_with": "PASS"}}, "set": {"closed": True}}},
    )
    assert [w["key"] for w in out["entries"] if w.get("closed")] == ["a", "c"]
    assert changes["entries"]["updated"] == 2


@pytest.mark.parametrize("ops", ["remove", "update"])
def test_a_bad_operator_in_a_write_is_refused_and_nothing_changes(ops):
    value = {"entries": list(WATCHES)}
    bad = {"retest_status": {"startswith": "PASS"}}
    args = (
        {"remove": {"entries": [bad]}}
        if ops == "remove"
        else {"update": {"entries": {"match": bad, "set": {"x": 1}}}}
    )
    with pytest.raises(ValueError, match="Operators:"):
        apply_list_ops(value, **args)
    assert value == {"entries": WATCHES}


@pytest.mark.asyncio
async def test_where_json_takes_operators(monkeypatch):
    executor = await _open_prs_executor(monkeypatch)
    result = await _call(
        executor,
        "get_shared_state",
        key="dev:open-prs",
        path="prs",
        where_json='{"branch": {"in": ["feat/x-1", "feat/x-2"]}}',
    )
    assert "where_json: 2 of 80 item(s) match" in result.content
    absent = await _call(
        executor, "get_shared_state", key="dev:open-prs", path="prs", where_json='{"hold": {"exists": false}}'
    )
    assert "where_json: 80 of 80 item(s) match" in absent.content


@pytest.mark.asyncio
async def test_where_json_refuses_an_unknown_operator(monkeypatch):
    executor = await _open_prs_executor(monkeypatch)
    result = await _call(
        executor,
        "get_shared_state",
        key="dev:open-prs",
        path="prs",
        where_json='{"branch": {"startswith": "feat/"}}',
    )
    assert result.content.startswith("Error: where_json:")
    assert "starts_with" in result.content
