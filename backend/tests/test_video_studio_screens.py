"""Screen replacement: the flat key followed frame to frame and the real screenshot drawn into it."""

from __future__ import annotations

import asyncio
import io
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.llm.video import VideoResult
from app.models.video_studio import JOB_FAILED, JOB_PENDING, JOB_READY, VideoClip
from app.services.video_studio import (
    assets,
    clips,
    content_drift,
    encoder,
    jobs,
    pictures,
    runners,
    screens,
    text_drift,
)

USER = SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-00000000c001"), role="user")
ROOM_ID = "00000000-0000-0000-0000-0000000000c1"
SHOT_ID = "00000000-0000-0000-0000-0000000000c2"

GREEN = screens.Key("green", (0, 177, 64))
SIZE = (480, 270)
QUAD = ((150.0, 70.0), (340.0, 80.0), (335.0, 190.0), (145.0, 185.0))


def _room(quad=QUAD, *, key=GREEN, shade: float = 1.0, size=SIZE, occluder: tuple | None = None) -> np.ndarray:
    """A grey room with a flat key screen at *quad*, darkened by *shade*, maybe a dark bar in front of it."""
    picture = Image.new("RGB", size, (96, 92, 88))
    draw = ImageDraw.Draw(picture)
    draw.rectangle((0, 200, size[0], size[1]), fill=(60, 48, 40))
    draw.polygon([tuple(p) for p in quad], fill=tuple(round(c * shade) for c in key.rgb))
    if occluder:
        draw.rectangle(occluder, fill=(30, 26, 24))
    return np.asarray(picture).copy()


def _moved(quad, dx: float, dy: float, grow: float = 0.0):
    cx = sum(p[0] for p in quad) / 4
    cy = sum(p[1] for p in quad) / 4
    return tuple((x + dx + (x - cx) * grow, y + dy + (y - cy) * grow) for x, y in quad)


def _error(found, truth) -> float:
    return float(np.max(np.linalg.norm(np.asarray(found) - np.asarray(truth), axis=1)))


def test_a_screen_is_parsed_field_by_field():
    spec = screens.parse_screen({"file_id": "3f0c3b4e-7a52-4a7e-9a3f-1d2b0f4c5e6a", "quad": [list(p) for p in QUAD]})
    # The screenshot whole, by its content box, letterboxed in its own background: cover cut product UIs.
    assert spec.fit == "contain" and spec.light == screens.DEFAULT_LIGHT and spec.quad == QUAD
    assert screens.parse_screen({"file_id": "f", "quad": [list(p) for p in QUAD], "fit": "cover"}).fit == "cover"
    with pytest.raises(screens.ScreenError, match=r"screen\.file_id"):
        screens.parse_screen({"quad": [list(p) for p in QUAD]})
    with pytest.raises(screens.ScreenError, match="four corners"):
        screens.parse_screen({"file_id": "f", "quad": [[0, 0], [1, 1]]})
    with pytest.raises(screens.ScreenError, match="convex"):
        screens.parse_screen({"file_id": "f", "quad": [[0, 0], [10, 10], [10, 0], [0, 10]]})
    with pytest.raises(screens.ScreenError, match=r"screen\.fit"):
        screens.parse_screen({"file_id": "f", "quad": [list(p) for p in QUAD], "fit": "fill"})
    with pytest.raises(screens.ScreenError, match=r"screen\.light"):
        screens.parse_screen({"file_id": "f", "quad": [list(p) for p in QUAD], "light": 2})


def test_a_quad_must_be_inside_the_start_picture_and_hold_a_screen():
    spec = screens.parse_screen({"file_id": "f", "quad": [list(p) for p in QUAD]})
    screens.check_fits(spec, SIZE)
    with pytest.raises(screens.ScreenError, match="outside the start picture"):
        screens.check_fits(spec, (100, 60))
    tiny = screens.parse_screen({"file_id": "f", "quad": [[10, 10], [14, 10], [14, 13], [10, 13]]})
    with pytest.raises(screens.ScreenError, match="almost nothing"):
        screens.check_fits(tiny, SIZE)


