"""A render replaced by the render of a newer revision of its video hands it the frames of the unchanged scenes.

* the window digest: two documents draw the same frames over a window when the
  scenes on screen, the scene before them's branding, the cues over them, the
  look and - near them only - the fades and the last frame are the same; a
  scene changed, moved or re-indexed is drawn again, and every window when
  the watermark stands on the captions' edge and a cue changed;
* the take-over: rev N's render stopped at 20% stored a segment in each of
  three scenes; rev N+1 changes the middle one: the segments of the two
  unchanged scenes are taken (with their audits and evidence), stored as the
  new render's own, and reported by scene - the changed scene's is drawn;
* the start: a person's start asks before cancelling an older revision's
  render once the gate passed; an agent's cancels it and says what is kept;
* the answer: what the new render kept, by scene, in its notes and its
  measured record, never in its rate.

Synthetic documents and a fake object store; the end-to-end draw needs Chromium and ffmpeg.
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.video_studio import JOB_RUNNING, VideoRender
from app.services.video_studio import (
    compositor,
    jobs,
    projects,
    render_checks,
    render_history,
    render_preflight,
    render_segments,
)
from app.services.video_studio.render import inherited_line
from app.services.video_studio.spec import QUALITY_PRESETS

FPS = 30
PRESET = QUALITY_PRESETS["ultra"]
SIZE = (3840, 2160)


def _document(**changes) -> dict:
    """Three 4 s title scenes, 360 frames, captions over each."""
    scenes = [
        {"id": "s01", "type": "title", "title": "Exposure", "start": 0.0, "end": 4.0},
        {"id": "s02", "type": "title", "title": "Quantified risk", "start": 4.0, "end": 8.0},
        {"id": "s03", "type": "title", "title": "Decision", "start": 8.0, "end": 12.0},
    ]
    document = {
        "version": 1,
        "width": 1920,
        "height": 1080,
        "fps": FPS,
        "duration": 12.0,
        "frames": 360,
        "fade_in": 0.6,
        "fade_out": 0.9,
        "theme": {"mode": "dark", "accent": "#00bcd4"},
        "captions": {
            "enabled": True,
            "style": "boxed",
            "position": "bottom",
            "cues": [[0.5, 3.5, "Your exposure"], [4.5, 7.5, "in euros"], [8.5, 11.5, "for the board"]],
        },
        "watermark": "none",
        "scenes": scenes,
    }
    for path, value in changes.items():
        target = document
        *parents, last = path.split(".")
        for part in parents:
            target = target[int(part)] if isinstance(target, list) else target[part]
        target[int(last) if isinstance(target, list) else last] = value
    return document


def _windows(document: dict) -> compositor.WindowDigests:
    return compositor.WindowDigests(document, PRESET, SIZE)


SEGMENTS = [(0, 60), (60, 120), (120, 180), (180, 240), (240, 300), (300, 360)]


def _same(before: dict, after: dict) -> list[tuple[int, int]]:
    old, new = _windows(before), _windows(after)
    return [piece for piece in SEGMENTS if old.of(*piece) == new.of(*piece)]


# ── The window digest ─────────────────────────────────────────────────────


def test_only_the_windows_over_a_changed_scene_are_drawn_again():
    edited = _document(**{"scenes.1.title": "Risk in euros"})
    # s01 alone over 0-2 s; s03 alone over 10-12 s (the sub-frame either side reaches the scene beside).
    assert _same(_document(), edited) == [(0, 60), (300, 360)]
    assert _same(_document(), _document()) == SEGMENTS


def test_a_scene_moved_in_time_or_among_the_scenes_is_drawn_again():
    longer = _document(**{"scenes.1.end": 9.0, "scenes.2.start": 9.0, "scenes.2.end": 13.0, "duration": 13.0, "frames": 390})
    # The scenes after the longer one draw at other times; the first scene's frames are the same.
    assert _same(_document(), longer) == [(0, 60)]
    reordered = _document()
    reordered["scenes"][0], reordered["scenes"][1] = (
        {**reordered["scenes"][1], "start": 0.0, "end": 4.0},
        {**reordered["scenes"][0], "start": 4.0, "end": 8.0},
    )
    assert (0, 60) not in _same(_document(), reordered)


def test_the_scene_before_counts_by_its_branding_only():
    # What the watermark reads of the scene before: a title brands itself, a statement without its logo does not.
    retitled = _document(**{"scenes.1.title": "Something else", "scenes.1.end": 8.0})
    unbranded = _document(**{"scenes.1.type": "statement"})
    assert (300, 360) in _same(_document(), retitled)
    assert (300, 360) not in _same(_document(), unbranded)


def test_a_cue_counts_over_its_scenes_unless_the_watermark_stands_on_its_edge():
    recaptioned = _document(**{"captions.cues.1.2": "in euros, per year"})
    assert _same(_document(), recaptioned) == [(0, 60), (300, 360)]
    # The watermark on the captions' edge is lifted above the tallest cue: every frame may move.
    marked = {"watermark": "bottom-right"}
    assert _same(_document(**marked), _document(**marked, **{"captions.cues.1.2": "in euros, per year"})) == []
    # On the other edge, it never moves.
    top = {"watermark": "top-right"}
    assert _same(_document(**top), _document(**top, **{"captions.cues.1.2": "in euros, per year"})) == [(0, 60), (300, 360)]


def test_the_fades_count_only_where_the_window_reaches_them():
    assert _same(_document(), _document(fade_out=0.0)) == SEGMENTS[:-1]
    assert _same(_document(), _document(fade_in=0.0)) == SEGMENTS[1:]
    assert _same(_document(), _document(**{"theme.accent": "#ff5722"})) == []


def test_the_files_count_by_what_they_hold_where_a_scene_on_screen_names_them(tmp_path):
    def assets(logo: str, picture: str) -> compositor.CompositorAssets:
        found = compositor.CompositorAssets(images={"logo": str(tmp_path / logo), "image/s02": str(tmp_path / picture)})
        found.origins = {str(tmp_path / logo): f"file:{logo}", str(tmp_path / picture): f"file:{picture}"}
        return found

    document = _document(**{"scenes.1.src": "image/s02"})
    before = compositor.WindowDigests(document, PRESET, SIZE, assets("logo-1", "shot-1"))
    picture = compositor.WindowDigests(document, PRESET, SIZE, assets("logo-1", "shot-2"))
    logo = compositor.WindowDigests(document, PRESET, SIZE, assets("logo-2", "shot-1"))
    # Another picture in s02: only its windows. Another logo, which no scene names: every window.
    assert [p for p in SEGMENTS if before.of(*p) == picture.of(*p)] == [(0, 60), (300, 360)]
    assert [p for p in SEGMENTS if before.of(*p) == logo.of(*p)] == []


def test_what_the_digest_reads_across_scenes_is_what_the_page_reads():
    """A tripwire: the page reads the scene before the one on screen through ``brandsItself``, and lifts a watermark on
    the captions' edge above the tallest cue. Either changed, the window digest must follow."""
    import re

    from app.services.video_studio import compositor_page

    source = Path(compositor_page.__file__).read_text(encoding="utf-8")
    branding = re.search(r"const brandsItself = \(sc\) => (.*?);\n", source, re.DOTALL)
    assert branding is not None
    assert set(re.findall(r"sc\.(\w+)", branding.group(1))) == set(compositor.BRANDING_FIELDS)
    # The scene before also says whether its spec turned the watermark off.
    lifted = re.search(r"const noWatermark = \(sc\) => (.*?);\n", source, re.DOTALL)
    assert lifted is not None
    assert set(re.findall(r"sc\.(\w+)", lifted.group(1))) == {compositor.WATERMARK_FIELD}
    assert "const before = i > 0 && !noWatermark(S.scenes[i - 1]);" in source
    assert "const sameEdge = c.enabled && S.wm && (S.capEdge === 'top') === (S.wm.y < S.H / 2);" in source
    assert "Math.max(...S.capInfo.map(i => i.h))" in source


