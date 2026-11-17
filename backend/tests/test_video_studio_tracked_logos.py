"""A logo ingredient drawn onto a subject the start frame does not show: the ``tracked`` path. Synthetic footage.

* the subject (a textured card) walks into an establishing shot: the detector
  (faked here with the footage's own truth at the sampled frames) sees it from
  the first sampled frame it is in, the track follows it between sampled
  frames and back to the frame it entered on, and the logo is drawn on its
  surface with its own pixels in every frame it shows in, nowhere before;
* two instances of the subject each get one logo;
* a surface covered while the subject stays fails the take as ``ingredient
  surface lost`` with that frame; a subject that walks out ends its instance
  and nothing fails; a subject never seen, or seen for a flicker only, fails
  as ``ingredient subject not found``;
* the detector's answer is bounded in code; the native path's check finds the
  logo as given and misses the model's redraw;
* the contract: a logo names its subject and nothing else may, the path is
  ``tracked`` on a model without reference images start frame or not, and
  ``run_clip`` draws it, records it and reports ``ingredients_path: tracked``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.services.video_studio import clips, frame_qc, logos, runners, tracked_logos
from app.services.video_studio.jobs import JOB_FAILED, JOB_READY
from tests.test_video_studio_shot_rescue import (
    FFMPEG,
    FPS,
    LOGO_FILE,
    SIZE,
    START,
    _box,
    _decoded,
    _encode,
    _garbled,
    _logo,
    _png,
    _shoot,
    _world,
    take,  # noqa: F401 - the run_clip fixture, shared with the composited path's tests
)

W, H = SIZE
SUBJECT_FILE = "00000000-0000-0000-0000-00000000c067"
CARD = (150, 90)
#: Where the logo sits on the card: the patch its 110 px wide mark covers.
PATCH = (20, 17, 130, 72)


def _card(mark: Image.Image | None, seed: int = 5) -> Image.Image:
    """The subject: a textured card, the *mark* printed on its patch (the real logo, the model's redraw, or none)."""
    rng = np.random.default_rng(seed)
    picture = Image.new("RGB", CARD, (230, 225, 215))
    draw = ImageDraw.Draw(picture)
    for _ in range(40):
        x, y = int(rng.integers(0, CARD[0])), int(rng.integers(0, CARD[1]))
        r = int(rng.integers(3, 8))
        draw.ellipse([x, y, x + r, y + r], fill=tuple(int(v) for v in rng.integers(40, 200, 3)))
    if mark is not None:
        sized = mark.resize((PATCH[2] - PATCH[0], PATCH[3] - PATCH[1]), Image.Resampling.LANCZOS)
        picture.paste(sized, PATCH[:2], sized)
    return picture


def _reference() -> Image.Image:
    """The picture of the subject wearing the logo, as the person gives it."""
    return _card(_logo()).resize((CARD[0] * 3, CARD[1] * 3), Image.Resampling.LANCZOS)


def _walk(index: int, *, enter: int = 0, speed: float = 10.0, start_x: float = -180.0, y: float = 130.0) -> tuple[int, int]:
    """Where the card's top left corner is in frame *index*: walking right from *start_x*, a little down."""
    return round(start_x + speed * (index - enter)), round(y + 1.5 * index)


def _surface(at: tuple[int, int]) -> tuple[tuple[float, float], ...]:
    """The patch's corners on the frame, 0-1, for a card at *at*."""
    x, y = at
    x0, y0, x1, y1 = x + PATCH[0], y + PATCH[1], x + PATCH[2], y + PATCH[3]
    return ((x0 / W, y0 / H), (x1 / W, y0 / H), (x1 / W, y1 / H), (x0 / W, y1 / H))


def _sighting(at: tuple[int, int]) -> tracked_logos.Sighting:
    x, y = at
    return tracked_logos.Sighting((x / W, y / H, (x + CARD[0]) / W, (y + CARD[1]) / H), _surface(at))


def _footage(count: int, cards: list[list[tuple[int, int] | None]], *, background: Image.Image | None = None) -> list[Image.Image]:
    """*count* frames of the table, each card of *cards* (its position per frame, ``None`` off the take) with the redraw."""
    table = background or _shoot(_world(seed=9), _box(0, pan=0, zoom=0))
    drawn = _card(_garbled(_logo()))
    frames = []
    for index in range(count):
        frame = table.copy()
        for positions in cards:
            if positions[index] is not None:
                frame.paste(drawn, positions[index])
        frames.append(frame)
    return frames


def _truth(cards: list[list[tuple[int, int] | None]], count: int):
    """A detector answering the footage's own truth at every sampled frame it is asked about (each named by its index).

    The mezzanine holds one frame past the *count* asked, a repeat of the last one.
    """

    async def detect(_item, frames: dict, _reference: bytes, _outlined: bool):
        out = {}
        for name in frames:
            index = min(int(name.rsplit("_", 1)[1]), count - 1)
            seen = []
            for positions in cards:
                at = positions[index]
                if at is None:
                    continue
                corners = np.asarray(_surface(at)) * [W, H]
                if 0 <= corners.mean(axis=0)[0] < W and 0 <= corners.mean(axis=0)[1] < H:
                    seen.append(_sighting(at))
            out[name] = seen
        return out

    return detect


def _item() -> tracked_logos.TrackedLogo:
    return tracked_logos.TrackedLogo(_logo(), "the brand logo", tracked_logos.Subject(_reference(), "the card"))


def _in_frame(at: tuple[int, int] | None) -> bool:
    return at is not None and -CARD[0] < at[0] < W and -CARD[1] < at[1] < H


# ── The subject walks in ──────────────────────────────────────────────────


@FFMPEG
def test_the_logo_is_drawn_onto_a_subject_absent_from_the_start_frame(tmp_path):
    count = 48
    positions = [_walk(i) for i in range(count)]
    video = _encode(tmp_path, _footage(count, [positions]))
    out = str(tmp_path / "tracked.mp4")
    done = asyncio.run(tracked_logos.composite(video, out, [_item()], _truth([positions], count)))
    # Seen from the first sampled frame it is in, followed back to the frame it entered on: most of the take.
    assert done.instances == 1 and done.keyframes == 5 and done.seen_in == 4
    # The mezzanine holds one frame past the last one asked; every frame of it is counted.
    assert done.share >= 0.7 and done.frames in (count, count + 1), done.record()
    assert done.notes[0].startswith("The brand logo drawn with its own pixels onto the card in")
    drawn, raw = _decoded(out), _decoded(video)
    truth = _card(_logo())
    for index in (18, 20, 30, 40, 45):
        # Between the sampled frames the track places the logo on the card's patch, the real pixels over the redraw.
        x, y = positions[index]
        x0, y0, x1, y1 = x + PATCH[0] + 6, y + PATCH[1] + 6, x + PATCH[2] - 6, y + PATCH[3] - 6
        expected = np.asarray(truth, dtype=np.float32)[PATCH[1] + 6 : PATCH[3] - 6, PATCH[0] + 6 : PATCH[2] - 6]
        shown = drawn[index][y0:y1, x0:x1].astype(np.float32)
        before = raw[index][y0:y1, x0:x1].astype(np.float32)
        assert np.abs(shown - expected).mean() < 30, (index, np.abs(shown - expected).mean())
        assert np.abs(before - expected).mean() > np.abs(shown - expected).mean() + 10, index
    # Before the subject walks in, nothing is drawn: the frames are the take's own.
    for index in (0, 4, 8):
        assert np.abs(drawn[index].astype(np.float32) - raw[index].astype(np.float32)).mean() < 2.5, index
    record = done.record()
    assert record["status"] == "drawn" and record["share"] == done.share and record["subjects"] == ["the card"]


@FFMPEG
def test_two_instances_of_the_subject_each_get_one_logo(tmp_path):
    count = 30
    first = [(60 + 4 * i, 40 + i) for i in range(count)]
    second = [(380 - 3 * i, 220 - i) for i in range(count)]
    video = _encode(tmp_path, _footage(count, [first, second]))
    out = str(tmp_path / "two.mp4")
    done = asyncio.run(tracked_logos.composite(video, out, [_item()], _truth([first, second], count)))
    assert done.instances == 2 and done.share > 0.95
    assert any(note.startswith("2 instances of the subject carry the logo") for note in done.notes)
    frame = _decoded(out)[15]
    quad = np.asarray(_surface(first[15])) * [W, H]
    # The logo shows exactly twice in the frame: once per instance, never twice on one.
    assert len(logos.matches(frame, _logo(), quad)) == 2


# ── Lost, left, never seen ────────────────────────────────────────────────


def _covered(frames: list[Image.Image], positions: list, window: range) -> list[Image.Image]:
    out = list(frames)
    for index in window:
        x, y = positions[index]
        picture = out[index].copy()
        ImageDraw.Draw(picture).rectangle([x - CARD[0], y - CARD[1], x + 2 * CARD[0], y + 2 * CARD[1]], fill=(196, 150, 120))
        out[index] = picture
    return out


@FFMPEG
def test_a_surface_covered_while_the_subject_stays_fails_the_take_with_that_frame(tmp_path):
    count = 36
    positions = [(120 + 3 * i, 100 + i) for i in range(count)]
    video = _encode(tmp_path, _covered(_footage(count, [positions]), positions, range(20, 24)))
    with pytest.raises(tracked_logos.SurfaceLostError, match=r"^ingredient surface lost at t=0\.83 s \(frame 20\) of the shot") as lost:
        asyncio.run(tracked_logos.composite(video, str(tmp_path / "lost.mp4"), [_item()], _truth([positions], count)))
    assert lost.value.frame == 20 and lost.value.frame_jpeg.startswith(b"\xff\xd8")
    assert "the card" in str(lost.value) and "generate the shot again" in str(lost.value)


@FFMPEG
def test_a_subject_that_walks_out_of_the_frame_ends_its_instance_and_nothing_fails(tmp_path):
    count = 48
    positions = [_walk(i, start_x=260.0, speed=12.0) for i in range(count)]
    cards = [[at if _in_frame(at) else None for at in positions]]
    video = _encode(tmp_path, _footage(count, cards))
    out = str(tmp_path / "left.mp4")
    done = asyncio.run(tracked_logos.composite(video, out, [_item()], _truth(cards, count)))
    assert done.instances == 1 and 0.4 <= done.share <= 0.8, done.record()


def test_a_subject_never_seen_fails_as_not_found(tmp_path, monkeypatch):
    async def probe(_path):
        return SimpleNamespace(width=W, height=H, fps=FPS, duration=2.0)

    async def frames(_path, *, width, height, resize=False):
        for _ in range(48):
            yield np.zeros((height, width, 3), dtype=np.uint8).tobytes()

    async def nothing(_item, frames_shown: dict, _reference: bytes, _outlined: bool):
        return {name: [] for name in frames_shown}

    from app.services.video_studio import encoder

    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "iter_rgb_frames", frames)
    with pytest.raises(tracked_logos.SubjectNotFoundError, match=r"^ingredient subject not found: the card, which the brand logo sits on, is seen in none of the 5 frames sampled"):
        asyncio.run(tracked_logos.composite("v.mp4", str(tmp_path / "o.mp4"), [_item()], nothing))

    async def unusable(_item, _frames, _reference, _outlined):
        return None

    with pytest.raises(tracked_logos.DetectionUnavailableError, match="came back unusable"):
        asyncio.run(tracked_logos.composite("v.mp4", str(tmp_path / "o.mp4"), [_item()], unusable))


