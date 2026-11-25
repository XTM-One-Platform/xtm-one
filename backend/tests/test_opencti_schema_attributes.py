"""The fields an OpenCTI instance defines reach the create tools.

``schemaAttributes(entityType)`` (OpenCTI 7.261124.0) tells, per type, each
field's description, whether this instance makes it mandatory and its default
values, custom fields included. ``get_opencti_schema_attributes`` reads it (or,
on an older OpenCTI, the type's entity setting), and the create tools take the
fields they have no parameter for in ``attributes``: a custom field could not
be passed at creation before, even a mandatory one.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

import app.integrations.providers.opencti.knowledge as knowledge_mod
from app.integrations.providers.opencti import build_tools
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.opencti.io||test-token"


class _Recorder:
    """Answers each GraphQL call from ``answers`` in turn and keeps what was sent."""

    def __init__(self, answers: list[tuple[dict[str, Any], str | None]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.answers = list(answers or [])

    async def __call__(
        self,
        _client: Any,
        _base_url: str,
        _headers: Any,
        query: str,
        variables: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> tuple[dict[str, Any], str | None]:
        self.calls.append((query, variables or {}))
        return self.answers.pop(0) if self.answers else ({}, None)

    @property
    def inputs(self) -> list[dict[str, Any]]:
        return [v.get("input") or {} for _, v in self.calls]


def _build(
    monkeypatch: pytest.MonkeyPatch, answers: list[tuple[dict[str, Any], str | None]] | None = None
) -> tuple[CallableToolExecutor, _Recorder, list[Any]]:
    recorder = _Recorder(answers)

    @contextlib.asynccontextmanager
    async def _client(*_args: Any, **_kwargs: Any) -> AsyncIterator[None]:
        yield None

    monkeypatch.setattr(knowledge_mod, "_client", _client)
    monkeypatch.setattr(knowledge_mod, "_graphql", recorder)
    executor = CallableToolExecutor()
    # direct_mutations skips the draft gate: the mutation document is what is pinned.
    definitions = build_tools(TOKEN, executor, direct_mutations=True)
    return executor, recorder, definitions


async def _call(executor: CallableToolExecutor, tool: str, /, **arguments: Any) -> Any:
    return await executor.execute_tool(ToolCall(id="call-1", name=tool, arguments=arguments))


def _field(
    name: str,
    kind: str = "string",
    *,
    multiple: bool = False,
    mandatory: bool = False,
    defaults: list[str] | None = None,
    description: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "type": kind,
        "label": name,
        "description": description,
        "mandatory": mandatory,
        "multiple": multiple,
        "defaultValues": [{"id": d, "name": d} for d in defaults] if defaults else None,
    }


def _fields(entity_type: str, *fields: dict[str, Any]) -> tuple[dict[str, Any], None]:
    """The answer of the field read a create makes before sending."""
    return {"schemaAttributes": [{"type": entity_type, "attributes": list(fields)}]}, None


def _definitions(*definitions: dict[str, Any]) -> tuple[dict[str, Any], None]:
    """The answer of the custom field definitions read."""
    edges = [{"node": d} for d in definitions]
    return {"customFieldDefinitionsForEntityType": {"edges": edges}}, None


def _definition(name: str, field_type: str = "string", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "label": name.removeprefix("x_opencti_cf_").replace("_", " ").title(),
        "aliases": [],
        "field_type": field_type,
        "multiple": field_type == "multi_select",
        "min_value": None,
        "max_value": None,
        "select_options": None,
        **extra,
    }


def _mutations(rec: _Recorder) -> list[str]:
    return [q for q, _ in rec.calls if q.lstrip().startswith("mutation")]


async def test_a_report_takes_a_custom_field_and_a_mandatory_field(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, rec, _ = _build(
        monkeypatch,
        [
            _fields(
                "Report",
                _field("name", mandatory=True),
                _field("published", "date", mandatory=True),
                _field("x_opencti_reliability", mandatory=True),
                _field("x_opencti_cf_case_reference", mandatory=True),
                _field("x_opencti_cf_regions", multiple=True),
                _field("x_opencti_cf_score", "numeric"),
                _field("x_opencti_cf_empty"),
            ),
            _definitions(
                _definition("x_opencti_cf_case_reference"),
                _definition("x_opencti_cf_regions", "multi_select", select_options=["EU", "NA"]),
                _definition("x_opencti_cf_score", "decimal"),
                _definition("x_opencti_cf_empty"),
            ),
        ],
    )
    result = await _call(
        executor,
        "create_opencti_container",
        container_type="Report",
        name="Weekly digest",
        published="2026-11-25T00:00:00Z",
        attributes={
            "x_opencti_reliability": "A - Completely reliable",
            "x_opencti_cf_case_reference": "CR-42",
            "x_opencti_cf_regions": ["EU", "NA"],
            "x_opencti_cf_score": 3.5,
            "x_opencti_cf_empty": "",
        },
    )
    assert rec.calls[0][1] == {"t": "Report"}
    assert len(_mutations(rec)) == 1, result.content
    sent = rec.inputs[-1]
    assert sent["name"] == "Weekly digest"
    assert sent["x_opencti_reliability"] == "A - Completely reliable"
    assert sent["customFieldValues"] == [
        {"field_name": "x_opencti_cf_case_reference", "value": ["CR-42"]},
        {"field_name": "x_opencti_cf_regions", "value": ["EU", "NA"]},
        {"field_name": "x_opencti_cf_score", "value": [3.5]},
    ]
    assert not any(k.startswith("x_opencti_cf_") for k in sent)


@pytest.mark.parametrize(
    ("tool", "arguments", "entity_type"),
    [
        ("create_opencti_entity", {"type": "Malware", "name": "Emotet"}, "Malware"),
        (
            "create_opencti_relationship",
            {"from_id": "a", "to_id": "b", "relationship_type": "uses"},
            "uses",
        ),
        ("create_opencti_note", {"content": "seen", "object_ids": ["a"]}, "Note"),
        (
            "create_opencti_indicator",
            {"name": "i", "pattern": "[ipv4-addr:value = '1.2.3.4']"},
            "Indicator",
        ),
    ],
)
async def test_every_create_tool_checks_then_sends_the_attributes(
    monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, Any], entity_type: str
) -> None:
    executor, rec, _ = _build(
        monkeypatch,
        [_fields(entity_type, _field("x_opencti_cf_source")), _definitions(_definition("x_opencti_cf_source"))],
    )
    await _call(executor, tool, **arguments, attributes={"x_opencti_cf_source": "feed-1"})
    assert rec.calls[0][1] == {"t": entity_type}
    assert rec.calls[1][1] == {"t": entity_type}
    assert rec.inputs[-1]["customFieldValues"] == [
        {"field_name": "x_opencti_cf_source", "value": ["feed-1"]}
    ]


async def test_a_custom_field_alias_is_sent_under_its_name(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, rec, _ = _build(
        monkeypatch,
        [
            _fields("Malware", _field("x_opencti_cf_source")),
            _definitions(_definition("x_opencti_cf_source", aliases=["x_opencti_cf_origin"])),
        ],
    )
    await _call(
        executor, "create_opencti_entity", type="Malware", name="Emotet",
        attributes={"x_opencti_cf_origin": "feed-1"},
    )
    assert rec.inputs[-1]["customFieldValues"] == [
        {"field_name": "x_opencti_cf_source", "value": ["feed-1"]}
    ]


@pytest.mark.parametrize("field", ["update", "stix_id", "x_opencti_stix_ids", "upsertOperations"])
async def test_a_field_choosing_the_object_written_is_refused(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    """``update: true`` would force-merge every matching entity: never through ``attributes``."""
    executor, rec, _ = _build(monkeypatch)
    result = await _call(
        executor,
        "create_opencti_entity",
        type="Malware",
        name="Emotet",
        attributes={field: True if field == "update" else ["malware--1"]},
    )
    assert result.content.startswith(f"Error: {field} cannot be given in 'attributes'")
    assert rec.calls == []


async def test_a_name_that_is_no_field_of_the_type_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenCTI drops an unknown custom field and still creates: it is refused before."""
    executor, rec, _ = _build(
        monkeypatch,
        [_fields("Malware", _field("x_opencti_cf_source")), _definitions(_definition("x_opencti_cf_source"))],
    )
    result = await _call(
        executor,
        "create_opencti_entity",
        type="Malware",
        name="Emotet",
        attributes={"x_opencti_cf_unknown": "x", "clientMutationId": "y"},
    )
    assert result.content.startswith(
        "Error: not fields of Malware on this OpenCTI: clientMutationId, x_opencti_cf_unknown."
    )
    assert _mutations(rec) == []


