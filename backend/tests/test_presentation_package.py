"""Portable presentation packages — export / import.

A deck is a spec plus two things that only mean something on the install it
was made on: the id of the template that styles it and the platform file ids
its images point at. These tests pin the contract that lets a deck move:

1. A deck **round-trips**: what the export writes, the import reads.
2. Assets travel — matched to what the target install already holds (a seeded
   brand asset) before being copied from the embedded bytes, and every file id
   in the spec is **rewritten** to the local one.
3. A reference that can be neither matched nor copied **fails the import**
   naming the file — never a deck with a silent hole.
4. The template is resolved by slug, then name, then the install's default,
   and the report says which.
5. Parsing an upload is parsing hostile input: bounded on size and count,
   hashes checked, filenames reduced to a safe basename, SVGs sanitized.

No DB required: fake-session style as ``test_presentation_defaults.py``; the
API wiring is asserted structurally.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.presentation_package import (
    MAX_EMBEDDED_ASSET_BYTES,
    MAX_PACKAGE_ASSETS,
    MAX_PACKAGE_BYTES,
    PACKAGE_EXPORT_TYPE,
    PACKAGE_VERSION,
    PRESENTATION_IMPORT_SOURCE,
    PackagedAsset,
    PresentationPackage,
    PresentationPackageError,
    build_presentation_package,
    import_presentation_package,
    package_filename,
    parse_presentation_package,
    safe_filename,
)

LOGO_ID = "11111111-1111-1111-1111-111111111111"
CHART_ID = "22222222-2222-2222-2222-222222222222"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'


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
    def __init__(self, results=None, objects=None):
        self._results = list(results or [])
        self._objects = dict(objects or {})
        self.statements: list = []
        self.added: list = []
        self.flushed = 0

    async def execute(self, statement, *a, **k):
        self.statements.append(statement)
        return self._results.pop(0) if self._results else _Result()

    async def get(self, model, key):
        return self._objects.get(key)

    async def flush(self):
        self.flushed += 1

    def add(self, obj):
        self.added.append(obj)


def _user(role: str = "user"):
    return SimpleNamespace(
        id=uuid.uuid4(), role=role, is_admin=(role == "admin"), full_name="Ada", email="ada@example.com"
    )


def _spec(**extra) -> dict:
    spec = {
        "title": "FY2027 strategy",
        "theme": "dark",
        "aspect_ratio": "16:9",
        "slides": [
            {"layout": "title", "title": "FY2027", "subtitle": "One platform", "logo_file_id": LOGO_ID},
            {
                "layout": "content",
                "title": "Growth",
                "blocks": [{"type": "image", "file_id": CHART_ID, "alt": "ARR chart"}],
            },
        ],
    }
    spec.update(extra)
    return spec


def _container(name="Filigran Brand Deck", slug="filigran-brand", **extra):
    from app.models.template_container import TemplateContainer

    return TemplateContainer(
        id=uuid.uuid4(),
        name=name,
        kind="presentation",
        tags=[f"slug:{slug}", "builtin"] if slug else ["mine"],
        is_company_managed=bool(slug),
        **extra,
    )


def _file(file_id: str, filename: str, *, container=None, user_id=None, source=None, object_name=None):
    from app.models.file_attachment import FileAttachment
    from app.models.template_container import TEMPLATE_ASSET_SOURCE

    return FileAttachment(
        id=uuid.UUID(file_id),
        user_id=user_id,
        filename=filename,
        content_type="image/svg+xml" if filename.endswith(".svg") else "image/png",
        size=len(SVG if filename.endswith(".svg") else PNG),
        s3_object_name=object_name or f"objects/{filename}",
        source=source or (TEMPLATE_ASSET_SOURCE if container is not None else "agent_generated"),
        template_container_id=container.id if container is not None else None,
        asset_role="logo" if container is not None else None,
        description="Brand mark" if container is not None else None,
    )


def _deck(spec: dict, *, user_id, template_container_id=None):
    from app.models.presentation import Presentation

    return Presentation(
        id=uuid.uuid4(),
        user_id=user_id,
        title=spec["title"],
        theme=spec.get("theme", "dark"),
        slide_count=len(spec["slides"]),
        revision=3,
        spec=spec,
        template_container_id=template_container_id,
    )


def _envelope(spec: dict | None = None, *, assets: list[dict] | None = None, template=None, **overrides) -> dict:
    body = {
        "source_id": str(uuid.uuid4()),
        "title": "FY2027 strategy",
        "template": template,
        "spec": spec if spec is not None else _spec(),
        "assets": assets if assets is not None else [],
    }
    document = {
        "version": PACKAGE_VERSION,
        "export_type": PACKAGE_EXPORT_TYPE,
        "exported_at": "2026-09-09T07:00:00+00:00",
        "source_platform": "XTM One (dev)",
        "presentation": body,
    }
    document.update(overrides)
    return document


def _raw(document: dict) -> bytes:
    return json.dumps(document).encode("utf-8")


def _embedded(file_id: str, filename: str, data: bytes, **extra) -> dict:
    entry = {
        "id": file_id,
        "filename": filename,
        "content_type": "image/png",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "asset_role": None,
        "description": None,
        "template": None,
        "data_base64": base64.b64encode(data).decode("ascii"),
    }
    entry.update(extra)
    return entry


@pytest.fixture
def storage(monkeypatch):
    """A fake object store: ``download`` serves seeded bytes, both record the keys."""
    import app.services.file_storage as file_storage_module

    store = {"objects/logo.svg": SVG, "objects/chart.png": PNG}
    uploads: list[tuple[bytes, str, str, str]] = []
    downloads: list[str] = []

    async def download(object_name):
        downloads.append(object_name)
        return store[object_name]

    async def upload(*, data, filename, content_type, prefix):
        uploads.append((data, filename, content_type, prefix))
        return f"{prefix}/{uuid.uuid4().hex}-{filename}"

    monkeypatch.setattr(file_storage_module.file_storage, "download", download)
    monkeypatch.setattr(file_storage_module.file_storage, "upload", upload)
    return SimpleNamespace(store=store, uploads=uploads, downloads=downloads)


@pytest.fixture
def saved(monkeypatch):
    """Capture what the import hands to ``save_presentation`` instead of writing."""
    import app.services.presentations as presentations

    calls: list[dict] = []

    async def fake_save(db, **kwargs):
        calls.append(kwargs)
        from app.models.presentation import Presentation

        spec = kwargs["spec_payload"]
        return Presentation(
            id=uuid.uuid4(),
            user_id=kwargs["user_id"],
            title=spec["title"],
            theme=spec.get("theme", "dark"),
            slide_count=len(spec["slides"]),
            revision=1,
            spec=spec,
            template_container_id=kwargs.get("template_container_id"),
        )

    monkeypatch.setattr(presentations, "save_presentation", fake_save)
    return calls


def _allow_all(monkeypatch, *, deny: set[str] = frozenset()):
    import app.services.file_access as file_access

    async def authorize(file_att, user, db, *, presentation_id=None):
        if str(file_att.id) in deny:
            raise HTTPException(status_code=403, detail="Access denied")

    monkeypatch.setattr(file_access, "authorize_file_access", authorize)


# -- safe_filename ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("logo.svg", "logo.svg"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\x\\chart.png", "chart.png"),
        ("/abs/path/to/file name (v2).png", "file name (v2).png"),
        ("weird\u202ename.png", "weird_name.png"),
        ("", "asset"),
        (None, "asset"),
        ("..", "asset"),
        ("   ", "asset"),
    ],
)
def test_filenames_are_reduced_to_a_safe_basename(raw, expected):
    assert safe_filename(raw) == expected


def test_overlong_filenames_keep_their_extension():
    name = safe_filename("a" * 400 + ".png")
    assert name.endswith(".png")
    assert len(name) <= 200


# -- Export -------------------------------------------------------------------


def test_export_embeds_readable_files_and_names_their_template(monkeypatch, storage):
    user = _user()
    brand = _container()
    logo = _file(LOGO_ID, "logo.svg", container=brand)
    chart = _file(CHART_ID, "chart.png", user_id=user.id)
    deck = _deck(_spec(), user_id=user.id, template_container_id=brand.id)
    _allow_all(monkeypatch)
    session = _FakeSession(
        results=[_Result(rows=[logo, chart]), _Result(rows=[brand])],
        objects={brand.id: brand},
    )

    bundle = asyncio.run(build_presentation_package(session, deck, user=user, platform_name="XTM One"))

    assert bundle["version"] == PACKAGE_VERSION
    assert bundle["export_type"] == PACKAGE_EXPORT_TYPE
    assert bundle["source_platform"] == "XTM One"
    body = bundle["presentation"]
    assert body["source_id"] == str(deck.id)
    assert body["title"] == "FY2027 strategy"
    assert body["kind"] == "deck"
    assert body["revision"] == 3
    assert body["template"] == {"slug": "filigran-brand", "name": "Filigran Brand Deck", "kind": "presentation"}
    assert body["spec"] == _spec()
    by_id = {entry["id"]: entry for entry in body["assets"]}
    assert set(by_id) == {LOGO_ID, CHART_ID}
    # The brand logo is named by its template so the import can MATCH it there
    # instead of duplicating a seeded asset; its bytes still travel as the
    # fallback for an install that never shipped that template.
    assert by_id[LOGO_ID]["template"] == {"slug": "filigran-brand", "name": "Filigran Brand Deck"}
    assert by_id[LOGO_ID]["asset_role"] == "logo"
    assert by_id[LOGO_ID]["description"] == "Brand mark"
    assert base64.b64decode(by_id[LOGO_ID]["data_base64"]) == SVG
    assert by_id[LOGO_ID]["sha256"] == hashlib.sha256(SVG).hexdigest()
    # A generated image has no template: bytes only.
    assert by_id[CHART_ID]["template"] is None
    assert base64.b64decode(by_id[CHART_ID]["data_base64"]) == PNG
    # The whole thing is JSON-serialisable as returned.
    json.dumps(bundle)


def test_export_never_embeds_a_file_the_exporter_may_not_read(monkeypatch, storage):
    """What ``authorize_file_access`` refuses travels as a reference only - no
    bytes, no hash - so the package never carries more than its exporter could
    fetch through the download route."""
    user = _user()
    chart = _file(CHART_ID, "chart.png", user_id=uuid.uuid4())
    deck = _deck({"title": "D", "slides": [_spec()["slides"][1]]}, user_id=uuid.uuid4())
    _allow_all(monkeypatch, deny={CHART_ID})
    session = _FakeSession(results=[_Result(rows=[chart])])

    bundle = asyncio.run(build_presentation_package(session, deck, user=user))

    (entry,) = bundle["presentation"]["assets"]
    assert entry["id"] == CHART_ID
    assert entry["filename"] == "chart.png"
    assert entry["data_base64"] is None
    assert entry["sha256"] is None
    assert storage.uploads == []


def test_export_by_a_reader_embeds_the_pictures_the_deck_shows(monkeypatch, storage):
    """Reading a deck grants the pictures its slides show, so a
    reader's package embeds the owner's generated picture - exactly what that
    reader could already fetch picture by picture through the download route,
    never more. Real authorizer: the deck is passed as the hint and resolved by
    primary key under the READER's visibility, with no text scan."""
    from sqlalchemy.dialects import postgresql

    import app.api.v1.helpers as helpers

    reader = _user()
    owner = uuid.uuid4()
    chart = _file(CHART_ID, "chart.png", user_id=owner)
    deck = _deck({"title": "D", "slides": [_spec()["slides"][1]]}, user_id=owner)

    async def _no_groups(_db, _user_id):
        return []

    monkeypatch.setattr(helpers, "get_user_group_ids", _no_groups)
    # The file rows, then the hinted deck lookup answered with the deck's spec.
    session = _FakeSession(results=[_Result(rows=[chart]), _Result(one=deck.spec)])

    bundle = asyncio.run(build_presentation_package(session, deck, user=reader))

    (entry,) = bundle["presentation"]["assets"]
    assert entry["id"] == CHART_ID
    assert base64.b64decode(entry["data_base64"]) == PNG
    assert entry["sha256"] == hashlib.sha256(PNG).hexdigest()
    grant = [
        str(s.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        for s in session.statements[1:]
    ]
    assert len(grant) == 1, "one primary-key lookup of the hinted deck"
    assert f"presentations.id = '{deck.id}'" in grant[0]
    assert f"presentations.user_id = '{reader.id}'" in grant[0], "under the reader's visibility, not the owner's"
    assert "ILIKE" not in grant[0]


def test_export_keeps_a_dangling_reference_as_missing(monkeypatch, storage):
    """The export does not edit the deck: a reference to a file that is gone
    travels flagged so the import side reports it, not this side."""
    user = _user()
    deck = _deck({"title": "D", "slides": [_spec()["slides"][1]]}, user_id=user.id)
    _allow_all(monkeypatch)
    session = _FakeSession(results=[_Result(rows=[])])

    bundle = asyncio.run(build_presentation_package(session, deck, user=user))

    assert bundle["presentation"]["assets"] == [{"id": CHART_ID, "filename": None, "missing": True}]


def test_a_lost_object_downgrades_to_a_reference_instead_of_failing(monkeypatch, storage):
    user = _user()
    chart = _file(CHART_ID, "chart.png", user_id=user.id, object_name="objects/vanished.png")
    deck = _deck({"title": "D", "slides": [_spec()["slides"][1]]}, user_id=user.id)
    _allow_all(monkeypatch)
    session = _FakeSession(results=[_Result(rows=[chart])])

    bundle = asyncio.run(build_presentation_package(session, deck, user=user))

    (entry,) = bundle["presentation"]["assets"]
    assert entry["data_base64"] is None


def test_the_download_name_is_the_slugified_title():
    deck = SimpleNamespace(title="FY2027 Strategy: One Platform!")
    assert package_filename(deck) == "fy2027-strategy-one-platform.presentation.json"
    assert package_filename(SimpleNamespace(title="   ")) == "presentation.presentation.json"


# -- Parse --------------------------------------------------------------------


def test_the_export_parses_back(monkeypatch, storage):
    user = _user()
    brand = _container()
    logo = _file(LOGO_ID, "logo.svg", container=brand)
    chart = _file(CHART_ID, "chart.png", user_id=user.id)
    deck = _deck(_spec(), user_id=user.id, template_container_id=brand.id)
    _allow_all(monkeypatch)
    session = _FakeSession(results=[_Result(rows=[logo, chart]), _Result(rows=[brand])], objects={brand.id: brand})
    bundle = asyncio.run(build_presentation_package(session, deck, user=user, platform_name="XTM One"))

    package = parse_presentation_package(_raw(bundle))

    assert package.spec == _spec()
    assert package.template_slug == "filigran-brand"
    assert package.template_name == "Filigran Brand Deck"
    assert package.source_platform == "XTM One"
    by_id = {asset.original_id: asset for asset in package.assets}
    assert by_id[LOGO_ID].data == SVG
    assert by_id[LOGO_ID].template_slug == "filigran-brand"
    assert by_id[LOGO_ID].asset_role == "logo"
    assert by_id[CHART_ID].data == PNG
    assert by_id[CHART_ID].template_slug is None


def test_a_bare_spec_is_accepted_as_a_package_without_assets():
    package = parse_presentation_package(_raw(_spec()))
    assert package.spec == _spec()
    assert package.assets == []
    assert package.template_slug is None


def test_a_bom_is_tolerated():
    package = parse_presentation_package(b"\xef\xbb\xbf" + _raw(_envelope()))
    assert package.spec["title"] == "FY2027 strategy"


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda d: d.__setitem__("export_type", "template_container"), "not a presentation package"),
        (lambda d: d.__setitem__("version", 99), "Unsupported presentation package version"),
        (lambda d: d.__setitem__("presentation", "nope"), "'presentation' object"),
        (lambda d: d["presentation"].__setitem__("spec", []), "'presentation.spec' object"),
        (lambda d: d["presentation"].__setitem__("template", "brand"), "'presentation.template'"),
        (lambda d: d["presentation"].__setitem__("assets", {}), "'presentation.assets' must be a list"),
        (lambda d: d["presentation"].__setitem__("assets", ["x"]), "assets[0] must be an object"),
        (lambda d: d["presentation"].__setitem__("assets", [{"filename": "a.png"}]), "assets[0].id is required"),
        (
            lambda d: d["presentation"].__setitem__("assets", [{"id": "a"}, {"id": "a"}]),
            "listed twice",
        ),
        (
            lambda d: d["presentation"].__setitem__("assets", [{"id": "a", "data_base64": 12}]),
            "data_base64 must be a string",
        ),
        (
            lambda d: d["presentation"].__setitem__("assets", [{"id": "a", "filename": "x.png", "data_base64": "%%%"}]),
            "not valid base64",
        ),
        (
            lambda d: d["presentation"].__setitem__(
                "assets",
                [{"id": "a", "filename": "x.png", "data_base64": base64.b64encode(PNG).decode(), "sha256": "0" * 64}],
            ),
            "does not match its declared sha256",
        ),
        (
            # A reference without bytes is not verified against anything, so a
            # value that is not a digest would be taken at its word.
            lambda d: d["presentation"].__setitem__("assets", [{"id": "a", "filename": "x.png", "sha256": "seed"}]),
            "assets[0].sha256 must be a SHA-256 digest",
        ),
        (
            lambda d: d["presentation"].__setitem__("assets", [{"id": "a", "filename": "x.png", "sha256": "g" * 64}]),
            "assets[0].sha256 must be a SHA-256 digest",
        ),
        (
            lambda d: d["presentation"].__setitem__("assets", [{"id": "a", "missing": True, "sha256": "seed"}]),
            "assets[0].sha256 must be a SHA-256 digest",
        ),
        (
            lambda d: d["presentation"].__setitem__("assets", [{"id": "a", "template": "brand"}]),
            "assets[0].template must be an object",
        ),
    ],
)
def test_a_malformed_package_is_refused_with_a_readable_message(mutate, fragment):
    document = _envelope()
    mutate(document)
    with pytest.raises(PresentationPackageError, match=fragment.replace("[", r"\[").replace("]", r"\]")):
        parse_presentation_package(_raw(document))


