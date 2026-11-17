"""A screen's flatness is judged only where a screen insert lands, on a screen, never on perspective.

Synthetic pictures only - a white cylinder with a printed mark, a teal mug, a glowing panel, a dark screen seen at
35 degrees, a screen whose edges bow 8%. No browser, no storage, no provider:

* the start picture's screen is measured only for a shot that is, or is about to be, the footage under a screen
  insert: a screen replacement, ``insert: true``, or a shot a video's scene draws a ``screen_insert`` on; any other
  shot validates, its notes saying nothing was checked (``clips.lint_surface``);
* ``insert: false`` checks no screen on the start picture or the take and drops ``stray_screens``;
* a candidate is four straight edges around a key-coloured or dark fill that is not the picture's brightest region
  (``screens.find_screen``): a mug, a lamp is no screen;
* a flat screen seen at an angle is flat, its angle said as a fact; an edge bowing past 5%, its points confirming it,
  is a finding.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.services.video_studio import clips, insert_bezels, screens, spec, surface_refine
from app.services.video_studio import stray_screens as stray

pytestmark = pytest.mark.usefixtures("no_default_video_template")

USER = SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-00000000b280"), role="user")
START = "00000000-0000-0000-0000-0000000000f2"
CLIP = "00000000-0000-0000-0000-00000000c280"
SIZE = (1280, 720)
TEAL = (0, 150, 130)


# ── Pictures ──────────────────────────────────────────────────────────────


def _cylinder() -> Image.Image:
    """A white cylinder seen from a little above, a blue mark printed around it: the mark's edges follow its curve."""
    image = Image.new("RGB", SIZE, (110, 104, 98))
    draw = ImageDraw.Draw(image)
    middle, half, minor = 640, 150, 40
    draw.rectangle((middle - half, 220, middle + half, 560), fill=(236, 236, 232))
    draw.ellipse((middle - half, 560 - minor, middle + half, 560 + minor), fill=(236, 236, 232))
    draw.ellipse((middle - half, 220 - minor, middle + half, 220 + minor), fill=(250, 250, 248))
    xs = np.linspace(middle - 120, middle + 120, 80)

    def around(y: float) -> list[tuple[float, float]]:
        return [(float(x), y + minor * math.sqrt(max(0.0, 1 - ((x - middle) / half) ** 2))) for x in xs]

    draw.polygon(around(300) + around(420)[::-1], fill=(30, 60, 200))
    return image


def _bowed(bow: float, colour=(0, 177, 64), *, top_only: bool = False) -> Image.Image:
    """A screen 640 px wide whose top (and bottom) edge bows *bow* of its length, on a grey wall."""
    image = Image.new("RGB", SIZE, (120, 120, 120))
    sag = bow * 640
    top = [(320 + t * 640, 170 + sag * (1 - (2 * t - 1) ** 2)) for t in np.linspace(0, 1, 80)]
    bottom = (
        [(960.0, 550.0), (320.0, 550.0)]
        if top_only
        else [(320 + t * 640, 550 - sag * (1 - (2 * t - 1) ** 2)) for t in np.linspace(1, 0, 80)]
    )
    ImageDraw.Draw(image).polygon(top + bottom, fill=colour)
    return image


def _turned(degrees: float, *, fill=(16, 16, 20), wall=(150, 146, 140)) -> tuple[Image.Image, list[tuple[float, float]]]:
    """A flat 16:9 screen turned *degrees* about the vertical, in the middle of the frame, seen through the lens
    ``surface_refine`` assumes: the picture and its corners (pixels)."""
    focal = surface_refine.ASSUMED_FOCAL * SIZE[0]
    turn = math.radians(degrees)
    corners = []
    for x, y in ((-0.8, -0.45), (0.8, -0.45), (0.8, 0.45), (-0.8, 0.45)):
        depth = 3.0 + x * math.sin(turn)
        corners.append((SIZE[0] / 2 + focal * x * math.cos(turn) / depth, SIZE[1] / 2 + focal * y / depth))
    image = Image.new("RGB", SIZE, wall)
    ImageDraw.Draw(image).polygon(corners, fill=fill)
    return image, corners


def _lamp(*, window: bool = False, core: bool = False) -> Image.Image:
    """A flat light panel glowing green in a dim room - the brightest thing there unless a window is brighter; with
    *core*, a green frame around a white glow."""
    image = Image.new("RGB", SIZE, (70, 70, 74))
    draw = ImageDraw.Draw(image)
    if core:
        draw.rectangle((420, 200, 860, 470), fill=(0, 177, 64))
        draw.rectangle((450, 230, 830, 440), fill=(250, 250, 245))
    else:
        draw.rectangle((420, 200, 860, 470), fill=(90, 255, 140))
    if window:
        draw.rectangle((960, 60, 1220, 300), fill=(248, 248, 248))
    return image


