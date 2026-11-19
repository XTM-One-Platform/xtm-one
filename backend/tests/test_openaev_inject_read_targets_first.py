# SPDX-License-Identifier: Apache-2.0
"""OpenAEV inject reads name the stored targets before any cut (xtm-one #351).

An inject whose ``inject_content`` is large (a long command line, about 110 IPs)
pushed ``manage_openaev_inject`` get past the read limit, and the cut marker
fell before ``inject_asset_groups``: the relation the executor targets could
not be read back. Inject reads now start with the targets line and put the
bulky keys (content, documents, expectations, status) last in the JSON.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.integrations.providers.openaev import build_tools
from app.integrations.providers.openaev._common import JSON_READ_CHARS, inject_read_answer
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.openaev.io||test-token"
GROUP = "de799619-00cb-409c-8f29-8013e50fdb08"


def _big_inject() -> dict[str, Any]:
    return {
        "inject_id": "i1",
        "inject_title": "Nmap",
        "inject_content": {"command": " ".join(f"10.0.{i // 250}.{i % 250}" for i in range(3000))},
        "inject_asset_groups": [{"asset_group_id": GROUP}],
        "inject_assets": [],
        "inject_enabled": True,
    }


def test_targets_line_precedes_a_cut_json() -> None:
    answer = inject_read_answer(_big_inject())
    first, _, body = answer.partition("\n")
    assert first == f"Targets stored: 1 asset group(s) [{GROUP}], 0 asset(s)."
    assert "[truncated" in body
    # Relations and scalars come before the bulky content, so they survive the cut.
    assert body.index('"inject_asset_groups"') < body.index('"inject_content"')
    assert '"inject_enabled": true' in body


def test_small_inject_is_whole_json_after_the_line() -> None:
    inject = {"inject_id": "i1", "inject_content": {"a": 1}, "inject_assets": [{"asset_id": "a1"}]}
    first, _, body = inject_read_answer(inject).partition("\n")
    assert first.startswith("Targets stored: 0 asset group(s), 1 asset(s) [a1]")
    assert json.loads(body) == {"inject_id": "i1", "inject_assets": [{"asset_id": "a1"}], "inject_content": {"a": 1}}


@pytest.mark.parametrize("value", [[], "x", None])
def test_non_object_falls_back_to_json_answer(value: Any) -> None:
    assert inject_read_answer(value) == json.dumps(value)


def test_limit_is_the_shared_read_limit() -> None:
    assert len(inject_read_answer(_big_inject())) < JSON_READ_CHARS + 400


class _Resp:
    def __init__(self, data: Any) -> None:
        self._data = data

    def json(self) -> Any:
        return self._data


@pytest.mark.parametrize("scope", ["scenario", "global"])
async def test_manage_inject_get_names_targets_first(monkeypatch: pytest.MonkeyPatch, scope: str) -> None:
    from app.integrations.providers.openaev import injects

    async def fake_api(*_a: Any, **_kw: Any) -> tuple[_Resp, None]:
        return _Resp(_big_inject()), None

    monkeypatch.setattr(injects, "_api", fake_api)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-get",
            name="manage_openaev_inject",
            arguments={"action": "get", "scope": scope, "parent_id": "s1", "inject_id": "i1"},
        )
    )
    assert not result.is_error, result.content
    assert result.content.startswith(f"Targets stored: 1 asset group(s) [{GROUP}]")
