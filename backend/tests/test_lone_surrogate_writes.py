"""A lone UTF-16 surrogate is refused where it enters an assignment or a flow.

A JSON ``"\\ud800"`` escape decodes to a lone surrogate; stored in a ``json``
column it is an escape every later ``::jsonb`` read refuses, and no response
can encode it: ``POST /assignments`` answered 500 after committing a row every
read of the agent's assignments then failed on. The engine now stores one as
U+FFFD and reads a stored one the same way, which changes what the
caller sent; every write of an assignment or flow definition refuses one
instead, before anything is written, naming where it is - the REST schemas and
their 422, agent templates, agent import, the agent-builder tools and flow
graphs - and a value that was accepted before (a surrogate pair is a
character) is accepted as it is.

The PostgreSQL half (the refusal is exactly what the database cannot read
back) is ``test_lone_surrogate_writes_postgres.py``.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.v1.schemas.agent import CreateTemplateRequest, UpdateTemplateRequest
from app.api.v1.schemas.assignment import CreateAssignmentRequest, UpdateAssignmentRequest
from app.api.v1.schemas.column_limits import text_or_json_columns
from app.core import database
from app.core.lone_surrogates import escape_surrogates, find_lone_surrogate, lone_surrogate_error
from app.models.agent_template import AgentTemplate
from app.models.assignment import Assignment
from app.services.builtin_tools import build_agent_builder_tool
from app.tools.base import ToolCall

EMOJI = "\U0001f600"  # what the JSON escape pair "\ud83d\ude00" decodes to


def _refusal(where: str, code_point: str = "D800") -> str:
    return (
        f"{where} holds a lone UTF-16 surrogate (U+{code_point}), half of a character "
        "that cannot be stored on its own. Send the whole character or remove it."
    )


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "path", "code_point", "in_key"),
    [
        pytest.param("a\ud800", "", 0xD800, False, id="lone-high"),
        pytest.param("\udc00b", "", 0xDC00, False, id="lone-low"),
        pytest.param("\ude00\ud83d", "", 0xDE00, False, id="reversed-pair"),
        pytest.param("\ud83d\ud83d\ude00", "", 0xD83D, False, id="high-before-a-pair"),
        pytest.param({"filter": {"subject": "x\ud800"}}, ".filter.subject", 0xD800, False, id="nested"),
        pytest.param({"events": ["push", "\udfff"]}, ".events[1]", 0xDFFF, False, id="list-item"),
        pytest.param({"filter": {"\ud800": 1}}, '.filter["\\ud800"]', 0xD800, True, id="key"),
        pytest.param({"a b": ["\ud800"]}, '["a b"][0]', 0xD800, False, id="non-identifier-key"),
        pytest.param(("ok", {"x": "\ud800"}), "[1].x", 0xD800, False, id="tuple"),
    ],
)
def test_a_lone_surrogate_is_found_where_it_is(value, path, code_point, in_key):
    found = find_lone_surrogate(value)

    assert found is not None
    assert (found.path, found.code_point, found.in_key) == (path, code_point, in_key)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("plain", id="ascii"),
        pytest.param("caf\u00e9 \u4e2d\u6587 " + EMOJI, id="non-ascii-and-astral"),
        pytest.param(json.loads('"\\ud83d\\ude00"'), id="json-escape-pair"),
        # A high surrogate directly followed by a low one is a pair PostgreSQL
        # decodes: only malformed UTF-8 bytes produce it, and it is kept.
        pytest.param("\ud83d\ude00", id="adjacent-pair"),
        pytest.param("\\ud800", id="escape-text"),
        pytest.param({"k": [1, 2.5, True, None, {"n": EMOJI}]}, id="nested-clean"),
        pytest.param(None, id="none"),
        pytest.param(12, id="number"),
    ],
)
def test_a_value_without_one_is_left_alone(value):
    assert find_lone_surrogate(value) is None
    assert lone_surrogate_error("trigger_config", value) is None


def test_the_first_one_in_document_order_is_reported():
    value = {"a": {"b": "fine", "c": "\ud800"}, "\udc01": "x", "d": ["\ud802"]}

    assert find_lone_surrogate(value).path == ".a.c"
    assert find_lone_surrogate({"x": "ok", "\udc01": "\ud800"}).in_key is True


def test_a_deeply_nested_value_costs_no_recursion():
    value: object = "\ud800"
    for _ in range(20_000):
        value = [value]

    assert find_lone_surrogate(value).path == "[0]" * 20_000


def test_the_refusal_names_the_place_and_never_carries_the_surrogate():
    message = lone_surrogate_error("trigger_config", {"filter": {"\ud800x": "v"}})

    assert message == _refusal('a key of trigger_config.filter["\\ud800x"]')
    assert message.isascii()
    assert lone_surrogate_error("", {"path": "\ud800"}) == _refusal("path")
    assert lone_surrogate_error("", "\ud800") == _refusal("the value")
    assert lone_surrogate_error("prompt", "\udc00") == _refusal("prompt", "DC00")


def test_escaping_makes_a_value_encodable_and_keeps_a_clean_one_as_is():
    clean = {"a": [EMOJI, "caf\u00e9", 1], "b": {"c": None}}
    assert escape_surrogates(clean) is clean

    dirty = [{"loc": ("body", "\ud800k"), "input": {"path": "x\ud800", "ok": EMOJI}}]
    escaped = escape_surrogates(dirty)

    assert escaped == [{"loc": ("body", "\\ud800k"), "input": {"path": "x\\ud800", "ok": EMOJI}}]
    json.dumps(escaped, ensure_ascii=False).encode("utf-8")
    assert escape_surrogates("\ud83d\ude00") == "\\ud83d\\ude00"


def test_the_checked_columns_are_every_text_and_json_column():
    assert set(text_or_json_columns(Assignment)) == {
        column.key
        for column in Assignment.__table__.columns
        if column.type.__class__.__name__ in {"String", "Text", "JSON"}
    }
    assert "trigger_config" in text_or_json_columns(Assignment)
    assert "agent_id" not in text_or_json_columns(Assignment)
    assert "default_assignments" in text_or_json_columns(AgentTemplate)


def test_every_write_path_asks_the_same_rule():
    from app.api.v1 import agent_import
    from app.api.v1.schemas import column_limits
    from app.core import lone_surrogates
    from app.services import flow_graph
    from app.services.builtin_tools import agent_builder

    for module in (column_limits, agent_import, flow_graph, agent_builder):
        assert module.lone_surrogate_error is lone_surrogates.lone_surrogate_error


def test_the_refusal_and_the_engine_repair_read_one_definition():
    """What is refused here is exactly what the engine would store as U+FFFD."""
    from pathlib import Path

    import app
    from app.core import lone_surrogates, utils

    assert lone_surrogates.LONE_SURROGATE_RE is utils.LONE_SURROGATE_RE
    pattern = r"[\ud800-\udbff](?![\udc00-\udfff])"
    holders = sorted(
        str(path.relative_to(Path(app.__file__).parent.parent))
        for path in Path(app.__file__).parent.rglob("*.py")
        if pattern in path.read_text(encoding="utf-8")
    )
    assert holders == [str(Path("app/core/utils.py"))], (
        f"{holders}: import app.core.utils.LONE_SURROGATE_RE instead of a copy"
    )
    for value in ("\ud800", "a\udc00b", "\ude00\ud83d", "\ud83d\ude00", EMOJI, "plain"):
        assert (find_lone_surrogate(value) is None) is (utils.sanitize_json_strings(value) == value)


def test_a_stored_row_is_read_repaired_so_a_copy_cannot_carry_one():
    """Why duplicate, catalog clone, create-from-template and a flow duplicate or
    restore need no check of their own: everything they copy is read through
    the engine's JSON decoder, which reads a lone surrogate stored before the
    fix as U+FFFD."""
    decoded = database.json_deserializer(
        '{"path": "\\ud800", "k": {"\\udc00": ["x\\ud800"]}, "e": "\\ud83d\\ude00", "s": {"\\ud83d": "\\ude00"}}'
    )

    assert find_lone_surrogate(decoded) is None
    # A pair split across a key and its value is two lone halves, one in each string.
    assert decoded == {"path": "\ufffd", "k": {"\ufffd": ["x\ufffd"]}, "e": EMOJI, "s": {"\ufffd": "\ufffd"}}


# ---------------------------------------------------------------------------
# REST schemas
# ---------------------------------------------------------------------------

#: A lone-surrogate value for every assignment field written to a text or JSON
#: column, and the place its refusal names.
_ASSIGNMENT_SAMPLES: dict[str, tuple[object, str]] = {
    "name": ("Digest \ud800", "name"),
    "description": ("A \ud800 note", "description"),
    "trigger_type": ("manual\ud800", "trigger_type"),
    "trigger_config": ({"path": "hook", "filter": {"subject": "x\ud800"}}, "trigger_config.filter.subject"),
    "activation_prompt": ("Run?\ud800", "activation_prompt"),
    "activation_check": ({"tool": "list_issues", "arguments": {"query": "x\ud800"}}, "activation_check.arguments.query"),
    "prompt": ("Summarise\ud800", "prompt"),
    "delay": ("\ud80010m", "delay"),
    "tool_sources": (["github", "\ud800"], "tool_sources[1]"),
    "integration_bindings": ({"github": ["\ud800"]}, "integration_bindings.github[0]"),
    "integration_allowed_tools": ({"github": ["list_\ud800"]}, "integration_allowed_tools.github[0]"),
    "builtin_allowed_tools": ({"agent_builder": ["list_\ud800"]}, "builtin_allowed_tools.agent_builder[0]"),
    "integration_element_bindings": (
        {"github:repository": [{"id": "o/\ud800"}]},
        'integration_element_bindings["github:repository"][0].id',
    ),
    "integration_element_enforcement": (["\ud800"], "integration_element_enforcement[0]"),
    "channel_config": ({"slack": {"channel": "\ud800"}}, "channel_config.slack.channel"),
    "read_knowledge_base_ids": (["\ud800"], "read_knowledge_base_ids[0]"),
    "write_knowledge_base_ids": (["\ud800"], "write_knowledge_base_ids[0]"),
    "runner_ids": (["\ud800"], "runner_ids[0]"),
    "ai_label_prompt": ("\ud800", "ai_label_prompt"),
    "human_in_the_loop_prompt": ("\ud800", "human_in_the_loop_prompt"),
    "manual_trigger_parameters": ([{"key": "topic", "label": "\ud800"}], "manual_trigger_parameters[0].label"),
    "wait_policy": ({"unbounded_with_until": "x\ud800"}, "wait_policy.unbounded_with_until"),
    "organization_context_mode": ("off\ud800", "organization_context_mode"),
    "organization_context_categories": (["software", "\ud800"], "organization_context_categories[1]"),
}


def _payload(**overrides: object) -> dict[str, object]:
    return {
        "agent_id": "00000000-0000-0000-0000-000000000001",
        "name": "Digest",
        "trigger_type": "manual",
        "prompt": "Go.",
        **overrides,
    }


def test_every_stored_request_field_has_a_sample():
    """A text or JSON field added to the requests is a place to check."""
    for schema in (CreateAssignmentRequest, UpdateAssignmentRequest):
        assert set(text_or_json_columns(Assignment)) & set(schema.model_fields) == set(_ASSIGNMENT_SAMPLES)


@pytest.mark.parametrize("field", sorted(_ASSIGNMENT_SAMPLES))
@pytest.mark.parametrize(
    "build",
    [
        lambda field, value: CreateAssignmentRequest(**_payload(**{field: value})),
        lambda field, value: UpdateAssignmentRequest(**{field: value}),
    ],
    ids=["create", "update"],
)
def test_the_rest_schemas_refuse_a_lone_surrogate_in_every_stored_field(build, field):
    value, where = _ASSIGNMENT_SAMPLES[field]

    with pytest.raises(ValidationError) as exc_info:
        build(field, value)

    [error] = exc_info.value.errors()
    assert (error["type"], error["loc"]) == ("value_error", (field,))
    assert error["msg"] == f"Value error, {_refusal(where)}"


def test_an_id_the_route_parses_is_left_to_its_own_answer():
    """A malformed agent id is the route's 404, never this refusal."""
    request = CreateAssignmentRequest(**_payload(agent_id="\ud800", run_as_user_id="\ud800"))

    assert request.agent_id == "\ud800"