@FFMPEG
def test_a_subject_seen_for_a_flicker_only_is_not_found(tmp_path):
    count = 30
    # A flat patch of the table: nothing for a track to hold on to past the one sampled frame the detector names it in.
    table = _shoot(_world(seed=9), _box(0, pan=0, zoom=0))
    ImageDraw.Draw(table).rectangle([80, 60, 420, 300], fill=(120, 120, 120))
    video = _encode(tmp_path, _footage(count, [], background=table))

    async def once(_item, frames: dict, _reference: bytes, _outlined: bool):
        names = list(frames)
        return {name: ([_sighting((150, 120))] if name == names[1] else []) for name in names}

    with pytest.raises(tracked_logos.SubjectNotFoundError, match=r"seen in 1 of the \d frames sampled but followed for \d consecutive frames? at most, under 6"):
        asyncio.run(tracked_logos.composite(video, str(tmp_path / "flicker.mp4"), [_item()], once))


# ── The detector's answer, bounded ────────────────────────────────────────


def test_the_detectors_answer_is_bounded_in_code():
    answer = """```json
    {"frame_0": [
        {"subject": [0.1, 0.2, 0.3, 0.4], "surface": [[0.2, 0.3], [0.3, 0.3], [0.3, 0.4], [0.2, 0.4]]},
        {"subject": [0.1, 0.2, 0.3, 0.4], "surface": [[0.21, 0.3], [0.31, 0.3], [0.31, 0.4], [0.21, 0.4]]},
        {"surface": [[0.6, 0.6], [0.7, 0.6], [0.7, 0.7], [0.6, 0.7]]},
        {"subject": [0.5, 0.5, 0.1, 0.1], "surface": [[0.5, 0.5], [0.6, 0.5], [0.6, 0.5], [0.5, 0.5]]},
        {"subject": [0.5, 0.5, 0.1, 0.1], "surface": [[0.5, 0.5], [0.501, 0.5], [0.501, 0.501], [0.5, 0.501]]},
        {"subject": "no", "surface": [[0.8, 0.1], [0.9, 0.1], [0.9, 0.2], [0.8, 0.2]]},
        "not a sighting"
     ],
     "frame_1": "none"}
    ```"""
    seen = tracked_logos.sightings_of(answer, ["frame_0", "frame_1", "frame_2"])
    assert seen is not None and seen["frame_1"] == [] and seen["frame_2"] == []
    # Two sightings of one surface are one; a surface in a line or too small is none; a box that cannot be read is
    # the surface's own bounds.
    assert [s.surface[0] for s in seen["frame_0"]] == [(0.2, 0.3), (0.6, 0.6), (0.8, 0.1)]
    assert seen["frame_0"][1].box == (0.6, 0.6, 0.7, 0.7) and seen["frame_0"][2].box == (0.8, 0.1, 0.9, 0.2)
    # Corners given in another order are put in the drawn order: top left first, clockwise.
    [turned] = tracked_logos.sightings_of(
        '{"f": [{"surface": [[0.3, 0.4], [0.2, 0.4], [0.2, 0.3], [0.3, 0.3]]}]}', ["f"]
    )["f"]
    assert turned.surface == ((0.2, 0.3), (0.3, 0.3), (0.3, 0.4), (0.2, 0.4))
    assert tracked_logos.sightings_of("no json here", ["f"]) is None
    assert tracked_logos.sightings_of("[1, 2]", ["f"]) is None


