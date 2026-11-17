"""Loading Swagger / OpenAPI documents: JSON and YAML, text and URL, hostile input."""

from __future__ import annotations

import codecs
import json
from pathlib import Path

import httpx
import pytest

from app.services.openapi_import import document as document_mod
from app.services.openapi_import.document import (
    MAX_SPEC_BYTES,
    OpenAPIImportError,
    detect_spec_version,
    fetch_spec_document,
    load_spec_source,
    normalize_spec_url,
    parse_spec_document,
    parse_spec_text,
)
from app.services.web_fetch import DestinationUnresolvedError, FetchedBytes

FIXTURES = Path(__file__).parent / "openapi_fixtures"

MINIMAL_YAML = """\
openapi: 3.0.3
info:
  title: Mini
  version: 1.0.0
paths: {}
"""


# ── Format detection ─────────────────────────────────────────────────────────


def test_json_document_is_parsed_as_json():
    data, fmt = parse_spec_text(json.dumps({"openapi": "3.0.0", "info": {}, "paths": {}}))
    assert fmt == "json"
    assert data["openapi"] == "3.0.0"


def test_yaml_document_is_parsed_as_yaml():
    data, fmt = parse_spec_text(MINIMAL_YAML)
    assert fmt == "yaml"
    assert data["info"]["title"] == "Mini"


def test_both_petstore_fixtures_load():
    v2 = parse_spec_document((FIXTURES / "petstore_v2.json").read_bytes())
    v3 = parse_spec_document((FIXTURES / "petstore_v3.yaml").read_bytes())
    assert (v2.format, v2.version.family) == ("json", "2.0")
    assert (v3.format, v3.version.family) == ("yaml", "3.0")
    assert v2.sha256 != v3.sha256
    assert v2.size_bytes == len((FIXTURES / "petstore_v2.json").read_bytes())


def test_yaml_keys_keep_their_literal_text():
    """YAML 1.1 turns ``200:`` into an int and ``on:`` into a bool; a spec means strings."""
    data, _ = parse_spec_text(
        "openapi: 3.0.0\n"
        "paths:\n"
        "  /x:\n"
        "    get:\n"
        "      responses:\n"
        "        200:\n"
        "          description: ok\n"
        "components:\n"
        "  schemas:\n"
        "    Flags:\n"
        "      properties:\n"
        "        on: {type: boolean}\n"
        "        yes: {type: boolean}\n"
    )
    assert list(data["paths"]["/x"]["get"]["responses"]) == ["200"]
    assert set(data["components"]["schemas"]["Flags"]["properties"]) == {"on", "yes"}


def test_yaml_timestamps_stay_strings():
    data, _ = parse_spec_text("openapi: 3.0.0\ninfo:\n  version: 2024-06-01\n  title: t\n")
    assert data["info"]["version"] == "2024-06-01"


def test_yaml_merge_keys_are_honoured():
    data, _ = parse_spec_text(
        "openapi: 3.0.0\n"
        "x-base: &base {type: string, maxLength: 5}\n"
        "components:\n"
        "  schemas:\n"
        "    Code:\n"
        "      <<: *base\n"
        "      description: a code\n"
    )
    assert data["components"]["schemas"]["Code"] == {
        "type": "string",
        "maxLength": 5,
        "description": "a code",
    }


def test_bom_prefixed_documents_decode():
    for bom, codec in ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le")):
        doc = parse_spec_document(bom + MINIMAL_YAML.encode(codec))
        assert doc.data["info"]["title"] == "Mini"


def test_non_utf8_bytes_are_refused():
    with pytest.raises(OpenAPIImportError, match="UTF-8"):
        parse_spec_document(b"openapi: 3.0.0\ninfo: {title: \xff\xfe\xfd}\n")


# ── Errors worth reading ─────────────────────────────────────────────────────


def test_empty_document_is_refused():
    with pytest.raises(OpenAPIImportError, match="empty"):
        parse_spec_text("   \n ")


