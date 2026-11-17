"""An insert's push is judged at the render's resolution, a recording is quoted at its painted size, and the preview
lane says how many workers draw previews.

The save-time legibility advice told an author to "push in with zoom {scale: 1.7}"; ``render_video`` then refused
the same insert at ultra as soft (0.65 source pixels per output pixel, at least 0.8): the advice judged the push on
the 1080p frame, the gate on the render's own. Synthetic fixtures, no browser, no database. What is proven:

* every push the advice offers passes the soft gate at the quality it is judged at; one a larger render would
  refuse says so; one the source cannot carry is never offered - the largest it carries is, and a browser zoom
  that makes it carry the rest where the page keeps its layout;
* the advice agrees with the save's every-quality soft check (``insert_resolution``), judged at the quality
  of the video's newest whole render;
* a zoomed recording is quoted at its painted size, and its pixel-ratio advice reads the ratio it was recorded at;
* the queue line says how many workers draw previews, and a preview that waited behind other videos' previews on a
  lane of one worker says once what a second preview worker gives.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.video_studio import (
    capture,
    insert_checks,
    insert_legibility,
    jobs,
    preview_lane,
    preview_routing,
    render,
    render_checks,
)
from app.services.video_studio.insert_legibility import TextSize
from app.services.video_studio.preview_lane import Lane
from app.services.video_studio.spec import painted_size, parse_video_spec, recorded_ratio, zoomed_window

CLIP = "00000000-0000-0000-0000-00000000c314"
TAKE = "00000000-0000-0000-0000-00000000a314"
#: A quad 75% of the frame wide and high: 1440x810 on the 1080p frame, 2880x1620 at 2160p.
QUAD = [[0.125, 0.125], [0.875, 0.125], [0.875, 0.875], [0.125, 0.875]]
#: The issue's recording: a 1600x900 window at pixel ratio 2, its page zoomed to 190% (laid out at 842x474).
SCRIPT = {"viewport": {"width": 1600, "height": 900}, "pixel_ratio": 2, "browser_zoom": 190}
CSS, RATIO = zoomed_window((1600, 900), 2.0, 190)
PAINTED = painted_size(CSS, RATIO)
FLUID = {"holds_down_to": 640, "changes_at": None}


def _spec(**overlay) -> object:
    return parse_video_spec(
        {
            "title": "Dashboard",
            "freestyle": True,
            "scenes": [
                {
                    "id": "s1",
                    "type": "clip",
                    "clip_id": CLIP,
                    "overlays": [{"type": "screen_insert", "source": {"take_id": TAKE}, "quad": QUAD, "duration": 6, **overlay}],
                }
            ],
        }
    )


def _judged(
    quality_name: str,
    layout: dict | None = FLUID,
    text: float = 15.7,
    *,
    painted: tuple[int, int] = PAINTED,
    ratio: float = 2.0,
    zoom: int = 190,
) -> insert_legibility.Legibility:
    """The issue's insert, its smallest text *text* px in the painted frames (7 px drawn on the 1080p frame)."""
    [item] = insert_legibility.judge(
        _spec(),
        {("take", TAKE): TextSize(text, 40)},
        sources={("take", TAKE): painted},
        footage={"s1": (1920, 1080)},
        pixel_ratios={TAKE: ratio},
        browser_zooms={TAKE: zoom},
        layouts={TAKE: layout} if layout else {},
        quality=quality_name,
    )
    return item


def _soft_at(size: tuple[int, int], scale: float) -> list:
    """What the render gate says of the issue's insert pushed in *scale* times, at *size*."""
    spec = _spec(zoom={"start": 0, "end": 2, "scale": scale}) if scale > 1 else _spec()
    return insert_checks.soft_before(
        spec, size, sources={("take", TAKE): PAINTED}, footage={"s1": (1920, 1080)}, pixel_ratios={TAKE: 2.0}, screens_replaced={}
    )


def _pushes(words: str) -> list[float]:
    return [float(value) for value in re.findall(r"zoom \{start, end, scale: ([0-9.]+)\}", words)]


# ── The push is judged where the render draws it ─────────────────────────


def test_the_issue_s_push_is_sharp_at_1080p_and_says_an_ultra_render_refuses_it():
    item = _judged("high")
    words = item.fix()
    assert _pushes(words) == [1.7]
    # The gate agrees on both sides: sharp at 1080p, refused at 2160p with the issue's 0.65.
    assert _soft_at((1920, 1080), 1.7) == []
    [(_scene, refused)] = _soft_at((3840, 2160), 1.7)
    assert "0.65 source pixels per pixel" in refused
    assert (
        "drawn that large, it stays sharp up to a high render (1080p): an ultra render (2160p) draws it at 0.65 of its "
        "pixels per output pixel (under 0.8) and refuses it"
    ) in words