# ── Planning a shot, nothing paid ─────────────────────────────────────────


@pytest.fixture
def planned(monkeypatch):
    """``clips.plan_clip`` from a start picture the test shows (``shown["picture"]``), its provider faked."""
    from app.llm.openai_provider import OpenAIProvider
    from app.services.video_studio import pictures

    shown: dict = {}

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return OpenAIProvider.__new__(OpenAIProvider), model or "sora-2", SimpleNamespace(id=uuid.uuid4(), provider="openai")

    async def reference(*_args, **_kwargs):
        return {"reference_file_id": START}

    async def read_picture(_db, _user, _file_id, *, what):
        return shown["picture"]

    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(clips, "resolve_reference", reference)
    monkeypatch.setattr(pictures, "read_picture", read_picture)

    def plan(picture: Image.Image, **asked):
        shown["picture"] = picture
        return asyncio.run(
            clips.plan_clip(None, USER, prompt="A desk by the window", reference_file_id=START, freestyle=True, **asked)
        )

    return plan


def _about_flatness(notes: list[str]) -> list[str]:
    return [note for note in notes if any(word in note for word in ("flat", "bow", "curved", "convex"))]


def test_a_shot_with_no_screen_insert_validates_on_a_white_cylinder_with_a_printed_mark(planned):
    """(a) The mark printed on the cylinder curves with it: measured, its edge bows past 5% - the lint refused the
    generation before. No insert is drawn on the shot, so nothing is measured."""
    measured = surface_refine.measure_bezel(np.asarray(_cylinder()))
    assert measured is not None and measured.flatness.bows[0] > surface_refine.FLAT_MAX_SHARE
    plan = planned(_cylinder())
    assert clips.SURFACE not in plan.params and clips.INSERT not in plan.params
    assert plan.notes == [f"{clips.NOT_MEASURED}."] and _about_flatness(plan.notes) == []
    assert plan.summary()["insert"] is None and plan.summary()["surface"] is None


def test_a_flat_dark_screen_at_35_degrees_validates_and_says_its_angle_as_a_fact(monkeypatch, planned):
    """(b) Its opposite sides differ, its edges are straight: the homography covers it exactly."""
    from app.services.video_studio import pictures

    # The screen keyed green, asked as an insert's footage: found, validates, its angle said.
    keyed, _corners = _turned(35.0, fill=(0, 177, 64))
    plan = planned(keyed, insert=True)
    assert plan.params[clips.INSERT] is True and plan.params[clips.SURFACE]["flatness"]["flat"] is True
    assert any("seen at 35 degrees" in note for note in plan.notes)
    picture, corners = _turned(35.0)

    async def read_picture(_db, _user, _file_id, *, what):
        return picture

    monkeypatch.setattr(pictures, "read_picture", read_picture)
    # A screen replacement given its quad: the dark screen is measured against it.
    screen = {"quad": [list(corner) for corner in corners], "start_size": list(SIZE)}
    surface, notes = asyncio.run(clips.lint_surface(None, USER, {"reference_file_id": START}, screen, insert=None))
    shape = surface_refine.Flatness.from_dict(surface["flatness"])
    assert shape.flat and shape.fault() is None and 34.0 <= shape.degrees <= 36.0
    assert notes == ["The start picture's screen is seen at 35 degrees: a fact, not a fault - flat, the insert is drawn onto it exactly."]
    # Saved as a screen insert on it: the angle is a note, never a finding.
    given = tuple((x / SIZE[0], y / SIZE[1]) for x, y in corners)
    bezel = surface_refine.measure_bezel(np.asarray(picture), given=given)
    item = insert_bezels.Measured("s1", 0, ("clip", CLIP), "file-1", 12, 0.5, given, bezel, "digest")
    authored = {
        "title": "Booth",
        "freestyle": True,
        "scenes": [{"id": "s1", "type": "clip", "clip_id": CLIP, "overlays": [{"type": "screen_insert", "quad": [list(p) for p in given]}]}],
    }
    resolved = insert_bezels.resolve(authored, [item])
    assert not resolved.problems and any("is seen at 35 degrees" in note for note in resolved.notes)
    assert insert_bezels.findings([item]) == []


