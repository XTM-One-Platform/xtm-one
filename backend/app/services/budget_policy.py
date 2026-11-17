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

"""Per-agent and per-flow overrides for the run budget, and who may raise them.

``app.tools.run_budget`` bounds one agent run along four dimensions (cost,
tokens, tool calls, duration) using ONE platform-wide configuration read from
the environment (``AGENT_LOOP_MAX_COST_USD`` and friends).  That was the right
shape for a first cut: a single dial an operator sets once.  It is the wrong
shape as soon as two agents on the same platform have different worth — a
cheap triage bot and an expensive research agent share one ceiling, so the
ceiling ends up sized for the expensive one and stops bounding the cheap one.

This module adds the **override layer** and, more importantly, the **authority
rule** that keeps it safe:

* An override may always make a run *tighter* than the platform configuration.
  Anyone who can edit an agent can do that — spending less is never a
  privilege.
* An override may only make a run *looser* — raise a limit above the platform
  ceiling, or lift one the platform set — when an **admin** writes it.  A
  non-admin submitting a higher limit is refused (HTTP 403 at the API edge),
  which is the whole point of the env value: it is the budget a user cannot
  exceed on their own.
* A value already stored above the ceiling is not re-litigated on every save.
  An admin raised it once; a non-admin editing the agent's persona must not
  be forced to lower it (or be blocked) just because the budget block rides
  along in the same PUT.  Only a *change* that escalates is refused.

Three fields are deliberately **not** gated, because none of them can increase
what a run spends:

* ``final_answer_reserve`` only moves the in-loop gate *below* the limit.
* ``fallback_enabled`` lets a cost-exhausted run continue on a cheaper model,
  re-priced at that model's rates — the cost ceiling still holds, so this
  buys work, never budget.
* ``fallback_model`` names which cheaper model that is, under the same
  ceiling.

Resolution is **nearest-wins, per dimension** (the same semantics as the flow
builder's ``inheritable`` config fields) among the CANVAS layers: each supplies
the dimensions it sets and defers the rest to the next one out.

    Budget node wired to the Agent node
      -> Budget inherited from the calling flow (``flow.call``)

so a flow author can hand a whole sub-flow one budget without editing any
agent.

The agent's own Budget tab is **not** simply the next layer out — it is a
**bound** on the canvas layers (:func:`bound_by`, limits composed as ``min``),
and only then does the platform environment fill in what nobody set:

    min(canvas, the agent's own budget)
      -> the platform environment

The distinction is the whole authority story, and getting it wrong once already
cost this module its central guarantee. Authority is checked against
:func:`platform_ceilings` at write time and only there, so if the agent's
budget were merely another layer a non-admin could wire a Budget node that
*widened* a limit an admin had pinned on the agent — legally, since it sits
under the platform ceiling. ``budget_config`` would then be advisory and the
env value the sole real bound, which is not what the Budget tab implies to the
admin who filled it in.

The module is deliberately **DB-free and settings-only**: it is imported by
the API edge (to authorise a write), by the flow runner (to resolve a chain)
and by ``run_budget`` (to build the object), and none of those should have to
agree on a session.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields, replace
from typing import Any, Literal

logger = logging.getLogger(__name__)

# The four bounded dimensions, in the order the UI and the docs present them.
# ``settings_attr`` is the platform ceiling each one is checked against, and
# ``kind`` drives coercion (a token count is an int, a cost is not).
BudgetFieldKind = Literal["float", "int"]


@dataclass(frozen=True, slots=True)
class BudgetLimitSpec:
    """One gated dimension: what it is called, and what bounds it."""

    key: str
    settings_attr: str
    label: str
    kind: BudgetFieldKind

    def format(self, value: float) -> str:
        """How the limit reads in a message ("$0.5", "120,000 tokens")."""
        if self.key == "max_cost_usd":
            return f"${value:.4f}".rstrip("0").rstrip(".")
        if self.key == "max_duration_seconds":
            return f"{int(value):,}s"
        return f"{int(value):,} {self.label.lower()}"


LIMIT_SPECS: tuple[BudgetLimitSpec, ...] = (
    BudgetLimitSpec("max_cost_usd", "agent_loop_max_cost_usd", "Cost", "float"),
    BudgetLimitSpec(
        "max_total_tokens", "agent_loop_max_total_tokens", "Tokens", "int"
    ),
    BudgetLimitSpec(
        "max_tool_calls", "agent_loop_max_tool_calls", "Tool calls", "int"
    ),
    BudgetLimitSpec(
        "max_duration_seconds",
        "agent_loop_max_duration_seconds",
        "Duration",
        "int",
    ),
)

_LIMIT_KEYS = tuple(spec.key for spec in LIMIT_SPECS)

# Fields that cannot escalate spend — see the module docstring.
_UNGATED_KEYS = ("final_answer_reserve", "fallback_enabled", "fallback_model")

ALL_KEYS = (*_LIMIT_KEYS, *_UNGATED_KEYS)

# ``final_answer_reserve`` shares the loop's clamp: a reserve above half the
# budget would leave the actual work with less than the closing synthesis.
RESERVE_MIN = 0.0
RESERVE_MAX = 0.5


@dataclass(frozen=True, slots=True)
class BudgetOverrides:
    """A partial run-budget configuration. ``None`` on a field means "inherit".

    Partial is the point: an agent that only cares about money sets
    ``max_cost_usd`` and inherits the platform's token / tool-call / duration
    limits, rather than having to restate (and then drift from) all four.
    """

    max_cost_usd: float | None = None
    max_total_tokens: int | None = None
    max_tool_calls: int | None = None
    max_duration_seconds: int | None = None
    final_answer_reserve: float | None = None
    fallback_enabled: bool | None = None
    fallback_model: str | None = None

    @property
    def is_empty(self) -> bool:
        """True when nothing is set — indistinguishable from no override."""
        return all(getattr(self, f.name) is None for f in fields(self))

    def to_dict(self) -> dict[str, Any]:
        """Only the fields actually set, so an empty override stores as ``{}``.

        Round-trips through :func:`parse_overrides` unchanged.
        """
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if getattr(self, f.name) is not None
        }


def _as_float(value: Any) -> float | None:
    """A positive float, or ``None``. ``0`` means "no limit", i.e. unset."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def _as_int(value: Any) -> int | None:
    parsed = _as_float(value)
    return int(parsed) if parsed is not None else None


