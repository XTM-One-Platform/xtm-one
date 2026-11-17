"""Tests for template container portability — export / import.

These lock in the contract:

1. A template **round-trips**: export a container with assets, import the package,
   get the same design back.
2. Assets travel. A template whose logo does not travel is not a template.
3. **Asset ids are remapped.** Imported assets are new rows, so stored references
   in ``layouts`` / ``extra_metadata`` are rewritten — without this a template
   imports "successfully" and renders with missing images, and nothing errors.
4. Tenancy never travels: ``is_company_managed`` / ``shared_group_ids`` /
   ``user_id`` describe the exporting deployment, not the design, so an import can
   never claim a visibility the importer is not entitled to.
5. Parsing an uploaded archive is parsing hostile input: bounded on entry count,
   uncompressed size and path shape.

The package format is exercised directly (no DB, no S3); the API wiring is
asserted structurally, the same style as ``test_reports.py``.
"""

from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile

import pytest

from app.services.template_package import (
    ASSET_DIR,
    EXPORTED_FIELDS,
    MANIFEST_NAME,
    MAX_ARCHIVE_ENTRIES,
    MAX_MANIFEST_BYTES,
    MAX_PACKAGE_BYTES,
    PACKAGE_EXPORT_TYPE,
    PACKAGE_VERSION,
    PackagedAsset,
    TemplatePackageError,
    build_manifest,
    build_package_zip,
    parse_bundle,
    parse_package,
    remap_asset_ids,
)
from tests.archive_helpers import corrupt_member

LOGO_ID = "11111111-1111-1111-1111-111111111111"
FONT_ID = "22222222-2222-2222-2222-222222222222"


def _container(**overrides) -> dict:
    base = {
        "name": "Acme Brand Deck",
        "description": "The Acme palette.",
        "kind": "presentation",
        "tags": ["acme", "brand"],
        "theme": "acme-dark",
        "theme_config": {"accent": "#ff4700", "background": "hsl(220 54% 8%)"},
        "aspect_ratio": "16:9",
        # A layout referencing an asset by id — the thing that breaks on import
        # unless ids are remapped.
        "layouts": [
            {"id": "title", "name": "Title", "background_image_file_id": LOGO_ID}
        ],
        "extra_metadata": {"deck_defaults": {"logo_file_id": LOGO_ID}},
    }
    base.update(overrides)
    return base


def _assets() -> list[PackagedAsset]:
    return [
        PackagedAsset(
            filename="logo.svg",
            data=b"<svg/>",
            asset_role="logo",
            content_type="image/svg+xml",
            original_id=LOGO_ID,
        ),
        PackagedAsset(
            filename="brand.woff2",
            data=b"\x00\x01font",
            asset_role="font",
            content_type="font/woff2",
            original_id=FONT_ID,
        ),
    ]


# ── Round-trip ───────────────────────────────────────────────────────────────


def test_a_template_round_trips_through_a_package():
    raw = build_package_zip(_container(), _assets())
    package = parse_package(raw)

    for field in EXPORTED_FIELDS:
        assert package.container[field] == _container()[field], field
    assert {a.filename for a in package.assets} == {"logo.svg", "brand.woff2"}
    assert {a.data for a in package.assets} == {b"<svg/>", b"\x00\x01font"}
    # Roles and content types survive, or the imported asset loses its meaning.
    roles = {a.filename: a.asset_role for a in package.assets}
    assert roles == {"logo.svg": "logo", "brand.woff2": "font"}


def test_the_archive_layout_is_the_dev_fixture_layout():
    """Reusing ``dev-docs/presentation-template-package/``'s shape means the dev
    fixture is a real importable package, not a format that can drift from it."""
    raw = build_package_zip(_container(), _assets())
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = set(archive.namelist())
    assert MANIFEST_NAME in names
    assert f"{ASSET_DIR}logo.svg" in names
    assert f"{ASSET_DIR}brand.woff2" in names


def test_manifest_carries_the_export_envelope():
    """Same envelope as every other entity's export, so tooling that reads one
    export can read this one."""
    manifest = build_manifest(_container(), [])
    assert manifest["version"] == PACKAGE_VERSION
    assert manifest["export_type"] == PACKAGE_EXPORT_TYPE
    assert manifest["exported_at"]
    assert manifest[PACKAGE_EXPORT_TYPE]["name"] == "Acme Brand Deck"


def test_metadata_only_bundle_round_trips():
    """A palette-only template is a real thing to hand around."""
    bundle = build_manifest(_container(), [])
    raw = json.dumps(bundle).encode("utf-8")
    package = parse_package(raw)
    assert package.container["theme_config"]["accent"] == "#ff4700"
    assert package.assets == []


def test_assets_with_the_same_filename_do_not_collide_in_the_archive():
    """Two assets may legitimately share a name (one logo, two roles); a ZIP entry
    cannot. The later one is suffixed and the manifest records what was written."""
    duplicated = [
        PackagedAsset(filename="logo.svg", data=b"a", original_id=LOGO_ID),
        PackagedAsset(filename="logo.svg", data=b"b", original_id=FONT_ID),
    ]
    package = parse_package(build_package_zip(_container(), duplicated))
    assert len(package.assets) == 2
    assert {a.data for a in package.assets} == {b"a", b"b"}
    # And each still maps back to its own original id, or the remap breaks.
    assert {a.original_id for a in package.assets} == {LOGO_ID, FONT_ID}


