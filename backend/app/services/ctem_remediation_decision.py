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
#: The room of each option text on the card (#2174): a longer text is refused, never cut,
#: so the decider reads what the agent meant and the agent rewrites it shorter.
TEXT_LIMITS: dict[str, int] = {
    "label": 120,
    "residual_risk": 160,
    "time_to_apply": 60,
    "runbook": 120,
    "not_priced_reason": 120,
}
#: What an option without figures reads when the agent gave no reason (#2174).
NOT_PRICED_DEFAULT = "no risk figures given"


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
    not_priced_reason: str = ""

    @property
    def priced(self) -> bool:
        """Whether the option carries any risk figure."""
        return any(v is not None for v in (self.cost, self.expected_loss_before, self.expected_loss_after))


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


def _field(item: dict[str, Any], name: str, key: str) -> str:
    """An option text within its room, whitespace collapsed; a longer one is refused with its length (#2174)."""
    text = " ".join(str(item.get(name) or "").split())
    limit = TEXT_LIMITS[name]
    if len(text) > limit:
        raise DecisionInputError(
            f"option {key or '?'} {name} is {len(text)} characters, the card holds {limit}: write it shorter "
            "and put the detail in the case notes"
        )
    return text


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
        label = _field(item, "label", key)
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
                residual_risk=_field(item, "residual_risk", key),
                time_to_apply=_field(item, "time_to_apply", key),
                runbook=_field(item, "runbook", key),
                preapproved=bool(item.get("preapproved")) and bool(_field(item, "runbook", key)),
                not_priced_reason=_field(item, "not_priced_reason", key),
            )
        )
    return out


#: A version an option's text cites: "9.10.0", "v7.2.6" (two dotted numbers at least),
#: also when a sentence ends on it ("Upgrade to 9.10.0.").
_VERSION = re.compile(r"(?<![\w.])v?(\d+(?:\.\d+){1,3})(?!\w|\.\d)")


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
    if figures and not option.priced:
        # Nothing to print as figures: say why, never "cost unknown" three times (#2174).
        parts.append(f"not priced: {option.not_priced_reason or NOT_PRICED_DEFAULT}")
    elif figures:
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
    severity: str = "",
    sla_due_at: Any = None,
) -> dict[str, Any]:
    """The ask of the unified decision system for a waiting remediation decision, validated by ``normalize_ask``.

    *currency* is the organization profile's (ISO 4217): the figures read in it;
    empty when the profile names none. *severity* (P1 to P4) opens the context and
    *sla_due_at* is the ask's deadline, which the card shows in the reader's time
    zone (#2174).
    """
    from app.services.decision_ask import normalize_ask

    choice = recommended(options, recommendation)
    lead = f"{severity.strip()} case. " if severity and severity.strip() else ""
    ask: dict[str, Any] = {
        "question": _text(f"Which remediation do we apply to the case {case_title}?", 500),
        "context": _text(f"{lead}The remediation decision waits because {reason}.", 800),
        "options": [
            {"key": o.key, "label": o.label, "effect": option_effect(o, figures=figures, currency=currency)}
            for o in options
        ],
        "recommendation": choice.key,
    }
    if case_url:
        ask["links"] = [{"label": "The case", "url": case_url}]
    if sla_due_at:
        ask["deadline"] = sla_due_at
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
