"""XTM One never changes the name of an inject expectation (#2480).

Whatever the type (VULNERABILITY, DETECTION, PREVENTION, MANUAL, ARTICLE,
CHALLENGE, LOGGING), an expectation keeps the name the injector contract gives
it, exactly as-is. A type the contract does not offer gets OpenAEV's own
default name (``ExpectationBuilderService``), never a home-made label: the old
"Vulnerability confirmed" read the opposite way of VULNERABILITY's polarity
(SUCCESS = not vulnerable). An update never rewrites a stored name either.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.integrations.providers.openaev import build_tools
from app.integrations.providers.openaev.core import (
    _OPENAEV_DEFAULT_EXPECTATION_NAMES,
    _build_inject_expectations,
    _contract_offered_expectations,
)
from app.integrations.providers.openaev.injects import stored_inject_input
from app.tools.base import CallableToolExecutor, ToolCall
from tests.test_openaev_inject_expectations import _add, _content, _contract_content, _install

#: The expectation types the acceptance rule names.
ALL_TYPES = ["VULNERABILITY", "DETECTION", "PREVENTION", "MANUAL", "ARTICLE", "CHALLENGE", "LOGGING"]
#: The types ``add_openaev_inject_to_scenario(expectations=[...])`` accepts.
CREATABLE_TYPES = [t for t in ALL_TYPES if t != "LOGGING"]

#: OpenAEV's defaults, read from ExpectationBuilderService / ExpectationType.
OPENAEV_DEFAULTS = {
    "PREVENTION": "Prevention",
    "DETECTION": "Detection",
    "LOGGING": "Logging",
    "VULNERABILITY": "Not vulnerable",
    "CHALLENGE": "Expect targets to complete the challenge(s)",
    "ARTICLE": "Expect targets to read the article(s)",
    "MANUAL": "Manual expectation",
}


def _contract_item(expectation_type: str, name: str, **extra: Any) -> dict[str, Any]:
    """One contract expectation object carrying a name XTM One must not touch."""
    return {
        "expectation_type": expectation_type,
        "expectation_name": name,
        "expectation_description": f"{name} (contract)",
        "expectation_score": 80.0,
        "expectation_expectation_group": False,
        "expectation_is_predefined": True,
        "expectation_expected_security_platform_types": extra.get("platforms", []),
    }


def _contract_name(expectation_type: str) -> str:
    return f"Contract name for {expectation_type.lower()} / kept as-is"


#: The Nuclei CVE Scan contract of OpenAEV-Platform/injectors, names as served.
NUCLEI_CVE_CONTRACT = [
    _contract_item("DETECTION", "Detection", platforms=["XDR", "SIEM", "NDR"]),
    _contract_item("PREVENTION", "Prevention", platforms=["XDR", "NDR"]),
    _contract_item("VULNERABILITY", "Not vulnerable"),
]


# ── Builder ───────────────────────────────────────────────────────────────


def test_no_home_made_label_remains():
    assert _OPENAEV_DEFAULT_EXPECTATION_NAMES == OPENAEV_DEFAULTS


@pytest.mark.parametrize("expectation_type", ALL_TYPES)
def test_builder_keeps_the_contract_object_and_its_name(expectation_type):
    item = _contract_item(expectation_type, _contract_name(expectation_type))
    offered = _contract_offered_expectations(_contract_content([item]))

    [built] = _build_inject_expectations([expectation_type], offered)

    assert built == item


@pytest.mark.parametrize("expectation_type", ALL_TYPES)
def test_builder_names_an_unoffered_type_with_openaev_default(expectation_type):
    [built] = _build_inject_expectations([expectation_type], {})

    assert built["expectation_name"] == OPENAEV_DEFAULTS[expectation_type]
    assert built["expectation_score"] == 100


def test_legacy_predefined_expectations_are_offered_too():
    item = _contract_item("VULNERABILITY", "Not vulnerable")
    content = json.dumps({"fields": [{"key": "expectations", "predefinedExpectations": [item]}]})

    assert _contract_offered_expectations(content) == {"VULNERABILITY": item}


@pytest.mark.parametrize("content", [None, "", "not json", "[]", '{"fields": "x"}'])
def test_offered_reader_tolerates_malformed_content(content):
    assert _contract_offered_expectations(content) == {}


# ── Create: add_openaev_inject_to_scenario(expectations=[...]) ──────────────


@pytest.mark.asyncio
async def test_nuclei_cve_scan_vulnerability_stores_not_vulnerable(monkeypatch):
    recorder, executor = _install(monkeypatch)
    recorder.available_expectations = NUCLEI_CVE_CONTRACT

    await _add(executor, expectations=["VULNERABILITY"])

    [written] = _content(recorder)["expectations"]
    assert written["expectation_type"] == "VULNERABILITY"
    assert written["expectation_name"] == "Not vulnerable"


@pytest.mark.asyncio
@pytest.mark.parametrize("expectation_type", CREATABLE_TYPES)
async def test_create_keeps_the_contract_name(monkeypatch, expectation_type):
    recorder, executor = _install(monkeypatch)
    item = _contract_item(expectation_type, _contract_name(expectation_type))
    recorder.available_expectations = [item]

    await _add(executor, expectations=[expectation_type])

    assert _content(recorder)["expectations"] == [item]


@pytest.mark.asyncio
async def test_create_keeps_every_contract_name_in_one_call(monkeypatch):
    recorder, executor = _install(monkeypatch)
    items = [_contract_item(t, _contract_name(t)) for t in CREATABLE_TYPES]
    recorder.available_expectations = items

    await _add(executor, expectations=CREATABLE_TYPES)

    assert _content(recorder)["expectations"] == items


@pytest.mark.asyncio
@pytest.mark.parametrize("expectation_type", CREATABLE_TYPES)
async def test_create_of_an_unoffered_type_uses_openaev_default(monkeypatch, expectation_type):
    recorder, executor = _install(monkeypatch)

    await _add(executor, expectations=[expectation_type])

    [written] = _content(recorder)["expectations"]
    assert written["expectation_name"] == OPENAEV_DEFAULTS[expectation_type]


@pytest.mark.asyncio
async def test_create_falls_back_to_openaev_defaults_when_the_contract_is_unreadable(monkeypatch):
    recorder, executor = _install(monkeypatch)
    recorder.available_expectations = NUCLEI_CVE_CONTRACT
    recorder.contract_error = "OpenAEV error (HTTP 503): Service Unavailable"

    await _add(executor, expectations=["VULNERABILITY"])

    [written] = _content(recorder)["expectations"]
    assert written["expectation_name"] == "Not vulnerable"


# ── Update: manage_openaev_inject(action="update") ─────────────────────────


STORED_EXPECTATIONS = [_contract_item(t, _contract_name(t)) for t in ALL_TYPES]


def _stored_inject() -> dict[str, Any]:
    return {
        "inject_id": "i1",
        "inject_title": "Scan",
        "inject_injector_contract": {"injector_contract_id": "c1"},
        "inject_content": {"target_selector": "assets", "expectations": STORED_EXPECTATIONS},
        "inject_teams": [],
        "inject_assets": [{"asset_id": "a1"}],
        "inject_asset_groups": [],
        "inject_tags": [],
    }


class _Resp:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def json(self) -> dict[str, Any]:
        return self._data


async def _update(monkeypatch: pytest.MonkeyPatch, **arguments: Any) -> dict[str, Any]:
    from app.integrations.providers.openaev import injects

    puts: list[dict[str, Any]] = []

    async def api(base_url: str, headers: Any, method: str, path: str, action: str, **kw: Any):
        if method == "put":
            puts.append(kw.get("json") or {})
            return _Resp({"inject_id": "i1", "inject_title": "Scan", **(kw.get("json") or {})}), None
        return _Resp(_stored_inject()), None

    monkeypatch.setattr(injects, "_api", api)
    executor = CallableToolExecutor()
    build_tools("https://demo.openaev.io||test-token", executor)
    result = await executor.execute_tool(
        ToolCall(
            id="u",
            name="manage_openaev_inject",
            arguments={"action": "update", "parent_id": "s1", "inject_id": "i1", **arguments},
        )
    )
    assert not result.is_error, result.content
    assert len(puts) == 1, result.content
    return puts[0]


def _names(content: dict[str, Any]) -> list[tuple[str, str]]:
    return [(e["expectation_type"], e["expectation_name"]) for e in content["expectations"]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"title": "Renamed scan"},
        {"asset_ids": ["a2"]},
        {"content": json.dumps({"target_selector": "assets", "command": "nuclei -u target"})},
    ],
    ids=["title", "targets", "content-without-expectations"],
)
async def test_update_leaves_every_stored_expectation_name_identical(monkeypatch, arguments):
    body = await _update(monkeypatch, **arguments)

    assert body["inject_content"]["expectations"] == STORED_EXPECTATIONS
    assert _names(body["inject_content"]) == [(t, _contract_name(t)) for t in ALL_TYPES]


@pytest.mark.asyncio
async def test_update_with_its_own_expectations_is_sent_verbatim(monkeypatch):
    own = [{"expectation_type": "VULNERABILITY", "expectation_name": "Caller's name"}]

    body = await _update(monkeypatch, content=json.dumps({"expectations": own}))

    assert body["inject_content"]["expectations"] == own


def test_stored_input_keeps_expectations_stored_as_a_json_string():
    stored = _stored_inject()
    stored["inject_content"] = json.dumps(stored["inject_content"])

    body, refusal = stored_inject_input(stored, {"inject_content": {"command": "id"}})

    assert refusal == ""
    assert body["inject_content"] == {"command": "id", "expectations": STORED_EXPECTATIONS}
