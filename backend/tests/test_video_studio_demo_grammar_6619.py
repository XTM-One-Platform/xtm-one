"""A demo told as a product manager tells it, on fixtures: no browser, no database.

* the narration: a UI action said as an instruction, a count of the data on
  screen and a label its frames show are each said by the quality check,
  naming the words; a narration of the value and the mechanism says nothing;
* the camera: an automatic camera making more than 4 moves a minute, or
  moving in on an element that reads at full frame;
* the speed: a recording slowed under 0.7x to last as long as its narration,
  with the narration that keeps it at 0.8x;
* the Filigran Product Video template: a still camera, calm crossfades, 3 s
  callouts, its opening card centred or left as the spec says;
* a demo written the skill's way, on that template, saves with no advisory of
  its grammar.
"""

from __future__ import annotations

import uuid
from typing import get_args

import pytest

from app.services.video_studio import demo_grammar, recording_fit, templates, timeline
from app.services.video_studio.quality import ContentFacts, content_findings
from app.services.video_studio.spec import TakeStep, VideoSpecError, authored_spec, parse_video_spec

TAKE = "00000000-0000-0000-0000-000000006619"
DEMO_KINDS = {"demo_narration", "demo_camera", "demo_speed"}


def _clicks_take(take_id: str, seconds: float, every: float, *, box: tuple[int, int] = (60, 24)) -> timeline.TakeMedia:
    """A take clicking a small element every *every* seconds for *seconds*, across the page."""
    events = []
    t, step = every, 0
    while t < seconds - 0.5:
        x, y = 120 + (step * 170) % 1300, 140 + (step * 90) % 600
        events.append(
            {
                "step": step,
                "action": "click",
                "t0": round(t - 0.4, 3),
                "ta": round(t, 3),
                "t1": round(t + 0.1, 3),
                "ok": True,
                "box": {"x": x, "y": y, "w": box[0], "h": box[1]},
                "point": {"x": x + box[0] / 2, "y": y + box[1] / 2},
            }
        )
        t, step = t + every, step + 1
    return timeline.TakeMedia(take_id, seconds, 1600, 900, 2.0, 60, int(seconds * 60), events=events)


def _findings(
    authored: dict, takes: dict, *, voices: dict | None = None, frame_text: dict | None = None, template=None
):
    """The quality check's findings of *authored* - its recordings fitted to their narration as a save fits them - and
    the plan they read."""
    effective = templates.apply_template(authored, template).spec
    spec = parse_video_spec(effective)
    plan = timeline.estimate_plan(spec, takes, voices=voices)
    fitted = recording_fit.fits(spec, plan.document, takes, plan.narration_spans)
    if fitted:
        spec = parse_video_spec(recording_fit.apply(effective, fitted))
        plan = timeline.estimate_plan(spec, takes, voices=voices)
    facts = ContentFacts(
        takes=takes,
        ready_takes=set(takes),
        narration=dict(plan.narration_spans),
        frame_text=dict(frame_text or {}),
        template=template,
    )
    return content_findings(spec, plan.document, facts, authored), plan


def _filigran() -> templates.VideoTemplate:
    from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS

    [entry] = [c for c in BUILTIN_TEMPLATE_CONTAINERS if c["slug"] == "filigran-video"]
    style = templates.VideoTemplateStyle.model_validate(entry["extra_metadata"]["video"])
    return templates.VideoTemplate(id=str(uuid.uuid4()), name=entry["name"], kind="video", style=style)


# ── 1. The narration ──────────────────────────────────────────────────────


def test_a_narration_reading_its_screen_is_said_three_times_naming_the_words():
    authored = authored_spec(
        {
            "title": "Graph",
            "freestyle": True,
            "scenes": [
                {
                    "id": "graph",
                    "type": "recording",
                    "take_id": TAKE,
                    "camera": {"mode": "static"},
                    "narration": "Click the layered button: 23 entities, 78 relationships.",
                }
            ],
        }
    )
    found, _plan = _findings(authored, {TAKE: _clicks_take(TAKE, 8.0, 1.0)})
    said = [f for f in found if f.kind == "demo_narration"]

    assert [(f.scene_id, f.scene_index, f.severity) for f in said] == [("graph", 0, "advisory")] * 3
    assert '"Click the layered button"' in said[0].message and "the action on screen" in said[0].message
    assert '"23 entities"' in said[1].message and '"78 relationships"' in said[2].message
    assert all("count off the screen" in f.message for f in said[1:])


