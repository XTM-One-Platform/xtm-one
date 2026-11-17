"""A render-start lock wait that runs out is said in plain words, never as the cancelled SQL (#8545)."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import DBAPIError

from app.services.video_studio import projects


class _CancelledError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__("canceling statement due to statement timeout")
        self.sqlstate = sqlstate


class _Db:
    def __init__(self, sqlstate: str) -> None:
        self.sqlstate = sqlstate
        self.asked: list[tuple[str, dict]] = []

    async def execute(self, statement, params=None):
        self.asked.append((str(statement), dict(params or {})))
        raise DBAPIError(str(statement), params, _CancelledError(self.sqlstate))


@pytest.mark.parametrize("sqlstate", ["57014", "55P03"])
def test_a_lock_wait_past_its_timeout_is_a_busy_start(sqlstate):
    project = SimpleNamespace(id=uuid.uuid4())
    db = _Db(sqlstate)
    with pytest.raises(projects.RenderStartBusyError) as raised:
        asyncio.run(projects._lock_scene_preview_start(db, project, "hook"))
    said = str(raised.value)
    assert "nothing was queued" in said and "Ask again" in said
    assert "pg_advisory" not in said and "SQL" not in said
    # Its own scene's lock, never the video-wide one a whole render takes.
    [(sql, params)] = db.asked
    assert "pg_advisory_xact_lock" in sql
    assert params == {"scope": "video_preview_start", "key": f"{project.id}:hook"}


def test_another_database_error_is_not_hidden():
    project = SimpleNamespace(id=uuid.uuid4())
    with pytest.raises(DBAPIError):
        asyncio.run(projects._lock_scene_preview_start(_Db("08006"), project, "hook"))


def test_a_whole_render_start_still_takes_the_video_lock_then_the_persons():
    project, user = SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4())
    taken: list[dict] = []

    class _Recording:
        async def execute(self, _statement, params=None):
            taken.append(dict(params or {}))

    asyncio.run(projects._lock_render_starts(_Recording(), project, user))
    assert taken == [
        {"scope": "video_render_start", "key": str(project.id)},
        {"scope": "video_render_start_user", "key": str(user.id)},
    ]
