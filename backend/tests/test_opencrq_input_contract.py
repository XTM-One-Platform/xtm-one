"""The OpenCRQ tools hold what they send against the connected OpenCRQ's own schema.

OpenCRQ's Zod schemas drop a key they do not know without an error, so a tool that sends a field
the connected instance lacks looks like it worked while the field is ignored.
``providers/opencrq/input_contract.py`` walks a call's input against the schema the instance's
manifest gives that procedure, and ``_trpc`` refuses, before anything is sent, an unknown key
(unless its value is ``None``) and a missing required one.

XTM One holds no OpenCRQ schema: every fixture here is a neutral shape written for the case it
serves, in the constructs JSON Schema's export of a Zod object spells (unions, ``const`` branches,
records, ``$ref``, intersections), never a copy of what OpenCRQ serves.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from unittest.mock import AsyncMock

import httpx
import pytest

import app.integrations.providers.opencrq as opencrq
from app.integrations.providers.opencrq import _common as opencrq_common
from app.integrations.providers.opencrq import input_contract
from app.integrations.providers.opencrq.input_contract import (
    InputContract,
    LimitedField,
    issues,
    known_paths,
    limited_fields,
)
from app.integrations.providers.opencrq.procedure_manifest import (
    PROBE,
    Manifest,
    ManifestProbe,
    load_tool_procedures,
)
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.opencrq.io||test-token"
BASE_URL = "https://demo.opencrq.io"

NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}
# An object closed with additionalProperties: false.
STRICT = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "note": {"type": ["string", "null"]},
        "curator": NULLABLE_STRING,
        "partIds": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["id"],
    "additionalProperties": False,
}
# Nullable strings, an enum, a nested union of objects told apart by const.
ITEMS = {
    "type": "object",
    "properties": {
        "page": {"type": "integer"},
        "sortBy": {"type": "string", "enum": ["rank", "label"]},
        "groupId": NULLABLE_STRING,
        "target": {
            "anyOf": [
                {
                    "oneOf": [
                        {
                            "type": "object",
                            "properties": {"kind": {"type": "string", "const": "alpha"}, "alphaId": {"type": "string"}},
                            "required": ["kind", "alphaId"],
                        },
                        {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string", "const": "beta"},
                                "betaRef": {"type": "string"},
                                "mode": {"type": "string", "enum": ["fast", "slow"]},
                            },
                            "required": ["kind", "betaRef", "mode"],
                        },
                    ]
                },
                {"type": "null"},
            ]
        },
    },
}
# The whole input is a oneOf, told apart by the const of ``state``.
STATE_UNION = {
    "oneOf": [
        {
            "type": "object",
            "properties": {"itemId": {"type": "string"}, "state": {"type": "string", "const": "open"}},
            "required": ["itemId", "state"],
        },
        {
            "type": "object",
            "properties": {
                "itemId": {"type": "string"},
                "state": {"type": "string", "const": "closed"},
                "closeKind": {"type": "string", "enum": ["done", "dropped"]},
                "reason": {"type": "string"},
            },
            "required": ["itemId", "state", "closeKind", "reason"],
        },
    ]
}
# A union of objects with no const to tell them apart.
EITHER = {
    "anyOf": [
        {
            "type": "object",
            "properties": {"refId": {"type": "string"}, "leftId": {"type": "string"}},
            "required": ["refId", "leftId"],
        },
        {
            "type": "object",
            "properties": {"refId": {"type": "string"}, "rightId": {"type": "string"}},
            "required": ["refId", "rightId"],
        },
    ]
}
# A union of consts nested in a nullable.
ANSWER = {
    "type": "object",
    "properties": {
        "formId": {"type": "string"},
        "level": {
            "anyOf": [
                {"anyOf": [{"type": "number", "const": 0}, {"type": "number", "const": 1}]},
                {"type": "null"},
            ]
        },
    },
    "required": ["formId", "level"],
}
# The input itself is an array.
LINKS = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "targetId": {"type": "string"}},
        "required": ["id", "targetId"],
    },
}
# Zod exports a nullable array of objects, a record, an intersection and a shared definition like this.
SAMPLES = {
    "type": "object",
    "properties": {
        "samples": {
            "anyOf": [
                {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"driverId": {"type": "string"}, "low": {"type": "number"}},
                        "required": ["driverId"],
                    },
                },
                {"type": "null"},
            ]
        }
    },
}
RECORD = {
    "type": "object",
    "properties": {
        "labels": {
            "type": "object",
            "propertyNames": {"type": "string"},
            "additionalProperties": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        }
    },
}
LOOSE = {"type": "object", "properties": {"id": {"type": "string"}, "extra": {}}, "additionalProperties": {}}
INTERSECTION = {
    "allOf": [
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
        {"type": "object", "properties": {"b": {"type": "string"}}, "required": ["b"]},
    ]
}
REFERENCED = {
    "$defs": {
        "Tag": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        "Node": {
            "type": "object",
            "properties": {"children": {"type": "array", "items": {"$ref": "#/$defs/Node"}}},
        },
    },
    "type": "object",
    "properties": {
        "tag": {"$ref": "#/$defs/Tag"},
        "tags": {"type": "array", "items": {"$ref": "#/$defs/Tag"}},
        "tree": {"$ref": "#/$defs/Node"},
    },
}


# --- The walk ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("schema", "value", "unknown", "missing"),
    [
        (STRICT, {"id": "r", "name": "n"}, [], []),
        (STRICT, {"id": "r", "renamed": "n"}, ["renamed"], []),
        (STRICT, {"name": "n"}, [], ["id"]),
        (STRICT, {"id": "r", "partIds": ["a"], "curator": None, "note": None}, [], []),
        (ITEMS, {"page": 1, "groupId": None, "target": None}, [], []),
        (ITEMS, {"unknownFilter": "x"}, ["unknownFilter"], []),
        (ITEMS, {"target": {"kind": "alpha", "alphaId": "A-1"}}, [], []),
        (ANSWER, {"formId": "a", "level": 3}, [], []),
        (ANSWER, {"formId": "a", "level": None}, [], []),
        (ANSWER, {"formId": "a"}, [], ["level"]),
        (ANSWER, {"formId": "a", "level": 1, "comment": "c"}, ["comment"], []),
        (LINKS, [{"id": "1", "targetId": "p"}], [], []),
        (SAMPLES, {"samples": None}, [], []),
        (SAMPLES, {"samples": [{"driverId": "d", "low": 1}]}, [], []),
    ],
    ids=[
        "strict accepts its keys",
        "strict refuses an unknown key",
        "strict names a missing required key",
        "nullable shapes and None values",
        "nullable anyOf keys",
        "unknown key beside nullable ones",
        "a branch told apart by const",
        "const values are not checked",
        "a required key may carry None",
        "a required key left out",
        "unknown key beside a const union",
        "top-level array",
        "a nullable array of objects, null",
        "a nullable array of objects, filled",
    ],
)
def test_the_walk_over_the_shapes_the_manifest_holds(schema, value, unknown, missing):
    assert issues(schema, value) == (unknown, missing)


def test_an_unknown_key_under_none_is_a_field_left_unset_and_passes():
    assert issues(STRICT, {"id": "r", "retired": None}) == ([], [])
    assert issues(STRICT, {"id": "r", "retired": 0}) == (["retired"], [])
    assert issues(STRICT, {"id": "r", "retired": ""}) == (["retired"], [])


def test_a_key_is_unknown_at_any_depth_and_named_by_its_path():
    value = {"target": {"kind": "beta", "betaRef": "B-1", "mode": "fast", "extra": 1}}
    assert issues(ITEMS, value) == (["target.extra"], [])
    assert issues(LINKS, [{"id": "1", "targetId": "p", "extra": 1}, {"id": "2"}]) == (
        ["[0].extra"],
        ["[1].targetId"],
    )
    samples = {"samples": [{"driverId": "d"}, {"driverId": "e", "high": 2}, {"low": 1}]}
    assert issues(SAMPLES, samples) == (["samples[1].high"], ["samples[2].driverId"])


def test_a_union_branch_is_chosen_by_const_before_keys_are_judged():
    applicable = {"itemId": "e", "state": "open", "reason": "why"}
    dismissed = {"itemId": "e", "state": "closed", "closeKind": "dropped", "reason": "why"}
    # ``reason`` exists in the dismissed branch, and is still unknown to the applicable one.
    assert issues(STATE_UNION, applicable) == (["reason"], [])
    assert issues(STATE_UNION, dismissed) == ([], [])
    assert issues(STATE_UNION, {"itemId": "e", "state": "closed"}) == ([], ["closeKind", "reason"])


def test_a_union_with_nothing_to_tell_its_branches_apart_knows_every_branchs_keys():
    assert issues(EITHER, {"refId": "a", "leftId": "s"}) == ([], [])
    assert issues(EITHER, {"refId": "a", "rightId": "o"}) == ([], [])
    assert issues(EITHER, {"refId": "a", "bogus": "x"}) == (["bogus"], ["leftId"])


def test_a_missing_key_names_the_closest_branch():
    assert issues(EITHER, {"leftId": "s"}) == ([], ["refId"])


def test_a_value_matching_no_branch_by_json_type_passes():
    schema = {"anyOf": [{"type": "string"}, {"type": "number"}]}
    assert issues(schema, {"anything": 1}) == ([], [])
    assert issues({"type": "object", "properties": {"a": schema}}, {"a": {"x": 1}, "b": 1}) == (["b"], [])


def test_a_discriminator_no_branch_has_passes():
    assert issues(STATE_UNION, {"itemId": "e", "state": "archived", "bogus": 1}) == ([], [])


def test_scalars_are_never_checked():
    assert issues(STRICT, {"id": 12, "partIds": "not a list", "name": {"a": 1}}) == ([], [])
    assert issues(ITEMS, {"sortBy": "no such sort", "page": "one"}) == ([], [])


def test_a_record_accepts_any_key_and_checks_its_values():
    assert issues(RECORD, {"labels": {"x": {"text": "t"}, "y": {"text": "u"}}}) == ([], [])
    assert issues(RECORD, {"labels": {"x": {"text": "t", "color": "red"}, "y": {}}}) == (
        ["labels.x.color"],
        ["labels.y.text"],
    )


def test_a_loose_object_accepts_keys_it_does_not_list():
    assert issues(LOOSE, {"id": "i", "whatever": {"deep": 1}, "extra": {"deep": 1}}) == ([], [])
    assert issues({"type": "object", "additionalProperties": True}, {"a": 1}) == ([], [])
    assert issues({}, {"a": 1}) == ([], [])


def test_an_intersection_knows_and_requires_the_keys_of_every_member():
    assert issues(INTERSECTION, {"a": "x", "b": "y"}) == ([], [])
    assert issues(INTERSECTION, {"a": "x", "c": "z"}) == (["c"], ["b"])


def test_a_reference_is_read_from_the_defs_of_the_procedure_root():
    value = {"tag": {"name": "n", "color": "red"}, "tags": [{"name": "a"}, {}], "tree": {"children": [{"bad": 1}]}}
    assert issues(REFERENCED, value) == (["tag.color", "tree.children[0].bad"], ["tags[1].name"])


@pytest.mark.parametrize(
    "ref",
    ["https://example.test/schema.json#/$defs/Tag", "#/definitions/Tag", "#/$defs/Missing", "#", 12],
    ids=["remote", "other root", "missing", "the root", "not a string"],
)
def test_a_reference_the_walk_cannot_follow_passes(ref):
    schema = {
        "$defs": REFERENCED["$defs"],
        "type": "object",
        "properties": {"tag": {"$ref": ref}, "plain": {"type": "string"}},
    }
    assert issues(schema, {"tag": {"unknown": 1}, "plain": "p", "stray": 1}) == (["stray"], [])


def test_a_reference_cycle_ends_and_passes():
    schema = {
        "$defs": {"Loop": {"$ref": "#/$defs/Loop"}},
        "type": "object",
        "properties": {"x": {"$ref": "#/$defs/Loop"}},
    }
    assert issues(schema, {"x": {"anything": 1}}) == ([], [])


def test_a_subtree_with_a_keyword_the_walk_does_not_read_passes():
    schema = {
        "type": "object",
        "properties": {
            "guarded": {"type": "object", "minProperties": 1, "properties": {}},
            "read": {"type": "object", "properties": {"a": {"type": "string"}}},
        },
    }
    assert issues(schema, {"guarded": {"unknown": 1}, "read": {"a": "x", "b": "y"}, "stray": 1}) == (
        ["read.b", "stray"],
        [],
    )


def test_a_union_with_a_branch_the_walk_cannot_read_passes_whole():
    schema = {"anyOf": [{"type": "object", "properties": {"a": {}}}, {"type": "object", "patternProperties": {}}]}
    assert issues(schema, {"unknown": 1}) == ([], [])


def test_known_paths_are_the_keys_a_value_may_carry_at_any_depth():
    schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "rows": {"type": "array", "items": {"type": "object", "properties": {"k": {"type": "string"}}}},
            "labels": {"type": "object", "additionalProperties": {"type": "object", "properties": {"v": {}}}},
            "pick": {
                "anyOf": [{"type": "object", "properties": {"a": {}}}, {"type": "object", "properties": {"b": {}}}]
            },
        },
    }
    paths, opaque = known_paths(schema)
    assert paths == {"id", "rows", "rows[].k", "labels", "labels.*.v", "pick", "pick.a", "pick.b"}
    assert opaque == set()
    # The static twin of the walk: a path it lists is never refused as unknown, one it does not list is.
    assert issues(schema, {"rows": [{"k": "x"}], "labels": {"any": {"v": 1}}, "pick": {"a": 1}}) == ([], [])
    assert issues(schema, {"rows": [{"nope": "x"}]}) == (["rows[0].nope"], [])


def test_a_subtree_the_walk_cannot_read_is_opaque_not_empty():
    schema = {"type": "object", "properties": {"guarded": {"type": "object", "minProperties": 1}, "a": {}}}
    paths, opaque = known_paths(schema)
    assert paths == {"guarded", "a"}
    assert opaque == {"guarded"}
    assert known_paths(None) == (set(), set())
    assert known_paths({"type": "object", "patternProperties": {}}) == (set(), {""})


def test_no_schema_or_none_has_nothing_to_check():
    assert issues(None, {"a": 1}) == ([], [])
    assert issues(STRICT, None) == ([], [])
    assert issues(STRICT, "text") == ([], [])


# --- What a refusal says ------------------------------------------------------------------

PROCEDURE = "widgets.update"
LABEL = "this OpenCRQ instance"


def _manifest(procedures: Mapping[str, object]) -> Manifest:
    return Manifest(
        None,
        MappingProxyType(
            {path: MappingProxyType({"type": "mutation", "input": schema}) for path, schema in procedures.items()}
        ),
    )


def _object(*names: str, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": {name: {"type": "string"} for name in names}, "required": list(required)}


WIDGET = _object("id", "label", required=("id", "label"))


def _contract(schema: object = WIDGET) -> InputContract:
    return InputContract(_manifest({PROCEDURE: schema}))


def test_a_field_the_instance_lacks_is_refused_and_named():
    assert _contract().check(PROCEDURE, {"id": "w", "label": "l", "owner": "o"}) == (
        f"Error: nothing was sent to OpenCRQ: widgets.update on {LABEL} "
        "does not accept the field(s) `owner` and would drop them."
    )


def test_a_missing_required_field_is_refused_and_named():
    assert _contract().check(PROCEDURE, {"id": "w"}) == (
        f"Error: nothing was sent to OpenCRQ: widgets.update on {LABEL} requires the field(s) `label`."
    )


# What ``z.toJSONSchema(input, { io: 'input' })`` spells for a field with a default: the default rides
# along and the key is absent from ``required``.
DEFAULTED = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "sort": {"type": "string", "enum": ["asc", "desc"], "default": "asc"},
    },
    "required": ["id"],
}


def test_a_field_with_a_default_is_not_required():
    contract = _contract(DEFAULTED)
    assert contract.check(PROCEDURE, {"id": "w"}) is None
    assert contract.check(PROCEDURE, {"id": "w", "sort": "desc"}) is None
    assert contract.check(PROCEDURE, {"sort": "desc"}) == (
        f"Error: nothing was sent to OpenCRQ: widgets.update on {LABEL} requires the field(s) `id`."
    )


def test_both_problems_are_named_in_one_refusal():
    refusal = _contract().check(PROCEDURE, {"owner": "o"})
    assert refusal.startswith("Error: ")
    assert "`owner`" in refusal and "`id`, `label`" in refusal


def test_a_refusal_carries_no_release_and_no_schema_text():
    refusal = _contract().check(PROCEDURE, {"id": "w", "label": "l", "owner": "o"})
    assert "release" not in refusal and "main" not in refusal and "properties" not in refusal


def test_a_refusal_lists_a_few_paths_and_counts_the_rest():
    keys = {f"field{i}": "x" for i in range(8)}
    refusal = _contract().check(PROCEDURE, {"id": "w", "label": "l", **keys})
    assert refusal.count("`field") == 5
    assert "3 more" in refusal


def test_a_path_the_model_wrote_is_cut_before_it_reaches_the_model_again():
    refusal = _contract().check(PROCEDURE, {"id": "w", "label": "l", "x" * 500: "v"})
    assert "x" * 81 not in refusal
    assert "`" + "x" * 80 + "`" in refusal


def test_a_procedure_with_no_input_or_unknown_to_the_manifest_has_nothing_to_check():
    contract = InputContract(_manifest({"widgets.list": None}))
    assert contract.check("widgets.list", {"anything": 1}) is None
    assert contract.check("widgets.gone", {"anything": 1}) is None


# --- _trpc ----------------------------------------------------------------------------------


@pytest.fixture
def sent(monkeypatch) -> AsyncMock:
    """The request ``_trpc`` would make, answered with a success."""
    request = AsyncMock(return_value=(httpx.Response(200, json={"result": {"data": {"ok": True}}}), None))
    monkeypatch.setattr(opencrq_common, "safe_request", request)
    return request


@pytest.fixture
def refreshed(monkeypatch) -> AsyncMock:
    refresh = AsyncMock()
    monkeypatch.setattr(opencrq_common, "_refresh_if_stale", refresh)
    return refresh


def _headers_of(manifest: Manifest | None) -> opencrq_common.OpenCrqHeaders:
    headers = opencrq_common._headers("test-token")
    headers.manifest_probe = ManifestProbe.known(manifest)
    return headers


def _held(schema: object = WIDGET) -> opencrq_common.OpenCrqHeaders:
    return _headers_of(_manifest({PROCEDURE: schema}))


async def _update(headers, input, *, mutation=True):
    return await opencrq_common._trpc(
        BASE_URL, headers, PROCEDURE, "update widget", input=input, mutation=mutation, tenant_id="t-1"
    )


async def test_a_call_with_an_unknown_field_sends_nothing(sent, refreshed):
    data, err = await _update(_held(), {"id": "w", "label": "l", "owner": "o"})
    assert data is None
    assert err.startswith("Error: nothing was sent to OpenCRQ: widgets.update on this OpenCRQ instance")
    assert "`owner`" in err
    sent.assert_not_awaited()
    refreshed.assert_not_awaited()


async def test_the_refusal_is_neither_an_http_failure_nor_an_auth_one(sent):
    _, err = await _update(_held(), {"id": "w", "label": "l", "owner": "o"})
    assert isinstance(err, opencrq_common.TrpcError)
    assert err.reason is None and err.status is None


async def test_a_call_that_leaves_out_a_required_field_sends_nothing(sent):
    _, err = await _update(_held(), {"id": "w"})
    assert "requires the field(s) `label`" in err
    sent.assert_not_awaited()


async def test_a_passing_input_is_sent_unchanged(sent, refreshed):
    payload = {"id": "w", "label": "l", "owner": None}
    data, err = await _update(_held(), payload)
    assert (data, err) == ({"ok": True}, None)
    sent.assert_awaited_once()
    assert sent.await_args.kwargs["json"] == payload
    assert sent.await_args.kwargs["headers"]["x-tenant-id"] == "t-1"
    refreshed.assert_awaited_once()


async def test_a_query_input_is_checked_as_it_is_sent_in_the_url(sent):
    _, err = await _update(_held(), {"id": "w", "owner": "o"}, mutation=False)
    assert "`owner`" in err
    sent.assert_not_awaited()


async def test_a_mutation_with_no_input_is_checked_as_the_empty_object_it_sends(sent):
    _, err = await _update(_held(), None)
    assert "requires the field(s) `id`, `label`" in err
    sent.assert_not_awaited()


async def test_a_query_with_no_input_is_not_checked(sent):
    data, err = await _update(_held(), None, mutation=False)
    assert (data, err) == ({"ok": True}, None)


async def test_headers_without_a_probe_are_never_checked(sent):
    bad = {"id": "w", "owner": "o", "bogus": "b"}
    for headers in ({"Authorization": "Bearer t"}, opencrq_common._headers("t")):
        data, err = await _update(headers, bad)
        assert (data, err) == ({"ok": True}, None)
    assert sent.await_count == 2


async def test_a_build_with_no_manifest_checks_nothing(sent):
    data, err = await _update(_headers_of(None), {"id": "w", "owner": "o", "bogus": "b"})
    assert (data, err) == ({"ok": True}, None)


async def test_the_first_call_of_a_build_with_no_registration_reads_the_manifest(sent, monkeypatch):
    read = AsyncMock(return_value=_manifest({PROCEDURE: WIDGET}))
    monkeypatch.setattr("app.integrations.providers.opencrq.procedure_manifest.read_manifest", read)
    headers = opencrq_common._headers("test-token")
    headers.manifest_probe = ManifestProbe(BASE_URL, headers)

    _, err = await _update(headers, {"id": "w", "label": "l", "owner": "o"})
    assert "`owner`" in err
    data, _ = await _update(headers, {"id": "w", "label": "l"})
    assert data == {"ok": True}
    read.assert_awaited_once()
    assert sent.await_count == 1, "the manifest read is not a call of _trpc: only the passing call went out"


# --- Through build_tools ----------------------------------------------------------------------

TOOL = "update_opencrq_risk_scenario"
TOOL_PROCEDURE = "riskScenarios.updateRiskScenario"


def _serving_every_tool(schema: object) -> Manifest:
    """Every procedure a tool sends, with no input but the one a tool of the case sends."""
    procedures = {p: None for sent in load_tool_procedures().values() for p in sent}
    procedures[TOOL_PROCEDURE] = schema
    return _manifest(procedures)


NARROW = _object("id", "name", "description", required=("id",))


async def _update_tool(executor, **arguments):
    return await executor.execute_tool(
        ToolCall(id="call-1", name=TOOL, arguments={"tenant_id": "t-1", "risk_scenario_id": "r-1", **arguments})
    )


@pytest.mark.parametrize("registered", [True, False])
async def test_a_tool_whose_field_the_instance_lacks_is_refused_before_it_sends(sent, monkeypatch, registered):
    manifest = _serving_every_tool(NARROW)
    if not registered:
        monkeypatch.setattr(
            "app.integrations.providers.opencrq.procedure_manifest.read_manifest", AsyncMock(return_value=manifest)
        )
    executor = CallableToolExecutor()
    opencrq.build_tools(TOKEN, executor, opencrq_manifest=manifest if registered else PROBE)

    refused = await _update_tool(executor, name="New name", owner_id="u-1")
    assert refused.is_error
    assert refused.content.startswith(
        f"Error: nothing was sent to OpenCRQ: {TOOL_PROCEDURE} on this OpenCRQ instance "
        "does not accept the field(s) `ownerId`"
    )
    sent.assert_not_awaited()

    accepted = await _update_tool(executor, name="New name")
    assert not accepted.is_error, accepted.content
    assert sent.await_args.kwargs["json"] == {"id": "r-1", "name": "New name"}


async def test_a_build_with_no_manifest_keyword_never_checks_what_a_tool_sends(sent):
    executor = CallableToolExecutor()
    opencrq.build_tools(TOKEN, executor)
    result = await _update_tool(executor, owner_id="u-1")
    assert not result.is_error, result.content
    assert sent.await_args.kwargs["json"] == {"id": "r-1", "ownerId": "u-1"}


async def test_a_manifest_that_takes_what_the_tool_sends_lets_it_through(sent):
    wide = _object("id", "name", "description", "ownerId", "primaryAssetIds", required=("id",))
    executor = CallableToolExecutor()
    opencrq.build_tools(TOKEN, executor, opencrq_manifest=_serving_every_tool(wide))
    result = await _update_tool(executor, name="n", description="d", owner_id="u", primary_asset_ids=["a"])
    assert not result.is_error, result.content
    assert sent.await_args.kwargs["json"] == {
        "id": "r-1",
        "name": "n",
        "description": "d",
        "primaryAssetIds": ["a"],
        "ownerId": "u",
    }


# --- limited_fields: what the card lists, through the walk a call goes through -------------


def _keys(*entries: dict) -> Mapping[str, tuple[Mapping[str, object], ...]]:
    return {"tool": tuple(MappingProxyType(entry) for entry in entries)}


def _entry(*fields: str, procedure: str = "a.setA", closed: bool = True) -> dict:
    return {"procedures": [procedure], "fields": list(fields), "conditional": [], "closed": closed}


def _limited(monkeypatch, schema: object, *entries: dict, hidden=()) -> dict[str, list[LimitedField]]:
    monkeypatch.setattr(input_contract, "load_tool_input_keys", lambda: _keys(*entries))
    manifest = Manifest(
        "ab" * 32, MappingProxyType({"a.setA": MappingProxyType({"type": "mutation", "input": schema})})
    )
    return limited_fields(manifest, hidden)


def test_a_field_the_schema_does_not_know_is_listed_as_unknown_at_any_depth(monkeypatch):
    schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "nested": {"type": "object", "properties": {"k": {"type": "string"}}},
            "rows": {"type": "array", "items": {"type": "object", "properties": {"r": {"type": "string"}}}},
        },
    }
    limited = _limited(
        monkeypatch, schema, _entry("id", "nested", "nested.k", "nested.gone", "rows", "rows[].r", "rows[].gone", "top")
    )
    assert limited == {
        "tool": [
            LimitedField("a.setA", "nested.gone", "unknown"),
            LimitedField("a.setA", "rows[].gone", "unknown"),
            LimitedField("a.setA", "top", "unknown"),
        ]
    }


def test_a_tool_the_schema_fully_accepts_is_not_listed(monkeypatch):
    schema = {"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}}}
    assert _limited(monkeypatch, schema, _entry("id", "name")) == {}


def test_a_required_field_a_closed_call_never_sends_is_listed_as_missing(monkeypatch):
    schema = {
        "type": "object",
        "properties": {"id": {"type": "string"}, "slug": {"type": "string"}},
        "required": ["id", "slug"],
    }
    assert _limited(monkeypatch, schema, _entry("id")) == {"tool": [LimitedField("a.setA", "slug", "missing")]}


def test_a_required_field_is_not_listed_for_a_call_with_keys_the_source_does_not_spell_out(monkeypatch):
    schema = {
        "type": "object",
        "properties": {"id": {"type": "string"}, "slug": {"type": "string"}},
        "required": ["slug"],
    }
    assert _limited(monkeypatch, schema, _entry("id", closed=False)) == {}


def test_a_required_field_below_the_top_level_is_not_listed(monkeypatch):
    schema = {
        "type": "object",
        "properties": {
            "n": {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}, "required": ["b"]}
        },
    }
    assert _limited(monkeypatch, schema, _entry("n", "n.a")) == {}


def test_a_tool_withheld_for_a_missing_procedure_is_not_listed(monkeypatch):
    schema = {"type": "object", "properties": {}}
    assert _limited(monkeypatch, schema, _entry("id"), hidden={"tool"}) == {}


def test_a_procedure_without_an_input_schema_or_an_unreadable_one_lists_nothing(monkeypatch):
    assert _limited(monkeypatch, None, _entry("id")) == {}
    assert _limited(monkeypatch, {"type": "object", "properties": {}, "patternProperties": {}}, _entry("id")) == {}


def test_the_card_lists_a_field_exactly_when_a_call_carrying_it_is_refused(monkeypatch):
    schema = {"type": "object", "properties": {"id": {"type": "string"}}, "additionalProperties": False}
    entry = _entry("id", "gone")
    [field] = _limited(monkeypatch, schema, entry)["tool"]
    manifest = Manifest(
        "ab" * 32, MappingProxyType({"a.setA": MappingProxyType({"type": "mutation", "input": schema})})
    )
    refusal = InputContract(manifest).check("a.setA", {"id": "x", "gone": "y"})
    assert field.path == "gone" and refusal is not None and "`gone`" in refusal
