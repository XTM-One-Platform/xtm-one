"""What a whole render takes instead of drawing, read before it is queued. Synthetic rows, a fake store.

XTM Blast, 20 Oct 2026: the pre-flight counted every figure of a video whose
figures an earlier ultra render had kept beside it, drawn again by none.

* a figure's inputs are what its content is drawn from that is known before
  a render: the scene's digest, the voice of its narration and of its
  neighbours', the figure page's version and the release - never the size
  nor the frame rate; the same inputs, the same digest; any of them changed,
  another;
* a whole render records each figure's inputs beside its key and the frames
  it drew and took; the pre-flight computes the same inputs and serves a
  figure a recent render recorded, at any quality, when a drawing of that
  content is kept beside the video at the variant it draws or sharper -
  nothing else: a blurrier drawing, a changed scene, a drawing
  evicted, another release, a store that cannot be listed.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.video_studio import figure_cache, figures, lexicon_store, render_checks, reuse_index
from app.services.video_studio.spec import parse_video_spec, validate_video_spec

VIDEO_ID = str(uuid.UUID("00000000-0000-0000-0000-000000006170"))
#: A 16:9 render: 1920x1080 design, so 3840x2160 draws a figure at scale 2, 1920x1080 at scale 1.
ULTRA, HIGH, DRAFT = ((3840, 2160), 60), ((1920, 1080), 60), ((1280, 720), 30)
CONTENT = "c" * 64
BLOCK = {
    "type": "architecture",
    "nodes": [{"id": "a", "label": "OpenCTI", "row": 1, "col": 1}, {"id": "b", "label": "OpenAEV", "row": 1, "col": 2}],
    "edges": [{"source": "a", "target": "b"}],
}


def _effective(narration: str = "OpenCTI feeds OpenAEV.") -> dict:
    return validate_video_spec(
        {
            "title": "XTM Blast",
            "freestyle": True,
            "scenes": [
                {"id": "open", "type": "title", "title": "XTM Blast", "duration": 4, "narration": "One platform."},
                {"id": "arch", "type": "figure", "block_spec": BLOCK, "duration": 12, "narration": narration},
                {"id": "close", "type": "title", "title": "filigran.io", "duration": 4},
            ],
        }
    )


DIGESTS = {"open": "d-open", "arch": "d-arch", "close": "d-close"}
VOICES = {"open": "v-open", "arch": "v-arch"}


def _inputs(spec=None, digests=DIGESTS, voices=VOICES) -> dict[str, str]:
    return reuse_index.figure_inputs(spec or parse_video_spec(_effective()), digests, voices)


def test_a_figures_inputs_are_what_its_content_is_drawn_from_that_is_known_before_a_render(monkeypatch):
    inputs = _inputs()
    # Figures only: the cards have no drawing to keep.
    assert list(inputs) == ["arch"] and inputs == _inputs()
    changed = [
        _inputs(digests={**DIGESTS, "arch": "d-arch-2"}),  # what the scene shows, beside its neighbours
        _inputs(voices={**VOICES, "arch": "v-arch-2"}),  # the take that times its build
        _inputs(voices={**VOICES, "open": "v-open-2"}),  # a neighbour's take: the transition between them
    ]
    assert all(other["arch"] != inputs["arch"] for other in changed)
    # The release and the figure page's version are in its key: a drawing of another one is never played.
    monkeypatch.setattr(settings, "app_version", "another-release")
    released = _inputs()["arch"]
    assert released != inputs["arch"]
    monkeypatch.setattr(figures, "RENDER_VERSION", figures.RENDER_VERSION + 1)
    assert _inputs()["arch"] != released
    # A scene no digest names is never matched.
    assert _inputs(digests={"open": "d-open"}) == {}


def test_the_narrations_voice_digests_are_the_voice_caches():
    from app.services.video_studio import voice

    spec = parse_video_spec(_effective())
    assert reuse_index.voice_digests(None, spec, None) == {}
    target = SimpleNamespace(model="gpt-4o-mini-tts", config=SimpleNamespace(id=uuid.uuid4(), provider="openai"), voice="marin")
    lexicon = voice.lexicon_of(spec.voiceover)
    digests = reuse_index.voice_digests(target, spec, lexicon)
    assert set(digests) == {"open", "arch"}
    assert digests["arch"] == voice.digest_of(target, voice.lines_of(spec)[1], spec.voiceover, lexicon)
    reworded = parse_video_spec(_effective("OpenCTI feeds OpenAEV, live."))
    again = reuse_index.voice_digests(target, reworded, voice.lexicon_of(reworded.voiceover))
    assert again["arch"] != digests["arch"] and again["open"] != digests["open"]  # its neighbour reads it as context


def test_a_render_records_each_figure_it_drew_or_took_beside_its_inputs():
    drawings = {
        "arch": figures.Drawing(key=figures.variant_key(CONTENT, 2, 60), drawn=0, reused=720),
        "other": figures.Drawing(key="", drawn=0, reused=0),  # never drawn: nothing to index
    }
    assert reuse_index.figure_entries({"arch": "i" * 40}, drawings) == [
        {"scene_id": "arch", "inputs": "i" * 40, "key": f"{CONTENT}-2x60", "drawn": 0, "reused": 720}
    ]


def test_the_cache_says_what_serves_a_render_the_same_drawing_or_a_sharper_one():
    ultra, high, draft = (figures.variant_key(CONTENT, *variant) for variant in ((2, 60), (1, 60), (1, 30)))
    assert figure_cache.serves(ultra, ultra) and figure_cache.serves(ultra, high) and figure_cache.serves(high, draft)
    # Never a blurrier drawing, another figure, nor a key of an older release.
    assert not figure_cache.serves(high, ultra) and not figure_cache.serves(figures.variant_key("d" * 64, 2, 60), high)
    assert not figure_cache.serves("a" * 64, high)


class _Db:
    """The renders' ``measured`` the index reads, newest first; how often it was read."""

    def __init__(self, rows):
        self.rows, self.reads = rows, 0

    async def execute(self, _statement):
        self.reads += 1
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(self.rows)))


