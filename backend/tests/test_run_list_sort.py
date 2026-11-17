"""The ``sort`` parameter of the assignment runs lists (#9365, UX round 1 B4 on #9369)."""

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.api.v1 import assignments
from app.api.v1.assignments import _RUN_SORT_PATTERN, _run_order


def _sql(sort: str | None) -> list[str]:
    return [str(c.compile(dialect=postgresql.dialect())) for c in _run_order(sort)]


def test_default_order_is_newest_first_with_an_id_tiebreak():
    assert _sql(None) == _sql("newest")
    assert _sql(None) == ["assignment_runs.created_at DESC", "assignment_runs.id DESC"]


def test_oldest_reverses_the_started_date():
    assert _sql("oldest") == ["assignment_runs.created_at ASC", "assignment_runs.id ASC"]


def test_status_orders_by_status_then_newest():
    assert _sql("status") == [
        "assignment_runs.status ASC",
        "assignment_runs.created_at DESC",
        "assignment_runs.id DESC",
    ]


@pytest.mark.parametrize("path", ["/runs/all", "/{assignment_id}/runs"])
def test_both_runs_lists_take_the_sort_parameter(path):
    route = next(
        r for r in assignments.router.routes if isinstance(r, APIRoute) and r.path.endswith(path) and "GET" in r.methods
    )
    names = {p.name for p in route.dependant.query_params}
    assert "sort" in names


def test_an_unknown_sort_is_refused_by_the_pattern():
    from pydantic import BaseModel, Field

    class _Q(BaseModel):
        sort: str = Field(pattern=_RUN_SORT_PATTERN)

    for ok in ("newest", "oldest", "status"):
        _Q(sort=ok)
    with pytest.raises(ValidationError):
        _Q(sort="name")
