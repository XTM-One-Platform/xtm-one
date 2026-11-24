"""rename the grc business vertical and the grc.* intents to crq

OpenGRC became OpenCRQ (cyber risk quantification). ``o1crq2opencrq1`` moved
the product slug, but the business vertical and the namespace of the intents
specific to it stayed ``grc``. They become ``crq``; the code names ``crq``
from this release on (``BUILTIN_AGENTS``, ``seed.py``) and an OpenCRQ build
that still registers ``grc`` is aliased at the registration and catalog
boundaries (``app.services.intent_aliases``).

What holds the old names, and how each is carried:

* ``verticals.name`` (UNIQUE): renamed; when ``crq`` already exists the
  ``grc`` row is redundant and dropped. Bindings reference the vertical by
  name, not by key, so nothing cascades.
* ``intent_bindings.vertical``: renamed. A ``crq`` row for the same intent,
  user and agent (``uq_intent_binding``) is a duplicate of the pre-rename row;
  the pre-rename row carries what an administrator or a user set (priority,
  lock, a custom or personal binding), so it wins and the duplicate goes.
* ``intent_bindings.priority_overrides``: the ``grc`` key becomes ``crq``.
* ``intents.name`` (UNIQUE) ``grc.<capability>``: renamed to
  ``crq.<capability>``. When the new name already exists the two intents are
  merged into it: the old intent's bindings move over (old wins on a
  duplicate, as above), its platform declarations move over (a declaration
  already present is kept, ``uq_platform_intent``), its description fills an
  empty one, and the emptied old intent is dropped. No binding is lost.
* ``agents.supported_verticals`` and ``agents.supported_intents`` (JSON
  lists): elements renamed.
* ``platform_registrations.business_vertical``: renamed.

The JSON columns are read as text and rewritten from Python, never cast to
``jsonb``: a stored value the cast refuses (a lone surrogate escape) would
abort the upgrade, and an unreadable value is left as it was.

Every statement is a guarded UPDATE or DELETE on a handful of rows, no DDL,
so re-running the body is a no-op and no lock is taken beyond the rows
touched. A fresh install runs ``create_all`` + ``stamp head`` and never this
body. ``downgrade`` runs the same steps from ``crq`` back to ``grc``.

Revision ID: c1crq2vertical1
Revises: h127healthmodanalyst01
Create Date: 2026-11-21 12:30:00.000000
"""

# ruff: noqa: S608 - the only values interpolated into these statements are the
# table and column names this module spells as literals; every value is bound.

import json
from collections.abc import Callable, Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op

revision: str = "c1crq2vertical1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "h127healthmodanalyst01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled as literals: a migration keeps meaning what it meant the day it ran.
_OLD = "grc"
_NEW = "crq"


