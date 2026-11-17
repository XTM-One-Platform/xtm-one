"""The duplicate logo pass keeps its place: a relaunch goes on from there (issue #9404).

A high render relaunched by deploys at its post-draw checks read every footage frame for the brand's mark again from
the first, at each attempt, until its attempt budget was spent. ``render._duplicate_logos`` now keeps what it judged
(``render.DuplicateLogoPlace``) every few frames and at its end: a pass over the same video reading the same frames
goes on from the first frame not judged, keeps the findings already made and draws again the proof a worker gone took
with its disk.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from app.services.video_studio import render, render_checks
from tests.test_video_studio_duplicate_logo_overlay_8421 import FPS, _card, _document, _mark, _wordmark


def _setup(tmp_path: Path) -> tuple[SimpleNamespace, Path]:
    logo_path, mark_path = tmp_path / "logo.png", tmp_path / "mark.png"
    _wordmark().save(logo_path)
    _mark().save(mark_path)
    card = tmp_path / "card.png"
    _card(overlay=True, watermark=True).save(card)
    media = SimpleNamespace(assets=SimpleNamespace(images={"logo": str(logo_path), "mark": str(mark_path)}))
    return media, card


def _run(tmp_path: Path, monkeypatch, place, *, workdir: Path | None = None) -> tuple[list, list[int]]:
    media, card = _setup(tmp_path)
    asked: list[int] = []

    async def extract_frames(path, numbers, *, fps, out_dir):
        asked.extend(numbers)
        return dict.fromkeys(numbers, str(card))

    monkeypatch.setattr(render.encoder, "extract_frames", extract_frames)
    work = workdir or tmp_path
    work.mkdir(exist_ok=True)
    found = asyncio.run(
        render._duplicate_logos(
            SimpleNamespace(scenes=[]),
            _document(overlay=True),
            media,
            str(tmp_path / "render.mp4"),
            work,
            fps=FPS,
            span=None,
            scene_id=None,
            notes=[],
            place=place,
        )
    )
    return found, asked


def test_a_finished_pass_is_not_read_again_and_keeps_its_finding(tmp_path, monkeypatch):
    place = render.DuplicateLogoPlace(tmp_path / render.DUPLICATE_LOGO_FILE, "video-a")
    first, asked_first = _run(tmp_path, monkeypatch, place)
    assert asked_first and len(first) == 1
    again, asked_again = _run(tmp_path, monkeypatch, place)
    assert asked_again == [], "a pass already judged reads no footage frame again"
    assert len(again) == 1
    finding, proof = again[0]
    assert finding.kind == render_checks.DUPLICATE_LOGO and finding.scene_id == "s1"
    assert Path(proof).is_file()


def test_a_pass_cut_mid_way_goes_on_from_the_first_frame_not_judged(tmp_path, monkeypatch):
    place = render.DuplicateLogoPlace(tmp_path / render.DUPLICATE_LOGO_FILE, "video-a")
    _found, asked = _run(tmp_path, monkeypatch, place)
    frames = sorted(asked)
    assert len(frames) > 1
    kept = json.loads(place.path.read_text(encoding="utf-8"))
    # As if the worker stopped after the first frame: nothing judged after it is kept.
    kept.update(next=frames[1], said=[], shown=[], seen_mark=[], read_marked=[], footage_said=[], notes=[])
    place.path.write_text(json.dumps(kept), encoding="utf-8")
    found, asked_again = _run(tmp_path, monkeypatch, place)
    assert sorted(asked_again) == frames[1:]
    assert len(found) == 1


def test_another_video_reads_every_frame(tmp_path, monkeypatch):
    path = tmp_path / render.DUPLICATE_LOGO_FILE
    _found, asked_first = _run(tmp_path, monkeypatch, render.DuplicateLogoPlace(path, "video-a"))
    _found, asked_again = _run(tmp_path, monkeypatch, render.DuplicateLogoPlace(path, "video-b"))
    assert sorted(asked_again) == sorted(asked_first)


def test_a_proof_lost_with_the_worker_s_disk_is_drawn_again(tmp_path, monkeypatch):
    path = tmp_path / render.DUPLICATE_LOGO_FILE
    place = render.DuplicateLogoPlace(path, "video-a")
    first, _asked = _run(tmp_path, monkeypatch, place)
    Path(first[0][1]).unlink()
    again, asked_again = _run(tmp_path, monkeypatch, place)
    assert len(asked_again) == 1, "only the frame of the finding whose proof is gone is read again"
    assert len(again) == 1 and Path(again[0][1]).is_file()


def test_an_unreadable_place_is_a_pass_from_the_start(tmp_path, monkeypatch):
    path = tmp_path / render.DUPLICATE_LOGO_FILE
    path.write_text("{not json", encoding="utf-8")
    found, asked = _run(tmp_path, monkeypatch, render.DuplicateLogoPlace(path, "video-a"))
    assert asked and len(found) == 1


def test_the_place_is_stored_beside_the_render(tmp_path, monkeypatch):
    stored: list[Path] = []

    async def keep(path: Path) -> None:
        stored.append(path)

    place = render.DuplicateLogoPlace(tmp_path / render.DUPLICATE_LOGO_FILE, "video-a", keep)
    _run(tmp_path, monkeypatch, place)
    assert stored and stored[-1] == place.path


def test_the_render_keeps_the_pass_place_beside_itself():
    """The render hands the pass a place stored with the render's other check places (``keep_check_place``)."""
    source = Path(render.__file__).read_text(encoding="utf-8")
    call = source[source.index("twice = await _duplicate_logos(") :][:1200]
    assert "place=DuplicateLogoPlace(" in call
    assert "render_segments.keep_check_place" in call
    assert "restore_check_place(render_id, logo_place)" in source
