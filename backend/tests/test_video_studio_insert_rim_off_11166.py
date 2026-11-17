"""The grey the compositing draws over the insert past what stands in front, off the keyed surface, gated (#11166).

Retest of #7310: a keyed insert (green, tolerance 0.3, feather 1.5, ``despill.edge: false``) with an animal crossing the
screen previewed green while its frames showed a grey contour over the insert around the subject: the report said
``grey past what stands in front, not gated: 39.7% on the surroundings off the keyed surface``. The subject's fringe
the key reads clear keeps the key's colour, the clamp turns it grey, and that band lies off the keyed surface, which
#7314 left a fact to clear a grey console the footage itself shows.

* the band off the keyed surface the compositing made grey (the footage had colour there) fails ``insert_rim``, the
  finding naming the frame and the edge despill turned on;
* the same scene drawn clean stays green; the footage's own grey off the surface (#7310's console) stays a fact;
* the threshold is ``MAX_RIM_SHARE``, unchanged; the drawing version moved.
"""

from __future__ import annotations

import numpy as np

from app.services.video_studio import insert_checks, render_checks, screen_insert, screens

FPS = 24.0
KEY = "insert/demo-cat-walk.0"
LABEL = "Scene demo-cat-walk: its screen insert 0"
SIZE, SPLIT = 240, 120
GREEN = (32, 255, 30)
BLUE = (40, 120, 235)
GREY = (100, 100, 100)
#: The footage off the keyed surface: a wall with colour (never the key's hue), what the fringe lies over.
WALL = (190, 110, 60)
FUR = (58, 38, 28)
KEYED = screens.Key("green", GREEN)


def _cat(centre: tuple[int, int], radius: int = 34) -> np.ndarray:
    rows, columns = np.mgrid[0:SIZE, 0:SIZE]
    y, x = centre
    return (rows - y) ** 2 + (columns - x) ** 2 <= radius**2


def _frame(*, drawn_grey: float = 0.0, wall: tuple[int, int, int] = WALL) -> screen_insert.RimRead:
    """One frame: the keyed surface above :data:`SPLIT`, *wall* below it, the subject across the split; *drawn_grey* of
    the band off the keyed surface drawn grey by the compositing (the footage keeps *wall* there)."""
    cat = _cat((SPLIT + 20, 120))
    surface = np.zeros((SIZE, SIZE), dtype=bool)
    surface[:SPLIT] = True
    footage = np.empty((SIZE, SIZE, 3), dtype=np.float32)
    footage[surface], footage[~surface] = GREEN, wall
    footage[cat] = FUR
    matte = (surface & ~cat).astype(np.float32)
    inside = np.ones((SIZE, SIZE), dtype=bool)
    clear = screen_insert.clear_part(cat, inside, matte, 1.0)
    assert clear is not None
    composed = np.where(matte[..., None] > 0.5, np.array(BLUE, dtype=np.float32), footage)
    band = np.zeros((SIZE, SIZE), dtype=bool)
    band[clear.window] = (
        ~clear.part & (clear.distance > screen_insert.RIM_FROM_PX) & (clear.distance <= screen_insert.RIM_PX)
    )
    off_surface = np.argwhere(band & ~surface)
    if drawn_grey:
        chosen = off_surface[: round(drawn_grey * len(off_surface))]
        composed[chosen[:, 0], chosen[:, 1]] = GREY
    own = np.broadcast_to(np.array(BLUE, dtype=np.float32), (SIZE, SIZE, 3))
    return screen_insert._rim_share(composed, own, own, matte, clear, inside, KEYED, 1.0, footage=footage, matte=matte)


