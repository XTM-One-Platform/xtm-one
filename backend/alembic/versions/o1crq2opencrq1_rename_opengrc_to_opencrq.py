"""rename the OpenGRC product slug to OpenCRQ in stored rows

OpenGRC was renamed to OpenCRQ. The new name is the canonical one everywhere
in code, but the old slug is persisted in rows written before the rename and
nothing rewrites them on its own. Three shapes of storage hold it:

* **Scalar identifier columns** — ``integrations.provider``,
  ``platform_registrations.platform_identifier``,
  ``verticals.platform_identifier`` and
  ``platform_intent_declarations.platform_identifier``. A row still reading
  ``opengrc`` no longer matches any catalog entry or canonical lookup, so the
  integration loses its tools, logo and setup card, and a declared intent or
  auto-discovered vertical stops resolving to its platform.

* **Provider-keyed JSON bindings** — ``integration_bindings`` on ``agents``,
  ``assignments`` and ``general_assistant_config``,
  ``users.ga_integration_bindings``, and ``integration_allowed_tools`` on
  ``agents`` and ``assignments``. A binding left under ``opengrc`` silently
  stops granting its OpenCRQ tools, and on ``agents`` the loss then becomes
  permanent: ``prune_orphaned_integration_scopes`` drops entries whose
  provider is no longer active, so the next save deletes the stale binding
  and its scopes outright.

* **Tool names**, which carry the provider as an infix
  (``update_opengrc_control_settings``). These live in
  ``agents.tool_deny_list``, in the values of ``integration_allowed_tools``
  on both tables, and in ``agents.mcp_allowed_tools``. The deny list is the
  urgent one: every other stale name fails *closed* — a tool stops resolving
  — but a stale deny entry fails **open**, because ``apply_tool_deny_list``
  matches literal names, so a tool an administrator blocked becomes callable
  again with no error and no log line.

* **Provider-prefixed JSON keys** — ``integration_element_bindings`` on
  ``agents`` and ``assignments`` is keyed ``"{provider}:{element_type}"``,
  so the provider is a prefix of the key and whole-key equality never
  matches it.

* **JSON arrays of provider keys** — ``agents.integration_template_bindings``
  and ``platform_settings.disabled_integration_providers``. A stale element
  means a pre-packaged agent stops provisioning per-user credentials, or an
  administrator's "disabled" choice silently stops applying.

Plus the built-in GRC assistant's ``agents.slug`` / ``agent_templates.slug``.
The seeder deletes built-ins whose slug left ``BUILTIN_AGENTS`` and inserts the
new one, which would work — but ``agents`` is referenced with ``ON DELETE SET
NULL`` from conversations and ``ON DELETE CASCADE`` from assignments, so that
delete would detach existing conversation history and destroy automations.
Renaming the slug here means the seeder finds the row and updates it in place.

Every statement is guarded so re-running the revision is a no-op, and the
conflict cases are resolved rather than skipped:

* ``platform_registrations.platform_identifier`` is UNIQUE — a pre-existing
  post-rename row means the platform already re-registered, so the stale row
  is dropped instead of colliding.
* ``platform_intent_declarations`` is UNIQUE on ``(platform_identifier,
  intent_id)`` — a declaration already present under the new identifier wins
  and the old duplicate is dropped.
* The two slug columns are UNIQUE. Migrations run before seeding
  (``run_migrations()`` precedes ``_bootstrap_platform_data()`` in
  ``main.py``), so a row already holding the new slug is normally a freshly
  seeded built-in — but that is an assumption about ordering this migration
  cannot verify, and an out-of-band upgrade, a retried deploy or a restored
  dump each break it. So it checks the property the assumption stands for:
  a fresh row carries no history. Not "no references" — a seeded built-in is
  referenced immediately, by the intent bindings the seeder writes for it —
  but nothing beyond what the seeder owns and recreates on the next boot
  (see ``_SEEDER_OWNED_REFERENCES``). Only such a row is deleted, letting
  the historical row take its place. A row someone created, or a built-in
  carrying anything a user made, is left alone and the upgrade stops with
  the row id and the remedy — skipping would leave the pre-rename row for
  the seeder to delete, which is the very loss this rename exists to avoid.

The rename is one-way by design: ``downgrade`` restores the old slug so the
previous release reads its own rows.

Revision ID: o1crq2opencrq1
Revises: m1mem516001
Create Date: 2026-09-16 10:00:00.000000
"""
# ruff: noqa: S608 - the only values interpolated into these statements are
# the table and column names in the mapping tables below, this module's own
# constants; every slug is passed as a bound parameter.

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.utils import sanitize_json_strings

