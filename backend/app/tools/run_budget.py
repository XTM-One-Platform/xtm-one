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

"""Per-run spending budget for the agentic loop.

``run_agent_loop`` historically bounded a run along three uneven axes:
``max_iterations`` (a rough proxy for how many tool calls happened),
``max_total_tokens`` (a *soft* wind-down once crossed) and
``wall_clock_deadline`` (duration).  Nothing tracked **cost**, so a burst of
expensive parallel tool-call batches — or simply an expensive model — could
blow past any intended spend before the soft token check ever fired.

This module unifies the four dimensions into one per-run object enforced with
the **reserve → call → debit → release** pattern OpenAI's "per-run spending
controller" cookbook describes:

1. Before every LLM call the loop *reserves* the call's worst-case spend
   (estimated prompt tokens + the full ``max_tokens`` output allowance,
   priced with the model's own rates).  When what is left cannot cover that
   allowance, the call is made with the largest ``max_tokens`` it can cover,
   down to ``MIN_OUTPUT_TOKENS``, and exactly that is reserved.
2. If not even that floor fits, the call is **blocked** — a hard stop, not a
   nudge.
3. After the response the loop *debits* the **actual** usage
   (``record_llm_usage``) and *releases* the reservation, so the difference
   between worst case and reality is handed back to the run.

Everything here is **provider-independent**: the budget only ever sees a
usage dict and a price row, so the same accounting applies to every LLM
backend XTM One talks to.  It is also **DB-free** — the caller resolves the
price row once (``app.services.tracing.cost_calculator.resolve_model_price``)
and hands it in, so the loop never holds a DB connection for accounting.

Three deliberate design points:

* **A closing-synthesis reserve.**  For the two dimensions that closing call
  actually consumes — cost and tokens — in-loop calls are gated against
  ``limit * (1 - final_answer_reserve_fraction)`` while the forced-final-answer
  backstop is gated against the *full* limit (``final=True``).  A run that
  exhausts its budget can therefore still pay for the one call that turns its
  work into an answer — the same trick ``_FORCED_ANSWER_DEADLINE_RESERVE_SECS``
  plays with the wall clock.  Without it, "budget exhausted" would always
  degrade to a deterministic tool-result digest.  The tool-call ceiling is
  never held back (the synthesis is tool-free, so a 50-call budget admits 50
  calls before the gate refuses; a parallel batch that crosses it still runs
  in full unless ``strict_tool_calls`` cuts it to the calls left) and
  neither is duration (which already gets a reserve in seconds from the
  loop's wall-clock machinery).
* **Cost enforcement fails open, never closed.**  When no price row is known
  for the model, the cost dimension is simply not enforced (and says so in the
  logs).  Missing pricing data must not be able to stop every run on the
  platform; the other three dimensions still apply.
* **A third outcome between "affordable" and "refused".**  Capping a
  run at $0.50 on an expensive model usually means "don't spend more than
  $0.50 *at this quality*", not "abandon the task" — and the same budget buys
  several times more work on a cheaper model.  So when a ``BudgetFallbackModel``
  is configured and the reservation that no longer fits *would* fit at the
  fallback's rates, ``can_downgrade`` says so and ``commit_downgrade`` switches
  the run's pricing to it for good (the agent loop then switches the actual
  model).  Deliberately bounded: **cost only** (tokens / tool calls / duration
  are model-independent — a cheaper model buys no extra wall clock) and **one
  downgrade per run**, never a ladder that limps along on progressively worse
  models.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.budget_policy import (
    LIMIT_SPECS as BUDGET_LIMIT_SPECS,
)
from app.services.budget_policy import (
    BudgetOverrides,
    resolve_effective,
)
from app.services.tracing.cost_calculator import (
    ModelPrice,
    calculate_costs,
    price_usage,
    reported_total_tokens,
    resolve_model_price,
    resolve_model_price_any,
    unaccounted_token_rate,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.llm.base import BaseLLMProvider

logger = logging.getLogger(__name__)

# Fraction of the cost/token limits held back for the closing synthesis call
# (see the module docstring).  10% of a sane budget comfortably covers one
# tool-free final answer while leaving 90% for the actual work.
DEFAULT_FINAL_ANSWER_RESERVE = 0.1

# Fraction of a limit at which the one-shot "budget is running low" warning
# fires.  Mirrors the existing wall-clock nudge (P4.2): telling the model to
# start consolidating BEFORE the hard stop is what turns an exhausted budget
# into a usable answer instead of an abrupt cut-off.
DEFAULT_WARN_FRACTION = 0.75

# Smallest output allowance a call is sized down to when the budget cannot
# cover its full ``max_tokens`` (``try_reserve``): room for a short answer or
# one tool call.  Below it the call is refused.
MIN_OUTPUT_TOKENS = 1024

_ZERO = Decimal(0)


class BudgetDimension(StrEnum):
    """The four dimensions a per-run budget can bound."""

    COST = "cost"
    TOKENS = "tokens"
    TOOL_CALLS = "tool_calls"
    DURATION = "duration"


@dataclass(frozen=True, slots=True)
class BudgetViolation:
    """One dimension's limit reached (or unaffordable for the next call).

    ``spent`` is what the run has actually consumed and ``projected`` adds the
    outstanding reservations plus the call being gated, so the log line can
    distinguish "already over" from "this call would take us over".

    ``limit`` is the limit as configured; ``gate`` is the threshold this
    particular check applied — the two differ for cost/tokens on in-loop calls,
    which are held to ``limit * (1 - reserve)``.  User-facing text always
    quotes ``limit``: telling someone who set $0.50 that they "reached their
    $0.45 budget" is just confusing.
    """

    dimension: BudgetDimension
    spent: float
    projected: float
    limit: float
    gate: float

    def log_detail(self) -> str:
        if self.dimension is BudgetDimension.COST:
            return (
                f"cost: ${self.spent:.6f} spent, ${self.projected:.6f} projected "
                f"(gate ${self.gate:.6f} of a ${self.limit:.6f} limit)"
            )
        if self.dimension is BudgetDimension.TOKENS:
            return (
                f"tokens: {int(self.spent):,} used, {int(self.projected):,} projected "
                f"(gate {int(self.gate):,} of a {int(self.limit):,} limit)"
            )
        if self.dimension is BudgetDimension.TOOL_CALLS:
            return f"tool calls: {int(self.spent):,} made (limit {int(self.limit):,})"
        return f"duration: {self.spent:.1f}s elapsed (limit {self.limit:.1f}s)"

    def user_message(self) -> str:
        """Short, user-facing clause naming the exhausted dimension."""
        if self.dimension is BudgetDimension.COST:
            return f"this run reached its cost budget (${self.limit:.4f})"
        if self.dimension is BudgetDimension.TOKENS:
            return f"this run reached its token budget ({int(self.limit):,} tokens)"
        if self.dimension is BudgetDimension.TOOL_CALLS:
            return f"this run reached its tool-call budget ({int(self.limit):,} calls)"
        return f"this run reached its time budget ({self.limit:.0f}s)"


@dataclass(slots=True)
class CallReservation:
    """Outcome of ``RunBudget.try_reserve`` for one LLM call.

    ``granted`` reservations hold ``cost_usd`` / ``tokens`` against the run
    until ``RunBudget.release`` hands them back; a refused reservation holds
    nothing and carries the ``violation`` that refused it.

    ``max_output_tokens`` is the output allowance the reservation covers: the
    ``max_tokens`` the call is made with, below the one asked for when the
    budget sized the call down.
    """

    id: str
    cost_usd: Decimal
    tokens: int
    max_output_tokens: int
    violation: BudgetViolation | None = None

    @property
    def granted(self) -> bool:
        return self.violation is None


@dataclass(frozen=True, slots=True)
class BudgetFallbackModel:
    """The cheaper model a cost-exhausted run continues on.

    Resolved ONCE by the caller, before the loop, from the same configuration
    surface the failover chain uses (``PlatformSettings.fallback_model`` /
    ``DEFAULT_FALLBACK_MODEL`` — see
    ``app.llm.router.resolve_configured_fallback``) so an operator has one
    fallback model to reason about, not two.

    ``price`` is what makes the downgrade *decidable*: with no price row for
    the fallback there is no way to know the same reservation is cheaper on it,
    so the run does not switch (cost enforcement fails open, never closed).

    ``provider`` is carried through untouched for the agent loop, which needs a
    provider instance to actually place calls on ``model_id``; this module
    never calls it — the budget stays provider-independent and DB-free.
    """

    model_id: str
    price: ModelPrice | None = None
    provider: BaseLLMProvider | None = None

    @property
    def usable(self) -> bool:
        """True when this fallback can carry a downgrade decision."""
        return bool(self.model_id) and self.price is not None


@dataclass(frozen=True, slots=True)
class ModelDowngrade:
    """Record of the one model downgrade a run is allowed.

    ``violation`` is the cost gate that *would* have stopped the run, kept so
    the logs, the event and the model-facing notice can all say why the switch
    happened rather than announcing an unexplained model change.
    """

    from_model: str | None
    to_model: str
    violation: BudgetViolation

    def log_detail(self) -> str:
        return (
            f"model downgraded {self.from_model or 'default'} -> {self.to_model} "
            f"({self.violation.log_detail()})"
        )


def _usage_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def count_usage_tokens(usage: dict | None) -> int:
    """Total tokens in a provider usage dict, best-effort across conventions.

    Prefers an explicit total, then sums the per-direction counters.  The key
    set matches the agent loop's own ``_accumulated_total_tokens`` so the
    budget and the loop's logging can never disagree about how much a run
    spent; ``input``/``output`` (the Langfuse-normalised shape) are consulted
    only when no raw provider key is present, so nothing is double counted.
    """
    if not usage:
        return 0

    total = reported_total_tokens(usage)
    if total:
        return total
    raw = (
        _usage_int(usage.get("input_tokens"))
        + _usage_int(usage.get("output_tokens"))
        + _usage_int(usage.get("prompt_tokens"))
        + _usage_int(usage.get("completion_tokens"))
    )
    if raw:
        return raw
    return _usage_int(usage.get("input")) + _usage_int(usage.get("output"))


def _positive_or_none(value: float | int | None) -> float | None:
    """``0`` / negative / ``None`` all mean "this dimension is disabled"."""
    if value is None:
        return None
    numeric = float(value)
    return numeric if numeric > 0 else None


class RunBudget:
    """A single run's budget across cost, tokens, tool calls and duration.

    Not thread-safe by design — one instance belongs to one agent loop, which
    is sequential.  Reservations are keyed so overlapping in-flight calls
    (a retry, a max-tokens continuation) each hold their own slice.

    ``clock`` is injectable so tests can drive elapsed time by hand.  A budget
    handed to ``run_agent_loop`` must keep the default ``time.monotonic``: the
    loop folds ``deadline()`` into its own ``time.monotonic``-based wall clock,
    so a foreign clock frame would produce a meaningless deadline.
    """

    def __init__(
        self,
        *,
        max_cost_usd: float | Decimal | None = None,
        max_total_tokens: int | None = None,
        max_tool_calls: int | None = None,
        max_duration_seconds: float | None = None,
        price: ModelPrice | None = None,
        fallback: BudgetFallbackModel | None = None,
        final_answer_reserve_fraction: float = DEFAULT_FINAL_ANSWER_RESERVE,
        warn_fraction: float = DEFAULT_WARN_FRACTION,
        clock: Callable[[], float] = time.monotonic,
        strict_tool_calls: bool = False,
    ) -> None:
        self.max_cost_usd = _positive_or_none(max_cost_usd)
        self.max_total_tokens = _positive_or_none(max_total_tokens)
        self.max_tool_calls = _positive_or_none(max_tool_calls)
        # Whether the loop cuts a parallel batch that crosses ``max_tool_calls``
        # to the calls left (``AGENT_LOOP_MAX_TOOL_CALLS_STRICT``). Off,
        # the crossing batch runs in full and only the next LLM call is refused.
        self.strict_tool_calls = bool(strict_tool_calls)
        self.max_duration_seconds = _positive_or_none(max_duration_seconds)
        self.price = price
        # Optional cheaper model this run may finish on instead of stopping
        # when the COST dimension is exhausted.  ``None`` (the
        # default) keeps the pure hard-stop behaviour.
        self.fallback = fallback
        self._downgrade: ModelDowngrade | None = None
        # Clamped so a misconfigured fraction can never invert the gate (a
        # reserve of 1.0 would block every call, including the synthesis).
        self.final_answer_reserve_fraction = min(max(float(final_answer_reserve_fraction), 0.0), 0.5)
        self.warn_fraction = min(max(float(warn_fraction), 0.0), 1.0)
        self._clock = clock
        self._started: float | None = None
        self._spent_cost: Decimal = _ZERO
        self._spent_tokens: int = 0
        self._tool_calls: int = 0
        self._reservations: dict[str, tuple[Decimal, int]] = {}
        self._warned: set[BudgetDimension] = set()
        # One log line per run for each shape of estimated tokens (see
        # ``_estimate_unaccounted_cost``).
        self._warned_total_only = False
        self._noted_unaccounted = False

    # ── Introspection ────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        """True when at least one dimension is bounded."""
        return any(
            limit is not None
            for limit in (
                self.max_cost_usd,
                self.max_total_tokens,
                self.max_tool_calls,
                self.max_duration_seconds,
            )
        )

    @property
    def enforces_cost(self) -> bool:
        """Cost is only enforceable when a limit AND a price row are known."""
        return self.max_cost_usd is not None and self.price is not None

    @property
    def downgrade(self) -> ModelDowngrade | None:
        """The downgrade this run has already taken, if any."""
        return self._downgrade

    @property
    def fallback_available(self) -> bool:
        """True when a cost downgrade is still possible for this run.

        One downgrade per run, not a ladder: a run that limps along on
        progressively worse models produces an answer nobody wants.  Cost is
        also the only dimension a downgrade can help with, so the fallback is
        only relevant while cost is actually enforced.
        """
        return (
            self._downgrade is None
            and self.fallback is not None
            and self.fallback.usable
            and self.enforces_cost
        )

    @property
    def spent_cost_usd(self) -> Decimal:
        return self._spent_cost

    @property
    def spent_tokens(self) -> int:
        return self._spent_tokens

    @property
    def tool_calls(self) -> int:
        return self._tool_calls

    @property
    def elapsed_seconds(self) -> float:
        if self._started is None:
            return 0.0
        return self._clock() - self._started

    def start(self) -> None:
        """Stamp the run's start time.  Idempotent — re-entry keeps the first."""
        if self._started is None:
            self._started = self._clock()

    def deadline(self) -> float | None:
        """Duration limit as an absolute value in this budget's clock frame.

        ``None`` when no duration limit is set (or ``start()`` has not run).
        The loop folds this into its existing ``wall_clock_deadline`` — one
        enforcement path for duration, whichever bound is tighter.
        """
        if self.max_duration_seconds is None or self._started is None:
            return None
        return self._started + self.max_duration_seconds

    def describe_limits(self) -> str:
        parts: list[str] = []
        if self.max_cost_usd is not None:
            suffix = "" if self.price is not None else " (NOT enforced — no price row for this model)"
            parts.append(f"max_cost=${self.max_cost_usd:.4f}{suffix}")
        if self.max_total_tokens is not None:
            parts.append(f"max_tokens={int(self.max_total_tokens):,}")
        if self.max_tool_calls is not None:
            parts.append(f"max_tool_calls={int(self.max_tool_calls):,}")
        if self.max_duration_seconds is not None:
            parts.append(f"max_duration={self.max_duration_seconds:.0f}s")
        if not parts:
            return "disabled"
        suffix = (
            f" (cost/token in-loop gate at {100 * (1 - self.final_answer_reserve_fraction):.0f}%"
            " so the closing synthesis stays affordable)"
        )
        if self.fallback is not None and self.fallback.usable:
            suffix += (
                f", downgrades to {self.fallback.model_id} once the cost gate "
                "is reached instead of stopping"
            )
        return ", ".join(parts) + suffix

    def snapshot(self) -> dict[str, float | int | str | None]:
        """Flat dict for logs / trace metadata."""
        return {
            "cost_usd": float(self._spent_cost),
            "max_cost_usd": self.max_cost_usd,
            "tokens": self._spent_tokens,
            "max_total_tokens": (None if self.max_total_tokens is None else int(self.max_total_tokens)),
            "tool_calls": self._tool_calls,
            "max_tool_calls": (None if self.max_tool_calls is None else int(self.max_tool_calls)),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "max_duration_seconds": self.max_duration_seconds,
            "cost_enforced": self.enforces_cost,
            "fallback_model": (
                self.fallback.model_id if self.fallback is not None else None
            ),
            "downgraded_to": (
                self._downgrade.to_model if self._downgrade is not None else None
            ),
        }

    # ── Cost estimation ──────────────────────────────────────────────────

    def estimate_call_cost(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        price: ModelPrice | None = None,
    ) -> Decimal:
        """Worst-case USD cost of one call, via the shared pricing formula.

        Returns ``0`` when no price row is known — the caller then simply has
        nothing to enforce (see the fail-open note in the module docstring).

        ``price`` overrides the run's current price row, which is how the same
        reservation gets re-priced at the fallback model's rates before the
        downgrade decision.
        """
        effective = price if price is not None else self.price
        if effective is None:
            return _ZERO
        costs = calculate_costs(
            {
                "input_tokens": max(0, int(input_tokens)),
                "output_tokens": max(0, int(output_tokens)),
            },
            effective,
        )
        return costs.get("total_cost") or _ZERO

    # ── reserve → call → debit → release ─────────────────────────────────

    def try_reserve(
        self,
        *,
        estimated_input_tokens: int,
        max_output_tokens: int,
        final: bool = False,
        min_output_tokens: int | None = None,
    ) -> CallReservation:
        """Reserve one LLM call's worst-case spend, or refuse it.

        ``final=True`` gates against the **full** limit instead of the
        in-loop gate, so the closing synthesis can spend the held-back
        reserve.  A refused reservation holds nothing: the caller reads
        ``violation`` and stops.

        ``min_output_tokens`` sizes a call the budget cannot cover in full:
        the reservation covers the largest allowance that still fits
        (:meth:`affordable_output_tokens`), which the caller makes the call
        with.  Only a call that cannot fit even that floor is refused, with
        the violation of its full allowance.
        """
        tokens = max(0, int(estimated_input_tokens)) + max(0, int(max_output_tokens))
        cost = self.estimate_call_cost(
            input_tokens=estimated_input_tokens, output_tokens=max_output_tokens
        )
        violation = self._first_violation(
            projected_cost=cost, projected_tokens=tokens, final=final
        )
        output_tokens = max(0, int(max_output_tokens))
        if violation is not None and min_output_tokens is not None:
            fitted = self.affordable_output_tokens(
                estimated_input_tokens=estimated_input_tokens,
                max_output_tokens=max_output_tokens,
                min_output_tokens=min_output_tokens,
                final=final,
            )
            if fitted is not None:
                violation = None
                output_tokens = fitted
                tokens = max(0, int(estimated_input_tokens)) + fitted
                cost = self.estimate_call_cost(
                    input_tokens=estimated_input_tokens, output_tokens=fitted
                )
        reservation = CallReservation(
            id=uuid.uuid4().hex[:12],
            cost_usd=cost,
            tokens=tokens,
            max_output_tokens=output_tokens,
            violation=violation,
        )
        if violation is None:
            self._reservations[reservation.id] = (cost, tokens)
        return reservation

    def affordable_output_tokens(
        self,
        *,
        estimated_input_tokens: int,
        max_output_tokens: int,
        min_output_tokens: int = MIN_OUTPUT_TOKENS,
        final: bool = False,
    ) -> int | None:
        """The largest output allowance, up to ``max_output_tokens``, one call
        of this prompt still fits in every dimension, or ``None`` when not even
        ``min_output_tokens`` does.  Reserves nothing.

        The floor never exceeds ``max_output_tokens``: a call is never sized
        above the allowance it asked for.
        """

        def fits(output_tokens: int) -> bool:
            return (
                self.can_afford(
                    estimated_input_tokens=estimated_input_tokens,
                    max_output_tokens=output_tokens,
                    final=final,
                )
                is None
            )

        ceiling = max(0, int(max_output_tokens))
        low = min(max(0, int(min_output_tokens)), ceiling)
        if not fits(low):
            return None
        # A call's projected cost and tokens grow with its allowance, so what
        # fits is everything up to one threshold: bisect for it.
        high = ceiling
        while low < high:
            mid = (low + high + 1) // 2
            if fits(mid):
                low = mid
            else:
                high = mid - 1
        return low

    def release(self, reservation: CallReservation | None) -> None:
        """Hand a granted reservation back (the actual spend was debited)."""
        if reservation is not None:
            self._reservations.pop(reservation.id, None)

    def record_llm_usage(self, usage: dict | None) -> None:
        """Debit one call's **actual** usage: tokens always, cost when priced.

        Priced by ``cost_calculator.price_usage``, the rule the trace store
        prices with too: a reported total above what the input / output
        split prices is debited as well, never as $0 - see
        :meth:`_note_unaccounted_tokens`.
        """
        tokens = count_usage_tokens(usage)
        if tokens:
            self._spent_tokens += tokens
        costs = price_usage(usage, self.price) if usage else None
        if costs is not None:
            self._spent_cost += costs.total_cost
            if costs.estimated_tokens > 0:
                self._note_unaccounted_tokens(
                    costs.estimated_tokens, total_only=costs.priced_tokens <= 0
                )

    def _note_unaccounted_tokens(self, tokens: int, *, total_only: bool) -> None:
        """Log, once per run, that tokens only the total accounts for were priced.

        Two shapes reach this: a server that fills in nothing but the
        total (``{prompt: 0, completion: 0, total: N}``), and xAI, whose total
        carries the reasoning tokens its ``completion_tokens`` leaves out. Both
        are priced at the HIGHER rate (``unaccounted_token_rate``). The first
        shape makes the whole call an estimate and WARNS once per run; the
        second is how xAI reports every reasoning call - billed at its output
        rate, the higher one - so it is noted once per run at INFO, nothing for
        an operator to fix.
        """
        price = self.price
        if price is None:
            return
        rate = unaccounted_token_rate(price)
        if total_only and not self._warned_total_only:
            self._warned_total_only = True
            logger.warning(
                "Per-run cost budget: model %s reported %d token(s) as a total "
                "with no input/output split - debiting it at the higher rate "
                "($%s per 1M tokens). Further total-only calls of this run are "
                "estimated the same way.",
                price.model_id,
                tokens,
                rate,
            )
        elif not total_only and not self._noted_unaccounted:
            self._noted_unaccounted = True
            logger.info(
                "Per-run cost budget: model %s reported %d token(s) in its total "
                "beyond its input/output counts (e.g. reasoning tokens left out "
                "of the completion count) - debiting them at the higher rate "
                "($%s per 1M tokens).",
                price.model_id,
                tokens,
                rate,
            )

    def record_tool_calls(self, count: int) -> None:
        """Debit tool calls actually dispatched this iteration."""
        if count > 0:
            self._tool_calls += count

    def remaining_tool_calls(self) -> int | None:
        """Tool calls this run may still dispatch; ``None`` when unbounded.

        With ``strict_tool_calls`` the loop cuts a parallel batch to this:
        the next-iteration gate alone lets one response fan out
        without bound; without it, a batch past this is the overshoot the loop
        logs and traces. Rounded up, so it is at least 1 exactly when the gate
        still admits a call.
        """
        if self.max_tool_calls is None:
            return None
        return max(0, math.ceil(self.max_tool_calls - self._tool_calls))

    # ── Gates ────────────────────────────────────────────────────────────

    def check(self, *, final: bool = False) -> BudgetViolation | None:
        """Violation for what has ALREADY been spent (nothing projected)."""
        return self._first_violation(projected_cost=_ZERO, projected_tokens=0, final=final)

    def can_afford(
        self,
        *,
        estimated_input_tokens: int,
        max_output_tokens: int,
        final: bool = False,
    ) -> BudgetViolation | None:
        """Would one call of this size fit?  Reserves nothing.

        For the *last* call of a run there is no later call to protect a
        reservation from, so the caller only needs the verdict — holding a
        reservation it would never release just leaves the budget looking
        more spent than it is.
        """
        return self._first_violation(
            projected_cost=self.estimate_call_cost(
                input_tokens=estimated_input_tokens, output_tokens=max_output_tokens
            ),
            projected_tokens=max(0, int(estimated_input_tokens)) + max(0, int(max_output_tokens)),
            final=final,
        )

    # ── The third outcome: degrade instead of refuse ─────────────

    def can_downgrade(
        self,
        violation: BudgetViolation,
        *,
        estimated_input_tokens: int,
        max_output_tokens: int,
        final: bool = False,
    ) -> bool:
        """Would this refused call fit at the **fallback model's** rates?

        The third outcome of the budget gate, between "affordable" and
        "refused": the run cannot pay for this call at the current model's
        rates, but the same reservation re-priced with the fallback's
        ``ModelPrice`` does fit — so downgrading and continuing is strictly
        better than abandoning the task mid-way.

        Only ``COST`` can degrade.  Tokens, tool calls and duration are
        model-independent: a cheaper model buys no extra wall-clock and no
        extra tool calls, so those must keep hard-stopping.

        ``max_output_tokens`` is the fallback's own output allowance (which the
        caller re-derives for the fallback model), not the primary's — pricing
        the decision with the primary's allowance would answer a question
        nobody is asking.

        Reserves nothing and commits nothing: the caller may still refuse the
        switch for reasons this module cannot see (a fallback whose context
        window cannot hold the transcript, for instance).  Commit with
        :meth:`commit_downgrade` once the switch is actually going to happen.
        """
        if violation.dimension is not BudgetDimension.COST:
            return False
        if not self.fallback_available or self.fallback is None:
            return False
        repriced = self.estimate_call_cost(
            input_tokens=estimated_input_tokens,
            output_tokens=max_output_tokens,
            price=self.fallback.price,
        )
        return (
            self._first_violation(
                projected_cost=repriced,
                projected_tokens=max(0, int(estimated_input_tokens))
                + max(0, int(max_output_tokens)),
                final=final,
            )
            is None
        )

    def commit_downgrade(
        self, violation: BudgetViolation, *, from_model: str | None = None
    ) -> ModelDowngrade:
        """Switch the run to the fallback model for good.

        **Re-prices the rest of the run**: every later reservation and every
        later debit uses the fallback's rates.  Without this the accounting
        would silently misprice every remaining call — a budget that appears
        respected but is not, which is the one failure mode worse than
        stopping.

        Raises ``RuntimeError`` when no downgrade is available, so a caller
        that skipped :meth:`can_downgrade` fails loudly instead of quietly
        continuing on the expensive model.
        """
        if not self.fallback_available or self.fallback is None:
            raise RuntimeError(
                "commit_downgrade() called with no usable fallback model "
                "(check RunBudget.fallback_available first)"
            )
        self._downgrade = ModelDowngrade(
            from_model=from_model
            or (self.price.model_id if self.price is not None else None),
            to_model=self.fallback.model_id,
            violation=violation,
        )
        self.price = self.fallback.price
        # The cost dimension is marked as already warned: the downgrade notice
        # the loop injects says everything the "running low" nudge would, and
        # spend is by definition past ``warn_fraction`` here — so without this
        # the model would get two near-identical consolidation nudges
        # back-to-back on the very next call.
        self._warned.add(BudgetDimension.COST)
        return self._downgrade

    def take_warning(self) -> str | None:
        """One-shot "running low" message for the first dimension past
        ``warn_fraction``, or ``None``.

        Each dimension warns at most once per run, so a long run cannot spam
        the model (or the user) with the same nudge every iteration.
        """
        if self.warn_fraction <= 0:
            return None
        for dimension, spent, limit in self._dimension_state():
            if limit is None or dimension in self._warned:
                continue
            if spent >= limit * self.warn_fraction:
                self._warned.add(dimension)
                return self._warning_message(dimension, spent, limit)
        return None

    # ── Internals ────────────────────────────────────────────────────────

    def _dimension_state(self) -> list[tuple[BudgetDimension, float, float | None]]:
        """(dimension, spent, limit) for each dimension, in ticket order.

        Cost reports ``None`` as its limit when no price row is known, which
        is what makes the whole cost dimension fail open.
        """
        return [
            (
                BudgetDimension.COST,
                float(self._spent_cost),
                self.max_cost_usd if self.enforces_cost else None,
            ),
            (BudgetDimension.TOKENS, float(self._spent_tokens), self.max_total_tokens),
            (BudgetDimension.TOOL_CALLS, float(self._tool_calls), self.max_tool_calls),
            (BudgetDimension.DURATION, self.elapsed_seconds, self.max_duration_seconds),
        ]

    def _reserved_cost(self) -> Decimal:
        return sum((cost for cost, _ in self._reservations.values()), _ZERO)

    def _reserved_tokens(self) -> int:
        return sum(tokens for _, tokens in self._reservations.values())

    def _first_violation(
        self, *, projected_cost: Decimal, projected_tokens: int, final: bool
    ) -> BudgetViolation | None:
        headroom = 1.0 if final else 1.0 - self.final_answer_reserve_fraction

        if self.enforces_cost and self.max_cost_usd is not None:
            spent = float(self._spent_cost)
            projected = spent + float(self._reserved_cost()) + float(projected_cost)
            gate = self.max_cost_usd * headroom
            if projected > gate:
                return BudgetViolation(
                    BudgetDimension.COST, spent, projected, self.max_cost_usd, gate
                )

        if self.max_total_tokens is not None:
            spent_tokens = float(self._spent_tokens)
            projected_total = spent_tokens + self._reserved_tokens() + projected_tokens
            gate = self.max_total_tokens * headroom
            if projected_total > gate:
                return BudgetViolation(
                    BudgetDimension.TOKENS,
                    spent_tokens,
                    projected_total,
                    self.max_total_tokens,
                    gate,
                )

        # Tool calls and duration are NOT part of an LLM call's reservation —
        # what has already been consumed is the whole question — and neither
        # takes the closing-synthesis headroom:
        #
        #   * the final answer is a tool-free call, so holding back tool calls
        #     for it would just silently shrink the budget the caller asked
        #     for ("50 tool calls" must mean 50, not 45); and
        #   * duration already gets an equivalent reserve in *seconds* from
        #     the loop's wall-clock machinery
        #     (``_FORCED_ANSWER_DEADLINE_RESERVE_SECS``), so applying the
        #     fraction here as well would stack two reserves and stop the run
        #     earlier than either intends.  Full-limit comparison keeps this a
        #     pure backstop behind that machinery — and still enforces the
        #     dimension for callers using ``RunBudget`` on its own.
        #
        # ``final`` skips the tool-call ceiling outright: the call it gates
        # dispatches no tools, so an exhausted tool-call budget must end tool
        # USE, never the one call that turns the work into an answer.
        if not final and self.max_tool_calls is not None and self._tool_calls >= self.max_tool_calls:
            return BudgetViolation(
                BudgetDimension.TOOL_CALLS,
                self._tool_calls,
                self._tool_calls,
                self.max_tool_calls,
                self.max_tool_calls,
            )

        if self.max_duration_seconds is not None:
            elapsed = self.elapsed_seconds
            if elapsed >= self.max_duration_seconds:
                return BudgetViolation(
                    BudgetDimension.DURATION,
                    elapsed,
                    elapsed,
                    self.max_duration_seconds,
                    self.max_duration_seconds,
                )

        return None

    def _warning_message(
        self, dimension: BudgetDimension, spent: float, limit: float
    ) -> str:
        pct = 100.0 * spent / limit if limit else 100.0
        if dimension is BudgetDimension.COST:
            detail = f"${spent:.4f} of the ${limit:.4f} cost budget"
        elif dimension is BudgetDimension.TOKENS:
            detail = f"{int(spent):,} of the {int(limit):,} token budget"
        elif dimension is BudgetDimension.TOOL_CALLS:
            detail = f"{int(spent):,} of the {int(limit):,} tool-call budget"
        else:
            detail = f"{spent:.0f}s of the {limit:.0f}s time budget"
        return (
            f"⚠️ Run budget is running low: {detail} used ({pct:.0f}%). "
            "Prioritise consolidating what you already have into a final answer."
        )


