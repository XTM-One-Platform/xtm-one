"""update_agent handover_types merges, and a target may hold several types (#2207).

Typing some targets never resets the others: on a legacy (empty) map the
targets not named keep running every type (written ``all``), on an explicit map
they keep their entry. ``A:consult|delegate`` (or repeated pairs for A) gives A
both tools.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.builtin_tools.handover import (
    handover_peer_tag,
    handover_tool_types,
    peer_handover_type,
    peer_tool_types,
)
from app.services.handover_types import (
    handover_type_set,
    merged_handover_types,
    parse_handover_type_entry,
)
from app.services.scheduled_work_items import hands_work_to

DESKS = [f"d{i}" for i in range(5)]
EXTERNAL = ["x1", "x2", "x3"]


def test_typing_three_targets_of_a_legacy_map_leaves_the_others_running_every_type():
    merged = merged_handover_types(DESKS + EXTERNAL, {}, {aid: "consult" for aid in EXTERNAL})
    for desk in DESKS:
        assert peer_tool_types(merged, desk) == frozenset({"delegate", "consult", "transfer"})
    for ext in EXTERNAL:
        assert peer_tool_types(merged, ext) == frozenset({"consult"})


def test_a_write_naming_one_target_leaves_the_other_stored_entries_unchanged():
    stored = {"a": "consult", "b": "transfer"}
    assert merged_handover_types(["a", "b"], stored, {"a": "delegate"}) == {"a": "delegate", "b": "transfer"}


def test_several_types_for_one_target_register_each_tool():
    types = {"a": "consult|delegate", "b": "consult"}
    assert peer_tool_types(types, "a") == frozenset({"consult", "delegate"})
    assert peer_tool_types(types, "b") == frozenset({"consult"})
    peers = [SimpleNamespace(id="a"), SimpleNamespace(id="b")]
    assert handover_tool_types(peers, types) == frozenset({"consult", "delegate"})
    assert peer_handover_type(types, "a") == "consult"
    tag, instruction = handover_peer_tag(types, "a", {"consult": "c", "delegate": "d", "transfer": "t"})
    assert tag == "CONSULT|DELEGATE"
    assert "consult_agent" in instruction and "spawn_background_task" in instruction


def test_entries_parse_to_one_canonical_form():
    assert parse_handover_type_entry("Delegate|consult") == "delegate|consult"
    assert parse_handover_type_entry("consult|delegate|transfer") == "all"
    assert parse_handover_type_entry("all") == "all"
    assert parse_handover_type_entry("mandatory_transfer") == "mandatory_transfer"
    assert parse_handover_type_entry("mandatory_transfer|consult") is None
    assert parse_handover_type_entry("bogus") is None
    assert handover_type_set("all") == frozenset({"delegate", "consult", "transfer"})
    assert handover_type_set("consult") == frozenset({"consult"})


def test_a_single_type_entry_runs_as_before():
    assert peer_tool_types({"a": "consult"}, "a") == frozenset({"consult"})
    assert peer_tool_types({"a": "mandatory_transfer"}, "a") == frozenset()
    assert peer_tool_types({"a": "consult"}, "b") == frozenset({"delegate"})


def test_responsibility_reads_every_type_of_an_entry():
    assert hands_work_to(["a"], {"a": "consult|delegate"}, "a") is True
    assert hands_work_to(["a"], {"a": "consult"}, "a") is False
    assert hands_work_to(["a"], {"a": "all"}, "a") is True
