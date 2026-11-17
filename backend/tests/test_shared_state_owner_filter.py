"""The owner-agent filter of Operations > Shared State.

``owner_agent_id`` on ``GET /shared-state``, ``/count`` and ``/segment-counts``
narrows to the entries one agent owns, composed with ``workspace`` and
``fleet``; ``GET /shared-state/owners`` is what the picker offers. These tests
pin the clause, the 422 on a malformed id, the route declarations (the
literal ``/owners`` before ``/{state_id}``), and the grouped count's query.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.api.v1 import shared_state as shared_state_api
from app.services import shared_state as shared_state_service


def _sql(clause) -> str:
    return str(
        clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    ).lower()


AGENT = uuid.uuid4()
FLEET = uuid.uuid4()


def test_owner_scope_keeps_one_agent():
    sql = _sql(shared_state_api._workspace_scope(None, None, str(AGENT)))
    assert "shared_states.owner_agent_id =" in sql
    assert AGENT.hex in sql.replace("-", "")
    assert "fleet_members" not in sql


def test_owner_scope_composes_with_the_fleet():
    sql = _sql(shared_state_api._workspace_scope(None, str(FLEET), str(AGENT)))
    assert "fleet_members" in sql
    assert " and " in sql
    assert AGENT.hex in sql.replace("-", "")


def test_no_owner_adds_nothing():
    assert shared_state_api._workspace_scope(None, None, None) is None
    assert shared_state_api._workspace_scope(None, None, "") is None


@pytest.mark.parametrize("value", ["not-a-uuid", "123", "none"])
def test_owner_scope_refuses_a_malformed_id(value):
    with pytest.raises(HTTPException) as exc:
        shared_state_api._workspace_scope(None, None, value)
    assert exc.value.status_code == 422
    assert "owner_agent_id" in str(exc.value.detail)


@pytest.mark.parametrize(
    "route",
    [
        shared_state_api.list_shared_state,
        shared_state_api.count_shared_state,
        shared_state_api.shared_state_segment_counts,
    ],
)
def test_lists_and_counts_take_owner_agent_id(route):
    parameter = inspect.signature(route).parameters["owner_agent_id"]
    # A plain None default: routes are also called as functions.
    assert parameter.default is None


def test_owners_route_takes_the_list_filters_but_not_the_owner():
    params = inspect.signature(shared_state_api.list_shared_state_owners).parameters
    for name in ("search", "status", "stale_only", "ownership", "vis", "workspace", "fleet"):
        assert params[name].default in (None, False)
    # The picker keeps every owner on offer once one is chosen.
    assert "owner_agent_id" not in params


def test_owners_route_is_declared_before_the_id_route():
    paths = [getattr(r, "path", "") for r in shared_state_api.router.routes]
    assert "/owners" in paths
    assert paths.index("/owners") < paths.index("/{state_id}")


@pytest.mark.asyncio
async def test_owners_route_describes_the_grouped_counts():
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False, role="user")
    counts = [(str(AGENT), 3), (None, 1)]
    described = [{"agent": {"id": str(AGENT)}, "count": 3}, {"agent": None, "count": 1}]
    with (
        patch.object(
            shared_state_service, "count_states_by_owner", AsyncMock(return_value=counts)
        ) as grouped,
        patch(
            "app.services.work_item_context.describe_owners", AsyncMock(return_value=described)
        ) as describe,
    ):
        out = await shared_state_api.list_shared_state_owners(
            search=None,
            status="active",
            stale_only=False,
            ownership=None,
            vis=None,
            workspace=None,
            fleet=str(FLEET),
            user=user,
        )
    assert out == described
    kwargs = grouped.await_args.kwargs
    assert kwargs["status"] == "active"
    assert "fleet_members" in _sql(kwargs["scope"])
    describe.assert_awaited_once_with(counts, viewer=user)


@pytest.mark.asyncio
async def test_count_by_owner_skips_the_database_when_staleness_is_off():
    with patch.object(shared_state_service, "_stale_clause", return_value=None):
        assert await shared_state_service.count_states_by_owner(user_id=None, stale_only=True) == []
