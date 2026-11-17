"""References to other documents: URL sources, uploaded files, ZIP archives, $self."""

from __future__ import annotations

import io
import json
import zipfile

import httpx
import pytest

from app.services.openapi_import import bundle as bundle_module
from app.services.openapi_import.bundle import (
    BUNDLE_KEY,
    bundle_external_refs,
    read_spec_archive,
)
from app.services.openapi_import.document import OpenAPIImportError, parse_spec_document
from app.services.openapi_import.service import analyze_source
from app.services.web_fetch import FetchedBytes

ROOT = """openapi: 3.1.0
info: {title: Split Pets, version: "1"}
servers: [{url: https://api.pets.test/v1}]
paths:
  /pets:
    $ref: paths/pets.yaml
  /pets/{id}:
    get:
      operationId: getPet
      parameters:
        - $ref: 'common.yaml#/parameters/PetId'
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema:
                $ref: 'schemas/pet.yaml#/Pet'
"""
PATHS = """get:
  operationId: listPets
  parameters:
    - $ref: '../common.yaml#/parameters/Limit'
  responses:
    "200": {description: ok}
post:
  operationId: createPet
  requestBody:
    required: true
    content:
      application/json:
        schema:
          $ref: '../schemas/pet.yaml#/Pet'
  responses:
    "201": {description: created}
"""
COMMON = """parameters:
  Limit: {name: limit, in: query, schema: {type: integer, maximum: 100}}
  PetId: {name: id, in: path, required: true, schema: {type: string, format: uuid}}
"""
PET = """Pet:
  type: object
  required: [name]
  properties:
    name: {type: string}
    tags:
      type: array
      items:
        $ref: '#/Tag'
    parent:
      $ref: '#/Pet'
    default:
      $ref: '#/Tag'
    note:
      type: string
      example:
        $ref: not-a-reference.yaml
Tag:
  type: object
  properties:
    label: {type: string}
"""
FILES = {
    "openapi.yaml": ROOT,
    "paths/pets.yaml": PATHS,
    "common.yaml": COMMON,
    "schemas/pet.yaml": PET,
}
BASE = "https://specs.pets.test/v1/"


def _serve(monkeypatch, files: dict[str, str], base: str = BASE) -> list[str]:
    fetched: list[str] = []

    async def fake_fetch(url, **kwargs):
        fetched.append(url)
        path = url.removeprefix(base)
        if url.startswith(base) and path in files:
            return FetchedBytes(url=url, content=files[path].encode(), content_type="application/yaml", status_code=200)
        request = httpx.Request("GET", url)
        raise httpx.HTTPStatusError("missing", request=request, response=httpx.Response(404, request=request))

    monkeypatch.setattr("app.services.web_fetch.fetch_url_bytes", fake_fetch)
    return fetched


@pytest.fixture(autouse=True)
def _no_discovery(monkeypatch):
    async def none(_document, _url_base):
        return {}

    monkeypatch.setattr("app.services.openapi_import.service.fetch_discovery", none)


