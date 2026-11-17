"""Regression coverage for owner-deletion / toggle side effects.

The always-set-owner invariant makes ``user_id`` a real owner on company-managed
entities (instead of ``NULL``). Their ``users.id`` FK is ``ON DELETE CASCADE``,
so two new invariants must hold:

1. **Owner deletion must not erase shared resources** —
   ``reassign_company_managed_entities`` hands every company-managed row owned by
   the deleted user to a surviving admin *before* the cascade, so shared agents,
   assignments, runs, integrations, KBs, MCP servers, custom tools, channel bots,
   prompts and variables outlive their owner. Private rows are left to CASCADE.

2. **Reverting company → private must not rewrite run attribution** —
   ``revert_company_managed_to_private`` flips visibility on every run but only
   backfills the owner onto *ownerless* runs, preserving the execution identity
   (triggered-by / run-as / owner) of past run-as and manual/follow-up runs.
"""

from __future__ import annotations

import uuid

import pytest


class _Result:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount


class _RecordingDB:
    """Fake async session that records the compiled SQL of every statement and
    returns a fixed ``rowcount`` (no real DB connection required)."""

    def __init__(self, rowcount: int = 1):
        self.statements: list[str] = []
        self._rowcount = rowcount

    async def execute(self, statement):
        self.statements.append(
            str(statement.compile(compile_kwargs={"literal_binds": True}))
        )
        return _Result(self._rowcount)


def _update_table(sql: str) -> str:
    """Extract the target table name from a compiled ``UPDATE <table> SET ...``."""
    lowered = sql.strip().lower()
    assert lowered.startswith("update "), sql
    return lowered.split()[1]


# ---------------------------------------------------------------------------
# reassign_company_managed_entities (owner-deletion guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reassign_covers_every_company_managed_table():
    """Every mapped table with both ``is_company_managed`` and ``user_id`` is
    reassigned — so a shared agent and (critically) its assignments and runs all
    survive their owner's deletion, and new entities are covered automatically."""
    from app.api.v1.helpers import reassign_company_managed_entities

    from_user = uuid.uuid4()
    to_user = uuid.uuid4()
    db = _RecordingDB(rowcount=1)

    reassigned = await reassign_company_managed_entities(db, from_user, to_user)

    touched = {_update_table(sql) for sql in db.statements}
    # The three agent-tree tables MUST all be covered: reassigning the agent but
    # not its assignments/runs would still let the cascade delete them.
    expected = {
        "agents",
        "assignments",
        "assignment_runs",
        "integrations",
        "knowledge_bases",
        "mcp_servers",
        "custom_tools",
        "channel_bots",
        "prompts",
        "variables",
    }
    assert expected <= touched, f"missing company-managed tables: {expected - touched}"
    # The returned map reflects the tables that had rows reassigned.
    assert expected <= set(reassigned)
    assert all(count == 1 for count in reassigned.values())


@pytest.mark.asyncio
async def test_reassign_filters_on_owner_and_company_flag():
    """Each UPDATE is scoped to the old owner AND ``is_company_managed`` is true,
    and sets ``user_id`` to the new owner — private rows are never touched."""
    from app.api.v1.helpers import reassign_company_managed_entities

    from_user = uuid.uuid4()
    to_user = uuid.uuid4()
    db = _RecordingDB(rowcount=1)

    await reassign_company_managed_entities(db, from_user, to_user)

    agents_sql = next(s for s in db.statements if _update_table(s) == "agents")
    lowered = agents_sql.lower()
    assert "is_company_managed" in lowered
    assert "user_id" in lowered
    # Old owner appears in the WHERE filter; new owner in the SET clause.
    # (SQLAlchemy renders UUID literals without hyphens, hence ``.hex``.)
    assert from_user.hex in lowered
    assert to_user.hex in lowered
    set_clause = lowered.split("where", 1)[0]
    assert to_user.hex in set_clause  # new owner is what we assign


@pytest.mark.asyncio
async def test_reassign_skips_tables_with_no_rows():
    """Tables whose UPDATE affects zero rows are omitted from the result map."""
    from app.api.v1.helpers import reassign_company_managed_entities

    db = _RecordingDB(rowcount=0)
    reassigned = await reassign_company_managed_entities(
        db, uuid.uuid4(), uuid.uuid4()
    )
    assert reassigned == {}
    # Statements are still issued (we just don't know rowcount up front).
    assert db.statements


# ---------------------------------------------------------------------------
# revert_company_managed_to_private (company → private toggle)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revert_preserves_run_execution_identity():
    """The visibility flip touches every run unconditionally, but the owner is
    only backfilled onto ownerless runs — non-null run identities (run-as /
    manual / follow-up) are preserved."""
    from app.services.agent_bindings import revert_company_managed_to_private

    agent_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    db = _RecordingDB()

    await revert_company_managed_to_private(db, agent_id, owner_id)

    # Three statements: assignments update, run visibility flip, run owner backfill.
    assert len(db.statements) == 3
    run_stmts = [s for s in db.statements if _update_table(s) == "assignment_runs"]
    assert len(run_stmts) == 2

    visibility_flip = next(s for s in run_stmts if "user_id" not in s.lower())
    owner_backfill = next(s for s in run_stmts if "user_id" in s.lower())

    # The visibility flip never references user_id (it must not rewrite identity).
    assert "is null" not in visibility_flip.lower()
    assert "is_company_managed" in visibility_flip.lower()

    # The owner backfill is gated on user_id IS NULL and assigns the owner.
    assert "is null" in owner_backfill.lower()
    assert owner_id.hex in owner_backfill.lower()


@pytest.mark.asyncio
async def test_revert_clears_run_as_override_on_assignments():
    """Reverting to private drops the company-managed-only run-as override."""
    from app.services.agent_bindings import revert_company_managed_to_private

    db = _RecordingDB()
    await revert_company_managed_to_private(db, uuid.uuid4(), uuid.uuid4())

    assignments_sql = next(
        s for s in db.statements if _update_table(s) == "assignments"
    )
    lowered = assignments_sql.lower()
    assert "run_as_user_id" in lowered
    assert "is_company_managed" in lowered
