"""``GET /video-studio/projects`` sorts the whole match on the server (#9378 slice 7).

The video library runs on the shared data table in server mode: ``sort`` and
``direction`` order every match before the page is cut, the last changed video
breaking ties; the default stays the last changed first; an unknown value is a 422.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql


class _Result:
    def __init__(self, one=None, rows=None):
        self._one = one
        self._rows = rows or []

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one(self):
        return self._one if self._one is not None else 0


class _FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.statements: list = []

    async def execute(self, statement, *a, **k):
        self.statements.append(statement)
        return self._results.pop(0) if self._results else _Result()


def _user():
    return SimpleNamespace(id=uuid.uuid4(), email="someone@example.com", role="user", is_admin=False)


def _rows_sql(**query) -> str:
    """The page's row query: groups, COUNT, then the rows, in that order."""
    from app.api.v1.video_studio import list_projects

    # Called directly, so every Query default is spelled out.
    params = {
        "search": None,
        "vis": None,
        "visibility": None,
        "limit": 24,
        "offset": 0,
        "sort": "updated_at",
        "direction": "desc",
        "render": None,
        **query,
    }
    session = _FakeSession([_Result(rows=[]), _Result(one=0), _Result(rows=[])])
    asyncio.run(list_projects(**params, user=_user(), db=session))
    rows = session.statements[2]
    return str(rows.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _order(**query) -> str:
    return _rows_sql(**query).split("ORDER BY", 1)[1].split(" LIMIT", 1)[0].strip()


def test_the_default_order_is_the_last_changed_first_once():
    order = _order()
    assert order.startswith("video_projects.updated_at DESC")
    assert order.count("video_projects.updated_at") == 1
    assert order.endswith("video_projects.id DESC")


def test_each_column_of_the_table_sorts_the_whole_match_with_the_last_changed_breaking_ties():
    by_title = _order(sort="title", direction="asc")
    assert by_title.startswith("lower(video_projects.title) ASC")
    assert "video_projects.updated_at DESC" in by_title
    assert _order(sort="scene_count", direction="desc").startswith("video_projects.scene_count DESC")
    assert _order(sort="created_at", direction="asc").startswith("video_projects.created_at ASC")
    assert _order(sort="updated_at", direction="asc").startswith("video_projects.updated_at ASC")


def test_the_render_filter_narrows_the_whole_match_and_the_count():
    rendered = _rows_sql(render="rendered")
    assert "video_projects.latest_render_id IS NOT NULL" in rendered
    not_rendered = _rows_sql(render="not_rendered")
    assert "video_projects.latest_render_id IS NULL" in not_rendered
    assert "latest_render_id IS" not in _rows_sql()


def _segment_count_sqls(**query) -> list[str]:
    """The four COUNT queries of ``GET /projects/segment-counts`` (after the groups read)."""
    from app.api.v1.video_studio import project_segment_counts

    params = {"search": None, "render": None, **query}
    session = _FakeSession([_Result(rows=[])])
    asyncio.run(project_segment_counts(**params, user=_user(), db=session))
    return [
        str(s.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        for s in session.statements[1:]
    ]


def test_the_segment_counts_follow_the_render_filter():
    """The scope switcher counts what the table lists (UX round 2 on #11116)."""
    rendered = _segment_count_sqls(render="rendered")
    assert len(rendered) == 4
    assert all("video_projects.latest_render_id IS NOT NULL" in sql for sql in rendered)
    assert all("latest_render_id IS" not in sql for sql in _segment_count_sqls())


def test_an_unknown_render_on_the_segment_counts_is_a_422():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _segment_count_sqls(render="failed")
    assert exc.value.status_code == 422


@pytest.mark.parametrize("bad", [{"sort": "owner"}, {"direction": "sideways"}, {"render": "failed"}])
def test_an_unknown_sort_or_direction_is_a_422(bad):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _rows_sql(**bad)
    assert exc.value.status_code == 422