# ── Tenancy never travels ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "field", ["user_id", "is_company_managed", "shared_group_ids", "lookup_count"]
)
def test_tenancy_and_stats_are_not_exported(field):
    """These describe the exporting deployment, not the design. Carrying
    ``is_company_managed`` would let an import claim an org-wide visibility that
    publishing a template is supposed to require a human action for."""
    assert field not in EXPORTED_FIELDS
    manifest = build_manifest(
        _container(**{field: "smuggled"}), []
    )
    assert field not in manifest[PACKAGE_EXPORT_TYPE]


def test_a_smuggled_company_managed_flag_is_ignored_on_parse():
    bundle = build_manifest(_container(), [])
    bundle[PACKAGE_EXPORT_TYPE]["is_company_managed"] = True
    package = parse_bundle(bundle)
    # Parsing keeps whatever is in the JSON; the persistence layer is what forces
    # the flag false. This test pins the *parser's* half — see
    # ``test_import_never_creates_a_company_managed_container`` for the other.
    assert package.container.get("is_company_managed") is True


# ── Asset id remapping ───────────────────────────────────────────────────────


def test_remap_rewrites_asset_references_in_nested_json():
    mapping = {LOGO_ID: "new-logo", FONT_ID: "new-font"}
    layouts = [{"id": "title", "background_image_file_id": LOGO_ID}]
    extra = {"deck_defaults": {"logo_file_id": LOGO_ID, "fonts": [FONT_ID]}}
    assert remap_asset_ids(layouts, mapping) == [
        {"id": "title", "background_image_file_id": "new-logo"}
    ]
    assert remap_asset_ids(extra, mapping) == {
        "deck_defaults": {"logo_file_id": "new-logo", "fonts": ["new-font"]}
    }


def test_remap_only_replaces_whole_values_never_substrings():
    """An id mentioned inside prose is not a reference; rewriting it would corrupt
    the text."""
    mapping = {LOGO_ID: "new-logo"}
    prose = {"note": f"See asset {LOGO_ID} for the logo."}
    assert remap_asset_ids(prose, mapping) == prose


def test_remap_is_a_noop_without_a_mapping():
    layouts = [{"id": "title", "background_image_file_id": LOGO_ID}]
    assert remap_asset_ids(layouts, {}) is layouts


def test_unknown_ids_are_left_alone():
    """A reference to an asset that was not in the package (deleted before export)
    stays as-is rather than being blanked — a dangling id is debuggable, a
    silently emptied field is not."""
    stranger = str(uuid.uuid4())
    assert remap_asset_ids({"x": stranger}, {LOGO_ID: "new"}) == {"x": stranger}


# ── Hostile input ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "assets/../../etc/passwd",
        "assets//etc/passwd",
        "assets/nested/dir/logo.svg",
        "assets/.hidden",
        "/absolute/assets/logo.svg",
        "assets/..",
    ],
)
def test_traversing_or_nested_asset_paths_are_not_imported(path):
    """The import never writes to a filesystem — assets go to object storage under
    a generated key — but a traversing name would still land in the stored
    ``filename``, which is later echoed into a Content-Disposition header."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(MANIFEST_NAME, json.dumps(build_manifest(_container(), [])))
        archive.writestr(path, b"payload")
    package = parse_package(buffer.getvalue())
    assert package.assets == [], path


def test_non_asset_entries_are_ignored_not_fatal():
    """Refusing a package because it carries a README or a .DS_Store would be
    useless strictness."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(MANIFEST_NAME, json.dumps(build_manifest(_container(), [])))
        archive.writestr("README.md", b"# notes")
        archive.writestr(".DS_Store", b"\x00")
        archive.writestr(f"{ASSET_DIR}logo.svg", b"<svg/>")
    package = parse_package(buffer.getvalue())
    assert [a.filename for a in package.assets] == ["logo.svg"]


def test_a_manifest_one_directory_down_is_accepted():
    """Zipping a folder is what a human actually does."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            f"acme-template/{MANIFEST_NAME}",
            json.dumps(build_manifest(_container(), [])),
        )
        archive.writestr(f"acme-template/{ASSET_DIR}logo.svg", b"<svg/>")
    package = parse_package(buffer.getvalue())
    assert package.container["name"] == "Acme Brand Deck"
    assert [a.filename for a in package.assets] == ["logo.svg"]


def test_too_many_entries_is_refused():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(MANIFEST_NAME, json.dumps(build_manifest(_container(), [])))
        for index in range(MAX_ARCHIVE_ENTRIES + 1):
            archive.writestr(f"{ASSET_DIR}a{index}.bin", b"x")
    with pytest.raises(TemplatePackageError) as exc:
        parse_package(buffer.getvalue())
    assert "too many entries" in str(exc.value)


def test_a_zip_bomb_is_refused_on_uncompressed_size():
    """The bound is checked against bytes actually read, never against the
    archive's own declared sizes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST_NAME, json.dumps(build_manifest(_container(), [])))
        # Highly compressible: tiny on disk, enormous expanded.
        for index in range(20):
            archive.writestr(f"{ASSET_DIR}bomb{index}.bin", b"\x00" * (12 * 1024 * 1024))
    with pytest.raises(TemplatePackageError) as exc:
        parse_package(buffer.getvalue())
    assert "expands to more than" in str(exc.value)


def test_an_oversized_upload_is_refused_before_unpacking():
    with pytest.raises(TemplatePackageError) as exc:
        parse_package(b"x" * (MAX_PACKAGE_BYTES + 1))
    assert "too large" in str(exc.value)


def test_an_empty_upload_is_refused():
    with pytest.raises(TemplatePackageError):
        parse_package(b"")


