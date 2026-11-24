# SPDX-License-Identifier: Apache-2.0

"""The qualitative CTEM scores: the posture read against the organization context.

The quantitative score of a stage (``ctem_scoring.score_stage``) reads the
connected modules' metrics alone. The qualitative score says how far that
score holds for THIS organization, from the organization context saved in
Settings (``services/organization_context.py``, the only source of company
context). Pure: no I/O, so the endpoint applies it to a cached payload too.

Formula (documented in the PR and pinned by ``tests/test_ctem_context_scoring.py``):

1. Each stage reads a set of context fields (``STAGE_CONTEXT_FIELDS``): the
   ones an analyst needs to judge that stage for the organization. The stage's
   context coverage ``C`` is the share of them the profile fills (0 to 1).
2. The stage's evidence score ``E`` is its quantitative score ``Q``, except
   for Prioritization when the software narrowed the known-exploited count
   (``kev_tailored_count``): ``E = (Q + T) / 2`` with
   ``T = max(0, 100 - 20 * kev_tailored_count)`` (each known-exploited CVE in
   software the organization runs costs 20 points).
3. ``qualitative = round(E * (0.4 + 0.6 * C))``: a stage judged with none of
   its context keeps 40% of its evidence, one judged with all of it keeps all.
   No profile saved, or ``C == 0``: no qualitative score (the page asks for
   the context instead of printing a number nobody grounded).
4. The composite qualitative posture is the weighted average of the stages
   that have one, with the same per-vertical weights as the quantitative one.

``context.state`` is ``missing`` (no profile saved), ``partial`` (a field the
stages read is empty, named in ``missing_fields``) or ``complete``.
"""

from __future__ import annotations

from typing import Any

from app.services.ctem_scoring import band

#: The context fields each stage is judged against, by contract path
#: (``organization_context.CONTRACT_FIELDS``).
STAGE_CONTEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "scoping": ("crown_jewels", "tech_stack", "sector", "regions"),
    "discovery": ("pir_ids", "sector", "regions"),
    "prioritization": ("tech_stack", "crown_jewels", "pir_ids"),
    "validation": ("crown_jewels", "tech_stack"),
    "remediation": ("crown_jewels", "risk_appetite.approval_threshold", "ticketing"),
}

#: Every field a stage reads, in contract order, for the "partial" notice.
CONTEXT_FIELDS: tuple[str, ...] = tuple(
    dict.fromkeys(field for fields in STAGE_CONTEXT_FIELDS.values() for field in fields)
)

#: Points one known-exploited CVE in the organization's own stack costs.
TAILORED_KEV_PENALTY = 20
#: Share of its evidence a stage judged with no context keeps.
CONTEXT_FLOOR = 0.4


def stage_coverage(stage: str, missing: set[str]) -> float:
    """Share (0 to 1) of the context fields *stage* reads that are filled."""
    fields = STAGE_CONTEXT_FIELDS.get(stage, ())
    if not fields:
        return 0.0
    return sum(1 for field in fields if field not in missing) / len(fields)


def tailored_kev_score(kev_tailored_count: Any) -> float | None:
    """``T``: 100 with no known-exploited CVE in the stack, minus 20 per CVE."""
    if not isinstance(kev_tailored_count, (int, float)) or isinstance(kev_tailored_count, bool):
        return None
    return max(0.0, 100.0 - TAILORED_KEV_PENALTY * float(kev_tailored_count))


def qualitative_stage_score(
    stage: str, quantitative: int | None, missing: set[str], kev_tailored_count: Any = None
) -> int | None:
    """The qualitative score of one stage, ``None`` without a quantitative score or any context."""
    if quantitative is None:
        return None
    coverage = stage_coverage(stage, missing)
    if coverage == 0:
        return None
    evidence = float(quantitative)
    if stage == "prioritization":
        tailored = tailored_kev_score(kev_tailored_count)
        if tailored is not None:
            evidence = (evidence + tailored) / 2
    return round(evidence * (CONTEXT_FLOOR + (1 - CONTEXT_FLOOR) * coverage))


def context_state(has_profile: bool, missing: set[str]) -> dict[str, Any]:
    """``{state, missing_fields}``: missing, partial (fields named) or complete."""
    if not has_profile:
        return {"state": "missing", "missing_fields": list(CONTEXT_FIELDS)}
    empty = [field for field in CONTEXT_FIELDS if field in missing]
    return {"state": "partial" if empty else "complete", "missing_fields": empty}


def with_context_scores(payload: dict[str, Any], *, has_profile: bool, missing_fields: list[str]) -> dict[str, Any]:
    """A copy of a posture payload with the qualitative scores and the context state.

    Adds ``qualitative_score``, ``qualitative_band`` and ``context`` to the
    payload and ``qualitative_score``, ``qualitative_band`` and
    ``context_missing`` (the empty fields that stage reads) to each stage. The
    quantitative fields are never changed.
    """
    missing = set(missing_fields) if has_profile else set(CONTEXT_FIELDS)
    headline = payload.get("headline") or {}
    weights = payload.get("weights") or {}
    stages: list[dict[str, Any]] = []
    weighted = 0.0
    total = 0.0
    for stage in payload.get("stages") or []:
        key = stage.get("key", "")
        score = (
            qualitative_stage_score(key, stage.get("score"), missing, headline.get("kev_tailored_count"))
            if has_profile
            else None
        )
        stages.append(
            {
                **stage,
                "qualitative_score": score,
                "qualitative_band": band(score),
                "context_missing": [f for f in STAGE_CONTEXT_FIELDS.get(key, ()) if f in missing],
            }
        )
        weight = weights.get(key)
        if score is not None and isinstance(weight, (int, float)):
            weighted += score * weight
            total += weight
    composite = round(weighted / total) if total else None
    return {
        **payload,
        "stages": stages,
        "qualitative_score": composite,
        "qualitative_band": band(composite),
        "context": context_state(has_profile, missing),
    }
