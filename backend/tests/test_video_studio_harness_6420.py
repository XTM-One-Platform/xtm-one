"""The Video Studio harness gaps of the OpenCTI graph demo session, on fixtures: no browser, no database.

* a script longer than a recording may last is refused before launch, naming
  the step that crosses the cap and where to split it; a take the cap still
  stops is kept, truncated, with its contact sheet;
* a step typing a credential is refused before launch;
* a statement backdrop and an image scene take a frame of a recording, kept
  as a picture by the save once;
* a recording its narration outruns is slowed by the save, never asked to be;
* the kept-preview key moves with every overlay field a frame draws;
* a recording's encode says how far it got while it runs;
* the preview lane is measured, sized by its p95 wait, and a sweep keeps
  both slots busy.

The Chromium half (role families, a password field refused at run time, the
browser tool's bounded ``evaluate`` and its restored cookies) is
``test_video_studio_harness_6420_chromium.py``.
"""

from __future__ import annotations

import asyncio
import base64
import io
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.video_studio import (
    capture,
    clips,
    encoder,
    kept_previews,
    preview_lane,
    recording_fit,
    render_checks,
    take_frames,
    take_preflight,
    timeline,
)
from app.services.video_studio.projects import apply_operations, unknown_file_words
from app.services.video_studio.quality import HELD_FRAME_SECONDS, held_seconds
from app.services.video_studio.spec import (
    TakeScript,
    VideoSpecError,
    authored_spec,
    parse_take_script,
    parse_video_spec,
    validate_take_script,
)
from tests.virtual_time import VirtualTimeLoop

TAKE = "00000000-0000-0000-0000-000000006420"
FILE = "00000000-0000-0000-0000-00000000f420"


def _script(steps: list[dict], **settings) -> TakeScript:
    return parse_take_script(validate_take_script({"steps": steps, **settings}))


# ── 1 + 2. The recording's cap ────────────────────────────────────────────


def test_a_script_past_the_cap_is_refused_before_launch_naming_the_step_that_crosses_it():
    script = _script([{"action": "wait", "seconds": 10} for _ in range(40)])

    refused = take_preflight.length_refusal(script, 300)

    assert refused is not None and refused.startswith("Refused before recording")
    assert "crosses the 300 s cap at step 29" in refused
    assert "about 402 s" in refused


def test_the_split_point_is_the_last_marker_before_the_crossing():
    steps = [{"action": "wait", "seconds": 10} for _ in range(40)]
    steps[20] = {"action": "marker", "label": "Graph view"}
    refused = take_preflight.length_refusal(_script(steps), 300)

    assert "Split it at step 20, its last marker before then ('Graph view'): record steps 0 to 19" in refused


def test_a_script_within_the_cap_less_its_margin_is_accepted():
    assert take_preflight.length_refusal(_script([{"action": "wait", "seconds": 10} for _ in range(26)]), 300) is None
    # Past 90 % of the cap, even short of the cap itself: the margin is for a slow page.
    refused = take_preflight.length_refusal(_script([{"action": "wait", "seconds": 10} for _ in range(28)]), 300)
    assert refused is not None and "past 270 s" in refused


def test_the_estimate_counts_typing_at_the_scripts_pace_and_an_optional_wait_for_its_whole_timeout():
    script = _script(
        [
            {"action": "type", "selector": "#search", "text": "x" * 100},
            {"action": "wait_for", "selector": "#banner", "optional": True},
            {"action": "wait_for", "selector": "#graph", "timeout": 30},
        ],
        settle_ms=600,
    )
    played = take_preflight.estimate(script)

    typed = take_preflight.ACTION_SECONDS + 0.6 + 100 * 0.055 + take_preflight.TYPE_OVERHEAD_SECONDS
    assert played.ends[0] == pytest.approx(take_preflight.LEAD_IN_SECONDS + typed, abs=1e-3)
    assert played.ends[1] - played.ends[0] == pytest.approx(3.0)
    # A step that must find its element is counted as an action, not its timeout.
    assert played.ends[2] - played.ends[1] == pytest.approx(take_preflight.ACTION_SECONDS)


def _jpeg(shade: int) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 36), (shade, shade, shade)).save(buffer, "JPEG")
    return buffer.getvalue()


