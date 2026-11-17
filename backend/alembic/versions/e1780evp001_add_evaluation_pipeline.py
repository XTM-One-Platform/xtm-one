"""add the evaluation pipeline

The evaluation vertical's whole schema change, collapsed into one revision on
``main``: it creates the final schema directly, so nothing the branch
added and later removed is ever created.

- New tables: ``dataset_versions``, ``dataset_version_entries``,
  ``evaluation_run_params``, ``evaluation_metrics``.
- New columns on ``datasets``, ``dataset_entries``, ``evaluations``,
  ``evaluation_agents``, ``evaluation_runs``, ``evaluation_results``,
  ``evaluation_episodes`` and ``knowledge_bases``.
- ``evaluations.performance_tolerance`` added with ``speed_tolerance``'s values.
  ``speed_tolerance`` stays, given a default since the code no longer
  writes it, so a replica still on the previous code reads and writes it through
  a rolling deploy; a later release drops it.
- ``evaluation_results.entry_id`` nullable, its key ``ON DELETE SET NULL``: a
  result survives the deletion of its live entry.
- Data: each callback resume secret stored in clear is encrypted, and
  the dataset entries and evaluation datasets appended at a taken position are
  renumbered before their positions become unique.
- Indexes, built in place.

Safety (backend/AGENTS.md > "Database migrations (safety)"): every step probes
first, so a re-run or a fresh install (``create_all`` + ``stamp head``) takes
no lock; each lock-taking ALTER runs behind ``terminate_lock_holders`` +
``set_lock_timeout`` and commits on its own, so no table stays locked while
another is altered; the ADDs are metadata-only (nullable or a constant
default); the CHECK and the foreign keys are added ``NOT VALID`` and validated
in a later transaction, which reads and writes go on through. The data steps
take row locks: the renumbering in bounded batches; the tolerance copy and the
secret encryption in one transaction each, over ``evaluations``, a
configuration table of a few rows per agent.

Downgrade: everything is dropped, the key reversed and the tolerance copied
back into ``speed_tolerance``.
``entry_id`` is made NOT NULL again, through a validated CHECK, only when no
result lost its entry; a renumbered position keeps its new number.

Revision ID: e1780evp001
Revises: v6539rret01
Create Date: 2026-10-19 00:00:00.000000
"""

import json
import logging
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON, JSONB, UUID

from alembic import op
from app.core.migration_ddl import (
    create_index_in_place,
    set_lock_timeout,
    terminate_lock_holders,
)
from app.core.utils import null_non_finite_numbers

revision: str = "e1780evp001"
down_revision: str | None = "v6539rret01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger(__name__)

# Columns added to existing tables: name and DDL, nullable or a constant default.
_NEW_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "datasets": (
        ("current_version", "INTEGER NOT NULL DEFAULT 0"),
        ("output_schema", "JSON"),
        ("family", "VARCHAR(32)"),
        ("label_vocabulary", "JSON"),
    ),
    "dataset_entries": (("output_schema", "JSON"),),
    "evaluations": (
        ("custom_params", "JSON"),
        ("scorer_selection", "JSON"),
        ("reuse_default", "VARCHAR(20) NOT NULL DEFAULT 'off'"),
        ("judge_max_tokens", "INTEGER NOT NULL DEFAULT 8192"),
        ("tags", "JSONB"),
        ("family", "VARCHAR(32)"),
        ("overall_quality_metrics", "JSON"),
        ("overall_performance_metrics", "JSON"),
        ("field_scoring_overrides", "JSON"),
        ("apply_prompt_guards", "BOOLEAN NOT NULL DEFAULT false"),
        ("require_tool_approval", "BOOLEAN NOT NULL DEFAULT false"),
    ),
    "evaluation_agents": (
        ("tags", "JSONB"),
        ("is_baseline", "BOOLEAN NOT NULL DEFAULT false"),
        ("output_extractor", "JSON"),
    ),
    "evaluation_runs": (
        ("pinned_dataset_versions", "JSON"),
        ("tags", "JSONB"),
        ("anomaly_count", "INTEGER NOT NULL DEFAULT 0"),
        ("quality_score", "DOUBLE PRECISION"),
        ("performance_score", "DOUBLE PRECISION"),
        ("headline_details", "JSON"),
        ("metric_suite_version", "VARCHAR(32)"),
        ("scorer_selection", "JSON"),
        ("owner_execution_id", "UUID"),
        ("execution_mode", "VARCHAR(20)"),
        ("apply_prompt_guards", "BOOLEAN"),
        ("require_tool_approval", "BOOLEAN"),
        ("rescore_error", "TEXT"),
    ),
    "evaluation_results": (
        ("version_entry_id", "UUID"),
        ("input_fingerprint", "VARCHAR(64)"),
        ("is_reused", "BOOLEAN NOT NULL DEFAULT false"),
        ("reused_from_result_id", "UUID"),
        ("execution_anomaly_kind", "VARCHAR(24)"),
        ("is_judge_failure", "BOOLEAN NOT NULL DEFAULT false"),
    ),
    "evaluation_episodes": (("first_turn_details", "JSONB"),),
    "knowledge_bases": (("revision", "INTEGER NOT NULL DEFAULT 0"),),
}

