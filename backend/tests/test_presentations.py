"""Tests for agent-generated interactive presentations.

These lock in the acceptance criteria:

1. A valid ``PresentationSpec`` is persisted and the tool returns a
   ``[[PRESENTATION:<id>]]`` marker the UI renders as a reveal.js deck.
2. No HTML reaches storage: the v1 ``rich_text`` field retired with its
   layouts, and a stored or re-sent v1 slide is upgraded to blocks
   whose text is inline markup, never markup a browser parses.
3. Editing a deck (add / remove / reorder slides) rewrites the SAME row and
   bumps ``revision`` — the marker already in the conversation stays valid, so
   the frontend reconciles the slide list instead of mounting a second deck.
4. A malformed spec fails with a field-addressed error and writes nothing.
5. The free-form "arbitrary HTML in a sandboxed iframe" deck mode is NOT
   implemented (explicitly out of MVP scope).

No DB required: the tool opens its own short-lived session, which is
monkeypatched with a capturing fake — same no-DB style as
``test_template_containers.py``.
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.services.presentation_spec import (
    DEFAULT_SLIDE_CAP,
    MAX_SLIDE_CAP,
    MAX_TITLE_CHARS,
    PRESENTATION_THEMES,
    SLIDE_LAYOUTS,
    PresentationSpecError,
    validate_spec,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend"


# ── Fakes ────────────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, one=None, rows=None):
        self._one = one
        self._rows = rows or []

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._one

    def first(self):
        # A one-row read of several columns (``resolve_public_link``: the deck and its link).
        return self._rows[0] if self._rows else self._one

    def scalar_one(self):
        # The seed's COUNT queries before a retired container is deleted:
        # an unqueued result counts zero bound rows.
        return self._one if self._one is not None else 0


class _FakeSession:
    """Async-context session capturing added rows and executed statements."""

    def __init__(self, results=None):
        self._results = list(results or [])
        self.statements: list = []
        self.added: list = []
        self.refreshed: list = []
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

    async def refresh(self, obj, *a, **k):
        self.refreshed.append((obj, k))

    def add(self, obj):
        self.added.append(obj)


def _session_factory(session: _FakeSession):
    def _factory():
        return session

    return _factory


def _slides(count: int = 2) -> list[dict]:
    return [{"layout": "title", "title": f"Slide {i}", "subtitle": "sub"} for i in range(count)]


def _build_tools(user_id: str | None = None, conversation_id: str | None = None):
    from app.services.builtin_tools import build_presentation_tools

    return build_presentation_tools(
        db=None,  # type: ignore[arg-type]
        user_id=user_id or str(uuid.uuid4()),
        conversation_id=conversation_id,
    )


def _run_tool(executor, name: str, arguments: dict):
    from app.tools.base import ToolCall

    return asyncio.run(executor.execute_tool(ToolCall(id="call-1", name=name, arguments=arguments)))


def _compiled(query) -> str:
    return str(
        query.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


# ── AC1: the spec contract ───────────────────────────────────────────────────


def test_valid_spec_normalizes_and_fills_defaults():
    spec = validate_spec({"title": "Quarterly review", "slides": [{"layout": "title", "title": "Q3"}]})
    assert spec["title"] == "Quarterly review"
    # Defaults are materialized so the renderer never has to guess.
    assert spec["theme"] == "dark"
    assert spec["aspect_ratio"] == "16:9"
    assert spec["transition"] == "slide"
    assert spec["slides"][0]["layout"] == "title"


@pytest.mark.parametrize("layout", SLIDE_LAYOUTS)
def test_every_catalog_layout_validates(layout):
    """One minimal slide per layout of the catalog - the guidance module's own
    example, which is what the agent is shown - validates as itself."""
    from app.services.presentation_guidance import LAYOUT_EXAMPLES

    spec = validate_spec({"title": "Deck", "slides": [LAYOUT_EXAMPLES[layout]]})
    assert spec["slides"][0]["layout"] == layout


# ── the retired v1 layouts are upgraded, never stored ─────────────────


_V1_SLIDES = {
    "two_columns": {
        "layout": "two_columns",
        "title": "T",
        "left": {"heading": "L", "bullets": ["a", "b"]},
        "right": {"rich_text": "<p>Hello <strong>world</strong></p>"},
    },
    "comparison": {
        "layout": "comparison",
        "title": "T",
        "left": {"label": "Now", "points": ["a"]},
        "right": {"label": "Next", "points": ["b"]},
        "verdict": "Next wins",
    },
    "dashboard": {
        "layout": "dashboard",
        "title": "T",
        "metrics": [{"label": "MTTR", "value": "3h", "delta": "-1h", "trend": "down"}],
    },
    "generic": {"layout": "generic", "title": "T", "bullets": ["a"], "notes": "say this"},
}


@pytest.mark.parametrize("layout", sorted(_V1_SLIDES))
def test_a_stored_v1_slide_validates_as_its_v2_upgrade(layout):
    """A deck written in the v1 shape still validates - as the ``content`` slide
    its v1 slide is upgraded to, words intact, shared fields kept. Three of
    the four are told by their retired id; the v1 ``dashboard`` wears an id the
    catalog has ALIVE again and is told by its shape."""
    from app.services.presentation_legacy import RETIRED_LAYOUTS, is_retired_slide

    assert is_retired_slide(_V1_SLIDES[layout])
    assert (layout in RETIRED_LAYOUTS) == (layout != "dashboard")
    spec = validate_spec({"title": "Deck", "slides": [_V1_SLIDES[layout]]})
    (slide,) = spec["slides"]
    assert slide["layout"] == "content"
    assert slide["title"] == "T"
    if layout == "generic":
        assert slide["notes"] == "say this"
    assert "rich_text" not in str(spec) and "<p>" not in str(spec)


def _items(block: dict) -> list[str]:
    return [item["text"] for item in block["items"]]


def test_upgrade_keeps_the_words_of_every_v1_shape():
    spec = validate_spec({"title": "Deck", "slides": list(_V1_SLIDES.values())})
    two_columns, comparison, dashboard, generic = spec["slides"]
    assert [[b["type"] for b in column] for column in two_columns["columns"]] == [["heading", "list"], ["text"]]
    assert two_columns["columns"][0][0]["content"] == "L"
    assert _items(two_columns["columns"][0][1]) == ["a", "b"]
    # The HTML lost its tags, not its words.
    assert two_columns["columns"][1][0]["content"] == "Hello world"
    compare, verdict = comparison["blocks"]
    assert compare["type"] == "comparison"
    assert [compare[side]["label"] for side in ("left", "right")] == ["Now", "Next"]
    assert [_items(compare[side]["blocks"][0]) for side in ("left", "right")] == [["a"], ["b"]]
    assert (verdict["type"], verdict["content"]) == ("text", "Next wins")
    (metrics,) = dashboard["blocks"]
    assert metrics["type"] == "metrics_grid"
    (tile,) = metrics["items"]
    assert (tile["value"], tile["label"], tile["delta"], tile["trend"]) == ("3h", "MTTR", "-1h", "down")
    (bullets,) = generic["blocks"]
    assert bullets["type"] == "list" and _items(bullets) == ["a"]


def test_an_empty_v1_slide_becomes_a_section_not_a_blank_content_slide():
    """A ``content`` slide needs at least one block; a v1 slide with no body
    (a bare-title ``generic`` used as a divider) is upgraded to a ``section``
    with that title rather than refused or padded with a placeholder."""
    spec = validate_spec({"title": "Deck", "slides": [{"layout": "generic", "title": "Part two"}]})
    assert spec["slides"][0]["layout"] == "section"
    assert spec["slides"][0]["title"] == "Part two"


def test_the_upgrade_is_idempotent_and_copy_on_write():
    from app.services.presentation_legacy import upgrade_spec

    v1 = {"title": "Deck", "slides": [_V1_SLIDES["generic"]]}
    once = upgrade_spec(v1)
    assert once is not v1 and v1["slides"][0]["layout"] == "generic", "the input is never mutated"
    assert upgrade_spec(once) is once, "a v2 spec is returned as-is, not rebuilt"
    assert upgrade_spec(once) == once


def test_a_stored_spec_is_upgraded_when_the_column_is_read():
    """The ORM column is the read boundary: ``Presentation.spec`` and
    ``PresentationVersion.spec`` are ``SpecJSON`` columns whose result hook
    runs the upgrader, so the ~25 readers of the column (viewer, studio
    prompt, exporter, package, version preview) never meet a v1 slide - with
    no data migration. A v2 document passes through untouched (same object),
    and a value that is not a deck at all is returned as it was stored."""
    from app.models.presentation import Presentation, PresentationVersion, SpecJSON
    from app.services.presentation_legacy import upgrade_spec

    assert isinstance(Presentation.__table__.c.spec.type, SpecJSON)
    assert isinstance(PresentationVersion.__table__.c.spec.type, SpecJSON)

    column = SpecJSON()
    v1 = {"title": "Deck", "slides": [_V1_SLIDES["generic"]]}
    read = column.process_result_value(v1, None)
    assert read == upgrade_spec(v1)
    assert read["slides"][0]["layout"] == "content"
    v2 = validate_spec(read)
    assert column.process_result_value(v2, None) is v2
    assert column.process_result_value(None, None) is None
    assert column.process_result_value(["not", "a", "deck"], None) == ["not", "a", "deck"]
    # Writes are not rewritten by the column: the WRITE boundary is validate_spec.
    assert "process_bind_param" not in vars(SpecJSON)


def test_a_generated_deck_that_names_a_retired_layout_is_upgraded_and_the_agent_told(monkeypatch):
    """An agent still emitting a v1 layout from an old instruction gets a
    working deck AND a sentence naming what was upgraded, so it stops."""
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    user_id = str(uuid.uuid4())
    _defs, executor = _build_tools(user_id, str(uuid.uuid4()))
    result = _run_tool(
        executor,
        "generate_presentation",
        {
            "title": "Deck",
            "slides": [
                {"layout": "title", "title": "Deck"},
                _V1_SLIDES["two_columns"],
            ],
        },
    )
    assert not result.is_error, result.content
    assert "retired v1 layout" in result.content and "two_columns" in result.content
    assert "position 2" in result.content
    from app.models.presentation import Presentation

    (written,) = [row for row in session.added if isinstance(row, Presentation)]
    assert [slide["layout"] for slide in written.spec["slides"]] == ["title", "content"]
    assert "rich_text" not in json.dumps(written.spec)


def test_layout_catalog_matches_the_documented_set():
    """The tool description and the frontend component map are built from this
    tuple — a layout added on one side only would render as a blank slide."""
    from app.services.presentation_spec import layout_catalog

    assert set(layout_catalog()) == set(SLIDE_LAYOUTS)


# ── AC4: malformed specs fail loudly, and nothing is written ──────────────────


@pytest.mark.parametrize(
    ("payload", "expected_fragment"),
    [
        ({"title": "D", "slides": []}, "at least 1 item"),
        ({"title": "D", "slides": [{"layout": "carousel"}]}, "does not match"),
        # An invented key would silently drop content if extras were ignored.
        (
            {"title": "D", "slides": [{"layout": "title", "title": "x", "body": "y"}]},
            "Extra inputs are not permitted",
        ),
        ({"slides": [{"layout": "title", "title": "x"}]}, "title"),
        ("not a dict", "must be a JSON object"),
    ],
)
def test_invalid_spec_reports_the_offending_field(payload, expected_fragment):
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(payload)
    assert expected_fragment in str(exc.value)


def test_slide_cap_is_enforced():
    """The platform default holds for a deck nobody set a cap for,
    and the refusal names the cap and how it is raised - the template's
    ``max_slides`` - rather than a platform constant."""
    payload = {"title": "D", "slides": _slides(DEFAULT_SLIDE_CAP + 1)}
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(payload)
    message = str(exc.value)
    assert "at most" in message
    assert f"at most {DEFAULT_SLIDE_CAP} slides" in message
    assert "max_slides" in message
    # Structured for the editor's 422 like any field problem.
    assert [p.path for p in exc.value.problems] == ["slides"]
    assert exc.value.human_message().startswith("The presentation cannot be saved as it is.")
    assert f"Slides - at most {DEFAULT_SLIDE_CAP} slides" in exc.value.human_message()


def test_the_slide_cap_is_the_decks_own(monkeypatch):
    """A template's cap admits what the default refuses: the SAME
    payload is refused at the default and accepted under ``slide_cap``; the
    ceiling holds whatever cap is passed; a page document says "pages"."""
    from app.services.presentation_spec import clamp_slide_cap

    long_deck = {"title": "D", "slides": _slides(DEFAULT_SLIDE_CAP + 1)}
    with pytest.raises(PresentationSpecError):
        validate_spec(long_deck)
    assert len(validate_spec(long_deck, slide_cap=150)["slides"]) == DEFAULT_SLIDE_CAP + 1
    assert len(validate_spec(long_deck, slide_cap=DEFAULT_SLIDE_CAP + 1)["slides"]) == DEFAULT_SLIDE_CAP + 1
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(long_deck, slide_cap=DEFAULT_SLIDE_CAP)
    assert f"got {DEFAULT_SLIDE_CAP + 1}" in str(exc.value)
    # The ceiling: a cap past it IS it, and the model refuses one more slide
    # whatever the caller asked for.
    assert clamp_slide_cap(MAX_SLIDE_CAP + 50) == MAX_SLIDE_CAP
    assert clamp_slide_cap(0) == DEFAULT_SLIDE_CAP
    assert clamp_slide_cap(True) == DEFAULT_SLIDE_CAP
    assert clamp_slide_cap("150") == 150
    assert clamp_slide_cap(None) == DEFAULT_SLIDE_CAP
    at_ceiling = {"title": "D", "slides": _slides(MAX_SLIDE_CAP)}
    assert len(validate_spec(at_ceiling, slide_cap=MAX_SLIDE_CAP)["slides"]) == MAX_SLIDE_CAP
    over = {"title": "D", "slides": _slides(MAX_SLIDE_CAP + 1)}
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(over, slide_cap=MAX_SLIDE_CAP + 100)
    assert "at most" in str(exc.value)
    # A ceiling refusal says there is nothing to raise.
    exact = {"title": "D", "slides": _slides(MAX_SLIDE_CAP)}
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(exact, slide_cap=MAX_SLIDE_CAP - 1)
    assert "max_slides" in str(exc.value)
    pages = {"title": "D", "aspect_ratio": "A4", "slides": _slides(3)}
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(pages, slide_cap=2)
    assert "at most 2 pages for this presentation" in str(exc.value)


def test_template_slide_cap_reads_the_setting_and_nothing_else():
    """``extra_metadata.max_slides`` is the one place a template says how long
    a deck may run; every other shape reads as the platform default,
    and a number past the ceiling reads as the ceiling."""
    from app.services.presentations import effective_slide_cap, template_slide_cap

    assert template_slide_cap({"max_slides": 150}) == 150
    assert template_slide_cap({"max_slides": "80"}) == 80
    assert template_slide_cap({"max_slides": MAX_SLIDE_CAP * 2}) == MAX_SLIDE_CAP
    assert template_slide_cap({"max_slides": 0}) == DEFAULT_SLIDE_CAP
    assert template_slide_cap({"max_slides": "many"}) == DEFAULT_SLIDE_CAP
    assert template_slide_cap({"max_slides": None}) == DEFAULT_SLIDE_CAP
    assert template_slide_cap({"deck_defaults": {"max_slides": 150}}) == DEFAULT_SLIDE_CAP
    assert template_slide_cap({}) == DEFAULT_SLIDE_CAP
    assert template_slide_cap(None) == DEFAULT_SLIDE_CAP
    # A re-save of a deck already over its cap is admitted at its size, never
    # one slide more: the deck stays saveable, it cannot grow.
    stored = {"title": "D", "slides": _slides(140)}
    assert effective_slide_cap(DEFAULT_SLIDE_CAP, stored) == 140
    assert effective_slide_cap(150, stored) == 150
    assert effective_slide_cap(DEFAULT_SLIDE_CAP, {"title": "D", "slides": _slides(3)}) == DEFAULT_SLIDE_CAP
    assert effective_slide_cap(DEFAULT_SLIDE_CAP, None) == DEFAULT_SLIDE_CAP
    assert effective_slide_cap(DEFAULT_SLIDE_CAP, {"slides": "nope"}) == DEFAULT_SLIDE_CAP


def test_template_facts_carry_the_slide_cap():
    """The one facts loader hands every write path the cap, and the
    palette hands the deck read ``max_slides`` for the editor."""
    from app.services.presentations import TemplatePalette, template_facts

    row = SimpleNamespace(
        id=uuid.uuid4(),
        name="Board Deck",
        kind="presentation",
        theme_config=_DARK_BASE,
        extra_metadata={"supported_schemes": ["dark"], "max_slides": 150},
        layouts=None,
    )
    assert template_facts(row).slide_cap == 150
    row.extra_metadata = {"supported_schemes": ["dark"]}
    assert template_facts(row).slide_cap == DEFAULT_SLIDE_CAP
    assert TemplatePalette(themes={}).slide_cap == DEFAULT_SLIDE_CAP


def test_the_filigran_deck_ships_with_its_own_slide_cap():
    """The corporate deck runs long: 150 slides, within the ceiling;
    the XTM One deck takes the platform default like any template that sets
    none - and the seed's declaration passes the write validator."""
    from app.services import template_container_seed as seed_mod
    from app.services.presentations import template_slide_cap
    from app.services.template_containers import validate_slide_cap

    by_slug = {c["slug"]: c for c in seed_mod.BUILTIN_TEMPLATE_CONTAINERS}
    assert by_slug["filigran-brand"]["extra_metadata"]["max_slides"] == 150
    assert template_slide_cap(by_slug["filigran-brand"]["extra_metadata"]) == 150
    assert "max_slides" not in by_slug["xtm-one-brand"]["extra_metadata"]
    assert template_slide_cap(by_slug["xtm-one-brand"]["extra_metadata"]) == DEFAULT_SLIDE_CAP
    _meta, problems = validate_slide_cap(by_slug["filigran-brand"]["extra_metadata"], kind="presentation")
    assert problems == []
    assert MAX_SLIDE_CAP >= 150


@pytest.mark.parametrize(
    ("raw", "kind", "stored", "problem"),
    [
        (150, "presentation", 150, None),
        ("80", "presentation", 80, None),
        (80.0, "presentation", 80, None),
        (MAX_SLIDE_CAP, "presentation", MAX_SLIDE_CAP, None),
        (None, "presentation", "dropped", None),
        (0, "presentation", "kept", "from 1 to"),
        (MAX_SLIDE_CAP + 1, "presentation", "kept", "platform ceiling"),
        (True, "presentation", "kept", "whole number"),
        ("many", "presentation", "kept", "whole number"),
        (12.5, "presentation", "kept", "whole number"),
        (150, "report", "dropped", None),
        ("many", "report", "dropped", None),
    ],
)
def test_validate_slide_cap_holds_the_write_to_what_the_reader_accepts(raw, kind, stored, problem):
    """The write validator stores a cap the reader reads as written:
    a value the reader would clamp or ignore is refused, ``null`` drops the
    key (the platform default), and a non-deck kind drops it without a word."""
    from app.services.template_containers import validate_slide_cap

    meta, problems = validate_slide_cap({"max_slides": raw, "guidance": "x"}, kind=kind)
    if problem is None:
        assert problems == []
        if stored == "dropped":
            assert "max_slides" not in meta
        else:
            assert meta["max_slides"] == stored
        assert meta["guidance"] == "x"
    else:
        assert len(problems) == 1 and problem in problems[0]
        assert "max_slides" in problems[0]
        assert meta == {"max_slides": raw, "guidance": "x"}  # returned as it was: the write is refused
    # Absent key: nothing to say.
    assert validate_slide_cap({"guidance": "x"}, kind=kind) == ({"guidance": "x"}, [])
    assert validate_slide_cap(None, kind=kind) == (None, [])


def test_error_message_names_the_layout_of_the_failing_slide():
    """The union tag is surfaced as ``(layout=…)`` rather than as a fake path
    segment, so the agent can address the slide it actually sent."""
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(
            {
                "title": "D",
                "slides": [
                    {"layout": "title", "title": "ok"},
                    {"layout": "content", "title": "bad", "blocks": []},
                ],
            }
        )
    message = str(exc.value)
    assert "slides.1" in message
    assert "(layout=content)" in message


# ── AC2: no markup a browser parses ever reaches storage ─────────────────────


@pytest.mark.parametrize(
    "rich_text",
    [
        "<script>alert(1)</script>",
        '<p onclick="steal()">x</p>',
        '<img src=x onerror="steal()">',
        '<iframe src="https://evil.example"></iframe>',
        '<form action="https://evil.example"><input name="p"></form>',
        '<a href="javascript:alert(1)">x</a>',
        '<object data="x"></object>',
        '<p srcdoc="<script>x</script>">y</p>',
        "<style>body{display:none}</style>",
    ],
)
def test_dangerous_v1_rich_text_never_survives_the_upgrade(rich_text):
    """The v1 ``rich_text`` field was rejected when it carried executable
    markup; it is not a field at all. A stored (or re-sent) v1
    slide is upgraded before validation and its fragment reduced to WORDS in
    a ``text`` block: no tag, no attribute, no URL scheme reaches storage,
    and the renderer has no HTML sink to feed it to anyway."""
    spec = validate_spec({"title": "D", "slides": [{"layout": "generic", "title": "T", "rich_text": rich_text}]})
    stored = json.dumps(spec)
    assert "rich_text" not in stored
    assert "<" not in stored and ">" not in stored
    for marker in ("onclick", "onerror", "srcdoc", "javascript:", "evil.example"):
        assert marker not in stored
    (slide,) = spec["slides"]
    assert slide["layout"] in {"content", "section"}


def test_safe_v1_paragraph_markup_keeps_its_words_as_a_text_block():
    html = (
        "<h3>Findings</h3><p>Two <strong>critical</strong> issues, see "
        '<a href="https://example.org">the report</a>.</p>'
        "<ul><li>one</li><li>two</li></ul>"
    )
    spec = validate_spec({"title": "D", "slides": [{"layout": "generic", "title": "T", "rich_text": html}]})
    (block,) = spec["slides"][0]["blocks"]
    assert block["type"] == "text"
    assert block["content"] == "Findings\nTwo critical issues, see the report.\none\ntwo"


def test_no_field_in_the_spec_carries_html():
    """AC5 — no field may carry a whole HTML document / iframe deck, and
    no field carries a fragment either. If a future change adds one,
    this test is the tripwire."""
    from app.services import presentation_spec as mod

    seen: set[type] = set()

    def fields(model) -> set[str]:
        if model in seen:
            return set()
        seen.add(model)
        found: set[str] = set()
        for name, info in model.model_fields.items():
            found.add(name)
            for candidate in _nested_models(info.annotation):
                found |= fields(candidate)
        return found

    every_field = fields(mod.PresentationSpec)
    for model in mod._LAYOUT_MODELS.values():
        every_field |= fields(model)
    assert len(seen) > 20, "the walk reached the block models, not just the top level"
    # The banner action's ``url`` is the ONE address field, and it is not a
    # document: ``check_action_url`` admits ``https://`` and ``mailto:`` only.
    url_fields = {name for name in every_field if name == "url" or name.endswith("_url")}
    assert url_fields == {"url"}
    assert mod.check_action_url("https://example.org") == "https://example.org"
    with pytest.raises(ValueError):
        mod.check_action_url("javascript:alert(1)")
    html_fields = {
        name
        for name in every_field
        if "html" in name or "iframe" in name or "srcdoc" in name or name == "script" or name == "rich_text"
    }
    assert html_fields == set()
    assert not hasattr(mod, "RichText") and not hasattr(mod, "MAX_RICH_TEXT_CHARS")


def _nested_models(annotation) -> list[type]:
    """Pydantic models reachable from a field annotation (unions, lists, dicts)."""
    import typing

    from pydantic import BaseModel

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    found: list[type] = []
    for arg in typing.get_args(annotation):
        found.extend(_nested_models(arg))
    return found


# ── The tool: create ────────────────────────────────────────────────────────


def test_generate_presentation_persists_and_returns_a_marker(monkeypatch):
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    user_id = str(uuid.uuid4())
    conversation_id = str(uuid.uuid4())
    _defs, executor = _build_tools(user_id, conversation_id)
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "Deck", "slides": _slides(3), "theme": "light"},
    )

    assert not result.is_error, result.content
    # The deck row plus its first history snapshot (every save is versioned).
    from app.models.presentation import Presentation, PresentationVersion

    assert [type(r) for r in session.added] == [Presentation, PresentationVersion]
    row = session.added[0]
    assert row.title == "Deck"
    assert row.theme == "light"
    assert row.slide_count == 3
    assert row.revision == 1
    assert str(row.user_id) == user_id
    assert str(row.updated_by_user_id) == user_id
    assert str(row.conversation_id) == conversation_id
    assert session.committed
    version = session.added[1]
    assert version.revision == 1
    assert version.source == "agent"
    assert str(version.author_id) == user_id
    assert version.spec == row.spec
    assert f"[[PRESENTATION:{row.id}]]" in result.content
    # The id is echoed as text too, so the model can edit the deck later.
    assert str(row.id) in result.content


def _marker_contract(key: str = "markers") -> list:
    """The answer-marker samples the backend and the web app are both tested on."""
    contract = FRONTEND_ROOT / "src" / "pages" / "chat" / "answer-markers.contract.json"
    return json.loads(contract.read_text(encoding="utf-8"))[key]


def test_marker_format_matches_the_channel_and_frontend_regexes(monkeypatch):
    """One marker shape, three consumers (web renderer, channel rewriter,
    tool output). A divergence leaks raw ``[[PRESENTATION:…]]`` text to users."""
    import app.core.database as database
    from app.channels.base import _PRESENTATION_MARKER_RE

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools()
    content = _run_tool(executor, "generate_presentation", {"title": "D", "slides": _slides(1)}).content

    assert _PRESENTATION_MARKER_RE.search(content)

    # Both sides read the SAME two forms: the plain deck marker and the
    # marker carrying the slide to land on, ``#/<slide id>``. The web app reads
    # markers with the chat panel's grammar, tested on the same samples
    # (frontend/src/pages/chat/answer-markers.contract.json, answerMarkers.test.ts).
    from app.services.presentation_spec import SLIDE_ID_PATTERN

    samples = [m for m in _marker_contract() if m["kind"] == "PRESENTATION"]
    assert {m["slideRef"] is None for m in samples} == {True, False}
    for sample in samples:
        match = _PRESENTATION_MARKER_RE.fullmatch(sample["marker"])
        assert match, sample["marker"]
        assert match.group(1).lower() == sample["id"]
        assert match.group(2) == sample["slideRef"]
    # The fragment grammar IS the slide id grammar - a slide id the validator
    # accepts is always addressable, and nothing else is.
    fragment_class = SLIDE_ID_PATTERN.strip("^$")
    assert fragment_class in _PRESENTATION_MARKER_RE.pattern
    for rejected in _marker_contract(key="rejected"):
        assert _PRESENTATION_MARKER_RE.fullmatch(rejected) is None, rejected


def test_slide_links_have_one_shape_and_every_surface_reads_it():
    """The link the tool hands the agent is the address the frontend
    follows: ``/presentations/<deck>#/<slide id>``. One writer, four readers -
    the chat's link renderer (in place), the transcript card (opens on that
    slide), the studio rail (moves the stage) and the viewer (hash). A reader
    that stops parsing the shape turns the agent's link into a dead click."""
    from app.services.builtin_tools.presentation import focus_marker, slide_link
    from app.services.presentation_spec import SLIDE_ID_PATTERN

    deck_id = str(uuid.uuid4())
    assert slide_link(deck_id, "slide-4b") == f"/presentations/{deck_id}#/slide-4b"
    assert focus_marker(deck_id, "slide-4b") == f"[[PRESENTATION:{deck_id}#/slide-4b]]"
    assert focus_marker(deck_id, None) == f"[[PRESENTATION:{deck_id}]]"

    links = (FRONTEND_ROOT / "src" / "components" / "presentation" / "slide-links.ts").read_text(encoding="utf-8")
    # The parser reads the viewer path and the SAME fragment grammar the
    # validator accepts for a slide id; the all-digits form stays the 0-based
    # index the viewer always wrote.
    assert "^/presentations/(" in links
    assert SLIDE_ID_PATTERN.strip("^$") in links
    for name in ("parsePresentationLink", "parseSlideFragment", "resolveSlideRef"):
        assert f"export function {name}" in links, name

    markdown = (FRONTEND_ROOT / "src" / "components" / "chat" / "ChatMarkdown.tsx").read_text(encoding="utf-8")
    assert "parsePresentationLink(href)" in markdown
    assert "navigateToSlide(link.presentationId, link.slideRef)" in markdown
    card = (FRONTEND_ROOT / "src" / "components" / "presentation" / "PresentationCard.tsx").read_text(encoding="utf-8")
    for token in ("registerSlideStage", "focusSlideRef", "resolveSlideRef"):
        assert token in card, token
    transcript = (FRONTEND_ROOT / "src" / "components" / "chat-panel" / "markers.tsx").read_text(encoding="utf-8")
    assert "focusSlideRef={slideRef}" in transcript
    assert "registerSlideStage" in transcript
    rail = "".join(
        (FRONTEND_ROOT / "src" / "components" / part).read_text(encoding="utf-8")
        for part in ("presentation/PresentationStudioRail.tsx", "studio/StudioRail.tsx")
    )
    assert "navigateToSlide" in rail
    viewer = (FRONTEND_ROOT / "src" / "pages" / "PresentationViewer.tsx").read_text(encoding="utf-8")
    assert "parseSlideFragment(hash)" in viewer