def test_the_key_is_the_colour_the_picture_holds_least():
    plain = Image.fromarray(_room())
    assert screens.pick_key(plain, QUAD).name == "green"
    foliage = Image.new("RGB", SIZE, (20, 160, 50))
    assert screens.pick_key(foliage, QUAD).name != "green"


def test_the_placeholder_fills_the_quad_with_the_key_whatever_the_size_sent():
    base = Image.new("RGB", (960, 540), (90, 90, 90))
    out = io.BytesIO()
    base.save(out, format="PNG")
    spec = screens.ScreenSpec("f", tuple((x * 2, y * 2) for x, y in QUAD), key=GREEN)
    # The start picture was made half the size the quad was given in.
    half = io.BytesIO()
    base.resize(SIZE).save(half, format="PNG")
    drawn = np.asarray(Image.open(io.BytesIO(screens.placeholder(half.getvalue(), spec, (960, 540)))))
    assert tuple(drawn[130, 240]) == GREEN.rgb and tuple(drawn[20, 20]) == (90, 90, 90)


def test_the_prompt_asks_for_the_flat_key_and_the_negative_keeps_what_was_asked():
    assert "flat, uniform, evenly lit pure green surface" in screens.provider_prompt("A slow push-in on a desk.", GREEN)
    assert screens.provider_negative("blur").startswith("blur, text on the screen")
    assert screens.provider_negative(None).startswith("text on the screen")


def test_the_tracker_follows_a_moving_shaded_screen():
    tracker = screens.Tracker(GREEN, QUAD, SIZE)
    for step in range(12):
        truth = _moved(QUAD, 1.5 * step, -0.8 * step, grow=0.01 * step)
        corners, found = tracker.track(_room(truth, shade=1.0 - 0.03 * step))
        assert found
        assert _error(corners, truth) < 2.0, step


def test_a_hand_across_an_edge_moves_no_corner():
    tracker = screens.Tracker(GREEN, QUAD, SIZE)
    corners, found = tracker.track(_room(occluder=(220, 160, 260, 230)))
    assert found and _error(corners, QUAD) < 2.5


def test_a_frame_without_the_screen_keeps_the_last_corners():
    tracker = screens.Tracker(GREEN, QUAD, SIZE)
    tracker.track(_room())
    corners, found = tracker.track(np.full((SIZE[1], SIZE[0], 3), 90, dtype=np.uint8))
    assert not found and _error(corners, QUAD) < 2.5


def test_lost_frames_are_taken_between_their_neighbours():
    a = np.array(QUAD)
    b = a + 10
    track = screens.smooth([a, a, b], [True, False, True], window=1)
    assert np.allclose(track[1], a + 5)


def test_a_shot_that_lost_its_screen_is_refused():
    screens.judge([True] * 30, 24, GREEN)
    with pytest.raises(screens.ScreenLostError, match="not in the first"):
        screens.judge([False] + [True] * 29, 24, GREEN)
    with pytest.raises(screens.ScreenLostError, match="found in 20 of 30"):
        screens.judge([True] * 20 + [False] * 10, 24, GREEN)
    with pytest.raises(screens.ScreenLostError, match="flat green surface"):
        screens.judge([True] * 10 + [False] * 14 + [True] * 76, 24, GREEN)


def test_the_screenshot_is_drawn_into_the_screen_under_what_stands_in_front():
    frame = _room(occluder=(230, 150, 250, 230))
    screenshot = Image.new("RGBA", (190, 110), (250, 250, 250, 255))
    ImageDraw.Draw(screenshot).rectangle((20, 20, 60, 40), fill=(200, 20, 20, 255))
    out, _iou = screens.composite(frame, screenshot, np.array(QUAD), GREEN, light=0.0)
    # Inside the screen: the screenshot (white, its red box near its top-left corner).
    assert out[120, 300].tolist() == [250, 250, 250]
    assert out[105, 185][0] > 150 and out[105, 185][1] < 80
    # In front of it: the dark bar the model drew; around it: the room, untouched.
    assert out[170, 240].max() < 60
    assert out[30, 30].tolist() == frame[30, 30].tolist()
    # No key colour survives anywhere in the screen.
    assert (screens.key_distance(out[80:180, 160:330], GREEN) < screens.KEY_IN).sum() == 0


