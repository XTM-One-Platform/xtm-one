"""A render a rollout hands to a worker of the next release resumes without tracking again, its percent never down.

During a burst of rollouts a long high render changed worker at every one. Each new attempt followed its screen
inserts through their whole footage again - the kept tracks were keyed on the release, which the rollout changed -
and reported its progress from 1 % again, though it resumed its drawing from the frames kept.

* a render keeps its tracks under a key of its own, without the release (``render._track_digest(render_id=...)``):
  the same render on the next release reads them back and follows nothing again; another render on a new release
  still follows its surfaces;
* a whole render taken over never reports less than where its attempts before stood
  (``render.resumed_progress`` over ``VideoRender.relaunches``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services.video_studio import insert_tracking, render, render_cache

FOOTAGE = "/work/footage.mp4"


def _asked() -> list:
    quad = ((0.3, 0.3), (0.6, 0.3), (0.6, 0.6), (0.3, 0.6))
    return [SimpleNamespace(quad=quad, first=0, last=191, time_map=((0.0, 0.0), (8.0, 8.0)), since=0, key=None)]


@pytest.fixture
def origins():
    token = render._ORIGINS.set({FOOTAGE: "file-1"})
    yield
    render._ORIGINS.reset(token)


def test_a_render_s_own_track_key_does_not_change_with_the_release(monkeypatch, origins):
    monkeypatch.setattr(render.settings, "app_version", "1.0.0")
    shared_before = render._track_digest(FOOTAGE, _asked(), None)
    own_before = render._track_digest(FOOTAGE, _asked(), None, render_id="r1")
    monkeypatch.setattr(render.settings, "app_version", "1.0.1")
    assert render._track_digest(FOOTAGE, _asked(), None) != shared_before
    assert render._track_digest(FOOTAGE, _asked(), None, render_id="r1") == own_before
    # Another render keeps its own.
    assert render._track_digest(FOOTAGE, _asked(), None, render_id="r2") != own_before
    # Footage the render draws itself is never kept.
    assert render._track_digest("/work/figure.mp4", _asked(), None, render_id="r1") is None


def test_the_next_release_reads_back_the_tracks_of_the_same_render(monkeypatch, origins):
    kept: dict = {}

    async def put_json(kind, digest, value):
        kept[(kind, digest)] = value

    async def get_json(kind, digest):
        return kept.get((kind, digest))

    monkeypatch.setattr(render_cache, "put_json", put_json)
    monkeypatch.setattr(render_cache, "get_json", get_json)
    quads = tuple(((0.3 + 0.001 * n, 0.3), (0.6, 0.3), (0.6, 0.6), (0.3, 0.6)) for n in range(4))
    track = insert_tracking.InsertTrack(4, quads, (0.3, 0.3, 0.604, 0.6))
    monkeypatch.setattr(render.settings, "app_version", "1.0.0")
    own = render._track_digest(FOOTAGE, _asked(), None, render_id="r1")
    asyncio.run(render._keep_tracks(own, [track]))
    monkeypatch.setattr(render.settings, "app_version", "1.0.1")
    assert asyncio.run(render._kept_tracks(render._track_digest(FOOTAGE, _asked(), None), 1)) is None
    [again] = asyncio.run(render._kept_tracks(render._track_digest(FOOTAGE, _asked(), None, render_id="r1"), 1))
    assert again == track


@pytest.mark.parametrize(
    ("relaunches", "floor"),
    [
        (None, 0),
        ([], 0),
        ([{"progress": 40}, {"progress": 11}, {"progress": 56}, {"progress": 12}], 56),
        ([{"progress": "bad"}, "not an entry", {"progress": 27}], 27),
        ([{"progress": 120}], 99),
    ],
)
def test_a_relaunched_render_never_reports_less_than_it_stood_at(relaunches, floor):
    assert render.resumed_progress(relaunches) == floor
