"""Another model when a take fails its checks. Synthetic frames, prompts and model names only.

``generate_video_clip`` takes ``fallback_models``; the worker, when a made
take fails a check of its frames, generates the shot again in place with the
next one (``runners._fall_back`` through the real ``clips.restart_clip``).
What is proven, through ``run_clip`` from one generation to the next:

* a take failing its content check on the primary model is generated again
  with the fallback - the row never reads as failed in between - and the
  fallback's take is kept, the answer saying which model made it and why the
  primary's failed;
* every model failing leaves the shot failed with each model's reason;
* a ready shot generated again keeps playing its take through the whole
  chain, each failed attempt kept beside it;
* the fallbacks are checked before anything is paid (a model that cannot
  take the shot's reference images), a manual regeneration starts a new
  chain, and a fallback the shot can no longer take ends the chain with a
  note;
* the shot's own model named among them is dropped, the others kept and the
  answer saying so: ``[own, X]`` is one take on its own model, X its fallback,
  ``[own]`` one take and no fallback, while a new shot asking ``[]`` is
  refused - as a validation note (``status: invalid_parameters``), never a
  tool error the agent's repeated-failure withdrawal counts.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from app.llm.video import VideoResult
from app.models.user import User
from app.models.video_studio import JOB_FAILED, JOB_PENDING, JOB_READY, VideoClip
from app.services.video_studio import assets, clips, content_drift, encoder, frame_qc, jobs, runners, text_drift
from app.services.video_studio.jobs import serialize_clip

pytestmark = pytest.mark.usefixtures("no_default_video_template")

USER = SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-00000000f064"), role="user", email="maker@example.com")
START = "00000000-0000-0000-0000-00000000f065"
PROVIDERS = {"model-a": "00000000-0000-0000-0000-00000000f0a1", "model-b": "00000000-0000-0000-0000-00000000f0b2", "model-c": "00000000-0000-0000-0000-00000000f0c3"}


def _scene(shift: int = 0) -> Image.Image:
    picture = Image.new("RGB", (640, 360), (40, 80, 150))
    draw = ImageDraw.Draw(picture)
    for index in range(6):
        x = index * 97 + shift
        draw.ellipse([x, 60 + index * 30, x + 50, 110 + index * 30], fill=(230, 200 - index * 20, 60 + index * 30))
    return picture


def _jpeg(picture: Image.Image) -> bytes:
    out = io.BytesIO()
    picture.save(out, format="JPEG")
    return out.getvalue()


class _Provider:
    """A video model: it makes whatever take the test says, and says which model it was."""

    def __init__(self, world, model: str):
        self.world, self.model = world, model

    async def generate_video(self, request, **_kwargs):
        self.world.asked.append(request.model)
        return VideoResult(data=b"MP4-" + request.model.encode(), job_id=f"job-{request.model}")

    def max_reference_images(self, model):
        return 3 if model == "model-c" else 0

    def reference_images_with_start_frame(self, model):
        return model == "model-c"


@pytest.fixture
def chain(monkeypatch):
    """``run_clip`` and ``restart_clip`` on one shot row; *world.drifting* are the models whose takes drift."""
    row = VideoClip(
        id=uuid.uuid4(),
        user_id=USER.id,
        provider="provider-a",
        model="model-a",
        prompt="A fox crossing a snowy road",
        params={
            "provider_id": PROVIDERS["model-a"],
            "duration_seconds": 8,
            "reference_file_id": START,
            clips.FALLBACK_MODELS: [{"model": "model-b", "provider_id": PROVIDERS["model-b"], "provider": "provider-b"}],
        },
        status=JOB_PENDING,
    )
    world = SimpleNamespace(row=row, asked=[], drifting={"model-a"}, enqueued=[], stored={}, deleted=[], committed=0)

    class _Db:
        async def get(self, model, _key, **_kwargs):
            return USER if model is User else row

        async def commit(self):
            world.committed += 1

        async def flush(self):
            return None

        async def rollback(self):
            return None

        async def refresh(self, _row, with_for_update=False):
            return None

        def add(self, _row):
            return None

    @asynccontextmanager
    async def session():
        yield _Db()

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        chosen = model or row.model
        return _Provider(world, chosen), chosen, SimpleNamespace(id=PROVIDERS.get(chosen, provider_id), provider=f"provider-{chosen[-1]}")

    async def mark(_kind, _job_id, **changes):
        for key, value in changes.items():
            setattr(row, key, value)

    async def store_path(path, **kwargs):
        stored = SimpleNamespace(id=uuid.uuid4(), size=1, **kwargs)
        world.stored[str(stored.id)] = Path(path).read_bytes()
        return stored

    async def delete_files(ids):
        world.deleted.extend(str(i) for i in ids or [] if i)

    async def download_file(file_id, target):
        data = world.stored.get(str(file_id))
        if data is None:
            return None
        Path(target).write_bytes(data)
        return SimpleNamespace(content_type="video/mp4")

    async def probe(_path):
        return SimpleNamespace(duration=8.0, width=640, height=360, fps=24.0, has_audio=False)

    async def poster(_path, *, at, out_path, width):
        Path(out_path).write_bytes(_jpeg(_scene()))

    async def still(_video, *, at, out_path, max_side, crop_to=None):
        Path(out_path).write_bytes(_jpeg(_scene(shift=0 if at == 0.0 else 4)))

    async def start_frame(params, _workdir, **_kwargs):
        out = io.BytesIO()
        _scene().save(out, format="PNG")
        return clips.StartFrame(out.getvalue(), "image/png", file_id=START) if params.get("reference_file_id") else None

    async def nothing(*_args, **_kwargs):
        return []

    async def text_check(*_args, **_kwargs):
        return text_drift.TextCheck(True)

    async def content_check(*_args, **_kwargs):
        # The take the provider made last: a drifting model's shows another animal.
        if world.asked and world.asked[-1] in world.drifting:
            return content_drift.ContentCheck(True, [content_drift.Finding("fox", "middle_frame", "fox replaced by a dog")])
        return content_drift.ContentCheck(True)

    async def enqueue(_db, kind, clip, *, generation=0, owed_to=None, **_kwargs):
        world.enqueued.append((kind, generation, clip.model))

    async def no_cooling(_provider_id, *, model=None):
        return None

    async def no_limits(*_args, **_kwargs):
        return None

    async def woken():
        return None

    from app.llm import router
    from app.services.video_studio import provider_cooling

    monkeypatch.setattr(runners, "async_session", session)
    monkeypatch.setattr(runners, "bind_llm_caller", lambda *_a, **_k: asyncio.sleep(0))
    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(clips, "start_frame", start_frame)
    monkeypatch.setattr(clips, "reference_pictures", nothing)
    monkeypatch.setattr(jobs, "mark", mark)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    monkeypatch.setattr(jobs, "wake", woken)
    monkeypatch.setattr(assets, "store_path", store_path)
    monkeypatch.setattr(assets, "delete_files", delete_files)
    monkeypatch.setattr(assets, "download_file", download_file)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_frame", poster)
    monkeypatch.setattr(encoder, "extract_still", still)
    monkeypatch.setattr(text_drift, "check", text_check)
    monkeypatch.setattr(content_drift, "check", content_check)
    monkeypatch.setattr(provider_cooling, "cooling_of", no_cooling)
    monkeypatch.setattr(router, "declared_video_reference_limits", no_limits)
    world.session = session
    return world


def _run(world) -> runners.JobOutcome:
    return asyncio.run(runners.run_clip(str(world.row.id)))


def test_a_take_that_fails_its_checks_is_made_again_with_the_fallback_model(chain):
    first = _run(chain)
    row = chain.row
    # Generated again in place, never failed in between: queued on the fallback, the primary's failure recorded.
    assert first.kind == "done" and row.status == JOB_PENDING and row.error is None
    assert (row.model, row.params["provider_id"]) == ("model-b", PROVIDERS["model-b"])
    assert chain.enqueued == [("clip", 1, "model-b")]
    [tried] = row.params[clips.FALLBACK_TRIED]
    assert tried["model"] == "model-a" and tried["reason"].startswith("content drift: the shot does not show")
    assert row.provider_job_id is None and row.file_id is None
    assert row.stage == "Generating again with model-b: the take of model-a failed its checks"
    # The primary's take goes once the fallback's generation ends.
    failed_take = row.params[clips.REPLACED_MEDIA]
    assert failed_take and set(failed_take) <= set(chain.stored)

    _run(chain)
    assert chain.asked == ["model-a", "model-b"]
    assert row.status == JOB_READY and row.model == "model-b"
    assert set(failed_take) <= set(chain.deleted)
    state = serialize_clip(row)["fallback"]
    assert state["made_by"] == "model-b" and state["fell_back"] and [t["model"] for t in state["tried"]] == ["model-a"]
    assert clips.fallback_note(row).startswith("The take kept was made by model-b, a fallback model, after model-a: content drift")


def test_every_model_failing_leaves_the_shot_failed_with_each_reason(chain):
    chain.drifting = {"model-a", "model-b"}
    _run(chain)
    _run(chain)
    row = chain.row
    assert row.status == JOB_FAILED and row.error.startswith("content drift:")
    assert [t["model"] for t in row.params[clips.FALLBACK_TRIED]] == ["model-a", "model-b"]
    assert chain.enqueued == [("clip", 1, "model-b")] and serialize_clip(row)["fallback"]["made_by"] is None
    assert clips.fallback_note(row).startswith("Every model tried failed its checks: model-a: content drift")
    # Its last take stays on the row to look at, as any failed take's.
    assert row.file_id is not None and str(row.file_id) not in chain.deleted


def test_a_ready_shot_keeps_its_take_through_the_whole_chain(chain):
    chain.drifting = set()
    _run(chain)
    row = chain.row
    assert row.status == JOB_READY and row.model == "model-a"
    playing = str(row.file_id)

    async def again():
        async with chain.session() as db:
            await clips.restart_clip(db, USER, row, prompt="A fox crossing a snowy road at dusk", fallback_models=["model-b"])

    asyncio.run(again())
    assert clips.keeps_take(row.params) and str(row.file_id) == playing
    chain.drifting = {"model-a"}
    _run(chain)
    # Generated again with the fallback, the previous take still the one that plays.
    assert row.status == JOB_PENDING and row.model == "model-b" and str(row.file_id) == playing and clips.playable(row)
    assert [a["status"] for a in row.params[clips.ATTEMPTS]] == [JOB_FAILED]
    _run(chain)
    assert row.status == JOB_READY and row.model == "model-b" and str(row.file_id) != playing
    # The replaced take is kept as a previous take (restore_take), not deleted (#10901).
    assert playing not in chain.deleted and not clips.keeps_take(row.params)
    assert [str(t["file_id"]) for t in clips.previous_takes(row.params)] == [playing]


def test_a_regeneration_failing_the_insert_lint_keeps_the_previous_take_playing(chain, monkeypatch):
    """#10901 S2: a take made again in place is held to the pre-render lint of the videos drawing a screen insert
    on the shot before it replaces the take that plays; failing it, it is a failed attempt naming the findings."""
    chain.drifting = set()
    _run(chain)
    row = chain.row
    playing = str(row.file_id)
    linted: list[tuple[str, str]] = []

    async def findings(_user_id, clip_id, file_id):
        linted.append((clip_id, file_id))
        return ["video `Booth`, scene s1: its screen insert 0's quad is 3.5% of the frame's width off the bezel"]

    monkeypatch.setattr(runners, "_insert_promotion_findings", findings)

    async def again():
        async with chain.session() as db:
            await clips.restart_clip(
                db, USER, row, prompt="A fox crossing a snowy road at dusk", fallback_models=["model-b"]
            )

    asyncio.run(again())
    _run(chain)
    _run(chain)
    # Both models' takes were linted on their own file, never on the take that plays; each failed on it.
    assert [clip for clip, _file in linted] == [str(row.id), str(row.id)] and playing not in {f for _c, f in linted}
    assert row.status == JOB_READY and str(row.file_id) == playing and clips.playable(row)
    failed = clips.failed_regeneration(row)
    assert failed is not None and failed["reason"].startswith("screen insert: video `Booth`, scene s1")


def test_a_first_take_is_never_held_to_the_insert_lint(chain, monkeypatch):
    """No take plays yet: nothing to keep, the lint before a promotion is not read."""
    chain.drifting = set()

    async def findings(*_args):
        pytest.fail("a first take is not a promotion")

    monkeypatch.setattr(runners, "_insert_promotion_findings", findings)
    _run(chain)
    assert chain.row.status == JOB_READY


def test_the_fallbacks_are_checked_before_anything_is_paid(chain):
    params = {"reference_file_id": START, clips.REFERENCE_IMAGES: ["00000000-0000-0000-0000-00000000f066"]}

    async def checked(raw, model="model-a", shot=None, again=False):
        async with chain.session() as db:
            return await clips.resolve_fallbacks(
                db, USER, raw, model=model, params=shot or {"reference_file_id": START}, again=again
            )

    assert asyncio.run(checked(["model-b", "model-b", "model-c"])) == (
        [
            {"model": "model-b", "provider_id": PROVIDERS["model-b"], "provider": "provider-b"},
            {"model": "model-c", "provider_id": PROVIDERS["model-c"], "provider": "provider-c"},
        ],
        [],
    )
    # A shot with reference images falls back only to a model that takes them beside its start frame.
    with pytest.raises(clips.ClipError, match=r"^fallback_models: model-b takes no reference images") as refused:
        asyncio.run(checked(["model-b"], shot=params))
    assert refused.value.about_model
    assert asyncio.run(checked(["model-c"], shot=params))[0][0]["model"] == "model-c"
    with pytest.raises(clips.ClipError, match="at most 3 other models"):
        clips.parse_fallback_models(["m1", "m2", "m3", "m4"])
    with pytest.raises(clips.ClipError, match="a list of model names"):
        clips.parse_fallback_models(["m1", 7])
    # The shot's own model is no fallback: never counted against the three.
    assert clips.parse_fallback_models(["model-a", "m1", "m2", "m3"], own="model-a") == ["model-a", "m1", "m2", "m3"]
    # A list naming no model asks a fallback of none: refused on a new shot, while a regeneration's [] drops them.
    with pytest.raises(clips.ClipError, match="fallback_models names no model"):
        asyncio.run(checked([]))
    assert asyncio.run(checked([], again=True)) == ([], [])
    assert asyncio.run(checked(None)) == ([], [])


def _planned(chain, **asked) -> clips.ClipPlan:
    """A new shot on model-a as ``generate_video_clip`` plans it, no start frame, queued on the chain's row."""
    plan = asyncio.run(clips.plan_clip(None, USER, prompt="A fox crossing a snowy road", model="model-a", freestyle=True, **asked))
    chain.row.params = dict(plan.params)
    chain.row.model, chain.row.status = plan.request.model, JOB_PENDING
    return plan


