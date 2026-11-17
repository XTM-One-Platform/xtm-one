"""The Operations "Mine" / "Shared" scope of work items, shared state and worksets.

``ownership_clause`` is the one helper the three list endpoints share; these
tests pin what it compiles to for each value and each resource, and that the
Executions vocabulary (``validate_run_ownership``) is the one the routes use.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.api.v1 import worksets as worksets_api
from app.api.v1.helpers import validate_run_ownership
from app.models.shared_state import SharedState
from app.models.work_item import WorkItem
from app.models.workset import Workset
from app.services import shared_state as shared_state_service
from app.services import work_item as work_item_service
from app.services.ownership_scope import ownership_clause


def _sql(clause) -> str:
    return str(
        select(WorkItem.id).where(clause).compile(dialect=postgresql.dialect())
    ).lower()


def _clause(ownership, user_uuid=None):
    return ownership_clause(
        ownership,
        user_uuid=user_uuid or uuid.uuid4(),
        user_column=WorkItem.user_id,
        agent_columns=(WorkItem.owner_agent_id, WorkItem.created_by_agent_id),
    )


@pytest.mark.parametrize("value", [None, "", "all"])
def test_no_ownership_adds_no_clause(value):
    """Omitted or "all": the tools and older callers keep their meaning."""
    assert _clause(value) is None


def test_mine_is_my_user_id_or_an_agent_i_own():
    sql = _sql(_clause("mine"))
    assert "work_items.user_id = " in sql
    assert "work_items.owner_agent_id in (select agents.id" in sql
    assert "work_items.created_by_agent_id in (select agents.id" in sql
    assert "agents.user_id = " in sql
    assert " or " in sql
    assert "not (" not in sql


@pytest.mark.parametrize("value", ["shared", "company_managed"])
def test_shared_is_the_negation_of_mine(value):
    """Shared and company-managed agents, a workspace I read: the rest of what I see."""
    sql = _sql(_clause(value))
    assert sql.count("not (") == 1
    assert "agents.user_id = " in sql


def test_every_term_is_null_guarded_so_shared_is_exactly_the_complement():
    """A NULL owner must land in "shared", never vanish from both buckets."""
    sql = _sql(_clause("mine"))
    assert "work_items.user_id is not null" in sql
    assert "work_items.owner_agent_id is not null" in sql
    assert "work_items.created_by_agent_id is not null" in sql


def test_an_anonymous_caller_owns_nothing():
    mine = ownership_clause(
        "mine", user_uuid=None, user_column=WorkItem.user_id, agent_columns=()
    )
    shared = ownership_clause(
        "shared", user_uuid=None, user_column=WorkItem.user_id, agent_columns=()
    )
    assert "false" in _sql(mine)
    assert "not false" in _sql(shared) or "true" in _sql(shared)


def test_an_unknown_value_is_refused_not_widened():
    with pytest.raises(ValueError):
        _clause("min")


def test_the_routes_validate_with_the_executions_vocabulary():
    assert validate_run_ownership("mine") == "mine"
    assert validate_run_ownership("all") is None
    with pytest.raises(HTTPException) as exc:
        validate_run_ownership("min")
    assert exc.value.status_code == 422


def test_work_items_mine_counts_owner_and_creator_agents():
    uid = uuid.uuid4()
    (clause,) = work_item_service._ownership_clauses("mine", uid)
    sql = _sql(clause)
    assert "work_items.owner_agent_id in" in sql
    assert "work_items.created_by_agent_id in" in sql
    assert work_item_service._ownership_clauses(None, uid) == []


def test_shared_state_mine_is_user_or_owner_agent():
    clause = shared_state_service._ownership_clause("mine", uuid.uuid4())
    sql = str(
        select(SharedState.id).where(clause).compile(dialect=postgresql.dialect())
    ).lower()
    assert "shared_states.user_id = " in sql or ".user_id = " in sql
    assert "owner_agent_id in (select agents.id" in sql
    assert shared_state_service._ownership_clause("all", uuid.uuid4()) is None


def test_worksets_mine_is_user_or_owner_agent():
    user = SimpleNamespace(id=uuid.uuid4())
    (clause,) = worksets_api._owned(user, "mine")
    sql = str(
        select(Workset.id).where(clause).compile(dialect=postgresql.dialect())
    ).lower()
    assert "owner_agent_id in (select agents.id" in sql
    assert worksets_api._owned(user, None) == []
    with pytest.raises(HTTPException):
        worksets_api._owned(user, "everyone")