def test_a_windows_frames_are_counted_to_their_scenes():
    windows = _windows(_document())
    assert windows.scene_frames(0, 60) == {"s01": 60}
    assert windows.scene_frames(100, 130) == {"s01": 20, "s02": 10}
    assert windows.scene_frames(300, 360) == {"s03": 60}


# ── The take-over ─────────────────────────────────────────────────────────


class _Store:
    """The object store: ``name -> bytes``."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def list_object_names(self, prefix: str, *, recursive: bool = True) -> list[str]:
        return sorted(name for name in self.objects if name.startswith(prefix))

    async def download(self, name: str) -> bytes:
        return self.objects[name]

    async def download_to_path(self, name: str, path: str, *, missing_ok: bool = False) -> None:
        if name not in self.objects:
            if missing_ok:
                return
            raise FileNotFoundError(name)
        Path(path).write_bytes(self.objects[name])

    async def upload_path(self, path: str, name: str, content_type: str | None = None) -> str:
        self.objects[name] = Path(path).read_bytes()
        return name

    async def delete(self, name: str) -> None:
        self.objects.pop(name, None)


@pytest.fixture
def store(monkeypatch):
    from app.services.file_storage import file_storage

    fake = _Store()
    for name in ("list_object_names", "download", "download_to_path", "upload_path", "delete"):
        monkeypatch.setattr(file_storage, name, getattr(fake, name))
    return fake


OLD, NEW = str(uuid.uuid4()), str(uuid.uuid4())


def _stored_by_rev_n(store: _Store, document: dict, pieces: list[tuple[int, int]]) -> None:
    """What rev N's render stored before it was replaced at 20%: one segment a page, each with its window."""
    windows = _windows(document)
    for first, end in pieces:
        prefix = render_segments.prefix_of(OLD, "d" * 64)
        name = compositor.segment_name(first, end)
        stem = name[: -len(".mp4")]
        store.objects[prefix + name] = f"frames {first}-{end}".encode()
        record = {"revision": 99, "attempt": "started 2026-10-23T10:44:00", "render": OLD, "window": windows.of(first, end)}
        store.objects[prefix + stem + compositor.DRAWN_SUFFIX] = json.dumps(record).encode()
        if first == 0:
            audits = {"insert/s01.0": {"evidence": {"blur": {"file": "proof-0.png"}}}}
            store.objects[prefix + stem + compositor.AUDIT_SUFFIX] = json.dumps(audits).encode()
            store.objects[f"{prefix}{compositor.EVIDENCE_DIR}/proof-0.png"] = b"png"


