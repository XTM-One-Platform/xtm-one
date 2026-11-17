"""A node step finds its node by its label with no hook in the application filmed, proven without a browser.

``record_browser_demo`` resolved ``node {label}`` only through ``window.__xtmDemoLocate``, a hook the filmed
application had to expose (``?demo=1``): a product without it, an older release or a third-party tool could not be
filmed with node steps. The recorder now reads what a canvas graph already shows (``node_locate``): the entry of the
list it keeps for screen readers (identity), the label painted under the circle read by OCR (position), else the
highlight the node takes when that entry is made active. The real Chromium and Tesseract:
``test_video_studio_node_locate_6684_chromium.py``.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from app.services.video_studio import canvas_ocr, capture, node_locate, pointer
from app.services.video_studio.node_locate import Entry
from tests.app_sources import APP_DIR, REPO_ROOT, source

LIST = [
    Entry("City Paris, 2 relationships", list=0, index=0, count=6, name="Elements of the graph"),
    Entry("City Parisian Quarter, 1 relationships", list=0, index=1, count=6, name="Elements of the graph"),
    Entry("Intrusion-Set Storm-1567, 6 relationships", list=0, index=2, count=6, name="Elements of the graph"),
    Entry("Malware Akira, 4 relationships", list=0, index=3, count=6, name="Elements of the graph"),
    Entry("Intrusion-Set APT28, 9 relationships", list=0, index=4, count=6, name="Elements of the graph"),
    Entry("Intrusion-Set APT29, 7 relationships", list=0, index=5, count=6, name="Elements of the graph"),
]


# ── Identity: the entry of the graph's accessible list ────────────────────


def test_a_label_ending_an_entry_matches_it_exactly_and_is_the_title_the_canvas_paints():
    identity = node_locate.match_label("Storm-1567", LIST).identity
    assert identity is not None and identity.entry.index == 2
    assert (identity.how, identity.titles) == ("exact", ("Storm-1567",))


def test_an_entry_named_whole_reads_its_title_without_the_type_too():
    """The refusal names the entry's text before its comma, type included: the canvas paints the title, which
    is read as the end of it."""
    identity = node_locate.match_label("Intrusion-Set Storm-1567", LIST).identity
    assert identity is not None and identity.how == "exact"
    assert identity.titles == ("Intrusion-Set Storm-1567", "Storm-1567")


def test_an_exact_label_wins_over_one_that_only_begins_another_entry():
    identity = node_locate.match_label("Paris", LIST).identity
    assert identity is not None and identity.entry.index == 0 and identity.how == "exact"


def test_a_label_in_another_case_matches_and_keeps_the_case_the_canvas_paints():
    identity = node_locate.match_label("storm-1567", LIST).identity
    assert identity is not None and (identity.how, identity.titles) == ("case", ("Storm-1567",))


def test_a_label_beginning_one_entry_only_matches_it_as_a_prefix():
    identity = node_locate.match_label("Aki", LIST).identity
    assert identity is not None and (identity.how, identity.titles) == ("prefix", ("Akira",))
    assert identity.logged() == {"entry": "Malware Akira, 4 relationships", "match": "prefix", "list": "Elements of the graph"}


def test_a_label_beginning_several_entries_is_ambiguous_and_names_them():
    matched = node_locate.match_label("APT", LIST)
    assert matched.identity is None and [entry.head for entry in matched.ambiguous] == [
        "Intrusion-Set APT28",
        "Intrusion-Set APT29",
    ]
    missed = node_locate._missed("APT", {"entries": [vars(e) for e in LIST]}, matched, None, b"png")
    assert str(missed).startswith("the label 'APT' begins 2 entries of the list 'Elements of the graph' mirroring the")
    assert str(missed).endswith(": give the whole label") and missed.picture == b"png"


def test_a_label_no_entry_holds_fails_naming_the_three_closest_titles():
    read = {"entries": [vars(e) for e in LIST], "canvases": [{"box": [0, 0, 100, 100]}]}
    missed = node_locate._missed("Akyra", read, node_locate.match_label("Akyra", LIST), None, b"png")
    assert str(missed) == (
        "no node labelled 'Akyra' on the graph: the list 'Elements of the graph' mirroring it for screen readers has no "
        "such entry"
    )
    assert len(missed.near) == 3 and missed.near[0] == {"role": "node", "name": "Malware Akira"}
    assert missed.picture == b"png"


def test_a_label_with_no_list_and_no_box_says_both_are_missing():
    read = {"entries": [], "canvases": [{"box": [0, 0, 100, 100]}]}
    missed = node_locate._missed("Atlantis", read, node_locate.Matched(), None, None)
    assert str(missed) == (
        "no node labelled 'Atlantis' on the graph: no list on the page mirrors its nodes for screen readers, and OCR "
        "read no 'Atlantis' painted on the canvas"
    )
    assert str(node_locate._missed("Atlantis", {}, node_locate.Matched(), None, None)) == (
        "no canvas shown on the page to find the node 'Atlantis' on"
    )


# ── Position: the label's box read by OCR ─────────────────────────────────


@pytest.mark.parametrize(
    ("box", "point"),
    [
        # A graph zoomed out (6 px labels), at 1x (12 px) and zoomed in (24 px): the circle 1.2 label heights above.
        ((400.0, 300.0, 30.0, 6.0), (415.0, 295.8)),
        ((400.0, 300.0, 60.0, 12.0), (430.0, 291.6)),
        ((400.0, 300.0, 120.0, 24.0), (460.0, 283.2)),
    ],
)
def test_the_node_stands_1_2_label_heights_above_the_label_and_its_box_follows_the_zoom(box, point):
    found, framed = node_locate.node_point(box)
    assert found == point
    height = box[3]
    side = node_locate.NODE_SIDE * height
    # The camera frames the node (a square of 2.4 label heights) and the label under it: larger as the graph zooms in.
    assert framed["y"] == pytest.approx(point[1] - side / 2, abs=0.1)
    assert framed["y"] + framed["height"] == pytest.approx(box[1] + height, abs=0.1)
    assert framed["width"] == pytest.approx(max(box[2], side), abs=0.1)


_TSV_HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"


def _tsv(*words: tuple[str, int, int, int, int, float]) -> str:
    rows = [_TSV_HEADER, "1\t1\t0\t0\t0\t0\t0\t0\t2400\t1400\t-1\t"]
    rows += [f"5\t1\t{i}\t1\t1\t1\t{x}\t{y}\t{w}\t{h}\t{conf}\t{text}" for i, (text, x, y, w, h, conf) in enumerate(words)]
    return "\n".join(rows)


def test_tesseract_words_are_read_back_in_the_pixels_of_the_shot():
    words = canvas_ocr.parse_tsv(_tsv(("Paris", 200, 400, 100, 24, 96.5), ("", 0, 0, 5, 5, 10.0)), factor=2.0)
    assert words == [canvas_ocr.Word("Paris", 100.0, 200.0, 50.0, 12.0, 96.5)]


def _words(*spec: tuple[str, float, float, float, float]) -> list[canvas_ocr.Word]:
    return [canvas_ocr.Word(text, x, y, w, h, 90.0) for text, x, y, w, h in spec]


def test_words_side_by_side_are_one_label_and_a_wide_gap_starts_another():
    words = _words(
        ("Storm-1567", 380, 100, 90, 12),
        ("Intrusion-Set", 260, 101, 110, 12),
        ("Paris", 700, 100, 40, 12),
        ("Berlin", 260, 300, 50, 12),
    )
    labels = [" ".join(w.text for w in run) for run in canvas_ocr.labels(words)]
    assert sorted(labels) == ["Berlin", "Intrusion-Set Storm-1567", "Paris"]


def test_a_title_reads_as_a_whole_label_exactly_then_normalised_then_inside_a_longer_one():
    words = _words(("Fancy", 100, 100, 50, 12), ("Bear", 156, 100, 40, 12), ("APT28", 400, 100, 50, 12))
    whole = canvas_ocr.find_title(words, "APT28")
    assert whole is not None and (whole.how, whole.whole, whole.box) == ("exact", True, (400, 100, 50, 12))
    spaced = canvas_ocr.find_title(words, "fancy  bear")
    assert spaced is not None and (spaced.how, spaced.whole, spaced.box) == ("normalised", True, (100, 100, 96, 12))
    inside = canvas_ocr.find_title(words, "Bear")
    assert inside is not None and (inside.whole, inside.box) == (False, (156, 100, 40, 12))
    # A title that is only the end of a longer label is never read as a whole one.
    assert canvas_ocr.find_title(words, "Bear", whole_only=True) is None
    assert canvas_ocr.find_title(words, "Cozy Bear") is None


def test_a_title_read_in_two_places_keeps_the_surest_and_says_how_many():
    words = [canvas_ocr.Word("Paris", 100, 100, 40, 12, 60.0), canvas_ocr.Word("Paris", 500, 300, 40, 12, 95.0)]
    found = canvas_ocr.find_title(words, "Paris")
    assert found is not None and found.box == (500, 300, 40, 12) and found.count == 2


async def test_no_ocr_engine_reads_nothing_and_says_so(monkeypatch):
    monkeypatch.setattr(canvas_ocr.shutil, "which", lambda _name: None)
    assert await canvas_ocr.read_words(b"not read", 1.0) is None
    assert not canvas_ocr.available()


# ── Highlight: what the node's highlight changed on the canvas ────────────


def _circle(shape: tuple[int, int], centre: tuple[float, float], radius: float) -> np.ndarray:
    ys, xs = np.mgrid[0 : shape[0], 0 : shape[1]]
    return (xs + 0.5 - centre[0]) ** 2 + (ys + 0.5 - centre[1]) ** 2 <= radius**2


def test_the_highlight_is_the_centroid_of_what_changed():
    before = np.full((300, 500), 20, dtype=np.uint8)
    after = before.copy()
    after[_circle(before.shape, (320.0, 140.0), 14)] = 210
    found = node_locate.highlight_of(before, after)
    assert isinstance(found, node_locate.Highlight)
    assert found.centre == pytest.approx((320.0, 140.0), abs=0.5)
    assert found.box == pytest.approx((306.0, 126.0, 28.0, 28.0), abs=1.0)
    assert 0 < found.share < 0.01


def test_the_largest_change_is_the_node_and_a_stray_one_elsewhere_is_left_out():
    before = np.zeros((300, 500), dtype=np.uint8)
    after = before.copy()
    after[_circle(before.shape, (100.0, 100.0), 16)] = 200
    after[280:284, 480:484] = 200
    found = node_locate.highlight_of(before, after)
    assert isinstance(found, node_locate.Highlight) and found.centre == pytest.approx((100.0, 100.0), abs=0.5)


def test_no_change_or_a_change_over_a_tenth_of_the_canvas_is_no_highlight():
    before = np.zeros((300, 500), dtype=np.uint8)
    assert node_locate.highlight_of(before, before.copy()) == "nothing changed on the canvas"
    panned = before.copy()
    panned[:, :100] = 200
    assert node_locate.highlight_of(before, panned) == "20% of the canvas changed, more than a node's highlight"
    faint = before.copy()
    faint[10:13, 10:13] = 200
    assert node_locate.highlight_of(before, faint, ratio=2.0) == "nothing changed on the canvas"
    assert node_locate.highlight_of(before, np.zeros((200, 500), dtype=np.uint8)) == "the canvas changed size"


def test_a_canvas_holds_still_when_its_two_shots_barely_differ():
    shot = np.zeros((100, 100), dtype=np.uint8)
    moved = shot.copy()
    moved[:20, :20] = 200
    assert node_locate.still(shot, shot.copy()) and not node_locate.still(shot, moved)
    assert not node_locate.still(None, shot)


# ── The recorder: the hook for one more release, the contact sheet ────────


class _HookPage:
    """A page that still exposes the old demo hook: it answers the node's centre."""

    async def evaluate(self, _script, args):
        return {"hook": True, "found": {"x": 400.0, "y": 300.0, "width": 40.0, "height": 40.0}}