revision: str = "o1crq2opencrq1"
down_revision: str | Sequence[str] | None = "m1mem516001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "opengrc"
_NEW = "opencrq"
_OLD_SLUG = "opengrc-assistant"
_NEW_SLUG = "opencrq-assistant"

#: Plain identifier columns with no uniqueness to reconcile.
_SCALAR: tuple[tuple[str, str], ...] = (
    ("integrations", "provider"),
    ("verticals", "platform_identifier"),
)

#: ``{provider_key: [uuid, ...]}`` objects — the key is renamed and, if both
#: spellings somehow exist, their UUID lists are concatenated rather than one
#: silently winning.
_JSON_OBJECT: tuple[tuple[str, str], ...] = (
    ("agents", "integration_bindings"),
    ("agents", "integration_allowed_tools"),
    ("assignments", "integration_bindings"),
    ("assignments", "integration_allowed_tools"),
    ("general_assistant_config", "integration_bindings"),
    ("users", "ga_integration_bindings"),
)

#: Tool names carry the provider as an infix (``update_opengrc_control_
#: settings``), so a stored name survives the rename only if it is rewritten
#: too. ``_opengrc_`` rather than ``opengrc`` keeps the match anchored to that
#: segment. Every one of the 113 provider tool names has this infix.
_TOOL_NAME_INFIX_OLD = f"_{_OLD}_"
_TOOL_NAME_INFIX_NEW = f"_{_NEW}_"

#: JSON arrays of tool names.
#:
#: ``agents.tool_deny_list`` is the reason this whole class of rewrite is not
#: optional. Every other stale name fails *closed* — a tool simply stops
#: resolving. A stale deny entry fails **open**: ``apply_tool_deny_list``
#: matches literal names, so an entry an administrator added to block a
#: mutating tool stops matching and the tool becomes callable again, with no
#: error and no log line.
_TOOL_NAME_ARRAY: tuple[tuple[str, str], ...] = (("agents", "tool_deny_list"),)

#: JSON objects whose *values* are arrays of tool names. The keys of the
#: first two are provider keys (renamed via ``_JSON_OBJECT`` above);
#: ``mcp_allowed_tools`` is keyed by MCP server UUID, so only its values need
#: rewriting.
_TOOL_NAME_OBJECT_VALUES: tuple[tuple[str, str], ...] = (
    ("agents", "integration_allowed_tools"),
    ("agents", "mcp_allowed_tools"),
    ("assignments", "integration_allowed_tools"),
)

#: JSON objects keyed ``"{provider}:{element_type}"`` — the provider is a
#: prefix of the key, not the whole key, so equality would never match.
_PREFIXED_KEY_OBJECT: tuple[tuple[str, str], ...] = (
    ("agents", "integration_element_bindings"),
    ("assignments", "integration_element_bindings"),
)

#: JSON arrays whose elements are provider keys.
_JSON_ARRAY: tuple[tuple[str, str], ...] = (
    ("agents", "integration_template_bindings"),
    ("platform_settings", "disabled_integration_providers"),
)

#: UNIQUE slug columns holding the built-in GRC assistant.
_SLUG_TABLES: tuple[str, ...] = ("agents", "agent_templates")


def _unreadable(column: str) -> str:
    """Whether PostgreSQL refuses to de-escape *column*'s stored text.

    A ``json`` column keeps a lone UTF-16 surrogate or ``\\u0000`` escape
    verbatim, and every ``::jsonb`` below refused such a row, aborting the
    upgrade; so does a number past ``numeric``'s range. The test of
    ``app.core.json_sql.json_unparseable``, spelled here because a migration
    must keep meaning what it meant; its patterns are the
    ``_UNREADABLE_PARAMS``. It picks the candidates ``_repair_unreadable``
    then decides exactly, each with a real cast.
    """
    return (
        f"((CAST({column} AS text) ~ :refused_escape "
        f"AND regexp_replace(replace(CAST({column} AS text), :escaped_backslash, '_'), "
        f":surrogate_pair, '_', 'g') ~ :refused_escape) "
        f"OR CAST({column} AS text) ~ :out_of_range_number)"
    )