# (index, table, definition, unique, required). A performance index the models
# declare is not required: a failed build is logged and ``_ensure_model_indexes``
# builds it after the upgrade. The baseline index enforces at most one baseline
# per evaluation.
_INDEXES: tuple[tuple[str, str, str, bool, bool], ...] = (
    ("ix_evaluation_results_input_fingerprint", "evaluation_results", "(input_fingerprint)", False, False),
    (
        "ix_evaluation_results_reused_from_result_id",
        "evaluation_results",
        "(reused_from_result_id) WHERE reused_from_result_id IS NOT NULL",
        False,
        False,
    ),
    (
        "ix_evaluation_results_version_entry_id",
        "evaluation_results",
        "(version_entry_id) WHERE version_entry_id IS NOT NULL",
        False,
        False,
    ),
    (
        "ix_dataset_version_entries_entry_id",
        "dataset_version_entries",
        "(entry_id) WHERE entry_id IS NOT NULL",
        False,
        False,
    ),
    ("uq_eval_agent_baseline", "evaluation_agents", "(evaluation_id) WHERE is_baseline", True, True),
    (
        "ix_evaluation_runs_stranded_candidates",
        "evaluation_runs",
        "(id, owner_execution_id) WHERE status = 'running' AND completed_at IS NULL",
        False,
        False,
    ),
    (
        "ix_orchestration_exec_rescore_run_ids",
        "orchestration_executions",
        "USING gin ((payload -> 'run_ids')) WHERE adapter = 'evaluation' AND state IN ('pending', 'running')",
        False,
        False,
    ),
    (
        "ix_orchestration_exec_sub_agent_cap_scope",
        "orchestration_executions",
        "((payload ->> 'cap_scope')) WHERE adapter = 'sub_agent' "
        "AND state IN ('pending', 'running') AND (payload ->> 'cap_scope') IS NOT NULL",
        False,
        False,
    ),
)

_ENTRY_KEY = "evaluation_results_entry_id_fkey"
# The tolerance a new evaluation gets, as the model declares it.
_TOLERANCE_DEFAULT = "5.0"
# The keys of the two columns added to ``evaluation_results``: name, column, referenced table.
_RESULT_KEYS = (
    ("evaluation_results_version_entry_id_fkey", "version_entry_id", "dataset_version_entries"),
    ("evaluation_results_reused_from_result_id_fkey", "reused_from_result_id", "evaluation_results"),
)


def _tables(conn: sa.engine.Connection) -> set[str]:
    return set(sa.inspect(conn).get_table_names())


def _columns(conn: sa.engine.Connection, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns(table)}


