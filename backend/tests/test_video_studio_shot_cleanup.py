"""What the platform draws over an AI shot after it is made, following its camera.

Synthetic shots, each built so that it needs the mechanism its test names:

* the camera's motion (``motion.align``): a known zoom, rotation and shift
  recovered to a fraction of a pixel, with a masked region full of noise and an
  exposure drift; an unrelated picture is not "found";
* stray screens (``stray_screens``): an office whose monitor shows an
  interface the negative prompt excluded, the camera pushing in, the screen
  located a few percent off as a model locates it - flattened in every frame,
  the room untouched; drawn with a screenshot instead; refused when something
  passes in front of it; a screen a pan carries out of the frame judged on
  what the frame still shows of it, refused when it shows in too few frames,
  and a term listed once per screen keeps every screen;
* kept regions (``kept_regions``): a photo whose banner the model melted into
  noise, the camera pushing in with an exposure drift - the still's banner is
  drawn back where the camera takes it; a shot that does not open on its
  still is refused as "kept region lost";
* the runner: a take whose only failing findings are on located screens is
  cleaned, read again and kept; one showing the term elsewhere fails as
  before; a take cleaned by a run that died is not cleaned twice; kept regions
  are drawn back before the text check reads the take.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw
from scipy import ndimage

from app.llm.video import VideoResult
from app.models.video_studio import JOB_FAILED, JOB_PENDING, JOB_READY, VideoClip
from app.services.video_studio import (
    assets,
    clips,
    content_drift,
    encoder,
    jobs,
    kept_regions,
    motion,
    runners,
    stray_screens,
    text_drift,
)

W, H, FRAMES, FPS = 320, 180, 24, 24
USER_ID = uuid.UUID("00000000-0000-0000-0000-00000000d001")
PICTURE_ID = "00000000-0000-0000-0000-0000000000d1"


def _texture(seed: int, size=(W, H), sigma: float = 3.0) -> np.ndarray:
    """A room-like picture: structure at several scales (walls, furniture, detail), mostly in its brightness."""
    rng = np.random.default_rng(seed)
    light = sum(
        ndimage.gaussian_filter(rng.random((size[1], size[0])), scale) * weight
        for scale, weight in ((sigma, 1.0), (sigma * 4, 3.0), (sigma * 12, 6.0))
    )
    tint = ndimage.gaussian_filter(rng.random((size[1], size[0], 3)), (sigma * 4, sigma * 4, 0))
    picture = light[..., None] * 3 + tint
    return ((picture - picture.min()) / (picture.max() - picture.min()) * 190 + 30).astype(np.uint8)


def _push(t: int, *, zoom: float = 0.05, dx: float = 4.0, dy: float = 2.0) -> motion.Similarity:
    """A slow push-in with a drift: the camera at frame *t* of the shot (the mezzanine holds its last one)."""
    share = min(t, FRAMES - 1) / (FRAMES - 1)
    scale = 1 + zoom * share
    centre = np.array([W / 2, H / 2])
    shift = centre - scale * centre + np.array([dx * share, dy * share])
    return motion.Similarity(scale, 0.0, float(shift[0]), float(shift[1]))


def _shot(picture: Image.Image, t: int, **move) -> np.ndarray:
    return np.asarray(
        picture.transform((W, H), Image.Transform.AFFINE, _push(t, **move).coefficients(), resample=Image.Resampling.BICUBIC)
    ).copy()


def _encode(path: Path, frames: list[np.ndarray]) -> str:
    """*frames* as a 24 fps H.264 shot at *path*, through the encoder's own mezzanine."""
    workdir = path.parent / f"{path.stem}-frames"
    workdir.mkdir()
    names = []
    for index, frame in enumerate(frames):
        name = f"f{index:03d}.png"
        Image.fromarray(np.asarray(frame, dtype=np.uint8)).save(workdir / name)
        names.append((index / FPS, name))
    asyncio.run(
        encoder.frames_to_mezzanine(
            names, end_time=len(frames) / FPS, out_path=str(path), fps=FPS, crf=12, preset="ultrafast", workdir=str(workdir)
        )
    )
    return str(path)


