"""A neighbour's screen insert is measured on a join only at the size it was last measured at.

A statement scene previewed at draft quality (1280x720) read the screen insert of the clip after it on their join
and filed ``insert_alignment`` (1.24% off, its top side 2.25%) on every draft preview, while the save, on its footage
frame, and the clip's own 1080p preview read it 0.02% off. Synthetic fixtures only - no browser, no worker:

* a draft preview of the statement says the neighbour's stored measure as one fact quoting it - the clip's own
  drawing as it is now, else its save - and files no ``insert_alignment``; a preview at the size it was measured at
  measures it again;
* a join frame drawn smaller than the footage frame the save measured, whose reading is past
  ``ALIGNMENT_MAX_SHARE`` off the save's, is set aside for it and said to the administrator (``infra_once``), never
  filed against the video; a frame at the save's size, or a reading close to the save's, stands;
* the save keeps the size of the frame it read with its measure, and a drawing keeps the size its frames were
  measured at and the alignment it read: what the next preview tells another size by.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from app.services.video_studio import insert_bezels, insert_checks, render_answer, render_checks, surface_refine
from app.services.video_studio.frame_owners import FrameOwners
from app.services.video_studio.render_checks import INSERT_ALIGNMENT, WARN, Checks
from app.services.video_studio.screen_insert import Footage, InsertSource
from app.services.video_studio.spec import parse_video_spec, validate_video_spec

pytestmark = pytest.mark.usefixtures("no_default_video_template")

FPS = 30
FADE = 0.5
CLIP = "00000000-0000-0000-0000-00000000c576"
BACKDROP = "00000000-0000-0000-0000-00000000e576"
KEY = "insert/s02.0"
LABEL = "Scene s02: its screen insert 0"
QUAD = [[0.25, 0.236], [0.75, 0.236], [0.75, 0.764], [0.25, 0.764]]
DRAFT, STANDARD, UHD = [1280, 720], [1920, 1080], [3840, 2160]
#: What the bezel finder read on the join's frames of the draft: the quad 1.24% off, its top side 2.25%.
MISREAD = {"alignment": 0.0124, "alignment_sides": {"top": 0.0225, "right": 0.006, "bottom": 0.004, "left": 0.008}}
ON_BEZEL = {"alignment": 0.0002, "alignment_sides": {"top": 0.0003, "right": 0.0002, "bottom": 0.0001, "left": 0.0002}}


def _document() -> dict:
    """A 3 s statement ``s01``, then a 3 s clip ``s02`` coming in by a 0.5 s crossfade, its static insert held over its
    whole scene - planned as ``timeline.plan`` lays it out (the statement painted on through the crossfade)."""
    payload = {"src": KEY, "quad": QUAD, "t0": 0.0, "t1": 3.0, "to_end": True}
    scenes = [
        {"id": "s01", "type": "statement", "start": 0.0, "end": 3.0 + FADE, "transition_in": {"type": "cut", "duration": 0.0}},
        {
            "id": "s02",
            "type": "clip",
            "start": 3.0,
            "end": 6.0,
            "transition_in": {"type": "crossfade", "duration": FADE},
            "inserts": [payload],
        },
    ]
    return {"fps": FPS, "frames": 6 * FPS, "duration": 6.0, "fade_in": 0.0, "fade_out": 0.0, "scenes": scenes}


def _placed() -> dict[str, insert_checks.Placed]:
    source = InsertSource(
        "clip.png",
        "image",
        tuple(tuple(point) for point in QUAD),
        footage=Footage("clip.mp4", "video", float(FPS), "clip/clip"),
        label=LABEL,
    )
    document = _document()
    return insert_checks.placed_inserts(document, {KEY: source}, owners=FrameOwners.of(document))


def _join(placed: dict) -> range:
    [(first, end)] = placed[KEY].joins
    return range(first, end)


def _audits(frames: dict[int, dict]) -> dict:
    return {KEY: {"frames": {str(n): {"refined": QUAD, **audit} for n, audit in frames.items()}, "evidence": {}}}


def _save(size: tuple[int, int] = (1920, 1080), error: float = 0.0002) -> insert_bezels.Measured:
    corners = [(480.0, 255.0), (1440.0, 255.0), (1440.0, 825.0), (480.0, 825.0)]
    flat = surface_refine.flatness(corners, (0.0,) * 4, (1.0,) * 4, (0.0,) * 4, size)
    bezel = surface_refine.Bezel(tuple(tuple(point) for point in QUAD), flat, error)
    given = tuple(tuple(point) for point in QUAD)
    return insert_bezels.Measured("s02", 0, ("clip", CLIP), "file-1", 0, 0.0, given, bezel, "d", size=size)


def _passed(saved: insert_bezels.Measured, drawings: dict | None = None) -> dict[str, list[dict]]:
    """What measured the insert before, gathered as ``render.py`` gathers it: its scene's drawing as it is now, then the
    save's measure."""
    passed = render_checks.insert_verdicts(drawings, {"s01": "d1", "s02": "d2"})
    for name, records in insert_bezels.verdicts([saved], 9).items():
        passed.setdefault(name, []).extend(records)
    return passed


