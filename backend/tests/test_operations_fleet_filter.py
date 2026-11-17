"""The ``?fleet=`` filter of the Operations lists.

Every list the Operations page and the fleet page's Operations tab read (runs,
background tasks, work items, shared state, worksets) takes ``fleet=<id>`` and
narrows to the rows of the fleet's current home members, resolved in SQL when
the query runs (``fleet_views.fleet_scope_clause``). These tests pin the clause
on each builder, the 422 on a malformed id, and that the filter is declared on
every list and count route.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.api.v1 import assignments as assignments_api
from app.api.v1 import chat as chat_api
from app.api.v1 import shared_state as shared_state_api
from app.api.v1 import work_items as work_items_api
from app.api.v1 import worksets as worksets_api
from app.api.v1.helpers import combine_scopes, fleet_operations_filter
from app.models.shared_state import SharedState
from app.models.work_item import WorkItem
from app.models.workset import Workset
from app.services.sub_agent import _background_tasks_query


def _sql(clause) -> str:
    return str(
        clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    ).lower()


def _where(statement) -> str:
    clause = statement.whereclause
    return "" if clause is None else _sql(clause)


def _user():
    return SimpleNamespace(id=uuid.uuid4(), is_admin=False, role="user")


FLEET = uuid.uuid4()


def _fleet_sql(where: str) -> None:
    assert "fleet_members" in where
    assert FLEET.hex in where.replace("-", "")


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


def test_fleet_operations_filter_narrows_to_home_members():
    sql = _sql(fleet_operations_filter(str(FLEET), WorkItem.owner_agent_id))
    _fleet_sql(sql)
    assert "work_items.owner_agent_id in" in sql
    assert "fleet_members.role" in sql


def test_fleet_operations_filter_ors_several_columns():
    sql = _sql(
        fleet_operations_filter(str(FLEET), WorkItem.owner_agent_id, WorkItem.created_by_agent_id)
    )
    assert "work_items.owner_agent_id in" in sql
    assert "work_items.created_by_agent_id in" in sql
    assert " or " in sql


@pytest.mark.parametrize("value", ["not-a-uuid", "none", "123"])
def test_fleet_operations_filter_refuses_a_malformed_id(value):
    with pytest.raises(HTTPException) as exc:
        fleet_operations_filter(value, WorkItem.owner_agent_id)
    assert exc.value.status_code == 422


def test_combine_scopes():
    assert combine_scopes(None, None) is None
    one = WorkItem.owner_agent_id.is_(None)
    assert combine_scopes(None, one) is one
    both = _sql(combine_scopes(one, WorkItem.workspace_id.is_(None)))
    assert " and " in both


# ---------------------------------------------------------------------------
# Each list
# ---------------------------------------------------------------------------


def test_runs_query_takes_the_fleet():
    user = _user()
    where = _where(assignments_api._filtered_runs_query(user, fleet=str(FLEET)))
    _fleet_sql(where)
    assert "assignment_runs.agent_id in" in where
    # Without ?fleet= the fleet is not in the query. fleet_members may still
    # appear: the activity hidden from non-admins (#11624) reads the members
    # of the fleets that hide it.
    assert FLEET.hex not in _where(assignments_api._filtered_runs_query(user)).replace("-", "")


def test_runs_query_fleet_composes_with_agent():
    agent = uuid.uuid4()
    where = _where(
        assignments_api._filtered_runs_query(_user(), agent_id=str(agent), fleet=str(FLEET))
    )
    _fleet_sql(where)
    assert agent.hex in where.replace("-", "")


def test_runs_query_refuses_a_malformed_fleet():
    with pytest.raises(HTTPException) as exc:
        assignments_api._filtered_runs_query(_user(), fleet="nope")
    assert exc.value.status_code == 422


def test_runs_of_a_fleet_include_the_runs_its_roi_records_carry():
    """#9837: the fleet's review rate counts the ROI records stamped with the
    fleet. All (no review) lists the current members' runs and the runs those
    records carry, so a record whose agent left the fleet is still listed."""
    where = _where(assignments_api._filtered_runs_query(_user(), fleet=str(FLEET)))
    _fleet_sql(where)
    assert "roi_run_records.fleet_id =" in where
    assert FLEET.hex in where.replace("-", "")
    assert "roi_run_records.outcome is null" not in where


def test_unreviewed_runs_of_a_fleet_narrow_the_same_population():
    """#9837: Unreviewed is the All population of the fleet narrowed to the
    unreviewed records of that fleet, so it is always a subset of All."""
    where = _where(
        assignments_api._filtered_runs_query(_user(), fleet=str(FLEET), review="unreviewed")
    )
    _fleet_sql(where)
    assert "roi_run_records.outcome is null" in where
    assert where.count("roi_run_records.fleet_id =") == 2


def test_unreviewed_runs_of_a_fleet_refuse_a_malformed_fleet():
    with pytest.raises(HTTPException) as exc:
        assignments_api._filtered_runs_query(_user(), fleet="nope", review="unreviewed")
    assert exc.value.status_code == 422


def test_background_tasks_query_takes_the_fleet():
    query = _background_tasks_query(
        requester_id=str(uuid.uuid4()),
        is_admin=False,
        scope="mine",
        status=None,
        agent_id=None,
        fleet_id=FLEET,
    )
    where = _where(query)
    _fleet_sql(where)
    assert "parent_agent_id in" in where
    assert "target_agent_id in" in where


def test_work_item_scope_takes_the_fleet_and_the_workspace():
    sql = _sql(work_items_api._workspace_scope(None, str(FLEET)))
    _fleet_sql(sql)
    assert "work_items.created_by_agent_id in" in sql
    workspace = uuid.uuid4()
    both = _sql(work_items_api._workspace_scope(str(workspace), str(FLEET)))
    assert " and " in both
    assert workspace.hex in both.replace("-", "")
    assert work_items_api._workspace_scope(None, None) is None


def test_shared_state_scope_takes_the_fleet():
    sql = _sql(shared_state_api._workspace_scope(None, str(FLEET)))
    _fleet_sql(sql)
    assert "shared_states.owner_agent_id in" in sql or "owner_agent_id in" in sql
    assert shared_state_api._workspace_scope(None, None) is None
    assert SharedState.owner_agent_id is not None


def test_workset_clauses_take_the_fleet():
    clauses = worksets_api._in_workspace(None, str(FLEET))
    assert len(clauses) == 1
    where = _where(select(Workset).where(*clauses))
    _fleet_sql(where)
    assert worksets_api._in_workspace(None, None) == []


# ---------------------------------------------------------------------------
# The routes declare it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "route",
    [
        assignments_api.list_all_runs,
        assignments_api.count_runs,
        chat_api.list_sub_agent_tasks,
        chat_api.count_sub_agent_tasks,
        work_items_api.list_work_items,
        work_items_api.count_work_items,
        work_items_api.list_work_item_owners,
        shared_state_api.list_shared_state,
        shared_state_api.count_shared_state,
        worksets_api.list_worksets,
        worksets_api.count_worksets,
    ],
)
def test_every_operations_list_and_count_takes_fleet(route):
    parameter = inspect.signature(route).parameters["fleet"]
    # A plain None default: routes are also called as functions (the
    # integration suites), where a ``Query(None)`` default would reach the
    # filter as a FieldInfo and be refused as an invalid fleet.
    assert parameter.default is None
