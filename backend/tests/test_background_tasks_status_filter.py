"""The Status filter and the server sorts of Operations > Background Tasks (#9378 slice 8e).

The Status pill on the Background Tasks toolbar is multi-select: the list, its
count, its segment counts and the bulk delete take ``status`` as one value or
several comma-separated, and ``/sub-agent-tasks/status-counts`` answers how many
tasks each status holds under the other filters. The table sorts on the server:
created, last progress, status, from (the delegating agent) and to (the target).
These tests pin the SQL of the filter and of each order, the 422 on an empty or
unknown status and the routes' declaration order.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.api.v1 import chat as chat_api
from app.models.sub_agent_task import SubAgentTask
from app.services import sub_agent

_ADMIN = {"requester_id": "00000000-0000-0000-0000-000000000001", "is_admin": True, "scope": "all"}


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})).lower()


def _query(status: str | None):
    return sub_agent._background_tasks_query(status=status, agent_id=None, **_ADMIN)


def test_statuses_split_trim_and_dedupe():
    assert sub_agent.background_task_statuses(None) == []
    assert sub_agent.background_task_statuses("running") == ["running"]
    assert sub_agent.background_task_statuses(" running, pending ,running,") == ["running", "pending"]


@pytest.mark.parametrize("value", [",", " , ,", "running,nope", "zombie"])
def test_statuses_refuse_an_empty_or_unknown_value(value):
    with pytest.raises(ValueError):
        sub_agent.background_task_statuses(value)


@pytest.mark.parametrize("value", [",", "running,nope"])
def test_endpoints_answer_an_empty_or_unknown_status_as_a_422(value):
    with pytest.raises(HTTPException) as exc:
        chat_api._checked_task_status(value)
    assert exc.value.status_code == 422


def test_endpoints_pass_a_clean_status_list_through():
    assert chat_api._checked_task_status(None) is None
    assert chat_api._checked_task_status("running, pending") == "running,pending"


def test_one_status_stays_an_equality():
    assert "sub_agent_tasks.status = 'running'" in _sql(_query("running"))


def test_several_statuses_are_an_in():
    assert "sub_agent_tasks.status in ('running', 'pending')" in _sql(_query("running,pending"))


def test_no_status_adds_no_status_clause():
    # An admin's All has no WHERE at all without a status.
    assert _query(None).whereclause is None


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        (None, "order by sub_agent_tasks.created_at desc, sub_agent_tasks.id desc"),
        ("created", "order by sub_agent_tasks.created_at desc, sub_agent_tasks.id desc"),
        ("created_asc", "order by sub_agent_tasks.created_at asc, sub_agent_tasks.id asc"),
        ("status", "order by sub_agent_tasks.status asc, sub_agent_tasks.created_at desc"),
        ("status_desc", "order by sub_agent_tasks.status desc, sub_agent_tasks.created_at desc"),
        ("from", "order by from_agent.name asc nulls last"),
        ("from_desc", "order by from_agent.name desc nulls last"),
        ("to", "order by to_agent.name asc nulls last"),
        ("to_desc", "order by to_agent.name desc nulls last"),
        (
            "last_progress",
            "sub_agent_tasks.completed_at, sub_agent_tasks.started_at, sub_agent_tasks.created_at) desc, "
            "sub_agent_tasks.id desc",
        ),
        (
            "last_progress_asc",
            "sub_agent_tasks.completed_at, sub_agent_tasks.started_at, sub_agent_tasks.created_at) asc, "
            "sub_agent_tasks.id asc",
        ),
        ("unknown", "order by sub_agent_tasks.created_at desc, sub_agent_tasks.id desc"),
    ],
)
def test_each_sort_orders_on_the_server(sort, expected):
    assert expected in _sql(sub_agent._order_background_tasks(select(SubAgentTask), sort))


def test_the_updated_sort_reads_the_time_the_cell_shows() -> None:
    """The Updated column shows an open task's execution move, else its end,
    start or creation; both directions order on that same value."""
    sql = _sql(sub_agent._order_background_tasks(select(SubAgentTask), "last_progress_asc"))
    assert (
        "order by coalesce(case when (sub_agent_tasks.status in ('pending', 'running')) "
        "then task_execution.updated_at end, sub_agent_tasks.completed_at"
    ) in sql
    assert "nulls last" not in sql


@pytest.mark.parametrize("sort", ["from", "to_desc"])
def test_an_agent_name_sort_keeps_the_tasks_of_a_deleted_agent(sort):
    assert "left outer join agents as" in _sql(sub_agent._order_background_tasks(select(SubAgentTask), sort))


def test_every_declared_sort_is_accepted_by_the_list_endpoint():
    route = next(
        r
        for r in chat_api.router.routes
        if isinstance(r, APIRoute) and r.path == "/sub-agent-tasks" and "GET" in r.methods
    )
    sort_param = next(p for p in route.dependant.query_params if p.name == "sort")
    assert set(sort_param.field_info.annotation.__args__) == set(sub_agent.BACKGROUND_TASK_SORTS)


def test_status_counts_route_is_declared_before_the_task_id_route():
    paths = [r.path for r in chat_api.router.routes if isinstance(r, APIRoute) and "GET" in r.methods]
    assert paths.index("/sub-agent-tasks/status-counts") < paths.index("/sub-agent-tasks/{task_id}")
