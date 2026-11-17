"""The OpenCRQ rename's duplicate-slug branch, executed against PostgreSQL.

``test_opencrq_rename_migration.py`` pins the revision's shape by reading its
source. This file runs it: ``_rename_slug`` decides whether a row already
holding the new slug may be deleted, and a wrong answer either destroys a
built-in's history (deleting a row that carries assignments or conversations)
or fails a deploy that should have resolved itself. Both only show against a
real schema, where ``_row_has_dependents`` reads the foreign keys from
``pg_constraint``.

The revision is loaded from its file and its slugs are spelled as literals: a
migration must keep meaning what it meant on the day it ran.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import app.models  # noqa: F401 - registers every table, so every FK onto agents exists
from app.agents.seed import seed_builtin_intent_bindings
from app.models.agent import Agent
from app.models.agent_template import AgentTemplate
from app.models.assignment import Assignment
from app.models.chat import Conversation
from app.models.intent import Intent, IntentBinding
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_REVISION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "o1crq2opencrq1_rename_opengrc_to_opencrq.py"
)
_OLD = "opengrc-assistant"
_NEW = "opencrq-assistant"


def _load_revision():
    spec = importlib.util.spec_from_file_location("o1crq2opencrq1", _REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


revision = _load_revision()


@asynccontextmanager
async def _temp_database() -> AsyncIterator[AsyncEngine]:
    """Isolated database with the full schema, dropped on exit."""
    async with scratch_database(
        "opencrq_rename", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        yield db.engine


# -- fixture rows ------------------------------------------------------------


async def _agent(db: AsyncSession, slug: str, *, builtin: bool = True) -> uuid.UUID:
    agent = Agent(name=slug, slug=slug, is_builtin=builtin)
    db.add(agent)
    await db.flush()
    return agent.id


async def _intent_binding(db: AsyncSession, agent_id: uuid.UUID, is_builtin: bool | None) -> None:
    intent = Intent(name=f"test.intent_{uuid.uuid4().hex[:8]}")
    db.add(intent)
    await db.flush()
    db.add(IntentBinding(intent_id=intent.id, agent_id=agent_id, is_builtin=is_builtin))
    await db.flush()


async def _assignment(db: AsyncSession, agent_id: uuid.UUID) -> uuid.UUID:
    assignment = Assignment(agent_id=agent_id, name="digest", trigger_type="manual", prompt="do it")
    db.add(assignment)
    await db.flush()
    return assignment.id


async def _conversation(db: AsyncSession, *, agent_id=None, original_agent_id=None) -> uuid.UUID:
    conversation = Conversation(agent_id=agent_id, original_agent_id=original_agent_id)
    db.add(conversation)
    await db.flush()
    return conversation.id


async def _template(db: AsyncSession, slug: str, *, builtin: bool = True) -> uuid.UUID:
    template = AgentTemplate(slug=slug, name=slug, is_builtin=builtin)
    db.add(template)
    await db.flush()
    return template.id


async def _historical_agent(db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """The pre-rename built-in, carrying the history the rename exists to keep."""
    historical = await _agent(db, _OLD)
    assignment = await _assignment(db, historical)
    conversation = await _conversation(db, agent_id=historical, original_agent_id=historical)
    return historical, assignment, conversation


# -- probes ------------------------------------------------------------------


async def _rename(engine: AsyncEngine, table: str = "agents") -> None:
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: revision._rename_slug(sync, table, _OLD, _NEW))


async def _slugs(engine: AsyncEngine, model=Agent) -> dict[uuid.UUID, str]:
    async with AsyncSession(engine) as db:
        rows = await db.execute(select(model.id, model.slug))
        return {row.id: row.slug for row in rows}


async def _count(engine: AsyncEngine, model) -> int:
    async with AsyncSession(engine) as db:
        return (await db.execute(select(func.count()).select_from(model))).scalar_one()


async def _assert_refused(
    engine: AsyncEngine, duplicate_id: uuid.UUID, historical_id: uuid.UUID, reason: str
) -> None:
    with pytest.raises(RuntimeError) as excinfo:
        await _rename(engine)
    message = str(excinfo.value)
    assert str(duplicate_id) in message
    assert "re-run the upgrade" in message
    assert reason in message
    assert await _slugs(engine) == {historical_id: _OLD, duplicate_id: _NEW}


# -- the duplicate branch ----------------------------------------------------


async def test_a_freshly_seeded_duplicate_is_cleared_and_the_history_keeps_its_row():
    async with _temp_database() as engine:
        async with AsyncSession(engine) as db:
            historical, assignment_id, conversation_id = await _historical_agent(db)
            duplicate = await _agent(db, _NEW)
            await db.commit()
            # The real seeder, not a hand-built row: ``_SEEDER_OWNED_REFERENCES``
            # is only right while the seeder marks the bindings it writes.
            await seed_builtin_intent_bindings(db, platform_mode="xtm_one")
            seeded = (
                await db.execute(
                    select(IntentBinding.is_builtin).where(IntentBinding.agent_id == duplicate)
                )
            ).scalars().all()
        assert seeded, "the seeder wrote no intent binding for the duplicate built-in"
        assert all(flag is True for flag in seeded)

        await _rename(engine)

        assert await _slugs(engine) == {historical: _NEW}
        async with AsyncSession(engine) as db:
            assignment = await db.get(Assignment, assignment_id)
            conversation = await db.get(Conversation, conversation_id)
        assert assignment.agent_id == historical
        assert (conversation.agent_id, conversation.original_agent_id) == (historical, historical)


async def test_a_duplicate_with_an_assignment_stops_the_upgrade():
    async with _temp_database() as engine:
        async with AsyncSession(engine) as db:
            historical, _, _ = await _historical_agent(db)
            duplicate = await _agent(db, _NEW)
            await _assignment(db, duplicate)
            await db.commit()

        await _assert_refused(engine, duplicate, historical, "history of its own")
        assert await _count(engine, Assignment) == 2


async def test_a_duplicate_holding_a_conversation_stops_the_upgrade():
    async with _temp_database() as engine:
        async with AsyncSession(engine) as db:
            historical, _, _ = await _historical_agent(db)
            duplicate = await _agent(db, _NEW)
            conversation_id = await _conversation(db, agent_id=duplicate)
            await db.commit()

        await _assert_refused(engine, duplicate, historical, "history of its own")
        async with AsyncSession(engine) as db:
            conversation = await db.get(Conversation, conversation_id)
        assert conversation.agent_id == duplicate


async def test_a_duplicate_named_as_a_conversations_original_agent_stops_the_upgrade():
    # ``original_agent_id`` exists only to record attribution and is SET NULL
    # on delete: losing it is silent, which is why it counts as history.
    async with _temp_database() as engine:
        async with AsyncSession(engine) as db:
            historical, _, _ = await _historical_agent(db)
            duplicate = await _agent(db, _NEW)
            await _conversation(db, original_agent_id=duplicate)
            await db.commit()

        await _assert_refused(engine, duplicate, historical, "history of its own")


async def test_a_duplicate_someone_created_stops_the_upgrade():
    async with _temp_database() as engine:
        async with AsyncSession(engine) as db:
            historical, _, _ = await _historical_agent(db)
            duplicate = await _agent(db, _NEW, builtin=False)
            await db.commit()

        await _assert_refused(engine, duplicate, historical, "not a built-in")


async def test_an_intent_binding_an_operator_added_counts_as_history():
    async with _temp_database() as engine:
        async with AsyncSession(engine) as db:
            historical, _, _ = await _historical_agent(db)
            duplicate = await _agent(db, _NEW)
            await _intent_binding(db, duplicate, is_builtin=True)
            await _intent_binding(db, duplicate, is_builtin=False)
            await db.commit()

        await _assert_refused(engine, duplicate, historical, "history of its own")
        assert await _count(engine, IntentBinding) == 2


async def test_an_intent_binding_with_no_marker_counts_as_history():
    """``NOT NULL`` is NULL, so a bare ``NOT is_builtin`` let this through.

    The column is NOT NULL on every install built from the baseline, but an
    older install whose column was added bare by ``_ensure_model_columns``, a
    raw-SQL write or a restored dump can hold NULL - so the legacy shape is
    rebuilt here. An unknown marker must refuse, never delete.
    """
    async with _temp_database() as engine:
        async with engine.begin() as conn:
            await conn.execute(
                text("ALTER TABLE intent_bindings ALTER COLUMN is_builtin DROP NOT NULL")
            )
        async with AsyncSession(engine) as db:
            historical, _, _ = await _historical_agent(db)
            duplicate = await _agent(db, _NEW)
            await _intent_binding(db, duplicate, is_builtin=True)
            # The ORM substitutes the column default for None, so the NULL is
            # written the way a legacy row got it: outside the ORM.
            await db.execute(
                text("UPDATE intent_bindings SET is_builtin = NULL WHERE agent_id = :id"),
                {"id": duplicate},
            )
            await db.commit()
            stored = (
                await db.execute(
                    text("SELECT is_builtin FROM intent_bindings WHERE agent_id = :id"),
                    {"id": duplicate},
                )
            ).scalar_one()
        assert stored is None

        await _assert_refused(engine, duplicate, historical, "history of its own")
        assert await _count(engine, IntentBinding) == 1


async def test_a_duplicate_whose_builtin_marker_is_unknown_stops_the_upgrade():
    """The duplicate's own ``is_builtin`` is the guard's other nullable boolean.

    Same legacy shape as the binding above: a NULL marker cannot prove the row
    is the seeder's, so it must refuse, never delete.
    """
    async with _temp_database() as engine:
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE agents ALTER COLUMN is_builtin DROP NOT NULL"))
        async with AsyncSession(engine) as db:
            historical, _, _ = await _historical_agent(db)
            duplicate = await _agent(db, _NEW)
            await db.execute(
                text("UPDATE agents SET is_builtin = NULL WHERE id = :id"),
                {"id": duplicate},
            )
            await db.commit()

        await _assert_refused(engine, duplicate, historical, "not a built-in")


async def test_a_reference_through_names_that_need_quoting_counts_as_history():
    """The probe renders the catalog's names as identifiers.

    The referencing tables are read from the catalog so that one added later is
    covered for free, which only holds if any legal name can be probed: a
    mixed-case or reserved-word column, left bare, is invalid SQL that aborts
    the upgrade. No model declares such a name, so the table is built by hand.
    """
    async with _temp_database() as engine:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    'CREATE TABLE "LegacyAgentLinks" '
                    '(id serial PRIMARY KEY, "agentId" uuid REFERENCES agents (id))'
                )
            )
        async with AsyncSession(engine) as db:
            historical, _, _ = await _historical_agent(db)
            duplicate = await _agent(db, _NEW)
            await db.execute(
                text('INSERT INTO "LegacyAgentLinks" ("agentId") VALUES (:id)'),
                {"id": duplicate},
            )
            await db.commit()

        await _assert_refused(engine, duplicate, historical, "history of its own")


async def test_a_second_run_after_the_rename_is_a_no_op():
    async with _temp_database() as engine:
        async with AsyncSession(engine) as db:
            historical, _, _ = await _historical_agent(db)
            duplicate = await _agent(db, _NEW)
            await _intent_binding(db, duplicate, is_builtin=True)
            await db.commit()

        await _rename(engine)
        await _rename(engine)

        assert await _slugs(engine) == {historical: _NEW}
        assert await _count(engine, Assignment) == 1


async def test_without_a_duplicate_the_slug_is_simply_renamed():
    async with _temp_database() as engine:
        async with AsyncSession(engine) as db:
            historical, _, _ = await _historical_agent(db)
            await db.commit()

        await _rename(engine)

        assert await _slugs(engine) == {historical: _NEW}


async def test_agent_templates_follow_the_same_rule():
    async with _temp_database() as engine:
        async with AsyncSession(engine) as db:
            historical = await _template(db, _OLD)
            await _template(db, _NEW)
            await db.commit()

        await _rename(engine, "agent_templates")
        assert await _slugs(engine, AgentTemplate) == {historical: _NEW}

    async with _temp_database() as engine:
        async with AsyncSession(engine) as db:
            historical = await _template(db, _OLD)
            duplicate = await _template(db, _NEW, builtin=False)
            await db.commit()

        with pytest.raises(RuntimeError, match="not a built-in"):
            await _rename(engine, "agent_templates")
        assert await _slugs(engine, AgentTemplate) == {historical: _OLD, duplicate: _NEW}


# -- the revision's own entry points -----------------------------------------

#: Provider-keyed columns of the historical row, before and after the rename.
_PRE_RENAME_COLUMNS = {
    "integration_bindings": {"opengrc": ["binding-1"]},
    "integration_allowed_tools": {"opengrc": ["update_opengrc_control_settings"], "github": []},
    "integration_element_bindings": {"opengrc:control": ["element-1"]},
    "integration_template_bindings": ["opengrc"],
    "tool_deny_list": ["update_opengrc_control_settings"],
}
_POST_RENAME_COLUMNS = {
    "integration_bindings": {"opencrq": ["binding-1"]},
    "integration_allowed_tools": {"opencrq": ["update_opencrq_control_settings"], "github": []},
    "integration_element_bindings": {"opencrq:control": ["element-1"]},
    "integration_template_bindings": ["opencrq"],
    "tool_deny_list": ["update_opencrq_control_settings"],
}


async def _run_revision(engine: AsyncEngine, step) -> None:
    """Call ``upgrade`` or ``downgrade`` the way Alembic does, ``op`` bound to the connection."""

    def _call(sync_conn) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            step()

    async with engine.begin() as conn:
        await conn.run_sync(_call)


async def _agent_columns(engine: AsyncEngine, agent_id: uuid.UUID) -> dict:
    async with AsyncSession(engine) as db:
        agent = await db.get(Agent, agent_id)
        return {column: getattr(agent, column) for column in _PRE_RENAME_COLUMNS}


async def test_upgrade_twice_downgrade_and_upgrade_again_keep_the_history_on_one_row():
    """The revision itself, both ways, on a database the new seeder already ran on.

    ``downgrade`` is the same ``_migrate`` with the spellings swapped, so the
    same guard decides in both directions, and the provider-keyed columns of
    the row must come back exactly as they were, an empty allow list included.
    """
    async with _temp_database() as engine:
        async with AsyncSession(engine) as db:
            historical, assignment_id, conversation_id = await _historical_agent(db)
            agent = await db.get(Agent, historical)
            for column, value in _PRE_RENAME_COLUMNS.items():
                setattr(agent, column, value)
            template = await _template(db, _OLD)
            await _agent(db, _NEW)
            await db.commit()
            await seed_builtin_intent_bindings(db, platform_mode="xtm_one")

        await _run_revision(engine, revision.upgrade)
        await _run_revision(engine, revision.upgrade)

        assert await _slugs(engine) == {historical: _NEW}
        assert await _slugs(engine, AgentTemplate) == {template: _NEW}
        assert await _agent_columns(engine, historical) == _POST_RENAME_COLUMNS

        await _run_revision(engine, revision.downgrade)

        assert await _slugs(engine) == {historical: _OLD}
        assert await _slugs(engine, AgentTemplate) == {template: _OLD}
        assert await _agent_columns(engine, historical) == _PRE_RENAME_COLUMNS

        await _run_revision(engine, revision.upgrade)

        assert await _slugs(engine) == {historical: _NEW}
        assert await _agent_columns(engine, historical) == _POST_RENAME_COLUMNS
        async with AsyncSession(engine) as db:
            assignment = await db.get(Assignment, assignment_id)
            conversation = await db.get(Conversation, conversation_id)
        assert assignment.agent_id == historical
        assert (conversation.agent_id, conversation.original_agent_id) == (historical, historical)