def test_conversation_previews_drop_the_artifact_markers():
    """The sidebar's last-message preview is plain text: a reply that closes
    with the deck marker must read as its sentence, not as the sentence plus
    a raw ``[[PRESENTATION:<uuid>]]`` — and the strip happens BEFORE the 150
    char cut, because a marker truncated mid-uuid never matches again."""
    import inspect
    import re

    from app.api.v1 import agent_chats, chat
    from app.channels.base import strip_artifact_markers

    deck = f"[[PRESENTATION:{uuid.uuid4()}]]"
    focused = f"[[PRESENTATION:{uuid.uuid4()}#/slide-4b]]"
    report = f"[[REPORT:{uuid.uuid4()}]]".lower()
    file = f"[[FILE:{uuid.uuid4()}]]".upper()
    text = f"Here's the deck, unchanged.\n\n{deck}\n{focused}\n{report} and {file} done."
    cleaned = strip_artifact_markers(text)
    assert "[[" not in cleaned
    assert "slide-4b" not in cleaned
    assert cleaned.startswith("Here's the deck, unchanged.")
    assert cleaned.endswith("and done.")
    # A marker-free message is returned as is (no gratuitous whitespace edits).
    assert strip_artifact_markers("plain  text") == "plain  text"
    assert strip_artifact_markers("") == ""

    # The preview drops EVERY marker family, the investigation one
    # included, and a family added later cannot be forgotten: every complete
    # marker pattern of the module is in the tuple the helper strips.
    from app.channels import base as channels_base

    every_family = f"Investigated it. [[INVESTIGATION:{uuid.uuid4()}]] Deck: [[PRESENTATION:{uuid.uuid4()}]] done."
    assert strip_artifact_markers(every_family) == "Investigated it. Deck: done."
    marker_patterns = {
        value
        for name, value in vars(channels_base).items()
        if re.fullmatch(r"_[A-Z]+_MARKER_RE", name) and isinstance(value, re.Pattern)
    }
    assert marker_patterns == set(channels_base._ALL_ARTIFACT_MARKER_RES)

    # Both list endpoints strip before they truncate: the web chat's and the
    # embedded chat's through `last_message_previews`.
    assert "last_message_previews(" in inspect.getsource(chat._serialize_conversation_list)
    for module, fn_name in (
        (chat, "last_message_previews"),
        (agent_chats, "list_agent_chats"),
    ):
        src = inspect.getsource(getattr(module, fn_name))
        strip_at = src.index("strip_artifact_markers(")
        cut_at = src.index('+ "..."')
        assert strip_at < cut_at, f"{module.__name__}.{fn_name} must strip markers before the preview cut"


def test_slides_supplied_as_a_json_string_are_parsed(monkeypatch):
    """Providers occasionally stringify array arguments; a guaranteed failure
    would otherwise be indistinguishable from a bad spec."""
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools()
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D", "slides": json.dumps(_slides(2))},
    )
    assert not result.is_error, result.content
    assert session.added[0].slide_count == 2


def test_invalid_spec_writes_nothing_and_returns_a_tool_error(monkeypatch):
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools()
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D", "slides": [{"layout": "title"}]},
    )

    assert result.is_error
    assert "was NOT saved" in result.content
    assert session.added == []
    assert not session.committed


def test_anonymous_caller_cannot_generate_a_deck(monkeypatch):
    """A deck is only reachable through the authenticated read endpoint, so an
    anonymous channel user would get an unrenderable artifact."""
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools(user_id="anonymous")
    result = _run_tool(executor, "generate_presentation", {"title": "D", "slides": _slides(1)})
    assert result.is_error
    assert "signed-in" in result.content
    assert session.added == []


# ── AC3: editing rewrites the same deck in place ─────────────────────────────


def _existing_row(user_id: uuid.UUID, slides: int = 3, **columns):
    from app.models.presentation import Presentation

    return Presentation(
        id=uuid.uuid4(),
        user_id=user_id,
        title="Deck",
        theme="dark",
        slide_count=slides,
        revision=1,
        spec={"title": "Deck", "slides": _slides(slides)},
        **columns,
    )


def _deck_lookup(existing, *, group_ids: list[str] | None = None) -> list[_Result]:
    """The two results a visibility-aware deck lookup consumes: the
    caller's group memberships, then the deck row itself."""
    return [_Result(rows=list(group_ids or [])), _Result(one=existing)]


def test_editing_reuses_the_row_and_bumps_the_revision(monkeypatch):
    import app.core.database as database

    user_id = uuid.uuid4()
    existing = _existing_row(user_id, slides=3)
    session = _FakeSession(results=_deck_lookup(existing))
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools(str(user_id))
    reordered = [
        {"layout": "title", "title": "Slide 1"},
        {"layout": "content", "title": "New middle", "blocks": [{"type": "list", "items": ["x"]}]},
    ]
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "Deck v2", "slides": reordered, "presentation_id": str(existing.id)},
    )

    assert not result.is_error, result.content
    # No second deck row: the SAME deck was rewritten, so the marker already
    # sitting in the conversation stays valid and the deck updates in place.
    # The only insert is the history snapshot of the new revision.
    assert [type(r).__name__ for r in session.added] == ["PresentationVersion"]
    assert existing.revision == 2
    assert existing.title == "Deck v2"
    assert existing.slide_count == 2
    assert [s["layout"] for s in existing.spec["slides"]] == ["title", "content"]
    assert f"[[PRESENTATION:{existing.id}]]" in result.content
    assert "Updated presentation" in result.content
    version = session.added[0]
    assert version.presentation_id == existing.id
    assert version.revision == 2
    assert version.spec == existing.spec


def test_cover_defaults_dress_a_new_deck_but_never_a_rewrite(monkeypatch):
    """A template's per-cover defaults fill a deck at its BIRTH. A
    wholesale rewrite through ``generate_presentation(presentation_id=...)``
    has no slide identity to read a stored cover from, so a re-emitted title
    slide left unset stays plain and inheriting - existing decks are untouched
    by a brand's cover defaults, whatever the agent omitted."""
    import app.core.database as database

    template_id = uuid.uuid4()
    brand = _template_row(
        template_id,
        extra_metadata={
            "deck_defaults": {
                "theme": "dark",
                "hero_defaults": {"title": "gradient", "closing": "gradient"},
                "slide_themes": {"title": "dark"},
            }
        },
    )
    session = _FakeSession(results=[_Result(rows=[]), _Result(one=brand)])
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools()
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D", "slides": [{"layout": "title", "title": "Cover"}], "template_id": str(template_id)},
    )
    assert not result.is_error, result.content
    born = session.added[0].spec["slides"][0]
    assert born["hero"] == "gradient" and born["theme"] == "dark"

    user_id = uuid.uuid4()
    existing = _existing_row(user_id, slides=1, template_container_id=template_id)
    session = _FakeSession(results=[*_deck_lookup(existing), _Result(rows=[]), _Result(one=brand)])
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D v2", "slides": [{"layout": "title", "title": "Cover again"}], "presentation_id": str(existing.id)},
    )
    assert not result.is_error, result.content
    assert existing.revision == 2
    rewritten = existing.spec["slides"][0]
    assert rewritten["hero"] == "plain"
    assert "theme" not in rewritten
    # The deck keys still come from the stored deck, never from the defaults again.
    assert existing.spec["theme"] == "dark"


def test_a_tiled_cover_asked_of_a_brand_deck_takes_its_gradient_cover(monkeypatch):
    """An agent asking a Filigran-style brand deck - or a free deck - for
    a THREAD cover gets the template's own default cover, and the result names
    the slide; the template that declares the cover keeps it."""
    import app.core.database as database

    slides = [{"layout": "title", "title": "Cover", "hero": "cascade"}, {"layout": "closing", "hero": "mosaic"}]
    template_id = uuid.uuid4()
    brand = _template_row(
        template_id,
        name="Filigran Brand Deck",
        extra_metadata={"deck_defaults": {"hero_defaults": {"title": "gradient", "closing": "gradient"}}},
    )
    session = _FakeSession(results=[_Result(rows=[]), _Result(one=brand)])
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools()
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D", "slides": copy.deepcopy(slides), "template_id": str(template_id)},
    )
    assert not result.is_error, result.content
    assert [slide["hero"] for slide in session.added[0].spec["slides"]] == ["gradient", "gradient"]
    assert (
        "Hero: 2 slides asked for a cover the template 'Filigran Brand Deck' does not offer; they take "
        "the default cover instead (1: 'cascade' -> 'gradient'; 2: 'mosaic' -> 'gradient')."
    ) in result.content

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D", "slides": copy.deepcopy(slides[:1]), "template_id": "none"},
    )
    assert not result.is_error, result.content
    assert session.added[0].spec["slides"][0]["hero"] == "gradient"
    assert "a cover a deck without a template does not offer" in result.content

    thread = _template_row(template_id, name="THREAD Conference Deck", extra_metadata={"heroes": ["mosaic", "cascade"]})
    session = _FakeSession(results=[_Result(rows=[]), _Result(one=thread)])
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D", "slides": copy.deepcopy(slides), "template_id": str(template_id)},
    )
    assert not result.is_error, result.content
    assert [slide["hero"] for slide in session.added[0].spec["slides"]] == ["cascade", "mosaic"]
    assert "Hero:" not in result.content


def test_get_presentation_reads_the_covers_as_the_reader_sees_them(monkeypatch):
    """The agent reads the cover the user sees: a stored tiled cover under a
    template that does not offer it reads as the template's default."""
    import app.core.database as database

    user_id = uuid.uuid4()
    template_id = uuid.uuid4()
    existing = _existing_row(user_id, slides=1, template_container_id=template_id)
    existing.spec["slides"][0]["hero"] = "cascade"
    brand = _template_row(
        template_id, extra_metadata={"deck_defaults": {"hero_defaults": {"title": "split"}}}
    )
    session = _FakeSession(
        results=[*_deck_lookup(existing), _Result(rows=[]), _Result(rows=[]), _Result(one=brand)]
    )
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools(str(user_id))
    content = _run_tool(executor, "get_presentation", {"presentation_id": str(existing.id)}).content
    assert '"hero":"split"' in content and "cascade" not in content
    assert existing.spec["slides"][0]["hero"] == "cascade"


def test_editing_a_deck_that_is_not_visible_is_refused(monkeypatch):
    import app.core.database as database

    session = _FakeSession(results=_deck_lookup(None))
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools()
    result = _run_tool(
        executor,
        "generate_presentation",
        {
            "title": "D",
            "slides": _slides(1),
            "presentation_id": str(uuid.uuid4()),
        },
    )
    assert result.is_error
    assert "was found" in result.content
    assert session.added == []


def test_edit_lookup_uses_the_visibility_predicate(monkeypatch):
    """The deck lookup behind an edit runs the platform's three-tier visibility
    predicate in SQL (own + shared + company-managed) — never a bare id
    match, or an agent could rewrite any deck whose id it was handed."""
    import app.core.database as database

    session = _FakeSession(results=_deck_lookup(None))
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    user_id = uuid.uuid4()
    _defs, executor = _build_tools(str(user_id))
    _run_tool(
        executor,
        "generate_presentation",
        {"title": "D", "slides": _slides(1), "presentation_id": str(uuid.uuid4())},
    )
    # statements[0] resolves the caller's groups; statements[1] is the deck.
    sql = _compiled(session.statements[1])
    assert "presentations.user_id" in sql
    assert str(user_id) in sql
    assert "shared_user_ids" in sql
    assert "is_company_managed" in sql


def test_a_config_grantee_can_edit_a_shared_deck_with_their_own_agent(monkeypatch):
    """The contract: a colleague granted ``config`` keeps working on the
    deck from their own conversation, and the edit lands on the SAME row."""
    import app.core.database as database

    owner, editor = uuid.uuid4(), uuid.uuid4()
    existing = _existing_row(
        owner,
        slides=2,
        shared_user_ids=[str(editor)],
        shared_user_permissions={str(editor): "config"},
    )
    session = _FakeSession(results=_deck_lookup(existing))
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools(str(editor))
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "Deck v2", "slides": _slides(1), "presentation_id": str(existing.id)},
    )
    assert not result.is_error, result.content
    # Only the history snapshot is inserted — no second deck row.
    assert [type(r).__name__ for r in session.added] == ["PresentationVersion"]
    assert existing.revision == 2
    # Ownership never moves with an edit; the editor is recorded separately.
    assert existing.user_id == owner
    assert existing.updated_by_user_id == editor
    assert session.added[0].author_id == editor


def test_a_read_only_grantee_is_told_the_deck_cannot_be_edited(monkeypatch):
    import app.core.database as database

    owner, reader = uuid.uuid4(), uuid.uuid4()
    existing = _existing_row(
        owner,
        slides=2,
        shared_user_ids=[str(reader)],
        shared_user_permissions={str(reader): "read_only"},
    )
    # Writable lookup (2 queries) finds the row but refuses; the fallback
    # visible lookup (2 more) finds it again to phrase the refusal.
    session = _FakeSession(results=_deck_lookup(existing) + _deck_lookup(existing))
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools(str(reader))
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "Deck v2", "slides": _slides(1), "presentation_id": str(existing.id)},
    )
    assert result.is_error
    assert "read-only" in result.content
    # The way out copies the deck exactly instead of re-emitting it.
    assert "duplicate_presentation" in result.content
    assert existing.revision == 1
    assert session.added == []


def test_deck_permission_tiers():
    from app.services.presentations import deck_permission

    owner, other, group = uuid.uuid4(), uuid.uuid4(), str(uuid.uuid4())
    private = _existing_row(owner)
    assert deck_permission(private, user_id=owner) == "owner"
    assert deck_permission(private, user_id=other) is None
    # An admin acting IN PERSON (REST) may manage a colleague's deck; the agent
    # tool never passes ``is_admin``.
    assert deck_permission(private, user_id=other, is_admin=True) == "config"

    by_group = _existing_row(
        owner, shared_group_ids=[group], shared_group_permissions={group: "config"}
    )
    assert deck_permission(by_group, user_id=other, user_group_ids=[group]) == "config"
    assert deck_permission(by_group, user_id=other, user_group_ids=[]) is None

    read_only = _existing_row(
        owner, shared_user_ids=[str(other)], shared_user_permissions={str(other): "read_only"}
    )
    assert deck_permission(read_only, user_id=other) == "read_only"

    published = _existing_row(owner, is_company_managed=True)
    assert deck_permission(published, user_id=other) == "read_only"
    assert deck_permission(published, user_id=other, is_admin=True) == "owner"
    assert deck_permission(published, user_id=owner) == "owner"


def test_non_uuid_presentation_id_is_rejected_without_touching_the_db(monkeypatch):
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools()
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D", "slides": _slides(1), "presentation_id": "not-a-uuid"},
    )
    assert result.is_error
    assert session.statements == []


# ── Read-back for multi-turn editing ─────────────────────────────────────────


def test_get_presentation_returns_the_current_spec(monkeypatch):
    import app.core.database as database

    user_id = uuid.uuid4()
    existing = _existing_row(user_id, slides=2)
    session = _FakeSession(results=_deck_lookup(existing))
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools(str(user_id))
    content = _run_tool(executor, "get_presentation", {"presentation_id": str(existing.id)}).content

    assert "revision 1" in content
    assert '"slides"' in content
    assert "[title] Slide 0" in content
    # The owner is told the deck is editable in place.
    assert "edit it in place" in content
    # The read-back carries the same marker generate_presentation returns, so
    # "open the deck someone shared with me" draws it inline instead of a JSON
    # dump.
    assert f"[[PRESENTATION:{existing.id}]]" in content


def test_get_presentation_tells_a_read_only_grantee_not_to_edit_in_place(monkeypatch):
    import app.core.database as database

    owner, reader = uuid.uuid4(), uuid.uuid4()
    existing = _existing_row(
        owner,
        slides=2,
        shared_user_ids=[str(reader)],
        shared_user_permissions={str(reader): "read_only"},
    )
    session = _FakeSession(results=_deck_lookup(existing))
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools(str(reader))
    content = _run_tool(executor, "get_presentation", {"presentation_id": str(existing.id)}).content
    assert "READ-ONLY" in content
    assert "duplicate_presentation" in content
    assert '"slides"' in content


def test_get_presentation_rejects_a_non_uuid(monkeypatch):
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools()
    content = _run_tool(executor, "get_presentation", {"presentation_id": "nope"}).content
    assert "valid UUID" in content
    assert session.statements == []


def test_get_presentation_names_the_template_only_as_the_reader_may_see_it(monkeypatch):
    """The ``Template:`` line of a read-back comes from the same
    visibility-checked load as the REST response: a template the reader can
    see is named with its schemes, one that was un-shared reads as a free deck
    - its name never reaches a reader who cannot open it."""
    import app.core.database as database

    user_id = uuid.uuid4()
    template_id = uuid.uuid4()
    existing = _existing_row(user_id, slides=2, template_container_id=template_id)
    _defs, executor = _build_tools(str(user_id))

    # Visible: deck lookup (groups + row), the open comments, then the template
    # lookup (groups + row).
    session = _FakeSession(
        results=[*_deck_lookup(existing), _Result(rows=[]), _Result(rows=[]), _Result(one=_template_row(template_id))]
    )
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    content = _run_tool(executor, "get_presentation", {"presentation_id": str(existing.id)}).content
    assert "Template: Brand Deck (themes: dark only)" in content

    # Un-shared since the deck was generated: the same deck reads as free.
    session = _FakeSession(results=[*_deck_lookup(existing), _Result(rows=[]), _Result(rows=[]), _Result(one=None)])
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    content = _run_tool(executor, "get_presentation", {"presentation_id": str(existing.id)}).content
    assert "Template: none (free deck" in content
    assert "Brand Deck" not in content


# ── Template container integration ───────────────────────────────────


def _template_row(template_id: uuid.UUID, **overrides):
    """A ``TemplateContainer`` row as ``load_visible_template_facts`` reads it."""
    row = {
        "id": template_id,
        "name": "Brand Deck",
        "kind": "presentation",
        "theme_config": {"background": "#0a0f1c", "accent": "#00b4ff"},
        "extra_metadata": {},
    }
    row.update(overrides)
    return SimpleNamespace(**row)


def test_template_id_is_linked_to_the_deck(monkeypatch):
    import app.core.database as database

    template_id = uuid.uuid4()
    # The explicit template is loaded once, as the acting user may see it
    # (existence, kind, schemes, defaults): the caller's group memberships,
    # then the visibility-filtered row.
    session = _FakeSession(results=[_Result(rows=[]), _Result(one=_template_row(template_id))])
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools()
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D", "slides": _slides(1), "template_id": str(template_id)},
    )
    assert not result.is_error, result.content
    assert session.added[0].template_container_id == template_id
    assert "Template: Brand Deck (themes: dark only)" in result.content


def test_invalid_template_id_is_refused(monkeypatch):
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools()
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D", "slides": _slides(1), "template_id": "nope"},
    )
    assert result.is_error
    assert session.added == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"accent": "#00b4ff"}, {"accent": "#00b4ff"}),
        ({"background": "hsl(220 56% 10%)"}, {"background": "hsl(220 56% 10%)"}),
        # Not whitelisted → dropped rather than forwarded.
        ({"palette": {"accent": "#fff"}}, {}),
        # Would escape the CSS declaration it is interpolated into.
        ({"accent": "red; position:fixed"}, {}),
        ({"background": "url(https://evil.example/x.png)"}, {}),
        ({"accent": "}html{display:none"}, {}),
        ({"heading_font": "Inter, sans-serif"}, {"heading_font": "Inter, sans-serif"}),
    ],
)
def test_theme_config_is_whitelisted_and_css_safe(raw, expected):
    from app.services.presentations import sanitize_theme_config

    assert sanitize_theme_config(raw) == expected


def test_theme_resolution_is_visibility_checked():
    """A container the reader can no longer see must contribute nothing rather
    than 404 the deck or leak its brand config.

    Both resolvers — the one-scheme one reports share and the per-scheme
    palette the deck read uses (widened to the template's name and
    supported schemes) — load the container through the SAME
    visibility-checked loader; a second copy of that query could fall behind
    and start leaking an un-shared container's palette. ``resolve_template_themes``
    is the palette resolver's thin ``.themes`` view, so it inherits the check.
    """
    import inspect

    from app.services import presentations

    loader = inspect.getsource(presentations._load_visible_template)
    assert "caller_visible_filter" in loader
    for resolver in (presentations.resolve_template_theme, presentations.resolve_template_palette):
        source = inspect.getsource(resolver)
        assert "_load_visible_template(" in source, resolver.__name__
        assert "visible_filter" not in source, resolver.__name__
    themes_source = inspect.getsource(presentations.resolve_template_themes)
    assert "resolve_template_palette(" in themes_source
    assert "select(" not in themes_source


# ── Theme variants (a palette for the other colour scheme) ───────────────────


@pytest.mark.parametrize(
    ("theme", "scheme"),
    [("light", "light"), ("dark", "dark"), ("filigran", "dark"), (None, "dark")],
)
def test_theme_scheme_puts_every_non_light_theme_on_the_dark_side(theme, scheme):
    from app.services.presentations import theme_scheme

    assert theme_scheme(theme) == scheme


def test_theme_variant_is_sanitized_like_the_base_palette():
    """A variant must never forward a key or a value the base could not: it is
    laid over the base at read time, so it would otherwise be the back door
    around the whitelist."""
    from app.services.presentations import template_theme_variant

    metadata = {
        "theme_variants": {
            "light": {
                "background": "#f2f2f3",
                "accent": "red; position:fixed",  # unsafe -> dropped
                "palette": {"x": 1},  # not whitelisted -> dropped
            },
            "sepia": {"background": "#fff"},  # unknown scheme -> ignored
        }
    }
    assert template_theme_variant(metadata, "light") == {"background": "#f2f2f3"}
    assert template_theme_variant(metadata, "dark") == {}
    assert template_theme_variant(metadata, "sepia") == {}
    # Malformed slots contribute nothing rather than raising.
    assert template_theme_variant({"theme_variants": "light"}, "light") == {}
    assert template_theme_variant({"theme_variants": {"light": []}}, "light") == {}
    assert template_theme_variant(None, "light") == {}


def test_theme_resolution_overlays_the_variant_for_the_decks_scheme():
    """The whole point: a dark brand on a ``light`` deck comes out light, in the
    same brand (fonts + logo keys inherited), and the deck's own scheme keeps
    the one palette untouched."""
    import inspect

    from app.services import presentations

    source = inspect.getsource(presentations.resolve_template_theme)
    assert "deck_theme" in source
    assert "template_theme_variant" in source
    assert "theme_scheme(deck_theme)" in source
    assert "scheme_palette(" in source

    # The pure overlay semantics, on the helpers the resolver composes.
    base = presentations.sanitize_theme_config(
        {
            "background": "#070d18",
            "foreground": "#f2f2f3",
            "heading_font": "'Geologica', sans-serif",
        }
    )
    metadata = {"theme_variants": {"light": {"background": "#f2f2f3", "foreground": "#18191b"}}}
    light = presentations.scheme_palette(
        base,
        presentations.template_theme_variant(metadata, presentations.theme_scheme("light")),
        presentations.theme_scheme("light"),
    )
    assert light == {
        "background": "#f2f2f3",
        "foreground": "#18191b",
        "heading_font": "'Geologica', sans-serif",
    }
    dark = presentations.scheme_palette(
        base,
        presentations.template_theme_variant(metadata, presentations.theme_scheme("filigran")),
        presentations.theme_scheme("filigran"),
    )
    assert dark == base


def test_export_stamp_moves_with_what_the_render_reads_and_nothing_else(monkeypatch):
    """The export tools keep a file by document, revision, format and this stamp:
    it moves when the template's palette moves, when the container is
    edited in a way the palette does not carry (its chrome, a hero, a deck
    default), when an asset is replaced in place on its own row and when an
    asset's description (a picture's alt text) is edited. A
    ``lookup_count`` bump (analytics) leaves it as it was. No template, or one
    the caller may not see, stamps empty."""
    import asyncio
    from datetime import UTC, datetime

    from app.services import presentations, template_containers

    container_id = uuid.uuid4()
    container = SimpleNamespace(
        id=container_id,
        theme_config={"accent": "#42caff"},
        extra_metadata={"chrome": {"footer": "{{title}}"}},
        lookup_count=0,
        updated_at=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
    )
    logo = SimpleNamespace(
        id=uuid.uuid4(),
        asset_role="logo",
        description="Acme logo",
        s3_object_name=f"templates/{container_id}/logo-v1.png",
        content_type="image/png",
        filename="logo.png",
    )

    async def _load(db, cid, *, user_id, is_admin=False):
        return container if cid == container_id else None

    async def _logos(db, c, themes, *, keys):
        return None

    async def _assets(db, cid):
        return [logo] if cid == container_id else []

    monkeypatch.setattr(presentations, "_load_visible_template", _load)
    monkeypatch.setattr(presentations, "_fill_role_logos", _logos)
    monkeypatch.setattr(template_containers, "load_assets", _assets)

    def _stamp(cid=container_id):
        return asyncio.run(
            presentations.resolve_template_export_stamp(None, cid, user_id=uuid.uuid4(), deck_theme="dark")
        )

    first = _stamp()
    assert first
    assert _stamp() == first

    # Analytics touch the row (and its updated_at), not the render.
    container.lookup_count += 1
    container.updated_at = datetime(2026, 11, 1, 10, 1, tzinfo=UTC)
    assert _stamp() == first

    container.extra_metadata = {"chrome": {"footer": "{{event}} - {{title}}"}}
    rechromed = _stamp()
    assert rechromed != first

    # The logo replaced in place: same id, new bytes under a new storage key.
    logo.s3_object_name = f"templates/{container_id}/logo-v2.png"
    relogoed = _stamp()
    assert relogoed != rechromed

    # The alt text the file carries for that picture.
    logo.description = "Acme logo, white on navy"
    redescribed = _stamp()
    assert redescribed != relogoed

    container.theme_config = {"accent": "#ff6a00"}
    assert _stamp() != redescribed

    assert _stamp(None) == ""
    assert _stamp(uuid.uuid4()) == ""


@pytest.mark.parametrize(
    ("value", "scheme"),
    [
        ("#070d18", "dark"),
        ("#F2F2F3", "light"),
        ("#fff", "light"),
        ("#000c", "dark"),  # 4-digit hex: alpha ignored
        ("#ffffff80", "light"),  # 8-digit hex: alpha ignored
        ("hsl(220 54% 8%)", "dark"),  # the seeded XTM One deck
        ("hsl(0, 0%, 100%)", "light"),
        ("hsla(220deg 54% 8% / 0.9)", "dark"),
        ("rgb(7, 13, 24)", "dark"),
        ("rgba(255 255 255 / 1)", "light"),
        ("rgb(100% 100% 100%)", "light"),
        ("white", "light"),
        ("Black", "dark"),
        ("#808080", "light"),  # mid gray: black ink wins, so a light surface
    ],
)
def test_palette_scheme_reads_the_background(value, scheme):
    """Which scheme a palette is designed for is read off its background, in
    every colour syntax a theme value can take."""
    from app.services.presentations import palette_scheme

    assert palette_scheme({"background": value}) == scheme


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "linear-gradient(#000, #fff)",
        "var(--paper)",
        "rebeccapurple",  # a named colour the reader does not know
        "hsl(220 54%)",  # too few channels
        "rgb(a, b, c)",
        "#12345",  # not a hex length
        42,
    ],
)
def test_palette_scheme_is_unknown_when_the_background_cannot_be_read(value):
    """No background, or one the reader cannot parse, means "cannot tell" - and
    the palette is then applied as authored rather than guessed at."""
    from app.services.presentations import palette_scheme, scheme_palette

    palette = {"foreground": "#fff", "accent": "#42caff"}
    if value is not None:
        palette["background"] = value
    assert palette_scheme(palette) is None
    assert scheme_palette(palette, {}, "light") == palette
    assert scheme_palette(palette, {}, "dark") == palette


# ── Supported schemes: what a template can dress, and what a deck may pick ───

_DARK_BASE = {"background": "hsl(220 54% 8%)", "foreground": "#ffffff", "accent": "#42caff"}
_LIGHT_BASE = {"background": "#ffffff", "foreground": "#0b1220", "accent": "#0f6fff"}


@pytest.mark.parametrize(
    ("theme_config", "extra_metadata", "expected"),
    [
        # One palette, one scheme: the one its background reads as.
        (_DARK_BASE, {}, ["dark"]),
        (_LIGHT_BASE, None, ["light"]),
        # The other scheme only through a variant whose own background reads
        # as that scheme...
        (_DARK_BASE, {"theme_variants": {"light": {"background": "#f5f7fb"}}}, ["dark", "light"]),
        (_LIGHT_BASE, {"theme_variants": {"dark": {"background": "#0a0f1c"}}}, ["dark", "light"]),
        # ...never through a "light" variant that is itself dark (a mislabelled
        # palette is not a light palette)...
        (_DARK_BASE, {"theme_variants": {"light": {"background": "#05070c"}}}, ["dark"]),
        # ...while a variant that names no background is taken at its word.
        (_DARK_BASE, {"theme_variants": {"light": {"accent": "#0f6fff"}}}, ["dark", "light"]),
        # A variant for the scheme the base already supports changes nothing.
        (_DARK_BASE, {"theme_variants": {"dark": {"accent": "#fff"}}}, ["dark"]),
        # A variant that sanitizes to nothing is no variant.
        (_DARK_BASE, {"theme_variants": {"light": {"palette": "x"}}}, ["dark"]),
        (_DARK_BASE, {"theme_variants": "light"}, ["dark"]),
        # A base whose scheme cannot be read is applied as authored on both.
        ({"background": "var(--paper)", "accent": "#fff"}, {}, ["dark", "light"]),
        ({"accent": "#42caff"}, {}, ["dark", "light"]),
        (None, None, ["dark", "light"]),
        # Canonical order, never the template's: dark first.
        (_LIGHT_BASE, {"theme_variants": {"dark": {"background": "black"}}}, ["dark", "light"]),
    ],
)
def test_template_supported_schemes_follow_the_palettes(theme_config, extra_metadata, expected):
    """The list the deck's theme picker is restricted to is DERIVED from the
    palettes a template ships - never from a label anyone typed."""
    from app.services.presentations import template_supported_schemes

    assert template_supported_schemes(theme_config, extra_metadata) == expected