_UNREADABLE_PARAMS = {
    "refused_escape": r"\\u(0000|[dD][89a-fA-F])",
    "surrogate_pair": r"\\u[dD][89abAB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}",
    "escaped_backslash": "\\\\",
    "out_of_range_number": (
        r"(^|[:,\[])\s*-?([0-9]+(\.[0-9]+)?[eE][+-]?[0-9]{5}"
        r"|([0-9]+\.)?[0-9]{250}[0-9]{250}[0-9]{250}[0-9]{250})"
    ),
}


def _castable(conn, stored: str) -> bool:
    """Whether PostgreSQL casts *stored* to ``jsonb``: the exact answer, in a savepoint."""
    try:
        with conn.begin_nested():
            # IS NOT NULL: the value itself is never decoded by the driver.
            conn.execute(sa.text("SELECT CAST(CAST(:v AS json) AS jsonb) IS NOT NULL"), {"v": stored})
    except sa.exc.DBAPIError:
        return False
    return True


def _text(statement: str):
    """``sa.text`` with ``:old`` / ``:new`` typed as text.

    The JSON rewrites below feed these parameters to ``jsonb_build_object``
    and ``to_jsonb``, which are overloaded: without a declared type Postgres
    cannot infer one and rejects the statement.
    """
    return sa.text(statement).bindparams(
        sa.bindparam("old", type_=sa.String()),
        sa.bindparam("new", type_=sa.String()),
    )


def _table_exists(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": table},
        ).scalar()
    )


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def _ready(conn, table: str, column: str) -> bool:
    # Fresh install: create_all builds the schema and stamps head, so there
    # are no pre-rename rows to carry over.
    return _table_exists(conn, table) and _column_exists(conn, table, column)


def _rename_scalar(conn, table: str, column: str, old: str, new: str) -> None:
    if not _ready(conn, table, column):
        return
    conn.execute(
        sa.text(f"UPDATE {table} SET {column} = :new WHERE {column} = :old"),
        {"old": old, "new": new},
    )


def _rename_json_key(
    conn, table: str, column: str, old: str, new: str, skip: Sequence[str] = ()
) -> None:
    """Move ``old``'s value onto ``new``, concatenating if both are present."""
    if not _ready(conn, table, column):
        return
    conn.execute(
        _text(
            f"UPDATE {table} SET {column} = ("
            f"  ({column}::jsonb - :old) || jsonb_build_object("
            f"    :new,"
            f"    COALESCE({column}::jsonb -> :new, '[]'::jsonb)"
            f"    || COALESCE({column}::jsonb -> :old, '[]'::jsonb)"
            f"  )"
            f")::json "
            f"WHERE {column} IS NOT NULL "
            f"AND CASE WHEN CAST(id AS text) = ANY(CAST(:skip AS text[])) THEN false ELSE "
            f"jsonb_typeof({column}::jsonb) = 'object' "
            # ``-> key IS NOT NULL`` rather than the ``?`` existence
            # operator, which collides with bind-parameter parsing.
            f"AND {column}::jsonb -> :old IS NOT NULL END"
        ),
        {"old": old, "new": new, "skip": list(skip)},
    )


def _rename_json_element(
    conn, table: str, column: str, old: str, new: str, skip: Sequence[str] = ()
) -> None:
    """Rewrite ``old`` to ``new`` wherever it appears as an array element."""
    if not _ready(conn, table, column):
        return
    conn.execute(
        _text(
            f"UPDATE {table} SET {column} = ("
            f"  SELECT jsonb_agg("
            f"    CASE WHEN elem = to_jsonb(:old)"
            f"         THEN to_jsonb(:new) ELSE elem END"
            f"  )"
            f"  FROM jsonb_array_elements({column}::jsonb) AS elem"
            f")::json "
            f"WHERE {column} IS NOT NULL "
            f"AND CASE WHEN CAST(id AS text) = ANY(CAST(:skip AS text[])) THEN false ELSE "
            f"jsonb_typeof({column}::jsonb) = 'array' "
            f"AND {column}::jsonb @> jsonb_build_array(:old) END"
        ),
        {"old": old, "new": new, "skip": list(skip)},
    )


