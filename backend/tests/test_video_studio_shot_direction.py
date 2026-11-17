"""The art direction of AI shots.

A video template says how its AI shots look and what they never show. The
platform records it on every shot made for a video and sends the video model
the prompt it shapes: the look on a shot drawn from text, what to avoid in the
negative prompt of a model that takes one - never in a prompt. It steers and
informs, it never refuses: a made shot showing what its template avoids is
kept with a note, and a save names the shots made under another template's
direction as advice. Synthetic templates, prompts and frames only.
"""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.llm import identity
from app.llm.video import VideoRequest, VideoResult
from app.models.user import User
from app.models.video_studio import JOB_FAILED, JOB_PENDING, JOB_READY, VideoClip
from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS
from app.services.video_studio import assets, clips, content_drift, encoder, jobs, projects, runners, templates

pytestmark = pytest.mark.usefixtures("video_studio_enabled")

USER = SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-00000000d001"), role="user")
STYLE = "Filmed in natural light, deep navy and cyan tones."
DIRECTION = {
    "template_id": "00000000-0000-0000-0000-0000000000e1",
    "template": "Brand Film",
    "style": STYLE,
    "avoid": ["laser beams", "starfields"],
}


def _row(name: str, shots: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        kind="video",
        description=None,
        tags=[],
        theme_config={},
        extra_metadata={"video": {"shots": shots} if shots else {}, "supported_schemes": ["dark"]},
    )


def _template(shots: dict | None = None) -> templates.VideoTemplate:
    return templates.VideoTemplate.from_container(_row("Brand Film", shots))


_VIDEO_ENTRIES = [entry for entry in BUILTIN_TEMPLATE_CONTAINERS if entry["kind"] == "video"]


# ── The rule on the template ──────────────────────────────────────────────


@pytest.mark.parametrize("entry", _VIDEO_ENTRIES, ids=lambda entry: entry["slug"])
def test_every_built_in_video_template_directs_its_ai_shots(entry):
    style = entry["extra_metadata"]["video"]
    shots = style["shots"]
    assert set(shots) == {"style", "avoid"} and shots["style"] and shots["avoid"]
    canonical, problems = templates.validate_style(style)
    assert problems == [] and canonical["shots"] == shots


@pytest.mark.parametrize("entry", _VIDEO_ENTRIES, ids=lambda entry: entry["slug"])
def test_a_built_in_template_never_asks_for_what_its_shots_avoid(entry):
    words = f"{entry['description']} {entry['extra_metadata']['guidance']}".casefold()
    asked = [term for term in entry["extra_metadata"]["video"]["shots"]["avoid"] if term.casefold() in words]
    assert asked == []


@pytest.mark.parametrize(
    "shots",
    [
        pytest.param({"style": "x" * 601}, id="style-too-long"),
        pytest.param({"avoid": ["x"]}, id="term-too-short"),
        pytest.param({"avoid": [f"term {i}" for i in range(templates.MAX_AVOIDED_TERMS + 1)]}, id="too-many-terms"),
        # A direction steers the shots, it never caps how many a video has.
        pytest.param({"max_share": 0.4}, id="no-share"),
    ],
)
def test_a_direction_the_rules_refuse_is_said_by_field(shots):
    canonical, problems = templates.validate_style({"shots": shots})
    assert canonical is None and any("shots" in problem for problem in problems), problems


def test_the_picker_and_the_assistant_read_the_direction():
    shots = {"style": "Filmed.", "avoid": ["lens flares"]}
    assert templates.template_summary(_row("Brand Film", shots))["shots"] == shots
    assert templates.template_summary(_row("Plain"))["shots"] is None


# ── The prompt a model is sent ────────────────────────────────────────────


def test_a_shot_drawn_from_text_takes_the_look_and_a_negative_prompt_what_the_template_avoids():
    prompt, negative = templates.direct_shot(
        "A crowded station at rush hour", "faces", DIRECTION, start_frame=False, negative_field=True, limit=4000
    )
    assert prompt == f"A crowded station at rush hour\n\n{STYLE}"
    assert negative == "faces, laser beams, starfields"
    # A shot that starts from a picture keeps the picture's look.
    prompt, negative = templates.direct_shot(
        "The screenshot comes alive", None, DIRECTION, start_frame=True, negative_field=True, limit=4000
    )
    assert (prompt, negative) == ("The screenshot comes alive", "laser beams, starfields")


