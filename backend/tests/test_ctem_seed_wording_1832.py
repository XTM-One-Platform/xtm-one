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

"""The seeded CTEM fleets say one thing in their personas and prompts (#1832)."""

from __future__ import annotations

from pathlib import Path

from app.agents import builtin_agents as ba
from app.agents import opencrq_risk_budget_desk as crq
from app.agents import opencti_pir_desk as pir

_PERSONAS = Path(ba.__file__).parent / "seed_data" / "agent_persona"


def _persona(slug: str) -> str:
    return (_PERSONAS / f"{slug}.md").read_text(encoding="utf-8")


def _f1(name: str) -> dict:
    return next(a for a in ba.CTEM_F1_ASSIGNMENTS if a["name"] == name)


def _f2(name: str) -> dict:
    return next(a for a in ba.CTEM_F2_ASSIGNMENTS if a["name"] == name)


def test_loop_persona_keys_the_launch_gate_by_case_and_names_the_ticket_tool_of_the_prompt() -> None:
    persona = _persona("ctem-threat-response-orchestrator")
    assert "ctem:f1:launch:" not in persona
    assert "`ctem:validation:` followed by the case id" in persona
    assert "record_ctem_case_ticket" not in persona
    assert "sync_ctem_case_ticket" in persona
    assert "Your run prompt states which" not in persona
    assert "sync_ctem_case_ticket" in ba.CTEM_F1_CASE_ASSIGNMENT["prompt"]


def test_a_retest_has_one_path_the_event_files_the_item() -> None:
    case_prompt = ba.CTEM_F1_CASE_ASSIGNMENT["prompt"]
    assert "run the case's re-test" not in case_prompt
    assert "`ctem:f1:retest:` followed by the case_id" in case_prompt
    assert "never run the re-test in this run" in case_prompt
    assert "`ctem:f2:retest:` followed by the case_id" in ba.CTEM_F2_CASE_ASSIGNMENT["prompt"]
    for retest in (_f1("Re-test due"), _f2("Re-test due")):
        prompt = retest["prompt"]
        assert "`ctem:validation:` followed by the case_id" in prompt
        assert "ctem_case.validation_finished" in prompt
        assert "Never wait for the result in this run" in prompt


def test_a_gate_decision_is_filed_with_dedupe_by_key_so_a_resumed_run_files_it_once() -> None:
    # A worker restart resumes the run and replays a completed create_work_item: without
    # dedupe the case got a second decision card for the same gate.
    for slug in ("ctem-threat-response-orchestrator", "ctem-vuln-prioritization-orchestrator"):
        persona = _persona(slug)
        assert "filed with `dedupe` true and `dedupe_by` \"key\"" in persona, slug


def test_threat_activity_flagged_in_a_pir_has_one_owner() -> None:
    loop_types = set(ba.CTEM_F1_ASSIGNMENTS[1]["trigger_config"]["provision"]["entity_types"])
    assert ba.CTEM_F1_ASSIGNMENTS[1]["name"] == ba.CTEM_F1_PIR_INTAKE
    assert not loop_types & set(pir.TRIGGER_ENTITY_TYPES)
    assert "once" in next(a for a in pir.PIR_DESK_ASSIGNMENTS if a["name"] == "PIR match created")["prompt"]


def test_posture_role_reads_the_cases() -> None:
    agent = next(a for a in ba.BUILTIN_AGENTS if a["slug"] == ba.CTEM_POSTURE_ORCHESTRATOR_SLUG)
    assert "ctem_read" in agent["builtin_tool_ids"]
    assert "posture and reporting fleet" not in agent["persona"]
    assert "CTEM loop" in agent["persona"]


def test_risk_desk_options_state_currency_rate_and_preconditions() -> None:
    persona = _persona("opencrq-risk-desk-orchestrator")
    assert "approve and reject options" not in persona
    assert "an amount always states its currency" in persona
    assert "preconditions" in persona


def test_weekly_sweep_records_signals_over_recent_activity() -> None:
    sweep = _f1("Weekly threat sweep")
    assert "opens a case" not in sweep["description"]
    assert "records a threat signal" in sweep["description"]
    assert "updated" in sweep["prompt"]


def _cron(assignments: list[dict], name: str) -> str:
    return next(a for a in assignments if a["name"] == name)["trigger_config"]["cron"]


def test_monday_and_first_of_month_schedules_do_not_pile_up() -> None:
    crons = [
        _cron(ba.CTEM_F1_ASSIGNMENTS, "Weekly threat sweep"),
        _cron(pir.PIR_DESK_ASSIGNMENTS, "Weekly PIR answer"),
        _cron(crq.RISK_BUDGET_DESK_ASSIGNMENTS, "Weekly investment review"),
    ]
    assert len(set(crons)) == len(crons)
    pack = _cron(crq.RISK_BUDGET_DESK_ASSIGNMENTS, "Monthly Board pack")
    report = _cron(ba.CTEM_POSTURE_ASSIGNMENTS, "Monthly board report")
    # The board report reads the briefing (08:00) and the Board pack: it runs after both.
    assert int(pack.split()[1]) > 8
    assert int(report.split()[1]) > int(pack.split()[1])
