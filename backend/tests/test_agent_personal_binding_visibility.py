"""Regression tests for binding visibility validation on personal agents.

An attacker who knows another user's private integration UUID can attempt to
bind it to their own agent, causing the agent to execute with the victim's
OAuth token or API key. Same risk applies to private KBs, MCP servers, custom
tools, handover agents, skills, and prompts.

``validate_personal_agent_bindings`` prevents this at save-time: all supplied
IDs must be visible to the requesting user (own + group-shared + company-managed).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.services import agent_bindings


class _FakeResult:
    def __init__(self, ids: list[uuid.UUID]):
        self._ids = ids

    def all(self):
        return [(i,) for i in self._ids]


class _FakeDB:
    def __init__(self):
        self._results: list = []

    async def execute(self, _stmt):
        return self._results.pop(0) if self._results else _FakeResult([])


async def _validate(fake_db, user_id, *, is_admin=False, **bindings):
    return await agent_bindings.validate_personal_agent_bindings(
        fake_db,
        user_id,
        user_group_ids=[],
        is_admin=is_admin,
        mcp_server_ids=bindings.get("mcp_server_ids"),
        knowledge_base_ids=bindings.get("knowledge_base_ids"),
        integration_bindings=bindings.get("integration_bindings"),
        handover_agent_ids=bindings.get("handover_agent_ids"),
        custom_tool_ids=bindings.get("custom_tool_ids"),
        skill_ids=bindings.get("skill_ids"),
        prompt_ids=bindings.get("prompt_ids"),
    )


def _binding_kwargs(field: str, uid: str) -> dict:
    if field == "integration_bindings":
        return {"integration_bindings": {"github": [uid]}}
    return {field: [uid]}


# ---------------------------------------------------------------------------
# Core security: invisible resource must be rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("field", [
    "knowledge_base_ids",
    "mcp_server_ids",
    "integration_bindings",
    "handover_agent_ids",
    "custom_tool_ids",
    "skill_ids",
    "prompt_ids",
])
async def test_binding_invisible_resource_raises_400(field):
    """Binding any resource type invisible to the user must raise 400.

    This is the primary IDOR guard: prevents hijacking another user's OAuth
    token, KB content, MCP server, skill content, or prompt by referencing
    their private resource UUID.
    """
    uid = str(uuid.uuid4())
    fake_db = _FakeDB()
    fake_db._results = [_FakeResult([])]  # resource not visible

    with pytest.raises(HTTPException) as exc_info:
        await _validate(fake_db, uuid.uuid4(), **_binding_kwargs(field, uid))

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_binding_visible_resource_passes():
    """Binding a resource visible to the user must succeed."""
    kb_id = uuid.uuid4()
    fake_db = _FakeDB()
    fake_db._results = [_FakeResult([kb_id])]

    await _validate(fake_db, uuid.uuid4(), knowledge_base_ids=[str(kb_id)])


@pytest.mark.asyncio
async def test_admin_can_bind_any_resource():
    """Admins bypass visibility so they can configure any resource on any agent."""
    kb_id = uuid.uuid4()
    fake_db = _FakeDB()
    fake_db._results = [_FakeResult([kb_id])]

    await _validate(fake_db, uuid.uuid4(), is_admin=True, knowledge_base_ids=[str(kb_id)])


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", ["", "not-a-uuid", "12345"])
async def test_invalid_uuid_format_raises_400(bad_id):
    """Malformed ID strings must raise 400, not a 500 from an uncaught ValueError."""
    fake_db = _FakeDB()

    with pytest.raises(HTTPException) as exc_info:
        await _validate(fake_db, uuid.uuid4(), knowledge_base_ids=[bad_id])

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_duplicate_ids_are_accepted():
    """Sending the same ID twice must not cause a false rejection.

    Before the dedup fix, count=1 vs len=2 would raise a spurious 400.
    """
    kb_id = uuid.uuid4()
    fake_db = _FakeDB()
    fake_db._results = [_FakeResult([kb_id])]

    await _validate(fake_db, uuid.uuid4(), knowledge_base_ids=[str(kb_id), str(kb_id)])