class _Store:
    def __init__(self):
        self.names: set[str] | None = set()
        self.listed = 0

    async def list_object_names(self, prefix, **_kw):
        self.listed += 1
        if self.names is None:
            raise RuntimeError("store unreachable")
        return [name for name in self.names if name.startswith(prefix)]

    def keep(self, *variants: tuple[int, int]) -> None:
        self.names = {figure_cache.object_name(VIDEO_ID, figures.variant_key(CONTENT, *variant)) for variant in variants}


@pytest.fixture
def world(monkeypatch):
    from app.services import file_storage as storage_module

    world = SimpleNamespace(digests=dict(DIGESTS), store=_Store(), digested=0)

    async def scene_digests(_db, _effective, _lexicon):
        world.digested += 1
        return world.digests

    async def no_lexicon(_db, _voiceover):
        return None

    monkeypatch.setattr(render_checks, "scene_digests", scene_digests)
    monkeypatch.setattr(lexicon_store, "for_voiceover", no_lexicon)
    monkeypatch.setattr(storage_module, "file_storage", world.store)
    return world


def _recorded(variant: tuple[int, int] = (2, 60)) -> dict:
    """What a whole render measured: its figure drawn from the inputs the next render has (no voice resolved here)."""
    inputs = reuse_index.figure_inputs(parse_video_spec(_effective()), DIGESTS, {})
    drawing = figures.Drawing(key=figures.variant_key(CONTENT, *variant), drawn=640, reused=80)
    return {"frames": 1200, "figures": reuse_index.figure_entries(inputs, {"arch": drawing})}


def _served(db, at=ULTRA) -> reuse_index.Served:
    size, fps = at
    return asyncio.run(
        reuse_index.served(db, _effective(), video_id=VIDEO_ID, user=None, size=size, fps=fps, lengths={"arch": 12.0})
    )


def test_a_figure_a_recent_render_drew_from_the_same_inputs_is_served_while_its_drawing_is_kept(world):
    world.store.keep((2, 60))
    assert _served(_Db([_recorded()])) == reuse_index.Served(figures=1, served=1, seconds=12.0)
    # Evicted from beside the video (past figure_cache.KEPT_PER_VIDEO): drawn again, so counted.
    world.store.names = {figure_cache.object_name(VIDEO_ID, figures.variant_key("d" * 64, 2, 60))}
    assert _served(_Db([_recorded()])) == reuse_index.Served(figures=1, served=0, seconds=0.0)
    # A store that cannot be listed serves nothing.
    world.store.names = None
    assert _served(_Db([_recorded()])).served == 0


def test_an_ultra_drawing_serves_the_lower_qualities_never_the_other_way(world):
    # A render at another quality rescales a sharper drawing, no page opened.
    world.store.keep((2, 60))
    assert _served(_Db([_recorded()]), at=HIGH).served == 1 and _served(_Db([_recorded()]), at=DRAFT).served == 1
    world.store.keep((1, 30))
    assert _served(_Db([_recorded((1, 30))]), at=ULTRA).served == 0


def test_a_changed_scene_or_a_new_release_is_drawn_again(world, monkeypatch):
    world.store.keep((2, 60))
    world.digests = {**DIGESTS, "arch": "d-arch-edited"}
    assert _served(_Db([_recorded()])).served == 0 and world.store.listed == 0
    world.digests = dict(DIGESTS)
    recorded = _recorded()
    monkeypatch.setattr(settings, "app_version", "the-next-release")
    assert _served(_Db([recorded])).served == 0


def test_nothing_indexed_reads_nothing_more(world):
    # Renders that recorded no figure, or a key of an older release (no variant): nothing to serve.
    older = {"figures": [{"scene_id": "arch", "inputs": _inputs()["arch"], "key": "a" * 64}]}
    db = _Db([{"frames": 1200}, None, older])
    assert _served(db) == reuse_index.Served(figures=1) and db.reads == 1
    assert world.digested == 0 and world.store.listed == 0
    # No figure to draw: the index is never read.
    none = _Db([_recorded()])
    asyncio.run(reuse_index.served(none, _effective(), video_id=VIDEO_ID, user=None, size=(3840, 2160), fps=60, lengths={}))
    assert none.reads == 0
