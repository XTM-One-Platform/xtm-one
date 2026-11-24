"""A flow Integration node whose operation ANSWERS with an error.

An error string a provider returned (``format_http_error``'s
``Error: ... resource not found`` for a lookup that finds nothing) reaches the
flow as the node's result and the step completes. The agent loop stamps such
answers ``is_error=True``, but a flow Integration node does not fail on them by
default; failing on an answered error is the node's ``fail_on_error_result``
opt-in. A RAISED failure fails the step.
"""

import uuid
from types import SimpleNamespace

import pytest

from app.services.flow_catalog import get_node_kind
from app.services.flow_runner import FlowWalker
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import CallableToolExecutor, ToolCall

_NOT_FOUND = "Error: GET /indicators/1.2.3.4 - resource not found (404)."


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


def _walker(monkeypatch, handler):
    _the_run_s_user_may_use_it(monkeypatch)
    integration = SimpleNamespace(id=uuid.uuid4(), provider="github")

    async def _fake_build(db, bindings, context=None):
        executor = _SafeIntegrationExecutor()
        executor.register("lookup", handler)
        return (
            [SimpleNamespace(name="lookup", input_schema={"type": "object"})],
            executor,
        )

    monkeypatch.setattr(
        "app.services.integration_tools.build_integration_tools", _fake_build
    )
    walker = object.__new__(FlowWalker)
    walker.db = _Db(integration)
    walker.user = SimpleNamespace(id=uuid.uuid4())
    walker._renderer = None
    return walker, integration


async def _answers_not_found(**_kwargs):
    return _NOT_FOUND


@pytest.mark.asyncio
async def test_an_answered_error_completes_and_passes_the_text_on(monkeypatch):
    walker, integration = _walker(monkeypatch, _answers_not_found)
    result = await walker._exec_integration(
        {"integration_id": str(integration.id), "operation": "lookup"}
    )
    assert result.error is None
    assert result.outputs["result"] == _NOT_FOUND
    assert result.warning == _NOT_FOUND


@pytest.mark.asyncio
async def test_the_opt_in_fails_the_step_on_an_answered_error(monkeypatch):
    walker, integration = _walker(monkeypatch, _answers_not_found)
    result = await walker._exec_integration(
        {
            "integration_id": str(integration.id),
            "operation": "lookup",
            "fail_on_error_result": True,
        }
    )
    assert result.error == _NOT_FOUND
    assert result.outputs == {}


@pytest.mark.asyncio
async def test_a_raised_failure_still_fails_the_step(monkeypatch):
    async def _raises(**_kwargs):
        raise RuntimeError("upstream exploded")

    walker, integration = _walker(monkeypatch, _raises)
    result = await walker._exec_integration(
        {"integration_id": str(integration.id), "operation": "lookup"}
    )
    assert result.error is not None
    assert "upstream exploded" in result.error


@pytest.mark.asyncio
async def test_a_plain_answer_carries_no_warning(monkeypatch):
    async def _ok(**_kwargs):
        return "indicator: 1.2.3.4 (score 80)"

    walker, integration = _walker(monkeypatch, _ok)
    result = await walker._exec_integration(
        {"integration_id": str(integration.id), "operation": "lookup"}
    )
    assert result.error is None and result.warning is None
    assert result.outputs["result"] == "indicator: 1.2.3.4 (score 80)"


@pytest.mark.asyncio
async def test_the_agent_loop_still_reads_an_answered_error_as_a_failure():
    """Decision 15d: the stamp the loop's cooldown and brake count stays."""
    for executor in (_SafeIntegrationExecutor(), CallableToolExecutor()):
        executor.register("lookup", _answers_not_found)
        result = await executor.execute_tool(
            ToolCall(id="c1", name="lookup", arguments={})
        )
        assert result.is_error is True
        assert result.error_returned is True


def test_the_opt_in_is_declared_in_the_catalog_and_off_by_default():
    kind = get_node_kind("integration")
    field = next(f for f in kind.config_fields if f.name == "fail_on_error_result")
    assert field.type == "boolean"
    assert field.default is False