async def _decoded(path: str) -> list[np.ndarray]:
    return [
        np.frombuffer(raw, dtype=np.uint8).reshape(H, W, 3).astype(np.float32)
        async for raw in encoder.iter_rgb_frames(path, width=W, height=H)
    ]


needs_ffmpeg = pytest.mark.skipif(not encoder.encoder_available(), reason="ffmpeg is not installed")


# ── The camera's motion ───────────────────────────────────────────────────


def test_a_known_camera_move_is_measured_to_a_fraction_of_a_pixel_around_what_is_masked():
    base = Image.fromarray(_texture(3, (640, 360)))
    truth = motion.Similarity(1.06 * np.cos(0.02), 1.06 * np.sin(0.02), -25.0, 12.0)
    frame = np.asarray(base.transform((640, 360), Image.Transform.AFFINE, truth.coefficients()), dtype=np.float32)
    frame = frame * 0.85 + 18  # the model's exposure drifts
    frame[100:200, 300:600] = np.random.default_rng(1).random((100, 300, 3)) * 255  # what it redrew
    reference, factor = motion.grey(base)
    image, _ = motion.grey(np.clip(frame, 0, 255).astype(np.uint8))
    weight = np.ones_like(reference)
    weight[int(100 * factor) : int(200 * factor) + 2, int(300 * factor) : int(600 * factor) + 2] = 0
    fit = motion.align(reference, image, weight=weight)
    points = np.array([[60.0, 40.0], [580.0, 320.0]])
    assert fit.found and fit.score > 0.9
    assert np.abs(fit.motion.scaled(1 / factor).apply(points) - truth.apply(points)).max() < 0.5
    other, _ = motion.grey(_texture(9, (640, 360)))
    assert not motion.align(reference, other).found


def test_a_similarity_composes_inverts_and_maps_to_pillow():
    move = motion.Similarity(1.1, 0.05, 3.0, -2.0)
    points = np.array([[10.0, 20.0], [200.0, 90.0]])
    assert np.allclose(move.then(move.inverse()).apply(points), points)
    assert np.allclose(move.scaled(2.0).apply(points * 2), move.apply(points) * 2)
    smoothed = motion.smooth([move, motion.Similarity(), move], [True, False, True])
    assert np.allclose(smoothed[1].apply(points), move.apply(points))
    assert motion.longest_run([True, False, False, True, False]) == 2


# ── Stray screens ─────────────────────────────────────────────────────────

SCREEN = ((125.0, 55.0), (215.0, 60.0), (214.0, 114.0), (126.0, 111.0))
#: A move the screen's edges alone cannot follow: the screen travels well past where its sides are looked for.
OFFICE_MOVE = {"zoom": 0.15, "dx": 24.0, "dy": 6.0}


def _office(*, interface: bool = True) -> Image.Image:
    room = Image.fromarray(_texture(11, sigma=2.0))
    draw = ImageDraw.Draw(room)
    draw.polygon([(122, 52), (218, 57), (217, 117), (123, 114)], fill=(18, 18, 20))  # the bezel
    draw.polygon(SCREEN, fill=(236, 238, 242) if interface else (20, 20, 24))
    if interface:
        rng = np.random.default_rng(5)
        for row in range(7):
            y = 64 + row * 6
            draw.line([(132, y), (132 + int(rng.integers(30, 70)), y)], fill=(30, 30, 40), width=1)
        draw.rectangle((190, 66, 208, 80), fill=(30, 120, 220))
    return room


def _office_shot(path: Path, *, crossing: bool = False) -> str:
    room, frames = _office(), []
    for t in range(FRAMES):
        frame = Image.fromarray(_shot(room, t, **OFFICE_MOVE))
        if crossing and 6 <= t < 16:
            ImageDraw.Draw(frame).ellipse((120 + t * 4, 45, 160 + t * 4, 130), fill=(150, 90, 70))
        frames.append(np.asarray(frame))
    return _encode(path, frames)


