from __future__ import annotations

import io
import json
import os
import zipfile

import pytest

from app.services.integration_bundle_manager import (
    INTEGRATION_BUNDLE_CHECKSUM_FILE,
    INTEGRATION_BUNDLE_METADATA_FILE,
    build_integration_bundle_zip,
    normalize_custom_provider_slug,
    parse_integration_bundle_zip,
)


def _metadata() -> dict:
    return {
        "format": "filigran-integration-v1",
        "version": "1.0.0",
        "name": "My Integration",
        "slug": "my-integration",
        "description": "Integration for tests.",
        "author": "Filigran",
        "provider": "my-integration",
        "auth_type": "api_key",
        "icon": "assets/icon.png",
        "settings": [
            {
                "key": "api_url",
                "label": "API URL",
                "type": "string",
                "required": True,
                "placeholder": "https://api.example.test",
            },
            {
                "key": "api_key",
                "label": "API Key",
                "type": "secret",
                "required": True,
            },
            {
                "key": "send_mode",
                "label": "Send Mode",
                "type": "select",
                "required": True,
                "options": [
                    {"value": "sync", "label": "Sync"},
                    {"value": "async", "label": "Async"},
                ],
            },
        ],
        "supports_polling": True,
        "supports_webhook": True,
        "test_connection": {"mode": "http", "path": "/health", "method": "GET"},
        "exported_at": "2026-07-07T00:00:00Z",
    }


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
    return buffer.getvalue()


def test_build_and_parse_bundle_roundtrip():
    raw = build_integration_bundle_zip(
        _metadata(),
        assets={"assets/icon.png": b"PNG"},
    )

    parsed = parse_integration_bundle_zip(raw)
    assert parsed.metadata.slug == "my-integration"
    assert parsed.metadata.provider == "my-integration"
    assert parsed.metadata.auth_type == "api_key"
    assert parsed.metadata.supports_webhook is True
    assert parsed.icon_bytes == b"PNG"
    assert parsed.metadata.test_connection is not None
    assert parsed.metadata.test_connection.method == "GET"


def test_parse_rejects_missing_checksum_file():
    metadata_json = json.dumps(_metadata()).encode("utf-8")
    raw = _zip({INTEGRATION_BUNDLE_METADATA_FILE: metadata_json})
    with pytest.raises(ValueError, match=r"checksum\.sha256"):
        parse_integration_bundle_zip(raw)


def test_parse_rejects_checksum_mismatch():
    metadata_json = json.dumps(_metadata()).encode("utf-8")
    raw = _zip(
        {
            INTEGRATION_BUNDLE_METADATA_FILE: metadata_json,
            INTEGRATION_BUNDLE_CHECKSUM_FILE: b"0" * 64,
            "assets/icon.png": b"PNG",
        }
    )
    with pytest.raises(ValueError, match="Checksum mismatch"):
        parse_integration_bundle_zip(raw)


def test_parse_allows_empty_settings():
    metadata = _metadata()
    metadata["settings"] = []
    metadata["icon"] = None
    raw = build_integration_bundle_zip(metadata)
    parsed = parse_integration_bundle_zip(raw)
    assert parsed.metadata.settings == []


def test_parse_allows_missing_settings():
    metadata = _metadata()
    del metadata["settings"]
    metadata["icon"] = None
    raw = build_integration_bundle_zip(metadata)
    parsed = parse_integration_bundle_zip(raw)
    assert parsed.metadata.settings == []


def test_parse_normalizes_invalid_slug():
    bad = _metadata()
    bad["slug"] = "Invalid Slug"
    raw = build_integration_bundle_zip(bad, assets={"assets/icon.png": b"PNG"})
    parsed = parse_integration_bundle_zip(raw)
    assert parsed.metadata.slug == "invalid-slug"


def test_parse_normalizes_invalid_provider_slug():
    bad = _metadata()
    bad["provider"] = "Provider Name V2"
    raw = build_integration_bundle_zip(bad, assets={"assets/icon.png": b"PNG"})
    parsed = parse_integration_bundle_zip(raw)
    assert parsed.metadata.provider == "provider-name-v2"


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "On Windows the stdlib zipfile sanitizes stored names at read time "
        "(os.sep -> '/'), so a backslash entry can never reach the guard; the "
        "guard is defense-in-depth for POSIX where backslashes survive."
    ),
)
def test_parse_rejects_backslash_zip_path():
    metadata_json = json.dumps(_metadata()).encode("utf-8")
    checksum = b"0" * 64
    raw = _zip(
        {
            INTEGRATION_BUNDLE_METADATA_FILE: metadata_json,
            INTEGRATION_BUNDLE_CHECKSUM_FILE: checksum,
            "assets/icon.png": b"PNG",
        }
    )
    # zipfile also normalizes os.sep to "/" at WRITE time, so patch the stored
    # name bytes directly, like a handcrafted malicious archive would contain.
    # Same length, so the ZIP local header and central directory stay
    # consistent.
    raw = raw.replace(b"assets/icon.png", b"assets\\icon.png")
    with pytest.raises(ValueError, match="Unsafe ZIP path"):
        parse_integration_bundle_zip(raw)


def test_parse_requires_select_options_when_type_is_select():
    bad = _metadata()
    bad["settings"][2] = {
        "key": "send_mode",
        "label": "Send Mode",
        "type": "select",
        "required": True,
    }
    with pytest.raises(ValueError, match="options"):
        build_integration_bundle_zip(bad)


