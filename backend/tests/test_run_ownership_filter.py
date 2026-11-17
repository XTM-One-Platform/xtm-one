"""The Executions feeds refuse an unknown ``ownership`` filter.

Both run feeds used to recognise ``mine`` and ``shared``/``company_managed``
and let anything else fall through every branch, which applied NO filter:
``?ownership=min`` returned everybody's runs, and a bulk "delete all matching"
with a typo'd ownership widened to every visible matching run. Every case here
is refused before the database is touched, so these tests need no database.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import assignments as assignments_api
from app.api.v1.assignments import _filtered_runs_query
from app.api.v1.flow_runs import list_all_flow_runs
from app.api.v1.helpers import RUN_OWNERSHIP_FILTERS, validate_run_ownership
from app.api.v1.schemas.assignment import BulkDeleteRunsRequest

BAD_VALUES = ["min", "Mine", "everyone", " mine"]


class _NoDB:
    """A session that fails the test if the handler reaches the database."""

    def __getattr__(self, name):
        raise AssertionError(f"the database was touched ({name}) before validation")


def _user(role: str = "user"):
    return SimpleNamespace(id=uuid.uuid4(), is_admin=role == "admin", role=role)


def _assert_refused(exc: pytest.ExceptionInfo, value: str) -> None:
    assert exc.value.status_code == 422
    assert f"`{value}`" in exc.value.detail


def test_the_vocabulary_is_what_the_feeds_accept():
    assert {"all", "mine", "shared", "company_managed"} == RUN_OWNERSHIP_FILTERS
    assert validate_run_ownership(None) is None
    assert validate_run_ownership("") is None
    assert validate_run_ownership("all") is None
    for value in ("mine", "shared", "company_managed"):
        assert validate_run_ownership(value) == value


@pytest.mark.parametrize("value", BAD_VALUES)
def test_an_unknown_value_is_a_422(value):
    with pytest.raises(HTTPException) as exc:
        validate_run_ownership(value)
    _assert_refused(exc, value)


@pytest.mark.parametrize("value", BAD_VALUES)
def test_the_assignment_query_builder_refuses_an_unknown_value(value):
    # Shared by the list, the count and the bulk delete.
    with pytest.raises(HTTPException) as exc:
        _filtered_runs_query(_user(), ownership=value)
    _assert_refused(exc, value)


def test_all_is_the_same_query_as_no_filter():
    user = _user()
    assert str(_filtered_runs_query(user, ownership="all")) == str(
        _filtered_runs_query(user, ownership=None)
    )


@pytest.mark.asyncio
async def test_the_flow_run_feed_refuses_before_touching_the_database():
    with pytest.raises(HTTPException) as exc:
        await list_all_flow_runs(
            limit=20,
            offset=0,
            status_filter=None,
            search=None,
            ownership="min",
            db=_NoDB(),
            user=_user(),
        )
    _assert_refused(exc, "min")


@pytest.fixture
def _a2a_scope_must_not_run(monkeypatch):
    async def _scope(_db, _user, _integration_id):
        raise AssertionError("the A2A scope was resolved before the filter was validated")

    monkeypatch.setattr(assignments_api, "_a2a_scope_for", _scope)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_a2a_scope_must_not_run")
@pytest.mark.parametrize("a2a_integration_id", [None, str(uuid.uuid4())], ids=["no-integration", "an-integration"])
async def test_the_assignment_run_list_and_count_refuse_an_unknown_value(
    a2a_integration_id,
):
    # The A2A variant resolves a scope that reads the database; the filter is
    # refused before it, the same way on every variant.
    with pytest.raises(HTTPException) as exc:
        await assignments_api.list_all_runs(
            limit=50,
            offset=0,
            ownership="min",
            a2a_integration_id=a2a_integration_id,
            user=_user(),
            db=_NoDB(),
        )
    _assert_refused(exc, "min")

    with pytest.raises(HTTPException) as exc:
        await assignments_api.count_runs(
            ownership="min",
            a2a_integration_id=a2a_integration_id,
            user=_user(),
            db=_NoDB(),
        )
    _assert_refused(exc, "min")


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["user", "admin"])
async def test_a_bulk_delete_with_an_unknown_ownership_deletes_nothing(role):
    body = BulkDeleteRunsRequest(all_matching_filters=True, ownership="sharred")
    with pytest.raises(HTTPException) as exc:
        await assignments_api.bulk_delete_runs(body, user=_user(role), db=_NoDB())
    _assert_refused(exc, "sharred")