@pytest.mark.parametrize(
    ("definition", "value", "problem"),
    [
        (_definition("x_opencti_cf_count", "integer"), "x", "x_opencti_cf_count takes a 32-bit integer, got 'x'"),
        (_definition("x_opencti_cf_count", "integer"), True, "x_opencti_cf_count takes a 32-bit integer, got True"),
        (_definition("x_opencti_cf_count", "integer"), 2.5, "x_opencti_cf_count takes a 32-bit integer, got 2.5"),
        (_definition("x_opencti_cf_count", "integer"), 2**31, f"x_opencti_cf_count takes a 32-bit integer, got {2**31}"),
        (_definition("x_opencti_cf_count", "integer", min_value=1), 0, "x_opencti_cf_count takes a value of at least 1, got 0"),
        (_definition("x_opencti_cf_flag", "boolean"), "yes", "x_opencti_cf_flag takes true or false, got 'yes'"),
        (_definition("x_opencti_cf_seen", "date"), "2026-11-25", "x_opencti_cf_seen takes an RFC 3339 date-time such as 2026-11-25T00:00:00Z, got '2026-11-25'"),
        (_definition("x_opencti_cf_tier", "select", select_options=["gold"]), "silver", "x_opencti_cf_tier takes one of gold, got 'silver'"),
        (_definition("x_opencti_cf_ref"), ["a", "b"], "x_opencti_cf_ref takes a single value"),
    ],
)
async def test_a_custom_value_that_does_not_fit_is_refused(
    monkeypatch: pytest.MonkeyPatch, definition: dict[str, Any], value: Any, problem: str
) -> None:
    """OpenCTI only logs a wrongly typed custom value and still creates: it is refused before."""
    name = definition["name"]
    executor, rec, _ = _build(monkeypatch, [_fields("Malware", _field(name)), _definitions(definition)])
    result = await _call(
        executor, "create_opencti_entity", type="Malware", name="Emotet", attributes={name: value}
    )
    assert result.content == f"Error: {problem}. Nothing was created."
    assert _mutations(rec) == []


