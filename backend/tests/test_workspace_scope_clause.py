"""The ``?workspace=`` scope of the Operations lists.

A workspace's Operations tab lists what is filed in it and what the fleet it
hosts produced: the work items, shared state and worksets owned (or, for a
work item, created) by a current home member. One clause,
``workspace_scope_clause``, for every table; membership is a subquery so it
is resolved when the list runs.
"""

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.api.v1.helpers import workspace_filter
from app.models.shared_state import SharedState
from app.models.work_item import WorkItem
from app.models.workset import Workset
from app.services.fleet_views import workspace_scope_clause


def _sql(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_a_work_item_belongs_when_filed_owned_or_created_by_a_home_member() -> None:
    workspace_id = uuid.uuid4()
    sql = _sql(
        workspace_scope_clause(
            workspace_id, WorkItem.workspace_id, WorkItem.owner_agent_id, WorkItem.created_by_agent_id
        )
    )
    assert f"work_items.workspace_id = '{workspace_id}'" in sql
    assert "work_items.owner_agent_id IN (SELECT fleet_members.agent_id" in sql
    assert "work_items.created_by_agent_id IN (SELECT fleet_members.agent_id" in sql
    assert f"fleets.workspace_id = '{workspace_id}'" in sql
    assert "fleet_members.role = 'member'" in sql, "connected agents are not the fleet's"


@pytest.mark.parametrize("model", [SharedState, Workset])
def test_shared_state_and_worksets_follow_their_owner(model) -> None:
    workspace_id = uuid.uuid4()
    table = model.__tablename__
    sql = _sql(workspace_scope_clause(workspace_id, model.workspace_id, model.owner_agent_id))
    assert f"{table}.workspace_id = '{workspace_id}'" in sql
    assert f"{table}.owner_agent_id IN (SELECT fleet_members.agent_id" in sql


def test_a_workspace_without_agent_columns_keeps_what_is_filed_in_it() -> None:
    workspace_id = uuid.uuid4()
    assert _sql(workspace_scope_clause(workspace_id, WorkItem.workspace_id)) == (
        f"work_items.workspace_id = '{workspace_id}'"
    )


def test_a_workspace_that_is_not_a_uuid_is_a_422() -> None:
    with pytest.raises(HTTPException) as refused:
        workspace_filter("not-a-uuid", WorkItem.workspace_id, WorkItem.owner_agent_id)
    assert refused.value.status_code == 422