def _checked(audits: dict, saved: insert_bezels.Measured, *, scene_id: str | None, drawings: dict | None = None) -> Checks:
    """A render's insert checks as ``render.py`` makes them: the bezel readings settled, then the findings and the
    reports, a scene preview's verdict its own scene's."""
    placed = _placed()
    passed = _passed(saved, drawings)
    measured = insert_bezels.measured_surfaces([saved])
    window = (0, _join(placed).stop) if scene_id else None
    readings = insert_checks.bezel_readings(audits, placed, passed=passed, measured=measured, scene_id=scene_id)
    found = insert_checks.findings(readings.audits, placed, fps=FPS, passed=passed, frame_range=window, measured=measured)
    checks = Checks(
        findings=[finding for finding, _proof in found],
        notes=list(readings.facts),
        infra=list(readings.infra),
        inserts=insert_checks.reports(readings.audits, placed, fps=FPS, measured=measured),
    )
    if scene_id:
        render_checks.settle_neighbours(checks, scene_id, {})
    return checks


def _aligned(checks: Checks) -> list:
    return [finding for finding in checks.findings if finding.kind == INSERT_ALIGNMENT]


# ── 1. A draft preview says the neighbour's stored measure ───────────────


def test_the_fixture_reads_the_neighbours_insert_off_its_bezel_on_the_drafts_join():
    """The bug as reported: read on the draft's join frames, the alignment gate fails on the neighbour's insert."""
    placed = _placed()
    audits = _audits({n: {"size": DRAFT, **MISREAD} for n in _join(placed)})
    found = insert_checks.findings(audits, placed, fps=FPS, frame_range=(0, _join(placed).stop))
    [(finding, _proof)] = [item for item in found if item[0].kind == INSERT_ALIGNMENT]
    assert "1.24% of the frame's width off the bezel" in finding.message and "its top side 2.25% off" in finding.message


def test_a_draft_preview_of_the_statement_files_no_alignment_and_says_the_saves_measure_once():
    placed = _placed()
    checks = _checked(_audits({n: {"size": DRAFT, **MISREAD} for n in _join(placed)}), _save(), scene_id="s01")
    assert _aligned(checks) == []
    answer = render_answer.of_checks(checks.as_dict())
    assert not [item for item in answer.findings if item["kind"] == INSERT_ALIGNMENT]
    [fact] = [item["what"] for item in answer.facts if "0.02%" in item["what"]]
    assert fact == (
        "Scene s02: its screen insert 0, drawn on this preview's join with scene s02 at 1280x720: 0.02% of the frame's "
        "width off its bezel (mean over its corners), as save revision 9 measured it on frame 0 (0.00 s) of its footage "
        "at 1920x1080 - not measured again at another size: a fact, not a finding."
    )
    assert answer.infra_once == []
    # The report says the measure that stands, never that no bezel was found under the quad.
    [report] = checks.inserts
    assert report["bezel_stored"] == {
        "alignment": 0.0002,
        "measured_at": [1920, 1080],
        "by": "save revision 9",
        "where": "frame 0 (0.00 s) of its footage",
    }
    assert report["seed_error"] is None and "verdicts" in report and "alignment" not in report["verdicts"]
    said = [measured.text for measured in render_checks.insert_measured(report)]
    assert not any("bezel" in text for text in said), said


def test_the_neighbours_own_preview_is_the_measure_a_draft_says_when_it_has_one():
    placed = _placed()
    own = {n: {"size": STANDARD, **ON_BEZEL} for n in range(_join(placed).stop, 6 * FPS)}
    [report] = insert_checks.reports(_audits(own), placed, fps=FPS)
    drawings = render_checks.record_seen(None, {"s02": "d2"}, "r-own", checks={"inserts": [report], "findings": []}, stage="preview")
    assert drawings["s02"]["inserts"][KEY]["alignment"] == 0.0002
    assert drawings["s02"]["inserts"][KEY]["measured_at"] == [1920, 1080]
    checks = _checked(_audits({n: {"size": DRAFT, **MISREAD} for n in _join(placed)}), _save(), scene_id="s01", drawings=drawings)
    assert _aligned(checks) == []
    [fact] = [note for note in checks.notes if "0.02%" in note]
    assert "as preview r-own measured it at 1920x1080 - not measured again at another size" in fact