class _Page:
    """A page that paints, answers nothing slowly and is never left."""

    url = "https://app.example.test/graph"

    def __init__(self) -> None:
        self.mouse = SimpleNamespace(move=self._nothing)

    async def _nothing(self, *_args, **_kwargs) -> None:
        return None

    async def wait_for_load_state(self, *_args, **_kwargs) -> None:
        return None


class _Cdp:
    def __init__(self) -> None:
        self.handler = None
        self.painting: asyncio.Task | None = None

    def on(self, _event, handler) -> None:
        self.handler = handler

    async def send(self, method: str, _params=None) -> None:
        if method == "Page.startScreencast":
            self.painting = asyncio.get_running_loop().create_task(self._paint())
        elif method == "Page.stopScreencast" and self.painting is not None:
            self.painting.cancel()

    async def _paint(self) -> None:
        shade = 0
        while True:
            shade = (shade + 37) % 256
            stamp = _VIRTUAL_EPOCH + asyncio.get_running_loop().time()
            self.handler({"sessionId": 1, "metadata": {"timestamp": stamp}, "data": base64.b64encode(_jpeg(shade)).decode()})
            await asyncio.sleep(0.05)


#: Where the virtual clock's wall time starts (seconds since the epoch): any fixed instant.
_VIRTUAL_EPOCH = 1_800_000_000.0


@pytest.fixture
def slow_page(monkeypatch):
    page = _Page()
    from app.services.video_studio import capture_rate

    # The take reads the loop's virtual clock, and records on software GL without launching a browser to probe it.
    monkeypatch.setattr(
        capture,
        "time",
        SimpleNamespace(
            time=lambda: _VIRTUAL_EPOCH + asyncio.get_running_loop().time(),
            monotonic=lambda: asyncio.get_running_loop().time(),
        ),
    )

    async def software_gl():
        return capture_rate.GlBackend(capture_rate.SOFTWARE_GL_ARGS, gpu=False, why="a unit test")

    monkeypatch.setattr(capture_rate, "gl_backend", software_gl)

    async def nothing(*_args, **_kwargs):
        return None

    async def cdp(_page):
        return _Cdp()

    rb = SimpleNamespace(page=page, goto=nothing, context=SimpleNamespace(add_init_script=nothing, new_cdp_session=cdp))

    @asynccontextmanager
    async def profile_browser(**_kwargs):
        yield rb

    monkeypatch.setattr(capture, "profile_browser", profile_browser)
    return page


def test_a_take_the_cap_stops_is_kept_truncated_with_its_contact_sheet(slow_page, tmp_path: Path):
    """The page is slower than the script said: its fourth wait runs past the cap, and the take is kept up to it."""
    script = _script([{"action": "wait", "seconds": 0.6} for _ in range(6)], settle_ms=0, end_hold_ms=0)

    result = asyncio.run(
        capture.record_take(
            script, workdir=str(tmp_path), profile=None, start_url=_Page.url, policy=None, max_seconds=2.0
        ),
        loop_factory=VirtualTimeLoop,
    )

    assert result.truncated_at_step == 2
    assert [event["step"] for event in result.events] == [0, 1]
    assert result.frames and result.duration <= 2.5
    assert any("reached its 2 s limit at step 2" in note and "steps 2 to 5 not recorded" in note for note in result.notes)
    sheet = tmp_path / "sheet.png"
    capture.contact_sheet(str(tmp_path), capture.pick_sheet_frames(result), out_path=str(sheet))
    assert sheet.exists() and sheet.stat().st_size > 0


def test_a_ready_take_whose_action_log_stops_short_is_truncated_at_that_step():
    from app.models.video_studio import JOB_FAILED, JOB_READY
    from app.services.video_studio import jobs

    steps = {"steps": [{"action": "wait", "seconds": 1}] * 5}
    take = SimpleNamespace(status=JOB_READY, events=[{"step": 0}, {"step": 1}], script=steps)
    assert jobs.truncated_at_step(take) == 2
    assert jobs.truncated_at_step(SimpleNamespace(status=JOB_READY, events=[{"step": n} for n in range(5)], script=steps)) is None
    assert jobs.truncated_at_step(SimpleNamespace(status=JOB_FAILED, events=[], script=steps)) is None


# ── 10. Credentials never in a step ───────────────────────────────────────


@pytest.mark.parametrize("selector", ["input[type=password]", '#login [type="password"]', "input[type='Password']"])
def test_a_step_typing_into_a_password_field_is_refused_before_launch(selector):
    refused = take_preflight.credential_refusal(_script([{"action": "type", "selector": selector, "text": "hunter22"}]))

    assert refused is not None and "step 0 types into a password field" in refused
    assert take_preflight.PROFILES_PAGE in refused and "hunter22" not in refused


