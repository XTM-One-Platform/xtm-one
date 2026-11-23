"""#1458: the validation gate states an inject's real side effects; a negative exploit attempt never resolves a case.

The classification is the platform's, read from the inject itself (a Nuclei
template, else the contract's flag, else unknown); the gate ask is built from
it; ``safe_mode`` is honoured only for a read-only inject; a pass of an
intrusive or unknown inject is inconclusive and the case stays open.
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
from app.services import ctem_inject_effects as effects_of

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)

#: An active exploit of an unauthenticated workflow API (shape of a public Nuclei CVE template).
EXPLOIT_TEMPLATE = """
id: CVE-2020-11978
info:
  name: Workflow scheduler - Remote Code Execution
  severity: high
  tags: cve,cve2020,apache,airflow,rce,kev
http:
  - raw:
      - |
        GET /api/experimental/test HTTP/1.1
        Host: {{Hostname}}
      - |
        GET /api/experimental/dags/example_trigger_target_dag/paused/false HTTP/1.1
        Host: {{Hostname}}
      - |
        POST /api/experimental/dags/example_trigger_target_dag/dag_runs HTTP/1.1
        Host: {{Hostname}}
        Content-Type: application/json

        {"conf": {"message": "\\"; touch test #"}}
      - |
        GET /api/experimental/dags/example_trigger_target_dag/dag_runs/{{exec_date}}/tasks/bash_task HTTP/1.1
        Host: {{Hostname}}
    matchers:
      - type: word
        words:
          - '"operator":"BashOperator"'
"""

#: A version read: one GET, matchers and an extractor.
VERSION_TEMPLATE = """
id: web-app-version-detect
info:
  name: Web application version
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

#: An authenticated file read: it logs in with variables the contract does not supply.
AUTHENTICATED_TEMPLATE = """
id: CVE-2024-9264
info:
  name: Dashboard - authenticated file read
  severity: critical
  tags: cve,cve2024,authenticated,sqli,lfr
http:
  - raw:
      - |
        POST /login HTTP/1.1
        Host: {{Hostname}}
        Content-Type: application/json

        {"user":"{{username}}","password":"{{password}}"}
      - |
        POST /api/ds/query HTTP/1.1
        Host: {{Hostname}}
        Content-Type: application/json

        {"queries":[{"expression":"SELECT content FROM read_blob('/etc/passwd')"}]}
"""


def test_the_exploit_template_is_intrusive_and_names_its_side_effects() -> None:
    effects = effects_of.classify_nuclei_template(EXPLOIT_TEMPLATE)
    assert effects.classification == effects_of.INTRUSIVE
    text = " ".join(effects.side_effects)
    assert "GET /api/experimental/dags/example_trigger_target_dag/paused/false" in text
    assert "POST /api/experimental/dags/example_trigger_target_dag/dag_runs" in text
    assert "command injection executed on the target" in text
    assert effects.cleanup and not effects.needs_credentials
    assert effects.negative_result.startswith("inconclusive")


def test_a_get_only_version_template_is_read_only() -> None:
    effects = effects_of.classify_nuclei_template(VERSION_TEMPLATE)
    assert effects.classification == effects_of.READ_ONLY
    assert effects.side_effects == [] and effects.cleanup == []


def test_a_template_needing_credentials_cannot_conclude() -> None:
    effects = effects_of.classify_nuclei_template(AUTHENTICATED_TEMPLATE)
    assert effects.needs_credentials is True
    assert effects.classification == effects_of.INTRUSIVE
    assert effects.negative_result.startswith("meaningless")
    assert effects.to_dict()["runs_unauthenticated"] is False


@pytest.mark.parametrize("text", ["", "not: [valid", "id: x\ninfo: {}\n"])
def test_an_unreadable_template_is_unknown(text: str) -> None:
    assert effects_of.classify_nuclei_template(text).classification == effects_of.UNKNOWN


def test_without_a_template_the_contract_flag_decides_else_unknown() -> None:
    assert effects_of.classify_inject(contract_read_only=True).classification == effects_of.READ_ONLY
    assert effects_of.classify_inject(contract_read_only=False).classification == effects_of.INTRUSIVE
    assert effects_of.classify_inject(injector="custom").classification == effects_of.UNKNOWN


def test_the_gate_ask_of_the_exploit_states_its_effects_and_does_not_recommend_approve() -> None:
    effects = effects_of.classify_nuclei_template(EXPLOIT_TEMPLATE)
    ask = effects_of.validation_gate_ask(
        effects, subject="CVE-2020-11978", passive_option="Read the running version only"
    )
    words = (ask["context"] + " " + json.dumps(ask["options"])).lower()
    for banned in ("safe", "read-only", "nothing left"):
        assert banned not in words
    assert "paused/false" in ask["context"] and "command injection" in ask["context"]
    assert ask["recommendation"] != "A"
    assert any(o["label"] == "Read the running version only" for o in ask["options"])
    assert ask["recommendation"] == "B" and ask["options"][1]["label"] == "Read the running version only"


def test_the_gate_ask_of_a_template_needing_credentials_never_offers_not_exposed() -> None:
    effects = effects_of.classify_nuclei_template(AUTHENTICATED_TEMPLATE)
    ask = effects_of.validation_gate_ask(effects, subject="CVE-2024-9264")
    assert "not exposed" not in json.dumps(ask).lower()
    assert "Runs without credentials: no" in ask["context"]


def test_a_read_only_or_lab_inject_may_be_recommended() -> None:
    read_only = effects_of.classify_nuclei_template(VERSION_TEMPLATE)
    assert effects_of.validation_gate_ask(read_only, subject="x")["recommendation"] == "A"
    exploit = effects_of.classify_nuclei_template(EXPLOIT_TEMPLATE)
    assert effects_of.validation_gate_ask(exploit, subject="x", target_is_lab=True)["recommendation"] == "A"


#: One unauthenticated POST, no version read (shape of a public object-storage CVE template).
POST_ONLY_TEMPLATE = """
id: CVE-2023-28432
info:
  name: Object storage - information disclosure
  severity: high
  tags: cve,cve2023,minio
http:
  - method: POST
    path:
      - "{{BaseURL}}/minio/bootstrap/v1/verify"
"""


def test_a_requested_cve_without_a_template_is_not_tested_and_not_promised() -> None:
    cover = effects_of.template_coverage([POST_ONLY_TEMPLATE], ["CVE-2023-28432", "CVE-2023-28434"])
    assert cover.covered == ["CVE-2023-28432"] and cover.uncovered == ["CVE-2023-28434"]
    assert cover.reads_version is False and cover.state == "partial"
    effects = effects_of.classify_templates([POST_ONLY_TEMPLATE])
    ask = effects_of.validation_gate_ask(effects, subject="CVE-2023-28432, CVE-2023-28434", coverage=cover)
    assert "Not tested (no template): CVE-2023-28434" in ask["context"]
    assert "CVE-2023-28434" not in ask["question"]
    assert "does not read the running version" in ask["context"]


def test_a_version_extractor_counts_as_a_version_read() -> None:
    assert effects_of.template_coverage([VERSION_TEMPLATE], []).reads_version is True


def test_safe_mode_is_honoured_only_for_a_read_only_inject() -> None:
    assert effects_of.checked_safe_mode(True, effects_of.READ_ONLY) == (True, None)
    corrected, warning = effects_of.checked_safe_mode(True, effects_of.INTRUSIVE)
    assert corrected is False and warning and "intrusive" in warning
    assert effects_of.checked_safe_mode(True, None)[0] is False
    assert effects_of.checked_safe_mode(False, effects_of.INTRUSIVE) == (False, None)


# --- The case loop -----------------------------------------------------------


class _Session:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, row: Any) -> None:
        self.added.append(row)


def _case(state: str = "needs_attention") -> CtemCase:
    return CtemCase(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        vertical="cyber",
        title="Upgrade the workflow scheduler",
        remediation_kind="upgrade",
        keys=["upgrade:scheduler@2"],
        pinned_issues=[],
        state=state,
        state_since=NOW,
        validations=[],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["passed", "not_exposed"])
async def test_a_negative_intrusive_validation_leaves_the_case_open(result: str) -> None:
    case, events = _case(), loop.PendingEvents()
    await loop.record_validation(
        _Session(),  # type: ignore[arg-type]
        case,
        provider="openaev",
        kind="validation",
        ref="inject-1",
        result=result,
        safe_mode=True,
        inject_class=effects_of.INTRUSIVE,
        events=events,
        now=NOW,
    )
    entry = case.validations[-1]
    assert case.state == "needs_attention" and case.resolution_kind is None
    assert entry["conclusive"] is False and entry["safe_mode"] is False and entry["inject_class"] == "intrusive"
    assert loop.CASE_RESOLVED not in [n for n, _, _ in events.items]
    assert rules.passing(entry) is False


@pytest.mark.asyncio
async def test_a_conclusive_version_check_resolves_the_case() -> None:
    case = _case()
    await loop.record_validation(
        _Session(),  # type: ignore[arg-type]
        case,
        provider="openaev",
        kind="retest",
        ref="inject-2",
        result="passed",
        inject_class=effects_of.INTRUSIVE,
        conclusive=True,
        now=NOW,
    )
    assert case.state == "resolved" and rules.passing(case.validations[-1])


@pytest.mark.asyncio
async def test_a_read_only_inject_keeps_safe_mode_and_resolves() -> None:
    case = _case()
    await loop.record_validation(
        _Session(),  # type: ignore[arg-type]
        case,
        provider="openaev",
        kind="retest",
        ref="inject-3",
        result="passed",
        safe_mode=True,
        inject_class=effects_of.READ_ONLY,
        evidence_type=effects_of.REMEDIATION_EVIDENCE,
        now=NOW,
    )
    assert case.state == "resolved" and case.validations[-1]["safe_mode"] is True
    assert "conclusive" not in case.validations[-1]


@pytest.mark.asyncio
async def test_an_unknown_inject_class_is_refused() -> None:
    with pytest.raises(rules.CaseRuleError):
        await loop.record_validation(
            _Session(),  # type: ignore[arg-type]
            _case(),
            provider="openaev",
            kind="retest",
            ref="r",
            result="passed",
            inject_class="harmless",
            now=NOW,
        )
