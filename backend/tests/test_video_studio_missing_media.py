"""A medium a video uses, deleted: the video keeps working, and the deletion asks first.

Deleting a recording a video still used made the editor's preview answer 422
(``no recording ... you can use``): the whole video stayed blank, and every
save was refused the same way. Now:

* a medium the stored video already names that was deleted since is no
  problem for ``verify_references`` (preview, save, duplicate): the scene that
  plays it says it is missing, the rest plays; a NEW reference to an id that
  names nothing, or to someone else's medium, is still refused;
* ``DELETE /takes|clips|music/{id}`` refuses a medium a video uses with 409
  ``media_in_use`` (the videos the caller can open, how many others) unless
  ``force=true``, and a forced deletion is audited.
"""

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import helpers
from app.api.v1 import video_studio as video_studio_api
from app.core.database import get_db
from app.models.presentation import Presentation
from app.models.video_studio import JOB_PENDING, JOB_READY, VideoClip, VideoMusic, VideoProject, VideoTake
from app.services.video_studio import library, preview, projects, templates
from app.services.video_studio.spec import VideoSpecError

GONE_TAKE = "00000000-0000-0000-0000-00000000a001"
PENDING_TAKE = "00000000-0000-0000-0000-00000000a002"
EXPIRED_TAKE = "00000000-0000-0000-0000-00000000a003"
GONE_CLIP = "00000000-0000-0000-0000-00000000b001"
GONE_MUSIC = "00000000-0000-0000-0000-00000000c001"
GONE_FILE = "00000000-0000-0000-0000-00000000d001"
GONE_DECK = "00000000-0000-0000-0000-00000000e001"
NEW_TAKE = "00000000-0000-0000-0000-00000000f001"


def _user():
    return SimpleNamespace(id=uuid.uuid4(), role="user", is_admin=False, email="user@example.com", is_active=True)


class _Result:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(first=lambda: self._value, all=lambda: [])


class _DB:
    """``get`` answers the scripted rows; every query finds nothing (or *count*)."""

    def __init__(self, rows=None, count=None):
        self._rows = rows or {}
        self._count = count

    async def get(self, model, key):
        return self._rows.get((model, str(key)))

    async def execute(self, _statement):
        return _Result(self._count)


@pytest.fixture(autouse=True)
def _no_groups(monkeypatch):
    async def none(*_args, **_kwargs):
        return []

    monkeypatch.setattr(helpers, "get_user_group_ids", none)


def _gone_everywhere() -> dict:
    return {
        "title": "Brand film",
        "music": {"music_id": GONE_MUSIC},
        "scenes": [
            {"id": "flow", "type": "recording", "take_id": GONE_TAKE},
            {"id": "shot", "type": "clip", "clip_id": GONE_CLIP},
            {"id": "pic", "type": "image", "file_id": GONE_FILE},
            {"id": "deck", "type": "slide", "presentation_id": GONE_DECK, "slide": 2},
        ],
    }


def _problems(spec: dict, *, held: dict | None, rows=None) -> list[str]:
    found = asyncio.run(projects.verify_references(_DB(rows), spec, _user(), held=held))
    return sorted(problem.path for problem in found)


# ── verify_references ─────────────────────────────────────────────────────


def test_media_the_video_already_names_and_were_deleted_since_are_no_problem():
    spec = _gone_everywhere()
    assert _problems(spec, held=spec) == []


def test_without_the_stored_spec_a_reference_to_nothing_is_refused_as_before():
    assert _problems(_gone_everywhere(), held=None) == [
        "clip_id",
        "file_id",
        "music.music_id",
        "presentation_id",
        "take_id",
    ]


def test_a_new_reference_to_an_id_that_names_nothing_is_still_refused():
    held = _gone_everywhere()
    draft = {**held, "scenes": [*held["scenes"], {"id": "new", "type": "recording", "take_id": NEW_TAKE}]}
    found = asyncio.run(projects.verify_references(_DB(), draft, _user(), held=held))
    assert [(problem.path, problem.message) for problem in found] == [
        ("take_id", f"no recording {NEW_TAKE} you can use")
    ]


def test_a_held_medium_that_still_exists_is_judged_as_before():
    """Only a deleted one is let through: someone else's recording stays refused to who may not use it."""
    theirs = VideoTake(id=uuid.UUID(GONE_TAKE), user_id=uuid.uuid4(), name="Theirs", status=JOB_READY, script={})
    spec = {"title": "T", "scenes": [{"id": "flow", "type": "recording", "take_id": GONE_TAKE}]}
    assert _problems(spec, held=spec, rows={(VideoTake, GONE_TAKE): theirs}) == ["take_id"]