def _shot(tmp_path, frames: list[np.ndarray], fps: int = 24) -> str:
    names = []
    for index, frame in enumerate(frames):
        name = f"f{index:03d}.png"
        Image.fromarray(frame).save(tmp_path / name)
        names.append((index / fps, name))
    out = str(tmp_path / "shot.mp4")
    asyncio.run(
        encoder.frames_to_mezzanine(names, end_time=len(frames) / fps, out_path=out, fps=fps, crf=12, preset="ultrafast", workdir=str(tmp_path))
    )
    return out


@pytest.mark.skipif(not encoder.encoder_available(), reason="ffmpeg is not installed")
def test_a_shot_has_the_screenshot_drawn_into_every_frame(tmp_path):
    truths = [_moved(QUAD, 1.2 * i, 0.6 * i) for i in range(24)]
    video = _shot(tmp_path, [_room(t) for t in truths])
    screenshot = Image.new("RGBA", (380, 220), (245, 245, 245, 255))
    spec = screens.ScreenSpec("f", QUAD, light=0.0, key=GREEN)
    out = str(tmp_path / "replaced.mp4")
    done = asyncio.run(screens.replace(video, out, screenshot, spec, start_size=SIZE))
    assert done.frames >= 24 and done.found == done.frames and "text is the screenshot's own" in done.notes[0]
    frames = asyncio.run(_decoded(out))
    assert len(frames) == done.frames
    for index in (0, 12, 23):
        cx = round(sum(p[0] for p in truths[index]) / 4)
        cy = round(sum(p[1] for p in truths[index]) / 4)
        assert frames[index][cy, cx].min() > 225, index


@pytest.mark.skipif(not encoder.encoder_available(), reason="ffmpeg is not installed")
def test_a_shot_whose_model_drew_on_the_screen_is_screen_lost(tmp_path):
    frames = [_room() for _ in range(4)] + [_room(key=screens.Key("grey", (128, 128, 128))) for _ in range(20)]
    video = _shot(tmp_path, frames)
    spec = screens.ScreenSpec("f", QUAD, key=GREEN)
    with pytest.raises(screens.ScreenLostError, match=r"found in 4 of 2\d frames"):
        asyncio.run(screens.replace(video, str(tmp_path / "out.mp4"), Image.new("RGBA", (10, 10)), spec, start_size=SIZE))


@pytest.fixture
def readable(monkeypatch):
    """``pictures.read_picture`` answering a 16:9 room and a screenshot; anything else is unreadable."""
    sizes = {ROOM_ID: (960, 540), SHOT_ID: (1600, 1000)}

    async def read_picture(_db, _user, file_id, *, what):
        if file_id not in sizes:
            raise pictures.PictureError(f"No picture `{file_id}` you can read for {what}.")
        return Image.new("RGBA", sizes[file_id], (90, 90, 90, 255))

    monkeypatch.setattr(pictures, "read_picture", read_picture)
    return sizes


def _resolve(raw, *, reference=None, aspect_ratio="16:9"):
    return asyncio.run(
        clips.resolve_screen(None, USER, raw, reference=reference or {"reference_file_id": ROOM_ID}, aspect_ratio=aspect_ratio)
    )


def test_a_screen_is_resolved_before_anything_is_paid(readable):
    quad = [[x * 2, y * 2] for x, y in QUAD]
    stored = _resolve({"file_id": SHOT_ID, "quad": quad, "light": 0.4})
    assert stored["file_id"] == SHOT_ID and stored["start_size"] == [960, 540] and stored["light"] == 0.4
    assert stored["key"] == {"name": "green", "hex": "#00b140"}
    assert screens.ScreenSpec.from_params(stored).key == GREEN


