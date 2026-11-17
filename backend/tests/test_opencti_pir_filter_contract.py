"""Tests for the OpenCTI PIR filter write contract.

PIRs created via the assistant were stored with loosely shaped or
entity-style filters: OpenCTI accepts them at the API level but the PIR then
fails to render in the dashboard and never flags any entity. The tool now
normalizes every FilterGroup to OpenCTI's canonical shape and rejects
criteria that do not follow the relationship-matching contract
(``entity_type`` + ``toId`` filters with resolved entity UUIDs).

No network: validation failures return before any GraphQL call, and the
success path stubs ``_graphql`` to capture the exact payload sent.
"""

import json

import app.integrations.providers.opencti.pir as pir_mod
from app.tools.base import CallableToolExecutor, ToolCall

ENTITY_UUID = "550e8400-e29b-41d4-a716-446655440000"

VALID_CRITERION = {
    "weight": 1,
    "filters": {
        "mode": "and",
        "filterGroups": [],
        "filters": [
            {"key": ["entity_type"], "values": ["targets"], "operator": "eq", "mode": "or"},
            {"key": ["toId"], "values": [ENTITY_UUID], "operator": "eq", "mode": "or"},
        ],
    },
}

VALID_FILTERS = {
    "mode": "and",
    "filterGroups": [],
    "filters": [
        {"key": ["confidence"], "values": ["60"], "operator": "gte", "mode": "or"},
    ],
}


class _RecordingGraphQL:
    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []

    async def __call__(self, client, base_url, headers, query, variables=None):
        self.calls.append((query, variables))
        if "pirAdd" in query:
            return {"pirAdd": {"id": "pir-1", "name": "Test PIR"}}, None
        if "pirFlagElement" in query:
            return {"pirFlagElement": True}, None
        return {}, None


def _build(monkeypatch):
    stub = _RecordingGraphQL()
    monkeypatch.setattr(pir_mod, "_graphql", stub)
    executor = CallableToolExecutor()
    pir_mod.register("https://demo.opencti.io", {}, executor)
    return executor, stub


async def _create(executor, *, pir_filters, pir_criteria, **extra):
    args = {
        "action": "create",
        "name": "Test PIR",
        "pir_type": "THREAT_LANDSCAPE",
        "pir_filters": json.dumps(pir_filters) if not isinstance(pir_filters, str) else pir_filters,
        "pir_criteria": json.dumps(pir_criteria) if not isinstance(pir_criteria, str) else pir_criteria,
        **extra,
    }
    result = await executor.execute_tool(ToolCall(id="t", name="manage_opencti_pir", arguments=args))
    return result.content


# ── Success path: canonical input goes through, loose input is normalized ──


async def test_create_with_canonical_filters_succeeds(monkeypatch):
    executor, stub = _build(monkeypatch)
    out = await _create(executor, pir_filters=VALID_FILTERS, pir_criteria=[VALID_CRITERION])
    assert "Created PIR" in out
    assert len(stub.calls) == 1
    inp = stub.calls[0][1]["input"]
    assert inp["pir_filters"] == VALID_FILTERS
    assert inp["pir_criteria"] == [VALID_CRITERION]


async def test_create_normalizes_loose_filter_shapes(monkeypatch):
    # Scalar keys, numeric values, missing filterGroups, and missing
    # operator/mode must all be coerced to the canonical OpenCTI shape.
    executor, stub = _build(monkeypatch)
    loose_filters = {
        "mode": "AND",
        "filters": [{"key": "confidence", "values": 60, "operator": "gte"}],
    }
    loose_criterion = {
        "weight": 2,
        "filters": {
            "mode": "and",
            "filters": [
                {"key": "entity_type", "values": "targets"},
                {"key": "toId", "values": [ENTITY_UUID]},
            ],
        },
    }
    out = await _create(executor, pir_filters=loose_filters, pir_criteria=[loose_criterion])
    assert "Created PIR" in out
    inp = stub.calls[0][1]["input"]
    assert inp["pir_filters"] == {
        "mode": "and",
        "filters": [{"key": ["confidence"], "values": ["60"], "operator": "gte", "mode": "or"}],
        "filterGroups": [],
    }
    sent_criterion = inp["pir_criteria"][0]
    assert sent_criterion["weight"] == 2
    assert sent_criterion["filters"]["filterGroups"] == []
    assert sent_criterion["filters"]["filters"] == [
        {"key": ["entity_type"], "values": ["targets"], "operator": "eq", "mode": "or"},
        {"key": ["toId"], "values": [ENTITY_UUID], "operator": "eq", "mode": "or"},
    ]


