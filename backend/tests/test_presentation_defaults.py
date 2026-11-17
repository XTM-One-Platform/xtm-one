"""Default brand template + ``template_id`` semantics for generated decks.

The template a deck is styled with when the agent names none is resolved
server-side (admin setting -> seeded brand -> none), the ``template_id``
argument has exactly three accepted shapes (UUID / opt-out sentinel / omitted,
with "omitted" meaning something different on an edit than on a create), and a
template's ``deck_defaults`` only ever fill what the agent left unset.

These tests pin that contract, the SQL the seeded fallback runs (``tags`` is a
plain JSON column - PostgreSQL has no ``LIKE`` for ``json``, so the lookup must
test the TEXT form; the original query only failed once it reached a real
database), the admin-side validation of the default, and the deployment /
documentation wiring of the feature. No DB required: same fake-session style as
``test_presentations.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
FRONTEND_ROOT = REPO_ROOT / "frontend"
DOCS_ROOT = REPO_ROOT / "docs"


# -- Fakes -------------------------------------------------------------------


class _Result:
    def __init__(self, one=None, rows=None):
        self._one = one
        self._rows = rows or []

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else self._one

    def scalar_one_or_none(self):
        return self._one


class _FakeSession:
    """Async-context session capturing added rows and executed statements."""

    def __init__(self, results=None):
        self._results = list(results or [])
        self.statements: list = []
        self.added: list = []
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


def _session_factory(session: _FakeSession):
    def _factory():
        return session

    return _factory


def _compiled(query) -> str:
    return str(
        query.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def _slides(count: int = 1) -> list[dict]:
    return [{"layout": "title", "title": f"Slide {i}", "subtitle": "sub"} for i in range(count)]


def _build_tools(
    user_id: str | None = None,
    *,
    conversation_id: str | None = None,
    assignment_run_id: str | None = None,
    request_text: str | None = None,
):
    """Tools for an automation by default (no conversation, nobody to ask);
    pass ``conversation_id`` for the interactive chat contract and
    ``request_text`` for the words the user actually wrote."""
    from app.services.builtin_tools import build_presentation_tools

    return build_presentation_tools(
        db=None,  # type: ignore[arg-type]
        user_id=user_id or str(uuid.uuid4()),
        conversation_id=conversation_id,
        assignment_run_id=assignment_run_id,
        request_text=request_text,
    )


def _run_tool(executor, name: str, arguments: dict):
    from app.tools.base import ToolCall

    return asyncio.run(executor.execute_tool(ToolCall(id="call-1", name=name, arguments=arguments)))


def _existing_row(user_id: uuid.UUID, **columns):
    from app.models.presentation import Presentation

    spec = {"title": "Deck", "slides": _slides(2)}
    spec.update(columns.pop("spec_extra", {}))
    return Presentation(
        id=uuid.uuid4(),
        user_id=user_id,
        title="Deck",
        theme=spec.get("theme", "dark"),
        slide_count=2,
        revision=1,
        spec=spec,
        **columns,
    )


def _deck_lookup(existing) -> list[_Result]:
    """The two results a visibility-aware deck lookup consumes: the caller's
    group memberships, then the deck row itself."""
    return [_Result(rows=[]), _Result(one=existing)]


def _settings_row(configured: uuid.UUID | None):
    return SimpleNamespace(default_presentation_template_id=configured)


#: A dark brand palette: ``template_supported_schemes`` reads ``dark`` off the
#: background, and with no ``theme_variants.light`` the template is dark-only.
DARK_PALETTE = {"background": "#0a0f1c", "accent": "#00b4ff", "text": "#ffffff"}
#: The light counterpart a two-scheme template ships under ``theme_variants``.
LIGHT_VARIANT = {"background": "#ffffff", "text": "#111111", "surface": "#f4f6fa"}


def _template_row(
    template_id: uuid.UUID,
    *,
    name: str = "Brand Deck",
    kind: str = "presentation",
    theme_config: dict | None = None,
    deck_defaults: dict | None = None,
    theme_variants: dict | None = None,
):
    """A ``TemplateContainer`` row as ``load_visible_template_facts`` reads it."""
    extra: dict = {}
    if deck_defaults is not None:
        extra["deck_defaults"] = deck_defaults
    if theme_variants is not None:
        extra["theme_variants"] = theme_variants
    return SimpleNamespace(
        id=template_id,
        name=name,
        kind=kind,
        theme_config=DARK_PALETTE if theme_config is None else theme_config,
        extra_metadata=extra,
    )


def _template_lookup(row) -> list[_Result]:
    """The two results a visibility-aware template load consumes: the caller's
    group memberships, then the template row as that caller may see it (``None``
    when it does not exist OR is not visible to them)."""
    return [_Result(rows=[]), _Result(one=row)]


# -- resolve_deck_template_id: the three accepted shapes ---------------------


def test_a_uuid_names_the_template_without_touching_the_db():
    from app.services.presentations import resolve_deck_template_id

    session = _FakeSession()
    template_id = uuid.uuid4()
    resolved = asyncio.run(
        resolve_deck_template_id(
            session,  # type: ignore[arg-type]
            requested=str(template_id),
            existing_template_id=None,
            is_edit=False,
        )
    )
    assert resolved == (template_id, False)
    assert session.statements == []


@pytest.mark.parametrize("sentinel", ["none", "NONE", " None "])
def test_an_opt_out_sentinel_means_no_template_and_flags_the_unlink(sentinel):
    from app.services.presentations import TEMPLATE_NONE, resolve_deck_template_id

    assert sentinel.strip().lower() == TEMPLATE_NONE
    session = _FakeSession()
    resolved = asyncio.run(
        resolve_deck_template_id(
            session,  # type: ignore[arg-type]
            requested=sentinel,
            existing_template_id=uuid.uuid4(),
            is_edit=True,
        )
    )
    # ``explicit_none`` is what lets an edit UNLINK the template the deck
    # carries; a plain ``None`` would be indistinguishable from "omitted".
    assert resolved == (None, True)
    assert session.statements == []


@pytest.mark.parametrize("guess", ["null", "no", "false", "-"])
def test_only_the_declared_opt_out_unlinks_any_other_word_is_refused(guess):
    """The tools declare 'none' alone; another word is refused with the accepted shapes."""
    from app.services.presentations import TemplateChoiceError, resolve_deck_template_id

    session = _FakeSession()
    with pytest.raises(TemplateChoiceError, match="'none' for no template"):
        asyncio.run(
            resolve_deck_template_id(
                session,  # type: ignore[arg-type]
                requested=guess,
                existing_template_id=uuid.uuid4(),
                is_edit=True,
            )
        )
    assert session.statements == []


@pytest.mark.parametrize("omitted", [None, "", "   "])
def test_omitted_on_an_edit_keeps_the_current_template(omitted):
    """Re-defaulting a deck mid-revision would restyle it under the user."""
    from app.services.presentations import resolve_deck_template_id

    session = _FakeSession()
    current = uuid.uuid4()
    resolved = asyncio.run(
        resolve_deck_template_id(
            session,  # type: ignore[arg-type]
            requested=omitted,
            existing_template_id=current,
            is_edit=True,
        )
    )
    assert resolved == (current, False)
    # Nothing to look up: the answer is already on the row.
    assert session.statements == []


@pytest.mark.parametrize("omitted", [None, "", "   "])
def test_omitted_on_a_create_takes_the_platform_default(monkeypatch, omitted):
    from app.services import presentations

    default_id = uuid.uuid4()
    seen: list = []

    async def _fake_default(db):
        seen.append(db)
        return default_id

    monkeypatch.setattr(presentations, "resolve_default_template_id", _fake_default)
    session = _FakeSession()
    resolved = asyncio.run(
        presentations.resolve_deck_template_id(
            session,  # type: ignore[arg-type]
            requested=omitted,
            existing_template_id=None,
            is_edit=False,
        )
    )
    assert resolved == (default_id, False)
    assert seen == [session]


@pytest.mark.parametrize("garbage", ["nope", "filigran", "12345", "none please", 42])
def test_anything_else_is_a_precise_error_the_agent_can_act_on(garbage):
    from app.services.presentations import TemplateChoiceError, resolve_deck_template_id

    session = _FakeSession()
    with pytest.raises(TemplateChoiceError) as excinfo:
        asyncio.run(
            resolve_deck_template_id(
                session,  # type: ignore[arg-type]
                requested=garbage,
                existing_template_id=None,
                is_edit=False,
            )
        )
    message = str(excinfo.value)
    # The message spells out the three accepted shapes rather than just "invalid".
    assert "search_templates" in message
    assert "'none'" in message
    assert "omitted" in message
    assert session.statements == []
    # ValueError subclass, so a generic handler still classifies it as bad input.
    assert isinstance(excinfo.value, ValueError)


# -- resolve_default_template_id: setting -> seeded brand -> none ------------


def test_a_valid_admin_setting_wins():
    from app.services.presentations import resolve_default_template_id

    configured = uuid.uuid4()
    session = _FakeSession(results=[_Result(one=_settings_row(configured)), _Result(one=configured)])
    assert asyncio.run(resolve_default_template_id(session)) == configured  # type: ignore[arg-type]
    # The settings read, then ONE existence probe - no seeded lookups.
    assert len(session.statements) == 2
    probe = _compiled(session.statements[1])
    # The probe insists on a company-managed container: a private one would be
    # invisible to everyone but its owner, whose decks would come out unthemed.
    assert "is_company_managed IS true" in probe
    assert str(configured) in probe


def test_a_setting_pointing_at_a_gone_or_private_container_falls_back_to_the_brand():
    from app.services.presentations import DEFAULT_TEMPLATE_SLUGS, resolve_default_template_id

    configured = uuid.uuid4()
    brand = uuid.uuid4()
    session = _FakeSession(
        results=[
            _Result(one=_settings_row(configured)),
            _Result(one=None),  # existence probe: gone, or no longer company-managed
            _Result(one=brand),  # first seeded slug
        ]
    )
    assert asyncio.run(resolve_default_template_id(session)) == brand  # type: ignore[arg-type]
    assert len(session.statements) == 3
    assert f'"slug:{DEFAULT_TEMPLATE_SLUGS[0]}"' in _compiled(session.statements[2])


def test_without_a_setting_the_seeded_brands_are_tried_in_declared_order():
    from app.services.presentations import DEFAULT_TEMPLATE_SLUGS, resolve_default_template_id

    assert DEFAULT_TEMPLATE_SLUGS == ("filigran-brand", "xtm-one-brand")
    second = uuid.uuid4()
    session = _FakeSession(
        results=[
            _Result(one=None),  # no platform_settings row at all
            _Result(one=None),  # filigran-brand missing: any XTM One mode
            _Result(one=second),  # xtm-one-brand, the XTM One default
        ]
    )
    assert asyncio.run(resolve_default_template_id(session)) == second  # type: ignore[arg-type]
    slug_lookups = [_compiled(s) for s in session.statements[1:]]
    for sql, slug in zip(slug_lookups, DEFAULT_TEMPLATE_SLUGS, strict=True):
        assert f'"slug:{slug}"' in sql
        assert '"builtin"' in sql
        assert "is_company_managed IS true" in sql


def test_no_brand_at_all_means_the_renderer_theme():
    """The behaviour without a brand: a deck with no template renders with the
    built-in theme. Never an error - a deck must not fail because of a seed."""
    from app.services.presentations import resolve_default_template_id

    session = _FakeSession(results=[_Result(one=None), _Result(one=None), _Result(one=None)])
    assert asyncio.run(resolve_default_template_id(session)) is None  # type: ignore[arg-type]
    assert len(session.statements) == 3


# -- The seeded lookup must be SQL PostgreSQL accepts -------------------------


def test_builtin_container_filter_tests_the_text_form_of_the_json_tags():
    """``JSON.contains([...])`` on the plain JSON ``tags`` column compiles to
    ``tags LIKE '%' || '["x"]'::JSON || '%'`` - PostgreSQL has no ``LIKE`` for
    ``json`` and rejects the statement, so every deck generated without an
    explicit template failed on a real database while the fake-session tests
    passed. The lookup must cast to text and match the QUOTED token."""
    from sqlalchemy import select

    from app.models.template_container import TemplateContainer
    from app.services.template_container_seed import builtin_container_filter

    sql = _compiled(select(TemplateContainer.id).where(builtin_container_filter("filigran-brand")))
    assert "CAST(template_containers.tags AS TEXT) LIKE" in sql
    assert "::JSON" not in sql
    assert "'\"builtin\"'" in sql
    # Quoted token: ``slug:filigran-brand`` cannot match ``slug:filigran-brand-2``.
    assert "'\"slug:filigran-brand\"'" in sql
    assert "is_company_managed IS true" in sql


@pytest.mark.parametrize(
    "module_path",
    [
        "app/services/presentations.py",
        "app/services/template_asset_seed.py",
        "app/services/template_container_seed.py",
    ],
)
def test_no_template_module_calls_json_contains_on_tags(module_path):
    source = (BACKEND_ROOT / module_path).read_text(encoding="utf-8")
    assert "tags.contains(" not in source, (
        f"{module_path} tests JSON tag membership with .contains([...]), which "
        "PostgreSQL rejects on a json column; use builtin_container_filter()"
    )


def test_the_asset_seed_and_the_default_resolution_share_one_lookup():
    from app.services import presentations, template_asset_seed

    assert "builtin_container_filter" in inspect.getsource(presentations.resolve_default_template_id)
    assert "builtin_container_filter" in inspect.getsource(template_asset_seed._load_builtin_container)


# -- deck_defaults: fill only what the agent left unset -----------------------


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        (None, {}),
        ("not a dict", {}),
        ({}, {}),
        ({"deck_defaults": "filigran"}, {}),
        ({"deck_defaults": {"theme": " filigran "}}, {"theme": "filigran"}),
        (
            {"deck_defaults": {"theme": "filigran", "transition": "fade", "aspect_ratio": "16:9"}},
            {"theme": "filigran", "transition": "fade", "aspect_ratio": "16:9"},
        ),
        # Not whitelisted / not a string / blank: dropped, never forwarded.
        ({"deck_defaults": {"title": "Hijack", "theme": 3, "transition": "  "}}, {}),
    ],
)
def test_template_deck_defaults_are_whitelisted_strings(extra, expected):
    from app.services.presentations import DECK_DEFAULT_KEYS, template_deck_defaults

    assert DECK_DEFAULT_KEYS == ("theme", "transition", "aspect_ratio")
    assert template_deck_defaults(extra) == expected


def test_apply_deck_defaults_fills_only_absent_or_null_keys():
    from app.services.presentations import apply_deck_defaults

    payload = {"title": "D", "theme": "light", "transition": None, "aspect_ratio": ""}
    out = apply_deck_defaults(payload, {"theme": "filigran", "transition": "fade", "aspect_ratio": "4:3"})
    assert out is payload  # mutated in place, returned for call-site brevity
    assert payload["theme"] == "light"  # an explicit choice always wins
    assert payload["transition"] == "fade"  # null -> filled
    assert payload["aspect_ratio"] == "4:3"  # empty string -> filled


def test_seeded_deck_defaults_pass_through_the_whitelist():
    """Every shipped brand deck declares defaults the whitelist keeps whole -
    and every built-in presentation container IS a brand deck (the
    logo packs became its assets), so there is no seeded deck without defaults.
    The built-in video templates carry video rules instead."""
    from app.services.presentations import template_deck_defaults
    from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS

    decks = [c for c in BUILTIN_TEMPLATE_CONTAINERS if c["kind"] == "presentation"]
    assert {c["slug"] for c in decks} == {"filigran-brand", "thread-brand", "xtm-one-brand"}
    assert {c["kind"] for c in BUILTIN_TEMPLATE_CONTAINERS} == {"presentation", "video"}
    for container in decks:
        declared = (container.get("extra_metadata") or {}).get("deck_defaults") or {}
        assert declared, container["slug"]
        assert template_deck_defaults(container["extra_metadata"]) == declared


# -- Through the tool ---------------------------------------------------------


def test_a_new_deck_without_template_id_gets_the_default_and_its_deck_defaults(monkeypatch):
    import app.core.database as database

    configured = uuid.uuid4()
    session = _FakeSession(
        results=[
            _Result(one=_settings_row(configured)),  # platform settings
            _Result(one=configured),  # company-managed existence probe
            # The template row itself: defaults + the schemes it supports.
            *_template_lookup(_template_row(configured, deck_defaults={"theme": "dark", "transition": "fade"})),
        ]
    )
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools()
    result = _run_tool(executor, "generate_presentation", {"title": "D", "slides": _slides(2)})

    assert not result.is_error, result.content
    row = session.added[0]
    assert row.template_container_id == configured
    # The brand's defaults filled what the agent left unset...
    assert row.theme == "dark"
    assert row.spec["theme"] == "dark"
    assert row.spec["transition"] == "fade"
    assert session.committed
    # ...and the result names the template with the schemes it can dress,
    # so the agent knows a "make it light" would be refused here.
    assert "Template: Brand Deck (themes: dark only)" in result.content


def test_an_explicit_agent_choice_beats_the_template_defaults(monkeypatch):
    import app.core.database as database

    configured = uuid.uuid4()
    session = _FakeSession(
        results=[
            _Result(one=_settings_row(configured)),
            _Result(one=configured),
            *_template_lookup(
                _template_row(
                    configured,
                    deck_defaults={"theme": "dark", "transition": "fade", "aspect_ratio": "4:3"},
                    theme_variants={"light": LIGHT_VARIANT},
                )
            ),
        ]
    )
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools()
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D", "slides": _slides(1), "theme": "light", "transition": "none"},
    )

    assert not result.is_error, result.content
    row = session.added[0]
    assert row.theme == "light"
    assert row.spec["transition"] == "none"
    assert row.spec["aspect_ratio"] == "4:3"  # the one key the agent left unset
    assert "Template: Brand Deck (themes: dark / light)" in result.content


def test_a_theme_the_template_cannot_dress_is_snapped_and_the_snap_is_stated(monkeypatch):
    """The template wins over the theme: a light deck asked for under
    a dark-only brand is saved dark, and the result says so - the agent must
    never believe it delivered a light deck the brand cannot render."""
    import app.core.database as database

    configured = uuid.uuid4()
    session = _FakeSession(
        results=[
            _Result(one=_settings_row(configured)),
            _Result(one=configured),
            *_template_lookup(_template_row(configured, name="XTM One Brand Deck", deck_defaults={"theme": "dark"})),
        ]
    )
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools()
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D", "slides": _slides(1), "theme": "light"},
    )

    assert not result.is_error, result.content
    row = session.added[0]
    assert row.theme == "dark"
    assert row.spec["theme"] == "dark"
    assert "Theme: 'light' is not supported by the template 'XTM One Brand Deck' (dark)" in result.content
    assert "the deck is 'dark'" in result.content


def test_an_explicit_template_that_does_not_exist_is_refused(monkeypatch):
    """A UUID the database does not know is refused up front - before,
    it was linked as-is and only the FK failed, after the spec was accepted."""
    import app.core.database as database

    session = _FakeSession(results=_template_lookup(None))
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools()
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D", "slides": _slides(1), "template_id": str(uuid.uuid4())},
    )

    assert result.is_error
    assert "No template" in result.content
    assert "search_templates" in result.content
    assert session.added == []


def test_the_template_is_loaded_as_the_acting_user_may_see_it(monkeypatch):
    """A UUID the model holds is not proof the user may use the template: the
    tool resolves it through the same three-tier visibility as every read, so
    another user's private template reads as "not found" - its name, schemes
    and deck defaults never reach the tool result, and no deck is linked to it."""
    import app.core.database as database
    from app.models.template_container import TemplateContainer
    from app.services import presentations

    user_id = uuid.uuid4()
    private_template = uuid.uuid4()
    session = _FakeSession(results=_template_lookup(None))
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    seen: list = []
    real_loader = presentations._load_visible_template

    async def _spy(db, container_id, *, user_id, is_admin=False):
        seen.append((container_id, user_id, is_admin))
        return await real_loader(db, container_id, user_id=user_id, is_admin=is_admin)

    monkeypatch.setattr(presentations, "_load_visible_template", _spy)

    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D", "slides": _slides(1), "template_id": str(private_template)},
    )

    assert result.is_error
    assert "No template" in result.content
    assert session.added == []
    # The load went through the visibility-checked path, for THIS user, and an
    # agent tool never asks for the admin bypass the REST surface uses...
    assert seen == [(private_template, user_id, False)]
    # ...and the row query carries the three-tier filter, not a bare id match.
    row_query = _compiled(session.statements[-1])
    assert str(TemplateContainer.__table__.name) in row_query
    assert "is_company_managed" in row_query
    assert str(private_template) in row_query


def test_an_asset_library_cannot_dress_a_deck(monkeypatch):
    """A logo pack is a template container too, but not a presentation
    template: linking it would style nothing and hide the deck's real brand."""
    import app.core.database as database

    library = uuid.uuid4()
    session = _FakeSession(
        results=_template_lookup(_template_row(library, name="Partner Logos", kind="asset-library", theme_config={}))
    )
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools()
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D", "slides": _slides(1), "template_id": str(library)},
    )

    assert result.is_error
    assert "'Partner Logos' is not a presentation template (kind: asset-library)" in result.content
    assert session.added == []


