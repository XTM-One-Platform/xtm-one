"""Tests for branding payload validators and the binary-asset decode helper.

Only covers the load-bearing behavior:

  * image fields are validated as ``data:image/...;base64,...`` URLs with a
    decoded-size cap (``platform_og_image`` additionally accepts http(s) URLs);
  * text fields are length-capped to match the DB schema;
  * color fields must be hex strings;
  * the ``branding/asset`` endpoint's decode helper extracts ``(mime, bytes)``
    from data URLs and rejects malformed input.

The resolver itself is covered by ``test_branding.py``.
"""

from __future__ import annotations

import base64

import pytest
from fastapi import HTTPException

from app.api.v1.settings import (
    _validate_branding_image,
    _validate_hex_color,
    _validate_text_length,
)
from app.services.branding import decode_branding_data_url


def _data_url(mime: str, nbytes: int) -> str:
    payload = base64.b64encode(bytes(nbytes)).decode()
    return f"data:{mime};base64,{payload}"


# ── Image field validator ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("platform_logo", _data_url("image/png", 128)),
        ("platform_favicon", _data_url("image/x-icon", 128)),
        ("platform_og_image", _data_url("image/jpeg", 128)),
        ("platform_og_image", "https://cdn.example.com/og.png"),
        ("platform_og_image", "http://example.com/og.png"),
    ],
    ids=[
        "logo_data_url",
        "favicon_data_url",
        "og_image_data_url",
        "og_image_https_url",
        "og_image_http_url",
    ],
)
def test_validate_branding_image_accepts(field: str, value: str) -> None:
    _validate_branding_image(field, value)  # must not raise


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        # platform_og_image-only: data URL still required for non-og fields
        ("platform_logo", "https://cdn.example.com/logo.png", "data URL"),
        ("platform_favicon", "https://cdn.example.com/favicon.ico", "data URL"),
        # Unsupported MIME
        ("platform_logo", _data_url("image/bmp", 32), "unsupported MIME"),
        ("platform_logo", _data_url("image/svg+xml", 32), "unsupported MIME"),
        ("platform_favicon", _data_url("image/svg+xml", 32), "unsupported MIME"),
        # Bad base64 payload
        ("platform_logo", "data:image/png;base64,!!not-base64!!", "invalid base64"),
        # Oversize (favicon cap is 64 KB)
        ("platform_favicon", _data_url("image/png", 64 * 1024 + 1), "exceeds"),
        # URL too long for og_image
        (
            "platform_og_image",
            "https://example.com/" + "a" * 4096,
            "URL exceeds",
        ),
        # Non-data, non-http for og_image (no data: prefix, no http(s) scheme)
        ("platform_og_image", "ftp://example.com/x.png", "data URL"),
    ],
    ids=[
        "logo_rejects_url",
        "favicon_rejects_url",
        "bad_mime",
        "svg_rejected",
        "favicon_svg_rejected",
        "bad_base64",
        "favicon_oversize",
        "og_image_url_too_long",
        "og_image_bad_scheme",
    ],
)
def test_validate_branding_image_rejects(field: str, value: str, match: str) -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_branding_image(field, value)
    assert exc.value.status_code == 400
    assert match in exc.value.detail


@pytest.mark.parametrize("value", [123, False, {"src": "x"}, ["x"]])
def test_validate_branding_image_rejects_non_strings(value) -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_branding_image("platform_logo", value)
    assert exc.value.status_code == 400
    assert "must be a string" in exc.value.detail


# ── Text length validator ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("field", "length", "should_raise"),
    [
        ("platform_name", 255, False),
        ("platform_name", 256, True),
        ("platform_description", 1000, False),
        ("platform_description", 1001, True),
        ("platform_login_message", 5000, False),
        ("platform_login_message", 5001, True),
    ],
)
def test_validate_text_length(field: str, length: int, should_raise: bool) -> None:
    value = "x" * length
    if should_raise:
        with pytest.raises(HTTPException) as exc:
            _validate_text_length(field, value)
        assert exc.value.status_code == 400
    else:
        _validate_text_length(field, value)


def test_validate_text_length_rejects_non_string() -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_text_length("platform_name", 123)
    assert exc.value.status_code == 400
    assert "must be a string" in exc.value.detail


# ── Hex color validator ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "value",
    ["#abc", "#abcdef", "#abcdef12", "#ABCDEF", "#000", "#FFFFFFFF"],
)
def test_validate_hex_color_accepts(value: str) -> None:
    _validate_hex_color("platform_theme_color", value)  # must not raise


@pytest.mark.parametrize(
    "value",
    ["", "abcdef", "#xyz", "#1234", "#1234567", "red", "#abcdefghi"],
)
def test_validate_hex_color_rejects(value: str) -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_hex_color("platform_theme_color", value)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("value", [123, False, {"color": "#fff"}, ["#fff"]])
def test_validate_hex_color_rejects_non_strings(value) -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_hex_color("platform_theme_color", value)
    assert exc.value.status_code == 400
    assert "must be a string" in exc.value.detail


# ── Asset endpoint decode helper ─────────────────────────────────────────────

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (_data_url("image/png", 4), ("image/png", b"\x00\x00\x00\x00")),
        (_data_url("IMAGE/PNG", 2), ("image/png", b"\x00\x00")),  # lowercased
        ("https://cdn.example.com/logo.png", None),
        ("data:image/png;base64,!!bad!!", None),
        ("not-a-url", None),
        ("", None),
    ],
    ids=["png", "mime_lowercased", "http_url", "bad_base64", "garbage", "empty"],
)
def test_decode_branding_data_url(
    value: str, expected: tuple[str, bytes] | None
) -> None:
    assert decode_branding_data_url(value) == expected
