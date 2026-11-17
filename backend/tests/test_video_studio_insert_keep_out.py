"""The screen of an AI shot carrying a screen insert, kept clear: asked of the model, measured on the take.

Video b56997a2 rev 137: six regenerations of one locked-off shot, four drawn with a head, hands or a cow in front of
the TV although each was in the negative prompt, each paid and failed as content drift after the fact.

* F56: a shot carrying a screen insert (``insert: true``, a screen replacement, a screen measured on its start
  picture) is sent a keep-out sentence built from its quad, and - on a model that takes reference images beside a
  start frame - its start picture a second time as a reference image.
* F54 / F55: its take is matted frame by frame as the render mattes the insert; a negative-prompt term the content
  check saw only in front of the screen keeps the take with a note when what stands there covers under 10% of the
  quad, the matte on and the mask surer than 0.7 (4%: ready), and fails it as content drift past that (25%), the
  measure in the reason. The screen showing no key leaves the matte off: the term fails the take as before.
* A content-drift failure says which attempt the take was and how many of the 5 regenerations remain.

Synthetic frames, a synthetic prompt, a fake provider and an in-memory Redis only.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from app.llm.video import VideoRequest
from app.models.video_studio import JOB_FAILED, JOB_READY
from app.services.video_studio import clips, content_drift, encoder, frame_qc, insert_keep_out
from tests.test_video_studio_regeneration import PROMPT, _asked, _picture, _regenerate, _run, redis, world  # noqa: F401

#: The screen's quad on the start frame, 0-1 across and down: the rectangle x 0.30-0.70, y 0.20-0.60.
QUAD = [[0.3, 0.2], [0.7, 0.2], [0.7, 0.6], [0.3, 0.6]]
#: The screen measured on the start picture (``clips.SURFACE``): found by its green, flat.
SURFACE = {"quad": QUAD, "flatness": {"flat": True}, "error": None, "found_by": "frame", "hue": 122.0}
NEGATIVE = "hands over the screen, cow turning toward the screen"
LINE = "Keep-out: nothing enters the rectangle x 0.30-0.70, y 0.20-0.60 of the frame"
#: The green a video model paints a chroma screen, and a hand in front of it.
GREEN, HAND, ROOM = (32, 230, 40), (182, 124, 92), (70, 64, 58)


def _frames(share: float, *, keyed: bool = True, count: int = 12):
    """A take of *count* frames: a room, the screen green (or dark, *keyed* false), and from the middle frame on a hand
    entering through the screen's bottom edge over *share* of its quad."""

    async def frames(_path, *, width, height, resize=False):
        x0, y0, x1, y1 = 0.3 * width, 0.2 * height, 0.7 * width, 0.6 * height
        for index in range(count):
            image = Image.new("RGB", (width, height), ROOM)
            draw = ImageDraw.Draw(image)
            draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=GREEN if keyed else (20, 20, 22))
            if share and index >= count // 2:
                left, right = x0 + 0.3 * (x1 - x0), x0 + 0.7 * (x1 - x0)
                draw.rectangle((left, y1 - share / 0.4 * (y1 - y0), right, y1 + 0.05 * height), fill=HAND)
            yield image.tobytes()

    return frames


def _hands(keep_out: bool = True) -> content_drift.ContentCheck:
    """A content check that saw the negative prompt's hands, in front of the screen only (*keep_out*) or not."""
    shown = content_drift.Finding(
        "hands over the screen",
        "middle_frame",
        "shows hands over the screen, which the negative prompt excludes",
        excluded=True,
        keep_out=keep_out,
    )
    return content_drift.ContentCheck(True, findings=[shown])


@pytest.fixture
def shot(world, monkeypatch):  # noqa: F811
    """The fixture's shot as a new one carrying a screen insert (``insert: true``), its screen measured green."""
    _asked(world)
    world.row.params.update({clips.INSERT: True, clips.SURFACE: dict(SURFACE), "negative_prompt": NEGATIVE})
    asked = {}

    async def probe(_path):
        return SimpleNamespace(duration=8.0, width=1280, height=720, has_audio=False, fps=24.0)

    async def take_surface(*_args):
        return None, None

    async def content_check(*_args, **kwargs):
        asked.update(kwargs)
        return world.content

    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(clips, "take_surface", take_surface)
    monkeypatch.setattr(content_drift, "check", content_check)
    world.asked = asked
    return world


# ── F56: asked ────────────────────────────────────────────────────────────


def test_f56_the_keep_out_line_is_in_the_sent_prompt_of_a_shot_asked_insert_true(shot, monkeypatch):
    monkeypatch.setattr(encoder, "iter_rgb_frames", _frames(0.0))
    params = shot.row.params
    request = VideoRequest(model="veo-synthetic", prompt=PROMPT, negative_prompt=NEGATIVE)
    plan = clips.ClipPlan(
        request=request,
        config=SimpleNamespace(provider="gemini", display_name="Synthetic Veo"),
        params=params,
        sent=clips.provider_prompts(params, request.prompt, request.negative_prompt),
    )
    sent = plan.summary()["sent_prompt"]
    assert sent.startswith(PROMPT) and LINE in sent and plan.summary()["sent_negative_prompt"] == NEGATIVE
    # The worker sends the model that very prompt, and tells the content check the rectangle.
    _run(shot)
    assert LINE in shot.provider.requests[-1].prompt
    assert shot.asked["keep_out"] == (0.3, 0.7, 0.2, 0.6)