def budget_configured(overrides: BudgetOverrides | None = None) -> bool:
    """True when at least one dimension is bounded for this run.

    Checked before anything expensive (a pricing query) so an unconfigured
    deployment — the default — pays nothing for this feature.  A caller's own
    deadline never activates a budget on its own: that is already enforced by
    the loop's ``wall_clock_deadline``.

    ``overrides`` is the resolved per-agent / per-flow configuration.
    A budget an agent carries activates the feature for that agent's runs even
    on a platform that configured none — which is the point of a per-agent
    budget on a deployment that never set a global one.
    """
    effective = resolve_effective(overrides)
    return any(
        getattr(effective, spec.key) is not None for spec in BUDGET_LIMIT_SPECS
    )


def build_run_budget(
    *,
    price: ModelPrice | None = None,
    fallback: BudgetFallbackModel | None = None,
    max_duration_seconds: float | None = None,
    overrides: BudgetOverrides | None = None,
) -> RunBudget | None:
    """Build the budget for one run, or ``None`` when nothing bounds it.

    ``max_duration_seconds`` lets a caller that already has a tighter deadline
    of its own (e.g. an assignment's timeout) fold it into the same object;
    the smaller of the two wins in the loop.  It cannot activate a budget by
    itself — see ``budget_configured``.

    ``fallback`` is the optional cheaper model the run may finish on instead of
    stopping when the cost gate is reached.

    ``overrides`` narrows (or, when an admin authorised it, widens) the
    platform configuration for this run — see
    ``app.services.budget_policy``.  Every limit is read from the *resolved*
    configuration, so the platform env stays the value that applies when no
    layer overrides it.
    """
    effective = resolve_effective(overrides)

    if not any(
        getattr(effective, spec.key) is not None for spec in BUDGET_LIMIT_SPECS
    ):
        return None

    configured_duration = _positive_or_none(effective.max_duration_seconds)
    caller_duration = _positive_or_none(max_duration_seconds)
    if configured_duration is None:
        duration = caller_duration
    elif caller_duration is None:
        duration = configured_duration
    else:
        duration = min(configured_duration, caller_duration)

    from app.core.config import settings

    budget = RunBudget(
        max_cost_usd=effective.max_cost_usd or 0.0,
        max_total_tokens=effective.max_total_tokens or 0,
        max_tool_calls=effective.max_tool_calls or 0,
        max_duration_seconds=duration,
        price=price,
        fallback=fallback,
        strict_tool_calls=bool(settings.agent_loop_max_tool_calls_strict),
        # ``platform_ceilings()`` always sets a reserve, so the fallback is
        # unreachable today — kept because it is the only place this module
        # would otherwise depend on that invariant holding in a caller.
        final_answer_reserve_fraction=(
            effective.final_answer_reserve
            if effective.final_answer_reserve is not None
            else DEFAULT_FINAL_ANSWER_RESERVE
        ),
    )
    if not budget.active:
        return None
    if budget.max_cost_usd is not None and price is None:
        logger.warning(
            "Per-run cost budget is configured ($%.4f) but no price row was "
            "resolved for this model — cost is NOT enforced for this run "
            "(the token / tool-call / duration limits still are). Seed the "
            "model's pricing to enable cost enforcement.",
            budget.max_cost_usd,
        )
    return budget