def test_a_push_sharp_at_a_draft_names_the_first_larger_render_that_refuses_it():
    words = _judged("draft", text=9.0).fix()
    assert _pushes(words) == [3.0]
    assert "it stays sharp up to a draft render (720p): a high render (1080p) draws it at 0.74 of its pixels" in words
    assert _soft_at((1280, 720), 3.0) == [] and len(_soft_at((1920, 1080), 3.0)) == 1


def test_at_ultra_the_advice_never_offers_a_push_the_gate_refuses():
    item = _judged("ultra")
    words = item.fix()
    # No surface, no push of 1.7: the largest push the recording carries, and the browser zoom that carries the rest.
    assert "of the 1080p frame's width" not in words and "scale: 1.7" not in words
    assert "the most the recording carries sharp at an ultra render (2160p), its smallest text then about 9 px" in words
    assert (
        "record it again with a browser zoom of 250% (browser_zoom: 250; its layout holds down to 640 px, probed when it "
        "was recorded), then push in on it with zoom {start, end, scale: 1.3}: its text larger in the same pixels, sharp "
        "at an ultra render (2160p)"
    ) in words
    # The call that sizes the recording from its saved quad.
    assert 'sharp at an ultra render (2160p) (record_browser_demo(target_render: "ultra", video_id, scene: "' in words
    for scale in _pushes(words):
        assert _soft_at((3840, 2160), scale) == [], f"a push of {scale} the advice offers is refused at ultra"


def test_a_browser_zoom_that_would_change_the_layout_is_said_never_advised():
    stacks = {"holds_down_to": 1000, "changes_at": 960, "why": "blocks it shows side by side stack (3 pairs)"}
    words = _judged("ultra", stacks).fix()
    assert "browser_zoom:" not in words
    assert "a browser zoom of 250% would change its layout (blocks it shows side by side stack (3 pairs) at 960 px)" in words
    # Not probed: no browser zoom either, only what the recording carries as it is.
    unprobed = _judged("ultra", None).fix()
    assert "browser_zoom:" not in unprobed and _pushes(unprobed) == [1.3]


def test_a_source_that_carries_no_push_at_all_is_told_so():
    # A 1600 px recording at pixel ratio 1 on the 1440 px quad: soft at ultra even unpushed, nothing larger offered.
    item = _judged("ultra", text=9.0, painted=(1600, 900), ratio=1.0, zoom=100)
    assert item.carried() < 1.0
    words = item.fix()
    assert _pushes(words) == [] and "browser_zoom:" not in words
    assert words.startswith("drawn large enough to read, its source is soft at an ultra render (2160p) (")


# ── The advice and the save's every-quality soft check agree ─────────────


def _soft_qualities(scale: float) -> list:
    """The save's ``insert_resolution`` reading of the issue's insert pushed in *scale* times."""
    spec = _spec(zoom={"start": 0, "end": 2, "scale": scale}) if scale > 1 else _spec()
    return insert_checks.soft_by_quality(
        spec, sources={("take", TAKE): PAINTED}, footage={"s1": (1920, 1080)}, pixel_ratios={TAKE: 2.0}, screens_replaced={}
    )


def test_a_push_the_advice_offers_raises_no_resolution_advisory_at_its_quality():
    # Advised at high, the push of 1.7 is the one the save then says ultra refuses - as the advice itself said.
    [soft] = _soft_qualities(1.7)
    assert soft.qualities == ("ultra",) and soft.sharp == ("standard", "high")
    # Advised at ultra, every push it offers is sharp at every quality.
    for scale in _pushes(_judged("ultra").fix()):
        assert _soft_qualities(scale) == [], f"a push of {scale} the advice offers at ultra is soft somewhere"


def test_the_quality_judged_is_the_newest_whole_render_s():
    class _Db:
        def __init__(self, value):
            self.value = value

        async def execute(self, _statement):
            if isinstance(self.value, Exception):
                raise self.value
            return SimpleNamespace(scalar_one_or_none=lambda: self.value)

    project = SimpleNamespace(id=uuid.uuid4())
    assert asyncio.run(render_checks.target_quality(_Db("ultra"), project)) == "ultra"
    assert asyncio.run(render_checks.target_quality(_Db(None), project)) == "high"
    assert asyncio.run(render_checks.target_quality(_Db("retired"), project)) == "high"
    # Unread, the advice is judged at the default quality: it never fails a save.
    assert asyncio.run(render_checks.target_quality(_Db(RuntimeError("down")), project)) == "high"


