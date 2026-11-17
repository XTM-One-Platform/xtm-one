"""texture_noise reads uncorrelated noise, never natural texture (issue #10083).

A stone barn wall over a straw floor, textured across the whole frame, passes; the crunchy over-sharpened carpet that
motivated the gate (#10036) still fails; a take that adds grain over its clean start picture fails, and a take that
only carries what its start picture already shows passes.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from scipy import ndimage

from app.services.video_studio import perceptual, quality

H, W = 540, 960


def _barn(*, seed: int = 3, grain: float = 0.0, camera: float = 1.5, soft: bool = False) -> Image.Image:
    """Stone blocks with dark joints and a mottled surface, a straw floor of bright strands, a cow: textured nearly
    everywhere, clean (camera noise of *camera* sigma), plus *grain* sigma of added noise."""
    rng = np.random.default_rng(seed)
    wall = np.zeros((H, W))
    course = 35
    for top in range(0, H, course):
        x = -int(rng.integers(0, 60))
        while x < W:
            width = int(rng.integers(45, 90))
            wall[top : top + course, max(x, 0) : max(x + width, 0)] = rng.uniform(80, 170)
            x += width
            if x > 0:
                wall[top : top + course, max(x - 2, 0) : x] = 40
        wall[top : top + 2, :] = 40
    wall += ndimage.gaussian_filter(rng.normal(0, 25, (H, W)), 1.5) * 3
    picture = Image.fromarray(np.clip(wall, 0, 255).astype(np.uint8))
    draw = ImageDraw.Draw(picture)
    for _ in range(2500):
        x, y = rng.uniform(0, W), rng.uniform(H * 0.6, H)
        length, angle = rng.uniform(15, 70), rng.normal(0, 0.5)
        end = (x + length * np.cos(angle), y + length * np.sin(angle))
        draw.line([(x, y), end], fill=int(rng.uniform(150, 230)), width=int(rng.integers(1, 3)))
    draw.ellipse([350, 175, 600, 350], fill=30)
    draw.rectangle([575, 150, 675, 225], fill=230)
    out = np.asarray(picture, dtype=np.float64)
    if soft:
        out = ndimage.gaussian_filter(out, 0.6)
    out = out + rng.normal(0, camera, out.shape)
    if grain:
        out = out + rng.normal(0, grain, out.shape)
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)).convert("RGB")


def _crunchy_carpet() -> Image.Image:
    """A booth: clean walls in the upper half, a carpet over-sharpened into crunchy noise in the lower half."""
    rng = np.random.default_rng(11)
    frame = np.full((H, W), 110.0)
    _yy, xx = np.mgrid[0:H, 0:W]
    frame[: H // 2] = 50 + 140 * ((xx[: H // 2] // 30) % 2)
    weave = Image.fromarray(np.clip(rng.normal(128, 30, (H - H // 2, W)), 0, 255).astype(np.uint8))
    weave = weave.filter(ImageFilter.GaussianBlur(0.8)).filter(ImageFilter.UnsharpMask(3, 300, 0))
    frame[H // 2 :] = np.asarray(weave, dtype=np.float64) - 18
    frame += rng.normal(0, 1.5, frame.shape)
    return Image.fromarray(np.clip(frame, 0, 255).astype(np.uint8)).convert("RGB")


def test_clean_stone_barn_passes_without_and_with_its_start_picture() -> None:
    take = _barn()
    assert perceptual.texture_noise(take) is None
    assert perceptual.texture_noise(take, _barn(soft=True, camera=0.8)) is None


def test_structured_texture_is_not_read_as_noise() -> None:
    noise = perceptual.noise_map(_barn())
    assert noise is not None
    # Straw and stone: busy, but their uncorrelated residual stays well under the floor a crunchy carpet reaches.
    assert float(noise.max()) < perceptual.MIN_NOISE_LEVEL


def test_crunchy_carpet_still_fails_on_the_lower_half() -> None:
    found = perceptual.texture_noise(_crunchy_carpet())
    assert found is not None
    _x0, y0, _x1, y1 = found.box
    assert y0 >= 0.4 and y1 == 1.0
    assert found.against == "frame"
    assert "rest of the frame" in found.measure()


def test_crunchy_carpet_over_most_of_the_frame_fails_on_the_absolute_floor() -> None:
    rng = np.random.default_rng(5)
    weave = Image.fromarray(np.clip(rng.normal(128, 30, (H, W)), 0, 255).astype(np.uint8))
    weave = weave.filter(ImageFilter.GaussianBlur(0.8)).filter(ImageFilter.UnsharpMask(3, 300, 0))
    found = perceptual.texture_noise(weave.convert("RGB"))
    assert found is not None
    assert found.share > perceptual.WHOLE_FRAME_SHARE
    assert found.against == "floor"


def test_grain_added_over_a_clean_start_picture_fails() -> None:
    start = _barn(soft=True, camera=0.8)
    found = perceptual.texture_noise(_barn(grain=8.0), start)
    assert found is not None
    assert found.against == "start"
    assert found.share > 0.5
    assert "start picture" in found.measure()


def test_noise_the_start_picture_already_shows_is_not_the_take_s() -> None:
    start = _barn(grain=8.0, seed=3)
    assert perceptual.texture_noise(_barn(grain=8.0, seed=3), start) is None


def test_round_trip_keeps_what_it_was_judged_against_and_reads_old_entries() -> None:
    found = perceptual.TextureNoise((0.0, 0.5, 1.0, 1.0), 0.5, 3.1, 16.0, "start")
    assert perceptual.TextureNoise.from_dict(found.as_dict()) == found
    old = perceptual.TextureNoise.from_dict({"box": [0, 0.5, 1, 1], "share": 0.5, "ratio": 3.0, "level": 9.0})
    assert old is not None and old.against == "frame"
    assert perceptual.TextureNoise.from_dict(None) is None


def test_report_and_cache_versions_moved() -> None:
    assert quality.QUALITY_REPORT_VERSION >= 55
    assert perceptual.TEXTURE_VERSION >= 2
