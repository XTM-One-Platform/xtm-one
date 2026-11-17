"""Regression tests for pre-dispatch argument validation on the integration
tool-test endpoint.

``POST /integrations/{id}/test-tool`` previously dispatched straight to
``executor.execute_tool(call)`` with the raw caller arguments, skipping the
agent loop's CORE-2 jsonschema pre-validation (``validate_tool_arguments``).
That made every provider handler individually vulnerable on this one path: a
JSON boolean for a string flag, a string for an integer field, or a missing
required field reached the handler raw and surfaced as an opaque exception (or
silent misbehaviour) - so reviewers kept finding the same class of bug one
handler argument at a time.

The fix validates the arguments against the tool's ``input_schema`` BEFORE
dispatch, exactly like the agent loop, so the test endpoint is a faithful
simulation of the agent path and returns controlled feedback for EVERY
integration tool at once - not just the Drata argument patched this week.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.services.integration_tools as integration_tools_mod
from app.api.v1 import integrations as integrations_api


def _user():
    return SimpleNamespace(id=uuid.uuid4(), is_admin=True)


class _FakeDB:
    async def execute(self, _stmt):  # pragma: no cover - never queried here
        return None


class _RecordingExecutor:
    """Stands in for the real tool executor; records whether it was dispatched."""

    def __init__(self) -> None:
        self.tool_names = {"list_drata_controls"}
        self.calls: list = []

    async def execute_tool(self, call):
        self.calls.append(call)
        return SimpleNamespace(is_error=False, content="ok")


_SCHEMA = {
    "type": "object",
    "properties": {
        "limit": {"type": "integer"},
        "is_ready": {"type": "string", "enum": ["true", "false"]},
    },
    "required": [],
}


def _patch_endpoint(monkeypatch, executor: _RecordingExecutor) -> None:
    integration = SimpleNamespace(
        id=uuid.uuid4(), status="connected", provider="drata", name="Drata"
    )

    async def _group_ids(_db, _uid):
        return []

    async def _get_integration(_db, _uid, _iid, _groups, **_kw):
        return integration

    async def _build_tools(_db, _bindings, context=None):
        defs = [SimpleNamespace(name="list_drata_controls", input_schema=_SCHEMA)]
        return defs, executor

    monkeypatch.setattr(integrations_api, "get_user_group_ids", _group_ids)
    monkeypatch.setattr(integrations_api, "get_user_integration", _get_integration)
    monkeypatch.setattr(
        integration_tools_mod, "build_integration_tools", _build_tools
    )


@pytest.mark.asyncio
async def test_schema_violating_args_rejected_before_dispatch(monkeypatch):
    """A string for an integer field is rejected with controlled feedback and
    the executor is NEVER reached (no raw handler exception)."""
    executor = _RecordingExecutor()
    _patch_endpoint(monkeypatch, executor)

    result = await integrations_api.test_integration_tool(
        integration_id=str(uuid.uuid4()),
        body={"tool_name": "list_drata_controls", "arguments": {"limit": "5"}},
        user=_user(),
        db=_FakeDB(),
    )

    assert result["status"] == "error"
    assert "Invalid arguments" in result["message"]
    assert "limit" in result["message"]
    # The malformed call must not have been dispatched to the handler.
    assert executor.calls == []


@pytest.mark.asyncio
async def test_valid_args_dispatch_to_executor(monkeypatch):
    """Schema-valid arguments pass validation and reach the executor as before."""
    executor = _RecordingExecutor()
    _patch_endpoint(monkeypatch, executor)

    result = await integrations_api.test_integration_tool(
        integration_id=str(uuid.uuid4()),
        body={
            "tool_name": "list_drata_controls",
            "arguments": {"limit": 5, "is_ready": "true"},
        },
        user=_user(),
        db=_FakeDB(),
    )

    assert result["status"] == "ok"
    assert result["output"] == "ok"
    assert len(executor.calls) == 1
    assert executor.calls[0].name == "list_drata_controls"
