"""A start-frame shot whose lettering the model redrew is rescued once before it fails. Synthetic shots only.

* the located blocks become regions a take keeps: grown by their margin,
  inside the picture, merged to at most four, the picture itself left out;
* the runner: a take failing its text check on a start picture that reads
  words has the lettering located, drawn back from the start picture and is
  read again - once; it is ready when the text then reads right, and fails
  as a text drift otherwise, its video kept; nothing is located for a picture
  without words; a run resumed on a take already rescued does not rescue it
  twice; a ready shot generated again keeps playing its take, the failed
  attempt's frames recorded beside it;
* on real pixels: a photo whose banner the model melted, the camera pushing
  in - the real region keeper draws the banner back and the real text check,
  reading each frame's banner, passes.
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
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
from app.services.video_studio import assets, clips, encoder, frame_qc, jobs, kept_regions, motion, runners, text_drift
from app.services.video_studio.spec import MAX_KEPT_REGIONS

USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000061e1")
PICTURE_ID = "00000000-0000-0000-0000-0000000061e2"
W, H, FRAMES, FPS = 320, 180, 24, 24
BANNER = (96, 20, 224, 52)
WORDS = "FILIGRAN TEAM 2026"
GARBLED = "FILIGRAM TEAN 2O26"


# ── The regions ───────────────────────────────────────────────────────────


def test_a_located_block_is_kept_with_a_margin_inside_the_picture():
    [box] = text_drift.lettering_boxes([{"text": "Orchestration", "box": [0.3, 0.1, 0.4, 0.1]}])
    grow = 0.1 * text_drift.LETTERING_MARGIN
    assert box == pytest.approx([0.3 - grow, 0.1 - grow, 0.4 + 2 * grow, 0.1 + 2 * grow], abs=1e-4)
    # At the edge: clipped to the picture.
    [edge] = text_drift.lettering_boxes([[0.0, 0.0, 0.2, 0.1]])
    assert edge[:2] == [0.0, 0.0] and edge[2] == pytest.approx(0.2 + 0.1 * text_drift.LETTERING_MARGIN, abs=1e-4)


def test_overlapping_lines_are_one_region_and_many_words_at_most_four():
    lines = [[0.1, 0.10, 0.5, 0.05], [0.1, 0.16, 0.5, 0.05]]
    assert len(text_drift.lettering_boxes(lines)) == 1
    words = [[0.05 + 0.15 * i, 0.5 + 0.06 * (i % 2), 0.08, 0.03] for i in range(6)]
    kept = text_drift.lettering_boxes(words)
    assert 1 <= len(kept) <= MAX_KEPT_REGIONS
    # Every word is inside a kept region.
    for x, y, w, h in words:
        assert any(kx <= x and ky <= y and x + w <= kx + kw + 1e-4 and y + h <= ky + kh + 1e-4 for kx, ky, kw, kh in kept)


def test_the_picture_itself_and_what_is_not_a_box_are_left_out():
    assert text_drift.lettering_boxes([[0.0, 0.0, 1.0, 0.95]]) == []
    assert text_drift.lettering_boxes([[0.1, 0.1, 0.2], ["a", 0, 0.1, 0.1], [True, 0.1, 0.1, 0.1], {"box": None}]) == []
    assert text_drift.lettering_boxes("not a list") == []


def test_the_reading_is_taken_from_a_fenced_answer():
    answer = '```json\n{"lettering": [{"text": "SOC", "box": [0.4, 0.4, 0.2, 0.1]}]}\n```'
    assert text_drift.lettering_boxes(text_drift._located(answer)) and text_drift._located("no json") == []


# ── The runner ────────────────────────────────────────────────────────────


def _drift(*, start: str = WORDS) -> text_drift.TextCheck:
    texts = {"start_picture": start, "first_frame": GARBLED, "last_frame": GARBLED}
    return text_drift.TextCheck(True, text_drift.drifted_words(start, texts), texts=texts)


def _reads_right() -> text_drift.TextCheck:
    return text_drift.TextCheck(True, [], texts={"start_picture": WORDS, "first_frame": WORDS, "last_frame": WORDS})


def _png() -> bytes:
    out = io.BytesIO()
    _photo().save(out, format="PNG")
    return out.getvalue()


@pytest.fixture
def run(monkeypatch):
    """``run_clip`` of a shot made from a picture whose video the provider made; the heavy steps stubbed."""
    world = SimpleNamespace(row=None, stored=[], order=[], verdicts=[], located=[[0.3, 0.1, 0.4, 0.2]], lost=None)

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
        Path(path).write_bytes(b"LETTERED" if str(file_id) == str(world.row.file_id) else b"RAW")
        return SimpleNamespace(content_type="video/mp4")

    async def probe(_path):
        return SimpleNamespace(duration=6.0, width=1280, height=720, has_audio=False, fps=24.0)

    async def poster(_path, *, at, out_path, width):
        Path(out_path).write_bytes(b"JPG")

    async def start_frame(params, _workdir, **_kwargs):
        return clips.StartFrame(_png(), "image/png", PICTURE_ID) if params.get("reference_file_id") else None

    async def keep_frames(_clip_id, _path, _duration, _workdir, _picture, *, params, **_kwargs):
        params[frame_qc.QC_KEY] = {"first_frame_file_id": str(uuid.uuid4()), "motion_ssim": 0.9}
        return params[frame_qc.QC_KEY]

    async def regions_kept(path, _still, boxes, _workdir):
        if world.lost:
            raise kept_regions.RegionLostError(world.lost)
        world.order.append("kept")
        world.boxes = boxes
        Path(path).write_bytes(b"LETTERED")
        return kept_regions.Kept(144, 144, ["Kept from the start picture in every frame: 1 region."])

    async def text_check(*_args, **_kwargs):
        world.order.append("text")
        return world.verdicts.pop(0)

    async def locate(_user_id, _picture):
        world.order.append("located")
        return text_drift.lettering_boxes(world.located)

    async def nothing(*_args, **_kwargs):
        return []

    monkeypatch.setattr(runners, "async_session", session)
    monkeypatch.setattr(runners, "bind_llm_caller", nothing)
    monkeypatch.setattr(runners, "_keep_frames", keep_frames)
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
    monkeypatch.setattr(text_drift, "check", text_check)
    monkeypatch.setattr(text_drift, "locate_lettering", locate)
    return world


def _row(**params) -> VideoClip:
    return VideoClip(
        id=uuid.uuid4(),
        user_id=USER_ID,
        provider="gemini",
        model="veo-3.1-generate-preview",
        prompt="Bring this team photo to life, the camera pushing in slowly",
        params={
            "provider_id": str(uuid.uuid4()),
            "duration_seconds": 6,
            "reference_file_id": PICTURE_ID,
            "content_check": False,
            **params,
        },
        status=JOB_PENDING,
    )


def test_redrawn_lettering_is_drawn_back_and_read_again_before_the_shot_fails(run):
    run.row = _row()
    run.verdicts = [_drift(), _reads_right()]
    asyncio.run(runners.run_clip(str(run.row.id)))
    row = run.row
    assert row.status == JOB_READY and run.order == ["text", "located", "kept", "text"] and b"LETTERED" in run.stored
    kept = row.params[text_drift.LETTERING_KEY]
    assert kept["drawn_file_id"] == str(row.file_id) and kept["boxes"] == run.boxes
    notes = " ".join(row.params["notes"])
    assert "The model redrew the start picture's lettering: it was drawn back from the start picture (1 region)" in notes
    # The take's frames record what it passed: the text check holds no threshold.
    assert row.params[frame_qc.QC_KEY][frame_qc.CHECKED_WITH] == {}


def test_a_take_still_reading_wrong_after_the_rescue_fails_as_a_text_drift(run):
    run.row = _row()
    run.verdicts = [_drift(), _drift()]
    asyncio.run(runners.run_clip(str(run.row.id)))
    row = run.row
    assert row.status == JOB_FAILED and row.error.startswith("text drift: the video model redrew the text")
    assert row.error.endswith("This is after its lettering was drawn back from the start picture.")
    assert row.file_id is not None and run.order == ["text", "located", "kept", "text"]


def test_lettering_that_cannot_be_located_or_followed_fails_saying_so(run):
    run.row = _row()
    run.verdicts, run.located = [_drift()], []
    asyncio.run(runners.run_clip(str(run.row.id)))
    assert run.row.status == JOB_FAILED and "it could not be located on the start picture" in run.row.error
    assert "kept" not in run.order
    run.row, run.order = _row(), []
    run.verdicts, run.located = [_drift()], [[0.3, 0.1, 0.4, 0.2]]
    run.lost = "kept region lost: the camera could not be followed from the still (found in 3 of 144 frames)."
    asyncio.run(runners.run_clip(str(run.row.id)))
    assert run.row.status == JOB_FAILED and "Its lettering was not drawn back: kept region lost" in run.row.error


def test_a_picture_without_words_is_not_rescued(run):
    run.row = _row()
    # Text the frames garble that the picture never had: unreadable places, no words to draw back.
    garbled = text_drift.TextCheck(True, [], texts={"start_picture": "", "first_frame": "", "last_frame": ""}, unreadable=[("last_frame", 3)])
    run.verdicts = [garbled]
    asyncio.run(runners.run_clip(str(run.row.id)))
    assert run.row.status == JOB_FAILED and run.order == ["text"]


def test_a_run_resumed_on_a_rescued_take_does_not_rescue_it_twice(run):
    lettered = uuid.uuid4()
    run.row = _row(
        take_steps=[{"step": "lettering", "file_id": str(lettered)}],
        **{text_drift.LETTERING_KEY: {"boxes": [[0.25, 0.05, 0.5, 0.3]], "drawn_file_id": str(lettered)}},
    )
    run.row.file_id = lettered
    run.verdicts = [_drift()]
    asyncio.run(runners.run_clip(str(run.row.id)))
    assert run.row.status == JOB_FAILED and run.order == ["text"]


def test_a_ready_shot_generated_again_keeps_playing_its_take_when_the_rescue_fails(run):
    playing, poster = uuid.uuid4(), uuid.uuid4()
    run.row = _row()
    run.row.status, run.row.file_id, run.row.poster_file_id = JOB_READY, playing, poster
    params = dict(run.row.params)
    clips.keep_take(params, run.row)
    run.row.params, run.row.status = params, JOB_PENDING
    run.verdicts = [_drift(), _drift()]
    asyncio.run(runners.run_clip(str(run.row.id)))
    row = run.row
    assert row.status == JOB_READY and row.file_id == playing
    state = jobs.job_state("clip", row)
    assert state.status == jobs.STATE_FAILED and state.previous_take_plays and "text drift" in state.reason
    # The failed attempt's video and frames are kept beside the take that plays.
    [attempt] = row.params[clips.ATTEMPTS]
    assert attempt["file_id"] and attempt["frame_qc"]["first_frame_file_id"]


# ── On real pixels ────────────────────────────────────────────────────────


def _texture(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    light = sum(ndimage.gaussian_filter(rng.random((H, W)), s) * k for s, k in ((2.5, 1.0), (10.0, 3.0), (30.0, 6.0)))
    tint = ndimage.gaussian_filter(rng.random((H, W, 3)), (10, 10, 0))
    picture = light[..., None] * 3 + tint
    return ((picture - picture.min()) / (picture.max() - picture.min()) * 190 + 30).astype(np.uint8)


def _photo() -> Image.Image:
    photo = Image.fromarray(_texture(7))
    draw = ImageDraw.Draw(photo)
    draw.rectangle(BANNER, fill=(240, 240, 240))
    draw.text((104, 30), WORDS, fill=(10, 10, 10))
    return photo


def _push(t: int) -> motion.Similarity:
    share = min(t, FRAMES - 1) / (FRAMES - 1)
    scale = 1 + 0.05 * share
    centre = np.array([W / 2, H / 2])
    shift = centre - scale * centre + np.array([4.0 * share, 2.0 * share])
    return motion.Similarity(scale, 0.0, float(shift[0]), float(shift[1]))


def _framed(picture: Image.Image, t: int) -> np.ndarray:
    return np.asarray(
        picture.transform((W, H), Image.Transform.AFFINE, _push(t).coefficients(), resample=Image.Resampling.BICUBIC)
    ).astype(np.float32)


def _melted_take(path: Path) -> str:
    """The take a video model made of the photo: the camera pushing in, the banner's letters melted to noise."""
    rng = np.random.default_rng(3)
    frames = path.parent / "frames"
    frames.mkdir()
    names = []
    for t in range(FRAMES):
        frame = _framed(_photo(), t)
        (x0, y0), (x1, y1) = _push(t).apply([BANNER[:2], BANNER[2:]]).astype(int)
        frame[y0:y1, x0:x1] = rng.random((y1 - y0, x1 - x0, 3)) * 255
        Image.fromarray(np.clip(frame, 0, 255).astype(np.uint8)).save(frames / f"f{t:03d}.png")
        names.append((t / FPS, f"f{t:03d}.png"))
    asyncio.run(
        encoder.frames_to_mezzanine(
            names, end_time=FRAMES / FPS, out_path=str(path), fps=FPS, crf=12, preset="ultrafast", workdir=str(frames)
        )
    )
    return str(path)


