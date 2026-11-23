# SPDX-License-Identifier: Apache-2.0

"""The ownership scope of the Operations lists: Mine | Shared | Company | All.

Executions has always opened on the runs that are mine, with a filter to widen
to the shared ones (``validate_run_ownership`` in ``api/v1/helpers.py``, the
``ownership`` query parameter). Work items, shared state and worksets take the
same parameter and the same vocabulary, and this module is the one place that
turns it into a SQL clause for each of them.

"Mine" is what a person is responsible for: a row whose ``user_id`` is theirs
(a work item they created in chat, an entry their own agents wrote for them),
or whose owner or creator agent is an agent they own (``agents.user_id``).
Everything else the caller already sees is split in two by the row's
``is_company_managed`` flag (dev:visibility-filters-consistency):

* "Company" (``COMPANY``): company-managed rows that are not mine;
* "Shared" (``SHARED_ONLY``): the rest, rows of agents shared with the caller
  directly or through a group, of a workspace they read.

Mine, Shared and Company are a disjoint partition of the visible set. A
company-managed row of mine stays in Mine: the person responsible for it finds
it there, as on "My Runs". The clause narrows the caller's visibility,
it never widens it, so it is always applied on top of the resource's own
visibility clause.

The [DEPRECATED] ``ownership=shared`` (and its alias ``company_managed``) keeps
its meaning, "not mine", which is Shared and Company together.

An omitted ``ownership`` (or ``"all"``) adds no clause: the agent tools and
every caller that predates the parameter keep their meaning.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import and_, false, not_, or_, select

from app.models.agent import Agent

#: The values a service accepts once the route validated the parameter
#: (``validate_run_ownership``). ``company_managed`` is the API's [DEPRECATED]
#: alias of ``shared``, as on the Executions feeds: both mean "not mine".
MINE = "mine"
SHARED_VALUES = frozenset({"shared", "company_managed"})
#: The Shared segment of ``?vis=``: not mine and not company-managed.
SHARED_ONLY = "shared_only"
#: The Company segment of ``?vis=``: company-managed and not mine.
COMPANY = "company"


def ownership_clause(
    ownership: str | None,
    *,
    user_uuid: uuid.UUID | None,
    user_column: Any,
    agent_columns: Sequence[Any] = (),
    company_column: Any = None,
    company_rows_mine_by_user: bool = True,
):
    """The WHERE clause of *ownership*, or ``None`` when it filters nothing.

    *user_column* is the row's person (``user_id``); *agent_columns* are the
    columns naming an agent the row belongs to (its owner agent, its creator
    agent); *company_column* is the row's ``is_company_managed`` flag. A NULL
    in any of them counts as "not mine" and "not company", never as unknown:
    each term is guarded so the clauses are never NULL and Mine, Shared and
    Company are exactly the visible rows, each in one bucket.

    A resource without a company flag (*company_column* ``None``) has an empty
    Company and a Shared that is every row not mine.

    An anonymous caller (no user) owns nothing: "mine" is empty for them and
    "shared" is everything they see.

    *company_rows_mine_by_user* ``False`` keeps a company-managed row out of
    Mine when only its *user_column* names the caller (a work item a
    company-managed agent filed for the person, #1499): it is listed under
    Company, and stays Mine only through an agent the caller owns.
    """
    if not ownership or ownership == "all":
        return None
    if user_uuid is None:
        mine = false()
    else:
        owned_agents = select(Agent.id).where(Agent.user_id == user_uuid)
        user_term = and_(user_column.is_not(None), user_column == user_uuid)
        if not company_rows_mine_by_user and company_column is not None:
            user_term = and_(user_term, company_column.is_not(True))
        terms = [user_term]
        terms.extend(
            and_(column.is_not(None), column.in_(owned_agents))
            for column in agent_columns
        )
        mine = or_(*terms)
    if ownership == MINE:
        return mine
    if ownership in SHARED_VALUES:
        return not_(mine)
    if ownership == COMPANY:
        if company_column is None:
            return false()
        return and_(company_column.is_(True), not_(mine))
    if ownership == SHARED_ONLY:
        if company_column is None:
            return not_(mine)
        return and_(company_column.is_not(True), not_(mine))
    # The routes validate the value first (a 422 on a typo); a service reached
    # with an unknown one refuses rather than silently listing everything.
    raise ValueError(f"Unknown ownership filter `{ownership}`")
