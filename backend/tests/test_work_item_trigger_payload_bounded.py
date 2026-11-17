"""The ``work_item`` trigger payload bounds a long description (#10572).

The payload sits in the inbox run's first message and is re-read at every
iteration: a long-lived item's notes must not ride along whole. It carries the
newest notes (the head) and the brief (the tail), the total and the offset
``get_work_item`` reads the rest from.
"""

from types import SimpleNamespace

import pytest

import app.services.autonomous as autonomous_mod
import app.services.work_item as wi_service
from app.services.work_item import (
    TRIGGER_CLAIM_NOTES_KEPT,
    TRIGGER_DESCRIPTION_HEAD_CHARS,
    TRIGGER_DESCRIPTION_TAIL_CHARS,
    bounded_trigger_item,
)


def _long_description() -> str:
    notes = "N" * 200_000
    return "newest note\n" + notes + "\nbrief: do the thing"


def test_short_description_is_kept_whole():
    item = {"id": "wi-1", "title": "t", "description": "a short brief"}
    assert bounded_trigger_item(item) is item


def test_description_at_the_bound_is_kept_whole():
    text = "x" * (TRIGGER_DESCRIPTION_HEAD_CHARS + TRIGGER_DESCRIPTION_TAIL_CHARS)
    item = {"id": "wi-1", "description": text}
    assert bounded_trigger_item(item)["description"] == text


def test_long_description_keeps_head_and_brief_under_ten_thousand_chars():
    description = _long_description()
    item = {"id": "wi-1", "title": "t", "description": description, "payload": {"k": 1}}

    bounded = bounded_trigger_item(item)

    assert len(bounded["description"]) < 10_000
    assert bounded["description"].startswith("newest note")
    assert bounded["description"].endswith("brief: do the thing")
    assert bounded["description_total_chars"] == len(description)
    assert f"offset={TRIGGER_DESCRIPTION_HEAD_CHARS}" in bounded["description"]
    assert f"{len(description):,}" in bounded["description"]
    assert bounded["title"] == "t"
    assert bounded["payload"] == {"k": 1}
    # The caller's dict is not changed.
    assert item["description"] == description
    assert "description_total_chars" not in item


@pytest.mark.parametrize("item", [None, {"id": "wi-1"}, {"id": "wi-1", "description": None}])
def test_missing_description_passes_through(item):
    assert bounded_trigger_item(item) == item


def _claim_notes(count: int) -> list[dict]:
    return [{"at": f"t{i}", "text": f"note {i}"} for i in range(count)]


def test_long_claim_notes_keep_the_newest_and_the_count():
    notes = _claim_notes(300)
    item = {"id": "wi-1", "description": "brief", "claim_notes": notes}

    bounded = bounded_trigger_item(item)

    assert TRIGGER_CLAIM_NOTES_KEPT == 10
    assert bounded["claim_notes"] == notes[-10:]
    assert bounded["claim_notes"][-1]["text"] == "note 299"
    assert bounded["claim_notes_total"] == 300
    assert bounded["description"] == "brief"
    assert "description_total_chars" not in bounded
    # The caller's dict is not changed.
    assert len(item["claim_notes"]) == 300
    assert "claim_notes_total" not in item


@pytest.mark.parametrize("count", [0, 1, TRIGGER_CLAIM_NOTES_KEPT])
def test_claim_notes_within_the_cap_are_kept_whole(count):
    item = {"id": "wi-1", "description": "brief", "claim_notes": _claim_notes(count)}
    assert bounded_trigger_item(item) is item


def test_long_description_and_long_claim_notes_are_both_bounded():
    item = {"id": "wi-1", "description": _long_description(), "claim_notes": _claim_notes(50)}

    bounded = bounded_trigger_item(item)

    assert len(bounded["description"]) < 10_000
    assert len(bounded["claim_notes"]) == TRIGGER_CLAIM_NOTES_KEPT
    assert bounded["claim_notes_total"] == 50


@pytest.mark.asyncio
async def test_inbox_dispatch_sends_the_bounded_item(monkeypatch):
    captured: dict = {}

    async def _trigger(**kwargs):
        captured["payload"] = kwargs["payload"]
        return "run-1"

    async def _release(**_kwargs):
        raise AssertionError("a started run keeps its claim")

    monkeypatch.setattr(autonomous_mod, "trigger_assignment", _trigger)
    monkeypatch.setattr(wi_service, "release_work_item_claim", _release)
    description = _long_description()
    item = {
        "id": "wi-1",
        "correlation_key": "k",
        "description": description,
        "claim_notes": _claim_notes(300),
    }

    run_id = await autonomous_mod._dispatch_inbox_item(
        None,
        SimpleNamespace(name="Inbox"),
        SimpleNamespace(name="Agent"),
        item,
        owner_user_id=None,
        writer=None,
        cap=1,
    )

    assert run_id == "run-1"
    sent = captured["payload"]["work_item"]
    assert sent["id"] == "wi-1"
    assert len(sent["description"]) < 10_000
    assert sent["description_total_chars"] == len(description)
    assert len(sent["claim_notes"]) == TRIGGER_CLAIM_NOTES_KEPT
    assert sent["claim_notes_total"] == 300
