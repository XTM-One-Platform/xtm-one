"""Tests for presentation collaboration: history, review comments,
presence, and the way they surface to agents and to the UI.

The contract these lock in:

1. Every save snapshots the spec into ``presentation_versions`` (agent edits,
   manual PATCH edits, duplicates, restores), keyed by revision, with the
   author and a human summary. Retention keeps the newest
   ``PRESENTATION_VERSION_RETENTION`` rows; restoring a past revision is a
   NEW revision (nothing is ever rewritten in history).
2. Review comments are anchored to a slide id, threaded one level deep, and
   resolvable; only the author or someone who can manage the deck may edit
   or delete one. A comment whose slide was removed is reported ``detached``
   instead of vanishing.
3. Presence is a Redis room with per-user heartbeats and a TTL: never a DB
   write, fail-open when Redis is down.
4. The agent reads open comments through ``get_presentation`` so "address the
   review comments" is a one-turn instruction, and records a
   ``change_summary`` with every ``generate_presentation`` edit.

No DB required — same fake-session style as ``test_presentations.py``.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend"


# ── Fakes ────────────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, one=None, rows=None, rowcount: int = 0):
        self._one = one
        self._rows = rows or []
        self.rowcount = rowcount

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._one

    def first(self):
        return self._rows[0] if self._rows else self._one


class _FakeSession:
    def __init__(self, results=None):
        self._results = list(results or [])
        self.statements: list = []
        self.added: list = []
        self.deleted: list = []
        self.committed = False
        self.flushed = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, *a, **k):
        self.statements.append(statement)
        return self._results.pop(0) if self._results else _Result()

    async def commit(self):
        self.committed = True

    async def flush(self):
        self.flushed += 1

    async def refresh(self, obj):
        return None

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)


def _slides(count: int = 3) -> list[dict]:
    return [{"id": f"s{i}", "layout": "title", "title": f"Slide {i}", "subtitle": "sub"} for i in range(count)]


def _deck(owner: uuid.UUID, slides: int = 3, **columns):
    from app.models.presentation import Presentation

    return Presentation(
        id=uuid.uuid4(),
        user_id=owner,
        title="Deck",
        theme="dark",
        slide_count=slides,
        revision=1,
        spec={"title": "Deck", "slides": _slides(slides)},
        **columns,
    )


def _version(deck, revision: int, **columns):
    from app.models.presentation import PresentationVersion

    return PresentationVersion(
        **{
            "id": uuid.uuid4(),
            "presentation_id": deck.id,
            "revision": revision,
            "title": deck.title,
            "slide_count": deck.slide_count,
            "spec": dict(deck.spec),
            "source": "agent",
            "created_at": datetime.now(UTC),
            **columns,
        }
    )


def _comment(deck, author: uuid.UUID, slide_id: str | None = "s1", **columns):
    from app.models.presentation import PresentationComment

    return PresentationComment(
        id=uuid.uuid4(),
        presentation_id=deck.id,
        slide_id=slide_id,
        author_id=author,
        body="Tighten this headline",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        **columns,
    )


def _user(user_id: uuid.UUID, *, is_admin: bool = False):
    return SimpleNamespace(id=user_id, is_admin=is_admin, email="u@example.com", full_name="U")


# ── 1. Versions ──────────────────────────────────────────────────────────────


def test_recording_a_version_snapshots_the_spec_with_author_and_summary():
    from app.models.presentation import VERSION_SOURCE_MANUAL
    from app.services.presentation_versions import record_presentation_version

    owner = uuid.uuid4()
    deck = _deck(owner)
    session = _FakeSession()
    version = asyncio.run(
        record_presentation_version(
            session,
            deck,
            author_id=owner,
            source=VERSION_SOURCE_MANUAL,
            summary="  Edited   title  ",
        )
    )
    assert session.added == [version]
    assert version.presentation_id == deck.id
    assert version.revision == deck.revision
    assert version.spec == deck.spec and version.spec is not deck.spec  # a copy
    assert version.author_id == owner
    assert version.source == "manual"
    assert version.summary == "Edited title"
    assert version.slide_count == deck.slide_count


def test_recording_the_same_revision_twice_is_idempotent():
    """A retried save must not violate the (presentation, revision) unique key."""
    from app.services.presentation_versions import record_presentation_version

    deck = _deck(uuid.uuid4())
    already = _version(deck, deck.revision)
    session = _FakeSession(results=[_Result(one=already)])
    got = asyncio.run(record_presentation_version(session, deck, author_id=None, source="agent"))
    assert got is already
    assert session.added == []


def test_unknown_version_source_is_refused():
    from app.services.presentation_versions import record_presentation_version

    deck = _deck(uuid.uuid4())
    with pytest.raises(ValueError):
        asyncio.run(record_presentation_version(_FakeSession(), deck, author_id=None, source="bogus"))


def test_retention_deletes_only_the_oldest_beyond_the_cap():
    from app.models.presentation import PRESENTATION_VERSION_RETENTION
    from app.services.presentation_versions import _enforce_retention

    deck = _deck(uuid.uuid4())
    # Nothing to do below the cap: the count query is the only statement.
    session = _FakeSession(results=[_Result(one=PRESENTATION_VERSION_RETENTION)])
    asyncio.run(_enforce_retention(session, deck.id))
    assert len(session.statements) == 1

    # Above the cap: the delete is scoped to THIS deck and removes exactly the
    # ``excess`` OLDEST revisions (ascending order + limit), never the newest.
    excess = 3
    session = _FakeSession(
        results=[
            _Result(one=PRESENTATION_VERSION_RETENTION + excess),
            _Result(rowcount=excess),
        ]
    )
    pruned = asyncio.run(_enforce_retention(session, deck.id))
    assert pruned == excess
    from sqlalchemy.dialects import postgresql

    delete_sql = str(
        session.statements[-1].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert delete_sql.startswith("DELETE FROM presentation_versions")
    assert str(deck.id) in delete_sql
    assert "ORDER BY presentation_versions.revision ASC" in delete_sql
    assert f"LIMIT {excess}" in delete_sql


def test_save_presentation_records_a_version_and_the_editor(monkeypatch):
    """The single write funnel snapshots history on every save, for agents and
    humans alike, and stamps who touched the deck last."""
    from app.models.presentation import VERSION_SOURCE_MANUAL, PresentationVersion
    from app.services.presentations import save_presentation

    owner, editor = uuid.uuid4(), uuid.uuid4()
    deck = _deck(owner)
    session = _FakeSession()
    spec = {"title": "Deck v2", "slides": _slides(2)}
    saved = asyncio.run(
        save_presentation(
            session,
            user_id=owner,
            spec_payload=spec,
            presentation=deck,
            author_id=editor,
            change_source=VERSION_SOURCE_MANUAL,
            change_summary="Edited slides",
        )
    )
    assert saved is deck
    assert deck.revision == 2
    assert deck.user_id == owner  # ownership never moves
    assert deck.updated_by_user_id == editor
    versions = [r for r in session.added if isinstance(r, PresentationVersion)]
    assert len(versions) == 1
    assert versions[0].revision == 2
    assert versions[0].author_id == editor
    assert versions[0].source == "manual"
    assert versions[0].summary == "Edited slides"
    assert versions[0].spec["title"] == "Deck v2"


def test_restore_is_a_new_revision_that_points_back(monkeypatch):
    """Restoring never rewrites history: the old snapshot stays, a new revision
    carrying its spec is appended, tagged with where it came from."""
    from app.models.presentation import PresentationVersion
    from app.services.presentation_versions import restore_presentation_version

    owner = uuid.uuid4()
    deck = _deck(owner, slides=3)
    deck.revision = 4
    old = _version(deck, 2)
    old.spec = {"title": "Old title", "slides": _slides(1)}
    old.title = "Old title"
    old.slide_count = 1
    session = _FakeSession()
    restored = asyncio.run(restore_presentation_version(session, deck, old, user_id=owner))
    assert restored is deck
    assert deck.revision == 5
    assert deck.title == "Old title"
    assert deck.slide_count == 1
    new_versions = [r for r in session.added if isinstance(r, PresentationVersion)]
    assert len(new_versions) == 1
    assert new_versions[0].revision == 5
    assert new_versions[0].source == "restore"
    assert new_versions[0].restored_from_revision == 2
    assert "revision 2" in (new_versions[0].summary or "")


def test_restoring_a_revision_whose_picture_is_gone_brings_it_back_without_it(monkeypatch):
    """A revision that showed a picture whose file no longer exists (a
    removed built-in template's asset) is restored, not refused; the history
    line counts the picture left empty."""
    from app.models.presentation import PresentationVersion
    from app.services.presentation_versions import restore_presentation_version

    owner = uuid.uuid4()
    deck = _deck(owner, slides=1)
    deck.revision = 3
    removed = str(uuid.uuid4())
    old = _version(deck, 2)
    old.spec = {
        "title": "With the logo",
        "slides": [{"layout": "content", "title": "Trusted by", "blocks": [{"type": "image", "file_id": removed}]}],
    }
    session = _FakeSession()
    restored = asyncio.run(restore_presentation_version(session, deck, old, user_id=owner))
    assert restored is deck and deck.revision == 4
    assert deck.spec["slides"][0]["blocks"][0]["file_id"] == removed
    (version,) = [r for r in session.added if isinstance(r, PresentationVersion)]
    assert version.summary == "Restored revision 2 (1 picture no longer on the platform left empty)"


def test_restoring_a_revision_that_names_an_existing_file_the_restorer_cannot_read_is_refused(monkeypatch):
    """The restore keeps the grant check: the restored revision's ids are
    carried, but a carried id that still has a row is vouched for against the
    person restoring - carrying it never hands a foreign file to the readers."""
    from fastapi import HTTPException

    import app.services.file_access as file_access
    from app.services.presentation_spec import PresentationSpecError
    from app.services.presentation_versions import restore_presentation_version

    owner = uuid.uuid4()
    deck = _deck(owner, slides=1)
    deck.revision = 3
    foreign = SimpleNamespace(id=uuid.uuid4())
    old = _version(deck, 2)
    old.spec = {
        "title": "With a private file",
        "slides": [{"layout": "content", "title": "Secret", "blocks": [{"type": "image", "file_id": str(foreign.id)}]}],
    }

    async def _authorize(row, _user, _db):
        raise HTTPException(status_code=403, detail="not yours")

    monkeypatch.setattr(file_access, "authorize_file_access", _authorize)
    session = _FakeSession([_Result(rows=[foreign]), _Result(one=SimpleNamespace(id=owner, role="user"))])
    with pytest.raises(PresentationSpecError, match="is not a platform file you can read"):
        asyncio.run(restore_presentation_version(session, deck, old, user_id=owner))
    assert deck.revision == 3 and not session.added


def test_manual_edit_summary_names_the_touched_fields():
    from app.services.presentation_versions import summarize_manual_edit

    assert summarize_manual_edit(["title"]) == "Edited title"
    # Stable (sorted) ordering: the same edit reads the same way every time.
    assert summarize_manual_edit(["theme", "slides"]) == "Edited slides, theme"
    assert summarize_manual_edit(["title", "theme", "slides"]) == "Edited slides, theme, title"
    assert summarize_manual_edit([]) is None


def test_manual_edit_summary_is_derived_from_what_changed_not_what_was_sent():
    """The editor PATCHes the whole spec; the history must name the real diff."""
    from app.services.presentation_spec import validate_spec
    from app.services.presentation_versions import changed_spec_fields, summarize_manual_edit

    stored = validate_spec({"title": "Quarterly review", "transition": "fade", "slides": _slides(2)})

    # The editor re-sends title + theme + transition + slides; only the title differs.
    retitled = validate_spec({**stored, "title": "Quarterly review (final)"})
    assert changed_spec_fields(stored, retitled) == ["title"]
    assert summarize_manual_edit(changed_spec_fields(stored, retitled)) == "Edited title"

    # The whole spec re-sent unchanged is not an edit at all.
    assert changed_spec_fields(stored, validate_spec(dict(stored))) == []

    # Resetting an optional key back to its default is a change on that key alone.
    reset = validate_spec({k: v for k, v in stored.items() if k != "transition"})
    assert changed_spec_fields(stored, reset) == ["transition"]

    # A brand-new deck (no stored spec yet) reports every key it carries.
    assert "title" in changed_spec_fields(None, stored)


def test_manual_edit_summary_names_the_slides_it_touched() -> None:
    """A manual save spells its ``slides`` change out by slide, the way
    an agent edit's ``summarize_patch`` does: matched by stable id, edited /
    inserted / deleted / moved apart, positions read from the revision the
    change is about. ``Edited slides`` alone told a reader nothing."""
    from app.services.presentation_spec import validate_spec
    from app.services.presentation_versions import changed_spec_fields, describe_slide_changes, summarize_manual_edit

    stored = validate_spec({"title": "Deck", "slides": _slides(5)})
    ids = [s["id"] for s in stored["slides"]]

    # One title changed, in place.
    retitled = {**stored["slides"][2], "title": "New"}
    edited = validate_spec({**stored, "slides": [*stored["slides"][:2], retitled, *stored["slides"][3:]]})
    assert describe_slide_changes(stored, edited) == ["Edited slide 3"]
    summary = summarize_manual_edit(changed_spec_fields(stored, edited), previous=stored, current=edited)
    assert summary == "Edited slide 3"

    # A slide inserted after the second, another deleted (the fifth).
    new_slide = {"layout": "section", "title": "Inserted"}
    shaped = validate_spec({**stored, "slides": [*stored["slides"][:2], new_slide, *stored["slides"][2:4]]})
    assert describe_slide_changes(stored, shaped) == ["Inserted slide 3", "Deleted slide 5"]

    # The last slide dragged to the front: ONE move, not four.
    reordered = validate_spec({**stored, "slides": [stored["slides"][4], *stored["slides"][:4]]})
    assert describe_slide_changes(stored, reordered) == ["Moved slide 5 to 1"]
    assert [s["id"] for s in reordered["slides"]] == [ids[4], *ids[:4]]

    # Slides and a deck field together: the slides first, the field named as changed.
    restyled = validate_spec({**edited, "theme": "light"})
    assert (
        summarize_manual_edit(changed_spec_fields(stored, restyled), previous=stored, current=restyled)
        == "Edited slide 3; Changed theme"
    )
    # Without the specs the legacy wording stands (nothing to read the slides from).
    assert summarize_manual_edit(["slides", "theme"]) == "Edited slides, theme"
    # A page document says "page".
    pages = validate_spec({"title": "Brief", "aspect_ratio": "A4", "slides": _slides(2)})
    repaged = validate_spec({**pages, "slides": [pages["slides"][1], pages["slides"][0]]})
    assert describe_slide_changes(pages, repaged) == ["Moved page 2 to 1"]


def test_manual_edit_summary_matches_id_less_stored_slides_by_position() -> None:
    """A deck stored before ``validate_spec`` assigned slide ids on every save
    has slides with no ``id``; the save it is compared with gives every slide a
    fresh one. Those are matched by POSITION, and the id is never an edit -
    otherwise a one-word fix read as the whole deck deleted and re-inserted."""
    from app.services.presentation_spec import validate_spec
    from app.services.presentation_versions import describe_slide_changes

    stored = validate_spec({"title": "Deck", "slides": _slides(3)})
    legacy = {**stored, "slides": [{k: v for k, v in s.items() if k != "id"} for s in stored["slides"]]}
    assert all("id" not in s for s in legacy["slides"])

    # One title changed, in place: that slide and only that slide.
    retitled = [legacy["slides"][0], {**legacy["slides"][1], "title": "New"}, legacy["slides"][2]]
    edited = validate_spec({**legacy, "slides": retitled})
    assert all(s["id"] for s in edited["slides"])
    assert describe_slide_changes(legacy, edited) == ["Edited slide 2"]

    # Re-saved untouched (the ids are the only difference): nothing at slide level.
    assert describe_slide_changes(legacy, validate_spec(legacy)) == []

    # A slide inserted BEFORE id-less ones: a positional match cannot tell an
    # insert from a shift (the stored slides carry nothing to follow them by),
    # so the shifted ones read as edited and the surplus as the insert - the
    # accepted degradation for a pre-id deck; never the whole deck deleted.
    grown = validate_spec({**legacy, "slides": [{"layout": "section", "title": "New"}, *legacy["slides"]]})
    assert describe_slide_changes(legacy, grown) == ["Edited slides 1, 2, 3", "Inserted slide 4"]


def test_manual_edit_summary_reads_an_older_deck_through_todays_validator() -> None:
    """The stored spec of an older deck is not today's validator output - a
    retired v1 layout upgraded on read without the defaults the models
    materialise, a v1-era `"subtitle": ""` the strict models drop. Compared
    raw, every such slide read as edited on a save that touched one word
    elsewhere; the baseline is read through the validator first."""
    from app.services.presentation_spec import validate_spec
    from app.services.presentation_versions import describe_slide_changes

    stored = {
        "title": "Deck",
        "slides": [
            {"id": "s0", "layout": "title", "title": "One", "subtitle": ""},
            {"id": "s1", "layout": "two_columns", "title": "T", "left": {"bullets": ["a"]}, "right": {"bullets": ["b"]}},
            {"id": "s2", "layout": "section", "title": "Three"},
        ],
    }
    # The person renamed the first slide and nothing else.
    edited = validate_spec({**stored, "slides": [{**stored["slides"][0], "title": "Uno"}, *stored["slides"][1:]]})
    assert edited["slides"][1]["layout"] == "content", "the v1 slide is upgraded on the way in"
    assert describe_slide_changes(stored, edited) == ["Edited slide 1"]
    # Re-saved untouched, the upgrade and the defaults alone: no slide is named.
    assert describe_slide_changes(stored, validate_spec(dict(stored))) == []
    # A stored spec today's validator refuses is compared raw, never raises.
    broken = {**stored, "slides": [{"id": "s0", "layout": "no-such-layout", "title": "x"}]}
    assert describe_slide_changes(broken, edited) == ["Edited slide 1", "Inserted slides 2, 3"]


def test_edit_route_skips_the_save_when_the_spec_is_unchanged():
    """A no-op PATCH must not bump the revision or write an empty history line."""
    import inspect

    from app.api.v1 import presentations as routes

    source = inspect.getsource(routes.update_presentation)
    # Held to the deck's own cap: the template's, or the deck's
    # current size when it is already over it.
    assert "validated = validate_spec(merged, slide_cap=effective_slide_cap(slide_cap, presentation.spec))" in source
    assert "changed = changed_spec_fields(presentation.spec, validated)" in source
    assert "if changed or relink_template:" in source
    # The history line reads the slides off both specs.
    assert "summarize_manual_edit(touched, previous=presentation.spec, current=validated)" in source


def _session_entry(deck, author: uuid.UUID, *, revision: int, started: timedelta, last: timedelta, **columns):
    """A manual entry an edit session wrote: begun ``started`` ago, last saved ``last`` ago."""
    now = datetime.now(UTC)
    return _version(
        deck,
        revision,
        source="manual",
        author_id=author,
        created_at=now - started,
        updated_at=now - last,
        **columns,
    )


def test_a_manual_save_opens_an_edit_session_that_the_next_save_moves_forward():
    """The editor autosaves: a run of saves by one person is ONE
    history entry, moved to each new revision - its spec replaced, its summary
    what changed since the entry under it - never one row per save, which
    would evict the 30 kept revisions within minutes of typing."""
    from app.models.presentation import VERSION_SOURCE_MANUAL
    from app.services.presentation_spec import validate_spec
    from app.services.presentation_versions import open_edit_session, record_presentation_version

    owner = uuid.uuid4()
    deck = _deck(owner)
    deck.spec = validate_spec(deck.spec)
    under = _version(deck, 1)

    # The first save opens a session: an entry a later save may continue.
    deck.revision = 2
    opened = asyncio.run(
        record_presentation_version(
            _FakeSession(), deck, author_id=owner, source=VERSION_SOURCE_MANUAL, summary="Edited slide 1", opens_edit_session=True
        )
    )
    assert opened.updated_at is not None and opened.created_at is not None

    # The next save by the same person finds it open...
    assert asyncio.run(open_edit_session(_FakeSession(results=[_Result(rows=[opened, under])]), deck, author_id=owner)) is opened

    # ...and moves it to the new revision instead of adding a row.
    slides = [dict(s) for s in deck.spec["slides"]]
    slides[0]["title"] = "First, edited"
    slides[1]["title"] = "Second, edited"
    deck.spec = validate_spec({**deck.spec, "slides": slides})
    deck.revision = 3
    session = _FakeSession(results=[_Result(), _Result(rows=[under])])
    moved = asyncio.run(
        record_presentation_version(
            session, deck, author_id=owner, source=VERSION_SOURCE_MANUAL, summary="Edited slide 2", edit_session=opened
        )
    )
    assert moved is opened
    assert session.added == []
    assert moved.revision == 3
    assert moved.spec == deck.spec and moved.spec is not deck.spec
    # The whole session against the entry it now sits on, not the last save alone.
    assert moved.summary == "Edited slides 1, 2"


def test_an_edit_session_is_closed_by_a_pause_its_length_another_kind_of_entry_or_a_single_save():
    from app.services.presentation_versions import (
        MANUAL_EDIT_SESSION_IDLE_SECONDS,
        MANUAL_EDIT_SESSION_MAX_SECONDS,
        open_edit_session,
    )

    me, colleague = uuid.uuid4(), uuid.uuid4()
    deck = _deck(me)
    fresh = _session_entry(deck, me, revision=4, started=timedelta(minutes=3), last=timedelta(seconds=5))

    def found(*rows):
        return asyncio.run(open_edit_session(_FakeSession(results=[_Result(rows=list(rows))]), deck, author_id=me))

    assert found(fresh) is fresh
    # A colleague's session above mine (two people editing at once) does not close it.
    theirs = _session_entry(deck, colleague, revision=5, started=timedelta(minutes=1), last=timedelta(seconds=2))
    assert found(theirs, fresh) is fresh
    # A pause past the idle window, or a session past its length: a new entry.
    idle = _session_entry(deck, me, revision=4, started=timedelta(minutes=8), last=timedelta(seconds=MANUAL_EDIT_SESSION_IDLE_SECONDS + 1))
    assert found(idle) is None
    long = _session_entry(deck, me, revision=4, started=timedelta(seconds=MANUAL_EDIT_SESSION_MAX_SECONDS + 1), last=timedelta(seconds=1))
    assert found(long) is None
    # An agent edit (or a restore) after it stays a point to go back to.
    agent = _version(deck, 5, source="agent")
    assert found(agent, fresh) is None
    # An entry no session wrote (a template change, any entry from before
    # sessions) is never continued.
    single = _version(deck, 4, source="manual", author_id=me, updated_at=None)
    assert found(single) is None
    # Nobody to attribute the save to: no session.
    assert asyncio.run(open_edit_session(_FakeSession(results=[_Result(rows=[fresh])]), deck, author_id=None)) is None


def test_the_edit_route_continues_a_session_and_audits_only_the_save_that_opened_it():
    """Wiring of the edit session in ``PATCH /presentations/{id}``: a content save is part
    of the person's edit session, a template change is an entry of its own,
    and an autosave that continued a session writes no audit row of its own
    (its content is in the history entry) unless it also changed the sharing;
    a save the server found unchanged wrote nothing and audits nothing. The
    deck row is locked before the revision check (two editors' autosaves),
    and so is a restore's. The session is looked up by the save itself, once
    the write is vouched for - a refused write reads no history. The race
    itself is ``test_presentation_autosave_postgres.py``."""
    import inspect

    from app.api.v1 import presentations as routes
    from app.services import presentations as decks

    source = inspect.getsource(routes.update_presentation)
    assert '_load_manageable(db, user, presentation_id, action="edit", for_update=True)' in source
    assert "edit_session=not relink_template," in source
    assert "continued_sessions=continued_sessions," in source
    assert "if (content_saved and not continued_sessions) or sharing_touched:" in source
    assert source.index("content_saved = True") > source.index("await save_presentation(")
    restore = inspect.getsource(routes.restore_presentation_version_endpoint)
    assert 'action="restore", for_update=True)' in restore
    save = inspect.getsource(decks.save_presentation)
    assert save.index("gone = await verify_file_references(") < save.index("await open_edit_session(db, presentation, author_id=editor)")


def test_save_presentation_continues_the_authors_session_and_says_so(monkeypatch):
    from app.models.presentation import VERSION_SOURCE_MANUAL
    from app.services import presentation_versions as versions
    from app.services.presentations import save_presentation

    owner = uuid.uuid4()
    deck = _deck(owner)
    entry = _session_entry(deck, owner, revision=1, started=timedelta(minutes=2), last=timedelta(seconds=3))
    seen: dict = {}

    async def fake_open(db, presentation, *, author_id, now=None):
        seen["author"] = author_id
        return entry

    async def fake_record(db, presentation, **kwargs):
        seen.update(kwargs)
        return entry

    monkeypatch.setattr(versions, "open_edit_session", fake_open)
    monkeypatch.setattr(versions, "record_presentation_version", fake_record)
    continued: list = []
    asyncio.run(
        save_presentation(
            _FakeSession(),
            user_id=owner,
            spec_payload={"title": "Deck", "slides": _slides(2)},
            presentation=deck,
            author_id=owner,
            change_source=VERSION_SOURCE_MANUAL,
            edit_session=True,
            continued_sessions=continued,
        )
    )
    assert seen["author"] == owner
    assert seen["edit_session"] is entry and seen["opens_edit_session"] is True
    assert continued == [entry]


def test_version_serializer_carries_author_name_and_optional_spec():
    from app.services.presentation_versions import serialize_presentation_version

    deck = _deck(uuid.uuid4())
    author = uuid.uuid4()
    version = _version(deck, 3, author_id=author, summary="Edited slides")
    out = serialize_presentation_version(version, author_name="Ada Lovelace")
    assert out["revision"] == 3
    assert out["author_id"] == str(author)
    assert out["author_name"] == "Ada Lovelace"
    assert out["summary"] == "Edited slides"
    assert out["source"] == "agent"
    assert out["updated_at"] is None  # not an edit session
    assert "spec" not in out
    with_spec = serialize_presentation_version(version, author_name=None, include_spec=True)
    assert with_spec["spec"] == version.spec


# ── 2. Comments ──────────────────────────────────────────────────────────────


def test_comment_body_is_normalized_and_bounded():
    from app.models.presentation import COMMENT_MAX_CHARS
    from app.services.presentation_comments import CommentError, normalize_body

    assert normalize_body("  hello \n") == "hello"
    with pytest.raises(CommentError):
        normalize_body("   ")
    with pytest.raises(CommentError):
        normalize_body("x" * (COMMENT_MAX_CHARS + 1))


def test_creating_a_comment_requires_a_known_slide_or_none():
    from app.services.presentation_comments import CommentError, create_comment

    author = uuid.uuid4()
    deck = _deck(author)
    session = _FakeSession()
    made = asyncio.run(create_comment(session, deck, author_id=author, body="Nice", slide_id="s1", parent_id=None))
    assert made in session.added
    assert made.slide_id == "s1"
    assert made.presentation_id == deck.id

    # Deck-level comment: no slide anchor is allowed.
    general = asyncio.run(
        create_comment(session, deck, author_id=author, body="Overall", slide_id=None, parent_id=None)
    )
    assert general.slide_id is None

    with pytest.raises(CommentError):
        asyncio.run(create_comment(session, deck, author_id=author, body="x", slide_id="nope", parent_id=None))


def test_replies_attach_to_a_root_and_inherit_its_slide():
    """Flat threads: a reply inherits its root's slide anchor, a reply to a
    reply is refused, and a parent from another deck is a 404."""
    from app.services.presentation_comments import CommentError, create_comment

    author = uuid.uuid4()
    deck = _deck(author)
    root = _comment(deck, author, slide_id="s2")
    reply = _comment(deck, author, slide_id="s2", parent_id=root.id)

    # Replying to the root.
    session = _FakeSession(results=[_Result(one=root)])
    made = asyncio.run(create_comment(session, deck, author_id=author, body="Agreed", slide_id="s0", parent_id=root.id))
    assert made.parent_id == root.id
    assert made.slide_id == "s2"  # root wins over the caller's slide

    # Replying to a reply is refused (one level of threading).
    session = _FakeSession(results=[_Result(one=reply)])
    with pytest.raises(CommentError) as exc:
        asyncio.run(create_comment(session, deck, author_id=author, body="+1", slide_id=None, parent_id=reply.id))
    assert exc.value.status_code == 400

    # A parent that is not on this deck is a 404 — the lookup is deck-scoped.
    session = _FakeSession(results=[_Result(one=None)])
    with pytest.raises(CommentError) as exc:
        asyncio.run(create_comment(session, deck, author_id=author, body="?", slide_id=None, parent_id=uuid.uuid4()))
    assert exc.value.status_code == 404


def test_only_the_author_or_a_deck_manager_can_manage_a_comment():
    from app.services.presentation_comments import can_manage_comment

    author, stranger = uuid.uuid4(), uuid.uuid4()
    deck = _deck(uuid.uuid4())
    comment = _comment(deck, author)
    assert can_manage_comment(comment, user_id=author, permission="read_only")
    assert not can_manage_comment(comment, user_id=stranger, permission="read_only")
    assert not can_manage_comment(comment, user_id=None, permission=None)
    assert can_manage_comment(comment, user_id=stranger, permission="config")
    assert can_manage_comment(comment, user_id=stranger, permission="owner")


def test_resolving_marks_the_root_thread_and_reopen_clears_it():
    """Resolving is idempotent: a thread already in the requested state keeps
    its stamp (who closed it, when) and the call reports that nothing changed."""
    from app.services.presentation_comments import set_comment_resolved

    author, resolver, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    deck = _deck(author)
    root = _comment(deck, author)
    session = _FakeSession()
    assert asyncio.run(set_comment_resolved(session, root, resolved=False, user_id=resolver)) is False  # already open
    assert root.resolved_at is None and session.flushed == 0
    assert asyncio.run(set_comment_resolved(session, root, resolved=True, user_id=resolver)) is True
    assert root.resolved_at is not None
    assert root.resolved_by_user_id == resolver
    stamped_at = root.resolved_at
    assert asyncio.run(set_comment_resolved(session, root, resolved=True, user_id=other)) is False
    assert root.resolved_at == stamped_at and root.resolved_by_user_id == resolver  # not re-attributed
    assert session.flushed == 1
    assert asyncio.run(set_comment_resolved(session, root, resolved=False, user_id=resolver)) is True
    assert root.resolved_at is None
    assert root.resolved_by_user_id is None


def test_comment_serializer_reports_position_title_and_detached_slides():
    from app.services.presentation_comments import serialize_comment

    author, stranger = uuid.uuid4(), uuid.uuid4()
    deck = _deck(author)
    anchored = _comment(deck, author, slide_id="s1")
    out = serialize_comment(
        anchored,
        spec=deck.spec,
        names={str(author): "Ada"},
        user_id=author,
        permission="read_only",
    )
    assert out["slide_id"] == "s1"
    assert out["slide_position"] == 2  # 1-based, s1 is the second slide
    assert out["slide_title"] == "Slide 1"
    assert out["detached"] is False
    assert out["resolved"] is False
    assert out["can_manage"] is True  # the author
    assert out["author_name"] == "Ada"
    # A read-only stranger sees the note but cannot manage it.
    assert serialize_comment(anchored, spec=deck.spec, user_id=stranger, permission="read_only")["can_manage"] is False

    # The slide was deleted since: the comment survives, flagged detached.
    orphan = _comment(deck, author, slide_id="gone")
    out = serialize_comment(orphan, spec=deck.spec)
    assert out["detached"] is True
    assert out["slide_position"] is None

    general = _comment(deck, author, slide_id=None)
    out = serialize_comment(general, spec=deck.spec)
    assert out["detached"] is False and out["slide_position"] is None


def test_open_comment_digest_for_the_agent_is_bounded_and_skips_resolved():
    from app.services.presentation_comments import format_open_comments_for_agent

    author = uuid.uuid4()
    deck = _deck(author)
    open_root = _comment(deck, author, slide_id="s1")
    reply = _comment(deck, author, slide_id="s1", parent_id=open_root.id)
    reply.body = "Also the subtitle"
    resolved = _comment(deck, author, slide_id="s2", resolved_at=datetime.now(UTC))
    resolved.body = "Already fixed"
    text = format_open_comments_for_agent([open_root, reply, resolved], spec=deck.spec, names={str(author): "Ada"})
    assert "Tighten this headline" in text
    assert "Also the subtitle" in text  # replies ride under their root
    assert "Already fixed" not in text  # resolved threads are not re-raised
    assert "Slide 2" in text  # s1 is the second slide
    assert "[id: s1]" in text  # the stable id the agent edits by
    assert "Ada" in text
    assert format_open_comments_for_agent([resolved], spec=deck.spec) == ""
    # Bounded: a deck with a hundred notes does not flood the tool result.
    many = [_comment(deck, author, slide_id="s0") for _ in range(40)]
    bounded = format_open_comments_for_agent(many, spec=deck.spec, limit=5)
    assert bounded.count("\n- ") == 5
    assert "and 35 more" in bounded


def test_comment_digest_carries_ids_and_lists_resolved_threads_or_one_slide_on_request():
    """The agent's handle on a note is its id: every root and reply the
    digest prints carries the id ``resolve_presentation_comment`` /
    ``add_presentation_comment(reply_to=...)`` take, so closing a note never
    means describing it. ``status`` widens the read to resolved threads,
    ``slide_id`` narrows it to one slide."""
    from app.services.presentation_comments import (
        COMMENT_STATUSES,
        CommentError,
        format_comments_for_agent,
        resolve_slide_anchor,
    )

    author, closer = uuid.uuid4(), uuid.uuid4()
    deck = _deck(author)
    open_root = _comment(deck, author, slide_id="s1")
    reply = _comment(deck, author, slide_id="s1", parent_id=open_root.id)
    reply.body = "Also the subtitle"
    done = _comment(deck, author, slide_id="s2", resolved_at=datetime.now(UTC), resolved_by_user_id=closer)
    done.body = "Already fixed"
    names = {str(author): "Ada", str(closer): "Bob"}
    everything = [open_root, reply, done]

    text = format_comments_for_agent(everything, spec=deck.spec, names=names)
    assert f"[{open_root.id}]" in text and f"[{reply.id}]" in text
    assert f"[{done.id}]" not in text  # default = open threads only
    assert "resolve_presentation_comment" in text  # the header names the closing tool and its condition
    assert "asked for it" in text

    resolved = format_comments_for_agent(everything, spec=deck.spec, names=names, status="resolved")
    assert "Already fixed" in resolved and "Tighten this headline" not in resolved
    assert "resolved by Bob" in resolved

    both = format_comments_for_agent(everything, spec=deck.spec, names=names, status="all")
    assert "Already fixed" in both and "Tighten this headline" in both

    only_s2 = format_comments_for_agent(everything, spec=deck.spec, status="all", slide_id="s2")
    assert "Already fixed" in only_s2 and "Tighten this headline" not in only_s2
    assert format_comments_for_agent(everything, spec=deck.spec, slide_id="s0") == ""

    assert set(COMMENT_STATUSES) == {"open", "resolved", "all"}
    with pytest.raises(CommentError):
        format_comments_for_agent(everything, spec=deck.spec, status="closed")

    # The agent addresses a slide the way the outline shows it (1-based
    # position) or by the stable id the store anchors on; out of range is a
    # precise error, never a note born detached.
    assert resolve_slide_anchor(deck.spec, slide_position=2) == "s1"
    assert resolve_slide_anchor(deck.spec, slide_id="s0") == "s0"
    assert resolve_slide_anchor(deck.spec, slide_id="s0", slide_position=3) == "s0"  # explicit id wins
    assert resolve_slide_anchor(deck.spec) is None
    for bad in ({"slide_position": 0}, {"slide_position": 9}, {"slide_position": "two"}, {"slide_id": "nope"}):
        with pytest.raises(CommentError):
            resolve_slide_anchor(deck.spec, **bad)


def test_comment_digest_caps_replies_per_thread_and_its_total_size():
    """``limit`` bounds the roots; a single long-lived thread must not undo it.
    Only the most recent replies ride under a root (the earlier ones are
    counted), and the whole block stops at a character budget - every bound
    reported as a count, never a silent cut."""
    from app.services.presentation_comments import (
        MAX_DIGEST_CHARS,
        MAX_REPLIES_PER_THREAD,
        format_comments_for_agent,
    )

    author = uuid.uuid4()
    deck = _deck(author)
    root = _comment(deck, author, slide_id="s1")
    replies = []
    for n in range(MAX_REPLIES_PER_THREAD + 3):
        reply = _comment(deck, author, slide_id="s1", parent_id=root.id)
        reply.body = f"Reply number {n}"
        replies.append(reply)

    text = format_comments_for_agent([root, *replies], spec=deck.spec)
    assert "(3 earlier replies not shown)" in text
    assert "Reply number 0" not in text and "Reply number 2" not in text  # the oldest go
    assert "Reply number 3" in text and f"Reply number {MAX_REPLIES_PER_THREAD + 2}" in text  # the latest stay
    assert text.count("reply from") == MAX_REPLIES_PER_THREAD
    assert "(1 earlier reply not shown)" in format_comments_for_agent([root, *replies], spec=deck.spec, max_replies=MAX_REPLIES_PER_THREAD + 2)

    # A total budget: roots past it are counted, and at least one is printed
    # even when the first alone blows the budget, so the agent has an id.
    heavy = []
    for _ in range(6):
        note = _comment(deck, author, slide_id="s0")
        note.body = "x" * 500
        heavy.append(note)
    budgeted = format_comments_for_agent(heavy, spec=deck.spec, max_chars=1200)
    shown = budgeted.count("\n- ")
    assert 1 < shown < 6 and f"and {6 - shown} more" in budgeted
    # The cut is exact: what printed is the shortest prefix past the budget.
    printed = budgeted.split("\n… and ")[0]
    assert len(printed) > 1200
    assert len(printed.rsplit("\n- ", 1)[0]) <= 1200
    tiny = format_comments_for_agent(heavy, spec=deck.spec, max_chars=10)
    assert tiny.count("\n- ") == 1 and "and 5 more" in tiny
    assert MAX_DIGEST_CHARS >= 10_000  # generous by default: a review is normally read whole


def test_comment_messages_speak_the_presentation_kind():
    """A page is never called a slide (the ``unit_noun`` contract): the digest
    labels, the anchor errors and the draft context all follow the kind."""
    from app.services import presentation_spec
    from app.services.presentation_comment_drafts import slide_context
    from app.services.presentation_comments import (
        CommentError,
        create_comment,
        format_comments_for_agent,
        resolve_slide_anchor,
        unknown_slide_error,
        whole_noun,
    )

    assert whole_noun is presentation_spec.whole_noun  # one vocabulary module, next to unit_noun
    author = uuid.uuid4()
    pages = _deck(author)
    pages.spec = {**pages.spec, "aspect_ratio": "A4"}
    anchored = _comment(pages, author, slide_id="s1")
    whole = _comment(pages, author, slide_id=None)
    gone = _comment(pages, author, slide_id="removed")
    text = format_comments_for_agent([anchored, whole, gone], spec=pages.spec)
    assert "Page 2" in text and "Slide 2" not in text
    assert "Whole presentation" in text and "Whole deck" not in text
    assert "Detached (page 'removed' was removed)" in text
    assert whole_noun(pages.spec) == "presentation" and whole_noun(_deck(author).spec) == "deck"

    # One wording for an unknown anchor wherever it is checked (the tools, the
    # REST composer and its draft endpoint), naming the count to fall back on.
    with pytest.raises(CommentError, match="Page 'nope' is not part of this presentation, which has 3 pages") as exc:
        resolve_slide_anchor(pages.spec, slide_id="nope")
    assert exc.value.status_code == 404
    assert str(unknown_slide_error(_deck(author).spec, "nope")) == "Slide 'nope' is not part of this deck, which has 3 slides."
    assert str(unknown_slide_error(_deck(author, slides=1).spec, "x")).endswith("which has 1 slide.")
    with pytest.raises(CommentError, match="Page 'nope' is not part of this presentation, which has 3 pages"):
        asyncio.run(create_comment(_FakeSession(), pages, author_id=author, body="x", slide_id="nope", parent_id=None))
    with pytest.raises(CommentError, match="this presentation has 3 pages"):
        resolve_slide_anchor(pages.spec, slide_position=9)

    context = slide_context(pages.spec, "s1")
    assert "page 2 of 3" in context and "Previous page: Slide 0" in context and "Page content (JSON)" in context
    assert "slide 2 of 3" not in context
    assert "presentation as a whole (3 pages)" in slide_context(pages.spec, None)
    assert "a page that is no longer in the presentation" in slide_context(pages.spec, "removed")


# ── 3. Presence ──────────────────────────────────────────────────────────────


class _FakeRedis:
    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self.expiries: dict[str, int] = {}

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    async def expire(self, key, ttl):
        self.expiries[key] = ttl

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hdel(self, key, *fields):
        bucket = self.hashes.get(key, {})
        for f in fields:
            bucket.pop(f, None)

    async def aclose(self):
        return None


def _patch_redis(monkeypatch, fake):
    from app.services import presentation_presence as mod

    async def _factory():
        return fake

    monkeypatch.setattr(mod, "redis_client", _factory)
    return fake


def test_presence_heartbeat_records_the_user_and_returns_the_room(monkeypatch):
    from app.services import presentation_presence as mod

    fake = _patch_redis(monkeypatch, _FakeRedis())
    deck_id = uuid.uuid4()
    alice, bob = uuid.uuid4(), uuid.uuid4()
    room = asyncio.run(mod.heartbeat_presence(deck_id, user_id=alice, name="Alice", editing=True, slide_index=3))
    assert [e["user_id"] for e in room] == [str(alice)]
    assert room[0]["editing"] is True and room[0]["slide_index"] == 3
    assert room[0]["is_self"] is True
    room = asyncio.run(mod.heartbeat_presence(deck_id, user_id=bob, name="Bob", editing=False, slide_index=0))
    # The caller comes first, then editors, then by name — a stable order so
    # the avatars do not reshuffle on every poll.
    assert [e["name"] for e in room] == ["Bob", "Alice"]
    assert [e["is_self"] for e in room] == [True, False]
    key = mod.presence_key(deck_id)
    assert fake.expiries[key] == mod.PRESENCE_TTL_SECONDS
    # A heartbeat is a Redis write only — never a DB session.
    import inspect

    assert "AsyncSession" not in inspect.getsource(mod)
    assert mod.PRESENCE_TTL_SECONDS > 2 * mod.PRESENCE_HEARTBEAT_SECONDS  # one missed beat is forgiven


def test_presence_says_where_an_editor_works_and_the_room_answers_the_revision(monkeypatch):
    """While someone's editor is open, their entry carries the slide's
    stable id and the selected block, which the others' stages outline; the
    heartbeat route answers the deck's current revision, so an editor holding
    an older one reads the deck again within a beat. Both fields are shapes
    the editor writes - nothing that reads as free text gets in."""
    import inspect

    from pydantic import ValidationError

    from app.api.v1 import presentations as routes
    from app.api.v1.schemas.presentation import PresentationPresenceHeartbeat
    from app.services import presentation_presence as mod

    _patch_redis(monkeypatch, _FakeRedis())
    deck_id = uuid.uuid4()
    alice, bob = uuid.uuid4(), uuid.uuid4()
    asyncio.run(mod.heartbeat_presence(deck_id, user_id=alice, name="Alice", editing=True, slide_id="a1b2c3", block="blocks.2"))
    room = asyncio.run(mod.heartbeat_presence(deck_id, user_id=bob, name="Bob"))
    entries = {entry["name"]: entry for entry in room}
    assert entries["Alice"]["slide_id"] == "a1b2c3"
    assert entries["Alice"]["block"] == "blocks.2"
    assert entries["Bob"]["slide_id"] is None and entries["Bob"]["block"] is None

    assert PresentationPresenceHeartbeat(slide_id="a1b2c3", block="columns.1.0.items.3").block == "columns.1.0.items.3"
    for bad in ({"block": "blocks.2\"] , *"}, {"block": "<img>"}, {"slide_id": "a b"}, {"slide_id": "x" * 65}):
        with pytest.raises(ValidationError):
            PresentationPresenceHeartbeat(**bad)

    source = inspect.getsource(routes.presentation_presence_heartbeat)
    assert "slide_id=body.slide_id" in source and "block=body.block" in source
    assert "revision=presentation.revision" in source
    # A beat every few seconds while someone edits: no spec read, a budget per
    # person and deck, fail-open like the room itself.
    assert "_load_visible(db, user, deck_id, without_spec=True)" in source
    assert source.index("await enforce_rate_limit(") < source.index("await _load_visible(")
    assert "limit=PRESENCE_BEATS_PER_MINUTE" in source and "fail_closed=False" in source


def test_presence_drops_stale_and_departed_users(monkeypatch):
    from app.services import presentation_presence as mod

    fake = _patch_redis(monkeypatch, _FakeRedis())
    deck_id = uuid.uuid4()
    alice, bob = uuid.uuid4(), uuid.uuid4()
    asyncio.run(mod.heartbeat_presence(deck_id, user_id=alice, name="Alice"))
    asyncio.run(mod.heartbeat_presence(deck_id, user_id=bob, name="Bob"))
    # Age Bob's entry past the TTL by rewriting its timestamp: a tab that
    # closed without saying goodbye must not linger in the toolbar.
    key = mod.presence_key(deck_id)
    import json

    entry = json.loads(fake.hashes[key][str(bob)])
    entry["ts"] = (datetime.now(UTC) - timedelta(seconds=mod.PRESENCE_TTL_SECONDS + 5)).timestamp()
    fake.hashes[key][str(bob)] = json.dumps(entry)
    fake.hashes[key]["garbage"] = "{not json"
    room = asyncio.run(mod.list_presence(deck_id))
    assert [e["name"] for e in room] == ["Alice"]
    asyncio.run(mod.leave_presence(deck_id, user_id=alice))
    assert asyncio.run(mod.list_presence(deck_id)) == []


def test_presence_fails_open_when_redis_is_down(monkeypatch):
    from app.services import presentation_presence as mod

    class _Broken:
        async def hset(self, *a, **k):
            raise ConnectionError("redis down")

        async def hgetall(self, *a, **k):
            raise ConnectionError("redis down")

        async def hdel(self, *a, **k):
            raise ConnectionError("redis down")

        async def expire(self, *a, **k):
            raise ConnectionError("redis down")

        async def aclose(self):
            return None

    _patch_redis(monkeypatch, _Broken())
    deck_id = uuid.uuid4()
    assert asyncio.run(mod.heartbeat_presence(deck_id, user_id=uuid.uuid4(), name="A")) == []
    assert asyncio.run(mod.list_presence(deck_id)) == []
    asyncio.run(mod.leave_presence(deck_id, user_id=uuid.uuid4()))  # no raise


def test_presence_activity_is_assistant_over_editing_over_viewing(monkeypatch):
    """An entry carries ONE activity: ``assistant`` when the route saw
    a turn in flight, else ``editing`` when the client holds unsaved edits,
    else ``viewing``. ``editing`` stays the derived boolean the sort and the
    badge key on, so both kinds of editor sort ahead of the readers."""
    from app.services import presentation_presence as mod

    assert mod.resolve_activity() == mod.ACTIVITY_VIEWING
    assert mod.resolve_activity(editing=True) == mod.ACTIVITY_EDITING
    assert mod.resolve_activity(assistant=True) == mod.ACTIVITY_ASSISTANT
    assert mod.resolve_activity(editing=True, assistant=True) == mod.ACTIVITY_ASSISTANT
    assert set(mod.PRESENCE_ACTIVITIES) == {"viewing", "editing", "assistant"}

    _patch_redis(monkeypatch, _FakeRedis())
    deck_id = uuid.uuid4()
    viewer, drafter, studio = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    asyncio.run(mod.heartbeat_presence(deck_id, user_id=viewer, name="Vera"))
    asyncio.run(mod.heartbeat_presence(deck_id, user_id=drafter, name="Dan", editing=True))
    # The client's own "editing" claim does not survive a turn the server saw.
    room = asyncio.run(mod.heartbeat_presence(deck_id, user_id=studio, name="Sam", editing=True, assistant=True))
    by_name = {e["name"]: e for e in room}
    assert by_name["Vera"]["activity"] == "viewing" and by_name["Vera"]["editing"] is False
    assert by_name["Dan"]["activity"] == "editing" and by_name["Dan"]["editing"] is True
    assert by_name["Sam"]["activity"] == "assistant" and by_name["Sam"]["editing"] is True
    # Self first, then whoever is changing the deck (either way), then readers.
    assert [e["name"] for e in room] == ["Sam", "Dan", "Vera"]
    # The next beat without a turn drops the badge: nothing is remembered
    # across beats, so a stuck client state cannot keep it on.
    room = asyncio.run(mod.heartbeat_presence(deck_id, user_id=studio, name="Sam"))
    assert {e["name"]: e["activity"] for e in room}["Sam"] == "viewing"


def test_presence_decodes_entries_written_before_activity_existed(monkeypatch):
    """A room written by a replica still on the old wire format (``editing``
    only) reads back with a derived activity; an unknown activity string falls
    back the same way rather than leaking into the UI."""
    import json

    from app.services import presentation_presence as mod

    fake = _patch_redis(monkeypatch, _FakeRedis())
    deck_id = uuid.uuid4()
    key = mod.presence_key(deck_id)
    now = datetime.now(UTC).timestamp()
    legacy_editor, legacy_reader, odd = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    fake.hashes[key] = {
        str(legacy_editor): json.dumps({"user_id": str(legacy_editor), "name": "Old editor", "editing": True, "ts": now}),
        str(legacy_reader): json.dumps({"user_id": str(legacy_reader), "name": "Old reader", "editing": False, "ts": now}),
        str(odd): json.dumps({"user_id": str(odd), "name": "Odd", "activity": "dancing", "editing": True, "ts": now}),
    }
    room = {e["name"]: e for e in asyncio.run(mod.list_presence(deck_id))}
    assert room["Old editor"]["activity"] == "editing" and room["Old editor"]["editing"] is True
    assert room["Old reader"]["activity"] == "viewing" and room["Old reader"]["editing"] is False
    assert room["Odd"]["activity"] == "editing"


def test_any_turn_active_is_one_exists_over_every_marker(monkeypatch):
    """The presence heartbeat asks about a user's handful of studio chats in
    one round-trip; no conversations means no call at all."""
    import app.core.redis as redis_module
    from app.services import chat_turn_guard as guard

    calls: list[tuple[str, ...]] = []

    class _Redis:
        def __init__(self, live: set[str]):
            self.live = live

        async def exists(self, *keys):
            calls.append(keys)
            return sum(1 for k in keys if k in self.live)

    a, b, c = (str(uuid.uuid4()) for _ in range(3))
    fake = _Redis({guard.processing_key(b)})

    async def _client():
        return fake

    monkeypatch.setattr(redis_module, "redis_client", _client)
    assert asyncio.run(guard.any_turn_active([a, b, c])) is True
    assert calls == [(guard.processing_key(a), guard.processing_key(b), guard.processing_key(c))]
    assert asyncio.run(guard.any_turn_active([a, c])) is False
    calls.clear()
    assert asyncio.run(guard.any_turn_active([])) is False
    assert calls == []


def test_studio_turn_active_reads_the_callers_bound_conversations(monkeypatch):
    """The ``assistant`` activity is the server's reading: the caller's
    conversations bound to the deck (``Conversation.presentation_id``), probed
    through the chat turn marker. No binding, no probe; a probe that fails is
    ``False`` - presence never errors a heartbeat."""
    from app.services import chat_turn_guard as guard
    from app.services import presentation_studio as studio

    deck_id, user_id = uuid.uuid4(), uuid.uuid4()
    conv_a, conv_b = uuid.uuid4(), uuid.uuid4()
    probed: list[list[str]] = []

    async def _any(ids):
        probed.append(list(ids))
        return True

    monkeypatch.setattr(guard, "any_turn_active", _any)

    db = _FakeSession([_Result(rows=[conv_a, conv_b])])
    assert asyncio.run(studio.studio_turn_active(db, presentation_id=deck_id, user_id=user_id)) is True
    assert probed == [[str(conv_a), str(conv_b)]]
    # The query is scoped to THIS deck and THIS user: another viewer's studio
    # chat on the same deck must never light up the caller's badge.
    sql = str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "conversations.presentation_id" in sql and "conversations.user_id" in sql
    assert deck_id.hex in sql and user_id.hex in sql  # literal binds render UUIDs unhyphenated

    probed.clear()
    db = _FakeSession([_Result(rows=[])])
    assert asyncio.run(studio.studio_turn_active(db, presentation_id=deck_id, user_id=user_id)) is False
    assert probed == []

    async def _down(ids):
        raise ConnectionError("redis down")

    monkeypatch.setattr(guard, "any_turn_active", _down)
    db = _FakeSession([_Result(rows=[conv_a])])
    assert asyncio.run(studio.studio_turn_active(db, presentation_id=deck_id, user_id=user_id)) is False


def test_presence_heartbeat_route_reads_the_turn_itself():
    """The route derives ``assistant`` from the turn marker and the wire format
    never lets a client claim it: the heartbeat body has no such field, the
    entry carries ``activity``."""
    import inspect

    from app.api.v1 import presentations as routes
    from app.api.v1.schemas.presentation import PresentationPresenceEntry, PresentationPresenceHeartbeat

    src = inspect.getsource(routes.presentation_presence_heartbeat)
    assert "studio_turn_active(db, presentation_id=presentation.id, user_id=user.id)" in src
    assert "assistant=assistant" in src
    assert "assistant" not in PresentationPresenceHeartbeat.model_fields
    assert PresentationPresenceEntry.model_fields["activity"].default == "viewing"
    assert PresentationPresenceEntry(user_id="u", last_seen_at=0.0).activity == "viewing"
    with pytest.raises(ValueError):
        PresentationPresenceEntry(user_id="u", last_seen_at=0.0, activity="dancing")


# ── 4. Agent surface ─────────────────────────────────────────────────────────


def test_generate_presentation_accepts_a_change_summary_and_get_surfaces_comments(monkeypatch):
    import app.core.database as database
    from app.services.builtin_tools import build_presentation_tools
    from app.tools.base import ToolCall

    owner = uuid.uuid4()
    deck = _deck(owner)
    defs, executor = build_presentation_tools(db=None, user_id=str(owner), conversation_id=None)  # type: ignore[arg-type]
    by_name = {d.name: d for d in defs}
    props = by_name["generate_presentation"].input_schema["properties"]
    assert "change_summary" in props
    assert "comment" in by_name["get_presentation"].description.lower()

    # Edit with a summary: the version row carries it.
    session = _FakeSession(results=[_Result(rows=[]), _Result(one=deck)])
    monkeypatch.setattr(database, "async_session", lambda: session)
    result = asyncio.run(
        executor.execute_tool(
            ToolCall(
                id="c1",
                name="generate_presentation",
                arguments={
                    "presentation_id": str(deck.id),
                    "title": "Deck",
                    "slides": _slides(2),
                    "change_summary": "Addressed the headline comment",
                },
            )
        )
    )
    assert not result.is_error, result.content
    from app.models.presentation import PresentationVersion

    versions = [r for r in session.added if isinstance(r, PresentationVersion)]
    assert versions and versions[0].summary == "Addressed the headline comment"
    assert versions[0].source == "agent"

    # get_presentation lists the open review comments so the agent can act.
    comment = _comment(deck, owner, slide_id="s1")
    session = _FakeSession(
        results=[
            _Result(rows=[]),  # groups
            _Result(one=deck),  # deck
            _Result(rows=[comment]),  # comments
            _Result(rows=[]),  # names
        ]
    )
    monkeypatch.setattr(database, "async_session", lambda: session)
    result = asyncio.run(
        executor.execute_tool(ToolCall(id="c2", name="get_presentation", arguments={"presentation_id": str(deck.id)}))
    )
    assert not result.is_error, result.content
    assert "Tighten this headline" in result.content
    assert "Open review comments" in result.content
    assert f"[{comment.id}]" in result.content  # the id the resolve tool takes


def _comment_tools(owner: uuid.UUID):
    from app.services.builtin_tools import build_presentation_tools
    from app.tools.base import ToolCall

    defs, executor = build_presentation_tools(db=None, user_id=str(owner), conversation_id=None)  # type: ignore[arg-type]

    def run(name: str, **arguments):
        return asyncio.run(executor.execute_tool(ToolCall(id="c", name=name, arguments=arguments)))

    return {d.name: d for d in defs}, run


def test_agent_comment_tools_read_add_and_close_notes_as_the_calling_user(monkeypatch):
    """"implement this comment and close it" is one conversation. The
    three tools run AS THE USER on the same service as the Comments panel —
    the list carries ids, a note is posted under the user's name, a thread is
    closed (optionally with the reply that says what was done) and every write
    is audited ``via: agent``. The closing clause is the tool's own contract:
    its description says when, no system prompt is inflated for it."""
    import app.core.database as database
    from app.models.audit_log import AuditLog
    from app.models.presentation import PresentationComment

    owner = uuid.uuid4()
    deck = _deck(owner)
    by_name, run = _comment_tools(owner)
    for name in ("list_presentation_comments", "add_presentation_comment", "resolve_presentation_comment"):
        assert name in by_name, name
        assert by_name[name].source == "builtin:generate_presentation"
    list_props = by_name["list_presentation_comments"].input_schema["properties"]
    assert list_props["status"]["enum"] == ["open", "resolved", "all"]
    assert "slide_position" in list_props
    assert "slide_id" in list_props  # the same two selectors as add_presentation_comment
    assert set(by_name["add_presentation_comment"].input_schema["required"]) == {"presentation_id", "body"}
    assert set(by_name["resolve_presentation_comment"].input_schema["required"]) == {"presentation_id", "comment_id"}
    assert "ONLY when the user asked" in by_name["resolve_presentation_comment"].description
    assert "generate_presentation" in by_name["resolve_presentation_comment"].description  # save first, then close

    def use(*results):
        session = _FakeSession(results=list(results))
        monkeypatch.setattr(database, "async_session", lambda: session)
        return session

    # Read: the open notes with their ids, narrowed to one slide on request.
    note = _comment(deck, owner, slide_id="s1")
    use(_Result(rows=[]), _Result(one=deck), _Result(rows=[note]), _Result(rows=[]))
    result = run("list_presentation_comments", presentation_id=str(deck.id), slide_position=2)
    assert not result.is_error, result.content
    assert f"[{note.id}]" in result.content and "Tighten this headline" in result.content
    use(_Result(rows=[]), _Result(one=deck), _Result(rows=[note]), _Result(rows=[]))
    result = run("list_presentation_comments", presentation_id=str(deck.id), slide_position=1)
    assert "No open review comments on slide 1" in result.content
    # By the stable id the listings print - and the empty message names the
    # slide by its resolved position, not by an argument it was never given.
    use(_Result(rows=[]), _Result(one=deck), _Result(rows=[note]), _Result(rows=[]))
    result = run("list_presentation_comments", presentation_id=str(deck.id), slide_id="s1")
    assert f"[{note.id}]" in result.content
    use(_Result(rows=[]), _Result(one=deck), _Result(rows=[note]), _Result(rows=[]))
    result = run("list_presentation_comments", presentation_id=str(deck.id), slide_id="s2")
    assert "No open review comments on slide 3" in result.content
    use(_Result(rows=[]), _Result(one=deck), _Result(rows=[note]), _Result(rows=[]))
    assert "out of range" in run("list_presentation_comments", presentation_id=str(deck.id), slide_position=7).content
    assert "status must be one of" in run("list_presentation_comments", presentation_id=str(deck.id), status="x").content

    # Write: a note on slide 2 (1-based, as the outline shows it), as the user.
    session = use(_Result(rows=[]), _Result(one=deck), _Result(one=_user(owner)))
    result = run("add_presentation_comment", presentation_id=str(deck.id), body="The totals disagree", slide_position=2)
    assert not result.is_error, result.content
    created = [r for r in session.added if isinstance(r, PresentationComment)]
    assert len(created) == 1 and created[0].slide_id == "s1" and created[0].author_id == owner
    assert created[0].parent_id is None and created[0].body == "The totals disagree"
    audits = [r for r in session.added if isinstance(r, AuditLog)]
    assert audits and audits[0].action == "create" and audits[0].entity_type == "presentation_comment"
    assert audits[0].details["via"] == "agent" and audits[0].details["slide_id"] == "s1"
    assert session.committed and "slide 2" in result.content and "posted as the user" in result.content

    # A reply inherits its thread's slide (the parent is looked up first).
    session = use(_Result(rows=[]), _Result(one=deck), _Result(one=note), _Result(one=_user(owner)))
    result = run("add_presentation_comment", presentation_id=str(deck.id), body="Done in rev 3", reply_to=str(note.id))
    assert not result.is_error, result.content
    created = [r for r in session.added if isinstance(r, PresentationComment)]
    assert created[0].parent_id == note.id and created[0].slide_id == "s1"
    assert result.content.startswith("Reply [")
    assert "Error" in run("add_presentation_comment", presentation_id=str(deck.id), body="x", reply_to="nope").content

    # Close, with the reply that says what was done: parent lookup for the
    # reply, the actor for the audit row, then the recount of open threads -
    # one SQL COUNT (the badge's ``count_open_comments`` rows), never every
    # comment loaded to count in Python.
    session = use(
        _Result(rows=[]),
        _Result(one=deck),
        _Result(one=note),
        _Result(one=note),
        _Result(one=_user(owner)),
        _Result(rows=[(deck.id, 2)]),
    )
    result = run(
        "resolve_presentation_comment",
        presentation_id=str(deck.id),
        comment_id=str(note.id),
        reply="Fixed in the latest revision",
    )
    assert not result.is_error, result.content
    assert note.resolved_at is not None and note.resolved_by_user_id == owner
    assert "Resolved comment" in result.content and "with your reply" in result.content
    assert "Open review comments left: 2" in result.content
    recount = str(session.statements[-1]).lower()
    assert "count(" in recount and "group by" in recount, recount
    assert "resolved_at is null" in recount and "parent_id is null" in recount  # open ROOTS only
    replies = [r for r in session.added if isinstance(r, PresentationComment)]
    assert replies and replies[0].parent_id == note.id and replies[0].body == "Fixed in the latest revision"
    audits = [r for r in session.added if isinstance(r, AuditLog)]
    assert audits[0].action == "update"
    assert audits[0].details == {
        "via": "agent",
        "presentation_id": str(deck.id),
        "fields": ["resolved"],
        "replied": True,
    }
    assert session.committed

    # Reopen: `resolved=false`, also when the model spells the boolean as text.
    use(_Result(rows=[]), _Result(one=deck), _Result(one=note), _Result(one=_user(owner)), _Result(rows=[(deck.id, 1)]))
    result = run("resolve_presentation_comment", presentation_id=str(deck.id), comment_id=str(note.id), resolved="false")
    assert not result.is_error, result.content
    assert note.resolved_at is None and "Reopened" in result.content and "left: 1" in result.content

    # A reply is not a thread: the service's rule reaches the tool unchanged,
    # also with a closing reply, which is not posted on the way there (it would
    # be a nested reply, and that error would mask the one that matters).
    child = _comment(deck, owner, slide_id="s1", parent_id=note.id)
    session = use(_Result(rows=[]), _Result(one=deck), _Result(one=child))
    result = run("resolve_presentation_comment", presentation_id=str(deck.id), comment_id=str(child.id), reply="Done")
    assert "Resolve the thread, not a reply" in result.content and not session.committed
    assert session.added == []


def test_agent_cannot_close_a_note_its_user_could_not_close_by_hand(monkeypatch):
    """A read-only grantee's assistant may comment and reply, never resolve
    someone else's thread — `can_manage_comment` decides for both the panel and
    the tool, so the two cannot drift."""
    import app.core.database as database
    from app.models.presentation import PresentationComment

    owner, viewer = uuid.uuid4(), uuid.uuid4()
    deck = _deck(owner, shared_user_ids=[str(viewer)], shared_user_permissions={str(viewer): "read_only"})
    note = _comment(deck, owner, slide_id="s1")
    _by_name, run = _comment_tools(viewer)

    session = _FakeSession(results=[_Result(rows=[]), _Result(one=deck), _Result(one=note)])
    monkeypatch.setattr(database, "async_session", lambda: session)
    result = run("resolve_presentation_comment", presentation_id=str(deck.id), comment_id=str(note.id))
    assert "only the comment's author or whoever manages" in result.content
    assert note.resolved_at is None and not session.committed and session.added == []

    # ...but their own note is theirs to close.
    mine = _comment(deck, viewer, slide_id="s1")
    session = _FakeSession(
        results=[_Result(rows=[]), _Result(one=deck), _Result(one=mine), _Result(one=_user(viewer)), _Result(rows=[])]
    )
    monkeypatch.setattr(database, "async_session", lambda: session)
    result = run("resolve_presentation_comment", presentation_id=str(deck.id), comment_id=str(mine.id))
    assert not result.is_error, result.content
    assert mine.resolved_by_user_id == viewer and session.committed

    # And a deck the user cannot see is not a deck at all to the tool.
    session = _FakeSession(results=[_Result(rows=[]), _Result(one=None)])
    monkeypatch.setattr(database, "async_session", lambda: session)
    result = run("add_presentation_comment", presentation_id=str(deck.id), body="hi")
    assert "you can access was found" in result.content
    assert not any(isinstance(r, PresentationComment) for r in session.added)


def test_resolving_an_already_resolved_thread_changes_nothing(monkeypatch):
    """Idempotent on both paths. A retried tool call (the first result never
    reached the model) or a thread a reviewer closed meanwhile must not move
    the closing onto the retrier, post the reply a second time or write a
    second audit row: the tool says so and points at `add_presentation_comment`
    for the reply; the REST PATCH returns the unchanged comment with no audit."""
    import app.core.database as database
    from app.api.v1 import presentations as api
    from app.api.v1.schemas.presentation import PresentationCommentUpdate

    owner, closer = uuid.uuid4(), uuid.uuid4()
    deck = _deck(owner)
    note = _comment(deck, owner, slide_id="s1", resolved_at=datetime.now(UTC), resolved_by_user_id=closer)
    stamped_at = note.resolved_at
    _by_name, run = _comment_tools(owner)

    # Tool: nothing written, the count still answers, the reply is redirected.
    session = _FakeSession(results=[_Result(rows=[]), _Result(one=deck), _Result(one=note), _Result(rows=[(deck.id, 2)])])
    monkeypatch.setattr(database, "async_session", lambda: session)
    result = run("resolve_presentation_comment", presentation_id=str(deck.id), comment_id=str(note.id), reply="Done")
    assert not result.is_error, result.content
    assert "is already resolved; nothing changed" in result.content
    assert f'add_presentation_comment with reply_to="{note.id}"' in result.content
    assert "Open review comments left: 2" in result.content
    assert note.resolved_at == stamped_at and note.resolved_by_user_id == closer
    assert session.added == [] and not session.committed
    # ...and the mirror image: reopening a thread that is open.
    fresh = _comment(deck, owner, slide_id="s1")
    session = _FakeSession(results=[_Result(rows=[]), _Result(one=deck), _Result(one=fresh), _Result(rows=[])])
    monkeypatch.setattr(database, "async_session", lambda: session)
    result = run("resolve_presentation_comment", presentation_id=str(deck.id), comment_id=str(fresh.id), resolved=False)
    assert "is already open; nothing changed" in result.content and session.added == []

    # REST: the same request twice writes one audit row, keeps the first stamp.
    audits: list[dict] = []

    async def _load(db, user, presentation_id, comment_id):
        return deck, fresh, "owner"

    async def _audit(db, **kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(api, "_load_comment_for_write", _load)
    monkeypatch.setattr(api, "log_audit", _audit)
    for _ in range(2):
        session = _FakeSession()
        response = asyncio.run(
            api.update_presentation_comment_endpoint(
                str(deck.id),
                str(fresh.id),
                PresentationCommentUpdate(resolved=True),
                request=None,  # type: ignore[arg-type]
                user=_user(owner),  # type: ignore[arg-type]
                db=session,  # type: ignore[arg-type]
            )
        )
        assert response.resolved is True and session.committed
    assert len(audits) == 1 and audits[0]["details"]["fields"] == ["resolved"]
    assert audits[0]["summary"].startswith("Resolved a comment")
    assert fresh.resolved_by_user_id == owner
    # An edit that also resolves an already-resolved thread audits the edit only.
    session = _FakeSession()
    asyncio.run(
        api.update_presentation_comment_endpoint(
            str(deck.id),
            str(fresh.id),
            PresentationCommentUpdate(body="Sharper headline", resolved=True),
            request=None,  # type: ignore[arg-type]
            user=_user(owner),  # type: ignore[arg-type]
            db=session,  # type: ignore[arg-type]
        )
    )
    assert len(audits) == 2 and audits[1]["details"]["fields"] == ["body"]
    assert audits[1]["summary"].startswith("Edited a comment")


def test_resolving_locks_the_thread_row_so_racing_resolves_stamp_it_once(monkeypatch):
    """Two first-time resolves of one thread in flight at once: the first to
    reach the row stamps it, the second waits on that commit, reads the thread
    resolved and is the no-op above — one closing, one audit row, never two.
    The lock is the `FOR UPDATE` on the comment lookup, on BOTH paths (the tool
    and the REST `PATCH` / `DELETE` loader); a read, such as a reply's parent
    lookup, takes none."""
    import app.core.database as database
    from app.api.v1 import presentations as api

    def locked(statement) -> bool:
        return " for update" in str(statement).lower()

    owner = uuid.uuid4()
    deck = _deck(owner)
    note = _comment(deck, owner, slide_id="s1")
    _by_name, run = _comment_tools(owner)

    # Tool: exactly one locked statement, the comment lookup.
    session = _FakeSession(
        results=[_Result(rows=[]), _Result(one=deck), _Result(one=note), _Result(one=_user(owner)), _Result(rows=[])]
    )
    monkeypatch.setattr(database, "async_session", lambda: session)
    result = run("resolve_presentation_comment", presentation_id=str(deck.id), comment_id=str(note.id))
    assert not result.is_error, result.content
    held = [s for s in session.statements if locked(s)]
    assert len(held) == 1 and "presentation_comments" in str(held[0]).lower()

    # REST: the same lookup through `_load_comment_for_write`.
    async def _visible(db, user, presentation_id):
        return deck, [], "owner"

    monkeypatch.setattr(api, "_load_visible", _visible)
    session = _FakeSession(results=[_Result(one=note)])
    asyncio.run(api._load_comment_for_write(session, _user(owner), str(deck.id), str(note.id)))  # type: ignore[arg-type]
    assert len(session.statements) == 1 and locked(session.statements[0])

    # A reply's parent lookup is a read: no lock.
    session = _FakeSession(results=[_Result(rows=[]), _Result(one=deck), _Result(one=note), _Result(one=_user(owner))])
    monkeypatch.setattr(database, "async_session", lambda: session)
    result = run("add_presentation_comment", presentation_id=str(deck.id), body="Done", reply_to=str(note.id))
    assert not result.is_error, result.content
    assert not any(locked(s) for s in session.statements)


# ── 4b. AI-drafted notes ─────────────────────────────────────────────


def test_comment_draft_is_built_from_the_deck_not_the_client():
    """The composer's "Generate with AI" sends a brief and an anchor; the slide
    under review, its neighbours and the thread being answered are read from
    the deck server-side, so the model drafts about THIS slide."""
    from app.services.presentation_comment_drafts import build_draft_messages, clean_draft, slide_context

    deck = _deck(uuid.uuid4(), slides=3)
    deck.spec["slides"][1]["title"] = "Revenue by region"
    context = slide_context(deck.spec, "s1")
    assert "slide 2 of 3" in context and "Revenue by region" in context
    assert "Previous slide: Slide 0" in context and "Next slide: Slide 2" in context
    assert '"layout":"title"' in context  # the slide's own content, as JSON
    assert "slide that is no longer in the deck" in slide_context(deck.spec, "gone")
    whole = slide_context(deck.spec, None)
    assert "deck as a whole (3 slides)" in whole and "2. [title] Revenue by region" in whole

    messages = build_draft_messages(
        spec=deck.spec,
        deck_title="Q3 review",
        slide_id="s1",
        brief="ask for the source of the numbers",
        current_text="Where do these numbers come from",
        thread=[("Ada", "Tighten this headline"), ("Bob", "Agreed")],
    )
    assert [m.role for m in messages] == ["system", "user"]
    prompt = messages[1].content
    assert "Q3 review" in prompt and "Revenue by region" in prompt
    assert "ask for the source of the numbers" in prompt
    assert "Where do these numbers come from" in prompt and "improve it" in prompt
    assert "REPLY in an existing thread" in prompt and "Ada: Tighten this headline" in prompt
    assert "no markdown" in messages[0].content.lower()

    # A long-lived thread is quoted by its root and its most recent replies;
    # the rest is a count, so the request never grows with the history.
    from app.services.presentation_comment_drafts import THREAD_MAX_QUOTED_MESSAGES

    long_thread = [("Ada", "Tighten this headline")] + [("Bob", f"Reply {n}") for n in range(20)]
    long_prompt = build_draft_messages(
        spec=deck.spec, deck_title="Q3 review", slide_id="s1", brief="answer Bob", thread=long_thread
    )[1].content
    skipped = 20 - (THREAD_MAX_QUOTED_MESSAGES - 1)
    assert f"({skipped} earlier replies not quoted)" in long_prompt
    assert "Ada: Tighten this headline" in long_prompt  # the root always
    assert "Bob: Reply 0" not in long_prompt and "Bob: Reply 19" in long_prompt
    assert long_prompt.count("- Bob: Reply") == THREAD_MAX_QUOTED_MESSAGES - 1

    # A model's habits are undone: fences, wrapping quotes, a "Comment:" label,
    # and the note is bounded by the comment column's own limit.
    assert clean_draft('```\n"Comment: Please cite the source."\n```') == "Please cite the source."
    assert clean_draft("  Note: Tighten this.  ") == "Tighten this."
    assert len(clean_draft("x" * 5000)) == 4000
    assert clean_draft(None) == ""


def test_comment_draft_endpoint_is_deck_scoped_ai_gated_and_bounded():
    import inspect

    from app.api.deps import require_ai_enabled
    from app.api.v1 import presentations as api
    from app.api.v1.schemas.presentation import PresentationCommentDraftRequest
    from app.models.presentation import COMMENT_MAX_CHARS
    from app.services.presentation_comment_drafts import DRAFT_BRIEF_MAX_CHARS, CommentDraftError, draft_comment

    paths = {(route.path, m) for route in api.router.routes for m in getattr(route, "methods", set())}
    assert ("/{presentation_id}/comments/draft", "POST") in paths
    source = inspect.getsource(api.draft_presentation_comment_endpoint)
    assert "require_ai_enabled" in source and require_ai_enabled is not None
    assert "_load_visible" in source  # read access is enough — drafting IS reviewing
    assert "CommentDraftError" in source and "BAD_GATEWAY" in source

    fields = PresentationCommentDraftRequest.model_fields
    assert fields["brief"].metadata and any(
        getattr(m, "max_length", None) == DRAFT_BRIEF_MAX_CHARS for m in fields["brief"].metadata
    )
    assert any(getattr(m, "max_length", None) == COMMENT_MAX_CHARS for m in fields["current_text"].metadata)

    class _Reply:
        def __init__(self, content):
            self.content = content

    class _LLM:
        def __init__(self, content=None, error: Exception | None = None):
            self.content, self.error, self.calls = content, error, []

        async def chat(self, messages, **kwargs):
            self.calls.append((messages, kwargs))
            if self.error:
                raise self.error
            return _Reply(self.content)

    deck = _deck(uuid.uuid4())
    llm = _LLM("Consider naming the data source under the chart.")
    draft = asyncio.run(
        draft_comment(llm, "gpt-x", spec=deck.spec, deck_title="Deck", slide_id="s1", brief="cite the source")
    )
    assert draft.body == "Consider naming the data source under the chart."
    assert draft.truncated is False
    assert llm.calls[0][1]["model"] == "gpt-x" and llm.calls[0][1]["max_tokens"] <= 1000
    for broken in (_LLM("   "), _LLM(error=RuntimeError("provider down"))):
        with pytest.raises(CommentDraftError):
            asyncio.run(draft_comment(broken, None, spec=deck.spec, deck_title="Deck", slide_id=None, brief="x"))


# ── 5. API surface + wiring tripwires ────────────────────────────────────────


def test_collaboration_routes_exist_and_are_scoped_to_a_deck():
    from app.api.v1 import presentations as api

    paths = {(route.path, m) for route in api.router.routes for m in getattr(route, "methods", set())}
    expected = {
        ("/{presentation_id}/versions", "GET"),
        ("/{presentation_id}/versions/{revision}", "GET"),
        ("/{presentation_id}/versions/{revision}/restore", "POST"),
        ("/{presentation_id}/comments", "GET"),
        ("/{presentation_id}/comments", "POST"),
        ("/{presentation_id}/comments/draft", "POST"),
        ("/{presentation_id}/comments/{comment_id}", "PATCH"),
        ("/{presentation_id}/comments/{comment_id}", "DELETE"),
        # Presence has no GET on purpose: the heartbeat POST returns the room,
        # so a viewer's poll is one request, not two.
        ("/{presentation_id}/presence", "POST"),
        ("/{presentation_id}/presence", "DELETE"),
    }
    assert expected <= paths
    assert ("/{presentation_id}/presence", "GET") not in paths
    # Every collaboration route hangs off a deck id: none is a global listing.
    for path, _m in expected:
        assert path.startswith("/{presentation_id}/")


def test_list_and_detail_responses_carry_the_collaboration_fields():
    from app.api.v1.schemas.presentation import PresentationResponse, PresentationSummaryResponse

    for schema in (PresentationResponse, PresentationSummaryResponse):
        fields = set(schema.model_fields)
        assert {"updated_by_user_id", "updated_by_name", "open_comment_count"} <= fields, schema


def test_models_are_registered_and_the_migration_is_lock_safe():
    """New tables reach ``create_all`` on fresh installs AND the migration on
    existing ones; the migration follows the mandatory lock-safety recipe."""
    import app.models  # noqa: F401 — the centralized registry imports every model module
    from app.core.database import Base

    assert {"presentation_versions", "presentation_comments"} <= set(Base.metadata.tables)
    assert "updated_by_user_id" in Base.metadata.tables["presentations"].c
    migration = next((BACKEND_ROOT / "alembic" / "versions").glob("*presentation_collaboration*.py")).read_text(
        encoding="utf-8"
    )
    assert "terminate_lock_holders" in migration
    assert "set_lock_timeout" in migration
    assert "IF NOT EXISTS" in migration
    assert "CONCURRENTLY" in migration
    assert "presentation_versions" in migration and "presentation_comments" in migration
    assert "updated_by_user_id" in migration


def test_frontend_wires_comments_history_presence_and_present():
    """The UI half of the contract:

    * a ``Present`` button on the toolbar (quick fullscreen), never hidden in
      the overflow menu;
    * ``Open in studio`` on the chat card, landing on the viewer with the
      studio rail out (the label moved from ``Open in editor`` when the studio
      became the editing surface — see ``test_presentation_studio.py``);
    * comments + history panels and presence avatars reachable from the viewer.
    """
    src = FRONTEND_ROOT / "src"
    toolbar = (src / "components" / "presentation" / "DeckToolbar.tsx").read_text(encoding="utf-8")
    assert "onPresent" in toolbar
    assert "onToggleComments" in toolbar
    assert "onToggleHistory" in toolbar
    assert "PresenceAvatars" in toolbar

    # The card hands both stages an `onOpenEditor`, and the label is drawn by
    # the toolbars (deck and page alike) — not by a card-local button, so the
    # affordance sits where the other actions are.
    card = (src / "components" / "presentation" / "PresentationCard.tsx").read_text(encoding="utf-8")
    assert "onOpenEditor" in card
    assert re.search(r"/presentations/\$\{", card), "the card must deep-link to the viewer"
    page_toolbar = (src / "components" / "presentation" / "PageToolbar.tsx").read_text(encoding="utf-8")
    for bar in (toolbar, page_toolbar):
        assert 't_i18n("Open in studio")' in bar

    viewer = (src / "pages" / "PresentationViewer.tsx").read_text(encoding="utf-8")
    for token in ("PresentationCommentsPanel", "PresentationHistoryPanel", "usePresentationPresence"):
        assert token in viewer, token

    hooks = (src / "hooks" / "api" / "usePresentationApi.ts").read_text(encoding="utf-8")
    for hook in (
        "usePresentationComments",
        "useCreatePresentationComment",
        "useUpdatePresentationComment",
        "useDeletePresentationComment",
        "useDraftPresentationComment",
        "usePresentationVersions",
        "usePresentationVersion",
        "useRestorePresentationVersion",
        "usePresentationPresence",
    ):
        assert f"export function {hook}" in hooks, hook

    # Presence heartbeats must be paced by the backend's interval constant so a
    # shorter client interval can never hammer Redis.
    from app.services.presentation_presence import PRESENCE_HEARTBEAT_SECONDS

    assert f"PRESENCE_HEARTBEAT_MS = {PRESENCE_HEARTBEAT_SECONDS}_000" in hooks or (
        f"PRESENCE_HEARTBEAT_MS = {PRESENCE_HEARTBEAT_SECONDS * 1000}" in hooks
    )


def test_frontend_presence_says_editing_only_for_a_change_on_its_way():
    """The UI half of the presence contract. "Julien is editing" sat on a colleague's avatar for
    fifteen minutes with no revision in History because the viewer heartbeated
    ``editing`` for an OPEN studio rail. The contract now:

    * the viewer's ``editing`` is a change on its way or just made - since the
      editor autosaves, the autosave's ``active`` (changes pending or
      being saved, or saved moments ago, so the badge does not flicker with
      every save) - never the rail's or the panel's visibility;
    * the rail reports its in-flight turn (``onBusyChange`` from
      ``chatWs.isSending``) and the viewer hands it to the presence hook, which
      only uses it to TIME a beat - the flag is never sent, the server reads
      the turn marker itself;
    * the avatars key on ``activity``: a distinct badge and tooltip for an
      assistant turn, the pencil for manual edits, nothing for an open panel;
    * the new tooltip exists in every catalog.
    """
    src = FRONTEND_ROOT / "src"
    viewer = (src / "pages" / "PresentationViewer.tsx").read_text(encoding="utf-8")
    assert "editing: editing || studioOpen" not in viewer, "an open rail is not editing"
    assert re.search(r"editing:\s*autosave\.active,", viewer), "editing = a change on its way or just saved"
    assert re.search(r"assistantBusy:\s*studioBusy", viewer)
    assert "onBusyChange={setStudioBusy}" in viewer
    autosave = (src / "components" / "presentation" / "useDeckAutosave.ts").read_text(encoding="utf-8")
    assert "active: enabled && (dirty || saving || lingering || !!editor?.editingText)" in autosave, (
        "active = pending, saving, saved moments ago, or typing in place - never an open panel"
    )

    rail = "".join(
        (src / "components" / part).read_text(encoding="utf-8")
        for part in ("presentation/PresentationStudioRail.tsx", "studio/StudioRail.tsx")
    )
    assert "onBusyChange?: (busy: boolean) => void" in rail
    assert re.search(r"const busy = frames\.working;", rail), "the turn in flight, not an upload"
    assert "onBusyChange?.(busy)" in rail
    assert "() => onBusyChange?.(false)" in rail, "a collapsed rail must not leave a stale true"

    hooks = (src / "hooks" / "api" / "usePresentationApi.ts").read_text(encoding="utf-8")
    assert "assistantBusy" in hooks and "PRESENCE_ANNOUNCE_SETTLE_MS" in hooks
    body = re.search(r"api\.post(?:<\w+>)?\(`/presentations/\$\{id\}/presence`,\s*\{(.*?)\}\)", hooks, re.S)
    assert body, "the heartbeat body"
    assert "editing:" in body.group(1) and "slide_index:" in body.group(1)
    assert "assistant" not in body.group(1), "the client never claims the assistant activity"

    avatars = (src / "components" / "presentation" / "PresenceAvatars.tsx").read_text(encoding="utf-8")
    assert 'viewer.activity === "assistant"' in avatars
    assert 'viewer.activity === "editing"' in avatars
    assert 'viewer.activity !== "viewing"' in avatars, "the badge keys on the activity"
    assert "Sparkles" in avatars and "Pencil" in avatars

    types = (src / "types" / "index.ts").read_text(encoding="utf-8")
    assert 'export type PresentationPresenceActivity = "viewing" | "editing" | "assistant";' in types
    assert "activity: PresentationPresenceActivity;" in types

    import json

    for lang in ("en", "fr", "es", "ja"):
        catalog = json.loads((src / "i18n" / "lang" / f"{lang}.json").read_text(encoding="utf-8"))
        assert "{name} is editing with an assistant" in catalog, lang
        assert "{name}" in catalog["{name} is editing with an assistant"], lang