# ── A zoomed recording at its painted size ───────────────────────────────


def test_a_zoomed_recording_is_quoted_at_its_painted_size():
    assert CSS == (842, 474) and PAINTED == (3200, 1801)
    # The script's own ratio; a script naming none, read back from the zoomed ratio.
    assert recorded_ratio(RATIO, SCRIPT) == 2.0 and abs(recorded_ratio(RATIO, {"browser_zoom": 190}) - 2.0) < 0.01
    assert capture.size_words(CSS, RATIO, SCRIPT) == (
        "3200x1801 px painted (a 1600x900 window at pixel ratio 2, its page laid out at 842x474 by a browser zoom of 190%)"
    )
    assert capture.size_words((1600, 900), 2.0, {}) == "3200x1800 px painted (a 1600x900 window at pixel ratio 2)"


def test_the_gate_reads_a_zoomed_recording_at_its_painted_size_and_recorded_ratio():
    take = SimpleNamespace(viewport_width=CSS[0], viewport_height=CSS[1], pixel_ratio=RATIO, script=SCRIPT, layout=None)
    clip = SimpleNamespace(width=1920, height=1080, params={})

    class _Db:
        async def get(self, model, uid):
            return {TAKE: take, CLIP: clip}.get(str(uid))

    geometry = asyncio.run(render_checks.insert_geometry(_Db(), _spec()))
    assert geometry.sources[("take", TAKE)] == PAINTED
    assert geometry.pixel_ratios[TAKE] == 2.0 and geometry.browser_zooms[TAKE] == 190


def test_the_render_s_soft_record_advises_from_the_ratio_the_recording_was_made_at():
    # A 1600x900 window at pixel ratio 1 zoomed to 190%: laid out at 842x474, painted 1600x900, stored ratio 1.9.
    css, stored = zoomed_window((1600, 900), 1.0, 190)
    payload = {"quad": [[0.0, 0.0], [0.8, 0.0], [0.8, 0.8], [0.0, 0.8]], "fit": "cover", "zoom": {"scale": 1.5}}
    source = SimpleNamespace(width=1600, height=900)
    ratio = recorded_ratio(stored, {**SCRIPT, "pixel_ratio": 1})
    record = render._insert_density(payload, source, (1920.0, 1080.0), "take", ratio)
    assert record is not None and record["value"] < 0.8 and record["pixel_ratio"] == 1.5
    # Read at the zoomed ratio, the window looked 842 px wide and no ratio fitted.
    assert "pixel_ratio" not in render._insert_density(payload, source, (1920.0, 1080.0), "take", stored)
    assert css == (842, 474)


# ── The preview lane says how many workers draw previews ─────────────────

T0 = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
A, B = uuid.uuid4(), uuid.uuid4()


def test_the_lane_counts_the_workers_that_said_what_they_have_free(monkeypatch):
    async def none(**_kwargs):
        return []

    monkeypatch.setattr(preview_routing, "rooms", none)
    assert asyncio.run(preview_routing.lane_workers()) is None

    async def one(**_kwargs):
        return [preview_routing.Room("w1", None, 1, capacity=2)]

    monkeypatch.setattr(preview_routing, "rooms", one)
    preview_routing.forget_lane_workers()
    lane = asyncio.run(preview_routing.lane_workers())
    assert (lane.workers, lane.slots) == (1, 2) and lane.words() == "1 worker draws previews, 2 at a time"

    async def three(**_kwargs):
        return [preview_routing.Room(f"w{n}", None, 2, capacity=2 if n else None) for n in range(3)]

    monkeypatch.setattr(preview_routing, "rooms", three)
    preview_routing.forget_lane_workers()
    assert asyncio.run(preview_routing.lane_workers()).words() == "3 workers draw previews, 6 at a time"


