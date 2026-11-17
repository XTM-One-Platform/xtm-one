"""The render gate trusts scene previews, and a save invalidates them scene by scene. Synthetic rows only.

Through the real ``render_checks.gate``, ``quality.content_facts`` and
``projects.start_render`` over a fake session:

* a dense finding about an AI shot is cleared once a preview of every scene
  showing the shot passed, drawn as it is now (the same trim) - never by a
  preview of one of them only, a preview at another trim, or one that failed;
* a finding a previous render of the revision left on a scene is cleared by a
  newer passing preview of that scene;
* a video whose every scene preview passes renders without ``force``;
* the take that plays is judged by the camera tolerance it passed its own
  checks under, never by one a later ``generate_video_clip`` call stored;
* a scene's digest is its spec, its neighbours', the video's look and audio,
  and the take each plays: editing one scene changes three digests at most, a
  shot generated again whose take still plays changes none, and a drawing
  hashed by an older version is no drawing at all.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.models.video_studio import JOB_READY, JOB_RUNNING, VideoClip, VideoRender
from app.services.video_studio import (
    clips,
    frame_qc,
    jobs,
    lexicon_store,
    projects,
    quality,
    render_checks,
    timeline,
    voice,
)
from app.services.video_studio import voice_check as voice_checks
from app.services.video_studio.spec import parse_video_spec, validate_video_spec

CLIP = "00000000-0000-0000-0000-0000000061c1"
USER = SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-0000000061a1"), role="user", email="maker@example.com")


def _effective(
    *,
    trim_end: float | None = None,
    lines: str = "One platform",
    title: str = "Hello",
    music: dict | None = None,
    claim: dict | None = None,
) -> dict:
    """Six scenes; the AI shot plays as the footage of `shot` and behind the statement `close`."""
    return validate_video_spec(
        {
            "title": "Trust",
            "freestyle": True,
            "scenes": [
                {"id": "open", "type": "title", "title": title, "duration": 3},
                {"id": "shot", "type": "clip", "clip_id": CLIP, **({"trim_end": trim_end} if trim_end else {})},
                {"id": "claim", "type": "statement", "lines": [lines], "duration": 4, **(claim or {})},
                {"id": "proof", "type": "statement", "lines": ["Every alert triaged"], "duration": 4},
                {"id": "close", "type": "statement", "lines": ["Every alert closed"], "backdrop": {"clip_id": CLIP}},
                {"id": "end", "type": "title", "title": "Thank you", "duration": 3},
            ],
            **({"music": music} if music else {}),
        }
    )


def _shot(**fields) -> VideoClip:
    return VideoClip(
        id=uuid.UUID(CLIP),
        user_id=USER.id,
        provider="p",
        model="m",
        prompt="A fox at dusk, the camera locked off",
        status=fields.pop("status", JOB_READY),
        file_id=fields.pop("file_id", uuid.UUID("00000000-0000-0000-0000-0000000061f1")),
        finished_at=fields.pop("finished_at", datetime(2026, 10, 20, tzinfo=UTC)),
        params=fields.pop("params", {}),
        **fields,
    )


class _Db:
    """The rows the gate reads: the shot as it is now, the whole renders of the revision (newest first)."""

    def __init__(self, shot: VideoClip | None = None, renders: list | None = None) -> None:
        self.shot = shot if shot is not None else _shot()
        self.renders = renders or []
        self.added: list = []

    async def get(self, model, _key, **_options):
        return self.shot if model is VideoClip else None

    async def execute(self, statement, _params=None):
        rows = self.renders if "video_renders.checks IS NOT NULL" in str(statement) else []
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


def _camera_moved() -> quality.QualityFinding:
    return quality.QualityFinding(
        1, "shot", "clip", "", "camera_moved", "dense", "camera moved on a locked-off shot", group="content", clip_id=CLIP
    )


@pytest.fixture
def world(monkeypatch):
    """The quality check saved for the revision (what a test puts in it), no narration to check."""
    world = SimpleNamespace(findings=[], audits=[])

    async def checked(*_args, **_kwargs):
        return quality.QualityReport(scene_count=6, findings=tuple(world.findings), revision=4)

    async def resolve(*_args, **_kwargs):
        return SimpleNamespace(name="video")

    async def no_failure(*_args, **_kwargs):
        return []

    async def own_lexicon(_db, voiceover):
        return voice.lexicon_of(voiceover)

    async def audit(_db, **kwargs):
        world.audits.append(kwargs)

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(quality, "check_video_quality", checked)
    monkeypatch.setattr(voice, "resolve", resolve)
    monkeypatch.setattr(voice_checks, "failed_narrations", no_failure)
    monkeypatch.setattr(lexicon_store, "for_voiceover", own_lexicon)
    monkeypatch.setattr(jobs, "renders_in_flight", no_failure)
    monkeypatch.setattr(jobs, "enqueue", nothing)
    monkeypatch.setattr("app.services.audit.log_audit", audit)
    return world


def _digests(db: _Db, effective: dict | None = None) -> dict[str, str]:
    return asyncio.run(render_checks.scene_digests(db, effective or _effective()))


def _previewed(db: _Db, scenes: list[str], *, effective: dict | None = None, passed: bool = True) -> dict:
    """What previews of *scenes* recorded on the video, drawn from *effective*; failing ones with a stutter."""
    digests = _digests(db, effective)
    seen: dict = {}
    for scene in scenes:
        checks = {"findings": [] if passed else [{"kind": "stutter", "severity": "fail", "at": 1.0, "scene_id": scene}]}
        seen = render_checks.record_seen(seen, {scene: digests[scene]}, uuid.uuid4(), checks=checks)
    return seen


def _project(previews: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), revision=4, title="Trust", scene_previews=previews)


def _gate(db: _Db, previews: dict | None = None, *, quality_name: str = "high") -> list[render_checks.GateReason]:
    return asyncio.run(render_checks.gate(db, _project(previews), USER, _effective(), quality=quality_name))


# ── A dense finding about a shot, cleared by its scenes' previews ─────────


def test_a_shot_finding_holds_the_render_until_every_scene_showing_it_passed_a_preview(world):
    db = _Db()
    world.findings = [_camera_moved()]
    assert [r.code for r in _gate(db)] == ["quality"]
    # One of the two scenes showing it is not enough: `close` plays it behind its words.
    assert [r.code for r in _gate(db, _previewed(db, ["shot"]))] == ["quality"]
    assert _gate(db, _previewed(db, ["shot", "close"])) == []


def test_a_preview_that_failed_or_drew_another_trim_clears_nothing(world):
    db = _Db()
    world.findings = [_camera_moved()]
    failed = _previewed(db, ["shot", "close"], passed=False)
    assert [r.code for r in _gate(db, failed)] == ["quality"]
    # `shot` previewed at another trim: changed since (its digest), and the finding still holds.
    other_trim = _previewed(db, ["shot", "close"], effective=_effective(trim_end=3.5))
    reasons = _gate(db, other_trim)
    assert sorted((r.code, r.scene_id) for r in reasons) == [("quality", "shot"), ("scene_changed", "shot")]


def test_a_scene_finding_is_never_cleared_by_a_preview(world):
    db = _Db()
    world.findings = [quality.QualityFinding(2, "claim", "statement", "", "reading", "dense", "3 words in 0.4 s")]
    assert [r.code for r in _gate(db, _previewed(db, ["open", "shot", "claim", "proof", "close", "end"]))] == ["quality"]


def test_a_finding_a_render_left_is_cleared_by_a_newer_passing_preview_of_its_scene(world):
    left = {"findings": [{"kind": "stutter", "severity": "fail", "at": 9.5, "scene_id": "claim"}]}
    db = _Db(renders=[VideoRender(id=uuid.uuid4(), quality="high", checks=left)])
    assert [r.code for r in _gate(db)] == ["open_findings"]
    assert _gate(db, _previewed(db, ["claim"])) == []
    # A finding of no scene stays open whatever was previewed.
    db.renders[0].checks = {"findings": [{"kind": "stutter", "severity": "fail", "at": 9.5, "scene_id": None}]}
    assert [r.code for r in _gate(db, _previewed(db, ["claim"]))] == ["open_findings"]


def test_a_video_whose_every_scene_preview_passes_renders_without_force(world):
    """Acceptance: a shot finding and a render's open finding, every scene then previewed and passed."""
    left = {"findings": [{"kind": "frozen", "severity": "warn", "at": 4.0, "end": 5.0, "scene_id": "shot"}]}
    db = _Db(renders=[VideoRender(id=uuid.uuid4(), quality="high", checks=left)])
    world.findings = [_camera_moved()]

    async def effective(*_args, **_kwargs):
        return SimpleNamespace(spec=_effective())

    project = _project(_previewed(db, ["open", "shot", "claim", "proof", "close", "end"]))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(projects, "effective_spec", effective)
        started = asyncio.run(projects.start_render(db, project, USER, quality="high"))
    assert started.render.gate_override is None and not started.reused and db.added == [started.render]
    assert world.audits and "forced" not in world.audits[-1]["details"]


