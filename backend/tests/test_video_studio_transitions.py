"""Transitions: ``auto`` - the default - cuts like an editor, from the
scenes on either side and the video's pace; a named one is used as named."""

import shutil
import subprocess

import pytest

from app.services.video_studio import compositor_page, spec, timeline

CLIP = "00000000-0000-0000-0000-00000000000c"
SHOT = timeline.ClipMedia(duration=8.0, width=1280, height=720, fps=24, frame_count=192)


def _montage(transition: dict | None = None, *, shot_seconds: float = 8.0) -> list[dict]:
    """A title, four AI shots, a statement, a closing card: the transition each scene comes in with."""
    raw: dict = {
        "title": "Cats",
        "voiceover": {"enabled": False},
        "scenes": [
            {"id": "open", "type": "title", "title": "Cats", "duration": 3.0},
            *[{"id": f"shot{i}", "type": "clip", "clip_id": CLIP, "trim_end": shot_seconds} for i in range(4)],
            {"id": "words", "type": "statement", "lines": ["Every cat is a *star*."], "duration": 3.0},
            {"id": "close", "type": "outro", "title": "Thanks", "duration": 3.0},
        ],
    }
    if transition is not None:
        raw["default_transition"] = transition
    parsed = spec.parse_video_spec(spec.validate_video_spec(raw))
    media = timeline.PlanMedia(clips={f"shot{i}": SHOT for i in range(4)})
    return [scene["transition_in"] for scene in timeline.plan(parsed, media, fps=30, theme={}).document["scenes"]]


def _kinds(transitions: list[dict]) -> list[str]:
    return [t["type"] for t in transitions]


def test_a_video_cuts_automatically_unless_told_otherwise():
    assert (
        spec.parse_video_spec(
            spec.validate_video_spec({"title": "x", "scenes": [{"type": "title", "title": "x"}]})
        ).default_transition.type
        == "auto"
    )


def test_a_calm_video_dissolves_its_shots_and_dips_to_black_before_the_end():
    transitions = _montage()
    assert _kinds(transitions) == ["cut", "zoom", "crossfade", "crossfade", "crossfade", "blur", "fade_black"]
    assert transitions[2]["duration"] == pytest.approx(0.7)
    assert transitions[-1]["duration"] == pytest.approx(0.8)


def test_a_dynamic_montage_alternates_its_push_and_whip():
    transitions = _montage({"type": "auto", "pace": "dynamic"})
    assert _kinds(transitions) == ["cut", "zoom", "zoom", "whip", "zoom", "flash", "fade_black"]
    assert transitions[3]["duration"] == pytest.approx(0.35)


def test_a_classic_video_only_cuts_crossfades_and_fades_to_black():
    """A brand film's grammar - every move of the calm grammar becomes a crossfade of its length."""
    transitions = _montage({"type": "auto", "pace": "classic"})
    assert _kinds(transitions) == ["cut", "crossfade", "crossfade", "crossfade", "crossfade", "crossfade", "fade_black"]
    assert set(_kinds(transitions)) <= timeline.CLASSIC_TRANSITIONS
    # The calm grammar's lengths: the title's zoom (0.7 s) and the blur into the statement (0.5 s).
    assert transitions[1]["duration"] == pytest.approx(0.7)
    assert transitions[5]["duration"] == pytest.approx(0.5)
    # A statement next to a recording - the cut the brand film's viewers rejected - is a crossfade too.
    previous = spec.parse_video_spec(
        spec.validate_video_spec({"title": "x", "scenes": [{"type": "statement", "lines": ["We see it all."]}]})
    ).scenes[0]
    recording = spec.RecordingScene(type="recording", take_id=CLIP)
    assert timeline.auto_transition(previous, recording, 4.0, "classic")[0] == "crossfade"
    assert timeline.auto_transition(recording, previous, 4.0, "classic")[0] == "crossfade"


def test_a_shot_too_short_to_dissolve_is_cut_from():
    transitions = _montage(shot_seconds=1.2)
    # The cuts out of the short shots, and into the statement after the last one.
    assert _kinds(transitions)[2:6] == ["cut", "cut", "cut", "cut"]


def test_a_named_transition_is_used_as_named_for_its_own_length_or_the_one_given():
    assert set(_kinds(_montage({"type": "wipe"}))[1:]) == {"wipe"}
    assert _montage({"type": "whip"})[2]["duration"] == pytest.approx(timeline.TRANSITION_SECONDS["whip"])
    assert _montage({"type": "auto", "duration": 1.0})[2]["duration"] == pytest.approx(1.0)


def test_the_built_in_templates_cut_automatically_at_their_pace():
    from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS

    paces = {
        entry["slug"]: entry["extra_metadata"]["video"]["defaults"]["default_transition"]
        for entry in BUILTIN_TEMPLATE_CONTAINERS
        if entry["kind"] == "video"
    }
    # The product video crossfades calmly between demo scenes; every other template cuts automatically.
    assert paces.pop("filigran-video") == {"type": "crossfade", "pace": "calm"}
    assert all(value["type"] == "auto" for value in paces.values())
    assert paces["filigran-event-loop"]["pace"] == "dynamic"
    # A brand film cuts, crossfades and fades to black, never a whip or a zoom.
    assert paces["filigran-brand-film"]["pace"] == paces["xtm-one-brand-film"]["pace"] == "classic"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_every_transition_is_drawn_and_leaves_no_trace_once_done(tmp_path):
    """Each type moves both layers mid-way, and the next scene is untouched once it is in."""
    source = compositor_page.PAGE_JS
    start = source.index("const clamp = ")
    helpers = source[start : source.index("const el = ", start)]
    function = source[source.index("function applyTransition(") :]
    function = function[: function.index("\n}\n") + 2]
    harness = """
const S = { W: 1920, H: 1080 };
const layer = () => ({ style: {} });
const out = {};
for (const type of JSON.parse(process.argv[2])) {
  const prev = { layer: layer() }, next = { layer: layer() };
  const sc = { start: 0, transition_in: { type, duration: 1 } };
  applyTransition(prev, next, sc, 0.5);
  const mid = JSON.stringify([prev.layer.style, next.layer.style]);
  applyTransition(prev, next, sc, 1);
  out[type] = { moved: mid !== JSON.stringify([prev.layer.style, next.layer.style]), done: next.layer.style };
}
console.log(JSON.stringify(out));
"""
    script = tmp_path / "transitions.js"
    script.write_text(helpers + function + harness, encoding="utf-8")
    kinds = [kind for kind in spec.TRANSITION_TYPES if kind not in ("auto", "cut")]
    import json

    result = subprocess.run(  # noqa: S603
        [shutil.which("node") or "node", str(script), json.dumps(kinds)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    drawn = json.loads(result.stdout)
    for kind in kinds:
        assert drawn[kind]["moved"], kind
        done = drawn[kind]["done"]
        assert (done["opacity"], done["transform"], done["filter"], done["maskImage"]) == (
            "1",
            "none",
            "none",
            "none",
        ), kind
