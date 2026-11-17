"""Mixed videos: statements, AI shots that never break a video, and the craft the assistant carries.

* A ``statement`` scene is the message in big type - lines revealed one after
  the other, accent words, a counted figure, chips - over the brand backdrop
  or a darkened AI shot, uploaded video or image.
* An AI shot that is still being made, or that failed, is still the video's:
  it saves, it previews (its scene says why it failed), it is generated again
  in place; only a render refuses it until it is ready.
* The Video Production skill is seeded for every platform and carried by the
  General Assistant (its own skill bindings) and the XTM One assistant.
"""

import asyncio
import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.video_studio import JOB_FAILED, JOB_PENDING, JOB_READY, JOB_RUNNING, VideoClip, VideoProject
from app.services.video_studio import clips, compositor_page, jobs, preview, projects, render, templates, timeline
from app.services.video_studio.spec import (
    VideoSpecError,
    parse_video_spec,
    referenced_ids,
    spec_summary,
    validate_video_spec,
)

pytestmark = pytest.mark.usefixtures("no_default_video_template")

BACKEND_ROOT = Path(__file__).resolve().parents[1]
CLIP_ID = "00000000-0000-0000-0000-0000000000b2"
FILE_ID = "00000000-0000-0000-0000-0000000000c3"


def _user():
    return SimpleNamespace(id=uuid.uuid4(), role="user", is_admin=False, email="user@example.com", is_active=True)


def _spec(*scenes, **top) -> dict:
    return validate_video_spec({"title": "Mixed", "freestyle": True, "scenes": list(scenes), **top})


# ── The statement scene ───────────────────────────────────────────────────


def test_a_statement_takes_lines_a_figure_chips_and_a_backdrop():
    spec = _spec(
        {
            "type": "statement",
            "lines": ["Too much *noise*.", "Not enough *context*."],
            "figure": "6,000+",
            "secondary": ["A proof point."],
            "chips": ["Open source", "Community led"],
            "backdrop": {"clip_id": f"clip:{CLIP_ID}"},
        }
    )
    scene = spec["scenes"][0]
    assert scene["reveal"] == "lines" and scene["dim"] == 0.55 and scene["backdrop"]["clip_id"] == CLIP_ID
    assert referenced_ids(spec)["clip"] == {CLIP_ID}
    assert f"statement: Too much *noise*. / Not enough *context*. over AI shot {CLIP_ID}" in spec_summary(spec)


@pytest.mark.parametrize(
    ("scene", "fragment"),
    [
        ({"type": "statement", "lines": []}, "lines"),
        ({"type": "statement", "lines": ["a", "b", "c", "d", "e"]}, "lines"),
        ({"type": "statement", "lines": ["x" * 91]}, "lines.0"),
        ({"type": "statement", "lines": ["x"], "chips": ["c"] * 7}, "chips"),
        ({"type": "statement", "lines": ["x"], "backdrop": {}}, "exactly one source: clip_id (an AI shot), file_id"),
        ({"type": "statement", "lines": ["x"], "backdrop": {"clip_id": CLIP_ID, "file_id": FILE_ID}}, "exactly one"),
        ({"type": "statement", "lines": ["x"], "dim": 0.95}, "dim"),
    ],
)
def test_a_statement_out_of_bounds_is_refused_where_it_is(scene, fragment):
    with pytest.raises(VideoSpecError) as raised:
        _spec(scene)
    assert "scenes.0 (type=statement)" in str(raised.value)
    assert fragment in str(raised.value)


def test_an_uploaded_backdrop_is_a_file_reference():
    spec = _spec({"type": "statement", "lines": ["x"], "backdrop": {"file_id": FILE_ID}})
    assert referenced_ids(spec)["file"] == {FILE_ID}


def test_a_template_presets_how_a_statement_looks_never_what_it_says():
    canonical, problems = templates.validate_style({"scene_defaults": {"statement": {"reveal": "words", "dim": 0.7}}})
    assert problems == [] and canonical["scene_defaults"]["statement"]["reveal"] == "words"
    _canonical, problems = templates.validate_style({"scene_defaults": {"statement": {"lines": ["Preset"]}}})
    assert any("statement.lines is what a scene shows" in p for p in problems)


