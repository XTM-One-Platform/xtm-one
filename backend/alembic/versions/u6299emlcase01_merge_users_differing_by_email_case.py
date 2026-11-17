"""merge users that differ only by email case, then index lower(email)

SSO sign-in matched and stored the IdP's email case-sensitively while every
other path lowercases it, so a person could hold ``jane@x`` and ``Jane@x``.
Only SSO ever wrote a mixed-case address, which makes the lowercase row the
original account and every other case variant a duplicate of it.

Per ``lower(email)`` group:

* the survivor is the row whose email is already lowercase (it keeps its id,
  password, role and profile); with none, the oldest row, lowercased;
* it takes the SSO link of a duplicate when it has none, and keeps its own
  ``is_active`` (a duplicate an administrator disabled does not disable it);
* every row that references a duplicate is moved to the survivor, then the
  duplicate is deleted. References are read from ``pg_constraint`` (a
  ``users`` foreign key mixes ``CASCADE`` with ``SET NULL``, and deleting first
  would destroy what the person created under the SSO account), plus the
  columns that hold a user id without a foreign key and the JSON sharing lists.
  A row the survivor already has an equivalent of (a UNIQUE key) is not moved:
  it goes with the duplicate.

Then every remaining mixed-case email is lowercased and ``ix_users_email_lower``
(UNIQUE, ``lower(email)``) makes the state impossible to reach again.

Re-runnable: with nothing to merge or lowercase, only the index probe runs.
``downgrade`` drops the index; a merge is not undone, the duplicate rows
carried no information the survivor lacks.

Revision ID: u6299emlcase01
Revises: x6367grpmgr01
Create Date: 2026-10-22 12:00:00.000000
"""
# ruff: noqa: S608 - the only values interpolated into these statements are
# table and column names read from the catalog (or this module's constants),
# quoted with quote_ident; every id is a bound parameter.

import json
import logging
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from alembic import op
from app.core.migration_ddl import (
    create_index_in_place,
    set_lock_timeout,
    terminate_lock_holders,
)

revision: str = "u6299emlcase01"
down_revision: str | Sequence[str] | None = "x6367grpmgr01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger(__name__)

_INDEX = "ix_users_email_lower"

#: Columns that hold a user id with no foreign key, as ``(table, column)``.
_UNKEYED_USER_IDS: tuple[tuple[str, str], ...] = (
    ("model_catalog_announcement_recipients", "user_id"),
    ("llm_scores", "user_id"),
    ("integrations", "oauth_token_owner_id"),
    ("persona_improvements", "requested_by_user_id"),
    ("persona_improvements", "decided_by_user_id"),
    ("platform_settings", "tou_accepted_by_user_id"),
    ("video_music", "user_id"),
    ("sandbox_workspaces", "user_id"),
    ("llm_traces", "user_id"),
    ("assignment_run_conversation_notices", "user_id"),
    ("video_pronunciation_lexicons", "created_by_user_id"),
    # Added after this revision: skipped where they do not exist yet.
    ("llm_traces", "attributed_user_id"),
    ("llm_daily_stats", "attributed_user_id"),
    # Also added after this revision, the same way.
    ("agent_tombstones", "user_id"),
    ("roi_run_records", "user_id"),
    ("roi_cost_facts", "user_id"),
)

#: JSON columns that list user ids (an array, or an object keyed by id). The
#: ids are UUID strings, so a text replacement cannot hit anything else.
_JSON_USER_ID_COLUMNS = ("shared_user_ids", "shared_user_permissions", "excluded_user_ids")

_UNIQUE_VIOLATION = "23505"


def _is_unique_violation(exc: IntegrityError) -> bool:
    return (
        getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    ) == _UNIQUE_VIOLATION


def _groups(conn) -> list[list]:
    """The users of every ``lower(email)`` shared by more than one row."""
    rows = conn.execute(
        sa.text(
            "SELECT id, email, created_at, is_active, sso_provider, sso_subject, "
            "       lower(email) AS key "
            "FROM users "
            "WHERE lower(email) IN ("
            "  SELECT lower(email) FROM users GROUP BY 1 HAVING count(*) > 1) "
            "ORDER BY lower(email), (email = lower(email)) DESC, created_at, id"
        )
    ).all()
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row.key, []).append(row)
    return list(grouped.values())