# Bounds on how much of a malformed override reaches one log line.
_LOGGED_KEYS = 10
_LOGGED_KEY_CHARS = 60


def _loggable_keys(keys: list[Any]) -> str:
    """Author-supplied keys, safe to put in a production log line.

    ``repr`` keeps a CR / LF inside the line it belongs to, and the caps stop
    one malformed node from flooding the log of every run that resolves it.
    """
    shown = []
    for key in sorted(keys, key=str)[:_LOGGED_KEYS]:
        text = repr(key)
        if len(text) > _LOGGED_KEY_CHARS:
            text = text[: _LOGGED_KEY_CHARS - 3] + "..."
        shown.append(text)
    more = len(keys) - len(shown)
    return ", ".join(shown) + (f" (+{more} more)" if more > 0 else "")


def parse_overrides(raw: Any) -> BudgetOverrides:
    """Coerce stored/submitted JSON into overrides, dropping anything invalid.

    Lenient on purpose: this parses a JSON column and a flow node's config
    dict, both of which can hold whatever an older version of the UI wrote.
    A malformed value reads as "not set" (so the next layer decides) rather
    than failing a run — the same fail-open stance the cost dimension takes
    when it has no price row.

    Fail-open is right for a stored column but has a sharp edge on the flow
    node path: a mistyped key ("max_cost" for "max_cost_usd") silently leaves
    a dimension the author believed they had capped resolving to the next
    layer. Nothing should FAIL over it — a budget that refuses to load is
    worse than one that inherits — but it must be findable, so anything
    dropped is logged rather than vanishing.
    """
    if not isinstance(raw, dict):
        if raw is not None:
            logger.debug(
                "Ignoring a run-budget override that is not an object (%r)",
                type(raw).__name__,
            )
        return BudgetOverrides()

    unknown = [key for key in raw if key not in ALL_KEYS]
    if unknown:
        # WARNING, not DEBUG: a mistyped key leaves a dimension the
        # author believed capped resolving to the next layer, and DEBUG is off
        # in production - "findable" has to mean findable there.
        logger.warning(
            "Ignoring unknown run-budget override key(s): %s. Known keys: %s",
            _loggable_keys(unknown),
            ", ".join(ALL_KEYS),
        )

    reserve = raw.get("final_answer_reserve")
    parsed_reserve: float | None
    try:
        parsed_reserve = (
            None if reserve is None or reserve == "" else float(reserve)
        )
    except (TypeError, ValueError):
        parsed_reserve = None
    if parsed_reserve is not None:
        parsed_reserve = min(max(parsed_reserve, RESERVE_MIN), RESERVE_MAX)

    enabled = raw.get("fallback_enabled")
    parsed_enabled = enabled if isinstance(enabled, bool) else None

    model = raw.get("fallback_model")
    parsed_model = model.strip() if isinstance(model, str) and model.strip() else None

    parsed = BudgetOverrides(
        max_cost_usd=_as_float(raw.get("max_cost_usd")),
        max_total_tokens=_as_int(raw.get("max_total_tokens")),
        max_tool_calls=_as_int(raw.get("max_tool_calls")),
        max_duration_seconds=_as_int(raw.get("max_duration_seconds")),
        final_answer_reserve=parsed_reserve,
        fallback_enabled=parsed_enabled,
        fallback_model=parsed_model,
    )
    # A key that WAS supplied but did not survive coercion. ``0`` is excluded:
    # it is the documented spelling of "no limit", so dropping it is the
    # intended reading, not a mistake worth a log line.
    dropped = [
        key
        for key in ALL_KEYS
        if key in raw
        and raw[key] not in (None, "", 0, False)
        and getattr(parsed, key) is None
    ]
    if dropped:
        logger.warning(
            "Ignoring unusable run-budget override value(s) for: %s",
            ", ".join(dropped),
        )
    return parsed