@pytest.mark.parametrize("raw", [b"", b"not json", b"[1, 2]", b'"a string"', b"\xff\xfe"])
def test_garbage_is_refused(raw):
    with pytest.raises(PresentationPackageError):
        parse_presentation_package(raw)


def test_an_oversized_document_is_refused_before_parsing():
    with pytest.raises(PresentationPackageError, match="too large"):
        parse_presentation_package(b" " * (MAX_PACKAGE_BYTES + 1))


def test_too_many_assets_are_refused():
    document = _envelope(assets=[{"id": str(i)} for i in range(MAX_PACKAGE_ASSETS + 1)])
    with pytest.raises(PresentationPackageError, match="too many assets"):
        parse_presentation_package(_raw(document))


def test_an_oversized_embedded_asset_is_refused_before_it_is_decoded(monkeypatch):
    """The base64 length bound is checked first, so a multi-GB blob is never
    materialised — ``b64decode`` must not even run."""
    import app.services.presentation_package as module

    def boom(*a, **k):
        raise AssertionError("decoded an oversized blob")

    monkeypatch.setattr(module.base64, "b64decode", boom)
    document = _envelope(
        assets=[{"id": "a", "filename": "big.png", "data_base64": "A" * ((MAX_EMBEDDED_ASSET_BYTES * 4) // 3 + 8)}]
    )
    with pytest.raises(PresentationPackageError, match="too large"):
        parse_presentation_package(_raw(document))


def test_the_packaged_digest_is_canonical_and_known_for_every_embedded_file():
    """What the import compares namesakes with: the declared digest in lower
    case, else the digest of the embedded bytes; ``None`` only for a reference
    that carries neither."""
    png_digest = hashlib.sha256(PNG).hexdigest()
    document = _envelope(
        assets=[
            _embedded("a", "upper.png", PNG, sha256=png_digest.upper()),
            _embedded("b", "undeclared.png", PNG, sha256=None),
            {"id": "c", "filename": "reference.svg", "sha256": hashlib.sha256(SVG).hexdigest().upper()},
            {"id": "d", "filename": "bare.svg"},
        ]
    )

    upper, undeclared, reference, bare = parse_presentation_package(_raw(document)).assets

    assert upper.sha256 == undeclared.sha256 == png_digest
    assert reference.sha256 == hashlib.sha256(SVG).hexdigest() and reference.data is None
    assert bare.sha256 is None


def test_hostile_filenames_are_sanitized_on_parse():
    document = _envelope(assets=[_embedded("a", "../../../etc/shadow.png", PNG)])
    (asset,) = parse_presentation_package(_raw(document)).assets
    assert asset.filename == "shadow.png"


def test_a_reference_flagged_missing_by_the_exporter_parses_without_bytes():
    document = _envelope(assets=[{"id": CHART_ID, "filename": None, "missing": True}])
    (asset,) = parse_presentation_package(_raw(document)).assets
    assert asset.original_id == CHART_ID
    assert asset.data is None
    assert asset.missing is True


@pytest.mark.parametrize("marker", ["false", "true", 1, 0, {}, [], "yes"], ids=repr)
def test_a_missing_marker_that_is_not_a_boolean_is_refused(marker):
    """The import gives a ``missing`` reference a fresh id instead of resolving
    it, so the marker must be exactly the boolean the exporter writes:
    ``"false"`` or ``1`` read by truthiness would skip the resolution and store
    a hole, ``{}`` would silently mean the opposite of ``"true"``."""
    document = _envelope(assets=[{"id": CHART_ID, "filename": "chart.png", "missing": marker}])
    with pytest.raises(PresentationPackageError, match=r"assets\[0\]\.missing must be true or false"):
        parse_presentation_package(_raw(document))


@pytest.mark.parametrize("entry", [{"missing": False}, {"missing": None}, {}], ids=["false", "null", "absent"])
def test_a_reference_not_flagged_missing_goes_through_normal_resolution(entry):
    document = _envelope(assets=[{"id": CHART_ID, "filename": "chart.png", **entry}])
    (asset,) = parse_presentation_package(_raw(document)).assets
    assert asset.missing is False and asset.filename == "chart.png"


# -- Import -------------------------------------------------------------------


def _package(*, assets: list[PackagedAsset], template_slug="filigran-brand", template_name="Filigran Brand Deck"):
    return PresentationPackage(
        spec=_spec(),
        template_slug=template_slug,
        template_name=template_name,
        assets=assets,
        source_platform="XTM One (dev)",
    )


def _logo_asset(data=SVG, slug="filigran-brand", *, template_name=None, original_id=LOGO_ID):
    return PackagedAsset(
        original_id=original_id,
        filename="logo.svg",
        content_type="image/svg+xml",
        sha256=hashlib.sha256(data).hexdigest() if data else None,
        asset_role="logo",
        template_slug=slug,
        template_name=template_name or ("Filigran Brand Deck" if slug else None),
        data=data,
    )


def _chart_asset(data=PNG):
    return PackagedAsset(original_id=CHART_ID, filename="chart.png", content_type="image/png", data=data)


def test_a_picture_the_exporter_already_missed_goes_instead_of_refusing_the_import(storage, saved):
    """A deck exported with a picture whose file was already gone (a removed
    built-in template's asset) imports without it: the reference gets a
    fresh id that names no file anywhere - never a probe of the exported id on
    this install - carried past the reference check, and the report says so."""
    user = _user()
    missing_chart = PackagedAsset(original_id=CHART_ID, filename="asset-2", missing=True)
    session = _FakeSession()
    _presentation, report = asyncio.run(
        import_presentation_package(
            session,
            _package(assets=[_logo_asset(), missing_chart], template_slug=None, template_name=None),
            user=user,
        )
    )
    (call,) = saved
    gone_id = call["spec_payload"]["slides"][1]["blocks"][0]["file_id"]
    assert gone_id != CHART_ID and str(uuid.UUID(gone_id)) == gone_id
    assert set(call["verify_carried"]) == {gone_id}
    assert any("already missing from the exported deck" in warning for warning in report.warnings)
    assert CHART_ID not in json.dumps(call["spec_payload"])


def test_import_matches_local_template_assets_and_copies_the_rest(storage, saved):
    user = _user()
    brand = _container()
    local_logo = _file(str(uuid.uuid4()), "logo.svg", container=brand)
    session = _FakeSession(
        results=[
            _Result(rows=[brand]),  # template by slug
            _Result(rows=[local_logo]),  # the brand's own logo.svg
        ]
    )

    presentation, report = asyncio.run(
        import_presentation_package(session, _package(assets=[_logo_asset(), _chart_asset()]), user=user)
    )

    # The deck is saved through the ONE validated path, as an import revision.
    (call,) = saved
    assert call["user_id"] == user.id
    assert call["author_id"] == user.id
    assert call["change_source"] == "import"
    assert call["change_summary"] == "Imported from XTM One (dev)"
    assert call["verify_files_for"] is user
    assert call["template_container_id"] == brand.id
    spec = call["spec_payload"]
    # The logo was MATCHED to the local brand asset — no duplicate upload —
    # and the chart, which the target install never held, was copied.
    assert spec["slides"][0]["logo_file_id"] == str(local_logo.id)
    (uploaded,) = storage.uploads
    assert uploaded[0] == PNG
    assert uploaded[1] == "chart.png"
    assert uploaded[3] == f"presentation-import/{user.id}"
    (new_row,) = session.added
    assert spec["slides"][1]["blocks"][0]["file_id"] == str(new_row.id)
    assert new_row.user_id == user.id
    assert new_row.source == PRESENTATION_IMPORT_SOURCE
    assert new_row.size == len(PNG)
    # Nothing from the source install survives in the spec.
    assert LOGO_ID not in json.dumps(spec) and CHART_ID not in json.dumps(spec)
    assert presentation.template_container_id == brand.id
    assert report.as_dict() == {
        "assets_total": 2,
        "assets_matched": 1,
        "assets_copied": 1,
        "template": "matched",
        "template_name": "Filigran Brand Deck",
        "warnings": [],
    }


def test_import_copies_a_template_asset_the_target_install_lacks(storage, saved):
    """The brand exists here but never got this particular logo: the embedded
    bytes are the fallback, not a failure."""
    user = _user()
    brand = _container()
    session = _FakeSession(results=[_Result(rows=[brand]), _Result(rows=[])])

    _presentation, report = asyncio.run(
        import_presentation_package(session, _package(assets=[_logo_asset(), _chart_asset()]), user=user)
    )

    assert report.assets_matched == 0
    assert report.assets_copied == 2
    assert [name for _data, name, _ct, _prefix in storage.uploads] == ["logo.svg", "chart.png"]


# -- Import: telling namesakes apart by content -------------------------
#
# Several assets of the matched template can share the packaged filename (a
# dark and a light ``logo.svg``). The package names the file it means by its
# SHA-256, and no object key holds that digest: a seeded key carries a 16-hex
# PREFIX of it, an uploaded one a random name. The rows are handed over by the
# fake session in the order the lookup's ORDER BY returns them.

DARK_LOGO = b'<svg xmlns="http://www.w3.org/2000/svg"><rect fill="black"/></svg>'
LIGHT_LOGO = b'<svg xmlns="http://www.w3.org/2000/svg"><rect fill="white"/></svg>'
OTHER_LOGO = b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="1"/></svg>'
REVIEW = "Quarterly review"


def _namesake(storage, container, data: bytes, *, seeded=False, key=None, sha256=None, filename="logo.svg"):
    """An asset of *container* named *filename*, its bytes held by the fake store."""
    from app.services.template_asset_seed import seed_object_name

    if key is None:
        key = (
            seed_object_name(container.id, hashlib.sha256(data).hexdigest(), filename)
            if seeded
            else f"template-asset/{container.id}/{uuid.uuid4().hex}.svg"
        )
    storage.store[key] = data
    row = _file(str(uuid.uuid4()), filename, container=container, object_name=key)
    row.size = len(data)
    row.sha256 = sha256
    return row


def _review_session():
    """An authored template found by name. Returns it and a session factory
    answering the group ids, the name match, then one namesake lookup per
    packaged file of that template."""
    review = _container(name=REVIEW, slug=None)
    return review, lambda lookups: _FakeSession(
        results=[_Result(rows=[]), _Result(rows=[review]), *(_Result(rows=rows) for rows in lookups)]
    )


def _review_package(*assets):
    return _package(assets=list(assets), template_slug=None, template_name=REVIEW)


def _namesake_sql(session) -> str:
    from sqlalchemy.dialects import postgresql

    compiled = (str(s.compile(dialect=postgresql.dialect())) for s in session.statements)
    return next(sql for sql in compiled if "FROM file_attachments" in sql)


def test_the_packaged_digest_picks_between_seeded_namesakes(storage, saved):
    """A seeded key carries only the first 16 hex characters of the digest, so
    the full packaged digest is never a substring of it. The prefix rules the
    dark logo out without a read; the light one is confirmed on its bytes."""
    user = _user()
    brand = _container()
    dark = _namesake(storage, brand, DARK_LOGO, seeded=True)
    light = _namesake(storage, brand, LIGHT_LOGO, seeded=True)
    session = _FakeSession(results=[_Result(rows=[brand]), _Result(rows=[dark, light])])

    _presentation, report = asyncio.run(
        import_presentation_package(session, _package(assets=[_logo_asset(LIGHT_LOGO), _chart_asset()]), user=user)
    )

    assert saved[0]["spec_payload"]["slides"][0]["logo_file_id"] == str(light.id)
    assert storage.downloads == [light.s3_object_name], "the dark key's prefix rules it out: no read"
    assert report.assets_matched == 1
    assert report.warnings == []


def test_the_packaged_digest_picks_between_uploaded_namesakes(storage, saved):
    """An uploaded asset's key is a random name with no digest in it: the
    namesakes' bytes are read, in listing order, and compared on the full
    digest - the light logo is found although it is listed second."""
    user = _user()
    review, session_for = _review_session()
    dark = _namesake(storage, review, DARK_LOGO)
    light = _namesake(storage, review, LIGHT_LOGO)
    session = session_for([[dark, light]])

    _presentation, report = asyncio.run(
        import_presentation_package(
            session, _review_package(_logo_asset(LIGHT_LOGO, None, template_name=REVIEW), _chart_asset()), user=user
        )
    )

    assert saved[0]["template_container_id"] == review.id
    assert saved[0]["spec_payload"]["slides"][0]["logo_file_id"] == str(light.id)
    assert storage.downloads == [dark.s3_object_name, light.s3_object_name]
    assert report.warnings == []


def test_a_namesake_recording_the_digest_is_matched_without_a_read(storage, saved):
    """``FileAttachment.sha256``, where a row carries it, is the digest of its
    stored bytes: compared exactly, it settles the match with no download."""
    user = _user()
    review, session_for = _review_session()
    dark = _namesake(storage, review, DARK_LOGO)
    light = _namesake(storage, review, LIGHT_LOGO, sha256=hashlib.sha256(LIGHT_LOGO).hexdigest())
    session = session_for([[dark, light]])

    asyncio.run(
        import_presentation_package(
            session, _review_package(_logo_asset(LIGHT_LOGO, None, template_name=REVIEW), _chart_asset()), user=user
        )
    )

    assert saved[0]["spec_payload"]["slides"][0]["logo_file_id"] == str(light.id)
    assert storage.downloads == []


def test_no_namesake_holding_the_packaged_file_falls_back_to_the_first_listed(storage, saved):
    """Nothing holds the packaged bytes: the design's fallback - the name, inside
    the matched template - picks the namesake the template lists first, in an
    ORDER BY that makes the choice the same on every run, and the report says the
    match was made by name only instead of letting a wrong logo pass silently."""
    user = _user()
    review, session_for = _review_session()
    dark = _namesake(storage, review, DARK_LOGO)
    light = _namesake(storage, review, LIGHT_LOGO)
    session = session_for([[dark, light]])

    _presentation, report = asyncio.run(
        import_presentation_package(
            session, _review_package(_logo_asset(OTHER_LOGO, None, template_name=REVIEW), _chart_asset()), user=user
        )
    )

    assert saved[0]["spec_payload"]["slides"][0]["logo_file_id"] == str(dark.id)
    assert (report.assets_matched, report.assets_copied) == (1, 1), "matched by name, never copied"
    assert report.warnings == [
        "2 assets of 'Quarterly review' are named 'logo.svg' and none was confirmed to hold the packaged "
        "file, so the deck uses the one the template lists first."
    ]
    sql = _namesake_sql(session)
    assert "file_attachments.template_container_id = " in sql, "never outside the matched template"
    assert "ORDER BY file_attachments.created_at ASC, file_attachments.id ASC" in sql


def test_a_seeded_key_prefix_alone_never_matches(storage, saved):
    """The 16 hex characters in a seeded key are a pre-filter, not the digest.
    A key whose prefix equals the packaged digest's but whose bytes are another
    file is read, found different on the full digest, and not matched - the
    namesake listed first is used, and said so."""
    from app.services.template_asset_seed import seed_object_name

    user = _user()
    brand = _container()
    first = _namesake(storage, brand, DARK_LOGO, seeded=True)
    wanted = hashlib.sha256(LIGHT_LOGO).hexdigest()
    impostor = _namesake(storage, brand, OTHER_LOGO, key=seed_object_name(brand.id, wanted, "logo.svg"))
    session = _FakeSession(results=[_Result(rows=[brand]), _Result(rows=[first, impostor])])

    _presentation, report = asyncio.run(
        import_presentation_package(session, _package(assets=[_logo_asset(LIGHT_LOGO), _chart_asset()]), user=user)
    )

    assert saved[0]["spec_payload"]["slides"][0]["logo_file_id"] == str(first.id)
    assert storage.downloads == [impostor.s3_object_name], "the prefix hit is confirmed, the ruled-out one never read"
    assert len(report.warnings) == 1 and "none was confirmed" in report.warnings[0]


def test_a_lone_namesake_is_matched_by_name_whatever_its_bytes(storage, saved):
    """One asset of that name: it IS the destination's version of the file (a
    brand logo refreshed since the export) - matched by name, nothing read."""
    user = _user()
    brand = _container()
    only = _namesake(storage, brand, DARK_LOGO, seeded=True)
    session = _FakeSession(results=[_Result(rows=[brand]), _Result(rows=[only])])

    _presentation, report = asyncio.run(
        import_presentation_package(session, _package(assets=[_logo_asset(LIGHT_LOGO), _chart_asset()]), user=user)
    )

    assert saved[0]["spec_payload"]["slides"][0]["logo_file_id"] == str(only.id)
    assert storage.downloads == []
    assert report.warnings == []


def test_a_reference_without_a_digest_takes_the_first_listed_namesake_and_says_so(storage, saved):
    """The exporter could not read the file, so the package names it by
    filename alone: nothing tells the namesakes apart, nothing is read, and the
    choice made by name is reported."""
    user = _user()
    brand = _container()
    dark = _namesake(storage, brand, DARK_LOGO, seeded=True)
    light = _namesake(storage, brand, LIGHT_LOGO, seeded=True)
    session = _FakeSession(results=[_Result(rows=[brand]), _Result(rows=[dark, light])])

    _presentation, report = asyncio.run(
        import_presentation_package(session, _package(assets=[_logo_asset(data=None), _chart_asset()]), user=user)
    )

    assert saved[0]["spec_payload"]["slides"][0]["logo_file_id"] == str(dark.id)
    assert storage.downloads == []
    assert len(report.warnings) == 1 and "none was confirmed" in report.warnings[0]


def test_content_reads_are_bounded_per_packaged_file(storage, saved):
    import app.services.presentation_package as module

    user = _user()
    review, session_for = _review_session()
    namesakes = [
        _namesake(storage, review, b"<svg xmlns='http://www.w3.org/2000/svg' id='%d'/>" % i)
        for i in range(module.MAX_CONTENT_MATCH_READS + 2)
    ]
    session = session_for([namesakes])

    _presentation, report = asyncio.run(
        import_presentation_package(
            session, _review_package(_logo_asset(OTHER_LOGO, None, template_name=REVIEW), _chart_asset()), user=user
        )
    )

    assert storage.downloads == [row.s3_object_name for row in namesakes[: module.MAX_CONTENT_MATCH_READS]]
    assert saved[0]["spec_payload"]["slides"][0]["logo_file_id"] == str(namesakes[0].id)
    assert len(report.warnings) == 1


def test_a_namesake_is_read_once_per_import(storage, saved):
    """A dark and a light ``logo.svg`` in the package compare the same two
    namesakes: each is downloaded once, the second comparison reuses the digest."""
    user = _user()
    review, session_for = _review_session()
    dark = _namesake(storage, review, DARK_LOGO)
    light = _namesake(storage, review, LIGHT_LOGO)
    session = session_for([[dark, light], [dark, light]])
    package = _review_package(
        _logo_asset(DARK_LOGO, None, template_name=REVIEW),
        _logo_asset(LIGHT_LOGO, None, template_name=REVIEW, original_id=CHART_ID),
    )

    _presentation, report = asyncio.run(import_presentation_package(session, package, user=user))

    spec = saved[0]["spec_payload"]
    assert spec["slides"][0]["logo_file_id"] == str(dark.id)
    assert spec["slides"][1]["blocks"][0]["file_id"] == str(light.id)
    assert storage.downloads == [dark.s3_object_name, light.s3_object_name]
    assert (report.assets_matched, report.assets_copied, report.warnings) == (2, 0, [])


def test_content_reads_share_one_byte_budget_per_import(monkeypatch, storage, saved):
    """Past the import's read budget a namesake is not downloaded: the file it
    might have matched falls back to the name rule, and the report says so."""
    import app.services.presentation_package as module

    monkeypatch.setattr(module, "MAX_CONTENT_MATCH_READ_BYTES", len(DARK_LOGO) + len(LIGHT_LOGO) - 1)
    user = _user()
    review, session_for = _review_session()
    dark = _namesake(storage, review, DARK_LOGO)
    light = _namesake(storage, review, LIGHT_LOGO)
    session = session_for([[dark, light], [dark, light]])
    package = _review_package(
        _logo_asset(DARK_LOGO, None, template_name=REVIEW),
        _logo_asset(LIGHT_LOGO, None, template_name=REVIEW, original_id=CHART_ID),
    )

    _presentation, report = asyncio.run(import_presentation_package(session, package, user=user))

    spec = saved[0]["spec_payload"]
    assert spec["slides"][0]["logo_file_id"] == str(dark.id)
    assert spec["slides"][1]["blocks"][0]["file_id"] == str(dark.id), "light was past the budget"
    assert storage.downloads == [dark.s3_object_name]
    assert len(report.warnings) == 1


def test_an_unreadable_namesake_is_never_matched_by_its_prefix(storage, saved):
    """A prefix hit whose bytes cannot be read stays unconfirmed: the import
    falls back to the name rule rather than trusting 16 hex characters."""
    user = _user()
    brand = _container()
    first = _namesake(storage, brand, DARK_LOGO, seeded=True)
    lost = _namesake(storage, brand, LIGHT_LOGO, seeded=True)
    del storage.store[lost.s3_object_name]
    session = _FakeSession(results=[_Result(rows=[brand]), _Result(rows=[first, lost])])

    _presentation, report = asyncio.run(
        import_presentation_package(session, _package(assets=[_logo_asset(LIGHT_LOGO), _chart_asset()]), user=user)
    )

    assert saved[0]["spec_payload"]["slides"][0]["logo_file_id"] == str(first.id)
    assert storage.downloads == [lost.s3_object_name]
    assert len(report.warnings) == 1


def test_content_reads_share_one_download_budget_per_import(monkeypatch, storage, saved):
    """The per-file bound and the byte budget do not bound how MANY objects an
    import fetches: a package naming hundreds of ambiguous files, in as many
    templates, could have it download eight small ones for each. Past the
    import's download budget a namesake is not fetched, the name rule decides,
    and the report says so."""
    import app.services.presentation_package as module

    monkeypatch.setattr(module, "MAX_CONTENT_MATCH_DOWNLOADS", 3)
    user = _user()
    review, session_for = _review_session()
    dark = _namesake(storage, review, DARK_LOGO)
    light = _namesake(storage, review, LIGHT_LOGO)
    mark_first = _namesake(storage, review, OTHER_LOGO, filename="mark.svg")
    mark_wanted = _namesake(storage, review, LIGHT_LOGO, filename="mark.svg")
    session = session_for([[dark, light], [mark_first, mark_wanted]])
    mark = PackagedAsset(
        original_id=CHART_ID,
        filename="mark.svg",
        content_type="image/svg+xml",
        sha256=hashlib.sha256(LIGHT_LOGO).hexdigest(),
        template_name=REVIEW,
        data=LIGHT_LOGO,
    )
    package = _review_package(_logo_asset(LIGHT_LOGO, None, template_name=REVIEW), mark)

    _presentation, report = asyncio.run(import_presentation_package(session, package, user=user))

    spec = saved[0]["spec_payload"]
    assert spec["slides"][0]["logo_file_id"] == str(light.id)
    assert spec["slides"][1]["blocks"][0]["file_id"] == str(mark_first.id), "mark_wanted was past the budget"
    assert storage.downloads == [dark.s3_object_name, light.s3_object_name, mark_first.s3_object_name]
    assert len(report.warnings) == 1 and "'mark.svg'" in report.warnings[0]


def test_the_same_choice_by_name_is_reported_once(storage, saved):
    """Two references to one ambiguous filename, neither told apart: both land on
    the namesake listed first, and the report says it once - the confirmation
    joins the warnings into one line, where a repeat reads as a second problem."""
    user = _user()
    brand = _container()
    dark = _namesake(storage, brand, DARK_LOGO, seeded=True)
    light = _namesake(storage, brand, LIGHT_LOGO, seeded=True)
    session = _FakeSession(results=[_Result(rows=[brand]), _Result(rows=[dark, light]), _Result(rows=[dark, light])])
    package = _package(assets=[_logo_asset(data=None), _logo_asset(data=None, original_id=CHART_ID)])

    _presentation, report = asyncio.run(import_presentation_package(session, package, user=user))

    spec = saved[0]["spec_payload"]
    assert spec["slides"][0]["logo_file_id"] == spec["slides"][1]["blocks"][0]["file_id"] == str(dark.id)
    assert report.warnings == [
        "2 assets of 'Filigran Brand Deck' are named 'logo.svg' and none was confirmed to hold the packaged "
        "file, so the deck uses the one the template lists first."
    ]


def test_import_refuses_a_reference_it_can_neither_match_nor_copy(storage, saved):
    user = _user()
    session = _FakeSession(results=[_Result(rows=[]), _Result(rows=[])])  # no brand, no groups

    with pytest.raises(PresentationPackageError, match=r"1 referenced file\(s\).*logo\.svg"):
        asyncio.run(
            import_presentation_package(session, _package(assets=[_logo_asset(data=None), _chart_asset()]), user=user)
        )
    assert saved == []


def test_import_refuses_a_spec_reference_the_package_does_not_list(storage, saved):
    user = _user()
    brand = _container()
    local_logo = _file(str(uuid.uuid4()), "logo.svg", container=brand)
    session = _FakeSession(results=[_Result(rows=[brand]), _Result(rows=[local_logo])])

    with pytest.raises(PresentationPackageError, match=f"file {CHART_ID}"):
        asyncio.run(import_presentation_package(session, _package(assets=[_logo_asset()]), user=user))
    assert saved == []


def test_import_falls_back_to_the_default_template_and_says_so(monkeypatch, storage, saved):
    import app.services.presentations as presentations

    user = _user()
    default = _container(name="XTM One Brand Deck", slug="xtm-one-brand")

    async def default_id(db):
        return default.id

    monkeypatch.setattr(presentations, "resolve_default_template_id", default_id)
    session = _FakeSession(
        results=[
            _Result(rows=[]),  # slug lookup: not here
            _Result(rows=[]),  # group ids for the name lookup
            _Result(rows=[]),  # name lookup: not here either
        ],
        objects={default.id: default},
    )

    _presentation, report = asyncio.run(
        import_presentation_package(session, _package(assets=[_logo_asset(slug=None), _chart_asset()]), user=user)
    )

    assert saved[0]["template_container_id"] == default.id
    assert report.template == "default"
    assert report.template_name == "XTM One Brand Deck"
    assert report.warnings == [
        "Template 'Filigran Brand Deck' is not available here; the deck uses 'XTM One Brand Deck' instead."
    ]
    # Both assets carried no template and were copied.
    assert report.assets_copied == 2


def test_the_template_name_lookup_is_three_tier_for_admins_too():
    """``_find_template_container`` never takes the admin bypass on the name
    match.

    A name match is a lookup, not a by-id read: with ``is_admin=True`` the
    predicate degenerates to SQL ``TRUE`` and an admin's import would bind the
    deck to a colleague's private template - one the admin's own Templates
    library (three-tier) does not list, the editor's re-link
    refuses, and no other reader can resolve a palette from. The admin gets
    the same own + shared + company-managed predicate a member gets.
    """
    from sqlalchemy.dialects import postgresql

    from app.services.presentation_package import _find_template_container

    def _lookup_sql(user) -> str:
        # group ids, then the name match (the slug lookup is skipped: no slug)
        session = _FakeSession(results=[_Result(rows=[]), _Result(rows=[])])
        found = asyncio.run(_find_template_container(session, slug=None, name="Quarterly review", user=user))
        assert found is None
        assert len(session.statements) == 2
        return str(session.statements[-1].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))

    admin, member = _user("admin"), _user()
    admin_sql, member_sql = _lookup_sql(admin), _lookup_sql(member)
    assert f"template_containers.user_id = '{admin.id}'" in admin_sql
    assert "template_containers.is_company_managed" in admin_sql
    assert "template_containers.shared_user_ids" in admin_sql
    assert "template_containers.name = 'Quarterly review'" in admin_sql
    assert admin_sql.replace(str(admin.id), "<caller>") == member_sql.replace(str(member.id), "<caller>")


def test_import_without_any_template_reports_none(monkeypatch, storage, saved):
    import app.services.presentations as presentations

    async def no_default(db):
        return None

    monkeypatch.setattr(presentations, "resolve_default_template_id", no_default)
    user = _user()
    package = PresentationPackage(spec=_spec(), assets=[_logo_asset(slug=None), _chart_asset()])
    session = _FakeSession()

    _presentation, report = asyncio.run(import_presentation_package(session, package, user=user))

    assert saved[0]["template_container_id"] is None
    assert saved[0]["change_summary"] == "Imported from a package"
    assert report.template == "none"
    assert report.warnings == []


def test_a_bare_spec_without_files_imports_with_no_asset_work(monkeypatch, storage, saved):
    import app.services.presentations as presentations

    async def no_default(db):
        return None

    monkeypatch.setattr(presentations, "resolve_default_template_id", no_default)
    spec = {"title": "Plain", "slides": [{"layout": "title", "title": "Plain", "subtitle": "s"}]}
    package = parse_presentation_package(_raw(spec))

    presentation, report = asyncio.run(import_presentation_package(_FakeSession(), package, user=_user()))

    assert presentation.title == "Plain"
    assert report.as_dict()["assets_total"] == 0
    assert storage.uploads == []


def test_import_can_retitle_the_deck(monkeypatch, storage, saved):
    import app.services.presentations as presentations

    async def no_default(db):
        return None

    monkeypatch.setattr(presentations, "resolve_default_template_id", no_default)
    package = PresentationPackage(spec=_spec(), assets=[_logo_asset(slug=None), _chart_asset()])

    asyncio.run(import_presentation_package(_FakeSession(), package, user=_user(), title="  FY2027 (prod)  "))

    assert saved[0]["spec_payload"]["title"] == "FY2027 (prod)"


def test_an_embedded_svg_that_does_not_parse_is_refused_naming_the_file(monkeypatch, storage, saved):
    """The import is a way bytes become a platform file; it runs the same
    sanitizer as an upload, and a broken SVG is refused, not stored."""
    import app.services.presentations as presentations

    async def no_default(db):
        return None

    monkeypatch.setattr(presentations, "resolve_default_template_id", no_default)
    package = PresentationPackage(spec=_spec(), assets=[_logo_asset(data=b"<svg><unclosed", slug=None), _chart_asset()])

    with pytest.raises(PresentationPackageError, match=r"'logo\.svg' is not a valid SVG"):
        asyncio.run(import_presentation_package(_FakeSession(), package, user=_user()))
    assert saved == []


def test_an_embedded_svg_is_stored_sanitized(monkeypatch, storage, saved):
    import app.services.presentations as presentations

    async def no_default(db):
        return None

    monkeypatch.setattr(presentations, "resolve_default_template_id", no_default)
    hostile = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><rect/></svg>'
    package = PresentationPackage(spec=_spec(), assets=[_logo_asset(data=hostile, slug=None), _chart_asset()])

    asyncio.run(import_presentation_package(_FakeSession(), package, user=_user()))

    stored = next(data for data, name, _ct, _prefix in storage.uploads if name == "logo.svg")
    assert b"<script" not in stored
    assert b"<rect" in stored


def test_a_copied_asset_records_the_digest_of_the_bytes_stored(monkeypatch, storage, saved):
    """The import's own copy records the digest of what storage holds -
    for a sanitized SVG, not the packaged digest - like every other write of an
    object key (``tests/test_file_attachment_digest.py``)."""
    import app.services.presentations as presentations
    from app.models.file_attachment import FileAttachment

    async def no_default(db):
        return None

    monkeypatch.setattr(presentations, "resolve_default_template_id", no_default)
    hostile = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><rect/></svg>'
    session = _FakeSession()
    package = PresentationPackage(spec=_spec(), assets=[_logo_asset(data=hostile, slug=None), _chart_asset()])

    asyncio.run(import_presentation_package(session, package, user=_user()))

    rows = {row.filename: row for row in session.added if isinstance(row, FileAttachment)}
    stored = {name: data for data, name, _ct, _prefix in storage.uploads}
    assert rows["logo.svg"].sha256 == hashlib.sha256(stored["logo.svg"]).hexdigest()
    assert rows["logo.svg"].sha256 != hashlib.sha256(hostile).hexdigest(), "the stored bytes, not the packaged ones"
    assert rows["chart.png"].sha256 == hashlib.sha256(PNG).hexdigest()


def test_a_packaged_asset_the_spec_no_longer_references_is_ignored(monkeypatch, storage, saved):
    import app.services.presentations as presentations

    async def no_default(db):
        return None

    monkeypatch.setattr(presentations, "resolve_default_template_id", no_default)
    stale = PackagedAsset(original_id=str(uuid.uuid4()), filename="old.png", data=PNG)
    package = PresentationPackage(spec=_spec(), assets=[_logo_asset(slug=None), _chart_asset(), stale])

    _presentation, report = asyncio.run(import_presentation_package(_FakeSession(), package, user=_user()))

    assert report.assets_total == 2
    assert [name for _data, name, _ct, _prefix in storage.uploads] == ["logo.svg", "chart.png"]


# -- API wiring -----------------------------------------------------------------


def test_package_and_import_routes_exist():
    from app.api.v1 import presentations as api

    routes = {
        (path, method)
        for route in api.router.routes
        for path in [getattr(route, "path", "")]
        for method in getattr(route, "methods", set())
    }
    assert ("/{presentation_id}/package", "GET") in routes
    assert ("/import", "POST") in routes


def test_the_package_download_is_visibility_scoped_and_named():
    from app.api.v1 import presentations as api

    source = inspect.getsource(api.export_presentation_package_endpoint)
    assert "_load_visible(" in source
    assert "build_content_disposition(package_filename(" in source
    assert "no-store" in source


def test_the_import_route_is_bounded_audited_and_owner_scoped():
    from app.api.v1 import presentations as api

    source = inspect.getsource(api.import_presentation_endpoint)
    # One byte past the cap decides, before the whole upload is in memory.
    assert "file.read(MAX_PACKAGE_BYTES + 1)" in source
    assert "HTTP_413_CONTENT_TOO_LARGE" in source
    # Refusals are 422 with the parser's own message; a half-done import rolls back.
    assert "PresentationPackageError" in source and "await db.rollback()" in source
    assert 'action="create"' in source and 'entity_type="presentation"' in source
    assert 'tier="owner"' in source
    # It is the ONLY route that creates a deck from JSON, and it is not gated
    # on AI: moving a deck between installs makes no model call.
    assert "require_agentic_ai" not in source


def test_the_import_version_source_is_known_to_the_history():
    from app.models.presentation import VERSION_SOURCE_IMPORT, VERSION_SOURCES

    assert VERSION_SOURCE_IMPORT == "import"
    assert VERSION_SOURCE_IMPORT in VERSION_SOURCES


def test_the_import_response_carries_the_report():
    from app.api.v1.schemas.presentation import PresentationImportReport, PresentationImportResponse

    assert "import_report" in PresentationImportResponse.model_fields
    assert set(PresentationImportReport.model_fields) == {
        "assets_total",
        "assets_matched",
        "assets_copied",
        "template",
        "template_name",
        "warnings",
    }


def test_frontend_offers_the_package_on_both_ends():
    """The export menu writes packages, the library page reads them.

    The same tripwire shape as ``test_frontend_offers_every_export_format``:
    a backend route with no UI entry is a feature nobody can reach, and a UI
    entry pointing at a renamed route is a 404 discovered in production.
    """
    from pathlib import Path

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "src"
    menu = (frontend / "components" / "presentation" / "PresentationExportMenu.tsx").read_text(encoding="utf-8")
    assert "useExportPresentationPackage" in menu and '"package"' in menu
    library = (frontend / "pages" / "Presentations.tsx").read_text(encoding="utf-8")
    assert "useImportPresentation" in library and 'type="file"' in library
    hooks = (frontend / "hooks" / "api" / "usePresentationApi.ts").read_text(encoding="utf-8")
    assert "/package`" in hooks and '"/presentations/import"' in hooks
    # The report's shape is mirrored by hand in the TS types.
    types = (frontend / "types" / "index.ts").read_text(encoding="utf-8")
    from app.api.v1.schemas.presentation import PresentationImportReport

    for field in PresentationImportReport.model_fields:
        assert f"{field}:" in types, f"PresentationImportReport TS type lacks {field}"
    # The history panel labels every version source, the import one included.
    history = (frontend / "components" / "presentation" / "PresentationHistoryPanel.tsx").read_text(encoding="utf-8")
    assert "import:" in history
    assert '"import"' in types
