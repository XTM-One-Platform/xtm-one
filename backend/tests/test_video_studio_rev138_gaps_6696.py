"""Three gaps of the rev 138 sweep: a jittering insert quad, a lexicon term read letter by letter, screen inserts
on an image scene. Synthetic data only: audits, voices, pictures and quads made here.

* a quad jumping on one frame against the frame before it, beyond the motion of the frames around it, fails
  ``insert_jitter`` past 0.1% of the frame's width (``insert_checks.quad_jitter``): the sweep's 0.4% jump between
  26.000 s and 26.017 s, 0.3% every other frame - its worst jump in the report's facts -, while a quad drifting
  smoothly 0.5% over 8 s passes it and still fails the drift gate, and a quad stepping with a footage slower than the
  render never jitters; the finding's evidence is its two frames side by side (``insert_checks.jitter_proofs``);
* a term read as words that its capitals spell (``CTEM``, read ``see-tem``) is held to the voice's syllables
  (``pronunciation_check.nuclei_in``): voiced "C-T-E-M" it fails as read letter by letter, voiced "see tem" it passes,
  the transcript writing ``CTEM`` for both; a video whose lexicon has no entry for a brand term is told so at save;
* an image scene takes screen inserts as a statement's picture backdrop does: two static inserts are measured, planned
  and sized alike on both, green on both, and the page places them on the same pixels.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.services.video_studio import (
    insert_bezels,
    insert_builds,
    insert_checks,
    lexicon,
    phonetics,
    pronunciation_check,
    render,
    timeline,
    voice_check,
)
from app.services.video_studio.render_checks import FAIL, INSERT_ALIGNMENT, INSERT_JITTER
from app.services.video_studio.spec import parse_video_spec, validate_video_spec

# ── 1. A quad jittering from one frame to the next ────────────────────────

FPS = 60.0
KEY = "insert/wall.0"
QUAD = ((0.30, 0.22), (0.70, 0.22), (0.70, 0.60), (0.30, 0.60))


def _placed() -> dict:
    return {KEY: insert_checks.Placed(KEY, "wall", "Scene wall: its screen insert 0", 0.0, 60.0)}


def _frames(first: int, count: int, shift, **audit) -> dict[int, dict]:
    """The audits of the output frames *first* on, the quad drawn moved ``shift(n)`` of the frame's width to the right;
    each other field of *audit* a value, or a function of the frame."""
    return {
        n: {
            "refined": [[x + shift(n), y] for x, y in QUAD],
            "size": [1920, 1080],
            **{name: value(n) if callable(value) else value for name, value in audit.items()},
        }
        for n in range(first, first + count)
    }


def _findings(frames: dict[int, dict]) -> list:
    return [finding for finding, _proof in insert_checks.findings({KEY: {"frames": frames, "evidence": {}}}, _placed(), fps=FPS)]


def _report(frames: dict[int, dict]) -> dict:
    [report] = insert_checks.reports({KEY: {"frames": frames, "evidence": {}}}, _placed(), fps=FPS)
    return report


def test_the_sweeps_jump_fails_between_its_two_frames_whatever_the_mean_alignment():
    # 26.000 s clean, 26.017 s 0.4% off, clean again: 0.11% off the bezel on every frame, the mean the gate called green.
    frames = _frames(1500, 120, lambda n: 0.004 if n == 1561 else 0.0, alignment=0.0011)
    [finding] = _findings(frames)
    assert finding.kind == INSERT_JITTER and finding.severity == FAIL
    assert finding.message.startswith("insert jitters: 0.4% between 26.000 s and 26.017 s: Scene wall: its screen insert 0's ")
    assert finding.data["jumped"] == [1560, 1561] and finding.data["worst"] == pytest.approx(0.004)
    assert finding.data["gate"] == "jitter" and finding.data["threshold"] == insert_checks.JITTER_MAX_SHARE == 0.001
    # The quad kept its error to the bezel across the jump: the bezel jumped with it, the footage itself shook.
    assert finding.data["action"]["kind"] == insert_checks.REGENERATE_SHOT
    report = _report(frames)
    assert report["alignment_max"] == pytest.approx(0.0011) and report["jitter_max"] == pytest.approx(0.004)
    assert report["jitter_frames"] == [1560, 1561] and report["jitter_at"] == [26.0, 26.017]
    assert report["verdicts"]["jitter"] is False
    held = [m for m in insert_checks.classify(report, {INSERT_JITTER}) if m.kind == INSERT_JITTER]
    [said] = [m for m in held if m.text.startswith("quad jitter")]
    assert said.cls == insert_checks.FINDING and "quad jitter at most 0.40% of the frame's width" in said.text
    assert "between 26.000 s and 26.017 s" in said.text
    # The coverage line's worst frame fails the jitter gate: the jitter finding holds that line too, never a fact.
    [covered] = [m for m in held if m is not said]
    assert covered.cls == insert_checks.FINDING and covered.text.startswith("its gates read ") and "over a gate" in covered.text


def test_a_quad_jittering_0_3_percent_every_other_frame_fails():
    # Off its bezel by 0.11% and 0.41% in turn: the bezel stays, the quad leaves it every other frame.
    frames = _frames(1500, 120, lambda n: 0.003 * (n % 2), alignment=lambda n: 0.0011 + 0.003 * (n % 2))
    jitters = [f for f in _findings(frames) if f.kind == INSERT_JITTER]
    [finding] = jitters
    assert finding.message.startswith("insert jitters: 0.3% between ")
    assert finding.data["frames"] == 118 and finding.data["action"]["kind"] == insert_checks.GIVE_QUAD
    assert _report(frames)["jitter_max"] == pytest.approx(0.003)
    # Under the alignment gate on every frame: only the jitter gate sees it.
    assert INSERT_ALIGNMENT not in {f.kind for f in _findings(frames)}


def test_a_quad_drifting_smoothly_passes_the_jitter_gate_and_still_fails_the_drift_gate():
    # 0.5% over 8 s at 60 fps, the bezel staying where the quad was 0.1% off at first.
    drift = lambda n: 0.005 * (n - 1500) / 479  # noqa: E731
    frames = _frames(1500, 480, drift, alignment=lambda n: 0.001 + drift(n))
    kinds = [f.kind for f in _findings(frames)]
    assert INSERT_JITTER not in kinds and INSERT_ALIGNMENT in kinds
    report = _report(frames)
    assert report["jitter_max"] < 1e-6 and report["verdicts"]["jitter"] is True and report["verdicts"]["alignment"] is False


def test_a_quad_stepping_with_a_footage_slower_than_the_render_never_jitters():
    # A 24 fps shot panning 0.5% of the frame a footage frame, drawn at 60 fps: two or three frames on each quad.
    frames = _frames(0, 300, lambda n: 0.005 * (n * 24 // 60), alignment=0.0011)
    assert INSERT_JITTER not in {f.kind for f in _findings(frames)}
    assert _report(frames)["jitter_max"] < 1e-6
    # Out and back is a jump; a frame not drawn parts two runs, and nothing is compared across it.
    pulse = _frames(0, 60, lambda n: 0.004 if 20 <= n < 40 else 0.0)
    assert insert_checks.quad_jitter(pulse).share == pytest.approx(0.004)
    assert insert_checks.quad_jitter({n: audit for n, audit in pulse.items() if n != 40}) is None


def test_its_evidence_is_both_frames_side_by_side(monkeypatch, tmp_path):
    from app.services.video_studio import encoder

    def frame(shift: int) -> Image.Image:
        picture = Image.new("RGB", (320, 180), (40, 40, 40))
        ImageDraw.Draw(picture).rectangle((96 + shift, 40, 224 + shift, 108), fill=(220, 220, 220))
        return picture

    async def extract_frames(_video, wanted, *, fps, out_dir):
        assert fps == FPS and sorted(wanted) == [10, 11]
        out = {}
        for n in wanted:
            out[n] = os.path.join(out_dir, f"frame-{n}.png")
            frame(0 if n == 10 else 2).save(out[n])
        return out

    monkeypatch.setattr(encoder, "extract_frames", extract_frames)
    finding = insert_checks.Finding(INSERT_JITTER, 0.2, 0.2, "wall", "insert jitters", FAIL, {"insert": KEY, "jumped": [1010, 1011]})
    audits = {KEY: {"placed": {"1010": [0.3, 0.22, 0.7, 0.6, 1.0], "1011": [0.306, 0.22, 0.706, 0.6, 1.0]}}}
    [(kept, proof)] = asyncio.run(
        insert_checks.jitter_proofs("final.mp4", [(finding, None)], audits, fps=FPS, workdir=tmp_path, first_frame=1000)
    )
    assert kept is finding and proof is not None
    with Image.open(proof) as sheet:
        sheet = sheet.convert("RGB")
    # Where the page placed it on either frame (96-226 px across, 40-108 down) grown a quarter each side, at 100%: the
    # two crops, 4 px apart.
    box = (63, 22, 258, 125)
    width, height = box[2] - box[0], box[3] - box[1]
    assert sheet.size == (2 * width + 4, height)
    assert np.array_equal(np.asarray(sheet.crop((0, 0, width, height))), np.asarray(frame(0).crop(box)))
    assert np.array_equal(np.asarray(sheet.crop((width + 4, 0, 2 * width + 4, height))), np.asarray(frame(2).crop(box)))
    # A finding that already shows a frame, or one of another kind, is left as it is.
    other = insert_checks.Finding(INSERT_ALIGNMENT, 0.2, 0.2, "wall", "off", FAIL, {"insert": KEY})
    assert asyncio.run(insert_checks.jitter_proofs("final.mp4", [(other, None)], audits, fps=FPS, workdir=tmp_path)) == [(other, None)]


# ── 2. A lexicon term read letter by letter ───────────────────────────────

SEED = lexicon.load_seed()
LEXICON = lexicon.merge(SEED)
RATE = 16000
_rng = np.random.default_rng(6696)


def _entry(text: str) -> lexicon.Entry:
    return next(e for e in SEED if e.text == text)


def _vowel(seconds: float, first_formant: float = 400.0) -> np.ndarray:
    """A voiced vowel at 140 Hz, its harmonics near *first_formant* the loudest, rising and falling at its ends."""
    t = np.arange(int(seconds * RATE)) / RATE
    wave = sum(np.sin(2 * np.pi * 140.0 * k * t) / k * (1.6 if abs(140.0 * k - first_formant) < 250 else 0.5) for k in range(1, 22))
    return 0.25 * wave * np.minimum(1.0, np.minimum(t / 0.03, (seconds - t) / 0.04))


def _hiss(seconds: float, level: float = 0.05) -> np.ndarray:
    """A fricative or a stop's burst: noise tilted up, all of it above a vowel's band."""
    return level * np.diff(np.concatenate([[0.0], _rng.standard_normal(int(seconds * RATE))]))


def _nasal(seconds: float) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return 0.08 * np.sin(2 * np.pi * 250.0 * t) * np.minimum(1.0, (seconds - t) / 0.03)


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * RATE))


#: "see tem": two syllables, fluent.
SEE_TEM = np.concatenate([_hiss(0.09), _vowel(0.17, 300), _silence(0.03), _hiss(0.015, 0.2), _vowel(0.15, 550), _nasal(0.08)])
#: "C-T-E-M": four letter names said together, a 50 ms closure between them - never a pause.
LETTERS = np.concatenate(
    [
        _hiss(0.09),
        _vowel(0.16, 300),
        _silence(0.05),
        _hiss(0.015, 0.2),
        _vowel(0.16, 300),
        _silence(0.05),
        _vowel(0.16, 300),
        _silence(0.05),
        _vowel(0.13, 550),
        _nasal(0.08),
    ]
)


def _narrated(term: np.ndarray) -> tuple[np.ndarray, list[tuple[str, float, float]]]:
    """"We measure CTEM every day." voiced with *term* for CTEM, and the words a speech-to-text writes back for it."""
    head = np.concatenate([_vowel(0.18), _silence(0.05), _vowel(0.2), _silence(0.25)])
    tail = np.concatenate([_silence(0.25), _vowel(0.2), _silence(0.05), _vowel(0.22)])
    start = len(head) / RATE
    end = start + len(term) / RATE
    words = [("We", 0.0, 0.18), ("measure", 0.23, 0.43), ("CTEM", round(start, 3), round(end, 3))]
    words += [("every", end + 0.25, end + 0.45), ("day", end + 0.5, end + 0.72)]
    return np.concatenate([head, term, tail]), words


def test_a_term_read_as_words_is_told_from_its_letters_by_its_syllables():
    ctem = _entry("CTEM")
    assert phonetics.syllables(ctem.phonemes) == 2 and pronunciation_check.spelled_syllables(ctem) == 4
    # Read letter by letter by its entry, or no more syllables at sight than its reading: nothing to tell apart.
    for written in ("XTM", "OpenCTI", "Filigran", "THREAD"):
        assert pronunciation_check.spelled_syllables(_entry(written)) is None, written


@pytest.mark.parametrize(("voiced", "passes"), [(SEE_TEM, True), (LETTERS, False)], ids=["see tem", "C-T-E-M"])
def test_a_narration_voiced_letter_by_letter_fails_and_one_voiced_see_tem_passes(voiced, passes):
    audio, words = _narrated(voiced)
    text = "We measure CTEM every day."
    pauses, nuclei = pronunciation_check.pauses_in(audio, RATE), pronunciation_check.nuclei_in(audio, RATE)
    (term,) = pronunciation_check.hear(text, words, LEXICON, pauses=pauses, nuclei=nuclei).terms
    # Whisper writes CTEM for both: the transcript alone accepts both, as it did.
    assert term.reading == "written" and pronunciation_check.hear(text, words, LEXICON, pauses=pauses).terms[0].passed
    assert term.pause is None and term.passed is passes
    check = voice_check.compare(text, " ".join(w for w, _s, _e in words), lexicon=LEXICON, words=words, pauses=pauses, nuclei=nuclei)
    assert check.passed is passes
    if passes:
        return
    assert term.syllables == 4 and term.reading_syllables == 2
    assert term.message() == "brand term mispronounced: CTEM read letter by letter at 0:00.7 (4 syllables heard where its reading says 2)"
    assert term.message() in check.reason()
    assert term.as_dict()["syllables"] == {"heard": 4, "reading": 2}


def test_a_brand_term_with_no_entry_in_the_videos_lexicon_is_said_at_save():
    from app.services.video_studio.quality import ContentFacts, content_findings
    from app.services.video_studio.spec import authored_spec

    (warning,) = lexicon.pronunciation_warnings("We measure CTEM every day.", lexicon.Lexicon(), lexicon.brand_lexicon())
    assert warning.startswith('CTEM has no pronunciation; the voice may spell it - the brand reads it "see-tem": add ')
    assert '"say": "see-tem"' in warning and '"ipa": "siːtɛm"' in warning
    # A brand term read as letters by the brand is no spelling risk: the voice guesses it, said apart from the others.
    (xtm,) = lexicon.pronunciation_warnings("XTM ships today.", lexicon.Lexicon(), lexicon.brand_lexicon())
    assert xtm.startswith("XTM has no pronunciation; the voice guesses how to say it")
    # With its entry - the platform's or the video's - nothing is said of it.
    assert lexicon.pronunciation_warnings("We measure CTEM every day.", LEXICON, lexicon.brand_lexicon()) == []
    own = lexicon.merge([], [{"text": "CTEM", "say": "see-tem"}])
    assert lexicon.pronunciation_warnings("We measure CTEM every day.", own, lexicon.brand_lexicon()) == []
    # At save: the lint of a video whose lexicon has no entry for it.
    raw = {"title": "CTEM", "freestyle": True, "scenes": [{"id": "s1", "type": "title", "title": "One", "narration": "We measure CTEM every day."}]}
    spec = parse_video_spec(validate_video_spec(raw))
    document = timeline.estimate_plan(spec, {}, {}).document
    found = [f for f in content_findings(spec, document, ContentFacts(lexicon=lexicon.Lexicon()), authored_spec(raw)) if f.kind == "pronunciation"]
    assert [(f.scene_id, f.severity) for f in found] == [("s1", "advisory")]
    assert "CTEM has no pronunciation; the voice may spell it" in found[0].message


# ── 3. Screen inserts on an image scene ───────────────────────────────────

STILL = "00000000-0000-0000-0000-00000000a696"
SHOWN = "00000000-0000-0000-0000-00000000b696"
#: The two monitors of :func:`_wall`, 0-1 of a 1280x720 still, and quads given within a few pixels of them.
LEFT = ((120 / 1280, 170 / 720), (560 / 1280, 170 / 720), (560 / 1280, 450 / 720), (120 / 1280, 450 / 720))
RIGHT = ((720 / 1280, 170 / 720), (1160 / 1280, 170 / 720), (1160 / 1280, 450 / 720), (720 / 1280, 450 / 720))


def _near(quad) -> list[list[float]]:
    return [[round(x + 0.003, 4), round(y + 0.003, 4)] for x, y in quad]


def _wall() -> Image.Image:
    """A 1280x720 room with two monitors side by side, each a flat green screen in a dark bezel."""
    picture = Image.new("RGB", (1280, 720), (46, 46, 46))
    draw = ImageDraw.Draw(picture)
    for x0 in (120, 720):
        draw.rectangle((x0 - 20, 150, x0 + 460, 470), fill=(18, 18, 20))
        draw.rectangle((x0, 170, x0 + 439, 449), fill=(0, 177, 64))
    return picture


def _inserts() -> list[dict]:
    return [
        {"type": "screen_insert", "source": {"file_id": SHOWN}, "quad": _near(quad), "key": {"color": "green"}, "start": 0.0, "duration": 4.0}
        for quad in (LEFT, RIGHT)
    ]


def _spec(kind: str):
    """The wall as an image scene, or as the statement an author had to write for it: a backdrop at dim 0."""
    scene = (
        {"id": "wall", "type": "image", "file_id": STILL, "fit": "cover", "motion": "zoom_in", "overlays": _inserts()}
        if kind == "image"
        else {"id": "wall", "type": "statement", "lines": ["Live"], "backdrop": {"file_id": STILL}, "dim": 0, "overlays": _inserts()}
    )
    return parse_video_spec(validate_video_spec({"title": "Wall", "freestyle": True, "scenes": [scene]}))


def _measured(kind: str, monkeypatch) -> list[insert_bezels.Measured]:
    from app.services.video_studio import assets, render_cache

    kept: dict = {}

    async def footage_of(_db, _scene):
        return (("file", STILL), STILL, True)

    async def download(_file_id, path):
        _wall().save(path, format="PNG")
        return SimpleNamespace(filename="wall.png")

    async def get_json(kind_, digest):
        return kept.get((kind_, digest))

    async def put_json(kind_, digest, value):
        kept[(kind_, digest)] = value

    async def put_file(kind_, digest, path, *, suffix=""):
        kept[(kind_, digest, suffix)] = path

    monkeypatch.setattr(insert_bezels, "_footage_of", footage_of)
    monkeypatch.setattr(assets, "download_file", download)
    monkeypatch.setattr(render_cache, "get_json", get_json)
    monkeypatch.setattr(render_cache, "put_json", put_json)
    monkeypatch.setattr(render_cache, "put_file", put_file)
    return asyncio.run(insert_bezels.measure(None, _spec(kind)))


def test_an_image_scenes_still_is_the_footage_its_inserts_land_on():
    from app.models.file_attachment import FileAttachment

    class Db:
        async def get(self, model, uid):
            assert model is FileAttachment and uid == uuid.UUID(STILL)
            return SimpleNamespace(content_type="image/png")

    scene = _spec("image").scenes[0]
    assert asyncio.run(insert_bezels._footage_of(Db(), scene)) == (("file", STILL), STILL, True)


def test_an_image_scene_with_two_static_inserts_measures_green_as_the_equivalent_statement(monkeypatch):
    image, statement = _measured("image", monkeypatch), _measured("statement", monkeypatch)
    assert [(m.scene_id, m.index, m.frame, m.seconds) for m in image] == [("wall", 0, 0, 0.0), ("wall", 1, 0, 0.0)]
    for mine, theirs in zip(image, statement, strict=True):
        assert mine.given == theirs.given and mine.bezel.quad == theirs.bezel.quad
        assert mine.bezel.error == theirs.bezel.error and mine.bezel.flatness.words() == theirs.bezel.flatness.words()
        assert mine.bezel.flatness.flat and mine.size == theirs.size == (1280, 720)
    # Each snapped to its own monitor's bezel, green: nothing refused, nothing found, every gate passed.
    for measured, quad in zip(image, (LEFT, RIGHT), strict=True):
        assert all(abs(a - b) < 0.004 for p, q in zip(measured.bezel.quad, quad, strict=True) for a, b in zip(p, q, strict=True))
    for measured in (image, statement):
        assert insert_bezels.findings(measured) == []
        assert all(record["gates"] and all(record["gates"].values()) for records in insert_bezels.verdicts(measured, 1).values() for record in records)
    # What the save says of them, word for word the statement's: both quads snapped to their bezels, nothing refused.
    said = {
        kind: insert_bezels.resolve({"title": "Wall", "freestyle": True, "scenes": [{"id": "wall", "type": kind, "overlays": _inserts()}]}, measured)
        for kind, measured in (("image", image), ("statement", statement))
    }
    assert not said["image"].problems and said["image"].notes == said["statement"].notes
    assert sum("snapped to it" in note for note in said["image"].notes) == 2


def _planned(kind: str, size: tuple[int, int] = (1280, 720)) -> dict:
    spec = _spec(kind)
    media = timeline.PlanMedia(takes={})
    if kind == "image":
        media.images["wall"] = timeline.ImageMedia(*size)
    else:
        media.backdrops["wall"] = timeline.ImageMedia(*size)
    media.inserts = {key: timeline.ImageMedia(1920, 1080) for key in timeline.insert_sources(spec)}
    media.inserts_gathered = True
    [scene] = timeline.plan(spec, media, fps=30, theme={}).document["scenes"]
    return scene


def test_an_image_scene_plans_and_sizes_its_inserts_as_the_statement():
    image, statement = _planned("image"), _planned("statement")
    drawn = ("src", "quad", "fit", "glow", "light", "t0", "t1", "kind", "keyed", "src_w", "src_h")
    assert [{k: p.get(k) for k in drawn} for p in image["inserts"]] == [{k: p.get(k) for k in drawn} for p in statement["inserts"]]
    assert len(image["inserts"]) == 2
    assert timeline.insert_footage(image) is image and timeline.insert_footage(statement) is statement["backdrop"]
    assert timeline.footage_still(image) and timeline.footage_still(statement["backdrop"])
    # The host their quads are shares of, as the page draws it: the same pixels, so the same density at every size.
    for size in ((1920, 1080), (3840, 2160)):
        assert render.host_size(image, size) == pytest.approx(render.host_size(statement, size))
        assert render.host_size(image, size) == pytest.approx((size[0] * timeline.STILL_HOST_ZOOM, size[1] * timeline.STILL_HOST_ZOOM))
    # A still of other proportions, contained: the box the page frames it in, 86% of the width or 82% of the height.
    contained = {**_planned("image", (1200, 900)), "fit": "contain"}
    width, height = render.host_size(contained, (1920, 1080))
    assert (width, height) == pytest.approx((1080 * 0.82 * 4 / 3 * timeline.STILL_HOST_ZOOM, 1080 * 0.82 * timeline.STILL_HOST_ZOOM))


def test_a_preview_draws_no_build_over_a_still():
    spec = _spec("image")
    document = {"scenes": [_planned("image")]}
    ids = {"image/wall": STILL, **{key: SHOWN for key in timeline.insert_sources(spec)}}
    assert insert_builds.builds_of(document, ids, spec) == []


@pytest.mark.chromium
async def test_the_page_places_an_image_scenes_inserts_on_the_pixels_a_statement_backdrop_gives(tmp_path):
    try:
        from playwright.async_api import async_playwright
    except ImportError:  # pragma: no cover - the backend image ships Playwright
        pytest.skip("Playwright is not installed")
    import io

    from app.services.headless_render import RENDER_CHROMIUM_ARGS
    from app.services.video_studio import compositor, screen_insert
    from app.services.video_studio.compositor_page import ORIGIN, page_html

    picture = tmp_path / "shown.png"
    Image.new("RGB", (400, 300), (200, 40, 40)).save(picture)
    inserts = {f"insert/wall.{n}": screen_insert.InsertSource(str(picture), "image", tuple(q)) for n, q in enumerate((LEFT, RIGHT))}
    server = compositor.FrameServer(compositor.CompositorAssets(inserts=inserts), page_html(""))
    buffer = io.BytesIO()
    _wall().resize((640, 360)).save(buffer, format="JPEG", quality=95)
    still = buffer.getvalue()

    async def handle(route) -> None:
        if "/image/wall" in route.request.url or "/backdrop/wall" in route.request.url:
            await route.fulfill(status=200, body=still, content_type="image/jpeg")
        else:
            await server.handle(route)

    payloads = [
        {"src": f"insert/wall.{n}", "kind": "image", "quad": [list(p) for p in q], "fit": "cover", "glow": 0.0, "t0": 0.0, "t1": 2.0}
        for n, q in enumerate((LEFT, RIGHT))
    ]
    common = {"id": "wall", "transition_in": {"type": "cut", "duration": 0}, "overlays": [], "inserts": payloads, "start": 0.0, "end": 2.0}
    scenes = {
        "image": {**common, "type": "image", "src": "image/wall", "motion": "zoom_in", "fit": "cover", "image_w": 640, "image_h": 360},
        "statement": {
            **common,
            "type": "statement",
            "lines": ["Live"],
            "secondary": [],
            "chips": [],
            "reveal": "together",
            "align": "center",
            "show_logo": False,
            "backdrop": {"src": "backdrop/wall", "dim": 0.0, "kind": "image", "motion": "zoom_in", "image_w": 640, "image_h": 360},
        },
    }
    placed: dict[str, list] = {}
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=list(RENDER_CHROMIUM_ARGS))
            try:
                for kind, scene in scenes.items():
                    context = await browser.new_context(viewport={"width": 640, "height": 360}, device_scale_factor=2)
                    await context.route("**/*", handle)
                    page = await context.new_page()
                    await page.goto(f"{ORIGIN}/index.html", wait_until="load")
                    document = {
                        "version": 1,
                        "width": 640,
                        "height": 360,
                        "fps": 30,
                        "duration": 2.0,
                        "frames": 60,
                        "fade_in": 0.0,
                        "fade_out": 0.0,
                        "theme": {},
                        "captions": {"enabled": False, "style": "pill", "position": "bottom", "cues": []},
                        "watermark": "none",
                        "scenes": [scene],
                    }
                    await page.evaluate("doc => window.__xtm.load(doc)", document)
                    await page.evaluate("i => window.__xtm.render(i)", 30)
                    placed[kind] = await page.evaluate(
                        "() => Array.from(document.querySelectorAll('img.ins')).map(n => { const r = n.getBoundingClientRect();"
                        " return [n.dataset.insert, n.dataset.src.split('?')[1], r.left, r.top, r.width, r.height]; })"
                    )
                    await context.close()
            finally:
                await browser.close()
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
    # Both inserts drawn, each warped at the size the picture's motion shows it at closest, on the same pixels mid-push.
    assert [item[0] for item in placed["image"]] == ["insert/wall.0", "insert/wall.1"]
    assert [item[1] for item in placed["image"]] == [item[1] for item in placed["statement"]]
    assert placed["image"][0][1].startswith(f"w={round(1280 * timeline.STILL_HOST_ZOOM)}&h={round(720 * timeline.STILL_HOST_ZOOM)}")
    for mine, theirs in zip(placed["image"], placed["statement"], strict=True):
        assert mine[2:] == pytest.approx(theirs[2:], abs=0.01)