# ── The take that plays, judged by what it passed ─────────────────────────


def _facts(params: dict) -> quality.ShotFacts:
    db = SimpleNamespace(get=lambda _model, _key: asyncio.sleep(0, result=_shot(params=params)))
    facts = asyncio.run(quality.content_facts(db, parse_video_spec(_effective()), USER))
    return facts.shots[CLIP]


def test_the_take_that_plays_is_judged_by_the_tolerance_it_passed_under(monkeypatch):
    async def no_speech(*_args, **_kwargs):
        return None

    monkeypatch.setattr(voice, "resolve", no_speech)
    moved = {"motion_ssim": 0.6}
    # A later call stored max_camera_motion 0.2 on the shot; the take that plays passed with no tolerance.
    checked = _facts({frame_qc.MAX_CAMERA_MOTION: 0.2, frame_qc.QC_KEY: {**moved, frame_qc.CHECKED_WITH: {}}})
    assert checked.max_camera_motion is None and not quality.camera_moved(checked)
    # It passed under 0.5: judged by 0.5, which its 0.4 of motion is under.
    passed = _facts({frame_qc.MAX_CAMERA_MOTION: 0.2, frame_qc.QC_KEY: {**moved, frame_qc.CHECKED_WITH: {"max_camera_motion": 0.5}}})
    assert passed.max_camera_motion == 0.5 and not quality.camera_moved(passed)
    # A take made before the record: the shot's own tolerance, as before.
    legacy = _facts({frame_qc.MAX_CAMERA_MOTION: 0.2, frame_qc.QC_KEY: dict(moved)})
    assert legacy.max_camera_motion == 0.2 and quality.camera_moved(legacy)