def test_the_shots_own_model_among_its_fallbacks_is_dropped_and_the_others_kept(chain):
    chain.drifting = set()
    plan = _planned(chain, fallback_models=["model-a", "model-b"])
    assert [item["model"] for item in plan.params[clips.FALLBACK_MODELS]] == ["model-b"]
    assert plan.summary()["fallback_models"] == ["model-b"]
    [note] = [note for note in plan.notes if note.startswith("fallback_models:")]
    assert note == (
        "fallback_models: model-a is the shot's own model, dropped (its first take is made with it); fallback model-b."
    )
    # One take, on its own model: it passed, so the fallback is never asked.
    _run(chain)
    assert chain.asked == ["model-a"] and chain.row.status == JOB_READY and chain.row.model == "model-a"


def test_the_shots_own_model_alone_leaves_no_fallback_and_one_take(chain):
    plan = _planned(chain, fallback_models=["model-a"])
    assert clips.FALLBACK_MODELS not in plan.params
    assert any(note.endswith("no fallback: a take failing a check of its frames fails the shot.") for note in plan.notes)
    # Its one take drifts: the shot fails, no other model asked.
    _run(chain)
    assert chain.asked == ["model-a"] and chain.row.status == JOB_FAILED and chain.enqueued == []
    # Generated again naming only its own model: the fallbacks it had are dropped, and said.
    chain.row.params[clips.FALLBACK_MODELS] = [{"model": "model-b", "provider_id": PROVIDERS["model-b"], "provider": "provider-b"}]

    async def again():
        async with chain.session() as db:
            return await clips.restart_clip(db, USER, chain.row, fallback_models=["model-a"])

    plan = asyncio.run(again())
    assert clips.FALLBACK_MODELS not in chain.row.params and any("no fallback" in note for note in plan.notes)


