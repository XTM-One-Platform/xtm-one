"""Items of a company-managed agent are company-managed and listed under Company (#1499).

A company-managed agent's work item was personal when the run filing it was
not company-managed (a hidden-runs inbox dispatch, a CTEM case decision parked
for the scope user), so it landed in Mine of that user. ``create_work_item``
now follows the creating agent, and the work items' Mine keeps a
company-managed row out unless the caller owns its agent.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.models.work_item import WorkItem
from app.services import work_item as work_item_service
from app.services.company_managed_writes import agent_writer, person_writer
from app.services.ownership_scope import ownership_clause


def _sql(clause) -> str:
    return str(select(WorkItem.id).where(clause).compile(dialect=postgresql.dialect())).lower()


def test_work_items_mine_keeps_company_rows_out_by_user_alone():
    (clause,) = work_item_service._ownership_clauses("mine", uuid.uuid4())
    sql = _sql(clause)
    # The user term is guarded by the company flag; the owned-agent terms are not.
    assert "work_items.user_id = " in sql
    assert "work_items.is_company_managed is not true" in sql
    assert "work_items.owner_agent_id in" in sql


def test_work_items_company_takes_the_company_rows_filed_for_the_caller():
    (clause,) = work_item_service._ownership_clauses("company", uuid.uuid4())
    sql = _sql(clause)
    assert "work_items.is_company_managed is true" in sql
    # Not mine = not (user and not company, or owned agent): a company row of the caller passes.
    assert "is_company_managed is not true" in sql


def test_other_resources_keep_the_former_mine():
    clause = ownership_clause(
        "mine",
        user_uuid=uuid.uuid4(),
        user_column=WorkItem.user_id,
        company_column=WorkItem.is_company_managed,
    )
    assert "is_company_managed" not in _sql(clause)


def _session_returning(flag):
    result = MagicMock()
    result.scalar_one_or_none.return_value = flag
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=db)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


@pytest.mark.asyncio
async def test_a_company_agent_filing_as_itself_files_a_company_item():
    agent_id = uuid.uuid4()
    with patch("app.core.database.async_session", return_value=_session_returning(True)):
        assert await work_item_service._creator_is_company_agent(
            agent_writer(is_company_managed=False, agent_id=agent_id), str(agent_id)
        )


@pytest.mark.asyncio
async def test_a_personal_agent_stays_personal():
    agent_id = uuid.uuid4()
    with patch("app.core.database.async_session", return_value=_session_returning(False)):
        assert not await work_item_service._creator_is_company_agent(
            agent_writer(is_company_managed=False, agent_id=agent_id), str(agent_id)
        )


@pytest.mark.asyncio
async def test_only_the_agents_own_write_counts():
    agent_id = uuid.uuid4()
    session = MagicMock(side_effect=AssertionError("no read expected"))
    with patch("app.core.database.async_session", session):
        # A person on the REST API naming a creator agent, another agent, no creator.
        assert not await work_item_service._creator_is_company_agent(person_writer(is_admin=False), str(agent_id))
        assert not await work_item_service._creator_is_company_agent(
            agent_writer(is_company_managed=False, agent_id=uuid.uuid4()), str(agent_id)
        )
        assert not await work_item_service._creator_is_company_agent(
            agent_writer(is_company_managed=False, agent_id=agent_id), None
        )


@pytest.mark.asyncio
async def test_create_work_item_follows_the_creating_agent_before_the_write_gate():
    """The run's non-company writer no longer refuses or personalises the item (ctem_cases._park, hidden runs)."""
    agent_id = uuid.uuid4()
    seen = {}

    def _gate(writer, *, is_company_managed, action, subject):
        seen["writer"] = writer
        seen["company"] = is_company_managed
        raise RuntimeError("stop after the gate")

    with (
        patch.object(work_item_service, "_creator_is_company_agent", AsyncMock(return_value=True)),
        patch.object(work_item_service, "refuse_company_managed_write", _gate),
        pytest.raises(RuntimeError, match="stop after the gate"),
    ):
        await work_item_service.create_work_item(
            user_id=str(uuid.uuid4()),
            title="Remediation decision: case",
            created_by_agent_id=str(agent_id),
            owner_agent_id=str(agent_id),
            writer=agent_writer(is_company_managed=False, agent_id=agent_id),
        )
    assert seen["company"] is True
    assert seen["writer"].may_write is True
    assert seen["writer"].agent_id == agent_id


@pytest.mark.asyncio
async def test_create_work_item_leaves_a_personal_agents_item_personal():
    agent_id = uuid.uuid4()
    seen = {}

    def _gate(writer, *, is_company_managed, action, subject):
        seen["company"] = is_company_managed
        raise RuntimeError("stop after the gate")

    with (
        patch.object(work_item_service, "_creator_is_company_agent", AsyncMock(return_value=False)),
        patch.object(work_item_service, "refuse_company_managed_write", _gate),
        pytest.raises(RuntimeError),
    ):
        await work_item_service.create_work_item(
            user_id=str(uuid.uuid4()),
            title="Personal",
            created_by_agent_id=str(agent_id),
            writer=agent_writer(is_company_managed=False, agent_id=agent_id),
        )
    assert seen["company"] is False


def test_the_migration_targets_open_items_of_company_agents_only():
    import importlib.util
    from pathlib import Path

    path = next(Path(__file__).resolve().parents[1].glob("alembic/versions/h1499wicompany1_*.py"))
    spec = importlib.util.spec_from_file_location("h1499", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    executed: list[str] = []
    inspector = SimpleNamespace(has_table=lambda name: True)
    with (
        patch.object(module.op, "get_bind", return_value=MagicMock()),
        patch.object(module.op, "execute", side_effect=lambda sql: executed.append(" ".join(str(sql).split()))),
        patch.object(module.sa, "inspect", return_value=inspector),
        patch.object(module, "set_lock_timeout"),
    ):
        module.upgrade()
    (sql,) = executed
    assert "a.is_company_managed IS TRUE" in sql
    assert "w.action_mode IS NULL" in sql
    assert "'open', 'claimed', 'in_progress', 'blocked'" in sql
    assert "user_id" not in sql
