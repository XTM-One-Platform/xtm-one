"""A screen insert's source resolution judged before any render, at every render quality.

The source-pixels-per-pixel check (``screen_insert.MIN_DENSITY``, 0.8 at the render's size) ran inside
``render_video``'s gate alone, at the quality asked: every scene previewed green at 1080p, then a 4K render refused
the insert as soft and every touched scene was previewed again. Synthetic geometry - the issue's own numbers: a
4:3 shot covering a 16:9 frame, a laptop screen on it 3575 px wide at 3840x2160, a 2400 px recording (1600x900 at
1.5x) on it. What is proven:

* the same reading runs for standard, high and ultra (``insert_checks.soft_by_quality``) and the save says it as an
  advisory naming the qualities and the source size needed (``quality`` ``insert_resolution``), once per insert;
* the render gate refuses it at ultra alone, never at standard or high;
* a scene preview at 1080p says it without refusing (``render_checks.soft_ahead``), and says nothing it already
  refuses at its own size;
* ``record_browser_demo(target_render="ultra")`` sizes the recording for that quad from the saved spec - a window at
  the quad's own proportions, the page laid out as wide as asked - that the gate then passes.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.models.video_studio import JOB_READY
from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import (
    insert_checks,
    insert_legibility,
    jobs,
    projects,
    quality,
    render_checks,
    screen_insert,
    timeline,
)
from app.services.video_studio.spec import parse_video_spec
from app.tools.base import ToolCall

CLIP = "00000000-0000-0000-0000-00000000c312"
TAKE = "00000000-0000-0000-0000-00000000a312"
VIDEO = "00000000-0000-0000-0000-00000000f312"
#: A laptop screen filling most of a 4:3 shot: 0.931 of its width, 0.8295 of its height - 3575x2389 px at 3840x2160,
#: the shot covering the frame (its top and bottom run past it).
QUAD = [[0.0345, 0.0852], [0.9655, 0.0852], [0.9655, 0.9147], [0.0345, 0.9147]]
SHOT = (1440, 1080)
#: The take: 1600x900 at pixel ratio 1.5.
RECORDED = (2400, 1350)


def _raw(**overlay) -> dict:
    return {
        "title": "Booth",
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


def _geometry(size: tuple[int, int] = RECORDED, ratio: float = 1.5) -> render_checks.InsertGeometry:
    return render_checks.InsertGeometry({("take", TAKE): size, ("clip", CLIP): SHOT}, {"s1": SHOT}, {TAKE: ratio}, {})


def _soft(spec, geometry: render_checks.InsertGeometry, **kwargs) -> list[insert_checks.SoftAt]:
    return insert_checks.soft_by_quality(
        spec,
        sources=geometry.sources,
        footage=geometry.footage,
        pixel_ratios=geometry.pixel_ratios,
        screens_replaced=geometry.screens_replaced,
        **kwargs,
    )


def test_the_quad_is_the_issues_and_its_density_names_the_least_source_that_passes():
    assert insert_checks.quad_on_frame(QUAD, SHOT, (3840, 2160)) == pytest.approx((3575.04, 2388.96))
    found = screen_insert.density(RECORDED, 3575.04, 2388.96)
    # Its height binds the cover fit: 0.57 per pixel at 4K, and the source that passes is 3398 px wide, not 2860.
    assert found.soft and round(found.value, 2) == 0.57
    assert found.needed == (4248, 2389) and found.least == (3398, 1912)
    assert found.as_dict()["least"] == [3398, 1912]
    assert not screen_insert.density(found.least, 3575.04, 2388.96).soft
    # The pixel ratio a 1600 px window passes at: none - 2x paints 3200 px.
    assert insert_checks.ratio_for(1600, found) is None
    assert insert_checks.ratio_for(1700, found) == 2.0


def test_the_save_advises_the_quality_that_refuses_it_and_the_size_it_needs():
    [item] = _soft(parse_video_spec(_raw()), _geometry())
    assert (item.scene_id, item.qualities, item.sharp) == ("s1", ("ultra",), ("standard", "high"))
    said = item.message()
    assert said.startswith("soft at ultra, which render_video refuses: its screen insert 0 draws take")
    assert f"`{TAKE}` (2400x1350 px) on a quad 3575x2389 px at 3840x2160: 0.57 source pixels per pixel" in said
    assert "it needs a source at least 3398x1912 px (4248x2389 draws it one to one)" in said
    assert 'record_browser_demo(target_render: "ultra", video_id, scene: "s1", insert: 0) sizes the recording' in said
    assert said.endswith("; standard and high draw it sharp")
    # A recording the size it names is sharp at every quality: nothing to say.
    assert _soft(parse_video_spec(_raw()), _geometry((3398, 1912), 2.0)) == []


def test_the_quality_report_says_it_as_an_advisory_that_never_blocks():
    spec = parse_video_spec(_raw())
    document = timeline.plan(
        spec, timeline.PlanMedia(clips={"s1": timeline.ClipMedia(8.0, *SHOT, 24.0, 192)}), fps=30, theme={}
    ).document
    facts = quality.ContentFacts(
        shots={CLIP: quality.ShotFacts("Booth", JOB_READY, width=SHOT[0], height=SHOT[1])},
        ready_takes={TAKE},
        soft=_soft(spec, _geometry()),
    )
    [finding] = [f for f in quality.content_findings(spec, document, facts, None) if f.kind == "insert_resolution"]
    assert finding.severity == "advisory" and finding.severity not in render_checks.BLOCKING_SEVERITIES
    assert finding.scene_id == "s1" and finding.group == "content"
    assert finding.message.startswith("soft at ultra, which render_video refuses:")
    assert finding.insert == f"insert/s1.0|take:{TAKE}|ultra"


async def test_check_content_reads_it_off_the_rows_as_the_gate_does(monkeypatch):
    monkeypatch.setattr(render_checks, "insert_geometry", _async(_geometry()))
    [item] = await render_checks.soft_qualities(None, parse_video_spec(_raw()))
    assert item.qualities == ("ultra",)

    async def broken(_db, _spec):
        raise OSError("database gone")

    # Advice that cannot be read is not given: the save goes on.
    monkeypatch.setattr(render_checks, "insert_geometry", broken)
    assert await render_checks.soft_qualities(None, parse_video_spec(_raw())) == []


async def test_the_render_gate_refuses_it_at_ultra_alone(monkeypatch):
    monkeypatch.setattr(render_checks, "insert_geometry", _async(_geometry()))
    spec = parse_video_spec(_raw())
    assert await render_checks._soft_reasons(None, spec, "standard") == []
    assert await render_checks._soft_reasons(None, spec, "high") == []
    [reason] = await render_checks._soft_reasons(None, spec, "ultra")
    assert reason.code == "insert_soft" and reason.scene_id == "s1"
    assert "at 3840x2160: 0.57 source pixels per pixel" in reason.message


async def test_a_scene_preview_says_what_a_larger_quality_refuses_and_never_refuses_it(monkeypatch):
    monkeypatch.setattr(render_checks, "insert_geometry", _async(_geometry()))
    effective = _raw()
    [advice] = await render_checks.soft_ahead(None, effective, scene_id="s1", quality="standard")
    assert advice.startswith("soft at ultra, which render_video refuses:")
    assert await render_checks.soft_ahead(None, effective, scene_id="s2", quality="standard") == []
    # A source soft at the preview's own size is its lint's reason, never said again as advice.
    monkeypatch.setattr(render_checks, "insert_geometry", _async(_geometry((1600, 900), 1.0)))
    assert await render_checks.soft_ahead(None, effective, scene_id="s1", quality="standard") == []
    # A 720p draft of that scene is told what 1080p and 4K would refuse.
    [drafted] = await render_checks.soft_ahead(None, effective, scene_id="s1", quality="draft")
    assert drafted.startswith("soft at standard, high and ultra, which render_video refuses:")


def test_the_advisory_is_said_once_per_insert_and_again_once_it_is_soft_elsewhere(monkeypatch):
    from app.services.video_studio import render_cache

    kept: dict = {}

    async def get_json(kind, digest):
        return kept.get((kind, digest))

    async def put_json(kind, digest, value):
        kept[(kind, digest)] = value

    monkeypatch.setattr(render_cache, "get_json", get_json)
    monkeypatch.setattr(render_cache, "put_json", put_json)

    def report(identity: str) -> quality.QualityReport:
        finding = quality.QualityFinding(0, "s1", "clip", "", "insert_resolution", "advisory", "words", "content", insert=identity)
        return quality.QualityReport(scene_count=1, findings=(finding,))

    ultra = f"insert/s1.0|take:{TAKE}|ultra"
    assert len(asyncio.run(insert_legibility.said_once(VIDEO, report(ultra), shown=8)).findings) == 1
    assert asyncio.run(insert_legibility.said_once(VIDEO, report(ultra), shown=8)).findings == ()
    # A larger quad soft at 1080p too is said again; so is a new recording on the same insert.
    assert len(asyncio.run(insert_legibility.said_once(VIDEO, report(f"{ultra[:-6]}|standard,high,ultra"), shown=8)).findings) == 1
    assert len(asyncio.run(insert_legibility.said_once(VIDEO, report("insert/s1.0|take:another|ultra"), shown=8)).findings) == 1


def test_a_recording_is_sized_for_the_quad_and_the_gate_passes_it():
    spec = parse_video_spec(_raw())
    surfaces = insert_checks.surfaces_at(spec, (3840, 2160), {"s1": SHOT}, scene_id="s1")
    recording = insert_checks.recording_for(surfaces, (1600, 900))
    # No pixel ratio paints enough on a 1600 px window: a wider one, zoomed in as much - the same layout.
    assert recording.frame[0] >= 3398 and recording.pixel_ratio == 2.0
    assert recording.viewport == (1699, 956) and recording.frame == (3398, 1912)
    assert recording.browser_zoom == 106 and recording.css_width >= 1600
    assert not recording.density.soft
    assert insert_checks.soft_before(
        spec, (3840, 2160), sources={("take", TAKE): recording.frame}, footage={"s1": SHOT}, pixel_ratios={TAKE: 2.0}, screens_replaced={}
    ) == []
    # For a 1080p render the 1600 px window at 1.5x is enough (1x paints 0.75 per pixel); asked at 2x, it stays 2x.
    standard = insert_checks.surfaces_at(spec, (1920, 1080), {"s1": SHOT}, scene_id="s1")
    assert insert_checks.recording_for(standard, (1600, 900)).pixel_ratio == 1.5
    assert insert_checks.recording_for(standard, (1600, 900), least_ratio=2.0).pixel_ratio == 2.0
    # A push of 4x on the whole quad at 4K: no window a recording takes is large enough.
    pushed = parse_video_spec(_raw(zoom={"end": 2, "scale": 4}))
    assert insert_checks.recording_for(insert_checks.surfaces_at(pushed, (3840, 2160), {"s1": SHOT}, scene_id="s1"), (1600, 900)) is None


def _async(value):
    async def answer(*_args, **_kwargs):
        return value

    return answer


class _Session:
    def add(self, _row) -> None:
        pass

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass


async def test_record_browser_demo_with_target_render_records_a_take_sized_for_the_quad(monkeypatch):
    @asynccontextmanager
    async def session():
        yield _Session()

    recorded: dict = {}

    def new_take(**kwargs):
        recorded.update(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(tool, "async_session", session)
    monkeypatch.setattr(tool, "_user", _async(SimpleNamespace(id=uuid.uuid4())))
    monkeypatch.setattr(projects, "load_visible_project", _async(SimpleNamespace(id=uuid.UUID(VIDEO), spec=_raw())))
    monkeypatch.setattr(projects, "effective_spec", _async(SimpleNamespace(spec=_raw())))
    monkeypatch.setattr(render_checks, "insert_geometry", _async(_geometry()))
    monkeypatch.setattr(jobs, "new_take", new_take)
    monkeypatch.setattr(tool, "same_take", _async(None))
    monkeypatch.setattr(jobs, "enqueue", _async(None))
    monkeypatch.setattr(jobs, "wake", _async(None))
    monkeypatch.setattr(jobs, "wait_for", _async([]))
    _definitions, executor = tool.build_video_studio_tools(None, str(uuid.uuid4()))

    async def call(**arguments):
        steps = [{"action": "goto", "url": "https://app.example.com/dashboard"}]
        base = {"steps": steps, "start_url": "https://app.example.com", "video_id": VIDEO, "scene": "s1"}
        return await executor.execute_tool(ToolCall(id="c1", name="record_browser_demo", arguments={**base, **arguments}))

    result = await call(target_render="ultra", insert=0)
    script = recorded["script"]
    # At the quad's own proportions: its frames fill the quad, so the 1600 px page asked passes at 2x, unzoomed.
    assert script["viewport"] == {"width": 1600, "height": 1069} and script["pixel_ratio"] == 2
    assert "browser_zoom" not in script
    assert result.content.startswith(
        "Sized for an ultra render (3840x2160) of scene `s1`: its screen insert 0, its quad 3575x2389 px there (1.50:1, "
        "the window's proportions): a 1600x1069 window at pixel ratio 2, its frames 3200x2138 px - 0.89 source pixels "
        "per pixel"
    )
    # Its scene by index works as edit_video reads it; a scene without that insert, or a target alone, is refused.
    recorded.clear()
    await call(target_render="high", scene="0")
    # Its frames filling the quad, the window asked is dense enough at 1x for a high render's 1787 px quad.
    assert recorded["script"]["pixel_ratio"] == 1 and recorded["script"]["viewport"] == {"width": 1600, "height": 1069}
    assert "browser_zoom" not in recorded["script"]
    refused = await call(target_render="ultra", insert=3)
    assert refused.is_error and "has no screen insert 3" in refused.content
    alone = await executor.execute_tool(
        ToolCall(id="c2", name="record_browser_demo", arguments={"steps": [{"action": "wait", "seconds": 1}], "target_render": "ultra"})
    )
    assert alone.is_error and "give its video_id and scene" in alone.content
