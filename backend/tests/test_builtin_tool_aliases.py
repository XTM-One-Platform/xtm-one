"""[DEPRECATED] built-in ids resolve to their group with the functions they granted."""

from __future__ import annotations

import pytest

from app.services.builtin_allowed_tools import BuiltinAllowedToolsError, normalize_builtin_allowed_tools
from app.services.builtin_tool_aliases import (
    alias_group,
    alias_scoped_functions,
    canonical_builtin_selection,
    describe_builtin_tool_id,
    fold_builtin_selection,
    runtime_builtin_tool_ids,
)

_MY = ["inspect_my_traces", "get_trace_payload"]
_OTHERS = ["inspect_agent_traces", "get_trace_payload"]
# The functions each former id of the platform group grants.
_AB = list(alias_scoped_functions("agent_builder"))
_FLEETS = list(alias_scoped_functions("fleets"))
_FLOWS = list(alias_scoped_functions("flow_builder"))


@pytest.mark.parametrize(
    ("ids", "allowed", "expected_ids", "expected_allowed"),
    [
        # One former id: the group, scoped to what that id granted.
        (["code_interpreter", "inspect_my_traces"], None, ["code_interpreter", "observability"], {"observability": _MY}),
        (["web_fetch"], None, ["web"], {"web": ["web_fetch", "fetch_image_asset"]}),
        (["inspect_agent_traces"], None, ["observability"], {"observability": _OTHERS}),
        # Both former ids granted every function: the group unscoped.
        (["inspect_my_traces", "inspect_agent_traces"], None, ["observability"], None),
        # The group listed itself wins over a former id next to it.
        (["observability", "inspect_my_traces"], None, ["observability"], None),
        # Other groups' scoping is carried as it was.
        (
            ["inspect_my_traces", "agent_builder"],
            {"agent_builder": ["list_agents"]},
            ["observability", "platform"],
            # Only agent_builder was carried: the group keeps what its list named.
            {"platform": ["list_agents"], "observability": _MY},
        ),
        # The three platform ids, unscoped: the group, and the flag-gated flow
        # sub-area kept by an explicit list so the flag never takes it away.
        (["agent_builder", "fleets"], None, ["platform"], {"platform": [*_AB, *_FLEETS]}),
        (
            ["agent_builder", "fleets", "flow_builder"],
            None,
            ["platform"],
            {"platform": [*_AB, *_FLEETS, *_FLOWS]},
        ),
        # Two former ids of one group, one of them scoped: each keeps what it gave.
        (["web_fetch", "browser"], {"browser": ["browser"]}, ["web"], None),
        (["web_fetch", "browser"], {"web_fetch": ["web_fetch"]}, ["web"], {"web": ["web_fetch", "browser"]}),
        (["work_items", "shared_state"], None, ["coordination"],
         {"coordination": ["create_work_item", "update_work_item", "list_work_items", "get_work_item",
                           "claim_work_item", "complete_work_item", "delegate_to_assignment",
                           "list_programs", "get_program_status", "send_decision_card",
                           "get_shared_state", "set_shared_state", "list_shared_state",
                           "resolve_shared_state", "delete_shared_state",
                           "record_friction", "list_frictions", "dispose_friction",
                           "arm_timer", "cancel_timer", "list_timers"]}),
        # A list keyed by a former id next to its group listed: restricts that id's functions only.
        (["design"], {"template_search": ["get_template"]}, ["design"],
         {"design": ["get_template", "create_template", "update_template", "delete_template",
                     "attach_template_asset", "detach_template_asset", "validate_presentation",
                     "render_source_pages", "inspect_pptx_identity", "inspect_html_identity",
                     "compare_palette"]}),
        # A list keyed by a former id moves under the group, within what it granted.
        (["inspect_agent_traces"], {"inspect_agent_traces": ["inspect_agent_traces", "inspect_my_traces"]},
         ["observability"], {"observability": ["inspect_agent_traces"]}),
        # No former id: unchanged.
        (["code_interpreter"], None, ["code_interpreter"], None),
        (["web"], {"web": ["browser"]}, ["web"], {"web": ["browser"]}),
        ([], None, [], None),
        (None, None, [], None),
    ],
)
def test_canonical_selection(ids, allowed, expected_ids, expected_allowed):
    assert canonical_builtin_selection(ids, allowed) == (expected_ids, expected_allowed)