def test_what_to_avoid_is_never_written_into_a_prompt():
    """A model renders what its prompt names: one with no negative prompt is steered by the look alone."""
    prompt, negative = templates.direct_shot(
        "A harbour at dawn", "people", DIRECTION, start_frame=False, negative_field=False, limit=4000
    )
    assert prompt == f"A harbour at dawn\n\n{STYLE}"
    assert "laser" not in prompt and "Avoid" not in prompt
    # The author's own negative prompt reaches the provider as before.
    assert negative == "people"


def test_the_direction_keeps_the_prompt_within_its_limit():
    prompt, _ = templates.direct_shot("x" * 5000, None, DIRECTION, start_frame=False, negative_field=True, limit=4000)
    assert len(prompt) <= 4000 and prompt.endswith(STYLE)


def test_the_avoided_terms_follow_the_authors_once_each():
    assert templates.merge_negative(None, None) is None
    assert templates.merge_negative("  ", []) is None
    assert templates.merge_negative("people", None) == "people"
    assert templates.merge_negative("no LASER BEAMS;", ["laser beams", "Lens flares", "lens flares"]) == (
        "no LASER BEAMS, Lens flares"
    )


def test_an_undirected_shot_is_sent_as_written():
    assert clips.shot_prompts({}, "A harbour at dawn", "people", negative_field=True) == ("A harbour at dawn", "people")
    # A template without a direction applies nothing either.
    plain = {clips.DIRECTION: {"template_id": "t", "template": "Plain", "style": None, "avoid": []}}
    assert clips.shot_prompts(plain, "A harbour at dawn", None, negative_field=True) == ("A harbour at dawn", None)


def test_the_provider_is_asked_about_a_negative_prompt_only_when_there_is_something_to_apply():
    class _Asked:
        asked = 0

        def takes_negative_prompt(self, model):
            self.asked += 1
            return True

    provider = _Asked()
    assert clips.takes_negative_prompt({}, provider, "m") is False and provider.asked == 0
    assert clips.takes_negative_prompt({clips.DIRECTION: DIRECTION}, provider, "m") is True and provider.asked == 1
    assert clips.takes_negative_prompt({clips.DIRECTION: DIRECTION}, None, "m") is False


def test_validate_only_shows_what_the_model_is_sent():
    request = VideoRequest(model="veo", prompt="A harbour at dawn", negative_prompt=None)
    params = {"provider_id": "p", clips.DIRECTION: DIRECTION}
    plan = clips.ClipPlan(
        request=request,
        config=SimpleNamespace(provider="gemini", display_name="Gemini"),
        params=params,
        sent=clips.shot_prompts(params, request.prompt, None, negative_field=True),
    )
    summary = plan.summary()
    assert summary["prompt"] == "A harbour at dawn" and summary["art_direction"] == "Brand Film"
    assert summary["sent_prompt"] == f"A harbour at dawn\n\n{STYLE}"
    assert summary["sent_negative_prompt"] == "laser beams, starfields"


def test_what_is_sent_names_the_ingredients_after_the_look_as_the_worker_does():
    class _NoNegative:
        def takes_negative_prompt(self, model):
            return False

    params = {
        clips.DIRECTION: DIRECTION,
        clips.INGREDIENTS: [{"file_id": str(uuid.uuid4()), "role": "logo", "label": "the brand logo"}],
        clips.INGREDIENTS_PATH: clips.PATH_PROMPT,
    }
    request = VideoRequest(model="sora-2", prompt="A harbour at dawn", negative_prompt="people")
    prompt, negative = clips._sent(params, request, _NoNegative())
    assert prompt == f"A harbour at dawn\n\n{STYLE} Keep the brand logo exactly as in the reference image."
    assert negative == "people"


