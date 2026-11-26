# SPDX-License-Identifier: Apache-2.0

"""A never_measured gate never recommends an option whose clean result cannot close the case (#2461)."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents import builtin_agents as ba
from app.services import ctem_case_loop, work_item
from app.services import ctem_control_measurement as measurement
from app.services import organization_context as org_context

_PERSONA = Path(ba.__file__).parent / "seed_data" / "agent_persona" / "ctem-threat-response-orchestrator.md"

# The gate of the issue: an unauthenticated OpenAEV scan recommended first, the structural option second.
_SCAN_FIRST = {
    "question": "How should web-01 be measured for the first time?",
    "context": "No version-reading scanner is connected for web-01.",
    "options": [
        {
            "key": "A",
            "label": "Run an OpenAEV vulnerability scan",
            "effect": "One-off unauthenticated scan; a clean result does not close the case.",
        },
        {
            "key": "B",
            "label": "Connect a vulnerability scanner",
            "effect": "Permanent version-reading scanning of web-01; its first result closes the case.",
        },
        {"key": "C", "label": "Hold", "effect": "Nothing runs."},
    ],
    "recommendation": "A",
}


class _Db:
    def __init__(self, case: Any) -> None:
        self.case = case

    async def get(self, _model: Any, _id: Any) -> Any:
        return self.case


def _case(state: str = "never_measured") -> SimpleNamespace:
    title = ctem_case_loop._CONTROL_DECAY_TITLES[state].format(asset="web-01")
    return SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4(), trigger_path="control_decay", title=title)


@pytest.fixture
def _max_age(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _version(_db: Any, _org: Any) -> Any:
        return SimpleNamespace(profile={"evidence_health": {"max_age_days": 10}})

    monkeypatch.setattr(org_context, "current_version", _version)


def test_the_title_tail_matches_the_platform_title_of_a_never_measured_case() -> None:
    assert measurement.is_never_measured(_case())
    assert not measurement.is_never_measured(_case("expired"))
    assert not measurement.is_never_measured(_case("attested_only"))
    other = SimpleNamespace(trigger_path="new_vulnerability", title=_case().title)
    assert not measurement.is_never_measured(other)


def test_the_case_prompt_asks_each_option_for_its_closing_outcome() -> None:
    rule = ba.CTEM_CONTROL_MEASUREMENT_RULE
    for words in (
        "compare what a pass of each option proves",
        f"'{measurement.CLOSES_WORDS}'",
        f"'{measurement.NOT_CLOSING_WORDS}'",
        "a clean unauthenticated scan gives it nothing to book",
        "is never the recommended one",
        "the first and only decision is the structural one",
        "never as a gate of its own before it",
        "OpenCRQ never takes an OpenAEV result written back",
    ):
        assert words in rule, words
    assert rule in ba.CTEM_F1_CASE_ASSIGNMENT["prompt"]


def test_the_persona_carries_the_same_rule() -> None:
    persona = _PERSONA.read_text(encoding="utf-8")
    for words in (
        '"closes the case" or "does not close the case"',
        "An option whose pass does not close the case is never the recommended one",
        "the first and only decision is the structural one",
        "never as a separate gate before it",
    ):
        assert words in persona, words


def test_the_card_line_says_what_closes_a_never_measured_case() -> None:
    line = measurement.never_measured_line(10)
    assert line.startswith(measurement.LINE_LEAD)
    assert "DETECTION or PREVENTION" in line and "CVE findings from a scan that reads versions" in line
    assert f"its pass {measurement.NOT_CLOSING_WORDS}" in line
    assert "expires after 10 days" in line
    assert len(line) < measurement._MAX_CONTEXT_CHARS


def test_the_recommendation_moves_to_the_option_that_closes_the_case() -> None:
    ask = measurement.closing_recommendation(_SCAN_FIRST)
    assert ask["recommendation"] == "B"
    assert ask["options"] == _SCAN_FIRST["options"]


def test_a_recommendation_with_no_closing_alternative_is_kept() -> None:
    only_scan = dict(_SCAN_FIRST, options=[_SCAN_FIRST["options"][0], _SCAN_FIRST["options"][2]])
    assert measurement.closing_recommendation(only_scan)["recommendation"] == "A"
    closing_first = dict(_SCAN_FIRST, recommendation="B")
    assert measurement.closing_recommendation(closing_first)["recommendation"] == "B"


@pytest.mark.usefixtures("_max_age")
async def test_the_gate_of_a_never_measured_case_is_completed_and_recommends_the_closing_option() -> None:
    case = _case()
    ask = await work_item._complete_case_ask(_Db(case), f"ctem:validation:{case.id}", _SCAN_FIRST)
    assert ask["context"].splitlines()[0] == measurement.never_measured_line(10)
    assert ask["recommendation"] == "B"
    # Written once, and a second write of the same card keeps it.
    assert await work_item._complete_case_ask(_Db(case), f"ctem:validation:{case.id}", ask) == ask


@pytest.mark.usefixtures("_max_age")
async def test_a_rewritten_ask_that_already_has_the_line_is_still_corrected() -> None:
    case = _case()
    first = await work_item._complete_case_ask(_Db(case), f"ctem:validation:{case.id}", _SCAN_FIRST)
    again = dict(first, recommendation="A")
    fixed = await work_item._complete_case_ask(_Db(case), f"ctem:validation:{case.id}", again)
    assert fixed["recommendation"] == "B"
    assert fixed["context"] == first["context"]


@pytest.mark.usefixtures("_max_age")
async def test_an_expired_case_keeps_the_general_line_and_its_recommendation() -> None:
    case = _case("expired")
    ask = await work_item._complete_case_ask(_Db(case), f"ctem:validation:{case.id}", _SCAN_FIRST)
    assert ask["context"].splitlines()[0] == measurement.card_line(10)
    assert ask["recommendation"] == "A"
