# SPDX-License-Identifier: Apache-2.0
"""A control_decay reconciliation reads what OpenAEV already runs on the asset (xtm-one #2368).

The reconciliation of #2354 named "the OpenAEV results on the asset" but the
only scenario read was a search by name, so a recurring scenario that targets
the asset's group was missed and a new one-off scan was proposed for a server
a recurring scenario already covered. get_openaev_asset_coverage reads the
asset's groups and every scenario, simulation and atomic testing whose injects
concern the asset, with the latest result and whether it can be reused.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.agents import builtin_agents as ba
from app.integrations.providers.openaev import build_tools
from app.integrations.providers.openaev.asset_coverage import usable_result
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.openaev.io||test-token"
ASSET = "asset-1"
GROUP = {"asset_group_id": "g-ovh", "asset_group_name": "OVHCloud Servers"}
SCORED = [{"type": "VULNERABILITY", "avgResult": "SUCCESS", "distribution": []}]


def _row(inject_id: str, exercise: str | None, status: str, results: Any, sent: str, **extra: Any) -> dict[str, Any]:
    return {
        "inject_id": inject_id,
        "inject_title": f"Nuclei scan {inject_id}",
        "inject_exercise": exercise,
        "inject_status": {"status_name": status, "tracking_sent_date": sent},
        "inject_expectation_results": results,
        "inject_updated_at": sent,
        **extra,
    }


class _Resp:
    def __init__(self, data: Any) -> None:
        self._data = data

    def json(self) -> Any:
        return self._data


class _Api:
    def __init__(self, rows: list[dict[str, Any]], *, groups: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows
        self.groups = [GROUP] if groups is None else groups
        self.calls: list[tuple[str, str]] = []

    async def __call__(
        self, base_url: str, headers: dict[str, str], method: str, path: str, action: str, **kw: Any
    ) -> tuple[_Resp | None, str | None]:
        self.calls.append((method, path))
        if path == f"/api/assets/{ASSET}":
            return _Resp({"asset_id": ASSET, "asset_name": "srv-1.ovh", "asset_asset_groups": self.groups}), None
        if path == f"/api/assets/{ASSET}/injects/search":
            return _Resp({"content": self.rows, "totalElements": len(self.rows)}), None
        if path == "/api/exercises/ex-weekly":
            return _Resp({"exercise_id": "ex-weekly", "exercise_scenario": "sc-weekly"}), None
        if path == "/api/scenarios/sc-weekly":
            return _Resp(
                {"scenario_id": "sc-weekly", "scenario_name": "Weekly OVH scan", "scenario_recurrence": "0 0 2 * * MON"}
            ), None
        return None, f"unexpected {method} {path}"


async def _run(monkeypatch: pytest.MonkeyPatch, api: _Api) -> str:
    from app.integrations.providers.openaev import asset_coverage

    monkeypatch.setattr(asset_coverage, "_api", api)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    result = await executor.execute_tool(
        ToolCall(id="c1", name="get_openaev_asset_coverage", arguments={"asset_id": ASSET})
    )
    assert not result.is_error, result.content
    return result.content


async def test_a_covered_asset_names_the_recurring_scenario_and_its_usable_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _Api(
        [
            _row("i1", "ex-weekly", "SUCCESS", SCORED, "2026-11-24T02:00:00Z"),
            _row("i0", "ex-weekly", "SUCCESS", SCORED, "2026-11-17T02:00:00Z"),
        ]
    )
    out = await _run(monkeypatch, api)
    assert "Asset groups (static and dynamic membership): OVHCloud Servers (id: g-ovh)" in out
    assert "Scenario Weekly OVH scan (id: sc-weekly), recurring (0 0 2 * * MON)" in out
    assert "at 2026-11-24T02:00:00Z" in out
    assert "Usable as evidence: yes (VULNERABILITY SUCCESS)." in out
    assert "nothing covers it" not in out
    # One read per object: the asset, the injects page, the simulation, the scenario.
    assert ("post", f"/api/assets/{ASSET}/injects/search") in api.calls
    assert ("get", "/api/scenarios/sc-weekly") in api.calls


async def test_an_unusable_last_result_says_why_and_proposes_re_running_that_scenario(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _Api([_row("i1", "ex-weekly", "SUCCESS", [], "2026-11-24T02:00:00Z")])
    out = await _run(monkeypatch, api)
    assert "Usable as evidence: no, it executed but declares no expectation" in out
    assert "re-run this scenario (manage_openaev_scenario action launch)" in out


async def test_no_covering_scenario_says_nothing_covers_the_asset(monkeypatch: pytest.MonkeyPatch) -> None:
    out = await _run(monkeypatch, _Api([], groups=[]))
    assert "Asset groups: none." in out
    assert "nothing covers it in OpenAEV" in out


async def test_an_atomic_testing_is_its_own_entry_with_a_relaunch(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _Api([_row("at1", None, "PENDING", [], "2026-11-24T02:00:00Z")])
    out = await _run(monkeypatch, api)
    assert "Atomic testing Nuclei scan at1 (id: at1)" in out
    assert "relaunch this atomic testing" in out


def test_usable_result_rules() -> None:
    assert usable_result(_row("a", None, "SUCCESS", SCORED, "t"))[0] is True
    assert usable_result(_row("a", None, "SUCCESS", SCORED, "t", inject_enabled=False)) == (
        False,
        "the inject is disabled, so it never runs",
    )
    pending = [{"type": "DETECTION", "avgResult": "PENDING"}]
    ok, why = usable_result(_row("a", None, "SUCCESS", pending, "t"))
    assert not ok and "not scored yet" in why


def test_the_reconciliation_names_the_coverage_read_and_the_re_run() -> None:
    step = ba.CTEM_CONTROL_DECAY_RECONCILE
    assert "get_openaev_asset_coverage" in step
    assert "static and dynamic" in step and "recurrence" in step
    assert "a scenario search by name is not this read" in step
    assert "cites each covering scenario" in step
    assert "proposes re-running that scenario" in step
    assert "new one-off scan is proposed only when nothing covers the asset" in step
    assert step in ba.CTEM_F1_CASE_ASSIGNMENT["prompt"]


def test_the_loop_persona_reads_the_coverage_before_proposing_a_scan() -> None:
    persona = (
        Path(ba.__file__).parent / "seed_data" / "agent_persona" / "ctem-threat-response-orchestrator.md"
    ).read_text(encoding="utf-8")
    start = persona.index("## A control_decay case: reconcile the controls first")
    section = persona[start : persona.index("\n## ", start + 10)]
    assert "`get_openaev_asset_coverage`" in section
    assert "re-running that scenario, not a new one" in section
    assert "only when nothing covers the asset" in section
