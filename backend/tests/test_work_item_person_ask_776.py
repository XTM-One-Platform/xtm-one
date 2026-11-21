"""An agent parking an item for a person writes the decision card they read (#776, slice 3)."""

from __future__ import annotations

import pytest

from app.services.work_item_ask import (
    normalize_ask,
    person_ask_missing,
    refuse_incomplete_person_ask,
)


def _card() -> dict:
    return {
        "question": "Post Legal's comment asking the supplier for the 4 missing documents?",
        "context": "The contract review found 4 documents missing.\nGo posts the prepared comment.",
        "options": [
            {"key": "go", "label": "Go", "effect": "the comment is posted"},
            {"key": "no", "label": "No", "effect": "the review closes as is"},
        ],
        "recommendation": "go",
    }


def test_a_complete_card_is_accepted():
    ask = normalize_ask(_card())
    assert person_ask_missing(ask) == []
    refuse_incomplete_person_ask(ask)


@pytest.mark.parametrize("ask", [None, {}, {"question": " "}, "text"])
def test_no_question_names_every_field(ask):
    assert person_ask_missing(ask) == [
        "ask.question",
        "ask.context",
        "ask.options (at least 2)",
        "ask.recommendation",
    ]


def test_a_card_without_context_is_refused_naming_it():
    ask = _card()
    del ask["context"]
    with pytest.raises(ValueError) as exc:
        refuse_incomplete_person_ask(normalize_ask(ask))
    assert "ask.context" in str(exc.value)
    assert "ask.options" not in str(exc.value)
    assert "good:" in str(exc.value)


def test_a_card_with_one_option_is_refused_naming_it():
    ask = _card()
    ask["options"] = ask["options"][:1]
    ask["recommendation"] = "go"
    with pytest.raises(ValueError) as exc:
        refuse_incomplete_person_ask(normalize_ask(ask))
    assert "ask.options (at least 2)" in str(exc.value)
    assert "ask.context" not in str(exc.value)


def test_a_card_without_recommendation_is_refused_naming_it():
    """#834: the reader sees which answer the agent recommends."""
    ask = _card()
    del ask["recommendation"]
    with pytest.raises(ValueError) as exc:
        refuse_incomplete_person_ask(normalize_ask(ask))
    assert "ask.recommendation" in str(exc.value)
    assert "ask.context" not in str(exc.value)


def test_the_refusal_says_needs_human_and_same_call():
    with pytest.raises(ValueError) as exc:
        refuse_incomplete_person_ask(None)
    message = str(exc.value)
    assert message.startswith("needs_human needs an ask card")
    assert "in the same call" in message
    # One message for the rule (#862): the four fields and nothing written.
    for field in ("question", "context", "(at least 2)", "recommendation", "nothing was written"):
        assert field in message


def test_the_tool_paths_require_the_card():
    """Both agent tools pass require_person_ask; the service default keeps internal writers unchanged."""
    import inspect

    from app.services import work_item as wi
    from app.services.builtin_tools import work_items as tools

    assert inspect.signature(wi.create_work_item).parameters["require_person_ask"].default is False
    assert inspect.signature(wi.update_work_item).parameters["require_person_ask"].default is False
    source = inspect.getsource(tools)
    assert source.count("require_person_ask=True") == 2