def test_a_screen_without_a_picture_of_the_room_or_of_another_shape_is_refused(readable):
    quad = [[x * 2, y * 2] for x, y in QUAD]
    with pytest.raises(clips.ClipError, match="starts from a picture of the room"):
        _resolve({"file_id": SHOT_ID, "quad": quad}, reference={"reference_video": {"file_id": "v"}})
    with pytest.raises(clips.ClipError, match="not 9:16 like the shot"):
        _resolve({"file_id": SHOT_ID, "quad": quad}, aspect_ratio="9:16")
    with pytest.raises(clips.ClipError, match="for the screenshot"):
        _resolve({"file_id": "00000000-0000-0000-0000-0000000000ff", "quad": quad})
    with pytest.raises(clips.ClipError, match="outside the start picture"):
        _resolve({"file_id": SHOT_ID, "quad": [[x * 9, y * 9] for x, y in QUAD]})


def test_a_validated_screen_shot_says_its_screen(readable):
    stored = _resolve({"file_id": SHOT_ID, "quad": [[x * 2, y * 2] for x, y in QUAD]})
    request = clips.build_request(model="veo", prompt="A laptop on a desk")
    plan = clips.ClipPlan(
        request=request,
        config=SimpleNamespace(provider="gemini", display_name="Gemini"),
        params={"reference_file_id": ROOM_ID, clips.SCREEN: stored},
    )
    assert plan.summary()["screen"] == {
        "file_id": SHOT_ID,
        "key": "green",
        "quad": [[x * 2, y * 2] for x, y in QUAD],
        "found": False,
    }
    plain = clips.ClipPlan(request=request, config=SimpleNamespace(provider="gemini", display_name="Gemini"), params={})
    assert plain.summary()["screen"] is None


# ── The screen found in the start picture ─────────────────────────


def _picture(quad=QUAD, colour=(0, 255, 0), *, plant: bool = False, scale: int = 1) -> Image.Image:
    """A grey room whose screen is one solid *colour* at *quad*, maybe with a green plant beside it, *scale* times larger."""
    picture = Image.new("RGB", (SIZE[0] * scale, SIZE[1] * scale), (96, 92, 88))
    draw = ImageDraw.Draw(picture)
    draw.rectangle((0, 200 * scale, SIZE[0] * scale, SIZE[1] * scale), fill=(60, 48, 40))
    if plant:
        draw.ellipse((350 * scale, 20 * scale, 470 * scale, 250 * scale), fill=(30, 150, 40))
    draw.polygon([(x * scale, y * scale) for x, y in quad], fill=colour)
    return picture


def test_a_screen_may_leave_its_quad_to_be_found():
    spec = screens.parse_screen({"file_id": "f", "light": 0.2})
    assert (spec.quad, spec.light, spec.found) == ((), 0.2, False)
    found = screens.ScreenSpec("f", QUAD, key=GREEN, found=True)
    assert screens.ScreenSpec.from_params(found.as_params()) == found


@pytest.mark.parametrize("colour", [(0, 177, 64), (0, 255, 0), (0, 140, 50), (20, 40, 250), (230, 0, 210)])
def test_a_solid_key_screen_is_found_to_the_pixel(colour):
    assert _error(screens.find_screen(_picture(colour=colour)), QUAD) < 2.0
    trapezoid = ((120.0, 60.0), (360.0, 95.0), (360.0, 175.0), (120.0, 215.0))
    assert _error(screens.find_screen(_picture(trapezoid, colour)), trapezoid) < 2.0


def test_the_screen_is_found_in_the_pixels_of_a_large_picture_beside_a_green_plant():
    assert _error(screens.find_screen(_picture(plant=True)), QUAD) < 2.0
    assert _error(screens.find_screen(_picture(scale=6)), [(x * 6, y * 6) for x, y in QUAD]) < 4.0


def test_no_screen_is_found_where_no_flat_quad_of_a_key_colour_is():
    with pytest.raises(screens.ScreenError, match=r"No screen found in the start picture: give screen\.quad"):
        screens.find_screen(Image.new("RGB", SIZE, (96, 92, 88)))
    blob = Image.new("RGB", SIZE, (96, 92, 88))
    ImageDraw.Draw(blob).ellipse((100, 40, 380, 230), fill=(0, 220, 60))
    with pytest.raises(screens.ScreenError, match="No screen found"):
        screens.find_screen(blob)


