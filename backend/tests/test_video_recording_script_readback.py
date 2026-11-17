"""A recording keeps the script it was submitted with, get_video_recording reads it back, a re-record copies it.

``record_browser_demo`` stores its steps and parameters exactly as given (``VideoTake.submitted_script``) once the
script passed its validation and the credential preflight; ``get_video_recording`` answers them under ``script``
(``include_script: false`` leaves them out); ``record_browser_demo(from_recording_id=...)`` records a new take from
them, every argument given overriding what it copies; a take recorded before answers ``script: null``.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

from app.models.video_studio import JOB_READY, VideoTake
from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import jobs, projects, take_script
from app.tools.base import ToolCall

OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")
SOURCE = uuid.UUID("8a3f2b1c-9d00-4000-8000-000000000001")
OLD = uuid.UUID("8a3f2b1c-9d00-4000-8000-000000000002")
STEPS = [
    {"action": "goto", "url": "https://app.example.com/graph"},
    {"action": "click", "role": "button", "name": "Layout", "label": "Open the layout menu"},
    {"action": "wait", "seconds": 1.5, "until_settled": True},
]


def _async(value):
    async def returns(*_args, **_kwargs):
        return value

    return returns


class _Session:
    def __init__(self, takes: list[VideoTake]):
        self.takes = takes
        self.added: list = []

    async def get(self, model, key):
        if model is VideoTake:
            return next((t for t in self.takes if str(t.id) == str(key)), None)
        return SimpleNamespace(id=OWNER, role="member")

    async def execute(self, statement):
        # The id prefix query only (``tool.take_prefix_query``): the user's takes whose id starts with the prefix.
        params = statement.compile().params
        prefix = next(v[:-1] for v in params.values() if isinstance(v, str) and v.endswith("%"))
        rows = [t for t in self.takes if str(t.id).startswith(prefix)][: statement._limit]
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        for row in self.added:
            if getattr(row, "id", None) is None:
                row.id = uuid.uuid4()

    async def commit(self):
        return None


def _take(take_id: uuid.UUID, *, submitted: dict | None, script: dict | None = None) -> VideoTake:
    return VideoTake(
        id=take_id,
        user_id=OWNER,
        name="Graph tour",
        status=JOB_READY,
        duration_ms=4000,
        script=script or {"steps": STEPS, "viewport": {"width": 1600, "height": 900}, "pixel_ratio": 2, "fps": 30},
        submitted_script=submitted,
        events=[],
    )


def _executor(monkeypatch, session: _Session, recorded: list[dict]):
    @asynccontextmanager
    async def opened():
        yield session

    monkeypatch.setattr(tool, "async_session", opened)
    monkeypatch.setattr(projects, "can_read_project_asset", _async(True))
    monkeypatch.setattr(jobs, "mark_told", _async(None))

    real_new_take = jobs.new_take

    def new_take(**kwargs):
        recorded.append(kwargs)
        return real_new_take(**kwargs)

    monkeypatch.setattr(jobs, "new_take", new_take)
    # No earlier take to answer: the same-take lookup (covered by its own tests) never reaches this fake session.
    monkeypatch.setattr(tool, "same_take", _async(None))
    monkeypatch.setattr(jobs, "enqueue", _async(None))
    monkeypatch.setattr(jobs, "wake", _async(None))
    monkeypatch.setattr(jobs, "wait_for", _async([]))
    return tool.build_video_studio_tools(None, str(OWNER))[1]


async def _call(executor, name: str, **arguments):
    return await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))


def _script_block(content: str) -> dict:
    return json.loads(content.rsplit("\n", 1)[1])["script"]


# ── Storage and readback ──────────────────────────────────────────────────


def test_submitted_keeps_the_steps_as_given_and_only_the_parameters_given():
    kept = take_script.submitted(STEPS, {"start_url": "https://app.example.com", "pixel_ratio": 1.5, "fps": None})
    assert kept == {"steps": STEPS, "params": {"start_url": "https://app.example.com", "pixel_ratio": 1.5}}
    assert take_script.readback(kept) == kept


def test_a_take_recorded_before_reads_back_none():
    assert take_script.readback(None) is None
    assert take_script.readback({"params": {}}) is None


async def test_record_browser_demo_stores_the_submission_and_get_video_recording_returns_it(monkeypatch):
    session = _Session([])
    recorded: list[dict] = []
    executor = _executor(monkeypatch, session, recorded)
    await _call(
        executor, "record_browser_demo", steps=STEPS, start_url="https://app.example.com", pixel_ratio=1.5, fps=60
    )
    stored = recorded[0]["submitted_script"]
    assert stored == {"steps": STEPS, "params": {"start_url": "https://app.example.com", "pixel_ratio": 1.5, "fps": 60}}
    # The validated script still carries its defaults; the submission does not.
    assert recorded[0]["script"]["settle_ms"] == 600 and "settle_ms" not in stored["params"]

    take = session.added[0]
    take.status, take.duration_ms, take.events = JOB_READY, 4000, []
    session.takes.append(take)
    answer = await _call(executor, "get_video_recording", recording_id=str(take.id), include_contact_sheet=False)
    assert not answer.is_error
    block = _script_block(answer.content)
    assert block["steps"] == STEPS and block["params"]["pixel_ratio"] == 1.5
    short = await _call(
        executor, "get_video_recording", recording_id=str(take.id), include_contact_sheet=False, include_script=False
    )
    assert "Script" not in short.content


async def test_a_take_recorded_before_answers_script_null_without_error(monkeypatch):
    session = _Session([_take(OLD, submitted=None)])
    executor = _executor(monkeypatch, session, [])
    answer = await _call(executor, "get_video_recording", recording_id=str(OLD), include_contact_sheet=False)
    assert not answer.is_error
    assert "Script: null (recorded before the submitted script was kept" in answer.content


async def test_window_adds_the_raw_capture_records_and_a_bad_window_is_refused(monkeypatch):
    take = _take(OLD, submitted=None)
    take.capture = {
        "alone": {"runs": [{"t": [30.9, 31.5], "step": 15, "composited": False, "why": "copy unlike paint"}]}
    }
    executor = _executor(monkeypatch, _Session([take]), [])
    answer = await _call(
        executor,
        "get_video_recording",
        recording_id=str(OLD),
        include_contact_sheet=False,
        include_script=False,
        window="30.7-31.7",
    )
    assert not answer.is_error
    head, block = answer.content.rsplit("\n", 1)
    assert head.endswith("Capture records over 30.7-31.7 s:")
    assert json.loads(block)["alone_runs"][0]["why"] == "copy unlike paint"
    refused = await _call(executor, "get_video_recording", recording_id=str(OLD), window="later")
    assert refused.is_error and "not START-END" in refused.content


async def test_a_refused_script_is_never_stored(monkeypatch):
    session = _Session([])
    recorded: list[dict] = []
    executor = _executor(monkeypatch, session, recorded)
    typed = [{"action": "type", "selector": "input[type=password]", "text": "hunter2"}]
    answer = await _call(executor, "record_browser_demo", steps=typed, start_url="https://app.example.com")
    assert answer.is_error and recorded == []


# ── Override merge ────────────────────────────────────────────────────────


def test_merged_copies_the_source_and_every_argument_given_overrides_it():
    source = {"steps": STEPS, "params": {"start_url": "https://a", "pixel_ratio": 2, "fps": 60, "hide": ["#banner"]}}
    steps, params = take_script.merged(source, steps=None, params={"pixel_ratio": 1.5, "fps": None})
    assert steps == STEPS
    assert params == {"start_url": "https://a", "pixel_ratio": 1.5, "fps": 60, "hide": ["#banner"]}
    own = [{"action": "wait", "seconds": 1}]
    assert take_script.merged(source, steps=own, params={})[0] == own


def test_an_explicit_size_drops_the_copied_render_sizing():
    source = {"steps": STEPS, "params": {"target_render": "ultra", "video_id": "v", "scene": "s1", "insert": 0}}
    _steps, params = take_script.merged(source, steps=None, params={"viewport": {"width": 1920, "height": 1080}})
    assert params == {"viewport": {"width": 1920, "height": 1080}}
    _steps, kept = take_script.merged(source, steps=None, params={"fps": 30})
    assert kept["target_render"] == "ultra" and kept["fps"] == 30


def test_a_take_recorded_before_is_copied_from_its_validated_script():
    copied = take_script.source_script(None, {"steps": STEPS, "pixel_ratio": 2, "settle_ms": 600}, OWNER)
    assert copied == {"steps": STEPS, "params": {"pixel_ratio": 2, "settle_ms": 600, "profile_id": str(OWNER)}}


async def test_record_browser_demo_from_recording_id_records_the_same_steps_at_the_overridden_size(monkeypatch):
    submitted = take_script.submitted(STEPS, {"start_url": "https://app.example.com", "pixel_ratio": 2, "fps": 60})
    source = _take(SOURCE, submitted=submitted)
    session = _Session([source])
    recorded: list[dict] = []
    executor = _executor(monkeypatch, session, recorded)
    await _call(
        executor,
        "record_browser_demo",
        from_recording_id=str(SOURCE),
        viewport={"width": 1920, "height": 1080},
        pixel_ratio=1.5,
    )
    new = recorded[0]
    assert [s["action"] for s in new["script"]["steps"]] == ["goto", "click", "wait"]
    assert new["script"]["steps"][1]["label"] == "Open the layout menu"
    assert new["script"]["viewport"] == {"width": 1920, "height": 1080} and new["script"]["pixel_ratio"] == 1.5
    assert new["script"]["fps"] == 60 and new["name"] == "Graph tour (re-recorded)"
    assert new["submitted_script"]["steps"] == STEPS
    # The take copied is untouched.
    assert source.submitted_script == submitted


async def test_from_recording_id_on_a_take_the_user_cannot_read_is_refused(monkeypatch):
    session = _Session([])
    executor = _executor(monkeypatch, session, [])
    answer = await _call(executor, "record_browser_demo", from_recording_id=str(SOURCE))
    assert answer.is_error and "you can read to record again" in answer.content
    missing = await _call(executor, "record_browser_demo", start_url="https://app.example.com")
    assert missing.is_error and "from_recording_id" in missing.content


# ── from_recording_id by id prefix (#9264) ────────────────────────────────


async def test_from_recording_id_resolves_a_unique_id_prefix(monkeypatch):
    other = uuid.UUID("c0dee81e-0000-4000-8000-000000000009")
    submitted = take_script.submitted(STEPS, {"start_url": "https://app.example.com"})
    session = _Session([_take(other, submitted=submitted)])
    recorded: list[dict] = []
    executor = _executor(monkeypatch, session, recorded)
    answer = await _call(executor, "record_browser_demo", from_recording_id="C0DEE81E")
    assert "you can read to record again" not in (answer.content or "")
    assert len(recorded) == 1 and recorded[0]["submitted_script"]["steps"] == STEPS


async def test_from_recording_id_refuses_an_ambiguous_prefix_naming_the_candidates(monkeypatch):
    session = _Session([_take(SOURCE, submitted=None), _take(OLD, submitted=None)])
    recorded: list[dict] = []
    executor = _executor(monkeypatch, session, recorded)
    answer = await _call(executor, "record_browser_demo", from_recording_id="8a3f2b1c")
    assert answer.is_error and "is ambiguous" in answer.content
    assert str(SOURCE) in answer.content and str(OLD) in answer.content
    assert recorded == []


async def test_from_recording_id_with_a_full_id_never_runs_the_prefix_query(monkeypatch):
    submitted = take_script.submitted(STEPS, {"start_url": "https://app.example.com"})
    session = _Session([_take(SOURCE, submitted=None), _take(OLD, submitted=submitted)])

    async def no_query(_statement):
        raise AssertionError("a full id is read as given")

    session.execute = no_query
    recorded: list[dict] = []
    executor = _executor(monkeypatch, session, recorded)
    await _call(executor, "record_browser_demo", from_recording_id=str(OLD))
    assert len(recorded) == 1


async def test_a_prefix_that_starts_no_recording_is_refused_as_before(monkeypatch):
    executor = _executor(monkeypatch, _Session([_take(SOURCE, submitted=None)]), [])
    answer = await _call(executor, "record_browser_demo", from_recording_id="deadbeef")
    assert answer.is_error and "you can read to record again" in answer.content