def _located(error=((-0.01, 0.012), (0.012, -0.01), (0.008, 0.01), (-0.012, -0.01))) -> list:
    """The screen as a frames reading locates it: a few percent off its real corners."""
    corners = [(x / W + dx, y / H + dy) for (x, y), (dx, dy) in zip(SCREEN, error, strict=True)]
    return [stray_screens.Sighting("first_frame", stray_screens.parse_corners(corners))]


def _detail(frame: np.ndarray, t: int) -> float:
    (x0, y0), (x1, y1) = _push(t, **OFFICE_MOVE).apply([(133, 62), (208, 106)]).astype(int)
    return float(np.abs(ndimage.laplace(frame[y0:y1, x0:x1].mean(axis=2))).mean())


@needs_ffmpeg
def test_a_stray_screen_is_flattened_in_every_frame_following_the_camera(tmp_path):
    shot = _office_shot(tmp_path / "office.mp4")
    cleaned = asyncio.run(stray_screens.clean(shot, str(tmp_path / "out.mp4"), _located(), {"mode": "flatten"}))
    before, after = asyncio.run(_decoded(shot)), asyncio.run(_decoded(str(tmp_path / "out.mp4")))
    assert (cleaned.screens, cleaned.frames, cleaned.followed) == (1, len(before), len(before)) == (1, len(after), len(after))
    assert _detail(before[0], 0) > 20
    assert max(_detail(frame, t) for t, frame in enumerate(after)) < 2.0
    # The room is the shot's own: only the screen was drawn.
    for t in (0, FRAMES - 1):
        (x0, y0), (x1, y1) = _push(t, **OFFICE_MOVE).apply([(110, 40), (230, 128)]).astype(int)
        keep = np.ones((H, W), dtype=bool)
        keep[y0:y1, x0:x1] = False
        assert np.abs(after[t][keep] - before[t][keep]).mean() < 3.0
    # Each read frame's cleaned screen, refined onto its edges: what a second reading is matched against.
    first = np.asarray(cleaned.at_read_frames["first_frame"][0]) * [W, H]
    assert np.abs(first - np.asarray(SCREEN)).max() < 6.0


@needs_ffmpeg
def test_a_stray_screen_is_drawn_with_the_screenshot_when_one_is_given(tmp_path):
    shot = _office_shot(tmp_path / "office.mp4")
    screenshot = Image.new("RGB", (160, 90), (200, 40, 160))
    option = {"mode": "screenshot", "file_id": PICTURE_ID, "fit": "cover", "light": 0.0}
    asyncio.run(stray_screens.clean(shot, str(tmp_path / "out.mp4"), _located(), option, screenshot))
    after = asyncio.run(_decoded(str(tmp_path / "out.mp4")))
    for t in (0, FRAMES - 1):
        (x0, y0), (x1, y1) = _push(t, **OFFICE_MOVE).apply([(140, 70), (200, 100)]).astype(int)
        colour = after[t][y0:y1, x0:x1].reshape(-1, 3).mean(axis=0)
        assert np.abs(colour - (200, 40, 160)).max() < 12


@needs_ffmpeg
def test_a_screen_something_passes_in_front_of_is_refused_rather_than_drawn_over(tmp_path):
    shot = _office_shot(tmp_path / "crossing.mp4", crossing=True)
    with pytest.raises(stray_screens.CleanupError, match="passes in front of a stray screen"):
        asyncio.run(stray_screens.clean(shot, str(tmp_path / "out.mp4"), _located(), {"mode": "flatten"}))


#: A room twice the frame's width, a screen in it, and a pan carrying the screen out of the frame's right edge.
ROOM_SCREEN = (380, 60, 470, 120)


def _wide_room() -> np.ndarray:
    room = Image.fromarray(_texture(17, (2 * W, H), sigma=2.0))
    draw = ImageDraw.Draw(room)
    x0, y0, x1, y1 = ROOM_SCREEN
    draw.rectangle((x0, y0, x1, y1), fill=(230, 232, 236))
    rng = np.random.default_rng(8)
    for row in range(8):
        draw.line([(x0 + 6, y0 + 6 + row * 6), (x0 + 6 + int(rng.integers(20, 70)), y0 + 6 + row * 6)], fill=(30, 30, 40))
    return np.asarray(room)