def merge_overrides(*layers: BudgetOverrides | None) -> BudgetOverrides:
    """Nearest-wins, per field: the first layer that sets a field decides it.

    Argument order IS the precedence order — nearest first. Passing ``None``
    for a layer that does not exist keeps call sites free of conditionals.
    """
    merged: dict[str, Any] = {}
    for layer in layers:
        if layer is None:
            continue
        for name in ALL_KEYS:
            if merged.get(name) is None:
                value = getattr(layer, name)
                if value is not None:
                    merged[name] = value
    return BudgetOverrides(**merged)


def platform_ceilings() -> BudgetOverrides:
    """The environment's own configuration, as an override layer.

    This is both the outermost layer of the resolution chain and the ceiling
    :func:`ceiling_violations` authorises against — one source of truth, so
    "what applies by default" and "what a user may not exceed" can never
    drift apart.
    """
    from app.core.config import settings

    # Read through LIMIT_SPECS rather than naming the four attributes again:
    # the specs already declare which setting bounds which dimension, and a
    # second copy of that mapping is exactly what drifts.
    limits = {
        spec.key: (_as_int if spec.kind == "int" else _as_float)(
            getattr(settings, spec.settings_attr)
        )
        for spec in LIMIT_SPECS
    }
    return BudgetOverrides(
        **limits,
        final_answer_reserve=min(
            max(float(settings.agent_loop_budget_final_answer_reserve), RESERVE_MIN),
            RESERVE_MAX,
        ),
        fallback_enabled=bool(settings.agent_loop_budget_fallback_enabled),
    )


def resolve_effective(*layers: BudgetOverrides | None) -> BudgetOverrides:
    """The full configuration one run is bounded by: the chain, then the env.

    The platform layer is appended here rather than by every caller, so no
    call site can accidentally resolve a chain that bottoms out in "unbounded"
    when the operator configured a ceiling.
    """
    return merge_overrides(*layers, platform_ceilings())


# ─── Authority ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CeilingViolation:
    """One dimension a non-admin tried to raise beyond the platform ceiling."""

    key: str
    label: str
    requested: float
    ceiling: float | None

    def message(self) -> str:
        spec = next(s for s in LIMIT_SPECS if s.key == self.key)
        if self.ceiling is None:
            return (
                f"{self.label}: the platform does not bound this dimension, so "
                f"raising it to {spec.format(self.requested)} for one agent "
                "needs an administrator."
            )
        return (
            f"{self.label}: {spec.format(self.requested)} exceeds the platform "
            f"budget of {spec.format(self.ceiling)}."
        )