def test_a_new_shot_asking_a_fallback_of_no_model_is_refused_before_anything_is_paid(chain):
    with pytest.raises(clips.ClipError, match="fallback_models names no model"):
        _planned(chain, fallback_models=[])
    assert chain.asked == [] and chain.enqueued == []


def _tool(chain, monkeypatch):
    """``generate_video_clip`` as the assistant calls it on the chain's session: ``call(**arguments) -> answer``."""
    from app.services.builtin_tools import video_studio as tool
    from app.tools.base import ToolCall

    async def find_user(_db, _user_id):
        return USER

    async def queued_only(_kind, rows, **_kwargs):
        return [chain.queued]

    async def enqueue(_db, kind, clip, **_kwargs):
        chain.queued = clip
        chain.enqueued.append((kind, 0, clip.model))

    async def generating(_kind, _row):
        return SimpleNamespace(status="generating", held=None, reason=None)

    monkeypatch.setattr(tool, "async_session", chain.session)
    monkeypatch.setattr(tool, "_user", find_user)
    monkeypatch.setattr(tool, "_state_of", generating)
    monkeypatch.setattr(jobs, "wait_for", queued_only)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    monkeypatch.setattr(jobs, "describe", lambda _state: "generating")
    _definitions, executor = tool.build_video_studio_tools(None, str(USER.id))
    return lambda **arguments: asyncio.run(
        executor.execute_tool(ToolCall(id="c1", name="generate_video_clip", arguments=arguments))
    )


