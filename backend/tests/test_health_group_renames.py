"""The Platform Health group ids renamed from concierge_* to health_* (#101).

The former ids stay accepted as [DEPRECATED] aliases: an agent row, an API
call or an allow list naming one keeps its meaning and is stored under the
current id; migration ``h119healthgrp`` rewrites the stored rows.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.services.builtin_allowed_tools import normalize_builtin_allowed_tools
from app.services.builtin_tool_aliases import (
    RENAMED_BUILTIN_GROUPS,
    canonical_builtin_selection,
    current_group_id,
    describe_builtin_tool_id,
    fold_builtin_selection,
    is_former_builtin_id,
)
from app.services.builtin_tool_groups import BUILTIN_TOOL_GROUPS


def _migration():
    path = (
        Path(__file__).resolve().parents[1] / "alembic" / "versions" / "h119healthgrp_rename_health_tool_group_ids.py"
    )
    spec = importlib.util.spec_from_file_location("h119healthgrp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_renamed_id_names_a_registered_group_and_no_former_id_is_one():
    group_ids = {group.id for group in BUILTIN_TOOL_GROUPS}
    assert set(RENAMED_BUILTIN_GROUPS.values()) <= group_ids
    assert not set(RENAMED_BUILTIN_GROUPS) & group_ids
    assert not any(gid.startswith("concierge_") for gid in group_ids)


def test_migration_map_holds_the_runtime_map():
    # The specialists' former ids were deleted with their groups (#1943, h1943healthgrp01).
    assert RENAMED_BUILTIN_GROUPS.items() <= _migration()._RENAMED.items()
    assert RENAMED_BUILTIN_GROUPS == {"concierge_orchestrator": "health_orchestrator"}


@pytest.mark.parametrize(("former", "current"), sorted(RENAMED_BUILTIN_GROUPS.items()))
def test_a_former_id_is_stored_under_its_current_id(former, current):
    assert current_group_id(former) == current
    assert is_former_builtin_id(former)
    assert not is_former_builtin_id(current)
    assert describe_builtin_tool_id(former) == f"{former} [DEPRECATED] alias of {current}"
    assert canonical_builtin_selection(["web", former], None) == (["web", current], None)


def test_an_allow_list_keyed_by_a_former_id_moves_with_its_functions():
    ids, allowed = canonical_builtin_selection(
        ["concierge_orchestrator"], {"concierge_orchestrator": ["list_health_signals"]}
    )
    assert ids == ["health_orchestrator"]
    assert allowed == {"health_orchestrator": ["list_health_signals"]}
    assert normalize_builtin_allowed_tools({"concierge_orchestrator": ["list_health_signals"]}) == {
        "health_orchestrator": ["list_health_signals"]
    }


def test_former_and_current_id_together_collapse_to_one():
    assert canonical_builtin_selection(["concierge_orchestrator", "health_orchestrator"], None) == (
        ["health_orchestrator"],
        None,
    )


def test_canonical_selection_is_idempotent_and_a_row_is_folded():
    once = canonical_builtin_selection(["concierge_orchestrator", "web"], None)
    assert canonical_builtin_selection(*once) == once

    class Row:
        builtin_tool_ids = ["concierge_orchestrator"]
        builtin_allowed_tools = None

    row = Row()
    assert fold_builtin_selection(row) is True
    assert row.builtin_tool_ids == ["health_orchestrator"]
    assert fold_builtin_selection(row) is False


def test_migration_downgrade_restores_the_former_ids():
    migration = _migration()
    up = canonical_builtin_selection(
        ["web", "concierge_orchestrator"], {"concierge_orchestrator": ["list_health_signals"]}
    )
    down = migration.downgrade_selection(*up)
    assert down == (["web", "concierge_orchestrator"], {"concierge_orchestrator": ["list_health_signals"]})
    assert canonical_builtin_selection(*down) == up
