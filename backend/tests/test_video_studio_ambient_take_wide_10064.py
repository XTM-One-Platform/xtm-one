"""An element a take proves ambient on one hold is ambient on every hold of the take (#10064, retest of #10045).

Take cc44d9b3 (OpenAEV Adversarial Exposure Overview): step 1 waits while the overview loads, and the pipeline's
endless pulse (``div.MuiBox-root.css-t4atmt``) starts once the score settled, mid-wait (6.1 s of a 3.1-8.2 s wait):
the per-hold rule (shown from the hold's first frame to its last) cannot call it ambient there. Steps 3 and 5 hold
on the same page and show it from start to end: ambient. Proven here without a browser:

* :func:`page_settle.loading_holds` strikes an element proven ambient on another hold of the take, so step 1 is no
  longer a hold on a loading page and the save raises no ``take_loading_hold``;
* a spinner (a loading signature) is never struck, even when another hold logged it ambient;
* a take whose loop shows on one hold only, starting mid-hold, is still flagged.
"""

from __future__ import annotations

from app.services.video_studio import page_settle
from app.services.video_studio.page_settle import LOOPING_MARK

PIPELINE = "div.MuiBox-root.css-t4atmt"
SPINNER = "div.spinner"


def _wait(step: int, t0: float, t1: float, spans: list[list], **extra) -> dict:
    return {"step": step, "action": "wait", "ta": t0, "t0": t0, "t1": t1, "ok": True, "loading": spans, **extra}


def _cc44d9b3(names_mid: list[str] | None = None) -> list[dict]:
    looping = [f"{PIPELINE}{LOOPING_MARK}"]
    return [
        {"step": 0, "action": "click", "ta": 2.27, "t0": 1.47, "t1": 2.37, "ok": True},
        _wait(1, 3.13, 8.2, [[6.1, 8.2, names_mid or looping]]),
        {"step": 2, "action": "hover", "ta": 10.56, "t0": 8.52, "t1": 10.56, "ok": True},
        _wait(3, 11.39, 21.47, [[11.6, 21.47, looping]]),
        {"step": 4, "action": "hover", "ta": 23.82, "t0": 21.78, "t1": 23.82, "ok": True},
        _wait(5, 24.7, 34.82, [[24.9, 34.82, looping]]),
    ]


def test_a_loop_proven_ambient_on_another_hold_clears_the_wait_it_starts_on():
    events = _cc44d9b3()
    # Alone, step 1 still reads as loading: the pulse appears mid-wait.
    assert page_settle.loading_hold(events[1]) is not None
    assert page_settle.take_ambient(events) == [PIPELINE]
    assert page_settle.loading_holds(events) == []
    assert page_settle.holds_words(events) == ""
    assert "Ambient animation (not loading): div.MuiBox-root.css-t4atmt" in page_settle.ambient_words(events)


def test_a_loop_logged_ambient_by_a_new_recording_clears_the_other_holds():
    events = _cc44d9b3()
    events[3] = _wait(3, 11.39, 21.47, [], ambient=[PIPELINE])
    events[5] = _wait(5, 24.7, 34.82, [], ambient=[PIPELINE])
    assert page_settle.loading_holds(events) == []


def test_a_spinner_is_never_struck_and_stays_flagged():
    spinner = f"{SPINNER}{LOOPING_MARK}"
    events = _cc44d9b3(names_mid=[spinner, f"{PIPELINE}{LOOPING_MARK}"])
    # Even a recording that logged the spinner ambient elsewhere does not clear it.
    events[3] = _wait(3, 11.39, 21.47, [], ambient=[PIPELINE, SPINNER])
    holds = page_settle.loading_holds(events)
    assert [hold.step for hold in holds] == [1]
    assert holds[0].shows == (spinner,)


def test_a_loop_on_one_hold_only_starting_mid_hold_is_still_flagged():
    events = _cc44d9b3()[:3]
    holds = page_settle.loading_holds(events)
    assert [hold.step for hold in holds] == [1]
    assert page_settle.take_ambient(events) == []