def test_a_zip_without_a_manifest_is_refused():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{ASSET_DIR}logo.svg", b"<svg/>")
    with pytest.raises(TemplatePackageError) as exc:
        parse_package(buffer.getvalue())
    assert MANIFEST_NAME in str(exc.value)


def test_garbage_is_refused_with_a_readable_message():
    with pytest.raises(TemplatePackageError) as exc:
        parse_package(b"this is neither a zip nor json")
    assert "neither" in str(exc.value)


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda b: {**b, "version": 99}, "version"),
        (lambda b: {**b, "export_type": "prompt"}, "export type"),
        (lambda b: {**b, PACKAGE_EXPORT_TYPE: "nope"}, PACKAGE_EXPORT_TYPE),
        (
            lambda b: {**b, PACKAGE_EXPORT_TYPE: {**b[PACKAGE_EXPORT_TYPE], "name": ""}},
            "name is required",
        ),
        (lambda b: ["not", "an", "object"], "JSON object"),
    ],
)
def test_a_malformed_bundle_is_refused(mutate, fragment):
    with pytest.raises(TemplatePackageError) as exc:
        parse_bundle(mutate(build_manifest(_container(), [])))
    assert fragment in str(exc.value)


def test_a_stale_asset_manifest_does_not_become_container_metadata():
    """The manifest's ``assets`` list describes what the archive *should* hold; the
    rows are built from the bytes actually found."""
    package = parse_package(build_package_zip(_container(), _assets()))
    assert "assets" not in package.container


# ── API wiring ───────────────────────────────────────────────────────────────


def test_export_and_import_routes_exist():
    from app.api.v1 import template_containers as api

    routes = {
        (path, method)
        for route in api.router.routes
        for path in [getattr(route, "path", "")]
        for method in getattr(route, "methods", set())
    }
    assert ("/{container_id}/export", "GET") in routes
    assert ("/import", "POST") in routes


def test_import_validates_through_the_existing_validators():
    """An imported container must be held to the contract an authored one is, or
    import is the back door around it: a palette key the renderer ignores, or a
    layout id that does not exist, would land in the database and fail silently at
    render time."""
    import inspect

    from app.api.v1 import template_containers as api

    source = inspect.getsource(api._persist_imported_package)
    assert "validate_theme_config(" in source
    assert "validate_layouts(" in source


# -- a retired kind is refused on import by name, never coerced -------


class _ImportSession:
    """The little a zero-asset import needs of a session: add / flush / commit /
    refresh. Records every row added so a refusal can be shown to write nothing."""

    def __init__(self):
        self.added: list = []
        self.committed = False

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        self.added.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        return None


def _persist(
    container: dict,
    session: _ImportSession | None = None,
    assets: list[PackagedAsset] | None = None,
):
    """Run ``_persist_imported_package`` on a package (metadata-only by default)."""
    import asyncio
    from types import SimpleNamespace

    from app.api.v1 import template_containers as api
    from app.services.template_package import TemplatePackage

    session = session if session is not None else _ImportSession()
    user = SimpleNamespace(
        id=uuid.uuid4(), email="importer@example.com", full_name="Importer", is_admin=False
    )
    row = asyncio.run(
        api._persist_imported_package(
            session,  # type: ignore[arg-type]
            TemplatePackage(container=container, assets=list(assets or [])),
            user=user,  # type: ignore[arg-type]
            request=None,  # type: ignore[arg-type]
        )
    )
    return row, session


@pytest.mark.parametrize("kind", ["dashboard", "asset-library", "Sponsor deck"])
def test_import_refuses_a_retired_kind_by_name_instead_of_coercing_it(kind):
    """An older export of a retired kind used to come in as a presentation
    template, silently. It is now refused like a REST create is - the one
    ``kind_problem`` sentence, naming the kind - and nothing is written, so the
    exporter edits one field instead of receiving a template they never asked for."""
    from fastapi import HTTPException

    from app.services.template_containers import kind_problem

    with pytest.raises(HTTPException) as excinfo:
        _persist(_container(kind=kind, layouts=[], extra_metadata={}))
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail == (
        "The package was NOT imported. Fix the export and try again: " + kind_problem(kind)
    )
    assert repr(kind) in excinfo.value.detail
    assert "presentation, report" in excinfo.value.detail


def test_import_refusal_of_a_retired_kind_writes_nothing():
    from fastapi import HTTPException

    session = _ImportSession()
    with pytest.raises(HTTPException):
        _persist(_container(kind="dashboard", layouts=[], extra_metadata={}), session)
    assert session.added == [] and session.committed is False


def test_import_refuses_a_palette_that_makes_viewers_fetch_a_url():
    """A package is hostile input like any other write: a palette value
    that loads an image - in the base or in a variant - refuses the whole
    import by name, and nothing is written."""
    from fastapi import HTTPException

    session = _ImportSession()
    with pytest.raises(HTTPException) as excinfo:
        _persist(
            _container(
                layouts=[],
                theme_config={"accent": "#ff4700", "background": "-webkit-image-set('//example.com/x.png' 1x)"},
                extra_metadata={"theme_variants": {"light": {"surface": "element(#secret)"}}},
            ),
            session,
        )
    assert excinfo.value.status_code == 422
    detail = excinfo.value.detail
    assert detail.startswith("The package was NOT imported.")
    assert "theme_config['background']" in detail and "-webkit-image-set()" in detail
    assert "extra_metadata.theme_variants.light['surface']" in detail and "element()" in detail
    assert session.added == [] and session.committed is False