def test_a_template_default_that_is_not_a_valid_spec_value_is_refused_not_smuggled(monkeypatch):
    """Deck defaults are validated like any agent value: a template cannot
    inject a theme the renderer has no stylesheet for. The scheme snap does
    not launder it either: an unknown theme name is not a scheme, so it is
    left for the validator to refuse by name."""
    import app.core.database as database

    configured = uuid.uuid4()
    session = _FakeSession(
        results=[
            _Result(one=_settings_row(configured)),
            _Result(one=configured),
            *_template_lookup(_template_row(configured, theme_config={}, deck_defaults={"theme": "neon-hacker"})),
        ]
    )
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools()
    result = _run_tool(executor, "generate_presentation", {"title": "D", "slides": _slides(1)})

    assert result.is_error
    assert "theme" in result.content
    assert session.added == []


def test_template_id_none_creates_an_unstyled_deck_without_any_lookup(monkeypatch):
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools()
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D", "slides": _slides(1), "template_id": "none"},
    )

    assert not result.is_error, result.content
    row = session.added[0]
    assert row.template_container_id is None
    assert row.theme == "dark"  # the spec's own default, no brand applied
    # Neither the platform default nor any deck defaults were looked up. The
    # statements the save DOES issue are the version snapshot's own bookkeeping
    # (`presentation_versions`: idempotency probe + retention), not a lookup.
    issued = [_compiled(statement) for statement in session.statements]
    assert not [q for q in issued if "platform_settings" in q or "template_containers" in q], issued
    assert all("presentation_versions" in q for q in issued), issued