@pytest.mark.parametrize(
    ("ids", "allowed"),
    [
        (["inspect_my_traces"], None),
        (["inspect_agent_traces", "web_fetch"], None),
        (["inspect_my_traces", "inspect_agent_traces"], None),
        (["observability"], {"observability": ["inspect_agent_traces"]}),
        (["web_fetch", "browser"], {"web_fetch": ["web_fetch"]}),
        (["workset", "generate_image", "fimi_analytics"], {"workset": ["workset_search"]}),
    ],
)
def test_canonical_selection_is_idempotent(ids, allowed):
    once = canonical_builtin_selection(ids, allowed)
    assert canonical_builtin_selection(*once) == once


@pytest.mark.parametrize(
    ("ids", "allowed", "wired"),
    [
        # A former id keeps wiring exactly the trace surface it wired before.
        (["inspect_my_traces"], None, {"observability", "inspect_my_traces"}),
        (["inspect_agent_traces"], None, {"observability", "inspect_agent_traces"}),
        # The group unscoped wires both; scoped, only what it keeps.
        (["observability"], None, {"observability", "inspect_my_traces", "inspect_agent_traces"}),
        (["observability"], {"observability": _MY}, {"observability", "inspect_my_traces"}),
        (["observability"], {"observability": ["get_trace_payload"]}, {"observability"}),
        (["code_interpreter"], None, {"code_interpreter"}),
        # A merged group wires under the former ids it keeps.
        (["web_fetch"], None, {"web", "web_fetch"}),
        (["web"], None, {"web", "web_fetch", "browser"}),
        (["web"], {"web": ["browser"]}, {"web", "browser"}),
        (["coordination"], {"coordination": ["workset_search"]}, {"coordination", "workset"}),
    ],
)
def test_runtime_ids_carry_the_former_trace_surface(ids, allowed, wired):
    assert set(runtime_builtin_tool_ids(ids, allowed)) == wired


def test_a_migrated_row_wires_what_the_row_wired_before():
    """The migration's output read by the runtime equals the former row read by the runtime."""
    for before in (
        ["inspect_my_traces"],
        ["inspect_agent_traces"],
        ["inspect_my_traces", "inspect_agent_traces"],
        ["web_fetch"],
        ["browser", "generate_file"],
        ["work_items", "shared_state", "workset"],
        ["template_search", "document_vision"],
        ["stix_bundle_validator"],
    ):
        after = canonical_builtin_selection(before, None)
        assert set(runtime_builtin_tool_ids(*after)) == set(runtime_builtin_tool_ids(before, None)), before


def test_the_grounded_extraction_is_its_own_group_never_reached_through_cti():
    """An agent with Threat Intelligence switched on does not get the grounded extraction (a
    tool of hundreds of model calls): it is a group of its own, wired only where selected."""
    assert alias_group("stix_extractor") is None
    assert "stix_extractor" not in runtime_builtin_tool_ids(["cti"], None)
    assert "stix_extractor" in runtime_builtin_tool_ids(["stix_extractor"], None)
    assert "stix_extractor" in runtime_builtin_tool_ids(["cti", "stix_extractor"], None)


def test_describe_names_the_group_of_a_former_id():
    assert alias_group("inspect_my_traces") == "observability"
    assert alias_group("observability") is None
    assert describe_builtin_tool_id("inspect_agent_traces") == "inspect_agent_traces [DEPRECATED] alias of observability"
    assert describe_builtin_tool_id("web_fetch") == "web_fetch [DEPRECATED] alias of web"
    assert describe_builtin_tool_id("code_interpreter") == "code_interpreter"


class _Row:
    def __init__(self, ids, allowed=None):
        self.builtin_tool_ids = ids
        self.builtin_allowed_tools = allowed


def test_fold_stores_a_former_id_as_its_group_scoped_to_what_it_granted():
    """A legacy agent shows one Traces card, scoped as before."""
    row = _Row(["code_interpreter", "inspect_agent_traces"])
    assert fold_builtin_selection(row) is True
    assert row.builtin_tool_ids == ["code_interpreter", "observability"]
    assert row.builtin_allowed_tools == {"observability": _OTHERS}


def test_fold_a_save_that_selects_the_group_next_to_a_former_id_writes_the_group_only():
    """Saving Traces next to the former id no longer stores both."""
    row = _Row(["inspect_agent_traces", "observability"])
    assert fold_builtin_selection(row) is True
    assert row.builtin_tool_ids == ["observability"]
    assert row.builtin_allowed_tools is None


