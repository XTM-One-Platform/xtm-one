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

"""#2393: the detection desk proposes its gaps ON the CTEM case that launched the simulation."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents import openaev_detection_desk as desk
from app.models.ctem_case import CtemCaseEvent
from app.services import ctem_case_audit as audit
from app.services import ctem_cases_store as store
from app.services import ctem_cases_view as view_of
from app.services.builtin_tools import CTEM_CASE_TOOL_NAMES
from tests.test_ctem_case_tools import env  # noqa: F401 (fixture)

SIMULATION = "exercise-7"


@pytest.fixture
def filed(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from app.services import work_item as work_item_module

    created: list[dict[str, Any]] = []

    async def _create(**kw: Any) -> dict[str, str]:
        created.append(kw)
        return {"id": f"item-{len(created)}"}

    monkeypatch.setattr(work_item_module, "create_work_item", _create)
    return created


@pytest.fixture
def validated(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:  # noqa: F811
    """The case validated by the OpenAEV simulation SIMULATION."""

    async def _for_ref(_db: object, *, organization_id: object, provider: str, ref: str, limit: int = 5) -> list:
        return [env.case.id] if (provider, ref) == ("openaev", SIMULATION) else []

    monkeypatch.setattr(store, "case_ids_for_validation_ref", _for_ref)
    return env


async def _propose(case_env: SimpleNamespace, **kw: Any) -> str:
    decision_key = f"{desk.REMEDIATION_PREFIX}{kw.get('inject_id')}:{kw.get('asset_id')}"
    args = {
        "action": "propose",
        "validation_provider": "openaev",
        "simulation_id": SIMULATION,
        "decision_key": decision_key,
        **kw,
    }
    return await case_env.call("record_ctem_case_detection_gap", **args)


async def test_both_gaps_are_listed_on_the_case_with_draft_and_history(
    validated: SimpleNamespace, filed: list[dict[str, Any]]
) -> None:
    env = validated  # noqa: F811 (the case tools' fixture, as validated)
    first = json.loads(
        await _propose(
            env,
            inject_id="inj-1",
            asset_id="host-1",
            technique="T1059.001",
            root_cause="no_rule",
            remediation="Deploy the drafted Sigma rule on the SIEM.",
            rule_draft="title: powershell encoded\ndetection: ...",
            rule_language="sigma",
        )
    )
    second = json.loads(
        await _propose(
            env,
            inject_id="inj-2",
            asset_id="host-1",
            root_cause="broken_collector",
            remediation="Repair the Splunk collector, silent for 3 weeks.",
        )
    )
    assert first["case_id"] == second["case_id"] == str(env.case.id)
    # Every desk item carries the case, under the desk's own key (the gate trigger takes it).
    assert [i["payload"]["ctem_case_id"] for i in filed] == [str(env.case.id)] * 2
    assert [i["correlation_key"] for i in filed] == [
        f"{desk.REMEDIATION_PREFIX}inj-1:host-1",
        f"{desk.REMEDIATION_PREFIX}inj-2:host-1",
    ]
    assert all(i["needs_human"] is True and i["ask"]["recommendation"] == "done" for i in filed)
    assert "title: powershell encoded" in filed[0]["description"]

    case = json.loads(await env.call("get_ctem_case", case_id=str(env.case.id)))
    gaps = {g["key"]: g for g in case["detection_gaps"]}
    assert set(gaps) == {"inj-1:host-1", "inj-2:host-1"}
    assert gaps["inj-1:host-1"]["root_cause"] == "no_rule"
    assert gaps["inj-1:host-1"]["rule_draft"].startswith("title: powershell encoded")
    assert gaps["inj-1:host-1"]["decision_item_id"] == "item-1"
    assert gaps["inj-2:host-1"]["root_cause"] == "broken_collector"
    history = [h for h in case["history"] if h["kind"] == view_of.DETECTION_GAP_KIND]
    assert len(history) == 2


async def test_an_answer_on_the_case_is_recorded_in_its_history(
    validated: SimpleNamespace, filed: list[dict[str, Any]]
) -> None:
    env = validated  # noqa: F811 (the case tools' fixture, as validated)
    await _propose(env, inject_id="inj-1", asset_id="host-1", root_cause="no_rule", remediation="Deploy it.")
    # The answer comes a moment later.
    answered = await env.call(
        "record_ctem_case_detection_gap",
        action="answer",
        case_id=str(env.case.id),
        inject_id="inj-1",
        asset_id="host-1",
        outcome="done",
        by="soc lead",
    )
    gap = json.loads(answered)["gap"]
    assert gap["answer"]["outcome"] == "done" and gap["answer"]["by"] == "soc lead"
    events = [e for e in env.db.added if isinstance(e, CtemCaseEvent) and e.kind == view_of.DETECTION_GAP_KIND]
    assert [e.to_value for e in events] == ["no_rule", "answered:done"]


async def test_a_caseless_simulation_keeps_todays_items(
    validated: SimpleNamespace, filed: list[dict[str, Any]]
) -> None:
    env = validated  # noqa: F811 (the case tools' fixture, as validated)
    answer = await env.call(
        "record_ctem_case_detection_gap",
        action="propose",
        validation_provider="openaev",
        decision_key=f"{desk.REMEDIATION_PREFIX}inj-9:host-9",
        simulation_id="exercise-without-case",
        inject_id="inj-9",
        asset_id="host-9",
        root_cause="no_rule",
    )
    assert answer.startswith("No case")
    assert filed == []
    assert not [e for e in env.db.added if isinstance(e, CtemCaseEvent)]


async def test_bad_arguments_write_nothing(validated: SimpleNamespace, filed: list[dict[str, Any]]) -> None:
    env = validated  # noqa: F811 (the case tools' fixture, as validated)
    assert "inject_id" in await _propose(env, inject_id="", asset_id="host-1", root_cause="no_rule")
    assert "root_cause" in await _propose(env, inject_id="i", asset_id="a", root_cause="none")
    assert filed == [] and env.db.commits == 0


def test_detection_gaps_of_keeps_the_newest_proposal_and_its_answer() -> None:
    case_id, t0 = uuid.uuid4(), datetime.now(UTC)

    def ev(at: int, to: str, **kw: Any) -> CtemCaseEvent:
        return CtemCaseEvent(
            case_id=case_id,
            actor="a",
            kind="detection_gap",
            at=t0 + timedelta(seconds=at),
            from_value="i:a",
            to_value=to,
            **kw,
        )

    gaps = view_of.detection_gaps_of(
        [
            ev(2, "answered:declined", reason="bob"),
            ev(0, "no_log", evidence="{}"),
            ev(1, "no_rule", evidence=json.dumps({"rule_draft": "r"})),
        ]
    )
    assert len(gaps) == 1 and gaps[0]["root_cause"] == "no_rule" and gaps[0]["rule_draft"] == "r"
    assert gaps[0]["answer"]["outcome"] == "declined"
    assert audit.KIND_PHASE["detection_gap"] == "remediation"


def test_the_desk_binds_the_case_subset_only() -> None:
    orchestrator = next(a for a in desk.DETECTION_DESK_AGENTS if a["slug"] == desk.ORCHESTRATOR)
    assert "ctem" in orchestrator["builtin_tool_ids"]
    assert orchestrator["builtin_allowed_tools"] == {"ctem": ["get_ctem_case", "record_ctem_case_detection_gap"]}
    assert set(desk.CASE_TOOLS) <= set(CTEM_CASE_TOOL_NAMES)


def test_the_desk_prompts_name_the_case_tool() -> None:
    finished = next(a for a in desk.DETECTION_DESK_ASSIGNMENTS if a["name"] == "Simulation finished")
    assert "record_ctem_case_detection_gap" in finished["prompt"] and "no case" in finished["prompt"].lower()
