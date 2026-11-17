"""Backward-compat + safety for the KB read/write access rework.

Two guards:

1. Legacy clients that still send the deprecated WRITE fields
   (``write_knowledge_base_enabled`` / ``target_knowledge_base_id``) must be
   normalized onto the new ``write_knowledge_base_ids`` field, so the new
   ``NULL`` = inherit-all-writable semantics don't silently grant writes. Read
   has no "none" state (NULL/[] both = inherit all agent KBs), so the deprecated
   ``use_knowledge_bases`` flag is no longer consulted and never coerces read.
   Modern clients (which send the new fields, or omit everything) are untouched.

2. ``store_knowledge`` must not resolve an ambiguous KB *name* (KB names aren't
   unique) to an arbitrary KB - an ambiguous name is rejected with an id list.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.v1.schemas.agent import CreateAgentRequest, UpdateAgentRequest
from app.api.v1.schemas.assignment import (
    CreateAssignmentRequest,
    UpdateAssignmentRequest,
)
from app.services.builtin_tools import build_kb_store_tool
from app.tools.base import ToolCall

# ── Deprecated-field normalization ──────────────────────────────────────────


def test_legacy_write_disabled_pins_write_to_empty():
    """Legacy ``write_knowledge_base_enabled=False`` with no new write field
    must pin ``write_knowledge_base_ids=[]`` (not leave NULL = inherit-all)."""
    req = CreateAssignmentRequest(
        agent_id="a", name="n", trigger_type="manual", prompt="p",
        write_knowledge_base_enabled=False,
    )
    assert req.write_knowledge_base_ids == []


def test_legacy_write_enabled_with_target_maps_to_list():
    req = CreateAssignmentRequest(
        agent_id="a", name="n", trigger_type="manual", prompt="p",
        write_knowledge_base_enabled=True, target_knowledge_base_id="kb-1",
    )
    assert req.write_knowledge_base_ids == ["kb-1"]


def test_legacy_use_kbs_false_does_not_disable_read():
    """Read has no "none" state (NULL/[] both = inherit all agent KBs), so the
    deprecated ``use_knowledge_bases=False`` flag has nothing to map onto and is
    no longer consulted: it must leave ``read_knowledge_base_ids`` as NULL
    (inherit all) rather than coercing a meaningless empty list."""
    req = CreateAssignmentRequest(
        agent_id="a", name="n", trigger_type="manual", prompt="p",
        use_knowledge_bases=False,
    )
    assert req.read_knowledge_base_ids is None


def test_modern_client_defaults_are_untouched():
    """A modern client that sends neither deprecated nor new KB fields keeps the
    new NULL = inherit-all defaults (normalization must not fire)."""
    req = CreateAssignmentRequest(
        agent_id="a", name="n", trigger_type="manual", prompt="p"
    )
    assert req.read_knowledge_base_ids is None
    assert req.write_knowledge_base_ids is None


def test_explicit_new_field_wins_over_deprecated():
    """When both a deprecated and its new counterpart are sent, the explicit new
    field is authoritative (no override)."""
    req = CreateAssignmentRequest(
        agent_id="a", name="n", trigger_type="manual", prompt="p",
        write_knowledge_base_enabled=False,
        write_knowledge_base_ids=["kb-9"],
    )
    assert req.write_knowledge_base_ids == ["kb-9"]


def test_update_request_normalizes_deprecated_write():
    req = UpdateAssignmentRequest(write_knowledge_base_enabled=False)
    assert req.write_knowledge_base_ids == []


def test_update_request_modern_partial_untouched():
    """A partial update that doesn't touch KB fields must leave them NULL."""
    req = UpdateAssignmentRequest(name="renamed")
    assert req.write_knowledge_base_ids is None
    assert req.read_knowledge_base_ids is None


# ── store_knowledge ambiguous-name safety ───────────────────────────────────


@pytest.mark.asyncio
async def test_store_knowledge_rejects_ambiguous_name():
    """Two writable KBs share the name "Docs"; resolving by that name must be
    rejected (with both ids offered) rather than silently writing to one."""
    result = build_kb_store_tool(
        None, "user-1", [("id-A", "Docs"), ("id-B", "Docs")]
    )
    assert result is not None
    _, executor = result
    out = await executor.execute_tool(
        ToolCall(
            id="x",
            name="store_knowledge",
            arguments={"title": "t", "content": "c", "knowledge_base": "Docs"},
        )
    )
    assert "is not a writable knowledge base" in out.content
    assert "id-A" in out.content and "id-B" in out.content


def test_store_knowledge_multi_description_lists_ids():
    """KB names are not unique, so the multi-KB store_knowledge description must
    surface each KB's id alongside its name to let the agent target by id."""
    result = build_kb_store_tool(
        None, "user-1", [("id-A", "Docs"), ("id-B", "Docs")]
    )
    assert result is not None
    definitions, _ = result
    description = definitions[0].description
    assert "id-A" in description and "id-B" in description


# ── knowledge_base_permissions value validation ─────────────────────────────


def test_create_agent_accepts_read_and_write_permissions():
    req = CreateAgentRequest(
        name="A", knowledge_base_permissions={"kb-1": "read", "kb-2": "write"}
    )
    assert req.knowledge_base_permissions == {"kb-1": "read", "kb-2": "write"}


def test_create_agent_normalizes_permission_case():
    """A client sending 'Write'/'READ' round-trips to the canonical lowercase
    union the rest of the code keys off (Agent.writable_knowledge_base_ids
    checks == 'write')."""
    req = CreateAgentRequest(
        name="A", knowledge_base_permissions={"kb-1": "Write", "kb-2": "READ"}
    )
    assert req.knowledge_base_permissions == {"kb-1": "write", "kb-2": "read"}


def test_create_agent_rejects_unknown_permission():
    with pytest.raises(ValidationError) as exc:
        CreateAgentRequest(
            name="A", knowledge_base_permissions={"kb-1": "admin"}
        )
    assert "must be 'read' or 'write'" in str(exc.value)


def test_update_agent_rejects_unknown_permission():
    with pytest.raises(ValidationError):
        UpdateAgentRequest(knowledge_base_permissions={"kb-1": "owner"})


def test_update_agent_none_permissions_untouched():
    req = UpdateAgentRequest(name="renamed")
    assert req.knowledge_base_permissions is None