def test_the_rest_schemas_keep_a_valid_value_byte_for_byte():
    body = (
        '{"agent_id": "00000000-0000-0000-0000-000000000001", "name": "Digest \\ud83d\\ude00",'
        ' "trigger_type": "webhook", "prompt": "Caf\\u00e9 \\u4e2d",'
        ' "trigger_config": {"path": "hook", "filter": {"subject": "\\ud83d\\ude00 x", "\\u00e9": ["\\\\ud800"]}},'
        ' "manual_trigger_parameters": [{"key": "k", "label": "\\ud83d\\ude00"}]}'
    )
    decoded = json.loads(body)

    for request in (
        CreateAssignmentRequest.model_validate_json(body),
        UpdateAssignmentRequest.model_validate_json(body),
    ):
        for field in ("name", "prompt", "trigger_config", "manual_trigger_parameters"):
            assert getattr(request, field) == decoded[field]
    assert decoded["trigger_config"]["filter"]["subject"] == f"{EMOJI} x"
    assert decoded["trigger_config"]["filter"]["\u00e9"] == ["\\ud800"]


def _validation_app() -> FastAPI:
    from app.main import _log_request_validation_error

    api = FastAPI()
    api.add_exception_handler(RequestValidationError, _log_request_validation_error)

    @api.post("/assignments")
    async def _create(body: CreateAssignmentRequest) -> dict[str, bool]:
        return {"ok": True}

    return api


