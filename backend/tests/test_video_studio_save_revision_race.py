"""Two saves of one video in flight together never number the same revision (``uq_video_project_revisions_revision``).

A batch of ``render_video_scene`` calls runs its pre-render lint once per call; when the lint stores a quad, each
call saves the video. ``projects.save_spec`` locks the video's row and reads it again before it numbers the next
revision: a save that finds the very spec the other one just wrote writes nothing, and one that finds another
spec numbers its revision after it. Synthetic session; the row lock itself is PostgreSQL's ``FOR UPDATE``.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.services.video_studio import projects
from app.services.video_studio.spec import authored_spec, validate_video_spec

USER = SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-00000000a1a1"), role="user", email="maker@example.com")


def _spec(title: str) -> dict:
    return authored_spec(
        validate_video_spec(
            {
                "title": title,
                "freestyle": True,
                "scenes": [{"id": "open", "type": "title", "title": title, "duration": 3}],
            }
        )
    )


@pytest.fixture
def quiet(monkeypatch):
    """Everything the save does before it numbers a revision, answered with nothing to say."""
    from app.services.video_studio import music as music_tracks
    from app.services.video_studio import templates

    async def none(*_args, **_kwargs):
        return None

    async def nothing_started(*_args, **_kwargs):
        return 0, []

    async def no_problem(*_args, **_kwargs):
        return []

    async def planned(*_args, **_kwargs):
        return SimpleNamespace(duration=3.0, document={"scenes": []})

    monkeypatch.setattr(projects, "verify_references", no_problem)
    monkeypatch.setattr(templates, "resolve_for_spec", none)
    monkeypatch.setattr(projects, "estimate", planned)
    monkeypatch.setattr(projects, "start_animations", nothing_started)
    monkeypatch.setattr(music_tracks, "start_for_spec", nothing_started)


class _NumberedError(Exception):
    """Raised at the first write: the revision the save numbered is on the project."""


class _Session:
    """A session whose row lock reads what the save in flight beside this one committed (*committed*)."""

    def __init__(self, committed: dict):
        self.committed = committed
        self.locked: list = []

    async def execute(self, _statement):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    async def refresh(self, row, *, attribute_names=None, with_for_update=False):
        self.locked.append((tuple(attribute_names or ()), with_for_update))
        for key, value in self.committed.items():
            setattr(row, key, value)

    def add(self, _row):
        raise _NumberedError

    async def flush(self):
        raise _NumberedError


def _project(spec: dict, revision: int):
    return SimpleNamespace(id=uuid.uuid4(), spec=spec, template_container_id=None, revision=revision, scene_previews={})


def test_a_save_that_finds_its_own_spec_written_meanwhile_writes_no_second_revision(quiet):
    stored, snapped = _spec("Before"), _spec("Snapped")
    project = _project(stored, 7)
    # The other call of the batch saved the very same snap as revision 8 while this one measured.
    db = _Session({"revision": 8, "spec": snapped})
    saved = asyncio.run(projects.save_spec(db, user=USER, raw=snapped, project=project))
    assert saved.unchanged and project.revision == 8
    assert db.locked == [(("revision", "spec", "template_container_id"), True)]


def test_a_save_that_finds_another_revision_written_meanwhile_numbers_its_own_after_it(quiet):
    stored = _spec("Before")
    project = _project(stored, 7)
    db = _Session({"revision": 8, "spec": _spec("Someone else")})
    with pytest.raises(_NumberedError):
        asyncio.run(projects.save_spec(db, user=USER, raw=_spec("Mine"), project=project))
    # Numbered after the revision committed beside it, never a second revision 8.
    assert project.revision == 9


def test_a_save_alone_numbers_the_next_revision(quiet):
    stored = _spec("Before")
    project = _project(stored, 7)
    db = _Session({})
    with pytest.raises(_NumberedError):
        asyncio.run(projects.save_spec(db, user=USER, raw=_spec("Mine"), project=project))
    assert project.revision == 8 and db.locked