def test_a_call_refused_on_its_arguments_is_a_validation_note_never_a_tool_error(chain, monkeypatch):
    import json

    call = _tool(chain, monkeypatch)
    answer = call(prompt="A fox crossing a snowy road", model="model-a", fallback_models=[])
    head = json.loads(answer.content.splitlines()[0])
    # Nothing reached a provider: no tool error, so nothing the loop's repeated-failure withdrawal counts.
    assert not answer.is_error and not answer.outcome_failure and answer.no_op
    assert head["status"] == "invalid_parameters" and head["clip_id"] is None and head["queued"] is False
    assert head["reason"].startswith("fallback_models names no model")
    assert "nothing was queued or paid" in answer.content and chain.enqueued == []
    # Every refusal of the call's arguments answers so, a shot to generate again naming none of the person's too.
    answer = call(prompt="A fox", start_frame=False)
    assert not answer.is_error and json.loads(answer.content.splitlines()[0])["status"] == "invalid_parameters"
    answer = call(prompt="")
    assert not answer.is_error and "needs a prompt" in answer.content


def test_a_new_shots_answer_says_the_own_model_it_dropped_from_its_fallbacks(chain, monkeypatch):
    call = _tool(chain, monkeypatch)
    answer = call(prompt="A fox crossing a snowy road", model="model-a", fallback_models=["model-a", "model-b"])
    assert not answer.is_error and chain.enqueued == [("clip", 0, "model-a")]
    assert [item["model"] for item in chain.queued.params[clips.FALLBACK_MODELS]] == ["model-b"]
    assert "model-a is the shot's own model, dropped (its first take is made with it); fallback model-b." in answer.content