def test_a_step_typing_a_stored_credential_is_refused_and_an_ordinary_one_is_not():
    credentials = {"username": "demo@example.test", "password": "S3cret-demo", "totp_secret": "JBSWY3DPEHPK3PXP"}
    typed = _script([{"action": "type", "selector": "#q", "text": "S3cret-demo"}])
    code = _script([{"action": "type", "text": "otp JBSWY3DPEHPK3PXP"}])
    search = _script([{"action": "type", "selector": "#q", "text": "APT29 demo@example.test"}])

    assert "stored credentials" in take_preflight.credential_refusal(typed, credentials)
    assert take_preflight.credential_refusal(code, credentials) is not None
    # The username is shown on any sign-in page: typing it is no secret.
    assert take_preflight.credential_refusal(search, credentials) is None


# ── 4. A recording's frame as a picture ───────────────────────────────────


def test_a_backdrop_and_an_image_scene_take_a_frame_of_a_recording():
    spec = authored_spec(
        {
            "title": "Frames",
            "freestyle": True,
            "scenes": [
                {"id": "s1", "type": "statement", "lines": ["The graph"], "backdrop": {"take_id": TAKE, "at_step": 14}},
                {"id": "s2", "type": "image", "take_id": TAKE, "at_seconds": 12.5},
            ],
        }
    )
    assert spec["scenes"][0]["backdrop"] == {"take_id": TAKE, "at_step": 14}
    with pytest.raises(VideoSpecError, match=r"take_id with at_step or at_seconds"):
        authored_spec({"title": "x", "freestyle": True, "scenes": [{"id": "s", "type": "image"}]})
    with pytest.raises(VideoSpecError, match=r"exactly one source: clip_id \(an AI shot\), file_id"):
        authored_spec(
            {
                "title": "x",
                "freestyle": True,
                "scenes": [{"id": "s", "type": "statement", "lines": ["x"], "backdrop": {"clip_id": TAKE, "take_id": TAKE}}],
            }
        )
    # The save keeps the frame beside the recording: both validate.
    authored_spec(
        {
            "title": "x",
            "freestyle": True,
            "scenes": [
                {
                    "id": "s",
                    "type": "statement",
                    "lines": ["x"],
                    "backdrop": {"take_id": TAKE, "at_step": 14, "file_id": FILE, "frame_of": f"take:{TAKE}:step:14"},
                }
            ],
        }
    )


@pytest.fixture
def frames(monkeypatch):
    taken: list[dict] = []

    async def resolve_frame(_db, _user, **asked):
        if asked["at_step"] == 99:
            raise clips.ClipError(f"Recording `{asked['recording_id']}` has no step 99: its steps are 0 to 20.")
        taken.append(asked)
        return {"kind": "recording", "name": "graph.mp4", "step": asked["at_step"], "at_seconds": asked["at_seconds"]}

    async def frame_picture(_source, _work):
        return b"\x89PNG picture"

    async def store_bytes(data, **kwargs):
        return SimpleNamespace(id=uuid.uuid4(), **kwargs)

    monkeypatch.setattr(clips, "resolve_frame", resolve_frame)
    monkeypatch.setattr(clips, "frame_picture", frame_picture)
    monkeypatch.setattr(clips, "frame_label", lambda source: f"the frame of the recording after step {source['step']}")
    from app.services.video_studio import assets

    monkeypatch.setattr(assets, "store_bytes", store_bytes)
    return taken


def _statement(backdrop: dict) -> dict:
    return {"title": "x", "freestyle": True, "scenes": [{"id": "s1", "type": "statement", "lines": ["x"], "backdrop": backdrop}]}


