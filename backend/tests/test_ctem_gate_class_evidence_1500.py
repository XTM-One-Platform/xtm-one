"""#1500: the CTEM validation and mobilize gates follow the inject class, and a patch case needs remediation evidence.

1. The gate title and safe-mode line follow the class; a non-read-only gate is never called read-only or safe.
2. An authored payload is unknown (handled as intrusive); its exact command is in the ask, else a payload review
   comes before the launch decision.
3. The ask lists the target count and its delta against the inventory the context cites.
4. The mobilize done criterion follows the re-test class; a negative exploit re-test never closes a ticket; the
   hold option depends on no other gate.
5. A patch or upgrade case resolves only on remediation evidence; a test result is validation evidence only.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.models.ctem_case import CtemCase
from app.services import ctem_case_loop as loop
from app.services import ctem_cases as rules
from app.services import ctem_inject_effects as fx

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)

EXPLOIT_TEMPLATE = """
id: example-rce
info:
  name: Example - Remote Code Execution
  severity: high
  tags: cve,rce
http:
  - raw:
      - |
        POST /api/run HTTP/1.1
        Host: {{Hostname}}

        {"cmd": "; id"}
"""

VERSION_TEMPLATE = """
id: example-version
info:
  name: Example version
  severity: info
  tags: tech,detect
http:
  - method: GET
    path:
      - "{{BaseURL}}/api/version"
    extractors:
      - type: regex
        regex:
          - '"version":"([0-9.]+)"'