async def test_a_number_or_boolean_written_as_text_is_read_as_one(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, rec, _ = _build(
        monkeypatch,
        [
            _fields("Malware", _field("x_opencti_cf_count", "numeric"), _field("x_opencti_cf_flag", "boolean")),
            _definitions(_definition("x_opencti_cf_count", "integer"), _definition("x_opencti_cf_flag", "boolean")),
        ],
    )
    await _call(
        executor, "create_opencti_entity", type="Malware", name="Emotet",
        attributes={"x_opencti_cf_count": "3", "x_opencti_cf_flag": "True"},
    )
    assert rec.inputs[-1]["customFieldValues"] == [
        {"field_name": "x_opencti_cf_count", "value": [3]},
        {"field_name": "x_opencti_cf_flag", "value": [True]},
    ]


async def test_an_older_opencti_checks_against_the_list_of_every_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused = (
        "GraphQL error in 'SchemaAttributeTypes': Unknown argument \"entityType\" on field "
        '"Query.schemaAttributes". [GRAPHQL_VALIDATION_FAILED]'
    )
    every_type = {
        "schemaAttributes": [
            {"type": "Report", "attributes": [{"name": "x_opencti_cf_other", "type": "string"}]},
            # Its schema-level mandatory flag is not this instance's: no preflight on it.
            {"type": "Malware", "attributes": [
                {"name": "x_opencti_cf_source", "type": "string"},
                {"name": "description", "type": "string", "mandatory": True},
            ]},
        ]
    }
    executor, rec, _ = _build(
        monkeypatch, [({}, refused), (every_type, None), _definitions(_definition("x_opencti_cf_source"))]
    )
    await _call(
        executor,
        "create_opencti_entity",
        type="Malware",
        name="Emotet",
        attributes={"x_opencti_cf_source": "feed-1"},
    )
    assert "schemaAttributes {" in rec.calls[1][0]
    assert rec.inputs[-1]["customFieldValues"] == [
        {"field_name": "x_opencti_cf_source", "value": ["feed-1"]}
    ]


async def test_a_field_already_set_is_refused_and_nothing_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, rec, _ = _build(monkeypatch)
    result = await _call(
        executor,
        "create_opencti_entity",
        type="Malware",
        name="Emotet",
        attributes={"name": "Other", "x_opencti_cf_source": "feed-1"},
    )
    assert result.content.startswith("Error: name already set")
    assert rec.calls == []


async def test_attributes_must_be_an_object(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, rec, _ = _build(monkeypatch)
    result = await _call(
        executor, "create_opencti_entity", type="Malware", name="Emotet", attributes=["x"]
    )
    assert result.content.startswith("Error: 'attributes' must be an object")
    assert rec.calls == []


# ── Preflight: a mandatory field missing is named before OpenCTI refuses ──


async def test_a_missing_mandatory_field_is_named_before_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, rec, _ = _build(
        monkeypatch,
        [
            _fields(
                "Malware",
                _field("name", mandatory=True),
                _field("x_opencti_cf_case_reference", mandatory=True, description="The case number, e.g. CR-42."),
                _field("description", mandatory=True),
                _field("objectMarking", "ref", mandatory=True),
                _field("malware_types", mandatory=True, defaults=["backdoor"]),
            ),
            ({"me": {"capabilities": [{"name": "KNOWLEDGE_KNUPDATE"}]}}, None),
        ],
    )
    result = await _call(executor, "create_opencti_entity", type="Malware", name="Emotet")
    text = result.content
    assert text.startswith("Error: this OpenCTI makes these fields of Malware mandatory")
    assert "- x_opencti_cf_case_reference (string, custom field): The case number, e.g. CR-42." in text
    assert "- description (string)" in text
    # Filled by OpenCTI at creation (default markings, a default value): not missing.
    assert "objectMarking" not in text
    assert "malware_types" not in text
    assert "Nothing was created." in text
    assert _mutations(rec) == []


@pytest.mark.parametrize("capability", ["KNOWLEDGE_KNUPDATE_KNBYPASSFIELDS", "BYPASS"])
async def test_a_user_who_may_skip_mandatory_fields_is_not_stopped(
    monkeypatch: pytest.MonkeyPatch, capability: str
) -> None:
    executor, rec, _ = _build(
        monkeypatch,
        [
            _fields("Malware", _field("description", mandatory=True)),
            ({"me": {"capabilities": [{"name": capability}]}}, None),
        ],
    )
    await _call(executor, "create_opencti_entity", type="Malware", name="Emotet")
    assert len(_mutations(rec)) == 1


async def test_given_mandatory_fields_pass_the_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, rec, _ = _build(
        monkeypatch,
        [
            _fields("Malware", _field("name", mandatory=True), _field("x_opencti_cf_case", mandatory=True)),
            _definitions(_definition("x_opencti_cf_case")),
        ],
    )
    await _call(
        executor, "create_opencti_entity", type="Malware", name="Emotet", attributes={"x_opencti_cf_case": "CR-1"}
    )
    assert len(_mutations(rec)) == 1
    assert not any("capabilities" in q for q, _ in rec.calls)


async def test_a_create_is_sent_when_the_fields_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, rec, _ = _build(monkeypatch, [({}, "GraphQL error in 'SchemaAttributeTypes': boom")])
    await _call(executor, "create_opencti_entity", type="Malware", name="Emotet")
    assert len(_mutations(rec)) == 1


async def test_the_fields_of_a_type_are_read_once_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, rec, _ = _build(monkeypatch, [_fields("Malware", _field("name", mandatory=True))])
    await _call(executor, "create_opencti_entity", type="Malware", name="Emotet")
    await _call(executor, "create_opencti_entity", type="Malware", name="TrickBot")
    assert sum("SchemaAttributeTypes" in q for q, _ in rec.calls) == 1
    assert len(_mutations(rec)) == 2


def test_the_create_tools_but_the_observable_declare_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, definitions = _build(monkeypatch)
    by_name = {d.name: d for d in definitions}
    for name in (
        "create_opencti_entity",
        "create_opencti_container",
        "create_opencti_relationship",
        "create_opencti_indicator",
        "create_opencti_note",
    ):
        assert by_name[name].input_schema["properties"]["attributes"]["type"] == "object"
        assert "call get_opencti_schema_attributes first" in by_name[name].description
    for name in ("update_opencti_field", "update_opencti_relationship_field"):
        assert "x_opencti_cf_" in by_name[name].description
    # An observable's input is nested per type: a flat field would land in the wrong place.
    assert "attributes" not in by_name["create_opencti_observable"].input_schema["properties"]
    assert by_name["get_opencti_schema_attributes"].input_schema["required"] == ["entity_type"]


_REPORT_FIELDS = {
    "schemaAttributes": [
        {
            "type": "Report",
            "attributes": [
                {
                    "name": "description",
                    "type": "string",
                    "label": "Description",
                    "description": "What the report covers, in Markdown.",
                    "mandatory": False,
                    "multiple": False,
                    "defaultValues": None,
                },
                {
                    "name": "x_opencti_cf_case_reference",
                    "type": "string",
                    "label": "Case reference",
                    "description": "The internal case number, e.g. CR-42.",
                    "mandatory": True,
                    "multiple": False,
                    "defaultValues": [{"id": "CR-0", "name": "CR-0"}],
                },
                {
                    "name": "createdBy",
                    "type": "ref",
                    "label": "Author",
                    "description": "The identity authoring the report.",
                    "mandatory": True,
                    "multiple": False,
                    "defaultValues": [{"id": "identity--1", "name": "ACME CERT"}],
                },
            ],
        }
    ]
}


async def test_schema_attributes_lists_mandatory_fields_first(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, rec, _ = _build(monkeypatch, [(_REPORT_FIELDS, None)])
    result = await _call(executor, "get_opencti_schema_attributes", entity_type="Report")
    query, variables = rec.calls[0]
    assert "schemaAttributes(entityType: $t)" in query
    assert variables == {"t": "Report"}
    text = result.content
    assert "Fields of Report on this OpenCTI: 3, 2 mandatory at creation." in text
    assert text.index("Mandatory:") < text.index("x_opencti_cf_case_reference") < text.index("Optional:")
    assert text.index("Optional:") < text.index("- description")
    assert "(string, custom field) — Case reference: The internal case number, e.g. CR-42. Default: CR-0." in text
    assert "Default: ACME CERT (id identity--1)." in text
    assert result.structured["entity_type"] == "Report"
    assert [a["name"] for a in result.structured["attributes"]] == [
        "x_opencti_cf_case_reference",
        "createdBy",
        "description",
    ]


async def test_mandatory_only_leaves_the_optional_fields_out(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, _, _ = _build(monkeypatch, [(_REPORT_FIELDS, None)])
    result = await _call(
        executor, "get_opencti_schema_attributes", entity_type="Report", mandatory_only=True
    )
    assert "Optional:" not in result.content
    assert "- description" not in result.content


async def test_an_older_opencti_answers_from_the_entity_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    refused = (
        "GraphQL error in 'SchemaAttributes': Unknown argument \"entityType\" on field "
        '"Query.schemaAttributes". [GRAPHQL_VALIDATION_FAILED]'
    )
    setting = {
        "entitySettingByType": {
            "target_type": "Report",
            "mandatoryAttributes": ["name", "published", "x_opencti_cf_case_reference"],
            "defaultValuesAttributes": [
                {"name": "report_types", "defaultValues": [{"id": "threat-report", "name": "threat-report"}]}
            ],
        }
    }
    executor, rec, _ = _build(monkeypatch, [({}, refused), (setting, None)])
    result = await _call(executor, "get_opencti_schema_attributes", entity_type="Report")
    assert "entitySettingByType(targetType: $t)" in rec.calls[1][0]
    text = result.content
    assert "predates the field list of schemaAttributes (7.261124.0)" in text
    assert "Fields of Report on this OpenCTI: 4, 3 mandatory at creation." in text
    assert "- x_opencti_cf_case_reference (custom field)" in text
    assert "- report_types Default: threat-report." in text


async def test_an_unknown_type_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # OpenCTI names the argument in the error data: it is no sign of an older version.
    unknown = (
        "GraphQL error in 'SchemaAttributes': Unknown entity type [FUNCTIONAL_ERROR] "
        '{"entityType": "Repport"}'
    )
    executor, rec, _ = _build(monkeypatch, [({}, unknown)])
    result = await _call(executor, "get_opencti_schema_attributes", entity_type="Repport")
    assert result.content == unknown
    assert len(rec.calls) == 1


# ── Custom fields read and updated ──


async def test_an_entity_is_read_with_its_custom_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    entity = {
        "id": "m1",
        "entity_type": "Malware",
        "standard_id": "malware--1",
        "name": "Emotet",
        "customFieldValues": [
            {"field_name": "x_opencti_cf_case_reference", "string_value": "CR-42"},
            {"field_name": "x_opencti_cf_regions", "select_values": ["EU", "NA"]},
            {"field_name": "x_opencti_cf_count", "int_value": 0},
        ],
    }
    executor, rec, _ = _build(
        monkeypatch,
        [({"stixCoreObject": entity}, None), _definitions(_definition("x_opencti_cf_case_reference"))],
    )
    result = await _call(executor, "get_opencti_entity", entity_id="m1")
    assert "customFieldValues {" in rec.calls[0][0]
    assert "Custom fields:" in result.content
    assert "  - x_opencti_cf_case_reference (Case Reference): CR-42" in result.content
    assert "  - x_opencti_cf_regions: EU, NA" in result.content
    assert "  - x_opencti_cf_count: 0" in result.content


async def test_an_opencti_without_custom_fields_is_read_again_without_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused = "GraphQL error in 'GetEntity': Cannot query field \"customFieldValues\" on type \"StixDomainObject\"."
    entity = {"id": "m1", "entity_type": "Malware", "standard_id": "malware--1", "name": "Emotet"}
    executor, rec, _ = _build(monkeypatch, [({}, refused), ({"stixCoreObject": entity}, None)])
    result = await _call(executor, "get_opencti_entity", entity_id="m1")
    assert "customFieldValues" not in rec.calls[1][0]
    assert "Emotet" in result.content
    assert "Custom fields:" not in result.content


@pytest.mark.parametrize(
    ("tool", "id_arg", "root"),
    [
        ("update_opencti_field", "entity_id", "stixCoreObject"),
        ("update_opencti_relationship_field", "relationship_id", "stixCoreRelationship"),
    ],
)
async def test_a_custom_field_is_updated_in_the_stored_format(
    monkeypatch: pytest.MonkeyPatch, tool: str, id_arg: str, root: str
) -> None:
    executor, rec, _ = _build(
        monkeypatch,
        [({root: {"entity_type": "Malware"}}, None), _definitions(_definition("x_opencti_cf_count", "integer"))],
    )
    result = await _call(executor, tool, **{id_arg: "m1", "key": "x_opencti_cf_count", "value": "7"})
    assert len(_mutations(rec)) == 1, result.content
    assert rec.calls[-1][1]["input"] == [
        {"key": "custom_field_values", "operation": "add", "value": [{"field_name": "x_opencti_cf_count", "int_value": 7}]}
    ]


async def test_an_empty_value_clears_a_custom_field(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, rec, _ = _build(
        monkeypatch,
        [
            ({"stixCoreObject": {"entity_type": "Malware"}}, None),
            _definitions(_definition("x_opencti_cf_tags", "multi_select", select_options=["a", "b"])),
        ],
    )
    await _call(executor, "update_opencti_field", entity_id="m1", key="x_opencti_cf_tags", value=[])
    assert rec.calls[-1][1]["input"] == [
        {"key": "custom_field_values", "operation": "remove", "value": [{"field_name": "x_opencti_cf_tags"}]}
    ]


async def test_a_multi_select_update_sends_its_options(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, rec, _ = _build(
        monkeypatch,
        [
            ({"stixCoreObject": {"entity_type": "Malware"}}, None),
            _definitions(_definition("x_opencti_cf_tags", "multi_select", select_options=["a", "b"])),
        ],
    )
    await _call(executor, "update_opencti_field", entity_id="m1", key="x_opencti_cf_tags", value=["a", "b"])
    assert rec.calls[-1][1]["input"][0]["value"] == [{"field_name": "x_opencti_cf_tags", "select_values": ["a", "b"]}]


async def test_an_unknown_or_unfit_custom_update_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, rec, _ = _build(
        monkeypatch,
        [
            ({"stixCoreObject": {"entity_type": "Malware"}}, None),
            _definitions(_definition("x_opencti_cf_count", "integer")),
            # The object's type is read on each update; its definitions once per run.
            ({"stixCoreObject": {"entity_type": "Malware"}}, None),
        ],
    )
    unknown = await _call(executor, "update_opencti_field", entity_id="m1", key="x_opencti_cf_nope", value="1")
    assert unknown.content.startswith("Error: x_opencti_cf_nope is no custom field of Malware")
    unfit = await _call(executor, "update_opencti_field", entity_id="m1", key="x_opencti_cf_count", value="many")
    assert unfit.content == "Error: x_opencti_cf_count takes a 32-bit integer, got 'many'. Nothing was changed."
    assert _mutations(rec) == []


async def test_a_native_field_update_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, rec, _ = _build(monkeypatch)
    await _call(executor, "update_opencti_field", entity_id="m1", key="description", value="new")
    assert len(rec.calls) == 1
    assert rec.calls[0][1]["input"] == [{"key": "description", "value": ["new"]}]


# ── The NLQ filter agent gets the custom-field filter keys ──


async def test_the_nlq_query_comes_with_the_custom_field_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    definitions = {
        "customFieldDefinitions": {
            "edges": [
                {"node": {
                    "name": "x_opencti_cf_tier", "label": "Tier", "field_type": "select", "multiple": False,
                    "entity_types": ["Report", "Malware"], "select_options": ["gold", "silver"],
                }},
                {"node": {
                    "name": "x_opencti_cf_case", "label": "Case", "field_type": "string", "multiple": False,
                    "entity_types": ["Report"], "select_options": None,
                }},
            ]
        }
    }
    executor, _, _ = _build(monkeypatch, [(definitions, None)])
    result = await _call(executor, "get_opencti_nlq_context", query="gold tier reports")
    lines = result.content.splitlines()
    assert lines[0] == "gold tier reports"
    assert "- x_opencti_cf_tier (Tier): select; values: gold, silver; on Report, Malware" in lines
    assert "- x_opencti_cf_case (Case): string; on Report" in lines


@pytest.mark.parametrize(
    "answer",
    [({}, "GraphQL error in 'CustomFieldFilterKeys': Cannot query field"), ({"customFieldDefinitions": {"edges": []}}, None)],
)
async def test_the_nlq_query_is_unchanged_without_custom_fields(
    monkeypatch: pytest.MonkeyPatch, answer: tuple[dict[str, Any], str | None]
) -> None:
    executor, _, _ = _build(monkeypatch, [answer])
    result = await _call(executor, "get_opencti_nlq_context", query="reports about Emotet")
    assert result.content == "reports about Emotet"


def test_the_nlq_agent_prepares_its_query_with_the_custom_field_keys() -> None:
    from app.agents.builtin_agents import BUILTIN_AGENTS

    nlq = next(a for a in BUILTIN_AGENTS if a["slug"] == "opencti-nlq")
    assert nlq["prepare_tool_ref"] == {
        "source": "integration",
        "provider": "opencti",
        "tool_name": "get_opencti_nlq_context",
    }
