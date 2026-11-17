"""The render gate judges a scene on what it renders, never on the dump of its defaults.

What is proven, without a worker or a database:

* a round trip that adds then removes the video's ``references`` and a clip
  scene's ``consistency`` keeps every scene's preview, and so does the edit
  itself: neither draws anything;
* a drawing recorded earlier (its digests hashed with every default the
  spec fills) still counts, read through ``render_checks.adopted``;
* rewording one scene's narration marks that scene alone and the refusal
  names ``narration``; editing what it shows marks it and its neighbours,
  each naming what moved (``lines``, ``scene before``, ``scene after``); an
  overlay's corners are named ``overlay 0 quad``;
* a music change moves every scene's whole digest: a scene whose preview
  failed is changed, one that passed keeps it, and its parts say
  ``music``;
* the save lists the scenes whose preview still holds.
"""

from __future__ import annotations

import asyncio
import uuid

from app.services.video_studio import projects, render_checks
from app.services.video_studio.spec import validate_video_spec

CLIP = "00000000-0000-0000-0000-000000006857"
REFERENCE = "00000000-0000-0000-0000-0000000068f1"
SOURCE = "00000000-0000-0000-0000-0000000068aa"
RUG = {"id": "rug", "file_id": REFERENCE, "label": "the branded rug"}
QUAD = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]


class _NoMedia:
    async def get(self, *_args, **_kwargs):
        return None


def _video(
    *,
    references: list | None = None,
    consistency: list | None = None,
    narration: str = "One platform.",
    lines: str = "Every alert triaged.",
    music: dict | None = None,
    quad: list | None = None,
) -> dict:
    shot: dict = {"id": "shot", "type": "clip", "clip_id": CLIP}
    if consistency is not None:
        shot["consistency"] = consistency
    if quad is not None:
        shot["overlays"] = [{"type": "screen_insert", "quad": quad, "source": {"file_id": SOURCE}, "duration": 3}]
    spec: dict = {
        "title": "Gate",
        "freestyle": True,
        "scenes": [
            {"id": "open", "type": "title", "title": "Hello", "duration": 3},
            {"id": "claim", "type": "statement", "lines": [lines], "narration": narration, "duration": 4},
            shot,
            {"id": "proof", "type": "statement", "lines": ["Every alert closed."], "duration": 4},
            {"id": "end", "type": "outro", "title": "Thanks", "duration": 3},
        ],
    }
    if references is not None:
        spec["references"] = references
    if music is not None:
        spec["music"] = music
    return validate_video_spec(spec)


def _marks(effective: dict) -> render_checks.SceneDigests:
    return asyncio.run(render_checks.digest_scenes(_NoMedia(), effective))


def _previewed(marks: render_checks.SceneDigests, *, failing: tuple[str, ...] = ()) -> dict:
    """Passing previews of every scene (failing with a stutter for *failing*), recorded as a render records them."""
    seen: dict | None = None
    for scene_id, digest in marks.whole.items():
        found = [{"kind": "stutter", "severity": "fail", "at": 1.0, "scene_id": scene_id}] if scene_id in failing else []
        seen = render_checks.record_seen(
            seen, {scene_id: digest}, uuid.uuid4(), checks={"findings": found}, content=marks.content, parts=marks.parts
        )
    return seen or {}


def test_a_references_round_trip_keeps_every_preview():
    before = _marks(_video())
    seen = _previewed(before)
    held = _marks(_video(references=[RUG], consistency=["rug"]))
    # What a shot is held to draws nothing: the edit itself marks no scene.
    assert held.whole == before.whole and held.content == before.content
    assert render_checks.changed_scenes(seen, held.whole, held.content) == []
    # Removed again (references [], consistency left out): every scene as it was rendered.
    back = _marks(_video(references=[]))
    assert back.whole == before.whole
    assert render_checks.changed_scenes(seen, back.whole, back.content) == []
    assert render_checks.holding_scenes(seen, back.whole, back.content) == ["open", "claim", "shot", "proof", "end"]


def test_a_drawing_recorded_before_the_settled_digest_still_counts():
    marks = _marks(_video())
    # As an earlier release recorded them: under the digests of every default the spec fills.
    legacy: dict | None = None
    for scene_id, digest in marks.whole.items():
        earlier = [old for old, now in marks.aliases[scene_id].items() if now == digest and old != digest]
        assert earlier, scene_id
        legacy = render_checks.record_seen(legacy, {scene_id: earlier[0]}, uuid.uuid4(), checks={"findings": []})
    # Read raw, the defect: every scene "changed", none previewed.
    assert render_checks.changed_scenes(legacy, marks.whole, marks.content) == list(marks.whole)
    assert render_checks.passed_scenes(legacy, marks.whole, marks.content) == set()
    adopted = render_checks.adopted(legacy, marks)
    assert render_checks.changed_scenes(adopted, marks.whole, marks.content) == []
    assert render_checks.passed_scenes(adopted, marks.whole, marks.content) == set(marks.whole)
    # The stored entries are read, never rewritten.
    assert legacy["open"]["digest"] != adopted["open"]["digest"]


