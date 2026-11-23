# SPDX-License-Identifier: Apache-2.0

"""A decision card's texts are cut on a word with a visible mark, never mid-word (#1598).

Also: remediation figures read in the organization's currency, and a CTEM card
never states its blindness twice when the agent already wrote it.
"""

from __future__ import annotations

from app.services import ctem_remediation_decision as rd
from app.services import ctem_version_match as matching
from app.services.decision_ask import CUT_MARK, MAX_CONTEXT_CHARS, MAX_LABEL_CHARS, fit, normalize_ask


def test_a_text_that_fits_is_unchanged() -> None:
    assert fit("Patch the gateway", 40) == "Patch the gateway"


def test_a_long_text_is_cut_on_a_word_and_marked() -> None:
    text = "Upgrade every browser on the finance laptops before the quarter closes"
    cut = fit(text, 30)
    assert len(cut) <= 30
    assert cut.endswith(CUT_MARK)
    assert text.startswith(cut[: -len(CUT_MARK)])
    assert cut[: -len(CUT_MARK)].split()[-1] in text.split()  # the last word kept is whole


def test_a_single_long_word_is_still_bounded_and_marked() -> None:
    cut = fit("x" * 50, 10)
    assert cut == "x" * 9 + CUT_MARK


def test_normalize_ask_marks_a_cut_label_and_context() -> None:
    words = " ".join(["remediation"] * 200)
    ask = normalize_ask({"question": "Apply it?", "context": words, "options": [{"key": "A", "label": words}]})
    assert ask is not None
    assert len(ask["context"]) <= MAX_CONTEXT_CHARS and ask["context"].endswith(CUT_MARK)
    assert ask["context"][: -len(CUT_MARK)].endswith("remediation")
    label = ask["options"][0]["label"]
    assert len(label) <= MAX_LABEL_CHARS and label.endswith(CUT_MARK)


def test_remediation_figures_read_in_the_organization_currency() -> None:
    options = rd.parse_options(
        [{"key": "A", "label": "Patch", "cost": 6000, "expected_loss_before": 100_000, "expected_loss_after": 1000}]
    )
    ask = rd.decision_ask("Chrome", options, "A", figures=True, reason="the gate waits", currency="EUR")
    assert "cost 6,000 EUR" in ask["options"][0]["effect"]
    assert "expected loss 100,000 EUR before, 1,000 EUR after" in ask["options"][0]["effect"]
    bare = rd.decision_ask("Chrome", options, "A", figures=True, reason="the gate waits")
    assert "cost 6,000;" in bare["options"][0]["effect"]


def test_the_agent_blindness_lines_are_not_repeated() -> None:
    match = matching.version_match(None, product_label="Example BI", fixed_version=None)
    ask = {
        "question": "Upgrade Example BI?",
        "context": "Proposed without version matching: the inventory is empty.\nThe case is P1.\n"
        + matching.BLIND_ACTION,
    }
    context = matching.complete_ask(ask, match)["context"]
    assert context.count("Proposed without version matching") == 1
    assert context.count(matching.BLIND_ACTION) == 1
    assert context.endswith("The case is P1.")
