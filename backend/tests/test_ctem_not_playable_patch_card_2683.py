# SPDX-License-Identifier: Apache-2.0

"""#2683: the not-playable validation card of a patch case keeps and recommends an option that closes it,
and the line the platform writes stays readable within the context cap."""

from __future__ import annotations

from typing import Any

from app.services import ctem_case_assets as assets
from app.services import ctem_control_measurement as measurement
from app.services import ctem_validation_scenario as scenario_mod
from app.services.decision_ask import CUT_MARK, MAX_CONTEXT_CHARS, lead_then_fit
from app.services.work_item_ask_backfill import card_unreadable

_FOUND: dict[str, Any] = {
    "url": "https://aev.example.com/admin/scenarios/3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
    "name": "Browser upgrade validation",
    "techniques_total": 3,
    "techniques_playable": 0,
    "techniques_missing": 3,
    "playable": False,
    "playable_tests": None,
}

_PATCH_ASK: dict[str, Any] = {
    "question": "How should the browser upgrade be validated?",
    "context": "The inventory shows 14 devices below the fixed version.",
    "options": [
        {
            "key": "A",
            "label": "Validate by installed version",
            "effect": "Read the installed version on every in-scope device after the upgrade.",
        },
        {"key": "B", "label": "Launch the coverage scenario", "effect": "Runs the OpenAEV scenario."},
    ],
    "recommendation": "A",
}


def _complete(ask: dict[str, Any], *, patch_case: bool, found: dict[str, Any] | None = None) -> dict[str, Any]:
    return scenario_mod.complete_ask(ask, found or _FOUND, None, "Browser exploit kit", patch_case=patch_case)


def test_patch_case_keeps_the_version_evidence_option_and_recommends_it() -> None:
    out = _complete(_PATCH_ASK, patch_case=True)
    assert str(out["context"]).startswith(scenario_mod.NOT_PLAYABLE_LEAD)
    labels = [o["label"] for o in out["options"]]
    assert labels[0] == "Validate by installed version"
    assert "Launch the coverage scenario" not in labels
    assert labels[1:] == [o["label"] for o in scenario_mod.NOT_PLAYABLE_OPTIONS]
    assert [o["key"] for o in out["options"]] == ["A", "B", "C", "D"]
    assert out["recommendation"] == "A"
    # Every option says whether its pass closes the case (#2461 rule).
    assert measurement.option_outcome(out["options"][0]) == "closes"
    assert measurement.option_outcome(out["options"][1]) == "not_closing"
    assert measurement.option_outcome(out["options"][2]) == "not_closing"
    # The recommendation never sits on an option that announces it does not close the case.
    assert measurement.closing_recommendation(out)["recommendation"] == out["recommendation"]


def test_patch_case_keeps_the_agent_recommendation_among_closing_options() -> None:
    ask = {
        **_PATCH_ASK,
        "options": [
            {"key": "A", "label": "Wait for the next inventory sync", "effect": "Nothing runs."},
            {"key": "B", "label": "Read the fixed version on the hosts", "effect": "One-off read."},
            {"key": "C", "label": "Record remediation evidence", "effect": "Its pass closes the case."},
        ],
        "recommendation": "C",
    }
    out = _complete(ask, patch_case=True)
    by_key = {o["key"]: o["label"] for o in out["options"]}
    assert by_key[out["recommendation"]] == "Record remediation evidence"
    # Every agent option is kept and says what its pass does (#2749): the wait does not close the case.
    outcomes = [measurement.option_outcome(o) for o in out["options"][:3]]
    assert outcomes == ["not_closing", "closes", "closes"]


def test_patch_case_without_a_closing_option_recommends_completing_the_scenario() -> None:
    ask = {**_PATCH_ASK, "options": [{"key": "A", "label": "Launch", "effect": "runs it"}]}
    out = _complete(ask, patch_case=True)
    assert [o["label"] for o in out["options"]] == [o["label"] for o in scenario_mod.NOT_PLAYABLE_OPTIONS]
    assert out["recommendation"] == "A"


def test_threat_case_is_unchanged() -> None:
    out = _complete(_PATCH_ASK, patch_case=False)
    assert [o["label"] for o in out["options"]] == [o["label"] for o in scenario_mod.NOT_PLAYABLE_OPTIONS]
    assert [o["effect"] for o in out["options"]] == [o["effect"] for o in scenario_mod.NOT_PLAYABLE_OPTIONS]
    assert out["recommendation"] == "A"


def test_patch_card_is_idempotent() -> None:
    once = _complete(_PATCH_ASK, patch_case=True)
    twice = _complete(once, patch_case=True)
    assert twice["options"] == once["options"] and twice["recommendation"] == once["recommendation"]
    assert twice["context"] == once["context"]


def test_raw_playable_tests_and_an_id_in_the_threat_never_reach_the_line() -> None:
    found = {**_FOUND, "techniques_playable": 1, "techniques_missing": 2, "playable_tests": '["T1003", "a1b2c3d4e"]'}
    line = scenario_mod.not_playable_line(found, "not CTEM 9f3a2b1c - browser exploit")
    assert line == "Scenario not playable: 2 of 3 techniques have no payload."
    readable = {**found, "playable_tests": "credential dumping on Windows hosts"}
    assert "credential dumping on Windows hosts, not the threat." in scenario_mod.not_playable_line(
        readable, "not CTEM 9f3a2b1c - browser exploit"
    )
    assert scenario_mod.not_playable_line(readable, "APT-X package").endswith("not APT-X package.")


def test_long_context_is_cut_at_a_sentence_and_the_card_stays_readable() -> None:
    sentence = "The inventory shows fourteen devices below the fixed version on the browser estate. "
    ask = {**_PATCH_ASK, "context": sentence * 12}
    found = {**_FOUND, "techniques_playable": 1, "techniques_missing": 2, "playable_tests": "[a1b2c3d4e]"}
    out = scenario_mod.complete_ask(ask, found, None, "not CTEM 9f3a2b1c", patch_case=True)
    context = str(out["context"])
    assert len(context) <= MAX_CONTEXT_CHARS
    assert context.startswith("Scenario not playable: 2 of 3 techniques have no payload.")
    assert CUT_MARK not in context
    assert all(line.rstrip().endswith(".") for line in context.splitlines())
    assert card_unreadable(out) is False


def test_lead_then_fit_keeps_the_head_whole_and_drops_a_sentence_it_cannot_end() -> None:
    head = "Platform line."
    assert lead_then_fit(head, "", 50) == head
    assert lead_then_fit(head, "Short.", 50) == "Platform line.\nShort."
    assert lead_then_fit(head, "One. Two words that will not fit at all here", 25) == "Platform line.\nOne."
    assert lead_then_fit(head, "No sentence end inside the room at all", 25) == head
    assert lead_then_fit("x" * 60, "rest.", 40).endswith(CUT_MARK)


def test_other_completers_keep_their_line_whole_before_a_long_context() -> None:
    long_context = "Agent sentence with enough words to fill the card. " * 30
    measured = measurement.complete_ask({"question": "Q?", "context": long_context}, 30)
    assert measured["context"].startswith(measurement.LINE_LEAD) and CUT_MARK not in measured["context"]
    assert measured["context"].rstrip().endswith(".") and len(measured["context"]) <= MAX_CONTEXT_CHARS
    line = assets.card_line([{"name": "web-01", "provenance": "agent"}])
    assert line is not None
    kept = lead_then_fit(line, long_context, MAX_CONTEXT_CHARS)
    assert kept.startswith(line) and CUT_MARK not in kept and len(kept) <= MAX_CONTEXT_CHARS