def test_the_sampled_frames_and_the_smoothing():
    assert tracked_logos.keyframes(48, 24) == [0, 12, 24, 35, 47]
    assert tracked_logos.keyframes(2, 24) == [0, 1] and tracked_logos.keyframes(0, 24) == []
    long = tracked_logos.keyframes(24 * 20, 24)
    assert len(long) == tracked_logos.MAX_KEYFRAMES and long[0] == 0 and long[-1] == 24 * 20 - 1
    square = np.array([[10.0, 10.0], [20.0, 10.0], [20.0, 20.0], [10.0, 20.0]])
    moved = np.array([1.0, 0.0])
    # A run of 8 frames is kept and averaged over 5, a run of 3 is a flicker.
    quads = {i: square + i * moved for i in range(8)} | {20: square, 21: square, 22: square}
    smooth = tracked_logos.smoothed(quads)
    assert sorted(smooth) == list(range(8))
    # Centred in the run, and still centred at its ends: a moving subject is never drawn behind itself.
    assert smooth[4][0][0] == pytest.approx(14.0) and smooth[1][0][0] == pytest.approx(11.0)
    assert smooth[0][0][0] == pytest.approx(10.0) and smooth[7][0][0] == pytest.approx(17.0)
    # One logo per subject: a later quad over an earlier one is the same subject.
    placed = [(2, square / [W, H]), (1, (square + moved) / [W, H]), (3, (square + 200 * moved) / [W, H])]
    assert [number for number, _q in tracked_logos._distinct(placed)] == [1, 3]