def test_the_refusal_is_a_422_naming_the_field_not_a_500():
    """The default 422 echoes the value, which UTF-8 cannot encode: it was a 500."""
    client = TestClient(_validation_app(), raise_server_exceptions=False)
    body = (
        '{"agent_id": "00000000-0000-0000-0000-000000000001", "name": "Digest",'
        ' "trigger_type": "webhook", "prompt": "Go.", "trigger_config": {"path": "\\ud800"}}'
    )

    response = client.post("/assignments", content=body, headers={"content-type": "application/json"})

    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert error["loc"] == ["body", "trigger_config"]
    assert error["msg"] == f"Value error, {_refusal('trigger_config.path')}"
    assert error["input"] == {"path": "\\ud800"}


def test_any_422_echoing_a_lone_surrogate_is_sent():
    client = TestClient(_validation_app(), raise_server_exceptions=False)
    body = (
        '{"agent_id": "00000000-0000-0000-0000-000000000001", "name": "Digest",'
        ' "trigger_type": "manual", "prompt": "Go.", "max_retries": "\\ud800"}'
    )

    response = client.post("/assignments", content=body, headers={"content-type": "application/json"})

    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert (error["type"], error["loc"], error["input"]) == ("string_unicode", ["body", "max_retries"], "\\ud800")