@pytest.mark.parametrize(
    ("narration", "said"),
    [
        ("Click the layered button.", ["Click the layered button"]),
        ("Now open the graph, then press Enter.", ["open the graph", "press Enter"]),
        ("Select a node and drag it into place.", ["Select a node", "drag it into place"]),
        ("Scroll down to the timeline.", ["Scroll down to the timeline"]),
        ("Open source, community led and free to run.", []),
        ("Any entity is one click away.", []),
        ("In one click, the whole campaign reads as a story.", []),
        ("Type and severity sit side by side.", []),
        ("The clicked node becomes the centre of the story.", []),
    ],
)
def test_an_instruction_is_a_ui_verb_opening_a_clause_and_acting_on_something(narration, said):
    assert demo_grammar.instructions(narration) == said


def test_the_spoken_actions_are_actions_a_take_plays():
    actions = set(get_args(TakeStep.model_fields["action"].annotation))
    assert set(demo_grammar.SPOKEN_ACTIONS) <= actions


def test_a_label_its_frame_shows_is_said_and_the_brand_and_the_title_are_not():
    narration = "The Threat Actor view ties each campaign to the people behind it, and XTM One keeps it current."
    frame = "Threat Actor APT28 Campaigns Reports XTM One"
    assert demo_grammar.labels_on_frame(narration, frame, known=["XTM One"]) == ["Threat Actor"]
    assert demo_grammar.labels_on_frame(narration, "Campaigns Reports", known=["XTM One"]) == []
    assert demo_grammar.labels_on_frame("Every link keeps its evidence.", frame) == []

    authored = authored_spec(
        {
            "title": "XTM One threat stories",
            "freestyle": True,
            "scenes": [
                {
                    "id": "story",
                    "type": "recording",
                    "take_id": TAKE,
                    "camera": {"mode": "static"},
                    "narration": narration,
                }
            ],
        }
    )
    found, _plan = _findings(authored, {TAKE: _clicks_take(TAKE, 10.0, 1.0)}, frame_text={"story": frame})
    [label] = [f for f in found if f.kind == "demo_narration"]
    assert '"Threat Actor"' in label.message and "label its frame shows" in label.message


def test_only_a_narration_naming_something_has_its_frames_read():
    take = _clicks_take(TAKE, 10.0, 1.0)
    raw = {
        "title": "Story",
        "freestyle": True,
        "scenes": [
            {
                "id": "named",
                "type": "recording",
                "take_id": TAKE,
                "narration": "The Threat Actor view ties it together.",
            },
            {"id": "plain", "type": "recording", "take_id": TAKE, "narration": "Every link keeps its evidence."},
        ],
    }
    spec = parse_video_spec(templates.apply_template(authored_spec(raw), None).spec)
    plan = timeline.estimate_plan(spec, {TAKE: take})
    moments = demo_grammar.frame_moments(spec, plan.document, plan.narration_spans)

    assert set(moments) == {"named"}
    seconds = [at for take_id, at in moments["named"] if take_id == TAKE]
    assert len(seconds) == len(demo_grammar.FRAME_MOMENTS) and seconds == sorted(seconds)
    assert all(0.0 <= at <= take.duration for at in seconds)
    assert demo_grammar.frame_moments(spec, plan.document, plan.narration_spans, known=["Threat Actor"]) == {}


async def test_no_frame_is_read_when_no_narration_names_anything():
    raw = {
        "title": "Story",
        "freestyle": True,
        "scenes": [
            {"id": "plain", "type": "recording", "take_id": TAKE, "narration": "Every link keeps its evidence."}
        ],
    }
    spec = parse_video_spec(templates.apply_template(authored_spec(raw), None).spec)
    plan = timeline.estimate_plan(spec, {TAKE: _clicks_take(TAKE, 6.0, 1.0)})
    assert await demo_grammar.frame_texts(uuid.uuid4(), spec, plan.document, plan.narration_spans) == {}