def test_editing_with_template_id_none_unlinks_the_template(monkeypatch):
    import app.core.database as database

    user_id = uuid.uuid4()
    existing = _existing_row(user_id, template_container_id=uuid.uuid4())
    session = _FakeSession(results=_deck_lookup(existing))
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D", "slides": _slides(1), "presentation_id": str(existing.id), "template_id": "none"},
    )

    assert not result.is_error, result.content
    # An edit rewrites the SAME row: the only thing added is the history
    # snapshot of the revision it just wrote, never a second deck.
    from app.models.presentation import Presentation, PresentationVersion

    assert not [row for row in session.added if isinstance(row, Presentation)]
    assert [type(row) for row in session.added] == [PresentationVersion]
    assert existing.template_container_id is None
    assert existing.revision == 2


def test_editing_without_template_id_keeps_the_link_and_the_stored_look(monkeypatch):
    """An edit that omits theme / transition / aspect ratio / chrome keeps what
    the deck already has - the defaults are not applied a second time."""
    import app.core.database as database

    user_id = uuid.uuid4()
    template_id = uuid.uuid4()
    existing = _existing_row(
        user_id,
        template_container_id=template_id,
        spec_extra={
            "theme": "light",
            "transition": "fade",
            "aspect_ratio": "4:3",
            "chrome": {"show_slide_numbers": False, "footer_text": "Confidential"},
        },
    )
    session = _FakeSession(results=_deck_lookup(existing))
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D v2", "slides": _slides(3), "presentation_id": str(existing.id)},
    )

    assert not result.is_error, result.content
    assert existing.template_container_id == template_id
    assert existing.theme == "light"
    assert existing.spec["transition"] == "fade"
    assert existing.spec["aspect_ratio"] == "4:3"
    assert existing.spec["chrome"]["footer_text"] == "Confidential"
    assert existing.spec["chrome"]["show_slide_numbers"] is False
    assert existing.slide_count == 3
    assert existing.revision == 2


