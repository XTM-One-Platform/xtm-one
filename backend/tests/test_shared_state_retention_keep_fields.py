"""Open list items compacted into the overflow key to fit max_chars (#754).

With ``keep_fields`` a retention whose ``archive_when`` keeps every open item
no longer stays over ``max_chars``: after the roll, the longest fields of the
open items move to ``overflow_key`` (an ``overflow://`` pointer left in place),
the largest first, until the value fits. The identifying field, the kept
fields and the fields the terminal rule reads never move.
"""

import uuid
from typing import Any

import pytest

import app.services.shared_state as shared_state_service
from app.core.config import settings
from app.services.shared_state import (
    OVERFLOW_POINTER,
    compact_open_items,
    retention_shape,
    value_size,
)
from tests.test_shared_state_list_ops import COMPANY_AGENT, _Session, _state


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "shared_state_value_soft_limit_chars", 0)
    monkeypatch.setattr(settings, "shared_state_value_hard_limit_chars", 0)
    monkeypatch.setattr(settings, "shared_state_list_max_items", 0)


def _sessions(monkeypatch: pytest.MonkeyPatch, *sessions: Any) -> None:
    queue = list(sessions)
    monkeypatch.setattr("app.core.database.async_session", lambda: queue.pop(0))


def _gaps(count: int) -> list[dict[str, Any]]:
    return [
        {"id": f"g{i}", "state": "queued", "module": "m", "what": "w" * 60, "evidence": "e" * 200} for i in range(count)
    ]


def test_keep_fields_is_checked() -> None:
    policy = retention_shape({"max_chars": 1000, "overflow_key": "o", "keep_fields": [" state ", "module", "state"]})
    assert policy["keep_fields"] == ["state", "module"]
    for bad, word in (
        ({"max_chars": 1000, "overflow_key": "o", "keep_fields": []}, "keep_fields"),
        ({"max_chars": 1000, "overflow_key": "o", "keep_fields": [""]}, "keep_fields"),
        ({"max_chars": 1000, "keep_fields": ["state"]}, "overflow_key"),
        ({"max_item_chars": 600, "overflow_key": "o", "keep_fields": ["state"]}, "max_chars"),
    ):
        with pytest.raises(ValueError, match=word):
            retention_shape(bad)


def test_without_keep_fields_nothing_moves(limits: None) -> None:
    value = {"gaps": _gaps(10)}
    assert compact_open_items(value, {"max_chars": 500, "overflow_key": "o"}) == {}
    assert value["gaps"][0]["evidence"] == "e" * 200


def test_under_the_cap_nothing_moves(limits: None) -> None:
    value = {"gaps": _gaps(2)}
    policy = {"max_chars": 100_000, "overflow_key": "o", "keep_fields": ["state"]}
    assert compact_open_items(value, policy) == {}


def test_the_largest_fields_move_until_the_value_fits(limits: None) -> None:
    value = {"gaps": _gaps(10)}
    cap = value_size(value) - 1000
    policy = {
        "max_chars": cap,
        "overflow_key": "o",
        "keep_fields": ["module"],
        "archive_when": {"state": {"starts_with": "merged"}},
    }
    spilled = compact_open_items(value, policy)
    assert value_size(value) <= cap
    moved = {field for fields in spilled["gaps"].values() for field in fields}
    assert moved == {"evidence"}
    first = next(item for item in value["gaps"] if item["id"] in spilled["gaps"])
    assert first["evidence"] == f"{OVERFLOW_POINTER}o/gaps/{first['id']}#evidence"
    assert spilled["gaps"][first["id"]]["evidence"] == "e" * 200
    assert all(item["state"] == "queued" and item["module"] == "m" for item in value["gaps"])
    untouched = [item for item in value["gaps"] if item["id"] not in spilled["gaps"]]
    assert untouched and untouched[0]["evidence"] == "e" * 200


def test_the_id_kept_and_rule_fields_never_move(limits: None) -> None:
    value = {"gaps": [{"id": "g", "state": "s" * 900, "module": "m" * 900, "what": "w" * 900}]}
    policy = {"max_chars": 10, "overflow_key": "o", "keep_fields": ["module"], "archive_when": {"state": "merged"}}
    spilled = compact_open_items(value, policy)
    assert list(spilled["gaps"]["g"]) == ["what"]
    assert value["gaps"][0]["state"] == "s" * 900 and value["gaps"][0]["module"] == "m" * 900


@pytest.mark.asyncio
async def test_a_write_over_the_cap_with_open_items_lands_under_it(
    monkeypatch: pytest.MonkeyPatch, limits: None
) -> None:
    items = _gaps(10)
    source = _state({"gaps": items})
    cap = value_size({"gaps": items}) - 500
    source.retention = {
        "max_chars": cap,
        "overflow_key": "gaps-overflow",
        "keep_fields": ["state", "module", "what"],
        "archive_when": {"state": {"starts_with": "merged"}},
    }
    overflow = _state({})
    overflow.key = "gaps-overflow"
    _sessions(monkeypatch, _Session(source), _Session(overflow))  # type: ignore[no-untyped-call]
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="harness-gaps",
        append={"gaps": [{"id": "new", "state": "queued", "evidence": "x" * 50}]},
        writer=COMPANY_AGENT,
        is_company_managed=True,
    )
    assert value_size(out["value"]) <= cap
    assert out["spilled_to"] == "gaps-overflow" and out["spilled"]["gaps"] >= 1
    assert "retention_held_open" not in out and "retention_unmatched" not in out
    moved = overflow.value["gaps"]
    assert all(fields == {"evidence": "e" * 200} for fields in moved.values())
    assert len(out["value"]["gaps"]) == 11