def test_a_ready_take_that_kept_playing_through_a_failed_regeneration_keeps_its_own_record(monkeypatch):
    async def no_speech(*_args, **_kwargs):
        return None

    monkeypatch.setattr(voice, "resolve", no_speech)
    params = {frame_qc.QC_KEY: {"motion_ssim": 0.6, frame_qc.CHECKED_WITH: {}}}
    clip = _shot(params=params)
    clips.keep_take(params, clip)
    # The regeneration asked a tolerance its take failed; the attempt ended, the previous take plays.
    params[frame_qc.MAX_CAMERA_MOTION] = 0.2
    params[frame_qc.QC_KEY] = {"motion_ssim": 0.3}
    clips.end_attempt(params, status="failed", reason="camera moved")
    facts = _facts(params)
    assert facts.max_camera_motion is None and not quality.camera_moved(facts)


def test_a_camera_finding_names_its_shot():
    shot = quality.ShotFacts("Fox", JOB_READY, motion_ssim=0.3, max_camera_motion=0.2)
    facts = quality.ContentFacts(shots={CLIP: shot})
    spec = parse_video_spec(_effective())
    found = quality.content_findings(spec, timeline.estimate_plan(spec, {}).document, facts, _effective())
    moved = [f for f in found if f.kind == "camera_moved"]
    assert moved and all(f.clip_id == CLIP for f in moved) and "a preview that passes clears this" in moved[0].message


# ── Previews invalidated scene by scene ───────────────────────────────────


def test_editing_one_scene_changes_three_digests_at_most():
    db = _Db()
    before = _digests(db)
    after = _digests(db, _effective(lines="Two platforms"))
    assert sorted(s for s in before if before[s] != after[s]) == ["claim", "proof", "shot"]
    # The first scene has one neighbour: two digests.
    changed = _digests(db, _effective(title="Hi"))
    assert sorted(s for s in before if before[s] != changed[s]) == ["open", "shot"]
    # The video's sound is every scene's.
    scored = _digests(db, _effective(music={"track": "ascent"}))
    assert all(before[s] != scored[s] for s in before)


def _changed(before: dict, after: dict) -> list[str]:
    return sorted(s for s in before if before[s] != after[s])


def test_a_narration_edit_changes_its_own_digest_and_a_neighbours_only_through_their_transition():
    """A neighbour's preview draws the scene's picture at the edge and the transition, never its narration."""
    db = _Db()
    said = _digests(db, _effective(claim={"narration": "One platform for every alert."}))
    reworded = _digests(db, _effective(claim={"narration": "One platform for each alert, and every analyst."}))
    assert _changed(said, reworded) == ["claim"]
    # Its pause before the narration, or its own transition in, are not drawn by a neighbour's preview either.
    assert _changed(said, _digests(db, _effective(claim={"narration": "One platform for every alert.", "narration_delay": 1.0}))) == ["claim"]
    # A scene too short to dissolve from is cut from; narrated, it lasts long enough: the cut into `proof` dissolves.
    short = _digests(db, _effective(claim={"duration": 1.2}))
    narrated = _digests(db, _effective(claim={"duration": 1.2, "narration": "One platform for every alert, every analyst."}))
    assert _changed(short, narrated) == ["claim", "proof"]
    transitions = asyncio.run(projects.preview_transitions(db, _effective(claim={"duration": 1.2})))
    assert transitions["claim"][1]["type"] == "cut" and transitions["proof"][0]["type"] == "cut"
    assert transitions["open"][0] is None and transitions["end"][1] is None


