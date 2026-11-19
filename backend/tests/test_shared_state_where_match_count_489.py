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

"""A where_json read of get_shared_state carries its match count (#489).

A gate check reads ``ToolResult.structured``: with ``match_count`` and
``matches`` there, an event-driven assignment can stand down at zero tokens
when a list holds no item for the event.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.services import shared_state as shared_state_service
from app.services.assignment_activation_check import condition_met
from app.services.builtin_tools.shared_state import build_shared_state_tools
from app.tools.base import ToolCall


def _state(key: str) -> dict:
    return {
        "key": key,
        "label": "Open pull requests",
        "status": "active",
        "value": {
            "rule": "r",
            "prs": [
                {"number": 1, "branch": "fix/a", "repository": "o/one"},
                {"number": 2, "branch": "fix/b", "repository": "o/one"},
                {"number": 3, "branch": "fix/b", "repository": "o/two"},
            ],
        },
        "notes": [],
    }


async def _executor(monkeypatch):
    async def _get_state(**kwargs):
        return _state(kwargs["key"])

    monkeypatch.setattr(shared_state_service, "get_state", _get_state)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    return executor


async def _read(executor, **arguments):
    return await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name="get_shared_state", arguments=arguments))


@pytest.mark.asyncio
async def test_a_path_read_counts_its_matches(monkeypatch):
    executor = await _executor(monkeypatch)
    result = await _read(executor, key="dev:open-prs", path="prs", where_json=json.dumps({"branch": "fix/b"}))
    assert result.structured["match_count"] == 2
    assert [item["number"] for item in result.structured["matches"]] == [2, 3]
    # The whole entry stays where it was.
    assert len(result.structured["value"]["prs"]) == 3


@pytest.mark.asyncio
async def test_a_layout_free_read_counts_its_matches_flat(monkeypatch):
    executor = await _executor(monkeypatch)
    result = await _read(executor, key="dev:open-prs", where_json=json.dumps({"repository": "o/two"}))
    assert result.structured["match_count"] == 1
    assert result.structured["matches"][0]["number"] == 3


@pytest.mark.asyncio
async def test_a_gate_on_the_count_skips_when_nothing_matches(monkeypatch):
    executor = await _executor(monkeypatch)
    condition = {"path": "$.match_count", "not_in": [0]}
    hit = await _read(executor, key="dev:open-prs", path="prs", where_json=json.dumps({"branch": "fix/a"}))
    miss = await _read(executor, key="dev:open-prs", path="prs", where_json=json.dumps({"branch": "feat/zz"}))
    assert hit.structured["match_count"] == 1
    assert miss.structured["match_count"] == 0
    assert condition_met(hit.structured, condition) is True
    assert condition_met(miss.structured, condition) is False


@pytest.mark.asyncio
async def test_a_plain_read_carries_no_count(monkeypatch):
    executor = await _executor(monkeypatch)
    result = await _read(executor, key="dev:open-prs", path="prs")
    assert "match_count" not in result.structured
    assert "matches" not in result.structured