def _references(conn) -> list[tuple[str, str]]:
    """``(table, column)`` for every single-column foreign key onto ``users.id``."""
    return [
        (row[0], row[1])
        for row in conn.execute(
            sa.text(
                "SELECT c.conrelid::regclass::text, quote_ident(a.attname) "
                "FROM pg_constraint c "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
                "WHERE c.contype = 'f' AND c.confrelid = 'users'::regclass "
                "  AND array_length(c.conkey, 1) = 1"
            )
        )
    ]


def _move(conn, table: str, column: str, pairs: list[tuple]) -> int:
    """Point ``table.column`` at the survivor instead of each duplicate.

    One set-based UPDATE; if it hits a UNIQUE key (the survivor already has an
    equivalent row) it is redone row by row, skipping only the rows that
    collide. Returns how many rows stayed behind.
    """
    losers = [loser for loser, _ in pairs]
    moved_sql = (
        f"UPDATE {table} AS t SET {column} = m.winner "
        f"FROM (SELECT unnest(CAST(:losers AS uuid[])) AS loser, "
        f"             unnest(CAST(:winners AS uuid[])) AS winner) AS m "
        f"WHERE t.{column} = m.loser"
    )
    params = {"losers": losers, "winners": [winner for _, winner in pairs]}
    try:
        with conn.begin_nested():
            conn.execute(sa.text(moved_sql), params)
        return 0
    except IntegrityError as exc:
        if not _is_unique_violation(exc):
            raise

    left = 0
    rows = conn.execute(
        sa.text(
            f"SELECT t.ctid::text, m.winner FROM {table} t "
            f"JOIN (SELECT unnest(CAST(:losers AS uuid[])) AS loser, "
            f"             unnest(CAST(:winners AS uuid[])) AS winner) m "
            f"ON t.{column} = m.loser"
        ),
        params,
    ).all()
    for ctid, winner in rows:
        try:
            with conn.begin_nested():
                conn.execute(
                    sa.text(
                        f"UPDATE {table} SET {column} = :winner "
                        f"WHERE ctid = CAST(CAST(:ctid AS text) AS tid)"
                    ),
                    {"winner": winner, "ctid": ctid},
                )
        except IntegrityError as exc:
            if not _is_unique_violation(exc):
                raise
            left += 1
    return left


#: Share tiers in ascending order of power, spelled out: a migration keeps
#: meaning what it meant on the day it ran.
_TIER_RANK = {"read_only": 0, "config": 1, "config_with_chats": 2}


def _fold_ids(value, remap: dict[str, str]):
    """A list of ids, or an ``{id: tier}`` object, with duplicates renamed.

    Ids are compared in lowercase (they are stored as typed). When the survivor
    and a duplicate are both present they become one entry: a list keeps it
    once, an object keeps the highest tier, so the merged person never loses
    access they had under either account.
    """
    if isinstance(value, list):
        out: list = []
        seen: set[str] = set()
        for item in value:
            item = remap.get(str(item).lower(), item)
            if str(item).lower() not in seen:
                seen.add(str(item).lower())
                out.append(item)
        return out
    if isinstance(value, dict):
        folded: dict = {}
        held: dict[str, str] = {}  # lowercase id -> the key stored in ``folded``
        for key, tier in value.items():
            key = remap.get(str(key).lower(), key)
            stored = held.setdefault(str(key).lower(), key)
            if stored not in folded or _TIER_RANK.get(tier, 0) > _TIER_RANK.get(
                folded[stored], 0
            ):
                folded[stored] = tier
        return folded
    return value