def test_the_newer_revisions_render_takes_the_frames_of_every_unchanged_scene(store, tmp_path):
    rev_n = _document()
    rev_n1 = _document(**{"scenes.1.title": "Risk in euros"})
    _stored_by_rev_n(store, rev_n, [(0, 60), (120, 180), (300, 360)])
    runs = asyncio.run(
        render_segments.take_over([(OLD, 99)], NEW, "e" * 64, tmp_path, (0, 360), _windows(rev_n1), set())
    )
    # The changed scene's segment is drawn again; the others are taken, by scene.
    assert [(run["frames"], run["scenes"], run["revision"], run["render"]) for run in runs] == [
        ([0, 60], {"s01": 60}, 99, OLD),
        ([300, 360], {"s03": 60}, 99, OLD),
    ]
    assert sorted(path.name for path in tmp_path.glob("seg-*.mp4")) == ["seg-0000000-0000060.mp4", "seg-0000300-0000360.mp4"]
    # With its audits and the evidence they name, and the record of who drew it.
    assert (tmp_path / compositor.EVIDENCE_DIR / "proof-0.png").read_bytes() == b"png"
    drawn = json.loads((tmp_path / f"seg-0000000-0000060{compositor.DRAWN_SUFFIX}").read_text(encoding="utf-8"))
    assert (drawn["render"], drawn["revision"]) == (OLD, 99)
    # Stored as the new render's own: an attempt on another worker takes them back.
    mine = render_segments.prefix_of(NEW, "e" * 64)
    assert {mine + "seg-0000000-0000060.mp4", mine + "seg-0000300-0000360.mp4"} <= set(store.objects)
    # The line its render says, by scene.
    assert inherited_line(runs) == (
        f"Kept from render `{OLD}` (revision 99), which this render replaced: 120 frames of the scenes this revision "
        "leaves unchanged (s01 60, s03 60), never drawn again."
    )


def test_a_segment_already_kept_or_out_of_range_or_without_a_window_is_not_taken(store, tmp_path):
    document = _document()
    _stored_by_rev_n(store, document, [(0, 60), (60, 120), (300, 360)])
    # Recorded by an older release: no window, never taken.
    prefix = render_segments.prefix_of(OLD, "d" * 64)
    store.objects[prefix + "seg-0000060-0000120" + compositor.DRAWN_SUFFIX] = json.dumps({"revision": 99}).encode()
    runs = asyncio.run(
        render_segments.take_over([(OLD, 99)], NEW, "e" * 64, tmp_path, (0, 300), _windows(document), {(0, 30)})
    )
    assert runs == []


def test_another_quality_draws_other_frames():
    assert _windows(_document()).of(0, 60) != compositor.WindowDigests(_document(), QUALITY_PRESETS["high"], (1920, 1080)).of(0, 60)


# ── The start ─────────────────────────────────────────────────────────────


USER = SimpleNamespace(id=uuid.uuid4(), role="user", email="maker@example.com")


def _older(project, *, progress: int = 35) -> VideoRender:
    return VideoRender(
        id=uuid.uuid4(), project_id=project.id, user_id=USER.id, revision=99, quality="ultra", status=JOB_RUNNING, progress=progress
    )


