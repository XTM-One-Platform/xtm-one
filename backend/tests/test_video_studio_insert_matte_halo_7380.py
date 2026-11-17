"""An unkeyed insert's matte past its subject (a dark halo, a ghost of the subject beside it), a matte lagging
the subject, and a legible insert hidden by what stands in front. Synthetic fixtures only."""

from __future__ import annotations

import numpy as np
import pytest

from app.services.video_studio import insert_checks, quality, render_checks, screen_insert

SIZE = 200
FRAME_PIXELS = 1920 * 1080


def _disc(cx: float, cy: float, radius: float) -> np.ndarray:
    ys, xs = np.mgrid[0:SIZE, 0:SIZE]
    return (xs - cx) ** 2 + (ys - cy) ** 2 <= radius**2


def _scene(subject: np.ndarray, matte: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Dark footage with a bright subject; the insert drawn everywhere outside *matte*."""
    region = np.full((SIZE, SIZE, 3), 20.0, dtype=np.float32)
    region[subject] = 230.0
    alpha = np.where(matte, 0.0, 1.0).astype(np.float32)
    inside = np.ones((SIZE, SIZE), dtype=bool)
    return region, alpha, matte, inside


def _dilated(mask: np.ndarray, px: int) -> np.ndarray:
    from scipy import ndimage

    return ndimage.binary_dilation(mask, iterations=px)


def test_matte_dilated_by_six_pixels_files_a_halo() -> None:
    subject = _disc(100, 100, 30)
    read = screen_insert.matte_read(*_scene(subject, _dilated(subject, 6)), 1.0, FRAME_PIXELS)
    audit = screen_insert.FrameAudit(matte_halo=read.halo, matte_halo_region=read.halo_region, matte_lag=read.lag)
    assert read.halo is not None and read.halo > screen_insert.MAX_MATTE_HALO_SHARE
    assert audit.matte_haloed and audit.failing
    assert read.hits is not None and read.hits.any()


def test_exact_matte_files_nothing() -> None:
    subject = _disc(100, 100, 30)
    read = screen_insert.matte_read(*_scene(subject, subject), 1.0, FRAME_PIXELS)
    audit = screen_insert.FrameAudit(matte_halo=read.halo, matte_halo_region=read.halo_region, matte_lag=read.lag)
    assert read.halo == 0.0 and read.halo_region == 0.0
    assert read.lag is not None and read.lag < 0.5
    assert not audit.matte_haloed and not audit.matte_lags


def test_matte_one_frame_behind_a_subject_moving_eight_pixels_lags() -> None:
    subject, behind = _disc(108, 100, 30), _disc(100, 100, 30)
    read = screen_insert.matte_read(*_scene(subject, behind), 1.0, FRAME_PIXELS)
    assert read.lag is not None and read.lag > screen_insert.MAX_MATTE_LAG_PX
    assert screen_insert.FrameAudit(matte_lag=read.lag).matte_lags


def test_a_subject_dark_on_the_dark_surface_has_no_lag_to_read() -> None:
    """A subject the footage cannot tell from the surface gives no centroid, never a lag."""
    subject = _disc(100, 100, 30)
    region, alpha, matte, inside = _scene(subject, subject)
    region[subject] = 24.0
    region[95:105, 160:170] = 230.0
    read = screen_insert.matte_read(region, alpha, matte, inside, 1.0, FRAME_PIXELS)
    assert read.lag is None
    assert not screen_insert.FrameAudit(matte_lag=read.lag).matte_lags


def test_a_matte_covering_its_subject_whole_lags_nothing() -> None:
    subject = _disc(100, 100, 30)
    read = screen_insert.matte_read(*_scene(subject, _disc(104, 100, 34)), 1.0, FRAME_PIXELS)
    assert read.lag == 0.0


def test_textured_surface_is_never_read() -> None:
    rng = np.random.default_rng(7)
    subject = _disc(100, 100, 30)
    region, alpha, matte, inside = _scene(subject, _dilated(subject, 6))
    region += rng.uniform(-60, 60, region.shape).astype(np.float32)
    read = screen_insert.matte_read(region, alpha, matte, inside, 1.0, FRAME_PIXELS)
    assert read.halo is None and read.lag is None


def _findings(frames: dict[int, dict], *, legible: bool = True) -> list[str]:
    where = insert_checks.Placed("s1:0", "s1", "Scene s1: a screen insert", 0.0, 2.0, legible=legible)
    return [finding.kind for finding, _ in insert_checks._matte_findings("s1:0", where, frames, {}, lambda n: n / 25.0)]


def test_matte_halo_and_lag_findings_over_frames() -> None:
    frames = {n: {"matte_halo": 0.4, "matte_halo_region": 0.001, "matte_lag": 8.0} for n in range(10)}
    frames.update({n: {"matte_halo": 0.0, "matte_halo_region": 0.0, "matte_lag": 0.0} for n in range(10, 50)})
    kinds = _findings(frames)
    assert render_checks.INSERT_MATTE_HALO in kinds and render_checks.INSERT_MATTE_LAG in kinds
    clean = {n: {"matte_halo": 0.0, "matte_halo_region": 0.0, "matte_lag": 1.0} for n in range(50)}
    assert _findings(clean) == []


@pytest.mark.parametrize(("share", "filed"), [(0.35, True), (0.15, False)])
def test_occluded_on_a_fifth_of_the_frames(share: float, filed: bool) -> None:
    frames = {n: {"foreground_share": share if n < 10 else 0.05} for n in range(50)}
    assert (render_checks.INSERT_OCCLUDED in _findings(frames)) is filed


def test_occluded_peak_on_one_frame_and_ambient_insert() -> None:
    frames = {n: {"foreground_share": 0.45 if n == 3 else 0.05} for n in range(50)}
    assert render_checks.INSERT_OCCLUDED in _findings(frames)
    assert _findings(frames, legible=False) == []


def test_failing_frames_name_the_matte_gates_and_versions_bumped() -> None:
    failing = insert_checks.failing_frames({0: {"matte_halo": 0.5}, 1: {"matte_lag": 9.0}, 2: {}})
    assert failing == {0: ["matte_halo"], 1: ["matte_lag"]}
    assert screen_insert.DRAWING_VERSION >= 17 and quality.QUALITY_REPORT_VERSION >= 36
    for kind in (render_checks.INSERT_MATTE_HALO, render_checks.INSERT_MATTE_LAG, render_checks.INSERT_OCCLUDED):
        assert kind in render_checks.PICTURE_KINDS
