"""``GET /video-studio/assets`` sorts the whole media match on the server (#9378 slice 7c).

The Media tab runs on the shared data table in server mode: ``sort`` (``created_at``
or ``title``) and ``direction`` order both kinds together before the page is cut,
the newest medium then the id breaking ties; the default stays the newest first.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

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


def _order(**kwargs) -> str:
    """The ORDER BY of the page's ranking query (COUNT first, then the ranking)."""
    from app.services.video_studio import library

    user = SimpleNamespace(id=uuid.uuid4(), email="someone@example.com", role="user", is_admin=False)
    session = _FakeSession([_Result(one=0), _Result(rows=[])])
    asyncio.run(library.asset_page(session, user, library.AssetFilters(), limit=50, offset=0, **kwargs))
    sql = str(session.statements[1].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    return sql.rsplit("ORDER BY", 1)[1].split(" LIMIT", 1)[0].strip()


def test_the_default_order_is_the_newest_first_once():
    order = _order()
    assert order.startswith("anon_1.created_at DESC")
    assert order.count("created_at") == 1
    assert order.endswith("anon_1.id DESC")


def test_the_title_order_sorts_both_kinds_with_the_newest_breaking_ties():
    order = _order(sort="title", direction="asc")
    assert order.startswith("anon_1.title ASC")
    assert "anon_1.created_at DESC" in order
    assert order.endswith("anon_1.id DESC")


def test_the_oldest_first():
    assert _order(sort="created_at", direction="asc").startswith("anon_1.created_at ASC")


def test_the_title_is_the_name_else_the_prompt_lower_case():
    from app.services.video_studio import library

    user = SimpleNamespace(id=uuid.uuid4(), email="someone@example.com", role="user", is_admin=False)
    listing = library._listing(user, library.AssetFilters())
    sql = str(listing.element.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "lower(coalesce(video_clips.name, video_clips.prompt, " in sql
    assert "lower(coalesce(video_takes.name" in sql
