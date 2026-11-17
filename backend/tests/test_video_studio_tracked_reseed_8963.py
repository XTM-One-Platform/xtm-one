"""A tracked insert's alignment finding names an edit the save accepts (xtm-one #8963).

A render's ``insert_alignment`` on a ``track: true`` insert advised "quad auto, then save: the track starts from that
seed" for a stray in the middle of the insert. ``quad: auto`` is refused on a tracked insert and a track starts only
from the quad given on the insert's first frame, so the advice could not be applied. The finding now carries the edit
field by field (a split, a trim or a re-seed), and applying it literally saves.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.video_studio import insert_checks, render_checks
from app.services.video_studio.render_checks import INSERT_ALIGNMENT
from app.services.video_studio.spec import Overlay

KEY = "insert/s1.0"
LABEL = "Scene s1: its screen insert 0"
GIVEN = [[0.3, 0.25], [0.7, 0.25], [0.7, 0.75], [0.3, 0.75]]
SNAPPED = [[0.31, 0.26], [0.71, 0.25], [0.7, 0.76], [0.3, 0.74]]
FPS = 60.0
SOURCE = {"file_id": "11111111-1111-4111-8111-111111111111"}


def _overlay(**over: object) -> Overlay:
    fields = {
        "type": "screen_insert",
        "source": SOURCE,
        "quad": GIVEN,
        "track": True,
        "start": 1.0,
        "duration": 4.0,
        "trim_start": 2.0,
        **over,
    }
    return Overlay.model_validate(fields)


def _frames(count: int, off: range) -> dict[str, dict]:
    """*count* frames on the bezel, *off* just past the 0.5% gate on the left side, each drawn into SNAPPED."""
    out = {}
    for n in range(count):
        share = 0.0052 if n in off else 0.001
        out[str(n)] = {
            "alignment": share,
            "alignment_sides": {"left": share, "right": 0.001, "top": 0.001, "bottom": 0.001},
            "refined": SNAPPED if n in off else GIVEN,
            "size": [1920, 1080],
        }
    return out


def _placed(overlay: Overlay, *, scene_start: float = 0.0) -> dict:
    part = SimpleNamespace(overlay=overlay, pane=None, kind="file")
    start = scene_start + overlay.start
    return {
        KEY: insert_checks.Placed(
            KEY,
            "s1",
            LABEL,
            start,
            start + overlay.duration,
            tracked=True,
            timing=insert_checks.InsertTiming.of(part),
        )
    }


def _alignment(overlay: Overlay, off: range, *, count: int = 300) -> object:
    # Frames are output frames of the video: the insert starts at 1 s, so its frame 0 there is output frame 60.
    first = round(overlay.start * FPS)
    frames = {str(first + int(n)): audit for n, audit in _frames(count, off).items()}
    found = insert_checks.findings({KEY: {"frames": frames, "evidence": {}}}, _placed(overlay), fps=FPS)
    [finding] = [finding for finding, _proof in found if finding.kind == INSERT_ALIGNMENT]
    return finding


def _apply(overlay: Overlay, edit: dict) -> list[Overlay]:
    """The edit applied literally: each part the overlay's fields with its own set, validated as the save does."""
    base = overlay.model_dump(exclude_none=True)
    return [Overlay.model_validate({**base, **fields}) for fields in edit["inserts"]]


def test_a_stray_mid_insert_is_split_there_and_both_inserts_save():
    overlay = _overlay()
    # Frames 120-139 of the insert (2.0 s in) stray; it lasts 240 frames (4 s).
    finding = _alignment(overlay, range(120, 140), count=240)
    assert finding.severity == render_checks.FAIL
    assert "quad auto" not in finding.fix and "track starts from that seed" not in finding.fix
    edit = finding.data["action"]["edit"]
    assert edit["how"] == "split" and edit["at"] == 2.0
    first, second = _apply(overlay, edit)
    assert first.start == 1.0 and first.duration == 2.0 and first.trim_start == 2.0 and first.quad == overlay.quad
    assert second.start == 3.0 and second.duration == 2.0 and second.trim_start == 4.0
    assert second.track and [list(corner) for corner in second.quad] == SNAPPED
    # The fix text names those same fields.
    assert "start: 3.0" in finding.fix and "trim_start: 4.0" in finding.fix and "duration: 2.0" in finding.fix