# ── 2. The camera ─────────────────────────────────────────────────────────


def _tour(camera: dict) -> dict:
    return authored_spec(
        {
            "title": "Tour",
            "freestyle": True,
            "scenes": [{"id": "tour", "type": "recording", "take_id": TAKE, "idle": {"mode": "off"}, "camera": camera}],
        }
    )


def test_an_automatic_camera_making_9_moves_in_a_minute_is_said():
    take = _clicks_take(TAKE, 60.0, 6.0)
    assert len(take.events) == 9
    found, plan = _findings(_tour({"mode": "auto"}), {TAKE: take})
    [planned] = plan.document["scenes"]
    _frames, moves = timeline.recording_camera(
        parse_video_spec(_tour({"mode": "auto"})).scenes[0], take, planned["time_map"], 0.0
    )

    assert len(moves) == 9 and [move.step for move in moves] == list(range(9))
    [said] = [f for f in found if f.kind == "demo_camera"]
    assert said.severity == "advisory" and "makes 9 moves in 60.0 s" in said.message
    assert "camera.mode static" in said.message
    # Still, or within 4 a minute, it says nothing.
    assert not [f for f in _findings(_tour({"mode": "static"}), {TAKE: take})[0] if f.kind == "demo_camera"]
    sparse = _clicks_take(TAKE, 60.0, 13.0)
    assert not [f for f in _findings(_tour({"mode": "auto"}), {TAKE: sparse})[0] if f.kind == "demo_camera"]


def test_a_move_in_on_an_element_covering_most_of_the_page_is_said():
    events = [
        {"step": 0, "action": "wait", "t0": 0.0, "ta": 0.0, "t1": 3.0, "ok": True},
        {
            "step": 1,
            "action": "highlight",
            "t0": 3.5,
            "ta": 3.5,
            "t1": 6.5,
            "ok": True,
            "box": {"x": 272, "y": 153, "w": 1056, "h": 594},
        },
        {"step": 2, "action": "wait", "t0": 6.6, "ta": 6.6, "t1": 9.5, "ok": True},
    ]
    take = timeline.TakeMedia(TAKE, 10.0, 1600, 900, 2.0, 60, 600, events=events)
    found, _plan = _findings(_tour({"mode": "auto"}), {TAKE: take})

    [said] = [f for f in found if f.kind == "demo_camera"]
    assert "moves in on step 1 at" in said.message and "covers 44% of the page" in said.message


# ── 3. The footage's speed ────────────────────────────────────────────────


def test_a_6_s_recording_under_a_14_s_narration_is_said_at_0_43x_naming_8_s():
    take = _clicks_take(TAKE, 6.0, 1.0)
    narration = " ".join(["word"] * 34)
    authored = authored_spec(
        {
            "title": "Slow",
            "freestyle": True,
            "scenes": [
                {
                    "id": "slow",
                    "type": "recording",
                    "take_id": TAKE,
                    "idle": {"mode": "off"},
                    "camera": {"mode": "static"},
                    "narration": narration,
                }
            ],
        }
    )
    # Spoken 13.2 s after its 0.35 s lead-in, its tail after it: the narration holds the scene 14 s.
    found, plan = _findings(authored, {TAKE: take}, voices={"slow": 14.0 - 0.35 - timeline.NARRATION_TAIL})
    [planned] = plan.document["scenes"]
    assert planned["time_map"][-1][0] == pytest.approx(14.0, abs=0.01)

    [said] = [f for f in found if f.kind == "demo_speed"]
    assert said.severity == "advisory"
    assert "plays 6.0 s at 1x and its narration holds it 14.0 s, so it plays at 0.43x" in said.message
    assert "a narration holding it about 8 s" in said.message and "keeps it at 0.8x" in said.message