"""


# --- 1. Title and safe mode by class -------------------------------------------


@pytest.mark.parametrize("klass", [fx.INTRUSIVE, fx.UNKNOWN])
def test_a_non_read_only_gate_is_never_titled_or_described_safe(klass: str) -> None:
    effects = fx.InjectEffects(klass, side_effects=["changes the target"])
    ask = fx.validation_gate_ask(effects, subject="CVE-0000-0001")
    assert ask["safe_mode"] is False
    assert ask["safe_mode_text"].startswith("Safe mode: no")
    title = ask["title"].lower()
    assert "read-only" not in title and "safe" not in title
    assert fx.wording_problems(klass, ask["title"] + "\n" + ask["context"]) == []


def test_a_read_only_gate_is_titled_read_only_with_safe_mode_yes() -> None:
    ask = fx.validation_gate_ask(fx.classify_nuclei_template(VERSION_TEMPLATE), subject="CVE-0000-0002")
    assert ask["title"].startswith("Read-only validation") and ask["safe_mode"] is True
    assert ask["safe_mode_text"].startswith("Safe mode: yes")


def test_a_drafted_safe_wording_on_an_intrusive_gate_is_refused() -> None:
    draft = "Read-only exposure check. Safe mode: yes, it only reads."
    problems = fx.wording_problems(fx.INTRUSIVE, draft)
    assert problems and any("read-only" in p for p in problems)
    assert fx.wording_problems(fx.READ_ONLY, draft) == []


# --- 2. Authored payload --------------------------------------------------------


def test_an_authored_payload_is_unknown_even_with_a_read_only_contract_flag() -> None:
    effects = fx.classify_inject(injector="command", contract_read_only=True, authored_payload=True)
    assert effects.classification == fx.UNKNOWN and effects.intrusive_or_unknown
    assert effects.source == "authored_payload"


def test_the_ask_of_an_authored_command_embeds_its_exact_text() -> None:
    command = "Get-ItemProperty 'HKLM:\\Software\\Example' | Select-Object Version"
    effects = fx.classify_inject(injector="command", command=command)
    ask = fx.validation_gate_ask(effects, subject="CVE-0000-0003")
    assert command in ask["context"]
    assert ask["payload_review_first"] is False and ask["recommendation"] != "A"


def test_an_authored_payload_without_its_text_asks_for_a_payload_review_first() -> None:
    effects = fx.classify_inject(injector="command", authored_payload=True)
    ask = fx.validation_gate_ask(effects, subject="CVE-0000-0004")
    assert ask["payload_review_first"] is True and ask["recommendation"] == "A"
    assert ask["options"][0]["label"].startswith("Review the payload first")
    assert not any(o["label"].startswith("Run it") for o in ask["options"])


# --- 3. Target count and delta --------------------------------------------------


def test_the_ask_lists_the_targets_and_the_delta_against_the_cited_inventory() -> None:
    effects = fx.classify_nuclei_template(EXPLOIT_TEMPLATE)
    ask = fx.validation_gate_ask(
        effects, subject="CVE-0000-0005", target_count=82, inventory_count=77, inventory_label="workstations"
    )
    assert "Targets: 82, 5 more than the 77 workstations the inventory cites" in ask["context"]


def test_target_lines_for_an_equal_a_smaller_and_an_uncounted_set() -> None:
    assert fx.target_line(10, 10) == "Targets: 10, as many as the 10 the inventory cites."
    assert "2 fewer than the 12" in fx.target_line(10, 12)
    assert fx.target_line(None).startswith("Targets: not counted")
    assert fx.target_line(4) == "Targets: 4."


# --- 4. Mobilize done criterion -------------------------------------------------


def test_an_intrusive_retest_is_never_a_silent_done_criterion() -> None:
    effects = fx.classify_nuclei_template(EXPLOIT_TEMPLATE)
    block = fx.mobilize_done_criterion(effects, remediation_kind="config", target_count=82)
    assert "intrusive" in block["done_criterion"] and "82 target(s)" in block["done_criterion"]
    assert block["negative_retest"] == "inconclusive"
    assert any("never closes the ticket alone" in line for line in block["lines"])


def test_an_intrusive_retest_with_a_version_check_names_the_non_intrusive_check() -> None:
    effects = fx.classify_nuclei_template(EXPLOIT_TEMPLATE)
    block = fx.mobilize_done_criterion(effects, remediation_kind="config", version_check="read the service version")
    assert "non-intrusive check" in block["done_criterion"] and "read the service version" in block["done_criterion"]


def test_the_hold_option_depends_on_no_other_gate() -> None:
    block = fx.mobilize_done_criterion(None)
    text = json.dumps(block["hold_option"]).lower()
    assert "gate" not in text and "pending" not in text and "if " not in text
    assert block["retest_class"] == fx.UNKNOWN


# --- 5. Remediation evidence only on a patch case --------------------------------


def test_a_patch_case_ask_states_the_evidence_type_of_each_option() -> None:
    effects = fx.classify_nuclei_template(EXPLOIT_TEMPLATE)
    ask = fx.validation_gate_ask(
        effects,
        subject="CVE-0000-0006",
        passive_option="Read the installed version on every endpoint",
        passive_reads_version=True,
        remediation_kind="upgrade",
    )
    kinds = {o["label"]: o["evidence"] for o in ask["options"]}
    assert kinds["Run it against the asset"] == fx.VALIDATION_EVIDENCE
    assert kinds["Read the installed version on every endpoint"] == fx.REMEDIATION_EVIDENCE
    assert kinds["Do not run it"] == "none"
    assert "resolved only by remediation evidence" in ask["context"]
    block = fx.mobilize_done_criterion(effects, remediation_kind="patch")
    assert block["evidence"] == fx.REMEDIATION_EVIDENCE and "version inventory" in block["done_criterion"]


class _Session:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, row: Any) -> None:
        self.added.append(row)


def _case(kind: str = "upgrade") -> CtemCase:
    return CtemCase(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        vertical="cyber",
        title="Upgrade a component",
        remediation_kind=kind,
        keys=[f"{kind}:component"],
        pinned_issues=[],
        state="remediating",
        state_since=NOW,
        validations=[],
    )


async def _record(case: CtemCase, **kw: Any) -> CtemCase:
    return await loop.record_validation(
        _Session(),  # type: ignore[arg-type]
        case,
        provider="openaev",
        kind=kw.pop("kind", "retest"),
        ref="r1",
        result=kw.pop("result", "passed"),
        now=NOW,
        **kw,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("klass", [fx.READ_ONLY, fx.INTRUSIVE])
async def test_a_test_result_never_resolves_a_patch_case(klass: str) -> None:
    case = await _record(_case("patch"), inject_class=klass, evidence_type=fx.VALIDATION_EVIDENCE)
    entry = case.validations[-1]
    assert case.state == "remediating" and entry["evidence_type"] == fx.VALIDATION_EVIDENCE
    assert entry["conclusive"] is False and rules.passing(entry) is False


@pytest.mark.asyncio
async def test_an_unlabelled_detection_pass_on_an_upgrade_case_is_validation_evidence() -> None:
    case = await _record(_case("upgrade"), kind="detection")
    assert case.state == "remediating" and case.validations[-1]["evidence_type"] == fx.VALIDATION_EVIDENCE


@pytest.mark.asyncio
async def test_a_version_inventory_resolves_a_patch_case() -> None:
    case = await _record(_case("patch"), evidence_type=fx.REMEDIATION_EVIDENCE)
    assert case.state == "resolved" and rules.passing(case.validations[-1])


@pytest.mark.asyncio
async def test_a_non_version_case_keeps_resolving_on_a_pass() -> None:
    case = await _record(_case("config"))
    assert case.state == "resolved" and "evidence_type" not in case.validations[-1]


@pytest.mark.asyncio
async def test_an_unknown_evidence_type_is_refused() -> None:
    with pytest.raises(rules.CaseRuleError):
        await _record(_case(), evidence_type="hunch")