async def resolve_budget_price(
    db: AsyncSession, *, model_id: str | None, provider: str | None
) -> ModelPrice | None:
    """Best-effort price row for a run's model, for cost enforcement.

    Resolved ONCE by the caller before the loop starts, so the loop itself
    never touches the DB for accounting (it would otherwise pin a pool
    connection for the whole run).  Falls back to a provider-agnostic lookup
    because most trace/run producers do not stamp a provider, and model ids
    are effectively unique across the seeded price table.

    Never raises: a pricing lookup failure degrades to "cost not enforced"
    (the other dimensions still apply) rather than failing the run.
    """
    if not model_id:
        return None
    try:
        if provider:
            price = await resolve_model_price(db, model_id, provider)
            if price is not None:
                return price
        return await resolve_model_price_any(db, model_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Could not resolve a price row for model=%s provider=%s (%s) — "
            "the per-run cost budget will not be enforced for this run",
            model_id,
            provider or "unknown",
            exc,
        )
        return None


async def resolve_budget_fallback(
    db: AsyncSession,
    *,
    current_model: str | None,
    overrides: BudgetOverrides | None = None,
    user_id: object = None,
) -> BudgetFallbackModel | None:
    """Resolve the cheaper model a cost-exhausted run may continue on.

    Opt-in (``AGENT_LOOP_BUDGET_FALLBACK_ENABLED``): with the flag off, the
    budget keeps the pure hard-stop behaviour.  Silently finishing a
    run on a weaker model has to be something an operator chose.

    Deliberately reuses the *same* configuration surface as the failover chain
    (``PlatformSettings.fallback_model`` / ``DEFAULT_FALLBACK_MODEL``, via
    ``app.llm.router.resolve_configured_fallback``) rather than growing a
    second "which model do we fall back to?" setting.

    Resolved here — before the loop, alongside the price row — because the loop
    holds no DB connection: building the fallback's provider needs its stored
    config row, and the switch has to be possible *without* a provider error
    (which is all ``FailoverLLMProvider`` reacts to).

    Returns ``None`` (no downgrade possible, the run hard-stops as before)
    when the flag is off, when no usable fallback is configured, or when the
    fallback model has no price row — with no price there is no way to know the
    same call is actually cheaper on it.  Never raises.

    ``overrides`` may enable the downgrade for one agent on a platform that
    left it off, and may name a *different* cheaper model than the platform
    fallback — an agent whose own primary model IS the platform
    fallback has nowhere to downgrade to otherwise.

    ``user_id`` is the run's caller: the cheaper model runs on one of their
    providers, as the primary does.
    """
    effective = resolve_effective(overrides)
    if not effective.fallback_enabled:
        return None
    try:
        from app.llm.router import (
            resolve_fallback_model_provider,
            resolve_model_provider,
        )

        target = (
            await resolve_model_provider(
                db, effective.fallback_model, user_id=user_id
            )
            if effective.fallback_model
            else await resolve_fallback_model_provider(
                db, current_model=current_model, user_id=user_id
            )
        )
        if target is None:
            return None
        fallback_provider, fallback_model = target
        # A downgrade to the model already in use is not a downgrade; it would
        # burn the one-per-run switch and change nothing.
        if current_model and fallback_model == current_model:
            return None
        price = await resolve_budget_price(
            db,
            model_id=fallback_model,
            provider=getattr(fallback_provider, "provider_name", None),
        )
        if price is None:
            logger.warning(
                "Budget-driven model downgrade is enabled but no price row is "
                "known for the configured fallback model %r — this run will "
                "hard-stop when its cost budget is reached instead of "
                "downgrading. Seed the model's pricing to enable it.",
                fallback_model,
            )
            return None
        return BudgetFallbackModel(
            model_id=fallback_model, price=price, provider=fallback_provider
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Could not resolve a budget fallback model (%s) — this run will "
            "hard-stop when its cost budget is reached",
            exc,
        )
        return None