def test_a_regeneration_asked_by_hand_starts_a_new_chain_of_the_fallbacks_it_names_alone(chain):
    _run(chain)
    row = chain.row
    assert row.params[clips.FALLBACK_TRIED]
    _run(chain)
    assert row.status == JOB_READY

    async def again(**changes):
        async with chain.session() as db:
            return await clips.restart_clip(db, USER, row, **changes)

    # One model per round: the chain an earlier call named is never this call's.
    plan = asyncio.run(again(model="model-a"))
    assert clips.FALLBACK_TRIED not in row.params and clips.FALLBACK_MODELS not in row.params
    assert plan.summary()["fallback_models"] == [] and row.model == "model-a"
    row.status = JOB_READY
    plan = asyncio.run(again(fallback_models=["model-b"]))
    assert plan.summary()["fallback_models"] == ["model-b"]
    row.status = JOB_READY
    plan = asyncio.run(again(fallback_models=[]))
    assert clips.FALLBACK_MODELS not in row.params and plan.summary()["fallback_models"] == []


def test_a_fallback_the_shot_can_no_longer_take_ends_the_chain_with_a_note(chain):
    # Asked since with reference images: the fallback, checked again for the shot as it is, refuses it.
    chain.row.params[clips.REFERENCE_IMAGES] = ["00000000-0000-0000-0000-00000000f066"]
    chain.row.params[clips.FALLBACK_MODELS] = [{"model": "model-b", "provider_id": PROVIDERS["model-b"], "provider": "provider-b"}]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(clips, "resolve_reference_images", _refusing)
        _run(chain)
    row = chain.row
    assert row.status == JOB_FAILED and row.error.startswith("content drift:")
    assert any(note.startswith("Not generated again with model-b: model-b takes no reference images") for note in row.params["notes"])
    assert [t["model"] for t in row.params[clips.FALLBACK_TRIED]] == ["model-a"] and chain.enqueued == []