def test_container_supported_schemes_is_empty_for_anything_that_is_not_a_deck_template():
    """An asset library or a report template has no deck palette to offer, so
    it supports no deck scheme - the REST serializer and the search filter
    both read this, and the deck's Template menu never lists such a row."""
    from app.services import template_containers as tpl_service

    logos = SimpleNamespace(kind="asset-library", theme_config=_DARK_BASE, extra_metadata={})
    report = SimpleNamespace(kind="report", theme_config=_LIGHT_BASE, extra_metadata={})
    deck = SimpleNamespace(kind="presentation", theme_config=_DARK_BASE, extra_metadata={})
    assert tpl_service.container_supported_schemes(logos) == []
    assert tpl_service.container_supported_schemes(report) == []
    assert tpl_service.container_supported_schemes(deck) == ["dark"]


@pytest.mark.parametrize(
    ("theme", "schemes", "fits", "fitted"),
    [
        ("dark", ["dark"], True, "dark"),
        ("light", ["dark"], False, "dark"),  # snapped to the template's scheme
        ("light", ["dark", "light"], True, "light"),  # kept: the template can dress it
        ("dark", ["light"], False, "light"),
        ("filigran", ["dark"], True, "dark"),  # the retired name IS the dark scheme
        ("filigran", ["light"], False, "light"),
        (" Light ", ["light"], True, "light"),
        (None, ["light"], False, "light"),  # no theme reads as dark (the renderer default)
        (None, ["dark", "light"], True, "dark"),
        (42, ["dark"], True, "dark"),
        # An empty list supports nothing - and there is nothing to snap to, so
        # the deck keeps its scheme (a template with no palette never gets here:
        # non-deck kinds are refused before the fit is consulted).
        ("light", [], False, "light"),
    ],
)
def test_theme_fit_against_the_supported_schemes(theme, schemes, fits, fitted):
    """``theme_fits_schemes`` is the check, ``fit_theme_to_schemes`` the
    "template wins" snap: a deck keeps its scheme whenever the template can
    dress it and lands on the template's first (designed) scheme when not."""
    from app.services.presentation_spec import PRESENTATION_THEMES
    from app.services.presentations import fit_theme_to_schemes, theme_fits_schemes

    assert theme_fits_schemes(theme, schemes) is fits
    assert fit_theme_to_schemes(theme, schemes) == fitted
    # The snap always yields a valid theme name - a theme is a scheme.
    assert fitted in PRESENTATION_THEMES


def test_unsupported_theme_message_is_the_one_sentence_every_surface_uses():
    """REST 422 and the tool error say the same thing about a theme a template
    cannot dress: which theme, which template, what it supports, and the three
    ways out (pick a supported theme, link a template that supports it, unlink)."""
    import inspect

    from app.api.v1 import presentations as api
    from app.services.builtin_tools import presentation as tool_module
    from app.services.presentations import unsupported_theme_message

    one = unsupported_theme_message("light", template_name="XTM One Brand Deck", supported_schemes=("dark",))
    assert one == (
        "Theme 'light' is not supported by the template 'XTM One Brand Deck' (dark only). "
        "Pick a supported theme, link a template that supports 'light', or unlink the template."
    )
    both = unsupported_theme_message("filigran", template_name="T", supported_schemes=("dark", "light"))
    assert "Theme 'dark' is not supported by the template 'T' (dark / light)." in both
    assert "only" not in both
    none = unsupported_theme_message(None, template_name="T", supported_schemes=())
    assert "(no scheme)" in none
    # Both REFUSING paths use it rather than re-spelling the sentence: the REST
    # theme change and the tool's ``update_deck``. Creating a deck under a
    # template never refuses - the template wins and the snap is stated.
    assert "unsupported_theme_message(" in inspect.getsource(api.update_presentation)
    tool_source = inspect.getsource(tool_module)
    assert tool_source.count("unsupported_theme_message(") == 1
    assert "Theme snapped from" in tool_source


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("filigran", "dark"),
        ("FILIGRAN", "dark"),
        (" Light ", "light"),
        ("dark", "dark"),
        # Not a theme: left for the validator to refuse with the field's name.
        ("blabla", "blabla"),
        ("", ""),
        (None, None),
        (7, 7),
    ],
)
def test_normalize_theme_reads_the_legacy_alias_and_nothing_else(value, expected):
    from app.services.presentation_spec import LEGACY_THEME_ALIASES, PRESENTATION_THEMES, normalize_theme

    assert normalize_theme(value) == expected
    # The contract the UI states: a theme is dark or light, nothing else, and
    # the only legacy name is the one the v1 stylesheet hard-coded.
    assert PRESENTATION_THEMES == ("dark", "light")
    assert LEGACY_THEME_ALIASES == {"filigran": "dark"}


def test_template_status_line_names_the_deck_template_and_its_schemes():
    """The ``Template:`` line of every deck tool result is the agent's one
    reading of what the deck wears and which themes it may pick."""
    from app.services.builtin_tools.presentation import template_status_line

    assert template_status_line(None, None) == (
        "Template: none (free deck - renderer theme only, dark or light; link one with template_id)"
    )
    assert template_status_line("Brand Deck", ("dark", "light")) == "Template: Brand Deck (themes: dark / light)"
    assert template_status_line("XTM One", ("dark",)) == "Template: XTM One (themes: dark only)"
    noted = template_status_line("Brand Deck", ("dark",), note="the only template available to you")
    assert noted.startswith("Template: Brand Deck (themes: dark only)")
    assert "the only template available to you" in noted


def test_scheme_palette_forwards_only_the_brand_to_the_opposite_scheme():
    """The residual half of "switch to light, everything stays dark": a template
    with NO light variant used to forward its dark surfaces under
    ``theme: light``, and the inline ``--deck-*`` variables outrank the
    stylesheet's light tokens - so the deck stayed dark while the theme select
    and the dark-on-light logo said light. Now the base's scheme-bound keys stay
    behind and the renderer's light tokens supply paper and ink; accent,
    gradient, fonts and logos - the brand - carry over."""
    from app.services.presentations import (
        SCHEME_BOUND_THEME_KEYS,
        THEME_CONFIG_KEYS,
        scheme_palette,
    )

    # The seeded XTM One Brand Deck, which ships no variant.
    base = {
        "background": "hsl(220 54% 8%)",
        "surface": "hsl(220 45% 12%)",
        "line": "hsl(220 30% 22%)",
        "foreground": "hsl(0 0% 100%)",
        "muted": "hsl(230 4% 70%)",
        "accent": "hsl(270 100% 65%)",
        "info": "hsl(197 100% 53%)",
        "success": "hsl(142 70% 45%)",
        "warning": "hsl(32 95% 55%)",
        "error": "hsl(4 88% 58%)",
        "gradient_from": "hsl(270 100% 65%)",
        "gradient_to": "hsl(197 100% 53%)",
        "heading_font": "'Geologica', system-ui, sans-serif",
        "body_font": "'IBM Plex Sans', system-ui, sans-serif",
        "mono_font": "'IBM Plex Mono', ui-monospace, monospace",
        "logo_file_id": str(uuid.uuid4()),
    }
    assert set(SCHEME_BOUND_THEME_KEYS) < set(THEME_CONFIG_KEYS)

    light = scheme_palette(base, {}, "light")
    assert not set(light) & set(SCHEME_BOUND_THEME_KEYS)
    assert light == {k: v for k, v in base.items() if k not in SCHEME_BOUND_THEME_KEYS}
    assert {"accent", "gradient_from", "gradient_to", "heading_font", "logo_file_id"} <= set(
        light
    )
    # Its own scheme: the palette as authored.
    assert scheme_palette(base, {}, "dark") == base

    # The mirror image - a light-first brand on a dark deck.
    paper = {"background": "#ffffff", "foreground": "#18191b", "accent": "#0015a8"}
    assert scheme_palette(paper, {}, "dark") == {"accent": "#0015a8"}
    assert scheme_palette(paper, {}, "light") == paper

    # A PARTIAL variant does not inherit the other scheme's surfaces either: a
    # light variant that only recolours the paper must not keep a dark ``muted``.
    partial = scheme_palette(base, {"background": "#f2f2f3", "foreground": "#18191b"}, "light")
    assert partial["background"] == "#f2f2f3"
    assert partial["foreground"] == "#18191b"
    assert "muted" not in partial and "surface" not in partial
    assert partial["accent"] == base["accent"]

    # The chart series palette is tuned for its surface too: a dark
    # brand's bright series hues stay behind on a light deck (the renderer's
    # light set takes over, ``chart_1`` falling back to the accent), and a
    # light variant supplies only the hues it names.
    charted = {**base, "chart_1": "#42caff", "chart_2": "#00f0bc", "chart_6": "#7587ff"}
    assert not {k for k in scheme_palette(charted, {}, "light") if k.startswith("chart_")}
    recoloured = scheme_palette(charted, {"chart_2": "#009474"}, "light")
    assert recoloured["chart_2"] == "#009474"
    assert "chart_1" not in recoloured and "chart_6" not in recoloured
    assert scheme_palette(charted, {}, "dark") == charted


def test_frontend_preview_mirrors_the_scheme_bound_keys():
    """The template preview builds its palette client-side (the template may not
    be saved yet), so `previewThemeForDeck` re-implements `scheme_palette`; the
    key list it filters on must be the backend's, or the preview shows a light
    deck production would draw differently."""
    import re

    from app.services.presentations import SCHEME_BOUND_THEME_KEYS

    source = (FRONTEND_ROOT / "src" / "components" / "presentation" / "previewSpec.ts").read_text(
        encoding="utf-8"
    )
    match = re.search(r"export const SCHEME_BOUND_THEME_KEYS[^=]*=\s*\[(.*?)\];", source, re.DOTALL)
    assert match, "previewSpec.ts must export SCHEME_BOUND_THEME_KEYS"
    keys = tuple(re.findall(r'"([a-z0-9_]+)"', match.group(1)))
    assert keys == SCHEME_BOUND_THEME_KEYS
    assert "paletteScheme(" in source and "cssColorLuminance(" in source
    # The preview applies the same rule the resolver does.
    preview_fn = source.split("export function previewThemeForDeck(", 1)[1]
    assert "paletteScheme(base)" in preview_fn
    assert "SCHEME_BOUND_THEME_KEYS" in preview_fn
    # And the dialog offers the switch for every template, not only those with
    # a variant - a variant-less template now has a real light rendering.
    dialog = (
        FRONTEND_ROOT / "src" / "components" / "presentation" / "TemplatePreviewDialog.tsx"
    ).read_text(encoding="utf-8")
    assert "templateThemeSchemes" not in dialog
    assert "hasVariants" not in dialog


def test_deck_reads_pass_the_decks_theme_to_the_resolver():
    """Every place a deck's theme is resolved for rendering must pass the deck's
    theme, or a light deck on the brand template silently renders dark again."""
    import inspect

    from app.api.v1 import presentations as api

    source = inspect.getsource(api)
    # Each chunk is one call's argument list (the import names the function
    # without a parenthesis, so it is not a chunk).
    calls = source.split("resolve_template_theme(")[1:]
    assert calls, "expected resolve_template_theme calls in the presentations API"
    for chunk in calls:
        args = chunk.split(")", 1)[0]
        assert "deck_theme=" in args, args


# ── Live theme preview: the palette of EVERY scheme travels with the deck ─────


async def test_resolve_template_themes_overlays_each_variant_over_the_base(monkeypatch):
    """One container load, one logo lookup, a palette per scheme: the dark
    entry is the base (no dark variant here), the light entry is the base with
    the light variant laid over it, and a role-resolved logo lands on both —
    a role is a property of the container, not of a colour scheme."""
    from types import SimpleNamespace

    from app.services import presentations

    container = SimpleNamespace(
        id=uuid.uuid4(),
        theme_config={
            "background": "#070d18",
            "foreground": "#f2f2f3",
            "heading_font": "'Geologica', sans-serif",
        },
        extra_metadata={
            "theme_variants": {"light": {"background": "#f2f2f3", "foreground": "#18191b"}}
        },
    )
    loads: list[uuid.UUID] = []

    async def fake_load(db, container_id, *, user_id, is_admin=False):
        loads.append(container_id)
        return container

    fills: list[list[dict[str, str]]] = []

    async def fake_fill(db, loaded, themes, *, keys):
        fills.append(themes)
        for theme in themes:
            theme.setdefault("logo_file_id", "logo-id")

    monkeypatch.setattr(presentations, "_load_visible_template", fake_load)
    monkeypatch.setattr(presentations, "_fill_role_logos", fake_fill)

    themes = await presentations.resolve_template_themes(
        None, container.id, user_id=uuid.uuid4()
    )

    assert set(themes) == set(presentations.THEME_SCHEMES) == {"dark", "light"}
    assert themes["dark"] == {
        "background": "#070d18",
        "foreground": "#f2f2f3",
        "heading_font": "'Geologica', sans-serif",
        "logo_file_id": "logo-id",
    }
    assert themes["light"] == {
        "background": "#f2f2f3",
        "foreground": "#18191b",
        "heading_font": "'Geologica', sans-serif",
        "logo_file_id": "logo-id",
    }
    # Exactly one container load and one logo lookup serve both schemes.
    assert loads == [container.id]
    assert len(fills) == 1 and len(fills[0]) == 2
    # Each scheme's entry is what the one-scheme resolver would have returned.
    for scheme in presentations.THEME_SCHEMES:
        single = await presentations.resolve_template_theme(
            None, container.id, user_id=uuid.uuid4(), deck_theme=scheme
        )
        assert single == themes[scheme], scheme

    # A template with NO light variant: the light entry is the brand half only
    # (the renderer's light tokens supply paper and ink), the logo still lands.
    container.extra_metadata = {}
    themes = await presentations.resolve_template_themes(None, container.id, user_id=uuid.uuid4())
    assert themes["dark"] == {
        "background": "#070d18",
        "foreground": "#f2f2f3",
        "heading_font": "'Geologica', sans-serif",
        "logo_file_id": "logo-id",
    }
    assert themes["light"] == {
        "heading_font": "'Geologica', sans-serif",
        "logo_file_id": "logo-id",
    }
    single = await presentations.resolve_template_theme(
        None, container.id, user_id=uuid.uuid4(), deck_theme="light"
    )
    assert single == themes["light"]
    # Reports name no deck theme and keep the one palette, as before.
    report = await presentations.resolve_template_theme(None, container.id, user_id=uuid.uuid4())
    assert report == themes["dark"]


async def test_resolve_template_themes_is_empty_per_scheme_without_a_template(monkeypatch):
    """No template, or one the caller may not see, contributes an EMPTY palette
    for each scheme — never a missing key, which the renderer would read as
    'fall back to the saved theme's palette' and keep the deck dark."""
    from app.services import presentations

    assert await presentations.resolve_template_themes(None, None, user_id=None) == {
        "dark": {},
        "light": {},
    }

    async def invisible(db, container_id, *, user_id, is_admin=False):
        return None

    monkeypatch.setattr(presentations, "_load_visible_template", invisible)
    assert await presentations.resolve_template_themes(
        None, uuid.uuid4(), user_id=uuid.uuid4()
    ) == {"dark": {}, "light": {}}


def test_serialized_deck_carries_the_palette_of_every_scheme():
    """`theme_config` stays the SAVED theme's palette (what a reader renders);
    `theme_configs` is the per-scheme map the editor previews a theme switch
    from. A caller that resolved no map emits an empty map — not empty
    palettes — so the renderer falls back to `theme_config` for it."""
    from app.services.presentations import serialize_presentation

    deck = _existing_row(uuid.uuid4())
    themes = {"dark": {"background": "#000"}, "light": {"background": "#fff"}}

    payload = serialize_presentation(deck, theme_config=themes["dark"], theme_configs=themes)
    assert payload["theme_config"] == {"background": "#000"}
    assert payload["theme_configs"] == themes
    # A copy, not the resolver's own dicts.
    assert payload["theme_configs"]["light"] is not themes["light"]

    bare = serialize_presentation(deck, theme_config={"background": "#000"})
    assert bare["theme_configs"] == {}

    # The list shape never carries either (no spec, no palette).
    summary = serialize_presentation(deck, theme_configs=themes, include_spec=False)
    assert "theme_configs" not in summary and "theme_config" not in summary


def test_both_shapes_name_the_template_the_deck_wears():
    """The library card says WHICH template a deck wears, not just a
    scheme: `template_name` rides on the list shape too, and a free deck (or a
    template the reader cannot see) reads `None` on both."""
    from app.services.presentations import serialize_presentation

    deck = _existing_row(uuid.uuid4())
    assert serialize_presentation(deck, include_spec=False, template_name="Brand Deck")["template_name"] == "Brand Deck"
    assert serialize_presentation(deck, template_name="Brand Deck")["template_name"] == "Brand Deck"
    assert serialize_presentation(deck, include_spec=False)["template_name"] is None
    assert serialize_presentation(deck)["template_name"] is None


@pytest.mark.asyncio
async def test_template_names_resolve_in_one_visible_batch():
    """One query names every template a list of decks wears; a template the
    reader cannot see is absent (the card shows a free deck, as the palette
    resolution would), and a list with no linked deck runs no query at all."""
    from app.services.presentations import resolve_template_names

    visible, hidden = uuid.uuid4(), uuid.uuid4()
    session = _FakeSession([_Result(rows=[(visible, "  Brand Deck "), (uuid.uuid4(), "")])])
    names = await resolve_template_names(
        session, [visible, None, hidden, visible], user_id=uuid.uuid4(), user_group_ids=[]
    )
    assert names[str(visible)] == "Brand Deck"
    assert str(hidden) not in names
    # A blank name never reaches a card as an empty label.
    assert set(names.values()) == {"Brand Deck", "template"}
    assert len(session.statements) == 1

    empty = _FakeSession()
    assert await resolve_template_names(empty, [None, None], user_id=uuid.uuid4()) == {}
    assert empty.statements == []


def test_the_list_endpoint_names_templates_in_one_batch():
    """The list resolves the template names ONCE for the page, never per row."""
    import inspect

    from app.api.v1 import presentations as api

    source = inspect.getsource(api.list_presentations)
    assert source.count("resolve_template_names(") == 1
    assert "template_name=template_names.get(" in source


def test_deck_read_serves_the_saved_palette_and_the_per_scheme_map():
    """The deck read resolves every scheme ONCE and derives the saved theme's
    palette from that map — one container load, and the two can never
    disagree on what the saved theme's palette is."""
    import inspect

    from app.api.v1 import presentations as api

    source = inspect.getsource(api._response)
    assert "resolve_template_palette(" in source
    assert "theme_configs=palette.themes" in source
    assert "theme_config=palette.theme_for(presentation.theme)" in source
    # The same load feeds the theme picker's restriction.
    assert "supported_schemes=palette.supported_schemes" in source
    assert "template_name=palette.name" in source
    # ...and there is exactly one container load in the read path.
    assert source.count("resolve_template_") == 1


def test_frontend_renders_the_palette_of_the_theme_on_screen():
    """Every host that puts a deck theme on the DOM (`data-deck-theme`) must
    resolve its inline palette through `themeConfigFor(...)` — the palette of
    the theme it RENDERS — never read `presentation.theme_config` (the SAVED
    theme's) directly. That direct read is exactly the bug: the editor's draft
    of `theme: light` rendered under the still-dark saved palette, and the
    inline palette outranks the theme tokens, so the deck stayed dark.

    `themeScheme` mirrors the backend `theme_scheme` (light → light, anything
    else → dark), and the panel/viewer pair actually wires the live draft.

    A host may resolve PER SLIDE instead, through
    `resolveSlideScheme(...)` - the wrapper that reads a slide's own theme and
    then `themeConfigFor` for THAT scheme; either read is the palette of what
    is on screen. A control OUTSIDE the stage (the inspector's hero colour
    chips) is handed a `SlidePalette` - the scheme and the tokens its caller
    resolved for the slide through the same pair - and carries it on a
    `.deck-palette` span, so it holds no palette read of its own to get wrong.
    """
    import re

    src = FRONTEND_ROOT / "src"
    hosts = [
        path
        for path in src.rglob("*.tsx")
        if "node_modules" not in path.parts and "data-deck-theme=" in path.read_text(encoding="utf-8")
    ]
    assert hosts, "expected at least one deck host setting data-deck-theme"
    for path in hosts:
        text = path.read_text(encoding="utf-8")
        assert "themeConfigFor(" in text or "resolveSlideScheme(" in text or "palette: SlidePalette" in text, path
        assert not re.search(r"themeStyle\(\s*presentation\.theme_config\s*\)", text), path

    theme = (src / "components" / "presentation" / "theme.ts").read_text(encoding="utf-8")
    assert 'return theme === "light" ? "light" : "dark";' in theme
    assert "theme_configs?.[themeScheme(theme)] ?? presentation.theme_config" in theme

    panel = (src / "components" / "presentation" / "PresentationEditorPanel.tsx").read_text(
        encoding="utf-8"
    )
    # The draft the host renders is the deck AS IT WOULD BE SAVED: the
    # spec with the edits, plus the design a template change brings - the
    # palette per scheme, the supported schemes and the template's name - so a
    # template picked in the panel restyles the stage before the save. A clean
    # panel hands back `null`, and the stage shows the stored deck again.
    assert "const spec = specWithDraft(presentation.spec, draft);" in panel
    assert "template_container_id: draft.template_container_id," in panel
    assert "theme_config: themeConfigs[draft.theme] ?? EMPTY_PALETTE," in panel
    assert "theme_configs: themeConfigs," in panel
    assert "supported_schemes: supportedSchemes," in panel
    assert "onDraftChange?.(null);" in panel
    viewer = (src / "pages" / "PresentationViewer.tsx").read_text(encoding="utf-8")
    # One side-panel slot serves both kinds (editor / comments / history), so
    # the editor is mounted once and its draft flows to whichever stage renders.
    assert viewer.count("onDraftChange={setDraft}") == 1
    # The overlay is spread over the stored row, so `themeConfigFor` reads the
    # draft's palette exactly as it reads a saved deck's; the SLIDES come from
    # the editor session in the same render as the edit, the overlay's
    # design one render behind.
    assert "? { ...data, ...(draft ?? {}), spec: liveSpec, title: liveSpec.title }" in viewer
    assert "specWithDraft(data.spec, liveDraft)" in viewer
    # Both stages (deck and page document) render the draft-aware deck; the
    # panel itself works from the editor SESSION, seeded from the STORED deck
    # (`useDeckEditor(data, ...)`), which is what it diffs its draft against
    # and what `expected_revision` is taken from.
    assert viewer.count("presentation={shown}") == 2
    assert re.search(r"useDeckEditor\(data,", viewer)
    panel_mounts = re.findall(r"<PresentationEditorPanel\s+editor=\{(\w+)\}", viewer)
    assert panel_mounts == ["editor"]


def test_theme_variants_are_validated_on_every_write_path():
    """Authoring tool and package import both run the variant validator, so no
    write path can store a palette the read-time overlay would have to drop."""
    import inspect

    from app.api.v1 import template_containers as api
    from app.services.builtin_tools import template_authoring
    from app.services.template_containers import validate_theme_variants

    assert "validate_theme_variants(" in inspect.getsource(template_authoring)
    assert "validate_theme_variants(" in inspect.getsource(api._persist_imported_package)

    accepted, problems = validate_theme_variants(
        {
            "guidance": "keep",
            "theme_variants": {
                "light": {"background": "#fff", "accent": "url(x)"},
                "sepia": {"background": "#fff"},
            },
        }
    )
    assert accepted["guidance"] == "keep"
    assert accepted["theme_variants"] == {"light": {"background": "#fff"}}
    assert any("theme_variants.light" in p for p in problems)
    assert any("theme_variants.sepia" in p for p in problems)
    # No slot -> untouched, no complaints.
    assert validate_theme_variants({"guidance": "x"}) == ({"guidance": "x"}, [])
    # A non-object slot is reported, not silently accepted.
    _, bad = validate_theme_variants({"theme_variants": ["light"]})
    assert bad and "must be an object" in bad[0]


# ── Duplicating a deck through the agent ────────────────────────────

#: The footer pattern the brand decks ship (``template_container_seed``): it
#: reads ``{{title}}``, so a copy's new title is drawn on every slide.
_BRAND_FOOTER = {
    "footer": "{{event}} - {{title}} - {{classification}}",
    "variables": [{"key": "classification", "label": "Classification"}],
}
_Q4_TITLE = "Quarterly Review, Q4 2026"


def _quarterly_review(owner: uuid.UUID | None, *, template_id: uuid.UUID | None = None, **columns):
    """The Q3 edition of a recurring deck as it is stored: validated (slide ids
    assigned), a ratio and a transition that are not the defaults, deck chrome,
    and a speaker note on every slide."""
    from app.models.presentation import Presentation

    spec = validate_spec(
        {
            "title": "Quarterly Review, Q3 2026",
            "aspect_ratio": "4:3",
            "transition": "fade",
            "chrome": {
                "event_label": "Executive committee",
                "fields": {"classification": "Internal"},
                "show_slide_numbers": False,
            },
            "slides": [
                {"layout": "title", "title": "Quarterly Review", "subtitle": "Q3 2026", "notes": "Open on the headline."},
                {
                    "layout": "content",
                    "title": "Highlights",
                    "blocks": [{"type": "list", "items": ["ARR +12%", "Two new logos"]}],
                    "notes": "One sentence per highlight.",
                },
                {"layout": "closing", "title": "Questions", "notes": "Take questions."},
            ],
        }
    )
    return Presentation(
        id=uuid.uuid4(),
        user_id=owner,
        title=spec["title"],
        theme=spec["theme"],
        slide_count=len(spec["slides"]),
        revision=7,
        spec=spec,
        template_container_id=template_id,
        **columns,
    )


def _duplicate_session(source, *, template=None, actor=None) -> _FakeSession:
    """The results in the order the tool reads them: the source deck (groups,
    row), the copy's first version (no snapshot yet, none to prune), its
    template as the caller may see it (groups, row - only for a deck that
    wears one), then the acting user for the audit entry."""
    template_lookup = [_Result(rows=[]), _Result(one=template)] if source.template_container_id else []
    return _FakeSession(results=[*_deck_lookup(source), _Result(), _Result(), *template_lookup, _Result(one=actor)])


def _record_layout_checks(monkeypatch) -> list:
    from app.services.builtin_tools import presentation as tool_module

    checks: list = []

    async def _check(deck_id, _user_uuid, _spec, *, revision=None, written_slide_ids=None):
        checks.append((deck_id, revision, written_slide_ids))
        return ""

    monkeypatch.setattr(tool_module, "layout_check_note", _check)
    return checks


def test_duplicate_presentation_starts_next_quarter_from_the_approved_deck(monkeypatch):
    """One call turns the Q3 deck into the Q4 one: the slides are the stored
    ones, ids and notes included, under the same template, theme, ratio and
    chrome; the copy is the caller's, at revision 1 of a history of its own;
    the footer reads the new title; and the result hands back what
    edit_presentation needs next."""
    import app.core.database as database
    from app.models.audit_log import AuditLog
    from app.models.presentation import Presentation, PresentationVersion

    user_id, template_id = uuid.uuid4(), uuid.uuid4()
    conversation_id = str(uuid.uuid4())
    source = _quarterly_review(user_id, template_id=template_id)
    before = copy.deepcopy(source.spec)
    brand = _template_row(template_id, name="Filigran Brand Deck", extra_metadata={"chrome": _BRAND_FOOTER})
    actor = SimpleNamespace(id=user_id, email="assistant-owner@example.com")
    session = _duplicate_session(source, template=brand, actor=actor)
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    checks = _record_layout_checks(monkeypatch)

    _defs, executor = _build_tools(str(user_id), conversation_id)
    result = _run_tool(executor, "duplicate_presentation", {"presentation_id": str(source.id), "title": _Q4_TITLE})

    assert not result.is_error, result.content
    # One new deck, the first entry of its history, the audit entry: no
    # comment, grant or other row comes along with the copy.
    assert [type(r) for r in session.added] == [Presentation, PresentationVersion, AuditLog]
    duplicate, version, audit = session.added
    assert duplicate.id != source.id
    assert duplicate.title == _Q4_TITLE
    assert duplicate.spec["title"] == _Q4_TITLE
    assert duplicate.spec["slides"] == before["slides"]
    for key in ("theme", "aspect_ratio", "transition", "chrome"):
        assert duplicate.spec[key] == before[key], key
    assert duplicate.template_container_id == template_id
    assert duplicate.user_id == user_id
    assert duplicate.updated_by_user_id == user_id
    assert str(duplicate.conversation_id) == conversation_id
    assert duplicate.revision == 1
    assert (version.revision, version.source, version.author_id) == (1, "duplicate", user_id)
    assert version.spec == duplicate.spec
    assert session.committed
    assert (audit.action, audit.entity_type, audit.entity_id) == ("create", "presentation", duplicate.id)
    assert audit.details == {"via": "agent", "source_id": str(source.id), "slide_count": 3}
    # The original is untouched.
    assert source.spec == before
    assert (source.revision, source.title) == (7, "Quarterly Review, Q3 2026")

    content = result.content
    assert 'Footer: reads "Executive committee - Quarterly Review, Q4 2026 - Internal"' in content
    # The title is on every slide through the footer: the whole copy is measured.
    assert checks == [(duplicate.id, 1, None)]
    assert "Template: Filigran Brand Deck (themes: dark only)" in content
    for slide in before["slides"]:
        assert f"id={slide['id']}" in content
    assert f"presentation_id: `{duplicate.id}`" in content
    assert "edit_presentation" in content
    assert f"[[PRESENTATION:{duplicate.id}]]" in content
    assert f"[[PRESENTATION:{source.id}]]" not in content


