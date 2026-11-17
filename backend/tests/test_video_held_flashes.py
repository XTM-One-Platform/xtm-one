"""A frame of another state spliced into a pause is dropped, the frame before held (static_flicker in a pause window)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from app.services.video_studio import capture_rate


def _page(path: Path, *, shifted: bool) -> None:
    """A page with a bottom toolbar: *shifted* draws it darkened and moved, as the spliced frame did."""
    image = Image.new("RGB", (320, 180), (10, 15, 26))
    draw = ImageDraw.Draw(image)
    dy = 6 if shifted else 0
    fill = (60, 70, 90) if shifted else (120, 140, 170)
    draw.rectangle((20, 40 + dy, 300, 140 + dy), fill=fill)
    draw.rectangle((20, 150 + dy, 300, 170), fill=fill)
    image.save(path, format="JPEG", quality=95)


def _take(tmp_path: Path, shifted: set[int], count: int = 40, prefix: str = "f") -> list[tuple[float, str]]:
    frames = []
    for k in range(count):
        name = f"{prefix}{k:06d}.jpg"
        _page(tmp_path / name, shifted=k in shifted)
        frames.append((round(k / 30, 6), name))
    return frames


PAUSE = [{"step": 4, "action": "marker", "t0": 0.0}, {"step": 5, "action": "pause", "t0": 0.0}]


def test_flash_in_a_pause_is_held(tmp_path: Path) -> None:
    frames = _take(tmp_path, shifted={20, 21, 22}, prefix="m")
    kept, flashes = capture_rate.held_flashes(frames, PAUSE, str(tmp_path))
    names = [name for _t, name in kept]
    assert all(name not in names for _t, name in frames[20:23])
    assert len(kept) == len(frames) - 3
    assert len(flashes) == 1
    flash = flashes[0]
    assert flash["frames"] == 3
    assert flash["step"] == 5
    assert flash["stages"] == ["transition pacing"]
    assert flash["source"] == [round(20 / 30, 3), round(21 / 30, 3), round(22 / 30, 3)]
    assert "dropped" in capture_rate.flash_words(flashes)


def test_long_paint_gap_held_frames_only(tmp_path: Path) -> None:
    # A long gap with no paint: only two frames, both the held picture, then the spliced frame between them.
    frames = _take(tmp_path, shifted={1})[:3]
    frames = [(0.0, frames[0][1]), (0.2, frames[1][1]), (0.4, frames[2][1])]
    kept, flashes = capture_rate.held_flashes(frames, PAUSE, str(tmp_path))
    assert [name for _t, name in kept] == [frames[0][1], frames[2][1]]
    assert flashes and flashes[0]["stages"] == ["screencast"]


def test_change_that_stays_is_kept(tmp_path: Path) -> None:
    frames = _take(tmp_path, shifted=set(range(20, 40)))
    kept, flashes = capture_rate.held_flashes(frames, PAUSE, str(tmp_path))
    assert kept == frames
    assert flashes == []


def test_flash_outside_a_hold_is_kept(tmp_path: Path) -> None:
    # The passes by step leave a click alone. A two-frame blink over a still page is still what the render's watch
    # reads at 30 fps, so only the gate replay drops it (retest ab13b8a2 of #11237: it read every third frame of a
    # 30 fps take as the one before, and missed it).
    frames = _take(tmp_path, shifted={20, 21})
    events = [{"step": 5, "action": "click", "t0": 0.0}]
    kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path))
    assert all(f.get("test") == "gate replay" for f in flashes)
    assert [name for _t, name in frames if (_t, name) not in kept] == ["f000020.jpg", "f000021.jpg"]


def test_flash_inside_a_scroll_is_kept(tmp_path: Path) -> None:
    frames = _take(tmp_path, shifted={20, 21})
    events = [{"step": 5, "action": "pause", "t0": 0.0, "scrolls": [{"t": [0.5, 0.9]}]}]
    kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path))
    # The hold passes leave a scroll alone; the gate replay drops only the blink the render's watch reads there.
    assert all(f.get("test") == "gate replay" for f in flashes)
    assert [name for _t, name in frames if (_t, name) not in kept] == ["f000020.jpg", "f000021.jpg"]


def test_gate_samples_show_every_frame_of_a_take_on_the_grid() -> None:
    # Frame times kept to the microsecond: k/30 rounds up on every third frame. A sample time summed step by step fell
    # just short of it and showed the frame before twice, so a two-frame blink there read as one frame away, no blink:
    # the take kept it and the preview flagged static_flicker (retest ab13b8a2 of #11237, 24.63-24.67 s, two frames).
    times = [round(k / 30, 6) for k in range(9000)]
    assert capture_rate.gate_samples(times, 1.0) == list(range(9000))
    assert capture_rate.gate_phases(times, 1.0) == [0.0]
    sixty = [round(k / 60, 6) for k in range(600)]
    assert capture_rate.gate_samples(sixty, 1.0) == list(range(0, 600, 2))


def _graph(path: Path, *, dimmed: bool, drift: int = 0) -> None:
    """A graph page: *dimmed* darkens every node but the hovered one and draws its tooltip; *drift* moves the
    nodes by a few px, as a graph still settling under a wait does."""
    image = Image.new("RGB", (320, 180), (10, 15, 26))
    draw = ImageDraw.Draw(image)
    for k in range(12):
        x, y = 20 + (k % 6) * 48 + drift, 30 + (k // 6) * 70
        lit = not dimmed or k == 3
        draw.ellipse((x, y, x + 30, y + 30), fill=(200, 210, 90) if lit else (50, 55, 30))
    if dimmed:
        draw.rectangle((170, 40, 300, 100), fill=(30, 60, 110))
    image.save(path, format="JPEG", quality=95)


def test_hover_dim_flash_on_a_settling_page_is_held(tmp_path: Path) -> None:
    # A wait on a page still settling: each frame drifts 1 px; three frames of a late canvas copy show the hover
    # dim and its tooltip, then the page is back to its state (not to the very same pixels).
    frames = []
    for k in range(30):
        name = f"a{k:06d}.jpg" if k in {12, 13, 14} else f"f{k:06d}.jpg"
        _graph(tmp_path / name, dimmed=k in {12, 13, 14}, drift=k // 5)
        frames.append((round(k / 30, 6), name))
    events = [{"step": 7, "action": "wait", "t0": 0.0}]
    kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path))
    assert [name for _t, name in kept if name.startswith("a")] == []
    assert len(flashes) == 1
    assert flashes[0]["stages"] == ["canvas composite"]
    assert flashes[0]["action"] == "wait"


def test_flash_in_a_hover_hold_is_held_but_not_while_the_pointer_moves(tmp_path: Path) -> None:
    frames = _take(tmp_path, shifted={20, 21, 22})
    events = [{"step": 14, "action": "hover", "t0": 0.0}]
    kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path), cursor=[(0.0, 10, 10), (0.1, 10, 10)])
    assert len(kept) == len(frames) - 3
    assert flashes[0]["action"] == "hover"
    # The pointer moving over the flash: the passes by step leave the page answering the pointer alone, but the
    # render's watch reads those frames too (#9590, retest of #9942), so the gate replay holds the picture before it
    # and names the run with its time.
    moving = [(0.6, 10, 10), (0.65, 40, 20), (0.7, 80, 30), (0.75, 120, 40)]
    kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path), cursor=moving)
    assert {name for _t, name in frames[20:23]}.isdisjoint(name for _t, name in kept)
    assert flashes and all(f["test"] == "gate replay" for f in flashes)
    assert any(f["t"][0] <= frames[22][0] and f["t"][1] >= frames[20][0] for f in flashes)


def _tooltip(path: Path, *, x: int) -> None:
    """A graph page held under a pause with a hovered edge's tooltip at *x*: a small part of the frame (under
    :data:`capture_rate.FLASH_SHARE`), as the edge tooltip and the hover of a canvas are."""
    image = Image.new("RGB", (640, 360), (10, 15, 26))
    draw = ImageDraw.Draw(image)
    for k in range(14):
        draw.line((320, 10, 20 + k * 46, 300), fill=(90, 150, 190), width=2)
        draw.ellipse((10 + k * 46, 290, 30 + k * 46, 310), fill=(200, 210, 90))
    draw.rectangle((x, 120, x + 120, 180), fill=(30, 60, 110))
    for row in range(4):
        draw.rectangle((x + 8, 128 + row * 12, x + 100, 132 + row * 12), fill=(230, 235, 245))
    image.save(path, format="JPEG", quality=95)


def test_ab_interleave_of_two_moments_in_a_pause_is_held(tmp_path: Path) -> None:
    # Two moments of a hover alternate frame by frame in a pause (A/B/A/B: the tooltip at two places), each change
    # under FLASH_SHARE of the frame: what static_flicker reads on the render, so the take must hold one moment.
    frames = []
    for k in range(40):
        moment_b = 10 <= k < 30 and k % 2 == 1
        name = f"{'c' if moment_b else 'f'}{k:06d}.jpg"
        _tooltip(tmp_path / name, x=236 if moment_b else 200)
        frames.append((round(k / 60, 6), name))
    events = [{"step": 9, "action": "pause", "t0": 0.0}]
    kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path))
    assert [name for _t, name in kept if name.startswith("c")] == []
    assert len(kept) == 30
    assert sum(f["frames"] for f in flashes) == 10
    assert {stage for f in flashes for stage in f["stages"]} == {"canvas copy"}
    assert "dropped" in capture_rate.flash_words(flashes)


def test_small_change_that_stays_is_kept(tmp_path: Path) -> None:
    # The tooltip moving once and staying (a new hover): never a flash.
    frames = []
    for k in range(30):
        name = f"f{k:06d}.jpg"
        _tooltip(tmp_path / name, x=236 if k >= 12 else 200)
        frames.append((round(k / 60, 6), name))
    kept, flashes = capture_rate.held_flashes(frames, [{"step": 9, "action": "pause", "t0": 0.0}], str(tmp_path))
    assert kept == frames
    assert flashes == []


def test_earlier_hover_back_for_a_dozen_frames_in_a_pause_is_held(tmp_path: Path) -> None:
    # A 60 fps take: an earlier pointer's hover (the tooltip at another place) drawn back for 12 frames in a long pause,
    # then the later state again (A/B/A). Compressed six times by the scene, it is away for 2 frames of the render: the
    # static_flicker the preview of the pause before a node click read after #9888, so the take holds the later state.
    frames = []
    for k in range(60):
        earlier = 20 <= k < 32
        name = f"{'c' if earlier else 'f'}{k:06d}.jpg"
        _tooltip(tmp_path / name, x=236 if earlier else 200)
        frames.append((round(k / 60, 6), name))
    kept, flashes = capture_rate.held_flashes(frames, [{"step": 15, "action": "pause", "t0": 0.0}], str(tmp_path))
    assert [name for _t, name in kept if name.startswith("c")] == []
    assert len(kept) == 48
    assert sum(f["frames"] for f in flashes) == 12
    assert flashes[0]["test"] == "chrome flicker"


def test_change_that_stays_past_the_flicker_reach_is_kept(tmp_path: Path) -> None:
    # The same move of the tooltip back after more than FLICKER_AWAY_SECONDS: two hovers the render shows apart.
    frames = []
    away = round(capture_rate.FLICKER_AWAY_SECONDS * 60) + 6
    for k in range(20 + away + 10):
        name = f"f{k:06d}.jpg"
        _tooltip(tmp_path / name, x=236 if 20 <= k < 20 + away else 200)
        frames.append((round(k / 60, 6), name))
    kept, flashes = capture_rate.held_flashes(frames, [{"step": 15, "action": "pause", "t0": 0.0}], str(tmp_path))
    assert kept == frames
    assert flashes == []


def _zoomed(path: Path, *, scale: float) -> None:
    """A graph canvas zoomed by *scale* about its centre, a toolbar standing still over it."""
    image = Image.new("RGB", (640, 360), (10, 15, 26))
    draw = ImageDraw.Draw(image)
    for k in range(14):
        x = 320 + (20 + k * 46 - 320) * scale
        draw.line((320, 180 + (10 - 180) * scale, x, 180 + (300 - 180) * scale), fill=(90, 150, 190), width=2)
        draw.ellipse((x - 10 * scale, 180 + 110 * scale, x + 10 * scale, 180 + 130 * scale), fill=(200, 210, 90))
    draw.rectangle((0, 330, 640, 360), fill=(40, 50, 70))
    image.save(path, format="JPEG", quality=95)


def test_unzoomed_keyframe_held_inside_a_wheel_zoom_is_dropped(tmp_path: Path) -> None:
    # A 30 fps take: a wheel zoom drawn from its keyframes, an un-zoomed keyframe held between zoomed frames (A/B/A/B)
    # under a still pointer: the static_flicker the preview of a wait and wheel read while the take said no flash.
    frames = []
    for k in range(30):
        stale = k in (12, 14, 16)
        name = f"{'f' if stale else 'z01'}{k:07d}.jpg"
        _zoomed(tmp_path / name, scale=1.0 if stale else (1.0 if k < 10 else 1.3))
        frames.append((round(k / 30, 6), name))
    events = [{"step": 8, "action": "wheel", "t0": 0.0}]
    cursor = [(0.0, 320, 180)]
    kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path), cursor=cursor)
    names = {name for _t, name in kept}
    assert not names & {frames[k][1] for k in (12, 14, 16)}
    assert flashes and flashes[0]["step"] == 8
    assert flashes[0]["action"] == "wheel"
    assert "dropped" in capture_rate.flash_words(flashes)


def test_wheel_zoom_that_goes_on_is_kept(tmp_path: Path) -> None:
    # A zoom in that never comes back to an earlier scale: the page's motion, kept whole.
    frames = []
    for k in range(30):
        name = f"z01{k:07d}.jpg"
        _zoomed(tmp_path / name, scale=1.0 + 0.02 * k)
        frames.append((round(k / 30, 6), name))
    kept, flashes = capture_rate.held_flashes(
        frames, [{"step": 8, "action": "wheel", "t0": 0.0}], str(tmp_path), cursor=[(0.0, 320, 180)]
    )
    assert kept == frames
    assert flashes == []


def _glide(tmp_path: Path, moments: list[int]) -> tuple[list[tuple[float, str]], list[tuple[float, int, int]]]:
    """A pointer gliding over a graph (one px a frame): *moments* names, per frame, which hover moment the frame
    draws (the tooltip at 200 + 36 * moment), an older moment after a newer one being a stale frame composited late."""
    frames, cursor = [], []
    for k, moment in enumerate(moments):
        name = f"{'c' if k % 2 else 'f'}{k:06d}.jpg"
        _tooltip(tmp_path / name, x=200 + 36 * moment)
        t = round(k / 60, 6)
        frames.append((t, name))
        cursor.append((t, 300 + k * 3, 150))
    return frames, cursor


CLICK = [{"step": 15, "action": "click", "t0": 0.0}]


def test_stale_moment_of_a_moving_pointer_is_dropped(tmp_path: Path) -> None:
    # The pointer glides on; the page went from hover moment 0 to moment 1, then an older frame of moment 0 is
    # composited between frames of moment 1 (A/B/A/B): what static_flicker reads on the preview of a hover window.
    moments = [0] * 10 + [1, 0, 1, 0, 1, 0, 1] + [1] * 10
    frames, cursor = _glide(tmp_path, moments)
    kept, flashes = capture_rate.held_flashes(frames, CLICK, str(tmp_path), cursor=cursor)
    names = {name for _t, name in kept}
    stale = {frames[k][1] for k in (11, 13, 15)}
    assert not names & stale
    assert len(kept) == len(frames) - 3
    assert len(flashes) == 1
    assert flashes[0]["test"] == "pointer interleave"
    assert flashes[0]["frames"] == 3
    assert "dropped" in capture_rate.flash_words(flashes)


def test_monotonic_hover_of_a_moving_pointer_is_kept(tmp_path: Path) -> None:
    # The pointer crosses three nodes in turn: three hover moments, each once, never back. Kept whole.
    moments = [0] * 8 + [1] * 8 + [2] * 8
    frames, cursor = _glide(tmp_path, moments)
    kept, flashes = capture_rate.held_flashes(frames, CLICK, str(tmp_path), cursor=cursor)
    assert kept == frames
    assert flashes == []


def test_hover_entered_and_left_by_a_moving_pointer_is_kept(tmp_path: Path) -> None:
    # The pointer crosses a node: its hover moment shows for two frames and goes (A/B/B/A), the page's own answer.
    moments = [0] * 10 + [1, 1] + [0] * 10
    frames, cursor = _glide(tmp_path, moments)
    kept, flashes = capture_rate.held_flashes(frames, CLICK, str(tmp_path), cursor=cursor)
    assert kept == frames
    assert flashes == []


def test_return_under_a_still_pointer_is_kept(tmp_path: Path) -> None:
    # The pointer does not move (a click pressed and released): the page going there and back is its own.
    moments = [0] * 10 + [1, 1, 0] + [0] * 10
    frames, _cursor = _glide(tmp_path, moments)
    still = [(t, 300, 150) for t, _name in frames]
    kept, flashes = capture_rate.held_flashes(frames, CLICK, str(tmp_path), cursor=still)
    assert kept == frames
    assert flashes == []


def test_locate_probe_frames_are_held(tmp_path: Path) -> None:
    # A node located by its highlight: the recorder moved the page's list over 0.5-0.7 s, the canvas lit and dimmed in
    # turn under a still cursor (A/B/A/B). Every frame of the probe and its tail is dropped, the frame before held.
    frames = _take(tmp_path, shifted=set(), count=30)
    events = [*PAUSE, {"step": 6, "action": "click", "t0": 0.75, "probed": [0.5, 0.7]}]
    kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path))
    inside = [name for t, name in frames if 0.5 <= t <= 0.7 + capture_rate.PROBE_TAIL]
    assert inside
    assert all(name not in [n for _t, n in kept] for name in inside)
    probe = [f for f in flashes if f.get("test") == "locate probe"]
    assert len(probe) == 1
    assert probe[0]["step"] == 6
    assert probe[0]["action"] == "pause"
    assert probe[0]["frames"] == len(inside)
    assert "locate probe" in capture_rate.flash_words(flashes)


def test_no_probe_keeps_every_frame(tmp_path: Path) -> None:
    frames = _take(tmp_path, shifted=set(), count=12)
    events = [*PAUSE, {"step": 6, "action": "click", "t0": 0.3}]
    kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path))
    assert kept == frames
    assert flashes == []


def test_probe_on_the_first_frame_keeps_it(tmp_path: Path) -> None:
    # Nothing before the probe to hold: the first frame stays, the rest of the probe is dropped.
    frames = _take(tmp_path, shifted=set(), count=10)
    events = [*PAUSE, {"step": 6, "action": "click", "t0": 0.2, "probed": [0.0, 0.1]}]
    kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path))
    assert kept[0] == frames[0]
    assert [f["test"] for f in flashes] == ["locate probe"]


def test_hover_search_frames_are_held(tmp_path: Path) -> None:
    # #9590: the node's first place named an edge ('indicates'); the cursor's rest there crossed the node's edge and the
    # page lit the node's hover for three frames and unlit it, then the cursor moved on to the place naming the node.
    # Every frame of the search is dropped, the frame before held: the real hover is the first the film shows.
    frames = _take(tmp_path, shifted=set(), count=30)
    events = [*PAUSE, {"step": 16, "action": "click", "t0": 0.75, "hover_searched": [0.4, 0.6]}]
    kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path))
    inside = [name for t, name in frames if 0.4 <= t <= 0.6]
    assert inside
    assert all(name not in [n for _t, n in kept] for name in inside)
    # The frames past the search (the real hover, the click) stay.
    assert all(name in [n for _t, n in kept] for t, name in frames if t > 0.6)
    search = [f for f in flashes if f.get("test") == "hover search"]
    assert len(search) == 1 and search[0]["step"] == 16 and search[0]["frames"] == len(inside)
    assert "hover search" in capture_rate.flash_words(flashes)


def test_probe_and_hover_search_are_two_runs(tmp_path: Path) -> None:
    frames = _take(tmp_path, shifted=set(), count=30)
    events = [*PAUSE, {"step": 16, "action": "click", "t0": 0.75, "probed": [0.1, 0.2], "hover_searched": [0.5, 0.6]}]
    _kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path))
    assert [f["test"] for f in flashes] == ["locate probe", "hover search"]


def test_node_approach_flash_is_held(tmp_path: Path) -> None:
    # #9590 retest of #9973: a long pause, then a click on a node located by its highlight. Its approach dimmed the graph
    # for a few frames and undimmed it (A/B/A) before the real hover; no probe or search span covers it.
    frames = _take(tmp_path, shifted={18, 19, 20}, count=40)
    events = [*PAUSE, {"step": 16, "action": "click", "t0": 0.5, "ta": 1.2, "approached": [0.5, 1.2]}]
    kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path))
    names = [name for _t, name in kept]
    assert all(name not in names for _t, name in frames[18:21])
    assert len(kept) == len(frames) - 3
    approach = [f for f in flashes if f.get("test") == "node approach"]
    assert len(approach) == 1
    assert approach[0]["step"] == 16 and approach[0]["frames"] == 3
    assert approach[0]["source"] == [round(18 / 30, 3), round(19 / 30, 3), round(20 / 30, 3)]
    assert "node approach" in capture_rate.flash_words(flashes)


def test_node_approach_keeps_the_hover_that_stays(tmp_path: Path) -> None:
    # The node's own hover dims the graph and stays until the button: the page's answer, kept.
    frames = _take(tmp_path, shifted=set(range(25, 40)), count=40)
    events = [*PAUSE, {"step": 16, "action": "click", "t0": 0.5, "ta": 1.2, "approached": [0.5, 1.2]}]
    kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path))
    assert kept == frames
    assert flashes == []


def test_node_approach_drops_nothing_outside_its_span(tmp_path: Path) -> None:
    # The same A/B/A under a click step whose approach ended before it: never the approach's.
    frames = _take(tmp_path, shifted={30, 31}, count=40)
    events = [{"step": 16, "action": "click", "t0": 0.0, "ta": 0.5, "approached": [0.0, 0.5]}]
    _kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path))
    assert not [f for f in flashes if f.get("test") == "node approach"]
    kept, approaches = capture_rate._approach_flashes(frames, events, tmp_path, capture_rate.MOST_FLASHES)
    assert kept == frames and approaches == []


def test_point_approach_before_wheel_is_held(tmp_path: Path) -> None:
    # #10071 retest of #9984: a pause, then a wheel at a point on a graph canvas. The glide to the point dimmed the graph
    # for a few frames and undimmed it (A/B/A) before the wheel; no node was located.
    frames = _take(tmp_path, shifted={18, 19, 20}, count=40)
    events = [
        *PAUSE,
        {"step": 8, "action": "wheel", "t0": 0.5, "ta": 1.2, "approached": [0.5, 1.2], "approach": "point"},
    ]
    kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path))
    names = [name for _t, name in kept]
    assert all(name not in names for _t, name in frames[18:21])
    assert len(kept) == len(frames) - 3
    approach = [f for f in flashes if f.get("test") == "point approach"]
    assert len(approach) == 1
    assert approach[0]["step"] == 8 and approach[0]["action"] == "wheel" and approach[0]["frames"] == 3
    assert "point approach" in capture_rate.flash_words(flashes)


def test_point_approach_keeps_the_hover_that_stays(tmp_path: Path) -> None:
    # The point's own hover dims the graph and stays until the wheel: the page's answer, kept.
    frames = _take(tmp_path, shifted=set(range(25, 40)), count=40)
    events = [
        *PAUSE,
        {"step": 8, "action": "wheel", "t0": 0.5, "ta": 1.2, "approached": [0.5, 1.2], "approach": "point"},
    ]
    kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path))
    assert kept == frames
    assert flashes == []


def test_take_window_answers_the_approach_span() -> None:
    from app.services.video_studio.take_window import window_records

    events = [
        {
            "step": 16,
            "action": "click",
            "t0": 43.0,
            "ta": 45.3,
            "t1": 45.4,
            "located_in": [43.0, 44.5],
            "approached": [43.0, 45.3],
        }
    ]
    step = window_records({}, events, 43.0, 46.0)["steps"][0]
    assert step["located_in"] == [43.0, 44.5] and step["approached"] == [43.0, 45.3]


def test_take_window_answers_the_hover_search_span() -> None:
    from app.services.video_studio.take_window import window_records

    events = [{"step": 16, "action": "click", "t0": 43.0, "ta": 44.0, "t1": 44.1, "hover_searched": [43.3, 43.7]}]
    assert window_records({}, events, 43.0, 45.0)["steps"][0]["hover_searched"] == [43.3, 43.7]


def test_take_window_answers_the_probe_span() -> None:
    from app.services.video_studio.take_window import window_records

    events = [{"step": 16, "action": "click", "t0": 44.0, "ta": 44.8, "t1": 44.9, "probed": [31.6, 43.9]}]
    assert window_records({}, events, 44.0, 45.0)["steps"][0]["probed"] == [31.6, 43.9]


def test_no_flash_is_said_too() -> None:
    assert "none" in capture_rate.flash_words([])


def test_frame_stage_names() -> None:
    assert capture_rate.frame_stage("f000001.jpg") == "screencast"
    assert capture_rate.frame_stage(f"{capture_rate.COMPOSITED_PREFIX}000001.jpg") == "canvas composite"
    assert capture_rate.frame_stage("z010000001.jpg") == "canvas zoom pacing"


def _replay_hits(frames: list[tuple[float, str]], folder: Path, speed: float, phase: float = 0.0) -> int:
    """The flicker frames of the runs the render's watch reports (``static_chrome.findings``: a run of MIN_FRAMES at
    least) on *frames* played at *speed*, read from *phase*."""
    from app.services.video_studio import static_chrome

    watch = static_chrome.ChromeWatch()
    times = [t for t, _n in frames]
    for count, index in enumerate(capture_rate.gate_samples(times, speed, phase=phase)):
        watch.feed(capture_rate._flicker_grey(folder / frames[index][1]), count, {"type": "recording", "id": "s"})
    runs: list[list[list[float]]] = []
    for hit in sorted(watch.hits, key=lambda h: h[0]):
        if runs and int(hit[0]) - int(runs[-1][-1][0]) <= static_chrome.RUN_GAP:
            runs[-1].append(hit)
        else:
            runs.append([hit])
    return sum(len({int(h[0]) for h in run}) for run in runs if static_chrome.is_run(run))


def test_compressed_pause_into_a_click_flicker_is_dropped_where_the_gate_reads_it(tmp_path: Path) -> None:
    # A 60 fps take: a long pause, then a node click. A canvas composite late by a few frames brings an earlier
    # picture back (the floating button at another place) for 8 take frames every 20, across the pause's end and into
    # the click. Played compressed six times, the render reads it A/B/A/B; the earlier passes read one step at a time.
    frames = []
    for k in range(240):
        earlier = 120 <= k < 200 and (k - 120) % 20 < 8
        name = f"{'a' if earlier else 'f'}{k:06d}.jpg"
        _tooltip(tmp_path / name, x=236 if earlier else 200)
        frames.append((round(k / 60, 6), name))
    events = [{"step": 15, "action": "pause", "t0": 0.0}, {"step": 16, "action": "click", "t0": 2.6}]
    assert _replay_hits(frames, tmp_path, 6.0) >= 3
    kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path))
    # What the render's watch reads is gone at either speed; one trip away and back left alone is no alternation.
    assert _replay_hits(kept, tmp_path, 6.0) == 0
    assert _replay_hits(kept, tmp_path, 1.0) == 0
    dropped = {name for _t, name in frames} - {name for _t, name in kept}
    assert dropped and all(name.startswith("a") for name in dropped)
    # The trip into the click stays: 8 take frames away are 4 frames of the render at 30 fps, past the watch's
    # MAX_AWAY, and one sample at 6x. A replay that drifted under the frame times read it 3 frames away and dropped it.
    for speed in capture_rate.GATE_SPEEDS:
        for phase in capture_rate.gate_phases([t for t, _n in kept], speed):
            assert _replay_hits(kept, tmp_path, speed, phase) == 0
    assert sum(f["frames"] for f in flashes) == len(dropped)
    assert all(f["t"][0] < f["t"][1] for f in flashes)


def test_gate_replay_keeps_a_still_page_and_a_change_that_stays(tmp_path: Path) -> None:
    frames = []
    for k in range(180):
        name = f"f{k:06d}.jpg"
        _tooltip(tmp_path / name, x=236 if k >= 90 else 200)
        frames.append((round(k / 60, 6), name))
    events = [{"step": 3, "action": "pause", "t0": 0.0}, {"step": 4, "action": "click", "t0": 1.4}]
    kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path))
    assert kept == frames
    assert flashes == []


def test_gate_replay_leaves_a_scroll_that_moves_on_alone(tmp_path: Path) -> None:
    # A scroll moves the page on and never comes back to a picture it left: nothing for the watch, nothing dropped.
    frames = []
    for k in range(240):
        name = f"f{k:06d}.jpg"
        _tooltip(tmp_path / name, x=120 + k)
        frames.append((round(k / 60, 6), name))
    events = [{"step": 5, "action": "click", "t0": 0.0, "scrolls": [{"t": [1.9, 3.4]}]}]
    kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path))
    assert kept == frames
    assert flashes == []


def _alternating(tmp_path: Path, fps: int, start: int, stop: int, every: int, away: int) -> list[tuple[float, str]]:
    """A take at *fps* whose frames *start* to *stop* bring an earlier picture back (the tooltip at another place)
    for *away* frames every *every*: the A/B/A/B the render's watch reported in the retest of #9942."""
    frames = []
    for k in range(stop + 2 * fps):
        earlier = start <= k < stop and (k - start) % every < away
        name = f"{'z' if earlier else 'f'}{k:06d}.jpg"
        _tooltip(tmp_path / name, x=236 if earlier else 200)
        frames.append((round(k / fps, 6), name))
    return frames