@pytest.mark.parametrize(
    ("provider", "model", "takes"),
    [
        pytest.param("app.llm.gemini_provider:GeminiProvider", "veo-3.1-generate-preview", True, id="veo"),
        pytest.param("app.llm.kling_provider:KlingProvider", "kling-v3", True, id="kling"),
        pytest.param("app.llm.runway_provider:RunwayProvider", "veo3.1", True, id="runway-veo"),
        pytest.param("app.llm.runway_provider:RunwayProvider", "an-unknown-model", False, id="runway-unknown"),
        pytest.param("app.llm.elevenlabs_provider:ElevenLabsProvider", "veo-3.1-generate-001", True, id="elevenlabs-veo"),
        pytest.param("app.llm.elevenlabs_provider:ElevenLabsProvider", "bytedance-seedance-v2", False, id="elevenlabs-seedance"),
        pytest.param("app.llm.seedance_provider:SeedanceProvider", "dreamina-seedance-2-0-260128", False, id="seedance"),
        pytest.param("app.llm.openai_provider:OpenAIProvider", "sora-2", False, id="openai-videos"),
    ],
)
def test_each_provider_says_whether_its_model_takes_a_negative_prompt(provider, model, takes):
    import importlib

    module, name = provider.split(":")
    cls = getattr(importlib.import_module(module), name)
    assert cls.__new__(cls).takes_negative_prompt(model) is takes


# ── Which direction a shot is made under ──────────────────────────────────


@pytest.fixture
def library(monkeypatch):
    brand = _row("Brand Film", {"style": "Filmed.", "avoid": ["starfields"]})
    default = _row("Product Video", {"style": "The default look."})
    plain = _row("Plain")
    rows = {str(row.id): row for row in (brand, default, plain)}
    videos = {
        "v-brand": SimpleNamespace(spec={}, template_container_id=brand.id),
        "v-free": SimpleNamespace(spec={"freestyle": True}, template_container_id=None),
        "v-hidden": SimpleNamespace(spec={}, template_container_id=uuid.uuid4()),
    }

    async def usable(_db, template_id, _user):
        return rows.get(str(template_id))

    async def visible(_db, video_id, _user):
        return videos.get(video_id)

    async def default_row(_db):
        return default

    monkeypatch.setattr(templates, "load_usable_template", usable)
    monkeypatch.setattr(templates, "default_template", default_row)
    monkeypatch.setattr(projects, "load_visible_project", visible)
    return SimpleNamespace(brand=brand, default=default, plain=plain, rows=rows)


def _direction(**made_for):
    return asyncio.run(clips.resolve_direction(None, USER, **made_for))


def test_a_shot_takes_the_direction_of_the_video_it_is_made_for(library):
    assert _direction(video_id="v-brand") == {
        "template_id": str(library.brand.id),
        "template": "Brand Film",
        "style": "Filmed.",
        "avoid": ["starfields"],
    }
    assert _direction(template_id=str(library.brand.id))["template"] == "Brand Film"
    # Named for no video: the platform's default template, as a video naming none wears it.
    assert _direction()["style"] == "The default look."
    # A video whose template the person can no longer use: the default too, never a silent free style.
    assert _direction(video_id="v-hidden")["template"] == "Product Video"
    # A template without a direction is recorded all the same: a shot generated again reads it again.
    assert _direction(template_id=str(library.plain.id)) == {
        "template_id": str(library.plain.id),
        "template": "Plain",
        "style": None,
        "avoid": [],
    }
    assert _direction(video_id="v-free") is None and _direction(freestyle=True) is None
    with pytest.raises(clips.ClipError, match="No video `v-gone`"):
        _direction(video_id="v-gone")
    with pytest.raises(clips.ClipError, match="No template `t-gone`"):
        _direction(template_id="t-gone")


def test_a_shot_generated_again_reads_its_template_again(library):
    stale = {**DIRECTION, "template_id": str(library.brand.id), "style": "An older look."}
    assert asyncio.run(clips._kept_direction(None, USER, {clips.DIRECTION: stale}))["style"] == "Filmed."
    # The template is no longer the person's to use: the snapshot the shot was made with.
    gone = {**DIRECTION, "template_id": str(uuid.uuid4())}
    assert asyncio.run(clips._kept_direction(None, USER, {clips.DIRECTION: gone})) == gone
    assert asyncio.run(clips._kept_direction(None, USER, {clips.FREESTYLE: True})) is None
    # A shot made before shots had a direction takes the default's.
    assert asyncio.run(clips._kept_direction(None, USER, {}))["template"] == "Product Video"