def _zip(files: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _tools(analysis) -> dict[str, dict]:
    return {op.key: op.tool for op in analysis.operations if op.tool}


def _check_split_pets(analysis) -> None:
    tools = _tools(analysis)
    assert set(tools) == {"GET /pets", "POST /pets", "GET /pets/{id}"}
    assert tools["GET /pets"]["input_schema"]["properties"]["limit"] == {"type": "integer", "maximum": 100}
    assert tools["GET /pets/{id}"]["input_schema"]["properties"]["id"]["format"] == "uuid"
    body = tools["POST /pets"]["input_schema"]
    assert body["required"] == ["name"]
    assert body["properties"]["tags"]["items"] == {"type": "object", "properties": {"label": {"type": "string"}}}
    # A property named like a keyword is still a schema; a recursive model is cut.
    assert body["properties"]["default"] == {"type": "object", "properties": {"label": {"type": "string"}}}
    assert "recursive" in body["properties"]["parent"]["description"]
    assert body["properties"]["note"] == {"type": "string"}


async def test_a_url_source_bundles_the_files_it_references(monkeypatch):
    fetched = _serve(monkeypatch, FILES)
    document, analysis = await analyze_source(url=BASE + "openapi.yaml", content=None)
    assert analysis.warnings == []
    _check_split_pets(analysis)
    # Every document once, in the order references appear, whatever the fetch order.
    assert sorted(fetched) == sorted(BASE + p for p in FILES)
    assert list(document.data[BUNDLE_KEY]) == ["d1", "d2", "d3"]
    # Examples are data: the reference-shaped value inside one was left alone.
    assert "not-a-reference.yaml" not in " ".join(fetched)


async def test_the_bundle_is_the_same_on_every_import(monkeypatch):
    _serve(monkeypatch, FILES)
    first, _ = await analyze_source(url=BASE + "openapi.yaml", content=None)
    second, _ = await analyze_source(url=BASE + "openapi.yaml", content=None)
    assert first.sha256 == second.sha256
    assert first.sha256 != parse_spec_document(ROOT).sha256  # it covers the referenced files too


async def test_an_uploaded_file_follows_only_absolute_references(monkeypatch):
    fetched = _serve(monkeypatch, {"schemas/pet.yaml": PET})
    root = ROOT.replace("'schemas/pet.yaml#/Pet'", f"'{BASE}schemas/pet.yaml#/Pet'")
    _document, analysis = await analyze_source(url=None, content=root)
    assert fetched == [BASE + "schemas/pet.yaml"]
    assert "cannot reach" in " ".join(analysis.warnings)
    assert "upload a ZIP" in " ".join(analysis.warnings)
    skipped = [w for w in analysis.warnings if "path(s)" in w]
    assert skipped and "/pets" in skipped[0]


async def test_self_is_the_base_of_an_uploaded_file(monkeypatch):
    fetched = _serve(monkeypatch, FILES)
    root = ROOT.replace("openapi: 3.1.0\n", f"openapi: 3.2.0\n$self: {BASE}openapi.yaml\n")
    _document, analysis = await analyze_source(url=None, content=root)
    assert analysis.warnings == []
    _check_split_pets(analysis)
    assert BASE + "openapi.yaml" not in fetched  # the root is not fetched again


async def test_a_zip_archive_holds_every_file(monkeypatch):
    fetched = _serve(monkeypatch, {})
    archive = _zip({f"pets-api/{name}": content for name, content in FILES.items()} | {"pets-api/README.md": "hi"})
    document, analysis = await analyze_source(url=None, content=None, archive=archive)
    assert fetched == []
    assert analysis.warnings == []
    _check_split_pets(analysis)
    assert document.source_url is None
    # Relative servers of an archive have no real base to resolve against.
    assert analysis.default_server_url == "https://api.pets.test/v1"


async def test_a_zip_archive_under_a_self_url_is_read_from_the_archive(monkeypatch):
    fetched = _serve(monkeypatch, {})
    files = dict(FILES)
    files["openapi.yaml"] = ROOT.replace("openapi: 3.1.0\n", f"openapi: 3.2.0\n$self: {BASE}openapi.yaml\n")
    # Another file named by its own $self, stored elsewhere in the archive.
    files["openapi.yaml"] = files["openapi.yaml"].replace(
        "'common.yaml#/parameters/PetId'", "'https://shared.pets.test/params.yaml#/PetId'"
    )
    files["vendor/params.yaml"] = (
        "$self: https://shared.pets.test/params.yaml\n"
        "PetId: {name: id, in: path, required: true, schema: {type: string, format: uuid}}\n"
    )
    _document, analysis = await analyze_source(url=None, content=None, archive=_zip(files))
    assert fetched == []
    assert analysis.warnings == []
    _check_split_pets(analysis)


async def test_a_reference_that_cannot_be_loaded_is_reported(monkeypatch):
    _serve(monkeypatch, {"openapi.yaml": ROOT, "paths/pets.yaml": PATHS, "common.yaml": COMMON})
    _document, analysis = await analyze_source(url=BASE + "openapi.yaml", content=None)
    warning = next(w for w in analysis.warnings if "schemas/pet.yaml" in w)
    assert "HTTP 404" in warning
    create = _tools(analysis)["POST /pets"]
    assert "could not be resolved" in json.dumps(create["input_schema"])


async def test_references_to_other_schemes_are_never_followed(monkeypatch):
    fetched = _serve(monkeypatch, {})
    root = ROOT.replace("'schemas/pet.yaml#/Pet'", "'file:///etc/passwd#/Pet'")
    document = parse_spec_document(root)
    _bundled, warnings = await bundle_external_refs(document)
    assert fetched == []
    assert any("'file:' references are not followed" in w for w in warnings)


async def test_the_number_of_documents_is_bounded(monkeypatch):
    monkeypatch.setattr(bundle_module, "MAX_EXTERNAL_DOCUMENTS", 2)
    _serve(monkeypatch, FILES)
    _document, analysis = await analyze_source(url=BASE + "openapi.yaml", content=None)
    assert any("more than 2 documents" in w for w in analysis.warnings)


async def test_the_bundle_budget_counts_the_root_and_every_concurrent_load(monkeypatch):
    """Root plus references, whatever order the loads of one batch finish in."""
    _serve(monkeypatch, FILES)
    everything = sum(len(content.encode()) for content in FILES.values())
    # Room for the three referenced files alone, not for them and the root.
    monkeypatch.setattr(bundle_module, "MAX_BUNDLE_BYTES", everything - 1)
    document, analysis = await analyze_source(url=BASE + "openapi.yaml", content=None)
    assert document.size_bytes <= everything - 1
    assert len(document.data[BUNDLE_KEY]) == 2
    assert any("too large to import together" in w for w in analysis.warnings)


# ── Archives ─────────────────────────────────────────────────────────────────


def test_archive_root_detection():
    archive = read_spec_archive(_zip({"a/openapi.json": '{"openapi": "3.0.0"}', "a/b/other.yaml": "openapi: 3.0.0"}))
    assert archive.root == "a/openapi.json"
    named = read_spec_archive(_zip({"spec.yaml": "openapi: 3.0.0", "openapi.yaml": "openapi: 3.1.0"}))
    assert named.root == "openapi.yaml"
    with pytest.raises(OpenAPIImportError, match="several API documents"):
        read_spec_archive(_zip({"one.yaml": "openapi: 3.0.0", "two.yaml": "swagger: '2.0'"}))


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"notes.txt": "hello"}, "no .json, .yaml or .yml"),
        ({"schemas/pet.yaml": "Pet: {type: object}"}, "No document in the archive"),
    ],
)
def test_archives_without_a_spec_are_refused(files, message):
    with pytest.raises(OpenAPIImportError, match=message):
        read_spec_archive(_zip(files))


