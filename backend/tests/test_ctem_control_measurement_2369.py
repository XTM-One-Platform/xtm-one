# SPDX-License-Identifier: Apache-2.0

"""A control_decay case is measured only by a test OpenCRQ can book, recurring below the re-test delay (#2369)."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents import builtin_agents as ba
from app.services import ctem_control_measurement as measurement
from app.services import organization_context as org_context
from app.services import work_item

_PERSONA = Path(ba.__file__).parent / "seed_data" / "agent_persona" / "ctem-threat-response-orchestrator.md"
_ASK = {
    "question": "Launch the validation scenario on web-01?",
    "context": "Reconciliation found no EDR source.",
    "options": [{"key": "A", "label": "Launch"}, {"key": "B", "label": "Hold"}],
    "recommendation": "A",
}


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start : end if end != -1 else len(text)]


def _rule_section() -> tuple[str, str]:
    section = _section(_PERSONA.read_text(encoding="utf-8"), "## A control_decay case: reconcile the controls first")
    return section, _section(section, "### What measures a control in OpenCRQ")


def test_the_persona_chooses_from_the_asset_and_the_live_integrations_permanent_first() -> None:
    section, rule = _rule_section()
    assert "from the asset type and the integrations the tenant has connected, read at decision time" in rule
    assert "never from a fixed recipe" in rule
    assert "A permanent control (recurring, running without a person) always comes before a one-off test" in rule
    order = [rule.index("(1) a permanent control an integration"), rule.index("(2) A permanent control in OpenAEV")]
    order.append(rule.index("(3) A one-off test, only when (1) and (2) are impossible"))
    assert order == sorted(order)
    # Reuse (#2368) counts only a covering scenario; a port-only scan is exposure.
    assert "a recurring port-only scan is cited as exposure, not as a measurement" in rule
    assert "Several options can be offered, the recommended one first" in rule
    assert "whether it is permanent or one-off" in rule and "what it will not (other controls" in rule
    # expired re-tests keep their behaviour under the same rule.
    assert "re-test or measure it as before, under the measurement rule below" in section


# The five mocked cases of the issue comment (25 Nov 2026 21:46Z). The choice itself is agent
# behaviour (the model reads the live integrations), so each case is a text test: the rule the
# persona and the case prompt give the agent names the input of the case and the proposal it gets.
_CASES = {
    "repository + Aikido": ("a repository with Aikido connected", "enabling Aikido scanning on that repository"),
    "IP + vulnerability scanner": (
        "an IP or host with a vulnerability scanner connected (Tenable, Qualys, Rapid7, Wiz)",
        "permanent vulnerability scanning of the asset in that scanner",
    ),
    "IP, no scanner": ("(2) A permanent control in OpenAEV", "OpenAEV generic vulnerability scanning"),
    "no permanent path": ("only when (1) and (2) are impossible", "the decision says why no permanent control fits"),
    "nothing at all": ("no integration for the asset type and no OpenAEV path", '"no measuring source connected"'),
}
_PROMPT_CASES = {
    "repository + Aikido": ("a repository with Aikido connected", "enable Aikido scanning on it"),
    "IP + vulnerability scanner": ("an IP or host with a vulnerability scanner", "permanent scanning of the asset"),
    "IP, no scanner": ("(2) a recurring OpenAEV scenario", "OpenAEV generic vulnerability scanning"),
    "no permanent path": ("only when (1) and (2) are impossible", "why no permanent control fits"),
    "nothing at all": ("no integration and no OpenAEV path", "no measuring source connected"),
}


@pytest.mark.parametrize("case", sorted(_CASES))
def test_each_mocked_case_gets_its_proposal_in_the_persona(case: str) -> None:
    _section_text, rule = _rule_section()
    given, proposed = _CASES[case]
    assert given in rule and proposed in rule


@pytest.mark.parametrize("case", sorted(_PROMPT_CASES))
def test_each_mocked_case_gets_its_proposal_in_the_case_prompt(case: str) -> None:
    given, proposed = _PROMPT_CASES[case]
    rule = ba.CTEM_CONTROL_MEASUREMENT_RULE
    assert given in rule and proposed in rule
    assert rule.index(given) <= rule.index(proposed)


def test_the_validation_step_of_the_loop_applies_the_rule_on_a_control_decay_case() -> None:
    persona = _PERSONA.read_text(encoding="utf-8")
    step = persona[persona.index("3. **Validation.**") : persona.index("4. **Risk.**")]
    assert "On a `control_decay` case" in step and "a permanent control an integration offers comes first" in step
    assert "never a port-only scan as the measurement" in step


def test_the_case_prompt_carries_the_measurement_rule() -> None:
    rule = ba.CTEM_CONTROL_MEASUREMENT_RULE
    assert rule in ba.CTEM_CONTROL_DECAY_RECONCILE
    assert ba.CTEM_CONTROL_DECAY_RECONCILE in ba.CTEM_F1_CASE_ASSIGNMENT["prompt"]
    for words in (
        "read at decision time",
        "never from a fixed recipe",
        "DETECTION or PREVENTION expectations on a technique the control",
        "A port-only scan with no expectation and no CVE output is exposure, never",
        "never counted as reusable evidence",
        "at a period below",
        "the recommended one first",
        "whether it is permanent or one-off",
        "no measuring source connected",
        "no scan",
    ):
        assert words in rule


@pytest.mark.parametrize("days", [1, 2, 7, 8, 30, 45, 3650])
def test_the_recurrence_period_is_always_below_the_re_test_delay(days: int) -> None:
    period = measurement.recurrence(days)
    assert period["period_hours"] < days * 24
    assert len(period["cron"].split()) == 5


def test_the_card_line_says_permanent_or_one_off_and_what_opencrq_updates() -> None:
    line = measurement.card_line(30)
    assert line.startswith(measurement.LINE_LEAD)
    # The re-run period belongs to the recurring option only, never to a one-off.
    assert "recurring OpenAEV scenario repeating weekly" in line
    assert "a one-off test counts once and its evidence expires after 30 days" in line
    assert "technique test result" in line and "CVE findings" in line and "DETECTION or PREVENTION" in line
    assert "a port-only scan with no expectation and no CVE output updates nothing" in line
    # It never contradicts the persona: no fixed scenario-only recipe.
    assert "only injects with" not in line


class _Db:
    def __init__(self, case: Any) -> None:
        self.case = case

    async def get(self, _model: Any, _id: Any) -> Any:
        return self.case


def _case(path: str = "control_decay") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(), organization_id=uuid.uuid4(), trigger_path=path, remediation_kind="restore_control"
    )


@pytest.fixture
def _max_age(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _version(_db: Any, _org: Any) -> Any:
        return SimpleNamespace(profile={"evidence_health": {"max_age_days": 10}})

    monkeypatch.setattr(org_context, "current_version", _version)


@pytest.mark.usefixtures("_max_age")
async def test_the_platform_completes_the_validation_card_of_a_control_decay_case() -> None:
    case = _case()
    ask = await work_item._complete_case_ask(_Db(case), f"ctem:validation:{case.id}", _ASK)
    lines = ask["context"].splitlines()
    assert lines[0] == measurement.card_line(10)
    assert "expires after 10 days" in lines[0] and "repeating weekly" in lines[0]
    assert lines[1] == _ASK["context"]
    # Written once: a second completion keeps the card as is.
    assert await work_item._complete_case_ask(_Db(case), f"ctem:validation:{case.id}", ask) == ask


@pytest.mark.usefixtures("_max_age")
async def test_other_cases_and_other_keys_keep_their_card() -> None:
    other = _case("new_vulnerability")
    assert await work_item._complete_case_ask(_Db(other), f"ctem:validation:{other.id}", _ASK) == _ASK
    decay = _case()
    assert await work_item._complete_case_ask(_Db(decay), f"ctem:review:{decay.id}", _ASK) == _ASK
    assert await work_item._complete_case_ask(_Db(decay), "ctem:validation:not-a-uuid", _ASK) == _ASK


async def test_an_unreadable_profile_leaves_the_card_as_given(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fails(_db: Any, _org: Any) -> Any:
        raise RuntimeError("database down")

    monkeypatch.setattr(org_context, "current_version", _fails)
    case = _case()
    assert await measurement.complete_gate_ask(_Db(case), f"ctem:validation:{case.id}", _ASK) == _ASK  # type: ignore[arg-type]