def test_editing_with_a_new_template_id_repoints_the_deck(monkeypatch):
    import app.core.database as database

    user_id = uuid.uuid4()
    existing = _existing_row(user_id, template_container_id=uuid.uuid4())
    replacement = uuid.uuid4()
    session = _FakeSession(
        results=[*_deck_lookup(existing), *_template_lookup(_template_row(replacement, name="Replacement"))]
    )
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "generate_presentation",
        {
            "title": "D",
            "slides": _slides(1),
            "presentation_id": str(existing.id),
            "template_id": str(replacement),
        },
    )

    assert not result.is_error, result.content
    assert existing.template_container_id == replacement
    assert "Template: Replacement (themes: dark only)" in result.content


def test_a_rewrite_cannot_repoint_the_deck_at_a_template_the_user_cannot_see(monkeypatch):
    """The rewrite path resolves the new template as the acting user may see it
    too: an invisible one is "not found", the deck keeps its current template."""
    import app.core.database as database

    user_id = uuid.uuid4()
    current = uuid.uuid4()
    existing = _existing_row(user_id, template_container_id=current)
    session = _FakeSession(results=[*_deck_lookup(existing), *_template_lookup(None)])
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "generate_presentation",
        {
            "title": "D",
            "slides": _slides(1),
            "presentation_id": str(existing.id),
            "template_id": str(uuid.uuid4()),
        },
    )

    assert result.is_error
    assert "No template" in result.content
    assert existing.template_container_id == current
    assert not session.committed