class _Db:
    def __init__(self) -> None:
        self.added: list = []

    async def execute(self, _statement, _params=None):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []), all=lambda: [])

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


@pytest.fixture
def starting(monkeypatch):
    world = SimpleNamespace(gate=[], enqueued=[])

    async def gate(*_args, **_kwargs):
        return list(world.gate)

    async def effective(*_args, **_kwargs):
        return SimpleNamespace(spec={"scenes": []})

    async def nothing(*_args, **_kwargs):
        return []

    async def enqueue(_db, kind, row, **_kwargs):
        world.enqueued.append(row)

    monkeypatch.setattr(render_checks, "gate", gate)
    monkeypatch.setattr(projects, "effective_spec", effective)
    monkeypatch.setattr(jobs, "renders_in_flight", nothing)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    monkeypatch.setattr("app.services.audit.log_audit", nothing)
    return world


def test_a_persons_start_asks_once_before_cancelling_an_older_revisions_render(starting, monkeypatch):
    project = SimpleNamespace(id=uuid.uuid4(), revision=100, title="Risk film", scene_previews=None)
    older = _older(project)

    async def older_renders(_db, _project):
        return [older]

    monkeypatch.setattr(projects, "renders_of_older_revisions", older_renders)
    with pytest.raises(projects.RenderReplaceAskedError) as asked:
        asyncio.run(projects.start_render(_Db(), project, USER, quality="ultra", ask_before_replacing=True))
    assert str(asked.value) == (
        "The render of revision 99 at 35% (ultra) is still in progress: cancel it and render revision 100? The frames it "
        "drew of the scenes this revision leaves unchanged are kept for the new render."
    )
    assert asked.value.as_dict()["renders"] == [{"id": str(older.id), "revision": 99, "quality": "ultra", "progress": 35}]
    assert starting.enqueued == []
    # Asked once: confirmed, the start goes on.
    started = asyncio.run(projects.start_render(_Db(), project, USER, quality="ultra"))
    assert starting.enqueued == [started.render]


def test_the_gate_is_said_before_the_question(starting, monkeypatch):
    project = SimpleNamespace(id=uuid.uuid4(), revision=100, title="Risk film", scene_previews=None)

    async def older_renders(_db, _project):
        return [_older(project)]

    monkeypatch.setattr(projects, "renders_of_older_revisions", older_renders)
    starting.gate.append(render_checks.GateReason("quality", "scene s02 changed since its preview"))
    with pytest.raises(render_checks.RenderGateError):
        asyncio.run(projects.start_render(_Db(), project, USER, quality="ultra", ask_before_replacing=True))


def test_an_agents_start_says_what_it_cancelled_and_what_is_kept():
    project = SimpleNamespace(id=uuid.uuid4(), revision=100)
    older = _older(project)
    assert projects.replaced_line([older], 100) == (
        f"This render of revision 100 replaced the render of revision 99 at 35% (ultra) still in progress (`{older.id}`), "
        "now cancelled; the frames it drew of the scenes this revision leaves unchanged are kept for this render, the "
        "rest drawn anew."
    )
    same = _older(project)
    same.revision = 100
    assert projects.replaced_line([same], 100) == (
        f"This render replaced the render of the video still in progress (`{same.id}`), now cancelled."
    )
    assert projects.replaced_line([], 100) == ""


# ── The answer ────────────────────────────────────────────────────────────


def test_the_frames_kept_from_the_replaced_render_are_said_by_scene_never_in_the_rate():
    runs = [{"render": OLD, "revision": 99, "frames": [0, 60], "segments": 1, "pieces": [[0, 60]], "scenes": {"s01": 60}}]
    layout = SimpleNamespace(as_dict=lambda: {"pages": 4, "cpus": 8})
    measured = render_preflight.measurement(layout, frames=240, seconds=120.0, frames_inherited=60, inherited=copy.deepcopy(runs))
    assert measured["frames_inherited"] == 60 and measured["frames_reused"] == 0
    assert measured["inherited"] == [{"render": OLD, "revision": 99, "frames": [0, 60], "segments": 1, "scenes": {"s01": 60}}]
    said = render_preflight.inherited_scenes(measured)
    assert said == f"revision 99, render `{OLD}`: s01 60 frames"
    # A render that took frames is no measure of its estimate's error.
    assert render_history.estimate_error({"model": render_preflight.ESTIMATE_MODEL, "raw_seconds": 100}, {**measured, "total_seconds": 130}) is None
