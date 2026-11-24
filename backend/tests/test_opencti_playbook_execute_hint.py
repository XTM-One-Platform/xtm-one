"""manage_opencti_playbook execute: OpenCTI's ineligibility error gets a hint.

OpenCTI answers "Playbook does not exist for this entity" for every playbook
that is not eligible for the entity (most often: the entity does not match the
entry component's filters), not only for a missing one. The tool passed that
text through, and agents concluded the playbook was missing. It now keeps the
error, says what it means and shows the playbook's entry node with its
configuration so the filter mismatch can be checked.

No network: ``_graphql`` is stubbed.
"""

import json
from typing import Any

import pytest

import app.integrations.providers.opencti.playbooks as playbooks_mod
from app.services.content_safety import untrusted_block_spans
from app.tools.base import CallableToolExecutor, ToolCall

_INELIGIBLE = (
    "GraphQL error in 'PlaybookExecute': Playbook does not exist for this entity "
    '[FUNCTIONAL_ERROR] {"http_status": 400, "genre": "BUSINESS"}'
)

_FILTERS = json.dumps(
    {
        "mode": "and",
        "filters": [{"key": ["x_opencti_workflow_id"], "values": ["status-new"], "operator": "eq", "mode": "or"}],
        "filterGroups": [],
    }
)

_GRAPH = {
    "nodes": [
        {
            "id": "entry-1",
            "name": "Listen",
            "component_id": "PLAYBOOK_INTERNAL_DATA_STREAM",
            "configuration": json.dumps({"create": True, "update": True, "filters": _FILTERS}),
        },
        {"id": "next-1", "name": "Label", "component_id": "PLAYBOOK_LABELING_COMPONENT", "configuration": "{}"},
    ],
    "links": [{"id": "l1", "from": {"id": "entry-1", "port": "out"}, "to": {"id": "next-1"}}],
}


class _Stub:
    def __init__(
        self, execute_error: str | None, playbook: dict[str, Any] | None = None, read_error: str | None = None
    ) -> None:
        self.execute_error = execute_error
        self.playbook = playbook
        self.read_error = read_error
        self.queries: list[str] = []

    async def __call__(
        self, client: Any, base_url: str, headers: dict[str, str], query: str, variables: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], str | None]:
        self.queries.append(query)
        if "playbookExecute" in query:
            if self.execute_error:
                return {}, self.execute_error
            return {"playbookExecute": True}, None
        if "playbook(id:" in query:
            if self.read_error:
                return {}, self.read_error
            return {"playbook": self.playbook}, None
        return {}, None


async def _execute(monkeypatch: pytest.MonkeyPatch, stub: _Stub) -> str:
    monkeypatch.setattr(playbooks_mod, "_graphql", stub)
    executor = CallableToolExecutor()
    playbooks_mod.register("https://demo.opencti.io", {}, executor)
    result = await executor.execute_tool(
        ToolCall(
            id="t",
            name="manage_opencti_playbook",
            arguments={"action": "execute", "playbook_id": "pb-1", "entity_id": "report-1"},
        )
    )
    return result.content


async def test_ineligible_error_names_the_entry_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    playbook = {
        "id": "pb-1",
        "name": "RSS triage",
        "playbook_running": True,
        "playbook_definition": json.dumps(_GRAPH),
    }
    content = await _execute(monkeypatch, _Stub(_INELIGIBLE, playbook))

    assert content.startswith(_INELIGIBLE)
    assert "exists but is not eligible for entity report-1" in content
    assert "does not match the filters of the playbook's entry component" in content
    assert "Playbook 'RSS triage' is running." in content
    assert "entry node entry-1 [PLAYBOOK_INTERNAL_DATA_STREAM]" in content
    assert "x_opencti_workflow_id" in content
    # The linked node is not an entry and is not shown.
    assert "next-1" not in content
    # The configuration is user-written text: wrapped as untrusted data.
    spans = untrusted_block_spans(content)
    assert spans and any("x_opencti_workflow_id" in content[start:end] for start, end in spans)


async def test_ineligible_error_when_the_playbook_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    content = await _execute(monkeypatch, _Stub(_INELIGIBLE, read_error="Error: forbidden"))

    assert content.startswith(_INELIGIBLE)
    assert "not eligible for entity report-1" in content
    assert "could not be read to show its entry (Error: forbidden)" in content


async def test_unreadable_definition_points_to_get(monkeypatch: pytest.MonkeyPatch) -> None:
    playbook = {"id": "pb-1", "name": "Broken", "playbook_running": False, "playbook_definition": "not json"}
    content = await _execute(monkeypatch, _Stub(_INELIGIBLE, playbook))

    assert "Playbook 'Broken' is stopped." in content
    assert "Its entry node could not be read" in content


async def test_other_errors_are_passed_through_without_a_read(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _Stub("Error: ACCESS_DENIED")
    content = await _execute(monkeypatch, stub)

    assert content == "Error: ACCESS_DENIED"
    assert not any("playbook(id:" in q for q in stub.queries)


async def test_success_points_to_the_step_read(monkeypatch: pytest.MonkeyPatch) -> None:
    content = await _execute(monkeypatch, _Stub(None))

    assert content.startswith("Triggered playbook pb-1 on entity report-1 (accepted: True).")
    assert "action='get'" in content
    assert "failed step" in content