def test_broken_json_reports_json_position():
    with pytest.raises(OpenAPIImportError, match=r"Invalid JSON at line 1"):
        parse_spec_text('{"openapi": "3.0.0",, }')


def test_broken_yaml_reports_yaml_position():
    with pytest.raises(OpenAPIImportError, match=r"Invalid YAML at line \d+"):
        parse_spec_text("openapi: 3.0.0\ninfo:\n  title: [unclosed\n")


def test_html_page_gets_a_pointed_message():
    with pytest.raises(OpenAPIImportError, match="HTML page"):
        parse_spec_text("<!DOCTYPE html><html><body>Swagger UI</body></html>")


def test_root_must_be_an_object():
    with pytest.raises(OpenAPIImportError, match="must be a JSON or YAML object"):
        parse_spec_text("[1, 2, 3]")


def test_non_finite_numbers_never_survive():
    # Not JSON (NaN is refused by the JSON parser) but valid YAML, where the
    # bare word is a plain string — harmless.
    data, fmt = parse_spec_text('{"openapi": "3.0.0", "x": NaN}')
    assert (fmt, data["x"]) == ("yaml", "NaN")
    # YAML's real float NaN / infinity are refused.
    for literal in (".nan", ".inf", "-.inf"):
        with pytest.raises(OpenAPIImportError, match="non-finite"):
            parse_spec_text(f"openapi: 3.0.0\nx: {literal}\n")


# ── Hostile YAML ─────────────────────────────────────────────────────────────


def test_alias_bomb_is_refused(monkeypatch):
    """Billion laughs: a few lines, exponential expansion — refused before any walk."""
    monkeypatch.setattr(document_mod, "_MAX_EXPANDED_NODES", 100_000)
    lines = ["openapi: 3.0.0", 'a0: &a0 ["x"]']
    for i in range(1, 12):
        prev = f"*a{i - 1}"
        lines.append(f"a{i}: &a{i} [{', '.join([prev] * 8)}]")
    with pytest.raises(OpenAPIImportError, match="too many nodes"):
        parse_spec_text("\n".join(lines))


def test_recursive_alias_is_refused():
    with pytest.raises(OpenAPIImportError):
        parse_spec_text("openapi: 3.0.0\nloop: &loop [*loop]\n")


@pytest.mark.parametrize(
    "value",
    ["!!binary aGVsbG8=", "!!set {a: null}", "!!python/object:os.system {}"],
)
def test_non_json_yaml_tags_are_refused(value):
    with pytest.raises(OpenAPIImportError):
        parse_spec_text(f"openapi: 3.0.0\nx: {value}\n")


def test_complex_mapping_keys_are_refused():
    with pytest.raises(OpenAPIImportError):
        parse_spec_text("openapi: 3.0.0\n? [a, b]\n: value\n")


def test_oversized_document_is_refused():
    with pytest.raises(OpenAPIImportError, match="maximum size"):
        parse_spec_document(b" " * (MAX_SPEC_BYTES + 1))


# ── Versions ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("data", "family", "raw"),
    [
        ({"swagger": "2.0"}, "2.0", "2.0"),
        ({"swagger": 2.0}, "2.0", "2.0"),  # unquoted in YAML → float
        ({"openapi": "3.0.3"}, "3.0", "3.0.3"),
        ({"openapi": 3.0}, "3.0", "3.0"),
        ({"openapi": "3.1.0"}, "3.1", "3.1.0"),
        ({"openapi": "3.2.0"}, "3.1", "3.2.0"),
    ],
)
def test_supported_versions(data, family, raw):
    version = detect_spec_version(data)
    assert (version.family, version.raw) == (family, raw)


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"swaggerVersion": "1.2"}, "Swagger 1.x"),
        ({"swagger": "3.0"}, "Swagger 3.0 is not supported"),
        ({"openapi": "4.0.0"}, "OpenAPI 4.0.0 is not supported"),
        ({"info": {}}, "not an OpenAPI or Swagger document"),
    ],
)
def test_unsupported_versions(data, match):
    with pytest.raises(OpenAPIImportError, match=match):
        detect_spec_version(data)