@pytest.mark.parametrize("grant", ["read_only", "company_managed"])
def test_reading_a_deck_is_enough_to_copy_it_and_the_copy_shares_nothing(monkeypatch, grant):
    """As in the UI: a deck shared read-only, or published to the company, is
    copied by whoever can read it. The copy is theirs alone - none of the
    original's grants, not company-managed."""
    import app.core.database as database

    owner, reader, group = uuid.uuid4(), uuid.uuid4(), str(uuid.uuid4())
    if grant == "read_only":
        source = _quarterly_review(
            owner,
            shared_user_ids=[str(reader)],
            shared_user_permissions={str(reader): "read_only"},
            shared_group_ids=[group],
            shared_group_permissions={group: "config"},
        )
    else:
        source = _quarterly_review(None, is_company_managed=True)
    session = _duplicate_session(source)
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _record_layout_checks(monkeypatch)

    _defs, executor = _build_tools(str(reader))
    result = _run_tool(executor, "duplicate_presentation", {"presentation_id": str(source.id), "title": _Q4_TITLE})

    assert not result.is_error, result.content
    duplicate = session.added[0]
    assert duplicate.user_id == reader
    assert not duplicate.is_company_managed
    assert not duplicate.shared_user_ids
    assert not duplicate.shared_user_permissions
    assert not duplicate.shared_group_ids
    assert not duplicate.shared_group_permissions
    assert duplicate.spec["slides"] == source.spec["slides"]
    assert "shared with nobody" in result.content


def test_a_deck_the_user_cannot_read_is_refused_and_nothing_is_written(monkeypatch):
    """The source is loaded through the platform's visibility predicate in SQL,
    so a deck the user cannot read gets exactly the answer an unknown id gets,
    and no copy is made."""
    import app.core.database as database

    session = _FakeSession(results=_deck_lookup(None))
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    user_id = uuid.uuid4()
    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(executor, "duplicate_presentation", {"presentation_id": str(uuid.uuid4()), "title": _Q4_TITLE})

    assert result.is_error
    assert "you can access was found" in result.content
    assert session.added == []
    assert not session.committed
    sql = _compiled(session.statements[1])
    assert "presentations.user_id" in sql
    assert str(user_id) in sql
    assert "shared_user_ids" in sql
    assert "is_company_managed" in sql


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"presentation_id": "not-a-uuid", "title": _Q4_TITLE}, "must be the UUID of the deck to copy"),
        ({"presentation_id": "0b3e1f52-7a1c-4d2e-9f00-1c2d3e4f5a6b"}, "title is required"),
        ({"presentation_id": "0b3e1f52-7a1c-4d2e-9f00-1c2d3e4f5a6b", "title": "   "}, "title is required"),
        (
            {"presentation_id": "0b3e1f52-7a1c-4d2e-9f00-1c2d3e4f5a6b", "title": "x" * (MAX_TITLE_CHARS + 1)},
            f"the limit is {MAX_TITLE_CHARS}",
        ),
    ],
    ids=["malformed-id", "no-title", "blank-title", "title-too-long"],
)
def test_duplicate_arguments_are_checked_before_the_database(monkeypatch, arguments, expected):
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools()
    result = _run_tool(executor, "duplicate_presentation", arguments)
    assert result.is_error
    assert expected in result.content
    assert session.statements == []


def test_an_anonymous_caller_cannot_duplicate_a_deck(monkeypatch):
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools(user_id="anonymous")
    result = _run_tool(executor, "duplicate_presentation", {"presentation_id": str(uuid.uuid4()), "title": _Q4_TITLE})
    assert result.is_error
    assert "signed-in users" in result.content
    assert session.statements == []


def test_a_stored_deck_that_no_longer_validates_is_not_copied(monkeypatch):
    import app.core.database as database
    from app.models.presentation import Presentation

    user_id = uuid.uuid4()
    broken = Presentation(
        id=uuid.uuid4(), user_id=user_id, title="Old", theme="dark", slide_count=0, revision=1,
        spec={"title": "Old", "slides": []},
    )
    session = _FakeSession(results=_deck_lookup(broken))
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(executor, "duplicate_presentation", {"presentation_id": str(broken.id), "title": _Q4_TITLE})
    assert result.is_error
    assert "no longer passes validation" in result.content
    assert "generate_presentation" in result.content
    assert session.added == []
    assert not session.committed


def test_the_copy_is_measured_only_when_its_title_is_drawn_on_every_slide(monkeypatch):
    """The copy's slides are the original's: only a new title drawn through a
    template footer that reads ``{{title}}`` changes what a slide shows. A free
    deck, or a footer that never shows the title, renders nothing."""
    import app.core.database as database

    user_id, template_id = uuid.uuid4(), uuid.uuid4()
    checks = _record_layout_checks(monkeypatch)
    _defs, executor = _build_tools(str(user_id))

    free = _quarterly_review(user_id)
    monkeypatch.setattr(database, "async_session", _session_factory(_duplicate_session(free)))
    result = _run_tool(executor, "duplicate_presentation", {"presentation_id": str(free.id), "title": _Q4_TITLE})
    assert not result.is_error, result.content
    assert "Template: none (free deck" in result.content

    fixed = _template_row(template_id, extra_metadata={"chrome": {**_BRAND_FOOTER, "footer": "{{event}} - {{classification}}"}})
    branded = _quarterly_review(user_id, template_id=template_id)
    monkeypatch.setattr(database, "async_session", _session_factory(_duplicate_session(branded, template=fixed)))
    result = _run_tool(executor, "duplicate_presentation", {"presentation_id": str(branded.id), "title": _Q4_TITLE})
    assert not result.is_error, result.content
    assert 'Footer: reads "Executive committee - Internal"' in result.content
    assert checks == []


def test_the_tool_and_the_library_copy_share_one_service():
    """The agent's copy and the library's ``Duplicate`` are one function, so
    what a copy keeps and drops cannot differ between the two surfaces."""
    import inspect

    from app.api.v1 import presentations as api
    from app.services.builtin_tools import presentation as mod

    source = inspect.getsource(mod.build_presentation_tools)
    handler = source[source.index("async def _duplicate_presentation(") :]
    handler = handler[: handler.index("\n    async def ", 1)]
    assert "await duplicate_presentation(" in handler
    assert "load_visible_presentation(" in handler
    assert "save_presentation(" not in handler
    assert "duplicate_presentation(" in inspect.getsource(api.duplicate_presentation_endpoint)

    defs, executor = _build_tools()
    definition = next(d for d in defs if d.name == "duplicate_presentation")
    assert definition.input_schema["required"] == ["presentation_id", "title"]
    assert definition.source == "builtin:generate_presentation"
    assert "duplicate_presentation" in executor.tool_names


# ── Registration / wiring ────────────────────────────────────────────────────

# The review-comment tools ride along with the deck tools and are never
# gated: reading, leaving and closing a note is part of editing a deck with an
# assistant, where exporting a file is only offered when the request asks.
_COMMENT_TOOLS = ["list_presentation_comments", "add_presentation_comment", "resolve_presentation_comment"]
#: Offered on every turn, unlike ``export_presentation``: looking at your own
#: render is not a file the user asked for, and sharing and the public
#: link are asked for in words a request gate cannot tell from a "yes"
#: to the agent's proposal.
_ALWAYS_OFFERED_TAIL = [
    *_COMMENT_TOOLS,
    "capture_presentation_slides",
    "share_presentation",
    "set_presentation_public_link",
    "presentation_poll",
]


def test_generate_presentation_is_a_recognized_builtin_tool_id():
    from app.services.builtin_tools.agent_builder import _VALID_BUILTIN_TOOL_IDS

    assert "generate_presentation" in _VALID_BUILTIN_TOOL_IDS


def test_builtin_catalog_advertises_every_sub_tool():
    from app.services.builtin_tool_catalog import build_builtin_tool_catalog

    catalog = build_builtin_tool_catalog()
    names = [entry["name"] for entry in catalog["generate_presentation"]]
    assert names == [
        "generate_presentation",
        "edit_presentation",
        "get_presentation",
        "search_presentation_text",
        "duplicate_presentation",
        "delete_presentation",
        *_ALWAYS_OFFERED_TAIL,
        "export_presentation",
    ]


def test_tool_schema_documents_the_layout_catalog():
    defs, _executor = _build_tools()
    generate = next(d for d in defs if d.name == "generate_presentation")
    layout_enum = generate.input_schema["properties"]["slides"]["items"]["properties"]["layout"]["enum"]
    assert layout_enum == list(SLIDE_LAYOUTS)
    assert generate.input_schema["required"] == ["title", "slides"]
    theme_enum = generate.input_schema["properties"]["theme"]["enum"]
    assert theme_enum == list(PRESENTATION_THEMES)


def test_tool_description_steers_away_from_generate_file_and_documents_editing():
    defs, _executor = _build_tools()
    description = next(d for d in defs if d.name == "generate_presentation").description
    assert "generate_file" in description
    assert "presentation_id" in description
    assert "[[PRESENTATION:" in description


@pytest.mark.parametrize(
    ("module_path", "function_name"),
    [
        ("app.services.agent_chat", "run_agent_chat"),
        ("app.services.general_chat", None),
        ("app.services.assignment_engine", "execute_assignment_run"),
    ],
)
def test_every_agentic_entry_point_wires_the_tool(module_path, function_name):
    """Chat, the general assistant and assignment runs must all build the tool
    when the agent opts in — a missing branch silently drops the capability on
    that path only, which is the classic way a builtin half-ships."""
    import importlib
    import inspect

    module = importlib.import_module(module_path)
    source = inspect.getsource(getattr(module, function_name)) if function_name else inspect.getsource(module)
    assert '"generate_presentation" in' in source
    assert "build_presentation_tools(" in source


def _presentation_tool_builder_calls(module_path: str, function_name: str) -> list:
    """Every ``build_presentation_tools(...)`` call in one entry point, as AST
    nodes — the shared walk behind the call-site tripwires below."""
    import ast
    import importlib
    import inspect
    import textwrap

    module = importlib.import_module(module_path)
    tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(module, function_name))))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None))
        == "build_presentation_tools"
    ]
    assert calls, f"{module_path}.{function_name} no longer builds the presentation tools"
    return calls


def test_assignment_runs_stamp_the_deck_with_their_run():
    """Without this the deck of a scheduled/delegated run is orphaned (neither
    conversation- nor run-scoped), exactly the ``generate_file`` bug."""
    calls = _presentation_tool_builder_calls("app.services.assignment_engine", "execute_assignment_run")
    assert all(any(kw.arg == "assignment_run_id" for kw in call.keywords) for call in calls), (
        "execute_assignment_run must call build_presentation_tools with "
        "assignment_run_id=... so run-generated decks stay attributable"
    )


# ── Export ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "request_text",
    [
        "Remove slide 24 and add a focus slide for each strategic objective",
        "export it as pptx",
        "",
        None,
    ],
)
def test_the_export_tool_is_offered_whatever_the_request_says(request_text):
    """Whether the user wants a file is the model's call, made on the tool's
    description: the platform reads no word of the request for it."""
    from app.services.builtin_tools import build_presentation_tools

    defs, executor = build_presentation_tools(
        db=None,  # type: ignore[arg-type]
        user_id=str(uuid.uuid4()),
        conversation_id=None,
        request_text=request_text,
    )
    assert [d.name for d in defs] == [
        "generate_presentation",
        "edit_presentation",
        "get_presentation",
        "search_presentation_text",
        "duplicate_presentation",
        "delete_presentation",
        *_ALWAYS_OFFERED_TAIL,
        "export_presentation",
    ]
    assert "export_presentation" in executor.tool_names


@pytest.mark.parametrize(
    ("module_path", "function_name"),
    [
        ("app.services.agent_chat", "run_agent_chat"),
        ("app.services.general_chat", "run_general_chat"),
        ("app.services.assignment_engine", "execute_assignment_run"),
    ],
)
def test_every_agentic_entry_point_hands_the_template_choice_its_request(module_path, function_name):
    """Every turn must hand the builder the text it is acting on: the template
    choice of a new deck reads it, and a call site that forgets
    ``request_text`` refuses, on that path only, the template the user named."""
    for call in _presentation_tool_builder_calls(module_path, function_name):
        assert any(kw.arg == "request_text" for kw in call.keywords), (
            f"{module_path}.{function_name} must call build_presentation_tools with "
            "request_text=<the text the agent acts on> for the template choice"
        )


def test_tool_never_writes_on_the_callers_session():
    """Tool closures run mid agentic loop after the caller released its pooled
    connection; flushing on ``db`` would pin a connection for minutes."""
    import inspect

    from app.services.builtin_tools import presentation as mod

    source = inspect.getsource(mod.build_presentation_tools)
    assert "del db" in source
    assert "async_session()" in source


# ── REST surface ─────────────────────────────────────────────────────────────


def _rest_user(user_id=None, *, admin: bool = False):
    return SimpleNamespace(
        id=user_id or uuid.uuid4(),
        email="someone@example.com",
        role="admin" if admin else "user",
        is_admin=admin,
    )


def test_read_endpoint_runs_the_visibility_predicate_in_sql():
    """A deck the caller cannot see is a 404, decided by the SAME three-tier
    predicate every other entity uses — not by an owner check after the fact."""
    from fastapi import HTTPException

    from app.api.v1.presentations import _load_visible

    owner = uuid.uuid4()
    deck = _existing_row(owner)
    session = _FakeSession(results=_deck_lookup(deck))
    loaded, _groups, tier = asyncio.run(_load_visible(session, _rest_user(owner), str(deck.id)))
    assert loaded is deck
    assert tier == "owner"
    sql = _compiled(session.statements[1])
    assert "shared_user_ids" in sql and "is_company_managed" in sql

    # Filtered out in SQL -> 404, never 403 (existence is not disclosed).
    session = _FakeSession(results=_deck_lookup(None))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_load_visible(session, _rest_user(), str(deck.id)))
    assert exc.value.status_code == 404

    # Admins bypass the predicate (``sa_true``) and get the manage tier on a
    # colleague's deck; an orphaned deck (owner deleted) is theirs to manage.
    session = _FakeSession(results=_deck_lookup(deck))
    _loaded, _groups, tier = asyncio.run(_load_visible(session, _rest_user(admin=True), str(deck.id)))
    assert tier == "config"
    orphan = _existing_row(None)
    session = _FakeSession(results=_deck_lookup(orphan))
    _loaded, _groups, tier = asyncio.run(_load_visible(session, _rest_user(admin=True), str(orphan.id)))
    assert tier == "config"

    # Fail-closed belt: a row the predicate admitted but the permission model
    # does not recognise (cannot happen in SQL; the fake ignores WHERE) is 403.
    session = _FakeSession(results=_deck_lookup(deck))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_load_visible(session, _rest_user(), str(deck.id)))
    assert exc.value.status_code == 403


def test_manage_endpoints_refuse_read_only_grantees_with_403():
    from fastapi import HTTPException

    from app.api.v1.presentations import _load_manageable

    owner, reader = uuid.uuid4(), uuid.uuid4()
    deck = _existing_row(
        owner, shared_user_ids=[str(reader)], shared_user_permissions={str(reader): "read_only"}
    )
    session = _FakeSession(results=_deck_lookup(deck))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_load_manageable(session, _rest_user(reader), str(deck.id), action="edit"))
    assert exc.value.status_code == 403
    assert "not edit" in exc.value.detail

    published = _existing_row(owner, is_company_managed=True)
    session = _FakeSession(results=_deck_lookup(published))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_load_manageable(session, _rest_user(reader), str(published.id), action="delete"))
    assert exc.value.status_code == 403
    assert "Only admins" in exc.value.detail


# ── Sharing through the agent ────────────────────────────────────────
#
# ``share_presentation`` runs as the calling user through the Share dialog's
# own write (``presentation_sharing.apply_sharing``). The fake session answers
# the tool's statements in order: the caller's groups and the deck, the accounts
# / groups the call names, the account locks taken before the deck is re-read
# FOR UPDATE (``_LOCKS``), the accounts / groups the deck's grants already name,
# the caller's row, the group check ``normalize_group_grants`` makes when the
# call changes the groups, then the account check ``normalize_user_grants``
# makes before storing. ``tests/test_presentation_sharing_postgres.py`` runs the same
# calls against PostgreSQL and the real REST routes.

#: The ``FOR KEY SHARE`` read ``lock_for_sharing`` makes: it returns nothing used.
_LOCKS = _Result()


def _account(email: str, *, name: str | None = None, active: bool = True):
    return SimpleNamespace(
        id=uuid.uuid4(),
        email=email,
        full_name=name or email.split("@")[0].title(),
        is_active=active,
        role="user",
        is_admin=False,
    )


def _account_rows(accounts) -> _Result:
    """The accounts ``resolve_principals`` / ``load_principals`` read: id, email, name, active."""
    return _Result(rows=[(a.id, a.email, a.full_name, a.is_active) for a in accounts])


def _grant_check_rows(accounts) -> _Result:
    """The accounts ``normalize_user_grants`` checks before storing: id as text, active."""
    return _Result(rows=[(str(a.id), a.is_active) for a in accounts])


def _q3_deck(owner, **columns):
    deck = _existing_row(owner.id, slides=3, **columns)
    deck.title = "Quarterly Review, Q3 2026"
    deck.spec = {**deck.spec, "title": deck.title}
    return deck


def _share(executor, deck, **arguments):
    return _run_tool(executor, "share_presentation", {"presentation_id": str(deck.id), **arguments})


def _audit_rows(session) -> list:
    from app.models.audit_log import AuditLog

    return [row for row in session.added if isinstance(row, AuditLog)]


def test_share_presentation_gives_ten_people_read_only_access_by_email_in_one_call(monkeypatch):
    """The EMT case: the owner's assistant shares the Q3 deck read-only with
    ten people by email, in one call. Each of them can then open, present and
    export it (``read_only``) and none can edit it."""
    from fastapi import HTTPException

    import app.core.database as database
    from app.api.v1.presentations import _load_manageable
    from app.services.presentations import deck_permission

    owner = _account("samuel@filigran.io", name="Samuel Hassine")
    emt = [_account(f"emt{i}@filigran.io", name=f"EMT Member {i}") for i in range(10)]
    deck = _q3_deck(owner)
    session = _FakeSession(
        results=[*_deck_lookup(deck), _account_rows(emt), _LOCKS, _Result(one=owner), _grant_check_rows(emt)]
    )
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools(str(owner.id))
    # Written as a person pastes them: any case, one with its display name.
    addresses = [a.email.upper() for a in emt[:-1]] + [f"EMT Member 9 <{emt[-1].email}>"]
    result = _share(executor, deck, users=addresses, permission="read_only")

    assert not result.is_error, result.content
    assert session.committed
    assert deck.shared_user_ids == [str(a.id) for a in emt]
    assert deck.shared_user_permissions == {str(a.id): "read_only" for a in emt}
    assert deck.user_id == owner.id
    assert "Given access (can view) to 10 people" in result.content
    assert "Shared with 10 people: EMT Member 0 <emt0@filigran.io> (can view)" in result.content
    assert f"[[PRESENTATION:{deck.id}]]" in result.content
    # All ten addresses were matched in ONE case-insensitive lookup.
    lookup = _compiled(session.statements[2])
    assert "lower(users.email) IN" in lookup
    assert lookup.count("@filigran.io") == 10
    # The accounts are locked BEFORE the deck is re-read FOR UPDATE: the order
    # a user deletion takes them in, so the two never deadlock, and two shares
    # of one deck serialize.
    locks = _compiled(session.statements[3])
    assert "FOR KEY SHARE" in locks
    assert all(str(member.id) in locks for member in emt)
    assert session.refreshed == [(deck, {"with_for_update": True})]

    for member in emt:
        assert deck_permission(deck, user_id=member.id) == "read_only"
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_load_manageable(_FakeSession(results=_deck_lookup(deck)), emt[0], str(deck.id), action="edit"))
    assert exc.value.status_code == 403


def test_share_presentation_remove_revokes_the_grant_at_once(monkeypatch):
    import app.core.database as database
    from app.services.presentations import deck_permission

    owner = _account("samuel@filigran.io")
    leaving, staying = _account("leaving@filigran.io"), _account("staying@filigran.io")
    deck = _q3_deck(
        owner,
        shared_user_ids=[str(leaving.id), str(staying.id)],
        shared_user_permissions={str(leaving.id): "read_only", str(staying.id): "config"},
    )
    session = _FakeSession(
        results=[
            *_deck_lookup(deck),
            _account_rows([leaving]),  # the account named
            _LOCKS,
            _account_rows([leaving, staying]),  # the accounts the grants name
            _Result(one=owner),
            _grant_check_rows([staying]),
        ]
    )
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools(str(owner.id))
    result = _share(executor, deck, users=[leaving.email], action="remove")

    assert not result.is_error, result.content
    assert session.committed
    assert deck.shared_user_ids == [str(staying.id)]
    assert deck.shared_user_permissions == {str(staying.id): "config"}
    assert deck_permission(deck, user_id=leaving.id) is None
    assert deck_permission(deck, user_id=staying.id) == "config"
    assert "Access removed, they can no longer open it: 1 person: Leaving <leaving@filigran.io>" in result.content
    assert "Shared with 1 person: Staying <staying@filigran.io> (can edit)." in result.content


def test_share_presentation_replace_makes_the_audience_exactly_the_list(monkeypatch):
    """``replace`` keeps only the people and groups named, at the tier asked
    for: a grantee left out loses access, a group too, one named keeps it at
    the new tier."""
    import app.core.database as database

    owner = _account("samuel@filigran.io")
    dropped, kept, newcomer = (
        _account("dropped@filigran.io"),
        _account("kept@filigran.io"),
        _account("newcomer@filigran.io"),
    )
    board = uuid.uuid4()
    deck = _q3_deck(
        owner,
        shared_user_ids=[str(dropped.id), str(kept.id)],
        shared_user_permissions={str(dropped.id): "read_only", str(kept.id): "config"},
        shared_group_ids=[str(board)],
        shared_group_permissions={str(board): "read_only"},
    )
    session = _FakeSession(
        results=[
            *_deck_lookup(deck),
            _account_rows([kept, newcomer]),
            _LOCKS,
            _account_rows([dropped, kept]),
            _Result(rows=[(board, "Board")]),
            _Result(one=owner),
            _grant_check_rows([kept, newcomer]),
        ]
    )
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools(str(owner.id))
    result = _share(executor, deck, users=[kept.email, str(newcomer.id)], action="replace", permission="read_only")

    assert not result.is_error, result.content
    assert deck.shared_user_ids == [str(kept.id), str(newcomer.id)]
    assert deck.shared_user_permissions == {str(kept.id): "read_only", str(newcomer.id): "read_only"}
    assert deck.shared_group_ids is None
    assert deck.shared_group_permissions is None
    assert "Given access (can view) to 1 person: Newcomer <newcomer@filigran.io>" in result.content
    assert "Now can view: 1 person: Kept <kept@filigran.io>" in result.content
    assert (
        "Access removed, they can no longer open it: 1 person and 1 group: "
        "Dropped <dropped@filigran.io>, group Board"
    ) in result.content
    (audit,) = _audit_rows(session)
    assert audit.details["removed_users"] == [str(dropped.id)]
    assert audit.details["removed_groups"] == [str(board)]
    assert audit.details["retiered_users"] == [str(kept.id)]
    assert audit.details["added_users"] == [str(newcomer.id)]


def test_share_presentation_refuses_an_unknown_email_and_names_it(monkeypatch):
    """An address no account answers to refuses the WHOLE call, by name: the
    tool never shares with nine of the ten people asked for, and never
    invites anyone from outside."""
    import app.core.database as database

    owner = _account("samuel@filigran.io")
    known = _account("known@filigran.io")
    deck = _q3_deck(owner)
    session = _FakeSession(results=[*_deck_lookup(deck), _account_rows([known])])
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools(str(owner.id))
    result = _share(executor, deck, users=[known.email, "partner@outside.example", "not an address"])

    assert result.is_error
    assert 'no account on this platform matches "partner@outside.example", "not an address"' in result.content
    assert "Nothing was changed" in result.content
    assert "nobody is invited" in result.content
    assert deck.shared_user_ids is None
    assert not session.committed
    assert _audit_rows(session) == []
    assert not any(type(row).__name__ == "User" for row in session.added)


def test_share_presentation_refuses_a_read_only_grantee_and_a_stranger(monkeypatch):
    import app.core.database as database

    owner, reader, other = _account("samuel@filigran.io"), _account("reader@filigran.io"), _account("other@x.io")
    deck = _q3_deck(
        owner, shared_user_ids=[str(reader.id)], shared_user_permissions={str(reader.id): "read_only"}
    )
    session = _FakeSession(results=_deck_lookup(deck))
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools(str(reader.id))
    result = _share(executor, deck, users=[other.email])

    assert result.is_error
    assert 'you can view "Quarterly Review, Q3 2026" but not share it' in result.content
    assert "only its owner or someone it is shared with as an editor" in result.content
    assert deck.shared_user_ids == [str(reader.id)]
    assert len(session.statements) == 2  # refused before any account is looked up
    assert not session.committed

    # Someone the deck is not shared with is told it does not exist.
    session = _FakeSession(results=_deck_lookup(None))
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools(str(other.id))
    result = _share(executor, deck, users=[owner.email])
    assert result.is_error
    assert "you can access was found" in result.content


def test_a_config_grantee_shares_the_deck_like_its_owner(monkeypatch):
    import app.core.database as database

    owner, editor, colleague = _account("o@filigran.io"), _account("e@filigran.io"), _account("c@filigran.io")
    deck = _q3_deck(owner, shared_user_ids=[str(editor.id)], shared_user_permissions={str(editor.id): "config"})
    session = _FakeSession(
        results=[
            *_deck_lookup(deck),
            _account_rows([colleague]),
            _LOCKS,
            _account_rows([editor]),
            _Result(one=editor),
            _grant_check_rows([editor, colleague]),
        ]
    )
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools(str(editor.id))
    result = _share(executor, deck, users=[colleague.email], permission="config")

    assert not result.is_error, result.content
    assert deck.shared_user_ids == [str(editor.id), str(colleague.id)]
    assert deck.shared_user_permissions == {str(editor.id): "config", str(colleague.id): "config"}
    assert deck.user_id == owner.id
    (audit,) = _audit_rows(session)
    assert audit.user_id == editor.id


def test_share_presentation_plans_from_the_deck_as_re_read_under_its_lock(monkeypatch):
    """The editor's grant is cut to read-only by the owner between the first
    read and the locked re-read: the call is refused on the row as committed,
    never on the stale one."""
    import app.core.database as database

    owner, editor, colleague = _account("o@filigran.io"), _account("e@filigran.io"), _account("c@filigran.io")
    deck = _q3_deck(owner, shared_user_ids=[str(editor.id)], shared_user_permissions={str(editor.id): "config"})
    session = _FakeSession(results=[*_deck_lookup(deck), _account_rows([colleague]), _LOCKS])

    async def _owner_downgraded_the_editor(obj, *a, **k):
        session.refreshed.append((obj, k))
        obj.shared_user_permissions = {str(editor.id): "read_only"}

    session.refresh = _owner_downgraded_the_editor
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools(str(editor.id))
    result = _share(executor, deck, users=[colleague.email])

    assert result.is_error
    assert "but not share it" in result.content
    assert deck.shared_user_ids == [str(editor.id)]
    assert not session.committed
    assert _audit_rows(session) == []


def test_share_presentation_writes_one_audit_row_naming_the_principals(monkeypatch):
    """One ``log_audit`` row per call that changes something, ``via: agent``,
    with who was added or removed and at which tier; a call that changes
    nothing writes nothing."""
    import app.core.database as database

    owner, analyst = _account("samuel@filigran.io"), _account("analyst@filigran.io")
    emt_group = uuid.uuid4()
    deck = _q3_deck(owner)
    session = _FakeSession(
        results=[
            *_deck_lookup(deck),
            _account_rows([analyst]),
            _Result(rows=[(emt_group, "EMT")]),
            _LOCKS,
            _Result(one=owner),
            # The groups ``normalize_group_grants`` checks before storing.
            _Result(rows=[(str(emt_group),)]),
            _grant_check_rows([analyst]),
        ]
    )
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools(str(owner.id))
    result = _share(executor, deck, users=[analyst.email], groups=["emt"], permission="config")

    assert not result.is_error, result.content
    assert deck.shared_group_ids == [str(emt_group)]
    assert deck.shared_group_permissions == {str(emt_group): "config"}
    (audit,) = _audit_rows(session)
    assert audit.action == "update"
    assert audit.entity_type == "presentation"
    assert audit.entity_id == deck.id
    assert audit.user_id == owner.id
    assert audit.details == {
        "via": "agent",
        "action": "add",
        "permission": "config",
        "added_users": [str(analyst.id)],
        "removed_users": [],
        "retiered_users": [],
        "added_groups": [str(emt_group)],
        "removed_groups": [],
        "retiered_groups": [],
        "sharing_changed": True,
    }

    # The same call again changes nothing: no write, no second audit row.
    session = _FakeSession(
        results=[
            *_deck_lookup(deck),
            _account_rows([analyst]),
            _Result(rows=[(emt_group, "EMT")]),
            _LOCKS,
            _account_rows([analyst]),
            _Result(rows=[(emt_group, "EMT")]),
        ]
    )
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    result = _share(executor, deck, users=[analyst.email], groups=[str(emt_group)], permission="config")
    assert not result.is_error, result.content
    assert "Nothing changed on **Quarterly Review, Q3 2026**" in result.content
    assert "Already can edit, unchanged: 1 person and 1 group" in result.content
    assert _audit_rows(session) == []
    assert not session.committed


