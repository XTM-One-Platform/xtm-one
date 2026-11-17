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

"""The ``wait`` primitive — an agent's timer, and the policy that bounds it.

Waiting is a base primitive of an agentic flow: an agent that delegated work
(``spawn_background_task``, a slow integration call, an assignment it triggered)
must be able to *pause* before re-checking, and a pause is not the same thing as
an action.  Two properties follow from that, and this module exists to make them
explicit rather than accidental:

* **A wait is bounded by policy, not by the tool budget.** The agentic loop's
  ``max_iterations`` bounds *reasoning and acting*; elapsed time is bounded by
  the platform's ``maximum_wait_time`` (longest single pause) and
  ``maximum_total_wait_time`` (total time one run may spend waiting), plus the
  run's own wall-clock deadline.  The loop therefore does not charge an
  iteration for a round that only waited (see ``WAIT_TOOL_NAMES``), which is
  what stops a 10-minute background task from consuming a 25-iteration budget
  in 20-second polls.
* **A wait is interruptible from both sides.** The user can cancel the whole
  run (the loop already races every tool execution against its
  ``cancel_event``) or *skip* just the pause and let the run continue — that is
  the ``wake_event`` threaded through :func:`sleep_or_wake`.  The agent wakes
  itself early by naming a condition (``until=<task ids>``), so a requested
  5-minute wait costs 40 seconds when the work finishes in 40 seconds.

Everything here is deliberately dependency-free (stdlib + :mod:`app.tools.base`)
so both the tools layer (the agentic loop) and the models layer (the
``general_assistant_config`` column defaults) can import it without a cycle.
The one thing it cannot resolve on its own is *what* the agent is waiting for:
``until`` needs a session and a user, so it arrives as the ``condition`` callback
built by ``app.services.builtin_tools.wait.make_task_condition``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.core.utils import is_valid_uuid
from app.services.run_clock import clock_sentence
from app.tools.base import ToolDefinition

logger = logging.getLogger(__name__)

# ── Tool identity ─────────────────────────────────────────────────────────
WAIT_TOOL_NAME = "wait"
CHECK_TASK_STATUS_TOOL_NAME = "check_task_status"

#: Tool calls that pause instead of acting.  A loop round whose tool calls are
#: ALL in this set does not consume an agentic-loop iteration (the iteration
#: waiver in ``agent_loop``); only ``wait`` is charged against the wait budget,
#: because ``check_task_status`` is capped at its own short, fixed budget
#: (``sub_agent._STATUS_WAIT_BUDGET``) and cannot be steered by the model.
WAIT_TOOL_NAMES = frozenset({WAIT_TOOL_NAME, CHECK_TASK_STATUS_TOOL_NAME})

#: Terminal sub-agent-task states.  Single source of truth: this module is the
#: dependency-free leaf, so ``sub_agent._STATUS_TERMINAL`` and
#: ``agent_loop._TASK_TERMINAL_STATUSES`` alias it rather than re-spelling the
#: set (a drift here would make a partial wake wake on the wrong states).  It is
#: what lets :func:`execute_wait` tell "one of several watched tasks finished"
#: from "all of them did" without importing the sub-agent service.
WAIT_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "incomplete"}
)

# ── Platform policy bounds ────────────────────────────────────────────────
#: Longest single ``wait`` call, in seconds (platform default, admin-editable
#: as ``platform_settings.maximum_wait_time``).  Five minutes covers the
#: "re-check in a few minutes" pattern that motivated the tool while keeping a
#: single pause short enough that a cancelled run feels responsive even without
#: the cancel plumbing.
DEFAULT_MAXIMUM_WAIT_TIME = 300
#: Ceiling an admin may configure for a single wait (one hour).  Also the basis
#: of the loop's per-tool timeout for ``wait`` — see :data:`WAIT_TOOL_TIMEOUT`.
MAXIMUM_WAIT_TIME_CEILING = 3600

#: Total time ONE run may spend waiting, in seconds (platform default,
#: admin-editable as ``platform_settings.maximum_total_wait_time``, Settings >
#: AI > Agent runs).
#: This is the cumulative bound that keeps an iteration-free wait from turning
#: into an unbounded loop: once it is spent, further waits are refused with an
#: explanation the model can act on.
DEFAULT_MAXIMUM_TOTAL_WAIT_TIME = 1800
#: Ceiling an admin may configure for the cumulative per-run wait (24 hours).
#: The platform value an admin sets is in turn the ceiling of every per-agent
#: and per-assignment override (:func:`resolve_wait_policy`).
MAXIMUM_TOTAL_WAIT_TIME_CEILING = 86400

#: A *clamped* grant below this is not worth the round-trip that produced it —
#: the tool refuses instead, so the model gets "there is no time left to wait"
#: rather than a 0.4-second pause that reads as a bug.  A deliberately small
#: request (``wait("1s")``) is still honoured — see :func:`grant_wait`.  Doubles
#: as the "did this round really pause?" threshold behind the loop's iteration
#: waiver.
MIN_GRANTED_WAIT_SECONDS = 2.0

#: Seconds of the run's wall-clock budget a wait must leave behind, so the work
#: *after* the pause (re-check + final answer) still fits.  Waiting until the
#: deadline would guarantee the forced-final-answer backstop.
WAIT_DEADLINE_RESERVE_SECONDS = 30.0

#: How often an ``until`` condition is re-checked while waiting.
WAIT_POLL_INTERVAL = 2.0

#: Per-tool execution timeout the agentic loop applies to ``wait``.  A wait is
#: already bounded by policy *before* it starts, so this is only a backstop
#: against a hung poll — it must sit above the configurable ceiling or a
#: legitimately long wait would be killed as a timeout.
WAIT_TOOL_TIMEOUT = MAXIMUM_WAIT_TIME_CEILING + 60

_DURATION_RE = re.compile(r"^(\d+)\s*(s|m|h|d|sec|secs|second|seconds|min|mins|minute|minutes|hour|hours|day|days)?$")
_UNIT_SECONDS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
}


def clamp_maximum_wait_time(value: int) -> int:
    """Clamp a configured ``maximum_wait_time`` into ``[1, ceiling]``.

    Shared by every server-side write path (the platform AI settings save path
    in ``api/v1/settings.py``, the agent and assignment overrides) so the API
    enforces the same bound the UI advertises, exactly like
    ``clamp_max_iterations``.
    """
    return max(1, min(int(value), MAXIMUM_WAIT_TIME_CEILING))


def clamp_maximum_total_wait_time(value: int) -> int:
    """Clamp a configured ``maximum_total_wait_time`` into ``[1, ceiling]``."""
    return max(1, min(int(value), MAXIMUM_TOTAL_WAIT_TIME_CEILING))


def parse_wait_duration(raw: object) -> int | None:
    """Parse an agent-supplied duration into whole seconds.

    Accepts the same human grammar as assignment delays (``"30s"``, ``"5m"``,
    ``"1h"``) plus bare numbers (seconds) and the spelled-out unit forms an LLM
    reaches for (``"5 minutes"``).  Returns ``None`` for anything that is not a
    positive duration — including ``"0s"`` and negatives — so the caller can
    answer with usage guidance instead of silently not waiting.

    >>> parse_wait_duration("5m")
    300
    >>> parse_wait_duration("90")
    90
    >>> parse_wait_duration("soon")
    """
    if isinstance(raw, bool):  # bool is an int subclass — never a duration
        return None
    if isinstance(raw, (int, float)):
        seconds = int(raw)
        return seconds if seconds > 0 else None
    if not isinstance(raw, str):
        return None
    match = _DURATION_RE.match(raw.strip().lower())
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2)
    seconds = value * _UNIT_SECONDS.get(unit or "s", 0)
    return seconds if seconds > 0 else None


def format_wait_duration(seconds: float) -> str:
    """Render seconds the way the tool result and the UI say it back.

    Compact and unit-mixed (``"5m"``, ``"1m 30s"``, ``"2h 5m"``) so the model
    reads its own requested duration back in the form it asked for.
    """
    total = round(max(0.0, seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        minutes, secs = divmod(total, 60)
        return f"{minutes}m" if not secs else f"{minutes}m {secs}s"
    hours, rest = divmod(total, 3600)
    minutes = rest // 60
    return f"{hours}h" if not minutes else f"{hours}h {minutes}m"


@dataclass(frozen=True)
class WaitPolicy:
    """Resolved wait bounds for one run.

    ``per_call_seconds`` caps a single ``wait``; ``total_seconds`` caps the sum
    across the run.  ``unbounded_with_until`` exempts a wait that names a
    condition (``until=``) from the total budget: the per-call cap still
    applies, only blind waits are charged.  Built by
    :func:`resolve_wait_policy` (platform settings, then the agent's and the
    assignment's overrides) so every call site resolves it the same way.
    """

    per_call_seconds: int = DEFAULT_MAXIMUM_WAIT_TIME
    total_seconds: int = DEFAULT_MAXIMUM_TOTAL_WAIT_TIME
    unbounded_with_until: bool = False

    @classmethod
    def from_config(cls, config: object | None) -> WaitPolicy:
        """Resolve from a ``PlatformSettings`` row (or ``None``).

        Duck-typed on purpose: this module stays model-free, and a ``None``
        config (fresh install, no row yet) resolves to the platform defaults
        rather than disabling waiting.
        """
        if config is None:
            return cls()
        per_call = getattr(config, "maximum_wait_time", None)
        total = getattr(config, "maximum_total_wait_time", None)
        unbounded = getattr(config, "wait_unbounded_with_until", None)
        return cls(
            unbounded_with_until=unbounded is True,
            per_call_seconds=clamp_maximum_wait_time(
                per_call if isinstance(per_call, int) else DEFAULT_MAXIMUM_WAIT_TIME
            ),
            total_seconds=clamp_maximum_total_wait_time(
                total if isinstance(total, int) else DEFAULT_MAXIMUM_TOTAL_WAIT_TIME
            ),
        )

    @classmethod
    def disabled(cls) -> WaitPolicy:
        """No waiting at all — the tool is not even offered.

        For surfaces where an anonymous caller must not be able to hold a worker
        idle: the public embed chat.  Not reachable through the admin settings
        (both values clamp to at least 1) — it is an internal call-site choice.
        """
        return cls(per_call_seconds=0, total_seconds=0)

    @property
    def enabled(self) -> bool:
        """Whether waiting is available at all under this policy."""
        return self.per_call_seconds > 0 and self.total_seconds > 0


#: Keys of a per-agent / per-assignment ``wait_policy`` override (JSON).
WAIT_POLICY_OVERRIDE_KEYS = frozenset({"per_call_seconds", "total_seconds", "unbounded_with_until"})


def normalize_wait_policy_override(raw: object) -> dict[str, int | bool] | None:
    """Validate a ``wait_policy`` override and return its canonical form.

    Accepts ``None`` / ``{}`` / ``"none"`` (no override: inherit) or an object
    with any of :data:`WAIT_POLICY_OVERRIDE_KEYS`.  Seconds must be positive
    integers (they are clamped to the platform values at resolution, not
    here, so an admin lowering the platform ceiling never invalidates a stored
    override).  Raises :class:`ValueError` with a message an API or a tool can
    return as is.
    """
    if raw is None or raw == {} or (isinstance(raw, str) and raw.strip().lower() in {"", "none", "null"}):
        return None
    if not isinstance(raw, dict):
        raise ValueError("wait_policy must be an object, or 'none' to clear it")
    unknown = sorted(set(raw) - WAIT_POLICY_OVERRIDE_KEYS)
    if unknown:
        raise ValueError(
            f"wait_policy: unknown key(s) {', '.join(unknown)}; "
            f"allowed: {', '.join(sorted(WAIT_POLICY_OVERRIDE_KEYS))}"
        )
    out: dict[str, int | bool] = {}
    for key in ("per_call_seconds", "total_seconds"):
        if raw.get(key) is None:
            continue
        seconds = parse_wait_duration(raw[key])
        if seconds is None:
            raise ValueError(f"wait_policy.{key} must be a positive duration in seconds")
        out[key] = seconds
    if raw.get("unbounded_with_until") is not None:
        flag = raw["unbounded_with_until"]
        if not isinstance(flag, bool):
            raise ValueError("wait_policy.unbounded_with_until must be true or false")
        out["unbounded_with_until"] = flag
    return out or None


def resolve_wait_policy(
    platform: WaitPolicy,
    *overrides: object,
) -> WaitPolicy:
    """Apply overrides, least specific first, on top of the platform policy.

    Resolution order is the call order: ``resolve_wait_policy(platform,
    agent.wait_policy, assignment.wait_policy)`` makes the assignment win
    over the agent, and the agent over the platform.  The platform values are
    the ceilings: an override may lower the per-call cap and the total, never
    raise them above what the administrator set (Settings > Agents and automations > Agent runs).
    ``unbounded_with_until`` is the platform's unless an override sets it.  A
    malformed stored override is ignored (logged), never fatal to a run.
    """
    per_call = platform.per_call_seconds
    total = platform.total_seconds
    unbounded = platform.unbounded_with_until
    for raw in overrides:
        try:
            override = normalize_wait_policy_override(raw)
        except ValueError:
            logger.warning("wait_policy: ignoring a malformed override %r", raw)
            continue
        if not override:
            continue
        if "per_call_seconds" in override:
            per_call = int(override["per_call_seconds"])
        if "total_seconds" in override:
            total = int(override["total_seconds"])
        if "unbounded_with_until" in override:
            unbounded = bool(override["unbounded_with_until"])
    return WaitPolicy(
        per_call_seconds=max(1, min(per_call, platform.per_call_seconds)),
        total_seconds=max(1, min(total, platform.total_seconds)),
        unbounded_with_until=unbounded,
    )


class WaitBudget:
    """Mutable per-run accounting of time already spent waiting.

    One instance per agentic run, captured by the tool closure.  Only *actual*
    elapsed wait time is charged (a wait that woke early on its ``until``
    condition costs what it really took), so an agent that delegates well is
    not penalised for the pause it asked for but did not need.
    """

    __slots__ = ("policy", "spent_seconds", "wait_count")

    def __init__(self, policy: WaitPolicy | None = None) -> None:
        self.policy = policy or WaitPolicy()
        self.spent_seconds: float = 0.0
        self.wait_count: int = 0

    def remaining_total(self) -> float:
        """Seconds of cumulative wait budget left for this run."""
        return max(0.0, float(self.policy.total_seconds) - self.spent_seconds)

    def charge(self, seconds: float, *, on_condition: bool = False) -> None:
        """Record an elapsed wait.  Never charges a negative amount.

        A wait on a named condition is not charged when the policy says
        ``unbounded_with_until``: it still counts as a wait, but only blind
        waits spend the total budget.
        """
        if not (on_condition and self.policy.unbounded_with_until):
            self.spent_seconds += max(0.0, seconds)
        self.wait_count += 1


@dataclass(frozen=True)
class WaitGrant:
    """The outcome of applying policy to a requested duration.

    ``granted`` is ``0.0`` when the wait is refused, in which case ``limit``
    names the bound that refused it so the tool result can tell the model what
    to do instead of leaving it to retry the same call.
    """

    requested: int
    granted: float
    limit: str | None = None

    @property
    def clamped(self) -> bool:
        return self.granted > 0 and self.granted < float(self.requested)

    @property
    def refused(self) -> bool:
        return self.granted <= 0


def grant_wait(
    requested_seconds: int,
    *,
    budget: WaitBudget,
    remaining_wall_clock: float | None = None,
    on_condition: bool = False,
) -> WaitGrant:
    """Apply the per-call cap, the cumulative budget and the run deadline.

    The three bounds are checked in that order and the tightest one wins; the
    name of the binding bound travels on the grant so a clamp or a refusal is
    always explainable.  ``remaining_wall_clock`` is the run's own deadline
    (``None`` = no deadline, e.g. interactive chat): a wait must leave
    :data:`WAIT_DEADLINE_RESERVE_SECONDS` behind so the re-check and the final
    answer still fit inside the run.
    """
    granted = float(requested_seconds)
    limit: str | None = None

    per_call = float(budget.policy.per_call_seconds)
    if granted > per_call:
        granted, limit = per_call, "maximum_wait_time"

    # A wait on a named condition under ``unbounded_with_until`` is not
    # charged, so the total budget does not bound it either.
    if not (on_condition and budget.policy.unbounded_with_until):
        remaining_total = budget.remaining_total()
        if granted > remaining_total:
            granted, limit = remaining_total, "maximum_total_wait_time"

    if remaining_wall_clock is not None:
        usable = remaining_wall_clock - WAIT_DEADLINE_RESERVE_SECONDS
        if granted > usable:
            granted, limit = max(0.0, usable), "run_deadline"

    # Refuse a pause the clamps ground down to nothing — but never refuse a
    # SMALL pause the agent deliberately asked for: ``wait("1s")`` is a legal
    # request and must be honoured, so the floor is the smaller of the minimum
    # and the request itself.
    if granted < min(MIN_GRANTED_WAIT_SECONDS, float(requested_seconds)):
        return WaitGrant(
            requested=requested_seconds,
            granted=0.0,
            limit=limit or "maximum_total_wait_time",
        )
    return WaitGrant(requested=requested_seconds, granted=granted, limit=limit)


async def sleep_or_wake(seconds: float, wake_event: asyncio.Event | None) -> bool:
    """Sleep for *seconds*, returning early when *wake_event* fires.

    Returns ``True`` when the event woke it (the user pressed "continue now",
    or a watched task finished), ``False`` when the full duration elapsed.  The
    ``asyncio.wait_for`` form is deliberate: a plain ``asyncio.sleep`` would
    make a skip take effect only after the timer expired, which is exactly the
    "a wait is not steerable" complaint this replaces.  Cancellation of the
    whole run needs nothing here — the loop cancels this task outright.
    """
    if seconds <= 0:
        return wake_event is not None and wake_event.is_set()
    if wake_event is None:
        await asyncio.sleep(seconds)
        return False
    if wake_event.is_set():
        return True
    try:
        await asyncio.wait_for(wake_event.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


#: Round, single-unit durations an example may use.  Single-unit because
#: :func:`parse_wait_duration` rejects the mixed ``"1m 30s"`` form that
#: :func:`format_wait_duration` would otherwise produce for an odd cap.
_EXAMPLE_LADDER = (30, 60, 120, 300)


def _example_durations(per_call_seconds: int) -> tuple[str, str]:
    """Two example durations for the prompt, both legal under *per_call_seconds*.

    Hardcoded examples are what an LLM copies verbatim: with a cap of 3 minutes
    and a fixed ``"5m"`` in the prompt, every wait was requested at 5m and
    silently clamped back to exactly 3m — the "it always waits 3 minutes"
    report.  Deriving them from the resolved cap fixes the contradiction, and
    showing *two different* sizes is what says the duration is a choice rather
    than a constant — so the pair must stay distinct across the whole
    configurable range (``clamp_maximum_wait_time`` allows 1s..1h), not only at
    the default cap.
    """
    longest = next((s for s in reversed(_EXAMPLE_LADDER) if s <= per_call_seconds), per_call_seconds)
    # Step down the ladder rather than pinning the short example at 30s, which
    # collapses both examples to one value for every cap below a minute —
    # legal, but a single example shown twice, which is the anchoring this is
    # here to avoid.  Halving is the fallback below the ladder's own floor; it
    # degenerates to an equal pair only at a 1s cap, where no distinct legal
    # pair exists at all.
    shortest = next((s for s in _EXAMPLE_LADDER if s < longest), max(1, longest // 2))
    return format_wait_duration(shortest), format_wait_duration(longest)


def build_wait_system_prompt(policy: WaitPolicy | None = None) -> str:
    """The system-prompt block that teaches every agent how to wait.

    ``wait`` is a **default primitive of every agent**, not a bindable built-in
    an owner can forget to enable: an agent that cannot pause has no way to
    control its own waiting and falls back to repeated status checks.  Because
    it is never configured, nothing else tells the model it exists — hence this
    block, injected by the loop for every run that offers the tool.

    Deliberately short, and paid for: the polling advice it replaces was trimmed
    from the ``check_task_status`` description in the same change, so the added
    prompt text is close to net-neutral (see AGENTS.md invariant 16, "system
    prompt inflation is banned").  It states the two call forms and the resolved
    caps — the facts a model needs to pick a legal duration on the first try.

    The examples are *derived* from the cap (:func:`_example_durations`) rather
    than hardcoded, and the block asks the model to size the pause: stating a
    limit says what is legal, not what is appropriate, and an example that
    contradicts the stated limit is simply copied.  Both halves of that were the
    "it always waits exactly 3 minutes" bug.
    """
    resolved = policy or WaitPolicy()
    per_call = format_wait_duration(resolved.per_call_seconds)
    total = format_wait_duration(resolved.total_seconds)
    short, long = _example_durations(resolved.per_call_seconds)
    if resolved.unbounded_with_until:
        total = f"{total} of blind waits (waits naming 'until' are not counted)"
    return (
        "[Waiting] After spawning background work, keep calling independent "
        "tools in this turn - do not pause until you are blocked. Then "
        f'wait(duration="{short}") or wait(duration="{long}", until="task-id,task-id"). '
        "The pause is real and does NOT consume your step budget. Size the "
        f"pause to the work; start short if unsure. Limits: {per_call} per "
        f"wait, {total} per run. Always pass 'until' with the task or run IDs you are "
        "waiting for - the pause ends the moment ANY of them finishes, so "
        "report what came back, relaunch it if it failed, and wait again for "
        "the rest. If you wait and call other tools in the same round, the "
        "other tools run first. The user can skip or cancel a wait at any time."
    )


def build_wait_tool_definition(policy: WaitPolicy | None = None) -> ToolDefinition:
    """The ``wait`` tool schema offered to the model.

    The description states the *resolved* caps: a model that knows the ceiling
    asks for a legal duration instead of learning it from a clamp notice, and
    the "name a condition" nudge is what makes early wake-up the common case.
    The ``duration`` examples come from the same cap-derived pair as the system
    prompt, so the two never disagree about what a legal duration looks like.
    """
    resolved = policy or WaitPolicy()
    per_call = format_wait_duration(resolved.per_call_seconds)
    total = format_wait_duration(resolved.total_seconds)
    short, long = _example_durations(resolved.per_call_seconds)
    total_clause = (
        f"one run may spend {total} in blind waits; a wait whose 'until' names "
        "tasks or runs is not charged to that total"
        if resolved.unbounded_with_until
        else f"one run may wait {total} in total"
    )
    return ToolDefinition(
        name=WAIT_TOOL_NAME,
        description=(
            "Pause before continuing — only when you are blocked on background "
            "tasks you delegated, or on work that needs time to settle. Do not "
            "wait if you still have independent tools to call; do that work "
            "first. Sibling tools in the same round run before the pause. "
            "Waiting does not consume your iteration budget, but a single wait "
            f"is capped at {per_call} and {total_clause}. "
            "Always pass 'until' with the IDs of the background tasks or runs "
            "you are waiting for: the "
            "wait ends the moment one of them finishes (not all) so you can "
            "report what came back and relaunch it if it failed, then wait "
            "again for the rest. The user can skip or cancel the wait at any "
            "time."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "duration": {
                    "type": "string",
                    "description": (
                        f"How long to pause, e.g. '{short}' or '{long}' (bare "
                        f"numbers are seconds). Clamped to {per_call}."
                    ),
                },
                "until": {
                    "type": "string",
                    "description": (
                        "Optional IDs, comma-separated: background tasks you "
                        "spawned, assignment and flow runs you started, or "
                        "Video Studio jobs you queued. The "
                        "wait ends as soon as ONE of them reaches a terminal "
                        "state (completed / failed / cancelled / incomplete) "
                        "so you can report what came back and relaunch it if "
                        "it failed, then wait again for the rest."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Short explanation of what you are waiting for, shown to the user next to the countdown."
                    ),
                },
            },
            "required": ["duration"],
        },
        source="builtin:wait",
        # Never dropped by the provider tool-count cap: a run that loses its
        # timer falls back to tight polling, which is the failure this replaces.
        priority=0,
    )


# ── Execution ─────────────────────────────────────────────────────────────
#: Resolves an ``until`` condition: given task ids, returns
#: ``(all_terminal, {task_id: status})``.  Supplied by the caller because
#: resolving it needs a session and a user, which this module must not have —
#: see ``app.services.builtin_tools.wait.make_task_condition``.
WaitCondition = Callable[[list[str]], Awaitable[tuple[bool, dict[str, str]]]]

#: Why a wait ended — carried on the result for logs, events and tests.
WAIT_REASON_ELAPSED = "elapsed"
WAIT_REASON_CONDITION = "condition"
WAIT_REASON_SKIPPED = "skipped"
WAIT_REASON_ALREADY_DONE = "already_done"
WAIT_REASON_PARTIAL = "partial"
WAIT_REASON_REFUSED = "refused"
WAIT_REASON_HANDOFF = "handoff"
WAIT_REASON_INVALID = "invalid"
WAIT_REASON_STEERED = "steered"
#: The pause was cut because this worker is stopping (a rollout, a drain):
#: the call completes, so the checkpointed turn winds down with it answered
#: and the attempt that resumes it counts it as progress.
WAIT_REASON_WORKER_STOPPING = "worker_stopping"

#: Tool result for a wait that never started because the user sent a new
#: message while the run was working (a steering message is queued for the
#: next iteration boundary).  Distinct from a mid-pause skip: the pause is
#: refused up front, before any budget is charged, so the model turns to the
#: user's new input immediately instead of sleeping a full slice on top of it.
STEERED_WAIT_MESSAGE = (
    "Wait not started: the user sent a new message while this run was "
    "working. Address the new message first - background task(s) keep "
    "running, and you can wait again afterwards if still needed."
)

_LIMIT_EXPLANATIONS = {
    "maximum_wait_time": "the platform caps a single wait at {cap} (maximum_wait_time)",
    "maximum_total_wait_time": (
        "this run has {remaining} of its total wait budget left (maximum_total_wait_time: {total})"
    ),
    "run_deadline": "the run's own time budget ends in {remaining}",
}


@dataclass(frozen=True)
class WaitResult:
    """What a ``wait`` call did, and what the model is told about it."""

    message: str
    reason: str
    elapsed: float = 0.0
    granted: float = 0.0
    requested: int = 0
    #: Set on a pause cut by the worker stopping (:data:`WAIT_REASON_WORKER_STOPPING`):
    #: what the attempt that resumes the turn needs to finish it (:func:`cut_wait_remaining`).
    cut: dict | None = None

    @property
    def paused(self) -> bool:
        """True when the run actually spent time waiting.

        The agentic loop refunds an iteration only for rounds that paused, so a
        refused or malformed wait still costs its iteration and cannot be
        spammed for free rounds.
        """
        return self.elapsed >= MIN_GRANTED_WAIT_SECONDS


async def execute_wait(
    arguments: dict[str, object] | None,
    *,
    budget: WaitBudget,
    remaining_wall_clock: float | None = None,
    skip_event: asyncio.Event | None = None,
    condition: WaitCondition | None = None,
    poll_interval: float = WAIT_POLL_INTERVAL,
    on_grant: Callable[[WaitGrant], Awaitable[None]] | None = None,
    pending_input: Callable[[], bool] | None = None,
    on_status: Callable[[dict[str, str]], Awaitable[None]] | None = None,
    stopping: Callable[[], bool] | None = None,
    carried: dict | None = None,
) -> WaitResult:
    """Run one ``wait`` call: clamp it, pause, and report what happened.

    Cancellation of the *run* is not handled here — the agentic loop races this
    coroutine against its ``cancel_event`` and cancels it outright, which is why
    the pause ends instantly rather than at the next poll.  A *skip* (the user
    choosing to continue now) is raced by :func:`sleep_or_wake`; the event is
    cleared on entry **and** on the way out, so one skip ends exactly one wait
    and never leaks into the next — including when this call returns before it
    ever pauses.

    ``pending_input`` reports whether user input is already queued for the run
    (the agentic loop passes its steering-queue check).  A wait must never
    START while a user message is waiting to be injected: the steering skip
    event is set when the message arrives, but a message that lands while the
    model is mid-generation has its skip consumed by the stray-skip guard
    below — the exact race that used to park a steered turn behind a full
    5-minute implicit-wait slice.  Checked again at every poll while
    pausing — condition waits and duration-only waits alike — so a mid-pause
    arrival ends the pause even if its skip was lost or never set (callers
    that wire a steering queue without a skip event).

    ``on_grant`` fires once the duration is settled and before the pause starts,
    carrying the *granted* seconds rather than the requested ones — the UI
    countdown has to tick down the real pause, not the number the model asked
    for and may not have been given.

    ``on_status`` receives the watched ids' statuses after every poll of the
    ``until`` condition during the pause, so the UI can show what the wait is
    watching. A failure in it never ends the pause.

    ``stopping`` reports whether this worker is about to stop (the agentic
    loop passes ``worker_shutdown.wind_down_due`` for a checkpointed turn).
    Checked at every poll slice: once true the pause ends with
    :data:`WAIT_REASON_WORKER_STOPPING`, so the call completes and is
    checkpointed instead of being cancelled when the grace runs out. A run
    whose waits are cut by rollouts then shows progress at every resume and is
    never failed at the bound of graceful resumes with no progress.

    ``carried`` is the :attr:`WaitResult.cut` of the same wait cut on an
    earlier attempt, when the resumed turn finishes it
    (:func:`continue_cut_wait`): the result reports the whole pause, from
    the cut wait's start, and a second cut keeps that start.
    """
    args = arguments or {}
    # A queued user message outranks any pause: refuse the wait before the
    # stray-skip guard below can eat the skip that message set. No budget is
    # charged — nothing was waited.
    if pending_input is not None and pending_input():
        if skip_event is not None and skip_event.is_set():
            skip_event.clear()
        return WaitResult(
            message=STEERED_WAIT_MESSAGE,
            reason=WAIT_REASON_STEERED,
        )
    # Drop a skip that arrived with no pause in flight (the user pressed
    # "continue now" while the model was still thinking, or the previous call
    # never paused at all).  The ``finally`` below only runs for calls that
    # reach the pause, so without this the three early returns underneath
    # (invalid duration, refused, already-satisfied ``until``) would leave the
    # event set and auto-skip the NEXT legitimate wait — the exact opposite of
    # this function's "one skip ends one wait" contract.
    if skip_event is not None and skip_event.is_set():
        skip_event.clear()

    requested = parse_wait_duration(args.get("duration"))
    if requested is None:
        return WaitResult(
            message=(
                f"Invalid duration {args.get('duration')!r}. Use a positive duration such as '30s', '5m' or '1h'."
            ),
            reason=WAIT_REASON_INVALID,
        )

    # A wait that names a resolvable condition is the one
    # ``unbounded_with_until`` exempts from the total budget.
    on_condition = bool(condition is not None and parse_until_task_ids(args.get("until")))
    grant = grant_wait(
        requested,
        budget=budget,
        remaining_wall_clock=remaining_wall_clock,
        on_condition=on_condition,
    )
    if grant.refused:
        until_ids = parse_until_task_ids(args.get("until"))
        if until_ids and condition is not None:
            # An ``until`` that is ALREADY satisfied must not hand off: the
            # results are ready, and exiting as "waiting_on_tasks" here would
            # kill a run whose children all finished during the last pause.
            try:
                already, done_statuses = await condition(until_ids)
            except Exception:
                already, done_statuses = False, {}
            if already:
                already_msg = (
                    "No wait needed — "
                    f"{_describe_statuses(until_ids, done_statuses)}. "
                    "Read the results and continue. " + _POST_WAIT_PROTOCOL
                )
                already_msg = await _with_extracts(
                    already_msg, condition, until_ids
                )
                return WaitResult(
                    message=already_msg,
                    reason=WAIT_REASON_ALREADY_DONE,
                    requested=requested,
                )
        if until_ids:
            # Named children are still in flight and there is no pause left
            # to wait for them: this is a hand-off, not a generic refusal
            # the model should retry. The agentic loop exits on this reason
            # instead of walking off the wait-budget cliff.
            id_list = ", ".join(until_ids)
            return WaitResult(
                message=(
                    f"Cannot wait: {_explain(grant.limit, budget)}. This run "
                    "ran out of wait budget while background task(s) were "
                    f"still running: {id_list}. Do not retry the wait. "
                    "Collect finished results with get_task_result, or report "
                    "that those tasks are still in flight."
                ),
                reason=WAIT_REASON_HANDOFF,
                requested=requested,
            )
        return WaitResult(
            message=(
                f"Cannot wait: {_explain(grant.limit, budget)}. Do not retry "
                "the wait — continue with the information you have, or report "
                "what is still pending."
            ),
            reason=WAIT_REASON_REFUSED,
            requested=requested,
        )

    task_ids, until_note = _resolve_until(args.get("until"), condition)

    # An ``until`` that is ALREADY satisfied must not pause at all — naming a
    # condition is precisely what buys the agent that shortcut.
    statuses: dict[str, str] = {}
    if task_ids and condition is not None:
        done, statuses = await condition(task_ids)
        if done:
            already_msg = (
                f"No wait needed — {_describe_statuses(task_ids, statuses)}. "
                "Read the results and continue. " + _POST_WAIT_PROTOCOL
            )
            already_msg = await _with_extracts(
                already_msg, condition, task_ids
            )
            return WaitResult(
                message=already_msg,
                reason=WAIT_REASON_ALREADY_DONE,
                requested=requested,
                granted=grant.granted,
            )
        # A watched task that finished before the pause even started must hand
        # control straight back: with several tasks in ``until`` the
        # agent should react to the one that came back — report it, relaunch it
        # if it failed — not sleep through the rest of the pause on the
        # slowest sibling. Only when at least one is still running (``done`` is
        # False), so there is genuinely more to wait for.
        finished = _finished_task_ids(task_ids, statuses)
        if finished and len(task_ids) > 1:
            partial_msg = _format_partial(task_ids, statuses, budget=budget)
            partial_msg = await _with_extracts(partial_msg, condition, finished)
            return WaitResult(
                message=partial_msg,
                reason=WAIT_REASON_PARTIAL,
                requested=requested,
                granted=grant.granted,
            )

    # Announce the settled pause (UI countdown) only now that we know it will
    # actually happen and for how long.
    if on_grant is not None:
        await on_grant(grant)

    started = time.monotonic()
    started_wall = time.time()
    reason = WAIT_REASON_ELAPSED
    try:
        if not (task_ids and condition is not None):
            if pending_input is None and stopping is None:
                if await sleep_or_wake(grant.granted, skip_event):
                    reason = WAIT_REASON_SKIPPED
            else:
                # Slice a duration-only pause too: some callers wire a
                # steering queue with NO skip event (the platform embedded
                # chat proxy, autonomous runs), so the pending-input poll is
                # the only thing that can end this pause when a message
                # lands after the entry check.
                while True:
                    remaining = grant.granted - (time.monotonic() - started)
                    if remaining <= 0:
                        break
                    if stopping is not None and stopping():
                        reason = WAIT_REASON_WORKER_STOPPING
                        break
                    if await sleep_or_wake(min(poll_interval, remaining), skip_event):
                        reason = WAIT_REASON_SKIPPED
                        break
                    if pending_input is not None and pending_input():
                        reason = WAIT_REASON_STEERED
                        break
        else:
            # Sleep in slices so the pause ends on whichever comes first: the
            # condition, a skip, pending user input, or the timer.
            while True:
                remaining = grant.granted - (time.monotonic() - started)
                if remaining <= 0:
                    break
                if stopping is not None and stopping():
                    reason = WAIT_REASON_WORKER_STOPPING
                    break
                if await sleep_or_wake(min(poll_interval, remaining), skip_event):
                    reason = WAIT_REASON_SKIPPED
                    break
                if pending_input is not None and pending_input():
                    reason = WAIT_REASON_STEERED
                    break
                done, statuses = await condition(task_ids)
                if on_status is not None:
                    try:
                        await on_status(statuses)
                    except Exception:
                        logger.debug("wait: the status hook failed", exc_info=True)
                if done:
                    reason = WAIT_REASON_CONDITION
                    break
                # First of several watched tasks reached a terminal state:
                # end the pause so the agent regains control to report it and
                # relaunch it if it failed, then wait again for the rest.
                # A single watched task never partial-wakes — for it
                # "any terminal" is "all terminal", already handled above.
                if len(task_ids) > 1 and _finished_task_ids(task_ids, statuses):
                    reason = WAIT_REASON_PARTIAL
                    break
    finally:
        elapsed = time.monotonic() - started
        budget.charge(elapsed, on_condition=bool(task_ids and condition is not None))
        # One skip ends one wait.
        if skip_event is not None and skip_event.is_set():
            skip_event.clear()

    # What the model is told the pause lasted: from the cut wait's own start
    # when this call finishes one, the worker's downtime included.
    reported = elapsed
    if carried is not None:
        reported = max(elapsed, started_wall + elapsed - _float(carried.get("started_at"), started_wall))
    cut = None
    if reason == WAIT_REASON_WORKER_STOPPING:
        cut = {
            "started_at": _float((carried or {}).get("started_at"), started_wall),
            "granted": _float((carried or {}).get("granted"), grant.granted),
            "requested": int(_float((carried or {}).get("requested"), requested)),
            "until": task_ids,
            "elapsed": _float((carried or {}).get("elapsed"), 0.0) + elapsed,
        }
    if reason == WAIT_REASON_PARTIAL:
        outcome = _format_partial(
            task_ids, statuses, budget=budget, elapsed=reported
        )
        outcome = await _with_extracts(
            outcome, condition, _finished_task_ids(task_ids, statuses)
        )
    else:
        outcome = _format_outcome(
            requested=requested,
            granted=grant.granted,
            elapsed=reported,
            clamped_by=grant.limit if grant.clamped and carried is None else None,
            reason=reason,
            task_ids=task_ids,
            statuses=statuses,
            until_note=until_note,
            budget=budget,
        )
        if reason in (WAIT_REASON_CONDITION, WAIT_REASON_ALREADY_DONE):
            outcome = await _with_extracts(outcome, condition, task_ids)
    return WaitResult(
        message=outcome,
        reason=reason,
        elapsed=elapsed,
        granted=grant.granted,
        requested=requested,
        cut=cut,
    )


def _float(value: object, default: float) -> float:
    """*value* as a float when it is a number, else *default*."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return float(default)
    return float(value)


def cut_wait_remaining(cut: dict, *, now: float, budget: WaitBudget) -> float:
    """Seconds left of a wait cut by its worker stopping, read at wall time *now*.

    The pause it was granted, less the time since it started (the worker's
    downtime counts: the work it waited on went on meanwhile). The pause
    made before the cut is charged to *budget* here, so the resumed run's
    total wait budget counts it; the remaining duration is then bounded by
    :func:`grant_wait` like any wait. Never negative.
    """
    granted = _float(cut.get("granted"), 0.0)
    started_at = _float(cut.get("started_at"), now)
    budget.charge(_float(cut.get("elapsed"), 0.0), on_condition=bool(cut.get("until")))
    return max(0.0, granted - max(0.0, now - started_at))


def cut_wait_to_continue(messages: list) -> tuple[int, Any] | None:
    """``(index, tool message)`` of the ``wait`` result a resumed turn must finish, or ``None``.

    The last tool result of *messages* when it is a ``wait`` cut by its
    worker stopping (``wait_cut`` set) and no tool call was made after it:
    the turn was handed back during that pause and its replayed rounds end
    on it. What follows it may be the resume note, or the task message a
    background task's turn appends after its replayed history. A cut on any
    other tool, or a turn that went on calling tools after the cut, is left
    as it is. Only a checkpoint's replayed rounds carry ``wait_cut``.
    """
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        role = getattr(message, "role", None)
        if role == "assistant" and getattr(message, "tool_calls", None):
            return None
        if role != "tool":
            continue
        if getattr(message, "wait_cut", None) and getattr(message, "name", None) == WAIT_TOOL_NAME:
            return index, message
        return None
    return None


def parse_until_task_ids(raw: object) -> list[str]:
    """Extract valid UUID task ids from a wait ``until`` argument.

    Accepts a comma-separated string or a sequence of strings. Invalid
    tokens are dropped; the order of valid ids is preserved.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        candidates = [str(t).strip() for t in raw if str(t).strip()]
    elif isinstance(raw, str):
        candidates = [t.strip() for t in raw.split(",") if t.strip()]
    else:
        return []
    return [t for t in candidates if is_valid_uuid(t)]


def _resolve_until(raw: object, condition: WaitCondition | None) -> tuple[list[str], str]:
    """Split and validate the ``until`` argument.

    Returns the usable task ids plus a note appended to the tool result when
    something was ignored — an ``until`` that quietly does nothing would make
    the agent trust an early wake-up that can never happen.
    """
    if not raw or not isinstance(raw, (str, list, tuple)):
        return [], ""
    if isinstance(raw, str) and not raw.strip():
        return [], ""
    if condition is None:
        return [], " (ignored 'until': task status is not available in this context)"
    valid = parse_until_task_ids(raw)
    if not valid:
        return [], f" (ignored 'until': {raw!r} contains no valid task or run ID)"
    return valid, ""


def _explain(limit: str | None, budget: WaitBudget) -> str:
    """Render the bound that clamped or refused a wait, in plain words."""
    return _LIMIT_EXPLANATIONS.get(limit or "", "no wait budget remains").format(
        cap=format_wait_duration(budget.policy.per_call_seconds),
        total=format_wait_duration(budget.policy.total_seconds),
        remaining=format_wait_duration(budget.remaining_total()),
    )


_POST_WAIT_PROTOCOL = (
    "Next: call get_task_result or collect_task_results for finished tasks "
    "(a finished run's summary is in the extract that follows), "
    "synthesise those results into the deliverable, and spawn a follow-up "
    "only if a concrete next wave of work is required. Do not end the turn "
    "with a checkpoint."
)

_POST_SKIP_PROTOCOL = (
    "Tasks may still be running. Fetch any finished results, wait again with "
    "until set to the task IDs still running, or spawn a follow-up only if a "
    "concrete next wave is required. Do not end the turn with a checkpoint."
)

_POST_PARTIAL_PROTOCOL = (
    "Report what came back to the user now - a short interim update on the "
    "finished task(s) - do not stay silent while the rest run. Read finished "
    "results with get_task_result or collect_task_results first. If a finished "
    "task FAILED or came back incomplete and the work still matters, relaunch "
    "it with spawn_background_task. Then call wait again with until set to only "
    "the task IDs still running to keep waiting for the rest. Do not end the turn "
    "with a checkpoint."
)


def _finished_task_ids(task_ids: list[str], statuses: dict[str, str]) -> list[str]:
    """The watched ids whose last observed status is terminal, order preserved."""
    return [t for t in task_ids if statuses.get(t) in WAIT_TERMINAL_STATUSES]


def _format_partial(
    task_ids: list[str],
    statuses: dict[str, str],
    *,
    budget: WaitBudget,
    elapsed: float = 0.0,
) -> str:
    """Describe a partial wake: some watched tasks finished, others still run.

    The result names which finished (with status) and which are not yet
    finished — each with its last observed status, so an id the status probe
    does not know reads as ``not found`` instead of being mislabelled as
    running — then hands the agent the report-relaunch-rewait protocol.  It reports
    *elapsed* only when the pause actually ran, so a task that had already
    finished when the wait started reads as an immediate hand-back rather than a
    zero-second pause.
    """
    finished = _finished_task_ids(task_ids, statuses)
    running = [t for t in task_ids if t not in finished]
    lines: list[str] = []
    if elapsed >= MIN_GRANTED_WAIT_SECONDS:
        lines.append(f"Waited {format_wait_duration(elapsed)}.")
    lines.append(
        "Work you were waiting for finished while the rest is still running: "
        f"{_describe_statuses(finished, statuses)}."
    )
    if running:
        lines.append(f"Not yet finished: {_describe_statuses(running, statuses)}.")
    lines.append(_POST_PARTIAL_PROTOCOL)
    remaining_total = budget.remaining_total()
    if remaining_total > 0:
        lines.append(
            f"Remaining wait budget for this run: {format_wait_duration(remaining_total)}."
        )
    # The prompt's start time is stale by the pause: hand back the live clock (#8660).
    lines.append(clock_sentence())
    return " ".join(lines)


async def _with_extracts(
    message: str,
    condition: WaitCondition | None,
    task_ids: list[str],
) -> str:
    """Append short result extracts when the condition can supply them."""
    extracts_fn = getattr(condition, "extracts", None) if condition else None
    if not callable(extracts_fn) or not task_ids:
        return message
    try:
        extracts = await extracts_fn(task_ids)
    except Exception:
        return message
    if not extracts:
        return message
    parts = [f"Extract from {tid[:12]}: {body}" for tid, body in extracts.items()]
    return message + " " + " ".join(parts)


def _describe_statuses(task_ids: list[str], statuses: dict[str, str]) -> str:
    """Render ``short_id: status`` for the watched tasks."""
    if not statuses:
        return f"{len(task_ids)} task(s) are no longer pending"
    return ", ".join(f"{tid[:12]}: {statuses.get(tid, 'not found')}" for tid in task_ids)


def _format_outcome(
    *,
    requested: int,
    granted: float,
    elapsed: float,
    clamped_by: str | None,
    reason: str,
    task_ids: list[str],
    statuses: dict[str, str],
    until_note: str,
    budget: WaitBudget,
) -> str:
    """Describe what happened, and what it means for the next step.

    The result reports *elapsed* rather than *requested* time and names the
    bound when the request was clamped: a model told "you asked for 10m, you got
    5m because of maximum_wait_time" waits again on purpose, whereas a silent
    clamp reads as a completed wait and the model moves on too early.
    """
    lines: list[str] = []
    if reason == WAIT_REASON_SKIPPED:
        lines.append(
            f"Wait ended after {format_wait_duration(elapsed)} — the user chose "
            "to continue without waiting the full duration."
        )
    elif reason == WAIT_REASON_STEERED:
        lines.append(
            f"Wait ended after {format_wait_duration(elapsed)} — the user sent "
            "a new message. Address it first; you can wait again afterwards "
            "if still needed."
        )
    elif reason == WAIT_REASON_WORKER_STOPPING:
        lines.append(
            f"Wait cut short after {format_wait_duration(elapsed)}: the server running this turn is "
            "stopping (a deployment). The turn resumes on another server; wait again there for the rest "
            "if still needed."
        )
    elif reason == WAIT_REASON_CONDITION:
        lines.append(f"Waited {format_wait_duration(elapsed)} — everything you were waiting for has finished.")
    else:
        lines.append(f"Waited {format_wait_duration(elapsed)}.")

    if clamped_by:
        lines.append(
            f"You asked for {format_wait_duration(requested)} but "
            f"{format_wait_duration(granted)} was granted: "
            f"{_explain(clamped_by, budget)}."
        )

    if task_ids:
        lines.append(_describe_statuses(task_ids, statuses) + ".")
        if reason == WAIT_REASON_ELAPSED:
            lines.append(
                "Some work is still running — read its status or result before "
                "deciding whether another wait is worth it."
            )

    remaining_total = budget.remaining_total()
    if remaining_total <= 0:
        lines.append(
            "This run has no wait budget left; do not call wait again — continue or report what is still pending."
        )
    else:
        lines.append(f"Remaining wait budget for this run: {format_wait_duration(remaining_total)}.")
    # The prompt's start time is stale by the pause: hand back the live clock (#8660).
    lines.append(clock_sentence())

    if reason == WAIT_REASON_SKIPPED:
        lines.append(_POST_SKIP_PROTOCOL)
    elif reason in (WAIT_REASON_CONDITION, WAIT_REASON_ALREADY_DONE):
        lines.append(_POST_WAIT_PROTOCOL)

    return (" ".join(lines) + until_note).strip()
