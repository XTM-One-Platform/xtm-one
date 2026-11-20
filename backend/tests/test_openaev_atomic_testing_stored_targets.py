# SPDX-License-Identifier: Apache-2.0

"""The atomic-testing tools name the targets OpenAEV stored (xtm-one #660).

OpenAEV answers POST/PUT/GET ``/api/atomic-testings`` with
``InjectResultOverviewOutput``, which carries no ``inject_assets`` /
``inject_asset_groups``. The tools read the stored relations from
``GET /api/injects/{id}`` and never claim zero targets from an answer that
lacks the keys.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers.openaev import build_tools
from app.integrations.providers.openaev._common import (
    inject_targets_line,
    stored_targets_line,
)
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.openaev.io||test-token"
ASSET_A = "a0000000-0000-0000-0000-00000000000a"
ASSET_B = "b0000000-0000-0000-0000-00000000000b"
SELECTOR = '{"target_selector": "assets"}'


class _Resp:
    def __init__(self, data: Any) -> None:
        self._data = data
        self.content = b"x"

    def json(self) -> Any:
        return self._data


class _AtomicApi:
    """OpenAEV double: the atomic-testing routes answer the overview DTO."""

    def __init__(self, stored_assets: list[str] | None, inject_read_fails: bool = False) -> None:
        self.stored_assets = stored_assets
        self.inject_read_fails = inject_read_fails
        self.calls: list[tuple[str, str]] = []

    async def __call__(
        self, base_url: str, headers: Any, method: str, path: str, action: str, **kw: Any
    ) -> tuple[_Resp | None, str | None]:
        self.calls.append((method, path))
        if path.startswith("/api/injects/"):
            if self.inject_read_fails:
                return None, "OpenAEV error 500"
            return _Resp(
                {
                    "inject_id": "i1",
                    "inject_assets": [{"asset_id": a} for a in self.stored_assets or []],
                    "inject_asset_groups": [],
                    "inject_content": {"target_selector": "assets"},
                }
            ), None
        # InjectResultOverviewOutput: no relation keys.
        return _Resp({"inject_id": "i1", "inject_title": "Nmap", "inject_type": "openaev_nmap"}), None


async def _run(arguments: dict[str, Any], name: str = "manage_openaev_atomic_testing") -> str:
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    result = await executor.execute_tool(ToolCall(id=f"call-{name}", name=name, arguments=arguments))
    assert not result.is_error, result.content
    return result.content


async def test_create_reads_the_stored_relations(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.providers.openaev import testing

    api = _AtomicApi([ASSET_A])
    monkeypatch.setattr(testing, "_api", api)
    content = await _run(
        {
            "action": "create",
            "title": "Nmap",
            "injector_contract_id": "c1",
            "asset_ids": [ASSET_A],
            "content": SELECTOR,
        }
    )
    assert ("get", "/api/injects/i1") in api.calls
    assert f"1 asset(s) [{ASSET_A}]" in content
    assert "Warning" not in content
    assert "Not stored" not in content


async def test_update_names_an_id_asked_but_not_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.providers.openaev import testing

    monkeypatch.setattr(testing, "_api", _AtomicApi([ASSET_A]))
    content = await _run({"action": "update", "inject_id": "i1", "asset_ids": [ASSET_A, ASSET_B]})
    assert f"1 asset(s) [{ASSET_A}]" in content
    assert f"Not stored: {ASSET_B}; unknown id or another tenant." in content


async def test_no_zero_claim_when_the_relations_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.providers.openaev import testing

    monkeypatch.setattr(testing, "_api", _AtomicApi(None, inject_read_fails=True))
    content = await _run(
        {
            "action": "create",
            "title": "Nmap",
            "injector_contract_id": "c1",
            "asset_ids": [ASSET_A],
            "content": SELECTOR,
        }
    )
    assert "0 asset(s)" not in content
    assert "Warning" not in content
    assert "not readable" in content


async def test_get_adds_the_stored_targets_line(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.providers.openaev import testing

    monkeypatch.setattr(testing, "_api", _AtomicApi([ASSET_A]))
    content = await _run({"action": "get", "inject_id": "i1"})
    assert content.startswith(f"Targets stored: 0 asset group(s), 1 asset(s) [{ASSET_A}].")
    assert '"inject_type"' in content


async def test_core_create_reads_the_stored_relations(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.providers.openaev import core

    async def fake_safe_request(client: Any, method: str, url: str, *a: Any, **kw: Any) -> tuple[_Resp, None]:
        return _Resp({"inject_id": "i1", "inject_title": "Nmap"}), None

    api = _AtomicApi([ASSET_A])
    monkeypatch.setattr(core, "scoped_safe_request", fake_safe_request)
    monkeypatch.setattr(core, "_api", api)
    content = await _run(
        {"title": "Nmap", "injector_contract_id": "c1", "asset_ids": [ASSET_A, ASSET_B], "content": SELECTOR},
        name="create_openaev_atomic_testing",
    )
    assert ("get", "/api/injects/i1") in api.calls
    assert f"1 asset(s) [{ASSET_A}]" in content
    assert f"Not stored: {ASSET_B}" in content


def test_absent_relations_are_not_empty_relations() -> None:
    overview = {"inject_id": "i1", "inject_content": {"target_selector": "assets"}}
    assert inject_targets_line(overview) == ""
    assert "not readable" in stored_targets_line(overview, None)
    stored = {"inject_assets": [], "inject_asset_groups": [], "inject_content": {"target_selector": "assets"}}
    assert "the injector receives no target" in stored_targets_line(overview, stored)
