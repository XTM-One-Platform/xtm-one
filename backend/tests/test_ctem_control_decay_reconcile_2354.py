# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""A control_decay case is reconciled with the controls known elsewhere before a scan (#2354)."""

from __future__ import annotations

from pathlib import Path

from app.agents import builtin_agents as ba
from app.services.organization_asset_identity import BUILTIN_SOURCE_CLASSES
from app.services.organization_context_categories import render_asset_page, split_asset_query

_PERSONA = Path(ba.__file__).parent / "seed_data" / "agent_persona" / "ctem-threat-response-orchestrator.md"


def _persona() -> str:
    return _PERSONA.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start : end if end != -1 else len(text)]


def test_the_loop_persona_reconciles_a_control_decay_case_before_the_loop() -> None:
    persona = _persona()
    section = _section(persona, "## A control_decay case: reconcile the controls first")
    # Read before the threat loop proposes a validation or a scan.
    assert persona.index("## A control_decay case") < persona.index("## The loop, one case per threat")
    for state in ("never_measured", "attested_only", "expired"):
        assert state in section
    # The sources: OpenAEV on the asset, EDR / device-management coverage, what OpenCRQ holds.
    assert "OpenAEV results on the asset" in section
    assert "`get_asset` with the asset name" in section and "sources" in section
    assert "get_opencrq_control_validation" in section
    # A control found: record it in OpenCRQ through one remediation decision, never a scan.
    assert "`ctem:remediation:` followed by the case id" in section
    assert "update_opencrq_control_capability_assessment" in section
    assert "manage_opencrq_scope_control_posture" in section
    assert "never a scan" in section
    # None found: the current behaviour. Every source checked is listed with what it returned.
    assert "No control found anywhere: propose putting one in place" in section
    assert "found, not found, or not readable" in section
    # A known control past its evidence keeps its re-test.
    assert "re-test or measure it as before" in section


def test_the_case_prompt_names_the_reconciliation_and_the_record_in_opencrq_outcome() -> None:
    prompt = ba.CTEM_F1_CASE_ASSIGNMENT["prompt"]
    assert ba.CTEM_CONTROL_DECAY_RECONCILE in prompt
    step = ba.CTEM_CONTROL_DECAY_RECONCILE
    assert "control_decay" in step and "never_measured" in step
    assert "before any validation, scan or new control is proposed" in step
    assert "get_asset with the asset name" in step and "get_opencrq_control_validation" in step
    assert "record it in OpenCRQ" in step and "never a scan" in step
    assert "put a control in place" in step
    assert "found, not found, not readable" in step
    # The step is read on the opened case, before the SLA and validation branches.
    assert prompt.index(step) < prompt.index("On ctem_case.sla_breached")
    assert prompt.index(step) < prompt.index("On ctem_case.validation_finished")


def test_the_orchestrator_gains_no_write_binding() -> None:
    orchestrator = next(a for a in ba.BUILTIN_AGENTS if a["slug"] == "ctem-threat-response-orchestrator")
    # Coverage is read from the inventory the EDR / device-management sources sync into,
    # so the loop holds no new integration (and announces none as missing).
    assert orchestrator["integration_template_bindings"] == ["opencti", "openaev", "opencrq"]


def test_edr_and_device_management_coverage_is_readable_by_source_from_the_inventory() -> None:
    # The sources the persona asks to filter on are inventory source classes.
    assert {BUILTIN_SOURCE_CLASSES[s] for s in ("defender", "crowdstrike")} == {"edr"}
    assert {BUILTIN_SOURCE_CLASSES[s] for s in ("intune", "tanium")} == {"mdm"}
    filters, text = split_asset_query("srv-db-01 source:defender")
    assert filters == {"source": ["defender"]} and text == "srv-db-01"


def test_an_asset_no_source_covers_reads_as_not_found_never_guessed() -> None:
    answer = render_asset_page(
        [], total=0, query="srv-db-01 source:defender", offset=0, limit=50, held={"source": ["intune"]}
    )
    assert "no row" in answer and "Do not guess it." in answer
    assert "source holds: intune." in answer
