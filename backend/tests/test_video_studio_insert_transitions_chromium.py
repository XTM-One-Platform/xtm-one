"""A keyed screen insert through both joins of its scene, in a real render: the page, the frame server, the encoder.

``video_studio_render_fixture``'s green screen with its keyed video insert, between the recording and the statement it
crossfades with, the insert made to come in 0.4 s after its scene and to go 0.8 s before its end - its surface left
raw there, both transitions included, before the fix (render dad61b44 at 96.7 s and 113.0 s). Drawn by
``compositor.render_frames`` at 960x540 on two pages, its frames' audits read by the render's gates and its joins read
back off the encoded video by the render's guard (``insert_checks.dropped``):

* its layer is drawn on every frame of its scene, the surface alone in the bezel's colour before it comes in and after
  it goes - never a frame missing, never a residual key;
* the guard reads no key on any frame of either join, where the page placed the layer on each of them.

Runs with Playwright's Chromium and ffmpeg - a failure, not a skip, under ``REQUIRE_CHROMIUM=1``.
"""

from __future__ import annotations

import os

import pytest

from app.services.builtin_tools.html_render import font_face_css
from app.services.video_studio import compositor, insert_checks, timeline
from app.services.video_studio.render_checks import INSERT_FLICKER, INSERT_RESIDUAL_KEY
from app.services.video_studio.spec import QUALITY_PRESETS
from tests import video_studio_render_fixture as fixture

pytestmark = pytest.mark.chromium

SIZE = (960, 540)
FPS = 24
SCENES = ("f02-console", "f03-green", "f04-claim")


def _missing(what: str) -> None:
    if os.environ.get("REQUIRE_CHROMIUM") == "1":
        pytest.fail(f"{what} is required here and is missing")
    pytest.skip(f"{what} is missing")


async def test_a_keyed_insert_is_drawn_through_both_joins_of_its_scene_and_the_guard_reads_no_key(tmp_path):
    if not fixture.ffmpeg():
        _missing("ffmpeg")
    made = fixture.build(tmp_path, seconds=12, fps=FPS, scenes=SCENES)
    document = made.plan.document
    scene = next(s for s in document["scenes"] if s["id"] == "f03-green")
    [payload] = scene["inserts"]
    length = float(scene["end"]) - float(scene["start"])
    # Planned to run to its scene's end, it is made to go inside it: no longer one that runs to the end, whose
    # t1 hold_surfaces would move back to the scene's length.
    assert payload.pop("to_end", None) is True
    payload["t0"], payload["t1"] = 0.4, round(length - 0.8, 4)
    timeline.hold_surfaces(scene["inserts"], length)
    assert payload["held"] == [0.0, round(length, 4)]
    await fixture.matte(made, SIZE)
    key = payload["src"]
    placed = insert_checks.placed_inserts(document, made.media.assets.inserts)
    guard = insert_checks.guard_frames(placed)
    read = sorted(n for n, keys in guard.items() if key in keys)
    assert len(placed[key].joins) == 2 and len(read) == 2 * insert_checks.GUARD_FRAMES
    audits: dict = {}
    try:
        video = await compositor.render_frames(
            document,
            made.media.assets,
            preset=QUALITY_PRESETS["draft"],
            output_size=SIZE,
            workdir=str(tmp_path / "render"),
            font_css=font_face_css(),
            pages=2,
            audits=audits,
            guard=guard,
            presence=insert_checks.presence_frames(placed),
        )
    except Exception as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            _missing(f"Playwright's Chromium: {str(exc)[:200]}")
        raise
    frames = {int(n): audit for n, audit in audits[key]["frames"].items()}
    drawn = placed[key].expected_frames()
    # Its layer on every frame of its scene; before it comes in and after it goes, the surface alone.
    assert insert_checks.missing_frames(placed[key], frames) == []
    assert frames[drawn[0]]["visibility"] == 0.0 and frames[drawn[-1]]["visibility"] == 0.0
    assert max(float(frames[n].get("residual_key") or 0.0) for n in drawn) <= 0.003
    found = insert_checks.findings(audits, placed, fps=FPS)
    assert [f.message for f, _ in found if f.data.get("insert") == key and f.kind in (INSERT_RESIDUAL_KEY, INSERT_FLICKER)] == []
    # Where the page placed it on every frame the guard reads, and no key there on the encoded video.
    assert set(int(n) for n in audits[key]["placed"]) >= set(read)
    keys = {name: insert.key.key() for name, insert in made.media.assets.inserts.items() if insert.key is not None}
    # Its scene drawn alone on each of those frames - the recording's test pattern beside it, green bars and all, never
    # read as its key - and the encoded frame where it shows alone.
    assert set(int(n) for n in audits[key]["alone"]) >= set(read)
    assert await insert_checks.dropped(video, audits, placed, guard, document, keys, fps=FPS, workdir=tmp_path) == []
    # Its source read off every encoded frame it shows whole on: present on each, nothing painted past its end.
    present = await insert_checks.presence(video, audits, placed, document, made.media.assets.inserts, fps=FPS, size=SIZE, workdir=tmp_path)
    assert [f.message for f, _ in present.findings] == []
    assert present.by_key[key]["frames"] == len(placed[key].whole_frames()) and present.by_key[key]["missing"] == 0
    assert insert_checks.overruns(audits, placed, document, fps=FPS) == []
