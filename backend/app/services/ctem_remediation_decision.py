# SPDX-License-Identifier: Apache-2.0

"""The remediation decision of a CTEM case (CTEM-17, gate B): decided by policy, or one decision of the unified system.

No decision structure lives on the case (KB 34 s.6.1): the decision is the
gate ``ctem.remediation.decision`` of the fleet's approval catalog (CTEM-34).
This module holds the rules, no model call:

- :func:`parse_options`: the options the fleet built (each with its cost,
  expected loss before and after, residual risk, time to apply, and the
  pre-approved runbook it matches, when any), figures read from OpenCRQ by the
  run with its own user's identity.
- :func:`decide`: whether the decision waits for a person. The gate waits when
  the fleet's level or pin says so; else, only when the organization set an
  approval threshold (``risk_appetite.approval_threshold``) and the case's
  expected loss is above it (or unknown), and the recommended option is not a
  pre-approved runbook. With no threshold set it is decided by policy.
- :func:`decision_ask`: the ask of the unified decision system
  (``services/decision_ask.py``: question, options with their effect, the
  recommendation) when it waits. Figures are left out for a user who cannot
  read OpenCRQ.

The case keeps the outcome in its event trail (``ctem_case_loop.record_gate``
plus one ``note`` naming the option). Every viewer of the case reads that
trail, so it never carries an OpenCRQ figure (cost, expected loss, threshold).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

GATE_KEY = "ctem.remediation.decision"
MAX_OPTIONS = 6


class DecisionInputError(ValueError):
    """The options or the recommendation are not usable; nothing is decided."""


@dataclass(frozen=True)
class RemediationOption:
    key: str
    label: str
    cost: float | None = None
    expected_loss_before: float | None = None
    expected_loss_after: float | None = None
    residual_risk: str = ""
    time_to_apply: str = ""
    runbook: str = ""
    preapproved: bool = False


@dataclass(frozen=True)
class Decision:
    """What the rule decided: ``waits`` for a person, or ``chosen`` by policy, with the reason."""

    waits: bool
    reason: str
    chosen: str = ""
    figures_shown: bool = True
    notes: list[str] = field(default_factory=list)
    trail_reason: str = ""

    @property
    def case_reason(self) -> str:
        """The reason written on the case trail, which every viewer of the case reads: never an OpenCRQ figure."""
        return self.trail_reason or self.reason


def _number(value: Any, name: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise DecisionInputError(f"{name} must be a number") from exc
    if number < 0:
        raise DecisionInputError(f"{name} cannot be negative")
    return number


def _text(value: Any, limit: int) -> str:
    from app.services.decision_ask import fit

    return fit(" ".join(str(value or "").split()), limit)


def parse_options(raw: Any) -> list[RemediationOption]:
    """The options as given, checked: 1 to 6, unique keys, a label each, figures as numbers."""
    if not isinstance(raw, list) or not raw:
        raise DecisionInputError("options must list at least one remediation option")
    if len(raw) > MAX_OPTIONS:
        raise DecisionInputError(f"at most {MAX_OPTIONS} options")
    out: list[RemediationOption] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise DecisionInputError("each option is an object with key and label")
        key = _text(item.get("key"), 40)
        label = _text(item.get("label"), 120)
        if not key or not label:
            raise DecisionInputError("each option needs a key and a label")
        if key.casefold() in seen:
            raise DecisionInputError(f"option key {key!r} is given twice")
        seen.add(key.casefold())
        out.append(
            RemediationOption(
                key=key,
                label=label,
                cost=_number(item.get("cost"), f"{key}.cost"),
                expected_loss_before=_number(item.get("expected_loss_before"), f"{key}.expected_loss_before"),
                expected_loss_after=_number(item.get("expected_loss_after"), f"{key}.expected_loss_after"),
                residual_risk=_text(item.get("residual_risk"), 80),
                time_to_apply=_text(item.get("time_to_apply"), 60),
                runbook=_text(item.get("runbook"), 120),
                preapproved=bool(item.get("preapproved")) and bool(_text(item.get("runbook"), 120)),
            )
        )
    return out


#: A version an option's text cites: "9.10.0", "v7.2.6" (two dotted numbers at least).
_VERSION = re.compile(r"(?<![\w.])v?(\d+(?:\.\d+){1,3})(?![\w.])")


def unverified_versions(raw: Any, fixed: Iterable[str]) -> list[str]:
    """The versions the options cite that the case's fixed versions do not carry (#2103 family, partner 0ae1fa2e).

    An option whose text names a version the case does not know is a claim no source backs:
    "upgrade to 9.10.0" on a case whose fixed version is unknown. An option that gives
    ``version_source`` (the advisory or release note it read) is taken as backed.
    """
    known = {v.strip().lstrip("vV") for v in fixed if v and v.strip()}
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or str(item.get("version_source") or "").strip():
            continue
        text = " ".join(str(item.get(k) or "") for k in ("label", "residual_risk", "runbook"))
        for match in _VERSION.finditer(text):
            if match.group(1) not in known and match.group(1) not in out:
                out.append(match.group(1))
    return out


def recommended(options: list[RemediationOption], recommendation: str) -> RemediationOption:
    wanted = _text(recommendation, 40).casefold()
    for option in options:
        if option.key.casefold() == wanted:
            return option
    raise DecisionInputError(f"recommendation {recommendation!r} is not the key of one of the options")


def case_expected_loss(options: list[RemediationOption]) -> float | None:
    """The case's expected loss before any treatment: the largest ``expected_loss_before`` given, else unknown."""
    known = [o.expected_loss_before for o in options if o.expected_loss_before is not None]
    return max(known) if known else None