@pytest.mark.parametrize("kind", ["", "   ", None])
def test_import_takes_a_blank_kind_as_a_presentation_template(kind):
    """The one lenient reading that stays: no kind at all is a presentation
    template, as it is on creation (``normalize_kind``)."""
    container = _container(layouts=[], extra_metadata={})
    if kind is None:
        del container["kind"]
    else:
        container["kind"] = kind
    row, session = _persist(container)
    assert row.kind == "presentation"
    assert session.committed is True
    (audit,) = [obj for obj in session.added if type(obj).__name__ == "AuditLog"]
    assert audit.details["kind"] == "presentation"


def test_import_keeps_the_canonical_spelling_of_a_supported_kind():
    row, _session = _persist(_container(kind=" Report ", layouts=[], extra_metadata={}))
    assert row.kind == "report"


def test_an_imported_asset_records_the_digest_of_the_bytes_it_stored(monkeypatch):
    """``_persist_imported_package`` writes the asset rows of both import
    routes and of in-app duplication. Each records the digest of what storage
    holds - the sanitized SVG, not the packaged bytes - which the presentation
    package import trusts to tell same-name assets apart without reading them."""
    import app.services.file_storage as storage_mod
    from app.models.file_attachment import FileAttachment
    from app.services.svg_sanitizer import sanitize_template_asset

    async def _upload(*, data, filename, content_type, prefix):
        return f"{prefix}/{uuid.uuid4().hex}"

    monkeypatch.setattr(storage_mod.file_storage, "upload", _upload)
    raw = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><rect/></svg>'
    stored = sanitize_template_asset(raw, "image/svg+xml", "logo.svg")
    assert stored != raw, "the case needs bytes the sanitizer rewrites"
    session = _ImportSession()

    _persist(
        _container(),
        session,
        assets=[PackagedAsset(filename="logo.svg", data=raw, content_type="image/svg+xml", original_id=LOGO_ID)],
    )

    (asset,) = [obj for obj in session.added if isinstance(obj, FileAttachment)]
    assert asset.sha256 == hashlib.sha256(stored).hexdigest()


def test_duplicate_copies_a_retired_kind_as_a_presentation_template_and_records_it():
    """A copy is a NEW row and a row of a retired kind cannot be created.
    Unlike an import there is no export to edit, and a read-only legacy source
    cannot be moved off its kind first, so the duplicate path maps the kind to
    presentation explicitly - before the strict shared pipeline - and records
    the mapping in the audit details instead of refusing the copy."""
    import inspect

    from app.api.v1 import template_containers as api

    duplicate = inspect.getsource(api.duplicate_template_container)
    assert "source_kind = normalize_kind(source.kind)" in duplicate
    assert "coerce_metadata_to_kind(" in duplicate and "kind=PRESENTATION_KIND" in duplicate
    assert '"kind_mapped_from": source.kind' in duplicate
    # The strict pipeline itself never coerces: the fallback is gone from it.
    persist = inspect.getsource(api._persist_imported_package)
    assert "or PRESENTATION_KIND" not in persist
    assert "kind_problem(data.get(\"kind\"))" in persist


def _duplicate(monkeypatch, source_kind: str, session: _ImportSession | None = None, **metadata):
    """Run ``duplicate_template_container`` on a source row of *source_kind*
    carrying *metadata* (``theme_config`` / ``layouts`` / ``extra_metadata``
    overrides). Only the visibility load, the asset load (none) and the response
    are faked: the real in-memory packaging, the kind mapping and the strict
    persist pipeline run. Returns the new row and its audit entry."""
    import asyncio
    from types import SimpleNamespace

    from app.api.v1 import template_containers as api
    from app.models.template_container import TemplateContainer

    source = TemplateContainer(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        is_company_managed=False,
        name="Partner logos",
        description="Our partner marks.",
        kind=source_kind,
        tags=["logos"],
        theme=None,
        theme_config=metadata.get("theme_config", {}),
        aspect_ratio="16:9",
        layouts=metadata.get("layouts", []),
        extra_metadata=metadata.get("extra_metadata", {}),
    )

    async def _visible(_db, _user, _container_id):
        return source

    async def _no_assets(_db, _container_id):
        return []

    async def _detail(_db, container, **_kw):
        return container

    monkeypatch.setattr(api, "_get_visible_container", _visible)
    monkeypatch.setattr(api, "load_assets", _no_assets)
    monkeypatch.setattr(api, "_to_detail_response", _detail)

    session = session if session is not None else _ImportSession()
    user = SimpleNamespace(
        id=uuid.uuid4(),
        email="copier@example.com",
        full_name="Copier",
        is_admin=False,
        role="user",
    )
    row = asyncio.run(
        api.duplicate_template_container(
            container_id=str(source.id),
            request=None,  # type: ignore[arg-type]
            user=user,  # type: ignore[arg-type]
            db=session,  # type: ignore[arg-type]
            _agentic_gate=None,
        )
    )
    (audit,) = [obj for obj in session.added if type(obj).__name__ == "AuditLog"]
    return row, audit


#: What an older ``asset-library`` (or ``dashboard``) row can carry: metadata
#: stored for a renderer the presentation kind never had - a report-only palette
#: key, a made-up tile key, layout ids of the old kind, a variant with the same
#: stray key plus a scheme nobody renders, and an unusable scheme declaration -
#: next to entries a presentation template honours as they are.
_LEGACY_METADATA = {
    "theme_config": {"accent": "#00aaff", "critical": "#ff0000", "tile_gap": "8px"},
    "layouts": [{"id": "title"}, {"id": "logo_wall", "fields": ["logos"]}, {"id": "kpi_tiles"}],
    "extra_metadata": {
        "theme_variants": {
            "light": {"background": "#ffffff", "critical": "#ff0000"},
            "sepia": {"background": "#f4ecd8"},
        },
        "supported_schemes": ["neon"],
        "deck_defaults": {"footer": "Partners"},
    },
}


