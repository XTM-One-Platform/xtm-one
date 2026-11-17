"""The Operations table's server sort of the work items list (#9378 slice 8d).

``GET /work-items?order=&direction=`` orders the page by a column of the table
(created, updated, due, priority, status, owner, completed), ``id`` breaking every
tie so offset pages never overlap; an unknown or missing column keeps ``sort``.
"""

import pytest
from sqlalchemy.dialects import postgresql

from app.services import work_item as work_item_service


def _sql(clauses) -> list[str]:
    return [str(c.compile(dialect=postgresql.dialect())) for c in clauses]


@pytest.mark.parametrize("order", [None, "", "title", "created_desc", "id; drop"])
def test_an_unknown_column_keeps_the_sort_order(order):
    assert work_item_service.table_order(order, "asc") is None


def test_the_offered_columns_are_the_table_s():
    assert work_item_service.WORK_ITEM_TABLE_ORDERS == (
        "created",
        "updated",
        "due",
        "priority",
        "status",
        "owner",
        "completed",
    )


@pytest.mark.parametrize(
    ("order", "fragment"),
    [
        ("created", "work_items.created_at"),
        ("updated", "work_items.updated_at"),
        ("due", "work_items.due_at"),
        ("completed", "work_items.completed_at"),
        ("priority", "CASE work_items.priority"),
        ("status", "CASE work_items.status"),
        ("owner", "lower(agents.name)"),
    ],
)
def test_each_column_orders_by_itself_then_the_id(order, fragment):
    primary, tie = _sql(work_item_service.table_order(order, "asc"))
    assert fragment in primary
    assert primary.endswith("ASC NULLS LAST")
    assert tie == "work_items.id ASC"


def test_descending_is_the_default_direction():
    primary, tie = _sql(work_item_service.table_order("due", None))
    assert primary.endswith("DESC NULLS LAST")
    assert tie == "work_items.id DESC"


def test_status_ranks_follow_the_life_of_an_item():
    (primary, _tie) = work_item_service.table_order("status", "asc")
    compiled = primary.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    text = str(compiled)
    positions = [text.index(f"'{s}'") for s in ("open", "claimed", "in_progress", "blocked", "done", "cancelled")]
    assert positions == sorted(positions)