def _column_exists(conn: Connection, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def _rename_vertical_rows(conn: Connection, old: str, new: str) -> None:
    if not _column_exists(conn, "verticals", "name"):
        return
    conn.execute(
        sa.text("DELETE FROM verticals WHERE name = :old AND EXISTS (SELECT 1 FROM verticals v WHERE v.name = :new)"),
        {"old": old, "new": new},
    )
    conn.execute(sa.text("UPDATE verticals SET name = :new WHERE name = :old"), {"old": old, "new": new})


def _rename_binding_verticals(conn: Connection, old: str, new: str) -> None:
    if not _column_exists(conn, "intent_bindings", "vertical"):
        return
    # The pre-rename row wins over a duplicate already under the new name.
    conn.execute(
        sa.text(
            "DELETE FROM intent_bindings dup WHERE dup.vertical = :new AND EXISTS ("
            "  SELECT 1 FROM intent_bindings b WHERE b.vertical = :old"
            "  AND b.intent_id = dup.intent_id AND b.agent_id = dup.agent_id"
            "  AND b.user_id IS NOT DISTINCT FROM dup.user_id)"
        ),
        {"old": old, "new": new},
    )
    conn.execute(
        sa.text("UPDATE intent_bindings SET vertical = :new WHERE vertical = :old"),
        {"old": old, "new": new},
    )


_Rewrite = Callable[[Any], Any]


def _rewrite_json(conn: Connection, table: str, column: str, old: str, rewrite: _Rewrite) -> None:
    """Rewrite the JSON values of *column* that mention *old*, from Python."""
    if not _column_exists(conn, table, column):
        return
    rows = conn.execute(
        sa.text(f"SELECT id, CAST({column} AS text) FROM {table} WHERE CAST({column} AS text) LIKE :needle"),
        {"needle": f'%"{old}%'},
    ).all()
    for row_id, raw in rows:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            continue  # unreadable: left as it was
        changed = rewrite(value)
        if changed is None or changed == value:
            continue
        conn.execute(
            sa.text(f"UPDATE {table} SET {column} = CAST(:value AS json) WHERE id = :id"),
            {"value": json.dumps(changed), "id": row_id},
        )


def _rename_override_key(old: str, new: str) -> _Rewrite:
    def rewrite(value: Any) -> Any:
        if not isinstance(value, dict) or old not in value:
            return None
        out = {k: v for k, v in value.items() if k != old}
        out[new] = value[old]  # the pre-rename priority wins
        return out

    return rewrite


def _rename_list_elements(mapping: Callable[[str], str]) -> _Rewrite:
    def rewrite(value: Any) -> Any:
        if not isinstance(value, list):
            return None
        out: list[Any] = []
        for item in value:
            item = mapping(item) if isinstance(item, str) else item
            if item not in out:
                out.append(item)
        return out

    return rewrite


def _intent_name(old: str, new: str) -> Callable[[str], str]:
    prefix = f"{old}."

    def mapping(name: str) -> str:
        return f"{new}.{name[len(prefix) :]}" if name.startswith(prefix) else name

    return mapping


def _rename_intents(conn: Connection, old: str, new: str) -> None:
    if not _column_exists(conn, "intents", "name"):
        return
    to_new = _intent_name(old, new)
    rows = conn.execute(
        sa.text("SELECT id, name, description FROM intents WHERE name LIKE :prefix"),
        {"prefix": f"{old}.%"},
    ).all()
    for old_id, old_name, old_description in rows:
        new_name = to_new(old_name)
        target = conn.execute(
            sa.text("SELECT id, description FROM intents WHERE name = :name"), {"name": new_name}
        ).first()
        if target is None:
            conn.execute(sa.text("UPDATE intents SET name = :name WHERE id = :id"), {"name": new_name, "id": old_id})
            continue
        new_id, new_description = target
        # Merge into the existing intent; the old intent's bindings win on a duplicate.
        conn.execute(
            sa.text(
                "DELETE FROM intent_bindings dup WHERE dup.intent_id = :new_id AND EXISTS ("
                "  SELECT 1 FROM intent_bindings b WHERE b.intent_id = :old_id"
                "  AND b.agent_id = dup.agent_id"
                "  AND b.vertical IS NOT DISTINCT FROM dup.vertical"
                "  AND b.user_id IS NOT DISTINCT FROM dup.user_id)"
            ),
            {"old_id": old_id, "new_id": new_id},
        )
        conn.execute(
            sa.text("UPDATE intent_bindings SET intent_id = :new_id WHERE intent_id = :old_id"),
            {"old_id": old_id, "new_id": new_id},
        )
        conn.execute(
            sa.text(
                "DELETE FROM platform_intent_declarations d WHERE d.intent_id = :old_id AND EXISTS ("
                "  SELECT 1 FROM platform_intent_declarations n WHERE n.intent_id = :new_id"
                "  AND n.platform_identifier = d.platform_identifier)"
            ),
            {"old_id": old_id, "new_id": new_id},
        )
        conn.execute(
            sa.text("UPDATE platform_intent_declarations SET intent_id = :new_id WHERE intent_id = :old_id"),
            {"old_id": old_id, "new_id": new_id},
        )
        if old_description and not new_description:
            conn.execute(
                sa.text("UPDATE intents SET description = :d WHERE id = :id"),
                {"d": old_description, "id": new_id},
            )
        conn.execute(sa.text("DELETE FROM intents WHERE id = :id"), {"id": old_id})


def _rename(conn: Connection, old: str, new: str) -> None:
    _rename_vertical_rows(conn, old, new)
    _rename_binding_verticals(conn, old, new)
    _rewrite_json(conn, "intent_bindings", "priority_overrides", old, _rename_override_key(old, new))
    _rename_intents(conn, old, new)
    _rewrite_json(conn, "agents", "supported_verticals", old, _rename_list_elements(lambda v: new if v == old else v))
    _rewrite_json(conn, "agents", "supported_intents", old, _rename_list_elements(_intent_name(old, new)))
    if _column_exists(conn, "platform_registrations", "business_vertical"):
        conn.execute(
            sa.text("UPDATE platform_registrations SET business_vertical = :new WHERE business_vertical = :old"),
            {"old": old, "new": new},
        )


def upgrade() -> None:
    _rename(op.get_bind(), _OLD, _NEW)


def downgrade() -> None:
    _rename(op.get_bind(), _NEW, _OLD)