def test_a_valid_request_still_reaches_the_route():
    client = TestClient(_validation_app(), raise_server_exceptions=False)
    body = (
        '{"agent_id": "00000000-0000-0000-0000-000000000001", "name": "Digest \\ud83d\\ude00",'
        ' "trigger_type": "webhook", "prompt": "Go.", "trigger_config": {"path": "\\ud83d\\ude00"}}'
    )

    response = client.post("/assignments", content=body, headers={"content-type": "application/json"})

    assert (response.status_code, response.json()) == (200, {"ok": True})


# ---------------------------------------------------------------------------
# Agent templates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        lambda entries: CreateTemplateRequest(slug="digest", name="Digest", default_assignments=entries),
        lambda entries: UpdateTemplateRequest(default_assignments=entries),
    ],
    ids=["create", "update"],
)
def test_a_template_refuses_a_default_assignment_holding_one(build):
    entries = [{"name": "Fine"}, {"name": "Hook", "trigger_config": {"path": "\ud800"}}]

    with pytest.raises(ValidationError) as exc_info:
        build(entries)

    [error] = exc_info.value.errors()
    assert error["loc"] == ("default_assignments",)
    assert error["msg"] == f"Value error, {_refusal('default_assignments[1].trigger_config.path')}"


def test_a_template_refuses_one_in_its_other_stored_fields():
    with pytest.raises(ValidationError) as exc_info:
        CreateTemplateRequest(
            slug="digest", name="Digest", must_be_defined=[{"kind": "integration", "target": "\ud800"}]
        )

    assert exc_info.value.errors()[0]["loc"] == ("must_be_defined",)


def test_a_template_keeps_a_valid_default_assignment():
    entries = [{"name": f"Hook {EMOJI}", "trigger_config": {"path": EMOJI}}]

    assert CreateTemplateRequest(slug="d", name="D", default_assignments=entries).default_assignments == entries


