"""Regression coverage: group-shared agent runs stay owner-private.

A group-shared agent is read-only-visible to every group member (they can
browse its config and assignments), but its *runs* must stay private to the
run owner. The run-detail / descendants / respond / dismiss / followup
endpoints already scope to ``own + company-managed`` (so a group member
clicking a shared agent's run gets 404 "Run not found"), yet the run
list / count / notification surfaces used to widen visibility to every
group member via ``shared_group_ids`` overlap. That made the notification
dropdown and the runs list show entries that 404 the moment they are
opened, and leaked one member's runs to the whole group.

These tests pin the run query to ``own + company-managed`` only — never a
group-share predicate — so list / count / notifications match the rest of
the run endpoints. Company-managed runs stay visible to all (read-only).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.api.v1.assignments import _filtered_runs_query
from app.api.v1.helpers import run_visible_filter


def _user():
    return SimpleNamespace(id=uuid.uuid4(), is_admin=False)


def test_run_visible_filter_is_own_plus_company_managed_only():
    """The single source of truth for run visibility must scope to own +
    company-managed and must NOT reference the group-share overlap."""
    sql = str(run_visible_filter(uuid.uuid4()))
    assert "assignment_runs.user_id =" in sql
    assert "assignment_runs.is_company_managed" in sql
    assert "shared_group_ids" not in sql


def test_all_ownership_scopes_to_own_plus_company_managed():
    """Default ("all") visibility = own runs OR company-managed runs.

    The ownership predicate must be a UNION (OR) of the two tiers, not an
    intersection — an accidental AND would hide every personal run that is
    not also company-managed (i.e. all of them), silently breaking the
    runs list. Pin the OR explicitly so a flip to AND fails here.
    """
    sql = str(_filtered_runs_query(_user()))
    assert "assignment_runs.user_id =" in sql
    # OR (not AND) between the owner and company-managed tiers.
    assert "OR assignment_runs.is_company_managed IS true" in sql
    # The group-share predicate must NEVER appear on a run query.
    assert "shared_group_ids" not in sql


def test_shared_ownership_is_company_managed_not_mine():
    """The "shared" filter resolves to company-managed runs NOT owned by me.

    My own company-managed runs move to "mine"; "shared" keeps the
    company-managed runs owned by someone else — including anonymous-triggered
    runs whose ``user_id`` is NULL. Group-shared agent runs stay excluded.
    """
    sql = str(_filtered_runs_query(_user(), ownership="shared"))
    assert "assignment_runs.is_company_managed IS true" in sql
    # Not attributed to me, and NULL (anonymous) owners count as "not mine".
    assert "assignment_runs.user_id !=" in sql
    assert "assignment_runs.user_id IS NULL" in sql
    assert "shared_group_ids" not in sql


def test_company_managed_ownership_is_company_managed_not_mine():
    sql = str(_filtered_runs_query(_user(), ownership="company_managed"))
    assert "assignment_runs.is_company_managed IS true" in sql
    assert "assignment_runs.user_id !=" in sql
    assert "assignment_runs.user_id IS NULL" in sql
    assert "shared_group_ids" not in sql


def test_mine_ownership_includes_my_company_managed_runs():
    """The "mine" filter = every run attributed to me by ``user_id``.

    It must scope to ``user_id == me`` and must NOT pin ``is_company_managed``
    to FALSE: an agent owner's own company-managed agent runs (stamped with the
    owner's ``user_id``) and company-managed runs I triggered are genuinely
    "mine" and must surface under "My Runs" and the dashboard Live Activity.
    The old ``is_company_managed IS false`` guard wrongly hid them.
    """
    sql = str(_filtered_runs_query(_user(), ownership="mine"))
    assert "assignment_runs.user_id =" in sql
    # The removed guard must be gone — its presence would re-hide owner runs.
    assert "assignment_runs.is_company_managed IS false" not in sql
    assert "shared_group_ids" not in sql


def test_no_ownership_branch_references_group_sharing():
    """Belt-and-braces: none of the ownership branches may leak the
    group-share overlap into the runs query."""
    for ownership in (None, "all", "mine", "shared", "company_managed"):
        sql = str(_filtered_runs_query(_user(), ownership=ownership))
        assert "shared_group_ids" not in sql, ownership


def test_mine_and_shared_are_a_disjoint_partition_keyed_on_owner():
    """"mine" and "shared" partition the visible set by the run owner.

    "mine" keys on ``user_id == me`` (no company-managed exclusion) while
    "shared" keys on ``is_company_managed`` AND a not-me owner. The two are
    disjoint (a run is mine xor shared-not-mine) and their union is the "all"
    visibility (own OR company-managed), so an owner's company-managed run shows
    under "mine" and never disappears from the union.
    """
    user = _user()
    mine = str(_filtered_runs_query(user, ownership="mine"))
    shared = str(_filtered_runs_query(user, ownership="shared"))

    # "mine" is a pure owner scope — the company-managed exclusion is gone.
    assert "assignment_runs.user_id =" in mine
    assert "assignment_runs.is_company_managed IS false" not in mine

    # "shared" is the complementary slice: company-managed but not mine.
    assert "assignment_runs.is_company_managed IS true" in shared
    assert "assignment_runs.user_id !=" in shared
