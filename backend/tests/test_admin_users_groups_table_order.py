"""The Users, Groups and group members tables in server mode (#9378 slice 6z, #11940).

``GET /users``, ``GET /groups`` and ``GET /groups/{id}/members`` grow optional
``sort`` / ``direction`` and multi-value filters; ``id`` breaks every tie so
offset pages never overlap, and an unknown or missing column keeps the order
of the full list (by name), so a caller that passes nothing gets what it got.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.api.v1 import groups, users
from app.models.group import Group
from app.models.user import User


def _sql(clauses) -> list[str]:
    return [str(c.compile(dialect=postgresql.dialect())) for c in clauses]


def _where(model, clauses) -> str:
    query = select(model.id).where(*clauses)
    return str(query.compile(dialect=postgresql.dialect()))


def test_the_offered_columns_are_the_tables():
    assert tuple(users.USER_SORTS) == ("name", "email", "role", "status", "sign_in", "created")
    assert tuple(groups.group_sorts()) == ("name", "description", "kind", "members", "created")
    assert tuple(groups.MEMBER_SORTS) == ("name", "email", "role", "added")


@pytest.mark.parametrize("sort", [None, "", "password", "name; drop"])
def test_an_unknown_column_keeps_the_full_list_order(sort):
    assert _sql(users.user_order(sort, "desc")) == [
        "lower(users.full_name) ASC",
        "users.email ASC",
        "users.id ASC",
    ]
    assert _sql(groups.group_order(sort, "desc")) == ["lower(groups.name) ASC", "groups.id ASC"]
    assert _sql(groups.member_order(sort, "desc"))[-1] == "group_members.id ASC"


@pytest.mark.parametrize(
    ("sort", "fragment"),
    [
        ("name", "lower(users.full_name)"),
        ("email", "lower(users.email)"),
        ("role", "users.role"),
        ("status", "users.is_active"),
        ("created", "users.created_at"),
    ],
)
def test_each_user_column_orders_by_itself_then_the_id(sort, fragment):
    primary, tie = _sql(users.user_order(sort, "desc"))
    assert primary == f"{fragment} DESC"
    assert tie == "users.id DESC"


@pytest.mark.parametrize("sort", ["integrations", "mcp_servers", "agents", "knowledge_bases"])
def test_each_count_column_orders_by_its_count(sort):
    primary, tie = _sql(users.user_order(sort, "desc"))
    assert primary.startswith("(SELECT count(") and primary.endswith("DESC")
    assert tie == "users.id DESC"


def test_the_created_filter_keeps_the_widest_window():
    from datetime import UTC, datetime

    (clause,) = users.user_filters(None, None, None, None, "24h,30d,bogus", now=datetime(2026, 11, 15, tzinfo=UTC))
    assert clause.right.value == datetime(2026, 10, 16, tzinfo=UTC)


def test_any_direction_but_desc_is_ascending():
    primary, _tie = _sql(users.user_order("role", "sideways"))
    assert primary.endswith("ASC")


def test_groups_sort_on_the_member_count():
    primary, tie = _sql(groups.group_order("members", "desc"))
    assert "count(group_members.id)" in primary and primary.endswith("DESC")
    assert tie == "groups.id DESC"


@pytest.mark.parametrize(
    ("raw", "values"),
    [(None, []), ("", []), ("admin", ["admin"]), (" admin , ,user ", ["admin", "user"])],
)
def test_a_filter_takes_one_value_or_several(raw, values):
    assert users.filter_values(raw) == values
    assert groups.filter_values(raw) == values


def test_no_parameter_filters_nothing():
    assert users.user_filters(None, None, None, None) == []
    assert groups.group_filters(None, None, None) == []


def test_user_filters_read_search_role_status_and_group():
    sql = _where(User, users.user_filters(" ada ", "admin,user", "active", "none"))
    assert "users.full_name ILIKE" in sql and "users.email ILIKE" in sql and "users.title ILIKE" in sql
    assert "users.role IN" in sql
    assert "users.is_active IS true" in sql
    assert "NOT IN (SELECT group_members.user_id" in sql


def test_both_statuses_filter_nothing():
    assert users.user_filters(None, None, "active,disabled", None) == []


def test_a_group_filter_of_no_valid_id_matches_no_user():
    sql = _where(User, users.user_filters(None, None, None, "not-a-uuid"))
    assert "users.id IS NULL" in sql


def test_group_filters_read_kind_and_members():
    sql = _where(Group, groups.group_filters("ops", "auto,access", "empty"))
    assert "groups.name ILIKE" in sql and "groups.description ILIKE" in sql
    assert "groups.kind IN" in sql and "groups.kind IS NULL" in sql
    assert "count(group_members.id)" in sql
