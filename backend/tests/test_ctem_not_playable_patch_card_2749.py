# SPDX-License-Identifier: Apache-2.0

"""#2749: the not-playable card of a patch case keeps every agent option, reads each one's intent from its
structure rather than a keyword, never stacks a stock option the agent already covers, never cuts an effect,
and keeps the figures and the requests of a long agent context."""

from __future__ import annotations

from typing import Any

from app.services import ctem_control_measurement as measurement
from app.services import ctem_validation_scenario as scenario_mod
from app.services.decision_ask import (
    CUT_MARK,
    MAX_CONTEXT_CHARS,
    MAX_EFFECT_CHARS,
    MAX_OPTIONS,
    fit_sentences,
    normalize_ask,
)

_FOUND: dict[str, Any] = {
    "url": "https://aev.example.com/admin/scenarios/3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
    "name": "Browser upgrade validation",
    "techniques_total": 3,
    "techniques_playable": 0,
    "techniques_missing": 3,
    "playable": False,
    "playable_tests": None,
}

_TAIL = (
    "The scenario and its arsenal actions are removed and the coverage keeps its Grouping for the next run, "
    "so nothing of this attempt stays in OpenAEV after the cleanup and the person deciding keeps the history. "
    "The case then waits for a new remediation proposal from the loop on its next pass over this asset. "
    "Please give a short reason."
)

_ASK: dict[str, Any] = {
    "question": "How should the upgrade be validated?",
    "context": "The inventory shows 14 devices below the fixed release.",
    "options": [
        {
            "key": "build_version_check",
            "label": "(A) Complete it: build the read-only version check",
            "effect": "The scenario gets a read-only check of the browser build on every device.",
        },
        {
            "key": "validate_manually",
            "label": "(B) Validate manually",
            "effect": (
                "the owner reads the product version on the device after the upgrade and that reading is "
                "recorded as the re-test"
            ),
        },
        {
            "key": "reject_and_clean",
            "label": "(C) Do not run: clean up",
            "effect": f"This closes the case without validation. {_TAIL}",
        },
    ],
    "recommendation": "build_version_check",
}


def _complete(ask: dict[str, Any]) -> dict[str, Any]:
    return scenario_mod.complete_ask(ask, _FOUND, None, "Browser exploit kit", patch_case=True)