def test_share_presentation_leaves_a_company_managed_deck_to_admins(monkeypatch):
    import app.core.database as database

    owner = _account("samuel@filigran.io")
    deck = _q3_deck(owner, is_company_managed=True)
    session = _FakeSession(results=_deck_lookup(deck))
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools(str(owner.id))
    result = _share(executor, deck, users=["someone@filigran.io"])
    assert result.is_error
    assert "published to the whole platform" in result.content
    assert not session.committed


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"users": ["a@filigran.io"], "permission": "owner"}, "permission must be one of read_only, config"),
        ({"users": ["a@filigran.io"], "action": "publish"}, "action must be one of add, remove, replace"),
        ({}, "name at least one user (email or user id) or group to add"),
        ({"users": [f"u{i}@filigran.io" for i in range(101)]}, "at most 100 people and groups"),
    ],
    ids=["unknown-permission", "unknown-action", "nobody-named", "too-many"],
)
def test_share_presentation_checks_its_arguments_before_any_query(monkeypatch, arguments, expected):
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools()
    result = _run_tool(executor, "share_presentation", {"presentation_id": str(uuid.uuid4()), **arguments})
    assert result.is_error
    assert expected in result.content
    assert session.statements == []


def test_get_presentation_says_who_has_access(monkeypatch):
    """The read-back names every grant with its tier, so the agent
    reports who can open the deck from the grants, not from memory."""
    import app.core.database as database

    owner, reader = _account("samuel@filigran.io"), _account("ada@filigran.io", name="Ada Lovelace")
    gone = _account("gone@filigran.io", active=False)
    board = uuid.uuid4()
    deck = _q3_deck(
        owner,
        shared_user_ids=[str(reader.id), str(gone.id)],
        shared_user_permissions={str(reader.id): "read_only", str(gone.id): "config"},
        shared_group_ids=[str(board)],
        shared_group_permissions={str(board): "config"},
    )
    session = _FakeSession(
        results=[
            *_deck_lookup(deck),
            _Result(rows=[]),  # open comments
            _account_rows([reader, gone]),
            _Result(rows=[(board, "Board")]),
        ]
    )
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools(str(reader.id))
    content = _run_tool(executor, "get_presentation", {"presentation_id": str(deck.id)}).content
    assert (
        "Shared with 2 people and 1 group: Ada Lovelace <ada@filigran.io> (can view), "
        "Gone <gone@filigran.io> (can edit, deactivated), group Board (can edit)."
    ) in content


def test_get_presentation_of_a_private_deck_says_so_without_a_query(monkeypatch):
    import app.core.database as database

    owner = _account("samuel@filigran.io")
    deck = _q3_deck(owner)
    session = _FakeSession(results=[*_deck_lookup(deck), _Result(rows=[])])
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools(str(owner.id))
    content = _run_tool(executor, "get_presentation", {"presentation_id": str(deck.id)}).content
    assert "Shared with: nobody - only its owner can open it." in content
    assert "Public link:" not in content
    # groups, deck, comments, its public link: no account lookup
    assert len(session.statements) == 4
    assert "presentation_public_links" in _compiled(session.statements[3])


def test_share_presentation_description_asks_for_the_user_and_names_the_audience_first():
    defs, _executor = _build_tools()
    share = next(d for d in defs if d.name == "share_presentation")
    assert share.source == "builtin:generate_presentation"
    assert "ONLY when the user asks" in share.description
    assert "name the people and groups who will get or lose access in your reply before you call it" in (
        share.description
    )
    assert "nobody is invited" in share.description
    properties = share.input_schema["properties"]
    assert properties["permission"]["enum"] == ["read_only", "config"]
    assert properties["action"]["enum"] == ["add", "remove", "replace"]
    assert properties["users"]["type"] == "array"
    assert share.input_schema["required"] == ["presentation_id"]


def test_the_share_dialog_and_the_tool_share_one_write():
    """``PATCH /presentations/{id}`` and ``share_presentation`` change the
    audience through the same function, so the two cannot drift."""
    import inspect

    from app.api.v1 import presentations as api
    from app.services.builtin_tools import presentation as tools

    route = inspect.getsource(api.update_presentation)
    assert "await apply_sharing(db, user, presentation, body)" in route
    assert "apply_user_share_fields" not in route
    tool = inspect.getsource(tools.build_presentation_tools)
    assert "await apply_sharing(own_db, actor, presentation, plan.changes)" in tool
    assert "shared_user_ids =" not in tool


def test_frontend_share_toggle_reopens_on_for_a_deck_shared_with_people_only():
    """The share toggle reflects a stored grant of EITHER kind.

    The sharing write keeps no empty group list (``normalize_group_grants``
    stores none as ``None``), so a deck shared with people only comes back with
    ``shared_group_ids = null``. The picker's on/off state used to read that list alone: such a deck
    reopened as "private" with the very people it lists hidden, and the
    dialog's audience line said "only you" about a deck a colleague could open.
    The write is the update route's and the share tool's, and a video's
    too.
    """
    sharing = (BACKEND_ROOT / "app" / "services" / "presentation_sharing.py").read_text(encoding="utf-8")
    assert "await apply_group_share_fields(db, entity, changes, partial=True)" in sharing

    card = (FRONTEND_ROOT / "src" / "components" / "ui" / "principal-sharing-card.tsx").read_text(
        encoding="utf-8"
    )
    enabled = card[card.index("const enabled =") :]
    enabled = enabled[: enabled.index(";")]
    assert "sharedGroupIds !== null" in enabled
    assert "userSharing?.userIds?.length" in enabled

    # The audience line is worded by the share dialogs' shared draft,
    # whose "nobody listed" rule - people AND groups, never the group list
    # alone - is pinned by `frontend/src/lib/sharing-draft.test.ts`; here, only
    # that the dialog delegates to it.
    dialog = (
        FRONTEND_ROOT / "src" / "components" / "presentation" / "PresentationShareDialog.tsx"
    ).read_text(encoding="utf-8")
    assert "sharingAudience(draft, t_i18n)" in dialog


def test_presentation_api_never_accepts_a_raw_spec():
    """Decks are BORN through the validated tool path and edited here only as
    validated pieces: ``PATCH`` merges title / theme / slides into the stored
    spec and re-runs ``validate_spec``. No route takes a whole ``spec``
    and no ``POST ""`` creates a deck from a JSON body — either would bypass
    validation. The ONE route that does read a whole spec is the package import
    (``POST /import``, a multipart upload), and it is not a bypass: the spec it
    carries goes through ``save_presentation`` — ``validate_spec`` plus
    ``verify_file_references`` — like an agent-authored one, after every file id
    has been rewritten to a file that exists here (``test_presentation_package``)."""
    import inspect

    from app.api.v1 import presentations as api
    from app.api.v1.schemas.presentation import PresentationUpdate
    from app.services import presentation_package

    non_get = {
        (route.path, method)
        for route in api.router.routes
        for method in getattr(route, "methods", set())
        if method != "GET"
    }
    assert non_get == {
        ("/{presentation_id}", "PATCH"),
        ("/{presentation_id}", "DELETE"),
        ("/{presentation_id}/duplicate", "POST"),
        ("/{presentation_id}/export", "POST"),
        ("/import", "POST"),
        # Collaboration surface: none of these takes deck content. Restore
        # re-applies a snapshot that was itself validated when it was saved.
        ("/{presentation_id}/versions/{revision}/restore", "POST"),
        ("/{presentation_id}/comments", "POST"),
        # The AI draft reads the deck to write a NOTE; it stores nothing.
        ("/{presentation_id}/comments/draft", "POST"),
        ("/{presentation_id}/comments/{comment_id}", "PATCH"),
        ("/{presentation_id}/comments/{comment_id}", "DELETE"),
        ("/{presentation_id}/presence", "POST"),
        ("/{presentation_id}/presence", "DELETE"),
        # The layout check takes no body at all and writes nothing: it
        # renders the stored deck and reports what the audience would see.
        ("/{presentation_id}/layout-check", "POST"),
        # The layout switch takes ONE slide, not a spec, and writes
        # nothing: the rebuilt slide goes back into the editor's draft and is
        # saved through the PATCH above, where ``validate_spec`` runs again.
        ("/{presentation_id}/slide-layout", "POST"),
        # The public link takes a ``regenerate`` flag and no content,
        # and its options a boolean.
        ("/{presentation_id}/public-link", "POST"),
        ("/{presentation_id}/public-link", "PATCH"),
        ("/{presentation_id}/public-link", "DELETE"),
        # The live poll controls take a verb and a question id, never deck
        # content: they write the poll's state row, not the spec (#10157).
        ("/{presentation_id}/polls/{poll_id}/control", "POST"),
        # Hand the clicker (#10552 slice B) takes a user id, a move verb and a
        # slide index, never deck content: it writes a Redis entry, not the spec.
        ("/{presentation_id}/clicker", "PUT"),
        ("/{presentation_id}/clicker", "DELETE"),
        ("/{presentation_id}/clicker/command", "POST"),
    }
    assert ("", "POST") not in non_get
    assert "spec" not in PresentationUpdate.model_fields
    # The import creates the deck through the validated save, never by
    # constructing a ``Presentation`` row itself.
    importer = inspect.getsource(presentation_package.import_presentation_package)
    assert "await save_presentation(" in importer
    assert "verify_files_for=user" in importer
    assert "Presentation(" not in importer
    from app.api.v1.schemas.presentation import (
        PresentationCommentCreate,
        PresentationCommentDraftRequest,
        PresentationCommentUpdate,
        PresentationPresenceHeartbeat,
    )

    for schema in (
        PresentationCommentCreate,
        PresentationCommentDraftRequest,
        PresentationCommentUpdate,
        PresentationPresenceHeartbeat,
    ):
        assert not ({"spec", "slides", "title", "theme"} & set(schema.model_fields)), schema
    assert set(api.PresentationExportRequest.model_fields) == {"format"}
    # Every content field PATCH accepts is validated by the spec model.
    assert set(api._CONTENT_FIELDS) <= {"title", "theme", "transition", "aspect_ratio", "chrome", "slides"}


def test_patch_merges_validated_pieces_and_honours_expected_revision():
    """A stale ``expected_revision`` is a 409 that writes nothing; a matching
    one applies the change through ``save_presentation`` and bumps the row."""
    from fastapi import HTTPException

    from app.api.v1 import presentations as api
    from app.api.v1.schemas.presentation import PresentationUpdate

    owner = uuid.uuid4()
    deck = _existing_row(owner, slides=3)
    user = _rest_user(owner)

    async def _patch(body, session):
        return await api.update_presentation(str(deck.id), body, request=None, user=user, db=session)

    # Stale revision -> 409 with the current revision in the detail.
    session = _FakeSession(results=_deck_lookup(deck))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_patch(PresentationUpdate(title="Renamed", expected_revision=7), session))
    assert exc.value.status_code == 409
    assert exc.value.detail["current_revision"] == 1
    assert deck.title == "Deck"

    # A slide list that does not validate -> 422, nothing written.
    session = _FakeSession(results=_deck_lookup(deck))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_patch(PresentationUpdate(slides=[{"layout": "carousel"}]), session))
    assert exc.value.status_code == 422
    assert deck.revision == 1

    # Matching revision: reorder to two slides + rename, revision bumps once.
    session = _FakeSession(results=_deck_lookup(deck))
    session.refresh = _noop_refresh
    body = PresentationUpdate(title="Renamed", slides=_slides(2), expected_revision=1)
    asyncio.run(_patch(body, session))
    assert deck.title == "Renamed"
    assert deck.slide_count == 2
    assert deck.revision == 2
    assert session.committed


async def _noop_refresh(_obj):
    return None


def test_patch_rethemes_a_stored_v1_deck_with_blank_optionals_and_speaks_to_a_person():
    """The report: switching an OLD deck to another theme failed on
    ``slides.3.subtitle: String should have at least 1 character`` for every
    title slide the v1 tool had stored with ``subtitle: ""`` — the PATCH
    merges the theme into the stored spec and re-validates the WHOLE deck, so
    a field the user never touched blocked a change they did make. Those
    blanks now validate as absent and are dropped on the way through. When a
    PATCH does fail validation, the 422 is written for the person behind the
    editor, not for the model behind the tool."""
    import inspect

    from fastapi import HTTPException

    from app.api.v1 import presentations as api
    from app.api.v1.schemas.presentation import PresentationUpdate

    owner = uuid.uuid4()
    deck = _existing_row(owner, slides=4)
    deck.spec = {
        "title": "OpenCTI Insight Meeting",
        "theme": "dark",
        "slides": [
            {"layout": "title", "title": "OpenCTI Insight Meeting", "subtitle": "Origo"},
            {"layout": "generic", "title": "Team", "bullets": ["a"], "rich_text": "", "image_file_id": ""},
            {"layout": "title", "title": "1. CONTEXT", "subtitle": "", "notes": ""},
            {"layout": "two_columns", "title": "Goals", "left": {"bullets": ["x"], "rich_text": ""}, "right": {}},
        ],
    }
    user = _rest_user(owner)

    async def _patch(body, session):
        return await api.update_presentation(str(deck.id), body, request=None, user=user, db=session)

    session = _FakeSession(results=_deck_lookup(deck))
    session.refresh = _noop_refresh
    asyncio.run(_patch(PresentationUpdate(theme="light", expected_revision=1), session))
    assert deck.theme == "light"
    assert deck.revision == 2
    assert session.committed
    # The blanks are gone from the stored spec; what the user wrote is intact.
    assert "subtitle" not in deck.spec["slides"][2]
    assert "notes" not in deck.spec["slides"][2]
    assert "rich_text" not in deck.spec["slides"][1]
    assert deck.spec["slides"][0]["subtitle"] == "Origo"
    # And the v1 slides left with the same save: the re-validation
    # upgraded them, so the row is now a v2 deck through and through.
    assert [s["layout"] for s in deck.spec["slides"]] == ["title", "content", "title", "content"]
    assert [b["type"] for b in deck.spec["slides"][3]["blocks"]] == ["list"]

    # The retired ``filigran`` theme name is still accepted from an old client
    # and stored as the scheme it always was: a free deck asked for
    # "filigran" is a dark deck, revision bumped once more.
    session = _FakeSession(results=_deck_lookup(deck))
    session.refresh = _noop_refresh
    asyncio.run(_patch(PresentationUpdate(theme="filigran", expected_revision=2), session))
    assert deck.theme == "dark"
    assert deck.spec["theme"] == "dark"
    assert deck.revision == 3

    # A slide list that does not validate -> 422 for a person: 1-based slide,
    # no tool instructions, one problem per line.
    session = _FakeSession(results=_deck_lookup(deck))
    bad = [
        {"layout": "title", "title": "ok"},
        {"layout": "title", "title": ""},
        {"layout": "content", "blocks": [{"type": "text", "content": "a", "bogus": 1}]},
    ]
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_patch(PresentationUpdate(slides=bad, expected_revision=3), session))
    assert exc.value.status_code == 422
    detail = exc.value.detail
    assert isinstance(detail, str)
    assert detail.startswith("The presentation cannot be saved as it is.")
    assert "\n- Slide 2 (title): title - String should have at least 1 character" in detail
    assert "\n- Slide 3 (content): blocks.0.bogus in text - Extra inputs are not permitted" in detail
    assert "call the tool again" not in detail
    assert "slides.1" not in detail
    assert deck.revision == 3

    # Every other route a person reaches with a whole spec renders the same
    # human problems: duplicate, restore and the package import re-validate a
    # stored spec and used to leak the agent wording.
    for route in (
        api.duplicate_presentation_endpoint,
        api.restore_presentation_version_endpoint,
        api.import_presentation_endpoint,
    ):
        source = inspect.getsource(route)
        handlers = source.split("except PresentationSpecError as exc:")[1:]
        assert handlers, route.__name__
        for handler in handlers:
            handler = handler.split("\n    except ", 1)[0].split("\n    await ", 1)[0]
            assert "exc.human_details()" in handler, route.__name__
            assert "str(exc)" not in handler, route.__name__
    assert inspect.getsource(api.update_presentation).count("exc.human_message()") == 2


def _facts(
    template_id: uuid.UUID,
    *,
    name: str = "Brand Deck",
    kind: str = "presentation",
    schemes=("dark",),
    extra_metadata: dict | None = None,
):
    from app.services.presentations import TemplateFacts, template_deck_defaults, template_heroes

    return TemplateFacts(
        id=template_id,
        name=name,
        kind=kind,
        supported_schemes=tuple(schemes),
        deck_defaults=template_deck_defaults(extra_metadata),
        heroes=template_heroes(extra_metadata),
    )


def test_template_facts_read_the_kind_like_every_other_surface():
    """``is_presentation_template`` goes through ``is_presentation_kind`` (trimmed,
    case-insensitive) rather than a raw string compare, so a legacy row spelt
    ``Presentation`` links and a retired kind does not."""
    template_id = uuid.uuid4()
    assert _facts(template_id, kind="presentation").is_presentation_template is True
    assert _facts(template_id, kind=" Presentation ").is_presentation_template is True
    assert _facts(template_id, kind="report").is_presentation_template is False
    assert _facts(template_id, kind="asset-library").is_presentation_template is False
    assert _facts(template_id, kind="").is_presentation_template is False


def test_patch_relinks_the_template_and_snaps_the_theme_it_cannot_dress(monkeypatch):
    """The deck's Design panel: ``template_container_id`` links, re-links
    or unlinks the template of an existing deck. A link is the intent, so the
    template wins over a theme it cannot dress (the deck is snapped, and the
    revision bumps because the deck LOOKS different); a theme CHANGE the linked
    template cannot dress is refused with the shared sentence, because the
    picker never offers it - such a request is a stale client, not a choice."""
    from fastapi import HTTPException

    from app.api.v1 import presentations as api
    from app.api.v1.schemas.presentation import PresentationUpdate

    owner = uuid.uuid4()
    deck = _existing_row(owner, slides=2)
    deck.spec = {**deck.spec, "theme": "light"}
    deck.theme = "light"
    user = _rest_user(owner)
    dark_only = uuid.uuid4()
    both = uuid.uuid4()
    facts_by_id = {
        dark_only: _facts(dark_only, name="XTM One Brand Deck", schemes=("dark",)),
        both: _facts(both, name="Filigran Brand Deck", schemes=("dark", "light")),
    }
    seen: list[tuple[uuid.UUID, uuid.UUID | None]] = []

    async def _fake_facts(_db, template_id, *, user_id, is_admin=False):
        seen.append((template_id, user_id))
        return facts_by_id.get(template_id)

    monkeypatch.setattr(api, "load_visible_template_facts", _fake_facts)

    async def _patch(body, session):
        return await api.update_presentation(str(deck.id), body, request=None, user=user, db=session)

    # Link a dark-only template to a light deck: linked, snapped to dark, one revision.
    session = _FakeSession(results=_deck_lookup(deck))
    session.refresh = _noop_refresh
    asyncio.run(_patch(PresentationUpdate(template_container_id=str(dark_only), expected_revision=1), session))
    assert deck.template_container_id == dark_only
    assert deck.theme == "dark"
    assert deck.spec["theme"] == "dark"
    assert deck.revision == 2
    assert session.committed
    # The template is loaded as the EDITOR sees it - a typed id, not one from search.
    assert seen == [(dark_only, owner)]
    # The history line and the audit entry both record a design change.
    assert [type(r).__name__ for r in session.added] == ["PresentationVersion", "AuditLog"]
    assert session.added[1].details["template_changed"] is True

    # Re-sending the template the deck already wears is not a change: no
    # revision bump, no history line, the link is not re-validated, and -
    # nothing written - no audit entry either (the editor autosaves).
    seen.clear()
    session = _FakeSession(results=_deck_lookup(deck))
    session.refresh = _noop_refresh
    asyncio.run(_patch(PresentationUpdate(template_container_id=str(dark_only), expected_revision=2), session))
    assert deck.template_container_id == dark_only
    assert deck.revision == 2
    assert session.added == []
    assert seen == []

    # Switching the theme to one the linked template cannot dress: 422, the
    # shared sentence, nothing written.
    session = _FakeSession(results=_deck_lookup(deck))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_patch(PresentationUpdate(theme="light", expected_revision=2), session))
    assert exc.value.status_code == 422
    assert exc.value.detail == (
        "Theme 'light' is not supported by the template 'XTM One Brand Deck' (dark only). "
        "Pick a supported theme, link a template that supports 'light', or unlink the template."
    )
    assert deck.theme == "dark"
    assert deck.revision == 2
    assert not session.committed

    # Re-link to a template that supports both AND ask for light in the same
    # save: the link carries the intent, the theme is kept because it fits.
    session = _FakeSession(results=_deck_lookup(deck))
    session.refresh = _noop_refresh
    asyncio.run(
        _patch(PresentationUpdate(template_container_id=str(both), theme="light", expected_revision=2), session)
    )
    assert deck.template_container_id == both
    assert deck.theme == "light"
    assert deck.revision == 3

    # Unlink: the deck becomes a free deck, keeps its theme, and the revision
    # bumps once more (it looks different).
    session = _FakeSession(results=_deck_lookup(deck))
    session.refresh = _noop_refresh
    asyncio.run(_patch(PresentationUpdate(template_container_id=None, expected_revision=3), session))
    assert deck.template_container_id is None
    assert deck.theme == "light"
    assert deck.revision == 4

    # ``null`` on a deck that is already free is not an unlink: nothing bumps,
    # nothing is written.
    session = _FakeSession(results=_deck_lookup(deck))
    session.refresh = _noop_refresh
    asyncio.run(_patch(PresentationUpdate(template_container_id=None, expected_revision=4), session))
    assert deck.revision == 4
    assert session.added == []

    # A free deck may pick either theme without consulting any template.
    seen.clear()
    session = _FakeSession(results=_deck_lookup(deck))
    session.refresh = _noop_refresh
    asyncio.run(_patch(PresentationUpdate(theme="dark", expected_revision=4), session))
    assert deck.theme == "dark"
    assert deck.revision == 5
    assert seen == []


def test_patch_holds_a_slides_own_theme_to_the_template_like_the_deck_theme(monkeypatch):
    """The slide Theme picker follows the deck picker's rule on the
    REST path too: a save that SETS a slide theme the linked template cannot
    dress is a 422 naming the slide and the field, nothing written; a
    template change snaps every override it cannot dress the way it snaps the
    deck; and a deck theme change leaves an overridden slide byte-identical."""
    from fastapi import HTTPException

    from app.api.v1 import presentations as api
    from app.api.v1.schemas.presentation import PresentationUpdate

    owner = uuid.uuid4()
    deck = _existing_row(owner, slides=2)
    user = _rest_user(owner)
    dark_only = uuid.uuid4()
    both = uuid.uuid4()
    facts_by_id = {
        dark_only: _facts(dark_only, name="XTM One Brand Deck", schemes=("dark",)),
        both: _facts(both, name="Filigran Brand Deck", schemes=("dark", "light")),
    }

    async def _fake_facts(_db, template_id, *, user_id, is_admin=False):
        return facts_by_id.get(template_id)

    monkeypatch.setattr(api, "load_visible_template_facts", _fake_facts)

    async def _patch(body, session):
        return await api.update_presentation(str(deck.id), body, request=None, user=user, db=session)

    # A dark-only template: a light cover is refused with the slide and the field.
    deck.template_container_id = dark_only
    slides = copy.deepcopy(deck.spec["slides"])
    slides[0]["theme"] = "light"
    session = _FakeSession(results=_deck_lookup(deck))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_patch(PresentationUpdate(slides=slides, expected_revision=1), session))
    assert exc.value.status_code == 422
    assert exc.value.detail.startswith(
        "Slide 1 theme 'light': not supported by the template 'XTM One Brand Deck' (dark only)."
    )
    assert "theme" not in deck.spec["slides"][0]
    assert deck.revision == 1
    assert not session.committed

    # Under a template that supports both, the cover takes its own dark theme...
    deck.template_container_id = both
    slides = copy.deepcopy(deck.spec["slides"])
    slides[0]["theme"] = "dark"
    slides[0]["hero"] = "gradient"
    session = _FakeSession(results=_deck_lookup(deck))
    session.refresh = _noop_refresh
    asyncio.run(_patch(PresentationUpdate(slides=slides, expected_revision=1), session))
    assert deck.spec["slides"][0]["theme"] == "dark"
    assert deck.spec["slides"][0]["hero"] == "gradient"
    assert deck.revision == 2

    # ...and a deck theme change leaves it byte-identical: light deck, dark cover.
    cover_before = copy.deepcopy(deck.spec["slides"][0])
    session = _FakeSession(results=_deck_lookup(deck))
    session.refresh = _noop_refresh
    asyncio.run(_patch(PresentationUpdate(theme="light", expected_revision=2), session))
    assert deck.theme == "light"
    assert deck.spec["slides"][0] == cover_before
    assert deck.revision == 3
    # Back to dark, and the cover still reads its own theme.
    session = _FakeSession(results=_deck_lookup(deck))
    session.refresh = _noop_refresh
    asyncio.run(_patch(PresentationUpdate(theme="dark", expected_revision=3), session))
    assert deck.spec["slides"][0] == cover_before

    # A template change to a dark-only brand snaps a light override like the deck.
    slides = copy.deepcopy(deck.spec["slides"])
    slides[1]["theme"] = "light"
    session = _FakeSession(results=_deck_lookup(deck))
    session.refresh = _noop_refresh
    asyncio.run(_patch(PresentationUpdate(slides=slides, expected_revision=4), session))
    assert deck.spec["slides"][1]["theme"] == "light"
    session = _FakeSession(results=_deck_lookup(deck))
    session.refresh = _noop_refresh
    asyncio.run(_patch(PresentationUpdate(template_container_id=str(dark_only), expected_revision=5), session))
    assert deck.template_container_id == dark_only
    assert deck.spec["slides"][1]["theme"] == "dark"
    assert deck.spec["slides"][0]["theme"] == "dark"
    assert deck.revision == 6

    # An override the template never supported may already be STORED (the
    # template narrowed its schemes after the save). Only what a save
    # INTRODUCES is refused, compared by the slide's stable id: a reorder plus
    # a title edit of that slide is not a request for the scheme...
    deck.spec["slides"][1]["theme"] = "light"
    slides = copy.deepcopy(deck.spec["slides"])
    slides.reverse()
    slides[0]["title"] = "Moved first"
    session = _FakeSession(results=_deck_lookup(deck))
    session.refresh = _noop_refresh
    asyncio.run(_patch(PresentationUpdate(slides=slides, expected_revision=6), session))
    assert deck.revision == 7
    assert deck.spec["slides"][0]["theme"] == "light" and deck.spec["slides"][0]["title"] == "Moved first"
    assert deck.spec["slides"][1]["theme"] == "dark"
    # ...while a NEW slide (no id yet) naming that scheme at the very position
    # the stored offender held is refused: the position is not the identity.
    slides = copy.deepcopy(deck.spec["slides"])
    slides.insert(0, {"layout": "title", "title": "New cover", "theme": "light"})
    session = _FakeSession(results=_deck_lookup(deck))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_patch(PresentationUpdate(slides=slides, expected_revision=7), session))
    assert exc.value.status_code == 422
    assert exc.value.detail.startswith(
        "Slide 1 theme 'light': not supported by the template 'XTM One Brand Deck' (dark only)."
    )
    assert deck.revision == 7 and not session.committed


# ── The covers a template offers ─────────────────────────────────────

#: A template declaring the four tiled covers as its own, the THREAD way.
_TILED_TEMPLATE = {
    "heroes": ["mosaic", "cascade", "horizon", "pillar"],
    "deck_defaults": {"hero_defaults": {"title": "cascade", "closing": "cascade"}},
}
#: A brand template that declares none and opens on the gradient cover.
_BRAND_TEMPLATE = {"deck_defaults": {"hero_defaults": {"title": "gradient", "closing": "gradient"}}}


@pytest.mark.parametrize(
    ("extra_metadata", "expected"),
    [
        (None, ("plain", "gradient", "split", "spotlight")),
        ({}, ("plain", "gradient", "split", "spotlight")),
        ({"heroes": "mosaic"}, ("plain", "gradient", "split", "spotlight")),
        ({"heroes": ["Pillar", "mosaic", "neon", "gradient"]}, ("plain", "gradient", "split", "spotlight", "mosaic", "pillar")),
        (_TILED_TEMPLATE, ("plain", "gradient", "split", "spotlight", "mosaic", "cascade", "horizon", "pillar")),
    ],
)
def test_a_template_offers_the_shared_covers_plus_the_ones_it_declares(extra_metadata, expected):
    from app.services.presentations import template_heroes

    assert template_heroes(extra_metadata) == expected


def test_a_cover_default_the_template_does_not_offer_is_dropped():
    """`hero_defaults` are read against the template's own covers: a tiled
    default on a template that declares none would dress a new deck with a
    cover the template does not offer."""
    from app.services.presentations import template_deck_defaults

    undeclared = {"deck_defaults": {"hero_defaults": {"title": "cascade", "closing": "gradient"}}}
    assert template_deck_defaults(undeclared)["hero_defaults"] == {"closing": "gradient"}
    assert template_deck_defaults(_TILED_TEMPLATE)["hero_defaults"] == {"title": "cascade", "closing": "cascade"}