def _rewrite_string_array(
    conn, table: str, column: str, old: str, new: str, skip: Sequence[str] = ()
) -> None:
    """Replace ``old`` with ``new`` inside every string of a JSON array."""
    if not _ready(conn, table, column):
        return
    conn.execute(
        _text(
            f"UPDATE {table} SET {column} = ("
            f"  SELECT jsonb_agg("
            f"    CASE WHEN jsonb_typeof(elem) = 'string'"
            f"         THEN to_jsonb(replace(elem #>> '{{}}', :old, :new))"
            f"         ELSE elem END"
            f"  )"
            f"  FROM jsonb_array_elements({column}::jsonb) AS elem"
            f")::json "
            f"WHERE {column} IS NOT NULL "
            f"AND CASE WHEN CAST(id AS text) = ANY(CAST(:skip AS text[])) THEN false ELSE "
            f"jsonb_typeof({column}::jsonb) = 'array' END "
            # Also the idempotency guard, and it keeps an empty array
            # untouched — ``jsonb_agg`` over no rows returns NULL, which
            # would otherwise blank the column.
            f"AND {column}::text LIKE '%' || :old || '%'"
        ),
        {"old": old, "new": new, "skip": list(skip)},
    )


def _rewrite_object_values(
    conn, table: str, column: str, old: str, new: str, skip: Sequence[str] = ()
) -> None:
    """Replace ``old`` with ``new`` inside the string arrays a JSON object maps to."""
    if not _ready(conn, table, column):
        return
    conn.execute(
        _text(
            f"UPDATE {table} SET {column} = ("
            f"  SELECT jsonb_object_agg("
            f"    entry.k,"
            f"    CASE WHEN jsonb_typeof(entry.v) = 'array' THEN COALESCE(("
            f"      SELECT jsonb_agg("
            f"        CASE WHEN jsonb_typeof(elem) = 'string'"
            f"             THEN to_jsonb(replace(elem #>> '{{}}', :old, :new))"
            f"             ELSE elem END"
            f"      )"
            f"      FROM jsonb_array_elements(entry.v) AS elem"
            # An empty allow list is meaningful — it denies everything — so
            # it must stay an empty array rather than collapse to NULL.
            f"    ), '[]'::jsonb) ELSE entry.v END"
            f"  )"
            f"  FROM jsonb_each({column}::jsonb) AS entry(k, v)"
            f")::json "
            f"WHERE {column} IS NOT NULL "
            f"AND CASE WHEN CAST(id AS text) = ANY(CAST(:skip AS text[])) THEN false ELSE "
            f"jsonb_typeof({column}::jsonb) = 'object' END "
            f"AND {column}::text LIKE '%' || :old || '%'"
        ),
        {"old": old, "new": new, "skip": list(skip)},
    )


def _rewrite_key_prefix(
    conn, table: str, column: str, old: str, new: str, skip: Sequence[str] = ()
) -> None:
    """Rewrite the ``old`` prefix of every key of a JSON object to ``new``."""
    if not _ready(conn, table, column):
        return
    conn.execute(
        _text(
            f"UPDATE {table} SET {column} = ("
            f"  SELECT jsonb_object_agg("
            f"    CASE WHEN entry.k LIKE :old || '%'"
            f"         THEN :new || substring(entry.k FROM length(:old) + 1)"
            f"         ELSE entry.k END,"
            f"    entry.v"
            f"  )"
            f"  FROM jsonb_each({column}::jsonb) AS entry(k, v)"
            f")::json "
            f"WHERE {column} IS NOT NULL "
            f"AND CASE WHEN CAST(id AS text) = ANY(CAST(:skip AS text[])) THEN false ELSE "
            f"jsonb_typeof({column}::jsonb) = 'object' END "
            f"AND {column}::text LIKE '%' || :old || '%'"
        ),
        {"old": old, "new": new, "skip": list(skip)},
    )


#: Columns whose entry, left under the old name, grants what it was written to
#: withhold: a stale deny entry un-denies the renamed tool, a stale disabled
#: provider re-enables it. A row of one the rewrites cannot read stops the
#: upgrade; every other column left under the old name grants less.
_DENYING: frozenset[tuple[str, str]] = frozenset(
    {("agents", "tool_deny_list"), ("platform_settings", "disabled_integration_providers")}
)


