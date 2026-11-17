"""A ready render whose spec revision fell out of the held window stays re-judgeable (issue #10899).

``render_rejudge.same_drawing_spec`` finds a held revision that draws and plays exactly what the render recorded
(``VideoRender.drawing``), the current one first; ``revisions.record_revision`` never prunes the revision of a ready
whole render.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.video_studio import (
    lexicon_store,
    projects,
    render_checks,
    render_rejudge,
    revisions,
)
from app.services.video_studio import spec as spec_module

DRAWN = {"s01": "d1", "s02": "d2"}
HEARD = "h1"


def _render(**values):
    base = {"scene_id": None, "revision": 206, "drawing": {"scenes": DRAWN, "heard": HEARD}}
    base.update(values)
    return SimpleNamespace(**base)


def _project():
    return SimpleNamespace(id="p", revision=209, spec={"name": "current"})


def _patch(monkeypatch, *, held=(207, 209), digests=None, missing=()):
    """Revisions 207-209 held; *digests*: authored spec name -> (drawn, heard)."""
    digests = digests or {}
    asked: list[int] = []

    async def held_range(db, project_id):
        return held

    async def spec_at(db, project_id, revision, *, current):
        asked.append(revision)
        if revision in missing:
            raise revisions.RevisionNotFoundError(f"Revision {revision} not found")
        return {"name": f"rev{revision}"}

    async def effective_spec(db, project, user, *, authored=None):
        return SimpleNamespace(spec=dict(authored or project.spec))

    async def for_voiceover(db, voiceover):
        return None

    async def digest_scenes(db, effective, lexicon=None):
        drawn, heard = digests.get(effective["name"], ({"s01": "other"}, "other"))
        return SimpleNamespace(drawn=drawn, heard=heard)

    monkeypatch.setattr(revisions, "held_range", held_range)
    monkeypatch.setattr(revisions, "spec_at", spec_at)
    monkeypatch.setattr(projects, "effective_spec", effective_spec)
    monkeypatch.setattr(lexicon_store, "for_voiceover", for_voiceover)
    monkeypatch.setattr(render_checks, "digest_scenes", digest_scenes)
    monkeypatch.setattr(spec_module, "parse_video_spec", lambda effective: SimpleNamespace(voiceover=None))
    return asked


async def test_a_pruned_revision_is_judged_on_the_current_spec_when_it_draws_the_same(monkeypatch):
    asked = _patch(monkeypatch, digests={"current": (DRAWN, HEARD)})
    found = await render_rejudge.same_drawing_spec(None, _project(), None, _render())
    assert found == ({"name": "current"}, 209)
    # The current revision matched: no older revision read.
    assert asked == []


async def test_a_pruned_revision_is_judged_on_the_oldest_held_revision_that_draws_the_same(monkeypatch):
    asked = _patch(monkeypatch, digests={"rev207": (DRAWN, HEARD)})
    found = await render_rejudge.same_drawing_spec(None, _project(), None, _render())
    assert found == ({"name": "rev207"}, 207)
    assert asked == [207]


async def test_no_held_revision_draws_the_same_answers_none(monkeypatch):
    _patch(monkeypatch, digests={"current": (DRAWN, "another sound")})
    assert await render_rejudge.same_drawing_spec(None, _project(), None, _render()) is None


async def test_a_render_that_recorded_no_drawing_is_never_matched(monkeypatch):
    _patch(monkeypatch, digests={"current": (DRAWN, HEARD)})
    assert await render_rejudge.same_drawing_spec(None, _project(), None, _render(drawing=None)) is None
    assert await render_rejudge.same_drawing_spec(None, _project(), None, _render(scene_id="s01")) is None


async def test_a_revision_that_cannot_be_read_leaves_the_others_to_compare(monkeypatch):
    asked = _patch(monkeypatch, digests={"rev208": (DRAWN, HEARD)}, missing=(207,))
    found = await render_rejudge.same_drawing_spec(None, _project(), None, _render())
    assert found == ({"name": "rev208"}, 208)
    assert asked == [207, 208]


async def test_the_compared_revisions_are_bounded(monkeypatch):
    asked = _patch(monkeypatch, held=(1, 209))
    assert await render_rejudge.same_drawing_spec(None, _project(), None, _render()) is None
    assert len(asked) == render_rejudge.SAME_DRAWING_TRIES - 1


class _Db:
    def __init__(self):
        self.statements: list = []

    def add(self, row):
        pass

    async def flush(self):
        pass

    async def execute(self, statement):
        self.statements.append(statement)


async def test_the_retention_keeps_the_revision_of_a_ready_whole_render():
    db = _Db()
    await revisions.record_revision(
        db,
        project_id="00000000-0000-0000-0000-000000000001",
        revision=revisions.KEPT_REVISIONS + 5,
        spec={},
        previous=None,
        author_label=None,
        via="test",
    )
    (statement,) = db.statements
    sql = str(statement.compile(compile_kwargs={"literal_binds": False}))
    assert "video_project_revisions" in sql and "NOT IN" in sql and "video_renders" in sql
