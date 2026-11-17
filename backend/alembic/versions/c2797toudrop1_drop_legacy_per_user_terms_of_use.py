"""drop the legacy per-user Terms of Use schema

Terms of Use acceptance has been a single platform-wide record on
``platform_settings``, whose migration ``c5g9tou20001`` (first
shipped in release 1.260809.1) was the EXPAND phase only: it left the per-user
schema it replaced in place, unmapped and inert, because the releases that read
it keep serving during a rolling deploy. Releases 1.260804.0 to 1.260809.0 map
``users.tou_accepted_version`` / ``users.tou_accepted_at`` on every user load
and write ``terms_acceptances`` on accept. This is the CONTRACT phase. It drops:

* ``users.tou_accepted_version`` and ``users.tou_accepted_at``;
* the ``terms_acceptances`` table, with its index and unique constraint.

Nothing reads them any more: ``User`` does not map the columns, no model maps
the table, and no route, service, export or frontend code names either.

Whether a replica may still read them depends on where the upgrade started,
not on the release it lands on. An install upgrading from 1.260809.1 or later
replaces replicas that never read the legacy schema. An install still on
1.260804.0 to 1.260809.0 that jumps straight here applies the expand migration
and this one in the same upgrade, while its old replicas keep selecting
``users.tou_accepted_*`` until the rollout replaces them - dropping the columns
then would fail every authenticated request those replicas serve. So the body
reads the revisions ``alembic/env.py`` recorded before the run
(``migration_ddl.run_starting_heads``) and drops only when the expand revision
was already applied, or when the legacy schema did not exist before the run (an
install older than 1.260804.0, whose replicas never read it).

A run can also be the rest of an interrupted upgrade: ``autocommit_block()``
commits the run so far, so an upgrade from 1.260804.0 - 1.260809.0 that fails
or dies after one (the startup runner retries a lock timeout; a pod restarts)
is finished by a new run that records the committed revision - already past the
expand - while the old replicas still serve. ``_RESUME_POINTS`` lists every
revision such a run can start from; a start on one of them cannot be told apart
from that case. 1.260813.0 and 1.260814.0 sit on one (``f2adpst28350``), so a
direct jump from them keeps the schema too.

In every one of those cases, an unknown starting point included, the body
leaves the inert schema exactly as every migrated install has carried it since
1.260809.1. Fresh installs never had it (``create_all`` + ``stamp head``). The
drop assumes the rollout that ran the expand phase has finished: a deploy
stacked on a rollout from 1.260804.0 - 1.260809.0 that never completed is not
something a migration can see.

Lock profile: ``DROP COLUMN`` is metadata-only but takes ACCESS EXCLUSIVE on
``users``, the hottest table (every authenticated request reads it), and
dropping ``terms_acceptances`` also takes ACCESS EXCLUSIVE on ``users`` to
remove its foreign key's triggers. Canonical recipe: idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL.

The downgrade rebuilds the prior shape, lifted from ``c5g9tou20001``'s
downgrade: the two nullable columns and the empty history table with its index.
The dropped rows are not recoverable; no release from 1.260809.1 on reads them,
and every per-user acceptance they record was also written to the audit log
when it was made (``terms_of_use`` entries).

Revision ID: c2797toudrop1
Revises: r4244gaveup01
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import (
    applied_before_this_run,
    run_starting_heads,
    set_lock_timeout,
    terminate_lock_holders,
)

# revision identifiers, used by Alembic.
revision: str = "c2797toudrop1"
down_revision: str | None = "r4244gaveup01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger(__name__)

_TABLE = "terms_acceptances"
_INDEX = "ix_terms_acceptances_user_id"
_USERS = "users"
_USER_COLUMN_TYPES = (
    ("tou_accepted_version", "VARCHAR(50)"),
    ("tou_accepted_at", "TIMESTAMPTZ"),
)

# Spelled as literals, never imported: a revision must keep meaning what it
# meant on the day it ran. The expand phase (first in 1.260809.1) and the
# revision that created the per-user schema (first in 1.260804.0).
_EXPAND_REVISION = "c5g9tou20001"
_LEGACY_REVISION = "a4c8tou10001"

# Every revision an interrupted upgrade can resume from between the expand
# revision and this one: the revision stamped when each migration opened its
# ``autocommit_block()``, named after it. An upgrade from 1.260804.0 -
# 1.260809.0 (head ``b2r7rep2720``) commits ``f2adpst28350`` first. Derived
# from the history and pinned by tests/test_terms_of_use_contract_migration.py.
_RESUME_POINTS = frozenset(
    {
        "f2adpst28350",  # a7f3run0001
        "a7f3run0001",  # a7f3run0002
        "f9u2fav0001",  # f3nova00rule
        "f3nova00rule",  # g1novasrc00
        "e7a3wait2698",  # d4e5ushr1881
        "d4e5ushr1881",  # d1f9flw1909
        "d1f9flw1909",  # e2c4flwrun2
        "e2c4flwrun2",  # e5f7flwevt1
        "p1fpool0001",  # f1fail3128
        "x1xtmmcp0001",  # a1paus3198
        "i3p3358adesc",  # j4p3358collab
        "j4p3358collab",  # k5p3358studio
        "a2a5respond1",  # a2a6delegidx
        "a2af1reopen1",  # m1mem516001
        "o2osd3696001",  # f1sha2dedup01
        "b3818logo001",  # r4244gaveup01
    }
)


def _table_exists(conn, table: str) -> bool:
    # Scoped to the active schema - information_schema spans ALL schemas, so a
    # same-named table elsewhere must not decide this probe.
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": table},
        ).scalar()
    )


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).scalar()
    )


def _index_exists(conn, index: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM pg_indexes "
                "WHERE schemaname = current_schema() AND indexname = :index"
            ),
            {"index": index},
        ).scalar()
    )


def _legacy_schema_may_still_be_read(migration_context) -> bool:
    """May a replica this rollout has not replaced yet read the legacy schema?

    Only a replica of 1.260804.0 to 1.260809.0 reads it, and those are the
    replicas being replaced exactly when the upgrade started between the
    revision that created the schema and the expand revision. An unknown
    starting point cannot be proven safe, and neither can a resume point.
    """
    starting_heads = run_starting_heads(migration_context)
    if starting_heads is None or _RESUME_POINTS.intersection(starting_heads):
        return True
    expand_applied = applied_before_this_run(migration_context, _EXPAND_REVISION)
    if expand_applied is None:
        return True
    if expand_applied:
        return False
    return applied_before_this_run(migration_context, _LEGACY_REVISION) is not False


def upgrade() -> None:
    conn = op.get_bind()
    # 1. Idempotency probe - fresh installs and re-runs take NO lock.
    table_present = _table_exists(conn, _TABLE)
    columns_present = [
        column
        for column, _ddl_type in _USER_COLUMN_TYPES
        if _column_exists(conn, _USERS, column)
    ]
    if not table_present and not columns_present:
        return
    migration_context = op.get_context()
    if _legacy_schema_may_still_be_read(migration_context):
        starting_heads = run_starting_heads(migration_context)
        logger.warning(
            "Keeping the inert legacy per-user Terms of Use schema (%s, %s.%s): "
            "this run started at %s, which may be an upgrade from a release "
            "that still reads it (1.260804.0 - 1.260809.0) or the rest of one, "
            "and its replicas may still be serving",
            _TABLE,
            _USERS,
            "/".join(column for column, _ddl_type in _USER_COLUMN_TYPES),
            ", ".join(starting_heads) if starting_heads else "an unknown revision",
        )
        return
    # 2. Pre-flight on every table the DDL locks: both statements take ACCESS
    #    EXCLUSIVE on ``users`` (the column drop, and the removal of the history
    #    table's foreign-key triggers).
    if table_present:
        terminate_lock_holders(conn, _TABLE)
    terminate_lock_holders(conn, _USERS)
    # 3. Safety belt: fail fast (55P03) instead of wedging the lock queue.
    set_lock_timeout(conn)
    # 4. Guarded DDL.
    if table_present:
        op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
    if columns_present:
        drops = ", ".join(f"DROP COLUMN IF EXISTS {column}" for column in columns_present)
        op.execute(f"ALTER TABLE {_USERS} {drops}")


def downgrade() -> None:
    conn = op.get_bind()
    table_missing = not _table_exists(conn, _TABLE)
    # Checked on its own, as ``a4c8tou10001`` does: a table can be present but
    # unindexed (a restore, a hand-made table).
    index_missing = table_missing or not _index_exists(conn, _INDEX)
    columns_missing = [
        (column, ddl_type)
        for column, ddl_type in _USER_COLUMN_TYPES
        if not _column_exists(conn, _USERS, column)
    ]
    if not table_missing and not index_missing and not columns_missing:
        return
    if columns_missing:
        terminate_lock_holders(conn, _USERS)
    if index_missing and not table_missing:
        # A plain index build locks the history table that already exists; a
        # table created below is new, nothing can hold a lock on it.
        terminate_lock_holders(conn, _TABLE)
    # The belt covers the column add, the history table's foreign key, which
    # takes SHARE ROW EXCLUSIVE on ``users`` even inside a brand-new table, and
    # an index build on a history table that already exists.
    set_lock_timeout(conn)
    if columns_missing:
        # NULL = never accepted, as the per-user schema read it.
        adds = ", ".join(
            f"ADD COLUMN IF NOT EXISTS {column} {ddl_type}"
            for column, ddl_type in columns_missing
        )
        op.execute(f"ALTER TABLE {_USERS} {adds}")
    if table_missing:
        op.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_TABLE} (
                id UUID PRIMARY KEY,
                user_id UUID NOT NULL REFERENCES {_USERS}(id) ON DELETE CASCADE,
                tou_version VARCHAR(50) NOT NULL,
                accepted_at TIMESTAMPTZ NOT NULL,
                CONSTRAINT uq_terms_acceptances_user_version
                    UNIQUE (user_id, tou_version)
            )
            """
        )
    if index_missing:
        op.execute(f"CREATE INDEX IF NOT EXISTS {_INDEX} ON {_TABLE} (user_id)")