@FFMPEG
def test_the_native_check_finds_the_logo_as_given_and_misses_the_models_redraw(tmp_path):
    count = 24
    positions = [(120 + 3 * i, 100 + i) for i in range(count)]
    table = _shoot(_world(seed=9), _box(0, pan=0, zoom=0))
    given = _card(_logo())
    kept = []
    for index in range(count):
        frame = table.copy()
        frame.paste(given, positions[index])
        kept.append(frame)
    native = asyncio.run(tracked_logos.native_check(_encode(tmp_path, kept, "given.mp4"), [(_logo(), "the brand logo")]))
    assert native.passed and native.sampled == 5 and native.found == {"the brand logo": 5}
    redrawn = asyncio.run(
        tracked_logos.native_check(_encode(tmp_path, _footage(count, [positions]), "redrawn.mp4"), [(_logo(), "the brand logo")])
    )
    assert not redrawn.passed and redrawn.found == {"the brand logo": 0}
    assert redrawn.reason() == "the brand logo was found as given in none of the 5 frames sampled of the model's take"


# ── The contract ──────────────────────────────────────────────────────────


def test_a_logo_names_its_subject_and_nothing_else_may():
    [logo] = clips.parse_ingredients(
        [{"file_id": LOGO_FILE, "role": "logo", "label": "the brand logo", "subject": {"file_id": SUBJECT_FILE, "label": "  the cow  in the navy blanket "}}]
    )
    assert logo.tracked and logo.subject == clips.Subject(SUBJECT_FILE, "the cow in the navy blanket")
    assert logo.as_dict()["subject"] == {"file_id": SUBJECT_FILE, "label": "the cow in the navy blanket"}
    assert clips.ingredients_of({clips.INGREDIENTS: [logo.as_dict()]}) == [logo]
    with pytest.raises(clips.ClipError, match=r"ingredients\.0\.subject names the subject a logo sits on: a product"):
        clips.parse_ingredients([{"file_id": LOGO_FILE, "role": "product", "subject": {"file_id": SUBJECT_FILE}}])
    with pytest.raises(clips.ClipError, match=r"ingredients\.0\.subject is \{file_id, label\?\}"):
        clips.parse_ingredients([{"file_id": LOGO_FILE, "role": "logo", "subject": "the cow"}])
    square = [[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6]]
    with pytest.raises(clips.ClipError, match="give quad or subject, not both"):
        clips.parse_ingredients([{"file_id": LOGO_FILE, "role": "logo", "quad": square, "subject": {"file_id": SUBJECT_FILE}}])
    with pytest.raises(clips.ClipError, match="Give every logo its subject, or none"):
        clips.parse_ingredients(
            [
                {"file_id": LOGO_FILE, "role": "logo", "subject": {"file_id": SUBJECT_FILE}},
                {"file_id": START, "role": "logo"},
            ]
        )
    # The editor's body says the shape itself.
    from app.api.v1.schemas.video_studio import ClipIngredient

    body = ClipIngredient(file_id=LOGO_FILE, role="logo", subject={"file_id": SUBJECT_FILE, "label": "the cow"})
    assert body.model_dump(exclude_none=True)["subject"] == {"file_id": SUBJECT_FILE, "label": "the cow"}