def test_a_preview_at_the_size_the_neighbour_was_measured_at_measures_it_again():
    placed = _placed()
    checks = _checked(_audits({n: {"size": STANDARD, **MISREAD} for n in _join(placed)}), _save(), scene_id="s01")
    assert checks.notes == [] and checks.infra == []
    # Read at the save's own size, its reading stands: a warning on the neighbour that disagrees with the save, as before.
    [finding] = _aligned(checks)
    assert finding.severity == WARN and finding.scene_id == "s02"
    assert finding.message.startswith("disagrees with save revision 9: insert off its bezel")


def test_a_neighbour_nothing_measured_before_is_measured_on_the_join():
    """A tracked insert, which no save measures, never previewed: nothing stored, its reading on the join stands."""
    placed = _placed()
    audits = _audits({n: {"size": DRAFT, **MISREAD} for n in _join(placed)})
    readings = insert_checks.bezel_readings(audits, placed, passed={}, measured={}, scene_id="s01")
    assert readings.audits == audits and readings.facts == [] and readings.infra == []


# ── 2. A lower-resolution reading the save's measure disagrees with ──────


def test_a_join_reading_drawn_smaller_than_the_saves_frame_and_off_its_measure_is_set_aside_and_said_to_the_administrator():
    placed = _placed()
    join = _join(placed)
    frames = {n: {"size": STANDARD, **MISREAD} for n in join}
    frames.update({n: {"size": STANDARD, **ON_BEZEL} for n in range(join.stop, 6 * FPS)})
    checks = _checked(_audits(frames), _save(size=(3840, 2160)), scene_id=None)
    assert _aligned(checks) == [] and checks.notes == []
    answer = render_answer.of_checks(checks.as_dict())
    [line] = answer.infra_once
    assert line == (
        f"bezel finder, {LABEL}: {len(join)} frames of its scene's joins drawn at 1920x1080 read its quad up to 2.25% of "
        "the frame's width off its bezel (mean or one side), where the save measured 0.02% on frame 0 (0.00 s) of its "
        "footage at 3840x2160 - more than 0.5% apart, so the lower-resolution reading is set aside for the save's: the "
        "platform's measurement, no finding of the video"
    )
    assert not answer.findings
    [report] = checks.inserts
    assert report["bezel_discarded"] == {"frames": len(join), "worst": 0.0225}
    # Its own frames are read as drawn: on its bezel.
    assert report["seed_error"] == 0.0002 and report["verdicts"]["alignment"] is True
    said = [measured.text for measured in render_checks.insert_measured(report)]
    assert f"bezel reading set aside on {len(join)} frames of its scene's joins" in " ".join(said)
    assert Checks.from_dict(checks.as_dict()).infra == checks.infra


def test_a_join_reading_at_the_saves_size_or_close_to_its_measure_stands():
    placed = _placed()
    join = _join(placed)
    whole = insert_checks.bezel_readings(
        _audits({n: {"size": UHD, **MISREAD} for n in join}),
        placed,
        measured=insert_bezels.measured_surfaces([_save(size=(3840, 2160))]),
    )
    assert whole.infra == [] and all("alignment" in audit for audit in whole.audits[KEY]["frames"].values())
    near = {"alignment": 0.004, "alignment_sides": {"top": 0.0045}}
    close = insert_checks.bezel_readings(
        _audits({n: {"size": STANDARD, **near} for n in join}),
        placed,
        measured=insert_bezels.measured_surfaces([_save(size=(3840, 2160))]),
    )
    assert close.infra == [] and all("alignment" in audit for audit in close.audits[KEY]["frames"].values())


# ── 3. The size of each measure is kept ───────────────────────────────────


def _screen() -> np.ndarray:
    """A 1280x720 room, a black bezel and, inside it, a flat green screen from (320, 170) to (960, 550)."""
    frame = np.full((720, 1280, 3), 46, dtype=np.uint8)
    frame[150:571, 300:981] = (18, 18, 20)
    frame[170:550, 320:960] = (0, 177, 64)
    return frame


