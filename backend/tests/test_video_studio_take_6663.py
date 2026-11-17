"""A step on a screen-reader-only element is refused with the node to act on instead.

Take a86f8d4f right-clicked ``role=option "Intrusion-Set Storm-1567"``: the option belongs to the graph's accessible
list, a visually hidden listbox mirroring the canvas, so the click landed off the page, no menu opened, and the take
failed two steps later with "0 elements of role menuitem". (The take refused for its worker's capture rate in the same
report, take 2f187f5e, waits in the recording lane instead: ``test_video_studio_recording_lane.py``.)

Proven here without a browser (the real Chromium: ``test_video_studio_take_6663_chromium.py``): the words of a
screen-reader-only miss name the role, the list and the node step to use instead.
"""

from __future__ import annotations

from app.services.video_studio import capture


def test_a_screen_reader_only_option_of_a_graph_list_names_the_node_step_to_use():
    """The node step named reads the entry by its text before the comma: no hook needed in the page."""
    words = capture.sr_only_words(
        {
            "role": "option",
            "name": "Intrusion-Set Storm-1567",
            "listbox": "Elements of the graph",
            "lands": None,
            "canvas": True,
        }
    )
    assert words == (
        "the option of the list 'Elements of the graph' is drawn for a screen reader only: a click at its point "
        "lands outside the window, not on it; if it mirrors the canvas, act on node {label: 'Intrusion-Set "
        "Storm-1567'} or a point (at {x, y, in: 'canvas'})"
    )
    assert "demo" not in words


def test_an_entry_without_text_names_the_node_step_without_a_label():
    words = capture.sr_only_words({"role": "option", "name": None, "listbox": None, "lands": "canvas", "canvas": True})
    assert words.endswith("; if it mirrors the canvas, act on node {label} or a point (at {x, y, in: 'canvas'})")


def test_a_screen_reader_only_element_off_any_canvas_is_said_plainly():
    assert capture.sr_only_words({"role": "button", "listbox": None, "lands": "div", "canvas": False}) == (
        "the button is drawn for a screen reader only: a click at its point lands on the div, not on it"
    )


def test_a_long_label_or_role_never_pushes_the_advice_out_of_the_step_message():
    """A failed step keeps the first 300 characters of its message: the advice after the label stays inside them."""
    words = capture.sr_only_words(
        {"role": "r" * 80, "name": "n" * 400, "listbox": "x" * 500, "lands": "y" * 80, "canvas": True}
    )
    assert len(words) <= 300
    assert words.endswith("(at {x, y, in: 'canvas'})")
    assert "{label: '" + "n" * capture.SR_ONLY_NAME_CHARS + "'}" in words


def test_a_screen_reader_only_miss_is_a_step_miss_never_a_take_refusal():
    assert not isinstance(capture.SrOnlyTargetError("x"), capture.TakeError)
