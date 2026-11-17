"""Company split out of Shared on the Operations lists (dev:visibility-filters-consistency).

The Operations lists (Executions, work items, shared state, worksets, files,
background tasks) read the list pages' ``?vis=`` segment, Mine | Shared |
Company | All, with the parameters they read before kept as [DEPRECATED]
aliases. These tests pin the mapping and what each segment compiles to.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.api.v1 import worksets as worksets_api
from app.api.v1.chat import _task_scope_from_vis
from app.api.v1.helpers import resolve_list_ownership
from app.models.shared_state import SharedState
from app.models.work_item import WorkItem
from app.models.workset import Workset
from app.services import shared_state as shared_state_service
from app.services import work_item as work_item_service
from app.services.ownership_scope import ownership_clause


def _sql(model, clause) -> str:
    return str(select(model.id).where(clause).compile(dialect=postgresql.dialect())).lower()


@pytest.mark.parametrize(
    ("vis", "ownership", "expected"),
    [
        ("mine", None, "mine"),
        ("shared", None, "shared_only"),
        ("company", None, "company"),
        ("all", None, None),
        # vis wins over the [DEPRECATED] alias.
        ("company", "mine", "company"),
        # Without vis the alias keeps its exact meaning.
        (None, "shared", "shared"),
        (None, "company_managed", "company_managed"),
        (None, "mine", "mine"),
        (None, None, None),
        ("", "mine", "mine"),
    ],
)
def test_vis_resolves_before_the_deprecated_ownership(vis, ownership, expected):
    assert resolve_list_ownership(vis, ownership) == expected


@pytest.mark.parametrize(("vis", "ownership"), [("min", None), (None, "everyone-ish"), (None, "shared_only")])
def test_an_unknown_value_is_a_422_never_a_dropped_filter(vis, ownership):
    with pytest.raises(HTTPException) as exc:
        resolve_list_ownership(vis, ownership)
    assert exc.value.status_code == 422


def _work_item_clause(value, uid=None):
    return ownership_clause(
        value,
        user_uuid=uid or uuid.uuid4(),
        user_column=WorkItem.user_id,
        agent_columns=(WorkItem.owner_agent_id,),
        company_column=WorkItem.is_company_managed,
    )


def test_company_is_company_managed_and_not_mine():
    sql = _sql(WorkItem, _work_item_clause("company"))
    assert "work_items.is_company_managed is true" in sql
    assert sql.count("not (") == 1


def test_shared_only_excludes_company_managed_rows():
    sql = _sql(WorkItem, _work_item_clause("shared_only"))
    assert "work_items.is_company_managed is not true" in sql
    assert sql.count("not (") == 1


def test_deprecated_shared_still_means_not_mine():
    sql = _sql(WorkItem, _work_item_clause("shared"))
    assert "is_company_managed" not in sql


def test_a_resource_without_a_company_flag_has_an_empty_company():
    company = ownership_clause(
        "company", user_uuid=uuid.uuid4(), user_column=WorkItem.user_id, agent_columns=()
    )
    shared = ownership_clause(
        "shared_only", user_uuid=uuid.uuid4(), user_column=WorkItem.user_id, agent_columns=()
    )
    assert "false" in _sql(WorkItem, company)
    assert "is_company_managed" not in _sql(WorkItem, shared)


def test_work_items_and_shared_state_pass_their_company_flag():
    uid = uuid.uuid4()
    (clause,) = work_item_service._ownership_clauses("company", uid)
    assert "work_items.is_company_managed is true" in _sql(WorkItem, clause)
    state = shared_state_service._ownership_clause("company", uid)
    assert "is_company_managed is true" in _sql(SharedState, state)


def test_worksets_read_vis_before_ownership():
    user = SimpleNamespace(id=uuid.uuid4())
    (clause,) = worksets_api._owned(user, "mine", "company")
    assert "worksets.is_company_managed is true" in _sql(Workset, clause)
    assert worksets_api._owned(user, None, "all") == []


@pytest.mark.parametrize(
    ("vis", "scope", "is_admin", "expected"),
    [
        ("mine", "mine", False, "mine"),
        # An admin's tasks that are not theirs split by the delegating agent.
        ("shared", "mine", True, "shared"),
        ("company", "mine", True, "company"),
        # A non-admin sees their own tasks only: Shared and Company are empty.
        ("shared", "mine", False, None),
        ("company", "mine", False, None),
        # The [DEPRECATED] scope never reaches the new segments.
        (None, "shared", True, "mine"),
        ("all", "mine", True, "all"),
        # All is what a non-admin can see, their own tasks, never a 403.
        ("all", "mine", False, "mine"),
        (None, "mine", False, "mine"),
        (None, "all", True, "all"),
    ],
)
def test_background_tasks_vis(vis, scope, is_admin, expected):
    assert _task_scope_from_vis(vis, scope, is_admin) == expected


def test_background_tasks_deprecated_scope_all_keeps_its_admin_gate():
    with pytest.raises(HTTPException) as exc:
        _task_scope_from_vis(None, "all", False)
    assert exc.value.status_code == 403
