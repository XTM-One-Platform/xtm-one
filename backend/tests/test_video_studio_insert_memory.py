"""A render's memory counts its screen inserts, per insert and per page.

A scene preview of a clip scene with three keyed video inserts and one
picture insert (1280x720 draft) used 8.5 to 8.7 GiB on 8 pages against an
8.3 GiB ceiling: ``render_preflight.memory_needed`` counted the pages and
their encoders, never the inserts each page's frame server decodes and
composes. What is proven:

* the inserts a page draws at once at most are read from a plan (a scene's,
  and two scenes' over the transition between them) or, before anything is
  planned, from a spec and a preview's window (a file counted as a video);
* every measured run of such a scene - one and two 1280x720 pages, one
  1920x1080 page, in Chromium - adds no more than the per-insert term says,
  and the preview ceiling sized with it holds the production run that the
  ceiling without it did not;
* the pages a render draws on, the pre-flight's memory and a preview's
  ceiling - its supervisor's, its admission's and its own process's alike -
  are sized to the scene;
* the frame server decodes the footage frame under a page's matted inserts
  once a frame, shared by all of them.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.orchestration_adapters import video_studio as adapters
from app.services.video_studio import compositor, render_preflight, render_process, resources, screen_insert
from app.services.video_studio.render_preflight import InsertLoad
from app.services.video_studio.spec import parse_video_spec, validate_video_spec

_MIB, _GIB = 1 << 20, 1 << 30
DRAFT = (1280, 720)
#: The measured runs (``compositor.render_frames`` in Chromium, a 6 s clip scene, three keyed 1920x1080 video inserts
#: and one picture insert): ``(output size, pages, the most the inserts added over the same scene without them)``.
MEASURED = [((1280, 720), 1, 398 * _MIB), ((1280, 720), 2, 534 * _MIB), ((1920, 1080), 1, 311 * _MIB)]
QUAD = [[0.3, 0.2], [0.7, 0.22], [0.69, 0.7], [0.31, 0.68]]


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(settings, "video_studio_render_memory_mb", 0)
    monkeypatch.setattr(settings, "video_studio_preview_memory_mb", 0)
    monkeypatch.setattr(settings, "video_studio_render_pages", 0)
    monkeypatch.setattr(resources, "memory_limit", lambda: 24 * _GIB)
    monkeypatch.setattr(render_process, "_ADMITTED", {})
    monkeypatch.setattr(render_process, "_ATTEMPTS", {})


def _insert(source: dict) -> dict:
    return {"type": "screen_insert", "source": source, "quad": QUAD, "key": {"color": "green"}}


def _spec():
    take, clip, file_id = (str(uuid.UUID(int=n)) for n in (1, 2, 3))
    return parse_video_spec(
        validate_video_spec(
            {
                "title": "SOC",
                "freestyle": True,
                "scenes": [
                    {"id": "intro", "type": "title", "title": "SOC", "duration": 3},
                    {
                        "id": "cat-soc",
                        "type": "clip",
                        "clip_id": clip,
                        "overlays": [
                            _insert({"take_id": take}),
                            _insert({"clip_id": clip}),
                            _insert({"file_id": file_id}),
                            _insert({"asset": "logo"}),
                        ],
                    },
                    {"id": "cat-cape", "type": "clip", "clip_id": clip, "overlays": [_insert({"asset": "logo"})]},
                    {"id": "outro", "type": "outro", "title": "End", "duration": 3},
                ],
            }
        )
    )


def test_the_inserts_a_page_draws_at_once_are_a_scenes_or_two_over_a_transition():
    document = {
        "scenes": [
            {"id": "a", "inserts": [{"kind": "video"}, {"kind": "video"}, {"kind": "video"}, {"kind": "image"}]},
            {"id": "b", "inserts": [{"kind": "video"}]},
            {"id": "c", "inserts": []},
            {"id": "d", "inserts": [{"kind": "image"}, {"kind": "image"}]},
        ]
    }
    assert InsertLoad.of(document) == InsertLoad(videos=4, pictures=2)
    assert InsertLoad.of({"scenes": [{"id": "a"}]}) == InsertLoad() and not InsertLoad()


def test_before_a_plan_the_spec_is_read_a_file_counted_as_a_video():
    spec = _spec()
    # A recording, an AI shot and a file play videos; the brand mark is a picture - over the transition into cat-cape.
    assert InsertLoad.of_spec(spec) == InsertLoad(videos=3, pictures=2)
    # A preview's window: the scene before, it, the scene after.
    assert InsertLoad.of_spec(spec, "intro") == InsertLoad(videos=3, pictures=1)
    assert InsertLoad.of_spec(spec, "outro") == InsertLoad(videos=0, pictures=1)
    assert InsertLoad.of_spec(spec, "gone") == InsertLoad()


@pytest.mark.parametrize(("size", "pages", "added"), MEASURED, ids=[f"{s[0]}x{s[1]}-{p}p" for s, p, _a in MEASURED])
def test_every_measured_run_adds_no_more_than_the_insert_term(size, pages, added):
    load = InsertLoad(videos=3, pictures=1)
    term = pages * render_preflight.insert_memory(size, load)
    assert term >= added, f"{term / _MIB:.0f} MiB counted, {added / _MIB:.0f} MiB measured"
    assert render_preflight.memory_needed(size, pages=pages, x264_preset="veryfast", inserts=load) == (
        render_preflight.memory_needed(size, pages=pages, x264_preset="veryfast") + term
    )


def test_the_preview_ceiling_sized_to_its_inserts_holds_the_run_the_old_one_did_not():
    """8 pages of a 1280x720 draft, three keyed video inserts and a picture: 8.5 to 8.7 GiB used, 8.3 GiB ceiling."""
    used, load = int(8.7 * _GIB), InsertLoad(videos=3, pictures=1)
    without = render_preflight.memory_needed(DRAFT, pages=8, x264_preset="veryfast") * render_process.PREVIEW_HEADROOM
    with_inserts = render_preflight.memory_needed(DRAFT, pages=8, x264_preset="veryfast", inserts=load) * render_process.PREVIEW_HEADROOM
    assert without < used <= with_inserts


def test_a_previews_ceiling_and_its_pages_are_sized_to_its_scene(monkeypatch):
    load = InsertLoad(videos=3, pictures=1)
    plain, sized = render_process.preview_memory_ceiling(), render_process.preview_memory_ceiling(load)
    assert sized > plain
    assert render_process.ceiling_of(SimpleNamespace(scene_id="cat-soc", quality="draft"), load) == sized
    # Never above the worker's memory, within the preview reserve unless the budget its pages are laid out under
    # alone is more, and the pages a ceiling holds are fewer with the inserts on each.
    heavy = InsertLoad(videos=40)
    monkeypatch.setattr(resources, "memory_limit", lambda: 3 * _GIB)
    assert render_process.preview_memory_ceiling(heavy) == 3 * _GIB
    monkeypatch.setattr(resources, "memory_limit", lambda: 64 * _GIB)
    _needed, budget = render_process._preview_drawing(heavy, None, None)
    assert render_process.preview_memory_ceiling(heavy) == max(budget, render_process.preview_reserve())
    ceiling = math.ceil(render_preflight.memory_needed(DRAFT, pages=4, x264_preset="veryfast") / render_preflight.MEMORY_HEADROOM)
    drawn = render_preflight.layout(DRAFT, x264_preset="veryfast", ceiling=ceiling + _MIB, pages=4, cpus=8)
    assert drawn.pages == 4
    assert render_preflight.layout(DRAFT, x264_preset="veryfast", ceiling=ceiling + _MIB, pages=4, cpus=8, inserts=load).pages < 4


def test_the_preflight_counts_the_inserts():
    load = InsertLoad(videos=3, pictures=1)
    plain = render_preflight.estimate(frames=900, size=DRAFT, quality="draft", pages=2, cpus=4)
    sized = render_preflight.estimate(frames=900, size=DRAFT, quality="draft", pages=2, cpus=4, inserts=load)
    assert sized.memory_bytes == plain.memory_bytes + 2 * render_preflight.insert_memory(DRAFT, load)


def test_a_previews_inserts_are_read_off_its_videos_spec(monkeypatch):
    from app.models.video_studio import VideoProject

    project = SimpleNamespace(spec=_spec().model_dump(mode="json"))
    asked: list = []

    @asynccontextmanager
    async def session():
        async def get(model, ident):
            asked.append((model, ident))
            return project

        yield SimpleNamespace(get=get)

    monkeypatch.setattr("app.core.database.async_session", session)
    row = SimpleNamespace(id="r", scene_id="cat-cape", project_id="p")
    assert asyncio.run(render_process.preview_inserts(row)) == InsertLoad(videos=3, pictures=2)
    assert asked == [(VideoProject, "p")]
    # A whole render has none; a video that cannot be read leaves the ceiling of a scene without inserts.
    assert asyncio.run(render_process.preview_inserts(SimpleNamespace(scene_id=None))) is None
    project.spec = {"scenes": "not a spec"}
    assert asyncio.run(render_process.preview_inserts(row)) is None


def test_a_preview_is_admitted_with_the_ceiling_of_its_scene(monkeypatch):
    sized = render_process.preview_size(InsertLoad(videos=3, pictures=1))
    held: dict = {}

    async def scene_size(_render_id):
        return sized

    async def run(_adapter, _ctx, _execution):
        held.update(render_process._ADMITTED)
        return "ran"

    monkeypatch.setattr(render_process, "preview_size_of", scene_size)
    monkeypatch.setattr(adapters.VideoRenderAdapter, "execute", run)
    ctx = SimpleNamespace(db=SimpleNamespace(), worker_id="w:1")
    execution = SimpleNamespace(id=uuid.uuid4(), payload={"render_id": "p1"})
    assert asyncio.run(adapters.VideoPreviewAdapter().execute(ctx, execution)) == "ran"
    assert held == {"p1": render_process.PreviewAdmission(sized.ceiling)} and render_process._ADMITTED == {}
    assert sized.ceiling == render_process.preview_memory_ceiling(InsertLoad(videos=3, pictures=1))


def test_the_footage_frame_under_a_pages_matted_inserts_is_decoded_once_a_frame(monkeypatch):
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 36), (32, 255, 30)).save(buffer, format="JPEG")
    reads: list[int] = []

    class Decoder:
        async def frame(self, index):
            reads.append(index)
            return buffer.getvalue()

    async def decoder(_self, _key, _source):
        return Decoder()

    monkeypatch.setattr(compositor.FrameServer, "_decoder", decoder)
    footage = screen_insert.Footage("shot.mp4", "video", 30.0, "clip/cat-soc")
    inserts = [screen_insert.InsertSource(f"s{n}.mp4", "video", tuple(map(tuple, QUAD)), footage=footage) for n in range(3)]
    server = compositor.FrameServer(compositor.CompositorAssets(), "")

    async def draw() -> list:
        under = [await server._footage_frame(insert, 7, (64, 36)) for insert in inserts]
        under.append(await server._footage_frame(inserts[0], 8, (64, 36)))
        return under

    under = asyncio.run(draw())
    assert reads == [7, 8] and under[0] is under[1] is under[2] and under[3] is not under[0]

