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


def test_the_tool_schema_says_the_recommendation_is_an_option_key():
    from app.services.builtin_tools.work_items import _BLOCKED_WHY_SCHEMA

    assert "recommendation: option key" in _BLOCKED_WHY_SCHEMA["ask"]["description"]


def test_a_context_keeps_its_lines_and_drops_blank_ones():
    ask = normalize_ask(
        {"question": "Post the comment?", "context": "  The review found  4 gaps.\n\nGo posts it.  ", "options": []}
    )
    assert ask["context"] == "The review found 4 gaps.\nGo posts it."
    assert "    context: The review found 4 gaps. / Go posts it." in ask_lines(ask)


def test_a_context_given_as_lines_is_joined():
    assert normalize_ask({"question": "Post it?", "context": ["One.", "Two."]})["context"] == "One.\nTwo."


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Card sent 2026-11-20T11:43:58Z, answer?", "machine timestamp"),
        ("Sent at 19:14Z, go?", "machine timestamp"),
        ("Approve item 16fe84b0-465c-4212-bcdb-3d0c81464b70?", "UUID"),
        ("Merge head 179dc57b?", "hexadecimal id"),
        ("Reply to Teams message 1795175038574?", "long run of digits"),
        ("Fire timer legal:fin-796-card-reminder now?", "correlation or timer key"),
        ("Resend the msg to the owner?", "delivery bookkeeping"),
        ("Card sent: go or no?", "delivery bookkeeping"),
    ],
)
def test_a_question_a_person_cannot_read_is_refused_with_how_to_rewrite_it(text, reason):
    with pytest.raises(ValueError, match=reason) as refused:
        normalize_ask({"question": text, "options": [{"key": "A"}, {"key": "B"}]})
    assert "blocked_reason" in str(refused.value)
    assert "good:" in str(refused.value)


def test_an_unreadable_context_or_option_is_refused_too():
    with pytest.raises(ValueError, match=r"ask\.context"):
        normalize_ask({"question": "Post it?", "context": "Timer dev:harness-gaps-zero fires at 08:00Z."})
    with pytest.raises(ValueError, match=r"ask\.options"):
        normalize_ask(
            {"question": "Post it?", "options": [{"key": "A", "label": "Go", "effect": "posts msg 1795202088870"}]}
        )


@pytest.mark.parametrize(
    "text",
    [
        "Post Legal's comment asking the supplier for the 4 missing documents?",
        "Renew the 2027 contract at 12% off before Monday 24 November, 10:00?",
        "Merge the fix for OpenCTI 6.9 now, or wait for the release on 1 December?",
        "Approve Jira approval #2004 as proposed?",
    ],
)
def test_a_clean_question_is_accepted(text):
    assert normalize_ask({"question": text, "options": [{"key": "A"}, {"key": "B"}]})["question"] == text


def test_the_tool_schema_carries_a_good_and_a_bad_example():
    from app.services.builtin_tools.work_items import _BLOCKED_WHY_SCHEMA

    description = _BLOCKED_WHY_SCHEMA["ask"]["description"]
    assert "Good:" in description and "Bad:" in description and "context" in description


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