def test_a_shot_generated_again_whose_take_still_plays_changes_no_scene():
    playing = _shot()
    before = _digests(_Db(playing))
    params: dict = {}
    clips.keep_take(params, playing)
    regenerating = _shot(status=JOB_RUNNING, params=params, finished_at=None)
    assert _digests(_Db(regenerating)) == before
    # Its attempt failed beside it: still the same take on screen.
    ended = dict(params)
    clips.end_attempt(ended, status="failed", reason="camera moved")
    assert _digests(_Db(_shot(params=ended, finished_at=datetime(2026, 10, 21, tzinfo=UTC)))) == before
    # A new take: the scenes showing it and their neighbours.
    new_take = _digests(_Db(_shot(file_id=uuid.uuid4())))
    assert sorted(s for s in before if before[s] != new_take[s]) == ["claim", "close", "end", "open", "proof", "shot"]


def test_the_scenes_changed_since_their_preview_are_the_ones_an_edit_touched():
    db = _Db()
    seen = _previewed(db, ["open", "shot", "claim", "proof", "close", "end"])
    changed = render_checks.changed_scenes(seen, _digests(db, _effective(lines="Two platforms")))
    assert changed == ["shot", "claim", "proof"]
    # A scene never drawn is never "changed"; a drawing of an older digest version is no drawing.
    other = render_checks.record_seen(None, {"claim": "x"}, uuid.uuid4(), checks={"findings": []})
    assert render_checks.changed_scenes(other, _digests(db)) == ["claim"]
    legacy = {"claim": {"digest": "x", "render_id": "r", "at": "t"}}
    assert render_checks.changed_scenes(legacy, _digests(db)) == []
    assert render_checks.passed_scenes(legacy, _digests(db)) == set()


def test_an_edit_previewed_then_reverted_passes_the_gate_without_force(world):
    """Acceptance: trim_end 3.6 -> 3.2 -> 3.6, the edited scene previewed in between."""
    db = _Db()
    world.findings = [_camera_moved()]
    original = _effective(trim_end=3.6)
    seen = _previewed(db, ["open", "shot", "claim", "proof", "close", "end"], effective=original)
    edited = _effective(trim_end=3.2)
    assert render_checks.changed_scenes(seen, _digests(db, edited)) == ["open", "shot", "claim"]
    seen = render_checks.record_seen(seen, {"shot": _digests(db, edited)["shot"]}, uuid.uuid4(), checks={"findings": []})
    # The revert finds the drawing of what the scene shows again: nothing changed, the shot's finding cleared.
    digests = _digests(db, original)
    assert render_checks.changed_scenes(seen, digests) == []
    assert render_checks.restored_scenes(seen, digests) == ["shot"]
    assert render_checks.passed_scenes(seen, digests) == {"open", "shot", "claim", "proof", "close", "end"}
    reasons = asyncio.run(render_checks.gate(db, _project(seen), USER, original, quality="high"))
    assert reasons == []


def test_a_scene_digest_reads_the_shared_inputs_it_depends_on():
    db = _Db()
    before = _digests(db)

    def changed(**fields) -> list[str]:
        after = _digests(db, validate_video_spec({**_effective(), **fields}))
        return [s for s in before if before[s] != after[s]]

    # No scene names its own transition: the video's default is every scene's.
    assert changed(default_transition={"type": "cut"}) == ["open", "shot", "claim", "proof", "close", "end"]
    # The clips' own sound: the clip scene and its neighbours.
    assert changed(clip_sound="off") == ["open", "shot", "claim"]
    # The video's fades: its first scene, its last, and theirs.
    assert changed(fade_in=False) == ["open", "shot"]
    assert changed(fade_out=False) == ["close", "end"]
    # A scene naming its own transition does not read the default: neither it nor its neighbours, all named, change.
    # `claim` neither: its preview draws `shot`'s edge and the transition into `claim`, never `shot`'s own
    # transition in; `close` does, its preview drawing the transition into `end`, which reads the default.
    own = _effective()
    for index in (2, 3, 4):
        own["scenes"][index]["transition"] = {"type": "crossfade"}
    named = _digests(db, validate_video_spec(own))
    cut = _digests(db, validate_video_spec({**own, "default_transition": {"type": "cut"}}))
    assert [s for s in named if named[s] != cut[s]] == ["open", "shot", "close", "end"]


