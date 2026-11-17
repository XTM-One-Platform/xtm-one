"""The revision history of a video's spec: change paths, scene restore, edit_video's restore_scene (#10798)."""

from __future__ import annotations

import copy
import json

import pytest

from app.services.video_studio.projects import apply_operations, restored_revisions
from app.services.video_studio.revisions import RevisionLine, changed_paths, restore_scene
from app.services.video_studio.spec import VideoSpecError


def _spec(tolerance: float, title: str = "Demo") -> dict:
    return {
        "title": title,
        "scenes": [
            {"id": "intro", "type": "title", "text": "Hello"},
            {
                "id": "a",
                "type": "clip",
                "overlays": [{"key": {"tolerance": tolerance, "feather": 2}, "despill": {"strength": 0.4}}],
            },
            {"id": "outro", "type": "title", "text": "Bye"},
        ],
    }


def test_changed_paths_name_the_field_paths_per_scene():
    changed = changed_paths(_spec(0.1), _spec(0.3, title="Demo 2"))
    assert changed == {"video": ["title"], "a": ["overlays.0.key.tolerance"]}


def test_changed_paths_name_added_removed_and_reordered_scenes():
    before = _spec(0.1)
    after = copy.deepcopy(before)
    after["scenes"] = [after["scenes"][2], after["scenes"][0], {"id": "new", "type": "title", "text": "x"}]
    changed = changed_paths(before, after)
    assert changed["new"] == ["added"]
    assert changed["a"] == ["removed"]
    assert "scenes.order" in changed["video"]


def test_restore_scene_copies_one_scene_and_leaves_the_others_byte_identical():
    revision_1, current = _spec(0.1), _spec(0.5)
    current["scenes"][0]["text"] = "Edited since"
    restored, line = restore_scene(current, revision_1, "a")
    assert restored["scenes"][1] == revision_1["scenes"][1]
    for index in (0, 2):
        assert json.dumps(restored["scenes"][index], sort_keys=True) == json.dumps(
            current["scenes"][index], sort_keys=True
        )
    assert restored["title"] == current["title"]
    assert line == "scene a: restored"


def test_restore_scene_puts_back_a_removed_scene_at_its_earlier_place():
    earlier, current = _spec(0.1), _spec(0.1)
    del current["scenes"][1]
    restored, _ = restore_scene(current, earlier, "a")
    assert [s["id"] for s in restored["scenes"]] == ["intro", "a", "outro"]


def test_restore_scene_refuses_a_scene_the_revision_did_not_have():
    with pytest.raises(LookupError, match="no scene 'zz'"):
        restore_scene(_spec(0.1), _spec(0.1), "zz")


def test_edit_video_restore_scene_operation_reads_the_loaded_revision():
    ops = [{"op": "restore_scene", "scene": "a", "revision": 1}]
    assert restored_revisions(ops) == {1}
    result, lines = apply_operations(_spec(0.5), ops, {1: _spec(0.1)})
    assert result["scenes"][1]["overlays"][0]["key"]["tolerance"] == 0.1
    assert lines == ["scene a: restored from revision 1"]


def test_edit_video_restore_scene_without_a_held_revision_is_refused():
    with pytest.raises(VideoSpecError):
        apply_operations(_spec(0.5), [{"op": "restore_scene", "scene": "a", "revision": 7}], {})


def test_revision_line_lists_author_summary_and_paths():
    line = RevisionLine(
        revision=3,
        at="2026-11-10 12:00 UTC",
        author="agent@example.com",
        via="agent",
        summary=["scene a: updated overlays"],
        changed={"a": ["overlays.0.key.tolerance"]},
    ).text()
    assert line.startswith("- r3 2026-11-10 12:00 UTC by agent@example.com (agent): scene a: updated overlays")
    assert "[scene a: overlays.0.key.tolerance]" in line