def test_a_rewrite_that_keeps_a_light_deck_under_a_dark_only_template_is_snapped(monkeypatch):
    """A stored light theme that the newly linked template cannot dress is
    snapped on the rewrite too - the link is what changed, so the template wins."""
    import app.core.database as database

    user_id = uuid.uuid4()
    existing = _existing_row(user_id, spec_extra={"theme": "light"})
    replacement = uuid.uuid4()
    session = _FakeSession(results=[*_deck_lookup(existing), *_template_lookup(_template_row(replacement))])
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "generate_presentation",
        {
            "title": "D",
            "slides": _slides(1),
            "presentation_id": str(existing.id),
            "template_id": str(replacement),
        },
    )

    assert not result.is_error, result.content
    assert existing.template_container_id == replacement
    assert existing.theme == "dark"
    assert existing.spec["theme"] == "dark"
    assert "Theme: 'light' is not supported by the template 'Brand Deck' (dark)" in result.content


def test_a_bad_template_id_is_returned_verbatim_and_writes_nothing(monkeypatch):
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools()
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D", "slides": _slides(1), "template_id": "the brand one"},
    )

    assert result.is_error
    # The TemplateChoiceError text, not the generic "failed to save".
    assert "search_templates" in result.content
    assert "failed to save" not in result.content
    assert session.added == []


def test_the_tool_schema_documents_the_three_shapes():
    defs, _executor = _build_tools()
    generate = next(d for d in defs if d.name == "generate_presentation")
    described = generate.input_schema["properties"]["template_id"]["description"].lower()
    assert "search_templates" in described
    assert "none" in described
    assert "default" in described
    # ...and the chat contract: the template is the user's choice.
    assert "creates no deck" in described
    assert "ask the user" in described


def test_the_result_says_how_the_template_was_chosen_when_the_agent_did_not_choose(monkeypatch):
    """An automation's platform default is a default, not a decision: the
    ``Template:`` line says so, so the agent reports it as such."""
    import app.core.database as database

    configured = uuid.uuid4()
    session = _FakeSession(
        results=[
            _Result(one=_settings_row(configured)),
            _Result(one=configured),
            *_template_lookup(_template_row(configured)),
        ]
    )
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools()
    result = _run_tool(executor, "generate_presentation", {"title": "D", "slides": _slides(1)})

    assert not result.is_error, result.content
    assert "Template: Brand Deck (themes: dark only) - platform default: no template was named" in result.content


# -- The chat contract: the template is the user's choice --------------


def _option(name: str, *, schemes=("dark", "light"), default=False, company=True, description=None):
    from app.services.presentations import TemplateOption

    return TemplateOption(
        id=uuid.uuid4(),
        name=name,
        description=description,
        supported_schemes=tuple(schemes),
        is_default=default,
        is_company_managed=company,
    )


def _offer(monkeypatch, options, *, already_asked: bool = False):
    """Stand in for ``list_template_options`` and record whether it was consulted.

    Also replaces the Redis-backed "asked in this conversation" marker with an
    in-memory one (``already_asked`` seeds it), recording every conversation the
    tool marks under ``calls.asked``."""
    from app.services import presentations

    class _Consulted(list):
        asked: list[str | None] = []

    calls = _Consulted()
    calls.asked = []

    async def _fake(db, *, user_id):
        calls.append(user_id)
        return list(options)

    async def _mark(conversation_id):
        calls.asked.append(conversation_id)

    async def _was_asked(conversation_id):
        return already_asked

    monkeypatch.setattr(presentations, "list_template_options", _fake)
    monkeypatch.setattr(presentations, "mark_template_choice_asked", _mark)
    monkeypatch.setattr(presentations, "template_choice_asked", _was_asked)
    return calls


def test_in_a_chat_with_several_templates_and_no_template_id_the_tool_asks_instead_of_creating(monkeypatch):
    """The deck's design is the user's call: with several templates to pick
    from and none named, a chat turn gets the list back, not a deck dressed in
    a default it never chose."""
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    brand = _option("Filigran Brand Deck", schemes=("dark",), default=True, description="The corporate look")
    own = _option("Acme Pitch", company=False)
    calls = _offer(monkeypatch, [brand, own])

    user_id = uuid.uuid4()
    conversation_id = str(uuid.uuid4())
    _defs, executor = _build_tools(
        str(user_id), conversation_id=conversation_id, request_text="Create a presentation about me"
    )
    result = _run_tool(executor, "generate_presentation", {"title": "D", "slides": _slides(2)})

    assert not result.is_error, result.content  # a question, not a failure
    assert calls == [user_id]
    assert session.added == []  # nothing was created
    assert not session.committed
    assert "No deck was created yet" in result.content
    # Every option is addressable: name, id, the themes it allows, its flags.
    assert f"Filigran Brand Deck (template_id {brand.id}; themes: dark)" in result.content
    assert "The corporate look" in result.content
    assert "[platform default]" in result.content
    assert f"Acme Pitch (template_id {own.id}; themes: dark / light)" in result.content
    assert "the user's own or shared with them" in result.content
    assert "No template (template_id 'none')" in result.content
    # What to do with it: ask - once, by name - and the answer is accepted.
    assert "Ask the user which template to use" in result.content
    assert "is accepted as the answer" in result.content
    assert "Design panel" in result.content
    assert "[[PRESENTATION:" not in result.content
    # The conversation now remembers the question was put to the user.
    assert calls.asked == [conversation_id]


def test_in_a_chat_the_single_available_template_is_used_and_the_result_says_so(monkeypatch):
    """One template needs no question - but the user is told it was the only
    one, never shown a default as if it had been chosen."""
    import app.core.database as database

    only = _option("Filigran Brand Deck", schemes=("dark",), default=True)
    session = _FakeSession(results=_template_lookup(_template_row(only.id, name="Filigran Brand Deck")))
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    calls = _offer(monkeypatch, [only])

    _defs, executor = _build_tools(conversation_id=str(uuid.uuid4()))
    result = _run_tool(executor, "generate_presentation", {"title": "D", "slides": _slides(1)})

    assert not result.is_error, result.content
    assert len(calls) == 1
    row = session.added[0]
    assert row.template_container_id == only.id
    assert (
        "Template: Filigran Brand Deck (themes: dark only) - the only template "
        "available to this user, so it was used without asking"
    ) in result.content
    # The platform default was not consulted: the single visible template IS
    # the answer, whatever an admin configured.
    issued = [_compiled(statement) for statement in session.statements]
    assert not [q for q in issued if "platform_settings" in q], issued


def test_in_a_chat_with_no_template_at_all_the_deck_is_free(monkeypatch):
    import app.core.database as database

    session = _FakeSession(results=[_Result(one=_settings_row(None)), _Result(one=None), _Result(one=None)])
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _offer(monkeypatch, [])

    _defs, executor = _build_tools(conversation_id=str(uuid.uuid4()))
    result = _run_tool(executor, "generate_presentation", {"title": "D", "slides": _slides(1)})

    assert not result.is_error, result.content
    assert session.added[0].template_container_id is None
    assert "Template: none (free deck" in result.content


