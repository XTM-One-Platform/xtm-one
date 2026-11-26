"""Decision items stay right at both ends, the pure parts (#1719)."""

from __future__ import annotations

import pytest

from app.services.builtin_tools.work_items import _blocked_why_lines
from app.services.decision_ask import ask_lines, keeps_waiting, normalize_ask
from app.services.work_item import _repeats_kept_answer

_ASK = {
    "question": "Ship the patch now?",
    "options": [
        {"key": "ship", "label": "Ship it"},
        {"key": "wait", "label": "Keep waiting", "keeps_waiting": True},
    ],
    "recommendation": "ship",
}


def test_an_option_may_keep_its_item_waiting() -> None:
    ask = normalize_ask(_ASK)
    assert ask is not None
    ship, wait = ask["options"]
    assert not keeps_waiting(ship) and "keeps_waiting" not in ship
    assert keeps_waiting(wait)
    assert "    - [wait] Keep waiting (keeps it waiting)" in ask_lines(ask)


def test_the_same_kept_answer_again_is_a_repeat() -> None:
    """Same option (any case) and same words repeat the stored answer; anything else is new (#2500)."""
    payload = {"answer": {"option": "wait", "label": "Keep waiting", "answered_at": "2026-11-26T09:00:00+00:00"}}
    assert _repeats_kept_answer(payload, "WAIT", "")
    assert not _repeats_kept_answer(payload, "wait", "one more week")
    assert not _repeats_kept_answer(payload, "ship", "")
    assert not _repeats_kept_answer({}, "wait", "")
    assert not _repeats_kept_answer(payload, None, "")
    worded = {"answer": {"option": "wait", "text": "one more week"}}
    assert _repeats_kept_answer(worded, "wait", "one more week")


def test_keeps_waiting_is_a_boolean() -> None:
    bad = {**_ASK, "options": [{"key": "a", "label": "A", "keeps_waiting": "yes"}, {"key": "b", "label": "B"}]}
    with pytest.raises(ValueError, match="keeps_waiting is true or false"):
        normalize_ask(bad)


def test_an_option_may_ask_for_a_confirm_step() -> None:
    """A terminal option marked confirm is kept, the others carry no flag (#2150)."""
    ask = normalize_ask(
        {
            **_ASK,
            "options": [
                {"key": "ship", "label": "Ship it"},
                {"key": "cancel", "label": "Cancel", "confirm": True},
                {"key": "wait", "label": "Keep waiting", "confirm": False},
            ],
        }
    )
    assert ask is not None
    ship, cancel, wait = ask["options"]
    assert cancel["confirm"] is True
    assert "confirm" not in ship and "confirm" not in wait


def test_confirm_is_a_boolean() -> None:
    bad = {**_ASK, "options": [{"key": "a", "label": "A", "confirm": "yes"}, {"key": "b", "label": "B"}]}
    with pytest.raises(ValueError, match="confirm is true or false"):
        normalize_ask(bad)


@pytest.mark.parametrize("status", ["done", "cancelled"])
def test_a_finished_item_reads_as_waiting_for_nobody(status: str) -> None:
    item = {
        "status": status,
        "needs_human": True,
        "ask": _ASK,
        "recipient_line": "  - waits for: X, notified in the bell.",
    }
    lines = _blocked_why_lines(item)
    assert not any("needs a person" in line or "waits for" in line or "ask:" in line for line in lines)


def test_a_blocked_item_still_reads_as_waiting_for_its_person() -> None:
    lines = _blocked_why_lines({"status": "blocked", "needs_human": True, "ask": _ASK})
    assert "  - needs a person (needs_human): nothing releases it by itself." in lines
    assert "  - ask: Ship the patch now?" in lines
