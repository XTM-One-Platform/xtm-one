"""A flow Integration node publishes the data its operation answers.

Whichever provider answers - a GitHub GraphQL query, a Notion query with
``output_format: json``, an OpenCTI creation - an object is data the flow
reads by key (``{{node.fields.KEY}}``), the contract of the Script and Run
code nodes, and a list (an OpenCTI listing) is ``items``. The data is the
operation's structured value, else its text when that text is JSON. Any other
answer, and an answered error, publish neither.
"""

import json
import uuid
from types import SimpleNamespace

import pytest

from app.services.flow_catalog import get_node_kind
from app.services.flow_runner import FlowWalker
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolResult


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Db:
    def __init__(self, integration):
        self._integration = integration

    async def execute(self, _stmt):
        return _Result(self._integration)


def _the_run_s_user_may_use_it(monkeypatch):
    """The node's visibility guard reads the database: these cases only need
    the run's user to be allowed (``test_agent_integration_bindings_postgres``
    covers the guard itself)."""

    async def _keep(*_args, **_kwargs):
        return []

    async def _no_groups(*_args, **_kwargs):
        return []

    monkeypatch.setattr("app.services.integration_tools.shadow_invisible_bindings", _keep)
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", _no_groups)


def _walker(monkeypatch, answer, contexts: list | None = None):
    _the_run_s_user_may_use_it(monkeypatch)
    integration = SimpleNamespace(id=uuid.uuid4(), provider="github")

    async def _handler(**_kwargs):
        return answer

    async def _fake_build(db, bindings, context=None):
        if contexts is not None:
            contexts.append(context)
        executor = _SafeIntegrationExecutor()
        executor.register("lookup", _handler)
        return [SimpleNamespace(name="lookup", input_schema={"type": "object"})], executor

    monkeypatch.setattr("app.services.integration_tools.build_integration_tools", _fake_build)
    walker = object.__new__(FlowWalker)
    walker.db = _Db(integration)
    walker.user = SimpleNamespace(id=uuid.uuid4())
    walker._renderer = None
    return walker, {"integration_id": str(integration.id), "operation": "lookup"}


@pytest.mark.asyncio
async def test_a_json_object_answer_is_published_as_fields(monkeypatch):
    answer = json.dumps({"data": {"e0": {"issue": {"number": 7}}}, "has_more": False})
    walker, config = _walker(monkeypatch, answer)

    result = await walker._exec_integration(config)

    assert result.error is None
    assert result.outputs["result"] == answer
    assert result.outputs["fields"] == {"data": {"e0": {"issue": {"number": 7}}}, "has_more": False}


@pytest.mark.parametrize(
    "answer",
    ["Found 2 issues:\n- #1\n- #2", "{not json", ""],
)
@pytest.mark.asyncio
async def test_any_other_answer_has_no_data(monkeypatch, answer):
    walker, config = _walker(monkeypatch, answer)

    result = await walker._exec_integration(config)

    assert result.outputs["result"] == answer
    assert "fields" not in result.outputs
    assert "items" not in result.outputs


@pytest.mark.asyncio
async def test_a_json_list_answer_is_published_as_items(monkeypatch):
    walker, config = _walker(monkeypatch, '["a", "b"]')

    result = await walker._exec_integration(config)

    assert result.outputs["items"] == ["a", "b"]
    assert "fields" not in result.outputs


@pytest.mark.asyncio
async def test_the_structured_value_beside_prose_is_the_data(monkeypatch):
    listed = [{"id": "r-1", "entity_type": "Report", "name": "A"}]
    answer = ToolResult(tool_call_id="", content="Found 1 report: A", structured=listed)
    walker, config = _walker(monkeypatch, answer)

    result = await walker._exec_integration(config)

    assert result.outputs["result"] == "Found 1 report: A"
    assert result.outputs["items"] == listed


@pytest.mark.asyncio
async def test_the_operation_runs_autonomously(monkeypatch):
    """A step asks nobody during the run: OpenCTI provisions its draft."""
    contexts: list = []
    walker, config = _walker(monkeypatch, "ok", contexts)

    await walker._exec_integration(config)

    assert contexts[0]["autonomous"] is True
    assert contexts[0]["credential_user_id"] == str(walker.user.id)


@pytest.mark.asyncio
async def test_an_answered_error_publishes_no_fields(monkeypatch):
    answer = '{"error": true, "message": "rate limited"}'
    walker, config = _walker(monkeypatch, answer)

    result = await walker._exec_integration(config)

    assert result.warning == answer
    assert "fields" not in result.outputs


def test_the_catalog_declares_the_data_outputs():
    outputs = {o.name: o for o in get_node_kind("integration").output_variables}

    assert list(outputs) == ["result", "fields", "items"]
    assert outputs["fields"].type == "object"
    assert outputs["items"].type == "list"
