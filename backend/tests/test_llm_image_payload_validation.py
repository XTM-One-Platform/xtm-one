"""Tests for image payload validation in LLM message preparation."""

import base64
import io

import pytest
from PIL import Image

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import (
    IMAGE_UNAVAILABLE_TEXT,
    ChatMessage,
    build_openai_messages,
    prepare_base64_image_for_llm,
    prepare_image_bytes_for_llm,
)


def test_prepare_base64_image_rejects_empty_and_invalid_payloads():
    assert prepare_base64_image_for_llm("", source="test-empty") is None
    assert prepare_base64_image_for_llm("   ", source="test-space") is None
    assert (
        prepare_base64_image_for_llm(
            "data:image/png;base64,",
            source="test-empty-data-url",
        )
        is None
    )
    assert (
        prepare_base64_image_for_llm(
            "not-base64-image-data",
            source="test-invalid",
        )
        is None
    )


def test_prepare_image_bytes_rejects_empty_and_non_image_payloads():
    assert prepare_image_bytes_for_llm(b"", "image/png", source="test-empty") is None
    assert (
        prepare_image_bytes_for_llm(
            b"not-an-image",
            "image/png",
            source="test-non-image",
        )
        is None
    )


def _make_png_base64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), color="red").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_prepare_base64_image_accepts_valid_png():
    b64 = _make_png_base64()

    result = prepare_base64_image_for_llm(b64, source="test-valid-png")

    assert result is not None
    optimized, mime = result
    assert isinstance(optimized, str)
    assert optimized
    assert mime.startswith("image/")


def test_prepare_base64_image_accepts_valid_data_url():
    data_url = f"data:image/png;base64,{_make_png_base64()}"

    result = prepare_base64_image_for_llm(data_url, source="test-valid-data-url")

    assert result is not None
    optimized, mime = result
    assert isinstance(optimized, str)
    assert optimized
    assert mime.startswith("image/")


def test_build_openai_messages_replaces_invalid_images_with_placeholder():
    messages = [ChatMessage(role="user", content="hello", images=[""])]

    converted = build_openai_messages(messages)

    assert converted[0]["role"] == "user"
    # Reference the shared ``IMAGE_UNAVAILABLE_TEXT`` constant exported
    # from ``app.llm.base`` so a future wording change only requires
    # updating the constant, not every test that asserts on the
    # placeholder.
    assert converted[0]["content"] == [
        {"type": "text", "text": "hello"},
        {"type": "text", "text": IMAGE_UNAVAILABLE_TEXT},
    ]


@pytest.mark.asyncio
async def test_anthropic_builder_replaces_invalid_images_with_placeholder():
    provider = AnthropicProvider(api_key="test-key")
    messages = [ChatMessage(role="user", content="hello", images=[""])]

    system_msg, conversation = await provider._build_anthropic_messages(messages)

    assert system_msg is None
    assert conversation == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": IMAGE_UNAVAILABLE_TEXT},
                {"type": "text", "text": "hello"},
            ],
        }
    ]


def _make_padded_png_base64() -> str:
    """Build a base64-encoded 1x1 PNG whose natural encoding has at
    least one trailing ``=`` padding character.

    Generates PNGs at varying widths and keeps the first whose byte
    count is not a multiple of 3 (which is what produces ``=`` padding
    in base64).  This guarantees the test fixture exercises the
    missing-padding code path no matter what PIL produces.
    """
    for width in range(1, 16):
        buf = io.BytesIO()
        Image.new("RGB", (width, 1), color="red").save(buf, format="PNG")
        encoded = base64.b64encode(buf.getvalue()).decode("ascii")
        if encoded.endswith("="):
            return encoded
    raise AssertionError(
        "Could not find a PIL PNG fixture whose base64 encoding has trailing "
        "padding — the regression test cannot be exercised."
    )


def test_prepare_base64_image_accepts_unpadded_payload():
    """A base64 payload that arrives without the trailing ``=`` padding
    (e.g. some transports strip it) must still optimize successfully.

    The validation step already pads internally before
    ``base64.b64decode(padded, validate=True)``; the optimization step
    must operate on the same padded variant or it will raise
    ``binascii.Error('Incorrect padding')`` for otherwise-valid images
    and they'll be silently dropped at the LLM boundary.

    The fixture is generated with guaranteed natural ``=`` padding so
    that stripping the padding produces an "unpadded but still valid"
    payload — important: we strip ONLY the ``=`` characters, never any
    real base64 character (doing so would corrupt the underlying image
    bytes and the test would no longer represent the regression
    scenario).
    """
    padded = _make_padded_png_base64()
    assert padded.endswith("="), "fixture invariant: must end with ``=``"
    unpadded = padded.rstrip("=")
    assert "=" not in unpadded, "post-strip invariant"

    result = prepare_base64_image_for_llm(unpadded, source="test-unpadded")

    assert result is not None
    optimized, mime = result
    assert optimized
    assert mime.startswith("image/")