def _pan(offsets: list[int], *, crossing: tuple[int, ...] = ()) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Each frame of a pan (the room seen from *offsets*), as greys, and the screen's corners in each (pixels)."""
    room, greys, quads = _wide_room(), [], []
    x0, y0, x1, y1 = ROOM_SCREEN
    for t, offset in enumerate(offsets):
        frame = room[:, offset : offset + W].copy()
        if t in crossing:
            frame[y0 - 10 : y1 + 10, x0 - offset + 10 : x0 - offset + 60] = (150, 90, 70)
        greys.append(motion.grey(frame)[0].astype(np.uint8))
        quads.append(np.array([(x0 - offset, y0), (x1 - offset, y0), (x1 - offset, y1), (x0 - offset, y1)], dtype=float))
    return greys, quads


def test_a_screen_leaving_the_frame_is_judged_on_what_the_frame_still_shows():
    """The black outside the frame was read as "something passes in front of it" and refused the take."""
    greys, quads = _pan([200 - 6 * t for t in range(FRAMES)], crossing=(4,))
    patches, shown = zip(*(stray_screens._patch(g, q) for g, q in zip(greys, quads, strict=True)), strict=True)
    judged = stray_screens.occluded(list(patches), list(shown))
    # Inside, then partly out and judged on what is left, then mostly out and not judged at all.
    assert [answer is None for answer in judged] == [t >= 16 for t in range(FRAMES)]
    # Something passing in front is still seen; the black around the leaving screen is not.
    assert [t for t, answer in enumerate(judged) if answer] == [4]
    # Judged on the whole patch, black included, the leaving frames refused it.
    assert sum(stray_screens.occluded(list(patches))) > stray_screens.MAX_OCCLUDED_SHARE * FRAMES


def _sighting(quad: np.ndarray) -> list:
    return [stray_screens.Sighting("first_frame", stray_screens.parse_corners([(x / W, y / H) for x, y in quad]))]