def test_a_logo_with_a_subject_takes_the_tracked_path_on_a_model_without_references():
    class _None:
        def max_reference_images(self, _model):
            return 0

        def reference_images_with_start_frame(self, _model):
            return False

    class _Three:
        def max_reference_images(self, _model):
            return 3

        def reference_images_with_start_frame(self, _model):
            return True

    for start_frame in (True, False):
        assert clips.ingredient_path(_None(), "m", 1, start_frame=start_frame, logos=True, tracked=True) == clips.PATH_TRACKED
    assert clips.ingredient_path(_Three(), "m", 1, start_frame=True, logos=True, tracked=True) == clips.PATH_NATIVE
    tracked = clips.Ingredient(LOGO_FILE, "logo", "the brand logo", subject=clips.Subject(SUBJECT_FILE, "the cow"))
    product = clips.Ingredient(START, "product")
    # A tracked logo needs no start frame; an ingredient the prompt points at does.
    assert not clips.needs_start_frame([tracked], clips.PATH_TRACKED)
    assert clips.needs_start_frame([tracked, product], clips.PATH_TRACKED)
    assert not clips.needs_start_frame([tracked, product], clips.PATH_NATIVE)
    params = {clips.INGREDIENTS: [tracked.as_dict(), product.as_dict()], clips.INGREDIENTS_PATH: clips.PATH_TRACKED}
    assert [item.name for item in clips.tracked_logos(params)] == ["the brand logo"]
    assert clips.tracked_logos({**params, clips.INGREDIENTS_PATH: clips.PATH_COMPOSITED}) == []
    assert clips.composited_logos(params) == []
    # The prompt names the product the start frame shows, never the logo drawn onto its subject.
    assert clips.with_ingredients("A street.", params) == "A street. Keep the product exactly as in the reference image."
    assert clips.reference_note(params) == (
        "Ingredients (the brand logo, the product) drawn with their own pixels onto their subject (the cow) wherever "
        "the take shows it, once the shot is made: the model takes no picture of them with this shot."
    )
    native = {**params, clips.INGREDIENTS_PATH: clips.PATH_NATIVE}
    assert [item.name for item in clips.native_logos_with_subject(native)] == ["the brand logo"]
    assert clips.native_logos_with_subject(params) == []
    # The subject's picture is one of the files the shot is made from.
    assert f"file:{SUBJECT_FILE}" in clips.source_refs(params) and f"file:{LOGO_FILE}" in clips.source_refs(params)