def test_a_narration_edit_marks_its_scene_and_names_it():
    before = _marks(_video())
    seen = _previewed(before)
    edited = _marks(_video(narration="Two platforms."))
    changed = render_checks.changed_scenes(seen, edited.whole, edited.content)
    assert changed == ["claim"]
    assert render_checks.changed_parts(seen, "claim", edited.parts["claim"]) == ["narration"]
    assert render_checks.holding_scenes(seen, edited.whole, edited.content) == ["open", "shot", "proof", "end"]


def test_a_picture_edit_marks_its_scene_and_its_neighbours_each_saying_what_moved():
    before = _marks(_video())
    seen = _previewed(before)
    edited = _marks(_video(lines="Two platforms."))
    changed = render_checks.changed_scenes(seen, edited.whole, edited.content)
    assert changed == ["open", "claim", "shot"]
    said = {scene_id: render_checks.changed_parts(seen, scene_id, edited.parts[scene_id]) for scene_id in changed}
    assert said == {"open": ["scene after"], "claim": ["lines"], "shot": ["scene before"]}


def test_an_overlay_corner_moved_is_named_by_its_field():
    before = _marks(_video(quad=QUAD))
    seen = _previewed(before)
    moved = _marks(_video(quad=[[0.2, 0.1], *QUAD[1:]]))
    assert "shot" in render_checks.changed_scenes(seen, moved.whole, moved.content)
    assert render_checks.changed_parts(seen, "shot", moved.parts["shot"]) == ["overlay 0 quad"]


def test_a_music_change_moves_every_scene_and_keeps_only_passing_previews():
    before = _marks(_video())
    seen = _previewed(before, failing=("proof",))
    scored = _marks(_video(music={"track": "signal"}))
    assert all(scored.whole[s] != before.whole[s] for s in before.whole)
    assert all("music" in render_checks.changed_parts(seen, s, scored.parts[s]) for s in before.whole)
    # A preview that passed keeps counting over a new track; a failed one never.
    assert "proof" in render_checks.changed_scenes(seen, scored.whole, scored.content)
    assert "proof" not in render_checks.holding_scenes(seen, scored.whole, scored.content)


def test_only_the_newest_drawing_keeps_its_parts():
    first = _marks(_video())
    second = _marks(_video(narration="Two platforms."))
    seen = _previewed(first)
    seen = render_checks.record_seen(
        seen, {"claim": second.whole["claim"]}, "r2", checks={"findings": []}, content=second.content, parts=second.parts
    )
    drawings = seen["claim"]["drawings"]
    assert drawings[0]["parts"] == second.parts["claim"] and all("parts" not in d for d in drawings[1:])


def test_the_gate_refusal_names_what_changed(monkeypatch):
    from types import SimpleNamespace

    from app.services.video_studio import lexicon_store, quality, voice, voice_check

    async def clean(*_args, **_kwargs):
        return quality.QualityReport(scene_count=5)

    async def own_lexicon(_db, voiceover):
        return voice.lexicon_of(voiceover)

    async def resolve(_db, _user_id, _voiceover):
        return SimpleNamespace(name="video")

    async def no_failure(_target, _spec, _lexicon=None):
        return []

    class _Rows(_NoMedia):
        async def execute(self, _statement, _params=None):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    monkeypatch.setattr(quality, "check_video_quality", clean)
    monkeypatch.setattr(lexicon_store, "for_voiceover", own_lexicon)
    monkeypatch.setattr(voice, "resolve", resolve)
    monkeypatch.setattr(voice_check, "failed_narrations", no_failure)
    seen = _previewed(_marks(_video()))
    project = SimpleNamespace(id=uuid.uuid4(), revision=3, title="Gate", scene_previews=seen)
    user = SimpleNamespace(id=uuid.uuid4())

    def changed_in(effective: dict) -> list[render_checks.GateReason]:
        reasons = asyncio.run(render_checks.gate(_Rows(), project, user, effective, quality="high"))
        return [reason for reason in reasons if reason.code == "scene_changed"]

    changed = changed_in(_video(narration="Two platforms."))
    assert [r.scene_id for r in changed] == ["claim"]
    assert "changed since it was last previewed or rendered (narration): preview it again" in changed[0].message
    # The round trip refuses nothing.
    assert changed_in(_video(references=[])) == []


def test_the_holding_line_names_the_scenes_and_counts_the_rest():
    assert projects.holding_line(["a", "b"]) == (
        "Previews that still hold: scene `a`, `b` - render_video needs no new preview of them."
    )
    many = [f"s{i}" for i in range(projects.HOLDING_NAMED + 3)]
    assert projects.holding_line(many).endswith("and 3 more - render_video needs no new preview of them.")