def _repaired_json(stored: str) -> str | None:
    """*stored* as the write path stores it now (``sanitize_json_strings``), ``None`` when unreadable."""
    try:
        return json.dumps(sanitize_json_strings(json.loads(stored)), allow_nan=False)
    except (ValueError, RecursionError):
        return None


def _repair_unreadable(conn, table: str, column: str, old: str) -> list[str]:
    """Rewrite the rows of *table*.*column* naming *old* that PostgreSQL cannot cast.

    A lone surrogate becomes U+FFFD and ``\\u0000`` goes, exactly as every JSON
    write now stores them, so the rewrites below rename them like any other
    row. Each candidate is decided by a real cast: one that casts is left
    alone, and so is one that does not name *old* - the rewrites cast every
    row, so it is left out of them, and it has nothing to rename. A row naming
    *old* that cannot be cast even once repaired is left out too, and named; in
    a ``_DENYING`` column that would fail open, so the upgrade stops instead.
    Returns the ids the rewrites must leave out.
    """
    if not _ready(conn, table, column):
        return []
    rows = conn.execute(
        sa.text(f"SELECT id, CAST({column} AS text) FROM {table} WHERE {_unreadable(column)}"),
        _UNREADABLE_PARAMS,
    ).all()
    repaired, unrepaired, left_out = 0, [], []
    for row_id, stored in rows:
        if _castable(conn, stored):
            continue
        if old not in stored:
            left_out.append(str(row_id))
            continue
        clean = _repaired_json(stored)
        if clean is None or not _castable(conn, clean):
            unrepaired.append(str(row_id))
            continue
        conn.execute(
            sa.text(f"UPDATE {table} SET {column} = CAST(:value AS json) WHERE id = :id"),
            {"value": clean, "id": row_id},
        )
        repaired += 1
    if repaired:
        print(  # noqa: T201 - migrations report through stdout
            f"opencrq rename: repaired {repaired} row(s) of {table}.{column} holding a lone "
            "surrogate or NUL escape (stored as U+FFFD, as every JSON write now stores them)"
        )
    if not unrepaired:
        return left_out
    named = ", ".join(sorted(unrepaired))
    if (table, column) in _DENYING:
        raise RuntimeError(
            f"Cannot rename '{old}' in {table}.{column}: row(s) {named} cannot be read, and "
            f"left under the old name they would stop withholding what they name. Fix or clear "
            f"the column of these rows, then re-run the upgrade."
        )
    print(  # noqa: T201 - migrations report through stdout
        f"opencrq rename: left {len(unrepaired)} row(s) of {table}.{column} unchanged ({named}): "
        "they cannot be read. Left under the old name they grant less, never more."
    )
    return left_out + unrepaired


def _rename_registration(conn, old: str, new: str) -> None:
    """Rename the platform registration, dropping a stale duplicate.

    ``platform_identifier`` is UNIQUE, so a row already carrying *new* means
    the platform re-registered under its new name; the pre-rename row is then
    history and would only collide.
    """
    table, column = "platform_registrations", "platform_identifier"
    if not _ready(conn, table, column):
        return
    conn.execute(
        sa.text(
            f"DELETE FROM {table} WHERE {column} = :old "
            f"AND EXISTS (SELECT 1 FROM {table} other WHERE other.{column} = :new)"
        ),
        {"old": old, "new": new},
    )
    _rename_scalar(conn, table, column, old, new)


def _rename_intent_declarations(conn, old: str, new: str) -> None:
    """Rename intent declarations, dropping duplicates per intent.

    UNIQUE on ``(platform_identifier, intent_id)``: where the same intent is
    already declared under the new identifier, the old row is redundant.
    """
    table, column = "platform_intent_declarations", "platform_identifier"
    if not _ready(conn, table, column):
        return
    conn.execute(
        sa.text(
            f"DELETE FROM {table} old_row WHERE old_row.{column} = :old "
            f"AND EXISTS ("
            f"  SELECT 1 FROM {table} new_row "
            f"  WHERE new_row.{column} = :new "
            f"  AND new_row.intent_id = old_row.intent_id"
            f")"
        ),
        {"old": old, "new": new},
    )
    _rename_scalar(conn, table, column, old, new)