async def test_create_lowercases_filter_operator_and_mode(monkeypatch):
    # operator/mode are stored verbatim by OpenCTI, so uppercase variants
    # ("EQ", "OR") must be coerced to the canonical lowercase form.
    executor, stub = _build(monkeypatch)
    loose_filters = {
        "mode": "and",
        "filterGroups": [],
        "filters": [
            {"key": ["confidence"], "values": ["60"], "operator": "GTE", "mode": "OR"},
        ],
    }
    out = await _create(executor, pir_filters=loose_filters, pir_criteria=[VALID_CRITERION])
    assert "Created PIR" in out
    sent = stub.calls[0][1]["input"]["pir_filters"]["filters"][0]
    assert sent["operator"] == "gte"
    assert sent["mode"] == "or"


async def test_create_rejects_non_string_operator_or_invalid_mode(monkeypatch):
    executor, stub = _build(monkeypatch)
    bad_operator = {
        "mode": "and",
        "filterGroups": [],
        "filters": [{"key": ["confidence"], "values": ["60"], "operator": 5}],
    }
    out = await _create(executor, pir_filters=bad_operator, pir_criteria=[VALID_CRITERION])
    assert "FilterGroup" in out
    assert not stub.calls
    bad_mode = {
        "mode": "and",
        "filterGroups": [],
        "filters": [{"key": ["confidence"], "values": ["60"], "mode": "xor"}],
    }
    out = await _create(executor, pir_filters=bad_mode, pir_criteria=[VALID_CRITERION])
    assert "FilterGroup" in out
    assert not stub.calls


# ── Contract violations are rejected before any API call ──────────────────


async def test_create_rejects_criterion_missing_to_id(monkeypatch):
    executor, stub = _build(monkeypatch)
    bad = {
        "weight": 1,
        "filters": {
            "mode": "and",
            "filterGroups": [],
            "filters": [{"key": ["entity_type"], "values": ["Malware"]}],
        },
    }
    out = await _create(executor, pir_filters=VALID_FILTERS, pir_criteria=[bad])
    assert "toId" in out
    assert not stub.calls


async def test_create_rejects_empty_to_id_values(monkeypatch):
    # An empty toId list passes the UUID check vacuously but produces a
    # criterion that can never match any relationship.
    executor, stub = _build(monkeypatch)
    bad = {
        "weight": 1,
        "filters": {
            "mode": "and",
            "filterGroups": [],
            "filters": [
                {"key": ["entity_type"], "values": ["targets"]},
                {"key": ["toId"], "values": []},
            ],
        },
    }
    out = await _create(executor, pir_filters=VALID_FILTERS, pir_criteria=[bad])
    assert "'toId' filter has no values" in out
    assert not stub.calls


async def test_create_rejects_unresolved_entity_names_in_to_id(monkeypatch):
    executor, stub = _build(monkeypatch)
    bad = {
        "weight": 1,
        "filters": {
            "mode": "and",
            "filterGroups": [],
            "filters": [
                {"key": ["entity_type"], "values": ["targets"]},
                {"key": ["toId"], "values": ["APT28"]},
            ],
        },
    }
    out = await _create(executor, pir_filters=VALID_FILTERS, pir_criteria=[bad])
    assert "UUID" in out
    assert "search_opencti" in out
    assert not stub.calls


async def test_create_rejects_non_filter_group_pir_filters(monkeypatch):
    executor, stub = _build(monkeypatch)
    out = await _create(executor, pir_filters=[{"key": "confidence"}], pir_criteria=[VALID_CRITERION])
    assert "FilterGroup" in out
    assert not stub.calls


async def test_create_rejects_invalid_weight(monkeypatch):
    executor, stub = _build(monkeypatch)
    bad = dict(VALID_CRITERION, weight=0)
    out = await _create(executor, pir_filters=VALID_FILTERS, pir_criteria=[bad])
    assert "weight" in out
    assert not stub.calls


# ── Flag path follows the same contract ───────────────────────────────────


async def test_flag_normalizes_and_validates_matching_criteria(monkeypatch):
    executor, stub = _build(monkeypatch)
    result = await executor.execute_tool(
        ToolCall(
            id="t",
            name="manage_opencti_pir",
            arguments={
                "action": "flag",
                "pir_id": "pir-1",
                "relationship_id": "rel-1",
                "source_id": "src-1",
                "matching_criteria": json.dumps([VALID_CRITERION]),
            },
        )
    )
    assert "Flagged element" in result.content
    inp = stub.calls[0][1]["input"]
    assert inp["matchingCriteria"] == [VALID_CRITERION]


async def test_flag_rejects_criteria_missing_contract_filters(monkeypatch):
    executor, stub = _build(monkeypatch)
    bad = {"weight": 1, "filters": {"mode": "and", "filters": [], "filterGroups": []}}
    result = await executor.execute_tool(
        ToolCall(
            id="t",
            name="manage_opencti_pir",
            arguments={
                "action": "flag",
                "pir_id": "pir-1",
                "relationship_id": "rel-1",
                "source_id": "src-1",
                "matching_criteria": json.dumps([bad]),
            },
        )
    )
    assert "matching_criteria" in result.content
    assert not stub.calls
