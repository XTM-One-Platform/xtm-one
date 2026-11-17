"""A pre-flight's history read on PostgreSQL: ``rates_of``, ``own_render`` and ``estimate_errors`` on real rows.

The unit tests answer these reads from a fake that reads the compiled SQL; here the database answers them:

* a video rendered at ultra prices its next pre-flight from its own last render (``own_render``): every scene type at
  what it measured - another video's render on the same worker shape priced from the shape's renders;
* ``estimate_errors`` filters in the database - the quality, the estimate model, the worker shape the estimate was made
  for and drawn on, the history it was priced from - and still reads every error a matching render left, newest first;
* a malformed row (pages that are no number) never drops the rest of the history.

``integration``-marked and skips when PostgreSQL is unreachable; the temporary database is ``tests/scratch_database.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.user import User
from app.models.video_studio import JOB_READY, VideoProject, VideoRender
from app.services.video_studio import render_history, render_preflight
from app.services.video_studio.render_history import Shape
from tests.scratch_database import ALL_TABLES, scratch_database
from tests.test_video_studio_preflight_pace_6372 import CEILING, COSTS, UHD, _estimate, _film, _measured, _platform

pytestmark = pytest.mark.integration

SHAPE = Shape.of(8, 9, CEILING)


async def _seed(db, *, renders: list[tuple[dict, dict | None]]) -> tuple[VideoProject, VideoProject, list[uuid.UUID]]:
    """Two videos of one user; *renders* - ``(measured, preflight)``, newest first - finished whole ultra renders of the
    first."""
    owner = User(email="maker@example.com", hashed_password="x", full_name="Video Maker", role="user")
    db.add(owner)
    await db.flush()
    film = VideoProject(user_id=owner.id, title="Launch film", spec={"title": "Launch film"}, scene_count=5)
    other = VideoProject(user_id=owner.id, title="Board pitch", spec={"title": "Board pitch"}, scene_count=5)
    db.add_all([film, other])
    await db.flush()
    now = datetime.now(UTC)
    ids = []
    for age, (measured, preflight) in enumerate(renders):
        row = VideoRender(
            project_id=film.id,
            user_id=owner.id,
            revision=1,
            quality="ultra",
            status=JOB_READY,
            width=UHD[0],
            height=UHD[1],
            duration_ms=141_033,
            measured=measured,
            preflight=preflight,
            finished_at=now - timedelta(hours=age),
        )
        db.add(row)
        await db.flush()
        ids.append(row.id)
    await db.commit()
    return film, other, ids


async def test_a_video_s_history_and_the_estimates_errors_are_read_from_the_database():
    film = _film()
    first = _estimate(film, _platform())
    second = _estimate(film, None)
    again = _measured(film, costs={name: cost * 1.06 for name, cost in COSTS.items()})
    # Newest first: its second render (estimated from the video's own), its first (from the platform's medians), and
    # a render of another estimate model that no error is read from.
    stale = {**second.as_dict(), "model": render_preflight.ESTIMATE_MODEL - 1}
    rows = [(again, {**second.as_dict(), "costs_from": "video"}), (_measured(film), first.as_dict()), (_measured(film), stale)]
    async with scratch_database("video_history", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as scratch:
        async with scratch.sessions() as db:
            video, other, ids = await _seed(db, renders=rows)
        async with scratch.sessions() as db:
            own = await render_history.own_render(db, video.id, "ultra")
            assert own is not None and own.render_id == str(ids[0]) and own.efficiency == pytest.approx(0.9, rel=1e-3)
            assert await render_history.own_render(db, other.id, "ultra") is None
            # The estimates priced from the video's own render: the newest only, never the platform-priced or the stale.
            from_video = await render_history.estimate_errors(db, "ultra", shape=SHAPE, costs_from="video")
            assert [item.render for item in from_video] == [str(ids[0])]
            from_platform = await render_history.estimate_errors(db, "ultra", shape=SHAPE, costs_from="platform")
            assert [item.render for item in from_platform] == [str(ids[1])]
            # Another shape, another quality: nothing.
            assert await render_history.estimate_errors(db, "ultra", shape=Shape.of(4, 9, CEILING), costs_from="video") == ()
            assert await render_history.estimate_errors(db, "high", shape=SHAPE, costs_from="video") == ()
            rates = await render_preflight.rates_of(
                db, "ultra", contended_pages=False, video_id=video.id, shape=SHAPE, scene_types={name for *_t, name in film.scenes}
            )
            assert rates.own is not None and rates.own.render_id == str(ids[0]) and rates.costs_from == "video"
            assert [item.render for item in rates.errors] == [str(ids[0])]
            # Another video, priced from the platform's history: corrected by the platform-priced estimate's error alone.
            elsewhere = await render_preflight.rates_of(db, "ultra", contended_pages=False, video_id=other.id, shape=SHAPE)
            assert elsewhere.own is None and elsewhere.priced == "platform"
            assert [item.render for item in elsewhere.errors] == [str(ids[1])]


async def test_a_malformed_row_never_drops_the_history():
    film = _film()
    said = _estimate(film, None).as_dict()
    broken = {**_measured(film), "pages": "eight", "cpus": {"many": True}}
    async with scratch_database("video_history_bad", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as scratch:
        async with scratch.sessions() as db:
            video, _other, ids = await _seed(db, renders=[(broken, said), (_measured(film), said)])
        async with scratch.sessions() as db:
            rates = await render_preflight.measured_rates(db, "ultra", shape=SHAPE)
            assert rates is not None and rates.measured >= 1
            errors = await render_history.estimate_errors(db, "ultra", shape=SHAPE, costs_from="platform")
            assert [item.render for item in errors] == [str(ids[1])]
            # The newest render's pages read as no number: it prices nothing, the one before it does.
            own = await render_history.own_render(db, video.id, "ultra")
            assert own is not None and own.render_id == str(ids[1])
