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

"""Where a chat turn's time goes before the model's first token.

The run rollup (:mod:`app.services.tracing.run_rollup`) says how long the run
took and when its first token streamed, measured from the trace's opening. The
trace opens late in the turn, after the history load, the compaction check and
the quota and scope setup, and the rollup does not split the setup itself: a
turn that shows "preparing" for two seconds could not say which stage held it.

:class:`TurnStages` is a sequence of checkpoints. Each :meth:`TurnStages.mark`
closes the stage that ran since the previous checkpoint and names it, so the
setup code gains one line per stage and is not re-indented into timers. The
first provider request of the run closes the timeline (:meth:`TurnStages.close`,
from ``langfuse_tracing.start_llm_generation``) and the first streamed output
stamps the time the user waited (:meth:`TurnStages.first_output`, from
``langfuse_tracing.mark_first_output``). ``end_trace`` writes
:meth:`TurnStages.summary` as ``rollup.turn_stages`` and lifts
``pre_generation_ms`` and ``turn_first_token_ms`` onto the rollup, where the
percentiles read them.

The web chat starts a turn's timeline when the message arrives
(:func:`begin_turn`) and hands it to ``run_agent_chat`` through a context
variable that is taken once (:func:`take_pending`): a consult or transfer that
runs a nested turn in the same task starts a timeline of its own and never
writes into its parent's. Timings only, never content; nothing here raises.

A stage in :data:`WAIT_STAGES` is a pause the turn was asked to make (a
resumed turn finishing the ``wait`` its stopping worker cut): it stays on the
timeline but is kept out of ``pre_generation_ms`` and reported as
``pre_generation_wait_ms``, so the percentiles measure preparation, not pauses.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any

#: The key a root trace context carries its timeline under.
TRACE_CTX_KEY = "local_turn_stages"

#: The stage the first provider request closes.
FIRST_REQUEST_STAGE = "loop_to_first_request"

#: The stages that are a requested pause, not preparation (see the module doc).
WAIT_STAGES = frozenset({"continued_cut_wait"})

#: Bound on the stages one timeline records: a loop that marks in a cycle cannot
#: grow the trace metadata without bound.
MAX_STAGES = 40

_clock = time.perf_counter


class TurnStages:
    """The checkpoints of one turn, in order, each with the ms it took."""

    __slots__ = ("_last", "_started", "closed", "first_output_ms", "stages", "taken")

    def __init__(self) -> None:
        self._started = _clock()
        self._last = self._started
        self.stages: list[tuple[str, float]] = []
        self.closed = False
        self.taken = False
        self.first_output_ms: float | None = None

    def mark(self, name: str) -> None:
        """Close the stage that ran since the previous checkpoint, under *name*."""
        if self.closed or len(self.stages) >= MAX_STAGES:
            return
        now = _clock()
        self.stages.append((name, round((now - self._last) * 1000.0, 1)))
        self._last = now

    def close(self, name: str = FIRST_REQUEST_STAGE) -> None:
        """Mark the last stage and stop: later checkpoints are ignored."""
        if self.closed:
            return
        self.mark(name)
        self.closed = True

    def first_output(self) -> None:
        """Stamp the first streamed output, measured from the timeline's start (first call only)."""
        if self.first_output_ms is None:
            self.first_output_ms = round((_clock() - self._started) * 1000.0, 1)

    @property
    def pre_generation_ms(self) -> float:
        """The ms from the timeline's start to its last checkpoint, pauses (:data:`WAIT_STAGES`) left out."""
        return round(sum(ms for name, ms in self.stages if name not in WAIT_STAGES), 1)

    @property
    def wait_ms(self) -> float:
        """The ms the turn spent in a requested pause before its first request."""
        return round(sum(ms for name, ms in self.stages if name in WAIT_STAGES), 1)

    def summary(self) -> dict[str, Any]:
        """The timeline as stored on the rollup: stages in order, totals, whether the first request closed it."""
        return {
            "stages": [{"stage": name, "ms": ms} for name, ms in self.stages],
            "pre_generation_ms": self.pre_generation_ms,
            "pre_generation_wait_ms": self.wait_ms,
            "turn_first_token_ms": self.first_output_ms,
            "reached_first_request": self.closed,
        }


_pending: ContextVar[TurnStages | None] = ContextVar("pending_turn_stages", default=None)


def begin_turn() -> TurnStages:
    """Start the timeline of the turn about to run in this context, for ``run_agent_chat`` to take."""
    stages = TurnStages()
    _pending.set(stages)
    return stages


def take_pending() -> TurnStages | None:
    """The timeline :func:`begin_turn` started in this context, once; ``None`` after that.

    Once across every context too: a task created before the take copies the
    context variable, and the ``taken`` flag keeps its nested turn off the
    timeline all the same.
    """
    stages = _pending.get()
    if stages is None or stages.taken:
        return None
    stages.taken = True
    _pending.set(None)
    return stages


def attach(trace_ctx: dict | None, stages: TurnStages | None) -> None:
    """Hang *stages* on a root trace context so the trace's own hooks can close and store it."""
    if isinstance(trace_ctx, dict) and stages is not None:
        trace_ctx[TRACE_CTX_KEY] = stages


def of_root(root_ctx: dict | None) -> TurnStages | None:
    """The timeline a root trace context carries, or ``None``."""
    if not isinstance(root_ctx, dict):
        return None
    stages = root_ctx.get(TRACE_CTX_KEY)
    return stages if isinstance(stages, TurnStages) else None


def of_trace(ctx: dict | None) -> TurnStages | None:
    """The timeline of the root trace *ctx* belongs to (any span under it), or ``None``."""
    current = ctx
    for _ in range(64):
        if not isinstance(current, dict):
            return None
        if "local_trace_id" in current:
            return of_root(current)
        current = current.get("parent_ctx")
    return None


def mark_current(ctx: dict | None, name: str) -> None:
    """Checkpoint *name* on the timeline of the trace *ctx* belongs to; nothing without one."""
    stages = of_trace(ctx)
    if stages is not None:
        stages.mark(name)


def rollup_fields(root_ctx: dict | None) -> dict[str, Any]:
    """The rollup fields of the timeline a root trace carries (``{}`` without one)."""
    stages = of_root(root_ctx)
    if stages is None or not stages.stages:
        return {}
    fields: dict[str, Any] = {
        "turn_stages": stages.summary(),
        "pre_generation_ms": stages.pre_generation_ms,
    }
    if stages.wait_ms:
        fields["pre_generation_wait_ms"] = stages.wait_ms
    if stages.first_output_ms is not None:
        fields["turn_first_token_ms"] = stages.first_output_ms
    return fields