class _Session:
    async def refresh(self, row, *, with_for_update=False):
        return None

    async def flush(self):
        return None


def test_generated_again_for_another_video_the_shot_takes_that_videos_direction(library, monkeypatch):
    config = SimpleNamespace(id=uuid.uuid4(), provider="gemini", display_name="Gemini")

    class _Veo:
        def takes_negative_prompt(self, model):
            return True

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return _Veo(), model or "veo-3.1-generate-preview", config

    async def enqueue(*_args, **_kwargs):
        return None

    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    clip = VideoClip(
        id=uuid.uuid4(),
        user_id=USER.id,
        prompt="A harbour at dawn",
        model="veo-3.1-generate-preview",
        status=JOB_FAILED,
        params={clips.FREESTYLE: True, "duration_seconds": 8},
    )
    plan = asyncio.run(clips.restart_clip(_Session(), USER, clip, video_id="v-brand"))
    assert clip.params[clips.DIRECTION]["template"] == "Brand Film" and clips.FREESTYLE not in clip.params
    # The prompt stays the author's: the direction is applied when the shot is sent, never written into it.
    assert clip.prompt == "A harbour at dawn"
    assert plan.summary()["sent_prompt"] == "A harbour at dawn\n\nFilmed."


# ── The worker ────────────────────────────────────────────────────────────


@pytest.fixture
def worker(monkeypatch):
    row = VideoClip(
        id=uuid.uuid4(),
        user_id=USER.id,
        provider="openai",
        model="sora-2",
        prompt="A crowded station at rush hour",
        params={"provider_id": str(uuid.uuid4()), "duration_seconds": 8, clips.DIRECTION: DIRECTION},
        status=JOB_PENDING,
    )
    world = SimpleNamespace(row=row, asked=[], checked=[], negative_field=False, seen=[])
    maker = User(id=row.user_id, email="maker@example.com")

    class _Provider:
        def takes_negative_prompt(self, model):
            return world.negative_field

        async def generate_video(self, request, **_kwargs):
            world.asked.append(request)
            return VideoResult(data=b"MP4", job_id="op/1")

    class _Db:
        async def get(self, model, _key):
            return maker if model is User else row

        async def commit(self):
            return None

    @asynccontextmanager
    async def session():
        yield _Db()

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return _Provider(), model, SimpleNamespace(id=provider_id, provider="openai")

    async def mark(_kind, _job_id, **changes):
        for key, value in changes.items():
            setattr(row, key, value)

    async def store_path(_path, **_kwargs):
        return SimpleNamespace(id=uuid.uuid4())

    async def probe(_path):
        return SimpleNamespace(duration=8.0, width=1280, height=720, has_audio=False)

    async def extract_frame(_path, *, at, out_path, width):
        Path(out_path).write_bytes(b"JPG")

    async def content_check(_user_id, **asked):
        world.checked.append(asked)
        return content_drift.ContentCheck(True, avoided_seen=list(world.seen))

    monkeypatch.setattr(identity, "_service_identity", None)
    monkeypatch.setattr(runners, "async_session", session)
    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(jobs, "mark", mark)
    monkeypatch.setattr(assets, "store_path", store_path)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_frame", extract_frame)
    monkeypatch.setattr(content_drift, "check", content_check)
    return world


def test_a_model_without_a_negative_prompt_is_sent_the_look_alone(worker):
    assert asyncio.run(runners.run_clip(str(worker.row.id))).kind == "done"
    (asked,) = worker.asked
    assert asked.prompt == f"A crowded station at rush hour\n\n{STYLE}"
    assert asked.negative_prompt is None
    # The check reads the author's prompt and negative prompt, and the template's terms apart.
    assert worker.checked[0]["prompt"] == "A crowded station at rush hour"
    assert worker.checked[0]["negative_prompt"] is None
    assert worker.checked[0]["avoided"] == ["laser beams", "starfields"]
    assert worker.row.status == JOB_READY
    assert "Under the Brand Film art direction (its look)." in worker.row.params["notes"]


def test_a_model_with_a_negative_prompt_gets_the_avoided_terms_there(worker):
    worker.negative_field = True
    asyncio.run(runners.run_clip(str(worker.row.id)))
    (asked,) = worker.asked
    assert asked.prompt.endswith("cyan tones.") and asked.negative_prompt == "laser beams, starfields"
    assert "Under the Brand Film art direction (its look and what it avoids)." in worker.row.params["notes"]


