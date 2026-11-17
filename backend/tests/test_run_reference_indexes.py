"""Every reference to ``assignment_runs`` is indexed.

Runs are deleted by the thousand - with their agent, with their assignment, by
the run purges - and PostgreSQL applies each foreign key's ``ON DELETE`` action
once per deleted run, with a query on the referencing column. A reference no
index leads with is scanned whole for every run deleted:
``file_attachments.assignment_run_id`` was one, and an agent with a few
thousand runs could no longer be deleted, its run delete cancelled by the 30 s
``statement_timeout`` on every attempt.
"""

from __future__ import annotations

from sqlalchemy import Column, PrimaryKeyConstraint, Table, UniqueConstraint

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.core.database import Base

_RUNS = "assignment_runs"


def _run_references() -> list[tuple[str, str]]:
    return sorted(
        (table.name, fk.parent.name)
        for table in Base.metadata.sorted_tables
        for fk in table.foreign_keys
        if fk.column.table.name == _RUNS
    )


def _leading_columns(table: Table) -> set[str]:
    """The columns a full index, the primary key or a unique constraint of *table* starts with.

    A partial index does not count: its predicate may leave out the rows the
    foreign key action looks for.
    """
    leading = {
        index.expressions[0].name
        for index in table.indexes
        if index.expressions
        and isinstance(index.expressions[0], Column)
        and index.dialect_options["postgresql"]["where"] is None
    }
    for constraint in table.constraints:
        if isinstance(constraint, PrimaryKeyConstraint | UniqueConstraint) and constraint.columns:
            leading.add(next(iter(constraint.columns)).name)
    return leading


def test_the_run_references_are_found():
    references = _run_references()

    assert ("file_attachments", "assignment_run_id") in references
    assert ("assignment_runs", "parent_run_id") in references


def test_every_run_reference_is_indexed():
    unindexed = [
        f"{table}.{column}"
        for table, column in _run_references()
        if column not in _leading_columns(Base.metadata.tables[table])
    ]

    assert not unindexed, (
        "Foreign key(s) to assignment_runs that no index leads with: "
        + ", ".join(unindexed)
        + ". Deleting runs then scans the referencing table once per run. "
        "Declare the index on the model (index=True) and build it in a migration "
        "with create_index_in_place (see f2runix5258)."
    )
