# SPDX-License-Identifier: Apache-2.0
"""manage_openaev_atomic_testing says why an atomic test is not ready.

An atomic test whose content leaves a mandatory field of its injector contract
empty used to save and launch with no error and reach no target. OpenAEV
(openaev #9569) now lists those fields under
``inject_missing_mandatory_fields`` and refuses the launch with a 400 naming
them; the tool surfaces both, and on create lists the contract's mandatory
fields.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.integrations.providers.openaev import build_tools
from app.integrations.providers.openaev.testing import contract_mandatory_fields, readiness_line
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.openaev.io||test-token"

CONTRACT: dict[str, Any] = {
    "injector_contract_id": "c-m365",
    "injector_contract_content": json.dumps(
        {
            "fields": [
                {"key": "teams", "type": "team", "mandatory": True, "cardinality": "n"},
                {"key": "emailTemplate", "type": "select", "mandatory": True, "cardinality": "1"},
                {"key": "subject", "type": "text", "mandatory": False},
                {
                    "key": "assets",
                    "type": "asset",
                    "mandatoryGroups": ["assets", "asset_groups"],
                },
                {
                    "key": "body",
                    "type": "textarea",
                    "mandatoryConditionFields": ["mode"],
                    "mandatoryConditionValues": {"mode": "custom"},
                },
            ]
        }
    ),
}

NOT_READY: dict[str, Any] = {
    "inject_id": "i1",
    "inject_title": "Phishing M365",
    "inject_ready": False,
    "inject_missing_mandatory_fields": ["teams"],
}


class _Resp:
    def __init__(self, data: Any) -> None:
        self._data = data
        self.content = json.dumps(data).encode()

    def json(self) -> Any:
        return self._data


class _Api:
    def __init__(self, routes: dict[tuple[str, str], tuple[Any, str | None]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str]] = []

    async def __call__(
        self, base_url: str, headers: Any, method: str, path: str, action: str, **kw: Any
    ) -> tuple[_Resp | None, str | None]:
        self.calls.append((method, path))
        data, err = self.routes.get((method, path), ({}, None))
        if err:
            return None, err
        return _Resp(data), None


async def _run(monkeypatch: pytest.MonkeyPatch, api: _Api, **arguments: Any) -> str:
    from app.integrations.providers.openaev import testing

    monkeypatch.setattr(testing, "_api", api)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    result = await executor.execute_tool(ToolCall(id="call", name="manage_openaev_atomic_testing", arguments=arguments))
    return result.content


def test_readiness_line_names_the_missing_fields() -> None:
    line = readiness_line(NOT_READY)
    assert line.startswith("NOT READY: mandatory field(s) teams ")
    assert "launch is refused" in line


def test_readiness_line_without_the_field_list_still_says_not_ready() -> None:
    line = readiness_line({"inject_ready": False})
    assert line.startswith("NOT READY:")
    assert "does not name the field" in line


@pytest.mark.parametrize("answer", [{"inject_ready": True}, {}, None, []])
def test_readiness_line_is_silent_when_ready_or_unknown(answer: Any) -> None:
    assert readiness_line(answer) == ""


def test_contract_mandatory_fields_reads_plain_conditional_and_grouped_fields() -> None:
    assert contract_mandatory_fields(CONTRACT) == [
        "teams [team]",
        "emailTemplate [select]",
        "assets [asset] (one of: assets, asset_groups)",
        "body [textarea] (when its condition holds)",
    ]


@pytest.mark.parametrize("contract", [None, {}, {"injector_contract_content": "not json"}])
def test_contract_mandatory_fields_of_unreadable_content_is_empty(contract: Any) -> None:
    assert contract_mandatory_fields(contract) == []


async def test_get_puts_the_not_ready_reason_first(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _Api({("get", "/api/atomic-testings/i1"): (NOT_READY, None)})
    content = await _run(monkeypatch, api, action="get", inject_id="i1")
    assert content.splitlines()[0].startswith("NOT READY: mandatory field(s) teams ")


async def test_create_lists_the_contract_mandatory_fields_and_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _Api(
        {
            ("post", "/api/atomic-testings"): (NOT_READY, None),
            ("get", "/api/injector_contracts/c-m365"): (CONTRACT, None),
        }
    )
    content = await _run(
        monkeypatch,
        api,
        action="create",
        title="Phishing M365",
        injector_contract_id="c-m365",
        content=json.dumps({"emailTemplate": "t1"}),
    )
    assert "NOT READY: mandatory field(s) teams " in content
    assert "Mandatory fields of its injector contract: teams [team]; emailTemplate [select]" in content
    assert ("get", "/api/injector_contracts/c-m365") in api.calls


async def test_create_says_when_the_contract_read_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _Api(
        {
            ("post", "/api/atomic-testings"): (NOT_READY, None),
            ("get", "/api/injector_contracts/c-m365"): (None, "Error: OpenAEV server error (503)"),
        }
    )
    content = await _run(monkeypatch, api, action="create", title="Phishing M365", injector_contract_id="c-m365")
    assert "Mandatory fields of its injector contract: not readable" in content


async def test_launch_refusal_is_answered_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    refusal = (
        "Error: OpenAEV rejected the request to launch atomic test - bad request. "
        "The atomic testing is not ready to be launched: mandatory field(s) teams "
        "of its injector contract are empty"
    )
    api = _Api({("post", "/api/atomic-testings/i1/launch"): (None, refusal)})
    content = await _run(monkeypatch, api, action="launch", inject_id="i1")
    assert content == refusal