async def test_a_page_that_still_has_the_hook_is_read_through_it_and_the_log_says_so():
    target = await pointer.locate_node(_HookPage(), type("N", (), {"label": "B"})(), viewport=(1600, 900), wait=1.0)
    assert target.point == (400.0, 300.0) and target.logged == {"node": "B"}
    assert target.located == {
        "by": "hook",
        "point": {"x": 400.0, "y": 300.0},
        "said": "located through the application's hook",
    }
    assert target.names == ("B",)


async def test_a_page_without_the_hook_reads_the_node_by_its_label(monkeypatch):
    class Page:
        async def evaluate(self, _script, _args):
            return {"hook": False}

    asked = {}

    async def locate(page, label, *, viewport, wait):
        asked.update(label=label, viewport=viewport, wait=wait)
        return pointer.Target((1.0, 2.0), {}, {"node": label}, located={"by": "ocr"})

    monkeypatch.setattr(node_locate, "locate", locate)
    target = await pointer.locate_node(Page(), type("N", (), {"label": "Paris"})(), viewport=(1600, 900), wait=3.0)
    assert target.located == {"by": "ocr"} and asked == {"label": "Paris", "viewport": (1600, 900), "wait": 3.0}


def _png(colour: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 36), (colour, colour, colour)).save(out, "PNG")
    return out.getvalue()


