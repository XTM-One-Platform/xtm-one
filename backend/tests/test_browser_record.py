"""The browser tool's ``record`` action: the viewport recorded as an MP4 through Chromium's screencast.

The contract runs on a fake page and DevTools session (no browser): the bounds of ``seconds``, the screencast
started at the viewport's size and stopped, every frame acknowledged, the frames handed to the encoder in order,
the painted rate, and the MP4 kept as a file. The real recording runs in ``test_browser_record_chromium.py``.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import browser_tool
from app.services.builtin_tools import build_browser_tool
from app.services.video_studio import encoder
from app.tools.base import ToolCall

JPEG = base64.b64encode(b"\xff\xd8frame").decode()


class FakeCdp:
    """A DevTools session that paints *frames* screencast frames when the screencast starts."""

    def __init__(self, stamps: list[float]) -> None:
        self.stamps = stamps
        self.sent: list[tuple[str, dict | None]] = []
        self.handlers: dict[str, object] = {}
        self.detach = AsyncMock()

    def on(self, event: str, handler) -> None:
        self.handlers[event] = handler

    async def send(self, method: str, params: dict | None = None):
        self.sent.append((method, params))
        if method == "Page.startScreencast":
            handler = self.handlers["Page.screencastFrame"]
            for index, stamp in enumerate(self.stamps):
                handler({"sessionId": index, "data": JPEG, "metadata": {"timestamp": stamp}})
        return {}


@pytest.fixture
def recorder(monkeypatch):
    def make(stamps: list[float]):
        cdp = FakeCdp(stamps)
        page = SimpleNamespace(url="https://app.example.com/chat", viewport_size={"width": 1280, "height": 720})
        context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=cdp))
        session = browser_tool.BrowserSession(pw=None, browser=None, context=context, page=page)
        monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=session))
        monkeypatch.setattr(browser_tool, "_reopen_resumed_page", AsyncMock(return_value=False))
        monkeypatch.setattr(encoder, "encoder_available", lambda: True)
        sleeps: list[float] = []

        async def no_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(browser_tool.asyncio, "sleep", no_sleep)
        encoded: dict = {}

        async def encode(frames, *, end_time, out_path, fps, crf, preset, workdir):
            encoded.update(frames=list(frames), end_time=end_time, fps=fps)
            with open(out_path, "wb") as handle:
                handle.write(b"mp4-bytes")

        monkeypatch.setattr(encoder, "frames_to_mezzanine", encode)
        return SimpleNamespace(cdp=cdp, sleeps=sleeps, encoded=encoded)

    return make


@pytest.mark.parametrize("seconds", [0.5, 31, True, "6", float("nan")])
def test_a_duration_out_of_bounds_is_refused(seconds):
    with pytest.raises(ValueError):
        browser_tool.record_seconds(seconds)


def test_the_default_duration():
    assert browser_tool.record_seconds(None) == browser_tool.DEFAULT_RECORD_SECONDS


@pytest.mark.parametrize(
    ("viewport", "size"),
    [
        ({"width": 640, "height": 360}, (640, 360)),
        ({"width": 960, "height": 600}, (960, 600)),
        ({"width": 1280, "height": 720}, (960, 540)),
        ({"width": 1728, "height": 1000}, (960, 556)),
        ({"width": 2560, "height": 1440}, (960, 540)),
    ],
)
def test_a_wide_viewport_is_captured_at_most_record_max_width_wide(viewport, size):
    assert browser_tool.record_frame_size(viewport) == size


async def test_a_wide_viewport_is_screencast_scaled_down(recorder):
    rec = recorder([1000.1, 1000.2])
    rec_session = await browser_tool._get_or_create_session("agent:conv")
    rec_session.page.viewport_size = {"width": 1728, "height": 1000}

    video = await browser_tool.browser_record("agent:conv", 1)

    start = dict(rec.cdp.sent)["Page.startScreencast"]
    assert (start["maxWidth"], start["maxHeight"]) == (960, 556)
    assert (video.width, video.height) == (960, 556)
    # 960 px at JPEG 60 kept 99 % of a page's rate on one CPU, 1280 px at JPEG 70 80 % (#11165).
    assert start["quality"] == 60


async def test_a_recording_screencasts_the_viewport_and_encodes_its_frames(recorder, monkeypatch):
    now = 1000.0
    monkeypatch.setattr(browser_tool.time, "time", lambda: now)
    rec = recorder([now + 0.5, now + 0.1, now + 0.3])

    video = await browser_tool.browser_record("agent:conv", 2)

    assert video.mp4 == b"mp4-bytes"
    assert (video.frames, video.seconds, video.width, video.height) == (3, 2.0, 960, 540)
    assert video.painted_fps == pytest.approx(5.0)
    assert rec.sleeps == [2.0]
    start = dict(rec.cdp.sent)["Page.startScreencast"]
    # Every second compositor frame (60 Hz / 2 = RECORD_FPS): the MP4 would drop the others anyway.
    assert (start["maxWidth"], start["maxHeight"], start["everyNthFrame"]) == (960, 540, 2)
    methods = [method for method, _ in rec.cdp.sent]
    assert methods.count("Page.screencastFrameAck") == 3
    assert methods.index("Page.stopScreencast") > methods.index("Page.startScreencast")
    rec.cdp.detach.assert_awaited_once()
    # Frames in the order they were painted, each held until the next one.
    assert [round(t, 3) for t, _name in rec.encoded["frames"]] == [0.1, 0.3, 0.5]
    assert (rec.encoded["end_time"], rec.encoded["fps"]) == (2.0, browser_tool.RECORD_FPS)


async def test_a_page_that_painted_nothing_says_so(recorder):
    recorder([])
    with pytest.raises(RuntimeError, match="painted no frame"):
        await browser_tool.browser_record("agent:conv", 1)


async def test_without_ffmpeg_the_recording_is_refused_before_the_page(recorder, monkeypatch):
    rec = recorder([1.0])
    monkeypatch.setattr(encoder, "encoder_available", lambda: False)
    with pytest.raises(RuntimeError, match="ffmpeg"):
        await browser_tool.browser_record("agent:conv", 1)
    assert rec.cdp.sent == []


class FakeBrowser:
    """A browser whose DevTools root lists its processes."""

    def __init__(self, pids: list[int]) -> None:
        self.root = SimpleNamespace(
            send=AsyncMock(return_value={"processInfo": [{"id": pid, "type": "renderer"} for pid in pids]}),
            detach=AsyncMock(),
        )

    async def new_browser_cdp_session(self):
        return self.root


async def test_the_browser_draws_in_the_render_lane_while_it_records(recorder, monkeypatch):
    from app.services.video_studio import cpu_lanes

    rec = recorder([1000.1, 1000.2])
    session = await browser_tool._get_or_create_session("agent:conv")
    session.browser = FakeBrowser([41, 42, 41])
    events: list[object] = []
    lane = cpu_lanes.Lane(cpu_lanes.PRIORITY, weight=1000, nice=-10)
    monkeypatch.setattr(cpu_lanes, "lane", lambda: lane)

    def lift(pids):
        events.append(("lift", pids, [m for m, _ in rec.cdp.sent]))
        return lambda: events.append(("restore", [m for m, _ in rec.cdp.sent]))

    monkeypatch.setattr(cpu_lanes, "lift", lift)

    video = await browser_tool.browser_record("agent:conv", 1)

    # Lifted before the screencast starts, put back once it stopped.
    assert events[0] == ("lift", [41, 42], [])
    assert events[1][0] == "restore" and "Page.stopScreencast" in events[1][1]
    assert video.lane == lane.words()


async def test_a_worker_without_a_render_lane_records_and_says_why(recorder, monkeypatch):
    from app.services.video_studio import cpu_lanes

    recorder([1000.1, 1000.2])
    session = await browser_tool._get_or_create_session("agent:conv")
    session.browser = FakeBrowser([41])
    none = cpu_lanes.Lane(cpu_lanes.NONE, reason="the container has no cgroup v2")
    monkeypatch.setattr(cpu_lanes, "lane", lambda: none)
    monkeypatch.setattr(cpu_lanes, "lift", lambda pids: pytest.fail("nothing to lift without a lane"))

    video = await browser_tool.browser_record("agent:conv", 1)

    assert "the container has no cgroup v2" in video.lane


async def test_a_browser_that_lists_no_process_records_as_before(recorder):
    recorder([1000.1, 1000.2])  # the fixture's browser is None: nothing to read
    video = await browser_tool.browser_record("agent:conv", 1)
    assert video.lane == ""


def test_lift_moves_running_processes_to_the_render_leaf_and_back(tmp_path, monkeypatch):
    from app.services.video_studio import cpu_lanes

    for leaf in (cpu_lanes.WORKER_LEAF, cpu_lanes.RENDER_LEAF):
        (tmp_path / leaf).mkdir()
    monkeypatch.setattr(cpu_lanes, "ROOT", str(tmp_path))
    monkeypatch.setattr(cpu_lanes, "lane", lambda: cpu_lanes.Lane(cpu_lanes.CGROUP, weight=1000))

    restore = cpu_lanes.lift([41])
    assert (tmp_path / cpu_lanes.RENDER_LEAF / "cgroup.procs").read_text() == "41"
    restore()
    assert (tmp_path / cpu_lanes.WORKER_LEAF / "cgroup.procs").read_text() == "41"


def test_lift_raises_every_thread_and_gives_each_its_niceness_back(monkeypatch):
    import os

    from app.services.video_studio import cpu_lanes

    niceness = {41: 0, 43: 5}
    monkeypatch.setattr(cpu_lanes, "lane", lambda: cpu_lanes.Lane(cpu_lanes.PRIORITY, weight=1000, nice=-10))
    monkeypatch.setattr(cpu_lanes, "_threads", lambda pid: [41, 43])
    monkeypatch.setattr(os, "PRIO_PROCESS", 0, raising=False)
    monkeypatch.setattr(os, "getpriority", lambda which, tid: niceness[tid], raising=False)
    monkeypatch.setattr(os, "setpriority", lambda which, tid, nice: niceness.__setitem__(tid, nice), raising=False)

    restore = cpu_lanes.lift([41])
    assert niceness == {41: -10, 43: -10}
    restore()
    assert niceness == {41: 0, 43: 5}


async def test_the_action_refuses_a_bad_duration_and_offers_the_argument():
    defs, executor = build_browser_tool("agent:conv")
    schema = defs[0].input_schema
    assert "record" in schema["properties"]["action"]["enum"]
    assert schema["properties"]["seconds"]["type"] == "number"
    assert "- record:" in defs[0].description
    result = await executor.execute_tool(
        ToolCall(id="c1", name="browser", arguments={"action": "record", "seconds": 120})
    )
    assert result.is_error
    assert "refused" in result.content


async def test_the_action_without_file_storage_says_what_it_recorded(recorder):
    recorder([1.0, 1.2])
    _defs, executor = build_browser_tool("agent:conv")
    result = await executor.execute_tool(
        ToolCall(id="c1", name="browser", arguments={"action": "record", "seconds": 1})
    )
    assert result.is_error
    assert "2 frame(s) painted in 1 s" in result.content
    assert "could not be kept" in result.content