def test_fold_leaves_a_canonical_selection_untouched():
    scoped = {"observability": _MY}
    row = _Row(["observability", "web"], scoped)
    assert fold_builtin_selection(row) is False
    assert row.builtin_tool_ids == ["observability", "web"]
    assert row.builtin_allowed_tools is scoped
    empty = _Row(None)
    assert fold_builtin_selection(empty) is False
    assert empty.builtin_tool_ids is None


def test_allowed_tools_keyed_by_a_former_id_land_on_the_group():
    # Alone, the list restricts what the former id granted, not its siblings' functions.
    normalized = normalize_builtin_allowed_tools({"inspect_my_traces": ["inspect_my_traces"]})
    assert normalized == {"observability": ["inspect_my_traces", "inspect_agent_traces"]}
    # With the ids the writer stores next to it, the row is exactly what it granted before.
    assert canonical_builtin_selection(["inspect_my_traces"], normalized) == (
        ["observability"],
        {"observability": ["inspect_my_traces"]},
    )
    with pytest.raises(BuiltinAllowedToolsError):
        normalize_builtin_allowed_tools({"inspect_my_traces": ["inspect_agent_traces"]})


def test_allowed_tools_keyed_by_a_former_id_never_narrow_its_siblings():
    """A list scoping ``browser`` leaves ``web_fetch``'s functions alone."""
    assert normalize_builtin_allowed_tools({"browser": []}) == {"web": ["web_fetch", "fetch_image_asset"]}
    assert normalize_builtin_allowed_tools({"browser": ["browser"], "web": ["browser", "web_fetch"]}) == {
        "web": ["web_fetch", "browser"]
    }
    with pytest.raises(BuiltinAllowedToolsError):
        normalize_builtin_allowed_tools({"browser": ["web_fetch"]})


def _migration_8156():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "m8156bigrp01_merge_builtin_ids_into_five_groups.py"
    spec = importlib.util.spec_from_file_location("m8156bigrp01", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _granted(ids, allowed):
    """Every function a selection grants, read the way the runtime reads it."""
    from app.services.builtin_tool_aliases import BUILTIN_GROUP_ALIASES, canonical_builtin_selection

    canon_ids, canon_allowed = canonical_builtin_selection(ids, allowed)
    out = set()
    for tool_id in canon_ids:
        functions = {fn for alias, (group, fns) in BUILTIN_GROUP_ALIASES.items() if group == tool_id for fn in fns}
        if not functions:
            out.add(tool_id)
            continue
        keep = (canon_allowed or {}).get(tool_id)
        out |= {f"{tool_id}.{fn}" for fn in functions if keep is None or fn in keep}
    return out


@pytest.mark.parametrize(
    ("ids", "allowed"),
    [
        (["web_fetch"], None),
        (["web_fetch", "browser"], {"browser": ["browser"]}),
        (["browser", "generate_file", "code_interpreter"], {"generate_file": ["generate_file", "read_file_content"]}),
        (["work_items", "shared_state", "workset"], {"workset": ["workset_search", "workset_read_item"]}),
        (["template_search", "template_authoring", "document_vision", "inspect_my_traces"], None),
        (["stix_bundle_validator", "agent_builder"], {"agent_builder": ["list_agents"]}),
    ],
)
def test_migration_8156_round_trips_what_every_row_grants(ids, allowed):
    """Upgrade then downgrade grants exactly what the row granted, and upgrading again is stable."""
    migration = _migration_8156()
    up_ids, up_allowed = canonical_builtin_selection(ids, allowed)
    assert _granted(up_ids, up_allowed) == _granted(ids, allowed)
    down_ids, down_allowed = migration.downgrade_selection(up_ids, up_allowed)
    assert not set(down_ids) & set(migration._GROUPS)
    assert _granted(down_ids, down_allowed) == _granted(ids, allowed)
    assert canonical_builtin_selection(down_ids, down_allowed) == (up_ids, up_allowed)


def test_alias_functions_match_the_builders():
    """Every former id grants exactly what its builder defines (no drift)."""
    from app.services.builtin_tool_aliases import BUILTIN_GROUP_ALIASES
    from app.services.builtin_tool_catalog import build_builtin_tool_member_catalog

    members = build_builtin_tool_member_catalog(include_disabled=True)
    for alias, (_group, functions) in BUILTIN_GROUP_ALIASES.items():
        if alias in {"inspect_my_traces", "inspect_agent_traces"}:
            continue  # pinned by test_builtin_tool_groups (the trace builder serves both)
        assert tuple(d["name"] for d in members[alias]) == functions, alias