def test_a_screen_whose_edges_bow_8_percent_is_a_finding(planned):
    """(c) A bow is what no planar insert covers: past 5% of the edge, its points confirming it, the lint says so."""
    bezel = surface_refine.measure_bezel(np.asarray(_bowed(0.08, top_only=True)))
    assert bezel is not None and not bezel.flatness.flat
    assert bezel.flatness.fault() == "its top edge bows 8% of its length, past 5%"
    with pytest.raises(clips.ClipError, match=r"the surface is curved \(its top edge bows 8% of its length, past 5%\)"):
        planned(_bowed(0.08, top_only=True), insert=True)
    item = insert_bezels.Measured("s1", 0, ("clip", CLIP), "file-1", 12, 0.5, None, bezel, "digest")
    [(scene, kind, words, shot)] = insert_bezels.findings([item])
    assert (scene, kind, shot) == ("s1", "insert_flatness", CLIP) and "bows 8%" in words
    # A bow of 4% is under the gate, whatever its points say.
    assert surface_refine.measure_bezel(np.asarray(_bowed(0.04))).flatness.flat


def test_insert_false_validates_the_bowed_screen_and_drops_stray_screens(planned):
    """(d) The shot says it carries no insert: nothing is measured, the bow nowhere said."""
    plan = planned(_bowed(0.05), insert=False)
    assert plan.params[clips.INSERT] is False and clips.SURFACE not in plan.params
    assert plan.notes == [f"{clips.INSERT_OFF}."] and plan.summary()["insert"] is False
    # Its stray screens are not cleaned either: dropped, said.
    plan = planned(_bowed(0.05), insert=False, stray_screens="flatten", negative_prompt="screens")
    assert stray.KEY not in plan.params
    assert "stray_screens dropped: insert: false says the shot carries no screen to clean." in plan.notes
    # A screen replacement is a screen insert: asking both is refused before anything is paid.
    with pytest.raises(clips.ClipError, match="insert: false says the shot carries no screen insert, and screen"):
        planned(_bowed(0.05), insert=False, screen={"file_id": START})
    with pytest.raises(clips.ClipError, match="insert is true"):
        planned(_bowed(0.05), insert="none")


# ── The candidate is a screen ─────────────────────────────────────────────


def test_a_rounded_or_glowing_object_of_a_key_hue_is_no_screen():
    # A teal mug seen from a little above: its rim bows 15% of its width - no straight edge.
    with pytest.raises(screens.ScreenError, match=r"one of its edges curves \(straightness 0\.95, under 0\.97\)"):
        screens.find_screen(_bowed(0.15, TEAL, top_only=True))
    assert surface_refine.measure_bezel(np.asarray(_bowed(0.15, TEAL, top_only=True))) is None
    # A light panel glowing green, the brightest thing in a dim room: a lamp, no screen ...
    with pytest.raises(screens.ScreenError, match="its fill is the picture's brightest region"):
        screens.find_screen(_lamp())
    # ... unless a window outshines it.
    assert screens.find_screen(_lamp(window=True))
    # A green frame around a white glow: the fill is the glow, neither a key colour nor dark.
    with pytest.raises(screens.ScreenError, match="its fill is neither a key colour nor dark"):
        screens.find_screen(_lamp(core=True))
    # A screen bowing 5% stays a screen (its straightness 0.99): the flatness lint is what says it bows.
    assert screens.find_screen(_bowed(0.05))


def test_an_edge_is_as_straight_as_the_curve_its_points_follow():
    corners = [(320.0, 170.0), (960.0, 170.0), (960.0, 550.0), (320.0, 550.0)]
    xs = np.arange(320.0, 961.0)
    ys = np.arange(170.0, 551.0)
    straight = np.concatenate(
        [np.column_stack([xs, np.full_like(xs, 170.0)]), np.column_stack([np.full_like(ys, 960.0), ys]),
         np.column_stack([xs, np.full_like(xs, 550.0)]), np.column_stack([np.full_like(ys, 320.0), ys])]
    )
    assert screens.edge_straightness(straight, corners) == (1.0, 1.0, 1.0, 1.0)
    # The top edge on an arc of 12% of its length: a chord over a longer curve.
    sag = 0.12 * 640
    arched = straight.copy()
    top = arched[:, 1] == 170.0
    arched[top, 1] = 170.0 - sag * (1 - (2 * (arched[top, 0] - 320.0) / 640.0 - 1) ** 2)
    curved, *rest = screens.edge_straightness(arched, corners)
    assert curved is not None and curved < screens.MIN_EDGE_STRAIGHTNESS and rest == [1.0, 1.0, 1.0]


