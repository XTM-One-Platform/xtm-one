"""The grey rim read on the keyed surface alone, the footage's own grey surroundings a fact.

Video fdd6e246 rev 290, scene demo-opencti-map: a cat on a grey console below a keyed wall, the quad reaching over the
console. ``insert_rim`` blocked at 18.4 % against 15 % on a take whose insert showed residual key 0 and halo 0 and whose
crops at 100 % showed no grey rim: the band past the cat lay on the console, grey in the footage itself. Read on one
frame's box as the layer reads it (``screen_insert._rim_share`` over ``clear_part``), then as the render's findings and
report (``insert_checks``):

* F60 a green surface over a grey plate, a cat-shaped subject on the plate alone: the band on the surface 0 %, on the
  surroundings past 15 % - a fact, never ``insert_rim``;
* F61 the same subject moved over the keyed surface with a 20 % grey rim painted there: ``insert_rim`` still fails, its
  data carrying ``rim_on_surface`` and ``rim_on_surroundings``;
* a grey the footage itself shows on the surface is never rim; without a matte the whole band is read as before;
* the fixtures of ``test_video_studio_insert_rim`` keep their verdicts; the drawing version moved.
"""

from __future__ import annotations

import numpy as np

from app.services.video_studio import compositor, insert_checks, render_checks, screen_insert, screens

FPS = 24.0
KEY = "insert/demo-opencti-map.0"
LABEL = "Scene demo-opencti-map: its screen insert 0"
#: The box a frame is read on: the keyed surface above row :data:`SPLIT`, a grey plate (a console) below it.
SIZE, SPLIT = 240, 120
GREEN = (32, 255, 30)
BLUE = (40, 120, 235)
PLATE = (100, 100, 100)
FUR = (58, 38, 28)
KEYED = screens.Key("green", GREEN)