def test_prepare_base64_image_accepts_unpadded_data_url():
    """Same regression but via the data-URL surface, which is the
    actual shape inbound chat messages use."""
    padded = _make_padded_png_base64()
    unpadded = padded.rstrip("=")
    data_url = f"data:image/png;base64,{unpadded}"

    result = prepare_base64_image_for_llm(
        data_url, source="test-unpadded-data-url"
    )

    assert result is not None
    optimized, mime = result
    assert optimized
    assert mime.startswith("image/")


def test_prepare_base64_image_logs_named_source_on_failure(caplog):
    """All ``prepare_base64_image_for_llm`` call sites pass a
    descriptive ``source`` argument so failures in production logs can
    be traced back to the originating provider / message / index. The
    helper must surface that source string in its warning output."""
    import logging

    caplog.set_level(logging.WARNING, logger="app.llm.base")
    result = prepare_base64_image_for_llm(
        "", source="anthropic:user_message_image:3"
    )

    assert result is None
    assert "anthropic:user_message_image:3" in caplog.text


def test_prepare_image_bytes_logs_named_source_on_failure(caplog):
    """Same expectation for the bytes-path helper — the four file-image
    call sites (OpenAI/Anthropic/Gemini base.py + provider modules) now
    all pass a stable ``source`` like
    ``{provider}:user_file_image:{idx}[{file_id}]`` - the id, never the
    stored name, which is a third party's text."""
    import logging

    caplog.set_level(logging.WARNING, logger="app.llm.base")
    result = prepare_image_bytes_for_llm(
        b"",
        "image/png",
        source="anthropic:user_file_image:0[file-1]",
    )

    assert result is None
    assert "anthropic:user_file_image:0[file-1]" in caplog.text


# ---------------------------------------------------------------------------
# SVG-detection tightening
# ---------------------------------------------------------------------------


def test_detect_image_mime_rejects_xml_documents_that_are_not_svg():
    """Plain XML documents (RSS feeds, sitemaps, SOAP bodies, …) must
    NOT be classified as SVG just because they start with ``<?xml``.
    The previous check let any payload with ``<?xml`` slip past the
    image gate and reach the provider as a non-image payload,
    reintroducing the deterministic request failures this PR is meant
    to prevent."""
    from app.llm.base import _detect_image_mime_from_bytes

    rss_feed = b'<?xml version="1.0"?><rss version="2.0"><channel/></rss>'
    sitemap = b'<?xml version="1.0"?><urlset xmlns="http://example/sitemap"><url/></urlset>'
    plain_xml = b'<?xml version="1.0"?><note><body>not an image</body></note>'

    assert _detect_image_mime_from_bytes(rss_feed) is None
    assert _detect_image_mime_from_bytes(sitemap) is None
    assert _detect_image_mime_from_bytes(plain_xml) is None


def test_detect_image_mime_accepts_svg_with_and_without_xml_prolog():
    """Both forms of SVG must still resolve to ``image/svg+xml``:
    bare ``<svg>`` and ``<?xml … ?><svg>``."""
    from app.llm.base import _detect_image_mime_from_bytes

    bare_svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'
    xml_decl_svg = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<svg xmlns="http://www.w3.org/2000/svg"/>'
    )
    xml_decl_doctype_svg = (
        b'<?xml version="1.0"?><!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "x">'
        b'<svg xmlns="http://www.w3.org/2000/svg"/>'
    )

    assert _detect_image_mime_from_bytes(bare_svg) == "image/svg+xml"
    assert _detect_image_mime_from_bytes(xml_decl_svg) == "image/svg+xml"
    assert _detect_image_mime_from_bytes(xml_decl_doctype_svg) == "image/svg+xml"


def test_prepare_base64_image_rejects_xml_document_misdetected_as_svg(caplog):
    """End-to-end regression: an RSS payload base64-encoded must be
    rejected at the ``prepare_*_for_llm`` boundary instead of being
    forwarded to the provider as ``image/svg+xml``."""
    import logging

    rss_b64 = base64.b64encode(
        b'<?xml version="1.0"?><rss version="2.0"><channel/></rss>'
    ).decode("ascii")
    caplog.set_level(logging.WARNING, logger="app.llm.base")

    result = prepare_base64_image_for_llm(rss_b64, source="test-rss-not-svg")

    assert result is None
    assert "non-image" in caplog.text


# ---------------------------------------------------------------------------
# PIL decodability verification
# ---------------------------------------------------------------------------


