# SPDX-License-Identifier: Apache-2.0

"""Pure CTEM scoring + aggregation helpers (no I/O).

Consumes the per-stage fact snapshots produced by ``ctem_metrics_collector`` and
turns them into the per-stage sub-scores + the composite **Exposure Posture
Score** that the ``/api/v1/ctem-metrics`` endpoints serve.

This module is intentionally side-effect free (no DB, no network) so it can be
unit-tested and reused both for the latest snapshot and for each historical day
when building the posture trend line. Every score it emits is a *derived*
indicator; the raw numbers it reads are *facts* gathered by the collector.

The five stages follow the CTEM lifecycle:

  Scoping -> Discovery -> Prioritization -> Validation -> Remediation

Each stage declares which connected products feed it. A stage is ``covered`` when
it has REAL collected data to show (a numeric score OR any non-null fact) and
``scored`` when it yields a numeric score; only ``scored`` stages move the
composite, so a missing product never silently tanks it with a 0. Because the
collector only ever emits facts for CONNECTED products (``build_posture_for_user``
adds facts solely for present providers), a stage whose primary product is missing
has no facts and is therefore an uncovered *blind spot* - its missing product is
surfaced for the UI Connect call-to-action.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.services.ctem_verticals import (
    DEFAULT_VERTICAL,
    METHODOLOGY,
    STAGE_LABELS_BY_VERTICAL,
    STAGE_PRODUCTS,
    STAGE_WEIGHTS,
    normalize_vertical,
)

# ── Stage identifiers ────────────────────────────────────────────────────────
STAGE_SCOPING = "scoping"
STAGE_DISCOVERY = "discovery"
STAGE_PRIORITIZATION = "prioritization"
STAGE_VALIDATION = "validation"
STAGE_REMEDIATION = "remediation"

STAGE_KEYS: list[str] = [
    STAGE_SCOPING,
    STAGE_DISCOVERY,
    STAGE_PRIORITIZATION,
    STAGE_VALIDATION,
    STAGE_REMEDIATION,
]

# Generic (cyber) stage labels - kept for backward-compatible imports. Per-vertical
# display labels live in ``ctem_verticals.STAGE_LABELS_BY_VERTICAL``.
STAGE_LABELS: dict[str, str] = STAGE_LABELS_BY_VERTICAL[DEFAULT_VERTICAL]


def stage_label(stage: str, vertical: str = DEFAULT_VERTICAL) -> str:
    return STAGE_LABELS_BY_VERTICAL.get(vertical, STAGE_LABELS).get(stage, stage.title())


# Posture / sub-score bands. Ordered low -> high.
BANDS: list[tuple[float, str]] = [
    (40.0, "critical"),
    (60.0, "at_risk"),
    (75.0, "fair"),
    (90.0, "good"),
    (100.1, "strong"),
]


def band(score: float | None) -> str:
    """Map a 0-100 score to a band label (``unknown`` when not computed)."""
    if score is None:
        return "unknown"
    for threshold, label in BANDS:
        if score < threshold:
            return label
    return "strong"


def clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def pct(numerator: float, denominator: float) -> float | None:
    """Return ``numerator/denominator`` as a 0-100 percentage, or ``None``."""
    if not denominator:
        return None
    return clamp(100.0 * numerator / denominator)


# ── Per-stage scorers (per vertical) ─────────────────────────────────────────
# Each scorer reads the *merged* fact dict for its stage and returns a 0-100
# float or ``None`` when the inputs are absent. Most are thin wrappers that read
# a derived percentage the collector already computed; the cyber scorers carry
# the richer cross-product blend (OpenAEV BAS preferred, OpenCTI fallback).

Scorer = Callable[[dict[str, Any]], "float | None"]


def _pct(key: str) -> Scorer:
    """A scorer that simply reads a derived percentage fact."""

    def _scorer(facts: dict[str, Any]) -> float | None:
        value = facts.get(key)
        return clamp(float(value)) if value is not None else None

    return _scorer


def _score_cyber_scoping(facts: dict[str, Any]) -> float | None:
    # Scoping = breadth of the attack surface IN SCOPE, blended across both
    # products so connecting OpenAEV can only ADD scope evidence, never subtract
    # it. OpenAEV contributes inventory breadth (``scope_breadth_pct``: managed
    # endpoints, asset groups, detection sources and ATT&CK attack domains that
    # have validation content); OpenCTI contributes intel-scope completeness
    # (``scope_completeness_pct``: observables, CVEs, indicators, reports
    # present). The available signals are averaged, so a connected-but-sparse
    # OpenAEV - rich inventory but no BAS content mapped to any attack domain yet
    # (``attack_surface_pct`` == 0) - no longer craters Scoping to 0 while the
    # intel scope is fully populated. Previously ``attack_surface_pct`` overrode
    # everything, so Scoping scored WORSE with OpenAEV connected (0/Critical)
    # than without it (the OpenCTI fallback, e.g. 100/Strong). Agent-agnostic:
    # agentless assets are a legitimate, in-scope part of the attack surface, so
    # endpoint-agent coverage is deliberately NOT used.
    breadth = facts.get("scope_breadth_pct")
    scope = facts.get("scope_completeness_pct")
    signals = [v for v in (breadth, scope) if v is not None]
    if signals:
        return clamp(sum(signals) / len(signals))
    # Legacy fallback: raw attack-surface domain coverage when neither breadth
    # signal is present (e.g. an older fact snapshot without ``scope_breadth_pct``).
    surface = facts.get("attack_surface_pct")
    return clamp(float(surface)) if surface is not None else None


def _score_cyber_discovery(facts: dict[str, Any]) -> float | None:
    # Prefer threat-informed defense coverage (threats' techniques exercised by
    # OpenAEV); fall back to OpenCTI ATT&CK detection coverage when no BAS data.
    tic = facts.get("threat_informed_coverage_pct")
    if tic is not None:
        return clamp(float(tic))
    cov = facts.get("detection_coverage_pct")
    return clamp(float(cov)) if cov is not None else None


def _score_cyber_validation(facts: dict[str, Any]) -> float | None:
    parts: list[float] = []
    weights: list[float] = []
    for key, weight in (
        ("prevention_success_pct", 0.4),
        ("detection_success_pct", 0.4),
        ("exercised_coverage_pct", 0.2),
    ):
        value = facts.get(key)
        if value is not None:
            parts.append(clamp(float(value)) * weight)
            weights.append(weight)
    if weights:
        return clamp(sum(parts) / sum(weights))
    rule_cov = facts.get("detection_rule_coverage_pct")
    return clamp(float(rule_cov)) if rule_cov is not None else None


def _score_cyber_remediation(facts: dict[str, Any]) -> float | None:
    # Closing the loop = blended response posture, NOT case closure alone:
    #  - case closure (resolved / total response cases) - present ONLY when there
    #    ARE cases, so an empty queue (no closure rate) never fakes a "strong"
    #    100; the score then rests on the other signals;
    #  - response-automation coverage (``playbook_automation_pct``: enabled /
    #    total playbooks);
    #  - when OpenAEV is connected, prevention efficacy
    #    (``prevention_success_pct``: the share of simulated attacks actually
    #    blocked) - a real remediation / mitigation signal.
    # Averaging the available signals: a strong automation + prevention program
    # credits Remediation while the case backlog is worked down, and nothing
    # inflates the score on an empty case queue.
    signals = [
        clamp(float(v))
        for v in (
            facts.get("case_closure_pct"),
            facts.get("playbook_automation_pct"),
            facts.get("prevention_success_pct"),
        )
        if v is not None
    ]
    return clamp(sum(signals) / len(signals)) if signals else None


def _score_fraud_validation(facts: dict[str, Any]) -> float | None:
    # Prefer proven OpenAEV phishing efficacy, else confirmed-malicious share.
    prevention = facts.get("prevention_success_pct")
    if prevention is not None:
        return clamp(float(prevention))
    confirmed = facts.get("confirmed_pct")
    return clamp(float(confirmed)) if confirmed is not None else None


_SCORERS: dict[str, dict[str, Scorer]] = {
    DEFAULT_VERTICAL: {
        STAGE_SCOPING: _score_cyber_scoping,
        STAGE_DISCOVERY: _score_cyber_discovery,
        STAGE_PRIORITIZATION: _pct("noncritical_share_pct"),
        STAGE_VALIDATION: _score_cyber_validation,
        STAGE_REMEDIATION: _score_cyber_remediation,
    },
    "fimi": {
        STAGE_SCOPING: _pct("scope_completeness_pct"),
        STAGE_DISCOVERY: _pct("monitoring_depth_pct"),
        STAGE_PRIORITIZATION: _pct("attribution_pct"),
        STAGE_VALIDATION: _pct("corroboration_pct"),
        STAGE_REMEDIATION: _pct("case_closure_pct"),
    },
    "fraud": {
        STAGE_SCOPING: _pct("scope_completeness_pct"),
        STAGE_DISCOVERY: _pct("discovery_pct"),
        STAGE_PRIORITIZATION: _pct("containment_pct"),
        STAGE_VALIDATION: _score_fraud_validation,
        STAGE_REMEDIATION: _pct("case_closure_pct"),
    },
}


def _confidence(stage: str, score: float | None, present: set[str], vertical: str) -> str:
    if score is None:
        return "none"
    primary = STAGE_PRODUCTS[vertical][stage]["primary"]
    return "high" if any(p in present for p in primary) else "low"


def score_stage(
    stage: str,
    facts: dict[str, Any],
    present_products: set[str],
    vertical: str = DEFAULT_VERTICAL,
) -> dict[str, Any]:
    """Score a single stage from its merged facts + the connected products."""
    vertical = normalize_vertical(vertical)
    products = STAGE_PRODUCTS[vertical][stage]
    primary = products["primary"]
    secondary = products["secondary"]
    facts = facts or {}
    score = _SCORERS[vertical][stage](facts)
    rounded = round(score) if score is not None else None
    missing = [p for p in primary if p not in present_products]
    # ``covered`` = the stage has REAL collected data to show: a numeric score OR
    # at least one non-null fact (so a closure rate with 0 cases, where the score
    # is null but ``case_incidents=0`` is real data, still counts as covered and
    # renders its real facts). A connected-but-empty stage - e.g. Validation when
    # OpenAEV (its main data source) is not connected and nothing was collected -
    # is NOT covered, so the card keeps its greyed SAMPLE placeholder instead of
    # rendering an empty card. ``primary_present`` alone is the wrong signal: a
    # stage's data can come from a product other than the one that happens to be
    # connected. ``scored`` (a numeric score) gates the composite so a null score
    # neither fakes nor tanks the headline posture.
    has_data = score is not None or any(v is not None for v in facts.values())
    return {
        "key": stage,
        "label": stage_label(stage, vertical),
        "score": rounded,
        # Band the *rounded* score so the displayed number and its band label can
        # never disagree at a boundary (e.g. 89.6 -> shown "90" must read "strong").
        "band": band(rounded),
        "covered": has_data,
        "scored": score is not None,
        "confidence": _confidence(stage, score, present_products, vertical),
        "primary_products": primary,
        "secondary_products": secondary,
        "missing_products": missing,
        "methodology": METHODOLOGY[vertical][stage],
    }


def compute_posture(
    stage_facts: dict[str, dict[str, Any]],
    present_products: set[str],
    vertical: str = DEFAULT_VERTICAL,
) -> dict[str, Any]:
    """Compute per-stage scores + the renormalized composite posture for a vertical.

    ``stage_facts`` maps a stage key to its merged fact dict. Returns a dict with
    ``posture_score`` (0-100 or ``None`` when nothing is scorable),
    ``posture_band``, coverage counts and the per-stage results (canonical order).
    """
    vertical = normalize_vertical(vertical)
    weights = STAGE_WEIGHTS[vertical]
    stages: list[dict[str, Any]] = []
    weighted_sum = 0.0
    weight_total = 0.0

    for key in STAGE_KEYS:
        result = score_stage(key, stage_facts.get(key, {}), present_products, vertical)
        stages.append(result)
        # Aggregate ONLY scored stages (primary product present AND a numeric
        # score). A connected stage with a null score (e.g. closure with 0 cases)
        # is still ``covered`` (shows real facts) but must not move - or fake - the
        # composite. A missing-product blind spot is neither covered nor scored.
        if result["scored"]:
            weight = weights[key]
            weighted_sum += result["score"] * weight
            weight_total += weight

    posture = round(weighted_sum / weight_total) if weight_total else None
    covered = sum(1 for s in stages if s["covered"])

    return {
        "posture_score": posture,
        "posture_band": band(posture),
        "covered_stages": covered,
        "total_stages": len(STAGE_KEYS),
        "vertical": vertical,
        "weights": weights,
        "stages": stages,
    }
