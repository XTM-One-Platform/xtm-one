"""The Audit logs table's server sort and multi-value filters (#9378 slice 6y, #11936).

``GET /audit-logs?sort=&direction=`` orders the page by a column of the table
(time, action, entity, user), ``id`` breaking every tie so offset pages never
overlap; an unknown or missing column keeps the newest-first order. ``action``,
``entity_type`` and ``user_id`` take one value or several comma-separated.
"""

import pytest
from sqlalchemy.dialects import postgresql

from app.api.v1 import audit_logs


def _sql(clauses) -> list[str]:
    return [str(c.compile(dialect=postgresql.dialect())) for c in clauses]


def test_the_offered_columns_are_the_table_s():
    assert tuple(audit_logs.AUDIT_LOG_SORTS) == ("time", "action", "entity", "user")


@pytest.mark.parametrize("sort", [None, "", "summary", "created_at; drop"])
def test_an_unknown_column_keeps_newest_first(sort):
    primary, tie = _sql(audit_logs.audit_log_order(sort, None))
    assert primary == "audit_logs.created_at DESC"
    assert tie == "audit_logs.id DESC"


@pytest.mark.parametrize(
    ("sort", "fragment"),
    [
        ("time", "audit_logs.created_at"),
        ("action", "audit_logs.action"),
        ("entity", "audit_logs.entity_type"),
        ("user", "lower(audit_logs.user_email)"),
    ],
)
def test_each_column_orders_by_itself_then_the_id(sort, fragment):
    primary, tie = _sql(audit_logs.audit_log_order(sort, "asc"))
    assert primary == f"{fragment} ASC"
    assert tie == "audit_logs.id ASC"


def test_any_direction_but_asc_is_descending():
    primary, _tie = _sql(audit_logs.audit_log_order("action", "sideways"))
    assert primary.endswith("DESC")


@pytest.mark.parametrize(
    ("raw", "values"),
    [
        (None, []),
        ("", []),
        ("create", ["create"]),
        ("create,delete", ["create", "delete"]),
        (" create , ,delete ", ["create", "delete"]),
    ],
)
def test_a_filter_takes_one_value_or_several(raw, values):
    assert audit_logs.filter_values(raw) == values