def test_prepare_base64_image_rejects_corrupt_png_with_valid_header():
    """A payload that has a valid PNG magic header but a truncated /
    corrupted body must be rejected at the ``prepare_*_for_llm``
    boundary instead of being forwarded to the provider. Without the
    PIL ``verify()`` step, ``optimize_base64_image`` swallows the PIL
    error and returns the original (corrupt) base64, which then
    deterministically fails on every provider request."""
    # PNG magic header followed by garbage that PIL can't decode.
    corrupt = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    corrupt_b64 = base64.b64encode(corrupt).decode("ascii")

    result = prepare_base64_image_for_llm(corrupt_b64, source="test-corrupt-png")

    assert result is None


def test_prepare_image_bytes_rejects_corrupt_png_with_valid_header():
    """Same regression for the bytes-path helper used by Gemini and
    file-attachment paths."""
    corrupt = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

    result = prepare_image_bytes_for_llm(
        corrupt, "image/png", source="test-corrupt-png-bytes"
    )

    assert result is None


def test_prepare_base64_image_accepts_valid_image_with_verify():
    """The new PIL decodability check must not break the happy path —
    a real PNG fixture still optimizes successfully."""
    b64 = _make_png_base64()

    result = prepare_base64_image_for_llm(b64, source="test-valid-verify")

    assert result is not None
    optimized, mime = result
    assert optimized
    assert mime.startswith("image/")


def test_prepare_base64_image_rasterizes_svg_to_png(monkeypatch):
    """SVG payloads are rasterized to PNG before being forwarded — no vision
    provider accepts ``image/svg+xml``.  The rasterizer is
    monkeypatched here so the test is deterministic without the optional
    ``resvg-py`` renderer; see ``test_svg_image_conversion.py`` for the real
    end-to-end conversion."""
    import io as _io

    from PIL import Image as _Image

    _buf = _io.BytesIO()
    _Image.new("RGB", (8, 8), (10, 20, 30)).save(_buf, format="PNG")
    monkeypatch.setattr(
        "app.llm.base._rasterize_svg_to_png",
        lambda raw, source="unknown": _buf.getvalue(),
    )

    svg_b64 = base64.b64encode(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'
    ).decode("ascii")

    result = prepare_base64_image_for_llm(svg_b64, source="test-svg-rasterize")

    assert result is not None
    _, mime = result
    # Never forwarded as SVG — the converted payload is a raster format.
    assert mime != "image/svg+xml"
    assert mime in ("image/png", "image/jpeg")


# ---------------------------------------------------------------------------
# Public ``detect_image_mime`` ↔ private detector alignment
# ---------------------------------------------------------------------------


def test_detect_image_mime_rejects_xml_documents_as_svg():
    """The public ``detect_image_mime`` previously had its own loose
    SVG check (``<?xml`` OR ``<svg ``) that contradicted the stricter
    rule in ``_detect_image_mime_from_bytes`` — any call site using
    the public helper directly (the OpenAI message builders, the
    optimizer's MIME-tracking branch) would reintroduce the
    misclassification bug we fixed in ``prepare_*_for_llm``.

    Now that the public function delegates to the private detector,
    both layers must reject plain XML payloads identically.
    """
    from app.llm.base import detect_image_mime

    rss_b64 = base64.b64encode(
        b'<?xml version="1.0"?><rss version="2.0"><channel/></rss>'
    ).decode("ascii")
    sitemap_b64 = base64.b64encode(
        b'<?xml version="1.0"?><urlset xmlns="http://example/sitemap"><url/></urlset>'
    ).decode("ascii")
    plain_xml_b64 = base64.b64encode(
        b'<?xml version="1.0"?><note><body>not an image</body></note>'
    ).decode("ascii")

    # Falls back to "image/jpeg" (the documented default for unknown
    # payloads) — critically, NOT "image/svg+xml" which would have
    # leaked these non-image payloads to the provider as SVGs.
    assert detect_image_mime(rss_b64) == "image/jpeg"
    assert detect_image_mime(sitemap_b64) == "image/jpeg"
    assert detect_image_mime(plain_xml_b64) == "image/jpeg"


def test_detect_image_mime_still_accepts_real_svg_with_and_without_prolog():
    """Both SVG shapes survive the delegation refactor."""
    from app.llm.base import detect_image_mime

    bare_svg_b64 = base64.b64encode(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'
    ).decode("ascii")
    xml_decl_svg_b64 = base64.b64encode(
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<svg xmlns="http://www.w3.org/2000/svg"/>'
    ).decode("ascii")

    assert detect_image_mime(bare_svg_b64) == "image/svg+xml"
    assert detect_image_mime(xml_decl_svg_b64) == "image/svg+xml"