@pytest.mark.parametrize(
    "request_text",
    [
        'Using the "Chosen" template (dark theme), create a presentation about our roadmap',
        "make me a deck about the roadmap with the chosen template please",
        "CHOSEN",  # the one-word answer to the question
        "Deck sur la roadmap, modèle Chosen",  # accents and another language
        "<uuid>",  # the id itself, pasted
    ],
)
def test_a_chat_call_whose_request_names_the_template_creates_the_deck(monkeypatch, request_text):
    """When the user's own words name the template - the New presentation
    dialog's primed message, a plain sentence, the one-word answer, its id - the
    choice is theirs and the deck is created at once, with no provenance note:
    nothing was decided for them."""
    import app.core.database as database

    chosen = _option("Chosen", schemes=("dark",))
    session = _FakeSession(results=_template_lookup(_template_row(chosen.id, name="Chosen")))
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    calls = _offer(monkeypatch, [_option("A"), chosen, _option("B")])

    text = f"deck about the roadmap with template {chosen.id}" if request_text == "<uuid>" else request_text
    _defs, executor = _build_tools(conversation_id=str(uuid.uuid4()), request_text=text)
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D", "slides": _slides(1), "template_id": str(chosen.id)},
    )

    assert not result.is_error, result.content
    assert len(calls) == 1  # the options were read to check the name against
    assert calls.asked == []  # no question was needed
    assert session.added[0].template_container_id == chosen.id
    assert "Template: Chosen (themes: dark only)\n" in result.content


@pytest.mark.parametrize(
    "request_text",
    [
        "Create a presentation about the roadmap without a template",
        "a plain deck about the roadmap, no template",
        "Fais une présentation sans template sur la roadmap",
        "Presentación sobre el roadmap sin plantilla",
    ],
)
def test_a_chat_call_opting_out_when_the_request_asks_for_no_template_creates_a_free_deck(
    monkeypatch, request_text
):
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    calls = _offer(monkeypatch, [_option("A"), _option("B")])

    _defs, executor = _build_tools(conversation_id=str(uuid.uuid4()), request_text=request_text)
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D", "slides": _slides(1), "template_id": "none"},
    )

    assert not result.is_error, result.content
    assert calls.asked == []
    assert session.added[0].template_container_id is None
    assert "Template: none (free deck" in result.content


@pytest.mark.parametrize("picked", ["<A>", "none"])
def test_a_chat_call_picking_a_template_the_user_never_named_asks_instead_of_creating(monkeypatch, picked):
    """The loophole this closes: the model looks the templates up, likes one
    (or decides on none) and passes it - the user, never asked, would get a
    design they did not choose. The tool refuses, names what the call picked,
    and lets the agent recommend it while asking."""
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    a = _option("Filigran Brand Deck", schemes=("dark",), default=True)
    calls = _offer(monkeypatch, [a, _option("Acme Pitch", company=False)])

    conversation_id = str(uuid.uuid4())
    _defs, executor = _build_tools(
        conversation_id=conversation_id,
        request_text="Create a presentation about why continuous threat exposure management matters",
    )
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D", "slides": _slides(1), "template_id": str(a.id) if picked == "<A>" else picked},
    )

    assert not result.is_error, result.content
    assert session.added == [] and not session.committed
    label = "Filigran Brand Deck" if picked == "<A>" else "no template"
    assert f"the call picked '{label}', but nothing the user wrote asked for it" in result.content
    assert f"You may recommend '{label}', but the user decides." in result.content
    assert "Ask the user which template to use" in result.content
    assert calls.asked == [conversation_id]


def test_once_the_question_was_asked_the_relayed_answer_is_accepted(monkeypatch):
    """After the question, the next template_id IS the user's answer - in
    whatever words they gave it ("the first one", "the dark one") - so the text
    match is not required a second time."""
    import app.core.database as database

    a = _option("Filigran Brand Deck", schemes=("dark",), default=True)
    session = _FakeSession(results=_template_lookup(_template_row(a.id, name="Filigran Brand Deck")))
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    calls = _offer(monkeypatch, [a, _option("Acme Pitch", company=False)], already_asked=True)

    _defs, executor = _build_tools(conversation_id=str(uuid.uuid4()), request_text="the first one")
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D", "slides": _slides(1), "template_id": str(a.id)},
    )

    assert not result.is_error, result.content
    assert calls.asked == []
    assert session.added[0].template_container_id == a.id
    assert "Template: Filigran Brand Deck (themes: dark only)\n" in result.content


def test_an_asked_conversation_still_needs_a_template_id_to_create(monkeypatch):
    """The marker settles WHICH template the relayed id is; it never turns a
    blank template_id into a default the user did not pick."""
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    calls = _offer(monkeypatch, [_option("A"), _option("B")], already_asked=True)

    _defs, executor = _build_tools(conversation_id=str(uuid.uuid4()), request_text="yes go ahead")
    result = _run_tool(executor, "generate_presentation", {"title": "D", "slides": _slides(1)})

    assert not result.is_error, result.content
    assert session.added == []
    assert "No deck was created yet" in result.content
    assert len(calls.asked) == 1


def test_template_choice_settled_matches_the_longest_name_and_the_id_only():
    """``Filigran Brand Deck`` is a substring of ``Filigran Brand Deck (copy)``:
    a request naming the copy must not validate a call that picked the base."""
    from app.services.presentations import template_choice_settled

    base = _option("Filigran Brand Deck")
    copy = _option("Filigran Brand Deck (copy)")
    other = _option("Acme Pitch")
    options = [base, copy, other]

    named_copy = "Use the Filigran Brand Deck (copy) template for a deck about Q3"
    assert template_choice_settled(named_copy, str(copy.id), options)
    assert not template_choice_settled(named_copy, str(base.id), options)
    assert not template_choice_settled(named_copy, str(other.id), options)

    named_base = "filigran brand deck, 4 slides about q3"
    assert template_choice_settled(named_base, str(base.id), options)
    assert not template_choice_settled(named_base, str(copy.id), options)

    # The id in the text settles it; a template name nowhere in the text does not.
    assert template_choice_settled(f"deck about q3 with {other.id}", str(other.id), options)
    assert not template_choice_settled("deck about q3", str(other.id), options)
    # Blank inputs never settle anything.
    assert not template_choice_settled(None, str(other.id), options)
    assert not template_choice_settled("Acme Pitch deck", None, options)
    assert not template_choice_settled("Acme Pitch deck", "  ", options)
    # 'none' needs the user to have asked for no template in so many words.
    assert template_choice_settled("a deck about q3 without any template", "none", options)
    assert not template_choice_settled("a deck about q3", "none", options)