def test_a_held_deck_that_exists_but_cannot_be_opened_is_still_refused():
    deck = Presentation(id=uuid.UUID(GONE_DECK))
    spec = {"title": "T", "scenes": [{"id": "deck", "type": "slide", "presentation_id": GONE_DECK}]}
    assert _problems(spec, held=spec, rows={(Presentation, GONE_DECK): deck}) == ["presentation_id"]


def test_a_save_reads_the_stored_spec_as_what_the_video_already_names(monkeypatch):
    seen: dict = {}

    async def capture(_db, _spec, _user, *, held=None):
        seen["held"] = held
        raise VideoSpecError([])

    monkeypatch.setattr(projects, "verify_references", capture)
    stored = _gone_everywhere()
    project = VideoProject(id=uuid.uuid4(), user_id=uuid.uuid4(), title="Brand film", spec=stored, revision=3)
    with pytest.raises(VideoSpecError):
        asyncio.run(projects.save_spec(_DB(), user=_user(), raw=stored, project=project))
    assert seen["held"] == stored
    with pytest.raises(VideoSpecError):
        asyncio.run(projects.save_spec(_DB(), user=_user(), raw=stored, held=stored))
    assert seen["held"] == stored


# ── The preview ───────────────────────────────────────────────────────────


@pytest.fixture
def _plain_preview(monkeypatch):
    async def no_template(*_args, **_kwargs):
        return None

    async def plain_theme(*_args, **_kwargs):
        return {}, {}, [], {}

    monkeypatch.setattr(templates, "resolve_for_spec", no_template)
    monkeypatch.setattr(preview, "_theme", plain_theme)


def _project(spec: dict) -> VideoProject:
    return VideoProject(id=uuid.uuid4(), user_id=uuid.uuid4(), title=spec["title"], spec=spec, revision=3)


@pytest.mark.usefixtures("_plain_preview")
def test_a_video_whose_recording_was_deleted_still_previews_and_names_the_scene_missing():
    user = _user()
    pending = VideoTake(id=uuid.UUID(PENDING_TAKE), user_id=user.id, name="Next", status=JOB_PENDING, script={})
    expired = VideoTake(
        id=uuid.UUID(EXPIRED_TAKE),
        user_id=user.id,
        name="Old",
        status=JOB_READY,
        script={},
        expired_at=datetime.now(UTC),
    )
    draft = {
        "title": "Brand film",
        "freestyle": True,
        "scenes": [
            {"id": "gone", "type": "recording", "take_id": GONE_TAKE},
            {"id": "shot", "type": "clip", "clip_id": GONE_CLIP},
            {"id": "next", "type": "recording", "take_id": PENDING_TAKE},
            {"id": "old", "type": "recording", "take_id": EXPIRED_TAKE},
        ],
    }
    db = _DB({(VideoTake, PENDING_TAKE): pending, (VideoTake, EXPIRED_TAKE): expired})
    result = asyncio.run(preview.build_preview(db, _project(draft), user, draft))

    scenes = {scene["id"]: scene for scene in result["document"]["scenes"]}
    assert list(scenes) == ["gone", "shot", "next", "old"]
    assert scenes["gone"]["preview_state"] == {
        "state": "failed",
        "kind": "recording",
        "missing": True,
        "error": "The recording no longer exists.",
    }
    assert scenes["shot"]["preview_state"]["missing"] is True and scenes["shot"]["preview_state"]["kind"] == "clip"
    # Still being made: polled until it plays. Expired: said, never "recording" forever.
    assert scenes["next"]["preview_state"] == {"state": "recording"}
    assert scenes["old"]["preview_state"] == {
        "state": "failed",
        "kind": "recording",
        "error": "Its media expired with the retention.",
    }
    assert result["duration"] > 0


@pytest.mark.usefixtures("_plain_preview")
def test_a_draft_adding_a_recording_that_names_nothing_is_still_refused():
    stored = {"title": "Brand film", "freestyle": True, "scenes": [{"id": "a", "type": "statement", "lines": ["Hi"]}]}
    draft = {**stored, "scenes": [*stored["scenes"], {"id": "new", "type": "recording", "take_id": NEW_TAKE}]}
    with pytest.raises(VideoSpecError, match="no recording"):
        asyncio.run(preview.build_preview(_DB(), _project(stored), _user(), draft))


# ── Deleting a medium a video uses ────────────────────────────────────────