def test_the_planner_hands_the_statement_to_the_compositor():
    spec = parse_video_spec(
        _spec(
            {
                "id": "s",
                "type": "statement",
                "lines": ["One"],
                "chips": ["A"],
                "figure": "42",
                "narration": " ".join(["word"] * 30),
            }
        )
    )
    plan = timeline.plan(spec, timeline.PlanMedia(), fps=30, theme={})
    scene = plan.document["scenes"][0]
    assert scene["type"] == "statement" and scene["lines"] == ["One"] and scene["chips"] == ["A"]
    assert scene["figure"] == "42" and scene["backdrop"] is None
    # The narration sets the length when it is longer than the scene.
    assert scene["end"] - scene["start"] > 4.0


def test_a_video_backdrop_fills_its_scene_and_an_image_moves():
    video_spec = parse_video_spec(
        _spec(
            {
                "id": "s",
                "type": "statement",
                "lines": ["x"],
                "duration": 6,
                "backdrop": {"clip_id": CLIP_ID, "trim_start": 1},
            }
        )
    )
    media = timeline.PlanMedia(
        backdrops={"s": timeline.ClipMedia(duration=4.0, width=1280, height=720, fps=24, frame_count=96)}
    )
    backdrop = timeline.plan(video_spec, media, fps=30, theme={}).document["scenes"][0]["backdrop"]
    assert backdrop["kind"] == "video" and backdrop["src"] == "backdrop/s" and backdrop["dim"] == 0.55
    # Its 3 s left after the trim play slowed to fill the 6 s scene, down to SLOWEST_FILL, then hold.
    assert backdrop["time_map"] == [[0.0, 1.0], [4.2857, 4.0], [6.0, 4.0]]
    image_spec = parse_video_spec(
        _spec({"id": "s", "type": "statement", "lines": ["x"], "backdrop": {"file_id": FILE_ID}})
    )
    media = timeline.PlanMedia(backdrops={"s": timeline.ImageMedia(width=1920, height=1080)})
    backdrop = timeline.plan(image_spec, media, fps=30, theme={}).document["scenes"][0]["backdrop"]
    assert backdrop["kind"] == "image" and backdrop["motion"] == "zoom_in"


def test_an_accent_opened_and_never_closed_is_drawn_as_typed_and_a_save_says_so():
    """An unbalanced * is text, never a refused spec; the note names the word to close."""
    from app.services.video_studio.spec import unclosed_accents

    spec = {
        "scenes": [
            {"id": "a", "type": "statement", "lines": ["Stop *every* breach", "5* rated, 4* loved"]},
            {"id": "b", "type": "statement", "lines": ["We see *everything."], "secondary": ["and *act"]},
            {"id": "c", "type": "title", "title": "*not a statement"},
        ]
    }
    validate_video_spec({"title": "T", "freestyle": True, "scenes": spec["scenes"][:2]})
    notes = unclosed_accents(spec)
    assert [note.split(":")[0] for note in notes] == ["Scene b", "Scene b"]
    assert 'the * before "everything" in line 1 opens no accent' in notes[0] and "(*everything*)" in notes[0]
    assert 'the * before "act" in secondary line 2' in notes[1]


def test_a_statements_text_is_one_composited_layer():
    """A layer per line and per word cost a render's software compositor about 4 ms a frame
    over a video backdrop: the card is the one layer of its text."""
    css = re.sub(r"/\*.*?\*/", "", compositor_page.PAGE_CSS, flags=re.S)
    rules = {
        selector.strip(): body
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        if selector.strip().startswith(".stmt")
    }
    assert "will-change: transform" in rules[".stmt"]
    promoted = [selector for selector, body in rules.items() if selector != ".stmt" and "will-change" in body]
    assert promoted == []