def test_a_template_trigger_config_that_is_not_an_object_reads_as_none():
    """The builder's create-from-template stored it as sent: a ``null`` became
    the JSON literal ``null``, a row every read fails to serialize. It
    now reads the entry like the REST path always did."""
    from app.models.agent_template import default_assignment_trigger_config

    config = {"path": EMOJI, "filter": {"x": 1}}
    assert default_assignment_trigger_config({"trigger_config": config}) is config
    assert default_assignment_trigger_config({}) == {}
    for malformed in (None, [], "x", 3):
        assert default_assignment_trigger_config({"trigger_config": malformed}) == {}


def test_both_template_paths_read_trigger_config_through_the_same_reader():
    import inspect

    from app.api.v1 import agents
    from app.services.builtin_tools import agent_builder

    for source, entry in (
        (inspect.getsource(agents.create_agent), "wf_data"),
        (inspect.getsource(agent_builder), "wf_def"),
    ):
        assert f"default_assignment_trigger_config({entry})" in source
        assert f'{entry}.get("trigger_config"' not in source


# ---------------------------------------------------------------------------
# Agent import
# ---------------------------------------------------------------------------


class _Untouchable:
    """A session that fails the test on any use."""

    def __getattr__(self, name: str):
        raise AssertionError(f"the database was touched ({name})")


async def _import(assignments: list[dict]) -> HTTPException:
    from app.api.v1.agent_import import create_agent_from_bundle

    bundle = {
        "version": 3,
        "agent": {"name": "Imported agent"},
        "variables": [{"id": str(uuid.uuid4()), "name": "REGION", "value": "eu"}],
        "assignments": assignments,
    }
    with pytest.raises(HTTPException) as exc_info:
        await create_agent_from_bundle(_Untouchable(), user=SimpleNamespace(id=uuid.uuid4()), bundle=bundle)
    return exc_info.value


async def test_import_refuses_a_lone_surrogate_before_the_database_is_touched():
    refused = await _import(
        [
            {"name": "First", "trigger_type": "manual", "prompt": "Go."},
            {"name": "Second", "trigger_type": "webhook", "prompt": "Go.", "trigger_config": {"path": "\ud800"}},
        ]
    )

    assert refused.status_code == 422
    assert refused.detail == f"Imported assignment 'Second': {_refusal('trigger_config.path')}"


async def test_import_names_the_entry_by_position_when_its_name_holds_one():
    """Checked before the length refusal, which would echo the name."""
    refused = await _import(
        [
            {"name": "First", "trigger_type": "manual", "prompt": "Go."},
            {"name": "Hook\ud800", "trigger_type": "manual", "prompt": "Go.", "delay": "x" * 500},
        ]
    )

    assert refused.status_code == 422
    assert refused.detail == f"Imported assignment #2: {_refusal('name')}"
    assert refused.detail.isascii()


# ---------------------------------------------------------------------------
# Agent-builder tools
# ---------------------------------------------------------------------------


def _no_session(monkeypatch) -> list[str]:
    """Record, and refuse, any database session a tool opens."""
    opened: list[str] = []

    def _factory():
        opened.append("session")
        raise AssertionError("the tool opened a database session")

    monkeypatch.setattr(database, "async_session", _factory)
    return opened


async def _tool(tool: str, arguments: dict[str, str]) -> str:
    _, executor = build_agent_builder_tool(None, str(uuid.uuid4()))
    result = await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=tool, arguments=arguments))
    return result.content


async def test_the_builder_tools_refuse_a_surrogate_character_in_trigger_config(monkeypatch):
    """The argument itself holds one: refused before a session, like any text argument."""
    opened = _no_session(monkeypatch)
    config = '{"filter": {"subject": "x\ud800"}}'

    created = await _tool(
        "create_assignment",
        {"agent_id": str(uuid.uuid4()), "name": "Hook", "prompt": "Go.", "trigger_config": config},
    )
    updated = await _tool("update_assignment", {"assignment_id": str(uuid.uuid4()), "trigger_config": config})

    assert created == _refusal("trigger_config")
    assert updated == f"{_refusal('trigger_config')} Nothing was saved."
    assert opened == []


