# SPDX-License-Identifier: Apache-2.0
"""manage_openaev_atomic_testing 'update' keeps every field left out.

OpenAEV's PUT /api/atomic-testings/{id} replaces the whole AtomicTestingInput:
a blank title is refused, a missing contract fails its lookup and an omitted
list (assets, asset groups, tags, teams, secret references, documents) is
stored empty. The tool reads the stored inject (GET /api/injects/{id}) and
lays the caller's fields over it before the PUT.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.integrations.providers.openaev import build_tools
from app.integrations.providers.openaev.testing import stored_atomic_input
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.openaev.io||test-token"

STORED: dict[str, Any] = {
    "inject_id": "i1",
    "inject_title": "Nmap sweep",
    "inject_description": "Stored description",
    "inject_injector_contract": {"injector_contract_id": "c1", "injector_contract_labels": {}},
    "inject_injector": "inj1",
    "inject_content": {"command": "nmap"},
    "inject_assets": [{"asset_id": "a1"}, {"asset_id": "a2"}],
    "inject_asset_groups": ["g1"],
    "inject_tags": ["t1"],
    "inject_teams": [],
    "inject_all_teams": False,
    "inject_secret_references": [],
    "inject_documents": [{"inject_id": "i1", "document_id": "d1", "document_attached": False}],
}


class _Resp:
    def __init__(self, data: Any) -> None:
        self._data = data

    def json(self) -> Any:
        return self._data


class _Api:
    """GET answers the stored inject, PUT echoes the body it receives."""

    def __init__(self, stored: Any) -> None:
        self.stored = stored
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, base_url: str, headers: Any, method: str, path: str, action: str, **kw: Any
    ) -> tuple[_Resp, None]:
        body = kw.get("json")
        self.calls.append({"method": method, "path": path, "json": body})
        if method == "get":
            return _Resp(self.stored), None
        return _Resp({"inject_id": "i1", **(body or {})}), None


async def _update(monkeypatch: pytest.MonkeyPatch, stored: Any, **arguments: Any) -> tuple[str, _Api]:
    from app.integrations.providers.openaev import testing

    api = _Api(stored)
    monkeypatch.setattr(testing, "_api", api)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-update",
            name="manage_openaev_atomic_testing",
            arguments={"action": "update", "inject_id": "i1", **arguments},
        )
    )
    assert not result.is_error, result.content
    return result.content, api


def _put(api: _Api) -> dict[str, Any]:
    puts = [c for c in api.calls if c["method"] == "put"]
    assert len(puts) == 1
    assert puts[0]["path"] == "/api/atomic-testings/i1"
    return puts[0]["json"]


async def test_update_description_only_keeps_everything_else(monkeypatch: pytest.MonkeyPatch) -> None:
    _, api = await _update(monkeypatch, STORED, description="New description")
    assert api.calls[0] == {"method": "get", "path": "/api/injects/i1", "json": None}
    body = _put(api)
    assert body == {
        "inject_title": "Nmap sweep",
        "inject_description": "New description",
        "inject_injector_contract": "c1",
        "inject_injector": "inj1",
        "inject_content": {"command": "nmap"},
        "inject_all_teams": False,
        "inject_assets": ["a1", "a2"],
        "inject_asset_groups": ["g1"],
        "inject_teams": [],
        "inject_tags": ["t1"],
        "inject_secret_references": [],
        "inject_documents": [{"document_id": "d1", "document_attached": False}],
    }


async def test_update_title_only_changes_the_title(monkeypatch: pytest.MonkeyPatch) -> None:
    _, api = await _update(monkeypatch, STORED, title="Renamed")
    body = _put(api)
    assert body["inject_title"] == "Renamed"
    assert body["inject_description"] == "Stored description"
    assert body["inject_assets"] == ["a1", "a2"]
    assert body["inject_content"] == {"command": "nmap"}


async def test_update_empty_asset_ids_clears_assets_on_purpose(monkeypatch: pytest.MonkeyPatch) -> None:
    _, api = await _update(monkeypatch, STORED, asset_ids=[])
    body = _put(api)
    assert body["inject_assets"] == []
    assert body["inject_asset_groups"] == ["g1"]


async def test_update_new_contract_drops_the_stored_injector(monkeypatch: pytest.MonkeyPatch) -> None:
    _, api = await _update(monkeypatch, STORED, injector_contract_id="c2")
    body = _put(api)
    assert body["inject_injector_contract"] == "c2"
    assert "inject_injector" not in body


async def test_update_content_keeps_the_content_target_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    content = json.dumps({"target_selector": "asset-groups", "asset_groups": ["g9"]})
    _, api = await _update(monkeypatch, STORED, content=content)
    body = _put(api)
    assert body["inject_asset_groups"] == ["g9"]
    assert body["inject_assets"] == ["a1", "a2"]


async def test_update_refuses_when_the_stored_relations_are_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = {k: v for k, v in STORED.items() if k != "inject_assets"}
    content, api = await _update(monkeypatch, stored, title="Renamed")
    assert "inject_assets" in content
    assert "Nothing was written" in content
    assert [c["method"] for c in api.calls] == ["get"]


async def test_update_refuses_without_a_stored_title(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = {**STORED, "inject_title": ""}
    content, api = await _update(monkeypatch, stored, description="x")
    assert "inject_title" in content
    assert [c["method"] for c in api.calls] == ["get"]


def test_stored_atomic_input_without_optional_keys() -> None:
    stored = {k: v for k, v in STORED.items() if k not in ("inject_secret_references", "inject_documents")}
    body, refusal = stored_atomic_input(stored, {"inject_title": "T"})
    assert refusal == ""
    assert "inject_secret_references" not in body
    assert "inject_documents" not in body


def test_stored_atomic_input_refuses_a_non_object() -> None:
    body, refusal = stored_atomic_input(["not", "an", "object"], {"inject_title": "T"})
    assert body == {}
    assert "Nothing was written" in refusal