def test_a_paid_shot_showing_what_its_template_avoids_is_kept_with_a_note(worker):
    worker.seen = ["laser beams"]
    asyncio.run(runners.run_clip(str(worker.row.id)))
    assert worker.row.status == JOB_READY
    note = next(n for n in worker.row.params["notes"] if "laser beams" in n)
    assert "which the Brand Film art direction avoids" in note and "kept" in note


# ── The frames ────────────────────────────────────────────────────────────


@pytest.fixture
def reader(monkeypatch):
    world = SimpleNamespace(stills=[], calls=[], seen=["laser beams"])

    class _Llm:
        async def chat(self, messages, model=None, temperature=None, max_tokens=None):
            files = messages[0].files or []
            world.calls.append(messages[0].content)
            if not files:
                return SimpleNamespace(content='{"subjects": []}')
            frames = {"first_frame": {"subjects": [], "excluded_seen": []}}
            frames["last_frame"] = {"subjects": [], "excluded_seen": list(world.seen)}
            return SimpleNamespace(content=json.dumps({"frames": frames}))

    @asynccontextmanager
    async def session():
        yield SimpleNamespace()

    async def get_llm(_db, _user_id):
        return _Llm(), "chat-model"

    async def extract_still(_video, *, at, out_path, max_side, crop_to=None):
        world.stills.append(at)
        Path(out_path).write_bytes(b"JPG")

    monkeypatch.setattr("app.core.database.async_session", session)
    monkeypatch.setattr("app.llm.router.get_llm_for_user", get_llm)
    monkeypatch.setattr(encoder, "extract_still", extract_still)
    return world


def _read(tmp_path, *, negative=None, avoided=None):
    return asyncio.run(
        content_drift.check(
            "u",
            prompt="A slow drift over a dark harbour",
            negative_prompt=negative,
            video_path="shot.mp4",
            duration=8.0,
            workdir=str(tmp_path),
            params={},
            avoided=avoided,
        )
    )


def test_what_the_template_avoids_is_read_on_an_abstract_shot_and_said_never_failed(reader, tmp_path):
    verdict = _read(tmp_path, avoided=["laser beams", "starfields"])
    # No subject named, yet its three frames are read for the template's terms.
    assert reader.stills == [0.0, 4.0, None] and "laser beams, starfields" in reader.calls[-1]
    assert verdict.findings == [] and verdict.avoided_seen == ["laser beams"]
    assert "Brand Film art direction avoids" in verdict.avoided_note("Brand Film")


def test_what_the_author_excludes_still_fails_the_shot(reader, tmp_path):
    verdict = _read(tmp_path, negative="laser beams", avoided=["laser beams", "starfields"])
    assert [finding.what for finding in verdict.findings] == ["shows laser beams, which the negative prompt excludes"]
    assert verdict.avoided_seen == []


def test_the_templates_terms_are_never_cut_by_a_long_negative_prompt(reader, tmp_path):
    authored = ", ".join(f"thing {i}" for i in range(30))
    _read(tmp_path, negative=authored, avoided=["laser beams", "starfields"])
    assert "laser beams, starfields" in reader.calls[-1]


def test_nothing_to_read_reads_no_frame(reader, tmp_path):
    verdict = _read(tmp_path)
    assert verdict.checked and reader.stills == []


# ── What a save says ──────────────────────────────────────────────────────


def test_a_save_names_only_the_shots_made_under_another_templates_direction():
    template = _template({"style": "Filmed.", "avoid": ["starfields"]})
    uses = [
        templates.ShotUse("s1", "c-1", template.id, "Brand Film"),
        templates.ShotUse("s3", "c-2", str(uuid.uuid4()), "Product Video"),
    ]
    (note,) = templates.shot_notes(template, uses, video_id="v-1")
    assert "s3 (`c-2`, Product Video)" in note and "s1" not in note
    assert "When one clashes" in note and "video_id='v-1'" in note


