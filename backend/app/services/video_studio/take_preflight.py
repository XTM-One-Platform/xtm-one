# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""What a recording script is refused for before step 1: a script longer than a recording may last, and a
step that would type a credential into the action log.

* **Its length.** A take stops at ``VIDEO_STUDIO_MAX_TAKE_SECONDS``. A script
  is played in the estimate first (:func:`estimate`): its waits and pauses,
  the timeout of an optional ``wait_for`` (it may wait all of it), each
  action's cursor travel and settle, each ``type`` step's keystrokes at the
  script's pace. One estimated past :data:`CAP_SHARE` of the cap - the rest is
  the margin for a slow page - is refused (:func:`length_refusal`), naming
  its estimate, the cap, the step that crosses it and where to split it: its
  last ``marker`` before that step. A take that still reaches the cap is kept
  up to there (``capture.record_take``), never thrown away.
* **Credentials.** The action log keeps what a ``type`` step typed. A step
  typing into a password field - its selector says so - or typing a secret
  the profile stores (its password, its TOTP secret) is refused
  (:func:`credential_refusal`): the profile signs in by itself before step 1,
  and the field's step at run time is refused too (``capture``), before a key
  is typed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.video_studio.spec import TYPING_DELAY_MS, TakeScript, TakeStep

#: Seconds an action takes besides its settle: the cursor's travel to its element and the page's answer.
ACTION_SECONDS = 0.9
#: Seconds between the screencast's start and step 1 (``capture.record_take``).
LEAD_IN_SECONDS = 0.5
#: Seconds a ``highlight`` holds when its step names none (``capture``).
HIGHLIGHT_SECONDS = 2.0
#: Seconds a ``type`` step adds around its keystrokes: the click into the field, the pause before ``submit``.
TYPE_OVERHEAD_SECONDS = 0.35
#: Seconds a ``right_click`` adds while the menu it opens shows: read as soon as it does.
MENU_SECONDS = 0.4
#: The share of the cap a script's estimate may take.
CAP_SHARE = 0.9
#: Where a profile's credentials are kept, which a refusal points to.
PROFILES_PAGE = "Tools & Integrations > Browser profiles"

#: A selector naming a password field: the input's own type, as CSS or Playwright writes it.
_PASSWORD_SELECTOR = re.compile(r"type\s*=\s*['\"]?password\b", re.IGNORECASE)
#: The stored credentials no step may type (the username is shown on a sign-in page anyway).
_SECRET_CREDENTIALS = ("password", "totp_secret")
#: The shortest stored secret compared with a step's text: a shorter one would match ordinary words.
_SHORTEST_SECRET = 4


def step_seconds(step: TakeStep, script: TakeScript) -> float:
    """Seconds *step* is expected to take when *script* plays it."""
    settle = script.settle_ms / 1000
    action = step.action
    if action in ("wait", "pause"):
        return float(step.seconds or 0.0)
    if action == "marker":
        return 0.0
    if action == "wait_for":
        return step.wait_seconds if step.optional else ACTION_SECONDS
    if action == "highlight":
        return ACTION_SECONDS + (step.seconds if step.seconds is not None else HIGHLIGHT_SECONDS)
    seconds = ACTION_SECONDS + settle
    if action == "type":
        delay = step.delay_ms if step.delay_ms is not None else TYPING_DELAY_MS[script.pace]
        seconds += len(step.text or "") * delay / 1000 + TYPE_OVERHEAD_SECONDS
    # A pointer step's rest on its target, its drag, its wheel and the menu it waits for (``capture``).
    from app.services.video_studio import pointer
    from app.services.video_studio.capture import DRAG_SECONDS, drives_pointer, scroll_seconds

    if drives_pointer(step):
        seconds += (step.hover_ms if step.hover_ms is not None else pointer.HOVER_MS if step.on_canvas else 0) / 1000
    if action == "drag":
        seconds += step.seconds if step.seconds is not None else DRAG_SECONDS
    elif action == "wheel":
        seconds += step.seconds if step.seconds is not None else scroll_seconds(step.by or 0, script.pace)
    elif action == "right_click":
        seconds += MENU_SECONDS
    return seconds