def test_the_failed_takes_contact_sheet_ends_on_the_canvas_the_node_was_looked_for_on(tmp_path):
    frames = []
    for i in range(12):
        name = f"f{i:06d}.jpg"
        Image.new("RGB", (64, 36), (i * 10, 0, 0)).save(tmp_path / name, "JPEG")
        frames.append((i * 0.5, name))
    (tmp_path / "step-003-canvas.png").write_bytes(_png(90))
    failed = capture.StepFailedError("x", step=3, at=5.0, label="Paris")
    failed.frames = frames
    failed.events = [{"step": i, "action": "click", "label": str(i), "t1": i * 0.4} for i in range(3)]
    failed.attached = [("step-003-canvas.png", "step 3: the canvas read for Paris")]
    picks = capture.failure_sheet_frames(failed)
    assert len(picks) <= capture.SHEET_MOST
    assert picks[-2][2] == "step 3 failed: Paris" and picks[-1] == (5.0, "step-003-canvas.png", "step 3: the canvas read for Paris")
    capture.contact_sheet(str(tmp_path), picks, out_path=str(tmp_path / "sheet.png"))
    assert (tmp_path / "sheet.png").stat().st_size > 0


def test_the_hook_is_no_longer_offered_to_the_agent_or_the_reader():
    """The tool description, the Video Production skill and the docs say how a node is found; none asks the filmed
    application for a hook."""
    said = [
        source(APP_DIR / "services" / "builtin_tools" / "video_studio.py"),
        (APP_DIR / "agents" / "seed_data" / "skill_content" / "video-production.md").read_text(encoding="utf-8"),
        (REPO_ROOT / "docs" / "docs" / "usage" / "video-studio.md").read_text(encoding="utf-8"),
    ]
    for text in said:
        assert "__xtmDemoLocate" not in text and "?demo=1" not in text