async def test_the_builder_parses_a_trigger_config_escape_into_a_refusal():
    """Both tools decode ``trigger_config`` here, once the writable row is loaded."""
    from app.services.builtin_tools.agent_builder import _parse_trigger_config_arg

    assert _parse_trigger_config_arg('{"path": "\\ud800"}') == (
        None,
        f"Invalid trigger_config: {_refusal('trigger_config.path')}",
    )
    config = f'{{"path": "hook", "note": "{EMOJI}", "pair": "\\ud83d\\ude00"}}'
    assert _parse_trigger_config_arg(config) == ({"path": "hook", "note": EMOJI, "pair": EMOJI}, None)


@pytest.mark.parametrize("field", ["name", "prompt", "description", "activation_prompt", "delay"])
async def test_the_builder_tools_refuse_one_in_a_text_argument_before_a_session(monkeypatch, field):
    opened = _no_session(monkeypatch)
    arguments = {"agent_id": str(uuid.uuid4()), "name": "Hook", "prompt": "Go.", field: "x\ud800"}

    created = await _tool("create_assignment", arguments)
    updated = await _tool("update_assignment", {"assignment_id": str(uuid.uuid4()), field: "x\ud800"})

    assert created == _refusal(field)
    assert updated == f"{_refusal(field)} Nothing was saved."
    assert opened == []


async def test_the_builder_tools_let_a_valid_value_through_to_the_database(monkeypatch):
    opened = _no_session(monkeypatch)
    config = f'{{"path": "hook", "note": "{EMOJI}", "pair": "\\ud83d\\ude00"}}'

    await _tool(
        "create_assignment",
        {"agent_id": str(uuid.uuid4()), "name": f"Hook {EMOJI}", "prompt": "Go.", "trigger_config": config},
    )

    assert opened == ["session"]


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------


def _graph(**node_overrides: object) -> dict:
    node = {"id": "n1", "kind": "trigger.manual", "position": {"x": 0, "y": 0}, "config": {}}
    node.update(node_overrides)
    return {"nodes": [node], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}}


def _surrogate_issues(graph: dict) -> list[dict]:
    from app.services.flow_graph import validate_graph

    return [i.to_dict() for i in validate_graph(graph).issues if i.code == "graph.lone_surrogate"]


def test_a_flow_node_holding_one_is_refused_on_its_node_and_field():
    from app.services.flow_graph import validate_graph

    graph = _graph(config={"url": "https://x/\ud800", "method": "\ud800"})
    result = validate_graph(graph)

    assert not result.is_valid
    # One issue per node, the first place in it; nothing else is checked, so no
    # other issue echoes the value.
    [issue] = [i.to_dict() for i in result.issues]
    assert issue == {
        "code": "graph.lone_surrogate",
        "message": _refusal("config.url"),
        "severity": "error",
        "node_id": "n1",
        "edge_id": None,
        "field": "url",
    }


def test_a_flow_edge_or_an_id_holding_one_is_addressed_without_it():
    graph = _graph(id="n\ud800")
    graph["edges"] = [{"id": "e1", "source": "n1", "target": "n1", "kind": "data", "label": "\udc00"}]

    issues = _surrogate_issues(graph)

    assert [(i["node_id"], i["edge_id"], i["message"]) for i in issues] == [
        (None, None, _refusal("nodes[0].id")),
        (None, "e1", _refusal("label", "DC00")),
    ]


def test_a_flow_key_the_write_drops_is_not_checked():
    """What is stored is checked: a transient canvas key never reaches the column."""
    graph = _graph(selected="\ud800", label=f"Start {EMOJI}")
    graph["client_state"] = {"x": "\ud800"}

    assert _surrogate_issues(graph) == []


def test_a_flow_with_valid_characters_is_validated_as_before():
    from app.services.flow_graph import validate_graph

    clean = _graph(label=f"Start {EMOJI}", config={"note": "caf\u00e9"})
    plain = _graph(label="Start", config={"note": "cafe"})

    assert [i.code for i in validate_graph(clean).issues] == [i.code for i in validate_graph(plain).issues]


async def test_the_flow_builder_tool_refuses_a_graph_holding_one():
    from app.services.builtin_tools.flow_builder import build_flow_builder_tool

    _, executor = build_flow_builder_tool(_Untouchable(), str(uuid.uuid4()))
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="create_flow",
            arguments={"name": "Hook", "graph": json.dumps(_graph(config={"url": "\ud800"}))},
        )
    )

    assert result.content.startswith("The graph cannot be saved as it stands:")
    assert "config.url holds a lone UTF-16 surrogate" in result.content
