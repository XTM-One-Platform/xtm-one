"""Where a text-drift failure is (#10919).

* the reading lists each unreadable place with its box (a bare count still reads);
* a failed take holds one place per unreadable place its reason counts, with the frame,
  its time, its box, the start picture's text there and the take's;
* a place over the start picture's lettering is ``redrawn``, one where it has none is
  ``invented``, each with its own advice; a count the reading did not locate stays counted;
* ``run_clip`` crops each located place and keeps the crop with the take;
* the start picture read at plan time names the text regions the check holds the take to,
  and ``validate_only`` answers them.
"""

from __future__ import annotations

import json
import re

from app.services.video_studio import clips, frame_qc, text_drift

NAMES = ["start_picture", *text_drift.FRAMES]
HEIGHTS = {"start_picture": 900, "first_frame": 882, "middle_frame": 882, "last_frame": 882}
#: One crisp word on a wall, top left.
WALL = {"text": "Harbour", "confidence": 0.97, "height": 0.06, "box": [0.1, 0.1, 0.3, 0.06], "known": True}


def _reading(middle: object = 0, last: object = 0, start: object = 0) -> text_drift.Reading:
    answer = {
        "start_picture": {"words": [WALL], "unreadable": start},
        "first_frame": {"words": [WALL], "unreadable": 0},
        "middle_frame": {"words": [WALL], "unreadable": middle},
        "last_frame": {"words": [WALL], "unreadable": last},
    }
    reading = text_drift._parse(json.dumps(answer), NAMES)
    assert reading is not None
    return reading


def test_the_reading_lists_each_unreadable_place_and_a_bare_count_still_reads():
    reading = _reading(middle=[{"box": [0.6, 0.7, 0.1, 0.03], "text": "Xq7"}, {"text": "no box"}], last=2)
    assert reading.unreadable["middle_frame"] == 2 and reading.unreadable["last_frame"] == 2
    [place] = reading.places["middle_frame"]
    assert place.box == (0.6, 0.7, 0.1, 0.03) and place.take_text == "Xq7"
    assert "last_frame" not in reading.places


def test_a_failed_take_names_each_place_its_reason_counts():
    # The wall word redrawn unreadable in the middle frame, a tag the model invented in the last frame.
    reading = _reading(
        middle=[{"box": [0.12, 0.1, 0.28, 0.06], "text": "Hrbr"}],
        last=[{"box": [0.6, 0.7, 0.05, 0.03], "text": "8Kx"}],
    )
    verdict = text_drift.verdict(reading, HEIGHTS, brand=set(), duration=4.0)
    assert verdict.failed and verdict.unreadable == [("middle_frame", 1), ("last_frame", 1)]
    record = verdict.record()
    places = record["places"]
    counted = sum(int(n) for n in re.findall(r"(\d+) places? in the", verdict.reason))
    assert counted == len(places) == 2
    redrawn, invented = places
    assert redrawn == {
        "frame": "middle_frame",
        "at": 2.0,
        "box": [0.12, 0.1, 0.28, 0.06],
        "start_text": "Harbour",
        "take_text": "Hrbr",
        "origin": "redrawn",
        "crop_file_id": None,
    }
    assert invented["origin"] == "invented" and invented["start_text"] == "" and invented["at"] == 4.0
    assert "1 place redraw the start picture's own lettering (middle frame at 2.0 s, x 0.12-0.40" in verdict.reason
    assert "keep_regions over its box" in verdict.reason
    assert (
        "new lettering-like marks the model invented where the start picture has none (last frame at 4.0 s"
        in verdict.reason
    )
    assert "in the negative prompt" in verdict.reason


def test_the_start_pictures_own_unreadable_places_are_taken_last_and_an_unlocated_count_stays_counted():
    soft = {"box": [0.8, 0.05, 0.1, 0.02], "text": ""}
    new = {"box": [0.4, 0.5, 0.1, 0.03], "text": "zz"}
    # The start picture garbles one place already; the take garbles it again and adds one: the new one is named.
    reading = _reading(start=[soft], last=[soft, new])
    verdict = text_drift.verdict(reading, HEIGHTS, brand=set())
    [place] = verdict.places
    assert place.box == (0.4, 0.5, 0.1, 0.03) and place.origin == text_drift.INVENTED_MARKS
    # A bare count, nothing located: as many places as counted, unlocated, said so.
    counted = text_drift.verdict(_reading(last=2), HEIGHTS, brand=set())
    assert [p.origin for p in counted.places] == [text_drift.UNLOCATED] * 2
    assert "2 places the reading counted without locating" in counted.reason
    assert all(p["box"] is None for p in counted.record()["places"])


def test_run_clip_crops_each_located_place(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from unittest import mock

    from PIL import Image

    from app.services.video_studio import runners

    paths = {}
    for name in text_drift.FRAMES:
        path = tmp_path / f"{name}.jpg"
        Image.new("RGB", (640, 360), (20, 30, 40)).save(path, format="JPEG")
        paths[name] = str(path)
    verdict = text_drift.TextCheck(
        True,
        texts={"start_picture": "Harbour"},
        unreadable=[("last_frame", 2)],
        expected=["Harbour"],
        frame_paths=paths,
        places=[
            text_drift.Place("last_frame", (0.6, 0.7, 0.05, 0.03), "8Kx", "", text_drift.INVENTED_MARKS, 4.0),
            text_drift.Place("last_frame", at=4.0),
        ],
    )
    stored = []

    async def store_bytes(data, **kwargs):
        stored.append(kwargs["filename"])
        return SimpleNamespace(id=f"crop-{len(stored)}")

    params: dict = {}
    with mock.patch.object(runners.assets, "store_bytes", store_bytes):
        asyncio.run(runners._record_text_check(verdict, params, base="shot", user_id="u"))
    record = frame_qc.text_check(params[frame_qc.QC_KEY])
    assert [p["crop_file_id"] for p in record["places"]] == ["crop-1", None]
    assert stored == ["shot-unreadable-last-0.jpg"]
    assert "crop-1" in frame_qc.frame_file_ids(params)


def test_the_start_picture_names_the_text_regions_the_check_holds_the_take_to():
    reading = _reading(start=[{"box": [0.8, 0.05, 0.1, 0.02], "text": ""}])
    lettering = text_drift.start_lettering(reading, 900, brand=set())
    assert lettering.regions == [
        {"text": "Harbour", "box": [0.1, 0.1, 0.3, 0.06], "held": "compared"},
        {"text": "", "box": [0.8, 0.05, 0.1, 0.02], "held": "baseline"},
    ]
    assert text_drift.StartLettering.from_dict(lettering.as_dict()) == lettering
    params = {text_drift.START_LETTERING_KEY: lettering.as_dict()}
    assert clips.lettering_regions(params) == lettering.regions
    assert clips.lettering_regions({}) == []