def test_the_save_keeps_a_recordings_frame_once_and_takes_it_again_only_for_another_moment(frames):
    user = SimpleNamespace(id=uuid.uuid4())
    spec, notes = asyncio.run(take_frames.resolve(None, user, _statement({"take_id": TAKE, "at_step": 14})))

    backdrop = spec["scenes"][0]["backdrop"]
    assert backdrop["frame_of"] == f"take:{TAKE}:step:14" and uuid.UUID(backdrop["file_id"])
    assert len(frames) == 1 and "kept as the picture" in notes[0]
    # Saved again, as it is or with the frame asked again by an edit: the picture it kept.
    again, _ = asyncio.run(take_frames.resolve(None, user, spec, previous=spec))
    asked_again, _ = asyncio.run(take_frames.resolve(None, user, _statement({"take_id": TAKE, "at_step": 14}), previous=spec))
    assert again == spec and asked_again["scenes"][0]["backdrop"]["file_id"] == backdrop["file_id"] and len(frames) == 1
    # Another step: another frame.
    moved, _ = apply_operations(spec, [{"op": "update_scene", "scene": "s1", "fields": {"backdrop": {"at_step": 15}}}])
    moved, _ = asyncio.run(take_frames.resolve(None, user, moved, previous=spec))
    assert moved["scenes"][0]["backdrop"]["frame_of"] == f"take:{TAKE}:step:15" and len(frames) == 2


def test_a_frame_that_cannot_be_taken_says_the_forms_a_picture_takes(frames):
    with pytest.raises(VideoSpecError) as refused:
        asyncio.run(take_frames.resolve(None, SimpleNamespace(id=uuid.uuid4()), _statement({"take_id": TAKE, "at_step": 99})))
    text = str(refused.value)
    assert "has no step 99" in text and "file_id (a picture), or take_id with at_step or at_seconds" in text
    # A recording's id given as a file: what to write instead.
    assert f'{{"take_id": "{TAKE}", "at_step": N}}' in unknown_file_words(TAKE, recording=True)


def test_switching_a_backdrop_to_a_file_drops_the_recordings_moment():
    spec = authored_spec(_statement({"take_id": TAKE, "at_step": 14, "file_id": FILE, "frame_of": f"take:{TAKE}:step:14"}))
    other = "00000000-0000-0000-0000-00000000f421"
    edited, _ = apply_operations(spec, [{"op": "update_scene", "scene": "s1", "fields": {"backdrop": {"file_id": other}}}])

    assert authored_spec(edited)["scenes"][0]["backdrop"] == {"file_id": other}


# ── 5. A recording slowed to its narration ────────────────────────────────


def _fitted(length: float, narration: float) -> tuple[list[recording_fit.Fit], float]:
    authored = authored_spec(
        {
            "title": "Fit",
            "freestyle": True,
            "voiceover": {"enabled": True},
            "scenes": [{"id": "demo", "type": "recording", "take_id": TAKE, "narration": "The graph, laid out by category."}],
        }
    )
    take = timeline.TakeMedia(TAKE, length, 1600, 900, 2.0, 60, int(length * 60))
    voices = {"demo": narration}
    plan = timeline.estimate_plan(parse_video_spec(authored), {TAKE: take}, voices=voices)
    found = recording_fit.fits(parse_video_spec(authored), plan.document, {TAKE: take})
    replanned = timeline.estimate_plan(parse_video_spec(recording_fit.apply(authored, found)), {TAKE: take}, voices=voices)
    return found, held_seconds(replanned.document["scenes"][0])


def test_a_recording_its_narration_outruns_is_slowed_by_the_save_and_holds_nothing():
    found, held = _fitted(12.0, 16.0)

    assert [fit.scene_id for fit in found] == ["demo"]
    assert 0.7 <= found[0].speed <= 0.76 and not found[0].clamped
    assert held < HELD_FRAME_SECONDS
    assert found[0].words().startswith(f"scene `demo`: speed set to {found[0].speed:g} automatically")


def test_a_recording_short_even_at_half_speed_is_set_to_it_and_says_the_seconds_still_missing():
    found, held = _fitted(5.0, 20.0)

    assert found[0].speed == recording_fit.SLOWEST_FIT_SPEED and found[0].clamped
    assert 10.0 <= found[0].short <= 11.0 and held == pytest.approx(found[0].short, abs=0.1)
    words = found[0].words()
    assert f"still ends {found[0].short:.1f} s before its narration" in words
    # Nothing to type back: no speed but the one it set.
    assert "speed 0." not in words.replace("speed set to 0.5", "")


def test_a_recording_with_fit_off_or_long_enough_is_left_as_it_is():
    assert _fitted(20.0, 10.0)[0] == []
    authored = authored_spec(
        {
            "title": "Fit",
            "freestyle": True,
            "scenes": [{"id": "demo", "type": "recording", "take_id": TAKE, "fit": "off", "narration": "x"}],
        }
    )
    take = timeline.TakeMedia(TAKE, 5.0, 1600, 900, 2.0, 60, 300)
    plan = timeline.estimate_plan(parse_video_spec(authored), {TAKE: take}, voices={"demo": 20.0})
    assert recording_fit.fits(parse_video_spec(authored), plan.document, {TAKE: take}) == []


