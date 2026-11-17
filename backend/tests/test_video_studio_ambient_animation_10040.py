"""A page's endless decorative animation is ambient, never a hold on a loading page (#10040).

Proven here without a browser:

* :class:`page_settle.PageWatch` keeps an element looping with no loading signature, shown from the hold's first
  read on a page whose DOM stands still, out of the loading spans: logged ``ambient``, and the wait settles;
* a spinner (a loading signature), or a loop appearing mid-hold, still reads as loading;
* :func:`page_settle.loading_hold` re-judges a take recorded before the rule from its logged spans, so a save
  clears its ``take_loading_hold`` advisory with no new recording, while a real spinner's hold is still flagged.
"""

from __future__ import annotations

import numpy as np

from app.services.video_studio import page_settle
from app.services.video_studio.page_settle import LOOPING_MARK, PageRead, PageWatch

PARTICLES = "div.MuiBox-root.css-t4atmt"
PING = "span.animate-ping"
ORBIT = "div.absolute.-top-32"
SPINNER = "div.spinner"
BOX = (10.0, 10.0, 40.0, 40.0)


def _crop(phase: int) -> np.ndarray:
    crop = np.zeros((20, 20), dtype=np.int16)
    crop[:, (phase * 3) % 20] = 200
    return crop


def _watch(names: list[str], *, reads: int = 10, appear_at: dict[str, int] | None = None) -> PageWatch:
    watch = PageWatch(began=0.0)
    for i in range(reads):
        shown = [n for n in names if i >= (appear_at or {}).get(n, 0)]
        read = PageRead(t=i * 0.2, changed=-5.0, looping=tuple((n, BOX) for n in shown))
        crops = {page_settle.canvas_key(f"loop:{n}", BOX): _crop(i) for n in shown}
        watch.observe(read, crops)
    return watch


def test_a_decorative_loop_shown_from_the_start_is_ambient_and_the_page_settles():
    watch = _watch([PARTICLES, PING, ORBIT])
    assert watch.loading == []
    assert watch.ambient == [PARTICLES, PING, ORBIT]
    assert watch.settled_at(2.0) is not None
    logged = watch.logged(until_settled=False, settled=None, seconds=2.0, ended=2.0)
    assert logged == {"ambient": [PARTICLES, PING, ORBIT]}
    assert page_settle.loading_hold({"step": 1, "action": "wait", "t0": 0.0, "t1": 2.0, **logged}) is None


def test_a_spinner_or_a_loop_appearing_mid_hold_still_reads_as_loading():
    spinner = _watch([SPINNER])
    assert spinner.ambient == [] and spinner.loading and f"{SPINNER}{LOOPING_MARK}" in spinner.loading[0][2]
    assert spinner.settled_at(2.0) is None
    late = _watch([PING], appear_at={PING: 4})
    assert late.ambient == [] and f"{PING}{LOOPING_MARK}" in late.loading[0][2]


def test_an_old_take_is_re_judged_at_save_with_no_new_recording():
    # As takes cc44d9b3, 5b9f8e7b and 138987e1 logged them: decorative loops from the wait's start to its end.
    names = [f"{PARTICLES}{LOOPING_MARK}", f"{PING}{LOOPING_MARK}"]
    old = {"step": 3, "action": "wait", "t0": 10.0, "t1": 16.0, "loading": [[10.2, 13.0, names], [13.4, 16.0, names]]}
    assert page_settle.ambient_names(old) == [PARTICLES, PING]
    assert page_settle.loading_hold(old) is None
    assert page_settle.holds_words([old]) == ""
    assert "Ambient animation (not loading)" in page_settle.ambient_words([old])
    pause = {**old, "action": "pause"}
    assert page_settle.loading_hold(pause) is None


def test_a_real_spinner_in_an_old_take_is_still_flagged():
    spinner = f"{SPINNER}{LOOPING_MARK}"
    old = {
        "step": 3,
        "action": "wait",
        "t0": 10.0,
        "t1": 16.0,
        "loading": [[10.2, 16.0, [spinner, f"{PING}{LOOPING_MARK}"]]],
    }
    hold = page_settle.loading_hold(old)
    assert hold is not None and hold.shows == (spinner,)
    assert "loading page" in page_settle.holds_words([old])
    # A loop that started mid-wait has no claim to be ambient.
    late = {**old, "loading": [[13.0, 16.0, [f"{PING}{LOOPING_MARK}"]]]}
    assert page_settle.ambient_names(late) == []
    assert page_settle.loading_hold(late) is not None


def test_the_loading_signature():
    for name in (
        "div.spinner",
        "span.MuiCircularProgress-root[progressbar]",
        "span.MuiSkeleton-root",
        "div.loader",
        "div.animate-pulse",
        "div.shimmer",
    ):
        assert page_settle.loading_signature(name), name
    for name in (PARTICLES, PING, ORBIT, "div.absolute.w-7"):
        assert not page_settle.loading_signature(name), name