def test_a_recording_at_its_own_pace_or_slowed_a_little_says_nothing():
    take = _clicks_take(TAKE, 12.0, 1.0)
    authored = authored_spec(
        {
            "title": "Paced",
            "freestyle": True,
            "scenes": [
                {"id": "own", "type": "recording", "take_id": TAKE, "idle": {"mode": "off"}, "narration": "Short."},
                {"id": "near", "type": "recording", "take_id": TAKE, "idle": {"mode": "off"}, "narration": "Longer."},
            ],
        }
    )
    found, _plan = _findings(authored, {TAKE: take}, voices={"own": 5.0, "near": 14.0})
    assert not [f for f in found if f.kind == "demo_speed"]


# ── 4. The Filigran Product Video template ───────────────────────────────


def test_a_new_recording_on_the_filigran_template_saves_still_with_calm_crossfades_and_3_s_callouts():
    raw = {
        "title": "Demo",
        "scenes": [
            {
                "id": "r",
                "type": "recording",
                "take_id": TAKE,
                "overlays": [{"type": "callout", "text": "Radial layout", "at_step": 1}],
            }
        ],
    }
    effective = templates.apply_template(authored_spec(raw), _filigran()).spec
    intro, recording, _outro = effective["scenes"]

    assert recording["camera"] == {
        **recording["camera"],
        "mode": "static",
        "pace": "calm",
        "max_zoom": 1.3,
        "move_seconds": 2.0,
    }
    assert effective["pace"] == "calm"
    assert effective["default_transition"] == {**effective["default_transition"], "type": "crossfade", "pace": "calm"}
    assert recording["overlays"][0]["duration"] == 3.0
    assert intro["type"] == "title" and intro["align"] == "center"
    # The opening card the template adds, set left by the spec; the closing card is always centred.
    left = templates.apply_template(
        authored_spec({**raw, "template_scenes": {"intro": {"align": "left"}}}), _filigran()
    ).spec
    assert left["scenes"][0]["align"] == "left"
    with pytest.raises(VideoSpecError, match="always centred"):
        authored_spec({**raw, "template_scenes": {"outro": {"align": "left"}}})


def test_a_scene_asking_for_a_detail_moves_calmly_and_shallow_on_the_filigran_template():
    raw = {"title": "Demo", "scenes": [{"id": "r", "type": "recording", "take_id": TAKE, "camera": {"mode": "auto"}}]}
    [_intro, recording, _outro] = templates.apply_template(authored_spec(raw), _filigran()).spec["scenes"]
    assert recording["camera"] == {
        **recording["camera"],
        "mode": "auto",
        "pace": "calm",
        "max_zoom": 1.3,
        "move_seconds": 2.0,
    }


# ── 5. Done when ──────────────────────────────────────────────────────────


def test_a_demo_written_the_skill_way_on_the_filigran_template_has_no_advisory_of_its_grammar():
    takes = {
        f"00000000-0000-0000-0000-00000000661{i}": _clicks_take(f"00000000-0000-0000-0000-00000000661{i}", 12.0, 1.0)
        for i in range(3)
    }
    narrations = [
        "Any entity can now become the centre of the story, so an investigation starts where the analyst is.",
        "Relationships fan out by kind around it, and a campaign reads as a story instead of a tangle.",
        "Every hop keeps its evidence one hover away, which is what a briefing needs.",
    ]
    authored = authored_spec(
        {
            "title": "OpenCTI: the graph tells the story",
            "voiceover": {"style": "A product manager presenting live to peers: warm, confident, never salesy."},
            "scenes": [
                {
                    "id": f"s{i}",
                    "type": "recording",
                    "take_id": take_id,
                    "narration": narration,
                    "overlays": [{"type": "callout", "text": "Any entity at the centre", "at_step": 2}]
                    if i == 0
                    else [],
                }
                for i, (take_id, narration) in enumerate(zip(takes, narrations, strict=True))
            ],
        }
    )
    found, _plan = _findings(authored, takes, template=_filigran())
    assert [f for f in found if f.kind in DEMO_KINDS | {"pace"}] == []
