"""The shared decision ask model and ``request_human_input`` options (#842, S1)."""

from __future__ import annotations

from typing import Any

import pytest

from app.services import work_item_ask
from app.services.decision_ask import (
    RECOMMENDED_EFFECT_MESSAGE,
    normalize_ask,
    normalize_run_ask,
    run_answer_text,
    run_question_text,
    unreadable_reasons,
)


def test_work_item_ask_re_exports_the_shared_model() -> None:
    assert work_item_ask.normalize_ask is normalize_ask
    assert work_item_ask.unreadable_reasons is unreadable_reasons


def test_old_call_shape_keeps_the_same_question() -> None:
    assert run_question_text(normalize_run_ask("Deploy to production?")) == "Deploy to production?"
    ask = normalize_run_ask("Deploy to production?", "The canary is green.\nThe last deploy failed.")
    assert ask == {"question": "Deploy to production?", "context": "The canary is green.\nThe last deploy failed."}
    assert run_question_text(ask) == ("Deploy to production?\n\nContext: The canary is green.\nThe last deploy failed.")


def test_run_context_is_kept_apart_and_unguarded() -> None:
    # A run's context carries what the agent found: ids and SHAs stay readable to it.
    ask = normalize_run_ask("Merge the fix?", "Commit 1a2b3c4d5e6f passed CI.")
    assert ask["question"] == "Merge the fix?"
    assert ask["context"] == "Commit 1a2b3c4d5e6f passed CI."


def test_options_recommendation_deadline_and_links_are_validated() -> None:
    ask = normalize_run_ask(
        "Which mailbox should I use?",
        options=[
            {"key": "A", "label": "Sales", "effect": "Sends from the sales mailbox"},
            {"key": "B", "label": "Support"},
        ],
        recommendation="a",
        deadline="2026-11-22T10:00:00+01:00",
        links=[{"url": "https://github.com/XTM-One-Platform/xtm-one/issues/842"}],
    )
    assert ask["recommendation"] == "A"
    assert ask["deadline"] == "2026-11-22T09:00:00+00:00"
    assert ask["links"] == [
        {"label": "xtm-one Issue #842", "url": "https://github.com/XTM-One-Platform/xtm-one/issues/842"}
    ]
    text = run_question_text(ask)
    assert "- A. Sales (recommended): Sends from the sales mailbox" in text
    assert "- B. Support" in text


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"recommendation": "C", "options": [{"key": "A", "label": "x"}, {"key": "B", "label": "y"}]}, "not the key"),
        ({"options": [{"key": "A", "label": "x"}, {"key": "a", "label": "y"}]}, "given twice"),
        ({"options": [{"label": "x"}]}, "needs a key"),
        ({"options": "A or B"}, "must be a list"),
        ({"deadline": "tomorrow"}, "not an ISO-8601"),
        ({"links": [{"url": "javascript:alert(1)"}]}, r"not an http\(s\) address"),
        ({"options": [{"key": str(i), "label": "x"} for i in range(7)]}, "at most 6"),
    ],
)
def test_invalid_options_are_refused(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_run_ask("Which mailbox should I use?", **kwargs)


def test_unreadable_question_is_refused_with_the_work_item_message() -> None:
    with pytest.raises(ValueError, match=r"ask\.question is read by a person"):
        normalize_run_ask("CARD SENT 2026-11-20T11:43:58Z: waiting for GO")
    with pytest.raises(ValueError, match="a UUID"):
        normalize_run_ask("Approve run 7e8aa6ff-4f10-470a-ab96-146c789ac061?")
    with pytest.raises(ValueError, match=r"ask\.question is required"):
        normalize_run_ask("   ")


def test_a_consent_code_stays_readable() -> None:
    # The merge tool asks the agent to quote its code: the person types it back.
    assert unreadable_reasons("Reply APPROVE M-1A2B3C4D to merge the two reports.") == []
    assert unreadable_reasons("Commit 1a2b3c4d5e6f") != []


def test_an_effect_opening_with_recommended_is_refused() -> None:
    options = [{"key": "A", "label": "Go", "effect": "Recommended: posts the comment"}, {"key": "B", "label": "No"}]
    with pytest.raises(ValueError) as refused:
        normalize_ask({"question": "Post the comment?", "options": options})
    assert str(refused.value) == RECOMMENDED_EFFECT_MESSAGE
    options[0]["effect"] = "(recommended) posts the comment"
    with pytest.raises(ValueError, match="drop the leading 'Recommended'"):
        normalize_run_ask("Post the comment?", options=options)
    options[0]["effect"] = "Posts the comment the review recommended"
    assert normalize_run_ask("Post the comment?", options=options)["options"][0]["effect"] == options[0]["effect"]


def test_the_answer_names_the_option_or_keeps_the_free_text() -> None:
    ask = {"options": [{"key": "A", "label": "Sales"}, {"key": "B", "label": "Support"}]}
    assert run_answer_text(ask, " a ") == "Human chose option A (Sales)"
    assert run_answer_text(ask, "support") == "Human chose option B (Support)"
    assert run_answer_text(ask, "Use the shared mailbox") == "Human response: Use the shared mailbox"
    assert run_answer_text({}, "Yes, go ahead.") == "Human response: Yes, go ahead."
    assert run_answer_text({"options": "garbage"}, "A") == "Human response: A"


def test_the_answer_text_keeps_a_defused_marker_defused() -> None:
    from app.core.redaction import defuse_secret_markers

    typed = defuse_secret_markers("use {{secret:token}}")
    assert run_answer_text({"options": [{"key": "A", "label": "Go"}]}, typed) == f"Human response: {typed}"