async def build_run_budget_for_run(
    db: AsyncSession,
    *,
    model_id: str | None,
    provider: str | None,
    max_duration_seconds: float | None = None,
    overrides: BudgetOverrides | None = None,
    user_id: object = None,
) -> RunBudget | None:
    """The budget for one run, resolving pricing only if needed.

    The single call sites need: nothing configured -> ``None`` and **no DB
    query**; no cost limit -> no pricing query either (a token / tool-call /
    duration budget does not need prices, and neither does the cost-only
    model downgrade).

    ``overrides`` is the resolved per-agent / per-flow layer: it can
    activate a budget on a platform that configured none, so every gate below
    reads the *resolved* configuration rather than the settings directly.
    """
    effective = resolve_effective(overrides)
    if not any(
        getattr(effective, spec.key) is not None for spec in BUDGET_LIMIT_SPECS
    ):
        return None
    _cost_bounded = effective.max_cost_usd is not None
    price = (
        await resolve_budget_price(db, model_id=model_id, provider=provider)
        if _cost_bounded
        else None
    )
    # A downgrade only ever helps the COST dimension, and only when cost is
    # actually enforceable for the primary — so nothing is resolved (and no
    # query issued) for a token / tool-call / duration-only budget.
    fallback = (
        await resolve_budget_fallback(
            db, current_model=model_id, overrides=overrides, user_id=user_id
        )
        if _cost_bounded and price is not None
        else None
    )
    return build_run_budget(
        price=price,
        fallback=fallback,
        max_duration_seconds=max_duration_seconds,
        overrides=overrides,
    )
