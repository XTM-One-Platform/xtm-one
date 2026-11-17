"""The work items of a fleet: one population for every count.

The fleet strip read the items filed in the fleet's workspace only, so a fleet
whose members own open items filed nowhere showed "0 open work items" beside
a members panel counting them. ``fleet_work_item_clause`` is the one
definition: filed in the hosting workspace, or owned or created by a current
home member.
"""

import uuid

from sqlalchemy.dialects import postgresql

from app.services.work_item import fleet_member_attribution, fleet_work_item_clause, fold_fleet_counts


def _sql(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_a_fleet_counts_what_its_workspace_holds_and_what_its_members_own_or_created() -> None:
    workspace_id = uuid.uuid4()
    member = uuid.uuid4()
    sql = _sql(fleet_work_item_clause([member], workspace_id))
    assert f"work_items.workspace_id = '{workspace_id}'" in sql
    assert f"work_items.owner_agent_id IN ('{member}')" in sql
    assert f"work_items.created_by_agent_id IN ('{member}')" in sql
    assert sql.count(" OR ") == 2


def test_a_fleet_without_members_counts_its_workspace_only() -> None:
    workspace_id = uuid.uuid4()
    sql = _sql(fleet_work_item_clause([], workspace_id))
    assert sql == f"work_items.workspace_id = '{workspace_id}'"


def test_nothing_to_resolve_matches_nothing() -> None:
    assert _sql(fleet_work_item_clause([], None)) == "false"


def test_an_item_counts_for_its_member_owner_else_its_member_creator() -> None:
    """An owner-less item created by a member counts for that member,
    so the members panel sees what the strip counts."""
    member = uuid.uuid4()
    sql = _sql(fleet_member_attribution([member]))
    owner_at = sql.index(f"WHEN (work_items.owner_agent_id IN ('{member}')) THEN work_items.owner_agent_id")
    creator_at = sql.index(
        f"WHEN (work_items.created_by_agent_id IN ('{member}')) THEN work_items.created_by_agent_id"
    )
    assert owner_at < creator_at, "the owner wins over the creator"
    assert " ELSE " not in sql, "anything else is NULL: the unattributed group"


def test_without_members_every_item_is_unattributed() -> None:
    assert _sql(fleet_member_attribution([])) == "CASE WHEN false THEN work_items.owner_agent_id END"


def test_the_strip_equals_the_members_rows_plus_the_unattributed_filed_items() -> None:
    """The fleet strip and the Members panel come from the same grouped rows:
    the total is the sum of every member row plus the items filed in the
    workspace that no member owns or created (the ``None`` group)."""
    owner, creator, idle = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rows = [(owner, 3, 1), (creator, 2, 0), (None, 4, 2)]
    per_member, total = fold_fleet_counts(rows)
    assert per_member == {owner: (3, 1), creator: (2, 0)}
    assert idle not in per_member
    filed_without_member = 4
    assert total == sum(open_ for open_, _ in per_member.values()) + filed_without_member == 9


def test_no_rows_is_an_empty_fleet() -> None:
    assert fold_fleet_counts([]) == ({}, 0)