def test_archive_paths_cannot_escape_or_hide():
    archive = read_spec_archive(
        _zip(
            {
                "../evil.yaml": "openapi: 3.0.0",
                "/abs.yaml": "openapi: 3.0.0",
                "__MACOSX/openapi.yaml": "openapi: 3.0.0",
                ".hidden/openapi.yaml": "openapi: 3.0.0",
                "ok/openapi.yaml": "openapi: 3.0.0",
            }
        )
    )
    assert set(archive.files) == {"ok/openapi.yaml"}


def test_archive_limits(monkeypatch):
    with pytest.raises(OpenAPIImportError, match="not a valid ZIP"):
        read_spec_archive(b"PK\x03\x04 not really")
    monkeypatch.setattr(bundle_module, "MAX_SPEC_BYTES", 10)
    with pytest.raises(OpenAPIImportError, match="larger than the document size limit"):
        read_spec_archive(_zip({"openapi.yaml": "openapi: 3.0.0\n" + "x" * 100}))


def test_an_archive_that_understates_its_sizes_is_bounded_by_what_it_inflates_to(monkeypatch):
    monkeypatch.setattr(bundle_module, "MAX_BUNDLE_BYTES", 100)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index in range(5):
            archive.writestr(f"part{index}.yaml", "k: " + "v" * 60)
    raw = bytearray(buffer.getvalue())
    # Claim one byte per member in the central directory (what infolist() reads).
    for entry in zipfile.ZipFile(io.BytesIO(bytes(raw))).infolist():
        offset = raw.find(b"PK\x01\x02" , 0)
        while offset != -1:
            name_length = int.from_bytes(raw[offset + 28 : offset + 30], "little")
            if raw[offset + 46 : offset + 46 + name_length] == entry.filename.encode():
                raw[offset + 24 : offset + 28] = (1).to_bytes(4, "little")
                break
            offset = raw.find(b"PK\x01\x02", offset + 4)
    # Refused either way: by what was read, or by the member failing its checksum.
    with pytest.raises(OpenAPIImportError, match=r"too large to import together|cannot be read"):
        read_spec_archive(bytes(raw))


def test_the_bundle_total_counts_what_members_inflate_to(monkeypatch):
    monkeypatch.setattr(bundle_module, "MAX_BUNDLE_BYTES", 100)
    files = {f"part{index}.yaml": "k: " + "v" * 40 for index in range(3)} | {"openapi.yaml": "openapi: 3.0.0"}
    with pytest.raises(OpenAPIImportError, match="too large to import together"):
        read_spec_archive(_zip(files))


def test_a_member_declaring_less_than_it_holds_is_refused_having_decompressed_what_it_declared():
    """``read_bounded`` stops at the declared size and the member fails its CRC."""
    from tests.archive_helpers import declare_size, peak_mib

    raw = declare_size(_zip({"openapi.yaml": "openapi: 3.0.0\n" + " " * (16 * 2**20)}), "openapi.yaml", 10)
    peak, raised = peak_mib(lambda: read_spec_archive(raw))
    assert isinstance(raised, OpenAPIImportError)
    assert str(raised) == "'openapi.yaml' cannot be read from the archive."
    assert peak <= 4


@pytest.mark.parametrize("how", ["damaged stream", "unknown compression"])
def test_an_unreadable_member_cannot_be_read(how):
    from tests.archive_helpers import corrupt_member

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("openapi.yaml", "openapi: 3.0.0\n" + "x: " + "y" * 4000)
    with pytest.raises(OpenAPIImportError, match=r"^'openapi\.yaml' cannot be read from the archive\.$"):
        read_spec_archive(corrupt_member(buffer.getvalue(), "openapi.yaml", how))

