"""#8457, slice 7: a canvas copy is composed on the encoder's thread, not on the page's.

The post-#8520 probe on a GPU worker (take 2d77a1e4) read "copies cost the page 19.2 ms (4.3 ms finding what is
drawn over)" while its canvas painted at 12 to 16 fps under the recorder: the page's thread spent more than a frame
of 30 fps per copy drawing each stacked layer onto a copy canvas and transferring it. It now only snapshots each
layer (``createImageBitmap``, cropped and sized to the copy) and hands the snapshots to an encoder, which fills the
colour behind and composes them before signing and encoding the copy. The colour behind is read once per canvas
until it stands still.
"""

from __future__ import annotations

from app.services.video_studio import canvas_alone


def test_the_page_snapshots_its_layers_and_draws_no_copy():
    script = canvas_alone.tap_script(30, "(el) => 'c'", encoders=3)
    assert "snapshots.push([parts.length - 1, createImageBitmap(layer, ...box)]);" in script
    # Cropped at the layer's own pixels, scaled by the encoder (slice 8: no resize asked of the snapshot).
    assert "resizeWidth" not in script
    assert "encoders[at].postMessage([id, key, parts, w, h, fill, quality], bitmaps);" in script
    # Nothing is drawn on the page's thread any more.
    assert "transferToImageBitmap" not in script
    # The old page-side draw, named whole: the encoder's own "drawing.drawImage(layer" is embedded in the script.
    assert "g.drawImage(layer, (clip[0]" not in script
    assert "st.copy.getContext('2d')" not in script
    assert "if (!st.behind || st.idle >= IDLE_AFTER) st.behind = behind(" in script
    # A snapshot refused answers the copy as unreadable, so it is not left in flight.
    assert "() => answered({ data: [id, null, 0, 0, true] })," in script
    assert "__" not in script.replace("__xtm", "")


def test_the_encoder_composes_the_layers_over_the_colour_behind():
    encoder = canvas_alone._ENCODER_JS
    assert "const [id, key, parts, width, height, behind, quality] = e.data;" in encoder
    assert "drawing.fillStyle = behind;" in encoder
    assert "for (const layer of layers) drawing.drawImage(layer, 0, 0, width, height);" in encoder
    # Composed before it is signed; the layers are kept for the copies after (slice 9), all closed on an error.
    assert encoder.index("drawing.drawImage(layer") < encoder.index("shrinking.drawImage(picture")
    assert "forget();" in encoder