def test_a_screen_the_camera_carries_out_of_the_frame_is_cleaned_while_it_shows():
    greys, quads = _pan([200 - 6 * t for t in range(FRAMES)])
    read_at = {"first_frame": 0, "middle_frame": FRAMES // 2, "last_frame": FRAMES - 1}
    corners, followed, _at_read = stray_screens.follow(greys, read_at, _sighting(quads[0]), FPS)
    assert followed == FRAMES and len(corners) == 1
    # Followed past the edge: drawn where the screen still is, clipped by the frame.
    assert np.abs(corners[0][12] - quads[12]).max() < 4.0


def test_a_screen_shown_in_too_few_frames_is_refused_saying_it_leaves_the_frame():
    # Already crossing the edge in the first frame: half of it or more shows in two frames only.
    greys, quads = _pan([max(20, 125 - 15 * t) for t in range(FRAMES)])
    read_at = {"first_frame": 0, "middle_frame": FRAMES // 2, "last_frame": FRAMES - 1}
    with pytest.raises(stray_screens.CleanupError, match="leaves the frame"):
        stray_screens.follow(greys, read_at, _sighting(quads[0]), FPS)


def test_a_located_screen_is_a_convex_quad_of_the_frame_top_left_first():
    assert stray_screens.parse_corners([[0.6, 0.2], [0.6, 0.5], [0.2, 0.5], [0.2, 0.2]]) == (
        (0.2, 0.2),
        (0.6, 0.2),
        (0.6, 0.5),
        (0.2, 0.5),
    )
    assert stray_screens.parse_corners([[0.2, 0.2], [0.6, 0.5], [0.6, 0.2], [0.2, 0.5]]) is None
    assert stray_screens.parse_corners([[0.2, 0.2], [0.21, 0.2], [0.21, 0.21], [0.2, 0.21]]) is None
    assert stray_screens.parse_corners([[0, 0], [2, 0], [2, 1], [0, 1]]) is None
    assert stray_screens.parse_option("fail") is None and stray_screens.parse_option("flatten") == {"mode": "flatten"}
    assert stray_screens.parse_option({"file_id": PICTURE_ID})["light"] == stray_screens.SCREENSHOT_LIGHT
    with pytest.raises(ValueError, match=r"fail .* flatten"):
        stray_screens.parse_option("erase")


def test_the_frames_reading_locates_the_screens_a_term_shows_on_only_when_asked():
    asked: list[str] = []

    class _Llm:
        async def chat(self, messages, **_kwargs):
            asked.append(messages[0].content)
            return SimpleNamespace(content="{}")

    frames = {"first_frame": (b"x", "image/jpeg")}
    asyncio.run(content_drift.observe(_Llm(), None, [], ["screens", "readable text"], frames))
    asyncio.run(content_drift.observe(_Llm(), None, [], ["screens", "readable text"], frames, locate=True))
    plain, located = asked
    assert "screens" not in plain.split("excluded_seen those an image shows.")[1].split("Report only")[0]
    assert '"noticeable": true}]' in plain
    assert "the displays of monitors" in located and '"elsewhere": false' in located


def _answer(*, elsewhere: bool = False, corners=None) -> dict:
    corners = corners or [[0.39, 0.31], [0.67, 0.33], [0.67, 0.63], [0.39, 0.62]]
    seen = {"term": "screens", "noticeable": True, "screens": [corners], "elsewhere": elsewhere}
    return {"frames": {"first_frame": {"subjects": [], "excluded_seen": [seen]}}}


def test_a_term_shown_only_on_located_screens_is_cleanable_and_one_shown_elsewhere_is_not():
    on_screen = content_drift.ContentCheck(True, content_drift.drifted([], ["screens"], _answer()))
    assert on_screen.cleanable() and len(on_screen.sightings()) == 1
    elsewhere = content_drift.ContentCheck(True, content_drift.drifted([], ["screens"], _answer(elsewhere=True)))
    assert elsewhere.findings and not elsewhere.cleanable() and elsewhere.sightings() == []
    # A cleaned screen still reads as a screen: answered by the cleanup, a screen it missed is not.
    cleaned = {"first_frame": [[[0.39, 0.31], [0.67, 0.33], [0.67, 0.63], [0.39, 0.62]]]}
    assert on_screen.failing_after(cleaned) == []
    missed = content_drift.ContentCheck(
        True, content_drift.drifted([], ["screens"], _answer(corners=[[0.05, 0.1], [0.2, 0.1], [0.2, 0.3], [0.05, 0.3]]))
    )
    assert missed.failing_after(cleaned) == missed.failing()


def test_the_same_term_listed_once_per_screen_keeps_every_screen():
    """The second entry's screens were dropped, one screen left uncleaned and the take failed after cleaning."""
    left = [[0.05, 0.1], [0.3, 0.1], [0.3, 0.4], [0.05, 0.4]]
    right = [[0.6, 0.1], [0.9, 0.1], [0.9, 0.4], [0.6, 0.4]]

    def reading(*entries) -> content_drift.ContentCheck:
        seen = [{"term": "screens", "noticeable": noticeable, "screens": [c], "elsewhere": e} for c, e, noticeable in entries]
        return content_drift.ContentCheck(
            True, content_drift.drifted([], ["screens"], {"frames": {"first_frame": {"subjects": [], "excluded_seen": seen}}})
        )

    both = reading((left, False, False), (right, False, True))
    assert len(both.findings) == 1 and both.findings[0].severity == content_drift.MAJOR
    assert both.cleanable() and len(both.sightings()) == 2
    assert both.failing_after({"first_frame": [left, right]}) == []
    # One entry saying the term also shows off its screens leaves the finding uncleanable.
    mixed = reading((left, False, True), (right, True, True))
    assert mixed.findings and not mixed.cleanable() and mixed.sightings() == []


# ── Kept regions ──────────────────────────────────────────────────────────

BANNER = (100, 22, 220, 52)


def _photo() -> Image.Image:
    photo = Image.fromarray(_texture(7, sigma=2.5))
    draw = ImageDraw.Draw(photo)
    draw.rectangle(BANNER, fill=(240, 240, 240))
    draw.text((108, 30), "FILIGRAN TEAM 2026", fill=(10, 10, 10))
    return photo


def _animated(path: Path, photo: Image.Image, *, opens_on_it: bool = True) -> str:
    rng = np.random.default_rng(2)
    source = photo if opens_on_it else Image.fromarray(_texture(21, sigma=2.5))
    frames = []
    for t in range(FRAMES):
        frame = _shot(source, t).astype(np.float32) * 0.92 + 8
        (x0, y0), (x1, y1) = _push(t).apply([BANNER[:2], BANNER[2:]]).astype(int)
        frame[y0:y1, x0:x1] = rng.random((y1 - y0, x1 - x0, 3)) * 255  # the model melted its letters
        frames.append(np.clip(frame, 0, 255).astype(np.uint8))
    return _encode(path, frames)


def _box() -> list[list[float]]:
    x0, y0, x1, y1 = BANNER
    return [[x0 / W, y0 / H, (x1 - x0) / W, (y1 - y0) / H]]


@needs_ffmpeg
def test_a_kept_region_is_drawn_back_from_the_still_where_the_camera_takes_it(tmp_path):
    photo = _photo()
    shot = _animated(tmp_path / "photo.mp4", photo)
    kept = asyncio.run(kept_regions.keep(shot, str(tmp_path / "out.mp4"), photo, _box()))
    after = asyncio.run(_decoded(str(tmp_path / "out.mp4")))
    assert (kept.frames, kept.found) == (len(after), len(after)) and "their text is the picture's own" in kept.notes[0]
    for t, frame in enumerate(after):
        expected = _shot(photo, t).astype(np.float32) * 0.92 + 8
        (x0, y0), (x1, y1) = _push(t).apply([(BANNER[0] + 4, BANNER[1] + 4), (BANNER[2] - 4, BANNER[3] - 4)]).astype(int)
        assert np.abs(frame[y0:y1, x0:x1] - expected[y0:y1, x0:x1]).mean() < 10, t


@needs_ffmpeg
def test_a_shot_that_does_not_open_on_its_still_loses_its_kept_regions(tmp_path):
    photo = _photo()
    shot = _animated(tmp_path / "other.mp4", photo, opens_on_it=False)
    with pytest.raises(kept_regions.RegionLostError, match=r"kept region lost: .*not in its first"):
        asyncio.run(kept_regions.keep(shot, str(tmp_path / "out.mp4"), photo, _box()))


def test_a_region_is_taken_through_the_centre_cut_a_provider_fits_the_still_by():
    # A 4:3 picture in a 16:9 frame loses its top and bottom: a box there is cut, one entirely there dropped.
    rects = kept_regions.frame_rects((400, 300), (320, 180), [[0.25, 0.0, 0.5, 0.2], [0.0, 0.0, 0.2, 0.1]])
    assert len(rects) == 1
    x0, y0, x1, y1 = rects[0]
    assert (round(x0), round(x1)) == (80, 240) and y0 == 0.0 and round(y1) == 18
    assert kept_regions.same_boxes([[0.1, 0.1, 0.2, 0.2]], [[0.10001, 0.1, 0.2, 0.2]])
    assert not kept_regions.same_boxes([[0.1, 0.1, 0.2, 0.2]], [])


# ── The runner ────────────────────────────────────────────────────────────


@pytest.fixture
def run(monkeypatch):
    """``run_clip`` of an office shot or an animated photo whose video the provider made; heavy steps stubbed."""
    world = SimpleNamespace(row=None, stored=[], order=[], readings=[], cleaned=0, kept_lost=None, frames_kept=0)

    class _Db:
        async def get(self, _model, _key):
            return world.row

        async def commit(self):
            return None

    @asynccontextmanager
    async def session():
        yield _Db()

    class _Provider:
        async def generate_video(self, request, **_kwargs):
            return VideoResult(data=b"RAW", job_id="op/1")

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return _Provider(), model, SimpleNamespace(id=provider_id, provider="gemini")

    async def mark(_kind, _job_id, **changes):
        for key, value in changes.items():
            setattr(world.row, key, value)

    async def store_path(path, **_kwargs):
        world.stored.append(Path(path).read_bytes())
        return SimpleNamespace(id=uuid.uuid4())

    async def download(file_id, path):
        Path(path).write_bytes(b"CLEANED" if str(file_id) == str(world.row.file_id) else b"RAW")
        return SimpleNamespace(content_type="video/mp4")

    async def probe(_path):
        return SimpleNamespace(duration=8.0, width=1920, height=1080, has_audio=False, fps=24.0)

    async def poster(_path, *, at, out_path, width):
        Path(out_path).write_bytes(b"JPG")

    async def start_frame(params, _workdir, **_kwargs):
        return clips.StartFrame(_png(), "image/png", PICTURE_ID) if params.get("reference_file_id") else None

    async def keep_frames(*_args, **_kwargs):
        world.frames_kept += 1
        return {}

    async def cleaned(path, sightings, option, picture, _workdir):
        world.cleaned += 1
        world.order.append("cleaned")
        Path(path).write_bytes(b"CLEANED")
        drawn = [[list(corner) for corner in s.corners] for s in sightings]
        return stray_screens.Cleaned(1, 192, 192, {"first_frame": drawn}, ["Stray screens: 1 screen flattened."])

    async def regions_kept(path, _still, _boxes, _workdir):
        if world.kept_lost:
            raise kept_regions.RegionLostError(world.kept_lost)
        world.order.append("kept")
        Path(path).write_bytes(b"KEPT")
        return kept_regions.Kept(192, 192, ["Kept from the start picture in every frame: 1 region."])

    async def content_check(*_args, **kwargs):
        world.order.append("content")
        world.located = kwargs.get("locate_screens")
        return world.readings.pop(0)

    async def text_check(*_args, **_kwargs):
        world.order.append("text")
        return text_drift.TextCheck(True)

    async def nothing(*_args, **_kwargs):
        return []

    monkeypatch.setattr(runners, "async_session", session)
    monkeypatch.setattr(runners, "bind_llm_caller", nothing)
    monkeypatch.setattr(runners, "_keep_frames", keep_frames)
    monkeypatch.setattr(runners, "_screens_cleaned", cleaned)
    monkeypatch.setattr(runners, "_regions_kept", regions_kept)
    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(clips, "start_frame", start_frame)
    monkeypatch.setattr(clips, "reference_pictures", nothing)
    monkeypatch.setattr(jobs, "mark", mark)
    monkeypatch.setattr(assets, "store_path", store_path)
    monkeypatch.setattr(assets, "delete_files", nothing)
    monkeypatch.setattr(assets, "download_file", download)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_frame", poster)
    monkeypatch.setattr(content_drift, "check", content_check)
    monkeypatch.setattr(text_drift, "check", text_check)
    return world


def _png() -> bytes:
    import io

    out = io.BytesIO()
    _photo().save(out, format="PNG")
    return out.getvalue()


def _office_row(**params) -> VideoClip:
    return VideoClip(
        id=uuid.uuid4(),
        user_id=USER_ID,
        provider="gemini",
        model="veo-3.1-generate-preview",
        prompt="An open-plan office at dusk, two colleagues talking by a window",
        params={
            "provider_id": str(uuid.uuid4()),
            "duration_seconds": 8,
            "negative_prompt": "screens, readable text",
            stray_screens.KEY: {"mode": "flatten"},
            **params,
        },
        status=JOB_PENDING,
    )


def _reading(*, elsewhere: bool = False, corners=None) -> content_drift.ContentCheck:
    return content_drift.ContentCheck(True, content_drift.drifted([], ["screens"], _answer(elsewhere=elsewhere, corners=corners)))


def test_a_take_showing_the_term_only_on_screens_is_cleaned_read_again_and_kept(run):
    run.row = _office_row()
    # The second reading still sees a screen where the cleaned one is: what the author chose, kept.
    run.readings = [_reading(), _reading()]
    asyncio.run(runners.run_clip(str(run.row.id)))
    row = run.row
    assert row.status == JOB_READY and run.cleaned == 1 and run.located is True
    assert run.order == ["content", "cleaned", "content"] and b"CLEANED" in run.stored
    assert row.params[stray_screens.KEY]["cleaned_file_id"] == str(row.file_id)
    assert row.params[stray_screens.KEY]["cleaned"]["first_frame"]
    notes = " ".join(row.params["notes"])
    assert "Stray screens: 1 screen flattened." in notes and "The cleaned screens still read as" in notes
    # Its frames are cut again from the cleaned take.
    assert run.frames_kept == 2


def test_a_take_showing_the_term_off_its_screens_fails_as_before(run):
    run.row = _office_row()
    run.readings = [_reading(elsewhere=True)]
    asyncio.run(runners.run_clip(str(run.row.id)))
    assert run.row.status == JOB_FAILED and run.row.error.startswith("content drift:") and run.cleaned == 0


def test_a_cleaned_take_showing_the_term_on_a_screen_it_missed_still_fails(run):
    run.row = _office_row()
    missed = [[0.05, 0.1], [0.2, 0.1], [0.2, 0.3], [0.05, 0.3]]
    run.readings = [_reading(), _reading(corners=missed)]
    asyncio.run(runners.run_clip(str(run.row.id)))
    row = run.row
    assert row.status == JOB_FAILED and "(after its stray screens were cleaned)" in row.error and run.cleaned == 1


def test_a_take_cleaned_by_a_run_that_died_is_not_cleaned_twice(run):
    cleaned_id = uuid.uuid4()
    corners = [[0.39, 0.31], [0.67, 0.33], [0.67, 0.63], [0.39, 0.62]]
    run.row = _office_row(
        **{stray_screens.KEY: {"mode": "flatten", "cleaned_file_id": str(cleaned_id), "cleaned": {"first_frame": [corners]}}}
    )
    run.row.file_id = cleaned_id
    run.readings = [_reading()]
    asyncio.run(runners.run_clip(str(run.row.id)))
    assert run.row.status == JOB_READY and run.cleaned == 0 and run.order == ["content"]


def test_without_stray_screens_the_reading_does_not_locate_screens(run):
    run.row = _office_row()
    run.row.params.pop(stray_screens.KEY)
    run.readings = [_reading()]
    asyncio.run(runners.run_clip(str(run.row.id)))
    assert run.row.status == JOB_FAILED and run.located is False and run.cleaned == 0


def _photo_row() -> VideoClip:
    return VideoClip(
        id=uuid.uuid4(),
        user_id=USER_ID,
        provider="gemini",
        model="veo-3.1-generate-preview",
        prompt="Bring this image to life",
        params={
            "provider_id": str(uuid.uuid4()),
            "duration_seconds": 6,
            "reference_file_id": PICTURE_ID,
            "content_check": False,
            kept_regions.KEY: {"boxes": _box()},
        },
        status=JOB_PENDING,
    )


def test_kept_regions_are_drawn_back_before_the_text_check_reads_the_take(run):
    run.row = _photo_row()
    asyncio.run(runners.run_clip(str(run.row.id)))
    row = run.row
    assert row.status == JOB_READY and run.order == ["kept", "text"] and b"KEPT" in run.stored
    assert row.params[kept_regions.KEY]["drawn_file_id"] == str(row.file_id)
    assert "Kept from the start picture in every frame" in " ".join(row.params["notes"])


def test_a_shot_whose_kept_regions_were_lost_fails_and_keeps_its_take(run):
    run.row = _photo_row()
    run.kept_lost = "kept region lost: the camera could not be followed from the still (found in 9 of 192 frames)."
    asyncio.run(runners.run_clip(str(run.row.id)))
    row = run.row
    assert row.status == JOB_FAILED and row.error.startswith("kept region lost:") and "text" not in run.order
    assert row.file_id is not None