@pytest.mark.usefixtures("no_default_video_template")
def test_a_tracked_logo_alone_needs_no_start_frame(monkeypatch):
    from app.services import file_access
    from tests.test_video_studio_ingredients import USER as PERSON
    from tests.test_video_studio_ingredients import _Files, _resolving

    async def can_read(_row, _user, _db):
        return True

    async def fine(_row):
        return None

    monkeypatch.setattr(file_access, "can_read_file", can_read)
    monkeypatch.setattr(clips, "check_start_picture", fine)
    _resolving(monkeypatch, SimpleNamespace(max_reference_images=lambda _m: 0, reference_images_with_start_frame=lambda _m: False), start_frame=False)
    asked = [{"file_id": LOGO_FILE, "role": "logo", "label": "the brand logo", "subject": {"file_id": SUBJECT_FILE, "label": "the cow"}}]
    clip, _label = asyncio.run(clips.start_clip(_Files(), PERSON, prompt="A street the cow walks into", ingredients=asked))
    assert clip.params[clips.INGREDIENTS_PATH] == clips.PATH_TRACKED and clips.REFERENCE_IMAGES not in clip.params
    assert clip.params[clips.INGREDIENTS][0]["subject"] == {"file_id": SUBJECT_FILE, "label": "the cow"}
    assert clip.prompt == "A street the cow walks into"
    # With a product beside it and no start frame, the prompt has nothing to point at: refused, the way out named.
    with pytest.raises(clips.ClipError, match="give a logo its subject") as refused:
        asyncio.run(
            clips.start_clip(_Files(), PERSON, prompt="x", ingredients=[*asked, {"file_id": START, "role": "product"}])
        )
    assert refused.value.about_model