def _read(frames: dict, despill: dict | None) -> tuple[dict, list]:
    insert = screen_insert.InsertSource(
        "panel.png",
        "image",
        ((0.3, 0.2), (0.7, 0.2), (0.7, 0.75), (0.3, 0.75)),
        key=screen_insert.KeySpec("green"),
        footage=screen_insert.Footage("clip", "video", FPS, "clip/demo-cat-walk"),
        label=LABEL,
    )
    placed = {
        KEY: insert_checks.Placed(
            KEY, "demo-cat-walk", insert.label, 0.0, len(frames) / FPS, light=insert.light, keyed=True, despill=despill
        )
    }
    audits = {KEY: {"frames": frames, "evidence": {}}}
    found = [finding for finding, _proof in insert_checks.findings(audits, placed, fps=FPS)]
    [report] = insert_checks.reports(audits, placed, fps=FPS)
    return report, found


def _audits(read: screen_insert.RimRead, count: int = 6) -> dict:
    audit = screen_insert.FrameAudit(
        rim=read.share,
        rim_edge=480,
        rim_light=read.light,
        rim_surroundings=read.surroundings,
        rim_whole=read.whole,
        rim_off=read.off,
    )
    return {str(n): audit.as_dict() for n in range(count)}


def test_the_grey_the_compositing_drew_off_the_surface_reads_past_the_gate():
    read = _frame(drawn_grey=0.4)
    assert read.share is None or read.share <= screen_insert.MAX_RIM_SHARE, read
    assert read.off is not None and read.off > screen_insert.MAX_RIM_SHARE, read
    assert read.hits is not None and int(read.hits.sum()) > 0


def test_a_clean_matte_reads_nothing_off_the_surface():
    read = _frame()
    assert read.off == 0.0, read
    assert read.hits is None


def test_the_footage_s_own_grey_off_the_surface_is_never_gated():
    read = _frame(drawn_grey=0.4, wall=GREY)
    assert read.off == 0.0, read
    assert read.surroundings is not None and read.surroundings > screen_insert.MAX_RIM_SHARE


def test_the_insert_blocks_with_its_frame_and_the_edge_despill_turned_on():
    read = _frame(drawn_grey=0.4)
    report, found = _read(_audits(read), {"edge": False})
    [rim] = [finding for finding in found if finding.kind == render_checks.INSERT_RIM]
    assert rim.severity == render_checks.FAIL and report["verdicts"]["rim"] is False
    assert rim.data["rim_off_surface"] == read.off and rim.data["value"] == read.off
    assert rim.data["threshold"] == screen_insert.MAX_RIM_SHARE == 0.15
    assert "off the keyed surface, where the footage had colour" in rim.message
    assert "insert rim at t=" in rim.message
    assert rim.data["action"]["kind"] == insert_checks.WIDEN_DESPILL
    assert "despill: {edge: true" in rim.data["action"]["text"]
    assert report["rim_off"] == read.off


def test_the_same_scene_drawn_clean_stays_green():
    report, found = _read(_audits(_frame()), {"edge": False})
    assert render_checks.INSERT_RIM not in {finding.kind for finding in found}, [f.message for f in found]
    assert report["verdicts"]["rim"] is True


def test_a_rim_on_the_surface_keeps_its_finding_and_its_action():
    frames = {str(n): {"rim": 0.3, "rim_edge": 640, "rim_light": 0.0, "rim_off": 0.0} for n in range(6)}
    _report, found = _read(frames, {"edge": False})
    [rim] = [finding for finding in found if finding.kind == render_checks.INSERT_RIM]
    assert rim.data["rim_on_surface"] == 0.3 and rim.data["value"] == 0.3
    assert "over the keyed surface" in rim.message
    assert rim.data["action"]["kind"] == insert_checks.REGENERATE_SHOT


def test_the_audit_keeps_the_share_and_the_drawing_version_moved():
    record = screen_insert.FrameAudit(rim=0.02, rim_off=0.4).as_dict()
    assert record["rim_off"] == 0.4
    audit = screen_insert.FrameAudit.from_dict(record)
    assert audit.rimmed and audit.failing
    assert not screen_insert.FrameAudit(rim=0.02, rim_off=0.1).rimmed
    assert screen_insert.DRAWING_VERSION >= 24