@pytest.fixture
def drawn_room(monkeypatch):
    """``pictures.read_picture`` answering the room pictures the test puts in, and a screenshot."""
    rooms: dict[str, Image.Image] = {}

    async def read_picture(_db, _user, file_id, *, what):
        if file_id == SHOT_ID:
            return Image.new("RGBA", (1600, 1000), (250, 250, 250, 255))
        if file_id not in rooms:
            raise pictures.PictureError(f"No picture `{file_id}` you can read for {what}.")
        return rooms[file_id]

    monkeypatch.setattr(pictures, "read_picture", read_picture)
    return rooms


def test_a_screen_without_a_quad_is_resolved_with_the_one_found(drawn_room):
    drawn_room[ROOM_ID] = _picture(scale=2)
    stored = _resolve({"file_id": SHOT_ID})
    assert stored["found"] is True and stored["start_size"] == [960, 540]
    assert _error(stored["quad"], [(x * 2, y * 2) for x, y in QUAD]) < 3.0
    # The screen was green in the picture; the flat colour sent is whichever the room holds least.
    assert stored["key"]["name"] in {"green", "blue", "magenta"}
    drawn_room[ROOM_ID] = Image.new("RGB", (960, 540), (96, 92, 88))
    with pytest.raises(clips.ClipError, match="No screen found in the start picture"):
        _resolve({"file_id": SHOT_ID})


def test_a_found_quad_is_found_again_in_another_start_picture_and_a_given_one_kept(drawn_room):
    drawn_room[ROOM_ID] = _picture(scale=2)
    found = _resolve({"file_id": SHOT_ID})
    moved = tuple((x + 30, y + 10) for x, y in QUAD)
    drawn_room[ROOM_ID] = _picture(moved, scale=2)
    again = _resolve(clips.screen_again(found))
    assert again["found"] is True and _error(again["quad"], [(x * 2, y * 2) for x, y in moved]) < 3.0
    given = _resolve({"file_id": SHOT_ID, "quad": [[x * 2, y * 2] for x, y in QUAD]})
    assert clips.screen_again(given) is given


@pytest.fixture
def made(monkeypatch):
    """``run_clip`` on a screen shot whose video the provider made; the replacement and the checks are stubbed."""
    screen = {
        **screens.ScreenSpec(SHOT_ID, QUAD, key=GREEN).as_params(),
        "start_size": list(SIZE),
    }
    row = VideoClip(
        id=uuid.uuid4(),
        user_id=USER.id,
        provider="gemini",
        model="veo-3.1-generate-preview",
        prompt="A slow push-in on a laptop on a desk at dusk",
        params={"provider_id": str(uuid.uuid4()), "duration_seconds": 8, "reference_file_id": ROOM_ID, clips.SCREEN: screen},
        status=JOB_PENDING,
    )
    world = SimpleNamespace(
        row=row, asked=None, stored=[], deleted=[], lost=None, checked=None, text_checked=False, measured=None
    )

    class _Db:
        async def get(self, _model, _key):
            return row

        async def commit(self):
            return None

    @asynccontextmanager
    async def session():
        yield _Db()

    class _Provider:
        async def generate_video(self, request, **_kwargs):
            world.asked = request
            return VideoResult(data=b"RAW", job_id="op/1")

        def max_reference_images(self, _model):
            return 0

        def reference_images_with_start_frame(self, _model):
            return False

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return _Provider(), model, SimpleNamespace(id=provider_id, provider="gemini")

    async def mark(_kind, _job_id, **changes):
        for key, value in changes.items():
            setattr(row, key, value)

    async def store_path(path, **_kwargs):
        world.stored.append(Path(path).read_bytes())
        return SimpleNamespace(id=uuid.uuid4())

    async def delete_files(ids):
        world.deleted.extend(i for i in ids or [] if i)

    async def probe(_path):
        return SimpleNamespace(duration=8.0, width=1920, height=1080, has_audio=False)

    async def poster(_path, *, at, out_path, width):
        Path(out_path).write_bytes(b"JPG")

    flat = io.BytesIO()
    Image.fromarray(_room()).save(flat, format="PNG")

    async def start_frame(_params, _workdir, **_kwargs):
        return clips.StartFrame(flat.getvalue(), "image/png", ROOM_ID)

    async def screenshot(_screen, _workdir):
        return Image.new("RGBA", (190, 110), (120, 120, 120, 255))

    async def replaced(path, _screen, _image, _stored, _workdir):
        if world.lost:
            raise screens.ScreenLostError(world.lost)
        Path(path).write_bytes(b"COMPOSITE")
        return screens.Replaced(192, 192, ["The screen was drawn."])

    async def keep_frames(_clip_id, _path, _duration, _workdir, picture, **_kwargs):
        world.measured = picture
        return {}

    async def content_check(*_args, **kwargs):
        world.checked = kwargs
        return content_drift.ContentCheck(True)

    async def text_check(*_args, **_kwargs):
        world.text_checked = True
        return text_drift.TextCheck(True)

    async def nothing(*_args, **_kwargs):
        return []

    monkeypatch.setattr(runners, "async_session", session)
    monkeypatch.setattr(runners, "bind_llm_caller", nothing)
    monkeypatch.setattr(runners, "_screenshot_of", screenshot)
    monkeypatch.setattr(runners, "_screen_replaced", replaced)
    monkeypatch.setattr(runners, "_keep_frames", keep_frames)
    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(clips, "start_frame", start_frame)
    monkeypatch.setattr(clips, "reference_pictures", nothing)
    monkeypatch.setattr(jobs, "mark", mark)
    monkeypatch.setattr(assets, "store_path", store_path)
    monkeypatch.setattr(assets, "delete_files", delete_files)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_frame", poster)
    monkeypatch.setattr(content_drift, "check", content_check)
    monkeypatch.setattr(text_drift, "check", text_check)
    return world