# ── run_clip ──────────────────────────────────────────────────────────────


def _serving(take, monkeypatch, *, subject: bytes) -> None:  # noqa: F811
    """The store serving the logo, the subject's picture and the files the run stored."""
    from app.services.video_studio import assets

    async def download_file(file_id, target):
        if str(file_id) == LOGO_FILE:
            data, mime = take.logo, "image/png"
        elif str(file_id) == SUBJECT_FILE:
            data, mime = subject, "image/png"
        else:
            data, mime = take.files.get(str(file_id)), "video/mp4"
        if data is None:
            return None
        Path(target).write_bytes(data)
        return SimpleNamespace(content_type=mime)

    monkeypatch.setattr(assets, "download_file", download_file)


@FFMPEG
def test_run_clip_draws_the_logo_onto_its_subject_and_reports_the_tracked_path(take, tmp_path, monkeypatch):  # noqa: F811
    count = 36
    positions = [_walk(i, start_x=-120.0) for i in range(count)]
    take.video = Path(_encode(tmp_path, _footage(count, [positions]))).read_bytes()
    take.start = _png(_shoot(_world(seed=9), _box(0, pan=0, zoom=0)))
    take.logo = _png(_logo())
    _serving(take, monkeypatch, subject=_png(_reference()))
    monkeypatch.setattr(tracked_logos, "detector", lambda _user_id: _truth([positions], count))
    take.row.params.update(
        {
            clips.INGREDIENTS: [
                {"file_id": LOGO_FILE, "role": "logo", "label": "the brand logo", "subject": {"file_id": SUBJECT_FILE, "label": "the card"}}
            ],
            clips.INGREDIENTS_PATH: clips.PATH_TRACKED,
        }
    )
    asyncio.run(runners.run_clip(str(take.row.id)))
    row = take.row
    assert row.status == JOB_READY, row.error
    assert [step["step"] for step in row.params[runners._TAKE_STEPS]] == ["tracked"]
    record = frame_qc.logo_tracked(row.params[frame_qc.QC_KEY])
    assert record["status"] == "drawn" and record["share"] >= 0.7 and record["instances"] == 1
    assert record["subjects"] == ["the card"] and "fell_back_from" not in record
    assert frame_qc.summary(row.params)["logo_tracked"] == record
    assert row.params[clips.INGREDIENTS_PATH] == clips.PATH_TRACKED
    assert any(note.startswith("The brand logo drawn with its own pixels onto the card") for note in row.params["notes"])
    kept = take.tmp / "kept.mp4"
    kept.write_bytes(take.files[str(row.file_id)])
    x, y = positions[30]
    x0, y0, x1, y1 = x + PATCH[0] + 6, y + PATCH[1] + 6, x + PATCH[2] - 6, y + PATCH[3] - 6
    expected = np.asarray(_card(_logo()), dtype=np.float32)[PATCH[1] + 6 : PATCH[3] - 6, PATCH[0] + 6 : PATCH[2] - 6]
    assert np.abs(_decoded(str(kept))[30][y0:y1, x0:x1].astype(np.float32) - expected).mean() < 30


