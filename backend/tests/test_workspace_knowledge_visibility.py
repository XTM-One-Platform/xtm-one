"""A workspace's own knowledge bases follow the workspace.

Spec 032 FR-002 to FR-004: a workspace's Sources and Conversations are listed
to exactly the people who can see the workspace, are changed only from the
workspace, and are never bound anywhere. These tests pin the clauses every
list, picker and binding validator inherits, as compiled SQL (the predicate is
what the call sites inherit, whether or not a database is reachable), and the
lock and refusal helpers on stand-in rows.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.v1 import helpers
from app.api.v1.helpers import (
    KB_WRITE_WORKSPACE,
    bindable_knowledge_base_clause,
    company_wide_visible_filter,
    knowledge_base_content_lock,
    knowledge_base_write_refusal,
    owning_workspace_id,
    user_visible_filter,
)
from app.models.agent import Agent
from app.models.knowledge_base import KnowledgeBase


def _sql(clause) -> str:
    return " ".join(str(clause.compile(compile_kwargs={"literal_binds": True})).split())


def _kb(**fields) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "name": "W — Sources",
        "owner_workspace_id": None,
        "is_builtin": False,
        "is_read_only": False,
        "is_company_managed": False,
    }
    base.update(fields)
    return SimpleNamespace(**base)


# ── Visibility ───────────────────────────────────────────────────────────────


def test_a_non_admin_sees_a_workspace_kb_through_the_workspace_only():
    sql = _sql(user_visible_filter(KnowledgeBase, uuid.uuid4(), ["g1"]))
    # The knowledge base's own tiers apply only to a knowledge base no
    # workspace owns...
    assert "knowledge_bases.owner_workspace_id IS NULL AND" in sql
    # ...and a workspace's is visible exactly when its workspace is.
    assert "knowledge_bases.owner_workspace_id IN (SELECT workspaces.id FROM workspaces WHERE" in sql
    assert "CAST(workspaces.shared_group_ids AS JSONB) ?| CAST(ARRAY['g1'] AS ARRAY)" in sql
    assert "knowledge_bases.is_internal IS false" in sql


def test_the_admin_branch_is_unchanged():
    """An admin sees every workspace, hence every workspace knowledge base:
    the admin clause stays the bare internal exclusion."""
    sql = _sql(user_visible_filter(KnowledgeBase, uuid.uuid4(), is_admin=True))
    assert sql == "knowledge_bases.is_internal IS false"


def test_other_entities_do_not_get_the_workspace_clause():
    sql = _sql(user_visible_filter(Agent, uuid.uuid4(), ["g1"]))
    assert "owner_workspace_id" not in sql
    assert "workspaces" not in sql


def test_a_caller_with_no_account_never_sees_a_workspace_kb():
    sql = _sql(company_wide_visible_filter(KnowledgeBase))
    assert "knowledge_bases.owner_workspace_id IS NULL" in sql
    assert "workspaces" not in sql


def test_the_bindable_clause_excludes_workspace_and_internal_kbs():
    sql = _sql(bindable_knowledge_base_clause())
    assert sql == (
        "knowledge_bases.owner_workspace_id IS NULL AND knowledge_bases.is_internal IS false"
    )


# ── The lock ─────────────────────────────────────────────────────────────────


def test_a_workspace_kb_is_locked_with_the_workspace_reason():
    kb = _kb(owner_workspace_id=uuid.uuid4())
    assert knowledge_base_content_lock(kb, package_installed=False) == KB_WRITE_WORKSPACE


def test_a_package_lock_still_wins_and_plain_kbs_are_unlocked():
    assert knowledge_base_content_lock(_kb(owner_workspace_id=uuid.uuid4()), package_installed=True) == (
        "package_installed"
    )
    assert knowledge_base_content_lock(_kb(), package_installed=False) is None


def test_a_fabricated_attribute_is_not_an_owner():
    """Callers pass stand-in rows: a mock's invented attribute must not lock."""
    from unittest.mock import MagicMock

    assert owning_workspace_id(MagicMock()) is None
    assert knowledge_base_content_lock(_kb(is_builtin=True), package_installed=False) == "builtin"


async def test_the_write_gate_refuses_a_workspace_kb_with_its_name(monkeypatch):
    workspace_id = uuid.uuid4()
    kb = _kb(owner_workspace_id=workspace_id)
    monkeypatch.setattr(helpers, "package_origin_knowledge_base_error", AsyncMock(return_value=None))
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: "Research")))

    refusal = await knowledge_base_write_refusal(db, kb, None)

    assert refusal is not None
    assert refusal.reason == KB_WRITE_WORKSPACE
    assert refusal.detail == "Managed by workspace 'Research'. Change it from the workspace."


async def test_the_route_guard_raises_403_for_a_workspace_kb_only():
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: "Research")))
    with pytest.raises(HTTPException) as exc:
        await helpers.assert_knowledge_base_not_workspace_managed(db, _kb(owner_workspace_id=uuid.uuid4()))
    assert exc.value.status_code == 403

    db.execute.reset_mock()
    await helpers.assert_knowledge_base_not_workspace_managed(db, _kb())
    db.execute.assert_not_awaited()



def test_the_parent_is_declared_on_the_model_not_spelled_in_the_filter():
    from app.api.v1.helpers import visibility_parent
    from app.models.workspace import Workspace

    column, parent = visibility_parent(KnowledgeBase)
    assert (column, parent) == (KnowledgeBase.owner_workspace_id, Workspace)
    assert visibility_parent(Agent) is None