def _by_label(out: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {o["label"]: o for o in out["options"]}


def test_every_agent_option_stays_in_order_with_keys_by_position_and_no_letter_prefix() -> None:
    out = _complete(_ASK)
    agent = out["options"][:3]
    assert [o["key"] for o in agent] == ["A", "B", "C"]
    assert [o["label"] for o in agent] == [
        "Complete it: build the read-only version check",
        "Validate manually",
        "Do not run: clean up",
    ]


def test_a_manual_version_read_closes_the_case_and_a_cleanup_never_does() -> None:
    out = _complete(_ASK)
    manual, cleanup = out["options"][1], out["options"][2]
    assert measurement.option_outcome(manual) == "closes"
    assert measurement.option_outcome(cleanup) == "not_closing"
    assert measurement.option_outcome(out["options"][0]) == "closes"


def test_no_stock_option_repeats_an_intent_the_agent_covers() -> None:
    out = _complete(_ASK)
    labels = [o["label"] for o in out["options"]]
    stock = [o["label"] for o in scenario_mod.NOT_PLAYABLE_OPTIONS]
    # The agent's version check is remediation evidence, not the stock completion: the completion is added once.
    assert labels.count(stock[0]) == 1
    # The agent's own "Validate manually" stays once; the stock manual and cleanup options are not added.
    assert labels.count(stock[1]) == 1 and stock[2] not in labels
    assert len(labels) == 4 and len(labels) == len(set(labels))


def test_no_effect_is_cut_and_the_request_to_the_decider_stays() -> None:
    out = _complete(_ASK)
    for option in out["options"]:
        assert not option["effect"].endswith(CUT_MARK)
        assert len(option["effect"]) <= MAX_EFFECT_CHARS
    assert "Please give a short reason." in out["options"][2]["effect"]
    # The card passes the ask normalisation whole.
    assert normalize_ask(out) is not None


def test_the_recommendation_stays_on_the_closing_agent_option() -> None:
    out = _complete(_ASK)
    assert out["recommendation"] == "A"
    assert measurement.closing_recommendation(out)["recommendation"] == "A"


def test_a_non_closing_agent_option_that_is_neither_manual_nor_cleanup_is_kept() -> None:
    ask = {
        **_ASK,
        "options": [
            {"key": "complete_default", "label": "Complete the scenario on every device", "effect": "Builds it."},
            {
                "key": "complete_sample",
                "label": "Complete it on a sample of devices",
                "effect": "Runs on 5 devices; its pass does not close the case.",
            },
            {"key": "complete_default_daily", "label": "Complete it and repeat daily", "effect": "Daily run."},
            {
                "key": "validate_manual",
                "label": "Validate manually",
                "effect": "Owners report the installed product version on each device.",
            },
            {"key": "reject", "label": "Reject", "effect": "Nothing runs."},
        ],
        "recommendation": "complete_default",
    }
    out = _complete(ask)
    labels = [o["label"] for o in out["options"]]
    assert labels[:5] == [o["label"] for o in ask["options"]]
    assert len(labels) <= MAX_OPTIONS
    # Every intent is covered by the agent: no stock option is stacked beside them.
    assert len(labels) == 5
    stock_effects = {o["effect"] for o in scenario_mod.NOT_PLAYABLE_OPTIONS}
    assert not any(o["effect"].startswith(tuple(e.rstrip(".") for e in stock_effects)) for o in out["options"])
    assert measurement.option_outcome(out["options"][1]) == "not_closing"
    # The agent recommended a completion that does not close a patch case: the version read is recommended.
    assert _by_label(out)["Validate manually"]["key"] == out["recommendation"]


def test_six_agent_options_are_never_cut_for_stock_ones_and_the_card_names_what_it_left_out() -> None:
    ask = {
        **_ASK,
        "options": [
            {"key": f"k{i}", "label": f"Read the installed version on site {i}", "effect": "One read."}
            for i in range(MAX_OPTIONS)
        ],
        "recommendation": "k2",
    }
    out = _complete(ask)
    assert [o["label"] for o in out["options"]] == [o["label"] for o in ask["options"]]
    assert out["recommendation"] == "C"
    assert "Not shown for room:" in str(out["context"])
    assert normalize_ask(out) is not None


def test_a_long_agent_context_keeps_its_figure_and_ask_lines() -> None:
    filler = "The browser estate spans several offices and teams that patch on their own schedule. "
    context = filler * 8 + "The upgrade costs 12 hours of IT time. Which validation do you choose?"
    out = _complete({**_ASK, "context": context})
    kept = str(out["context"])
    assert kept.startswith(scenario_mod.NOT_PLAYABLE_LEAD)
    assert len(kept) <= MAX_CONTEXT_CHARS and CUT_MARK not in kept
    assert "The upgrade costs 12 hours of IT time." in kept
    assert "Which validation do you choose?" in kept


def test_fit_sentences_keeps_the_marked_sentences_then_the_opening_ones_in_order() -> None:
    text = "One. Two. Three. Cost is 5. Four."
    assert fit_sentences(text, 100, lambda s: False) == text
    assert fit_sentences(text, 16, lambda s: any(c.isdigit() for c in s)) == "One. Cost is 5."
    assert fit_sentences("No end at all and too long", 5, lambda s: False) == ""


def test_the_completed_card_is_idempotent() -> None:
    once = _complete(_ASK)
    twice = _complete(once)
    assert twice["options"] == once["options"]
    assert twice["recommendation"] == once["recommendation"]
    assert twice["context"] == once["context"]