def test_gate_replay_reads_a_hover_under_a_moving_pointer(tmp_path: Path) -> None:
    # Take A of the retest of #9942: 60 fps, a pause then a node hover and click; the pointer moves while the hover's
    # dim and the relationship tooltip toggle. The gate skipped the pointer's span; the render's watch did not.
    frames = _alternating(tmp_path, 60, 120, 200, 20, 8)
    events = [{"step": 15, "action": "pause", "t0": 0.0}, {"step": 16, "action": "hover", "t0": 1.9}]
    cursor = [(round(1.9 + k / 60, 4), 300 + 3 * k, 200 + k) for k in range(90)]
    assert _replay_hits(frames, tmp_path, 6.0) >= 3
    kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path), cursor=cursor)
    assert _replay_hits(kept, tmp_path, 6.0) == 0
    assert _replay_hits(kept, tmp_path, 1.0) == 0
    assert any(f.get("test") == "gate replay" for f in flashes)
    assert all(f["t"][0] < f["t"][1] for f in flashes)


def test_gate_replay_reads_a_wheel_zoom_pair(tmp_path: Path) -> None:
    # Take B of the retest of #9942: 30 fps, a canvas wheel zoom drawn from a keyframe pair; the earlier keyframe
    # comes back between two later ones. The gate skipped the wheel's span; the render's watch did not.
    frames = _alternating(tmp_path, 30, 50, 90, 6, 2)
    events = [{"step": 9, "action": "wheel", "t0": 1.5, "scrolls": [{"t": [1.6, 3.1]}]}]
    assert _replay_hits(frames, tmp_path, 1.0) >= 3
    kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path))
    assert _replay_hits(kept, tmp_path, 1.0) == 0
    assert _replay_hits(kept, tmp_path, 6.0) == 0
    dropped = {name for _t, name in frames} - {name for _t, name in kept}
    assert sum(f["frames"] for f in flashes) == len(dropped)
    assert any(f.get("test") == "gate replay" and f["action"] == "wheel" for f in flashes)


