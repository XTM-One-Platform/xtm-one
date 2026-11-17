"""Perceptual quality gates (issue #10036): synthetic voice, AI shot texture noise, insert capture floor."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from app.services.video_studio import perceptual, quality

RATE = 24000


def _voice(seconds: float = 2.0, *, metallic: float = 0.0, seed: int = 1) -> np.ndarray:
    """A voiced signal: harmonics of 140 Hz with a syllable envelope, plus *metallic* energy above 8 kHz."""
    t = np.arange(int(RATE * seconds)) / RATE
    voice = sum(np.sin(2 * np.pi * 140 * k * t) / k for k in range(1, 20))
    envelope = 0.6 + 0.4 * np.sin(2 * np.pi * 4 * t)
    out = voice * envelope
    if metallic:
        rng = np.random.default_rng(seed)
        buzz = sum(np.sin(2 * np.pi * f * t + rng.uniform(0, 6)) for f in (8600, 9300, 10100, 10900, 11500))
        out = out + metallic * buzz
    return (out / np.abs(out).max() * 0.8).astype(np.float64)


def test_clean_voice_raises_nothing() -> None:
    measured = perceptual.timbre(_voice(), RATE)
    assert measured is not None
    assert perceptual.synthetic_voice(measured) is None
    assert perceptual.synthetic_voice(measured, measured) is None


def test_metallic_backup_take_is_synthetic_voice() -> None:
    reference = perceptual.timbre(_voice(), RATE)
    take = perceptual.timbre(_voice(metallic=0.6), RATE)
    assert reference is not None and take is not None
    assert take.artifact_share > reference.artifact_share
    why = perceptual.synthetic_voice(take, reference)
    assert why is not None and "metallic" in why
    # Without a reference, the fixed ceilings still catch it.
    assert perceptual.synthetic_voice(take) is not None


def test_timbre_round_trip_and_unreadable() -> None:
    measured = perceptual.timbre(_voice(), RATE)
    assert measured is not None
    assert perceptual.Timbre.from_dict(measured.as_dict()) is not None
    assert perceptual.Timbre.from_dict({"flatness": 0.1}) is None
    assert perceptual.timbre(np.zeros(RATE), RATE) is None
    assert perceptual.timbre(_voice(), 8000) is None


@pytest.mark.parametrize(
    ("model", "speed", "said"),
    [
        ("tts-1-hd", 0.85, True),
        ("tts-1", 0.92, True),
        ("tts-1-hd", 1.0, False),
        ("tts-1-hd", 0.95, False),
        ("gpt-4o-mini-tts", 0.85, False),
        (None, 0.85, False),
    ],
)
def test_time_stretched_speed_advisory(model: str | None, speed: float, said: bool) -> None:
    assert (perceptual.stretched_speed(model, speed) is not None) is said


def _frame(noisy_carpet: bool) -> Image.Image:
    """A 512x288 frame: a detailed subject in the upper half, a flat carpet in the lower half."""
    rng = np.random.default_rng(7)
    h, w = 288, 512
    frame = np.full((h, w), 120.0)
    yy, xx = np.mgrid[0:h, 0:w]
    # Subject: strong structure (bars and a gradient) over the upper half.
    upper = yy < h // 2
    frame[upper] = 60 + 120 * ((xx[upper] // 24) % 2) + 0.2 * yy[upper]
    if noisy_carpet:
        frame[~upper] += rng.normal(0, 28, size=frame[~upper].shape)
    return Image.fromarray(np.clip(frame, 0, 255).astype(np.uint8)).convert("RGB")


def test_noisy_carpet_is_flagged_with_its_region() -> None:
    found = perceptual.texture_noise(_frame(noisy_carpet=True))
    assert found is not None
    _x0, y0, _x1, y1 = found.box
    assert y0 >= 0.4 and y1 == 1.0
    assert "lower" in found.region()
    assert perceptual.TextureNoise.from_dict(found.as_dict()) == perceptual.TextureNoise.from_dict(found.as_dict())


def test_clean_carpet_raises_nothing() -> None:
    assert perceptual.texture_noise(_frame(noisy_carpet=False)) is None


def test_capture_floor() -> None:
    assert perceptual.under_capture_floor(70, 10.0) == pytest.approx(7.0)
    assert perceptual.under_capture_floor(300, 10.0) is None
    assert perceptual.under_capture_floor(None, 10.0) is None


def test_blocks_on_brand_film_and_product_demo_only() -> None:
    assert perceptual.blocks(("brand_film",))
    assert perceptual.blocks(("tutorial", "product_demo"))
    assert not perceptual.blocks(("tutorial",))
    assert not perceptual.blocks(None)
    template = SimpleNamespace(style=SimpleNamespace(purposes=["brand_film"]))
    assert perceptual.purposes_of(template) == ("brand_film",)
    assert perceptual.purposes_of(None) == ()


def test_quality_report_version_and_kinds() -> None:
    assert quality.QUALITY_REPORT_VERSION >= 53
    kinds = set(quality.FindingKind.__args__)  # type: ignore[attr-defined]
    assert {"synthetic_voice", "texture_noise", "insert_capture_floor"} <= kinds
