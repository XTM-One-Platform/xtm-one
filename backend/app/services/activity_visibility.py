# SPDX-License-Identifier: Apache-2.0

"""Hide the activity of a company-managed agent or fleet from non-admins (#11624).

``hide_activity_from_non_admins`` on an ``Agent`` or a ``Fleet`` (default
false everywhere, built-in and seeded fleets included) takes the company-wide
tier away from a non-administrator for that agent's activity:

* runs (``AssignmentRun``) and traces (``LLMTrace``) of the agent;
* work items and shared state the agent owns or created, when they sit in
  the company-wide (unfiled) tier;
* for a fleet, the same for every HOME member agent, plus every work item and
  shared state row filed in the fleet's workspace (the workspace-to-fleet
  rule: ``fleets.workspace_id`` names the fleet's coordination workspace, so
  the fleet's flag governs what is filed there).

Who still sees it: an administrator (``users.role = 'admin'``), and the owner
of the agent or fleet (``user_id``), whose fleet runs as them and must keep
reading its own coordination. A person's OWN rows (``user_id`` = them) are
never hidden from them: the flag narrows the company-wide and workspace tiers
only.

Every clause here is evaluated in SQL with the caller's id, so a call site
keeps its signature and an administrator is decided by the same row the API
reads. With every flag off (the default) the hidden sets are empty and each
clause is true for every row: no behaviour changes.
"""

from __future__ import annotations

import uuid

from sqlalchemy import and_, false, not_, or_, select

from app.models.agent import Agent
from app.models.fleet import Fleet, FleetMember
from app.models.user import User


def caller_is_admin_clause(user_id: uuid.UUID | None):
    """True in SQL when *user_id* is an administrator; false for no caller."""
    if user_id is None:
        return false()
    return select(User.id).where(User.id == user_id, User.role == "admin").exists()


def hidden_fleet_ids(user_id: uuid.UUID | None):
    """Fleets whose activity *user_id* must not see (before the admin check)."""
    query = select(Fleet.id).where(Fleet.hide_activity_from_non_admins.is_(True))
    if user_id is not None:
        query = query.where(Fleet.user_id.is_distinct_from(user_id))
    return query


def hidden_agent_ids(user_id: uuid.UUID | None):
    """Agents whose activity *user_id* must not see (before the admin check).

    The agent's own flag, or the flag of its HOME fleet; the owner of the
    agent keeps it.
    """
    in_hidden_fleet = select(FleetMember.agent_id).where(
        FleetMember.role == "member",
        FleetMember.agent_id.is_not(None),
        FleetMember.fleet_id.in_(hidden_fleet_ids(user_id)),
    )
    query = select(Agent.id).where(
        or_(
            Agent.hide_activity_from_non_admins.is_(True),
            Agent.id.in_(in_hidden_fleet),
        )
    )
    if user_id is not None:
        query = query.where(Agent.user_id.is_distinct_from(user_id))
    return query


def hidden_workspace_ids(user_id: uuid.UUID | None):
    """Coordination workspaces of the fleets *user_id* must not see into."""
    query = select(Fleet.workspace_id).where(Fleet.hide_activity_from_non_admins.is_(True))
    if user_id is not None:
        query = query.where(Fleet.user_id.is_distinct_from(user_id))
    return query


def agent_activity_shown(agent_column, user_id: uuid.UUID | None):
    """True when the activity of the agent in *agent_column* is shown to *user_id*.

    A NULL agent (no agent behind the row) is never hidden.
    """
    return or_(
        agent_column.is_(None),
        not_(agent_column.in_(hidden_agent_ids(user_id))),
        caller_is_admin_clause(user_id),
    )


def agents_activity_shown(user_id: uuid.UUID | None, *agent_columns):
    """``agent_activity_shown`` for every column (a row with several agents)."""
    return or_(
        and_(
            *(
                or_(column.is_(None), not_(column.in_(hidden_agent_ids(user_id))))
                for column in agent_columns
            )
        ),
        caller_is_admin_clause(user_id),
    )


def workspace_activity_shown(workspace_column, user_id: uuid.UUID | None):
    """True when the rows filed in the workspace of *workspace_column* are shown."""
    return or_(
        workspace_column.is_(None),
        not_(workspace_column.in_(hidden_workspace_ids(user_id))),
        caller_is_admin_clause(user_id),
    )
