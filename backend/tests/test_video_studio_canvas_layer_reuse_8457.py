"""#8457, slice 9: a layer not drawn on since an encoder got it is not snapshotted again.

The post-#8741 probe on a GPU worker (take 4b30650d) read "copies cost the page 16.8 ms (11.5 ms snapshotting, 4.5 ms
finding what is drawn over)": the page snapshots every stacked layer of a canvas for every copy, while a graph's
lasso and relation layers mostly stand still as its main layer moves. Each layer is now numbered and marked (times
drawn on, its size and crop); an encoder keeps the last snapshot of each layer it got, and a layer it holds as it is
now is sent without its bitmap. An encoder missing a layer answers ``resend`` and the page sends every layer to it
again. The capture line also says the share of layers kept and the ms the page's own animation frames spent
drawing, so the next probe tells the page's paint from the recorder's cost.
"""

from __future__ import annotations

from app.services.video_studio import canvas_alone


def _script() -> str:
    return canvas_alone.tap_script(30, "(el) => 'c'", encoders=3)


def test_every_draw_marks_its_canvas_held_or_not():
    script = _script()
    assert "canvas.__xtmDrawn = (canvas.__xtmDrawn || 0) + 1;" in script
    assert script.index("canvas.__xtmDrawn = ") < script.index("if (!tap.off && !held) { dirty.add(canvas); wake(); }")


def test_a_layer_the_encoder_holds_is_not_snapshotted_again():
    script = _script()
    assert "const holds = keeps.get(layer.__xtmLayer) === layerMark;" in script
    # The page forgets what the encoder closes: as many layers, least used first.
    assert "while (keeps.size > KEPT_LAYERS) keeps.delete(keeps.keys().next().value);" in script
    assert "tap.cost.reused++;" in script
    # The mark carries the draw count, the layer's size and its crop.
    assert "(layer.__xtmDrawn || 0) + ':' + layer.width + 'x' + layer.height + ':' + box.join(',')" in script
    # A copy goes to the encoder whose holdings it was checked against.
    assert "const id = ++serial, at = id % encoders.length, keeps = sent[at];" in script
    assert "encoders[at].postMessage(" in script
    assert "__" not in script.replace("__xtm", "")


def test_an_encoder_missing_a_layer_asks_for_every_layer_again():
    encoder = canvas_alone._ENCODER_JS.replace("__KEPT_LAYERS__", str(canvas_alone.KEPT_LAYERS))
    assert "if (!before || before.mark !== mark) { lost = true; continue; }" in encoder
    assert "if (lost) { postMessage([id, null, 0, 0, true]); return; }" in encoder
    # At most KEPT_LAYERS snapshots held, the least used closed first.
    assert "while (kept.size > KEPT) {" in encoder
    script = _script()
    assert "if (resend) sent[id % encoders.length].clear();" in script
    assert f"const KEPT = {canvas_alone.KEPT_LAYERS};" in script


def test_the_page_times_its_own_frames():
    script = _script()
    assert "tap.cost.drawing += performance.now() - began;" in script
    assert "running = null; woken = false; tap.cost.frames++;" in script
    for key in ("layers", "reused", "drawing", "frames"):
        assert key in canvas_alone.COST_KEYS


def test_the_capture_line_says_the_layers_kept_and_the_page_drawing():
    cost = {
        "page": 40.0,
        "copied": 20.0,
        "encoded": 16.0,
        "encoding": 640.0,
        "bytes": 16 * 200 * 1024,
        "snapshotting": 10.0,
        "layers": 60.0,
        "reused": 40.0,
        "drawing": 300.0,
        "frames": 20.0,
    }
    assert canvas_alone.cost_words(cost) == (
        "copies cost the page 2.0 ms (0.5 ms snapshotting), 3.0 layers a copy, "
        "67% of its layers kept from a copy before, "
        "its own frames 15.0 ms drawing, the encoders 40 ms, 200 KB each (20 drawn, 16 encoded)"
    )


def test_a_take_kept_before_slice_9_says_neither_part():
    cost = {"page": 46.0, "copied": 20.0, "encoded": 16.0, "encoding": 640.0, "bytes": 16 * 200 * 1024}
    assert canvas_alone.cost_words(cost) == "copies cost the page 2.3 ms, the encoders 40 ms, 200 KB each (20 drawn, 16 encoded)"