# ── Sources ──────────────────────────────────────────────────────────────────


async def test_load_spec_source_needs_exactly_one_source():
    with pytest.raises(OpenAPIImportError, match="either"):
        await load_spec_source()
    with pytest.raises(OpenAPIImportError, match="either"):
        await load_spec_source(url="https://x.test/o.json", content=MINIMAL_YAML)


async def test_load_spec_source_from_content():
    doc = await load_spec_source(content=MINIMAL_YAML)
    assert doc.format == "yaml"
    assert doc.source_url is None


@pytest.mark.parametrize(
    "url",
    ["ftp://x.test/spec.json", "file:///etc/passwd", "/relative/spec.json", "https://u:p@x.test/s.json"],
)
def test_spec_url_shape_is_checked(url):
    with pytest.raises(OpenAPIImportError):
        normalize_spec_url(url)


async def test_fetch_uses_the_private_network_policy_and_final_url(monkeypatch):
    calls: dict = {}

    async def fake_fetch(url, **kwargs):
        calls.update(kwargs, url=url)
        return FetchedBytes(
            url="https://api.internal.test/v3/openapi.yaml",
            content=MINIMAL_YAML.encode(),
            content_type="application/yaml",
            status_code=200,
        )

    monkeypatch.setattr("app.services.web_fetch.fetch_url_bytes", fake_fetch)
    doc = await fetch_spec_document(" https://api.internal.test/spec ")
    assert calls["url"] == "https://api.internal.test/spec"
    assert calls["allow_private"] is True
    assert calls["max_bytes"] == MAX_SPEC_BYTES
    assert "application/json" in calls["accept"] and "yaml" in calls["accept"]
    # Relative server URLs resolve against where the document really came from.
    assert doc.source_url == "https://api.internal.test/v3/openapi.yaml"


@pytest.mark.parametrize(
    ("error", "match"),
    [
        (ValueError("URL targets a loopback address: 127.0.0.1"), "cannot be fetched"),
        (
            httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("GET", "https://x.test"),
                response=httpx.Response(404, request=httpx.Request("GET", "https://x.test")),
            ),
            "HTTP 404",
        ),
        # Filtered, refused and unresolved read the same: the reason would map
        # the hosts and ports of the networks the platform reaches.
        (httpx.ReadTimeout("slow"), "could not be reached"),
        (httpx.ConnectError("refused"), "could not be reached"),
        (DestinationUnresolvedError("Cannot resolve hostname: x.test"), "could not be reached"),
    ],
)
async def test_fetch_errors_become_import_errors(monkeypatch, error, match):
    async def fake_fetch(url, **kwargs):
        raise error

    monkeypatch.setattr("app.services.web_fetch.fetch_url_bytes", fake_fetch)
    with pytest.raises(OpenAPIImportError, match=match) as raised:
        await fetch_spec_document("https://x.test/spec.json")
    assert "refused" not in str(raised.value) and "slow" not in str(raised.value)


@pytest.mark.parametrize("url", ["https://[::1/spec.json", "https://api.test:99999/spec.json"])
def test_a_url_that_is_no_url_is_an_import_error(url):
    with pytest.raises(OpenAPIImportError, match="not a valid URL"):
        normalize_spec_url(url)


def test_a_number_past_the_digit_limit_is_an_import_error():
    huge = "9" * 5000
    with pytest.raises(OpenAPIImportError, match="cannot be read"):
        parse_spec_text(f"openapi: 3.1.0\ninfo:\n  title: t\n  version: {huge}\npaths: {{}}\n")


def test_a_version_in_other_digits_is_refused():
    with pytest.raises(OpenAPIImportError, match="not supported"):
        detect_spec_version({"openapi": "3.\u0661"})