def test_a_screen_shot_is_asked_flat_then_has_the_screenshot_drawn_in(made):
    asyncio.run(runners.run_clip(str(made.row.id)))
    row = made.row
    assert row.status == JOB_READY
    assert "pure green surface" in made.asked.prompt and "text on the screen" in made.asked.negative_prompt
    # The raw take is stored first, then the composite replaces it on the row.
    assert made.stored[0] == b"RAW" and b"COMPOSITE" in made.stored
    assert row.params[clips.SCREEN]["replaced_file_id"] == str(row.file_id)
    assert "The screen was drawn." in row.params["notes"]
    # Its frames are read against the prompt as asked, never the flat-screen wording; its text is exact.
    assert made.checked["prompt"] == "A slow push-in on a laptop on a desk at dusk" and not made.text_checked
    # Its first frame is measured against the start with the screenshot in it, never the flat key.
    measured = np.asarray(Image.open(io.BytesIO(made.measured.data)).convert("RGB"))
    assert measured.shape[:2] == (SIZE[1], SIZE[0]) and abs(int(measured[130, 240].mean()) - 120) <= 3


def test_a_screen_shot_whose_screen_was_lost_fails_and_keeps_the_raw_take(made):
    made.lost = "screen lost: the model did not keep the screen a flat green surface (found in 3 of 192 frames)."
    asyncio.run(runners.run_clip(str(made.row.id)))
    row = made.row
    assert row.status == JOB_FAILED and row.error.startswith("screen lost:")
    assert row.file_id is not None and made.stored == [b"RAW", b"JPG"] and made.checked is None


async def _decoded(path: str) -> list[np.ndarray]:
    info = await encoder.probe(path)
    return [
        np.frombuffer(raw, dtype=np.uint8).reshape(info.height, info.width, 3)
        async for raw in encoder.iter_rgb_frames(path, width=info.width, height=info.height)
    ]


def test_the_light_drawn_on_the_screen_plays_over_the_screenshot():
    frame = _room()
    # A reflection: a brighter band across the flat surface.
    frame[100:115, 160:330] = np.clip(np.asarray(GREEN.rgb) * 1.35, 0, 255).astype(np.uint8)
    screenshot = Image.new("RGBA", (190, 110), (120, 120, 120, 255))
    lit, _iou = screens.composite(frame, screenshot, np.array(QUAD), GREEN, light=1.0)
    flat, _iou = screens.composite(frame, screenshot, np.array(QUAD), GREEN, light=0.0)
    assert lit[107, 240].mean() > flat[107, 240].mean() + 10
    assert abs(int(lit[150, 240].mean()) - int(flat[150, 240].mean())) <= 3
