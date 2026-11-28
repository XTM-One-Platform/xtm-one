"""A decision card never shows two recommendations (#2799).

The card marks the recommended option from ``ask.recommendation``; an option
label or effect that carries its own 'recommended' marker is refused at the
write, and an ask stored before the guard renders with the marker dropped.
"""

from __future__ import annotations

import pytest

from app.services.decision_ask import (
    RECOMMENDED_EFFECT_MESSAGE,
    ask_lines,
    has_recommended_mark,
    normalize_ask,
    run_question_text,
    without_recommended_mark,
)
from app.services.decision_answer_links import _option_row


def _ask(label_a: str, effect_a: str | None = None) -> dict:
    option_a = {"key": "a", "label": label_a}
    if effect_a is not None:
        option_a["effect"] = effect_a
    return {
        "question": "Which way should the gate go?",
        "context": "The scenario cannot run on the instance yet.",
        "options": [option_a, {"key": "b", "label": "Deploy the agent then run the scenario"}],
        "recommendation": "b",
    }


# The boundary: a marker form is refused, the word in a sentence is accepted.
@pytest.mark.parametrize(
    ("text", "marked"),
    [
        ("Connect the scanner (recommended)", True),
        ("Connect the scanner (Recommended)", True),
        ("[Recommended] Connect the scanner", True),
        ("Connect the scanner [recommended]", True),
        ("Connect the scanner - recommended", True),
        ("Connect the scanner, recommended.", True),
        ("Recommended: connect the scanner", True),
        ("recommended", True),
        ("Connecter le scanner (recommandé)", True),
        ("Apply the vendor's recommended patch", False),
        ("Posts the comment the review recommended", False),
        ("Connect the scanner", False),
        ("", False),
    ],
)
def test_the_marker_boundary(text: str, marked: bool) -> None:
    assert has_recommended_mark(text) is marked


def test_a_label_carrying_the_mark_is_refused() -> None:
    for label in ("Connect the scanner (recommended)", "[Recommended] Connect the scanner", "Connect - recommended"):
        with pytest.raises(ValueError) as refused:
            normalize_ask(_ask(label))
        assert "ask.options[a].label" in str(refused.value)
        assert str(refused.value).endswith(RECOMMENDED_EFFECT_MESSAGE)


def test_an_effect_with_a_trailing_mark_is_refused() -> None:
    with pytest.raises(ValueError, match=r"ask\.options\[a\]\.effect carries a 'recommended' mark"):
        normalize_ask(_ask("Connect the scanner", "Connects the scanner first (recommended)"))


def test_a_clean_ask_is_unchanged_and_shows_one_recommendation() -> None:
    ask = normalize_ask(_ask("Apply the vendor's recommended patch", "Patches the host"))
    assert ask is not None
    assert ask["options"][0] == {"key": "a", "label": "Apply the vendor's recommended patch", "effect": "Patches the host"}
    assert ask["recommendation"] == "b"
    assert run_question_text(ask).count("(recommended)") == 1
    assert sum("(recommended)" in line for line in ask_lines(ask)) == 1


def test_an_ask_stored_before_the_guard_renders_one_recommendation() -> None:
    # Old rows are never rewritten: every renderer drops the agent's own marker.
    stored = _ask("Connect the scanner (recommended)", "Connects it first - recommended")
    text = run_question_text(stored)
    assert text.count("(recommended)") == 1
    assert "- a. Connect the scanner: Connects it first" in text
    assert "- b. Deploy the agent then run the scenario (recommended)" in text
    assert sum("(recommended)" in line for line in ask_lines(stored)) == 1
    row = _option_row(stored["options"][0], recommended=False, language="en")
    texts = [item["text"] for item in row["items"]]
    assert texts == ["**a.** Connect the scanner", "Connects it first"]


def test_a_text_that_is_only_the_mark_is_kept() -> None:
    assert without_recommended_mark("(recommended)") == "(recommended)"
    assert without_recommended_mark(None) == ""
    assert without_recommended_mark("Patch now") == "Patch now"