@pytest.mark.parametrize(
    ("name", "names_it", "does_not"),
    [
        ("Brand", "a deck in the Brand template", "a deck about our rebranding"),
        ("AI", "use AI, 5 slides", "she said yes to the 5 slides"),
        ("Q3", "the Q3 deck about revenue", "the Q30 deck about revenue"),
        ("Pitch", "Pitch: a deck about Q3", "the pitching deck about q3"),
        ("Acme Pitch", "Acme Pitch. 4 slides.", "the acme pitches of 2026"),
    ],
)
def test_template_choice_settled_needs_the_name_as_whole_words(name, names_it, does_not):
    """A template name is named by WORDS, not by a fragment of a longer one:
    a fragment match would settle the choice on the user's behalf, which is
    the loophole the gate exists to close."""
    from app.services.presentations import template_choice_settled

    option = _option(name)
    options = [option, _option("Something Else")]
    assert template_choice_settled(names_it, str(option.id), options)
    assert not template_choice_settled(does_not, str(option.id), options)


def test_fold_text_is_case_and_diacritics_insensitive_and_collapses_whitespace():
    from app.services.presentations import fold_text

    assert fold_text("  Filigrán   BRAND\tDeck ") == "filigran brand deck"
    assert fold_text("Modèle") == fold_text("modele") == "modele"


def test_the_template_choice_marker_is_fail_open(monkeypatch):
    """No Redis: the marker is skipped, the read says 'not asked', nothing raises
    - at worst the question is put once more, never a deck the user did not pick."""
    import app.core.redis as redis_module
    from app.services.presentations import (
        TEMPLATE_CHOICE_ASKED_TTL_SECS,
        mark_template_choice_asked,
        template_choice_asked,
        template_choice_asked_key,
    )

    async def _down():
        raise ConnectionError("redis is down")

    monkeypatch.setattr(redis_module, "redis_client", _down)
    asyncio.run(mark_template_choice_asked("conv-1"))
    assert asyncio.run(template_choice_asked("conv-1")) is False
    # Without a conversation there is nothing to remember - and no Redis call.
    asyncio.run(mark_template_choice_asked(None))
    assert asyncio.run(template_choice_asked(None)) is False
    assert template_choice_asked_key("conv-1") == "presentation:template_asked:conv-1"
    assert TEMPLATE_CHOICE_ASKED_TTL_SECS == 7 * 24 * 3600


def test_the_template_choice_marker_round_trips_through_redis(monkeypatch):
    import app.core.redis as redis_module
    from app.services.presentations import mark_template_choice_asked, template_choice_asked

    store: dict[str, tuple[str, int]] = {}

    class _Redis:
        async def set(self, key, value, ex=None):
            store[key] = (value, ex)

        async def exists(self, key):
            return 1 if key in store else 0

    async def _client():
        return _Redis()

    monkeypatch.setattr(redis_module, "redis_client", _client)
    assert asyncio.run(template_choice_asked("conv-2")) is False
    asyncio.run(mark_template_choice_asked("conv-2"))
    assert store == {"presentation:template_asked:conv-2": ("1", 7 * 24 * 3600)}
    assert asyncio.run(template_choice_asked("conv-2")) is True


def test_an_edit_in_a_chat_never_asks_for_a_template(monkeypatch):
    """The question is about a NEW deck; a rewrite keeps the deck's template."""
    import app.core.database as database

    user_id = uuid.uuid4()
    existing = _existing_row(user_id, template_container_id=None)
    session = _FakeSession(results=_deck_lookup(existing))
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    calls = _offer(monkeypatch, [_option("A"), _option("B")])

    _defs, executor = _build_tools(str(user_id), conversation_id=str(uuid.uuid4()))
    result = _run_tool(
        executor,
        "generate_presentation",
        {"title": "D", "slides": _slides(1), "presentation_id": str(existing.id)},
    )

    assert not result.is_error, result.content
    assert calls == []
    assert existing.revision == 2


def test_a_chat_turn_delegated_from_an_assignment_run_is_an_automation(monkeypatch):
    """``assignment_run_id`` is the parent run: nobody is there to answer, so
    the platform default applies exactly as in the engine."""
    import app.core.database as database

    configured = uuid.uuid4()
    session = _FakeSession(
        results=[
            _Result(one=_settings_row(configured)),
            _Result(one=configured),
            *_template_lookup(_template_row(configured)),
        ]
    )
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    calls = _offer(monkeypatch, [_option("A"), _option("B")])

    _defs, executor = _build_tools(
        conversation_id=str(uuid.uuid4()), assignment_run_id=str(uuid.uuid4())
    )
    result = _run_tool(executor, "generate_presentation", {"title": "D", "slides": _slides(1)})

    assert not result.is_error, result.content
    assert calls == []
    assert session.added[0].template_container_id == configured
    assert "platform default: no template was named" in result.content


@pytest.mark.parametrize(
    ("conversation_id", "assignment_run_id", "interactive"),
    [
        ("conv", None, True),
        (None, None, False),
        ("conv", "run", False),
        (None, "run", False),
        ("", None, False),
    ],
)
def test_is_interactive_turn(conversation_id, assignment_run_id, interactive):
    from app.services.builtin_tools.presentation import is_interactive_turn

    assert is_interactive_turn(conversation_id, assignment_run_id) is interactive


def test_format_template_choice_lists_every_option_and_the_no_template_row():
    from app.services.presentations import format_template_choice

    brand = _option("Brand", schemes=("dark", "light"), default=True)
    text = format_template_choice([brand, _option("Mine", schemes=("light",), company=False)])
    lines = text.splitlines()
    assert lines[0].startswith("No deck was created yet")
    assert lines[1] == f"- Brand (template_id {brand.id}; themes: dark / light) [platform default]"
    assert "- Mine (template_id " in lines[2] and "themes: light" in lines[2]
    assert lines[3].startswith("- No template (template_id 'none')")
    assert "BEFORE writing the slides" in lines[4]


def test_list_template_options_offers_visible_presentation_templates_default_first(monkeypatch):
    """Visibility is the three-tier model through ``search_containers`` (never
    a second query), only ``kind = presentation`` is offered, and the order is
    the one a list read aloud needs: default, company-managed, then the rest."""
    from app.api.v1 import helpers
    from app.services import presentations, template_containers

    user_id = uuid.uuid4()
    groups = [uuid.uuid4()]
    default_id = uuid.uuid4()
    rows = [
        SimpleNamespace(
            id=uuid.uuid4(),
            name="Zed private",
            description="  ",
            is_company_managed=False,
            theme_config=DARK_PALETTE,
            extra_metadata={"theme_variants": {"light": LIGHT_VARIANT}},
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            name="Company B",
            description="Sales",
            is_company_managed=True,
            theme_config=DARK_PALETTE,
            extra_metadata={},
        ),
        SimpleNamespace(
            id=default_id,
            name="Brand",
            description=None,
            is_company_managed=True,
            theme_config=DARK_PALETTE,
            extra_metadata={},
        ),
    ]
    seen: dict = {}

    async def _groups(db, uid):
        seen["groups_for"] = uid
        return groups

    async def _search(db, **kwargs):
        seen["search"] = kwargs
        return rows

    async def _default(db):
        return default_id

    monkeypatch.setattr(helpers, "get_user_group_ids", _groups)
    monkeypatch.setattr(template_containers, "search_containers", _search)
    monkeypatch.setattr(presentations, "resolve_default_template_id", _default)

    options = asyncio.run(presentations.list_template_options(None, user_id=user_id))  # type: ignore[arg-type]

    assert seen["groups_for"] == user_id
    assert seen["search"]["user_id"] == user_id
    assert seen["search"]["user_group_ids"] == groups
    assert seen["search"]["kind"] == template_containers.PRESENTATION_KIND
    assert [o.name for o in options] == ["Brand", "Company B", "Zed private"]
    assert options[0].is_default and options[0].is_company_managed
    assert options[1].description == "Sales"
    assert options[2].description is None  # blank is absent
    assert options[2].supported_schemes == ("dark", "light")
    assert options[1].supported_schemes == ("dark",)


