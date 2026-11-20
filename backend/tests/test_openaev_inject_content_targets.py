# SPDX-License-Identifier: Apache-2.0
"""OpenAEV inject tools carry content targets onto the relations (xtm-one #267).

OpenAEV targets an inject through its relations (``inject_asset_groups``,
``inject_assets``): the "Update the inject" form and the executor both read
them, never ``inject_content.asset_groups``. A group named only in the content
was shown empty and "Required" and the injector got no target. The tools now
fill a relation the caller left out from the content, refuse a relation that
misses an id the content names, and echo the stored targets.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.integrations.providers.openaev import build_tools
from app.integrations.providers.openaev._common import (
    inject_targets_line,
    mirror_content_targets,
)
from app.integrations.providers.openaev.autonomous import _build_inject
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.openaev.io||test-token"
GROUP = "de799619-00cb-409c-8f29-8013e50fdb08"
CONTENT = json.dumps(
    {
        "target_selector": "asset-groups",
        "asset_groups": [GROUP],
        "target_property_selector": "seen_ip",
        "expectations": [{"expectation_type": "PREVENTION"}],
    }
)


class _Resp:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def json(self) -> dict[str, Any]:
        return self._data


class _RecordingApi:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, base_url: str, headers: Any, method: str, path: str, action: str, **kw: Any
    ) -> tuple[_Resp, None]:
        body = kw.get("json") or {}
        self.calls.append({"method": method, "path": path, "json": body})
        return _Resp({"inject_id": "i1", "inject_title": "Nmap", **body}), None


async def _run(name: str, arguments: dict[str, Any]) -> str:
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    result = await executor.execute_tool(ToolCall(id=f"call-{name}", name=name, arguments=arguments))
    assert not result.is_error, result.content
    return result.content


def test_mirror_fills_a_relation_left_out() -> None:
    assets, groups, note, err = mirror_content_targets(json.loads(CONTENT), None, None)
    assert err == ""
    assert groups == [GROUP]
    assert assets is None
    assert "asset_group_ids set from content.asset_groups (1)" in note


def test_mirror_keeps_a_relation_that_covers_the_content() -> None:
    assets, groups, note, err = mirror_content_targets(json.loads(CONTENT), ["a1"], [GROUP, "g2"])
    assert (assets, groups, note, err) == (["a1"], [GROUP, "g2"], "", "")


def test_mirror_refuses_a_relation_missing_a_content_id() -> None:
    _, _, _, err = mirror_content_targets(json.loads(CONTENT), None, ["g2"])
    assert GROUP in err
    assert "asset_group_ids" in err


@pytest.mark.parametrize("content", [None, [], "x", {"command": "id"}])
def test_mirror_leaves_content_without_targets_alone(content: Any) -> None:
    assert mirror_content_targets(content, None, None) == (None, None, "", "")


def test_targets_line_names_relations_and_warns_when_empty() -> None:
    stored = inject_targets_line({"inject_asset_groups": [{"asset_group_id": GROUP}], "inject_assets": []})
    assert stored == f"Targets stored: 1 asset group(s) [{GROUP}], 0 asset(s)."
    empty = inject_targets_line({"inject_assets": [], "inject_content": {"target_selector": "asset-groups"}})
    assert "the injector receives no target" in empty
    # Relations absent from the answer are unknown, not empty (xtm-one #660).
    assert inject_targets_line({"inject_content": {"target_selector": "asset-groups"}}) == ""
    assert inject_targets_line("not an object") == ""


async def test_manage_inject_create_writes_the_group_relation(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.providers.openaev import injects

    api = _RecordingApi()
    monkeypatch.setattr(injects, "_api", api)
    content = await _run(
        "manage_openaev_inject",
        {"action": "create", "parent_id": "s1", "title": "Nmap", "injector_contract_id": "c1", "content": CONTENT},
    )
    assert api.calls[-1]["json"]["inject_asset_groups"] == [GROUP]
    assert "set from content.asset_groups" in content
    assert f"Targets stored: 1 asset group(s) [{GROUP}]" in content


async def test_manage_inject_update_refuses_before_any_write(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.providers.openaev import injects

    api = _RecordingApi()
    monkeypatch.setattr(injects, "_api", api)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-update",
            name="manage_openaev_inject",
            arguments={
                "action": "update",
                "parent_id": "s1",
                "inject_id": "i1",
                "content": CONTENT,
                "asset_group_ids": ["g2"],
            },
        )
    )
    assert GROUP in result.content
    assert api.calls == []


async def test_atomic_testing_create_writes_the_group_relation(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.providers.openaev import testing

    api = _RecordingApi()
    monkeypatch.setattr(testing, "_api", api)
    content = await _run(
        "manage_openaev_atomic_testing",
        {"action": "create", "title": "Nmap", "injector_contract_id": "c1", "content": CONTENT},
    )
    post = [c for c in api.calls if c["method"] == "post"][-1]
    assert post["json"]["inject_asset_groups"] == [GROUP]
    assert f"Targets stored: 1 asset group(s) [{GROUP}]" in content


async def test_add_inject_to_scenario_writes_the_group_relation(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.providers.openaev import core

    calls: list[dict[str, Any]] = []

    async def fake_safe_request(
        client: Any,
        method: str,
        url: str,
        provider: str,
        action: str,
        *,
        headers: Any = None,
        json: dict[str, Any] | None = None,
        **kw: Any,
    ) -> tuple[_Resp, None]:
        calls.append({"method": method, "url": url, "json": json})
        return _Resp({"inject_id": "i1", **(json or {})}), None

    monkeypatch.setattr(core, "scoped_safe_request", fake_safe_request)
    content = await _run(
        "add_openaev_inject_to_scenario",
        {"scenario_id": "s1", "title": "Nmap", "injector_contract_id": "c1", "content": CONTENT},
    )
    post = [c for c in calls if c["method"] == "post"][-1]
    assert post["json"]["inject_asset_groups"] == [GROUP]
    assert f"Targets stored: 1 asset group(s) [{GROUP}]" in content


def test_autonomous_build_inject_mirrors_and_refuses() -> None:
    inject, err = _build_inject("Nmap", "c1", CONTENT, "", None, None, None, None, None)
    assert err is None
    assert inject is not None
    assert inject["inject_asset_groups"] == [GROUP]
    inject, err = _build_inject("Nmap", "c1", CONTENT, "", None, None, ["g2"], None, None)
    assert inject is None
    assert err is not None
    assert GROUP in err
