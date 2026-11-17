"""A GPU worker's recordings copy their canvases alone too.

On a GPU recording lane a 0.8 s canvas re-layout after a click was still screencast at 11 to 20 fps and flagged as
stepping motion: the screencast's frames are encoded on the CPU whatever draws them. Its canvases are now copied at
each animation frame of the take's rate on GPU GL as on software GL (``capture_rate.GlBackend.alone``), at the take's
own pixel ratio up to ``canvas_alone.GPU_ALONE_RATIO`` and at ``GPU_ALONE_QUALITY``, and composited only where the
screencast fell short and the copies match what it painted (``canvas_alone.composite``, unchanged).
"""

from __future__ import annotations

from app.services.video_studio import canvas_alone, capture_rate

NAME_JS = "(el) => el.tagName.toLowerCase()"


def test_both_backends_copy_their_canvases_alone():
    software = capture_rate.GlBackend(capture_rate.SOFTWARE_GL_ARGS, gpu=False)
    gpu = capture_rate.GlBackend(capture_rate.GPU_GL_ARGS, gpu=True)
    assert software.alone is True and gpu.alone is True


def test_software_gl_keeps_its_reduced_copies():
    software = capture_rate.GlBackend(capture_rate.SOFTWARE_GL_ARGS, gpu=False)
    assert (software.alone_ratio, software.alone_quality) == (canvas_alone.ALONE_RATIO, canvas_alone.ALONE_QUALITY)
    script = canvas_alone.tap_script(30, NAME_JS, ratio=software.alone_ratio, quality=software.alone_quality)
    assert "const FPS = 30, RATIO = 1.0, QUALITY = 0.8" in script


def test_gpu_gl_copies_at_its_ratio_and_a_higher_quality():
    gpu = capture_rate.GlBackend(capture_rate.GPU_GL_ARGS, gpu=True)
    assert (gpu.alone_ratio, gpu.alone_quality) == (canvas_alone.GPU_ALONE_RATIO, canvas_alone.GPU_ALONE_QUALITY)
    # Ratio 1 since #8457 slice 6: a copy at the take's own ratio held the encoders back.
    assert gpu.alone_ratio == canvas_alone.ALONE_RATIO and gpu.alone_quality > canvas_alone.ALONE_QUALITY
    script = canvas_alone.tap_script(30, NAME_JS, ratio=gpu.alone_ratio, quality=gpu.alone_quality)
    assert f"const FPS = 30, RATIO = {float(gpu.alone_ratio)!r}, QUALITY = {float(gpu.alone_quality)!r}" in script
    # The page never copies above its own pixel ratio, at its full ratio or lowered to keep up (#8462).
    assert "Math.min(devicePixelRatio, lower ? LOW_RATIO : RATIO)" in script
    assert "__" not in script.replace("__xtm", "")


def test_the_default_script_is_the_software_one():
    assert "const FPS = 60, RATIO = 1.0, QUALITY = 0.8" in canvas_alone.tap_script(60, NAME_JS)


async def test_a_tap_keeps_the_ratio_and_quality_it_copied_at(tmp_path):
    class _Page:
        async def evaluate(self, *_args):
            return {"frames": [], "off": None}

    tap = canvas_alone.Tap(
        _Page(),
        str(tmp_path),
        origin=1000.0,
        max_copies=10,
        ratio=canvas_alone.GPU_ALONE_RATIO,
        quality=canvas_alone.GPU_ALONE_QUALITY,
    )
    kept = await tap.stop()
    assert kept == {"ratio": canvas_alone.GPU_ALONE_RATIO, "quality": canvas_alone.GPU_ALONE_QUALITY, "copies": 0}


def test_the_note_names_the_gl_it_was_drawn_with():
    runs = [{"composited": True, "step": 2, "t": [4.0, 4.8], "painted": 14.0, "fps": 30.0, "canvas": "canvas#graph"}]
    [software] = canvas_alone.notes(runs)
    assert software.startswith("Captured alone where the screencast fell short (software GL): canvas#graph at step 2")
    assert software.endswith("Those transitions play at reduced quality while they move.")
    [gpu] = canvas_alone.notes(runs, "GPU GL")
    assert gpu == (
        "Captured alone where the screencast fell short (GPU GL): canvas#graph at step 2 (0.8 s, painted at 14 fps, "
        "copied at 30 fps)."
    )
    assert canvas_alone.notes([{**runs[0], "composited": False}]) == []
    # On a GPU a run kept as painted is said too, with why (#10955).
    assert canvas_alone.notes([{**runs[0], "composited": False, "why": "copied no faster than painted"}], "GPU GL") == [
        "Copied alone and kept as painted (GPU GL): canvas#graph at step 2 (0.8 s, painted at 14 fps, copied at 30 fps, "
        "copied no faster than painted)."
    ]
