"""OpenCRQ never scores a technique-test finding: the tools say so (#2598).

A technique finding is a control's answer to a test and carries no score by
design; only a CVE finding waits for a score (or for a CVSS vector). A risk run
that covers no technique says what coverage needs: an assessed control.
"""

from __future__ import annotations

from typing import Any

from app.integrations.providers.opencrq import _common
from app.integrations.providers.opencrq._common import (
    CVE_NOT_SCORED,
    CVE_PENDING_VECTOR,
    NO_COVERED_TECHNIQUE_HINT,
    TECHNIQUE_NOT_SCORED,
    _fmt_risk_scenario,
    _unscored_wording,
)
from app.integrations.providers.opencrq.crq import _fmt_run
from app.integrations.providers.opencrq.findings import _fmt_finding_row

CVE_GROUP = {"key": "cve:CVE-2024-3400", "cveId": "CVE-2024-3400", "score": None, "pendingVector": False}
CVE_PENDING = {"key": "cve:CVE-2024-6387", "cveId": "CVE-2024-6387", "score": None, "pendingVector": True}
TECHNIQUE_GROUP = {
    "key": "technique:T1059:prevention",
    "mitreTechniqueExternalId": "T1059",
    "mitreTechniqueName": "Command and Scripting Interpreter",
    "expectationType": "prevention",
    "score": None,
    "pendingVector": False,
}


def test_three_unscored_groups_read_three_distinct_wordings() -> None:
    wordings = [_unscored_wording(r) for r in (CVE_GROUP, CVE_PENDING, TECHNIQUE_GROUP)]
    assert wordings == [CVE_NOT_SCORED, CVE_PENDING_VECTOR, TECHNIQUE_NOT_SCORED]
    assert len(set(wordings)) == 3
    assert "not scored yet" not in TECHNIQUE_NOT_SCORED
    assert "not scored by design" in TECHNIQUE_NOT_SCORED


def test_a_technique_group_is_read_from_its_key_alone() -> None:
    assert _unscored_wording({"key": "technique:T1003", "score": None}) == TECHNIQUE_NOT_SCORED


def test_a_technique_finding_row_never_reads_not_scored_yet() -> None:
    row = dict(TECHNIQUE_GROUP, assetName="Trading Platform", currentState="applicable", evidenceId="e1")
    line = _fmt_finding_row(row)
    assert "not scored yet" not in line
    assert "unscored" not in line
    assert TECHNIQUE_NOT_SCORED in line


def test_a_scored_finding_row_keeps_its_score() -> None:
    row = dict(CVE_GROUP, score=52.9, band="medium", assetName="Portal", currentState="applicable")
    assert "score 52 (medium)" in _fmt_finding_row(row)


def _scenario(covered: int) -> dict[str, Any]:
    return {
        "id": "s1",
        "name": "Ransomware on trading",
        "latestRun": {"aleMean": 1000, "techniqueCount": 12, "coveredTechniqueCount": covered},
    }


def test_a_risk_scenario_with_no_covered_technique_prints_the_hint() -> None:
    out = _fmt_risk_scenario(_scenario(0))
    assert "0 of 12 techniques covered" in out
    assert NO_COVERED_TECHNIQUE_HINT in out


def test_a_risk_scenario_with_covered_techniques_prints_no_hint() -> None:
    assert NO_COVERED_TECHNIQUE_HINT not in _fmt_risk_scenario(_scenario(3))


def test_a_run_with_no_covered_technique_prints_the_hint() -> None:
    assert NO_COVERED_TECHNIQUE_HINT in _fmt_run({"techniqueCount": 12, "coveredTechniqueCount": 0})
    assert NO_COVERED_TECHNIQUE_HINT not in _fmt_run({"techniqueCount": 12, "coveredTechniqueCount": 5})


def test_the_hint_names_no_placeholder() -> None:
    assert "<" not in _common.NO_COVERED_TECHNIQUE_HINT