def test_duplicate_of_a_legacy_row_keeps_what_a_presentation_honours_and_records_the_rest(
    monkeypatch,
):
    """Mapping the kind alone is not enough for the copy to exist: the strict
    pipeline validates the copy AS a presentation, so a legacy row's dashboard
    layouts or report palette keys would turn into a 422 on a source the user
    cannot fix. The conversion keeps the honoured subset, derives the layout
    fields the way any create does, leaves free metadata alone, and the audit
    entry lists what was dropped in the validators' own words."""
    session = _ImportSession()
    row, audit = _duplicate(monkeypatch, "asset-library", session, **_LEGACY_METADATA)

    assert row.kind == "presentation" and session.committed is True
    assert row.theme_config == {"accent": "#00aaff"}
    assert [layout["id"] for layout in row.layouts] == ["title"]
    assert row.layouts[0]["fields"], "the fields list is derived, as on any create"
    assert row.extra_metadata["theme_variants"] == {"light": {"background": "#ffffff"}}
    assert "supported_schemes" not in row.extra_metadata, "unusable, so it declares nothing"
    assert row.extra_metadata["deck_defaults"] == {"footer": "Partners"}

    dropped = audit.details["metadata_dropped"]
    assert audit.details["kind_mapped_from"] == "asset-library"
    assert len(dropped) == 7
    for needle in (
        "theme_config key 'critical'",
        "theme_config key 'tile_gap'",
        "'logo_wall' is not a slide layout",
        "'kpi_tiles' is not a slide layout",
        "extra_metadata.theme_variants.light key 'critical'",
        "extra_metadata.theme_variants.sepia is not a scheme",
        "extra_metadata.supported_schemes names a scheme the renderer does not know: 'neon'",
    ):
        assert any(needle in line for line in dropped), needle


def test_duplicate_of_a_legacy_row_with_nothing_to_drop_records_an_empty_list(monkeypatch):
    _row, audit = _duplicate(monkeypatch, "dashboard", theme_config={"accent": "#00aaff"})
    assert audit.details["kind_mapped_from"] == "dashboard"
    assert audit.details["metadata_dropped"] == []


def test_duplicate_of_a_supported_kind_with_stale_metadata_is_still_refused_by_name(monkeypatch):
    """The conversion is for retired kinds only. A presentation row that carries
    a layout id no renderer knows is a real problem in the source, and the copy
    is refused the way an import is - naming the id - with nothing written."""
    from fastapi import HTTPException

    session = _ImportSession()
    with pytest.raises(HTTPException) as excinfo:
        _duplicate(monkeypatch, "presentation", session, layouts=[{"id": "logo_wall"}])
    assert excinfo.value.status_code == 422
    assert "'logo_wall' is not a slide layout" in excinfo.value.detail
    assert session.added == [] and session.committed is False


def test_coerce_metadata_to_kind_revalidates_clean_whatever_it_is_handed():
    """The invariant the duplicate path relies on: whatever a legacy row carries -
    fields of the wrong JSON type included - the coerced metadata passes the four
    validators of its new kind without a problem, so the strict pipeline stores
    it as is."""
    from app.services.template_containers import (
        coerce_metadata_to_kind,
        validate_layouts,
        validate_supported_schemes,
        validate_theme_config,
        validate_theme_variants,
    )

    hostile = {
        "kind": "asset-library",
        "theme_config": ["not", "an", "object"],
        "layouts": "title",
        "extra_metadata": {"theme_variants": "light", "supported_schemes": "dark", "note": 1},
    }
    for data in (_LEGACY_METADATA, hostile):
        coerced, dropped = coerce_metadata_to_kind(dict(data), kind="presentation")
        assert coerced["kind"] == "presentation" and dropped
        _cfg, problems = validate_theme_config(coerced["theme_config"], kind="presentation")
        _layouts, more = validate_layouts(coerced["layouts"], kind="presentation")
        problems += more
        meta, more = validate_theme_variants(coerced["extra_metadata"], kind="presentation")
        problems += more
        _meta, more = validate_supported_schemes(meta, kind="presentation")
        problems += more
        assert problems == []
    coerced, _dropped = coerce_metadata_to_kind(dict(hostile), kind="presentation")
    assert coerced["theme_config"] == {} and coerced["layouts"] == []
    assert coerced["extra_metadata"] == {"note": 1}, "free metadata survives, the unusable slots go"


@pytest.mark.parametrize("value", [["a", "b"], "free text", 7, True])
def test_coerce_metadata_to_kind_records_a_non_object_extra_metadata_it_cannot_carry(value):
    """Every omission is recorded, the whole ``extra_metadata`` included: a
    legacy row whose slot is a list, a string, a number or a boolean loses that
    value on conversion, and the audit must not read ``metadata_dropped: []``
    over it. ``None`` is simply absent and records nothing."""
    from app.services.template_containers import coerce_metadata_to_kind

    coerced, dropped = coerce_metadata_to_kind(
        {"kind": "asset-library", "extra_metadata": value}, kind="presentation"
    )
    assert coerced["extra_metadata"] == {}
    assert len(dropped) == 1
    assert dropped[0].startswith("extra_metadata must be a JSON object")
    assert f"got {type(value).__name__}" in dropped[0]

    coerced, dropped = coerce_metadata_to_kind(
        {"kind": "asset-library", "extra_metadata": None}, kind="presentation"
    )
    assert coerced["extra_metadata"] == {} and dropped == []