def test_a_reader_is_served_every_cover_on_a_hero_its_template_offers():
    """An existing deck that already wears a tiled cover under a template that
    does not offer it is drawn on that template's default for the cover
    (else `gradient`), without touching the stored row; a deck with nothing
    to fit is served as stored, and a body slide is never read as a cover."""
    from app.services.presentation_spec import SHARED_HEROES
    from app.services.presentations import served_hero_spec, template_heroes

    stored = {
        "title": "Deck",
        "slides": [
            {"id": "a", "layout": "title", "hero": "cascade"},
            {"id": "b", "layout": "content", "hero": "mosaic"},
            {"id": "c", "layout": "closing", "hero": "horizon"},
            {"id": "d", "layout": "closing", "hero": "spotlight"},
        ],
    }
    before = copy.deepcopy(stored)
    served = served_hero_spec(stored, SHARED_HEROES, {"title": "split"})
    assert [slide.get("hero") for slide in served["slides"]] == ["split", "mosaic", "gradient", "spotlight"]
    assert stored == before
    assert served_hero_spec(stored, template_heroes(_TILED_TEMPLATE), {}) is stored
    # A default the template does not offer is no fallback: `gradient` is.
    assert served_hero_spec(stored, SHARED_HEROES, {"title": "cascade"})["slides"][0]["hero"] == "gradient"


def test_the_deck_response_serves_the_template_covers_and_the_fitted_spec():
    from app.services.presentations import serialize_presentation, template_heroes

    deck = _existing_row(uuid.uuid4(), slides=2)
    deck.spec["slides"][0]["hero"] = "mosaic"
    brand = serialize_presentation(deck, supported_heroes=template_heroes(_BRAND_TEMPLATE), hero_defaults={"title": "gradient"})
    assert brand["supported_heroes"] == ["plain", "gradient", "split", "spotlight"]
    assert brand["spec"]["slides"][0]["hero"] == "gradient"
    tiled = serialize_presentation(deck, supported_heroes=template_heroes(_TILED_TEMPLATE))
    assert tiled["supported_heroes"] == ["plain", "gradient", "split", "spotlight", "mosaic", "cascade", "horizon", "pillar"]
    assert tiled["spec"]["slides"][0]["hero"] == "mosaic"
    assert deck.spec["slides"][0]["hero"] == "mosaic"


def test_patch_holds_a_template_cover_to_the_template_that_offers_it(monkeypatch):
    """The editor's save follows the slide-theme rule for heroes: a save that
    SETS a tiled cover the deck's template does not offer is a 422 naming the
    slide, nothing written; one the stored deck already carried survives a
    title edit; a template change gives every cover it does not offer its
    default; and the tiled covers are the template's to offer."""
    from fastapi import HTTPException

    from app.api.v1 import presentations as api
    from app.api.v1.schemas.presentation import PresentationUpdate

    owner = uuid.uuid4()
    deck = _existing_row(owner, slides=2)
    user = _rest_user(owner)
    brand = uuid.uuid4()
    tiled = uuid.uuid4()
    facts_by_id = {
        brand: _facts(brand, name="Filigran Brand Deck", extra_metadata=_BRAND_TEMPLATE),
        tiled: _facts(tiled, name="THREAD Conference Deck", extra_metadata=_TILED_TEMPLATE),
    }

    async def _fake_facts(_db, template_id, *, user_id, is_admin=False):
        return facts_by_id.get(template_id)

    monkeypatch.setattr(api, "load_visible_template_facts", _fake_facts)

    async def _patch(body, session):
        return await api.update_presentation(str(deck.id), body, request=None, user=user, db=session)

    # The brand template: a cascade cover is refused, naming the slide and the covers it offers.
    deck.template_container_id = brand
    slides = copy.deepcopy(deck.spec["slides"])
    slides[0]["hero"] = "cascade"
    session = _FakeSession(results=_deck_lookup(deck))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_patch(PresentationUpdate(slides=slides, expected_revision=1), session))
    assert exc.value.status_code == 422
    assert exc.value.detail.startswith(
        "Slide 1 hero 'cascade': a cover the template 'Filigran Brand Deck' does not offer "
        "(plain / gradient / split / spotlight)."
    )
    assert "hero" not in deck.spec["slides"][0]
    assert not session.committed

    # A free deck offers the shared covers only.
    deck.template_container_id = None
    session = _FakeSession(results=_deck_lookup(deck))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_patch(PresentationUpdate(slides=slides, expected_revision=1), session))
    assert exc.value.detail.startswith("Slide 1 hero 'cascade': a cover a deck without a template does not offer")

    # The template that declares it takes it.
    deck.template_container_id = tiled
    session = _FakeSession(results=_deck_lookup(deck))
    session.refresh = _noop_refresh
    asyncio.run(_patch(PresentationUpdate(slides=slides, expected_revision=1), session))
    assert deck.spec["slides"][0]["hero"] == "cascade"
    assert deck.revision == 2

    # Re-linking the brand template gives the cover the brand's own default.
    session = _FakeSession(results=_deck_lookup(deck))
    session.refresh = _noop_refresh
    asyncio.run(_patch(PresentationUpdate(template_container_id=str(brand), expected_revision=2), session))
    assert deck.template_container_id == brand
    assert deck.spec["slides"][0]["hero"] == "gradient"
    assert deck.revision == 3

    # A cover the stored deck already wore (its template narrowed its covers
    # since) is not refused by a title edit of that slide.
    deck.spec["slides"][0]["hero"] = "mosaic"
    slides = copy.deepcopy(deck.spec["slides"])
    slides[0]["title"] = "Renamed"
    session = _FakeSession(results=_deck_lookup(deck))
    session.refresh = _noop_refresh
    asyncio.run(_patch(PresentationUpdate(slides=slides, expected_revision=3), session))
    assert deck.spec["slides"][0]["title"] == "Renamed"
    assert deck.spec["slides"][0]["hero"] == "mosaic"
    assert deck.revision == 4


@pytest.mark.parametrize(
    ("raw", "kind", "stored", "problem"),
    [
        (["Pillar", "mosaic", "mosaic"], "presentation", ["mosaic", "pillar"], None),
        ([], "presentation", None, None),
        (None, "presentation", None, None),
        ("mosaic", "presentation", "mosaic", "must be a list"),
        (["mosaic", "gradient"], "presentation", ["mosaic", "gradient"], "names 'gradient'"),
        (["neon"], "presentation", ["neon"], "names 'neon'"),
        (["mosaic"], "report", None, None),
    ],
)
def test_validate_template_heroes_holds_the_write_to_what_the_reader_accepts(raw, kind, stored, problem):
    """`extra_metadata.heroes` names the template's OWN covers: a shared one or
    an unknown one is refused rather than silently ignored on read; an empty
    declaration is dropped; a template that dresses no deck drops the key."""
    from app.services.template_containers import validate_template_heroes

    meta, problems = validate_template_heroes({"heroes": raw, "guidance": "x"}, kind=kind)
    if problem is None:
        assert problems == []
    else:
        assert len(problems) == 1 and problem in problems[0], problems
    assert meta.get("heroes") == stored
    assert meta["guidance"] == "x"
    assert validate_template_heroes({"guidance": "x"}, kind=kind) == ({"guidance": "x"}, [])


def test_patch_relink_refuses_an_invisible_template_and_a_non_deck_kind(monkeypatch):
    """A template the editor cannot see is a 404 (not found OR not visible -
    the two are indistinguishable on purpose); an asset library has no deck
    palette, so linking it is a 422 that names its kind. Neither writes."""
    from fastapi import HTTPException

    from app.api.v1 import presentations as api
    from app.api.v1.schemas.presentation import PresentationUpdate

    owner = uuid.uuid4()
    deck = _existing_row(owner, slides=2)
    user = _rest_user(owner)
    library = uuid.uuid4()

    async def _fake_facts(_db, template_id, *, user_id, is_admin=False):
        if template_id == library:
            return _facts(library, name="Partner logos", kind="asset-library", schemes=())
        return None

    monkeypatch.setattr(api, "load_visible_template_facts", _fake_facts)

    async def _patch(body, session):
        return await api.update_presentation(str(deck.id), body, request=None, user=user, db=session)

    session = _FakeSession(results=_deck_lookup(deck))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_patch(PresentationUpdate(template_container_id=str(uuid.uuid4())), session))
    assert exc.value.status_code == 404
    assert exc.value.detail == "Template not found"

    session = _FakeSession(results=_deck_lookup(deck))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_patch(PresentationUpdate(template_container_id=str(library)), session))
    assert exc.value.status_code == 422
    assert exc.value.detail == (
        "'Partner logos' is not a presentation template (kind: asset-library), so it cannot dress a deck."
    )

    session = _FakeSession(results=_deck_lookup(deck))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_patch(PresentationUpdate(template_container_id="not-a-uuid"), session))
    assert exc.value.status_code in (400, 422)

    assert deck.template_container_id is None
    assert deck.revision == 1


def test_patch_theme_is_free_when_the_stored_template_is_invisible(monkeypatch):
    """A deck whose STORED template the editor cannot see (un-shared since, or a
    collaborator's private template) accepts a theme change to ANY scheme -
    on purpose, and consistently with what that editor reads: the
    read path renders such a deck as a free deck (no palette, no template name,
    ``supported_schemes`` = both), so its picker offers both schemes and a
    refusal here would contradict the response the editor is looking at. The
    link is kept as stored and the invisible template's schemes are never read
    on the editor's behalf - no visibility bypass is introduced."""
    from app.api.v1 import presentations as api
    from app.api.v1.schemas.presentation import PresentationUpdate
    from app.services.presentations import (
        THEME_SCHEMES,
        resolve_template_palette,
        serialize_presentation,
    )

    owner = uuid.uuid4()
    hidden = uuid.uuid4()
    deck = _existing_row(owner, slides=2, template_container_id=hidden)
    user = _rest_user(owner)
    seen: list[tuple[uuid.UUID, uuid.UUID | None]] = []

    async def _fake_facts(_db, template_id, *, user_id, is_admin=False):
        # Not found and not visible are indistinguishable here on purpose.
        seen.append((template_id, user_id))
        return None

    monkeypatch.setattr(api, "load_visible_template_facts", _fake_facts)

    session = _FakeSession(results=_deck_lookup(deck))
    session.refresh = _noop_refresh
    asyncio.run(
        api.update_presentation(
            str(deck.id), PresentationUpdate(theme="light", expected_revision=1), request=None, user=user, db=session
        )
    )
    # The template WAS looked up as the editor sees it, contributed nothing,
    # and the change went through: any scheme, link untouched, revision bumped.
    assert seen == [(hidden, owner)]
    assert deck.theme == "light"
    assert deck.spec["theme"] == "light"
    assert deck.template_container_id == hidden
    assert deck.revision == 2
    assert session.committed

    # The READ of the same deck by the same editor is a free deck: the invisible
    # template contributes no palette and no name, and the picker is offered
    # both schemes - the list this PATCH must stay consistent with.
    read_session = _FakeSession(results=[_Result(rows=[]), _Result(one=None)])
    palette = asyncio.run(resolve_template_palette(read_session, hidden, user_id=owner))
    assert palette.supported_schemes is None
    assert palette.name is None
    assert palette.theme_for(deck.theme) == {}
    payload = serialize_presentation(
        deck,
        theme_config=palette.theme_for(deck.theme),
        theme_configs=palette.themes,
        supported_schemes=palette.supported_schemes,
        template_name=palette.name,
    )
    assert payload["supported_schemes"] == list(THEME_SCHEMES)
    assert payload["template_name"] is None
    assert payload["theme"] == "light"
    assert payload["theme_config"] == {}


def test_serializer_exposes_the_spec_and_resolved_theme():
    from app.services.presentations import serialize_presentation

    deck = _existing_row(uuid.uuid4(), slides=2)
    payload = serialize_presentation(deck, theme_config={"accent": "#fff"})
    assert payload["spec"]["title"] == "Deck"
    assert payload["slide_count"] == 2
    assert payload["revision"] == 1
    assert payload["theme_config"] == {"accent": "#fff"}
    # A spec that names no format is a 16:9 slide deck.
    assert payload["aspect_ratio"] == "16:9"
    assert payload["kind"] == "deck"


def test_portrait_formats_are_a_distinct_page_kind():
    """A4 / letter output is a DOCUMENT, not a slide show: the kind is
    derived from the aspect ratio everywhere - serializer (summary AND full),
    the spec helpers and the noun the tools speak - so the frontend can pick a
    stacked-page viewer and the agent never calls a product brief "2 slides"."""
    from app.services.presentation_spec import (
        PRESENTATION_KINDS,
        presentation_kind,
        spec_kind,
        unit_noun,
    )
    from app.services.presentations import serialize_presentation

    assert PRESENTATION_KINDS == ("deck", "page")
    assert presentation_kind("16:9") == "deck"
    assert presentation_kind("4:3") == "deck"
    assert presentation_kind(None) == "deck"
    assert presentation_kind("A4") == "page"
    assert presentation_kind("letter") == "page"
    # The stored spelling is canonical, but the helper is tolerant anyway.
    assert presentation_kind("a4") == "page"
    assert spec_kind({}) == "deck"
    assert spec_kind({"aspect_ratio": "A4"}) == "page"
    assert unit_noun({"aspect_ratio": "A4"}, 1) == "page"
    assert unit_noun({"aspect_ratio": "A4"}, 2) == "pages"
    assert unit_noun({}, 1) == "slide"
    assert unit_noun({"aspect_ratio": "4:3"}, 6) == "slides"

    brief = _existing_row(uuid.uuid4(), slides=2)
    brief.spec = {**brief.spec, "aspect_ratio": "A4"}
    full = serialize_presentation(brief)
    summary = serialize_presentation(brief, include_spec=False)
    assert full["kind"] == summary["kind"] == "page"
    assert full["aspect_ratio"] == summary["aspect_ratio"] == "A4"
    assert "spec" not in summary


def test_tool_results_name_a_portrait_brief_as_pages_not_slides():
    from app.services.builtin_tools.presentation import _count, _describe

    brief = {"title": "Brief", "aspect_ratio": "A4", "slides": _slides(2)}
    assert _describe(brief) == "A4 page"
    assert _count(brief) == "2 pages"
    one_pager = {"title": "One-pager", "aspect_ratio": "letter", "slides": _slides(1)}
    assert _describe(one_pager) == "letter page"
    assert _count(one_pager) == "1 page"
    deck = {"title": "Deck", "slides": _slides(6)}
    assert _describe(deck) == "presentation"
    assert _count(deck) == "6 slides"


def _list_page(user, *, total: int = 0, **query):
    """Call ``GET /presentations`` directly: the page, its COUNT and its row query.

    The statements run in order: the caller's groups, the COUNT of every
    match, then the page of rows.
    """
    from app.api.v1.presentations import list_presentations

    # Called directly, so every Query default must be spelled out.
    params = {
        "conversation_id": None,
        "search": None,
        "vis": None,
        "visibility": "all",
        "mine": False,
        "kind": None,
        "limit": 24,
        "offset": 0,
        "sort": "updated_at",
        "direction": "desc",
        **query,
    }
    session = _FakeSession(results=[_Result(rows=[]), _Result(one=total), _Result(rows=[])])
    page = asyncio.run(list_presentations(**params, user=user, db=session))
    return page, _compiled(session.statements[1]), _compiled(session.statements[2])


def _where(sql: str) -> str:
    return sql.split("WHERE", 1)[1].split(" ORDER BY", 1)[0]


def test_list_endpoint_filters_by_kind_in_sql():
    """``?kind=page`` keeps the A4 / letter rows and ``?kind=deck`` keeps the
    rest INCLUDING specs that name no format (they are 16:9); an unknown kind
    is a 422, not an empty list."""
    from fastapi import HTTPException

    user = _rest_user()

    def _run(kind):
        return _list_page(user, kind=kind)[2]

    unfiltered = _run(kind=None)
    assert "aspect_ratio" not in unfiltered
    pages = _run(kind="page")
    assert "aspect_ratio" in pages and "IN (" in pages
    assert "A4" in pages and "letter" in pages
    decks = _run(kind="deck")
    assert "aspect_ratio" in decks and "NOT IN (" in decks
    assert "IS NULL" in decks

    with pytest.raises(HTTPException) as exc:
        _run(kind="poster")
    assert exc.value.status_code == 422


def test_list_endpoint_sorts_the_whole_match_on_the_server():
    """``?sort=&direction=`` order every match before the page is cut (the
    library's data table sorts on the server), the newest change breaking ties;
    the default stays the last changed first; an unknown sort is a 422."""
    from fastapi import HTTPException

    user = _rest_user()

    def _order(**query):
        return _list_page(user, **query)[2].split("ORDER BY", 1)[1]

    assert _order().strip().startswith("presentations.updated_at DESC")
    assert _order().count("presentations.updated_at") == 1
    by_title = _order(sort="title", direction="asc")
    assert by_title.strip().startswith("lower(presentations.title) ASC")
    assert "presentations.updated_at DESC" in by_title
    assert _order(sort="slide_count", direction="desc").strip().startswith("presentations.slide_count DESC")
    assert _order(sort="created_at", direction="asc").strip().startswith("presentations.created_at ASC")

    for bad in ({"sort": "owner"}, {"direction": "sideways"}):
        with pytest.raises(HTTPException) as exc:
            _list_page(user, **bad)
        assert exc.value.status_code == 422


def test_list_endpoint_is_three_tier_for_admins_too():
    """The library LIST never takes the admin bypass.

    ``user_visible_filter(..., is_admin=True)`` degenerates to SQL ``TRUE``,
    which made an admin's Presentations page list every user's private deck
    - rows the ``Mine`` / ``Shared`` / ``Company`` pills cannot account for.
    Like agents, prompts and every other library page, the list runs the same
    own + shared + company-managed predicate for an admin; only the detail
    read (``_load_visible``) keeps the bypass, which is how a deck found in
    Settings > Objects opens. The COUNT behind the page bar is the same.
    """

    def _list_sql(user) -> str:
        _page, count_sql, rows_sql = _list_page(user)
        assert _where(count_sql) == _where(rows_sql)
        return rows_sql

    admin = _rest_user(admin=True)
    admin_sql = _list_sql(admin)
    # Before the fix this read ``WHERE true``.
    where = admin_sql.split("WHERE", 1)[1]
    assert f"presentations.user_id = '{admin.id}'" in where
    assert "presentations.is_company_managed" in where
    assert "shared_user_ids" in where

    # The very same predicate a regular user gets - the admin flag changes
    # nothing about what the library lists.
    member = _rest_user()
    member_sql = _list_sql(member)
    assert member_sql.replace(str(member.id), "<caller>") == admin_sql.replace(str(admin.id), "<caller>")


def test_the_list_comes_a_page_at_a_time_with_the_total_of_every_match():
    """The library pages in SQL: the newest change first, ties broken
    by id so no deck appears on two pages, and a total counted over every deck
    the filters match - not the page - so the page bar reaches the last one."""
    page, count_sql, rows_sql = _list_page(_rest_user(), total=57, limit=24, offset=48, kind="page")

    assert page.total == 57
    assert page.items == []
    assert count_sql.startswith("SELECT count(*)")
    assert _where(count_sql) == _where(rows_sql)
    assert "ORDER BY presentations.updated_at DESC, presentations.id DESC" in rows_sql
    assert rows_sql.endswith("LIMIT 24 OFFSET 48")


def test_the_list_filters_and_searches_with_the_rule_the_video_library_shares():
    """The pills and the word search are ``library_filters``'s, the rule the
    Videos tab applies: the route adds nothing of its own to them."""
    from sqlalchemy import select

    from app.models.presentation import Presentation
    from app.services.library_filters import library_filters

    user = _rest_user()
    for visibility in ("all", "mine", "shared", "company"):
        for search in (None, "board  Ada"):
            expected = _where(
                _compiled(
                    select(Presentation).where(
                        *library_filters(Presentation, user, [], search=search, visibility=visibility)
                    )
                )
            )
            _page, count_sql, rows_sql = _list_page(user, visibility=visibility, search=search)
            assert _where(rows_sql) == _where(count_sql) == expected, (visibility, search)


def test_the_library_rule_narrows_by_pill_and_matches_every_word_on_title_owner_or_template():
    from sqlalchemy import select

    from app.models.presentation import Presentation
    from app.models.video_studio import VideoProject
    from app.services.library_filters import MAX_SEARCH_WORDS, library_filters, search_words
    from app.services.video_studio.library import project_filters

    user = _rest_user()

    def where(model=Presentation, **kw) -> str:
        return _where(_compiled(select(model).where(*library_filters(model, user, [], **kw))))

    everything = where()
    assert f"presentations.user_id = '{user.id}'" in everything
    assert "presentations.is_company_managed IS true" in everything

    mine = where(visibility="mine")
    assert everything.strip() in mine
    assert mine.endswith(f"AND presentations.user_id = '{user.id}' AND presentations.is_company_managed IS NOT true")
    shared = where(visibility="shared")
    assert shared.endswith(
        f"AND (presentations.user_id IS NULL OR presentations.user_id != '{user.id}') "
        "AND presentations.is_company_managed IS NOT true"
    )
    assert where(visibility="company").endswith("AND presentations.is_company_managed IS true")

    # Every word is required, each found in the title, the owner's name or a visible template's name.
    searched = where(search="Board  ADA 50%")
    for word in ("Board", "ADA", "50/%%"):
        needle = f"ILIKE '%%' || '{word}' || '%%' ESCAPE '/'"
        assert f"presentations.title {needle}" in searched
        assert f"users.full_name {needle}" in searched
        assert f"template_containers.name {needle}" in searched
    assert search_words(" ".join(f"w{n}" for n in range(20))) == [f"w{n}" for n in range(MAX_SEARCH_WORDS)]
    assert search_words("   ") == search_words(None) == []

    # The Videos tab reads the very same rule over its own table.
    video = _where(
        _compiled(select(VideoProject).where(*project_filters(user, [], search="board", visibility="shared")))
    )
    assert video == where(VideoProject, search="board", visibility="shared")


# ── External channels ────────────────────────────────────────────────────────


def test_channel_markers_become_web_app_links():
    from app.channels.base import resolve_channel_presentation_markers

    deck_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    row = SimpleNamespace(id=deck_id, title="Q3 review")
    session = _FakeSession(results=[_Result(rows=[row])])

    text = asyncio.run(
        resolve_channel_presentation_markers(
            session,
            f"Here is the deck [[PRESENTATION:{deck_id}]]",
            "https://app.example.com/",
            conversation_id=conversation_id,
        )
    )
    assert f"[Q3 review](https://app.example.com/presentations/{deck_id})" in text
    assert "[[PRESENTATION" not in text


def test_channel_markers_keep_the_slide_to_land_on():
    """A marker carrying a focus slide becomes a link INTO the deck:
    the ``#/<slide id>`` fragment the viewer reads rides on the web-app link,
    as written (slide ids are case-sensitive; the uuid is lowercased as before).
    A marker for a deck out of scope is stripped whole, fragment included."""
    from app.channels.base import resolve_channel_presentation_markers

    deck_id = uuid.uuid4()
    row = SimpleNamespace(id=deck_id, title="Q3 review")
    session = _FakeSession(results=[_Result(rows=[row])])

    text = asyncio.run(
        resolve_channel_presentation_markers(
            session,
            f"Slide 4 is updated: [[PRESENTATION:{str(deck_id).upper()}#/Ab3_x-9]]",
            "https://app.example.com/",
            conversation_id=uuid.uuid4(),
        )
    )
    assert text == f"Slide 4 is updated: [Q3 review](https://app.example.com/presentations/{deck_id}#/Ab3_x-9)"

    session = _FakeSession(results=[_Result(rows=[])])
    text = asyncio.run(
        resolve_channel_presentation_markers(
            session,
            f"see [[PRESENTATION:{uuid.uuid4()}#/Ab3_x-9]] now",
            "https://app.example.com",
            conversation_id=uuid.uuid4(),
        )
    )
    assert text == "see  now"


def test_channel_resolver_absolutizes_the_agent_s_relative_slide_links():
    """The edit account hands the agent RELATIVE slide links — the web
    chat's in-place navigation shape. In a channel reply they resolve nowhere,
    so the resolver absolutizes an in-scope deck's relative viewer paths against
    the same frontend URL the marker uses (fragment kept, bare or in a Markdown
    link), leaves an out-of-scope path exactly as written, never touches a path
    already inside an absolute URL, and still needs a scope to do anything."""
    from app.channels.base import resolve_channel_presentation_markers

    deck_id = uuid.uuid4()
    other_id = uuid.uuid4()
    row = SimpleNamespace(id=deck_id, title="Q3 review")
    session = _FakeSession(results=[_Result(rows=[row])])

    text = asyncio.run(
        resolve_channel_presentation_markers(
            session,
            f"Done - see [slide 4](/presentations/{str(deck_id).upper()}#/Ab3_x-9) "
            f"or the whole deck /presentations/{deck_id}. Elsewhere: "
            f"[old](/presentations/{other_id}#/s1) and https://x.io/presentations/{deck_id}#/s2 "
            f"[[PRESENTATION:{deck_id}#/Ab3_x-9]]",
            "https://app.example.com/",
            conversation_id=uuid.uuid4(),
        )
    )
    assert f"[slide 4](https://app.example.com/presentations/{deck_id}#/Ab3_x-9)" in text
    assert f"whole deck https://app.example.com/presentations/{deck_id}." in text
    assert f"[old](/presentations/{other_id}#/s1)" in text
    assert f"https://x.io/presentations/{deck_id}#/s2" in text
    assert f"[Q3 review](https://app.example.com/presentations/{deck_id}#/Ab3_x-9)" in text
    assert "[[PRESENTATION" not in text
    # One query covers markers and links alike.
    assert len(session.statements) == 1

    session = _FakeSession()
    relative = f"see [slide 4](/presentations/{deck_id}#/Ab3_x-9)"
    assert asyncio.run(resolve_channel_presentation_markers(session, relative, "https://app.example.com")) == relative
    assert session.statements == []


def test_channel_markers_for_unknown_decks_are_stripped():
    from app.channels.base import resolve_channel_presentation_markers

    session = _FakeSession(results=[_Result(rows=[])])
    text = asyncio.run(
        resolve_channel_presentation_markers(
            session,
            f"deck [[PRESENTATION:{uuid.uuid4()}]] done",
            "https://app.example.com",
            conversation_id=uuid.uuid4(),
        )
    )
    assert "[[PRESENTATION" not in text
    assert "deck  done" in text


def test_channel_markers_are_stripped_when_there_is_no_scope():
    """Fail closed: with nothing to authorize against, never emit a link."""
    from app.channels.base import resolve_channel_presentation_markers

    session = _FakeSession()
    text = asyncio.run(
        resolve_channel_presentation_markers(
            session,
            f"[[PRESENTATION:{uuid.uuid4()}]]",
            "https://app.example.com",
        )
    )
    assert text == ""
    assert session.statements == []


def test_channel_router_resolves_presentation_markers():
    import inspect

    from app.channels import router

    source = inspect.getsource(router)
    assert "resolve_channel_presentation_markers" in source


# ── Template asset access (assets inside a deck) ───────────────────────


def _authorize(file_att, user, tpl_visible: bool):
    """Run ``_authorize_file_access`` with a fake session for the template probe."""
    from app.api.v1.chat import _authorize_file_access

    class _Session:
        def __init__(self):
            self.statements = []

        async def execute(self, statement, *a, **k):
            self.statements.append(statement)
            # First query is the group lookup, later ones the visibility probe.
            return _Result(one=file_att.template_container_id if tpl_visible else None)

    return asyncio.run(_authorize_file_access(file_att, user, _Session()))


def _template_asset(owner_id, container_id):
    from app.models.template_container import TEMPLATE_ASSET_SOURCE

    return SimpleNamespace(
        user_id=owner_id,
        conversation_id=None,
        assignment_run_id=None,
        template_container_id=container_id,
        source=TEMPLATE_ASSET_SOURCE,
    )


def test_slide_image_from_a_visible_template_is_downloadable(monkeypatch):
    """A deck built from a company-managed brand template must render its images
    for every user the template is shared with — not only for the admin who
    uploaded them (which would silently blank every branded slide)."""
    import app.api.v1.helpers as helpers

    async def _no_groups(_db, _user_id):
        return []

    monkeypatch.setattr(helpers, "get_user_group_ids", _no_groups)

    asset = _template_asset(owner_id=uuid.uuid4(), container_id=uuid.uuid4())
    reader = SimpleNamespace(id=uuid.uuid4(), role="user")
    # Returns (rather than raising) = access granted.
    assert _authorize(asset, reader, tpl_visible=True) is None


def test_slide_image_from_an_invisible_template_is_refused(monkeypatch):
    from fastapi import HTTPException

    import app.api.v1.helpers as helpers

    async def _no_groups(_db, _user_id):
        return []

    monkeypatch.setattr(helpers, "get_user_group_ids", _no_groups)

    asset = _template_asset(owner_id=uuid.uuid4(), container_id=uuid.uuid4())
    reader = SimpleNamespace(id=uuid.uuid4(), role="user")
    with pytest.raises(HTTPException) as exc:
        _authorize(asset, reader, tpl_visible=False)
    assert exc.value.status_code == 404


def test_container_fk_alone_does_not_grant_access(monkeypatch):
    """Only a row whose ``source`` really is ``template_asset`` may inherit the
    container's visibility — a stray FK on another attachment must not."""
    from fastapi import HTTPException

    import app.api.v1.helpers as helpers

    async def _no_groups(_db, _user_id):
        return []

    monkeypatch.setattr(helpers, "get_user_group_ids", _no_groups)

    asset = _template_asset(owner_id=uuid.uuid4(), container_id=uuid.uuid4())
    asset.source = "chat"
    reader = SimpleNamespace(id=uuid.uuid4(), role="user")
    with pytest.raises(HTTPException):
        _authorize(asset, reader, tpl_visible=True)


# ── Files a readable deck shows ──────────────────────────────────────
#
# A picture an agent generates for a slide is owned by the author and bound to
# the author's conversation; sharing the deck shares the spec, which only names
# the file. Without the grant every other reader of the deck would get a 403 on
# every generated image. The grant is evaluated against the STORED spec of the
# decks the requester may read, so a deck shared before the grant existed is
# covered too.