def _rewrite_json_lists(conn, pairs: list[tuple]) -> None:
    """Replace a duplicate's id by the survivor's in the JSON sharing lists.

    The columns are found by name, not by table: any ``json`` / ``jsonb``
    column called like one of ``_JSON_USER_ID_COLUMNS`` is read, and since only
    a UUID string equal to a duplicate's id is rewritten, a column that reuses
    the name for something else is untouched. The survivor's id is written in
    canonical lowercase. Each table is scanned once, for every pair, and only
    the rows that name a duplicate are rewritten.
    """
    remap = {str(loser).lower(): str(winner) for loser, winner in pairs}
    tables = conn.execute(
        sa.text(
            "SELECT table_name, column_name, data_type "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND column_name = ANY(:names) "
            "  AND data_type IN ('json', 'jsonb')"
        ),
        {"names": list(_JSON_USER_ID_COLUMNS)},
    ).all()
    for table, column, data_type in tables:
        name = f'"{table}"'
        col = f'"{column}"'
        rows = conn.execute(
            sa.text(
                f"SELECT ctid::text, {col}::text FROM {name} "
                f"WHERE {col}::text ~* CAST(:pattern AS text)"
            ),
            {"pattern": "|".join(remap)},
        ).all()
        for ctid, raw in rows:
            conn.execute(
                sa.text(
                    f"UPDATE {name} SET {col} = CAST(:v AS {data_type}) "
                    f"WHERE ctid = CAST(CAST(:c AS text) AS tid)"
                ),
                {"v": json.dumps(_fold_ids(json.loads(raw), remap)), "c": ctid},
            )
        if rows:
            logger.info("%s.%s: %d sharing list(s) rewritten", table, column, len(rows))


def _fold_quota_usage(conn, pairs: list[tuple]) -> None:
    """Add each duplicate's quota counters to the survivor's.

    One row per user, usage type, model, period and agent: a counter the
    survivor already has for the same period would make the generic move drop
    the duplicate's, and the person's usage that period would be under-counted
    against their quota. These rows are the durable side only: the counters of
    the current period live in Redis and are not carried over.
    """
    params = {
        "losers": [loser for loser, _ in pairs],
        "winners": [winner for _, winner in pairs],
    }
    conn.execute(
        sa.text(
            "WITH m AS (SELECT unnest(CAST(:losers AS uuid[])) AS loser, "
            "                  unnest(CAST(:winners AS uuid[])) AS winner) "
            "INSERT INTO quota_usage (id, user_id, usage_type, model, period, agent_id, "
            "                        count, updated_at) "
            "SELECT gen_random_uuid(), m.winner, x.usage_type, x.model, x.period, "
            "       x.agent_id, sum(x.count), now() "
            "FROM quota_usage x JOIN m ON x.user_id = m.loser "
            "GROUP BY m.winner, x.usage_type, x.model, x.period, x.agent_id "
            "ON CONFLICT (user_id, usage_type, model, period, agent_id) "
            "DO UPDATE SET count = quota_usage.count + EXCLUDED.count, updated_at = now()"
        ),
        params,
    )
    conn.execute(
        sa.text("DELETE FROM quota_usage WHERE user_id = ANY(CAST(:losers AS uuid[]))"),
        params,
    )


def _merge_histograms(histograms: list) -> dict | None:
    """Add the buckets of the stored latency histograms (``{"v": 1, "b": {...}}``).

    Spelled out here, like the version, so the revision keeps meaning what it
    meant. A row with no histogram (written before they existed) or a version
    other than 1 makes the merge ``None``: the read path then falls back to the
    stored percentiles and flags the result as approximate, which is honest
    where a union of some of the samples would not be.
    """
    buckets: dict[str, int] = {}
    for histogram in histograms:
        if isinstance(histogram, str):
            histogram = json.loads(histogram)
        if not histogram or histogram.get("v") != 1:
            return None
        for bucket, count in (histogram.get("b") or {}).items():
            buckets[bucket] = buckets.get(bucket, 0) + int(count)
    return {"v": 1, "b": buckets}


def _merge_daily_rows(rows: list) -> dict:
    """One ``llm_daily_stats`` row from the rows of one key.

    Counts, tokens and cost add, the mean duration is weighted by
    ``trace_count`` and the histogram's buckets add. The percentiles and the
    feedback score cannot be recombined; they are those of the busiest row.
    """
    busiest = max(rows, key=lambda row: row["trace_count"])
    costs = [row["total_cost"] for row in rows if row["total_cost"] is not None]
    timed = [row for row in rows if row["avg_duration_ms"] is not None]
    weight = sum(row["trace_count"] for row in timed)
    return {
        "trace_count": sum(row["trace_count"] for row in rows),
        "error_count": sum(row["error_count"] for row in rows),
        "total_input_tokens": sum(row["total_input_tokens"] for row in rows),
        "total_output_tokens": sum(row["total_output_tokens"] for row in rows),
        "total_cost": sum(costs) if costs else None,
        "avg_duration_ms": (
            sum(row["avg_duration_ms"] * row["trace_count"] for row in timed) / weight
            if weight
            else None
        ),
        "p50_duration_ms": busiest["p50_duration_ms"],
        "p95_duration_ms": busiest["p95_duration_ms"],
        "avg_feedback_score": busiest["avg_feedback_score"],
        "duration_histogram": _merge_histograms([row["duration_histogram"] for row in rows]),
    }


