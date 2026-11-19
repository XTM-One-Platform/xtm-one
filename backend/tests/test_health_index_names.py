"""The health_* tables carry no ``concierge`` index or constraint name (#101, spec 2.3).

The models declare ``ix_health_*``, ``uq_health_*`` and ``ck_health_*``; migration
``h120healthidx`` renames the former names of a migrated database to match.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import app.models  # noqa: F401 - registers every model on Base.metadata
from app.core.database import Base


def _migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "h120healthidx_rename_health_index_constraint_names.py"
    )
    spec = importlib.util.spec_from_file_location("h120healthidx", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_migrated_table_is_a_model_table():
    assert set(_migration().TABLES) <= set(Base.metadata.tables)


def test_no_model_of_a_health_table_declares_a_concierge_name():
    names = []
    for table_name in _migration().TABLES:
        table = Base.metadata.tables[table_name]
        names += [index.name for index in table.indexes]
        names += [constraint.name for constraint in table.constraints if isinstance(constraint.name, str)]
    assert names, "the health tables declare named indexes and constraints"
    assert [name for name in names if "concierge" in name] == []


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ix_concierge_cycles_month", "ix_health_cycles_month"),
        ("uq_concierge_findings_fingerprint", "uq_health_findings_fingerprint"),
        ("ck_concierge_entry_sources_case_kind", "ck_health_entry_sources_case_kind"),
        ("concierge_cycles_pkey", "health_cycles_pkey"),
        ("concierge_cycle_runs_cycle_id_fkey", "health_cycle_runs_cycle_id_fkey"),
    ],
)
def test_upgrade_replaces_the_first_concierge_token(name, expected):
    renamed = _migration().renamed
    assert renamed(name, "concierge", "health") == expected
    assert renamed(expected, "health", "concierge") == name


@pytest.mark.parametrize("name", ["health_findings_pkey_x", "ix_agents_conciergerie", "ixconcierge_x"])
def test_a_name_without_the_token_is_left_alone(name):
    assert _migration().renamed(name, "concierge", "health") is None