def test_detect_image_mime_returns_detected_format_for_raster_images():
    """Raster magic-byte detection still works through the delegation."""
    from app.llm.base import detect_image_mime

    png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20).decode("ascii")
    jpeg_b64 = base64.b64encode(b"\xff\xd8\xff" + b"\x00" * 20).decode("ascii")
    gif_b64 = base64.b64encode(b"GIF8" + b"\x00" * 20).decode("ascii")
    webp_b64 = base64.b64encode(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 20).decode(
        "ascii"
    )

    assert detect_image_mime(png_b64) == "image/png"
    assert detect_image_mime(jpeg_b64) == "image/jpeg"
    assert detect_image_mime(gif_b64) == "image/gif"
    assert detect_image_mime(webp_b64) == "image/webp"


def test_detect_image_mime_falls_back_to_jpeg_for_url_and_unknown():
    """Backward-compat: URL inputs and undecodable payloads still
    default to ``image/jpeg``, matching the pre-refactor contract."""
    from app.llm.base import detect_image_mime

    assert detect_image_mime("https://example.com/img.jpg") == "image/jpeg"
    assert detect_image_mime("http://example.com/img.jpg") == "image/jpeg"
    assert detect_image_mime("not-valid-base64-at-all!@#$") == "image/jpeg"


# ---------------------------------------------------------------------------
# Declared-vs-detected content type
# ---------------------------------------------------------------------------


def test_prepare_image_bytes_detects_and_rasterizes_mislabelled_svg(monkeypatch):
    """If the caller declares the wrong ``content_type`` (common for
    file uploads where the client-supplied MIME is unreliable), the
    helper uses the DETECTED type as canonical.  An SVG mislabelled as
    ``image/png`` is therefore still detected by its magic bytes and
    rasterized to PNG before being forwarded — no vision provider accepts
    ``image/svg+xml``.  The rasterizer is monkeypatched so
    the test runs without the optional ``resvg-py`` renderer.
    """
    import io as _io

    from PIL import Image as _Image

    _buf = _io.BytesIO()
    _Image.new("RGB", (8, 8), (10, 20, 30)).save(_buf, format="PNG")
    seen: dict = {}

    def _fake(raw, source="unknown"):
        seen["raw"] = raw
        return _buf.getvalue()

    monkeypatch.setattr("app.llm.base._rasterize_svg_to_png", _fake)

    svg_bytes = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'

    result = prepare_image_bytes_for_llm(
        svg_bytes,
        content_type="image/png",  # declared lies — actually SVG
        source="test-mistyped-svg",
    )

    assert result is not None
    _, mime = result
    # The SVG was detected (not trusted as PNG) and handed to the rasterizer…
    assert seen["raw"] == svg_bytes
    # …and the result is a raster format, never SVG.
    assert mime in ("image/png", "image/jpeg")
    assert mime != "image/svg+xml"


def test_prepare_image_bytes_uses_detected_mime_for_correctly_declared_png():
    """The detected-mime canonicalisation must not break the happy
    path: when declared and detected match, the helper still returns
    the (potentially optimized) bytes + the correct MIME."""
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), color="red").save(buf, format="PNG")
    png_bytes = buf.getvalue()

    result = prepare_image_bytes_for_llm(
        png_bytes,
        content_type="image/png",
        source="test-matching-png",
    )

    assert result is not None
    _, mime = result
    assert mime.startswith("image/")


def test_high_detail_png_screenshot_stays_png_and_is_not_resized_under_2048():
    """Runner screenshots must not be JPEG-crushed or downscaled at 1080p."""
    from app.llm.base import IMAGE_DETAIL_HIGH, optimize_image_bytes

    buf = io.BytesIO()
    Image.new("RGB", (1920, 1080), color=(30, 40, 50)).save(buf, format="PNG")
    raw = buf.getvalue()

    out, mime = optimize_image_bytes(raw, "image/png", detail=IMAGE_DETAIL_HIGH)
    assert mime == "image/png"
    with Image.open(io.BytesIO(out)) as result:
        assert result.size == (1920, 1080)


def test_high_detail_png_downscales_only_above_2048():
    from app.llm.base import IMAGE_DETAIL_HIGH, optimize_image_bytes

    buf = io.BytesIO()
    Image.new("RGB", (3000, 2000), color=(10, 20, 30)).save(buf, format="PNG")
    raw = buf.getvalue()

    out, mime = optimize_image_bytes(raw, "image/png", detail=IMAGE_DETAIL_HIGH)
    assert mime == "image/png"
    with Image.open(io.BytesIO(out)) as result:
        assert max(result.size) == 2048
