"""Decision items stay right at both ends, the pure parts (#1719)."""

from __future__ import annotations

import pytest

from app.services.builtin_tools.work_items import _blocked_why_lines
from app.services.decision_ask import ask_lines, keeps_waiting, normalize_ask

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


def test_keeps_waiting_is_a_boolean() -> None:
    bad = {**_ASK, "options": [{"key": "a", "label": "A", "keeps_waiting": "yes"}, {"key": "b", "label": "B"}]}
    with pytest.raises(ValueError, match="keeps_waiting is true or false"):
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
