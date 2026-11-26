# SPDX-License-Identifier: Apache-2.0
"""A validation launch on a sample of the group runs a retargeted copy (#2482).

``manage_openaev_scenario`` ``launch`` with ``asset_ids`` duplicates the
scenario, retargets every inject of the copy to exactly those assets (no asset
group), launches the copy and leaves the original scenario unchanged. Without
``asset_ids`` the launch is the default scope, as before. HTTP is mocked.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from app.integrations.providers.openaev import build_tools, simulations
from app.integrations.providers.openaev.simulations import sample_targets_change
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.openaev.io||test-token"
GROUP = "group-g"
GROUP_ASSETS = ["a1", "a2", "a3", "a4", "a5"]
SAMPLE = ["a1", "a3", "a5"]


class _Resp:
    def __init__(self, data: Any) -> None:
        self._data = data

    def json(self) -> Any:
        return self._data


def _inject(inject_id: str) -> dict[str, Any]:
    return {
        "inject_id": inject_id,
        "inject_title": f"Inject {inject_id}",
        "inject_injector_contract": {"injector_contract_id": "contract-1"},
        "inject_content": {"asset_groups": [GROUP], "command": "whoami"},
        "inject_teams": [],
        "inject_assets": [],
        "inject_asset_groups": [{"asset_group_id": GROUP}],
        "inject_tags": [],
        "inject_enabled": True,
    }


class _OpenAEV:
    """Two scenarios: the original s1 on group G and the copy OpenAEV mints."""

    def __init__(self, fail_put: bool = False) -> None:
        self.scenarios: dict[str, dict[str, dict[str, Any]]] = {"s1": {"i1": _inject("i1"), "i2": _inject("i2")}}
        self.calls: list[tuple[str, str]] = []
        self.fail_put = fail_put

    async def api(
        self, base_url: str, headers: dict[str, str], method: str, path: str, action: str, **kw: Any
    ) -> tuple[_Resp | None, str | None]:
        self.calls.append((method, path))
        parts = path.strip("/").split("/")
        if method == "post" and path.endswith("/exercise/running"):
            return _Resp({"simulation_id": f"sim-of-{parts[2]}"}), None
        if method == "get" and path.startswith("/api/exercises/"):
            return _Resp({"exercise_name": "Run", "exercise_status": "RUNNING"}), None
        if method == "post" and len(parts) == 3:  # duplicate
            self.scenarios["copy"] = copy.deepcopy(self.scenarios[parts[2]])
            return _Resp({"scenario_id": "copy"}), None
        if method == "delete" and len(parts) == 3:
            self.scenarios.pop(parts[2], None)
            return _Resp({}), None
        if method == "get" and len(parts) == 4:
            return _Resp(list(self.scenarios[parts[2]].values())), None
        if method == "get" and len(parts) == 5:
            return _Resp(self.scenarios[parts[2]][parts[4]]), None
        if method == "put" and len(parts) == 5:
            if self.fail_put:
                return None, "OpenAEV error (HTTP 400): Bad Request"
            body = kw["json"]
            stored = self.scenarios[parts[2]][parts[4]]
            stored["inject_assets"] = [{"asset_id": a} for a in body["inject_assets"]]
            stored["inject_asset_groups"] = [{"asset_group_id": g} for g in body["inject_asset_groups"]]
            stored["inject_content"] = body["inject_content"]
            return _Resp(stored), None
        raise AssertionError((method, path))


async def _launch(arguments: dict[str, Any]) -> str:
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    result = await executor.execute_tool(
        ToolCall(
            id="c1", name="manage_openaev_scenario", arguments={"action": "launch", "scenario_id": "s1", **arguments}
        )
    )
    assert not result.is_error, result.content
    return result.content


def _targets(inject: dict[str, Any]) -> tuple[list[str], list[str]]:
    assets = [a["asset_id"] if isinstance(a, dict) else a for a in inject["inject_assets"]]
    groups = [g["asset_group_id"] if isinstance(g, dict) else g for g in inject["inject_asset_groups"]]
    return assets, groups


async def test_a_sample_of_three_launches_a_copy_on_exactly_those_assets(monkeypatch: pytest.MonkeyPatch) -> None:
    aev = _OpenAEV()
    monkeypatch.setattr(simulations, "_api", aev.api)
    content = await _launch({"asset_ids": SAMPLE})

    for inject in aev.scenarios["copy"].values():
        assert _targets(inject) == (SAMPLE, [])
        assert inject["inject_content"]["asset_groups"] == []
        assert inject["inject_content"]["command"] == "whoami"
    for inject in aev.scenarios["s1"].values():
        assert _targets(inject) == ([], [GROUP])
    assert ("post", "/api/scenarios/copy/exercise/running") in aev.calls
    assert ("post", "/api/scenarios/s1/exercise/running") not in aev.calls
    assert not [c for c in aev.calls if c[0] == "put" and "/s1/" in c[1]]
    assert "scenario s1 left unchanged" in content
    assert "a1, a3, a5" in content and "measures only these assets" in content
    assert "Launched scenario copy" in content


async def test_without_asset_ids_the_default_scope_launches_the_scenario_as_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aev = _OpenAEV()
    monkeypatch.setattr(simulations, "_api", aev.api)
    content = await _launch({})
    assert aev.calls[0] == ("post", "/api/scenarios/s1/exercise/running")
    assert "copy" not in aev.scenarios
    assert "Launched scenario s1" in content


async def test_an_empty_sample_launches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    aev = _OpenAEV()
    monkeypatch.setattr(simulations, "_api", aev.api)
    content = await _launch({"asset_ids": []})
    assert aev.calls == []
    assert "Nothing was launched" in content


async def test_a_copy_that_cannot_be_retargeted_is_deleted_and_not_launched(monkeypatch: pytest.MonkeyPatch) -> None:
    aev = _OpenAEV(fail_put=True)
    monkeypatch.setattr(simulations, "_api", aev.api)
    content = await _launch({"asset_ids": SAMPLE})
    assert "copy" not in aev.scenarios
    assert not [c for c in aev.calls if c[1].endswith("/exercise/running")]
    assert "Sample launch refused" in content and "Nothing was launched" in content


def test_the_change_leaves_a_content_without_targets_alone() -> None:
    change = sample_targets_change({"inject_content": {"command": "id"}}, ["a1"])
    assert change == {"inject_assets": ["a1"], "inject_asset_groups": []}