def _generated_image(owner_id):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=owner_id,
        conversation_id=uuid.uuid4(),
        assignment_run_id=None,
        template_container_id=None,
        source="chat",
    )


def _candidates(specs: list) -> list:
    """The rows the deck scan reads: ``(presentation id, spec)``."""
    return [(uuid.uuid4(), spec) for spec in specs]


def _authorize_with_decks(
    file_att,
    user,
    specs: list,
    monkeypatch,
    *,
    groups: list[str] | None = None,
    pages: list[list] | None = None,
    hinted=None,
    presentation_id=None,
    video_project_id=None,
    custom_dashboard_id=None,
):
    """Run the real authorizer; the deck scan yields *specs* as the readable
    candidates (or *pages* of ``(id, spec)`` rows, one per round-trip), the
    hinted lookup yields *hinted*, and the requester belongs to *groups*.
    Returns the session so the statements can be inspected."""
    import app.api.v1.helpers as helpers
    from app.services.file_access import authorize_file_access

    async def _user_groups(_db, _user_id):
        return list(groups or [])

    monkeypatch.setattr(helpers, "get_user_group_ids", _user_groups)
    scan_pages = list(pages) if pages is not None else [_candidates(specs)]

    class _Session:
        def __init__(self):
            self.statements = []

        async def execute(self, statement, *a, **k):
            self.statements.append(statement)
            if "ILIKE" not in _compiled(statement):
                return _Result(one=hinted)  # the hinted primary-key lookup
            return _Result(rows=scan_pages.pop(0) if scan_pages else [])

    session = _Session()
    asyncio.run(authorize_file_access(file_att, user, session, presentation_id=presentation_id))
    return session


def test_slide_image_of_a_shared_deck_is_downloadable_by_its_readers(monkeypatch):
    """The production failure: the author's agent generated the picture, the
    deck is shared, every colleague sees a blank slide and a 403 in the console
    on ``/chat/files/{id}/download``."""
    image = _generated_image(owner_id=uuid.uuid4())
    reader = SimpleNamespace(id=uuid.uuid4(), role="user")
    deck = {"slides": [{"layout": "content", "blocks": [{"type": "image", "file_id": str(image.id)}]}]}
    session = _authorize_with_decks(image, reader, [deck], monkeypatch)
    # One round-trip: the deck probe (the group lookup is faked out above).
    assert len(session.statements) == 1


def test_any_slide_field_naming_the_file_grants_it(monkeypatch):
    """Backgrounds, speaker photos and architecture logos are ``*_file_id``
    fields, not ``image`` blocks — the same walk the verifier uses finds them."""
    image = _generated_image(owner_id=uuid.uuid4())
    reader = SimpleNamespace(id=uuid.uuid4(), role="user")
    deck = {"slides": [{"layout": "title", "background_image_file_id": str(image.id)}]}
    _authorize_with_decks(image, reader, [deck], monkeypatch)
    deck = {
        "slides": [
            {
                "layout": "content",
                "blocks": [{"type": "architecture", "nodes": [{"id": "hub", "logo_file_id": str(image.id).upper()}]}],
            }
        ]
    }
    # A stored id in another case is the same file: the comparison is by value.
    _authorize_with_decks(image, reader, [deck], monkeypatch)


def test_a_deck_that_merely_mentions_the_id_in_prose_grants_nothing(monkeypatch):
    """The SQL pre-filter is a text match; the decision is structural. A UUID
    pasted into a slide's text is a mention, not a reference."""
    from fastapi import HTTPException

    image = _generated_image(owner_id=uuid.uuid4())
    reader = SimpleNamespace(id=uuid.uuid4(), role="user")
    deck = {"slides": [{"layout": "content", "blocks": [{"type": "text", "content": f"see file {image.id}"}]}]}
    with pytest.raises(HTTPException) as exc:
        _authorize_with_decks(image, reader, [deck], monkeypatch)
    assert exc.value.status_code == 404


def test_a_file_no_readable_deck_names_stays_refused(monkeypatch):
    """A deck naming ANOTHER file, or no candidate at all, changes nothing: the
    contract still ends in the 403 it always did."""
    from fastapi import HTTPException

    image = _generated_image(owner_id=uuid.uuid4())
    reader = SimpleNamespace(id=uuid.uuid4(), role="user")
    other = {"slides": [{"layout": "content", "blocks": [{"type": "image", "file_id": str(uuid.uuid4())}]}]}
    with pytest.raises(HTTPException) as exc:
        _authorize_with_decks(image, reader, [other], monkeypatch)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException):
        _authorize_with_decks(image, reader, [], monkeypatch)


def test_the_deck_scan_is_the_three_tier_visibility_predicate_paged(monkeypatch):
    """What the grant is made of: only decks the requester may READ (the same
    predicate as the library list, so an invisible deck grants nothing), a
    case-insensitive text pre-filter in both UUID spellings, read a page at a
    time in id order."""
    from app.services.file_access import PRESENTATION_SCAN_PAGE_SIZE

    image = _generated_image(owner_id=uuid.uuid4())
    reader = SimpleNamespace(id=uuid.uuid4(), role="user")
    deck = {"slides": [{"layout": "image", "file_id": str(image.id)}]}
    session = _authorize_with_decks(image, reader, [deck], monkeypatch, groups=["team-a"])
    sql = _compiled(session.statements[0])
    assert "FROM presentations" in sql
    assert f"presentations.user_id = '{reader.id}'" in sql, "own decks"
    assert "presentations.is_company_managed" in sql, "company-managed decks"
    assert f"'{reader.id}'" in sql and "shared_user_ids" in sql, "decks shared with the person"
    assert "'team-a'" in sql and "shared_group_ids" in sql, "decks shared with the person's groups"
    assert sql.count("ILIKE") == 2
    assert f"%{image.id}%" in sql and f"%{image.id.hex}%" in sql
    assert f"LIMIT {PRESENTATION_SCAN_PAGE_SIZE}" in sql
    assert "ORDER BY presentations.id" in sql
    assert "presentations.id >" not in sql, "the first page starts at the beginning"


def test_the_deck_scan_is_exhaustive_a_page_at_a_time(monkeypatch):
    """A file shown by ONE deck among many that merely mention its id is still
    granted: a full page of mentions is followed by the next page (keyset on
    the last id seen), and the walk stops at the first deck that names it. The
    page size is a round-trip size, never a cap on correctness."""
    from fastapi import HTTPException

    from app.services.file_access import PRESENTATION_SCAN_PAGE_SIZE

    image = _generated_image(owner_id=uuid.uuid4())
    reader = SimpleNamespace(id=uuid.uuid4(), role="user")
    mention = {"slides": [{"layout": "content", "blocks": [{"type": "text", "content": f"see {image.id}"}]}]}
    shows = {"slides": [{"layout": "content", "blocks": [{"type": "image", "file_id": str(image.id)}]}]}
    first_page = _candidates([mention] * PRESENTATION_SCAN_PAGE_SIZE)
    session = _authorize_with_decks(
        image, reader, [], monkeypatch, pages=[first_page, _candidates([mention, shows, mention])]
    )
    assert len(session.statements) == 2
    second = _compiled(session.statements[1])
    assert f"presentations.id > '{first_page[-1][0]}'" in second, "the next page starts after the last id seen"
    assert "ORDER BY presentations.id" in second and f"LIMIT {PRESENTATION_SCAN_PAGE_SIZE}" in second

    # A short page is the last one: no further round-trip, and still a 403 when
    # nothing in it names the file.
    with pytest.raises(HTTPException) as exc:
        _authorize_with_decks(image, reader, [], monkeypatch, pages=[first_page, _candidates([mention])])
    assert exc.value.status_code == 404


def test_the_deck_the_request_is_for_is_tried_first_by_primary_key(monkeypatch):
    """The static render page behind the export and the layout check fetches
    every picture of ONE deck and says which: that deck is looked up by id
    under the same visibility predicate, and a hit costs no text scan."""
    image = _generated_image(owner_id=uuid.uuid4())
    reader = SimpleNamespace(id=uuid.uuid4(), role="user")
    deck_id = uuid.uuid4()
    shows = {"slides": [{"layout": "content", "blocks": [{"type": "image", "file_id": str(image.id)}]}]}
    session = _authorize_with_decks(
        image, reader, [], monkeypatch, groups=["team-a"], hinted=shows, presentation_id=deck_id
    )
    assert len(session.statements) == 1
    sql = _compiled(session.statements[0])
    assert f"presentations.id = '{deck_id}'" in sql
    assert "ILIKE" not in sql, "a hit on the hinted deck never scans"
    assert f"presentations.user_id = '{reader.id}'" in sql and "'team-a'" in sql, "the same predicate"

    # A hint that does not pan out - the deck is invisible to the reader, or
    # it does not name the file - falls through to the scan; it never refuses.
    session = _authorize_with_decks(image, reader, [shows], monkeypatch, hinted=None, presentation_id=deck_id)
    assert len(session.statements) == 2 and "ILIKE" in _compiled(session.statements[1])
    mention = {"slides": [{"layout": "content", "blocks": [{"type": "text", "content": str(image.id)}]}]}
    session = _authorize_with_decks(image, reader, [shows], monkeypatch, hinted=mention, presentation_id=deck_id)
    assert len(session.statements) == 2


def test_the_owner_and_an_admin_never_reach_the_deck_probe():
    """The grant is a last resort: the cheap paths still answer first, and a
    request they answer costs no query at all."""
    from app.services.file_access import authorize_file_access

    class _Session:
        async def execute(self, *a, **k):  # pragma: no cover - must not run
            raise AssertionError("no query expected")

    owner = uuid.uuid4()
    image = _generated_image(owner_id=owner)
    asyncio.run(authorize_file_access(image, SimpleNamespace(id=owner, role="user"), _Session()))
    asyncio.run(authorize_file_access(image, SimpleNamespace(id=uuid.uuid4(), role="admin"), _Session()))


def test_the_grant_follows_the_deck_not_the_picture_s_owner(monkeypatch):
    """Whose file it is does not enter the decision: a picture a THIRD party
    generated - neither the requester nor the deck's owner - is served to the
    deck's readers exactly like the owner's own, and the probe never consults
    ``file_attachments``. This is what lets a reference the REST editor or a
    restore stored BEFORE the write-side verification existed serve as it
    stands instead of needing a repair (the only surface that handed a
    non-admin the id of a file they could not read was a deck they could read
    at the time, which this path now grants by design). Chaining the grant on
    the deck owner's own right to the file would break a ``config`` grantee's
    own picture in the owner's deck and every duplicate of a shared deck, so
    this is a deliberate pin, not an oversight."""
    third_party = uuid.uuid4()
    image = _generated_image(owner_id=third_party)
    reader = SimpleNamespace(id=uuid.uuid4(), role="user")
    deck = {"slides": [{"layout": "image", "file_id": str(image.id)}]}
    session = _authorize_with_decks(image, reader, [deck], monkeypatch)
    sql = _compiled(session.statements[0])
    assert "file_attachments" not in sql
    assert str(third_party) not in sql


def test_spec_references_file_is_structural_and_slides_only():
    from app.services.presentations import spec_references_file

    target = uuid.uuid4()
    assert spec_references_file({"slides": [{"layout": "image", "file_id": str(target)}]}, target)
    assert spec_references_file({"slides": [{"layout": "image", "file_id": str(target).upper()}]}, target)
    assert spec_references_file({"slides": [{"layout": "image", "file_id": target.hex}]}, target)
    assert not spec_references_file({"slides": [{"layout": "content", "blocks": [{"text": str(target)}]}]}, target)
    # Only the slides are walked - the same subset the verifier vouches for.
    assert not spec_references_file({"logo_file_id": str(target), "slides": []}, target)
    assert not spec_references_file(None, target)
    assert not spec_references_file({"slides": "not a list"}, target)


def test_no_deck_level_field_can_name_a_file():
    """Why walking ``slides`` is the WHOLE walk: every ``file_id`` /
    ``*_file_id`` the spec model admits sits under a slide, so on a validated
    spec the grant (``spec_references_file``), the write-side check
    (``verify_file_references``) and the export's render allow-list
    (``referenced_file_ids``, which walks the whole spec) name the same files.
    A deck-level picture field added later - a chrome logo, say - fails here on
    purpose: ``slide_file_references`` must walk it too, or the deck's readers
    get a 403 on it and the writer is never asked to vouch for it."""
    from app.services.presentation_export import referenced_file_ids
    from app.services.presentation_spec import Chrome, PresentationSpec, validate_spec
    from app.services.presentations import slide_file_references

    assert set(PresentationSpec.model_fields) == {"title", "theme", "aspect_ratio", "transition", "chrome", "slides"}
    assert not [name for name in Chrome.model_fields if name.endswith("file_id")]

    background, photo, inline, picture, logo, node_logo = (str(uuid.uuid4()) for _ in range(6))
    spec = validate_spec(
        {
            "title": "Every picture a deck can show",
            "slides": [
                {
                    "layout": "title",
                    "title": "T",
                    "background_image_file_id": background,
                    # The one-speaker form: the portrait folds onto speakers[0].
                    "speaker": {"name": "Ada"},
                    "speaker_photo_file_id": photo,
                },
                # A v1 slide's picture rides the v1 upgrade into an `image`
                # block, so the walk must still find it where it now lives.
                {"layout": "generic", "title": "G", "bullets": ["x"], "image_file_id": inline},
                {
                    "layout": "content",
                    "title": "C",
                    "blocks": [
                        {"type": "image", "file_id": picture},
                        {"type": "logo_grid", "items": [logo]},
                        {
                            "type": "architecture",
                            "nodes": [
                                {"id": "hub", "label": "Hub", "row": 1, "col": 1, "logo_file_id": node_logo},
                                {"id": "edge", "label": "Edge", "row": 1, "col": 2},
                            ],
                            "edges": [{"source": "hub", "target": "edge"}],
                        },
                    ],
                },
            ],
        }
    )
    expected = {background, photo, inline, picture, logo, node_logo}
    assert {value for _, _, value in slide_file_references(spec)} == expected
    assert set(referenced_file_ids(spec)) == expected


# ── File references are grounded on save ─────────────────────────────


def test_walk_file_references_finds_every_file_id_at_any_depth():
    from app.services.presentation_pictures import walk_file_references as _walk_file_references

    a, b, c = (str(uuid.uuid4()) for _ in range(3))
    slide = {
        "layout": "columns",
        "background_image_file_id": a,
        "columns": [
            {"blocks": [{"block": "image", "file_id": b, "caption": "x"}]},
            {"blocks": [{"block": "text", "text": "no file here"}]},
        ],
        "meta": {"logo_file_id": c, "file_id": 42, "not_a_file": "y"},
    }
    found: list[tuple[str, str]] = []
    _walk_file_references(slide, "slides.0 (layout=columns)", found)
    assert found == [
        ("slides.0 (layout=columns).background_image_file_id", a),
        ("slides.0 (layout=columns).columns.0.blocks.0.file_id", b),
        ("slides.0 (layout=columns).meta.logo_file_id", c),
    ]


def _verify(
    spec: dict,
    *,
    rows: list,
    forbidden: set[uuid.UUID],
    monkeypatch,
    baseline: dict | None = None,
    checked: list | None = None,
):
    """Run the verifier against a fake session holding *rows*; ids in
    *forbidden* fail the access check the way a private upload would. Every
    id handed to the access check is appended to *checked* when given."""
    from fastapi import HTTPException

    import app.services.file_access as file_access
    from app.services.presentations import verify_file_references

    async def _authorize(row, _user, _db):
        if checked is not None:
            checked.append(row.id)
        if row.id in forbidden:
            raise HTTPException(status_code=403, detail="nope")

    monkeypatch.setattr(file_access, "authorize_file_access", _authorize)
    session = _FakeSession([_Result(rows=rows)])
    user = SimpleNamespace(id=uuid.uuid4(), role="user")
    asyncio.run(verify_file_references(session, spec, user=user, baseline=baseline))
    return session


def test_an_invented_file_id_is_refused_naming_the_slide_and_field(monkeypatch):
    """The failure the check exists for: a UUID that looks right, names no
    file, and would otherwise render as a slide silently missing its image."""
    from app.services.presentation_spec import PresentationSpecError

    real = SimpleNamespace(id=uuid.uuid4())
    ghost = uuid.uuid4()
    spec = {
        "slides": [
            {"layout": "image", "file_id": str(real.id)},
            {"layout": "image", "file_id": str(ghost)},
        ]
    }
    with pytest.raises(PresentationSpecError) as exc:
        _verify(spec, rows=[real], forbidden=set(), monkeypatch=monkeypatch)
    message = str(exc.value)
    assert f"slides.1 (layout=image).file_id = `{ghost}` is not a platform file" in message
    assert str(real.id) not in message, "the valid reference is not reported"
    assert "get_template" in message, "the fix is pointed at, not just the fault"


def test_a_file_the_user_cannot_read_is_refused_not_served(monkeypatch):
    """Everyone who may read a deck may fetch what its slides name, so
    a deck must not be able to launder someone else's private upload by naming
    its id: the writer's own read access is what vouches for every reference."""
    from app.services.presentation_spec import PresentationSpecError

    private = SimpleNamespace(id=uuid.uuid4())
    spec = {"slides": [{"layout": "image", "file_id": str(private.id)}]}
    with pytest.raises(PresentationSpecError, match="is not a platform file you can read"):
        _verify(spec, rows=[private], forbidden={private.id}, monkeypatch=monkeypatch)


def test_a_carried_picture_whose_file_is_gone_is_returned_not_refused(monkeypatch):
    """A picture the stored deck (or the restored revision) already named
    whose file no longer exists - the removed Filigran deck's assets - never
    refuses the write; it comes back to the caller. An id the write itself
    introduces is still refused, and a carried id that still has a row is still
    checked against the writer."""
    from fastapi import HTTPException

    import app.services.file_access as file_access
    from app.services.presentation_spec import PresentationSpecError
    from app.services.presentations import verify_file_references

    removed, invented = str(uuid.uuid4()), str(uuid.uuid4())
    private = SimpleNamespace(id=uuid.uuid4())

    async def _authorize(row, _user, _db):
        raise HTTPException(status_code=403, detail="nope")

    monkeypatch.setattr(file_access, "authorize_file_access", _authorize)
    user = SimpleNamespace(id=uuid.uuid4(), role="user")
    spec = {"slides": [{"layout": "content", "blocks": [{"type": "image", "file_id": removed}]}]}
    gone = asyncio.run(
        verify_file_references(_FakeSession([_Result(rows=[])]), spec, user=user, carried={removed})
    )
    assert gone == [("slides.0.blocks.0.file_id", "content", removed)]

    introduced = {"slides": [*spec["slides"], {"layout": "content", "blocks": [{"type": "image", "file_id": invented}]}]}
    with pytest.raises(PresentationSpecError, match=invented):
        asyncio.run(
            verify_file_references(_FakeSession([_Result(rows=[])]), introduced, user=user, carried={removed})
        )

    unreadable = {"slides": [{"layout": "content", "blocks": [{"type": "image", "file_id": str(private.id)}]}]}
    with pytest.raises(PresentationSpecError, match="is not a platform file you can read"):
        asyncio.run(
            verify_file_references(
                _FakeSession([_Result(rows=[private])]), unreadable, user=user, carried={str(private.id)}
            )
        )


def test_a_carried_spelling_of_an_existing_unreadable_file_is_still_refused(monkeypatch):
    """The gone check and the row lookup read an id the same way: an upper-case
    spelling of an EXISTING file the writer cannot read, carried from the stored
    spec, finds its row and is refused - it is never taken for a gone picture."""
    from fastapi import HTTPException

    import app.services.file_access as file_access
    from app.services.presentation_pictures import canonical_file_reference
    from app.services.presentation_spec import PresentationSpecError
    from app.services.presentations import verify_file_references

    private = SimpleNamespace(id=uuid.uuid4())

    async def _authorize(row, _user, _db):
        raise HTTPException(status_code=403, detail="nope")

    monkeypatch.setattr(file_access, "authorize_file_access", _authorize)
    user = SimpleNamespace(id=uuid.uuid4(), role="user")
    spelled = str(private.id).upper()
    spec = {"slides": [{"layout": "content", "blocks": [{"type": "image", "file_id": spelled}]}]}
    with pytest.raises(PresentationSpecError, match="is not a platform file you can read"):
        asyncio.run(
            verify_file_references(
                _FakeSession([_Result(rows=[private])]), spec, user=user, carried={canonical_file_reference(spelled)}
            )
        )


def test_a_non_uuid_file_id_is_refused_without_a_lookup(monkeypatch):
    """``FileId`` only bounds the length, so "logo.svg" reaches the verifier."""
    from app.services.presentation_spec import PresentationSpecError

    spec = {"slides": [{"layout": "image", "file_id": "logo.svg"}]}
    with pytest.raises(PresentationSpecError, match=r"`logo\.svg` is not a platform file"):
        _verify(spec, rows=[], forbidden=set(), monkeypatch=monkeypatch)


def test_valid_readable_references_pass_and_a_file_free_deck_skips_the_query(monkeypatch):
    real = SimpleNamespace(id=uuid.uuid4())
    spec = {"slides": [{"layout": "image", "file_id": str(real.id)}]}
    session = _verify(spec, rows=[real], forbidden=set(), monkeypatch=monkeypatch)
    assert len(session.statements) == 1

    session = _verify({"slides": _slides(2)}, rows=[], forbidden=set(), monkeypatch=monkeypatch)
    assert session.statements == [], "no file named, no round-trip"


def test_save_presentation_verifies_references_only_when_given_a_user(monkeypatch):
    """The agent path passes the acting user; the library duplicate passes
    nothing and copies a deck the caller can already read. A baseline, when
    given, travels to the verifier so an edit checks only what it introduces."""
    import app.services.presentations as presentations

    seen: list = []

    async def _record(_db, spec, *, user, baseline=None, carried=()):
        seen.append((user, spec["title"], baseline))
        return []

    monkeypatch.setattr(presentations, "verify_file_references", _record)
    session = _FakeSession()
    user = SimpleNamespace(id=uuid.uuid4(), role="user")
    payload = {"title": "Checked", "slides": _slides(1)}

    asyncio.run(presentations.save_presentation(session, user_id=user.id, spec_payload=payload))
    assert seen == [], "no user, no check"
    asyncio.run(
        presentations.save_presentation(
            session, user_id=user.id, spec_payload=payload, verify_files_for=user
        )
    )
    assert seen == [(user, "Checked", None)]
    stored = {"title": "Before", "slides": _slides(1)}
    asyncio.run(
        presentations.save_presentation(
            session, user_id=user.id, spec_payload=payload, verify_files_for=user, verify_baseline=stored
        )
    )
    assert seen[-1] == (user, "Checked", stored)


def test_a_user_id_is_resolved_lazily_and_an_unknown_one_reads_nothing(monkeypatch):
    """The tool hands the check the acting user's ID, not the row: a file-free
    deck must not pay for a user lookup, and an id that resolves to no user must
    refuse every reference — never skip the check."""
    import app.services.file_access as file_access
    from app.services.presentation_spec import PresentationSpecError
    from app.services.presentations import verify_file_references

    calls: list = []

    async def _authorize(row, user, _db):
        calls.append((row.id, user.id))

    monkeypatch.setattr(file_access, "authorize_file_access", _authorize)

    # No file named: no statement at all, the id is never resolved.
    session = _FakeSession()
    asyncio.run(verify_file_references(session, {"slides": _slides(2)}, user=uuid.uuid4()))
    assert session.statements == []

    # A file named: the rows, then the user, then the access check with the row.
    real = SimpleNamespace(id=uuid.uuid4())
    acting = SimpleNamespace(id=uuid.uuid4(), role="user")
    session = _FakeSession([_Result(rows=[real]), _Result(one=acting)])
    spec = {"slides": [{"layout": "image", "file_id": str(real.id)}]}
    asyncio.run(verify_file_references(session, spec, user=acting.id))
    assert len(session.statements) == 2
    assert calls == [(real.id, acting.id)]

    # The id resolves to nobody: fail closed.
    session = _FakeSession([_Result(rows=[real]), _Result(one=None)])
    with pytest.raises(PresentationSpecError, match="is not a platform file you can read"):
        asyncio.run(verify_file_references(session, spec, user=uuid.uuid4()))
    assert calls == [(real.id, acting.id)], "no access check was attempted without a user"


def test_generate_presentation_passes_the_acting_user_to_the_check():
    import inspect

    from app.services.builtin_tools import presentation as tool

    source = inspect.getsource(tool)
    assert "verify_files_for=user_uuid" in source
    # The check runs AFTER the shape validation so a malformed spec is reported
    # before a dangling reference; ``save_presentation`` orders the two.
    saver = inspect.getsource(
        __import__("app.services.presentations", fromlist=["save_presentation"]).save_presentation
    )
    assert saver.index("validate_spec(") < saver.index("verify_file_references(")


def test_a_baseline_scopes_the_check_to_what_the_write_introduces(monkeypatch):
    """The REST edit and a restore verify against the spec they replace:
    a reference the deck already carries was vouched for by whoever wrote it -
    even one whose file is gone since - and must not block an unrelated edit;
    a reference the write ADDS is checked against the writer."""
    from app.services.presentation_spec import PresentationSpecError

    kept = SimpleNamespace(id=uuid.uuid4())
    fresh = SimpleNamespace(id=uuid.uuid4())
    ghost = uuid.uuid4()  # deleted since it was written: no row
    baseline = {
        "slides": [
            {"layout": "image", "file_id": str(kept.id)},
            {"layout": "image", "file_id": str(ghost)},
        ]
    }
    edited = {
        "slides": [
            {"layout": "image", "file_id": str(kept.id)},
            {"layout": "image", "file_id": str(ghost).upper()},
            {"layout": "image", "file_id": str(fresh.id)},
        ]
    }
    checked: list = []
    session = _verify(edited, rows=[fresh], forbidden=set(), monkeypatch=monkeypatch, baseline=baseline, checked=checked)
    assert checked == [fresh.id], "only the introduced reference is checked"
    assert len(session.statements) == 1

    # Nothing introduced: no lookup at all, whatever the baseline's ids resolve to.
    session = _verify(baseline, rows=[], forbidden=set(), monkeypatch=monkeypatch, baseline=baseline)
    assert session.statements == []

    # An introduced reference the writer cannot read is still refused, and the
    # refusal names it alone.
    foreign = SimpleNamespace(id=uuid.uuid4())
    edited["slides"].append({"layout": "image", "file_id": str(foreign.id)})
    with pytest.raises(PresentationSpecError) as exc:
        _verify(edited, rows=[fresh, foreign], forbidden={foreign.id}, monkeypatch=monkeypatch, baseline=baseline)
    assert str(foreign.id) in str(exc.value)
    assert str(ghost) not in str(exc.value) and str(kept.id) not in str(exc.value)


def test_the_rest_edit_vouches_only_for_the_references_it_introduces():
    """The REST editor can put a file id into a stored spec, and the
    deck-reference grant makes the stored spec an authorization surface:
    what a save ADDS is checked against the person saving - an id of
    no platform file, or of a file they cannot read, is a 422 that writes
    nothing - while a reference the deck already carries is left alone, so a
    picture deleted since never blocks an unrelated edit. Real route, real
    verifier, real authorizer; only the session is faked. (The restore is
    covered the same way just below.)"""
    from fastapi import HTTPException

    from app.api.v1 import presentations as api
    from app.api.v1.schemas.presentation import PresentationUpdate

    owner = uuid.uuid4()
    ghost = str(uuid.uuid4())  # named by the stored deck, deleted since
    # The stored slide is a v1 one on purpose: the save upgrades it
    # to a `content` slide whose `image` block names the same ghost, and that
    # must still read as a reference the deck ALREADY carried, not a new one.
    kept = {"layout": "generic", "title": "Old", "bullets": ["a"], "image_file_id": ghost}
    deck = _existing_row(owner, slides=1)
    deck.spec = {"title": "Deck", "slides": [kept]}
    user = _rest_user(owner)

    def _picture(owner_id):
        return SimpleNamespace(
            id=uuid.uuid4(),
            user_id=owner_id,
            conversation_id=None,
            assignment_run_id=None,
            template_container_id=None,
            source="chat",
        )

    def _patch(slides, session, *, expected_revision):
        body = PresentationUpdate(slides=slides, expected_revision=expected_revision)
        return asyncio.run(api.update_presentation(str(deck.id), body, request=None, user=user, db=session))

    def _new_slide(image_file_id=None):
        blocks = [{"type": "list", "items": ["b"]}]
        if image_file_id is not None:
            blocks.append({"type": "image", "file_id": image_file_id})
        return {"layout": "content", "title": "New", "blocks": blocks}

    # Keeping the ghost and adding a slide without pictures introduces no
    # reference: no file is looked up, the save lands.
    session = _FakeSession(results=_deck_lookup(deck))
    session.refresh = _noop_refresh
    _patch([kept, _new_slide()], session, expected_revision=1)
    assert deck.revision == 2
    assert not [s for s in session.statements if "file_attachments" in _compiled(s)]

    # Introducing an id that is no platform file: 422, nothing written.
    session = _FakeSession(results=[*_deck_lookup(deck), _Result(rows=[])])
    with pytest.raises(HTTPException) as exc:
        _patch([kept, _new_slide(str(uuid.uuid4()))], session, expected_revision=2)
    assert exc.value.status_code == 422
    assert "not a platform file" in str(exc.value.detail)
    assert deck.revision == 2
    assert not session.committed

    # Introducing a colleague's private picture: the real authorizer runs the
    # owner, template and deck paths on the EDITOR's behalf (their groups, then
    # the decks they may read - none here) and refuses -> 422, nothing written.
    foreign = _picture(uuid.uuid4())
    session = _FakeSession(results=[*_deck_lookup(deck), _Result(rows=[foreign])])
    with pytest.raises(HTTPException) as exc:
        _patch([kept, _new_slide(str(foreign.id))], session, expected_revision=2)
    assert exc.value.status_code == 422
    assert "is not a platform file you can read" in str(exc.value.detail)
    assert deck.revision == 2
    assert not session.committed
    assert any("FROM presentations" in _compiled(s) and "ILIKE" in _compiled(s) for s in session.statements)

    # The editor's own picture is theirs to show: saved.
    mine = _picture(owner)
    session = _FakeSession(results=[*_deck_lookup(deck), _Result(rows=[mine])])
    session.refresh = _noop_refresh
    _patch([kept, _new_slide(str(mine.id))], session, expected_revision=2)
    assert deck.revision == 3
    assert deck.spec["slides"][1]["blocks"][1]["file_id"] == str(mine.id)
    # The stored v1 slide left with this save, its ghost picture with it.
    assert deck.spec["slides"][0]["layout"] == "content"
    assert deck.spec["slides"][0]["blocks"][-1] == {**deck.spec["slides"][0]["blocks"][-1], "type": "image", "file_id": ghost}
    assert session.committed


