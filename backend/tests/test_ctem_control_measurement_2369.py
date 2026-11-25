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


def test_the_persona_measures_a_control_only_with_detection_or_prevention_expectations() -> None:
    section = _section(_PERSONA.read_text(encoding="utf-8"), "## A control_decay case: reconcile the controls first")
    rule = _section(section, "### What measures a control in OpenCRQ")
    assert "DETECTION or PREVENTION expectation" in rule and "technique the control covers" in rule
    assert "never_measured" in rule and "attested_only" in rule
    assert "Never propose a port or version scan, nor an inject with no expectation, as the measurement" in rule
    # Reuse (#2368) counts only a covering scenario with those expectations.
    assert "a recurring port scan is cited as exposure, not as a measurement" in rule
    # The gate says what OpenCRQ will and will not update.
    assert "technique test result" in rule and "findings, other controls" in rule
    # Recurring below the re-test delay, on the existing recurring scenario or group.
    assert "recurring, at a period below `evidence_health.max_age_days`" in rule
    # No measurable test: say it, name the root cause, no scan.
    assert '"no measuring source connected"' in rule and "never a scan" in rule
    # expired re-tests keep their behaviour under the same rule.
    assert "re-test or measure it as before, under the measurement rule below" in section


def test_the_validation_step_of_the_loop_applies_the_rule_on_a_control_decay_case() -> None:
    persona = _PERSONA.read_text(encoding="utf-8")
    step = persona[persona.index("3. **Validation.**") : persona.index("4. **Risk.**")]
    assert "On a `control_decay` case" in step and "never a port or version scan" in step


def test_the_case_prompt_carries_the_measurement_rule() -> None:
    rule = ba.CTEM_CONTROL_MEASUREMENT_RULE
    assert rule in ba.CTEM_CONTROL_DECAY_RECONCILE
    assert ba.CTEM_CONTROL_DECAY_RECONCILE in ba.CTEM_F1_CASE_ASSIGNMENT["prompt"]
    for words in (
        "DETECTION or PREVENTION expectations on a technique the control covers",
        "a port or version scan is exposure, never the measurement",
        "nor counted as reusable evidence",
        "what it will not (findings, other controls)",
        "recurring at a period below evidence_health.max_age_days",
        "no measuring source connected",
        "no scan",
    ):
        assert words in rule


@pytest.mark.parametrize("days", [1, 2, 7, 8, 30, 45, 3650])
def test_the_recurrence_period_is_always_below_the_re_test_delay(days: int) -> None:
    period = measurement.recurrence(days)
    assert period["period_hours"] < days * 24
    assert len(period["cron"].split()) == 5


def test_the_card_line_says_what_opencrq_updates_and_what_not() -> None:
    line = measurement.card_line(30)
    assert line.startswith(measurement.LINE_LEAD)
    assert "DETECTION or PREVENTION" in line and "technique test result" in line
    assert "a port or version scan, findings and other controls are not updated" in line
    assert "expires after 30 days" in line and "repeats weekly" in line


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
    assert "expires after 10 days" in lines[0] and "repeats weekly" in lines[0]
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