def test_the_save_keeps_the_size_of_the_frame_it_read_with_its_measure(monkeypatch):
    from app.services.video_studio import assets, render_cache

    quad = [[320 / 1280, 170 / 720], [960 / 1280, 170 / 720], [960 / 1280, 550 / 720], [320 / 1280, 550 / 720]]
    parsed = parse_video_spec(
        validate_video_spec(
            {
                "title": "Booth",
                "freestyle": True,
                "scenes": [
                    {
                        "id": "s1",
                        "type": "statement",
                        "lines": ["Live"],
                        "backdrop": {"file_id": BACKDROP},
                        "overlays": [{"type": "screen_insert", "source": {"file_id": CLIP}, "quad": quad, "duration": 4.0}],
                    }
                ],
            }
        )
    )
    kept: dict = {}

    async def footage_of(_db, _scene):
        return (("file", BACKDROP), BACKDROP, True)

    async def download(_file_id, path):
        Image.fromarray(_screen()).save(path, format="PNG")
        return SimpleNamespace(filename="booth.png")

    async def get_json(kind, digest):
        return kept.get((kind, digest))

    async def put_json(kind, digest, value):
        kept[(kind, digest)] = value

    async def put_file(kind, digest, path, *, suffix=""):
        kept[(kind, digest, suffix)] = path

    monkeypatch.setattr(insert_bezels, "_footage_of", footage_of)
    monkeypatch.setattr(assets, "download_file", download)
    monkeypatch.setattr(render_cache, "get_json", get_json)
    monkeypatch.setattr(render_cache, "put_json", put_json)
    monkeypatch.setattr(render_cache, "put_file", put_file)
    [item] = asyncio.run(insert_bezels.measure(None, parsed))
    assert item.size == (1280, 720) and item.bezel is not None and item.bezel.error is not None
    [record] = insert_bezels.verdicts([item], 4)["insert/s1.0"]
    assert record["measured_at"] == [1280, 720] and record["where"] == "frame 0 (0.00 s) of its footage"
    assert record["alignment"] == round(item.bezel.error, 5)
    # Read back from its cache, the next save keeps it without reading the frame again.
    monkeypatch.setattr(surface_refine, "measure_bezel", lambda *_a, **_k: pytest.fail("measured again"))
    [again] = asyncio.run(insert_bezels.measure(None, parsed))
    assert again.size == (1280, 720)
    # A quad left to the save is the bezel: no alignment to say.
    left = insert_bezels.Measured("s1", 0, ("file", BACKDROP), BACKDROP, 0, 0.0, None, item.bezel, "d", size=(1280, 720))
    assert "alignment" not in insert_bezels.verdicts([left], 4)["insert/s1.0"][0]


def test_a_new_take_is_measured_in_place_of_the_one_that_plays(monkeypatch):
    """#10901 S2: ``measure(swap=...)`` reads an AI shot's new take, never the file its row names."""
    from app.services.video_studio import assets, render_cache

    quad = [[320 / 1280, 170 / 720], [960 / 1280, 170 / 720], [960 / 1280, 550 / 720], [320 / 1280, 550 / 720]]
    parsed = parse_video_spec(
        validate_video_spec(
            {
                "title": "Booth",
                "freestyle": True,
                "scenes": [
                    {
                        "id": "s1",
                        "type": "statement",
                        "lines": ["Live"],
                        "backdrop": {"file_id": BACKDROP},
                        "overlays": [
                            {"type": "screen_insert", "source": {"file_id": CLIP}, "quad": quad, "duration": 4.0}
                        ],
                    }
                ],
            }
        )
    )
    shot, playing, new = (
        "00000000-0000-0000-0000-0000000a1901",
        "00000000-0000-0000-0000-0000000a1902",
        "00000000-0000-0000-0000-0000000a1903",
    )
    read: list[str] = []

    async def footage_of(_db, _scene):
        return (("clip", shot), playing, True)

    async def download(file_id, path):
        read.append(str(file_id))
        Image.fromarray(_screen()).save(path, format="PNG")
        return SimpleNamespace(filename="booth.png")

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(insert_bezels, "_footage_of", footage_of)
    monkeypatch.setattr(assets, "download_file", download)
    monkeypatch.setattr(render_cache, "get_json", nothing)
    monkeypatch.setattr(render_cache, "put_json", nothing)
    monkeypatch.setattr(render_cache, "put_file", nothing)
    [item] = asyncio.run(insert_bezels.measure(None, parsed, swap={shot: new}))
    assert item.file_id == new and item.shot == shot and read == [new]