def test_the_queue_lines_and_the_lane_advice_share_one_read_of_the_preview_workers(monkeypatch):
    """``lane_workers`` scans the rooms once every ``LANE_WORKERS_SECONDS`` - every queue line read in that time
    and every preview start's lane advice take that read."""
    scans: list[int] = []

    async def counted(**_kwargs):
        scans.append(1)
        return [preview_routing.Room("w1", None, 1, capacity=2)]

    monkeypatch.setattr(preview_routing, "rooms", counted)
    reads = [asyncio.run(preview_routing.lane_workers()) for _ in range(5)]
    assert len(scans) == 1 and all(read is reads[0] for read in reads)
    # Past its time, read again.
    preview_routing._lane_read[0] = (preview_routing._lane_read[0][0] - preview_routing.LANE_WORKERS_SECONDS - 0.1, reads[0])
    asyncio.run(preview_routing.lane_workers())
    assert len(scans) == 2


def test_the_queue_line_says_the_workers_the_lane_has():
    queue = jobs.QueuePlace(running=2, ahead=1, per_worker=2, lane="preview", workers=preview_routing.LaneWorkers(1, 2))
    row = SimpleNamespace(id=uuid.uuid4(), status="pending", progress=0, stage="Queued", progress_at=None)
    line = jobs.describe(jobs.job_state("render", row, queue=queue))
    assert line.endswith("(1 worker draws previews, 2 at a time)")
    assert queue.as_dict()["preview_workers"] == 1 and queue.as_dict()["preview_slots"] == 2
    # No worker said it: a worker's own slots, as before.
    unknown = jobs.QueuePlace(running=2, ahead=1, per_worker=2, lane="preview")
    assert jobs.describe(jobs.job_state("render", row, queue=unknown)).endswith("(a worker makes 2 at a time)")
    assert "preview_workers" not in unknown.as_dict()


def _execution(seconds: int, *, state: str = "pending"):
    return SimpleNamespace(
        id=uuid.uuid4(), state=state, adapter_state=None, payload={}, run_at=T0 + timedelta(seconds=seconds), created_at=T0 + timedelta(seconds=seconds)
    )


def test_a_preview_that_waited_behind_other_videos_on_one_worker_says_what_a_second_gives(monkeypatch):
    mine, theirs = _execution(0, state="running"), _execution(5, state="running")
    lane = Lane([], [mine, theirs], {mine.id: A, theirs.id: B}, {})

    async def read(_db):
        return lane

    monkeypatch.setattr(preview_lane, "read_lane", read)
    one = preview_routing.LaneWorkers(1, 2)
    later = T0 + timedelta(minutes=25)
    advice = asyncio.run(preview_lane.lane_advice(None, mine, one, now=later))
    assert advice is not None and advice.kind == "preview_workers" and advice.key == "preview_workers:1:2"
    assert advice.text.startswith("Scene previews of 2 videos share one worker's 2 preview slots: this one waited 25 min")
    assert "VIDEO_STUDIO_RENDER_CONCURRENCY=0" in advice.text and "The video needs no change." in advice.text
    # Two workers, a short wait, its own video alone, or no worker said: nothing to say.
    assert asyncio.run(preview_lane.lane_advice(None, mine, preview_routing.LaneWorkers(2, 4), now=later)) is None
    assert asyncio.run(preview_lane.lane_advice(None, mine, one, now=T0 + timedelta(seconds=30))) is None
    assert asyncio.run(preview_lane.lane_advice(None, mine, None, now=later)) is None
    lane.video_of[theirs.id] = A
    assert asyncio.run(preview_lane.lane_advice(None, mine, one, now=later)) is None


def test_a_preview_keeps_both_kinds_of_advice_each_said_once():
    from app.services.video_studio import render_preflight
    from app.services.video_studio.render_preflight import Infra

    reserve = Infra("preview_reserve", "preview_reserve:8192:2:16384", "Give it more memory.")
    workers = Infra("preview_workers", "preview_workers:1:2", "Run a second preview worker.")
    row = SimpleNamespace(id=uuid.uuid4(), preflight={"infra": [reserve.as_dict()]})
    # An earlier preview said the lane advice with these numbers, and the reserve advice with others.
    earlier = [({"infra": [workers.as_dict()]},), ({"infra": [{**reserve.as_dict(), "key": "preview_reserve:8192:2:24576"}]},)]

    class _Db:
        async def get(self, _model, _key):
            return row

        async def execute(self, _statement):
            return SimpleNamespace(all=lambda: earlier)

        async def commit(self):
            return None

    asyncio.run(render_preflight.record_preview_infra(_Db(), str(row.id), workers))
    kinds = {item["kind"]: item for item in row.preflight["infra"]}
    assert set(kinds) == {"preview_reserve", "preview_workers"} and kinds["preview_workers"]["said"] is False
    assert render_preflight.infra_of(row) == ["Give it more memory."]
