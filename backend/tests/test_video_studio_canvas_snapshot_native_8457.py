"""#8457, slice 8: a canvas layer is snapshotted at its own pixels, scaled on the encoder's thread.

The post-#8677 probe on a GPU worker (take bdaf7b20) still read "copies cost the page 20.0 ms (3.8 ms finding what
is drawn over)" with the page composing nothing: the snapshots it asked resized (``resizeWidth``) scale a GPU
canvas's buffer on the page's thread. The page now asks each snapshot cropped at the layer's own pixels and the
encoder scales it while composing (``drawImage`` to the copy's size); the page's ms spent taking the snapshots is
kept (``snapshotting``) and said on the capture line, so the next probe tells that part from the rest.
"""

from __future__ import annotations

from app.services.video_studio import canvas_alone


def test_the_page_asks_snapshots_at_the_layers_own_pixels():
    script = canvas_alone.tap_script(30, "(el) => 'c'", encoders=3)
    assert "resizeWidth" not in script and "resizeQuality" not in script
    assert "Math.max(1, Math.round(clip[2] * sx)), Math.max(1, Math.round(clip[3] * sy))];" in script
    assert "createImageBitmap(layer, ...box)" in script
    assert "tap.cost.snapshotting += performance.now() - snapped;" in script
    # The encoder still scales every layer to the copy's size.
    assert "drawing.drawImage(layer, 0, 0, width, height);" in canvas_alone._ENCODER_JS
    assert "__" not in script.replace("__xtm", "")


def test_the_capture_line_says_the_snapshot_part():
    cost = {
        "page": 40.0,
        "copied": 20.0,
        "encoded": 16.0,
        "encoding": 640.0,
        "bytes": 16 * 200 * 1024,
        "scanning": 24.0,
        "snapshotting": 10.0,
    }
    assert canvas_alone.cost_words(cost) == (
        "copies cost the page 2.0 ms (0.5 ms snapshotting, 1.2 ms finding what is drawn over), the encoders 40 ms, "
        "200 KB each (20 drawn, 16 encoded)"
    )
    assert "snapshotting" in canvas_alone.COST_KEYS