# ── Perspective is a fact, a bow or a fold a fault ────────────────────────


def test_the_angle_is_read_off_the_quad_and_never_fails_it():
    for degrees in (0.0, 20.0, 35.0, 50.0):
        _picture, corners = _turned(degrees)
        assert surface_refine.viewing_angle(corners, SIZE) == pytest.approx(degrees, abs=0.2)
        shape = surface_refine.flatness(corners, size=SIZE)
        assert shape.flat and shape.fault() is None
    assert surface_refine.flatness(_turned(5.0)[1], size=SIZE).seen_at() is None
    folded = surface_refine.flatness([(320, 170), (960, 170), (640, 300), (320, 550)], size=SIZE)
    assert not folded.flat and folded.fault() == "its corners fold into a quad that is not convex"
    # An older record, sides 50% apart and no bow, no degrees read: flat, by design, said by its sides.
    old = surface_refine.Flatness.from_dict({"sides": [645.0, 280.0, 645.0, 420.0], "bows": [0.0, 0.0, 0.0, 0.0], "flat": False})
    assert old.flat and old.degrees is None and old.seen_at() == "seen at an angle: left side 50% taller than right"


# ── Whose screen is measured ──────────────────────────────────────────────


class _Videos:
    """A session answering the one query of ``clips.drawn_under_insert``: the videos naming the shot, and whose."""

    def __init__(self, *specs: dict, owner: object = USER.id) -> None:
        self.specs = specs
        self.owner = owner
        self.asked = 0

    async def execute(self, _statement):
        self.asked += 1
        return SimpleNamespace(all=lambda: [("Booth", self.owner, stored) for stored in self.specs])


def _scene(*, inserted: bool, kind: str = "clip") -> dict:
    overlays = [{"type": "screen_insert", "source": {"file_id": START}}] if inserted else [{"type": "image", "file_id": START}]
    if kind == "statement":
        return {"id": "s1", "type": "statement", "lines": ["Live"], "backdrop": {"clip_id": CLIP}, "overlays": overlays}
    return {"id": "s1", "type": "clip", "clip_id": CLIP, "overlays": overlays}


def test_a_shot_generated_again_is_checked_only_where_a_scene_draws_an_insert_on_it(monkeypatch):
    from app.services.video_studio import pictures

    shown = {"picture": _bowed(0.08)}

    async def read_picture(_db, _user, _file_id, *, what):
        return shown["picture"]

    monkeypatch.setattr(pictures, "read_picture", read_picture)
    reference = {"reference_file_id": START}

    def lint(db):
        return asyncio.run(clips.lint_surface(db, USER, reference, None, insert=None, clip_id=CLIP))

    with pytest.raises(clips.ClipError, match=r"past 5%\) - a planar insert cannot cover it \(scene `s1` of the video `Booth` draws a screen insert on this shot\)"):
        lint(_Videos({"scenes": [_scene(inserted=True)]}))
    # Another person's video using the shot decides it too, its title not said.
    with pytest.raises(clips.ClipError, match=r"\(scene `s1` of a video draws a screen insert on this shot\)"):
        lint(_Videos({"scenes": [_scene(inserted=True)]}, owner=uuid.uuid4()))
    assert lint(_Videos({"scenes": [_scene(inserted=False)]})) == (None, [f"{clips.NOT_MEASURED}."])
    # No screen on the picture: no video is read, nothing is said.
    shown["picture"] = Image.new("RGB", SIZE, (120, 120, 120))
    videos = _Videos({"scenes": [_scene(inserted=True)]})
    assert lint(videos) == (None, []) and videos.asked == 0
    # A statement's backdrop shot under an insert is footage too.
    assert spec.insert_footage({"scenes": [_scene(inserted=True, kind="statement")]}) == {CLIP: "s1"}
    assert spec.insert_footage({"scenes": [_scene(inserted=False)]}) == {}


def test_only_a_shot_carrying_a_screen_insert_has_its_take_measured():
    assert not clips.carries_insert({})
    assert not clips.carries_insert({"reference_file_id": START})
    assert clips.carries_insert({clips.INSERT: True})
    assert clips.carries_insert({clips.SCREEN: {"file_id": START}})
    assert clips.carries_insert({clips.SURFACE: {"quad": []}})
    assert not clips.carries_insert({clips.INSERT: False, clips.SURFACE: {"quad": []}})
