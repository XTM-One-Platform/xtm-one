"""Tests for SVG → PNG rasterization in the LLM image pipeline.

No vision provider (Anthropic, OpenAI, Gemini) accepts ``image/svg+xml`` — an
uploaded SVG previously exhausted every provider and produced a hard
``AllProvidersFailedError``. The fix rasterizes SVG to PNG inside the shared
``prepare_*_for_llm`` choke points so the model can actually see the image, and
degrades gracefully (skip → caller substitutes ``[Image unavailable]``) when
conversion is unavailable or fails — never a hard provider error.

The "real conversion" tests need ``resvg-py`` (a backend dependency in
pyproject.toml, present in CI); they are skipped where the optional renderer is not installed.
The wiring tests monkeypatch the rasterizer boundary so they run everywhere
with deterministic PNG bytes.
"""

import base64
import importlib.util
import io

import pytest

from app.llm.base import (
    _detect_image_mime_from_bytes,
    _rasterize_svg_to_png,
    prepare_base64_image_for_llm,
    prepare_image_bytes_for_llm,
)

SVG_BYTES = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
    b'viewBox="0 0 24 24">'
    b'<rect width="24" height="24" fill="#00bbff"/>'
    b'<circle cx="12" cy="12" r="8" fill="#ffffff"/>'
    b"</svg>"
)

_HAS_RESVG = importlib.util.find_spec("resvg_py") is not None


def _make_png(width: int = 16, height: int = 16, color: tuple = (200, 30, 30)) -> bytes:
    """Return real PNG bytes (Pillow is a project dependency)."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _raster_mimes() -> tuple[str, ...]:
    # optimize_* may re-encode the converted PNG to JPEG when that is smaller.
    return ("image/png", "image/jpeg")


# ---------------------------------------------------------------------------
# Detection sanity
# ---------------------------------------------------------------------------


def test_svg_detected_from_bytes():
    assert _detect_image_mime_from_bytes(SVG_BYTES) == "image/svg+xml"


# ---------------------------------------------------------------------------
# Real rasterization (integration — requires resvg-py)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_RESVG, reason="resvg-py not installed")
def test_rasterize_svg_to_png_produces_valid_png():
    png = _rasterize_svg_to_png(SVG_BYTES, source="test")
    assert png is not None
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert _detect_image_mime_from_bytes(png) == "image/png"


@pytest.mark.skipif(not _HAS_RESVG, reason="resvg-py not installed")
def test_prepare_base64_svg_real_end_to_end():
    b64 = base64.b64encode(SVG_BYTES).decode("ascii")
    result = prepare_base64_image_for_llm(b64, source="test-real-b64")
    assert result is not None
    _, mime = result
    assert mime in _raster_mimes()
    assert mime != "image/svg+xml"


@pytest.mark.skipif(not _HAS_RESVG, reason="resvg-py not installed")
def test_prepare_bytes_svg_real_end_to_end():
    result = prepare_image_bytes_for_llm(SVG_BYTES, "image/svg+xml", source="test-real-bytes")
    assert result is not None
    _, mime = result
    assert mime in _raster_mimes()
    assert mime != "image/svg+xml"


def test_rasterize_garbage_svg_never_raises():
    """A malformed SVG must return None (or valid PNG) — never raise."""
    out = _rasterize_svg_to_png(b"<svg this is not valid xml", source="test-garbage")
    assert out is None or out[:8] == b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# Conversion wiring (monkeypatched rasterizer — runs without the dependency)
# ---------------------------------------------------------------------------


def test_prepare_base64_svg_is_converted_to_raster(monkeypatch):
    png = _make_png()
    monkeypatch.setattr(
        "app.llm.base._rasterize_svg_to_png", lambda raw, source="unknown": png
    )

    b64 = base64.b64encode(SVG_BYTES).decode("ascii")
    result = prepare_base64_image_for_llm(b64, source="test-b64")

    assert result is not None
    optimized_b64, mime = result
    assert mime in _raster_mimes()
    assert mime != "image/svg+xml"
    decoded = base64.b64decode(optimized_b64)
    assert _detect_image_mime_from_bytes(decoded) in _raster_mimes()


def test_prepare_bytes_svg_is_converted_to_raster(monkeypatch):
    png = _make_png()
    monkeypatch.setattr(
        "app.llm.base._rasterize_svg_to_png", lambda raw, source="unknown": png
    )

    result = prepare_image_bytes_for_llm(SVG_BYTES, "image/svg+xml", source="test-bytes")

    assert result is not None
    optimized, mime = result
    assert mime in _raster_mimes()
    assert _detect_image_mime_from_bytes(optimized) in _raster_mimes()


def test_prepare_bytes_svg_detected_even_when_declared_png(monkeypatch):
    """An SVG mislabelled as ``image/png`` is still detected by magic bytes
    and rasterized — the declared content type is only a hint."""
    png = _make_png()
    received: dict = {}

    def _fake(raw, source="unknown"):
        received["raw"] = raw
        return png

    monkeypatch.setattr("app.llm.base._rasterize_svg_to_png", _fake)

    result = prepare_image_bytes_for_llm(SVG_BYTES, "image/png", source="test-mislabelled")

    assert result is not None
    assert received["raw"] == SVG_BYTES
    _, mime = result
    assert mime in _raster_mimes()


# ---------------------------------------------------------------------------
# Graceful fallback — conversion unavailable / failed → skip, never hard-fail
# ---------------------------------------------------------------------------


def test_prepare_base64_svg_returns_none_when_conversion_fails(monkeypatch):
    monkeypatch.setattr(
        "app.llm.base._rasterize_svg_to_png", lambda raw, source="unknown": None
    )
    b64 = base64.b64encode(SVG_BYTES).decode("ascii")
    assert prepare_base64_image_for_llm(b64, source="test-fallback") is None


def test_prepare_bytes_svg_returns_none_when_conversion_fails(monkeypatch):
    monkeypatch.setattr(
        "app.llm.base._rasterize_svg_to_png", lambda raw, source="unknown": None
    )
    assert (
        prepare_image_bytes_for_llm(SVG_BYTES, "image/svg+xml", source="test-fallback")
        is None
    )


def test_non_svg_raster_path_unaffected():
    """A normal PNG must still pass straight through without invoking the
    SVG converter."""
    png = _make_png()
    result = prepare_image_bytes_for_llm(png, "image/png", source="test-png")
    assert result is not None
    _, mime = result
    assert mime in _raster_mimes()


# ---------------------------------------------------------------------------
# Import-failure discrimination: an absent dependency is
# benign (no traceback), but a present-but-broken wheel must log a traceback.
# ---------------------------------------------------------------------------


def _patch_resvg_import(monkeypatch, exc: Exception) -> None:
    """Make ``import resvg_py`` raise *exc*, delegating all other imports."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "resvg_py":
            raise exc
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_rasterize_missing_dependency_is_benign_without_traceback(monkeypatch, caplog):
    """resvg-py itself absent (``ModuleNotFoundError`` naming ``resvg_py``) is
    the expected optional-dep case: warn WITHOUT a traceback and return None."""
    import logging

    _patch_resvg_import(
        monkeypatch,
        ModuleNotFoundError("No module named 'resvg_py'", name="resvg_py"),
    )

    with caplog.at_level(logging.WARNING, logger="app.llm.base"):
        out = _rasterize_svg_to_png(SVG_BYTES, source="absent")

    assert out is None
    recs = [r for r in caplog.records if "not installed" in r.getMessage()]
    assert recs, "expected a 'not installed' warning"
    assert recs[-1].exc_info is None, "an absent optional dep must not log a traceback"