# ── 6. The kept-preview key ───────────────────────────────────────────────

_SPEC = {
    "title": "Cache",
    "freestyle": True,
    "scenes": [
        {"id": "s1", "type": "title", "title": "Hook"},
        {
            "id": "graph",
            "type": "title",
            "title": "The graph",
            "overlays": [{"type": "callout", "text": "Layered layout", "subtext": "by category", "position": "top", "start": 1.0, "duration": 3.0}],
        },
        {"id": "s3", "type": "title", "title": "Close"},
    ],
}


@pytest.fixture
def preview_inputs(monkeypatch):
    from app.services.video_studio import lexicon_store, projects, reuse_index

    async def digests(_db, _effective, _lexicon=None):
        return render_checks.SceneDigests({"s1": "w1", "graph": "wg", "s3": "w3"}, {"graph": "cg"})

    async def estimate(_db, effective, **_kwargs):
        return SimpleNamespace(document={"scenes": [{"id": s["id"], "start": 4.0 * i} for i, s in enumerate(effective["scenes"])]})

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(render_checks, "digest_scenes", digests)
    monkeypatch.setattr(projects, "estimate", estimate)
    monkeypatch.setattr(lexicon_store, "for_voiceover", nothing)
    monkeypatch.setattr(reuse_index, "voice_target", nothing)
    monkeypatch.setattr(reuse_index, "voice_digests", lambda *_args: {})
    user = SimpleNamespace(id=uuid.uuid4(), role="user")

    def digest(spec: dict) -> str:
        return asyncio.run(kept_previews.inputs(None, spec, None, scene_id="graph", quality="standard", user=user)).digest

    return digest


@pytest.mark.parametrize(
    "field, value",
    [("position", "bottom"), ("text", "Layered layout (by entity category)"), ("subtext", "by type"), ("start", 1.5), ("duration", 2.0)],
)
def test_an_overlay_changed_is_never_served_from_the_frames_drawn_before(preview_inputs, field, value):
    import copy

    edited = copy.deepcopy(_SPEC)
    edited["scenes"][1]["overlays"][0][field] = value

    assert preview_inputs(edited) != preview_inputs(_SPEC)
    assert preview_inputs(copy.deepcopy(_SPEC)) == preview_inputs(_SPEC)


def test_a_preview_served_from_a_kept_one_says_what_it_compared():
    said = kept_previews.cached_line(654, 1.2, "abc")

    assert said.startswith("654 of 654 frames cached, encoded in 1.2 s")
    assert "each overlay's text, subtext, position, start and duration" in said


# ── 7. Progress inside an encode ──────────────────────────────────────────


class _SleepingProcess:
    """A process stood in for that runs for *seconds* of its loop's clock, then exits 0 with no output."""

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self.returncode: int | None = None

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.sleep(self._seconds)
        self.returncode = 0
        return b"", b""

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


def test_a_long_run_reports_at_the_cadence_it_is_given(monkeypatch):
    """On the loop's virtual clock: on the wall clock, a loaded xdist worker let one report slip by 1.6 s."""
    seen: list[float] = []

    async def spawn(*_command, **_kwargs):
        return _SleepingProcess(1.3)

    monkeypatch.setattr(encoder.asyncio, "create_subprocess_exec", spawn)

    async def waited() -> None:
        seen.append(asyncio.get_running_loop().time())

    asyncio.run(
        encoder.run(sys.executable, ["-c", "pass"], on_wait=waited, wait_every=0.2),
        loop_factory=VirtualTimeLoop,
    )

    assert seen == pytest.approx([0.2, 0.4, 0.6, 0.8, 1.0, 1.2], abs=1e-6)