def test_a_shot_carrying_no_insert_is_sent_as_written():
    assert clips.provider_prompts({}, PROMPT, None) == (PROMPT, None)
    # insert: false carries none, whatever its start picture showed.
    assert clips.provider_prompts({clips.INSERT: False, clips.SURFACE: dict(SURFACE)}, PROMPT, None) == (PROMPT, None)
    # insert: true with no screen measured has no rectangle to keep clear.
    assert insert_keep_out.rectangle_of({clips.INSERT: True}) is None


def test_a_screen_replacement_is_kept_clear_on_its_own_quad_within_the_prompt_limit():
    screen = {"file_id": "shot", "quad": [[384, 144], [896, 144], [896, 432], [384, 432]], "start_size": [1280, 720]}
    params = {clips.SCREEN: screen}
    assert insert_keep_out.rectangle_of(params) == (0.3, 0.7, 0.2, 0.6)
    prompt, _negative = clips.provider_prompts(params, "x" * 5000, None)
    assert len(prompt) <= clips.MAX_PROMPT_CHARS and prompt.endswith(insert_keep_out.line((0.3, 0.7, 0.2, 0.6)))
    # A rectangle widened to whole hundredths covers the screen whole.
    assert insert_keep_out.rectangle(((0.304, 0.207), (0.695, 0.2), (0.7, 0.601), (0.3, 0.6))) == (0.3, 0.7, 0.2, 0.61)


def test_the_start_picture_goes_again_as_a_reference_on_a_model_that_takes_both(shot, monkeypatch):
    monkeypatch.setattr(encoder, "iter_rgb_frames", _frames(0.0))
    start = clips.StartFrame(_picture(), "image/jpeg", "start-picture")

    async def start_frame(*_args, **_kwargs):
        return start

    monkeypatch.setattr(clips, "start_frame", start_frame)
    shot.row.params.update({"reference_file_id": "start-picture", "text_check": False})
    veo = insert_keep_out.note(shot.row.params, shot.provider, "veo-synthetic")
    assert "takes no reference image beside a start frame: its start picture is sent once" in veo
    _run(shot)
    request = shot.provider.requests[-1]
    assert request.reference_image == start.data and request.reference_images == []
    # A model taking reference images beside a start frame (Gemini Omni's kind) is sent it a second time.
    shot.provider.max_reference_images = lambda _model: 3
    shot.provider.reference_images_with_start_frame = lambda _model: True
    assert "its start picture sent a second time as a reference image" in insert_keep_out.note(
        shot.row.params, shot.provider, "omni-synthetic"
    )
    _asked(shot)
    shot.row.provider_job_id = None
    _run(shot)
    request = shot.provider.requests[-1]
    assert request.reference_image == start.data and request.reference_images == [(start.data, start.mime)]


# ── F54 / F55: measured and judged ────────────────────────────────────────


def test_f54_a_take_whose_hand_covers_4_percent_of_the_matted_screen_is_ready_with_a_note(shot, monkeypatch):
    monkeypatch.setattr(encoder, "iter_rgb_frames", _frames(0.04))
    shot.content = _hands()
    _run(shot)
    row = shot.row
    assert row.status == JOB_READY
    measured = frame_qc.summary(row.params)["foreground_overlap"]
    assert measured["key"] == "green" and 0.03 < measured["max_share"] < 0.05
    assert measured["frame"] == 6 and measured["at"] == 0.25 and measured["confidence"] > 0.7
    [kept] = [note for note in row.params["notes"] if note.startswith("Kept: hands over the screen")]
    assert "covers at most 4% of its quad (frame 6, 0.25 s), matted by its green key" in kept
    # Accepted, the term is no take shown against the negative prompt: the next take is not asked it harder.
    assert row.params[clips.EXCLUDED_SHOWN][-1]["terms"] == []


def test_f55_a_take_whose_hand_covers_25_percent_of_the_screen_fails_as_content_drift(shot, monkeypatch):
    monkeypatch.setattr(encoder, "iter_rgb_frames", _frames(0.25))
    shot.content = _hands()
    _run(shot)
    row = shot.row
    assert row.status == JOB_FAILED and row.error.startswith("content drift:")
    assert "On the screen, what stands in front of it covers at most 25% of its quad (frame 6, 0.25 s)" in row.error
    assert "a term in front of the screen is kept only under 10% of its quad" in row.error
    assert row.error.endswith("Attempt 1 of this shot: 5 of the 5 regenerations of this prompt and start frame remain.")
    assert row.params[clips.EXCLUDED_SHOWN][-1]["terms"] == ["hands over the screen"]


