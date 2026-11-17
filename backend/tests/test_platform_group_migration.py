"""Migration p8270platgrp: rows move to the platform group and back, granting the same."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.services.builtin_tool_aliases import (
    BUILTIN_GROUP_ALIASES,
    alias_scoped_functions,
    canonical_builtin_selection,
    runtime_builtin_tool_ids,
)


def _migration():
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "p8270platgrp_merge_platform_ids_into_platform.py"
    spec = importlib.util.spec_from_file_location("p8270platgrp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _granted(ids, allowed):
    """Every function a selection grants, read the way the runtime reads it."""
    canon_ids, canon_allowed = canonical_builtin_selection(ids, allowed)
    out = set()
    for tool_id in canon_ids:
        functions = {fn for _alias, (group, fns) in BUILTIN_GROUP_ALIASES.items() if group == tool_id for fn in fns}
        if not functions:
            out.add(tool_id)
            continue
        keep = (canon_allowed or {}).get(tool_id)
        out |= {f"{tool_id}.{fn}" for fn in functions if keep is None or fn in keep}
    return out


_FLOWS = list(alias_scoped_functions("flow_builder"))


@pytest.mark.parametrize(
    ("ids", "allowed"),
    [
        (["agent_builder"], None),
        (["fleets"], None),
        (["agent_builder", "fleets"], None),
        (["agent_builder", "fleets", "flow_builder"], None),
        (["flow_builder", "web"], None),
        (["agent_builder", "code_interpreter"], {"agent_builder": ["list_agents", "get_agent"]}),
        (["fleets", "agent_builder"], {"fleets": ["list_fleets"]}),
        (["inspect_my_traces", "agent_builder"], None),
    ],
)
def test_round_trip_grants_what_every_row_granted(ids, allowed):
    """Upgrade then downgrade grants exactly what the row granted; upgrading again is stable."""
    migration = _migration()
    up_ids, up_allowed = canonical_builtin_selection(ids, allowed)
    assert not set(up_ids) & set(migration._FORMER)
    assert _granted(up_ids, up_allowed) == _granted(ids, allowed)
    down_ids, down_allowed = migration.downgrade_selection(up_ids, up_allowed)
    assert "platform" not in down_ids
    assert _granted(down_ids, down_allowed) == _granted(ids, allowed)
    assert canonical_builtin_selection(down_ids, down_allowed) == (up_ids, up_allowed)


def test_a_row_without_flow_builder_never_gains_the_flow_functions(monkeypatch):
    """The two default-on ids merge without widening: no flow function appears."""
    monkeypatch.setattr("app.core.feature_flags.is_feature_enabled", lambda flag: True)
    ids, allowed = canonical_builtin_selection(["agent_builder", "fleets"], None)
    assert ids == ["platform"]
    assert not set((allowed or {}).get("platform", [])) & set(_FLOWS)
    assert "flow_builder" not in runtime_builtin_tool_ids(ids, allowed)


def test_a_row_with_flow_builder_keeps_it_whatever_the_flag(monkeypatch):
    """The flag is UI-only: a row that named the id keeps the flow functions."""
    monkeypatch.setattr("app.core.feature_flags.is_feature_enabled", lambda flag: False)
    ids, allowed = canonical_builtin_selection(["agent_builder", "fleets", "flow_builder"], None)
    assert ids == ["platform"]
    assert "flow_builder" in runtime_builtin_tool_ids(ids, allowed)


def test_an_unscoped_platform_group_follows_the_flow_flag(monkeypatch):
    """The card switched on: the flow sub-area comes with it only while the flag is on."""
    monkeypatch.setattr("app.core.feature_flags.is_feature_enabled", lambda flag: False)
    off = runtime_builtin_tool_ids(["platform"], None)
    assert {"agent_builder", "fleets"} <= set(off)
    assert "flow_builder" not in off
    monkeypatch.setattr("app.core.feature_flags.is_feature_enabled", lambda flag: True)
    assert {"agent_builder", "fleets", "flow_builder"} <= set(runtime_builtin_tool_ids(["platform"], None))


def test_the_migration_revises_the_previous_head():
    migration = _migration()
    assert migration.revision == "p8270platgrp"
    assert migration.down_revision == "e8297flimp01"
