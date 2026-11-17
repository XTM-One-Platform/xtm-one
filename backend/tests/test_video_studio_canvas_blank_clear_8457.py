"""#8457, slice 10: a blank 2D layer cleared again is not snapshotted again.

The post-#8763 probe on a GPU worker (take 840ddc8f) read "copies cost the page 19.2 ms (13.3 ms snapshotting, ...),
its own frames 1.8 ms drawing" and no share of layers kept: a graph clears its lasso and relation layers every frame
though nothing is drawn on them, and every clear marked the layer drawn on, so each copy snapshotted all three layers.
A clear of a 2D layer that holds nothing drawn since its last whole clear no longer marks it; a clear covering the
whole layer leaves it blank; any other draw marks it as before.
"""

from __future__ import annotations

from app.services.video_studio import canvas_alone


def _script() -> str:
    return canvas_alone.tap_script(30, "(el) => 'c'", encoders=3)


def test_a_draw_inks_its_layer():
    script = _script()
    assert "canvas.__xtmDrawn = (canvas.__xtmDrawn || 0) + 1;\n    canvas.__xtmInk = true;" in script


def test_a_clear_of_a_blank_layer_marks_nothing():
    script = _script()
    assert "if (!(canvas instanceof HTMLCanvasElement) || !canvas.__xtmInk) return;" in script
    # Marked first (it erased what was drawn), then blank when the clear covered the whole layer.
    assert script.index("    mark(canvas);\n    if (whole) canvas.__xtmInk = false;") > script.index("const cleared = ")


def test_only_the_2d_clears_go_through_the_blank_check():
    script = _script()
    assert "const twoD = window.CanvasRenderingContext2D && proto === CanvasRenderingContext2D.prototype;" in script
    assert "twoD && (n === 'clearRect' || n === 'reset')" in script
    assert "cleared(this, n === 'reset' || covers(this, args))" in script
    # Every other method, WebGL's clear included, marks its canvas as before.
    assert "function (...args) { mark(this.canvas); return f.apply(this, args); }" in script


def test_a_whole_clear_is_read_through_the_layer_transform():
    script = _script()
    assert "const t = g.getTransform ? g.getTransform() : null;" in script
    # A rotated or skewed clear is never taken as covering the layer.
    assert "if (!t || t.b || t.c || ![x, y, w, h].every(Number.isFinite)) return false;" in script
    assert "Math.max(...xs) >= canvas.width && Math.max(...ys) >= canvas.height" in script
    assert "__" not in script.replace("__xtm", "")
