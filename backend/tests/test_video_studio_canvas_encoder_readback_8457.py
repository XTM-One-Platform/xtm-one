"""#8457, slice 5: the encoders no longer read a whole GPU copy back to sign it.

The copy-cost probe of slice 4 (production take on a GPU worker, 2880x1620 copies) read "copies cost the page
23.9 ms, the encoders 218 ms, 177 KB each; 20 copies drawn smaller to keep up": the encoders, not the page, hold
the copy rate back. Each encoder signed a copy by drawing it straight onto a canvas kept for reading
(``willReadFrequently``), which reads the whole GPU copy back to memory before shrinking it, then read it back a
second time to encode it. The copy is now shrunk on a GPU-drawn canvas and only the 64x36 signature is read back.
The take also says the part of the encoders' ms spent comparing and the part of the page's ms spent finding what
is drawn over its canvases, so the next probe names the remaining cost.
"""

from __future__ import annotations

from app.services.video_studio import canvas_alone


def test_the_encoder_shrinks_on_the_gpu_and_reads_back_the_signature_only():
    encoder = canvas_alone._ENCODER_JS
    assert "small = new OffscreenCanvas(64, 36), shrinking = small.getContext('2d');" in encoder
    assert "shrinking.drawImage(picture, 0, 0, 64, 36);" in encoder
    assert "signing.drawImage(small, 0, 0);" in encoder
    # The full-size copy is never drawn onto the canvas kept for reading.
    assert "signing.drawImage(picture" not in encoder
    assert encoder.index("shrinking.drawImage(picture") < encoder.index("signing.getImageData(0, 0, 64, 36)")


def test_the_encoder_answers_the_ms_it_spent_comparing():
    encoder = canvas_alone._ENCODER_JS
    assert "const signed = performance.now() - began;" in encoder
    assert "performance.now() - began, signed]" in encoder
    script = canvas_alone.tap_script(30, "(el) => 'c'", encoders=3)
    assert "const [id, data, spent, signed, resend] = e.data;" in script
    assert "tap.cost.signing += Number(signed) || 0;" in script
    assert "tap.cost.scanning += performance.now() - scanned;" in script
    assert "__" not in script.replace("__xtm", "")


def test_the_capture_line_says_the_parts_of_the_cost():
    cost = {
        "page": 40.0,
        "copied": 20.0,
        "encoded": 16.0,
        "encoding": 640.0,
        "bytes": 16 * 200 * 1024,
        "signing": 64.0,
        "scanning": 24.0,
    }
    assert canvas_alone.cost_words(cost) == (
        "copies cost the page 2.0 ms (1.2 ms finding what is drawn over), the encoders 40 ms (4 ms comparing), "
        "200 KB each (20 drawn, 16 encoded)"
    )


def test_a_take_kept_before_the_parts_says_the_totals_alone():
    cost = {"page": 46.0, "copied": 20.0, "encoded": 16.0, "encoding": 640.0, "bytes": 16 * 200 * 1024}
    assert canvas_alone.cost_words(cost) == "copies cost the page 2.3 ms, the encoders 40 ms, 200 KB each (20 drawn, 16 encoded)"
