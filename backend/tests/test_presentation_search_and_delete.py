"""Searching a deck's text without changing it, and deleting a deck from a chat.

Two gaps of the deck tool surface:

* ``search_presentation_text`` answers "does this word still appear anywhere?"
  as a read. Before it, an agent probed with ``edit_presentation``
  ``replace_text`` and an identical find / replace, the tool refused each
  probe, and a few refusals withdrew every tool of the turn. The search walks
  exactly the fields ``replace_text`` touches (one walker), names the slide,
  the block address and the field of each occurrence, and covers every slide
  of a deck too long for one ``get_presentation`` call.
* ``delete_presentation`` removes a deck its owner no longer wants (the copy
  made with ``duplicate_presentation``, typically) through the library's own
  delete, for the owner only.

No database: the tools open their own short-lived session, replaced here by a
scripted fake - the style of ``test_presentation_patch.py``. The cascades and
the "no longer listed or openable" round trip run against PostgreSQL in
``test_presentation_delete_postgres.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.services import presentation_patch as pp
from app.services.presentation_patch import (
    PresentationPatchError,
    PresentationPatchNoChangeError,
    apply_operations,
    search_text,
)
from app.services.presentation_spec import MAX_SLIDE_CAP, validate_spec

# ── Fixtures ─────────────────────────────────────────────────────────────────


def _search_deck(extra_slides: int = 0) -> dict:
    """A validated deck with the word "core" in every kind of text field.

    Slide 1: the title and the notes. Slide 2: a columns slide (column 2).
    Slide 3: a comparison block (one side's label and one side's block) and a
    columns BLOCK. Slide 4: a dashboard panel's title and its chart's caption.
    Slide 5: a table cell, a callout's child, and "scores" (which "core" must
    not match as a whole word). The deck's footer text says it too.
    """
    slides: list[dict] = [
        {
            "layout": "title",
            "title": "Core platform review",
            "subtitle": "Q3 2026",
            "notes": "Open with the core message.",
        },
        {
            "layout": "content",
            "title": "Where we stand",
            "columns": [
                [{"type": "text", "content": "EMEA strong"}],
                [{"type": "text", "content": "The core team grew"}],
            ],
        },
        {
            "layout": "content",
            "title": "Options",
            "blocks": [
                {
                    "type": "comparison",
                    "left": {"label": "Today", "blocks": [{"type": "text", "content": "Manual core checks"}]},
                    "right": {"label": "Core next", "blocks": [{"type": "text", "content": "Automated"}]},
                },
                {
                    "type": "columns",
                    "columns": [
                        [{"type": "text", "content": "Left side"}],
                        [{"type": "text", "content": "A core idea"}],
                    ],
                },
            ],
        },
        {
            "layout": "dashboard",
            "title": "KPIs",
            "tiles": [{"value": "EUR 12.3M", "label": "ARR"}, {"value": "109%", "label": "NRR"}],
            "panels": [
                {
                    "title": "Core ARR",
                    "block": {
                        "type": "chart",
                        "kind": "bar",
                        "categories": ["Q2", "Q3"],
                        "series": [{"name": "ARR", "values": [6.7, 12.3]}],
                        "caption": "Source: the core ledger",
                    },
                }
            ],
        },
        {
            "layout": "content",
            "title": "Scores",
            "blocks": [
                {"type": "table", "headers": ["Area", "Score"], "rows": [["Edge", "7"], ["Core", "9"]]},
                {
                    "type": "callout",
                    "tone": "success",
                    "title": "High scores",
                    "children": [{"type": "text", "content": "Everything at the CORE improved."}],
                },
            ],
            "notes": "Mention the scores.",
        },
    ]
    for index in range(extra_slides):
        slides.append(
            {
                "layout": "content",
                "title": f"Appendix {index + 1}",
                "blocks": [{"type": "text", "content": f"Filler paragraph {index + 1}. " * 25}],
            }
        )
    return validate_spec(
        {"title": "Quarterly review", "chrome": {"footer_text": "Core team - internal"}, "slides": slides},
        slide_cap=MAX_SLIDE_CAP,
    )


def _places(matches) -> list[tuple]:
    return [(m.slide, m.block, m.field, m.text) for m in matches]


# ── The engine ───────────────────────────────────────────────────────────────


#: Every whole-word "core" of :func:`_search_deck`, as (slide, block, field, text).
_WHOLE_WORD_CORE = [
    (1, None, "notes", "core"),
    (1, None, "title", "Core"),
    (2, "2.1", "content", "core"),
    (3, "1.left.1", "content", "core"),
    (3, "1", "right.label", "Core"),
    (3, "2.2.1", "content", "core"),
    (4, "1", "title", "Core"),
    (4, "1", "caption", "core"),
    (5, "1", "rows[1][0].text", "Core"),
    (5, "2.1", "content", "CORE"),
    (None, None, "chrome.footer_text", "Core"),
]


def test_search_names_the_slide_the_block_and_the_field_of_every_occurrence():
    spec = _search_deck()
    matches = search_text(spec, "core", whole_word=True)
    assert _places(matches) == _WHOLE_WORD_CORE
    title = matches[1]
    assert (title.slide_id, title.layout) == (spec["slides"][0]["id"], "title")
    assert title.snippet == "«Core» platform review"
    assert matches[0].snippet == "Open with the «core» message."
    assert matches[-1].slide_id is None and matches[-1].layout is None


@pytest.mark.parametrize(
    ("query", "where"),
    [
        ("platform review", (1, None, "title")),
        ("core team grew", (2, "2.1", "content")),
        ("Core next", (3, "1", "right.label")),
        ("Manual core", (3, "1.left.1", "content")),
        ("Core ARR", (4, "1", "title")),
        ("core ledger", (4, "1", "caption")),
        ("core message", (1, None, "notes")),
        ("Mention the scores", (5, None, "notes")),
        ("Edge", (5, "1", "rows[0][0].text")),
        ("Area", (5, "1", "headers[0].text")),
        ("NRR", (4, None, "tiles[1].label")),
    ],
)
def test_one_query_per_kind_of_text_field(query, where):
    """A title, a block in a column, a comparison side, a dashboard panel, the
    notes, a table cell: each is found, and addressed the way edit_presentation
    addresses it. (Whole words: "Edge" is also inside "ledger".)"""
    matches = search_text(_search_deck(), query, whole_word=True)
    assert [(m.slide, m.block, m.field) for m in matches] == [where]


def test_case_sensitivity():
    spec = _search_deck()
    capitalised = search_text(spec, "Core", case_sensitive=True, whole_word=True)
    assert [(m.slide, m.field) for m in capitalised] == [
        (1, "title"),
        (3, "right.label"),
        (4, "title"),
        (5, "rows[1][0].text"),
        (None, "chrome.footer_text"),
    ]
    assert [(m.slide, m.block) for m in search_text(spec, "CORE", case_sensitive=True)] == [(5, "2.1")]
    assert {m.text for m in search_text(spec, "core", case_sensitive=True, whole_word=True)} == {"core"}
    # Case-insensitive finds every casing, reported as stored.
    assert {m.text for m in search_text(spec, "core", whole_word=True)} == {"core", "Core", "CORE"}


def test_whole_word_never_matches_inside_a_longer_word():
    """ "core" is inside "Scores", "Score" and "scores" on slide 5: a substring
    search finds those four, a whole-word search none of them."""
    spec = _search_deck()
    substring = search_text(spec, "core")
    whole = search_text(spec, "core", whole_word=True)
    inside = [m for m in substring if (m.slide, m.block, m.field, m.text) not in _WHOLE_WORD_CORE]
    assert [(m.slide, m.block, m.field) for m in inside] == [
        (5, None, "notes"),
        (5, None, "title"),
        (5, "1", "headers[1].text"),
        (5, "2", "title"),
    ]
    assert len(substring) == len(whole) + 4
    assert [m.field for m in search_text(spec, "Scores", case_sensitive=True, whole_word=True)] == ["title"]


def test_whole_word_on_a_query_that_ends_with_punctuation():
    spec = validate_spec({"title": "Deck", "slides": [{"layout": "title", "title": "Results Q3: strong"}]})
    assert [m.text for m in search_text(spec, "Q3:", whole_word=True)] == ["Q3:"]
    assert search_text(spec, "Q", whole_word=True) == []


def test_zero_results_is_an_empty_list():
    assert search_text(_search_deck(), "kubernetes") == []


def test_structure_is_never_searched():
    """Ids, layouts, block types, icons, tones and file ids are strings too,
    and never text: the same rule as replace_text."""
    file_id = str(uuid.uuid4())
    spec = validate_spec(
        {
            "title": "Deck",
            "slides": [
                {
                    "layout": "content",
                    "title": "Diagram",
                    "blocks": [
                        {"type": "image", "file_id": file_id},
                        {"type": "callout", "tone": "success", "icon": "rocket", "title": "Win"},
                    ],
                }
            ],
        }
    )
    for query in ("content", "image", "callout", "success", "rocket", file_id, spec["slides"][0]["id"]):
        assert search_text(spec, query, case_sensitive=True) == [], query


def test_search_finds_exactly_what_replace_text_counts():
    """The tripwire behind "covers every field replace_text would touch": one
    walker, so the case-sensitive substring search and replace_text's count
    agree on every query - and replace_text with that count succeeds."""
    spec = _search_deck()
    for query in ("core", "Core", "CORE", "e", "Q", "scores", " ", "ARR", "-"):
        found = len(search_text(spec, query, case_sensitive=True))
        counted = sum(pp._count_strings(slide, query, None) for slide in spec["slides"])
        counted += sum(pp._count_strings(spec[name], query, name) for name in pp.TEXT_DECK_FIELDS if name in spec)
        assert found == counted, query
        if found:
            out = apply_operations(spec, [{"op": "replace_text", "find": query, "replace": query + "#", "count": found}])
            assert out.replacements == found, query


def test_search_changes_nothing():
    spec = _search_deck()
    before = pp.canonical(spec)
    search_text(spec, "core", whole_word=True)
    assert pp.canonical(spec) == before


def test_an_empty_query_is_refused():
    with pytest.raises(PresentationPatchError, match="non-empty"):
        search_text(_search_deck(), "")


def test_replace_text_with_find_equal_to_replace_points_at_the_search():
    """A probe is not an edit: answered as a no-change (never a failure the
    brake counts) that names search_presentation_text."""
    spec = _search_deck()
    with pytest.raises(PresentationPatchNoChangeError) as exc:
        apply_operations(spec, [{"op": "replace_text", "find": "core", "replace": "core"}])
    assert "search_presentation_text" in str(exc.value)
    assert "identical" in str(exc.value)


# ── The search tool ──────────────────────────────────────────────────────────


class _Result:
    def __init__(self, *, rows=None, one=None):
        self._rows = rows or []
        self._one = one

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._one


class _FakeSession:
    def __init__(self, results=None):
        self._results = list(results or [])
        self.statements: list = []
        self.added: list = []
        self.deleted: list = []
        self.committed = False

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
        return None

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)


def _deck_lookup(existing, *, group_ids: list[str] | None = None) -> list[_Result]:
    return [_Result(rows=list(group_ids or [])), _Result(one=existing)]


def _row(owner, spec: dict | None = None, **columns):
    from app.models.presentation import Presentation

    spec = spec or _search_deck()
    return Presentation(
        id=uuid.uuid4(),
        user_id=owner,
        title=spec["title"],
        theme=spec.get("theme") or "dark",
        slide_count=len(spec["slides"]),
        revision=4,
        spec=spec,
        **columns,
    )


def _patch_session(monkeypatch, session: _FakeSession) -> None:
    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: session)


def _run(user_id, name: str, arguments: dict):
    from app.services.builtin_tools import build_presentation_tools
    from app.tools.base import ToolCall

    _defs, executor = build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]
    return asyncio.run(executor.execute_tool(ToolCall(id="call-1", name=name, arguments=arguments)))


def test_the_search_tool_lists_every_occurrence_and_writes_nothing(monkeypatch):
    owner = uuid.uuid4()
    deck = _row(owner)
    before = pp.canonical(deck.spec)
    session = _FakeSession(results=_deck_lookup(deck))
    _patch_session(monkeypatch, session)

    result = _run(owner, "search_presentation_text", {"presentation_id": str(deck.id), "query": "core", "whole_word": True})

    assert not result.is_error, result.content
    content = result.content
    assert "11 occurrence(s), on slides 1-5 and in the deck's chrome.footer_text. Nothing was changed." in content
    first = deck.spec["slides"][0]["id"]
    assert f'- slide 1 [title] id={first}, title: "«Core» platform review"' in content
    assert ", block 1.left.1, content: " in content
    assert ", block 2.2.1, content: " in content
    assert ", block 1, rows[1][0].text: " in content
    assert '- deck chrome.footer_text: "«Core» team - internal"' in content
    assert "replace_text" in content
    assert "[[PRESENTATION:" not in content
    assert session.added == [] and session.deleted == [] and not session.committed
    assert pp.canonical(deck.spec) == before


def test_a_read_only_sharee_can_search(monkeypatch):
    owner, reader = uuid.uuid4(), uuid.uuid4()
    deck = _row(owner, shared_user_ids=[str(reader)], shared_user_permissions={str(reader): "read_only"})
    _patch_session(monkeypatch, _FakeSession(results=_deck_lookup(deck)))
    result = _run(reader, "search_presentation_text", {"presentation_id": str(deck.id), "query": "Core ARR"})
    assert not result.is_error, result.content
    assert "1 occurrence(s), on slide 4." in result.content
    assert ", block 1, title: " in result.content


def test_zero_results_say_so_and_what_a_looser_search_finds(monkeypatch):
    owner = uuid.uuid4()
    deck = _row(owner)
    for arguments, expected in [
        ({"query": "kubernetes"}, "No occurrence: the text does not appear anywhere. Nothing was changed."),
        ({"query": "CORE ARR", "case_sensitive": True}, "No occurrence (a case-insensitive search finds 1)"),
        ({"query": "scor", "whole_word": True}, "without whole_word it occurs 4 time(s), inside longer words"),
    ]:
        _patch_session(monkeypatch, _FakeSession(results=_deck_lookup(deck)))
        result = _run(owner, "search_presentation_text", {"presentation_id": str(deck.id), **arguments})
        assert not result.is_error, result.content
        assert expected in result.content, result.content


def test_a_deck_over_the_read_back_budget_is_searched_to_its_last_slide(monkeypatch):
    """get_presentation serves a long deck slide by slide over several calls;
    the search covers every slide in one."""
    owner = uuid.uuid4()
    spec = _search_deck(extra_slides=60)
    spec["slides"][-1]["blocks"][0]["content"] += "The last slide still says core."
    deck = _row(owner, spec)

    _patch_session(monkeypatch, _FakeSession(results=_deck_lookup(deck)))
    read = _run(owner, "get_presentation", {"presentation_id": str(deck.id)})
    assert "over the 20000-char per-call budget" in read.content
    assert "Not shown:" in read.content

    _patch_session(monkeypatch, _FakeSession(results=_deck_lookup(deck)))
    result = _run(owner, "search_presentation_text", {"presentation_id": str(deck.id), "query": "still says core"})
    assert not result.is_error, result.content
    assert "1 occurrence(s), on slide 65." in result.content
    assert f"id={spec['slides'][64]['id']}, block 1, content: " in result.content


def test_search_lists_at_most_the_limit_and_counts_the_rest(monkeypatch):
    from app.services.builtin_tools import presentation as tool_module

    owner = uuid.uuid4()
    deck = _row(owner, _search_deck(extra_slides=20))
    _patch_session(monkeypatch, _FakeSession(results=_deck_lookup(deck)))
    result = _run(owner, "search_presentation_text", {"presentation_id": str(deck.id), "query": "e"})
    total = len(search_text(deck.spec, "e"))
    assert total > tool_module._SEARCH_LIST_LIMIT
    assert f"{total} occurrence(s)" in result.content
    assert f"... and {total - tool_module._SEARCH_LIST_LIMIT} more, not listed." in result.content
    assert result.content.count("\n- ") == tool_module._SEARCH_LIST_LIMIT


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"presentation_id": "not-a-uuid", "query": "core"}, "must be the UUID of the deck to search"),
        ({"presentation_id": str(uuid.uuid4())}, "query is required"),
        ({"presentation_id": str(uuid.uuid4()), "query": "   "}, "query is required"),
        ({"presentation_id": str(uuid.uuid4()), "query": "x" * 501}, "the limit is 500"),
    ],
)
def test_search_arguments_are_refused_before_the_database(monkeypatch, arguments, expected):
    session = _FakeSession()
    _patch_session(monkeypatch, session)
    result = _run(uuid.uuid4(), "search_presentation_text", arguments)
    assert result.is_error and result.refused
    assert expected in result.content
    assert session.statements == []


def test_a_deck_the_user_cannot_read_is_not_searched(monkeypatch):
    _patch_session(monkeypatch, _FakeSession(results=_deck_lookup(None)))
    result = _run(uuid.uuid4(), "search_presentation_text", {"presentation_id": str(uuid.uuid4()), "query": "core"})
    assert result.is_error and result.refused
    assert "you can access was found" in result.content


def test_edit_presentation_answers_an_identical_find_and_replace_with_the_search_hint(monkeypatch):
    """The probe that withdrew the agent's tools: now one "No change" line, not
    an error, naming the read-only tool to use instead."""
    owner = uuid.uuid4()
    deck = _row(owner)
    session = _FakeSession(results=_deck_lookup(deck))
    _patch_session(monkeypatch, session)
    result = _run(
        owner,
        "edit_presentation",
        {"presentation_id": str(deck.id), "operations": [{"op": "replace_text", "find": "core", "replace": "core"}]},
    )
    assert not result.is_error, result.content
    assert result.no_op
    assert result.content.startswith("No change: operation 1 (replace_text): 'find' and 'replace' are identical")
    assert "search_presentation_text" in result.content
    assert session.added == [] and not session.committed


def test_edit_presentation_refusals_are_flagged_and_its_runtime_errors_are_not(monkeypatch):
    """A refused edit (a validation reason) carries ``refused``, so the loop's
    failure brake never counts it; a save that crashed does not."""
    owner = uuid.uuid4()
    deck = _row(owner)
    _patch_session(monkeypatch, _FakeSession(results=_deck_lookup(deck)))
    refused = _run(
        owner,
        "edit_presentation",
        {"presentation_id": str(deck.id), "operations": [{"op": "replace_text", "find": "core", "replace": "x"}]},
    )
    assert refused.is_error and refused.refused, refused.content
    assert "occurs" in refused.content

    class _Broken(_FakeSession):
        async def execute(self, statement, *a, **k):
            raise RuntimeError("connection reset")

    _patch_session(monkeypatch, _Broken())
    crashed = _run(
        owner,
        "edit_presentation",
        {"presentation_id": str(deck.id), "operations": [{"op": "replace_text", "find": "core", "replace": "x"}]},
    )
    assert crashed.is_error and not crashed.refused
    assert crashed.content.startswith("Error: failed to save")


# ── The delete tool ──────────────────────────────────────────────────────────


def _delete_session(deck, *, actor=None, group_ids=None, role=None) -> _FakeSession:
    """What the delete reads, in order: the deck (groups, row FOR UPDATE), the
    caller's role for a company-managed deck they do not own, then the
    acting user for the audit row."""
    results = _deck_lookup(deck, group_ids=group_ids)
    if deck is not None and deck.is_company_managed:
        results.append(_Result(one=role))
    results.append(_Result(one=actor))
    return _FakeSession(results=results)


def test_the_owner_deletes_a_deck_through_the_library_delete(monkeypatch):
    from sqlalchemy.dialects import postgresql

    from app.models.audit_log import AuditLog

    owner = uuid.uuid4()
    deck = _row(owner, shared_user_ids=[str(uuid.uuid4())], shared_group_ids=[str(uuid.uuid4())])
    actor = SimpleNamespace(id=owner, email="owner@example.com")
    session = _delete_session(deck, actor=actor)
    _patch_session(monkeypatch, session)

    result = _run(owner, "delete_presentation", {"presentation_id": str(deck.id)})

    assert not result.is_error, result.content
    assert session.deleted == [deck]
    assert session.committed
    deck_select = str(session.statements[1].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE OF presentations" in deck_select
    [audit] = session.added
    assert isinstance(audit, AuditLog)
    assert (audit.action, audit.entity_type, audit.entity_id, audit.user_id) == ("delete", "presentation", deck.id, owner)
    assert audit.details == {"slide_count": 5, "revision": 4, "is_company_managed": False, "via": "agent"}
    assert audit.summary == "Deleted presentation 'Quarterly review' through an agent"
    content = result.content
    assert "Deleted presentation **Quarterly review**" in content
    assert "there is no trash to restore it from" in content
    assert "the 2 person/group grant(s)" in content
    assert "[[PRESENTATION:" not in content


@pytest.mark.parametrize("grant", ["read_only", "config"])
def test_a_sharee_cannot_delete_someone_elses_deck(monkeypatch, grant):
    """Edit rights are not ownership: a config grantee is refused like a
    read-only one, and nothing is deleted or audited."""
    owner, sharee = uuid.uuid4(), uuid.uuid4()
    deck = _row(owner, shared_user_ids=[str(sharee)], shared_user_permissions={str(sharee): grant})
    session = _delete_session(deck)
    _patch_session(monkeypatch, session)

    result = _run(sharee, "delete_presentation", {"presentation_id": str(deck.id)})

    assert result.is_error and result.refused
    assert result.content.startswith("Nothing was deleted:")
    assert "only its owner can delete it" in result.content
    assert ("for editing" in result.content) is (grant == "config")
    assert session.deleted == [] and session.added == [] and not session.committed


def test_a_deck_the_user_cannot_see_is_refused_like_an_unknown_one(monkeypatch):
    session = _delete_session(None)
    _patch_session(monkeypatch, session)
    result = _run(uuid.uuid4(), "delete_presentation", {"presentation_id": str(uuid.uuid4())})
    assert result.is_error and result.refused
    assert "you can access was found" in result.content
    assert session.deleted == [] and not session.committed


@pytest.mark.parametrize(("role", "deleted"), [("user", False), ("admin", True)])
def test_a_company_managed_deck_is_deleted_by_an_administrator_only(monkeypatch, role, deleted):
    caller = uuid.uuid4()
    deck = _row(None, is_company_managed=True)
    actor = SimpleNamespace(id=caller, email="someone@example.com")
    session = _delete_session(deck, actor=actor, role=role)
    _patch_session(monkeypatch, session)

    result = _run(caller, "delete_presentation", {"presentation_id": str(deck.id)})

    assert (not result.is_error) is deleted, result.content
    assert session.deleted == ([deck] if deleted else [])
    if not deleted:
        assert result.refused
        assert "published to the company, and only an administrator can delete it" in result.content


@pytest.mark.parametrize(
    ("arguments", "user", "expected"),
    [
        ({"presentation_id": "nope"}, None, "must be the UUID of the deck to delete"),
        ({"presentation_id": str(uuid.uuid4())}, "anonymous", "signed-in users"),
    ],
)
def test_delete_arguments_are_refused_before_the_database(monkeypatch, arguments, user, expected):
    session = _FakeSession()
    _patch_session(monkeypatch, session)
    result = _run(user or uuid.uuid4(), "delete_presentation", arguments)
    assert result.is_error and result.refused
    assert expected in result.content
    assert session.statements == []


def test_a_failed_delete_is_a_runtime_error_not_a_refusal(monkeypatch):
    class _Broken(_FakeSession):
        async def execute(self, statement, *a, **k):
            raise RuntimeError("connection reset")

    _patch_session(monkeypatch, _Broken())
    result = _run(uuid.uuid4(), "delete_presentation", {"presentation_id": str(uuid.uuid4())})
    assert result.is_error and not result.refused
    assert "nothing was deleted" in result.content


def test_duplicate_then_delete_the_copy(monkeypatch):
    """The round trip the tool exists for: copy a deck shared read-only (which
    cannot be edited in place), then remove the copy - the original is never
    touched and only the copy is deleted."""
    from app.models.presentation import Presentation

    owner, reader = uuid.uuid4(), uuid.uuid4()
    source = _row(owner, shared_user_ids=[str(reader)], shared_user_permissions={str(reader): "read_only"})
    actor = SimpleNamespace(id=reader, email="reader@example.com")
    copy_session = _FakeSession(results=[*_deck_lookup(source), _Result(), _Result(), _Result(one=actor)])
    _patch_session(monkeypatch, copy_session)

    async def _no_check(*_a, **_k):
        return ""

    from app.services.builtin_tools import presentation as tool_module

    monkeypatch.setattr(tool_module, "layout_check_note", _no_check)
    copied = _run(reader, "duplicate_presentation", {"presentation_id": str(source.id), "title": "My copy"})
    assert not copied.is_error, copied.content
    duplicate = next(row for row in copy_session.added if isinstance(row, Presentation))
    assert duplicate.user_id == reader

    # The original is the owner's: the reader's delete is refused.
    _patch_session(monkeypatch, _delete_session(source))
    refused = _run(reader, "delete_presentation", {"presentation_id": str(source.id)})
    assert refused.is_error and refused.refused

    # The copy is the reader's own.
    delete_session = _delete_session(duplicate, actor=actor)
    _patch_session(monkeypatch, delete_session)
    removed = _run(reader, "delete_presentation", {"presentation_id": str(duplicate.id)})
    assert not removed.is_error, removed.content
    assert delete_session.deleted == [duplicate]
    assert "**My copy**" in removed.content


def test_the_tool_and_the_library_delete_share_one_service():
    """The agent's delete and the library's are one function, so what goes
    with a deck cannot differ between the two surfaces."""
    import inspect

    from app.api.v1 import presentations as api
    from app.services.builtin_tools import presentation as mod

    source = inspect.getsource(mod.build_presentation_tools)
    handler = source[source.index("async def _delete_presentation(") :]
    handler = handler[: handler.index("\n    async def ", 1)] if "\n    async def " in handler[1:] else handler
    assert "await remove_presentation(" in handler
    assert "agent_deck_permission(" in handler
    assert "db.delete(" not in handler
    route = inspect.getsource(api.delete_presentation)
    assert "remove_presentation(" in route
    assert "db.delete(" not in route


# ── Definitions ──────────────────────────────────────────────────────────────


def test_both_tools_are_defined_and_discoverable():
    from app.services.builtin_tools import build_presentation_tools

    defs, executor = build_presentation_tools(db=None, user_id=str(uuid.uuid4()), conversation_id=None)  # type: ignore[arg-type]
    by_name = {d.name: d for d in defs}
    search = by_name["search_presentation_text"]
    assert search.input_schema["required"] == ["presentation_id", "query"]
    assert set(search.input_schema["properties"]) == {"presentation_id", "query", "case_sensitive", "whole_word"}
    assert "WITHOUT changing anything" in search.description
    delete = by_name["delete_presentation"]
    assert delete.input_schema["required"] == ["presentation_id"]
    assert "IRREVERSIBLE" in delete.description
    assert "ONLY when the user explicitly asks" in delete.description
    assert {"search_presentation_text", "delete_presentation"} <= set(executor.tool_names)
    # Discoverable from the tools an agent reaches for first.
    assert "search_presentation_text" in by_name["get_presentation"].description
    assert "search_presentation_text" in by_name["edit_presentation"].description
    assert "delete_presentation" in by_name["duplicate_presentation"].description


def test_the_search_is_a_read_and_the_delete_a_write():
    """The loop and the approval gate classify by name: the search may be
    replayed from the dedup cache and runs unattended (it changes nothing),
    the delete always runs and is gated like every write."""
    from app.services.hitl_whitelist_default import SHIPPED_DEFAULT_WHITELIST
    from app.tools import agent_loop as al

    assert not al._is_write_tool("search_presentation_text")
    assert al._is_write_tool("delete_presentation")
    assert "search_presentation_text" in SHIPPED_DEFAULT_WHITELIST
    assert "delete_presentation" not in SHIPPED_DEFAULT_WHITELIST
