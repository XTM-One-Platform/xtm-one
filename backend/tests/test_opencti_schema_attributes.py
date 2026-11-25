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


def _fields(entity_type: str, *fields: tuple[str, str, bool]) -> tuple[dict[str, Any], None]:
    """The answer of the field read a create makes before taking ``attributes``."""
    return (
        {
            "schemaAttributes": [
                {
                    "type": entity_type,
                    "attributes": [
                        {"name": name, "type": kind, "multiple": multiple}
                        for name, kind, multiple in fields
                    ],
                }
            ]
        },
        None,
    )


async def test_a_report_takes_a_custom_field_and_a_mandatory_field(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, rec, _ = _build(
        monkeypatch,
        [
            _fields(
                "Report",
                ("x_opencti_reliability", "string", False),
                ("x_opencti_cf_case_reference", "string", False),
                ("x_opencti_cf_regions", "string", True),
                ("x_opencti_cf_score", "numeric", False),
                ("x_opencti_cf_empty", "string", False),
            )
        ],
    )
    await _call(
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
    executor, rec, _ = _build(monkeypatch, [_fields(entity_type, ("x_opencti_cf_source", "string", False))])
    await _call(executor, tool, **arguments, attributes={"x_opencti_cf_source": "feed-1"})
    assert rec.calls[0][1] == {"t": entity_type}
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
    executor, rec, _ = _build(monkeypatch, [_fields("Malware", ("x_opencti_cf_source", "string", False))])
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
    assert len(rec.calls) == 1  # the field read, no mutation


@pytest.mark.parametrize(
    ("field", "value", "problem"),
    [
        (("x_opencti_cf_count", "numeric", False), "3", "x_opencti_cf_count takes numeric values, got '3'"),
        (("x_opencti_cf_count", "numeric", False), True, "x_opencti_cf_count takes numeric values, got True"),
        (("x_opencti_cf_flag", "boolean", False), "yes", "x_opencti_cf_flag takes boolean values, got 'yes'"),
        (("x_opencti_cf_ref", "string", False), ["a", "b"], "x_opencti_cf_ref takes a single value"),
    ],
)
async def test_a_custom_value_of_the_wrong_type_is_refused(
    monkeypatch: pytest.MonkeyPatch, field: tuple[str, str, bool], value: Any, problem: str
) -> None:
    """OpenCTI only logs a wrongly typed custom value and still creates: it is refused before."""
    executor, rec, _ = _build(monkeypatch, [_fields("Malware", field)])
    result = await _call(
        executor, "create_opencti_entity", type="Malware", name="Emotet", attributes={field[0]: value}
    )
    assert result.content == f"Error: {problem}. Nothing was created."
    assert len(rec.calls) == 1


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
            {"type": "Malware", "attributes": [{"name": "x_opencti_cf_source", "type": "string"}]},
        ]
    }
    executor, rec, _ = _build(monkeypatch, [({}, refused), (every_type, None)])
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
        # Nothing makes the agent read the fields before a create: the description says to.
        assert "call get_opencti_schema_attributes first" in by_name[name].description
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