# -- ensure_deck_tools: a deck agent can always list templates ----------------


def test_ensure_deck_tools_appends_template_search_to_a_deck_agent_only():
    from app.services.builtin_tools import ensure_deck_tools
    from app.services.builtin_tools.presentation import DECK_COMPANION_TOOL_IDS

    assert DECK_COMPANION_TOOL_IDS == ("template_search",)
    assert ensure_deck_tools(["web_fetch", "generate_presentation"]) == [
        "web_fetch",
        "generate_presentation",
        "template_search",
    ]
    # Idempotent, order-preserving, never removes anything.
    once = ensure_deck_tools(["template_search", "generate_presentation"])
    assert once == ["template_search", "generate_presentation"]
    assert ensure_deck_tools(once) == once
    # No deck tool, no companion.
    assert ensure_deck_tools(["web_fetch"]) == ["web_fetch"]
    assert ensure_deck_tools(None) == []
    assert ensure_deck_tools([]) == []


def test_every_tool_assembly_path_guarantees_the_deck_companion():
    """Chat, General Assistant and the assignment engine all resolve the
    effective built-in ids through ``ensure_deck_tools`` - a deck
    agent that could not list templates could not answer the tool's question."""
    from app.services import agent_chat, assignment_engine, general_chat

    for module in (agent_chat, general_chat, assignment_engine):
        assert "ensure_deck_tools(" in inspect.getsource(module), module.__name__


# -- The admin setting ---------------------------------------------------------


def test_platform_settings_carry_a_nullable_default_template_column():
    from app.models.platform_settings import PlatformSettings

    column = PlatformSettings.__table__.columns["default_presentation_template_id"]
    assert column.nullable is True
    # A NULL means "the shipped brand", so the seed never has to write it and
    # clearing the field in Settings returns to the brand rather than to nothing.
    assert column.default is None and column.server_default is None


def test_the_settings_update_validates_the_default_in_four_steps():
    """UUID shape -> the container exists -> it is a presentation template ->
    it is company-managed. The order matters for the error a user reads."""
    source = (BACKEND_ROOT / "app/api/v1/settings.py").read_text(encoding="utf-8")
    start = source.index('if "default_presentation_template_id" in body:')
    block = source[start : start + 3500]
    assert 'raw_template in (None, "")' in block  # null / "" reverts to the brand
    order = [
        block.index("must be a template container UUID"),
        block.index("template container not found"),
        block.index("presentation template (kind is"),
        block.index("company-managed template so every user"),
    ]
    assert order == sorted(order)
    # Not a branding change: a deck theme is not part of the branding snapshot.
    assert "ps.default_presentation_template_id = template_row.id" in block


def test_the_settings_response_exposes_the_default():
    source = (BACKEND_ROOT / "app/api/v1/settings.py").read_text(encoding="utf-8")
    assert '"default_presentation_template_id": (' in source


def test_the_settings_ui_maps_the_brand_choice_to_null():
    """The Settings picker offers "Filigran Brand Deck (built-in)" as a first
    option; choosing it sends ``null``, never the seeded container's id, so a
    later brand re-seed cannot strand the setting on a stale row."""
    source = (FRONTEND_ROOT / "src/pages/settings/ParametersSection.tsx").read_text(encoding="utf-8")
    assert "default_presentation_template_id" in source
    assert re.search(r"default_presentation_template_id:\s*next === BRAND_DEFAULT_TEMPLATE \? null : next", source)


def test_the_default_template_migration_is_idempotent_and_lock_safe():
    path = BACKEND_ROOT / "alembic/versions/f1p3358dflt_add_platform_settings_default_presentation_template.py"
    source = path.read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS" in source
    assert "terminate_lock_holders(" in source
    assert "set_lock_timeout(" in source


# -- Deployment + documentation wiring ----------------------------------------


def test_render_base_url_is_configurable_and_documented():
    from app.core.config import Settings

    assert "presentation_render_base_url" in Settings.model_fields
    env_sample = (REPO_ROOT / ".env.sample").read_text(encoding="utf-8")
    assert "PRESENTATION_RENDER_BASE_URL" in env_sample
    configuration = (DOCS_ROOT / "docs/deployment/configuration.md").read_text(encoding="utf-8")
    assert "PRESENTATION_RENDER_BASE_URL" in configuration
    assert "## Presentation export" in configuration


def test_the_user_docs_are_wired_into_the_navigation():
    nav = (DOCS_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    for page in ("usage/presentations.md", "usage/resources/templates.md"):
        assert page in nav, page
        assert (DOCS_ROOT / "docs" / page).is_file(), page
    resources_index = (DOCS_ROOT / "docs/usage/resources/index.md").read_text(encoding="utf-8")
    assert "templates.md" in resources_index
    settings_doc = (DOCS_ROOT / "docs/administration/settings.md").read_text(encoding="utf-8")
    assert "Default Presentation Template" in settings_doc


def test_the_developer_doc_names_modules_that_exist():
    """``dev-docs/presentations.md`` is the map of the feature; a path it names
    must exist, or the map rots the first time a module is renamed."""
    doc = (REPO_ROOT / "dev-docs/presentations.md").read_text(encoding="utf-8")
    paths = set(
        re.findall(
            r"`((?:app|frontend/src|backend/scripts|dev-docs|docs/docs)/[\w./-]+\.(?:py|ts|tsx|md|css|json|yml))`", doc
        )
    )
    assert len(paths) >= 15, sorted(paths)  # the regex still finds the map
    roots = {"app": BACKEND_ROOT, "frontend": REPO_ROOT, "backend": REPO_ROOT, "dev-docs": REPO_ROOT, "docs": REPO_ROOT}
    missing = [p for p in sorted(paths) if not (roots[p.split("/", 1)[0]] / p).exists()]
    assert not missing, missing