async def _refusing(_db, _user, ids, *, provider, model, start_frame, again=False, **_fit):
    clips.check_reference_combination(
        {"reference_images": provider.max_reference_images(model), "reference_images_with_start_frame": False},
        model,
        references=len(clips._reference_ids(ids)),
        start_frame=start_frame,
        again=again,
    )
    return list(ids)


def test_the_answer_names_the_model_and_the_fallbacks():
    row = VideoClip(
        id=uuid.uuid4(),
        provider="provider-b",
        model="model-b",
        prompt="x",
        status=JOB_READY,
        params={
            clips.FALLBACK_MODELS: [{"model": "model-b"}],
            clips.FALLBACK_TRIED: [{"model": "model-a", "provider": "provider-a", "reason": "camera moved: ..."}],
            frame_qc.QC_KEY: {"motion_ssim": 0.9},
        },
    )
    served = serialize_clip(row)
    assert served["fallback"] == {
        "models": ["model-b"],
        "tried": [{"model": "model-a", "provider": "provider-a", "reason": "camera moved: ..."}],
        "made_by": "model-b",
        "fell_back": True,
    }
    assert serialize_clip(VideoClip(id=uuid.uuid4(), provider="p", model="m", prompt="x", status=JOB_READY, params={}))["fallback"] is None