#: References a freshly seeded built-in legitimately owns, mapped to the
#: predicate that identifies the seeder's own rows.
#:
#: "A fresh row is referenced by nothing" is false: the seeder writes one
#: ``intent_bindings`` row per supported intent a built-in declares, and
#: ``opencrq-assistant`` declares several (``_EDITING_INTENT_AGENTS`` in
#: ``seed.py`` lists it explicitly). Those rows are regenerated on the next
#: boot — seconds later, in the same startup — so cascading them away while
#: disposing of a seeded duplicate loses nothing. Treating them as history
#: would make this guard refuse in the only situation it can ever fire.
#:
#: The predicate narrows that to rows the seeder marked as its own: a binding
#: an operator added by hand is not ``is_builtin`` and still blocks.
#:
#: Nothing else belongs here. No built-in declares seeded assignments, and
#: the seeder writes no config or prompt versions, so every other reference
#: to a built-in agent is user history.
_SEEDER_OWNED_REFERENCES: dict[tuple[str, str], str] = {
    ("intent_bindings", "agent_id"): "is_builtin",
}


def _row_has_dependents(conn, table: str, row_id) -> bool:
    """Whether any row that is not the seeder's own references this row.

    The referencing tables are read from the catalog rather than listed
    here. ``agents`` is the target of some thirty foreign keys — chats,
    worksets, work items, memory entries, prompt versions, quotas, intents,
    evaluations, delegations, sub-agent tasks — mixing ``ON DELETE CASCADE``
    (the row's history is destroyed) with ``ON DELETE SET NULL`` (it is
    silently detached, including ``conversations.original_agent_id``, a
    column that exists only to record attribution). Successive rounds of
    review on this rename each found another reference a hand-written list
    had missed, so this asks Postgres: the catalog cannot fall behind the
    schema, and a table added later is covered for free.

    What the catalog cannot say is which references are the seeder's own.
    That is what ``_SEEDER_OWNED_REFERENCES`` carries, and it is deliberately
    the smaller, better-justified list of the two.
    """
    references = conn.execute(
        sa.text(
            # Both names are interpolated into the probe below, so both come
            # back as identifiers: ``regclass`` output quotes itself when
            # needed and ``quote_ident`` does the same for the column. A plain
            # snake_case name is returned unchanged, which is also how the
            # ``_SEEDER_OWNED_REFERENCES`` keys are spelled.
            "SELECT c.conrelid::regclass::text, quote_ident(a.attname) "
            "FROM pg_constraint c "
            "JOIN unnest(c.conkey) AS k(attnum) ON true "
            "JOIN pg_attribute a "
            "  ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
            "WHERE c.contype = 'f' "
            # ``to_regclass(:t)`` rather than ``:t::regclass``: a cast applied
            # directly to a bind parameter does not survive SQLAlchemy's
            # parameter rewriting (the same reason the JSON statements above
            # declare their types instead of casting inline).
            "AND c.confrelid = to_regclass(:t) "
            # Composite keys cannot point at a bare id column, so a
            # single-column constraint is the whole population here.
            "AND array_length(c.conkey, 1) = 1 "
            # A single-column foreign key may target any UNIQUE column, not
            # just the primary key. None does today, but one pointed at
            # ``agents.web_token`` or ``agent_templates.slug`` would have the
            # comparison below testing a uuid against text and error out
            # rather than answer.
            "AND (SELECT target.attname FROM pg_attribute target "
            "     WHERE target.attrelid = c.confrelid "
            "     AND target.attnum = c.confkey[1]) = 'id'"
        ),
        {"t": table},
    ).fetchall()
    for ref_table, ref_column in references:
        seeder_owned = _SEEDER_OWNED_REFERENCES.get((ref_table, ref_column))
        # ``NOT NULL`` is NULL, which would drop an unmarked row from the probe
        # and let the duplicate be deleted: an unknown marker is history.
        exclude = f" AND NOT COALESCE({seeder_owned}, false)" if seeder_owned else ""
        if conn.execute(
            sa.text(
                f"SELECT 1 FROM {ref_table} "
                f"WHERE {ref_column} = :id{exclude} LIMIT 1"
            ),
            {"id": row_id},
        ).scalar():
            return True
    return False