_TEXT_WORDS_HARNESS = """
function el(tag, cls, parent) {
  const node = { tag, textContent: '', classes: [cls], classList: { add(c) { node.classes.push(c); } } };
  parent.children.push(node);
  return node;
}
const cases = JSON.parse(process.argv[2]);
const out = {};
for (const [key, text] of Object.entries(cases)) {
  const box = { children: [], textContent: '' };
  textWords(box, text, true, 'mark');
  out[key] = box.children
    .filter(n => n.tag === 'br' || n.textContent.trim())
    .map(n => (n.tag === 'br' ? ['\\n', []] : [n.textContent, n.classes.slice(1)]));
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_accent_words_are_the_balanced_asterisks_and_a_lone_one_is_text(tmp_path):
    source = compositor_page.PAGE_JS
    start = source.index("const ACCENT = ")
    function = source[start : source.index("\n}\n", source.index("function textWords(", start)) + 2]
    script = tmp_path / "words.js"
    script.write_text(function + _TEXT_WORDS_HARNESS, encoding="utf-8")
    cases = {
        "marked": "Stop *every* breach",
        "lone": "5* rated by analysts",
        "loose": "5* rated, 4* loved",
        "plain": "Know your threats",
        "two lines": "Uncover threats.\n*Take action.*",
    }
    node = shutil.which("node") or "node"
    result = subprocess.run(  # noqa: S603
        [node, str(script), json.dumps(cases)], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    words = json.loads(result.stdout)
    assert words["marked"] == [["Stop", []], ["every", ["mark"]], ["breach", []]]
    # A lone asterisk is no markup: drawn as typed, and the last word takes the gradient as without any.
    assert words["lone"] == [["5*", []], ["rated", []], ["by", []], ["analysts", ["hl"]]]
    # Two asterisks are an accent only when they hug words: "4* loved" does not close "5* rated".
    assert words["loose"] == [["5*", []], ["rated,", []], ["4*", []], ["loved", ["hl"]]]
    assert words["plain"][-1] == ["threats", ["hl"]]
    # A line break the author typed breaks the line, and the accent starts on the new one.
    assert words["two lines"] == [["Uncover", []], ["threats.", []], ["\n", []], ["Take", ["mark"]], ["action.", ["mark"]]]


# ── AI shots never break a video ──────────────────────────────────────────


class _Rows:
    """``db.get`` answers scripted rows by model and id; ``execute`` finds nothing."""

    def __init__(self, rows):
        self._rows = rows

    async def get(self, model, key):
        return self._rows.get((model, str(key)))

    async def execute(self, *_args, **_kwargs):
        return SimpleNamespace(scalar_one_or_none=lambda: None, scalars=lambda: SimpleNamespace(all=lambda: []))


def _clip(status: str, **columns) -> VideoClip:
    base = {
        "id": uuid.UUID(CLIP_ID),
        "provider": "openai",
        "model": "sora-2",
        "prompt": "Particles drifting over a dark globe",
        "params": {"duration_seconds": 8, "provider_id": str(uuid.uuid4())},
        "status": status,
    }
    base.update(columns)
    return VideoClip(**base)


def test_a_failed_shot_never_blocks_a_save(monkeypatch):
    async def no_groups(*_args, **_kwargs):
        return []

    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", no_groups)
    user = _user()
    db = _Rows({(VideoClip, CLIP_ID): _clip(JOB_FAILED, user_id=user.id, error="The prompt was blocked")})
    spec = _spec({"type": "clip", "clip_id": CLIP_ID})
    assert asyncio.run(projects.verify_references(db, spec, user)) == []


def test_a_failed_shot_previews_with_its_reason_and_a_pending_one_as_being_made(monkeypatch):
    async def no_template(*_args, **_kwargs):
        return None

    async def no_problems(*_args, **_kwargs):
        return []

    async def plain_theme(*_args, **_kwargs):
        return {}, {}, [], {}

    monkeypatch.setattr(projects, "verify_references", no_problems)
    monkeypatch.setattr(templates, "resolve_for_spec", no_template)
    monkeypatch.setattr(preview, "_theme", plain_theme)
    monkeypatch.setattr(preview, "_media_url", lambda file_id, _user: f"/media/{file_id}" if file_id else None)
    draft = {
        "title": "Mixed",
        "freestyle": True,
        "scenes": [
            {"id": "shot", "type": "clip", "clip_id": CLIP_ID},
            {"id": "words", "type": "statement", "lines": ["x"], "backdrop": {"clip_id": CLIP_ID}},
        ],
    }
    project = VideoProject(id=uuid.uuid4(), user_id=uuid.uuid4(), title="Mixed", spec=draft, revision=1)
    failed = _clip(JOB_FAILED, error="The prompt was blocked")
    result = asyncio.run(preview.build_preview(_Rows({(VideoClip, CLIP_ID): failed}), project, _user(), draft))
    scenes = {scene["id"]: scene for scene in result["document"]["scenes"]}
    assert scenes["shot"]["preview_state"] == {
        "state": "failed",
        "kind": "clip",
        "clip_id": CLIP_ID,
        "error": "The prompt was blocked",
    }
    # Behind a statement, the brand backdrop stands in; the scene still says why.
    assert scenes["words"]["preview_state"]["backdrop"] is True and scenes["words"]["backdrop"] is not None
    assert "backdrop/words" not in result["document"]["preview_media"]

    pending = _clip(JOB_PENDING, progress=40)
    result = asyncio.run(preview.build_preview(_Rows({(VideoClip, CLIP_ID): pending}), project, _user(), draft))
    shot = result["document"]["scenes"][0]
    assert shot["preview_state"] == {"state": "clip", "clip_id": CLIP_ID, "progress": 40}
    ready = _clip(JOB_READY, file_id=uuid.uuid4(), duration_ms=8000, width=1280, height=720)
    result = asyncio.run(preview.build_preview(_Rows({(VideoClip, CLIP_ID): ready}), project, _user(), draft))
    assert result["document"]["preview_media"]["backdrop/words"] == f"/media/{ready.file_id}"
    # A deleted shot is never "being generated" forever.
    result = asyncio.run(preview.build_preview(_Rows({}), project, _user(), draft))
    shot = result["document"]["scenes"][0]
    assert shot["preview_state"]["state"] == "failed" and "no longer exists" in shot["preview_state"]["error"]


def test_a_render_says_why_a_shot_stops_it():
    db = _Rows({(VideoClip, CLIP_ID): _clip(JOB_FAILED, error="moderation")})
    with pytest.raises(RuntimeError, match=r"AI shot .* failed \(moderation\): generate it again"):
        asyncio.run(render._ready_clip(db, "s1", CLIP_ID))
    db = _Rows({(VideoClip, CLIP_ID): _clip(JOB_PENDING)})
    with pytest.raises(RuntimeError, match="still being generated"):
        asyncio.run(render._ready_clip(db, "s1", CLIP_ID))


class _Session:
    def __init__(self):
        self.flushed = 0
        self.locked: list = []

    async def refresh(self, row, *, with_for_update=False):
        if with_for_update:
            self.locked.append(row)

    async def flush(self):
        self.flushed += 1


def test_a_failed_shot_is_generated_again_in_place(monkeypatch):
    config = SimpleNamespace(id=uuid.uuid4(), provider="gemini", display_name="Gemini")
    enqueued: list = []

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return object(), model or "veo-3.1-generate-preview", config

    async def enqueue(_db, kind, row, *, generation=0, owed_to=None):
        enqueued.append((kind, row.id, generation))

    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    clip = _clip(
        JOB_FAILED,
        error="The prompt was blocked",
        params={"duration_seconds": 6, "resolution": "720p", "attempt": 1},
    )
    session = _Session()
    plan = asyncio.run(clips.restart_clip(session, _user(), clip, prompt="A calmer prompt", model="veo-3.1-fast"))
    assert plan.label == "Gemini" and plan.notes == []
    # Locked before its status is read: the editor and the assistant at once start one attempt.
    assert session.locked == [clip]
    # The same row: every scene using it keeps it.
    assert clip.id == uuid.UUID(CLIP_ID) and clip.status == JOB_PENDING and clip.error is None
    assert clip.prompt == "A calmer prompt" and clip.model == "veo-3.1-fast" and clip.provider == "gemini"
    assert clip.params["attempt"] == 2 and clip.params["previous_error"] == "The prompt was blocked"
    # What the retry leaves out stays as the shot was asked.
    assert clip.params["duration_seconds"] == 6 and clip.params["resolution"] == "720p"
    assert enqueued == [("clip", clip.id, 2)]
    # A shot still being made is not asked twice.
    with pytest.raises(clips.ClipError, match="still being made"):
        asyncio.run(clips.restart_clip(_Session(), _user(), _clip(JOB_RUNNING)))
    # Each attempt is a paid generation: the retries of one prompt and start frame stop at a cap.
    spent = _clip(JOB_FAILED, params={"duration_seconds": 8, "attempt": clips.MAX_RETRIES})
    spent.params[clips.REGENERATIONS] = {"key": clips.regeneration_key(spent.prompt, spent.params), "count": clips.MAX_RETRIES}
    with pytest.raises(clips.ClipError, match=f"generated again {clips.MAX_RETRIES} times from the same prompt"):
        asyncio.run(clips.restart_clip(_Session(), _user(), spent))


def test_a_ready_shot_that_came_out_wrong_is_generated_again_in_place(monkeypatch):
    """A bad-but-ready shot is generated again on its row, its take playing until a new one is ready."""
    config = SimpleNamespace(id=uuid.uuid4(), provider="gemini", display_name="Gemini")
    enqueued: list = []

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return object(), model or "veo-3.1-generate-preview", config

    async def enqueue(_db, kind, row, *, generation=0, owed_to=None):
        enqueued.append((kind, row.id, generation))

    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    video, poster = uuid.uuid4(), uuid.uuid4()
    clip = _clip(JOB_READY, file_id=video, poster_file_id=poster, params={"duration_seconds": 8, "sound": False})
    plan = asyncio.run(clips.restart_clip(_Session(), _user(), clip, prompt="The same badge, the camera still"))
    assert clip.id == uuid.UUID(CLIP_ID) and clip.status == JOB_PENDING
    # The take keeps playing: the row still names it, nothing of it is to be deleted.
    assert (clip.file_id, clip.poster_file_id) == (video, poster) and plan.previous_take == "kept"
    assert clips.keeps_take(clip.params) and clips.REPLACED_MEDIA not in clip.params
    assert clips.playable(clip)
    # What the call leaves out - a silent shot - stays as asked.
    assert clip.params["sound"] is False and clip.prompt == "The same badge, the camera still"
    assert enqueued == [("clip", clip.id, 1)]


def test_a_ready_shot_generated_again_with_discard_drops_its_take(monkeypatch):
    """``discard: true`` is the explicit way to drop a ready take: deleted once the new generation ends."""
    config = SimpleNamespace(id=uuid.uuid4(), provider="gemini", display_name="Gemini")

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return object(), model or "veo-3.1-generate-preview", config

    async def enqueue(*_args, **_kwargs):
        return None

    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    video, poster = uuid.uuid4(), uuid.uuid4()
    clip = _clip(JOB_READY, file_id=video, poster_file_id=poster, params={"duration_seconds": 8})
    plan = asyncio.run(clips.restart_clip(_Session(), _user(), clip, discard=True))
    assert clip.file_id is None and clip.poster_file_id is None and plan.previous_take == "discarded"
    assert clip.params[clips.REPLACED_MEDIA] == [str(video), str(poster)] and not clips.keeps_take(clip.params)
    assert not clips.playable(clip)


def _run_clip_tool(monkeypatch, arguments: dict) -> tuple[list, list]:
    """Call ``generate_video_clip`` as the assistant does: ``(restarts, starts)`` it asked for."""
    from contextlib import asynccontextmanager

    from app.services.builtin_tools import video_studio as tool
    from app.tools.base import ToolCall

    user = _user()
    clip = _clip(JOB_FAILED, user_id=user.id, error="busy")
    restarts: list[dict] = []
    starts: list[dict] = []

    class _Db:
        async def get(self, _model, _key):
            return clip

        async def commit(self):
            return None

    @asynccontextmanager
    async def session():
        yield _Db()

    async def find_user(_db, _user_id):
        return user

    async def restart(_db, _user, _row, **changes):
        restarts.append(changes)
        return SimpleNamespace(label="Gemini", notes=[])

    async def plan(_db, _user, **settings):
        starts.append(settings)
        return SimpleNamespace(label="Gemini", notes=[])

    async def queue(_db, _user, _plan, **_kwargs):
        return clip

    async def nothing(*_args, **_kwargs):
        return []

    monkeypatch.setattr(tool, "async_session", session)
    monkeypatch.setattr(tool, "_user", find_user)
    monkeypatch.setattr(clips, "restart_clip", restart)
    monkeypatch.setattr(clips, "plan_clip", plan)
    monkeypatch.setattr(clips, "queue_clip", queue)
    monkeypatch.setattr(jobs, "wake", nothing)
    monkeypatch.setattr(jobs, "wait_for", nothing)
    _definitions, executor = tool.build_video_studio_tools(None, str(user.id))
    asyncio.run(executor.execute_tool(ToolCall(id="c1", name="generate_video_clip", arguments=arguments)))
    return restarts, starts


def test_a_retry_from_the_assistant_keeps_what_it_leaves_out(monkeypatch):
    restarts, _ = _run_clip_tool(monkeypatch, {"clip_id": CLIP_ID})
    # Nothing said, nothing changed: the shot keeps its length and resolution.
    assert restarts[0]["duration_seconds"] is None and restarts[0]["resolution"] is None
    restarts, _ = _run_clip_tool(monkeypatch, {"clip_id": CLIP_ID, "duration_seconds": 4, "resolution": "720p"})
    assert restarts[0]["duration_seconds"] == 4 and restarts[0]["resolution"] == "720p"
    # A new shot takes the defaults.
    _, starts = _run_clip_tool(monkeypatch, {"prompt": "Particles over a globe"})
    assert (starts[0]["duration_seconds"], starts[0]["resolution"], starts[0]["aspect_ratio"]) == (8, "1080p", "16:9")


def test_the_assistant_starts_a_shot_from_a_recording_step_or_continues_a_shot(monkeypatch):
    from app.services.builtin_tools import video_studio as tool

    take_id = "00000000-0000-0000-0000-0000000000b1"
    _, starts = _run_clip_tool(
        monkeypatch, {"prompt": "The dashboard comes alive", "reference_recording_id": take_id, "reference_at_step": 2}
    )
    assert (starts[0]["reference_recording_id"], starts[0]["reference_at_step"]) == (take_id, 2)
    assert starts[0]["reference_clip_id"] is None and starts[0]["reference_at_seconds"] is None
    _, starts = _run_clip_tool(monkeypatch, {"prompt": "The orbit goes on", "reference_clip_id": CLIP_ID})
    assert starts[0]["reference_clip_id"] == CLIP_ID and starts[0]["reference_recording_id"] is None
    assert starts[0]["crop"] is None
    # A 16:9 recording into a vertical shot: its middle, cut to 9:16.
    _, starts = _run_clip_tool(
        monkeypatch,
        {"prompt": "Vertical", "reference_recording_id": take_id, "aspect_ratio": "9:16", "crop": "center"},
    )
    assert (starts[0]["aspect_ratio"], starts[0]["crop"]) == ("9:16", "center")
    schema = next(d for d in tool._definitions(["high"]) if d.name == "generate_video_clip").input_schema
    assert {
        "reference_file_id",
        "reference_recording_id",
        "reference_clip_id",
        "reference_at_step",
        "reference_at_seconds",
        "crop",
    } <= set(schema["properties"])
    assert schema["properties"]["crop"]["enum"] == ["center"]


def test_each_generation_of_a_shot_runs_on_its_own_execution(monkeypatch):
    keys: list[str] = []

    async def create_execution(_db, **kwargs):
        keys.append(kwargs["dedup_key"])
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr("app.services.orchestration_engine.create_execution", create_execution)
    clip = _clip(JOB_PENDING)
    asyncio.run(jobs.enqueue(object(), "clip", clip))
    asyncio.run(jobs.enqueue(object(), "clip", clip, generation=2))
    assert keys == [f"video_clip:{CLIP_ID}", f"video_clip:{CLIP_ID}:2"]


def test_the_editor_and_the_assistant_can_generate_a_shot_again():
    from app.api.v1 import video_studio as video_studio_api
    from app.services.builtin_tools.video_studio import _definitions, _failed_clip_text

    route = next(r for r in video_studio_api.router.routes if r.path == "/clips/{clip_id}/retry")
    assert "POST" in route.methods
    assert any(dep.call.__name__ == "require_ai_enabled" for dep in route.dependant.dependencies)
    tool = next(d for d in _definitions(["high"]) if d.name == "generate_video_clip")
    assert "clip_id" in tool.input_schema["properties"] and "required" not in tool.input_schema
    text = _failed_clip_text(_clip(JOB_FAILED, error="The prompt was blocked"))
    assert "The prompt was blocked" in text and f"generate_video_clip(clip_id='{CLIP_ID}')" in text
    assert "never put one in its place without saying so" in text


def test_the_spec_contract_names_the_statement_and_real_footage():
    from app.services.builtin_tools.video_studio import _definitions

    contract = next(d for d in _definitions(["high"]) if d.name == "create_video").description
    for words in ("statement {lines", "backdrop?", "*words* between asterisks", "an extract of an event film"):
        assert words in contract, words


# ── The Video Production skill and the General Assistant's skills ─────────


SKILL_PATH = BACKEND_ROOT / "app" / "agents" / "seed_data" / "skill_content" / "video-production.md"


def test_the_skill_is_seeded_everywhere_and_carried_by_both_assistants():
    from app.agents.builtin_agents import BUILTIN_SKILLS
    from app.agents.seed import _resolve_seed_content, seed_matches_platform
    from app.services.builtin_tools.skills import MAX_CATALOG_DESCRIPTION_CHARS
    from app.services.general_assistant_skills import GENERAL_ASSISTANT_SKILL_SLUGS

    skill = next(s for s in BUILTIN_SKILLS if s["slug"] == "video-production")
    for mode in ("copilot", "xtm_one", "dev"):
        assert seed_matches_platform(skill, mode), mode
    assert "xtm-one-assistant" in skill["bind_to_agent_slugs"]
    assert len(skill["description"]) <= MAX_CATALOG_DESCRIPTION_CHARS
    assert _resolve_seed_content("skill_content", skill["content"]) == SKILL_PATH.read_text(encoding="utf-8")
    assert {"video-production", "presentation-design"} <= set(GENERAL_ASSISTANT_SKILL_SLUGS)


def test_every_scene_the_skill_shows_is_a_valid_scene():
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)\n```", text, re.S)
    assert len(blocks) >= 4
    for block in blocks:
        validate_video_spec({"title": "Skill", "freestyle": True, "scenes": [json.loads(block)]})


def test_the_skill_teaches_every_kind_and_the_recovery_of_a_failed_shot():
    text = SKILL_PATH.read_text(encoding="utf-8")
    for purpose in templates.TEMPLATE_PURPOSES:
        assert f"({purpose}" in text, purpose
    for words in (
        "generate_video_clip(clip_id=...)",
        "No text, no logos, no real people",
        "get_template",
        "trim_start",
    ):
        assert words in text, words


class _SkillRows:
    """``(id, tags)`` rows, as the default lookup selects them; the statement is kept."""

    def __init__(self, rows):
        self.rows = rows
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(all=lambda: list(self.rows))


def test_a_general_assistant_never_configured_carries_the_built_in_skills():
    from app.services import general_assistant_skills

    video, decks = uuid.uuid4(), uuid.uuid4()
    rows = [
        (uuid.uuid4(), ["slug:video-production"]),  # not built-in: never a default
        (video, ["builtin", "video", "slug:video-production"]),
        (decks, ["builtin", "slug:presentation-design"]),
    ]
    db = _SkillRows(rows)
    defaults = asyncio.run(general_assistant_skills.effective_skill_ids(db, SimpleNamespace(skill_ids=None)))
    assert set(defaults) == {str(video), str(decks)}
    # Two columns of the enabled company-managed skills: this runs on every turn.
    statement = db.statements[0]
    assert [column.name for column in statement.selected_columns] == ["id", "tags"]
    where = str(statement.whereclause)
    assert "enabled" in where and "is_company_managed" in where
    # The admin's choice is kept as it is, an empty one included.
    assert asyncio.run(general_assistant_skills.effective_skill_ids(db, SimpleNamespace(skill_ids=[]))) == []
    chosen = [str(uuid.uuid4())]
    assert asyncio.run(general_assistant_skills.effective_skill_ids(db, SimpleNamespace(skill_ids=chosen))) == chosen


def test_the_general_assistant_only_carries_company_managed_skills():
    from app.services import general_assistant_skills

    allowed = uuid.uuid4()

    class _Db:
        async def execute(self, _statement):
            return SimpleNamespace(scalars=lambda: iter([allowed]))

    refused = asyncio.run(general_assistant_skills.unbindable_skills(_Db(), [str(allowed), str(uuid.uuid4()), "nope"]))
    assert str(allowed) not in refused and "nope" in refused and len(refused) == 2


class _TurnBuilt(BaseException):
    """Raised from the integration tool build, the step after the skills, to end the turn there."""


def test_a_general_assistant_turn_carries_its_skills(monkeypatch):
    from app.services import builtin_tools, general_chat, integration_tools
    from app.tools.base import CompositeToolExecutor, ToolDefinition

    skill_id = str(uuid.uuid4())
    resolved = [SimpleNamespace(id=skill_id, name="Video production")]
    load_skill = ToolDefinition(name="load_skill", description="Load a skill", input_schema={"type": "object"})
    seen: dict = {}
    registered: list[str] = []

    class _Composite(CompositeToolExecutor):
        def add_tools(self, definitions, executor):
            registered.extend(d.name for d in definitions)
            super().add_tools(definitions, executor)

    async def access(_db, owner_id, *, fallback_user_id=None, **_kwargs):
        seen["access"] = (owner_id, fallback_user_id)
        return "access"

    async def resolve(_db, skill_ids, *, access=None, **_kwargs):
        seen["resolved_ids"] = list(skill_ids)
        return resolved

    def catalog(skills, **_kwargs):
        seen["catalog"] = skills
        return "SKILLS CATALOG"

    def tools(*, skill_ids=None, access=None, **_kwargs):
        seen["tool_ids"] = list(skill_ids)
        return [load_skill], object()

    async def stop(*_args, **_kwargs):
        raise _TurnBuilt

    async def no_quota(*_args, **_kwargs):
        return None

    ga = SimpleNamespace(
        llm_provider=None,
        llm_provider_id=None,
        llm_model=None,
        linked_agent_id=None,
        system_prompt="p",
        knowledge_base_ids=[],
        mcp_server_ids=[],
        custom_tool_ids=[],
        builtin_tool_ids=[],
        skill_ids=[skill_id],
        integration_bindings={"jira": [str(uuid.uuid4())]},
        handover_agent_ids=[],
        handover_types={},
        available_channel_types=["web"],
        max_iterations=None,
        maximum_wait_time=None,
        maximum_total_wait_time=None,
    )
    preferences = SimpleNamespace(
        ga_builtin_tool_ids=None,
        ga_custom_tool_ids=None,
        ga_integration_bindings=None,
        ga_mcp_server_ids=None,
        ga_knowledge_base_ids=None,
    )
    rows = [ga, preferences]

    class _Db:
        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: rows.pop(0))

    monkeypatch.setattr(general_chat, "CompositeToolExecutor", _Composite)
    monkeypatch.setattr(builtin_tools, "build_skill_access_context", access)
    monkeypatch.setattr(builtin_tools, "resolve_agent_skills", resolve)
    monkeypatch.setattr(builtin_tools, "build_skills_catalog_prompt", catalog)
    monkeypatch.setattr(builtin_tools, "build_pinned_skills_prompt", lambda _skills, **_k: "")
    monkeypatch.setattr(builtin_tools, "build_skill_tools", tools)
    monkeypatch.setattr(builtin_tools, "build_identity_context", lambda *_a, **_k: [])
    monkeypatch.setattr("app.services.agent_chat.enforce_agentic_quota", no_quota)
    monkeypatch.setattr(integration_tools, "build_integration_tools", stop)

    async def _platform_wait_policy(_db):
        from app.tools.wait_policy import WaitPolicy

        return WaitPolicy()

    # The wait policy is a platform read of its own, not a row of this stub.
    monkeypatch.setattr("app.services.wait_settings.load_platform_wait_policy", _platform_wait_policy)
    user_id = str(uuid.uuid4())
    with pytest.raises(_TurnBuilt):
        asyncio.run(general_chat.run_general_chat(_Db(), user_id, [], "hello", prebuilt_llm=(object(), "m")))
    # The admin's skills, read as the user (no owner), are catalogued and loadable in the turn.
    assert seen["access"] == (None, user_id)
    assert seen["resolved_ids"] == [skill_id] and seen["tool_ids"] == [skill_id]
    assert seen["catalog"] == resolved
    assert "load_skill" in registered


class _Unset:
    """Platform settings with nothing configured: every field reads ``None``."""

    def __getattr__(self, _name):
        return None


class _OneRow:
    """A session whose every read answers *row* (the General Assistant's configuration, or none)."""

    def __init__(self, row):
        self.row = row
        self.committed = False

    async def execute(self, _statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self.row)

    async def commit(self):
        self.committed = True


def test_the_settings_answer_the_built_in_skills_and_refuse_a_skill_the_assistant_cannot_carry(monkeypatch):
    """The General Assistant's skills through its real settings routes, not their source text."""
    from fastapi import BackgroundTasks, HTTPException

    from app.api.v1 import settings as settings_api
    from app.api.v1.schemas.settings import GeneralAssistantConfigRequest
    from app.services import general_assistant_skills

    async def unset(_db):
        return _Unset()

    monkeypatch.setattr("app.api.v1.platform_settings_helpers.get_platform_settings", unset)
    # Never configured: the built-ins are named by slug, a constant - no query for them.
    answer = asyncio.run(settings_api.get_general_assistant_config(user=SimpleNamespace(id=uuid.uuid4()), db=_OneRow(None)))
    assert answer.default_skill_slugs == list(general_assistant_skills.GENERAL_ASSISTANT_SKILL_SLUGS)

    refused_id, allowed_id = str(uuid.uuid4()), str(uuid.uuid4())
    asked: list = []

    async def unbindable(_db, skill_ids):
        asked.append(list(skill_ids))
        return [refused_id]

    monkeypatch.setattr(general_assistant_skills, "unbindable_skills", unbindable)
    config = SimpleNamespace(skill_ids=[allowed_id], handover_agent_ids=[], handover_types={}, builtin_tool_ids=[])
    db = _OneRow(config)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(
            settings_api.update_general_assistant_config(
                body=GeneralAssistantConfigRequest(skill_ids=[allowed_id, refused_id]),
                request=SimpleNamespace(),
                background_tasks=BackgroundTasks(),
                user=SimpleNamespace(id="admin", email="admin@filigran.io"),
                db=db,
            )
        )
    assert refused.value.status_code == 422 and refused_id in refused.value.detail
    assert asked == [[allowed_id, refused_id]]
    assert config.skill_ids == [allowed_id] and not db.committed
