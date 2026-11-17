"""The presence gate on an insert the video's fade to black takes to near black.

An insert that runs to its scene's last frame is shown under the video's fade out to the end: on the frames the fade
leaves less than ``PRESENCE_MIN_SHOWN`` of the whole frame, the quad and the frame around it are near black alike and
nothing tells the insert from the black laid over it. Those frames are not judged (``faded``, said as a fact), as a
transition's frames showing under the floor of the insert are (``faint``); on the frames the fade leaves visible, an
insert whose source is removed still reads missing.
"""

from __future__ import annotations

import functools

import pytest

from app.services.video_studio import insert_checks
from app.services.video_studio.render_checks import FAIL, INSERT_MISSING
from tests.video_studio_insert_fixture import FPS, PresenceRender, clip_document, key_of

KEY = key_of("b")
#: One scene of 2.5 s, its insert to the last frame, the video fading out over 0.9 s (the planner's default).
SCENE = (("b", 0.0, 2.5, 2.5, "cut", 0.0),)
FADE_OUT = 0.9


def _document() -> dict:
    document = clip_document(list(SCENE))
    document["fade_out"] = FADE_OUT
    return document


def _blind(monkeypatch) -> None:
    """Its source's structure never read on a quad under the floor: the frame around it judges it, as on the render
    the defect was seen on."""
    original = insert_checks.structural_similarity

    def blind(seen, expected, mask):
        if insert_checks.detail_variance(seen, mask) < insert_checks.FLAT_VAR_MIN:
            return 0.0
        return original(seen, expected, mask)

    monkeypatch.setattr(insert_checks, "structural_similarity", blind)


def _left(frame: int) -> float:
    return insert_checks.video_fade_left(_document(), frame / FPS)


@functools.cache
def _whole() -> list[int]:
    render_ = PresenceRender(_document())
    return list(insert_checks.placed_inserts(render_.document, render_.inserts)[KEY].whole_frames())


def test_video_fade_left_follows_the_page_fade():
    document = {"duration": 10.0, "fade_in": 0.5, "fade_out": 1.0}
    assert insert_checks.video_fade_left(document, 0.0) == pytest.approx(0.0)
    assert insert_checks.video_fade_left(document, 0.25) == pytest.approx(0.5)
    assert insert_checks.video_fade_left(document, 5.0) == pytest.approx(1.0)
    assert insert_checks.video_fade_left(document, 9.5) == pytest.approx(0.5)
    assert insert_checks.video_fade_left(document, 10.0) == pytest.approx(0.0)
    assert insert_checks.video_fade_left({"duration": 4.0}, 3.99) == pytest.approx(1.0)


def test_an_insert_to_the_scene_s_end_under_the_video_s_fade_out_is_never_missing(monkeypatch):
    _blind(monkeypatch)
    gate = PresenceRender(_document()).gate()
    result = gate.result()
    assert [finding.message for finding, _ in result.findings] == []
    summary = result.by_key[KEY]
    faded = [n for n in _whole() if _left(n) < insert_checks.PRESENCE_MIN_SHOWN]
    assert faded and summary["faded"] == len(faded)
    assert summary["missing"] == 0
    # None of the frames the fade takes under the floor is read, present or missing.
    assert not set(faded) & set(gate.read_frames.get(KEY, []))


def test_the_report_says_the_faded_frames_as_a_fact(monkeypatch):
    _blind(monkeypatch)
    render_ = PresenceRender(_document())
    gate = render_.gate()
    result = gate.result()
    faded = result.by_key[KEY]["faded"]
    report = next(r for r in insert_checks.reports(render_.audits, gate.placed, fps=FPS, presence=result.by_key) if r["key"] == KEY)
    said = next(m.text for m in insert_checks.classify(report) if m.text.startswith("its source present on all"))
    assert f"; {faded} frame(s) the video's fade leaves under 65% of, not judged" in said


def test_an_insert_with_its_source_removed_where_the_fade_leaves_it_visible_is_missing():
    window = insert_checks.blend_windows(_document(), 0)[-1]
    assert window[2] == "video fade out"
    visible = [n for n in _whole() if n < window[0] - 2]
    assert visible
    result = PresenceRender(_document(), removed=set(visible)).gate().result()
    hits = [finding for finding, _proof in result.findings if finding.data["insert"] == KEY]
    assert hits
    (finding,) = hits
    assert finding.kind == INSERT_MISSING and finding.severity == FAIL
    assert finding.data["frame"] == visible[0]
