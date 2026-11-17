"""A failed take says what drew its canvases and how long its failed step ran, and a node whose every place shows
another node's card fails in seconds.

Production retest of #8463 on a GPU recording lane (1920x1080 at pixel ratio 1.5, 30 fps): a ``right_click`` on
``node {label: 'Storm-1567'}`` failed, rightly, naming the cards it read ('targets', 'uses'), but about 28 s after the
step before, and the failed take served no capture line: neither the gap nor the GPU half ("copies drawn smaller to
keep up") could be read. Now the failure message says the step's seconds, a failed take keeps ``capture.alone``,
``capture.gl`` and ``capture.backend`` and ``get_video_recording`` prints them, and the places a node is tried at under
the cursor are bounded by ``pointer.HOVER_BUDGET_SECONDS``.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

from app.models.video_studio import JOB_FAILED, VideoTake
from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import canvas_alone, capture, pointer
from app.tools.base import ToolCall

NAMES = ("Storm-1567", "Intrusion Set Storm-1567")
GPU = {"gpu": True}
ALONE = {"ratio": 1.5, "quality": 0.8, "copies": 40, "lowered": 12, "busy": 1, "runs": [{"composited": True}]}


# -- 1. the capture line, ready or failed --------------------------------------------------------------------------


def test_the_capture_words_say_the_backend_the_gl_and_the_copies():
    said = canvas_alone.take_capture_words({"backend": "ANGLE (NVIDIA)", "gl": GPU, "alone": ALONE})
    assert said.startswith("WebGL drawn by ANGLE (NVIDIA); canvases drawn with GPU GL; canvas transitions captured alone")
    assert "1 copy skipped, every encoder behind" in said and said.endswith("12 copies drawn smaller to keep up")
    software = canvas_alone.take_capture_words({"gl": {"gpu": False, "why": "no GPU on this worker"}})
    assert software == "canvases drawn with software GL (no GPU on this worker)"
    assert canvas_alone.take_capture_words(None) == "" and canvas_alone.take_capture_words({"opened": {}}) == ""


def test_a_failed_step_keeps_an_empty_capture_until_the_take_fills_it():
    failed = capture.StepFailedError("Step 12 failed", step=12, at=55.5, label="Right-click Storm-1567")
    assert failed.capture == {}


def _executor(monkeypatch, take: VideoTake):
    class _Session:
        async def get(self, model, key):
            if model is VideoTake:
                return take if str(key) == str(take.id) else None
            return SimpleNamespace(id=take.user_id, role="member")

    @asynccontextmanager
    async def session():
        yield _Session()

    monkeypatch.setattr(tool, "async_session", session)
    return tool.build_video_studio_tools(None, str(take.user_id))[1]


async def test_a_failed_take_s_answer_says_its_capture_up_to_the_failure(monkeypatch):
    take = VideoTake(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="Storm-1567",
        status=JOB_FAILED,
        progress=100,
        duration_ms=31200,
        viewport_width=1920,
        viewport_height=1080,
        pixel_ratio=1.5,
        fps=30,
        captured_frames=0,
        error="Step 12 (right_click Right-click Storm-1567) failed (after 4.1 s, at 31.2 s of the take): the card ...",
        events=[],
        script={"steps": [], "pixel_ratio": 1.5},
        capture={"backend": "ANGLE (NVIDIA)", "gl": GPU, "alone": ALONE},
    )
    answer = await _executor(monkeypatch, take).execute_tool(
        ToolCall(id="c1", name="get_video_recording", arguments={"recording_id": str(take.id), "include_contact_sheet": False})
    )
    # A failed take is a status the call read, never a tool error (#9780).
    assert not answer.is_error and "failed (after 4.1 s, at 31.2 s of the take)" in answer.content
    assert "\nCapture up to the failure: WebGL drawn by ANGLE (NVIDIA); canvases drawn with GPU GL;" in answer.content
    assert "12 copies drawn smaller to keep up." in answer.content


# -- 2. the places a node is tried at are bounded in time ----------------------------------------------------------


class _Cursor:
    """What ``_hover_to_node`` drives on the recorder: the cursor's moves and rests, nothing else."""

    def __init__(self) -> None:
        self.page = None
        self.moves: list[tuple[float, float]] = []

    async def move_to(self, x: float, y: float) -> None:
        self.moves.append((x, y))

    async def _hover(self, _ms: int) -> None:
        return None

    _xy = staticmethod(capture._Recorder._xy)


def _others_card(monkeypatch) -> None:
    async def read_shown(_page):
        return []

    def hover_check(_before, _shown, point, _names, _stale=None):
        return {"title": "targets", "node": False, "key": f"card@{point[0]:.0f}"}

    monkeypatch.setattr(pointer, "read_shown", read_shown)
    monkeypatch.setattr(pointer, "hover_check", hover_check)


def _target() -> pointer.Target:
    others = tuple((1000.0 + 10 * i, 400.0) for i in range(6))
    return pointer.Target((1108.0, 375.0), {}, {"node": "Storm-1567"}, located={"by": "highlight"}, names=NAMES, others=others)


async def test_every_place_is_tried_within_the_budget_and_the_cursor_stays_for_the_failure(monkeypatch):
    _others_card(monkeypatch)
    cursor = _Cursor()
    tried, _target_after = await capture._Recorder._hover_to_node(cursor, _target(), [], 0)
    assert len(tried) == 1 + pointer.HOVER_RETRIES and all(entry["title"] == "targets" for entry in tried)
    # A card read: the step fails where the cursor stands, no glide back to the first place.
    assert cursor.moves[-1] == _target().others[-1]
    assert pointer.hover_missed(tried, _target()) is not None


async def test_no_place_is_tried_past_the_budget(monkeypatch):
    _others_card(monkeypatch)
    monkeypatch.setattr(pointer, "HOVER_BUDGET_SECONDS", 0.0)
    cursor = _Cursor()
    tried, _target_after = await capture._Recorder._hover_to_node(cursor, _target(), [], 0)
    assert [entry["title"] for entry in tried] == ["targets"] and cursor.moves == []


def test_the_budget_keeps_a_node_step_under_five_seconds():
    assert 0 < pointer.HOVER_BUDGET_SECONDS < 5.0