def test_rasterize_broken_install_modulenotfound_logs_traceback(monkeypatch, caplog):
    """A ``ModuleNotFoundError`` naming a *different* module means resvg-py is
    installed but its import chain is broken (missing runtime dep / compiled
    submodule). That must NOT be silenced as "absent" — log WITH a traceback
    while still returning None for graceful fallback."""
    import logging

    _patch_resvg_import(
        monkeypatch,
        ModuleNotFoundError(
            "No module named 'resvg_py._resvg_py'", name="resvg_py._resvg_py"
        ),
    )

    with caplog.at_level(logging.WARNING, logger="app.llm.base"):
        out = _rasterize_svg_to_png(SVG_BYTES, source="broken-install")

    assert out is None
    recs = [r for r in caplog.records if "its import failed" in r.getMessage()]
    assert recs, "expected an 'import failed (missing module ...)' warning"
    assert recs[-1].exc_info is not None, "a broken install must log a traceback"


def test_rasterize_broken_wheel_importerror_logs_traceback(monkeypatch, caplog):
    """A present-but-broken wheel raises a plain ``ImportError`` (missing
    symbols / ABI mismatch), NOT ``ModuleNotFoundError``. That must be logged
    WITH a traceback while still returning None for graceful fallback."""
    import logging

    _patch_resvg_import(
        monkeypatch,
        ImportError("dynamic module does not define module export function"),
    )

    with caplog.at_level(logging.WARNING, logger="app.llm.base"):
        out = _rasterize_svg_to_png(SVG_BYTES, source="broken-wheel")

    assert out is None
    recs = [r for r in caplog.records if "present but failed" in r.getMessage()]
    assert recs, "expected a 'present but failed to import' warning"
    assert recs[-1].exc_info is not None, "a broken wheel must log a traceback"
