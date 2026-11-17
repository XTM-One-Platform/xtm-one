"""Tests for the non-vectorized template container + asset surface.

The whole point of the feature is that templates are retrieved by **structured
metadata**, never by vector similarity, and that their content never enters the
RAG corpus. These tests lock in the three acceptance criteria:

1. Structured search returns container metadata + asset references, with no
   vector/embedding code path involved.
2. Adding / replacing / removing one asset changes only the asset list — the
   container's other metadata is never rewritten or required in the request.
3. Creating or updating a container triggers no embedding or chunking, and the
   container is unreachable from any ``search_knowledge`` code path.

No DB required: the service functions open their own sessions, which are
monkeypatched with capturing fakes (mirroring the no-DB style of
``test_work_items.py`` / ``test_work_item_lifecycle.py``). The SQL-level filter
semantics are asserted by compiling the statement against the PostgreSQL dialect,
which needs no connection.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import io
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

import app.services.template_containers as tpl_service
from app.models.template_container import (
    MAX_ASSET_DESCRIPTION_CHARS,
    MAX_ASSETS_PER_CONTAINER,
    TEMPLATE_ASSET_SOURCE,
    TemplateContainer,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]


# ── Fixtures / fakes ──────────────────────────────────────────────────────────


def _container(**overrides) -> SimpleNamespace:
    now = datetime.now(UTC)
    base = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        is_company_managed=False,
        shared_group_ids=None,
        shared_group_permissions=None,
        shared_user_ids=None,
        shared_user_permissions=None,
        access_restricted=False,
        name="Quarterly Deck",
        description="Executive quarterly review deck",
        kind="presentation",
        tags=["quarterly", "executive"],
        theme="dark",
        theme_config={"palette": {"accent": "#00b4ff"}},
        aspect_ratio="16:9",
        layouts=[{"id": "title-slide", "name": "Title"}, {"id": "kpi", "name": "KPIs"}],
        extra_metadata={"owner_team": "brand"},
        lookup_count=3,
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _asset(**overrides) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        filename="Inter-Bold.ttf",
        content_type="font/ttf",
        size=204_800,
        asset_role="font",
        description=None,
        s3_object_name="template-asset/abc/def.ttf",
        created_at=datetime.now(UTC),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Result:
    def __init__(self, rows=None, one=None, pairs=None):
        self._rows = rows or []
        self._one = one
        self._pairs = pairs or []

    def scalars(self):
        return self

    def all(self):
        return self._pairs if self._pairs else self._rows

    def scalar_one_or_none(self):
        return self._one


class _FakeSession:
    """Async-context session returning a queued result per ``execute``."""

    def __init__(self, results):
        self._results = list(results)
        self.statements: list = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, *a, **k):
        self.statements.append(statement)
        return self._results.pop(0) if self._results else _Result()

    def in_transaction(self):
        # A real session autobegins on its first statement.
        return bool(self.statements) and not self.committed

    async def commit(self):
        self.committed = True

    async def refresh(self, *a, **k):
        return None

    def add(self, obj):
        pass

    async def flush(self):
        pass


def _compiled(query) -> str:
    """Render *query* as PostgreSQL SQL (with literals) — needs no connection."""
    return str(
        query.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def _run_tool(executor, name: str, arguments: dict):
    """Run one built-in tool through the real executor and return the result."""
    from app.tools.base import ToolCall

    return asyncio.run(
        executor.execute_tool(ToolCall(id="call-1", name=name, arguments=arguments))
    )


def _call_tool(executor, name: str, arguments: dict) -> str:
    """Run one built-in tool, asserting it SUCCEEDED, and return its text."""
    result = _run_tool(executor, name, arguments)
    assert not result.is_error, result.content
    return result.content


# ── AC1: structured search, no vector similarity ──────────────────────────────


def test_search_filters_are_exact_for_controlled_labels():
    """kind / aspect_ratio (and a legacy theme label) must be EXACT, not substring.

    Substring matching there would let ``aspect_ratio="4:3"`` also select a
    ``"14:3"``-style value and quietly widen the result set.
    """
    sql = _compiled(
        tpl_service.apply_structured_filters(
            select(TemplateContainer),
            kind="Presentation",
            theme="Corporate",
            aspect_ratio="16:9",
        )
    )
    # Lower-cased equality, never LIKE, on all three controlled labels.
    assert "lower(template_containers.kind) = 'presentation'" in sql
    assert "lower(template_containers.theme) = 'corporate'" in sql
    assert "lower(template_containers.aspect_ratio) = '16:9'" in sql
    assert "LIKE" not in sql.upper()


def test_scheme_theme_filter_is_not_pushed_to_sql():
    """``theme="dark"`` / ``"light"`` is a COLOUR SCHEME, derived from the
    palette on read - so it cannot be a column predicate. The SQL carries no
    ``theme`` clause and the caller applies ``filter_by_theme`` to the rows."""
    for scheme in ("dark", "Light"):
        assert tpl_service.is_scheme_filter(scheme) is True
        sql = _compiled(
            tpl_service.apply_structured_filters(select(TemplateContainer), theme=scheme)
        )
        assert "WHERE" not in sql.upper(), sql
        assert "lower(template_containers.theme)" not in sql, sql
    assert tpl_service.is_scheme_filter("corporate") is False
    assert tpl_service.is_scheme_filter(None) is False


def test_scheme_theme_filter_reads_the_supported_schemes():
    """A scheme filter keeps exactly the presentation templates whose
    ``supported_schemes`` include it - the same list the deck editor restricts
    its theme picker to - and never an asset library or a report skin, which
    dress no deck."""
    dark_only = _container(
        name="Dark only", theme=None, theme_config={"background": "#0b1020"}
    )
    light_only = _container(
        name="Light only", theme=None, theme_config={"background": "#ffffff"}
    )
    both = _container(
        name="Both",
        theme=None,
        theme_config={"background": "#0b1020"},
        extra_metadata={"theme_variants": {"light": {"background": "#ffffff"}}},
    )
    bare = _container(name="Bare", theme=None, theme_config={}, extra_metadata={})
    logos = _container(
        name="Partner logos", kind="asset-library", theme=None, theme_config={}
    )
    report = _container(
        name="Report skin", kind="report", theme=None, theme_config={"background": "#fff"}
    )
    rows = [dark_only, light_only, both, bare, logos, report]

    names = lambda theme: [c.name for c in tpl_service.filter_by_theme(rows, theme)]  # noqa: E731
    assert names("dark") == ["Dark only", "Both", "Bare"]
    assert names("light") == ["Light only", "Both", "Bare"]
    assert names(None) == [c.name for c in rows]
    assert names("  ") == [c.name for c in rows]
    # Served the same way: no deck constraint at all for a non-deck kind.
    assert tpl_service.container_supported_schemes(logos) == []
    assert tpl_service.container_supported_schemes(report) == []
    assert tpl_service.container_supported_schemes(bare) == ["dark", "light"]
    assert tpl_service.container_supported_schemes(light_only) == ["light"]


def test_legacy_theme_label_still_matches_exactly():
    """A saved filter written against the old free-text label keeps working -
    for a value that is NOT a scheme. Exact, case-insensitive, no substring."""
    labelled = _container(
        name="Labelled", theme="acme-dark", theme_config={"background": "#ffffff"}
    )
    assert tpl_service.theme_filter_matches(labelled, "ACME-dark") is True
    assert tpl_service.theme_filter_matches(labelled, "acme") is False
    # A scheme word is not a label lookup: it reads the palette (light here).
    assert tpl_service.theme_filter_matches(labelled, "dark") is False
    assert tpl_service.theme_filter_matches(labelled, "light") is True
    # A container with no label at all only ever passes an empty filter.
    unlabelled = _container(name="Unlabelled", theme=None, theme_config={})
    assert tpl_service.theme_filter_matches(unlabelled, "acme-dark") is False
    assert tpl_service.theme_filter_matches(unlabelled, None) is True


def test_scheme_filter_never_consults_the_legacy_label():
    """``theme=dark`` uses supported-scheme semantics ONLY.

    A light-only template that still carries the legacy label ``dark`` cannot
    dress a dark deck, so it must not come back for ``theme=dark`` - the label
    is a free-text tag, not a fact about the palette. Conversely a template
    whose palette reads dark is found for ``dark`` whatever its label says.
    """
    mislabelled = _container(
        name="Mislabelled", theme="dark", theme_config={"background": "#ffffff"}
    )
    assert tpl_service.theme_filter_matches(mislabelled, "dark") is False
    assert tpl_service.theme_filter_matches(mislabelled, "Light") is True

    dark_capable = _container(
        name="Dark capable", theme="corporate", theme_config={"background": "#0b1020"}
    )
    assert tpl_service.theme_filter_matches(dark_capable, "dark") is True
    assert tpl_service.theme_filter_matches(dark_capable, "light") is False
    # The label still answers a non-scheme value on the same container.
    assert tpl_service.theme_filter_matches(dark_capable, "Corporate") is True

    # Documented consequences of the derived list: a non-presentation kind has
    # no supported scheme and never comes back for one, even when labelled;
    # a presentation template with no readable palette dresses either scheme.
    logos = _container(name="Logos", kind="asset-library", theme="dark", theme_config={})
    assert tpl_service.theme_filter_matches(logos, "dark") is False
    assert tpl_service.theme_filter_matches(logos, "light") is False
    bare = _container(name="Bare", theme="light", theme_config={}, extra_metadata={})
    assert tpl_service.theme_filter_matches(bare, "dark") is True
    assert tpl_service.theme_filter_matches(bare, "light") is True


def test_rest_list_and_search_tool_share_the_theme_filter(monkeypatch):
    """``GET /template-containers?theme=dark`` and the ``search_templates``
    tool must agree on a scheme filter, so both go through the ONE pair
    ``apply_structured_filters`` + ``filter_by_theme`` - the semantics are
    never re-spelled in the route or the tool."""
    from app.services.builtin_tools import build_template_search_tools

    user = _user()
    mislabelled = _container(
        name="Mislabelled", user_id=user.id, theme="dark",
        theme_config={"background": "#ffffff"},
    )
    dark_capable = _container(
        name="Dark capable", user_id=user.id, theme=None,
        theme_config={"background": "#0b1020"},
    )
    rows = [mislabelled, dark_capable]

    # REST: the SQL carries no theme predicate for a scheme, the rows are
    # filtered by the shared helper, and only the dark-capable template lists.
    session = _FakeSession([_Result(rows=[]), _Result(rows=rows), _Result(rows=[])])
    client, _ = _api_client(user, session_factory=lambda: session)
    response = client.get("/api/v1/template-containers", params={"theme": "dark"})
    assert response.status_code == 200, response.text
    assert [c["name"] for c in response.json()] == ["Dark capable"]
    assert "lower(template_containers.theme)" not in _compiled(session.statements[1])

    # Tool: the service's ``search_containers`` (faked at the DB level only) runs
    # the same ``filter_by_theme`` and the tool output lists the same template.
    async def _fake_load_assets(db, container_id):
        return []

    async def _fake_groups(db, uid):
        return []

    monkeypatch.setattr(tpl_service, "load_assets", _fake_load_assets)
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", _fake_groups, raising=False)
    monkeypatch.setattr(
        "app.core.database.async_session",
        lambda: _FakeSession([_Result(rows=rows)]),
        raising=False,
    )
    _defs, executor = build_template_search_tools(db=None, user_id=str(user.id))  # type: ignore[arg-type]
    output = _call_tool(executor, "search_templates", {"theme": "dark"})
    assert "Dark capable" in output
    assert "Mislabelled" not in output

    # And the non-scheme form reaches SQL as the legacy exact label match.
    session = _FakeSession([_Result(rows=[]), _Result(rows=[mislabelled]), _Result(rows=[])])
    client, _ = _api_client(user, session_factory=lambda: session)
    response = client.get("/api/v1/template-containers", params={"theme": "Dark-Corporate"})
    assert response.status_code == 200, response.text
    assert "lower(template_containers.theme) = 'dark-corporate'" in _compiled(session.statements[1])


def test_search_containers_cuts_to_the_limit_after_the_scheme_filter():
    """A scheme filter fetches the ordered candidates WITHOUT the SQL limit and
    slices after filtering - a page of light-only templates must not leave a
    ``theme="dark"`` search empty while dark ones exist further down."""
    light = [
        _container(name=f"Light {i}", theme=None, theme_config={"background": "#ffffff"})
        for i in range(3)
    ]
    dark = _container(name="Dark", theme=None, theme_config={"background": "#0b1020"})
    session = _FakeSession([_Result(rows=[*light, dark])])

    found = asyncio.run(
        tpl_service.search_containers(
            session, user_id=uuid.uuid4(), user_group_ids=[], theme="dark", limit=2
        )
    )
    assert [c.name for c in found] == ["Dark"]
    assert "LIMIT" not in _compiled(session.statements[0]).upper()

    # A non-scheme filter keeps the SQL limit (nothing to post-filter).
    session = _FakeSession([_Result(rows=[dark])])
    asyncio.run(
        tpl_service.search_containers(
            session, user_id=uuid.uuid4(), user_group_ids=[], theme="acme", limit=2
        )
    )
    assert "LIMIT 2" in _compiled(session.statements[0]).upper()


def test_name_filter_is_case_insensitive_substring():
    sql = _compiled(
        tpl_service.apply_structured_filters(select(TemplateContainer), name="Quarterly")
    )
    # ``ILIKE`` is PostgreSQL's case-insensitive LIKE — the substring match is
    # intentional here (a human names a template loosely), unlike the exact
    # controlled labels above.
    assert "template_containers.name ILIKE '%%Quarterly%%'" in sql


def test_tag_filter_quotes_the_token_so_it_cannot_match_a_neighbour():
    """A bare token would make tag ``dark`` match a ``darkroom`` tag."""
    sql = _compiled(
        tpl_service.apply_structured_filters(select(TemplateContainer), tags=["Dark"])
    )
    # The token is matched QUOTED, which is what prevents the substring false
    # positive, and the whole JSON column is lower-cased first.
    assert "lower(CAST(template_containers.tags AS TEXT)) LIKE" in sql
    assert '\'"dark"\'' in sql


def test_tag_filter_any_of_by_default_and_all_of_on_request():
    any_of = _compiled(
        tpl_service.apply_structured_filters(
            select(TemplateContainer), tags=["a", "b"], match_all_tags=False
        )
    )
    all_of = _compiled(
        tpl_service.apply_structured_filters(
            select(TemplateContainer), tags=["a", "b"], match_all_tags=True
        )
    )
    assert " OR " in any_of
    assert " OR " not in all_of
    assert " AND " in all_of


def test_blank_and_empty_tags_are_ignored():
    """Empty strings must not become a `LIKE '%""%'` clause matching everything."""
    sql = _compiled(
        tpl_service.apply_structured_filters(
            select(TemplateContainer), tags=["", "   ", None]  # type: ignore[list-item]
        )
    )
    # No WHERE clause at all: every supplied tag was blank, so no filter applies.
    # (``tags`` still appears in the SELECT column list — that is the projection,
    # not a predicate.)
    assert "WHERE" not in sql


def test_normalize_tags_trims_lowercases_and_dedupes():
    assert tpl_service.normalize_tags([" Executive ", "QUARTERLY"]) == [
        "executive",
        "quarterly",
    ]
    # Blanks dropped, not turned into empty tags.
    assert tpl_service.normalize_tags(["", "   ", None]) == []  # type: ignore[list-item]
    # Duplicates that collapse after normalising are dropped, order preserved.
    assert tpl_service.normalize_tags(["A", "a", "b", "A"]) == ["a", "b"]
    assert tpl_service.normalize_tags(None) == []


def test_normalize_asset_description_is_one_bounded_line_or_none():
    """The caption is quoted one-asset-per-line into the ``get_template``
    result: a newline would break that listing, an empty string is
    not a description, and an unbounded one would crowd out the other assets."""
    normalize = tpl_service.normalize_asset_description
    assert normalize("  XTM platform:\n\tfour products.  ") == "XTM platform: four products."
    assert normalize("") is None
    assert normalize("   \n ") is None
    assert normalize(None) is None
    assert normalize(42) is None
    assert len(normalize("x" * (MAX_ASSET_DESCRIPTION_CHARS + 50))) == MAX_ASSET_DESCRIPTION_CHARS
    # Every write path agrees on what lands: REST, package import, seed, tool.
    for rel in (
        "app/api/v1/template_containers.py",
        "app/services/template_asset_seed.py",
        "app/services/builtin_tools/template_authoring.py",
    ):
        source = (BACKEND_ROOT / rel).read_text(encoding="utf-8")
        assert "normalize_asset_description(" in source, f"{rel} stores a raw description"


def test_stored_tags_are_findable_by_the_filter_that_reads_them():
    """The write/read round-trip is the whole point of normalising on write.

    An un-normalised tag (``" Executive "``) serialises into the JSON column with
    its spaces and capital, while the filter looks for the lower-cased quoted
    token ``'"executive"'`` — so the tag would be permanently unsearchable.
    Normalising on write is what closes that gap; this asserts the two ends agree.
    """
    stored = tpl_service.normalize_tags([" Executive "])
    # What the DB would hold, as the filter's CAST(... AS TEXT) would see it.
    serialized = json.dumps(stored).lower()

    sql = _compiled(
        tpl_service.apply_structured_filters(
            select(TemplateContainer), tags=["Executive"]
        )
    )
    # Pull the quoted token the predicate searches for, and check it is present
    # in the serialised column value.
    assert '\'"executive"\'' in sql
    assert '"executive"' in serialized


def test_search_containers_never_calls_a_vector_or_embedding_path(monkeypatch):
    """AC1: no vector similarity search is performed.

    Any call into the embedding/vector services during a structured search is a
    hard failure — this is the invariant the whole issue exists to protect.
    """
    import app.services.persistent_memory as memory_mod
    import app.services.vector as vector_mod

    def _boom(*a, **k):
        raise AssertionError("template search must never touch the vector pipeline")

    monkeypatch.setattr(vector_mod, "search_embeddings", _boom, raising=False)
    monkeypatch.setattr(vector_mod, "store_embedding", _boom, raising=False)
    monkeypatch.setattr(memory_mod, "embed_text", _boom, raising=False)

    container = _container()
    session = _FakeSession([_Result(rows=[])], )

    async def _run():
        return await tpl_service.search_containers(
            session,
            user_id=container.user_id,
            user_group_ids=[],
            kind="presentation",
        )

    assert asyncio.run(_run()) == []


def test_search_result_is_capped_at_the_service_ceiling():
    """A caller cannot widen the cap past ``MAX_SEARCH_RESULTS``."""
    session = _FakeSession([_Result(rows=[])])

    async def _run(limit):
        await tpl_service.search_containers(
            session, user_id=uuid.uuid4(), user_group_ids=[], limit=limit
        )
        return _compiled(session.statements[-1])

    over = asyncio.run(_run(10_000))
    assert f"LIMIT {tpl_service.MAX_SEARCH_RESULTS}" in over

    session.statements.clear()
    session._results = [_Result(rows=[])]
    under = asyncio.run(_run(0))
    # A zero/negative limit floors at 1 rather than emitting `LIMIT 0`.
    assert "LIMIT 1" in under


def test_anonymous_caller_sees_only_company_managed_containers():
    """An external-channel user with no account must never inherit private rows."""
    session = _FakeSession([_Result(rows=[])])

    async def _run():
        await tpl_service.search_containers(session, user_id=None, user_group_ids=None)
        return _compiled(session.statements[-1])

    sql = asyncio.run(_run())
    assert "template_containers.is_company_managed IS true" in sql
    assert "user_id" not in sql.split("WHERE")[-1]


def test_serialize_container_exposes_metadata_and_asset_refs_but_not_storage_keys():
    """AC1: the caller gets metadata + asset references.

    ``s3_object_name`` must NOT leak — it is an internal storage detail, and
    assets are addressed by id through the download endpoint.
    """
    asset = _asset()
    payload = tpl_service.serialize_container(_container(), assets=[asset])

    assert payload["name"] == "Quarterly Deck"
    assert payload["kind"] == "presentation"
    assert payload["tags"] == ["quarterly", "executive"]
    assert payload["theme"] == "dark"
    assert payload["aspect_ratio"] == "16:9"
    assert payload["asset_count"] == 1
    assert payload["assets"][0]["id"] == str(asset.id)
    assert payload["assets"][0]["asset_role"] == "font"

    serialized = repr(payload)
    assert "s3_object_name" not in serialized
    assert asset.s3_object_name not in serialized


def test_an_asset_version_moves_with_its_bytes_and_hides_the_storage_key():
    """``version`` is the download's ETag: the client-side cache key of
    the thumbnail. It changes when the stored object does - the one thing an
    in-place replace changes - and is a digest, never the key itself."""
    from app.services.file_responses import file_etag

    asset = _asset()
    before = tpl_service.serialize_asset(asset)["version"]
    assert before == file_etag(asset).strip('"')
    assert asset.s3_object_name not in before

    asset.s3_object_name = "template-asset/abc/replaced.ttf"
    assert tpl_service.serialize_asset(asset)["version"] != before


def test_load_assets_filters_on_both_fk_and_source():
    """A row that lost its ``template_asset`` source must not be served as one."""
    session = _FakeSession([_Result(rows=[])])
    container_id = uuid.uuid4()

    async def _run():
        await tpl_service.load_assets(session, container_id)
        return _compiled(session.statements[-1])

    sql = asyncio.run(_run())
    assert "file_attachments.template_container_id" in sql
    assert f"file_attachments.source = '{TEMPLATE_ASSET_SOURCE}'" in sql


def test_record_lookups_bumps_each_retrieved_container():
    containers = [_container(lookup_count=0), _container(lookup_count=7)]

    asyncio.run(tpl_service.record_lookups(_FakeSession([]), containers))

    assert containers[0].lookup_count == 1
    assert containers[1].lookup_count == 8


def test_record_lookups_is_best_effort_and_never_raises():
    """Analytics must never fail a search — a write error is swallowed and logged."""

    class _Unsettable:
        """Stands in for a container whose ``lookup_count`` write blows up."""

        @property
        def lookup_count(self):
            return 0

        @lookup_count.setter
        def lookup_count(self, value):
            raise RuntimeError("column is read-only in this fake")

    survivor = _container(lookup_count=5)

    # No exception escapes even though the first container's write raises.
    asyncio.run(
        tpl_service.record_lookups(_FakeSession([]), [_Unsettable(), survivor])
    )

    # The failure aborts the (best-effort) loop rather than corrupting state —
    # the search result itself is already built and is what the caller returns.
    assert survivor.lookup_count == 5


# ── AC3 (static): no embedding/chunking anywhere in the template modules ──────

_TEMPLATE_MODULES = (
    "app/models/template_container.py",
    "app/services/template_containers.py",
    "app/services/builtin_tools/templates.py",
    "app/api/v1/template_containers.py",
    # The brand seed and its sanitizer are template modules too.
    "app/services/template_container_seed.py",
    "app/services/template_asset_seed.py",
    "app/services/svg_sanitizer.py",
)

#: Modules and symbols that would mean a template touched the RAG pipeline.
_FORBIDDEN_IMPORT_MODULES = (
    "app.services.vector",
    "app.services.chunking",
    "app.services.hybrid_search",
    "app.services.document_processor",
    "app.models.embedding",
    "pgvector",
)
_FORBIDDEN_NAMES = (
    "enqueue_document_processing",
    "enqueue_documents",
    "make_search_knowledge_fn",
    "store_knowledge",
    "search_knowledge",
    "embed_text",
    "Vector",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{a.name}" for a in node.names)
    return modules


@pytest.mark.parametrize("rel_path", _TEMPLATE_MODULES)
def test_template_modules_never_import_the_vector_pipeline(rel_path):
    """AC3: no embedding or chunking operation can be triggered for a container.

    Enforced structurally rather than behaviourally: if the module cannot even
    import the vector/chunking machinery, no future edit can accidentally
    vectorize a template without this test failing first.
    """
    modules = _imported_modules(BACKEND_ROOT / rel_path)
    offenders = [
        m
        for m in modules
        for bad in _FORBIDDEN_IMPORT_MODULES
        if m == bad or m.startswith(f"{bad}.")
    ]
    assert not offenders, f"{rel_path} imports RAG machinery: {sorted(set(offenders))}"


@pytest.mark.parametrize("rel_path", _TEMPLATE_MODULES)
def test_template_modules_never_reference_rag_entry_points(rel_path):
    """No call to a knowledge-base ingest/search entry point, by name."""
    source = (BACKEND_ROOT / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name:
                called.add(name)
    offenders = sorted(called & set(_FORBIDDEN_NAMES))
    assert not offenders, f"{rel_path} calls RAG entry point(s): {offenders}"


def test_knowledge_base_search_paths_never_reference_template_containers():
    """AC3: the container must not be exposed through any ``search_knowledge`` path."""
    for rel_path in (
        "app/services/vector.py",
        "app/services/builtin_tools/kb.py",
        "app/services/hybrid_search.py",
        "app/services/document_processor.py",
        "app/services/chunking.py",
    ):
        source = (BACKEND_ROOT / rel_path).read_text(encoding="utf-8")
        assert "TemplateContainer" not in source, f"{rel_path} references TemplateContainer"
        assert "template_container" not in source, (
            f"{rel_path} references template_container"
        )


def test_create_and_update_endpoints_do_not_enqueue_document_processing():
    """AC3 again, at the API layer: no ingest enqueue on create/update.

    Checked against the parsed AST rather than the raw text, because the module
    docstring deliberately *names* ``enqueue_document_processing`` to state that
    it is not called.
    """
    tree = ast.parse(
        (BACKEND_ROOT / "app/api/v1/template_containers.py").read_text(encoding="utf-8")
    )
    identifiers = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
    }
    assert "enqueue_document_processing" not in identifiers
    assert "enqueue_documents" not in identifiers
    assert "document_processor" not in _imported_modules(
        BACKEND_ROOT / "app/api/v1/template_containers.py"
    )


# ── Built-in tool contract ────────────────────────────────────────────────────


def _tool_defs():
    from app.services.builtin_tools import build_template_search_tools

    defs, _executor = build_template_search_tools(db=None, user_id="")  # type: ignore[arg-type]
    return {d.name: d for d in defs}


def test_builtin_tool_exposes_search_and_get():
    defs = _tool_defs()
    assert set(defs) == {"search_templates", "get_template"}
    for definition in defs.values():
        assert definition.source == "builtin:design"


def test_tool_descriptions_steer_away_from_semantic_search():
    """Tool-result/description grounding: the agent must not confuse this with RAG."""
    search = _tool_defs()["search_templates"]
    lowered = search.description.lower()
    assert "not a semantic" in lowered or "not a semantic/rag" in lowered
    assert "search_knowledge" in lowered


def test_search_templates_declares_no_required_arguments():
    """Calling it with no filters must be legal — that is the discovery path."""
    assert _tool_defs()["search_templates"].input_schema["required"] == []


def test_get_template_takes_an_optional_template_id():
    """No id = the platform's default brand template: that is the one
    ``generate_presentation`` applies silently, so this is how an agent learns
    which predefined assets it can place without being told an id first."""
    definition = _tool_defs()["get_template"]
    assert definition.input_schema["required"] == []
    assert "default brand template" in definition.description
    assert "what it depicts" in definition.description


def test_template_search_is_a_recognized_builtin_tool_id():
    """Otherwise the agent-builder tool would reject a valid configuration."""
    from app.services.builtin_tools.agent_builder import _VALID_BUILTIN_TOOL_IDS

    assert "template_search" in _VALID_BUILTIN_TOOL_IDS


def test_builtin_tool_catalog_advertises_the_template_sub_tools():
    from app.services.builtin_tool_catalog import build_builtin_tool_catalog

    catalog = build_builtin_tool_catalog()
    # The ``design`` group opens with the former template_search functions.
    names = [entry["name"] for entry in catalog["design"]]
    assert names[:2] == ["search_templates", "get_template"]


# ── Built-in tool behaviour ───────────────────────────────────────────────────


def _run_search_tool(monkeypatch, containers, *, user_id: str, **kwargs) -> str:
    """Invoke ``search_templates`` with the service layer faked out."""
    from app.services.builtin_tools import build_template_search_tools

    calls: dict = {}

    async def _fake_search(db, **params):
        calls["params"] = params
        return containers

    async def _fake_load_assets(db, container_id):
        return [_asset()]

    async def _fake_groups(db, uid):
        return []

    monkeypatch.setattr(tpl_service, "search_containers", _fake_search)
    monkeypatch.setattr(tpl_service, "load_assets", _fake_load_assets)
    monkeypatch.setattr(
        "app.api.v1.helpers.get_user_group_ids", _fake_groups, raising=False
    )
    monkeypatch.setattr(
        "app.core.database.async_session", lambda: _FakeSession([]), raising=False
    )

    _defs, executor = build_template_search_tools(db=None, user_id=user_id)  # type: ignore[arg-type]
    return _call_tool(executor, "search_templates", kwargs), calls


def test_search_tool_returns_metadata_and_asset_references(monkeypatch):
    """AC1 end-to-end through the tool: metadata + asset refs come back."""
    container = _container()
    output, _calls = _run_search_tool(
        monkeypatch, [container], user_id=str(container.user_id), theme="dark"
    )

    assert "Quarterly Deck" in output
    assert str(container.id) in output
    # The deck themes a template can dress are the colour schemes derived from
    # its palette; a palette with no readable background dresses either.
    # The legacy free-text label is NOT echoed: it renders nothing, and quoting
    # it next to the deck themes made the two read alike.
    assert "deck themes supported: dark / light" in output
    assert "theme label" not in output
    assert "aspect ratio: 16:9" in output
    assert "quarterly, executive" in output
    # Layout labels, not the full definitions (the agent is still choosing).
    assert "title-slide" in output and "kpi" in output
    # Asset references with their structured role.
    assert "[font] Inter-Bold.ttf" in output
    # And an explicit statement that no similarity search happened.
    assert "no semantic similarity" in output


def test_search_tool_parses_comma_separated_tags(monkeypatch):
    container = _container()
    _output, calls = _run_search_tool(
        monkeypatch,
        [container],
        user_id=str(container.user_id),
        tags=" quarterly , executive ",
    )
    assert calls["params"]["tags"] == ["quarterly", "executive"]


def test_search_tool_empty_result_explains_the_exact_match_semantics(monkeypatch):
    """Grounding: an over-specified filter is the usual cause, not a missing template."""
    output, _calls = _run_search_tool(
        monkeypatch, [], user_id=str(uuid.uuid4()), kind="deck", theme="light"
    )

    assert "No templates matched" in output
    assert "matched exactly" in output
    # ``theme`` is named in both of its forms - the scheme and the
    # exact legacy label - so the retry advice matches the schema.
    assert "'dark' / 'light'" in output and "legacy label" in output
    assert "fewer filters" in output
    # The applied filters are echoed so the agent can see what it over-specified.
    assert "'deck'" in output and "'light'" in output


def test_search_tool_theme_argument_accepts_both_filter_forms():
    """The tool schema keeps ``theme`` a plain string.

    The filter has two legal forms - a scheme (``dark`` / ``light``) and the
    legacy free-text label, matched exactly - and an ``enum`` of the two
    schemes would make the label form unreachable from the tool while the
    REST route still honours it. The description names both forms so the
    schema, the route and the docs agree on what the parameter accepts.
    """
    tool = _tool_defs()["search_templates"]
    schema = tool.input_schema["properties"]["theme"]
    assert schema["type"] == "string"
    assert "enum" not in schema
    description = schema["description"].lower()
    assert "'dark'" in description and "'light'" in description
    assert "legacy" in description and "label" in description
    # The tool-level text says the same, so no instruction the model reads
    # contradicts the schema and keeps it from issuing a label query.
    assert "legacy label" in tool.description


def test_search_tool_never_leaks_the_storage_key(monkeypatch):
    container = _container()
    output, _calls = _run_search_tool(
        monkeypatch, [container], user_id=str(container.user_id)
    )
    assert "template-asset/abc/def.ttf" not in output
    assert "s3_object_name" not in output


def test_get_template_rejects_a_non_uuid_without_touching_the_db(monkeypatch):
    from app.services.builtin_tools import build_template_search_tools

    def _no_session():
        raise AssertionError("must not open a session for an invalid id")

    monkeypatch.setattr("app.core.database.async_session", _no_session, raising=False)

    _defs, executor = build_template_search_tools(db=None, user_id=str(uuid.uuid4()))  # type: ignore[arg-type]
    result = _run_tool(executor, "get_template", {"template_id": "not-a-uuid"})
    # The rejection is a FAILED call, flagged as such for the trace and the
    # loop's per-tool error counters — it is not a success that merely
    # happens to describe a problem.
    assert result.is_error is True
    assert "must be a valid UUID" in result.content


def _run_get_template(monkeypatch, *, container, assets, default_id, arguments):
    """Invoke ``get_template`` with the default-template resolver and the
    container / asset loads faked out."""
    import app.services.presentations as presentations
    from app.services.builtin_tools import build_template_search_tools

    async def _default(_db):
        return default_id

    async def _fake_load_assets(_db, _container_id):
        return assets

    async def _fake_groups(_db, _uid):
        return []

    async def _no_lookups(_db, _containers):
        return None

    monkeypatch.setattr(presentations, "resolve_default_template_id", _default)
    monkeypatch.setattr(tpl_service, "load_assets", _fake_load_assets)
    monkeypatch.setattr(tpl_service, "record_lookups", _no_lookups)
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", _fake_groups, raising=False)
    session = _FakeSession([_Result(one=container)])
    monkeypatch.setattr("app.core.database.async_session", lambda: session, raising=False)

    _defs, executor = build_template_search_tools(db=None, user_id=str(uuid.uuid4()))  # type: ignore[arg-type]
    return _run_tool(executor, "get_template", arguments), session


def test_get_template_without_an_id_reads_the_default_brand_template(monkeypatch):
    container = _container(is_company_managed=True, user_id=None)
    diagram = _asset(
        filename="xtm-platform.svg",
        content_type="image/svg+xml",
        asset_role="illustration",
        description="XTM platform: the four products around XTM One.",
    )
    result, session = _run_get_template(
        monkeypatch,
        container=container,
        assets=[diagram, _asset()],
        default_id=container.id,
        arguments={},
    )
    assert not result.is_error, result.content
    assert "Quarterly Deck" in result.content
    # The asset line carries the caption the agent picks by AND the file id
    # the ``image`` block takes — a filename alone is not a description.
    assert (
        f"[illustration] xtm-platform.svg (id: {diagram.id}, image/svg+xml, 204,800 bytes)"
        " — XTM platform: the four products around XTM One." in result.content
    )
    assert "[font] Inter-Bold.ttf" in result.content
    # The default id was what the visibility query was scoped to.
    assert str(container.id) in _compiled(session.statements[-1])


def test_get_template_without_an_id_and_no_default_points_at_search(monkeypatch):
    result, session = _run_get_template(
        monkeypatch, container=None, assets=[], default_id=None, arguments={}
    )
    assert not result.is_error
    assert "no default brand template" in result.content
    assert "search_templates" in result.content
    assert session.statements == [], "nothing to look up when there is no default"


# ── get_template narrowed to the assets an agent is picking ──────────


def _brand_assets() -> list[SimpleNamespace]:
    return [
        _asset(filename="filigran-logo.svg", content_type="image/svg+xml", asset_role="logo"),
        _asset(
            filename="splunk.svg",
            content_type="image/svg+xml",
            asset_role="integration_logo",
            description="Splunk - SIEM integration mark.",
        ),
        _asset(
            filename="sekoia.svg",
            content_type="image/svg+xml",
            asset_role="integration_logo",
            description="Sekoia.io - XDR integration mark.",
        ),
        _asset(
            filename="thales.svg",
            content_type="image/svg+xml",
            asset_role="customer_logo",
            description="Thales - customer logo.",
        ),
        _asset(
            filename="orange-cyberdefense.svg",
            content_type="image/svg+xml",
            asset_role="partner_logo",
            description="Orange Cyberdefense - MSSP partner.",
        ),
    ]


def test_get_template_role_filter_lists_only_that_role(monkeypatch):
    """The brand deck now carries every partner / integration / customer logo,
    so an agent building a ``logo_grid`` asks for ONE role instead of
    reading two hundred asset lines."""
    container = _container(is_company_managed=True, user_id=None)
    assets = _brand_assets()
    result, _session = _run_get_template(
        monkeypatch,
        container=container,
        assets=assets,
        default_id=container.id,
        arguments={"role": "integration_logo"},
    )
    assert not result.is_error, result.content
    assert "assets matching role='integration_logo' (2 of 5):" in result.content
    assert "[integration_logo] splunk.svg" in result.content
    assert "[integration_logo] sekoia.svg" in result.content
    assert "thales.svg" not in result.content
    assert "orange-cyberdefense.svg" not in result.content
    # The narrowed reading keeps the template's identity and skips its design:
    # an agent picking logos is not restyling the deck.
    assert "Quarterly Deck" in result.content
    assert "deck themes supported" in result.content
    assert "theme config" not in result.content
    assert "layout definitions" not in result.content


def test_get_template_query_filter_reads_filename_and_caption(monkeypatch):
    container = _container(is_company_managed=True, user_id=None)
    result, _session = _run_get_template(
        monkeypatch,
        container=container,
        assets=_brand_assets(),
        default_id=container.id,
        arguments={"query": "  SIEM "},
    )
    assert not result.is_error, result.content
    # Case-insensitive over the filename AND the caption: 'SIEM' only appears in
    # Splunk's description.
    assert "assets matching query='SIEM' (1 of 5):" in result.content
    assert "splunk.svg" in result.content
    assert "sekoia.svg" not in result.content

    result, _session = _run_get_template(
        monkeypatch,
        container=container,
        assets=_brand_assets(),
        default_id=container.id,
        arguments={"role": "partner_logo", "query": "orange"},
    )
    assert "assets matching role='partner_logo', query='orange' (1 of 5):" in result.content
    assert "orange-cyberdefense.svg" in result.content


def test_get_template_filter_with_no_match_says_what_the_template_holds(monkeypatch):
    container = _container(is_company_managed=True, user_id=None)
    result, _session = _run_get_template(
        monkeypatch,
        container=container,
        assets=_brand_assets(),
        default_id=container.id,
        arguments={"role": "customer_logo", "query": "acme"},
    )
    assert not result.is_error, result.content
    # Not "no assets": the counts per role tell the agent what IS there, so the
    # next call is a better filter rather than a guess.
    assert "assets matching role='customer_logo', query='acme': none of 5" in result.content
    assert "1 logo" in result.content and "2 integration_logo" in result.content
    assert "Widen the query or drop the role filter" in result.content


def _asset_wall(count_by_role: dict[str, int]) -> list[SimpleNamespace]:
    """*count_by_role* assets per role, each with a filename and a description."""
    assets = []
    for role, count in count_by_role.items():
        for index in range(count):
            assets.append(
                _asset(
                    filename=f"{role}-{index:03d}.svg",
                    content_type="image/svg+xml",
                    asset_role=role,
                    description=f"{role.replace('_', ' ')} number {index} - what it depicts.",
                )
            )
    return assets


_BRAND_SIZED_WALL = {
    "logo": 2,
    "logo_light": 1,
    "logo_mark": 1,
    "product_logo": 6,
    "partner_logo": 42,
    "integration_logo": 66,
    "customer_logo": 24,
    "illustration": 9,
    "diagram": 12,
    "screenshot": 15,
    "badge": 2,
}


def test_get_template_without_a_filter_is_bounded_on_a_brand_sized_template(monkeypatch):
    """The brand deck carries ~180 predefined assets. Unfiltered,
    ``get_template`` used to list every one of them - tens of thousands of
    characters for an agent that wanted the palette and one diagram. Above
    ``_DETAIL_ASSET_LINES`` the LARGEST roles collapse to a count in the header
    and one ``role=`` pointer, biggest first, until what remains fits; the small
    roles an agent picks single assets from (marks, diagrams, screenshots) stay
    listed in full, and the logo walls - placed by role in a ``logo_grid`` - are
    one filtered call away."""
    from app.services.builtin_tools import templates as tool_mod

    container = _container(is_company_managed=True, user_id=None)
    assets = _asset_wall(_BRAND_SIZED_WALL)
    assert len(assets) == 180
    result, _session = _run_get_template(
        monkeypatch, container=container, assets=assets, default_id=container.id, arguments={}
    )
    assert not result.is_error, result.content
    asset_lines = [line for line in result.content.splitlines() if line.startswith("    - [")]
    assert 0 < len(asset_lines) <= tool_mod._DETAIL_ASSET_LINES, len(asset_lines)
    assert len(result.content) < 12_000, len(result.content)

    # The header still says what the template holds, per role, in catalog order.
    assert (
        "assets (180): 2 logo, 1 logo_light, 1 logo_mark, 6 product_logo, "
        "42 partner_logo, 66 integration_logo, 24 customer_logo, 9 illustration, "
        "12 diagram, 15 screenshot, 2 badge"
    ) in result.content
    # The small roles are listed in full, description and file id included.
    for role in ("logo", "logo_light", "logo_mark", "product_logo", "illustration", "diagram", "screenshot", "badge"):
        listed = [line for line in asset_lines if line.startswith(f"    - [{role}]")]
        assert len(listed) == _BRAND_SIZED_WALL[role], role
    assert "diagram-000.svg" in result.content and "diagram number 0 - what it depicts." in result.content
    # The three logo walls are collapsed - not one line each - behind a pointer
    # that names the call listing one of them.
    for role in ("partner_logo", "integration_logo", "customer_logo"):
        assert not any(line.startswith(f"    - [{role}]") for line in asset_lines), role
    assert "42 partner_logo, 66 integration_logo, 24 customer_logo not listed" in result.content
    assert f'get_template(template_id="{container.id}", role="partner_logo")' in result.content
    assert 'query=' in result.content
    # The design is still served: this is the unfiltered reading.
    assert "theme config" in result.content
    assert "layout definitions" in result.content


def test_get_template_serves_the_guidance_whole_on_its_own_line(monkeypatch):
    """``extra_metadata.guidance`` is the prose a composing agent reads.
    It used to travel only inside the compact-JSON dump of ``extra_metadata``,
    which ``get_template`` cuts at ``_EXTRA_METADATA_BUDGET`` - the brand deck's
    palettes alone pushed its guidance past the cut, mid-sentence. It is now a
    line of its own, served whole, and the JSON dump carries the rest."""
    from app.services.builtin_tools import templates as tool_mod

    guidance = "House style rule. " + " ".join(f"Rule {i} of the house style." for i in range(60))
    assert len(guidance) < tool_mod._GUIDANCE_BUDGET
    palettes = {f"variant_{i}": {f"key_{j}": f"#{i:02x}{j:02x}00" for j in range(12)} for i in range(8)}
    extra = {"supported_schemes": ["dark"], "theme_variants": palettes, "guidance": guidance, "brand": {"a": 1}}
    assert len(json.dumps(extra, separators=(",", ":"))) > tool_mod._EXTRA_METADATA_BUDGET
    container = _container(is_company_managed=True, user_id=None, extra_metadata=extra)
    result, _session = _run_get_template(
        monkeypatch, container=container, assets=[_asset()], default_id=container.id, arguments={}
    )
    assert not result.is_error, result.content
    lines = result.content.splitlines()
    assert f"  - guidance: {guidance}" in lines, "the guidance is one whole line"
    (metadata_line,) = [line for line in lines if line.startswith("  - extra metadata: ")]
    assert "guidance" not in metadata_line, "the guidance is not dumped twice"
    assert '"brand"' in metadata_line or "truncated" in metadata_line
    # The order a composing agent reads in: what the deck is, what it holds,
    # how to compose in it, then the design values.
    assert lines.index(f"  - guidance: {guidance}") < lines.index(
        next(line for line in lines if line.startswith("  - theme config: "))
    )


def test_get_template_bounds_a_runaway_guidance_and_keeps_an_odd_one_in_the_metadata(monkeypatch):
    from app.services.builtin_tools import templates as tool_mod

    runaway = "x" * (tool_mod._GUIDANCE_BUDGET + 500)
    container = _container(is_company_managed=True, user_id=None, extra_metadata={"guidance": runaway})
    result, _session = _run_get_template(
        monkeypatch, container=container, assets=[_asset()], default_id=container.id, arguments={}
    )
    (guidance_line,) = [line for line in result.content.splitlines() if line.startswith("  - guidance: ")]
    assert guidance_line.endswith(f"… (truncated, {len(runaway)} chars total)")
    assert len(guidance_line) < len(runaway)
    assert "extra metadata" not in result.content, "nothing left once the guidance is lifted out"

    # A guidance that is not prose (a list, a blank) stays where it is stored,
    # for the agent to see as it is - and a non-dict metadata is served as is.
    for odd in ({"guidance": ["a", "b"], "k": 1}, {"guidance": "   ", "k": 1}, ["legacy", "list"]):
        container = _container(is_company_managed=True, user_id=None, extra_metadata=odd)
        result, _session = _run_get_template(
            monkeypatch, container=container, assets=[_asset()], default_id=container.id, arguments={}
        )
        assert "  - guidance: " not in result.content, odd
        assert f"  - extra metadata: {json.dumps(odd, ensure_ascii=False, separators=(',', ':'))}" in result.content


def test_get_template_lists_every_asset_of_a_template_within_the_ceiling(monkeypatch):
    from app.services.builtin_tools import templates as tool_mod

    container = _container(is_company_managed=True, user_id=None)
    assets = _asset_wall({"logo": 2, "diagram": 20, "screenshot": 20, "integration_logo": 18})
    assert len(assets) == tool_mod._DETAIL_ASSET_LINES
    result, _session = _run_get_template(
        monkeypatch, container=container, assets=assets, default_id=container.id, arguments={}
    )
    assert not result.is_error, result.content
    asset_lines = [line for line in result.content.splitlines() if line.startswith("    - [")]
    assert len(asset_lines) == len(assets)
    assert "not listed" not in result.content
    assert f"assets ({len(assets)}):" in result.content


def test_get_template_collapses_the_biggest_roles_first_until_the_rest_fits(monkeypatch):
    """A single oversized role must not drag the small ones down with it: only
    the roles it takes to get under the ceiling are collapsed, biggest first."""
    from app.services.builtin_tools import templates as tool_mod

    container = _container(is_company_managed=True, user_id=None)
    assets = _asset_wall({"logo": 1, "customer_logo": 200, "diagram": 4})
    result, _session = _run_get_template(
        monkeypatch, container=container, assets=assets, default_id=container.id, arguments={}
    )
    assert not result.is_error, result.content
    asset_lines = [line for line in result.content.splitlines() if line.startswith("    - [")]
    assert len(asset_lines) == 5 <= tool_mod._DETAIL_ASSET_LINES
    assert "200 customer_logo not listed" in result.content
    assert f'get_template(template_id="{container.id}", role="customer_logo")' in result.content
    # The filtered reading of the collapsed role is unchanged: every one of them.
    result, _session = _run_get_template(
        monkeypatch,
        container=container,
        assets=assets,
        default_id=container.id,
        arguments={"role": "customer_logo"},
    )
    assert "assets matching role='customer_logo' (200 of 205):" in result.content
    assert result.content.count("    - [customer_logo]") == 200


def test_get_template_breaks_a_size_tie_towards_the_marks_and_the_query_only_groups(monkeypatch):
    """Two groups of the same size collapse in a fixed order, not in whatever
    order the roles were first seen: the catalog role later in the catalog goes
    before the brand marks at its head, and a catalog role (one ``role=`` call
    away) goes before an unlabelled group that only ``query=`` can reach - so
    the assets an agent picks one by one are the last to leave the listing."""
    container = _container(is_company_managed=True, user_id=None)

    # logo (30) ties customer_logo (30): the wall collapses, the marks stay.
    result, _session = _run_get_template(
        monkeypatch,
        container=container,
        assets=_asset_wall({"logo": 30, "customer_logo": 30, "diagram": 3}),
        default_id=container.id,
        arguments={},
    )
    assert not result.is_error, result.content
    asset_lines = [line for line in result.content.splitlines() if line.startswith("    - [")]
    assert len(asset_lines) == 33
    assert sum(line.startswith("    - [logo]") for line in asset_lines) == 30
    assert "30 customer_logo not listed" in result.content
    assert f'get_template(template_id="{container.id}", role="customer_logo")' in result.content

    # customer_logo (30) ties an unlabelled group (30): the catalog role
    # collapses - it is one role= call away - and the unlabelled group, which
    # only query= reaches, stays listed.
    unlabelled = [
        _asset(filename=f"scan-{index:03d}.png", content_type="image/png", asset_role=None, description=None)
        for index in range(30)
    ]
    result, _session = _run_get_template(
        monkeypatch,
        container=container,
        assets=[*_asset_wall({"customer_logo": 30, "diagram": 3}), *unlabelled],
        default_id=container.id,
        arguments={},
    )
    assert not result.is_error, result.content
    asset_lines = [line for line in result.content.splitlines() if line.startswith("    - [")]
    assert len(asset_lines) == 33
    assert sum(line.startswith("    - [unlabelled]") for line in asset_lines) == 30
    assert "30 customer_logo not listed" in result.content
    assert 'role="unlabelled"' not in result.content


def test_get_template_never_points_at_a_role_filter_the_tool_would_refuse(monkeypatch):
    """``role=`` takes a catalog role only (the schema enum, checked before the
    DB is touched), while an asset may carry no role at all or a custom label the
    REST API stored. A collapsed group of those must not be advertised behind a
    ``role="unlabelled"`` that fails validation: it is reachable by ``query=``,
    and that is what the pointer says."""
    container = _container(is_company_managed=True, user_id=None)
    unlabelled = [
        _asset(filename=f"scan-{index:03d}.png", content_type="image/png", asset_role=None, description=None)
        for index in range(90)
    ]
    custom = [
        _asset(filename=f"mockup-{index:03d}.png", content_type="image/png", asset_role="mockup", description=None)
        for index in range(70)
    ]
    small = _asset_wall({"logo": 1, "diagram": 3})

    # Only non-catalog groups collapse: the pointer offers query= alone.
    result, _session = _run_get_template(
        monkeypatch, container=container, assets=[*unlabelled, *small], default_id=container.id, arguments={}
    )
    assert not result.is_error, result.content
    assert "90 unlabelled not listed" in result.content
    assert 'role="unlabelled"' not in result.content
    assert f'get_template(template_id="{container.id}", query="unlabelled") lists them by label' in result.content
    assert "role= takes catalog roles only" in result.content
    assert result.content.count("    - [") == 4, "the small catalog roles stay listed"

    # A catalog role AND a custom one collapse: role= names the catalog one, the
    # custom label is pointed at query=.
    result, _session = _run_get_template(
        monkeypatch,
        container=container,
        assets=[*custom, *_asset_wall({"customer_logo": 80}), *small],
        default_id=container.id,
        arguments={},
    )
    assert not result.is_error, result.content
    assert "80 customer_logo, 70 mockup not listed" in result.content
    assert f'get_template(template_id="{container.id}", role="customer_logo") lists one role' in result.content
    assert 'role="mockup"' not in result.content
    assert '(query="mockup" the rest: role= takes catalog roles)' in result.content


def test_the_query_pointer_of_a_collapsed_custom_group_recovers_it_whatever_its_files_are_called(
    monkeypatch,
):
    """The fallback the pointer advertises has to work: a custom label is not a
    filename, so ``[mockup] slide-001.png`` would be unreachable if ``query=``
    searched names and descriptions only. The label is part of the haystack, so
    the exact call the pointer names lists the whole group - and the same holds
    for an unlabelled group behind ``query="unlabelled"``."""
    container = _container(is_company_managed=True, user_id=None)
    custom = [
        _asset(filename=f"slide-{index:03d}.png", content_type="image/png", asset_role="mockup", description=None)
        for index in range(70)
    ]
    unlabelled = [
        _asset(filename=f"scan-{index:03d}.png", content_type="image/png", asset_role=None, description=None)
        for index in range(65)
    ]
    assets = [*custom, *unlabelled, *_asset_wall({"logo": 1, "diagram": 3})]

    result, _session = _run_get_template(
        monkeypatch, container=container, assets=assets, default_id=container.id, arguments={}
    )
    assert not result.is_error, result.content
    # Non-catalog groups keep the listing's filename order (scan- before slide-).
    assert "65 unlabelled, 70 mockup not listed" in result.content
    pointer = f'get_template(template_id="{container.id}", query="unlabelled" / query="mockup") lists them by label'
    assert pointer in result.content

    for label, size, prefix in (("mockup", 70, "slide-"), ("unlabelled", 65, "scan-")):
        result, _session = _run_get_template(
            monkeypatch,
            container=container,
            assets=assets,
            default_id=container.id,
            arguments={"query": label},
        )
        assert not result.is_error, result.content
        assert f"assets matching query='{label}' ({size} of {len(assets)}):" in result.content
        assert result.content.count(f"    - [{label}] {prefix}") == size

    # A word of a filename or description still narrows as before.
    result, _session = _run_get_template(
        monkeypatch, container=container, assets=assets, default_id=container.id, arguments={"query": "slide-00"}
    )
    assert f"assets matching query='slide-00' (10 of {len(assets)}):" in result.content


def test_get_template_refuses_an_unknown_role_without_touching_the_db(monkeypatch):
    from app.services.builtin_tools import build_template_search_tools

    def _no_session():
        raise AssertionError("must not open a session for an unknown role")

    monkeypatch.setattr("app.core.database.async_session", _no_session, raising=False)

    _defs, executor = build_template_search_tools(db=None, user_id=str(uuid.uuid4()))  # type: ignore[arg-type]
    result = _run_tool(executor, "get_template", {"role": "sponsor_logo"})
    assert result.is_error is True
    assert "role must be one of" in result.content
    assert "integration_logo" in result.content
    assert "'sponsor_logo'" in result.content


def test_get_template_schema_offers_the_asset_filters():
    from app.models.template_container import ASSET_ROLES

    properties = _tool_defs()["get_template"].input_schema["properties"]
    assert properties["role"]["enum"] == list(ASSET_ROLES)
    assert "integration_logo" in properties["role"]["enum"]
    assert properties["query"]["type"] == "string"


def test_search_tool_kind_argument_is_the_closed_choice():
    """A template dresses a presentation, a report or a video:
    the schema offers exactly those, so the agent never filters on a kind that
    cannot exist."""
    from app.services.template_containers import TEMPLATE_KINDS

    schema = _tool_defs()["search_templates"].input_schema["properties"]["kind"]
    assert schema["enum"] == list(TEMPLATE_KINDS) == ["presentation", "report", "video"]
    description = _tool_defs()["search_templates"].description
    assert "asset-library" not in description and "asset library" not in description
    assert "dashboard" not in description


def test_a_legacy_non_presentation_row_is_described_generically(monkeypatch):
    """A row created under the retired open ``kind`` still reads, and the agent is
    told the one thing that matters: it dresses no deck."""
    container = _container(kind="asset-library", supported_schemes=[])
    output, _calls = _run_search_tool(
        monkeypatch, [container], user_id=str(container.user_id)
    )
    assert "not a presentation template (kind: asset-library)" in output
    assert "never pass its id as template_id" in output
    assert "deck themes supported" not in output


def test_tool_visibility_has_no_admin_bypass():
    """An admin's agent must not surface other users' private templates.

    Asserted on BOTH signatures — the tool builder and the service function it
    calls. A dormant ``is_admin=False`` parameter on the service would be a lever
    a future caller flips without re-deciding, so neither may accept one.
    """
    import inspect

    from app.services.builtin_tools import build_template_search_tools

    assert "is_admin" not in inspect.signature(build_template_search_tools).parameters
    assert "is_admin" not in inspect.signature(tpl_service.search_containers).parameters

    source = (
        BACKEND_ROOT / "app/services/builtin_tools/templates.py"
    ).read_text(encoding="utf-8")
    assert "is_admin=" not in source


def test_search_containers_query_has_no_admin_bypass_branch():
    """Belt to the signature check: the emitted SQL is always tier-filtered.

    ``user_visible_filter(..., is_admin=True)`` degenerates to a true-predicate
    (no filtering at all), so the regression to guard against is a query with no
    ownership/company-managed predicate in it.
    """
    session = _FakeSession([_Result(rows=[])])
    user_id = uuid.uuid4()

    async def _run():
        await tpl_service.search_containers(
            session, user_id=user_id, user_group_ids=[]
        )
        return _compiled(session.statements[-1])

    sql = asyncio.run(_run())
    assert "WHERE" in sql
    # Both tiers a logged-in caller is entitled to are present as predicates.
    assert f"template_containers.user_id = '{user_id}'" in sql
    assert "template_containers.is_company_managed" in sql.split("WHERE")[-1]


# ── AC2: asset independence ───────────────────────────────────────────────────


def _api_client(user, *, session_factory=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import deps
    from app.api.v1 import template_containers as api_mod
    from app.core.database import get_db

    app = FastAPI()
    app.include_router(api_mod.router, prefix="/api/v1/template-containers")
    app.dependency_overrides[deps.get_current_user] = lambda: user
    # The Agentic AI licence gate reads PlatformSettings from a real DB; these
    # tests exercise the route logic, not the licence check.
    app.dependency_overrides[deps.require_agentic_ai] = lambda: None
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None
    factory = session_factory or (lambda: _FakeSession([]))
    app.dependency_overrides[get_db] = factory
    return TestClient(app), api_mod


def _user(is_admin: bool = False, user_id: uuid.UUID | None = None):
    return SimpleNamespace(
        id=user_id or uuid.uuid4(),
        is_admin=is_admin,
        role="admin" if is_admin else "user",
        full_name="Test User",
    )


def test_list_endpoint_is_three_tier_for_admins_too():
    """``GET /template-containers`` is the library list: three-tier for an admin
    too, unless the admin asks for ``scope=all``.

    The Templates library carries the same ``Mine`` / ``Shared`` / ``Company``
    pills as every other library page, so an admin must get the same own +
    shared + company-managed predicate a regular user gets - not the SQL
    ``TRUE`` that ``is_admin=True`` degenerates to, which listed every user's
    private template. The detail route keeps the bypass (that is how a
    template found in Settings > Objects opens), and so does ``scope=all``,
    which the deck's Design panel reads for the template a colleague's deck is
    linked to; a non-admin's ``scope=all`` is their library set.
    """

    def _list_sql(user, query: str = "") -> str:
        session = _FakeSession([_Result(rows=[]), _Result(rows=[])])
        client, _ = _api_client(user, session_factory=lambda: session)
        response = client.get(f"/api/v1/template-containers{query}")
        assert response.status_code == 200, response.text
        assert response.json() == []
        # statements[0] is the caller's group lookup; [1] is the list query.
        return _compiled(session.statements[1])

    admin = _user(is_admin=True)
    admin_sql = _list_sql(admin)
    # Before the fix this read ``WHERE true``.
    where = admin_sql.split("WHERE", 1)[1]
    assert f"template_containers.user_id = '{admin.id}'" in where
    assert "template_containers.is_company_managed" in where
    assert "shared_user_ids" in where

    member = _user()
    member_sql = _list_sql(member)
    assert member_sql.replace(str(member.id), "<caller>") == admin_sql.replace(str(admin.id), "<caller>")

    # ``scope=all``: the whole platform for an admin, nothing wider for anyone else.
    assert _list_sql(admin, "?scope=all").split("WHERE", 1)[1].split("ORDER BY")[0].strip() == "true"
    assert _list_sql(member, "?scope=all") == member_sql

    # The detail read is where the admin bypass lives, and stays; the list's
    # only bypass is the scope.
    detail_source = (BACKEND_ROOT / "app/api/v1/template_containers.py").read_text(encoding="utf-8")
    detail_body = detail_source.split("async def _get_visible_container", 1)[1].split("\n\n\n", 1)[0]
    assert "is_admin=user.is_admin" in detail_body
    list_body = detail_source.split("async def list_template_containers", 1)[1].split("\n\n\n", 1)[0]
    assert list_body.count("is_admin=") == 1
    assert "is_admin=list_scope_admin_bypass(scope, user)" in list_body


@pytest.fixture
def asset_api(monkeypatch):
    """Client + captured state for the asset endpoints, with storage faked out."""
    user = _user()
    container = _container(user_id=user.id)
    state = {
        "container": container,
        "assets": [],
        "uploaded": [],
        "deleted": [],
        # Snapshot of the container's metadata so a test can prove the asset
        # operations never rewrote it.
        "metadata_before": {
            "name": container.name,
            "kind": container.kind,
            "tags": list(container.tags),
            "theme": container.theme,
            "theme_config": dict(container.theme_config),
            "aspect_ratio": container.aspect_ratio,
            "layouts": list(container.layouts),
            "extra_metadata": dict(container.extra_metadata),
        },
    }

    # ``add()`` stamps the id/created_at a real flush would, so the response can
    # be serialized without a DB, and records the row in ``state["assets"]``.
    class _AddingSession(_FakeSession):
        def add(self, obj):
            obj.id = getattr(obj, "id", None) or uuid.uuid4()
            obj.created_at = datetime.now(UTC)
            state["assets"].append(obj)

    client, api_mod = _api_client(user, session_factory=lambda: _AddingSession([]))

    async def _get_manageable(db, model_cls, u, entity_id, **k):
        return state["container"]

    async def _load_assets(db, container_id):
        return list(state["assets"])

    async def _upload(data, filename, content_type=None, prefix=""):
        object_name = f"{prefix}/{uuid.uuid4().hex}"
        state["uploaded"].append(object_name)
        return object_name

    async def _delete_object(object_name):
        state["deleted"].append(object_name)

    async def _delete_attachments(db, **filters):
        removed = [a for a in state["assets"] if a.id == filters.get("id")]
        for a in removed:
            state["assets"].remove(a)
            state["deleted"].append(a.s3_object_name)
        return len(removed)

    async def _log_audit(*a, **k):
        return None

    async def _get_asset(db, container, asset_id):
        from fastapi import HTTPException

        for candidate in state["assets"]:
            if str(candidate.id) == str(asset_id):
                return candidate
        raise HTTPException(status_code=404, detail="Template asset not found")

    monkeypatch.setattr(api_mod, "get_manageable_entity", _get_manageable)
    monkeypatch.setattr(api_mod, "load_assets", _load_assets)
    monkeypatch.setattr(api_mod, "log_audit", _log_audit)
    monkeypatch.setattr(api_mod, "_get_asset", _get_asset)
    monkeypatch.setattr(tpl_service, "load_assets", _load_assets)

    import app.services.file_cleanup as cleanup_mod
    import app.services.file_storage as storage_mod

    monkeypatch.setattr(storage_mod.file_storage, "upload", _upload)
    monkeypatch.setattr(storage_mod.file_storage, "delete", _delete_object)
    monkeypatch.setattr(cleanup_mod, "delete_file_attachments", _delete_attachments)
    return client, state


def test_adding_an_asset_leaves_container_metadata_untouched(asset_api):
    """AC2: add reflects in the asset list without recreating other metadata."""
    client, state = asset_api

    resp = client.post(
        f"/api/v1/template-containers/{state['container'].id}/assets"
        "?asset_role=Font",
        files={"file": ("Inter-Bold.ttf", io.BytesIO(b"font-bytes"), "font/ttf")},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["filename"] == "Inter-Bold.ttf"
    # Role labels are normalized to lower case so filtering stays predictable.
    assert body["asset_role"] == "font"
    assert len(state["assets"]) == 1
    assert len(state["uploaded"]) == 1

    container = state["container"]
    for field, value in state["metadata_before"].items():
        assert getattr(container, field) == value, f"{field} was rewritten by an upload"


def test_asset_upload_only_needs_the_file(asset_api):
    """No container metadata is required in the request body."""
    client, state = asset_api
    resp = client.post(
        f"/api/v1/template-containers/{state['container'].id}/assets",
        files={"file": ("logo.png", io.BytesIO(b"png"), "image/png")},
    )
    assert resp.status_code == 201
    assert resp.json()["asset_role"] is None


def test_empty_upload_is_rejected(asset_api):
    client, state = asset_api
    resp = client.post(
        f"/api/v1/template-containers/{state['container'].id}/assets",
        files={"file": ("empty.ttf", io.BytesIO(b""), "font/ttf")},
    )
    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"].lower()


def test_asset_count_cap_is_enforced(asset_api, monkeypatch):
    client, state = asset_api
    state["assets"] = [_asset() for _ in range(MAX_ASSETS_PER_CONTAINER)]

    resp = client.post(
        f"/api/v1/template-containers/{state['container'].id}/assets",
        files={"file": ("one-too-many.png", io.BytesIO(b"png"), "image/png")},
    )
    assert resp.status_code == 409
    assert str(MAX_ASSETS_PER_CONTAINER) in resp.json()["detail"]
    # Nothing was uploaded to storage once the cap refused the request.
    assert state["uploaded"] == []


def test_replacing_an_asset_preserves_its_id_and_drops_the_old_object(asset_api):
    """AC2: replace swaps the binary without invalidating stored references."""
    client, state = asset_api
    existing = _asset()
    state["assets"] = [existing]
    original_id = existing.id
    original_object = existing.s3_object_name

    resp = client.put(
        f"/api/v1/template-containers/{state['container'].id}/assets/{existing.id}",
        files={"file": ("Inter-Regular.ttf", io.BytesIO(b"new-font"), "font/ttf")},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The id is stable — a layout referencing this asset stays valid.
    assert body["id"] == str(original_id)
    assert body["filename"] == "Inter-Regular.ttf"
    assert body["size"] == len(b"new-font")
    # Old binary removed only after the new object name was committed.
    assert original_object in state["deleted"]
    assert existing.s3_object_name in state["uploaded"]

    container = state["container"]
    for field, value in state["metadata_before"].items():
        assert getattr(container, field) == value, f"{field} was rewritten by a replace"


def test_a_replaced_asset_is_served_fresh_on_the_next_request(asset_api, monkeypatch):
    """A replace keeps the asset id, so its download URL does not change.

    The download is revalidated on every use (``private, no-cache``) against a
    strong ``ETag``; the ETag a client held before the replace gets a ``200``
    with the new bytes afterwards, never a ``304``, and ``version`` in the
    asset payload moves with it. Same file name, type and size on both sides:
    only the stored object changed, which is all a replace changes.
    """
    import app.services.file_storage as storage_mod
    from app.api.v1 import template_containers as api_mod

    client, state = asset_api
    blobs: dict[str, bytes] = {}
    reads: list[str] = []

    async def _upload(data, filename, content_type=None, prefix=""):
        key = f"{prefix}/{uuid.uuid4().hex}"
        blobs[key] = data
        state["uploaded"].append(key)
        return key

    async def _download(key):
        reads.append(key)
        return blobs[key]

    async def _visible(db, user, container_id):
        return state["container"]

    monkeypatch.setattr(storage_mod.file_storage, "upload", _upload)
    monkeypatch.setattr(storage_mod.file_storage, "download", _download)
    monkeypatch.setattr(api_mod, "_get_visible_container", _visible)
    base = f"/api/v1/template-containers/{state['container'].id}/assets"

    created = client.post(base, files={"file": ("logo.png", io.BytesIO(b"old-logo"), "image/png")})
    assert created.status_code == 201, created.text
    asset_id = created.json()["id"]
    url = f"{base}/{asset_id}/download"

    first = client.get(url)
    assert first.status_code == 200
    assert first.content == b"old-logo"
    assert first.headers["cache-control"] == "private, no-cache"
    assert first.headers["vary"] == "Authorization"
    assert first.headers["x-content-type-options"] == "nosniff"
    # Still a download: the Templates page names the saved file from it.
    assert first.headers["content-disposition"].startswith("attachment;")
    held = first.headers["etag"]
    assert held.strip('"') == created.json()["version"]

    again = client.get(url, headers={"If-None-Match": held})
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == held
    assert len(reads) == 1, "a 304 must not read the object store"

    replaced = client.put(
        f"{base}/{asset_id}", files={"file": ("logo.png", io.BytesIO(b"new-logo"), "image/png")}
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["id"] == asset_id
    assert replaced.json()["size"] == created.json()["size"]
    assert replaced.json()["version"] != created.json()["version"]

    fresh = client.get(url, headers={"If-None-Match": held})
    assert fresh.status_code == 200
    assert fresh.content == b"new-logo"
    assert fresh.headers["etag"] != held
    assert fresh.headers["etag"].strip('"') == replaced.json()["version"]


def test_replacing_an_asset_records_the_digest_of_its_new_bytes(asset_api):
    """``sha256`` describes the object the row names. The presentation
    package import trusts it to tell a template's same-name assets apart without
    reading them, so a replace keeping the previous digest would let an import
    take this row for the file it no longer holds."""
    client, state = asset_api
    existing = _asset(sha256=hashlib.sha256(b"old-font").hexdigest())
    state["assets"] = [existing]

    resp = client.put(
        f"/api/v1/template-containers/{state['container'].id}/assets/{existing.id}",
        files={"file": ("Inter-Regular.ttf", io.BytesIO(b"new-font"), "font/ttf")},
    )

    assert resp.status_code == 200, resp.text
    assert existing.sha256 == hashlib.sha256(b"new-font").hexdigest()


def test_an_uploaded_asset_records_the_digest_of_the_bytes_it_stored(asset_api):
    """The digest is of what storage holds - the SANITIZED SVG - never of the
    upload, which no object holds once the sanitizer rewrote it."""
    from app.services.svg_sanitizer import sanitize_template_asset

    client, state = asset_api
    raw = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><rect/></svg>'
    stored = sanitize_template_asset(raw, "image/svg+xml", "logo.svg")
    assert stored != raw, "the case needs an upload the sanitizer rewrites"

    resp = client.post(
        f"/api/v1/template-containers/{state['container'].id}/assets",
        files={"file": ("logo.svg", io.BytesIO(raw), "image/svg+xml")},
    )

    assert resp.status_code == 201, resp.text
    (row,) = state["assets"]
    assert row.sha256 == hashlib.sha256(stored).hexdigest()


def test_replacing_keeps_the_existing_role_when_none_is_supplied(asset_api):
    client, state = asset_api
    existing = _asset(asset_role="background")
    state["assets"] = [existing]

    resp = client.put(
        f"/api/v1/template-containers/{state['container'].id}/assets/{existing.id}",
        files={"file": ("bg2.png", io.BytesIO(b"png"), "image/png")},
    )
    assert resp.status_code == 200
    assert resp.json()["asset_role"] == "background"


def test_relabelling_an_asset_does_not_touch_its_binary(asset_api):
    client, state = asset_api
    existing = _asset(asset_role="image", description="Keep me.")
    state["assets"] = [existing]

    resp = client.patch(
        f"/api/v1/template-containers/{state['container'].id}/assets/{existing.id}",
        json={"asset_role": "Logo"},
    )

    assert resp.status_code == 200
    assert resp.json()["asset_role"] == "logo"
    # Patch semantics: a body that only names the role leaves the
    # description alone — relabelling must not wipe the caption.
    assert resp.json()["description"] == "Keep me."
    assert state["uploaded"] == []


def test_asset_description_is_stored_normalized_and_cleared_explicitly(asset_api):
    """The caption an agent picks the asset by: one line, bounded,
    settable at upload, re-describable alone, and cleared only by ``null``."""
    client, state = asset_api
    base = f"/api/v1/template-containers/{state['container'].id}/assets"

    resp = client.post(
        base + "?description=" + "OpenAEV%20architecture%3A%0A%20%20agents%20and%20collectors.",
        files={"file": ("openaev-architecture.svg", io.BytesIO(b"<svg xmlns='http://www.w3.org/2000/svg'/>"), "image/svg+xml")},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["description"] == "OpenAEV architecture: agents and collectors."
    asset_id = resp.json()["id"]

    resp = client.patch(f"{base}/{asset_id}", json={"description": "  Reworded.  "})
    assert resp.status_code == 200
    assert resp.json()["description"] == "Reworded."
    assert resp.json()["asset_role"] is None, "re-describing alone leaves the role alone"

    resp = client.patch(f"{base}/{asset_id}", json={"description": None})
    assert resp.status_code == 200
    assert resp.json()["description"] is None

    too_long = "x" * (MAX_ASSET_DESCRIPTION_CHARS + 1)
    resp = client.patch(f"{base}/{asset_id}", json={"description": too_long})
    assert resp.status_code == 422, "the bound is enforced at the schema, not silently truncated"
    assert state["deleted"] == []


def test_removing_an_asset_leaves_the_container_and_its_metadata_intact(asset_api):
    """AC2: remove reflects in the asset list; the container survives untouched."""
    client, state = asset_api
    existing = _asset()
    state["assets"] = [existing]

    resp = client.delete(
        f"/api/v1/template-containers/{state['container'].id}/assets/{existing.id}"
    )

    assert resp.status_code == 204
    assert state["assets"] == []
    assert existing.s3_object_name in state["deleted"]

    container = state["container"]
    for field, value in state["metadata_before"].items():
        assert getattr(container, field) == value, f"{field} was rewritten by a delete"


def test_asset_lookup_is_scoped_to_its_container(asset_api):
    """An asset id from another container must 404, not leak across containers."""
    client, state = asset_api
    state["assets"] = []  # the requested asset is not in this container

    resp = client.delete(
        f"/api/v1/template-containers/{state['container'].id}/assets/{uuid.uuid4()}"
    )
    assert resp.status_code == 404


# ── Company-managed gate ──────────────────────────────────────────────────────


def test_non_admin_cannot_create_a_company_managed_container(monkeypatch):
    client, api_mod = _api_client(_user(is_admin=False))

    async def _log_audit(*a, **k):
        return None

    monkeypatch.setattr(api_mod, "log_audit", _log_audit)

    resp = client.post(
        "/api/v1/template-containers",
        json={"name": "Corporate Deck", "is_company_managed": True},
    )
    assert resp.status_code == 403
    assert "admin" in resp.json()["detail"].lower()


# ── The closed kind and the scheme declaration on the REST surface ───


def _creating_client(monkeypatch):
    """Client whose session stamps what a real INSERT would, so ``POST`` can
    return the created row without a database."""
    user = _user()
    created: list = []

    class _CreatingSession(_FakeSession):
        def add(self, obj):
            now = datetime.now(UTC)
            obj.id = getattr(obj, "id", None) or uuid.uuid4()
            obj.created_at = now
            obj.updated_at = now
            obj.lookup_count = 0
            created.append(obj)

    client, api_mod = _api_client(user, session_factory=lambda: _CreatingSession([]))

    async def _log_audit(*a, **k):
        return None

    async def _no_assets(db, container_id):
        return []

    async def _apply_shares(*a, **k):
        return None

    monkeypatch.setattr(api_mod, "log_audit", _log_audit)
    monkeypatch.setattr(api_mod, "load_assets", _no_assets)
    monkeypatch.setattr(api_mod, "apply_user_share_fields", _apply_shares)
    return client, created


def test_create_refuses_a_kind_outside_the_closed_choice(monkeypatch):
    """``kind`` is Presentation or Report: the retired free text
    (``dashboard``, ``asset-library``) is a 422 that names both."""
    client, created = _creating_client(monkeypatch)
    for retired in ("dashboard", "asset-library", "Deck"):
        resp = client.post("/api/v1/template-containers", json={"name": "Acme", "kind": retired})
        assert resp.status_code == 422, resp.text
        assert "kind must be one of presentation, report" in resp.json()["detail"]
        assert repr(retired) in resp.json()["detail"]
    assert created == []


def test_create_normalizes_the_kind_and_stores_the_scheme_declaration(monkeypatch):
    client, created = _creating_client(monkeypatch)
    resp = client.post(
        "/api/v1/template-containers",
        json={
            "name": "Acme Light",
            "kind": " Presentation ",
            "theme_config": {"background": "#0b1020"},
            "extra_metadata": {"supported_schemes": ["LIGHT", "light"], "guidance": "x"},
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "presentation"
    # Canonical, de-duplicated - and the declaration WINS over the palette: the
    # background reads as dark, the author said light, the template is light.
    assert body["extra_metadata"]["supported_schemes"] == ["light"]
    assert body["supported_schemes"] == ["light"]
    (row,) = created
    assert row.extra_metadata == {"supported_schemes": ["light"], "guidance": "x"}

    # A blank kind is the default kind, and a report carries no declaration:
    # the key is dropped, the request is not refused.
    client, created = _creating_client(monkeypatch)
    resp = client.post(
        "/api/v1/template-containers",
        json={"name": "Acme Report", "kind": "report", "extra_metadata": {"supported_schemes": ["dark"]}},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["kind"] == "report"
    assert resp.json()["extra_metadata"] == {}
    assert resp.json()["supported_schemes"] == []


def test_create_refuses_a_declaration_that_names_no_usable_scheme(monkeypatch):
    client, created = _creating_client(monkeypatch)
    resp = client.post(
        "/api/v1/template-containers",
        json={"name": "Acme", "extra_metadata": {"supported_schemes": ["sepia"]}},
    )
    assert resp.status_code == 422, resp.text
    assert "does not know: 'sepia'" in resp.json()["detail"]

    resp = client.post(
        "/api/v1/template-containers",
        json={"name": "Acme", "extra_metadata": {"supported_schemes": []}},
    )
    assert resp.status_code == 422, resp.text
    assert "at least one scheme" in resp.json()["detail"]

    resp = client.post(
        "/api/v1/template-containers",
        json={"name": "Acme", "extra_metadata": {"supported_schemes": "dark"}},
    )
    assert resp.status_code == 422, resp.text
    assert "must be a list" in resp.json()["detail"]
    assert created == []


def test_create_refuses_a_theme_value_that_makes_viewers_fetch_a_url(monkeypatch):
    """A palette lands in the CSS every viewer of a deck wearing the template
    paints with, so a value that loads an image is refused before it is stored -
    by name, in the base palette and in a variant - while what the
    REST surface always trusted its caller with (a key the renderer does not
    read) still saves as it did."""
    client, created = _creating_client(monkeypatch)
    resp = client.post(
        "/api/v1/template-containers",
        json={"name": "Tracker", "theme_config": {"background": 'image-set("//example.com/x.png" 1x)'}},
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "theme_config['background']" in detail and "image-set()" in detail

    resp = client.post(
        "/api/v1/template-containers",
        json={
            "name": "Tracker",
            "theme_config": {"background": "#0b1020"},
            "extra_metadata": {
                "theme_variants": {"light": {"accent": "cross-fade('//example.com/a.png', '//example.com/b.png', 50%)"}}
            },
        },
    )
    assert resp.status_code == 422, resp.text
    assert "extra_metadata.theme_variants.light['accent']" in resp.json()["detail"]
    assert "cross-fade()" in resp.json()["detail"]
    assert created == []

    gradient = "linear-gradient(135deg, #070d18 0%, #1f3965 100%)"
    resp = client.post(
        "/api/v1/template-containers",
        json={"name": "Acme", "theme_config": {"background": gradient, "palette": {"accent": "#fff"}}},
    )
    assert resp.status_code == 201, resp.text
    (row,) = created
    assert row.theme_config == {"background": gradient, "palette": {"accent": "#fff"}}


def test_update_holds_the_kind_and_the_declaration_to_the_same_contract(asset_api):
    client, state = asset_api
    container = state["container"]
    container.extra_metadata = {"supported_schemes": ["dark"], "deck_defaults": {"theme": "dark"}}
    # The detail response reads the per-user share columns the asset fixture's
    # namespace does not carry (the asset routes never serialize the container).
    container.shared_user_ids = None
    container.shared_user_permissions = None
    container.access_restricted = False
    url = f"/api/v1/template-containers/{container.id}"

    resp = client.put(url, json={"kind": "dashboard"})
    assert resp.status_code == 422, resp.text
    assert "kind must be one of presentation, report" in resp.json()["detail"]
    assert container.kind == "presentation"

    resp = client.put(url, json={"extra_metadata": {"supported_schemes": ["neon"]}})
    assert resp.status_code == 422, resp.text
    assert container.extra_metadata["supported_schemes"] == ["dark"], "a refused write changes nothing"

    # The Configuration page's Dark / Light switches: the declaration changes in
    # place, next to the deck defaults it lives with.
    resp = client.put(
        url, json={"extra_metadata": {"supported_schemes": ["light", "dark"], "deck_defaults": {"theme": "dark"}}}
    )
    assert resp.status_code == 200, resp.text
    assert container.extra_metadata["supported_schemes"] == ["dark", "light"]
    assert resp.json()["supported_schemes"] == ["dark", "light"]

    # Re-kinding to a report drops the declaration rather than refusing the row.
    resp = client.put(url, json={"kind": "Report"})
    assert resp.status_code == 200, resp.text
    assert container.kind == "report"
    assert "supported_schemes" not in container.extra_metadata
    assert resp.json()["supported_schemes"] == []


def test_update_refuses_a_theme_value_that_makes_viewers_fetch_a_url(asset_api):
    """The Configuration page saves the palette through this route: a value
    that loads an image - whatever its case or escapes - is refused by name
    and nothing is stored."""
    client, state = asset_api
    container = state["container"]
    container.shared_user_ids = None
    container.shared_user_permissions = None
    container.access_restricted = False
    container.theme_config = {"background": "#0b1020"}
    container.extra_metadata = {}
    url = f"/api/v1/template-containers/{container.id}"

    resp = client.put(url, json={"theme_config": {"background": "IMAGE-SET('//example.com/x.png' 1x)"}})
    assert resp.status_code == 422, resp.text
    assert "theme_config['background']" in resp.json()["detail"]
    assert "image-set()" in resp.json()["detail"]
    assert container.theme_config == {"background": "#0b1020"}, "a refused write stores nothing"

    resp = client.put(
        url, json={"extra_metadata": {"theme_variants": {"light": {"surface": "u\\rl(//example.com/x.png)"}}}}
    )
    assert resp.status_code == 422, resp.text
    assert "extra_metadata.theme_variants.light['surface']" in resp.json()["detail"]
    assert "url()" in resp.json()["detail"]
    assert container.extra_metadata == {}

    resp = client.put(url, json={"theme_config": {"background": "oklch(20% 0.05 250)"}})
    assert resp.status_code == 200, resp.text
    assert container.theme_config == {"background": "oklch(20% 0.05 250)"}


def test_resolve_kind_update_takes_an_echo_as_a_no_op_and_holds_a_change_to_the_choice():
    """The one rule behind the REST update and the agent's ``update_template``:
    echoing what is stored never refuses, changing it must
    name one of the two kinds."""
    from app.services.template_containers import resolve_kind_update

    # An echo of a retired kind keeps it - trimmed and case-insensitive, since
    # the form lower-cases what it sends back.
    assert resolve_kind_update("asset-library", "asset-library") == "asset-library"
    assert resolve_kind_update("asset-library", " ASSET-LIBRARY ") == "asset-library"
    assert resolve_kind_update("dashboard", "dashboard") == "dashboard"
    # A supported kind stored under an older spelling comes back canonical.
    assert resolve_kind_update("Presentation", "presentation") == "presentation"
    assert resolve_kind_update("presentation", "presentation") == "presentation"
    # A CHANGE is the closed choice: a retired kind is refused, a supported one
    # taken, and blank still means the default.
    assert resolve_kind_update("presentation", "dashboard") is None
    assert resolve_kind_update("asset-library", "dashboard") is None
    assert resolve_kind_update("asset-library", "presentation") == "presentation"
    assert resolve_kind_update("asset-library", " Report ") == "report"
    assert resolve_kind_update("asset-library", "") == "presentation"


def test_update_saves_the_other_fields_of_a_legacy_kind_row_that_echoes_its_kind(asset_api):
    """A row still carrying a retired kind (a user-created ``asset-library`` from
    before the kind became a closed choice) must be able to save its name, tags
    or palettes: the form sends the kind back on every save, and an echo that merely restates what is stored
    is a no-op - never the 422 a CHANGE to a retired kind gets."""
    client, state = asset_api
    container = state["container"]
    container.kind = "asset-library"
    container.extra_metadata = {}
    container.shared_user_ids = None
    container.shared_user_permissions = None
    container.access_restricted = False
    url = f"/api/v1/template-containers/{container.id}"

    # The echo, exactly as ``ConfigTab`` lower-cases it, alongside a real edit.
    resp = client.put(
        url, json={"kind": "asset-library", "name": "Partner logos (ours)", "tags": ["Logos"]}
    )
    assert resp.status_code == 200, resp.text
    assert container.name == "Partner logos (ours)"
    assert container.tags == ["logos"]
    assert container.kind == "asset-library", "the echo changes nothing"
    assert resp.json()["kind"] == "asset-library"

    # Spelling and whitespace do not turn an echo into a change.
    resp = client.put(url, json={"kind": " Asset-Library ", "description": "Our partner marks"})
    assert resp.status_code == 200, resp.text
    assert container.description == "Our partner marks"
    assert container.kind == "asset-library"

    # A CHANGE to another retired kind is still the closed-choice 422 ...
    resp = client.put(url, json={"kind": "dashboard"})
    assert resp.status_code == 422, resp.text
    assert "kind must be one of presentation, report" in resp.json()["detail"]
    assert "'dashboard'" in resp.json()["detail"]
    assert container.kind == "asset-library"

    # ... and moving the row onto a supported kind is how it leaves the legacy
    # label behind - the ``TemplateKindSelect`` advice.
    resp = client.put(url, json={"kind": "Presentation"})
    assert resp.status_code == 200, resp.text
    assert container.kind == "presentation"
    assert resp.json()["kind"] == "presentation"


def test_update_and_the_agent_tool_share_the_echo_rule():
    """The echo rule lives once, in ``resolve_kind_update``: both write paths
    call it, neither re-spells a comparison of its own (rule 81)."""
    api_source = (BACKEND_ROOT / "app/api/v1/template_containers.py").read_text(encoding="utf-8")
    update_body = api_source.split("async def update_template_container", 1)[1].split("\n\n\n", 1)[0]
    assert "resolve_kind_update(container.kind, body.kind)" in update_body
    assert "normalize_kind(body.kind)" not in update_body

    tool_source = (BACKEND_ROOT / "app/services/builtin_tools/template_authoring.py").read_text(
        encoding="utf-8"
    )
    update_tool = tool_source.split("async def _update_template", 1)[1].split("\n    async def ", 1)[0]
    assert "resolve_kind_update(container.kind, kind)" in update_tool
    assert "normalize_kind(kind)" not in update_tool


def test_company_managed_toggle_skips_the_agent_reference_sweep():
    """Containers are discovered via the tool, not bound by id on agents.

    The update endpoint must therefore pass ``agent_field=None`` — passing a real
    agent list field would sweep an unrelated binding.
    """
    source = (BACKEND_ROOT / "app/api/v1/template_containers.py").read_text(
        encoding="utf-8"
    )
    assert "agent_field=None" in source


# -- Thumbnails --------------------------------------------------------


def _encoded(image, fmt: str, **params) -> bytes:
    out = io.BytesIO()
    image.save(out, format=fmt, **params)
    return out.getvalue()


def _png(width: int, height: int, *, alpha: bool = True) -> bytes:
    from PIL import Image

    mode, color = ("RGBA", (15, 188, 255, 128)) if alpha else ("RGB", (15, 188, 255))
    return _encoded(Image.new(mode, (width, height), color), "PNG")


def _noisy(fmt: str, size: tuple[int, int] = (40, 40)) -> bytes:
    """An incompressible picture, so that cutting its file cuts its pixels."""
    from PIL import Image

    count = size[0] * size[1] * 3
    pixels = b"".join(hashlib.sha256(str(block).encode()).digest() for block in range(count // 32 + 1))[:count]
    return _encoded(Image.frombytes("RGB", size, pixels), fmt)


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    import struct
    import zlib

    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _png_header(width: int, height: int) -> bytes:
    """A PNG that declares *width* x *height* and holds no pixel at all."""
    import struct

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) + _png_chunk(b"IEND", b"")


def _png_with_a_broken_chunk() -> bytes:
    """A PNG whose pixel data goes on in a chunk of no valid type: Pillow raises a
    ``SyntaxError`` mid-decode, not an ``OSError``."""
    import struct

    data, pos, out = _noisy("PNG"), 8, [b"\x89PNG\r\n\x1a\n"]
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IDAT":
            out += [_png_chunk(b"IDAT", body[: length // 2]), _png_chunk(b"\x00\x01\x02\x03", body[length // 2 :])]
        else:
            out.append(_png_chunk(kind, body))
    return b"".join(out)


def _thumbnail(data: bytes, size: int = 128):
    from PIL import Image

    from app.services.template_thumbnails import render_thumbnail

    image = Image.open(io.BytesIO(render_thumbnail(data, size)))
    assert image.format == "PNG"
    image.load()
    return image


def test_a_raster_thumbnail_is_bounded_keeps_its_aspect_and_its_alpha():
    image = _thumbnail(_png(600, 300), 256)
    assert image.size == (256, 128)
    assert image.mode == "RGBA"
    # Resampled with premultiplied alpha: a channel may round by one level.
    pixel = image.getpixel((128, 64))
    assert all(abs(got - want) <= 1 for got, want in zip(pixel, (15, 188, 255, 128), strict=True)), pixel
    # An image that already fits is decoded and re-encoded too, at its own size.
    assert _thumbnail(_png(40, 40, alpha=False), 64).size == (40, 40)


def test_a_small_image_that_does_not_decode_is_refused_not_served():
    """Opening only reads the header, so a small image passed
    through "because it already fits" would serve cut or corrupt bytes as a
    successful thumbnail. Every thumbnail is the fully decoded frame."""
    import app.services.template_thumbnails as thumbs

    for fmt in ("PNG", "JPEG", "GIF", "BMP", "WEBP"):
        whole = _noisy(fmt)
        assert _thumbnail(whole, 64).size == (40, 40), fmt
        with pytest.raises(thumbs.ThumbnailRefusedError):
            thumbs.render_thumbnail(whole[: len(whole) // 2], 64)
    for garbage in (b"not an image", b"", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40):
        with pytest.raises(thumbs.ThumbnailRefusedError):
            thumbs.render_thumbnail(garbage, 64)
    # Pillow's SyntaxError on a broken chunk is a refusal too, never a 500.
    with pytest.raises(thumbs.ThumbnailRefusedError) as refused:
        thumbs.render_thumbnail(_png_with_a_broken_chunk(), 64)
    assert isinstance(refused.value.__cause__, SyntaxError)


def test_a_header_declaring_too_many_pixels_is_refused_before_any_decode(monkeypatch):
    import app.services.template_thumbnails as thumbs

    # 8000 x 8000 is refused on its header: the file holds no pixel to decode.
    with pytest.raises(thumbs.ThumbnailRefusedError, match="8000x8000 px, too large"):
        thumbs.render_thumbnail(_png_header(8000, 8000), 128)
    # Past Pillow's own bomb limit, its error is a refusal too.
    with pytest.raises(thumbs.ThumbnailRefusedError):
        thumbs.render_thumbnail(_png_header(50_000, 50_000), 128)
    monkeypatch.setattr(thumbs, "MAX_THUMBNAIL_SOURCE_PIXELS", 100)
    with pytest.raises(thumbs.ThumbnailRefusedError, match="too large"):
        thumbs.render_thumbnail(_png(20, 20), 64)


def test_a_thumbnail_is_turned_upright_once_by_its_exif_orientation():
    """A portrait photo tagged orientation 6 was drawn on its
    side. The pixels are turned and the PNG carries no EXIF, so no browser can
    turn it a second time."""
    from PIL import Image

    stored = Image.new("RGB", (60, 30), (0, 0, 255))
    stored.paste((255, 0, 0), (0, 0, 10, 30))  # the stored left edge is the visual top
    exif = Image.Exif()
    exif[274] = 6
    for fmt in ("JPEG", "WEBP", "PNG"):
        image = _thumbnail(_encoded(stored, fmt, exif=exif.tobytes()), 64)
        assert image.size == (30, 60), fmt
        red, _green, blue = image.convert("RGB").getpixel((15, 0))
        assert red > 200 and blue < 60, fmt
        assert 274 not in image.getexif(), fmt
    # A large photo is reduced and turned the same way.
    photo = _encoded(stored.resize((600, 300)), "JPEG", exif=exif.tobytes())
    assert _thumbnail(photo, 128).size == (64, 128)
    # An EXIF block Pillow cannot read leaves the pixels as decoded.
    unreadable = _encoded(stored, "JPEG", exif=b"Exif\x00\x00not a tiff header")
    assert _thumbnail(unreadable, 64).size == (60, 30)


def test_every_decoded_mode_becomes_rgb_or_rgba_on_purpose():
    from PIL import Image

    # CMYK: converted, and its CMYK profile never lands on the RGB pixels.
    cmyk = _thumbnail(_encoded(Image.new("CMYK", (30, 20), (0, 255, 255, 0)), "JPEG", icc_profile=b"cmyk" * 8))
    assert cmyk.mode == "RGB" and "icc_profile" not in cmyk.info
    assert cmyk.getpixel((15, 10))[0] > 200
    # An RGB source keeps its own profile (a Display P3 screenshot stays P3),
    # unless it is too large to carry into a tile.
    rgb = Image.new("RGB", (30, 20), (10, 20, 30))
    assert _thumbnail(_encoded(rgb, "PNG", icc_profile=b"display-p3")).info["icc_profile"] == b"display-p3"
    assert "icc_profile" not in _thumbnail(_encoded(rgb, "PNG", icc_profile=b"x" * (65 * 1024))).info
    # A palette with a transparent index, grey with alpha: RGBA, alpha kept.
    palette = Image.new("P", (10, 10), 0)
    palette.putpalette([255, 0, 0] * 256)
    assert _thumbnail(_encoded(palette, "PNG", transparency=0)).getpixel((0, 0)) == (255, 0, 0, 0)
    assert _thumbnail(_encoded(Image.new("LA", (10, 10), (128, 0)), "PNG")).getpixel((0, 0))[3] == 0
    # 16-bit grey is scaled to 8 bits, never clipped to white.
    grey = _thumbnail(_encoded(Image.new("I;16", (8, 8), 30000), "PNG")).convert("RGB")
    assert grey.getpixel((0, 0)) == (117, 117, 117)
    # An animation is drawn from its first frame.
    frames = [Image.new("RGB", (20, 10), color) for color in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    animated = _encoded(frames[0], "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    assert _thumbnail(animated).convert("RGB").getpixel((10, 5)) == (255, 0, 0)


def test_only_the_raster_decoders_ever_run_whatever_the_row_declares():
    """The declared type is the uploader's word: a TIFF, an icon or a Photoshop
    file named ``image/png`` never reaches another Pillow decoder."""
    from PIL import Image

    import app.services.template_thumbnails as thumbs

    for fmt in ("TIFF", "ICO", "PCX", "TGA"):
        with pytest.raises(thumbs.ThumbnailRefusedError):
            thumbs.render_thumbnail(_encoded(Image.new("RGB", (16, 16), (1, 2, 3)), fmt), 64)


def test_an_avif_asset_gets_a_thumbnail_where_pillow_decodes_avif():
    from PIL import Image

    Image.init()
    if "AVIF" not in Image.OPEN:
        pytest.skip("this Pillow build has no AVIF decoder")
    assert _thumbnail(_encoded(Image.new("RGB", (300, 150), (9, 99, 199)), "AVIF")).size == (128, 64)


@pytest.fixture
def thumbnail_api(asset_api, monkeypatch):
    """The asset API with a visible container and objects served from memory."""
    client, state = asset_api
    import app.api.v1.template_containers as api_mod
    import app.services.file_storage as storage_mod

    state["objects"] = {}
    state["downloads"] = []

    async def _visible(db, user, container_id):
        if str(container_id) != str(state["container"].id):
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Template container not found")
        return state["container"]

    async def _download(object_name):
        state["downloads"].append(object_name)
        return state["objects"][object_name]

    monkeypatch.setattr(api_mod, "_get_visible_container", _visible)
    monkeypatch.setattr(storage_mod.file_storage, "download", _download)

    def add(filename: str, content_type: str, data: bytes):
        asset = SimpleNamespace(
            id=uuid.uuid4(),
            filename=filename,
            content_type=content_type,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            s3_object_name=f"k/{uuid.uuid4().hex}",
            source=TEMPLATE_ASSET_SOURCE,
        )
        state["objects"][asset.s3_object_name] = data
        state["assets"].append(asset)
        return asset

    state["add"] = add
    return client, state


def _thumb_url(state, asset, size=128):
    return f"/api/v1/template-containers/{state['container'].id}/assets/{asset.id}/thumbnail?size={size}"


def test_the_thumbnail_route_resizes_and_answers_with_the_file_cache_contract(thumbnail_api):
    from PIL import Image

    client, state = thumbnail_api
    asset = state["add"]("portrait.png", "image/png", _png(1000, 500))
    resp = client.get(_thumb_url(state, asset))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(resp.content)) as image:
        assert image.size == (128, 64)
    assert resp.headers["cache-control"] == "private, no-cache"
    assert resp.headers["vary"] == "Authorization"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-disposition"] == "inline"
    etag = resp.headers["etag"]
    # Revalidated before the object store is read, the header read from the
    # request (a weak form of the tag matches too, RFC 9110).
    downloads = len(state["downloads"])
    again = client.get(_thumb_url(state, asset), headers={"If-None-Match": f"W/{etag}"})
    assert again.status_code == 304 and again.headers["etag"] == etag
    assert again.headers["cache-control"] == "private, no-cache"
    assert again.headers["vary"] == "Authorization"
    assert len(state["downloads"]) == downloads
    # Another size is another validator; an asset replaced in place is too.
    assert client.get(_thumb_url(state, asset, 64)).headers["etag"] != etag
    asset.s3_object_name = "k/replaced"
    state["objects"]["k/replaced"] = _png(10, 10)
    assert client.get(_thumb_url(state, asset), headers={"If-None-Match": etag}).status_code == 200


def test_the_thumbnail_etag_follows_the_stored_bytes_not_only_the_object_key():
    from app.services.template_thumbnails import thumbnail_etag

    row = SimpleNamespace(s3_object_name="k/a", content_type="image/png", filename="a.png", size=10, sha256="aa")
    etag = thumbnail_etag(row, 128)
    assert thumbnail_etag(SimpleNamespace(**{**vars(row), "size": 11}), 128) != etag
    assert thumbnail_etag(SimpleNamespace(**{**vars(row), "sha256": "bb"}), 128) != etag
    assert thumbnail_etag(SimpleNamespace(**vars(row)), 128) == etag


def test_the_thumbnail_route_passes_svg_through_and_refuses_the_rest(thumbnail_api):
    client, state = thumbnail_api
    svg = state["add"]("mark.svg", "image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    resp = client.get(_thumb_url(state, svg))
    assert resp.status_code == 200
    assert resp.content == b"<svg xmlns='http://www.w3.org/2000/svg'/>"
    assert resp.headers["content-disposition"].startswith("attachment")
    assert resp.headers["cache-control"] == "private, no-cache"
    # Not an image: refused on the row, before the object store is read.
    font = state["add"]("Inter.ttf", "font/ttf", b"font")
    downloads = len(state["downloads"])
    assert client.get(_thumb_url(state, font)).status_code == 415
    assert len(state["downloads"]) == downloads
    broken = state["add"]("broken.png", "image/png", _noisy("PNG")[:200])
    assert client.get(_thumb_url(state, broken)).status_code == 415
    assert client.get(_thumb_url(state, svg, 999)).status_code == 422
    # A template the caller cannot see answers as the download does.
    other = f"/api/v1/template-containers/{uuid.uuid4()}/assets/{svg.id}/thumbnail"
    assert client.get(other).status_code == 404


def test_the_bytes_decide_the_thumbnail_not_the_declared_type(thumbnail_api):
    """The Assets tab drew every ``image/*`` asset from its full file before the
    thumbnail route: a JPEG stored as ``image/jpg`` or a PNG labelled JPEG kept
    its preview, so it keeps its thumbnail. A file no raster decoder reads is a
    415 whatever its label."""
    from PIL import Image

    client, state = thumbnail_api
    photo = _encoded(Image.new("RGB", (400, 200), (200, 30, 30)), "JPEG")
    for content_type in ("image/jpg", "image/pjpeg", "image/png", "IMAGE/JPEG; charset=binary"):
        asset = state["add"]("photo.jpg", content_type, photo)
        resp = client.get(_thumb_url(state, asset, 64))
        assert resp.status_code == 200, content_type
        assert resp.headers["content-type"] == "image/png"
        assert Image.open(io.BytesIO(resp.content)).size == (64, 32)
    tiff = state["add"]("scan.png", "image/png", _encoded(Image.new("RGB", (8, 8)), "TIFF"))
    assert client.get(_thumb_url(state, tiff)).status_code == 415
    svg_as_png = state["add"]("mark.png", "image/png", b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    assert client.get(_thumb_url(state, svg_as_png)).status_code == 415
    # What the route renders nothing for is a 415, the answer on which a tile
    # falls back to the original it drew before the route existed.
    for filename, content_type, fmt in (("scan.tif", "image/tiff", "TIFF"), ("favicon.ico", "image/x-icon", "ICO")):
        other = state["add"](filename, content_type, _encoded(Image.new("RGB", (16, 16), (1, 2, 3)), fmt))
        assert client.get(_thumb_url(state, other)).status_code == 415, content_type
    heic = state["add"]("photo.heic", "image/heic", b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64)
    assert client.get(_thumb_url(state, heic)).status_code == 415


def test_the_thumbnail_route_reads_the_template_as_its_non_admin_caller(monkeypatch):
    """The real lookups, as a NON-admin (an admin's predicate degenerates to
    ``true`` and would prove nothing): a template the caller cannot see is a 404
    before any storage read; a visible one is read three-tier and its asset
    scoped to it, exactly as the download does."""
    import app.services.file_storage as storage_mod

    member = _user()
    container = _container(user_id=member.id)
    asset = SimpleNamespace(
        id=uuid.uuid4(),
        filename="logo.png",
        content_type="image/png",
        size=10,
        sha256=None,
        s3_object_name="k/logo",
        source=TEMPLATE_ASSET_SOURCE,
    )
    downloads: list[str] = []
    sessions: list[_FakeSession] = []

    async def _download(object_name):
        # The pooled connection went back before the storage read.
        assert sessions[-1].committed
        downloads.append(object_name)
        return _png(300, 100)

    monkeypatch.setattr(storage_mod.file_storage, "download", _download)
    url = f"/api/v1/template-containers/{container.id}/assets/{asset.id}/thumbnail"

    hidden = _FakeSession([_Result(rows=[]), _Result(one=None)])
    client, _ = _api_client(member, session_factory=lambda: hidden)
    assert client.get(url).status_code == 404
    assert downloads == []
    where = _compiled(hidden.statements[1]).split("WHERE", 1)[1]
    assert f"template_containers.user_id = '{member.id}'" in where
    assert "template_containers.is_company_managed" in where
    assert "shared_user_ids" in where

    visible = _FakeSession([_Result(rows=[]), _Result(one=container), _Result(one=asset)])
    sessions.append(visible)
    client, _ = _api_client(member, session_factory=lambda: visible)
    resp = client.get(url)
    assert resp.status_code == 200, resp.text
    assert downloads == ["k/logo"]
    asset_sql = _compiled(visible.statements[2])
    assert f"file_attachments.template_container_id = '{container.id}'" in asset_sql
    assert f"file_attachments.source = '{TEMPLATE_ASSET_SOURCE}'" in asset_sql


def test_a_burst_of_cold_thumbnails_waits_at_the_gate_not_as_downloaded_blobs(monkeypatch):
    """The Assets tab asks for every image at once, and each
    request downloaded its whole asset before any bounded capacity, queueing
    blobs behind the parse pool document parsing shares. A raster render holds
    the gate from the storage read to its PNG; an SVG is never held by it."""
    import app.api.v1.template_containers as api_mod
    import app.core.parse_offload as parse_mod
    import app.services.file_storage as storage_mod
    import app.services.template_thumbnails as thumbs
    from app.core.config import settings

    monkeypatch.setattr(settings, "parse_thread_pool_max_workers", 3)
    monkeypatch.setattr(thumbs, "_render_gate", None)
    container = _container()
    assets = {
        name: SimpleNamespace(
            id=uuid.uuid4(),
            filename=name,
            content_type="image/svg+xml" if name.endswith(".svg") else "image/png",
            size=1,
            sha256=None,
            s3_object_name=f"k/{name}",
            source=TEMPLATE_ASSET_SOURCE,
        )
        for name in [f"{index}.png" for index in range(6)] + ["mark.svg"]
    }
    by_id = {str(asset.id): asset for asset in assets.values()}
    held = {"now": 0, "peak": 0, "svg_while_full": False}

    async def _visible(db, user, container_id):
        return container

    async def _asset(db, container, asset_id):
        return by_id[asset_id]

    async def _download(object_name):
        if object_name.endswith(".svg"):
            held["svg_while_full"] = held["now"] == 2
            return b"<svg xmlns='http://www.w3.org/2000/svg'/>"
        held["now"] += 1
        held["peak"] = max(held["peak"], held["now"])
        await asyncio.sleep(0.02)
        return _png(300, 100)

    async def _render(data, size):
        try:
            await asyncio.sleep(0.02)
            return thumbs.render_thumbnail(data, size)
        finally:
            held["now"] -= 1

    async def _document_pool(*args):
        raise AssertionError("a thumbnail must never queue on the document parse pool")

    monkeypatch.setattr(api_mod, "_get_visible_container", _visible)
    monkeypatch.setattr(api_mod, "_get_asset", _asset)
    monkeypatch.setattr(storage_mod.file_storage, "download", _download)
    monkeypatch.setattr(api_mod, "render_thumbnail_async", _render)
    monkeypatch.setattr(parse_mod, "run_parse", _document_pool)

    async def _burst():
        return await asyncio.gather(
            *(
                api_mod.thumbnail_template_asset(
                    container_id=str(container.id),
                    asset_id=str(asset.id),
                    size=128,
                    if_none_match=None,
                    user=_user(),
                    db=_FakeSession([]),
                )
                for asset in assets.values()
            )
        )

    responses = asyncio.run(_burst())
    assert [response.status_code for response in responses] == [200] * len(assets)
    # Three parse workers: two renders at once.
    assert held["peak"] == 2
    assert held["svg_while_full"]
    monkeypatch.setattr(settings, "parse_thread_pool_max_workers", 1)
    monkeypatch.setattr(thumbs, "_render_gate", None)
    assert thumbs.thumbnail_render_gate()._value == 1


def test_a_render_runs_on_its_own_pool_even_with_a_single_parse_worker(monkeypatch):
    """``PARSE_THREAD_POOL_MAX_WORKERS=1`` is supported, and
    a thumbnail on the document parse pool would then hold its only worker."""
    import threading

    import app.core.parse_offload as parse_mod
    import app.services.template_thumbnails as thumbs
    from app.core.config import settings

    async def _document_pool(*args):
        raise AssertionError("a thumbnail must never queue on the document parse pool")

    monkeypatch.setattr(settings, "parse_thread_pool_max_workers", 1)
    monkeypatch.setattr(thumbs, "_render_executor", None)
    monkeypatch.setattr(parse_mod, "run_parse", _document_pool)
    threads: list[str] = []
    render = thumbs.render_thumbnail

    def _spy(data, size):
        threads.append(threading.current_thread().name)
        return render(data, size)

    monkeypatch.setattr(thumbs, "render_thumbnail", _spy)
    try:
        body = asyncio.run(thumbs.render_thumbnail_async(_png(300, 100), 128))
        assert body.startswith(b"\x89PNG")
        assert threads and threads[0].startswith("xtmone-thumbnail")
        assert thumbs._render_executor._max_workers == 1
    finally:
        thumbs._render_executor.shutdown(wait=True)


def test_the_thumbnail_and_the_download_share_the_visibility_rule():
    source = (BACKEND_ROOT / "app/api/v1/template_containers.py").read_text(encoding="utf-8")
    for route in ("async def download_template_asset", "async def thumbnail_template_asset"):
        body = source.split(route, 1)[1].split("\n\n\n", 1)[0]
        assert "await _get_visible_container(db, user, container_id)" in body, route


def test_the_tiles_that_list_assets_draw_thumbnails_and_keep_every_original_preview():
    """A tile draws the thumbnail of a type the route decodes and, for every
    other type or a thumbnail refused, the original exactly as before:
    nothing a template showed before shows less now."""
    from app.services.template_thumbnails import THUMBNAIL_DECODERS, THUMBNAIL_SIZES

    hooks = (BACKEND_ROOT.parent / "frontend/src/hooks/api/useTemplateContainerApi.ts").read_text(encoding="utf-8")
    sizes = ", ".join(str(size) for size in THUMBNAIL_SIZES)
    assert f"export const TEMPLATE_THUMBNAIL_SIZES = [{sizes}] as const;" in hooks
    # A replace keeps the id, so the URL carries the asset's version:
    # without it a mounted tile keeps its old blob.
    assert "/assets/${asset.id}/thumbnail?size=${size}&v=${encodeURIComponent(asset.version)}" in hooks
    tab = (BACKEND_ROOT.parent / "frontend/src/pages/template-detail/AssetsTab.tsx").read_text(encoding="utf-8")
    thumb = tab.split("function AssetThumb", 1)[1].split("\n}\n", 1)[0]
    assert "useFirstAuthenticatedSrc(" in thumb and "templateAssetPreviewSources(" in thumb
    assert "templateAssetThumbnailUrl(containerId, asset)" in thumb
    # The original the tab drew on main, unchanged.
    assert "/assets/${asset.id}/download?v=${encodeURIComponent(asset.version)}" in thumb
    picker = (BACKEND_ROOT.parent / "frontend/src/components/presentation/TemplateAssetField.tsx").read_text(
        encoding="utf-8"
    )
    assert "templateAssetThumbnailUrl(templateId, asset)" in picker
    # The picker's own preview on main: the file itself up to 2 MB.
    assert "asset.size <= MAX_PREVIEW_BYTES ? fileUrl(asset.id) : null" in picker
    assert "const MAX_PREVIEW_BYTES = 2 * 1024 * 1024;" in picker
    # Every decoder the route runs is a declared type the tiles ask it for.
    preview = (BACKEND_ROOT.parent / "frontend/src/lib/template-asset-preview.ts").read_text(encoding="utf-8")
    for decoder in THUMBNAIL_DECODERS:
        assert f'"image/{decoder.lower()}"' in preview, decoder
    assert '"image/svg+xml"' not in preview and '"image/tiff"' not in preview