def test_parse_accepts_prerelease_with_build_metadata_version():
    # Per SemVer, pre-release and build metadata are independent segments and
    # may both be present: "1.0.0-alpha+build1" is fully compliant.
    metadata = _metadata()
    metadata["version"] = "1.0.0-alpha+build1"
    metadata["icon"] = None
    raw = build_integration_bundle_zip(metadata)
    parsed = parse_integration_bundle_zip(raw)
    assert parsed.metadata.version == "1.0.0-alpha+build1"


@pytest.mark.parametrize(
    "bad_version",
    [
        "1.0.0-alpha+build1+extra",  # double build segment
        "1.0.0-alpha..1",  # empty pre-release identifier
        "01.2.3",  # leading zero in major
        "1.02.3",  # leading zero in minor
        "1.0.0-01",  # leading zero in numeric pre-release identifier
        "1.0.0+",  # empty build metadata
        "1.0",  # missing patch
    ],
)
def test_parse_rejects_malformed_version(bad_version):
    metadata = _metadata()
    metadata["version"] = bad_version
    metadata["icon"] = None
    with pytest.raises(ValueError, match="semantic version"):
        build_integration_bundle_zip(metadata)


@pytest.mark.parametrize(
    "good_version",
    ["1.0.0", "0.1.0", "10.20.30", "1.0.0-alpha.10", "1.0.0-0.3.7", "1.0.0+20130313144700"],
)
def test_parse_accepts_valid_semver_versions(good_version):
    metadata = _metadata()
    metadata["version"] = good_version
    metadata["icon"] = None
    parsed = parse_integration_bundle_zip(build_integration_bundle_zip(metadata))
    assert parsed.metadata.version == good_version


def test_normalize_custom_provider_slug():
    assert normalize_custom_provider_slug("my-integration") == "custom:my-integration"
    assert normalize_custom_provider_slug("custom:my-integration") == "custom:my-integration"


def test_parse_integration_instance_payload():
    metadata = _metadata()
    metadata["integration_instance"] = {
        "name": "Primary Connection",
        "description": "Reusable imported instance",
        "auth_type": "api_key",
        "api_key": "super-secret",
        "extra_config": {"api_url": "https://api.example.test"},
        "tags": ["prod", "bundle"],
    }

    raw = build_integration_bundle_zip(metadata, assets={"assets/icon.png": b"PNG"})
    parsed = parse_integration_bundle_zip(raw)

    assert parsed.integration_instance is not None
    assert parsed.integration_instance.name == "Primary Connection"
    assert parsed.integration_instance.api_key == "super-secret"
    assert parsed.integration_instance.extra_config == {
        "api_url": "https://api.example.test"
    }


def _zip_with_checksum(metadata: dict) -> bytes:
    """Build a raw bundle ZIP with a valid checksum, bypassing build-time validation."""
    from hashlib import sha256

    metadata_json = json.dumps(metadata).encode("utf-8")
    return _zip(
        {
            INTEGRATION_BUNDLE_METADATA_FILE: metadata_json,
            INTEGRATION_BUNDLE_CHECKSUM_FILE: sha256(metadata_json).hexdigest().encode(),
            "assets/icon.png": b"PNG",
        }
    )


def test_parse_accepts_valid_tool_execution_spec():
    metadata = _metadata()
    metadata["tools"] = [
        {
            "name": "search_things",
            "description": "Search things.",
            "input_schema": {"type": "object", "properties": {}},
            "execution": {
                "method": "POST",
                "path": "/v1/search",
                "body_mapping": {"query": {"from": "query", "required": True}},
                "auth": {"type": "bearer"},
                "timeout_ms": 5000,
            },
        }
    ]

    parsed = parse_integration_bundle_zip(build_integration_bundle_zip(
        metadata, assets={"assets/icon.png": b"PNG"}
    ))
    assert parsed.metadata_raw["tools"][0]["execution"]["method"] == "POST"


def test_parse_rejects_invalid_tool_execution_spec():
    """Import/validation must reject malformed execution specs (not just export)."""
    metadata = _metadata()
    metadata["tools"] = [
        {
            "name": "broken_tool",
            "description": "Broken execution spec.",
            "execution": {"method": "INVALID", "path": "no-leading-slash"},
        }
    ]

    raw = _zip_with_checksum(metadata)
    with pytest.raises(ValueError, match=r"tools\[0\]\.execution"):
        parse_integration_bundle_zip(raw)


# ── Members read within a bound, not as they declare ─────────────────


def test_a_member_declaring_less_than_it_holds_is_refused_having_decompressed_what_it_declared():
    """``ZipFile.read`` holds such a member twice over (128 MiB here)."""
    from tests.archive_helpers import declare_size, peak_mib

    metadata_json = json.dumps(_metadata()).encode("utf-8")
    raw = _zip(
        {
            INTEGRATION_BUNDLE_METADATA_FILE: metadata_json + b" " * (64 * 2**20),
            INTEGRATION_BUNDLE_CHECKSUM_FILE: b"0" * 64,
            "assets/icon.png": b"PNG",
        }
    )
    raw = declare_size(raw, INTEGRATION_BUNDLE_METADATA_FILE, 10)
    peak, raised = peak_mib(lambda: parse_integration_bundle_zip(raw))
    assert type(raised) is ValueError
    assert str(raised) == "Invalid ZIP file."
    assert peak <= 16


@pytest.mark.parametrize("how", ["damaged stream", "unknown compression", "encrypted"])
def test_an_unreadable_member_is_an_invalid_zip_not_a_500(how):
    from tests.archive_helpers import corrupt_member

    raw = build_integration_bundle_zip(_metadata(), assets={"assets/icon.png": bytes(range(256)) * 64})
    with pytest.raises(ValueError, match=r"^Invalid ZIP file\.$"):
        parse_integration_bundle_zip(corrupt_member(raw, "assets/icon.png", how))