def test_a_screen_showing_no_key_leaves_the_matte_off_and_the_term_fails_the_take(shot, monkeypatch):
    monkeypatch.setattr(encoder, "iter_rgb_frames", _frames(0.04, keyed=False))
    shot.content = _hands()
    _run(shot)
    assert shot.row.status == JOB_FAILED
    assert "the foreground matte is off" in shot.row.error
    assert frame_qc.summary(shot.row.params)["foreground_overlap"]["key"] is None


def test_a_term_seen_elsewhere_than_in_front_of_the_screen_is_never_the_measures_to_keep(shot, monkeypatch):
    monkeypatch.setattr(encoder, "iter_rgb_frames", _frames(0.04))
    shot.content = _hands(keep_out=False)
    _run(shot)
    assert shot.row.status == JOB_FAILED
    # The measure is still said with the failure: what stands in front of the screen, at most 4%.
    assert "On the screen, what stands in front of it covers at most 4% of its quad" in shot.row.error


def test_a_take_that_passes_says_what_stands_in_front_of_its_screen(shot, monkeypatch):
    monkeypatch.setattr(encoder, "iter_rgb_frames", _frames(0.0))
    _run(shot)
    assert shot.row.status == JOB_READY
    assert "On the screen of the shot, nothing stands in front of it on any of the 12 frames, matted by its green key." in (
        shot.row.params["notes"]
    )


def test_a_mask_not_sure_enough_never_keeps_the_take():
    sure = insert_keep_out.Overlap(12, "green", 0.04, 6, 0.25, 0.9, 6)
    assert sure.tolerated()
    assert not insert_keep_out.Overlap(12, "green", 0.04, 6, 0.25, 0.55, 6).tolerated()
    assert not insert_keep_out.Overlap(12, "green", 0.10, 6, 0.25, 0.9, 6).tolerated()
    assert not insert_keep_out.Overlap(0, None).tolerated()
    assert insert_keep_out.Overlap.from_dict(sure.as_dict()) == sure


# ── The reading ───────────────────────────────────────────────────────────


def test_the_reading_says_which_terms_show_only_in_front_of_the_screen():
    def answer(*seen: dict) -> dict:
        return {"frames": {"middle_frame": {"subjects": [], "excluded_seen": list(seen)}}}

    only = content_drift.drifted([], ["hands over the screen"], answer({"term": "hands over the screen", "keep_out": True}))
    assert [finding.keep_out for finding in only] == [True]
    # Listed twice in a frame, once elsewhere too: not only in front of the screen.
    twice = answer({"term": "hands over the screen", "keep_out": True}, {"term": "hands over the screen", "keep_out": False})
    assert [finding.keep_out for finding in content_drift.drifted([], ["hands over the screen"], twice)] == [False]
    # Never said: not only in front of it.
    unsaid = content_drift.drifted([], ["hands over the screen"], answer({"term": "hands over the screen"}))
    assert [finding.keep_out for finding in unsaid] == [False]


def test_the_frames_reading_is_told_the_rectangle_only_with_terms_to_place():
    asked: list[str] = []

    class _Llm:
        async def chat(self, messages, **_kwargs):
            asked.append(messages[0].content)
            return SimpleNamespace(content='{"frames": {}}')

    frames = {"first_frame": (_picture(), "image/jpeg")}
    asyncio.run(content_drift.observe(_Llm(), None, [], ["hands over the screen"], frames, keep_out=(0.3, 0.7, 0.2, 0.6)))
    asyncio.run(content_drift.observe(_Llm(), None, [], [], frames, keep_out=(0.3, 0.7, 0.2, 0.6)))
    assert "rectangle x 0.30-0.70, y 0.20-0.60 of each image" in asked[0] and '"keep_out": false' in asked[0]
    assert "keep_out" not in asked[1]


# ── The attempt ───────────────────────────────────────────────────────────


def test_a_drifted_take_says_its_attempt_and_the_regenerations_left(world):  # noqa: F811
    world.content = _hands(keep_out=False)
    world.row.params["negative_prompt"] = NEGATIVE
    _regenerate(world)
    _run(world)
    [failed] = world.row.params[clips.ATTEMPTS]
    assert failed["reason"].endswith(
        "Attempt 2 of this shot: 4 of the 5 regenerations of this prompt and start frame remain."
    )
    spent = {"attempt": 5, clips.REGENERATIONS: {"key": clips.regeneration_key(PROMPT, {}), "count": 5}}
    assert clips.attempt_clause(spent, PROMPT) == (
        " Attempt 6 of this shot: none of the 5 regenerations of this prompt and start frame remain - change its "
        "prompt or its start frame (the count starts again), or ask another model."
    )
    # Counted for another prompt: this one starts its own count.
    assert clips.attempt_clause(spent, "Another shot entirely").startswith(" Attempt 6 of this shot: 5 of the 5")