def test_a_video_within_its_direction_or_a_template_without_one_gets_no_note():
    template = _template({"style": "Filmed."})
    assert templates.shot_notes(template, [templates.ShotUse("s1", "c-1", template.id, "Brand Film")], video_id="v") == []
    other = [templates.ShotUse("s1", "c-1", "another", "Product Video")]
    assert templates.shot_notes(_template(), other, video_id="v") == []
    assert templates.shot_notes(None, other, video_id="v") == []


def test_only_shots_made_under_a_template_from_text_are_weighed():
    directed = VideoClip(id=uuid.uuid4(), params={clips.DIRECTION: {"template_id": "t-1", "template": "Brand Film"}})
    legacy = VideoClip(id=uuid.uuid4(), params={})
    free = VideoClip(id=uuid.uuid4(), params={clips.FREESTYLE: True})
    from_picture = VideoClip(
        id=uuid.uuid4(), params={clips.DIRECTION: {"template_id": "t-2"}, "reference_file_id": str(uuid.uuid4())}
    )
    rows = {clip.id: clip for clip in (directed, legacy, free, from_picture)}

    class _Db:
        async def get(self, _model, key):
            return rows.get(key)

    def clip_scene(scene_id, clip):
        return {"id": scene_id, "type": "clip", "clip_id": str(clip.id)}

    effective = {
        "scenes": [
            clip_scene("a", directed),
            {"id": "b", "type": "statement", "lines": ["Too much noise."], "backdrop": {"clip_id": str(directed.id)}},
            clip_scene("c", legacy),
            clip_scene("d", free),
            clip_scene("e", from_picture),
            {"id": "f", "type": "image", "file_id": str(uuid.uuid4()), "animate": {"clip_id": str(directed.id)}},
        ]
    }
    assert asyncio.run(projects.shot_uses(_Db(), effective)) == [
        templates.ShotUse("a", str(directed.id), "t-1", "Brand Film"),
        templates.ShotUse("b", str(directed.id), "t-1", "Brand Film"),
    ]


# ── The tool ──────────────────────────────────────────────────────────────


def test_the_assistant_names_the_video_a_shot_is_for(monkeypatch):
    from app.services.builtin_tools import video_studio as tool
    from app.tools.base import ToolCall

    clip = VideoClip(id=uuid.uuid4(), user_id=USER.id, model="veo", prompt="x", params={}, status=JOB_FAILED)
    asked: list[tuple[str, dict]] = []

    class _Db:
        async def get(self, _model, _key):
            return clip

        async def commit(self):
            return None

    @asynccontextmanager
    async def session():
        yield _Db()

    async def find_user(_db, _user_id):
        return USER

    async def plan(_db, _user, **settings):
        asked.append(("start", settings))
        return SimpleNamespace(label="Gemini", notes=[])

    async def queue(_db, _user, _plan, **_kwargs):
        return clip

    async def restart(_db, _user, _row, **changes):
        asked.append(("restart", changes))
        return SimpleNamespace(label="Gemini", notes=[])

    async def nothing(*_args, **_kwargs):
        return []

    monkeypatch.setattr(tool, "async_session", session)
    monkeypatch.setattr(tool, "_user", find_user)
    monkeypatch.setattr(clips, "plan_clip", plan)
    monkeypatch.setattr(clips, "queue_clip", queue)
    monkeypatch.setattr(clips, "restart_clip", restart)
    monkeypatch.setattr(clips, "check_restart", nothing)
    monkeypatch.setattr(jobs, "wake", nothing)
    monkeypatch.setattr(jobs, "wait_for", nothing)
    _definitions, executor = tool.build_video_studio_tools(None, str(USER.id))

    def call(arguments):
        asyncio.run(executor.execute_tool(ToolCall(id="c1", name="generate_video_clip", arguments=arguments)))

    call({"prompt": "A harbour at dawn", "video_id": "v-1"})
    call({"prompt": "A harbour at dawn", "template_id": "t-1"})
    call({"prompt": "A harbour at dawn", "freestyle": True})
    call({"clip_id": str(clip.id), "video_id": "v-2"})
    made_for = [(kind, (s["video_id"], s["template_id"], s["freestyle"])) for kind, s in asked]
    assert made_for == [
        ("start", ("v-1", None, False)),
        ("start", (None, "t-1", False)),
        ("start", (None, None, True)),
        ("restart", ("v-2", None, False)),
    ]