def test_gate_samples_play_the_take_at_speed() -> None:
    times = [k / 60 for k in range(61)]
    assert capture_rate.gate_samples(times, 1.0)[:4] == [0, 2, 4, 6]
    assert capture_rate.gate_samples(times, 6.0) == [0, 12, 24, 36, 48, 60]
    # Shifted by a scene's trim: the frames the first replay skipped.
    assert capture_rate.gate_samples(times, 1.0, phase=1 / 60)[:4] == [1, 3, 5, 7]


def test_gate_phases_cover_every_take_frame() -> None:
    sixty = [k / 60 for k in range(121)]
    assert len(capture_rate.gate_phases(sixty, 1.0)) == 2
    # A compressed pause stays read from the take's first frame: its phases would read a change that goes on as a
    # return (a wheel's zoom, a page settling), and its run rule spans a dozen take frames whatever the phase.
    assert capture_rate.gate_phases(sixty, 6.0) == [0.0]
    assert capture_rate.gate_phases([k / 30 for k in range(61)], 1.0) == [0.0]
    for speed in capture_rate.GATE_SPEEDS:
        shown = set()
        for phase in capture_rate.gate_phases(sixty, speed):
            shown.update(capture_rate.gate_samples(sixty, speed, phase=phase))
        if speed == 1.0:
            assert shown == set(range(len(sixty)))
    assert capture_rate.GATE_PHASES_MOST >= 2