def _rename_slug(conn, table: str, old: str, new: str) -> None:
    """Rename the built-in assistant's slug, resolving a duplicate safely.

    A row already holding *new* is usually a freshly seeded built-in, because
    migrations run before seeding — but that is an argument about ordering,
    and this migration cannot see the ordering it ran under: an out-of-band
    ``alembic upgrade head``, a retried deploy or a restored dump all break
    it. So the property the invariant stands for is checked directly. A
    genuinely fresh row is referenced by nothing, and only such a row is
    deleted, letting the historical row keep its identity.

    Anything else — a row someone created, or a built-in that has acquired
    history of its own — is left untouched. Skipping is not an option
    either: it would leave the pre-rename row for the seeder, which deletes
    built-ins whose slug left the set and takes their history with it. So
    the upgrade stops instead, naming the row and the remedy.
    """
    if not _ready(conn, table, "slug"):
        return

    stale = conn.execute(
        sa.text(f"SELECT id FROM {table} WHERE slug = :old"),
        {"old": old},
    ).scalar()
    if stale is None:
        # Nothing to rename — already migrated, or never present.
        return

    duplicate = conn.execute(
        sa.text(f"SELECT id, is_builtin FROM {table} WHERE slug = :new"),
        {"new": new},
    ).first()

    if duplicate is not None:
        duplicate_id, is_builtin = duplicate
        disposable = is_builtin and not _row_has_dependents(
            conn, table, duplicate_id
        )
        if not disposable:
            reason = (
                "it is not a built-in (an agent someone created)"
                if not is_builtin
                else "it already carries history of its own"
            )
            raise RuntimeError(
                f"Cannot rename {table}.slug '{old}' -> '{new}': row "
                f"{duplicate_id} already uses '{new}' and this migration "
                f"will not remove it — {reason}. Decide which row to keep, "
                f"rename or delete the other, then re-run the upgrade. "
                f"Leaving '{old}' in place is not safe: the built-in seeder "
                f"would delete it, destroying the rows that cascade from it "
                f"and detaching the conversations that point at it."
            )
        conn.execute(
            sa.text(f"DELETE FROM {table} WHERE id = :id"),
            {"id": duplicate_id},
        )

    conn.execute(
        sa.text(f"UPDATE {table} SET slug = :new WHERE slug = :old"),
        {"old": old, "new": new},
    )


def _migrate(conn, old: str, new: str, old_slug: str, new_slug: str) -> None:
    # Rows no cast can read, even repaired: left out of the rewrites below.
    unreadable = {
        (table, column): _repair_unreadable(conn, table, column, old)
        for table, column in dict.fromkeys(
            (*_JSON_OBJECT, *_JSON_ARRAY, *_TOOL_NAME_ARRAY, *_TOOL_NAME_OBJECT_VALUES, *_PREFIXED_KEY_OBJECT)
        )
    }
    for table, column in _SCALAR:
        _rename_scalar(conn, table, column, old, new)
    for table, column in _JSON_OBJECT:
        _rename_json_key(conn, table, column, old, new, unreadable[table, column])
    for table, column in _JSON_ARRAY:
        _rename_json_element(conn, table, column, old, new, unreadable[table, column])

    # Tool names carry the provider as an infix, so these rewrite inside the
    # strings rather than matching whole values.
    infix_old = f"_{old}_"
    infix_new = f"_{new}_"
    for table, column in _TOOL_NAME_ARRAY:
        _rewrite_string_array(conn, table, column, infix_old, infix_new, unreadable[table, column])
    for table, column in _TOOL_NAME_OBJECT_VALUES:
        _rewrite_object_values(conn, table, column, infix_old, infix_new, unreadable[table, column])
    for table, column in _PREFIXED_KEY_OBJECT:
        _rewrite_key_prefix(conn, table, column, f"{old}:", f"{new}:", unreadable[table, column])

    _rename_registration(conn, old, new)
    _rename_intent_declarations(conn, old, new)
    for table in _SLUG_TABLES:
        _rename_slug(conn, table, old_slug, new_slug)


def upgrade() -> None:
    _migrate(op.get_bind(), _OLD, _NEW, _OLD_SLUG, _NEW_SLUG)


def downgrade() -> None:
    _migrate(op.get_bind(), _NEW, _OLD, _NEW_SLUG, _OLD_SLUG)