def test_a_scene_keeps_its_last_drawings_by_digest():
    seen = None
    for index in range(render_checks.DRAWINGS_KEPT + 2):
        seen = render_checks.record_seen(seen, {"s": f"d{index}"}, f"r{index}", checks={"findings": []})
    drawings = seen["s"]["drawings"]
    assert [d["digest"] for d in drawings] == [f"d{i}" for i in range(render_checks.DRAWINGS_KEPT + 1, 1, -1)]
    assert seen["s"]["digest"] == drawings[0]["digest"] and seen["s"]["version"] == render_checks.DIGEST_VERSION
    # A digest drawn again replaces its older drawing and comes first; one that failed no longer passes.
    failed = {"findings": [{"kind": "stutter", "severity": "fail", "at": 1.0, "scene_id": "s"}]}
    seen = render_checks.record_seen(seen, {"s": "d5"}, "again", checks=failed)
    assert [d["digest"] for d in seen["s"]["drawings"]].count("d5") == 1 and seen["s"]["drawings"][0]["render_id"] == "again"
    assert render_checks.passed_scenes(seen, {"s": "d5"}) == set() and render_checks.passed_scenes(seen, {"s": "d6"}) == {"s"}
    assert render_checks.drawing_of(seen, "s", "d0") is None and render_checks.changed_scenes(seen, {"s": "d0"}) == ["s"]


# ── An edit that changes nothing ──────────────────────────────────────────


def test_a_save_byte_identical_to_the_stored_spec_writes_nothing(monkeypatch):
    from app.services.video_studio import music as music_tracks
    from app.services.video_studio import templates
    from app.services.video_studio.spec import authored_spec

    async def none(*_args, **_kwargs):
        return None

    async def nothing_started(*_args, **_kwargs):
        return 0, []

    async def no_problem(*_args, **_kwargs):
        return []

    async def planned(*_args, **_kwargs):
        return SimpleNamespace(duration=21.0, document={"scenes": []})

    monkeypatch.setattr(projects, "verify_references", no_problem)
    monkeypatch.setattr(templates, "resolve_for_spec", none)
    monkeypatch.setattr(projects, "estimate", planned)
    monkeypatch.setattr(projects, "start_animations", nothing_started)
    monkeypatch.setattr(music_tracks, "start_for_spec", nothing_started)
    stored = authored_spec(_effective(trim_end=3.6))
    project = SimpleNamespace(id=uuid.uuid4(), spec=stored, template_container_id=None, revision=7, scene_previews={"shot": {}})

    class _Unwritten:
        """A session nothing may be written to."""

        async def execute(self, _statement):
            # What a save that changes the video reads first: its renders in progress it may carry - none.
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

        async def refresh(self, _row, **_kwargs):
            # The video's row locked before its next revision is numbered: nobody saved it meanwhile.
            return None

        def add(self, _row):
            raise AssertionError("a save that changes nothing adds no row")

        async def flush(self):
            raise AssertionError("a save that changes nothing flushes nothing")

    # The same spec, its keys in another order.
    raw = {key: stored[key] for key in reversed(list(stored))}
    saved = asyncio.run(projects.save_spec(_Unwritten(), user=USER, raw=raw, project=project))
    assert saved.unchanged and saved.changed_since_preview == [] and project.revision == 7 and project.spec is stored
    # Another template is another video, whatever the spec says.
    project.template_container_id = uuid.uuid4()
    with pytest.raises(AssertionError, match="flushes nothing"):
        asyncio.run(projects.save_spec(_Unwritten(), user=USER, raw=raw, project=project))
    assert projects.same_spec(stored, authored_spec(raw)) and not projects.same_spec(stored, authored_spec(_effective()))


def test_a_drawing_records_whether_its_scene_passed():
    checks = {"findings": [{"kind": "frozen", "severity": "warn", "at": 2.0, "scene_id": "shot"}]}
    seen = render_checks.record_seen(None, {"open": "d1", "shot": "d2"}, "r1", checks=checks)
    assert seen["open"]["passed"] is True and seen["shot"]["passed"] is False
    assert seen["open"]["version"] == render_checks.DIGEST_VERSION
    # A failing finding placed on no scene fails every scene the render drew.
    unplaced = {"findings": [{"kind": "transcript", "severity": "fail", "at": 0.0, "scene_id": None}]}
    assert render_checks.record_seen(None, {"open": "d1"}, "r2", checks=unplaced)["open"]["passed"] is False