def test_a_restore_re_verifies_only_the_references_it_brings_back(monkeypatch):
    """Bringing back a revision that names a file the deck no longer shows is
    an introduction: checked against the person restoring, refused when they
    cannot read it, saved when they can."""
    from fastapi import HTTPException

    import app.services.file_access as file_access
    from app.models.presentation import PresentationVersion
    from app.services.presentation_spec import PresentationSpecError
    from app.services.presentation_versions import restore_presentation_version

    owner = uuid.uuid4()
    restorer = SimpleNamespace(id=uuid.uuid4(), role="user")
    picture = SimpleNamespace(id=uuid.uuid4(), user_id=owner)
    deck = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=owner,
        conversation_id=None,
        template_container_id=None,
        title="Now",
        theme="dark",
        slide_count=1,
        revision=3,
        spec={"title": "Now", "slides": _slides(1)},
        updated_by_user_id=owner,
    )
    old = SimpleNamespace(
        presentation_id=deck.id,
        revision=1,
        spec={
            "title": "Then",
            "slides": [
                {"layout": "content", "blocks": [{"type": "image", "file_id": str(picture.id), "alt": "then"}]}
            ],
        },
    )

    async def _refuse(_row, _user, _db):
        raise HTTPException(status_code=403, detail="nope")

    monkeypatch.setattr(file_access, "authorize_file_access", _refuse)
    # The verifier loads the named rows, then resolves the restorer's id.
    session = _FakeSession([_Result(rows=[picture]), _Result(one=restorer)])
    with pytest.raises(PresentationSpecError, match="is not a platform file you can read"):
        asyncio.run(restore_presentation_version(session, deck, old, user_id=restorer.id))
    assert deck.revision == 3 and deck.title == "Now", "nothing was written"

    async def _allow(_row, _user, _db):
        return None

    monkeypatch.setattr(file_access, "authorize_file_access", _allow)
    session = _FakeSession([_Result(rows=[picture]), _Result(one=restorer)])
    asyncio.run(restore_presentation_version(session, deck, old, user_id=restorer.id))
    assert deck.revision == 4 and deck.title == "Then"
    snapshot = next(r for r in session.added if isinstance(r, PresentationVersion))
    assert snapshot.restored_from_revision == 1

    with pytest.raises(ValueError, match="acting user"):
        asyncio.run(restore_presentation_version(session, deck, old, user_id=None))


# ── Built-in brand template seed ─────────────────────────────────────────────


class _SeedSession(_FakeSession):
    """Fake session that also records deletions."""

    def __init__(self, existing=None):
        super().__init__(results=[_Result(rows=list(existing or []))])
        self.deleted: list = []

    async def delete(self, obj):
        self.deleted.append(obj)


def _seed(session, mode: str = "xtm_one"):
    from app.services.template_container_seed import (
        seed_builtin_template_containers,
    )

    return asyncio.run(seed_builtin_template_containers(session, platform_mode=mode))


_DECK_NAMES = {
    "filigran-brand": "Filigran Brand Deck",
    "thread-brand": "THREAD Conference Deck",
    "xtm-one-brand": "XTM One Brand Deck",
}


#: The built-in VIDEO templates the same seed ships beside the decks,
#: by the modes that carry them: the Filigran and THREAD ones in Copilot mode
#: like their decks, the XTM One one everywhere.
_VIDEO_NAMES = {
    "filigran-video": "Filigran Product Video",
    "thread-video": "THREAD Conference Video",
    "xtm-one-video": "XTM One Product Video",
    "filigran-brand-film": "Filigran Brand Film",
    "filigran-event-loop": "Filigran Event Loop",
    "xtm-one-brand-film": "XTM One Brand Film",
    "threat-briefing-video": "Threat Briefing",
    "social-teaser-video": "Social Teaser",
}
#: The video templates every platform ships: the Filigran ones are Copilot's.
_GLOBAL_VIDEO_SLUGS = {"xtm-one-video", "xtm-one-brand-film", "threat-briefing-video", "social-teaser-video"}
_VIDEO_SLUGS_BY_MODE = {
    "copilot": set(_VIDEO_NAMES),
    "xtm_one": _GLOBAL_VIDEO_SLUGS,
    "dev": _GLOBAL_VIDEO_SLUGS,
}


def _video_rows(mode: str) -> list:
    """The video templates a previous boot of *mode* left, as seeded rows."""
    rows = [_seeded_row(slug) for slug in sorted(_VIDEO_SLUGS_BY_MODE[mode])]
    for row in rows:
        row.kind = "video"
    return rows


def _seed_every_deck() -> list:
    """Every declared brand deck: Copilot mode ships them all."""
    session = _SeedSession()
    _seed(session, "copilot")
    return session.added


def _seeded_row(slug: str = "xtm-one-brand", tags=None):
    from app.services.template_container_seed import BUILTIN_TAG

    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="stale name",
        description=None,
        kind="presentation",
        tags=tags if tags is not None else [BUILTIN_TAG, f"slug:{slug}"],
        theme=None,
        theme_config={},
        aspect_ratio=None,
        layouts=[],
        extra_metadata={},
        is_company_managed=True,
    )


@pytest.fixture(autouse=False)
def _owner(monkeypatch, video_studio_enabled):
    # The seed ships the video templates beside the decks only while the Video
    # Studio is on; these tests describe that deployment.
    import app.api.v1.helpers as helpers

    owner_id = uuid.uuid4()

    async def _resolve(_db):
        return owner_id

    monkeypatch.setattr(helpers, "resolve_company_managed_owner_id", _resolve)
    return owner_id


def _by_slug(rows) -> dict[str, object]:
    return {tag[len("slug:") :]: row for row in rows for tag in row.tags if tag.startswith("slug:")}


#: The three logo LIBRARIES are retired: their assets live in
#: the Filigran Brand Deck, and an install that still has the library rows
#: moves them there - or, in a mode that does not ship that deck, drops them.
_RETIRED_LIBRARY_SLUGS = {
    "filigran-partner-logos",
    "filigran-integration-logos",
    "filigran-customer-logos",
}


def _decks(rows):
    return [row for row in rows if row.kind == "presentation"]


@pytest.mark.parametrize(
    ("mode", "slugs"),
    [
        ("copilot", {"filigran-brand", "thread-brand", "xtm-one-brand"}),
        ("xtm_one", {"xtm-one-brand"}),
        ("dev", {"xtm-one-brand"}),
    ],
)
def test_each_mode_seeds_its_brand_decks_company_managed(_owner, mode, slugs):
    """The XTM One deck ships in every mode, the one working example every
    platform has; the Filigran deck in Copilot mode only, since it
    carries Filigran's own customer and partner logos and team photos, and the
    THREAD deck with it, which includes those same logos. Nothing
    else: a template is a template, the logo packs are assets of the
    brand deck, not containers of their own."""
    session = _SeedSession()
    videos = _VIDEO_SLUGS_BY_MODE[mode]
    assert _seed(session, mode) == len(slugs) + len(videos)
    rows = _by_slug(session.added)
    assert set(rows) == slugs | videos
    for slug in slugs:
        assert rows[slug].name == _DECK_NAMES[slug]
    for slug in videos:
        assert rows[slug].name == _VIDEO_NAMES[slug]
        assert rows[slug].kind == "video"
        assert rows[slug].layouts == [], "a video template advertises no slide layout"
    for row in session.added:
        assert row.is_company_managed is True
        assert row.user_id == _owner
        assert "builtin" in row.tags
    for row in _decks(session.added):
        assert row.layouts, "every seeded deck template is a deck design with a layout catalog"
    assert set(_by_slug(_decks(session.added))) == slugs
    assert session.committed


def test_seeded_decks_declare_the_schemes_they_support(_owner):
    """A template SAYS which colour schemes it supports; the luminance
    of its palettes is only the fallback for a row that predates the
    declaration. The seed declares, and what it declares is true of its
    palettes, so the declaration can never promise a scheme the deck would
    render badly."""
    from app.services.presentations import (
        declared_supported_schemes,
        derived_supported_schemes,
        template_supported_schemes,
    )

    rows = _by_slug(_seed_every_deck())
    assert declared_supported_schemes(rows["filigran-brand"].extra_metadata) == ["dark", "light"]
    assert declared_supported_schemes(rows["thread-brand"].extra_metadata) == ["dark", "light"]
    assert declared_supported_schemes(rows["xtm-one-brand"].extra_metadata) == ["dark"]
    for row in rows.values():
        declared = declared_supported_schemes(row.extra_metadata)
        assert declared == derived_supported_schemes(row.theme_config, row.extra_metadata)
        assert template_supported_schemes(row.theme_config, row.extra_metadata) == declared


def test_seeded_theme_config_survives_the_render_whitelist(_owner):
    """A brand value the renderer would drop is a silently ineffective seed."""
    from app.services.presentations import (
        FEEDBACK_TEXT_THEME_CONFIG_KEYS,
        THEME_CONFIG_KEYS,
        sanitize_theme_config,
    )

    for row in _decks(_seed_every_deck()):
        assert sanitize_theme_config(row.theme_config) == row.theme_config
        # The brand palette is complete: every renderer variable is set, so a
        # deck styled by the seed never falls back to the renderer's own theme
        # for one stray colour (the logo file ids come from assets by role).
        # A tone as text falls back to the palette's own tone, never to
        # the renderer's: a palette sets it only where the tone fails as text.
        missing = {
            key
            for key in THEME_CONFIG_KEYS
            if not key.endswith("_file_id") and key not in FEEDBACK_TEXT_THEME_CONFIG_KEYS
        } - set(row.theme_config)
        assert not missing, f"{row.name} leaves {sorted(missing)} unset"


@pytest.mark.parametrize("slug", ["filigran-brand", "thread-brand"])
def test_seeded_brand_decks_ship_a_complete_light_variant(_owner, slug):
    """A ``light`` deck on a brand template must come out light in every
    colour, not dark with one or two swapped values — so the variant covers the
    whole colour set, survives the whitelist as written, and leaves the fonts to
    the base (they are the brand in both schemes). The THREAD deck ships one
    too."""
    from app.services.presentations import (
        FEEDBACK_TEXT_THEME_CONFIG_KEYS,
        THEME_CONFIG_KEYS,
        template_theme_variant,
    )
    from app.services.template_containers import validate_theme_variants

    session = _SeedSession()
    _seed(session, "copilot")
    brand = next(row for row in session.added if f"slug:{slug}" in row.tags)
    light = template_theme_variant(brand.extra_metadata, "light")
    assert light, f"the {slug} seed lost its light variant"
    # A tone as text is optional: it falls back to the variant's tone.
    colour_keys = {
        key
        for key in THEME_CONFIG_KEYS
        if not key.endswith("_file_id")
        and not key.endswith("_font")
        and key not in FEEDBACK_TEXT_THEME_CONFIG_KEYS
    }
    assert colour_keys <= set(light), sorted(colour_keys - set(light))
    assert not any(key.endswith("_font") for key in light)
    # Written exactly as the validator would accept it: nothing is dropped.
    accepted, problems = validate_theme_variants(brand.extra_metadata, kind=brand.kind)
    assert problems == []
    assert accepted["theme_variants"]["light"] == brand.extra_metadata["theme_variants"]["light"]
    # And it is genuinely the other scheme: light page, dark text.
    assert light["background"].lower() != brand.theme_config["background"].lower()
    assert light["foreground"].lower() != brand.theme_config["foreground"].lower()


def test_seeded_layouts_are_derived_from_the_validator(_owner):
    """The advertised catalog cannot drift from what validate_spec accepts."""
    from app.services.presentation_spec import layout_catalog

    catalog = layout_catalog()
    decks = _decks(_seed_every_deck())
    assert len(decks) == len(_DECK_NAMES)
    for row in decks:
        assert [entry["id"] for entry in row.layouts] == list(catalog)
        design = (row.extra_metadata or {}).get("design")
        plain_agenda = isinstance(design, dict) and design.get("agenda") == "plain"
        for entry in row.layouts:
            expected = catalog[entry["id"]]
            if plain_agenda and entry["id"] == "agenda":
                # The plain agenda advertises no highlighted item.
                expected = [field for field in expected if field != "current"]
            assert entry["fields"] == expected


def _contrast(a: str, b: str) -> float:
    def luminance(hex_colour: str) -> float:
        channels = [int(hex_colour.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _is_neutral(colour: str) -> bool:
    """A grey: one of the steps between Black Anthracite and Alabaster."""
    red, green, blue = (colour.lstrip("#")[i : i + 2] for i in (0, 2, 4))
    return red == green == blue


def test_the_thread_deck_opens_on_the_cascade_and_reads_on_paper(_owner):
    """The THREAD covers are the marketing kit's cascade. Every
    colour of both schemes is a colour of the kit's Color Set, a neutral step
    between its Black Anthracite and Alabaster, or one of the two light shades
    the set has no colour for - and the text still reads: the ink, the muted
    text and the accent at 4.5:1 on the page and the cards, every tone and the
    gradient at 3:1 on the page (slide-size text)."""
    from app.services.template_container_seed import _THREAD_LIGHT_SHADES, THREAD_COLOR_SET

    thread = _by_slug(_seed_every_deck())["thread-brand"]
    defaults = thread.extra_metadata["deck_defaults"]
    assert defaults["hero_defaults"] == {"title": "cascade", "closing": "cascade"}
    assert defaults["theme"] == "dark"
    colour_set = set(THREAD_COLOR_SET.values())
    dark = {key: value for key, value in thread.theme_config.items() if not key.endswith("_font")}
    light = thread.extra_metadata["theme_variants"]["light"]
    for scheme, palette in (("dark", dark), ("light", light)):
        for key, value in palette.items():
            shade = scheme == "light" and _THREAD_LIGHT_SHADES.get(key) == value
            assert value in colour_set or _is_neutral(value) or shade, (scheme, key, value)
        for key in ("accent", "gradient_from", "gradient_to", "info", "error", *(f"chart_{n}" for n in range(1, 7))):
            assert palette[key] in colour_set, (scheme, key, palette[key])
    assert set(_THREAD_LIGHT_SHADES) == {"success", "warning"}
    assert light["accent"] == THREAD_COLOR_SET["cobalt_blue"] and dark["accent"] == THREAD_COLOR_SET["cyan"]
    # The mosaic draws the same six colours of the set in both schemes.
    assert {light[f"chart_{n}"] for n in range(1, 7)} == {dark[f"chart_{n}"] for n in range(1, 7)}
    for scheme, palette in (("dark", dark), ("light", light)):
        for key in ("foreground", "muted", "accent"):
            for paper in ("background", "surface"):
                assert _contrast(palette[key], palette[paper]) >= 4.5, (scheme, key, paper)
        for key in ("info", "success", "warning", "error", "gradient_from", "gradient_to"):
            assert _contrast(palette[key], palette["background"]) >= 3, (scheme, key, palette[key])


def test_only_the_thread_deck_offers_the_tiled_covers(_owner):
    """The pixel-tile covers are the THREAD marketing kit's artwork. The
    THREAD deck declares them as its own; the Filigran and XTM One brand decks
    offer the shared covers only and open and close on the gradient cover."""
    from app.services.presentation_spec import SHARED_HEROES, TEMPLATE_HEROES
    from app.services.presentations import template_deck_defaults, template_heroes
    from app.services.template_containers import validate_template_heroes

    by_slug = _by_slug(_seed_every_deck())
    thread = by_slug["thread-brand"]
    assert template_heroes(thread.extra_metadata) == SHARED_HEROES + TEMPLATE_HEROES
    assert validate_template_heroes(thread.extra_metadata, kind=thread.kind) == (thread.extra_metadata, [])
    for slug in ("filigran-brand", "xtm-one-brand"):
        deck = by_slug[slug]
        assert template_heroes(deck.extra_metadata) == SHARED_HEROES, slug
        assert template_deck_defaults(deck.extra_metadata)["hero_defaults"] == {
            "title": "gradient",
            "closing": "gradient",
        }, slug


def test_seeded_deck_defaults_are_valid_spec_values(_owner):
    """`deck_defaults` is what fills a deck the agent left unstyled."""
    from app.services.presentation_spec import (
        PRESENTATION_ASPECT_RATIOS,
        PRESENTATION_THEMES,
        PRESENTATION_TRANSITIONS,
    )
    from app.services.presentations import template_deck_defaults, template_supported_schemes

    rows = _seed_every_deck()
    for row in _decks(rows):
        defaults = template_deck_defaults(row.extra_metadata)
        assert defaults["theme"] in PRESENTATION_THEMES
        assert defaults["aspect_ratio"] in PRESENTATION_ASPECT_RATIOS
        assert defaults["transition"] in PRESENTATION_TRANSITIONS
        # A theme is a colour scheme: the brand lives in the template's
        # palette, and the default theme is one the template can dress.
        assert defaults["theme"] in template_supported_schemes(row.theme_config, row.extra_metadata)
    by_slug = _by_slug(rows)
    # The Filigran deck ships a light variant, so it dresses both schemes and
    # opens dark; the XTM One deck is dark only.
    filigran = by_slug["filigran-brand"]
    assert filigran.extra_metadata["deck_defaults"]["theme"] == "dark"
    assert template_supported_schemes(filigran.theme_config, filigran.extra_metadata) == ["dark", "light"]
    xtm_one = by_slug["xtm-one-brand"]
    assert xtm_one.extra_metadata["deck_defaults"]["theme"] == "dark"
    assert template_supported_schemes(xtm_one.theme_config, xtm_one.extra_metadata) == ["dark"]


def test_seeded_guidance_is_a_non_empty_composition_brief(_owner):
    """``extra_metadata.guidance`` is what the agent reads via get_template."""
    rows = _seed_every_deck()
    for row in rows:
        guidance = row.extra_metadata.get("guidance")
        assert isinstance(guidance, str) and len(guidance) > 200
    for row in _decks(rows):
        guidance = row.extra_metadata["guidance"]
        assert "content" in guidance and "blocks" in guidance
        # Every brief sends the agent to the predefined assets
        # before it draws an approximation of a figure that already exists.
        # Only the Filigran briefs name the logo sets (by role): the
        # XTM One deck ships on every platform with the product figures alone,
        # so its brief names none of the Copilot-only families.
        filigran = "filigran" in (row.tags or [])
        assert "PREDEFINED" in guidance
        for role in ("partner_logo", "integration_logo", "customer_logo"):
            assert (role in guidance) is filigran, f"{row.name} guidance and the {role} set"
        assert "asset-library" not in guidance
        # Sales collateral is reproducible from the brand template: the brief
        # points a product brief / one-pager at the portrait pages and the
        # collateral blocks instead of leaving it to a 16:9 slide arc.
        for token in ("A4", "letter", "pricing_tiers", "personas", "key_value"):
            assert token in guidance, f"{row.name} guidance lacks {token}"


def test_reseeding_syncs_in_place_without_inserting(_owner):
    existing = [_seeded_row("filigran-brand"), _seeded_row("xtm-one-brand"), _seeded_row("thread-brand")]
    # An older deployment carries the retired search labels.
    existing[0].theme = "filigran"
    existing[1].theme = "xtm-one"
    session = _SeedSession(existing=[*existing, *_video_rows("copilot")])
    assert _seed(session, "copilot") == 0
    assert session.added == []
    assert session.deleted == []
    # Full sync: brand changes reach an existing deployment - including the
    # removal of the legacy label, so no built-in template page ever shows a
    # "legacy" search label the admin cannot clear.
    assert existing[0].name == "Filigran Brand Deck"
    assert existing[0].theme is None
    assert existing[1].name == "XTM One Brand Deck"
    assert existing[1].theme is None
    assert existing[2].name == "THREAD Conference Deck"
    for row in existing:
        assert row.aspect_ratio == "16:9"
        assert row.layouts
        assert row.theme_config["background"]
        # The scheme declaration reaches an existing deployment too.
        assert row.extra_metadata["supported_schemes"]


def _library_rows():
    rows = [_seeded_row(slug) for slug in sorted(_RETIRED_LIBRARY_SLUGS)]
    for row in rows:
        row.kind = "asset-library"
        row.layouts, row.theme_config = [], {}
    return rows


def _rehome_updates(session):
    """The ``UPDATE file_attachments`` statements the seed issued, as SQL."""
    return [
        text
        for text in (str(statement) for statement in session.statements)
        if text.startswith("UPDATE file_attachments") and "template_container_id" in text
    ]


def test_retired_logo_libraries_are_re_homed_into_the_brand_deck(_owner, monkeypatch):
    """An older Copilot install has the three logo libraries.
    Their asset rows MOVE to the Filigran Brand Deck (same ids - a deck's
    logo_grid references them) and the emptied containers go; nothing is purged
    from the object store, because nothing was orphaned."""
    from app.services import template_container_seed as seed_mod

    purged: list = []

    async def _purge(_db, container):
        purged.append(container)
        return []

    monkeypatch.setattr(seed_mod, "_delete_container_assets", _purge)
    decks = [_seeded_row(slug) for slug in _DECK_NAMES]
    libraries = _library_rows()
    session = _SeedSession(existing=[*decks, *_video_rows("copilot"), *libraries])
    assert _seed(session, "copilot") == 0
    assert session.added == []
    assert session.deleted == libraries
    assert purged == [], "moved assets are not orphans; their objects stay"
    updates = _rehome_updates(session)
    assert len(updates) >= len(libraries), "one re-bind per retired library at least"
    assert all("source" in text for text in updates), "only template assets move"
    assert session.committed


def test_a_retired_library_is_re_homed_when_the_heir_is_created_in_the_same_boot(_owner, monkeypatch):
    """The heir may not exist yet (a brand deck an admin deleted by hand and
    the seed re-creates): the move waits for the insert, never drops the rows."""
    from app.services import template_container_seed as seed_mod

    purged: list = []

    async def _purge(_db, container):
        purged.append(container)
        return []

    monkeypatch.setattr(seed_mod, "_delete_container_assets", _purge)
    libraries = _library_rows()
    session = _SeedSession(existing=libraries)
    assert _seed(session, "copilot") == len(_DECK_NAMES) + len(_VIDEO_NAMES)
    assert set(_by_slug(session.added)) == set(_DECK_NAMES) | set(_VIDEO_NAMES)
    assert session.deleted == libraries
    assert purged == []
    assert len(_rehome_updates(session)) >= len(libraries)


def test_admin_created_container_is_never_touched(_owner):
    """No slug tag and no builtin tag = not ours; never synced, never deleted."""
    admin_row = _seeded_row(tags=["brand", "mine"])
    admin_row.name = "My own deck"
    session = _SeedSession(existing=[admin_row])
    _seed(session)
    assert session.deleted == []
    assert admin_row.name == "My own deck"


def test_stale_builtin_container_is_removed(_owner, monkeypatch):
    from app.services import template_container_seed as seed_mod

    purged: list = []

    async def _purge(_db, container):
        purged.append(container)
        return []

    monkeypatch.setattr(seed_mod, "_delete_container_assets", _purge)
    stale = _seeded_row(slug="retired-brand")
    session = _SeedSession(existing=[stale])
    _seed(session)
    assert session.deleted == [stale]
    # Its binaries go with it: the S3 objects of a retired built-in must not
    # leak, and the seed is the only thing that knows the row is gone.
    assert purged == [stale]


def test_copilot_mode_keeps_the_xtm_one_brand_deck(monkeypatch, _owner):
    """The XTM One deck is the working example every platform has: a
    Copilot deployment keeps it next to the Filigran deck, and only the retired
    libraries go - re-homed into the Filigran deck first (Phase 3), so nothing
    is purged."""
    from app.services import template_container_seed as seed_mod

    purged: list = []

    async def _purge(_db, container):
        purged.append(container)
        return []

    monkeypatch.setattr(seed_mod, "_delete_container_assets", _purge)
    xtm_one = _seeded_row("xtm-one-brand")
    filigran = _seeded_row("filigran-brand")
    thread = _seeded_row("thread-brand")
    libraries = _library_rows()
    session = _SeedSession(existing=[xtm_one, filigran, thread, *_video_rows("copilot"), *libraries])
    assert _seed(session, "copilot") == 0
    assert session.added == []
    assert session.deleted == libraries
    assert purged == []
    assert xtm_one.name == "XTM One Brand Deck"
    assert filigran.name == "Filigran Brand Deck"
    assert thread.name == "THREAD Conference Deck"


@pytest.mark.parametrize("mode", ["xtm_one", "dev"])
def test_the_xtm_one_modes_remove_the_filigran_brand_deck(monkeypatch, _owner, mode):
    """An XTM One platform seeded before the fix carries the Filigran
    deck (and, older still, its three logo libraries). The next start removes
    all of them in Phase 1, each with every asset it holds - the libraries'
    heir is not shipped here, so they take the plain delete rather than the
    re-home - and syncs the product deck in place."""
    from app.services import template_container_seed as seed_mod

    purged: list = []

    async def _purge(_db, container):
        purged.append(container)
        return []

    monkeypatch.setattr(seed_mod, "_delete_container_assets", _purge)
    xtm_one = _seeded_row("xtm-one-brand")
    filigran = _seeded_row("filigran-brand")
    libraries = _library_rows()
    session = _SeedSession(existing=[xtm_one, filigran, *_video_rows(mode), *libraries])
    assert _seed(session, mode) == 0
    assert session.added == []
    assert session.deleted == [filigran, *libraries]
    assert purged == [filigran, *libraries], "every removal goes through the asset purge"
    assert _rehome_updates(session) == [], "nothing is re-homed into a deck this mode does not ship"
    assert xtm_one.name == "XTM One Brand Deck"


def test_platform_targets_use_the_shared_seed_matcher():
    """One spelling of "which modes ships this" for skills AND containers."""
    import inspect

    from app.agents.seed import seed_matches_platform
    from app.services import template_container_seed as seed_mod

    assert "seed_matches_platform" in inspect.getsource(seed_mod.builtin_containers_for)
    filigran = next(c for c in seed_mod.BUILTIN_TEMPLATE_CONTAINERS if c["slug"] == "filigran-brand")
    xtm_one = next(c for c in seed_mod.BUILTIN_TEMPLATE_CONTAINERS if c["slug"] == "xtm-one-brand")
    assert seed_matches_platform(filigran, "copilot")
    assert not seed_matches_platform(filigran, "xtm_one")
    assert not seed_matches_platform(filigran, "dev")
    for mode in ("copilot", "xtm_one", "dev"):
        assert seed_matches_platform(xtm_one, mode)


def test_startup_wires_the_container_seed():
    """Boot still seeds the containers, one level down.

    The seed sequence moved into ``services/provisioning.py`` so the runtime AI
    toggle can create the same content; boot calls it, so assert both
    halves rather than the old single source match.
    """
    import inspect

    from app import main
    from app.services import provisioning

    assert "provision_builtin_content()" in inspect.getsource(main)
    assert "seed_builtin_template_containers" in inspect.getsource(provisioning)


def test_xtm_one_entry_point_carries_the_deck_tools_the_general_assistant_ships_with():
    """In XTM One mode the CTEM Assistant IS the General Assistant.

    ``provision_builtin_content`` links it as the GA and makes it every user's
    default chat agent, so it is the agent a person types "create a
    presentation about X" into. A Copilot install answers that with the GA's
    seeded tools (``DEFAULT_GA_BUILTIN_TOOL_IDS``); an XTM One install whose
    entry point lacks them has NO tool to answer with, and the seeded brand
    templates are unreachable from the default chat. The seed sync
    overwrites ``builtin_tool_ids`` on every boot, so a regression here would
    silently strip the capability from every deployment.

    The platform builder included: "create an agent that ..." typed
    into the default chat is answered there, not only after a transfer.
    """
    import inspect

    from app.agents.builtin_agents import BUILTIN_AGENTS
    from app.models.general_assistant_config import DEFAULT_GA_BUILTIN_TOOL_IDS
    from app.services import provisioning

    # The premise: provisioning really does make the CTEM Assistant the GA.
    source = inspect.getsource(provisioning)
    assert 'slug == "ctem-assistant"' in source
    assert "ga.linked_agent_id = ctem_id" in source

    ctem = next(a for a in BUILTIN_AGENTS if a["slug"] == "ctem-assistant")
    missing = [t for t in DEFAULT_GA_BUILTIN_TOOL_IDS if t not in ctem["builtin_tool_ids"]]
    assert not missing, f"ctem-assistant lacks the GA's default tools: {missing!r}"
    assert "agent_builder" in ctem["builtin_tool_ids"]
    assert "generate_presentation" in ctem["builtin_tool_ids"]
    assert "template_search" in ctem["builtin_tool_ids"]
