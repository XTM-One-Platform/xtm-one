"""manage_opencti_connector action 'reset_state' (xtm-one #673).

OpenCTI's resetStateConnector(id) clears the stored state of a connector and
purges its queues, the Reset state button of the connector page. The tool reads
the queue depth first (the reset empties it), sends the mutation, and says what
happens next from the status read before the reset.
"""

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

import app.integrations.providers.opencti.connectors as connectors_mod
from app.integrations.providers.opencti import build_tools
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

TOKEN = "https://demo.opencti.io||test-token"


class _FakeGraphQL:
    """Answers the pre-check query and the reset mutation; records every call."""

    def __init__(self, connector: dict[str, Any] | None, *, precheck_error: str | None = None) -> None:
        self.connector = connector
        self.precheck_error = precheck_error
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def __call__(self, _client: Any, _url: Any, _headers: Any, query: str, variables: dict | None = None):
        self.calls.append((query, variables))
        if "resetStateConnector" in query:
            return {"resetStateConnector": {"id": "c1", "name": "AlienVault"}}, None
        if self.precheck_error:
            return {}, self.precheck_error
        return {"connector": self.connector}, None


def _build(monkeypatch: pytest.MonkeyPatch, fake: _FakeGraphQL) -> CallableToolExecutor:
    @contextlib.asynccontextmanager
    async def _client(*_args: Any, **_kwargs: Any) -> AsyncIterator[None]:
        yield None

    monkeypatch.setattr(connectors_mod, "_client", _client)
    monkeypatch.setattr(connectors_mod, "_graphql", fake)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor, live_writes=True)
    return executor


async def _run(executor: CallableToolExecutor, arguments: dict[str, Any]) -> ToolResult:
    return await executor.execute_tool(ToolCall(id="call-1", name="manage_opencti_connector", arguments=arguments))


def _connector(status: str = "started", messages: float | None = 42.0, *, managed: bool = True) -> dict[str, Any]:
    return {
        "id": "c1",
        "name": "AlienVault",
        "active": status == "started",
        "is_managed": managed,
        "manager_current_status": status if managed else None,
        "connector_queue_details": None if messages is None else {"messages_number": messages},
    }


async def test_reset_state_sends_the_mutation_and_names_the_purged_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGraphQL(_connector())
    result = await _run(_build(monkeypatch, fake), {"action": "reset_state", "connector_id": "c1"})
    assert not result.is_error, result.content
    queries = [query for query, _ in fake.calls]
    assert "connector_queue_details" in queries[0]
    assert "resetStateConnector" in queries[1]
    assert fake.calls[1][1] == {"id": "c1"}
    assert "state cleared" in result.content
    assert "42 message(s) waiting before the reset" in result.content
    assert "on its next run" in result.content


async def test_reset_state_of_a_stopped_managed_connector_says_start(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGraphQL(_connector("stopped", 0))
    result = await _run(_build(monkeypatch, fake), {"action": "reset_state", "connector_id": "c1"})
    assert "0 message(s)" in result.content
    assert "action='start'" in result.content


async def test_reset_state_of_an_unknown_connector_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGraphQL(None)
    result = await _run(_build(monkeypatch, fake), {"action": "reset_state", "connector_id": "nope"})
    assert "Connector nope not found." in result.content
    assert all("resetStateConnector" not in query for query, _ in fake.calls)


async def test_reset_state_still_resets_when_the_precheck_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # An OpenCTI without connector_queue_details answers the pre-check with an
    # error: the reset still runs and the answer says the depth was not read.
    fake = _FakeGraphQL(None, precheck_error="Cannot query field connector_queue_details")
    result = await _run(_build(monkeypatch, fake), {"action": "reset_state", "connector_id": "c1"})
    assert any("resetStateConnector" in query for query, _ in fake.calls)
    assert "could not be read before the reset" in result.content
    assert "action='get'" in result.content


async def test_reset_state_needs_a_connector_id_and_names_other_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGraphQL(_connector())
    executor = _build(monkeypatch, fake)
    missing = await _run(executor, {"action": "reset_state"})
    assert "connector_id is required for action 'reset_state'" in missing.content
    assert fake.calls == []
    ignored = await _run(executor, {"action": "reset_state", "connector_id": "c1", "config": "{}"})
    assert "does not apply config" in ignored.content
    assert any("resetStateConnector" in query for query, _ in fake.calls)


def test_reset_state_is_in_the_schema() -> None:
    executor = CallableToolExecutor()
    tools = build_tools(TOKEN, executor, live_writes=True)
    schema = {t.name: t for t in tools}["manage_opencti_connector"].input_schema
    assert "reset_state" in schema["properties"]["action"]["enum"]