def _has_constraint(conn: sa.engine.Connection, name: str) -> bool:
    return bool(conn.execute(sa.text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": name}).scalar())


def _commit() -> None:
    """End the step's transaction, so its locks go before the next one takes its own."""
    with op.get_context().autocommit_block():
        pass


def _validate(conn: sa.engine.Connection, table: str, name: str) -> None:
    """Validate a ``NOT VALID`` constraint: a scan under SHARE UPDATE EXCLUSIVE, reads and writes go on."""
    validated = conn.execute(sa.text("SELECT convalidated FROM pg_constraint WHERE conname = :n"), {"n": name}).scalar()
    if validated is None or validated:
        return
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")


# ── Tables ───────────────────────────────────────────────────────────────────


def _create_tables(conn: sa.engine.Connection) -> None:
    """The four new tables, as the models declare them.

    On an existing install the startup ``create_all`` has already built them; the
    Alembic CLI path builds them here. Each carries a foreign key to an existing
    table, whose lock the belt bounds.
    """
    tables = _tables(conn)
    set_lock_timeout(conn)
    if "dataset_versions" not in tables:
        op.create_table(
            "dataset_versions",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "dataset_id", UUID(as_uuid=True), sa.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column("version_number", sa.Integer(), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("entry_count", sa.Integer(), nullable=False),
            sa.Column("output_schema", JSON, nullable=True),
            sa.Column("label_vocabulary", JSON, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("dataset_id", "version_number", name="uq_dataset_version_number"),
        )
    if "dataset_version_entries" not in tables:
        op.create_table(
            "dataset_version_entries",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "version_id",
                UUID(as_uuid=True),
                sa.ForeignKey("dataset_versions.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "entry_id", UUID(as_uuid=True), sa.ForeignKey("dataset_entries.id", ondelete="SET NULL"), nullable=True
            ),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("input_prompt", sa.Text(), nullable=True),
            sa.Column("expected_answer", sa.Text(), nullable=False),
            sa.Column("evaluation_prompt", sa.Text(), nullable=True),
            sa.Column("file_ids", JSON, nullable=True),
            sa.Column("output_schema", JSON, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "evaluation_run_params" not in tables:
        op.create_table(
            "evaluation_run_params",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "run_id", UUID(as_uuid=True), sa.ForeignKey("evaluation_runs.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column("key", sa.String(100), nullable=False),
            sa.Column("value", sa.Text(), nullable=True),
            sa.Column("source", sa.String(20), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("run_id", "key", name="uq_eval_run_param_key"),
            sa.CheckConstraint("source IN ('auto', 'evaluation', 'manual')", name="ck_eval_run_params_source"),
        )
    if "evaluation_metrics" not in tables:
        op.create_table(
            "evaluation_metrics",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "result_id",
                UUID(as_uuid=True),
                sa.ForeignKey("evaluation_results.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "run_id", UUID(as_uuid=True), sa.ForeignKey("evaluation_runs.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column("key", sa.String(100), nullable=False),
            sa.Column("value", sa.Float(), nullable=True),
            sa.Column("numerator", sa.Float(), nullable=True),
            sa.Column("denominator", sa.Float(), nullable=True),
            sa.Column("class_counts", JSONB, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("result_id", "key", name="uq_eval_metric_key"),
            sa.Index("ix_eval_metrics_run_key", "run_id", "key"),
        )


# ── Columns and constraints ──────────────────────────────────────────────────


def _add_columns(conn: sa.engine.Connection) -> None:
    for table, columns in _NEW_COLUMNS.items():
        existing = _columns(conn, table)
        pending = [f"ADD COLUMN IF NOT EXISTS {name} {ddl}" for name, ddl in columns if name not in existing]
        if not pending:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} " + ", ".join(pending))
        _commit()


def _add_result_keys(conn: sa.engine.Connection) -> None:
    """The keys of the added result columns, ``NOT VALID``: an inline ``REFERENCES`` would scan the table locked."""
    for name, column, referenced in _RESULT_KEYS:
        if _has_constraint(conn, name):
            continue
        terminate_lock_holders(conn, "evaluation_results")
        set_lock_timeout(conn)
        op.execute(
            f"ALTER TABLE evaluation_results ADD CONSTRAINT {name} FOREIGN KEY ({column}) "
            f"REFERENCES {referenced}(id) ON DELETE SET NULL NOT VALID"
        )
        _commit()


def _expand_tolerance(conn: sa.engine.Connection) -> None:
    """``performance_tolerance`` beside ``speed_tolerance``, not a rename.

    The column, the copy and the old column's default in one transaction, so a
    retry finds the column absent and copies again. The copy rewrites every
    evaluation, a configuration table of a few rows per agent.
    """
    columns = _columns(conn, "evaluations")
    if "performance_tolerance" in columns:
        return
    terminate_lock_holders(conn, "evaluations")
    set_lock_timeout(conn)
    op.execute(
        "ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS performance_tolerance "
        f"DOUBLE PRECISION NOT NULL DEFAULT {_TOLERANCE_DEFAULT}"
    )
    if "speed_tolerance" in columns:
        op.execute("UPDATE evaluations SET performance_tolerance = speed_tolerance")
        op.execute(f"ALTER TABLE evaluations ALTER COLUMN speed_tolerance SET DEFAULT {_TOLERANCE_DEFAULT}")


def _restore_speed_tolerance(conn: sa.engine.Connection) -> None:
    """Downgrade: ``speed_tolerance`` with the current values and no default, as the previous code has it."""
    columns = _columns(conn, "evaluations")
    if "performance_tolerance" not in columns:
        return
    terminate_lock_holders(conn, "evaluations")
    set_lock_timeout(conn)
    if "speed_tolerance" not in columns:
        op.execute(
            f"ALTER TABLE evaluations ADD COLUMN speed_tolerance DOUBLE PRECISION NOT NULL DEFAULT {_TOLERANCE_DEFAULT}"
        )
    op.execute("UPDATE evaluations SET speed_tolerance = performance_tolerance")
    op.execute("ALTER TABLE evaluations ALTER COLUMN speed_tolerance DROP DEFAULT")
    op.execute("ALTER TABLE evaluations DROP COLUMN performance_tolerance")


def _add_reuse_default_check(conn: sa.engine.Connection) -> None:
    if _has_constraint(conn, "ck_evaluations_reuse_default"):
        return
    terminate_lock_holders(conn, "evaluations")
    set_lock_timeout(conn)
    op.execute(
        "ALTER TABLE evaluations ADD CONSTRAINT ck_evaluations_reuse_default "
        "CHECK (reuse_default IN ('off', 'on')) NOT VALID"
    )


def _entry_key_rule(conn: sa.engine.Connection) -> str | None:
    """``evaluation_results.entry_id``'s delete rule (``CASCADE``, ``SET NULL``), or ``None``."""
    return conn.execute(
        sa.text(
            "SELECT rc.delete_rule FROM information_schema.referential_constraints rc "
            "WHERE rc.constraint_schema = current_schema() AND rc.constraint_name = :n"
        ),
        {"n": _ENTRY_KEY},
    ).scalar()


def _set_entry_key(conn: sa.engine.Connection, rule: str) -> None:
    """Point ``evaluation_results.entry_id`` at ``dataset_entries`` ``ON DELETE`` *rule*.

    Swapped in one ALTER and added ``NOT VALID``, so no scan runs under the
    exclusive lock; :func:`_validate` checks the rows in a later transaction.
    """
    if _entry_key_rule(conn) == rule:
        return
    terminate_lock_holders(conn, "evaluation_results")
    set_lock_timeout(conn)
    op.execute(
        f"ALTER TABLE evaluation_results DROP CONSTRAINT IF EXISTS {_ENTRY_KEY}, "
        f"ADD CONSTRAINT {_ENTRY_KEY} FOREIGN KEY (entry_id) REFERENCES dataset_entries(id) "
        f"ON DELETE {rule} NOT VALID"
    )


def _set_entry_id_not_null(conn: sa.engine.Connection) -> None:
    """``entry_id`` NOT NULL without a scan under the exclusive lock: a validated CHECK proves it first."""
    check = "ck_evaluation_results_entry_id_not_null"
    if not _has_constraint(conn, check):
        terminate_lock_holders(conn, "evaluation_results")
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE evaluation_results ADD CONSTRAINT {check} CHECK (entry_id IS NOT NULL) NOT VALID")
        _commit()
    _validate(conn, "evaluation_results", check)
    _commit()
    terminate_lock_holders(conn, "evaluation_results")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE evaluation_results ALTER COLUMN entry_id SET NOT NULL")
    op.execute(f"ALTER TABLE evaluation_results DROP CONSTRAINT IF EXISTS {check}")
    _commit()


def _relax_entry_id(conn: sa.engine.Connection) -> None:
    """A result survives the deletion of its live entry: its snapshot entry still describes it."""
    entry = next(c for c in sa.inspect(conn).get_columns("evaluation_results") if c["name"] == "entry_id")
    if not entry["nullable"]:
        terminate_lock_holders(conn, "evaluation_results")
        set_lock_timeout(conn)
        op.execute("ALTER TABLE evaluation_results ALTER COLUMN entry_id DROP NOT NULL")
    _set_entry_key(conn, "SET NULL")


# ── Resume secrets ───────────────────────────────────────────────────
# ``evaluations.trigger_config.resume_secret`` held the callback's signing secret
# in clear; the callback now reads only ``resume_secret_enc``, Fernet-encrypted.

_CLEAR = "resume_secret"
_ENC = "resume_secret_enc"


def _rows_holding(conn: sa.engine.Connection, key: str) -> list[tuple[object, dict]]:
    """The evaluations whose ``trigger_config`` object holds *key*.

    Read as text and filtered here, not in SQL: a ``::jsonb`` cast or a JSON
    operator refuses a row holding a ``\\u0000`` or lone-surrogate escape, and
    the error fails the whole statement (``core/json_sql.py``).
    """
    rows = conn.execute(
        sa.text("SELECT id, trigger_config::text FROM evaluations WHERE trigger_config IS NOT NULL")
    ).all()
    configs = [(row_id, json.loads(text)) for row_id, text in rows]
    return [(row_id, config) for row_id, config in configs if isinstance(config, dict) and key in config]


def _store(conn: sa.engine.Connection, row_id: object, config: dict) -> None:
    """Write *config* back; a number too large for a float (``1e400``) or for ``numeric`` (``1e131072``) reads as ``inf`` and is written as null.

    The row must not stay as it was: the clear secret it holds is dropped or encrypted here.
    """
    text = json.dumps(null_non_finite_numbers(config), allow_nan=False)
    conn.execute(
        sa.text("UPDATE evaluations SET trigger_config = CAST(:config AS json) WHERE id = :id"),
        {"config": text, "id": row_id},
    )


def encrypt_clear_secrets(conn: sa.engine.Connection) -> None:
    """Encrypt every clear secret into ``resume_secret_enc`` and drop the clear key.

    A secret is read as a write reads it (``configured_secret``: a JSON number is
    its digits). One no sender could sign with, or a vault reference the callback
    does not resolve, is dropped and logged: it already failed every callback.
    """
    from app.core.secret_refs import holds_secret_reference
    from app.core.security import UnusableSecretError, configured_secret, encrypt_value

    for row_id, config in _rows_holding(conn, _CLEAR):
        value = config.pop(_CLEAR)
        try:
            secret = configured_secret(value)
        except UnusableSecretError:
            secret = None
            logger.warning("Evaluation %s: its resume secret could not sign a callback; dropped", row_id)
        if secret is not None and holds_secret_reference(secret):
            secret = None
            logger.warning("Evaluation %s: its resume secret is a vault reference; dropped", row_id)
        if secret is not None and not config.get(_ENC):
            config[_ENC] = encrypt_value(secret)
        _store(conn, row_id, config)


def decrypt_secrets(conn: sa.engine.Connection) -> None:
    """Put every ``resume_secret_enc`` back in clear as ``resume_secret`` (downgrade)."""
    from cryptography.fernet import InvalidToken

    from app.core.security import decrypt_stored_value

    for row_id, config in _rows_holding(conn, _ENC):
        stored = config.pop(_ENC)
        try:
            config[_CLEAR] = decrypt_stored_value(stored)
        except (InvalidToken, AttributeError, TypeError):
            logger.warning("Evaluation %s: its resume secret cannot be decrypted; dropped", row_id)
        _store(conn, row_id, config)


# ── Unique positions ─────────────────────────────────────────────────
# ``max(position) or -1`` gave every item appended after the first position 0, and
# an evaluation package names an entry by its position.

# Each table, the column its positions are numbered within, and its unique index.
_ORDERED = (
    ("dataset_entries", "dataset_id", "uq_dataset_entries_position"),
    ("evaluation_datasets", "evaluation_id", "uq_evaluation_datasets_position"),
)
_BATCH = 100
# Builds tried before the upgrade fails: each failure renumbers the tie an older
# replica appended meanwhile.
_BUILD_ATTEMPTS = 3


def renumber_ties(conn) -> int:
    """Renumber every parent holding a tied position, in append order; returns how many."""
    renumbered = 0
    for table, parent, _index in _ORDERED:
        if not sa.inspect(conn).has_table(table):
            continue
        tied = sa.text(
            f"SELECT {parent} FROM {table} WHERE {parent} > :after GROUP BY {parent} "  # noqa: S608 - names are literals above
            f"HAVING count(*) > count(DISTINCT position) ORDER BY {parent} LIMIT :batch"
        )
        renumber = sa.text(
            f"UPDATE {table} t SET position = r.n FROM ("  # noqa: S608 - names are literals above
            f"SELECT id, row_number() OVER (PARTITION BY {parent} "
            "ORDER BY position, created_at, id) - 1 AS n "
            f"FROM {table} WHERE {parent} IN :parents) r "
            "WHERE t.id = r.id AND t.position <> r.n"
        ).bindparams(sa.bindparam("parents", expanding=True))
        after = "00000000-0000-0000-0000-000000000000"
        while True:
            parents = [row[0] for row in conn.execute(tied, {"after": after, "batch": _BATCH})]
            if not parents:
                break
            conn.execute(renumber, {"parents": parents})
            renumbered += len(parents)
            after = str(parents[-1])
    return renumbered


def add_unique_positions(conn) -> None:
    """Build each unique position index, renumbering a tie appended during the build."""
    for table, parent, index in _ORDERED:
        if not sa.inspect(conn).has_table(table):
            continue
        for attempt in range(1, _BUILD_ATTEMPTS + 1):
            try:
                create_index_in_place(conn, index, table, f"({parent}, position)", unique=True, required=True)
                break
            except sa.exc.IntegrityError:
                if attempt == _BUILD_ATTEMPTS:
                    raise
                renumber_ties(conn)


# ── Upgrade / downgrade ──────────────────────────────────────────────────────


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    # Each step commits before the next, so it holds its locks alone.
    _create_tables(conn)
    _commit()
    _add_columns(conn)
    _expand_tolerance(conn)
    _commit()
    _add_reuse_default_check(conn)
    _commit()
    _relax_entry_id(conn)
    _commit()
    _add_result_keys(conn)
    for table, name in [
        ("evaluations", "ck_evaluations_reuse_default"),
        ("evaluation_results", _ENTRY_KEY),
        *[("evaluation_results", name) for name, _column, _referenced in _RESULT_KEYS],
    ]:
        _validate(conn, table, name)
        _commit()
    set_lock_timeout(conn)
    encrypt_clear_secrets(conn)
    with op.get_context().autocommit_block():
        renumber_ties(conn)
        add_unique_positions(conn)
        for index, table, definition, unique, required in _INDEXES:
            create_index_in_place(conn, index, table, definition, unique=unique, required=required)


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    tables = _tables(conn)

    for index, table in [(i, t) for i, t, *_ in reversed(_INDEXES)] + [(i, t) for t, _p, i in _ORDERED]:
        if conn.execute(sa.text("SELECT to_regclass(:index)"), {"index": index}).scalar() is None:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"DROP INDEX IF EXISTS {index}")
        _commit()

    set_lock_timeout(conn)
    decrypt_secrets(conn)
    _commit()

    # The key cascades again; NOT NULL only when no result lost its entry.
    _set_entry_key(conn, "CASCADE")
    _commit()
    _validate(conn, "evaluation_results", _ENTRY_KEY)
    _commit()
    if not conn.execute(sa.text("SELECT 1 FROM evaluation_results WHERE entry_id IS NULL LIMIT 1")).scalar():
        _set_entry_id_not_null(conn)

    if _has_constraint(conn, "ck_evaluations_reuse_default"):
        terminate_lock_holders(conn, "evaluations")
        set_lock_timeout(conn)
        op.execute("ALTER TABLE evaluations DROP CONSTRAINT IF EXISTS ck_evaluations_reuse_default")
    _restore_speed_tolerance(conn)
    _commit()

    for table, columns in reversed(_NEW_COLUMNS.items()):
        existing = _columns(conn, table)
        present = [name for name, _ddl in columns if name in existing]
        if not present:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} " + ", ".join(f"DROP COLUMN IF EXISTS {name}" for name in present))
        _commit()

    # Children first. Each drop locks the tables its keys reference.
    for table in ("evaluation_metrics", "evaluation_run_params", "dataset_version_entries", "dataset_versions"):
        if table in tables:
            set_lock_timeout(conn)
            op.execute(f"DROP TABLE IF EXISTS {table}")
            _commit()