def _cat(centre: tuple[int, int], radius: int = 34) -> np.ndarray:
    """A cat-shaped mask over the box: a head and two ears (boolean)."""
    rows, columns = np.mgrid[0:SIZE, 0:SIZE]
    y, x = centre
    head = (rows - y) ** 2 + (columns - x) ** 2 <= radius**2
    ears = np.zeros_like(head)
    for side in (-1, 1):
        tip = (y - radius - 10, x + side * radius // 2)
        ears |= (np.abs(columns - tip[1]) <= (rows - tip[0]) * 0.6) & (rows >= tip[0]) & (rows <= y - radius // 2)
    return head | ears


def _frame(centre: tuple[int, int], *, painted: float = 0.0, plate_on_surface: bool = False) -> screen_insert.RimRead:
    """One frame's rim read: the footage (green surface, grey plate, the cat), its matte, the insert drawn blue where the
    surface shows and the footage kept elsewhere; *painted* of the band over the surface drawn grey (the key's residue);
    *plate_on_surface*: the footage already grey on that band (a grey the camera saw, keyed nothing)."""
    cat = _cat(centre)
    surface = np.zeros((SIZE, SIZE), dtype=bool)
    surface[:SPLIT] = True
    footage = np.empty((SIZE, SIZE, 3), dtype=np.float32)
    footage[surface], footage[~surface] = GREEN, PLATE
    footage[cat] = FUR
    matte = (surface & ~cat).astype(np.float32)
    inside = np.ones((SIZE, SIZE), dtype=bool)
    clear = screen_insert.clear_part(cat, inside, matte, 1.0)
    assert clear is not None
    composed = np.where(matte[..., None] > 0.5, np.array(BLUE, dtype=np.float32), footage)
    band = np.zeros((SIZE, SIZE), dtype=bool)
    band[clear.window] = ~clear.part & (clear.distance > screen_insert.RIM_FROM_PX) & (clear.distance <= screen_insert.RIM_PX)
    on_surface = np.argwhere(band & surface)
    if painted:
        chosen = on_surface[: round(painted * len(on_surface))]
        composed[chosen[:, 0], chosen[:, 1]] = PLATE
        if plate_on_surface:
            footage[chosen[:, 0], chosen[:, 1]] = PLATE
    own = np.broadcast_to(np.array(BLUE, dtype=np.float32), (SIZE, SIZE, 3))
    alpha = matte
    return screen_insert._rim_share(composed, own, own, alpha, clear, inside, KEYED, 1.0, footage=footage, matte=matte)


def test_f60_a_cat_on_the_grey_plate_alone_reads_no_rim_on_the_surface():
    read = _frame((190, 120))
    assert read.share is None or read.share == 0.0, read
    assert read.surroundings is not None and read.surroundings > screen_insert.MAX_RIM_SHARE, read
    assert read.whole is not None and read.whole > screen_insert.MAX_RIM_SHARE, read
    assert read.hits is None


def test_f60_where_the_cat_touches_the_surface_the_surface_s_band_reads_clean():
    read = _frame((150, 120))
    assert read.share == 0.0, read
    assert read.surroundings is not None and read.surroundings > screen_insert.MAX_RIM_SHARE, read
    # The rim read earlier - the whole band - is past the gate on the same frame.
    assert read.whole is not None and read.whole > screen_insert.MAX_RIM_SHARE


def test_f61_a_real_grey_rim_over_the_keyed_surface_still_reads_past_the_gate():
    read = _frame((60, 120), painted=0.2)
    assert read.share is not None and 0.18 <= read.share <= 0.22, read
    assert read.share > screen_insert.MAX_RIM_SHARE
    assert read.hits is not None and int(read.hits.sum()) > 0
    assert read.light == 0.0


def test_a_grey_the_footage_itself_shows_on_the_surface_is_never_rim():
    read = _frame((60, 120), painted=0.2, plate_on_surface=True)
    assert read.share == 0.0, read


def test_without_a_matte_the_whole_band_is_read_as_before():
    cat = _cat((190, 120))
    inside = np.ones((SIZE, SIZE), dtype=bool)
    matte = np.zeros((SIZE, SIZE), dtype=np.float32)
    matte[:SPLIT] = 1.0
    matte[cat] = 0.0
    clear = screen_insert.clear_part(cat, inside, matte, 1.0)
    footage = np.where(matte[..., None] > 0.5, np.array(GREEN, dtype=np.float32), np.array(PLATE, dtype=np.float32))
    footage[cat] = FUR
    composed = np.where(matte[..., None] > 0.5, np.array(BLUE, dtype=np.float32), footage)
    own = np.broadcast_to(np.array(BLUE, dtype=np.float32), (SIZE, SIZE, 3))
    read = screen_insert._rim_share(composed, own, own, matte, clear, inside, KEYED, 1.0)
    assert read.surroundings is None and read.share == read.whole and read.share > screen_insert.MAX_RIM_SHARE


# ── The gate, its finding and its report ──────────────────────────────────


def _insert() -> screen_insert.InsertSource:
    return screen_insert.InsertSource(
        "panel.png",
        "image",
        ((0.3, 0.2), (0.7, 0.2), (0.7, 0.75), (0.3, 0.75)),
        key=screen_insert.KeySpec("green"),
        footage=screen_insert.Footage("clip", "video", FPS, "clip/demo-opencti-map"),
        label=LABEL,
    )


def _read(frames: dict) -> tuple[dict, list]:
    insert = _insert()
    placed = {KEY: insert_checks.Placed(KEY, "demo-opencti-map", insert.label, 0.0, len(frames) / FPS, light=insert.light, keyed=True)}
    audits = {KEY: {"frames": frames, "evidence": {}}}
    found = [finding for finding, _proof in insert_checks.findings(audits, placed, fps=FPS)]
    [report] = insert_checks.reports(audits, placed, fps=FPS)
    return report, found


def _audits(read: screen_insert.RimRead, count: int = 6) -> dict:
    audit = screen_insert.FrameAudit(rim=read.share, rim_edge=640, rim_light=read.light, rim_surroundings=read.surroundings, rim_whole=read.whole)
    return {str(n): audit.as_dict() for n in range(count)}


def test_f60_previews_green_and_says_both_shares_as_facts():
    read = _frame((150, 120))
    report, found = _read(_audits(read))
    assert render_checks.INSERT_RIM not in {finding.kind for finding in found}, [f.message for f in found]
    assert report["verdicts"]["rim"] is True
    assert report["rim_surroundings"] == read.surroundings and report["rim_whole"] == read.whole
    lines = [m.line() for m in insert_checks.classify(report)]
    fact = f"fact: grey past what stands in front, not gated: {read.surroundings * 100:.1f}% on the surroundings off the keyed surface; {read.whole * 100:.1f}% on the whole band"
    assert fact in lines, lines


def test_f61_blocks_with_the_same_finding_kind_and_both_shares():
    read = screen_insert.RimRead(0.2, 640, 0.0, surroundings=0.42, whole=0.3)
    report, found = _read(_audits(read))
    [rim] = [finding for finding in found if finding.kind == render_checks.INSERT_RIM]
    assert rim.severity == render_checks.FAIL and report["verdicts"]["rim"] is False
    assert (rim.data["rim_on_surface"], rim.data["rim_on_surroundings"]) == (0.2, 0.42)
    assert rim.data["value"] == rim.data["worst"] == report["rim"] == 0.2
    assert "grey on 20.0% of the band 2-12 px past what stands in front over the keyed surface (640 px of its edge)" in rim.message
    assert "42.0% on the surroundings off the keyed surface, not gated" in rim.message


def test_a_finding_from_an_audit_without_the_surroundings_says_none():
    frames = {str(n): {"rim": 0.3, "rim_edge": 640, "rim_light": 0.0} for n in range(6)}
    _report, found = _read(frames)
    [rim] = [finding for finding in found if finding.kind == render_checks.INSERT_RIM]
    assert rim.data["rim_on_surface"] == 0.3 and rim.data["rim_on_surroundings"] is None
    assert "surroundings" not in rim.message


def test_the_audit_keeps_both_facts_and_the_drawing_version_moved():
    record = screen_insert.FrameAudit(rim=0.02, rim_surroundings=0.4, rim_whole=0.2).as_dict()
    assert (record["rim_surroundings"], record["rim_whole"]) == (0.4, 0.2)
    audit = screen_insert.FrameAudit.from_dict(record)
    assert not audit.rimmed and not audit.failing
    assert screen_insert.DRAWING_VERSION >= 16
    assert compositor._insert_inputs(_insert(), compositor.CompositorAssets(), [])["drawing"] == screen_insert.DRAWING_VERSION