def test_the_refusal_names_the_videos_the_reader_can_open_and_counts_the_others(monkeypatch):
    take_id = uuid.uuid4()

    async def visible(_db, _user, _groups, media):
        assert media == [("take", take_id)]
        return {str(take_id): [{"id": "v-1", "title": "My film"}]}

    monkeypatch.setattr(library, "used_in", visible)
    refusal = asyncio.run(library.deletion_refusal(_DB(count=3), _user(), "take", take_id))
    assert refusal is not None
    assert refusal["code"] == "media_in_use"
    assert refusal["videos"] == [{"id": "v-1", "title": "My film"}] and refusal["others"] == 2
    assert "recording is used in 3 videos" in refusal["message"]
    assert asyncio.run(library.deletion_refusal(_DB(count=0), _user(), "take", take_id)) is None


class _WriteDB:
    def __init__(self):
        self.deleted: list = []
        self.committed = False

    async def delete(self, row):
        self.deleted.append(row)

    async def commit(self):
        self.committed = True


@pytest.fixture
def _delete_client(monkeypatch):
    user = _user()
    db = _WriteDB()
    audits: list[dict] = []
    removed: list = []
    state: dict = {"refusal": None}
    rows = {
        "take": VideoTake(id=uuid.uuid4(), user_id=user.id, name="Flow", status=JOB_READY, script={}),
        "clip": VideoClip(
            id=uuid.uuid4(),
            user_id=user.id,
            provider="openai",
            model="sora-2",
            prompt="A cow",
            params={},
            status=JOB_READY,
        ),
        "music": VideoMusic(
            id=uuid.uuid4(), user_id=user.id, provider="x", model="y", prompt="A theme", status=JOB_READY
        ),
    }

    async def owned(_db, kind, _asset_id, _user):
        return rows[kind]

    async def refusal(_db, _user, kind, asset_id):
        return state["refusal"]

    async def nothing(*_args, **_kwargs):
        return None

    async def audit(_db, **kwargs):
        audits.append(kwargs)

    async def delete_files(files):
        removed.append(files)

    monkeypatch.setattr(video_studio_api, "_owned_asset", owned)
    monkeypatch.setattr(library, "deletion_refusal", refusal)
    monkeypatch.setattr(video_studio_api.jobs, "cancel", nothing)
    monkeypatch.setattr(video_studio_api, "log_audit", audit)
    monkeypatch.setattr(video_studio_api.assets, "delete_files", delete_files)
    app = FastAPI()
    app.include_router(video_studio_api.router, prefix="/api/v1/video-studio")
    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    return SimpleNamespace(client=TestClient(app), db=db, audits=audits, removed=removed, state=state, rows=rows)


_PATHS = {"take": "takes", "clip": "clips", "music": "music"}


@pytest.mark.parametrize("kind", ["take", "clip", "music"])
def test_a_medium_a_video_uses_is_refused_unless_forced_and_a_forced_deletion_is_audited(_delete_client, kind):
    env = _delete_client
    row = env.rows[kind]
    env.state["refusal"] = {
        "code": "media_in_use",
        "message": "used",
        "videos": [{"id": "v-1", "title": "My film"}],
        "others": 1,
    }
    path = f"/api/v1/video-studio/{_PATHS[kind]}/{row.id}"

    refused = env.client.delete(path)
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "media_in_use"
    assert env.db.deleted == [] and not env.db.committed and env.removed == []

    forced = env.client.delete(path, params={"force": "true"})
    assert forced.status_code == 204
    assert env.db.deleted == [row] and env.db.committed
    assert len(env.audits) == 1
    assert env.audits[0]["entity_id"] == str(row.id)
    assert env.audits[0]["details"] == {"forced": True, "videos": ["v-1"], "other_videos": 1}


def test_a_medium_no_video_uses_is_deleted_at_once_and_not_audited(_delete_client):
    env = _delete_client
    row = env.rows["take"]
    answer = env.client.delete(f"/api/v1/video-studio/takes/{row.id}")
    assert answer.status_code == 204
    assert env.db.deleted == [row] and env.audits == []


def test_a_recording_a_repair_is_made_from_is_refused_even_forced(_delete_client):
    """A queued or running repair needs its source; force=true does not lift that refusal."""
    env = _delete_client
    row = env.rows["take"]
    env.state["refusal"] = {"code": "media_in_use", "message": "repair", "videos": [], "others": 0, "force_allowed": False}
    forced = env.client.delete(f"/api/v1/video-studio/takes/{row.id}", params={"force": "true"})
    assert forced.status_code == 409 and forced.json()["detail"]["force_allowed"] is False
    assert env.db.deleted == [] and not env.db.committed and env.audits == [] and env.removed == []
