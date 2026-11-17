"""The Operations table's server sort of the shared-state list (#9378 slice 8c).

``GET /shared-state?sort=&direction=`` orders the page by key, owner name,
update time or value size, ``id`` breaking every tie so offset pages never
overlap; an unknown or missing sort keeps the list's default order.
"""

import pytest
from sqlalchemy.dialects import postgresql

from app.services import shared_state as shared_state_service


def _sql(clauses) -> list[str]:
    return [str(c.compile(dialect=postgresql.dialect())) for c in clauses]


@pytest.mark.parametrize("sort", [None, "", "value", "created", "id; drop"])
def test_an_unknown_sort_keeps_the_default_order(sort):
    assert shared_state_service._list_order(sort, "asc") is None


def test_the_offered_sorts_are_the_four_of_the_table():
    assert shared_state_service.LIST_SORTS == ("key", "owner", "updated", "size")


@pytest.mark.parametrize(
    ("sort", "fragment"),
    [
        ("key", "shared_states.key"),
        ("updated", "shared_states.updated_at"),
        ("size", "length(CAST(shared_states.value AS TEXT))"),
        ("owner", "lower(agents.name)"),
    ],
)
def test_each_sort_orders_by_its_column_then_the_id(sort, fragment):
    primary, tie = _sql(shared_state_service._list_order(sort, "asc"))
    assert fragment in primary
    assert primary.endswith("ASC NULLS LAST")
    assert tie == "shared_states.id ASC"


def test_descending_is_the_default_direction():
    primary, tie = _sql(shared_state_service._list_order("key", None))
    assert primary.endswith("DESC NULLS LAST")
    assert tie == "shared_states.id DESC"