def test_a_recordings_encode_reports_its_frames_muxed_out_of_its_total(monkeypatch, tmp_path: Path):
    """ffmpeg stood in for: it writes its ``-progress`` report as it goes, and the encode reads it on every report."""
    reported: list[tuple[int, int]] = []
    cadences: list[float | None] = []

    async def run(_binary, args, *, cwd=None, on_wait=None, wait_every=None, **_kwargs):
        cadences.append(wait_every)
        report = Path(cwd) / args[args.index("-progress") + 1]
        await on_wait()
        for frame in (1200, 5580, 9000):
            with report.open("a", encoding="utf-8") as out:
                out.write(f"frame={frame}\nfps=240.0\nprogress=continue\n")
            await on_wait()
        return b"", ""

    async def progress(done: int, total: int) -> None:
        reported.append((done, total))

    monkeypatch.setattr(encoder, "run", run)
    monkeypatch.setattr(encoder, "ffmpeg_path", lambda: "ffmpeg")
    frames = [(0.0, "f000000.jpg"), (75.0, "f000001.jpg")]
    asyncio.run(
        encoder.frames_to_mezzanine(
            frames, end_time=150.0, out_path="take.mp4", fps=60, workdir=str(tmp_path), on_progress=progress
        )
    )

    assert cadences == [encoder.PROGRESS_REPORT_SECONDS]
    assert reported == [(0, 9000), (1200, 9000), (5580, 9000), (9000, 9000)]


# ── 8. The browser tool's idle sweep ──────────────────────────────────────


class _SweptOnceError(Exception):
    pass


def test_an_idle_sweep_never_closes_a_session_an_action_holds(monkeypatch):
    """A script that held the session past the idle timeout had it closed under it: the next call was signed out."""
    from app.services import browser_tool

    held = SimpleNamespace(closed=False, last_used=0.0, lock=SimpleNamespace(locked=lambda: True))
    idle = SimpleNamespace(closed=False, last_used=0.0, lock=SimpleNamespace(locked=lambda: False))
    monkeypatch.setattr(browser_tool, "_sessions", {"held": held, "idle": idle})
    closed: list[str] = []
    ticks: list[float] = []

    async def close(key: str, **_kwargs) -> None:
        closed.append(key)
        browser_tool._sessions.pop(key, None)

    async def tick(seconds: float) -> None:
        ticks.append(seconds)
        if len(ticks) > 1:
            raise _SweptOnceError

    monkeypatch.setattr(browser_tool, "_close_session", close)
    monkeypatch.setattr(browser_tool, "asyncio", SimpleNamespace(sleep=tick))
    with pytest.raises(_SweptOnceError):
        asyncio.run(browser_tool._cleanup_loop())

    assert closed == ["idle"]


# ── 9. The preview lane, measured and sized ───────────────────────────────


def _timings(waits: list[float], draw: float) -> list[tuple[datetime, datetime, datetime]]:
    start = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
    return [
        (start, start + timedelta(seconds=wait), start + timedelta(seconds=wait + draw))
        for wait in waits
    ]


def test_the_lane_is_measured_and_sized_by_its_p95_wait():
    workers = SimpleNamespace(workers=1, slots=2)
    calm = preview_lane.measure(_timings([5.0] * 19 + [60.0], 120.0), workers=workers, queued=0)
    assert calm.as_dict() | {"sizing": None} == {
        "workers": 1,
        "slots": 2,
        "queued": 0,
        "measured_previews": 20,
        "median_wait_s": 5.0,
        "p95_wait_s": 5.0,
        "median_draw_s": 120.0,
        "sizing": None,
    }
    assert not calm.undersized and calm.sizing().startswith("sized for what is asked")

    swept = preview_lane.measure(_timings([60.0 * n for n in range(18)], 130.0), workers=workers, queued=23)
    assert swept.undersized and swept.p95_wait_s == 1020.0
    assert "Add a worker that draws scene previews only" in swept.sizing()
    assert preview_lane.measure([], workers=None).sizing().startswith("no scene preview started")


def test_a_sweep_of_one_video_keeps_both_slots_busy():
    """18 previews of one video, 2 slots: the dealer never leaves a slot idle while one of them waits."""
    draws = [60.0 + 7.0 * (n % 5) for n in range(18)]
    queue = [(n, "video") for n in range(18)]
    slots: list[tuple[float, int] | None] = [None, None]
    clock, done, running = 0.0, 0, {"video": 0}
    while done < len(draws):
        for index, held in enumerate(slots):
            if held is None and queue:
                order = preview_lane.dealt(queue, running, cap=2).order
                if order:
                    job = order[0]
                    queue = [entry for entry in queue if entry[0] != job]
                    running["video"] += 1
                    slots[index] = (clock + draws[job], job)
        clock, ending = min((held[0], index) for index, held in enumerate(slots) if held is not None)
        slots[ending] = None
        running["video"] -= 1
        done += 1

    assert clock <= 1.5 * sum(draws) / 2
