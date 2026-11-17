"""The structured ask of a work item waiting for a person (#9437, slice 1)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import work_item as wi
from app.services.work_item_ask import MAX_OPTIONS, ask_lines, normalize_ask


def _full() -> dict:
    return {
        "question": "  Merge the  fix now or wait for the release?  ",
        "options": [
            {"key": "A", "label": "Merge now", "effect": "the desk merges at green"},
            {"key": "B", "label": "Wait"},
        ],
        "recommendation": "a",
        "deadline": "2026-11-08T09:00:00+01:00",
    }


def test_a_full_ask_is_trimmed_and_normalized():
    ask = normalize_ask(_full())
    assert ask == {
        "question": "Merge the fix now or wait for the release?",
        "options": [
            {"key": "A", "label": "Merge now", "effect": "the desk merges at green"},
            {"key": "B", "label": "Wait"},
        ],
        "recommendation": "A",
        "deadline": "2026-11-08T08:00:00+00:00",
    }


def test_the_json_text_of_an_ask_is_read_like_the_object():
    import json

    assert normalize_ask(json.dumps(_full())) == normalize_ask(_full())


@pytest.mark.parametrize("cleared", [None, {}, "", "   "])
def test_an_empty_ask_clears_it(cleared):
    assert normalize_ask(cleared) is None


def test_a_question_alone_is_a_free_text_ask():
    assert normalize_ask({"question": "Which customer environment?"}) == {"question": "Which customer environment?"}


def test_a_label_defaults_to_its_key():
    ask = normalize_ask({"question": "q", "options": [{"key": "yes"}]})
    assert ask["options"] == [{"key": "yes", "label": "yes"}]


@pytest.mark.parametrize(
    ("ask", "reason"),
    [
        ({"options": [{"key": "A"}]}, "question is required"),
        ({"question": "q", "options": "A or B"}, "must be a list"),
        ({"question": "q", "options": [{"label": "no key"}]}, "needs a key"),
        ({"question": "q", "options": [{"key": "A"}, {"key": "a"}]}, "given twice"),
        ({"question": "q", "options": [{"key": "A"}], "recommendation": "C"}, "not the key"),
        ({"question": "q", "recommendation": "A"}, "not the key"),
        ({"question": "q", "deadline": "next week"}, "ISO-8601"),
        ({"question": "q", "colour": "red"}, "unknown field"),
        ({"question": "q", "options": [{"key": str(i)} for i in range(MAX_OPTIONS + 1)]}, "at most"),
        (["q"], "ask is {question"),
        ("{not json", "not valid JSON"),
    ],
)
def test_an_invalid_ask_is_refused_with_its_reason(ask, reason):
    with pytest.raises(ValueError, match=reason):
        normalize_ask(ask)


def test_a_naive_deadline_is_read_as_utc():
    assert normalize_ask({"question": "q", "deadline": "2026-11-08T09:00:00"})["deadline"] == (
        "2026-11-08T09:00:00+00:00"
    )


def test_the_tool_lines_name_the_question_the_options_and_the_recommendation():
    lines = ask_lines(normalize_ask(_full()))
    assert lines[0] == "  - ask: Merge the fix now or wait for the release?"
    assert "[A] Merge now (recommended): the desk merges at green" in lines[1]
    assert lines[2].endswith("[B] Wait")
    assert "answer needed by 2026-11-08T08:00:00+00:00" in lines[3]


@pytest.mark.parametrize("legacy", [None, {}, {"options": []}])
def test_a_legacy_item_has_no_ask_lines(legacy):
    assert ask_lines(legacy) == []


def test_the_needs_human_event_carries_the_ask():
    ask = normalize_ask(_full())
    item = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        status="blocked",
        needs_human=True,
        blocked_reason="decide",
        ask=ask,
        owner_agent_id=None,
        created_at=None,
        last_activity_at=None,
    )
    name, data = wi._needs_human_event(item, previous_status="in_progress")
    assert name == "work_item.needs_human"
    assert data["ask"] == ask
