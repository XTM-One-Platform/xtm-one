"""A company-managed hand-off filed in the creator's fleet workspace (#12120).

#11900 admitted the owner agent of a company-managed item filed in a fleet
workspace it is a HOME member of. A company-managed creator acting in a
person's chat turn files the item in ITS home-fleet workspace
(``_default_workspace``), so these paths stayed refused:

* the owner a home member of a DIFFERENT fleet;
* the owner only a CONNECTED member of the creator's fleet;
* the creator agent updating or completing (take_over) what it created.

Pinned here, on compiled SQL: the creator branch, the filed-by-its-creator
branch (company-managed creator, home member, live fleet, the item's own
workspace), and nothing new for a person, a personal agent or no agent.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

import app.services.work_item as wi_service
from app.models.work_item import WorkItem
from app.services.company_managed_writes import agent_writer, person_writer

AGENT = uuid.uuid4()
CM_AGENT = agent_writer(is_company_managed=True, agent_id=AGENT)


def _compiled(clause):
    return select(WorkItem.id).where(clause).compile(dialect=postgresql.dialect())


def _sql(clause) -> str:
    return str(_compiled(clause)).lower()


def test_the_creator_agent_reaches_its_item_in_its_home_fleet_workspace():
    sql = _sql(wi_service._owner_agent_clause(CM_AGENT))
    assert "work_items.created_by_agent_id = " in sql
    assert AGENT in _compiled(wi_service._owner_agent_clause(CM_AGENT)).params.values()


def test_the_owner_reaches_an_item_filed_in_its_company_managed_creator_s_fleet():
    sql = _sql(wi_service._owner_agent_clause(CM_AGENT))
    # Whatever fleet the owner belongs to (another home fleet, or connected
    # only): the workspace is the one the item's creator filed it in.
    assert "exists (select fleets.id" in sql
    assert "fleets.workspace_id = work_items.workspace_id" in sql
    assert "fleet_members.agent_id = work_items.created_by_agent_id" in sql
    assert "agents.is_company_managed is true" in sql
    # Still a live fleet, a home member, a filed company-managed item.
    assert sql.count("fleets.is_archived is false") == 2
    assert "work_items.is_company_managed is true" in sql
    assert "work_items.workspace_id is not null" in sql


def test_the_creator_branch_never_reaches_beyond_the_owner_or_claimer():
    # The filed-by-its-creator branch admits only the owner or claimer: a
    # third agent of the creator's fleet is not admitted through it.
    sql = _sql(wi_service._owner_agent_clause(CM_AGENT))
    branch = sql.split("exists (select fleets.id", 1)[0].rsplit(" or (", 1)[-1]
    assert "work_items.owner_agent_id" in branch and "work_items.claimed_by_agent_id" in branch


@pytest.mark.parametrize(
    "actor",
    [
        None,
        person_writer(is_admin=True),
        agent_writer(is_company_managed=False, agent_id=uuid.uuid4()),
        agent_writer(is_company_managed=True),
    ],
)
def test_no_widening_for_persons_personal_agents_or_no_agent(actor):
    assert _sql(wi_service._owner_agent_clause(actor)).endswith("where false")


def test_read_and_write_paths_carry_the_same_tier():
    # get_work_item (read) and claim / update / complete (write) of an agent.
    read = _sql(wi_service._visible_clause(None, [], CM_AGENT))
    write = _sql(wi_service._writable_clause(None, [], CM_AGENT))
    for sql in (read, write):
        assert "fleet_members.agent_id = work_items.created_by_agent_id" in sql
        assert "work_items.created_by_agent_id = " in sql
    # A person's read is unchanged.
    uid = uuid.uuid4()
    assert "created_by_agent_id" not in _sql(wi_service._writable_clause(uid, []))