def ceiling_violations(
    submitted: BudgetOverrides,
    *,
    previous: BudgetOverrides | None = None,
    is_admin: bool,
) -> list[CeilingViolation]:
    """Which submitted limits a non-admin is not allowed to write.

    Empty for an admin, and empty for any override that only ever *tightens*
    what the platform configured — the common case, and the one that must stay
    frictionless.

    ``previous`` is what is stored today. A limit that is unchanged is never a
    violation even when it sits above the ceiling: an admin already authorised
    that value, and a later editor (of the persona, the tools, anything) sends
    the whole object back. Without this, one admin-raised budget would lock
    every non-admin out of the agent.

    A dimension the platform leaves unbounded (``0``) is a special case worth
    being explicit about: there is no ceiling to exceed, yet setting one for a
    single agent is still a *tightening*, so it is allowed. What needs an
    admin is the reverse — and that is expressible only as "unset it", which
    falls back to the platform's own (unbounded) value and is therefore not a
    write this function can see. Nothing to gate.
    """
    if is_admin:
        return []

    ceilings = platform_ceilings()
    stored = previous or BudgetOverrides()
    violations: list[CeilingViolation] = []
    for spec in LIMIT_SPECS:
        requested = getattr(submitted, spec.key)
        if requested is None:
            # Unset = inherit the platform value. Never an escalation.
            continue
        if getattr(stored, spec.key) == requested:
            # Unchanged — already authorised when it was first written.
            continue
        ceiling = getattr(ceilings, spec.key)
        if ceiling is None:
            # Platform unbounded: any finite limit is tighter. Allowed.
            continue
        if requested > ceiling:
            violations.append(
                CeilingViolation(
                    key=spec.key,
                    label=spec.label,
                    requested=float(requested),
                    ceiling=float(ceiling),
                )
            )
    return violations


def clamp_to_ceilings(overrides: BudgetOverrides) -> BudgetOverrides:
    """Lower any limit that sits above the platform ceiling.

    The counterpart of :func:`ceiling_violations`, for writes a caller did not
    *author*: duplicating an agent or a flow, importing a document, restoring
    an old version. Those carry a budget somebody else chose, and refusing the
    whole action over a value the caller never typed would block routine work
    (a duplicate is how you adapt a company-managed agent) to prevent an
    escalation that lowering the value already prevents.

    So the rule across the API is two-clause, and the clause is chosen by
    whether the caller picked the number:

    * typed it (agent create/update, flow save, detach) -> refuse, naming the
      dimension and the ceiling, because a precise message is actionable;
    * merely copied it (duplicate, import, restore) -> carry it at the ceiling.

    Mirrors what ``clamp_max_iterations`` already does on the agent-duplicate
    path for the same reason. Called only for a caller who cannot exceed the
    ceiling; an admin's copy keeps its values untouched.
    """
    ceilings = platform_ceilings()
    updates: dict[str, Any] = {}
    for spec in LIMIT_SPECS:
        value = getattr(overrides, spec.key)
        ceiling = getattr(ceilings, spec.key)
        if value is not None and ceiling is not None and value > ceiling:
            updates[spec.key] = ceiling
    return replace(overrides, **updates) if updates else overrides


def bound_by(
    overrides: BudgetOverrides, bound: BudgetOverrides
) -> BudgetOverrides:
    """Compose two layers so ``overrides`` can only TIGHTEN ``bound``.

    The fix for the hole that "nearest-wins everywhere" left open. Authority is
    checked against the platform ceiling at write time, and only there — so a
    flow's Budget node written by a non-admin was free to *widen* a budget an
    admin had pinned on the agent, as long as it stayed under the platform
    value:

        admin pins the triage bot at $0.10, platform ceiling is $10.00
        -> a non-admin wires a Budget node at $9.99, passes the ceiling check
        -> nearest-wins hands the run ~100x the cap the admin set for it

    Making the agent's own budget a bound closes that at RESOLUTION time, which
    is the only place it can be closed properly: a flow is saved once but the
    agent's budget can be tightened afterwards, so a save-time check would go
    stale. It also removes the need to know who authored a node at run time.

    Limits compose as ``min`` when both layers set them (either alone applies
    on its own). The three ungated fields keep nearest-wins — none of them can
    increase what a run spends, so there is nothing to bound: the reserve only
    moves the in-loop gate down, and the cheaper-model failsafe keeps the same
    cost ceiling re-priced at the cheaper model's rates.

    Deliberately applies to admins too. The run-time layer cannot tell who drew
    a node, and "an admin's node may widen an agent's budget" would be a rule
    nobody could verify by reading the canvas. An admin who needs the wider
    budget raises it on the agent, where the intent is visible and audited.
    """
    composed: dict[str, Any] = {}
    for spec in LIMIT_SPECS:
        near = getattr(overrides, spec.key)
        far = getattr(bound, spec.key)
        if near is None:
            composed[spec.key] = far
        elif far is None:
            composed[spec.key] = near
        else:
            composed[spec.key] = min(near, far)
    for key in _UNGATED_KEYS:
        near = getattr(overrides, key)
        composed[key] = near if near is not None else getattr(bound, key)
    return BudgetOverrides(**composed)
