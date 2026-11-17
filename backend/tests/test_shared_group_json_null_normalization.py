"""Guard the fix for the production 500 ``cannot get array length of a scalar``.

The intent catalog crashed because ``shared_group_ids`` held the JSON ``null``
*scalar*: the bare ``postgresql.JSON`` type encodes a Python ``None`` as
``'null'`` (``none_as_null=False``), and ``jsonb_array_length`` raises on any
JSON scalar.

Two complementary guards keep it from happening again:

1. **Write-time** — every shareable entity's ``shared_group_ids`` /
   ``shared_group_permissions`` column sets ``none_as_null=True`` so a ``None``
   assignment persists SQL NULL, never the JSON ``null`` literal.
2. **Backfill** — a data migration normalizes rows already written with a JSON
   ``null`` / scalar back to SQL NULL.

This source-level test locks the write-time guard down without needing a live
database.
"""

from __future__ import annotations

import pytest

# Importing the model registry populates ``Base.metadata`` with every table.
import app.models  # noqa: F401
from app.core.database import Base

# (table, column) pairs that must round-trip ``None`` -> SQL NULL.
_ARRAY_COLUMNS = (
    ("agents", "shared_group_ids"),
    ("integrations", "shared_group_ids"),
    ("knowledge_bases", "shared_group_ids"),
    ("mcp_servers", "shared_group_ids"),
    ("custom_tools", "shared_group_ids"),
    ("channel_bots", "shared_group_ids"),
    ("prompts", "shared_group_ids"),
    ("agent_skills", "shared_group_ids"),
    ("datasets", "shared_group_ids"),
    ("variables", "shared_group_ids"),
    ("evaluations", "shared_group_ids"),
)

_OBJECT_COLUMNS = (
    ("agents", "shared_group_permissions"),
    ("integrations", "shared_group_permissions"),
    ("knowledge_bases", "shared_group_permissions"),
    ("mcp_servers", "shared_group_permissions"),
    ("custom_tools", "shared_group_permissions"),
    ("channel_bots", "shared_group_permissions"),
    ("prompts", "shared_group_permissions"),
    ("agent_skills", "shared_group_permissions"),
    ("datasets", "shared_group_permissions"),
    ("variables", "shared_group_permissions"),
)

_ALL_COLUMNS = _ARRAY_COLUMNS + _OBJECT_COLUMNS


# ── Write-time guard: none_as_null=True on every shareable column ────────────


@pytest.mark.parametrize(("table", "column"), _ALL_COLUMNS)
def test_shared_group_columns_use_none_as_null(table: str, column: str) -> None:
    """A Python ``None`` must persist as SQL NULL, not the JSON ``null`` scalar.

    Without ``none_as_null=True`` the ``= None`` write paths (create, update,
    company-managed toggle) reintroduce the exact ``'null'`` scalar that crashed
    ``jsonb_array_length`` in production.
    """
    assert table in Base.metadata.tables, (
        f"table {table!r} is missing from the model registry — did a model get "
        "renamed or dropped from app/models/__init__.py?"
    )
    col = Base.metadata.tables[table].columns[column]
    assert getattr(col.type, "none_as_null", False) is True, (
        f"{table}.{column} must be declared as JSON(none_as_null=True) so a "
        "None write becomes SQL NULL instead of the JSON 'null' scalar that "
        "crashes jsonb_array_length."
    )