def decide(
    options: list[RemediationOption],
    recommendation: str,
    *,
    gate_waits: bool,
    gate_by: str,
    approval_threshold: float | None,
    figures_visible: bool = True,
) -> Decision:
    """Whether the remediation decision waits for a person, and why.

    *gate_waits* and *gate_by* come from the fleet's effective catalog (level
    and pin of ``ctem.remediation.decision``). A pin or level that waits always
    wins. Otherwise the threshold rule applies only when *approval_threshold*
    is set: an expected loss above it, or unknown to this user, waits unless
    the recommended option is a pre-approved runbook. No threshold: decided by
    policy.
    """
    choice = recommended(options, recommendation)
    if gate_waits:
        return Decision(True, f"the gate waits ({gate_by})", figures_shown=figures_visible)
    if approval_threshold is not None:
        loss = case_expected_loss(options) if figures_visible else None
        if choice.preapproved:
            return Decision(
                False,
                f"decided by policy: pre-approved runbook {choice.runbook} ({gate_by})",
                chosen=choice.key,
                figures_shown=figures_visible,
            )
        if loss is None:
            return Decision(
                True,
                "an approval threshold is set and the expected loss is unknown",
                figures_shown=figures_visible,
            )
        if loss > approval_threshold:
            return Decision(
                True,
                f"expected loss {loss:g} is above the approval threshold {approval_threshold:g}",
                figures_shown=figures_visible,
                trail_reason="the expected loss is above the approval threshold",
            )
        return Decision(
            False,
            f"decided by policy: expected loss {loss:g} within the approval threshold {approval_threshold:g} ({gate_by})",
            chosen=choice.key,
            figures_shown=figures_visible,
            trail_reason=f"decided by policy: expected loss within the approval threshold ({gate_by})",
        )
    return Decision(False, f"decided by policy ({gate_by})", chosen=choice.key, figures_shown=figures_visible)


def _money(value: float | None, currency: str = "") -> str:
    """A figure in words, with the organization's currency when the profile names one (#1598)."""
    if value is None:
        return "unknown"
    return f"{value:,.0f} {currency}" if currency else f"{value:,.0f}"


def option_effect(option: RemediationOption, *, figures: bool, currency: str = "") -> str:
    """One line per option: what it costs, the risk it leaves, how long it takes (figures only when readable)."""
    parts: list[str] = []
    if figures:
        parts.append(f"cost {_money(option.cost, currency)}")
        parts.append(
            f"expected loss {_money(option.expected_loss_before, currency)} before, "
            f"{_money(option.expected_loss_after, currency)} after"
        )
    if option.residual_risk:
        parts.append(f"residual risk {option.residual_risk}")
    if option.time_to_apply:
        parts.append(f"time to apply {option.time_to_apply}")
    if option.preapproved:
        parts.append(f"pre-approved runbook {option.runbook}")
    from app.services.decision_ask import MAX_EFFECT_CHARS, fit

    return fit("; ".join(parts), MAX_EFFECT_CHARS) or "no figures given"


def decision_ask(
    case_title: str,
    options: list[RemediationOption],
    recommendation: str,
    *,
    figures: bool,
    reason: str,
    case_url: str = "",
    currency: str = "",
) -> dict[str, Any]:
    """The ask of the unified decision system for a waiting remediation decision, validated by ``normalize_ask``.

    *currency* is the organization profile's (ISO 4217): the figures read in it;
    empty when the profile names none.
    """
    from app.services.decision_ask import normalize_ask

    choice = recommended(options, recommendation)
    ask: dict[str, Any] = {
        "question": _text(f"Which remediation do we apply to the case {case_title}?", 500),
        "context": _text(f"The remediation decision waits because {reason}.", 800),
        "options": [
            {"key": o.key, "label": o.label, "effect": option_effect(o, figures=figures, currency=currency)}
            for o in options
        ],
        "recommendation": choice.key,
    }
    if case_url:
        ask["links"] = [{"label": "The case", "url": case_url}]
    normalized = normalize_ask(ask)
    if normalized is None:  # pragma: no cover - the ask above is never empty
        raise DecisionInputError("the decision could not be built")
    return normalized


def outcome_note(decision: Decision, options: list[RemediationOption]) -> str:
    """The case note of the outcome: the option chosen (or the wait) and each option's line.

    The case history is read by every viewer of the case, whatever their own
    OpenCRQ access, so the note never carries a cost or an expected loss: the
    figures stay in the ask and the tool answer of the deciding user.
    """
    head = (
        f"Remediation option {decision.chosen}: {decision.case_reason}"
        if decision.chosen
        else f"Remediation decision waits: {decision.case_reason}"
    )
    lines = [head] + [f"{o.key}. {o.label}: {option_effect(o, figures=False)}" for o in options]
    return "\n".join(lines)[:2000]