def test_gate_replay_reads_the_frames_a_shifted_scene_shows(tmp_path: Path) -> None:
    # Take A of the retest of #9950: 60 fps, a long pause then a node click located by its highlight; the earlier
    # picture (the relationship tooltip at its other place, the hover dim toggled) comes back on the odd take frames
    # only. The replay read from the take's first frame shows the even ones and found nothing; the scene trimmed at
    # 30.88 s reads the odd ones and its preview flickered (static_flicker 0:03.70-0:03.80, 3 frames).
    times = [round(k / 60, 6) for k in range(300)]
    read = set(capture_rate.gate_samples(times, 1.0)) | set(capture_rate.gate_samples(times, 6.0))
    frames = []
    for k in range(300):
        earlier = 120 <= k < 200 and k not in read and (k - 120) % 8 < 4
        name = f"{'m' if earlier else 'f'}{k:06d}.jpg"
        _tooltip(tmp_path / name, x=236 if earlier else 200)
        frames.append((round(k / 60, 6), name))
    events = [{"step": 15, "action": "pause", "t0": 0.0}, {"step": 16, "action": "click", "t0": 1.9}]
    assert _replay_hits(frames, tmp_path, 1.0) == 0
    assert _replay_hits(frames, tmp_path, 1.0, 1 / 60) >= 3
    kept, flashes = capture_rate.held_flashes(frames, events, str(tmp_path))
    dropped = {name for _t, name in frames} - {name for _t, name in kept}
    # Dropped inside the alternating window only, and what the watch reads there is no run any more at any phase.
    assert dropped and all(120 <= int(name[1:7]) < 200 for name in dropped)
    for speed in capture_rate.GATE_SPEEDS:
        for phase in capture_rate.gate_phases([t for t, _n in kept], speed):
            assert _replay_hits(kept, tmp_path, speed, phase) == 0
    assert flashes and all(f["t"][0] < f["t"][1] for f in flashes)
    assert any("transition pacing" in f["stages"] for f in flashes)
    assert sum(f["frames"] for f in flashes) == len(dropped)


