"""The Status filter of Operations > Executions (#9378 slice 8f).

The Status pill on the Executions toolbar is multi-select: both run feeds take
``status`` as one value or several comma-separated, and each answers how many
runs every status holds (``/runs/status-counts``) for the counts on the pill.
These tests pin the SQL of the multi-value filter, the 422 on an empty or
unknown value, the Executions vocabulary of the flow feed's counts and the
routes' declaration order.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from sqlalchemy.dialects import postgresql

from app.api.v1 import assignments as assignments_api
from app.api.v1 import flow_runs as flow_runs_api


def _where(statement) -> str:
    clause = statement.whereclause
    if clause is None:
        return ""
    return str(clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})).lower()


def _user():
    return SimpleNamespace(id=uuid.uuid4(), is_admin=False, role="user")


def test_status_values_split_trim_and_dedupe():
    assert assignments_api.run_status_values("running") == ["running"]
    assert assignments_api.run_status_values(" running, failed ,running,") == ["running", "failed"]


@pytest.mark.parametrize("value", [",", " , ,"])
def test_status_values_refuse_a_filter_naming_no_status(value):
    with pytest.raises(HTTPException) as exc:
        assignments_api.run_status_values(value)
    assert exc.value.status_code == 422


def test_assignment_runs_one_status_stays_an_equality():
    where = _where(assignments_api._filtered_runs_query(_user(), status="running"))
    assert "assignment_runs.status = 'running'" in where


def test_assignment_runs_several_statuses_are_an_in():
    where = _where(assignments_api._filtered_runs_query(_user(), status="running,awaiting_human"))
    assert "assignment_runs.status in ('running', 'awaiting_human')" in where


def test_flow_runs_several_statuses_map_to_the_flow_vocabulary():
    where = _where(
        flow_runs_api._global_runs_query(
            _user(), [], status_filter="running,awaiting_human,failed", search=None, ownership=None
        )
    )
    assert "flow_runs.status in ('running', 'pending', 'awaiting', 'failed')" in where


def test_flow_runs_refuse_an_unknown_status_in_a_list():
    with pytest.raises(HTTPException) as exc:
        flow_runs_api._global_runs_query(_user(), [], status_filter="running,nope", search=None, ownership=None)
    assert exc.value.status_code == 422


@pytest.mark.parametrize(
    ("flow_status", "expected"),
    [("pending", "running"), ("running", "running"), ("awaiting", "awaiting_human"), ("failed", "failed")],
)
def test_flow_statuses_count_in_the_executions_vocabulary(flow_status, expected):
    assert flow_runs_api.executions_status(flow_status) == expected


@pytest.mark.parametrize("module", [assignments_api, flow_runs_api])
def test_status_counts_is_declared_before_the_run_detail_route(module):
    paths = [r.path for r in module.router.routes if isinstance(r, APIRoute) and "GET" in r.methods]
    assert "/runs/status-counts" in paths
    assert paths.index("/runs/status-counts") < paths.index("/runs/{run_id}")


def test_activity_tab_filters_reach_the_feed_and_its_status_counts():
    """The runs Activity tabs (#9378 slice 4a) read the feed, its count and its status
    counts with one set of scopes: an assignment, bound agents, an A2A partner, scope=all."""
    wanted = {"assignment_id", "agent_ids", "a2a_integration_id", "scope", "agent_id", "review", "search"}
    for path in ("/runs/all", "/runs/count", "/runs/status-counts"):
        route = next(
            r
            for r in assignments_api.router.routes
            if isinstance(r, APIRoute) and r.path == path and "GET" in r.methods
        )
        taken = {p.alias or p.name for p in route.dependant.query_params}
        assert wanted <= taken, (path, wanted - taken)


def test_status_counts_never_takes_a_status():
    for module in (assignments_api, flow_runs_api):
        route = next(r for r in module.router.routes if isinstance(r, APIRoute) and r.path == "/runs/status-counts")
        taken = {p.alias or p.name for p in route.dependant.query_params}
        assert "status" not in taken


def _order_sql(clauses) -> list[str]:
    return [str(c.compile(dialect=postgresql.dialect())).lower() for c in clauses]


@pytest.mark.parametrize(
    ("order", "sort", "first"),
    [
        (assignments_api._run_order, "agent", "agents.name asc nulls last"),
        (assignments_api._run_order, "agent_desc", "agents.name desc nulls last"),
        (assignments_api._run_order, "status_desc", "assignment_runs.status desc"),
        (assignments_api._run_order, "longest", "assignment_runs.completed_at - assignment_runs.started_at desc nulls last"),
        (assignments_api._run_order, "shortest", "assignment_runs.completed_at - assignment_runs.started_at asc nulls last"),
        (flow_runs_api.flow_run_order, "agent", "flows.name asc nulls last"),
        (flow_runs_api.flow_run_order, "status", "flow_runs.status asc"),
        (flow_runs_api.flow_run_order, "longest", "flow_runs.completed_at - flow_runs.started_at desc nulls last"),
    ],
)
def test_sorts_of_the_executions_columns(order, sort, first):
    """Every sortable column of the Executions table orders on the server, newest first breaking ties."""
    clauses = _order_sql(order(sort))
    assert clauses[0] == first
    assert clauses[-1].endswith("id desc")


@pytest.mark.parametrize("order", [assignments_api._run_order, flow_runs_api.flow_run_order])
def test_default_sort_is_newest_first(order):
    assert _order_sql(order(None))[0].endswith("created_at desc")
    assert _order_sql(order("oldest"))[0].endswith("created_at asc")
