"""#2118: a passive read that proves a case still exposed is recorded as exposure evidence.

``evidence_type`` ``exposure`` with ``failed`` keeps the case's verdict (affected) and
``conclusive`` true (the read proves its claim); a pass with exposure evidence is refused.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.api.v1.ctem_cases import CaseValidation
from app.models.ctem_case import CtemCase
from app.services import ctem_case_loop as loop
from app.services import ctem_cases as rules
from app.services.builtin_tools import ctem_cases as ctem_tools

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def _case(state: str = "remediating", remediation_kind: str = "patch") -> CtemCase:
    return CtemCase(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        vertical="cyber",
        title="Upgrade the VPN appliance",
        remediation_kind=remediation_kind,
        keys=["cve:CVE-2026-0001"],
        pinned_issues=[],
        state=state,
        state_since=NOW,
        validations=[],
    )


class _Session:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, row: Any) -> None:
        self.added.append(row)


@pytest.mark.asyncio
async def test_exposure_evidence_records_a_failed_conclusive_read_and_keeps_its_type() -> None:
    case = _case()
    await loop.record_validation(
        _Session(),  # type: ignore[arg-type]
        case,
        provider="intune",
        kind="inventory",
        ref="inventory-2026-11-25",
        result="failed",
        conclusive=True,
        evidence_type="exposure",
        now=NOW,
    )
    entry = case.validations[-1]
    assert entry["evidence_type"] == "exposure"
    assert entry["result"] == "failed"
    assert entry["conclusive"] is True
    assert case.state == "remediating"


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["passed", "not_exposed"])
async def test_a_pass_with_exposure_evidence_is_refused(result: str) -> None:
    case = _case()
    with pytest.raises(rules.CaseRuleError, match="exposure evidence"):
        await loop.record_validation(
            _Session(),  # type: ignore[arg-type]
            case,
            provider="intune",
            kind="inventory",
            ref="inventory-1",
            result=result,
            evidence_type="exposure",
            now=NOW,
        )
    assert case.validations == []


@pytest.mark.asyncio
async def test_exposure_evidence_on_a_config_case_is_kept_too() -> None:
    case = _case(remediation_kind="config")
    await loop.record_validation(
        _Session(),  # type: ignore[arg-type]
        case,
        provider="openaev",
        kind="check",
        ref="read-1",
        result="failed",
        conclusive=True,
        evidence_type="exposure",
        now=NOW,
    )
    assert case.validations[-1]["evidence_type"] == "exposure"


def test_the_tool_and_the_api_accept_exposure_evidence() -> None:
    assert "exposure" in loop.EVIDENCE_TYPES
    body = CaseValidation(provider="intune", ref="inventory-1", result="failed", evidence_type="exposure")
    assert body.evidence_type == "exposure"
    import inspect

    source = inspect.getsource(ctem_tools)
    assert '"enum": ["remediation", "validation", "exposure"]' in source