@dataclass(frozen=True)
class Estimate:
    """A script played in the estimate (:func:`estimate`): when each of its steps ends, and when it does."""

    ends: tuple[float, ...]
    seconds: float

    def crossing(self, limit: float) -> int | None:
        """The index of the first step that ends past *limit* seconds; ``None`` when none does."""
        return next((index for index, end in enumerate(self.ends) if end > limit), None)


def estimate(script: TakeScript) -> Estimate:
    """How long *script* plays, step by step, from the screencast's start to its end hold."""
    ends, clock = [], LEAD_IN_SECONDS
    for step in script.steps:
        clock += step_seconds(step, script)
        ends.append(round(clock, 3))
    return Estimate(tuple(ends), round(clock + script.end_hold_ms / 1000, 3))


def _split_point(script: TakeScript, before: int) -> str:
    """Where to split *script* so its first recording ends before step *before*: its last marker before it."""
    marker = next(
        (index for index in range(before - 1, 0, -1) if script.steps[index].action == "marker"),
        None,
    )
    if marker is not None:
        return (
            f"split it at step {marker}, its last marker before then ({script.steps[marker].label!r}): record steps 0 "
            f"to {marker - 1}, then the rest as a second recording"
        )
    return (
        f"split it before step {before} (it has no marker before then: add one where a second recording may start) "
        "and record the rest as a second recording"
    )


def length_refusal(script: TakeScript, cap_seconds: float) -> str | None:
    """Why *script* is refused before step 1 for its length against a cap of *cap_seconds*; ``None`` when it fits."""
    played = estimate(script)
    budget = cap_seconds * CAP_SHARE
    if played.seconds <= budget:
        return None
    over_cap = played.crossing(cap_seconds)
    over_budget = played.crossing(budget)
    at = over_cap if over_cap is not None else over_budget if over_budget is not None else len(script.steps) - 1
    step = script.steps[at]
    crosses = (
        f"it crosses the {cap_seconds:g} s cap at step {at} ({step.action}, ending about {played.ends[at]:.0f} s in)"
        if over_cap is not None
        else f"its step {at} ({step.action}) ends about {played.ends[at]:.0f} s in, past {budget:.0f} s"
    )
    split = _split_point(script, at)
    return (
        f"Refused before recording: this script plays about {played.seconds:.0f} s and a recording stops at "
        f"{cap_seconds:g} s (a script may take {budget:.0f} s of it, the rest kept for a slow page); {crosses}. "
        f"{split[:1].upper()}{split[1:]}."
    )


def is_password_selector(selector: str | None) -> bool:
    """Whether *selector* names a password field by its type (``input[type=password]``, ``[type="password"]``)."""
    return bool(selector and _PASSWORD_SELECTOR.search(selector))


def _secrets(credentials: dict | None) -> list[str]:
    found = []
    for key in _SECRET_CREDENTIALS:
        value = (credentials or {}).get(key)
        if isinstance(value, str) and len(value.strip()) >= _SHORTEST_SECRET:
            found.append(value.strip())
    return found


def credential_refusal(script: TakeScript, credentials: dict | None = None) -> str | None:
    """Why *script* is refused before step 1 for a credential it would type; ``None`` when it types none.

    A ``type`` step into a field its selector names a password field, or one
    whose text holds a secret the profile stores (*credentials*: its password,
    its TOTP secret).
    """
    secrets = _secrets(credentials)
    for index, step in enumerate(script.steps):
        if step.action != "type":
            continue
        if is_password_selector(step.selector):
            return refused_password(index)
        text = step.text or ""
        if any(secret in text for secret in secrets):
            return (
                f"Refused before recording: step {index} types one of the browser profile's stored credentials, and "
                "a recording keeps what its steps type in its action log. The profile signs in by itself before step "
                f"1: keep its credentials in {PROFILES_PAGE}, never in a step."
            )
    return None


def refused_password(index: int) -> str:
    """The refusal of step *index*, a ``type`` step into a password field."""
    return (
        f"Refused: step {index} types into a password field, and a recording keeps what its steps type in its action "
        f"log. Store the credentials on the browser profile ({PROFILES_PAGE}): the profile signs in by itself before "
        "step 1."
    )