def test_zoom_never_returns_to_the_keyframe_before_once_it_drew_the_one_after() -> None:
    from app.services.video_studio.scroll_pacing import nearer_sides

    # A wheel's log jitters about the midpoint of a pair: the canvas and the boxes held over it would be drawn from
    # the keyframe before, after, before, after (a floating button at two places in turn on a 30 fps preview).
    placed = [(0, 0.1), (0, 0.4), (0, 0.55), (0, 0.48), (0, 0.6), (0, 0.49), (0, 0.9), (1, 0.2), (1, 0.7)]
    assert nearer_sides(placed) == [False, False, True, True, True, True, True, False, True]
    # A log that goes on monotonically is drawn as before: half way on, the keyframe after.
    assert nearer_sides([(0, u / 10) for u in range(11)]) == [u >= 5 for u in range(11)]


def _dimmed_graph(path: Path, *, dim: int, layer: bool = False) -> None:
    """A graph canvas: *dim* darkens its nodes and edges, *layer* draws one more box over the dimmed canvas."""
    image = Image.new("RGB", (640, 360), (10, 15, 26))
    draw = ImageDraw.Draw(image)
    level = max(0, 200 - dim)
    for k in range(24):
        x, y = 30 + (k % 8) * 72, 40 + (k // 8) * 100
        draw.ellipse((x, y, x + 40, y + 40), fill=(level, level, 90))
        draw.line((x + 20, y + 20, x + 92, y + 120), fill=(level, level, level), width=3)
    if layer:
        draw.rectangle((500, 260, 620, 340), fill=(90, 90, 160))
    image.save(path, format="JPEG", quality=95)


def test_gate_replay_drops_a_blink_whose_away_frames_differ(tmp_path: Path) -> None:
    # Take 1 of the retest of #11190 (4c4194f6): at a layout click kept as painted, the canvas is drawn dimmed, then
    # dimmed with one more layer, then full again. The flip between the two dimmed frames stopped the watch at one
    # frame away, no blink, and the blink of the whole excursion was never read: the take kept it, the preview
    # flickered. Both sampling phases of a 60 fps take.
    _dimmed_graph(tmp_path / "a.jpg", dim=0)
    _dimmed_graph(tmp_path / "b.jpg", dim=60)
    _dimmed_graph(tmp_path / "c.jpg", dim=60, layer=True)
    for shift in (0, 1):
        names = ["a.jpg"] * (60 + shift) + ["b.jpg", "b.jpg", "c.jpg", "c.jpg"] + ["a.jpg"] * 60
        frames = [(round(k / 60, 6), name) for k, name in enumerate(names)]
        kept, flashes = capture_rate.held_flashes(frames, [{"step": 6, "action": "click", "t0": 0.0}], str(tmp_path))
        assert all(name == "a.jpg" for _t, name in kept), shift
        assert any(f.get("test") == "gate replay" and f["action"] == "click" for f in flashes)
        for speed in capture_rate.GATE_SPEEDS:
            for phase in capture_rate.gate_phases([t for t, _n in kept], speed):
                assert _replay_hits(kept, tmp_path, speed, phase) == 0


def test_gate_replay_keeps_a_dim_that_stays(tmp_path: Path) -> None:
    # The same canvas dimmed, then dimmed with its layer, and staying so: no return, nothing dropped.
    _dimmed_graph(tmp_path / "a.jpg", dim=0)
    _dimmed_graph(tmp_path / "b.jpg", dim=60)
    _dimmed_graph(tmp_path / "c.jpg", dim=60, layer=True)
    names = ["a.jpg"] * 60 + ["b.jpg", "b.jpg"] + ["c.jpg"] * 60
    frames = [(round(k / 60, 6), name) for k, name in enumerate(names)]
    kept, flashes = capture_rate.held_flashes(frames, [{"step": 6, "action": "click", "t0": 0.0}], str(tmp_path))
    assert kept == frames
    assert flashes == []


def _page_limited_blink(tmp_path: Path) -> list[tuple[float, str]]:
    """A 30 fps take whose page painted off its grid (fps_fallback fps, page-limited): the canvas held, then one dimmed
    frame painted at 2.010 s and held 40 ms, then the canvas again from 2.050 s."""
    _dimmed_graph(tmp_path / "a.jpg", dim=0)
    _dimmed_graph(tmp_path / "b.jpg", dim=60)
    frames = [(round(k / 30, 6), "a.jpg") for k in range(60)]
    frames.append((2.010, "b.jpg"))
    frames += [(round(2.050 + k / 30, 6), "a.jpg") for k in range(60)]
    return frames


def test_gate_phases_follow_a_page_painted_off_the_grid(tmp_path: Path) -> None:
    # Retest ab13b8a2 of #11237: a wheel zoom's approach on a page-limited take. Read from the take's first frame, the
    # dimmed frame is one sample: no blink. A scene whose trim reads it 12 ms later shows it twice: a blink, the
    # preview's static_flicker. The phases follow the frames' own times, so one replay reads it as that scene does.
    frames = _page_limited_blink(tmp_path)
    times = [t for t, _n in frames]
    assert _replay_hits(frames, tmp_path, 1.0) == 0
    assert _replay_hits(frames, tmp_path, 1.0, 0.012) >= 2
    phases = capture_rate.gate_phases(times, 1.0)
    assert 1 < len(phases) <= capture_rate.GATE_PHASES_MOST
    assert any(_replay_hits(frames, tmp_path, 1.0, phase) >= 2 for phase in phases)


def test_gate_replay_drops_a_blink_painted_off_the_grid(tmp_path: Path) -> None:
    frames = _page_limited_blink(tmp_path)
    kept, flashes = capture_rate.held_flashes(frames, [{"step": 13, "action": "click", "t0": 0.0}], str(tmp_path))
    assert all(name == "a.jpg" for _t, name in kept)
    assert any(f.get("test") == "gate replay" for f in flashes)
    # Whatever the scene's trim, what is left reads no run.
    for k in range(12):
        assert _replay_hits(kept, tmp_path, 1.0, k / 360) == 0