@FFMPEG
def test_run_clip_falls_back_to_the_tracked_path_when_the_native_take_fails_the_logo_check(take, tmp_path, monkeypatch):  # noqa: F811
    count = 30
    positions = [(120 + 3 * i, 100 + i) for i in range(count)]
    take.video = Path(_encode(tmp_path, _footage(count, [positions]))).read_bytes()
    take.start = _png(_shoot(_world(seed=9), _box(0, pan=0, zoom=0)))
    take.logo = _png(_logo())
    _serving(take, monkeypatch, subject=_png(_reference()))
    monkeypatch.setattr(tracked_logos, "detector", lambda _user_id: _truth([positions], count))
    take.row.params.update(
        {
            clips.INGREDIENTS: [{"file_id": LOGO_FILE, "role": "logo", "label": "the brand logo", "subject": {"file_id": SUBJECT_FILE}}],
            clips.INGREDIENTS_PATH: clips.PATH_NATIVE,
            clips.REFERENCE_IMAGES: [LOGO_FILE],
        }
    )
    asyncio.run(runners.run_clip(str(take.row.id)))
    row = take.row
    assert row.status == JOB_READY, row.error
    # The model's take, checked for the logo as given, failed: the tracked path drew it, and the path says so.
    assert row.params[clips.INGREDIENTS_PATH] == clips.PATH_TRACKED
    assert [step["step"] for step in row.params[runners._TAKE_STEPS]] == ["tracked"]
    record = frame_qc.logo_tracked(row.params[frame_qc.QC_KEY])
    assert record["fell_back_from"] == clips.PATH_NATIVE and record["share"] > 0.9 and record["subjects"] == ["the subject"]
    assert any(note.startswith("The model's take failed the logo check (the brand logo was found as given in none") for note in row.params["notes"])


@FFMPEG
def test_run_clip_fails_a_take_whose_surface_is_lost_and_keeps_the_frame(take, tmp_path, monkeypatch):  # noqa: F811
    count = 36
    positions = [(120 + 3 * i, 100 + i) for i in range(count)]
    take.video = Path(_encode(tmp_path, _covered(_footage(count, [positions]), positions, range(20, 24)))).read_bytes()
    take.start = _png(_shoot(_world(seed=9), _box(0, pan=0, zoom=0)))
    take.logo = _png(_logo())
    _serving(take, monkeypatch, subject=_png(_reference()))
    monkeypatch.setattr(tracked_logos, "detector", lambda _user_id: _truth([positions], count))
    take.row.params.update(
        {
            clips.INGREDIENTS: [{"file_id": LOGO_FILE, "role": "logo", "subject": {"file_id": SUBJECT_FILE, "label": "the card"}}],
            clips.INGREDIENTS_PATH: clips.PATH_TRACKED,
        }
    )
    asyncio.run(runners.run_clip(str(take.row.id)))
    row = take.row
    assert row.status == JOB_FAILED and row.error.startswith("ingredient surface lost at t=0.83 s (frame 20) of the shot")
    assert row.file_id is not None and str(row.file_id) not in take.deleted
    lost = next(qc for qc in [row.params.get(frame_qc.QC_KEY), *(a.get("frame_qc") for a in row.params.get(clips.ATTEMPTS) or [])] if qc)
    frame_id = lost[frame_qc.LOST_FRAME]
    assert row.error.endswith(f"(frame: file `{frame_id}`)") and take.files[frame_id].startswith(b"\xff\xd8")
    assert frame_id in frame_qc.frame_file_ids({frame_qc.QC_KEY: lost})


def test_the_tool_answer_shape_carries_the_tracked_record():
    params = {frame_qc.QC_KEY: {frame_qc.LOGO_TRACKED: tracked_logos.Tracked(10, 7, 1, 3, 2, ["the brand logo"], ["the cow"]).record()}}
    summary = frame_qc.summary(params)
    assert summary["logo_tracked"]["share"] == 0.7 and summary["logo_tracked"]["labels"] == ["the brand logo"]
    assert frame_qc.summary({})["logo_tracked"] is None and frame_qc.summary({})["text_check"] is None
    # The record is carried when the frames are cut again, like the stabilisation.
    assert frame_qc.LOGO_TRACKED in frame_qc.CARRIED