def test_duplicate_of_a_legacy_row_with_a_non_object_extra_metadata_records_the_loss(monkeypatch):
    """The endpoint counterpart: the copy lands, and its audit entry names the
    discarded value instead of claiming nothing was dropped."""
    row, audit = _duplicate(monkeypatch, "asset-library", extra_metadata=["not", "an", "object"])
    assert row.kind == "presentation" and row.extra_metadata == {}
    assert audit.details["kind_mapped_from"] == "asset-library"
    assert len(audit.details["metadata_dropped"]) == 1
    assert "extra_metadata must be a JSON object" in audit.details["metadata_dropped"][0]
    assert "got list" in audit.details["metadata_dropped"][0]


def test_duplicate_of_a_legacy_library_is_a_presentation_template_and_the_audit_says_so(monkeypatch):
    """Behavioural counterpart of the source pin above: the copy of an
    ``asset-library`` row lands as a presentation template, the strict pipeline
    is not asked to create a retired kind, and the audit entry records what the
    kind was mapped from."""
    row, audit = _duplicate(monkeypatch, "asset-library")
    assert row.kind == "presentation"
    assert row.name == "Partner logos (copy)"
    assert row.is_company_managed is False
    assert audit.action == "duplicate"
    assert audit.details["kind"] == "presentation"
    assert audit.details["kind_mapped_from"] == "asset-library"


@pytest.mark.parametrize("source_kind", ["report", "presentation"])
def test_duplicate_keeps_a_supported_kind_and_records_no_mapping(monkeypatch, source_kind):
    """The mapping is for retired kinds only: a report skin is copied as a report
    skin, a presentation template as a presentation template."""
    row, audit = _duplicate(monkeypatch, source_kind)
    assert row.kind == source_kind
    assert "kind_mapped_from" not in audit.details and "metadata_dropped" not in audit.details


def test_duplicate_canonicalises_a_pre_closed_choice_spelling_without_calling_it_a_mapping(monkeypatch):
    """A free-text ``Presentation`` from before the kind became a closed choice
    is a supported kind spelt differently, not a retired one: the copy comes out canonical and the audit
    does not claim a kind was mapped."""
    row, audit = _duplicate(monkeypatch, "Presentation")
    assert row.kind == "presentation"
    assert "kind_mapped_from" not in audit.details


def test_import_never_creates_a_company_managed_container():
    """Publishing a template org-wide is a human action in the Templates UI,
    whatever the importer's role. An import is not a publishing channel."""
    import inspect

    from app.api.v1 import template_containers as api

    source = inspect.getsource(api._persist_imported_package)
    assert "is_company_managed=False" in source
    # And nothing may read the flag back off the package.
    assert 'data.get("is_company_managed")' not in source


def test_import_remaps_asset_ids():
    import inspect

    from app.api.v1 import template_containers as api

    source = inspect.getsource(api._persist_imported_package)
    assert "remap_asset_ids(" in source
    assert "layouts" in source and "extra_metadata" in source
    # The palette's file-id keys (`mark_file_id` joined `logo_file_id`
    # / `logo_light_file_id`) are references like any other: an explicit id
    # left as exported would point at the other instance's row.
    assert "container.theme_config = remap_asset_ids(container.theme_config, id_map)" in source


def test_remap_rewrites_the_palettes_file_ids_and_leaves_its_colours_alone():
    """Every ``THEME_FILE_ID_KEYS`` value of the base palette and of a
    ``theme_variants`` palette follows the imported asset; a colour or a font
    token is never a mapping key, so nothing else moves."""
    from app.services.presentations import THEME_FILE_ID_KEYS

    mark = "33333333-3333-3333-3333-333333333333"
    mapping = {LOGO_ID: "new-logo", mark: "new-mark"}
    theme_config = {
        "accent": "#ff4700",
        "background": "hsl(220 54% 8%)",
        "logo_file_id": LOGO_ID,
        "mark_file_id": mark,
        "logo_light_file_id": "44444444-4444-4444-4444-444444444444",
    }
    assert set(THEME_FILE_ID_KEYS) == {"logo_file_id", "logo_light_file_id", "mark_file_id"}
    assert remap_asset_ids(theme_config, mapping) == {
        "accent": "#ff4700",
        "background": "hsl(220 54% 8%)",
        "logo_file_id": "new-logo",
        "mark_file_id": "new-mark",
        # An id no imported asset carried stays as it is: the import did not
        # bring that file, and the role fallback fills only an ABSENT key.
        "logo_light_file_id": "44444444-4444-4444-4444-444444444444",
    }
    extra = {"theme_variants": {"light": {"background": "#fff", "mark_file_id": mark}}}
    assert remap_asset_ids(extra, mapping) == {
        "theme_variants": {"light": {"background": "#fff", "mark_file_id": "new-mark"}}
    }


def test_import_enforces_the_asset_cap_and_per_asset_size():
    import inspect

    from app.api.v1 import template_containers as api

    source = inspect.getsource(api._persist_imported_package)
    assert "MAX_ASSETS_PER_CONTAINER" in source
    assert "MAX_UPLOAD_SIZE_BYTES" in source