_DAILY_KEY = ("date", "agent_id", "model", "source")


def _fold_daily_stats(conn, pairs: list[tuple]) -> None:
    """Add each duplicate's daily usage rollup to the survivor's.

    ``llm_daily_stats`` is unique per day, agent, model, source and user, so the
    generic move would leave a collision behind, naming a deleted user. The rows
    of one key are merged by ``_merge_daily_rows`` into the survivor's row.
    """
    if not _column_exists(conn, "llm_daily_stats", "user_id"):
        return
    params = {
        "losers": [loser for loser, _ in pairs],
        "winners": [winner for _, winner in pairs],
    }
    winner_of = dict(pairs)
    rows = conn.execute(
        sa.text(
            "SELECT * FROM llm_daily_stats WHERE user_id = ANY(CAST(:losers AS uuid[])) "
            "UNION ALL "
            "SELECT s.* FROM llm_daily_stats s WHERE s.user_id = ANY(CAST(:winners AS uuid[])) "
            "  AND EXISTS (SELECT 1 FROM llm_daily_stats x "
            "              WHERE x.user_id = ANY(CAST(:losers AS uuid[])) "
            "                AND x.date = s.date AND x.model = s.model AND x.source = s.source "
            "                AND x.agent_id IS NOT DISTINCT FROM s.agent_id)"
        ),
        params,
    ).mappings().all()
    keyed: dict[tuple, list] = {}
    for row in rows:
        user = winner_of.get(row["user_id"], row["user_id"])
        keyed.setdefault((user, *(row[k] for k in _DAILY_KEY)), []).append(row)

    for (user, *key), group in keyed.items():
        merged = _merge_daily_rows(group)
        histogram = merged.pop("duration_histogram")
        values = ", ".join(f"{column} = EXCLUDED.{column}" for column in (*merged, "duration_histogram"))
        conn.execute(
            sa.text(
                "INSERT INTO llm_daily_stats (id, user_id, date, agent_id, model, source, "
                f"  {', '.join(merged)}, duration_histogram) "
                "VALUES (gen_random_uuid(), :user_id, :date, :agent_id, :model, :source, "
                f"  {', '.join(':' + column for column in merged)}, "
                "  CAST(:duration_histogram AS jsonb)) "
                # By name: a later migration widened the grain under the same constraint name.
                "ON CONFLICT ON CONSTRAINT uq_llm_daily_stats_date_agent_model_source_user "
                f"DO UPDATE SET {values}"
            ),
            {
                **dict(zip(_DAILY_KEY, key, strict=True)),
                **merged,
                "user_id": user,
                "duration_histogram": None if histogram is None else json.dumps(histogram),
            },
        )
    conn.execute(
        sa.text("DELETE FROM llm_daily_stats WHERE user_id = ANY(CAST(:losers AS uuid[]))"),
        params,
    )