def _banner_reads(picture: Image.Image, t: int) -> bool:
    """Whether the banner in *picture* (a frame of the take at *t*) is the photo's own: its letters, not noise."""
    frame = np.asarray(picture.convert("RGB").resize((W, H)), dtype=np.float32)
    (x0, y0), (x1, y1) = _push(t).apply([(BANNER[0] + 6, BANNER[1] + 6), (BANNER[2] - 6, BANNER[3] - 6)]).astype(int)
    expected = _framed(_photo(), t)
    return float(np.abs(frame[y0:y1, x0:x1] - expected[y0:y1, x0:x1]).mean()) < 14


@pytest.mark.skipif(not encoder.encoder_available(), reason="ffmpeg is not installed")
def test_on_real_pixels_the_melted_banner_is_drawn_back_and_reads_again(monkeypatch, tmp_path):
    take = _melted_take(tmp_path / "take.mp4")
    world = SimpleNamespace(row=_row(), stored=[], readings=0)

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
            return VideoResult(data=Path(take).read_bytes(), job_id="op/1")

    class _Reader:
        """The person's chat model: where the start picture's lettering is, as a model locates it (a little off)."""

        async def chat(self, _messages, **_kwargs):
            x0, y0, x1, y1 = BANNER
            box = [x0 / W + 0.01, y0 / H - 0.01, (x1 - x0) / W - 0.02, (y1 - y0) / H]
            return SimpleNamespace(content=json.dumps({"lettering": [{"text": WORDS, "box": box}]}))

    async def transcribe(_llm, _model, pictures):
        # Each frame reads the banner's words when its banner is the photo's own; the noise reads garbled.
        world.readings += 1
        texts = {"start_picture": WORDS}
        for name, at in (("first_frame", 0), ("last_frame", FRAMES - 1)):
            picture = Image.open(io.BytesIO(pictures[name][0]))
            texts[name] = WORDS if _banner_reads(picture, at) else GARBLED
        return text_drift.Reading(texts, {name: 0 for name in texts})

    @asynccontextmanager
    async def reading_session():
        yield None

    async def reader(_db, _user_id):
        return _Reader(), "m"

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return _Provider(), model, SimpleNamespace(id=provider_id, provider="gemini")

    async def mark(_kind, _job_id, **changes):
        for key, value in changes.items():
            setattr(world.row, key, value)

    async def store_path(path, **_kwargs):
        kept = tmp_path / f"stored-{len(world.stored)}{Path(path).suffix}"
        shutil.copyfile(path, kept)
        world.stored.append(kept)
        return SimpleNamespace(id=uuid.uuid4())

    async def start_frame(params, _workdir, **_kwargs):
        return clips.StartFrame(_png(), "image/png", PICTURE_ID)

    async def keep_frames(*_args, **_kwargs):
        return {}

    async def nothing(*_args, **_kwargs):
        return []

    monkeypatch.setattr(runners, "async_session", session)
    monkeypatch.setattr(runners, "bind_llm_caller", nothing)
    monkeypatch.setattr(runners, "_keep_frames", keep_frames)
    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(clips, "start_frame", start_frame)
    monkeypatch.setattr(clips, "reference_pictures", nothing)
    monkeypatch.setattr(jobs, "mark", mark)
    monkeypatch.setattr(assets, "store_path", store_path)
    monkeypatch.setattr(assets, "delete_files", nothing)
    monkeypatch.setattr(text_drift, "transcribe", transcribe)
    monkeypatch.setattr("app.core.database.async_session", reading_session)
    monkeypatch.setattr("app.llm.router.get_llm_for_user", reader)
    asyncio.run(runners.run_clip(str(world.row.id)))
    row = world.row
    assert row.status == JOB_READY, row.error
    assert world.readings == 2
    [box] = row.params[text_drift.LETTERING_KEY]["boxes"]
    x0, y0, x1, y1 = BANNER
    assert box[0] <= x0 / W and box[1] <= y0 / H and box[0] + box[2] >= x1 / W - 0.02 and box[1] + box[3] >= y1 / H
    # The take kept is the one drawn back: its banner reads in every frame.
    lettered = [path for path in world.stored if path.suffix == ".mp4"][-1]
    frames = asyncio.run(_frames(str(lettered)))
    # The mezzanine holds the last frame once more.
    assert len(frames) >= FRAMES and all(_banner_reads(frame, min(t, FRAMES - 1)) for t, frame in enumerate(frames))


async def _frames(path: str) -> list[Image.Image]:
    return [
        Image.fromarray(np.frombuffer(raw, dtype=np.uint8).reshape(H, W, 3))
        async for raw in encoder.iter_rgb_frames(path, width=W, height=H)
    ]