def test_import_is_gated_like_creating_a_container():
    """An ungated import would be the way around the gate on creating one."""
    import inspect

    from app.api.v1 import template_containers as api

    assert "require_agentic_ai" in inspect.getsource(api.import_template_container)
    assert "require_agentic_ai" in inspect.getsource(api.create_template_container)


def test_import_writes_an_audit_entry():
    import inspect

    from app.api.v1 import template_containers as api

    source = inspect.getsource(api._persist_imported_package)
    # The action is a parameter since in-app duplication reuses this
    # pipeline under its own audit action; an import still records "import".
    assert 'audit_action: str = "import"' in source
    assert "action=audit_action" in source
    assert 'entity_type="template_container"' in source
    duplicate = inspect.getsource(api.duplicate_template_container)
    assert 'audit_action="duplicate"' in duplicate


def test_a_duplicate_of_a_builtin_describes_itself_not_the_builtin():
    """The seeded description ends with a sentence about the built-in's own
    read-only status and tells the reader to duplicate it. Copied verbatim it is
    false on the copy (editable, and the very duplicate it recommends), so the
    copy says where it comes from instead; an ordinary template's description
    is copied as written."""
    import inspect

    from app.api.v1 import template_containers as api
    from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS
    from app.services.template_containers import derived_description

    for seed in BUILTIN_TEMPLATE_CONTAINERS:
        if seed.get("kind") != "presentation":
            continue
        derived = derived_description(seed["name"], seed["description"], from_builtin=True)
        assert "Read-only" not in derived
        assert "duplicate it" not in derived
        assert derived.endswith(f"Derived from the built-in '{seed['name']}' template; edit it freely.")
        # The design half survives untouched.
        assert derived.startswith(seed["description"].split(" Read-only:")[0])

    assert derived_description("Acme", "Our sales look.", from_builtin=False) == "Our sales look."
    assert derived_description("Acme", None, from_builtin=False) is None
    assert derived_description("Acme", None, from_builtin=True) == (
        "Derived from the built-in 'Acme' template; edit it freely."
    )
    # The route applies it to the copy it persists.
    duplicate = inspect.getsource(api.duplicate_template_container)
    assert 'package.container["description"] = derived_description(' in duplicate
    assert "from_builtin=is_builtin_container(source)" in duplicate


def test_export_is_visibility_scoped():
    """Export must not become a way to read a container the caller cannot see."""
    import inspect

    from app.api.v1 import template_containers as api

    source = inspect.getsource(api.export_template_container)
    assert "_get_visible_container(" in source


def test_export_rejects_an_unknown_format():
    import inspect

    from app.api.v1 import template_containers as api

    source = inspect.getsource(api.export_template_container)
    assert '("zip", "json")' in source


def test_the_real_dev_fixture_is_an_importable_package():
    """The claim that motivated reusing this layout: the fixture in
    ``dev-docs/presentation-template-package/`` is a real package, not a parallel
    format that can drift from the exporter. If someone changes the fixture into
    something the importer rejects, this fails."""
    import pathlib

    root = (
        pathlib.Path(__file__).resolve().parents[2]
        / "dev-docs"
        / "presentation-template-package"
    )
    if not root.is_dir():  # pragma: no cover - fixture removed
        pytest.skip("dev-docs fixture not present in this checkout")

    fixture = json.loads((root / "template-container.json").read_text(encoding="utf-8"))
    assets = [
        PackagedAsset(
            filename=path.name,
            data=path.read_bytes(),
            asset_role="logo" if "logo" in path.name else "background",
            content_type="image/svg+xml",
        )
        for path in sorted((root / "assets").glob("*.svg"))
    ]
    assert assets, "the fixture should ship at least one asset"

    package = parse_package(build_package_zip(fixture, assets))
    for field in EXPORTED_FIELDS:
        assert package.container.get(field) == fixture.get(field), field
    assert {a.filename for a in package.assets} == {a.filename for a in assets}
    # And the fixture's layouts must still satisfy the container validator, or the
    # "import it and it works" promise is hollow.
    from app.services.template_containers import validate_layouts

    _layouts, problems = validate_layouts(
        package.container["layouts"], kind=package.container["kind"]
    )
    assert problems == []


def test_import_validates_the_palette_against_the_container_kind():
    """A report template honours a wider palette than a deck. Validating
    without `kind` would silently strip an imported report template's risk
    colours — the palette half-applies and nobody can tell why."""
    import inspect

    from app.api.v1 import template_containers as api

    source = inspect.getsource(api._persist_imported_package)
    assert "validate_theme_config(" in source
    assert "kind=kind" in source


def test_a_report_template_keeps_its_extra_palette_keys_through_validation():
    """The behaviour the wiring above buys, asserted end-to-end on the validator."""
    from app.services.template_containers import validate_theme_config

    palette = {"accent": "#ff4700", "critical": "#c82118", "paper": "#efe8dc"}
    accepted, problems = validate_theme_config(palette, kind="report")
    assert accepted == palette
    assert problems == []

    # The same package imported as a presentation keeps only what a deck renders.
    deck_accepted, deck_problems = validate_theme_config(palette, kind="presentation")
    assert deck_accepted == {"accent": "#ff4700"}
    assert deck_problems


# ── Review fixes ──────────────────────────────────────────────────────