def _fold_state(conn, group: list) -> tuple[list[tuple], list]:
    """Decide the survivor and fold the group's account state into it.

    The survivor keeps its own SSO link; with none it takes the newest
    duplicate's that has one, so an older duplicate's link is dropped. Its
    ``is_active`` is left as it is: disabling the stray SSO duplicate is what an
    administrator would have done about this bug, and must not lock the person
    out of the account that remains.
    """
    survivor, losers = group[0], group[1:]
    link = next((row for row in reversed(losers) if row.sso_subject), None)

    conn.execute(
        sa.text(
            "UPDATE users SET "
            "  sso_provider = COALESCE(sso_provider, :provider), "
            "  sso_subject = CASE WHEN sso_provider IS NULL THEN :subject "
            "                     ELSE sso_subject END "
            "WHERE id = :id"
        ),
        {
            "id": survivor.id,
            "provider": link.sso_provider if link else None,
            "subject": link.sso_subject if link else None,
        },
    )
    return [(row.id, survivor.id) for row in losers], losers


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "  AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def _merge_duplicates(conn) -> None:
    groups = _groups(conn)
    if not groups:
        return

    pairs: list[tuple] = []
    losers: list = []
    for group in groups:
        group_pairs, group_losers = _fold_state(conn, group)
        pairs += group_pairs
        losers += group_losers

    # One pass per column for every group, so a table is scanned once.
    composite = conn.execute(
        sa.text(
            "SELECT c.conrelid::regclass::text FROM pg_constraint c "
            "WHERE c.contype = 'f' AND c.confrelid = 'users'::regclass "
            "  AND array_length(c.conkey, 1) > 1"
        )
    ).scalars().all()
    if composite:
        logger.warning(
            "Composite foreign keys onto users are not followed by the merge: %s",
            ", ".join(composite),
        )

    # Tables with a counter per period fold first; the generic move below then
    # finds nothing of the duplicate's left in them.
    _fold_quota_usage(conn, pairs)
    loser_ids = [loser for loser, _ in pairs]
    for table, column in _references(conn):
        left = _move(conn, table, column, pairs)
        if left:
            # The survivor already has an equivalent of these rows, so they go
            # with the duplicate. Left alone, ``SET NULL`` would keep them as
            # orphans and ``NO ACTION`` / ``RESTRICT`` (none today) would refuse
            # the delete below.
            conn.execute(
                sa.text(
                    f"DELETE FROM {table} WHERE {column} = ANY(CAST(:losers AS uuid[]))"
                ),
                {"losers": loser_ids},
            )
            logger.warning(
                "%s.%s: %d row(s) of a duplicate user were not moved, the "
                "survivor already has one",
                table,
                column,
                left,
            )
    for table, column in _UNKEYED_USER_IDS:
        if not _column_exists(conn, table, column):
            continue
        left = _move(conn, table, column, pairs)
        if left:
            # No foreign key: these keep pointing at a user that is deleted.
            logger.warning(
                "%s.%s: %d row(s) still name a deleted duplicate user",
                table,
                column,
                left,
            )
    _fold_daily_stats(conn, pairs)
    _rewrite_json_lists(conn, pairs)
    conn.execute(
        sa.text("DELETE FROM users WHERE id = ANY(CAST(:losers AS uuid[]))"),
        {"losers": [loser.id for loser in losers]},
    )


#: Rounds of merge-then-index. Merge and index build are separate transactions
#: (the build needs ``autocommit_block``); a pod still on the old code can
#: insert a case variant between them, which fails the build with a unique
#: violation. The next round merges it. The build holds a lock that stops
#: inserts, so a case variant cannot slip in once it has started.
_ROUNDS = 3


def _has_work(conn) -> bool:
    """A duplicate group always holds a mixed-case row, so one probe covers both."""
    return bool(
        conn.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM users WHERE email <> lower(email))")
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    for attempt in range(_ROUNDS):
        # Re-set every round: the autocommit block below commits, which resets
        # a transaction-scoped timeout. A statement that waits on a row lock
        # fails fast and the startup runner retries the whole body.
        set_lock_timeout(conn)
        try:
            # A savepoint, so a unique violation from a case variant committed
            # meanwhile (the merge's lowercasing included) rolls back to here
            # and the next round merges it too.
            with conn.begin_nested():
                if _has_work(conn):
                    terminate_lock_holders(conn, "users")
                    _merge_duplicates(conn)
                    conn.execute(
                        sa.text(
                            "UPDATE users SET email = lower(email) "
                            "WHERE email <> lower(email)"
                        )
                    )
            with op.get_context().autocommit_block():
                create_index_in_place(
                    conn, _INDEX, "users", "(lower(email))", unique=True, required=True
                )
            return
        except IntegrityError as exc:
            if not _is_unique_violation(exc) or attempt == _ROUNDS - 1:
                raise
            logger.warning("A case variant appeared during the upgrade; merging again")


def downgrade() -> None:
    conn = op.get_bind()
    if conn.execute(
        sa.text("SELECT to_regclass(:i)"), {"i": _INDEX}
    ).scalar() is None:
        return
    terminate_lock_holders(conn, "users")
    set_lock_timeout(conn)
    conn.execute(sa.text(f"DROP INDEX IF EXISTS {_INDEX}"))
