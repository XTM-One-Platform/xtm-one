"""#8457, slice 6: a GPU page copies its canvases at pixel ratio 1.

The production probe after slice 5 (take 7179792a, 2880x1620 copies at the take's pixel ratio 1.5) read "copies
cost the page 21.9 ms (4.4 ms finding what is drawn over), the encoders 165 ms (22 ms comparing), 186 KB each;
3 copies skipped, every encoder behind; 14 copies drawn smaller to keep up": three encoders kept about 13 copies a
second, short of the take's 30. The readback and the JPEG encode of a copy grow with its pixels, so a GPU page now
copies at ratio 1 (44% of the pixels of a ratio 1.5 copy), the composite scaling each copy to the frame as it did
for the copies drawn smaller. At ratio 1 no copy is drawn smaller: every copy of a run has one size.
"""

from __future__ import annotations

from app.services.video_studio import canvas_alone, capture_rate


def test_a_gpu_page_copies_at_ratio_1_with_its_higher_quality():
    gpu = capture_rate.GlBackend(capture_rate.GPU_GL_ARGS, gpu=True)
    assert canvas_alone.GPU_ALONE_RATIO == 1.0 == gpu.alone_ratio
    assert gpu.alone_quality == canvas_alone.GPU_ALONE_QUALITY > canvas_alone.ALONE_QUALITY
    assert gpu.alone_encoders == canvas_alone.gpu_encoders() >= 1


def test_a_gpu_page_never_draws_a_copy_smaller():
    gpu = capture_rate.GlBackend(capture_rate.GPU_GL_ARGS, gpu=True)
    script = canvas_alone.tap_script(
        30, "(el) => 'c'", ratio=gpu.alone_ratio, quality=gpu.alone_quality, encoders=gpu.alone_encoders
    )
    # ``lower`` needs RATIO above LOW_RATIO: equal, no copy is drawn smaller ("copies drawn smaller to keep up").
    assert "RATIO = 1.0, QUALITY = 0.9," in script and "LOW_RATIO = 1.0," in script
    assert "const lower = RATIO > LOW_RATIO && flight >= LOWER;" in script
    assert "__" not in script.replace("__xtm", "")