def test_duplicate_extensionless_names_suffix_from_the_original_base():
    """Suffixing the previous *candidate* instead of the original base gives
    `asset-2-3` for the third duplicate, growing with every collision."""
    assets = [
        PackagedAsset(filename="logo", data=b"a"),
        PackagedAsset(filename="logo", data=b"b"),
        PackagedAsset(filename="logo", data=b"c"),
        PackagedAsset(filename="logo", data=b"d"),
    ]
    raw = build_package_zip(_container(), assets)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = sorted(
            n[len(ASSET_DIR) :] for n in archive.namelist() if n.startswith(ASSET_DIR)
        )
    assert names == ["logo", "logo-2", "logo-3", "logo-4"]


def test_duplicate_names_with_an_extension_keep_it():
    assets = [
        PackagedAsset(filename="logo.svg", data=b"a"),
        PackagedAsset(filename="logo.svg", data=b"b"),
        PackagedAsset(filename="logo.svg", data=b"c"),
    ]
    with zipfile.ZipFile(io.BytesIO(build_package_zip(_container(), assets))) as archive:
        names = sorted(
            n[len(ASSET_DIR) :] for n in archive.namelist() if n.startswith(ASSET_DIR)
        )
    assert names == ["logo-2.svg", "logo-3.svg", "logo.svg"]


def test_an_oversized_manifest_is_refused():
    """The manifest is read BEFORE any asset, so a running total that starts after
    it leaves a highly-compressible manifest as the hole in the zip-bomb budget."""
    bloated = build_manifest(_container(description="x" * (MAX_MANIFEST_BYTES + 1024)), [])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST_NAME, json.dumps(bloated))
    with pytest.raises(TemplatePackageError) as exc:
        parse_package(buffer.getvalue())
    assert MANIFEST_NAME in str(exc.value)


def test_the_manifest_counts_against_the_uncompressed_budget():
    """Not just capped on its own — it is part of what the archive expands to."""
    import inspect

    from app.services import template_package as mod

    source = inspect.getsource(mod.parse_package)
    assert "total = len(manifest_raw)" in source


def test_entries_are_read_bounded_not_measured_after_the_fact():
    """`archive.read(entry)` then checking the length is not a bound: the entry is
    already in memory, which is exactly what a zip bomb wants."""
    import inspect

    from app.services import template_package as mod

    source = inspect.getsource(mod.parse_package)
    assert "_read_bounded(" in source
    assert "archive.read(" not in source
    # And the bound must never be taken from the archive's own declared size.
    assert "file_size" not in source


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("description", {"nested": "object"}),
        ("theme", ["an", "array"]),
        ("aspect_ratio", 169),
        ("name", {"not": "a string"}),
        ("tags", "comma,separated"),
        ("layouts", {"id": "title"}),
        ("theme_config", ["accent", "#fff"]),
        ("extra_metadata", "a string"),
    ],
)
def test_a_field_of_the_wrong_type_is_refused_readably(field, bad_value):
    """These reach `.strip()` / iteration in the persistence layer, so an unchecked
    type surfaces as a 500. Refusing here makes it a 422 that names the field."""
    bundle = build_manifest(_container(), [])
    bundle[PACKAGE_EXPORT_TYPE][field] = bad_value
    with pytest.raises(TemplatePackageError) as exc:
        parse_bundle(bundle)
    message = str(exc.value)
    assert field in message
    assert "wrong type" in message


def test_absent_optional_fields_are_still_fine():
    """The type check must not turn optional into required."""
    minimal = {"name": "Bare Template"}
    package = parse_bundle(build_manifest(minimal, []))
    assert package.container["name"] == "Bare Template"


def test_the_import_route_bounds_the_upload_read():
    """Reading the whole upload and checking afterwards means an oversized file is
    already in memory."""
    import inspect

    from app.api.v1 import template_containers as api

    source = inspect.getsource(api.import_template_container)
    assert "file.read(MAX_PACKAGE_BYTES + 1)" in source
    assert "await file.read()" not in source


def test_the_persist_layer_coerces_text_fields():
    """Belt-and-braces behind the parser: this helper is also reachable from the
    seed script, so neither caller can crash the insert on a stray type."""
    import inspect

    from app.api.v1 import template_containers as api

    source = inspect.getsource(api._persist_imported_package)
    for field in ("name", "description", "theme", "aspect_ratio"):
        assert f'str(data.get("{field}")' in source, field


def test_the_json_export_docstring_does_not_promise_an_asset_count():
    """It returns the manifest, which has no `asset_count` — the docstring used to
    claim otherwise."""
    import inspect

    from app.api.v1 import template_containers as api

    # Whitespace-normalised: the docstring wraps, so a literal substring match
    # would break on reflow rather than on the claim changing.
    doc = " ".join((inspect.getdoc(api.export_template_container) or "").split())
    assert "asset_count" not in doc
    assert "``assets`` list is empty" in doc


# ── An unreadable member ─────────────────────────────────────────────


@pytest.mark.parametrize("how", ["damaged stream", "unknown compression", "encrypted"])
@pytest.mark.parametrize("member", [MANIFEST_NAME, "logo.svg"])
def test_an_unreadable_member_is_refused_as_a_package_error(member, how):
    """A ``TemplatePackageError`` (the route's 422), never the ``BadZipFile`` reading
    it raises (a 500), and never reported as a package too large."""
    assets = [PackagedAsset(filename="logo.svg", data=bytes(range(256)) * 64, asset_role="logo")]
    package = build_package_zip(_container(), assets)
    name = next(n for n in zipfile.ZipFile(io.BytesIO(package)).namelist() if n.endswith(member))
    raw = corrupt_member(package, name, how)
    with pytest.raises(TemplatePackageError, match="not a readable ZIP archive") as raised:
        parse_package(raw)
    assert "expands" not in str(raised.value)
