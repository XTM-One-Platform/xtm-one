"""#8457 slice 12: a moving canvas finds what the page draws over it less often, and the take says its WebGL snapshots.

Take b02099f7 (a graph moving on a GPU worker): copies cost the page 43.2 ms, 14.0 ms of it finding what is drawn
over (a grid of hit tests every 500 ms) and 27.0 ms snapshotting layers none of which were kept from a copy before.
"""

from app.services.video_studio import canvas_alone


def _script() -> str:
    return canvas_alone.tap_script(30, "(el) => el.id")


def test_a_moving_canvas_scans_what_is_drawn_over_it_every_two_seconds() -> None:
    script = _script()
    assert "const OVER_MS = 500, OVER_MOVING_MS = 2000, KEPT_LAYERS = " in script
    assert "t - st.overAt > (st.idle ? OVER_MS : OVER_MOVING_MS)" in script
    assert "st.over = over(layers, clip); st.overAt = t;" in script
    assert "__" not in script.replace("__xtm", "")


def test_the_page_counts_the_snapshots_of_its_webgl_layers() -> None:
    script = _script()
    assert "layers: 0, reused: 0, drawing: 0, frames: 0, gl: 0," in script
    assert "if (layer.__xtmContext && layer.__xtmContext !== '2d') tap.cost.gl++;" in script
    assert "gl" in canvas_alone.COST_KEYS


def test_the_capture_line_says_the_share_of_webgl_snapshots() -> None:
    cost = {"page": 43.2, "copied": 1, "layers": 3, "reused": 0, "gl": 3, "frames": 1, "drawing": 2.8}
    assert canvas_alone.cost_words(cost) == (
        "copies cost the page 43.2 ms, 3.0 layers a copy, 100% of its snapshots WebGL layers,"
        " its own frames 2.8 ms drawing"
        " (1 drawn, 0 encoded)"
    )
    cost = {"page": 4.0, "copied": 2, "layers": 6, "reused": 2, "gl": 2}
    assert canvas_alone.cost_words(cost) == (
        "copies cost the page 2.0 ms, 3.0 layers a copy, 33% of its layers kept from a copy before,"
        " 50% of its snapshots WebGL layers"
        " (2 drawn, 0 encoded)"
    )


def test_a_take_without_webgl_snapshots_says_nothing_of_them() -> None:
    assert "WebGL" not in canvas_alone.cost_words({"page": 4.0, "copied": 2, "layers": 6, "reused": 0, "gl": 0})
    assert "WebGL" not in canvas_alone.cost_words({"page": 4.0, "copied": 2, "layers": 6, "reused": 6, "gl": 3})


def test_the_capture_line_says_the_layers_a_copy_holds() -> None:
    # Take 12d1da1e: a 2D graph copied on a GPU worker said no share of layers kept and none of WebGL snapshots.
    said = canvas_alone.cost_words({"page": 13.6, "copied": 1, "layers": 1, "reused": 0, "gl": 0})
    assert said == "copies cost the page 13.6 ms, 1.0 layers a copy (1 drawn, 0 encoded)"
    assert "layers a copy" not in canvas_alone.cost_words({"page": 3.0, "copied": 2})