def test_a_stray_from_the_first_frame_starts_it_after_the_stray_never_its_own_quad_again():
    # xtm-one #9196: the save snaps a first-frame quad to the bezel, so the stored quad is already those corners;
    # "give its quad again" drew the same frames and the same finding came back.
    overlay = _overlay()
    finding = _alignment(overlay, range(0, 30), count=240)
    edit = finding.data["action"]["edit"]
    assert edit["how"] == "late" and edit["at"] == 0.5
    assert "give its quad again" not in finding.fix
    [late] = _apply(overlay, edit)
    # Something the drawing is made of changes: when it starts, the source time it shows, the seed of its track.
    assert late.track and late.start == 1.5 and late.duration == 3.5 and late.trim_start == 2.5
    assert [list(corner) for corner in late.quad] == GIVEN
    drawn = lambda part: SimpleNamespace(overlay=part, kind="file", ref=SOURCE["file_id"], quad=part.quad, pane=None)  # noqa: E731
    assert insert_checks.rest_digest(drawn(late)) != insert_checks.rest_digest(drawn(overlay))
    assert "start: 1.5" in finding.fix and "trim_start: 2.5" in finding.fix


def test_a_first_frames_stray_leaving_too_little_offers_no_field_edit():
    overlay = _overlay(duration=0.6)
    finding = _alignment(overlay, range(0, 20), count=36)
    assert "edit" not in finding.data["action"]
    assert "give its quad again" not in finding.fix and "snaps to the same corners" in finding.fix


def test_a_stray_to_its_end_trims_it_and_saves():
    overlay = _overlay()
    finding = _alignment(overlay, range(200, 240), count=240)
    edit = finding.data["action"]["edit"]
    assert edit["how"] == "trim"
    [trimmed] = _apply(overlay, edit)
    assert trimmed.duration < overlay.duration and trimmed.start == overlay.start


def test_a_brand_mark_is_split_without_a_trim_start():
    overlay = _overlay(source={"asset": "logo"}, trim_start=None)
    part = SimpleNamespace(overlay=overlay, pane=None, kind="asset")
    where = insert_checks.Placed(KEY, "s1", LABEL, 1.0, 5.0, tracked=True, timing=insert_checks.InsertTiming.of(part))
    frames = {n: audit for n, audit in ((int(k), v) for k, v in _frames(240, range(120, 140)).items())}
    edit = insert_checks.tracked_reseed(frames, list(range(120, 140)), lambda n: 1.0 + n / FPS, where)
    assert edit is not None and edit["how"] == "split"
    assert "trim_start" not in edit["inserts"][1]
    _apply(overlay, edit)


def test_without_its_spec_the_fix_still_names_a_split_never_quad_auto():
    frames = {n: audit for n, audit in ((int(k), v) for k, v in _frames(240, range(120, 140)).items())}
    _lead, chosen = insert_checks._alignment_remedy(
        frames, list(range(120, 140)), frames[130], lambda n: n / FPS, tracked=True
    )
    assert "edit" not in chosen
    assert "split it at t=" in chosen["text"] and "quad auto" not in chosen["text"]


def test_a_tracked_quad_auto_is_still_refused():
    # The constraint the fix text is written around: no mid-insert seed through quad auto.
    try:
        _overlay(quad="auto")
    except ValueError as exc:
        assert "quad auto" in str(exc)
    else:  # pragma: no cover - the refusal is the contract
        raise AssertionError("a tracked quad auto saved")
