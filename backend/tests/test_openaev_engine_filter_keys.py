# SPDX-License-Identifier: Apache-2.0

"""Analytics filter keys come from OpenAEV's engine schema and are checked before the query (#1717)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.integrations.providers.openaev import build_tools, dashboards, platform
from app.integrations.providers.openaev.engine_schema import (
    engine_entity_of,
    widget_entities,
    widget_key_error,
)
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

_ENGINE = [
    {"schema_property_name": "base_tags_side", "schema_property_type": "string", "schema_property_entity": "asset"},
    {"schema_property_name": "asset_hostname", "schema_property_type": "string", "schema_property_entity": "asset"},
    {
        "schema_property_name": "asset_category",
        "schema_property_type": "enum",
        "schema_property_entity": "asset",
        "schema_property_override_operators": ["eq", "not_eq"],
    },
]
_JPA = [{"schema_property_name": "asset_tags", "schema_property_type": "string"}]


class _Resp:
    def __init__(self, data: object) -> None:
        self._data = data

    def json(self) -> object:
        return self._data


class _PathApi:
    """An ``_api`` stub answering by path, recording every call."""

    def __init__(self, answers: dict[str, object], errors: dict[str, str] | None = None) -> None:
        self.answers = answers
        self.errors = errors or {}
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        base_url: str,
        headers: dict[str, str],
        method: str,
        path: str,
        action: str,
        *,
        json: Any = None,
        params: Any = None,
        **kw: Any,
    ) -> tuple[_Resp | None, str | None]:
        self.calls.append({"method": method, "path": path, "json": json, "params": params})
        if path in self.errors:
            return None, self.errors[path]
        return _Resp(self.answers.get(path, {})), None

    def paths(self) -> list[str]:
        return [c["path"] for c in self.calls]


def _config(key: str, operator: str = "eq", entity: str = "asset") -> str:
    return json.dumps(
        {
            "widget_configuration_type": "flat",
            "series": [
                {
                    "name": "s",
                    "filter": {
                        "mode": "and",
                        "filters": [
                            {"key": "base_entity", "operator": "eq", "values": [entity], "mode": "or"},
                            {"key": key, "operator": operator, "values": ["x"], "mode": "or"},
                        ],
                    },
                }
            ],
        }
    )


async def _run(name: str, arguments: dict[str, Any]) -> ToolResult:
    executor = CallableToolExecutor()
    build_tools("https://demo.openaev.io||test-token", executor)
    return await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))


async def test_analytics_refuses_a_model_key_the_engine_does_not_index(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _PathApi({"/api/engine/schemas": _ENGINE})
    monkeypatch.setattr(dashboards, "_api", fake)
    result = await _run("query_openaev_analytics", {"kind": "count", "widget_config": _config("asset_tags")})
    assert "nothing queried" in result.content
    assert "'asset_tags' is not an OpenAEV engine field" in result.content
    assert "base_tags_side" in result.content
    assert fake.paths() == ["/api/engine/schemas"]
    assert fake.calls[0]["params"] == {"classNames": ["asset"]}


async def test_analytics_sends_an_engine_key(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _PathApi({"/api/engine/schemas": _ENGINE, "/api/dashboards/adhoc/count": {"interval_count": 4}})
    monkeypatch.setattr(dashboards, "_api", fake)
    result = await _run("query_openaev_analytics", {"kind": "count", "widget_config": _config("base_tags_side")})
    assert "Ad-hoc count result" in result.content
    assert fake.paths() == ["/api/engine/schemas", "/api/dashboards/adhoc/count"]


async def test_analytics_refuses_an_operator_the_engine_imposes_against(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _PathApi({"/api/engine/schemas": _ENGINE})
    monkeypatch.setattr(dashboards, "_api", fake)
    result = await _run(
        "query_openaev_analytics",
        {"kind": "count", "widget_config": _config("asset_category", operator="starts_with")},
    )
    assert "operator 'starts_with' is not accepted by the engine on 'asset_category'" in result.content
    assert "eq, not_eq" in result.content
    assert "/api/dashboards/adhoc/count" not in fake.paths()


async def test_analytics_runs_unchecked_when_the_schema_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _PathApi({"/api/dashboards/adhoc/count": {"interval_count": 1}}, errors={"/api/engine/schemas": "404"})
    monkeypatch.setattr(dashboards, "_api", fake)
    result = await _run("query_openaev_analytics", {"kind": "count", "widget_config": _config("asset_tags")})
    assert "Ad-hoc count result" in result.content
    assert "/api/dashboards/adhoc/count" in fake.paths()


async def test_filter_keys_lists_the_engine_keys_beside_the_model_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _PathApi({"/api/schemas/Asset": _JPA, "/api/engine/schemas": _ENGINE})
    monkeypatch.setattr(platform, "_api", fake)
    result = await _run("manage_openaev_settings", {"action": "filter_keys", "class_name": "Asset"})
    assert "asset_tags" in result.content
    assert "Engine keys of 'asset' (3)" in result.content
    assert "base_tags_side" in result.content
    assert "operators: eq, not_eq" in result.content


async def test_filter_keys_answers_an_engine_entity_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # 'asset-group' is no model class: /api/schemas fails, the engine keys still answer.
    fake = _PathApi({"/api/engine/schemas": _ENGINE}, errors={"/api/schemas/asset-group": "400 Class not allowed"})
    monkeypatch.setattr(platform, "_api", fake)
    result = await _run("manage_openaev_settings", {"action": "filter_keys", "class_name": "asset-group"})
    assert "Engine keys of 'asset-group'" in result.content
    assert "Class not allowed" not in result.content


def test_engine_entity_of_maps_model_classes() -> None:
    assert engine_entity_of("Asset") == "asset"
    assert engine_entity_of("Exercise") == "simulation"
    assert engine_entity_of("InjectExpectation") == "expectation-inject"
    assert engine_entity_of("vulnerable-endpoint") == "vulnerable-endpoint"
    assert engine_entity_of("Payload") == ""


def test_widget_key_error_reads_nested_and_list_keys() -> None:
    keys = {"base_tags_side": {"type": "string", "entities": set(), "operators": set()}}
    config = json.loads(_config("base_tags_side"))
    assert widget_entities(config) == ["asset"]
    assert widget_key_error(config, keys, ["asset"]) == ""
    config["series"][0]["filter"]["filters"][1]["key"] = ["asset_tags"]
    assert "'asset_tags'" in widget_key_error(config, keys, ["asset"])
    assert widget_key_error(config, {}, []) == ""
