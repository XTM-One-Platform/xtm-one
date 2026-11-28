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

"""The relevance score of the Threat Relevance Analyst, computed, never added up by the model.

The analyst scores six factors with fixed weights (``CTEM_RELEVANCE_WEIGHTS``); the
score is their weighted sum, rounded half up. A model doing that arithmetic slips
(#2763: a right ``score`` of 58 next to a ``reason`` "correcting" it to 56), and near
the bar of ``CTEM_RELEVANCE_MIN_SCORE`` a slip in ``score`` flips ``relevant``.

``ctem_relevance_score`` is the analyst's finalize step: it reads the answer, recomputes
the score of every threat whose factors it can read, overwrites a mismatching ``score``
and the ``relevant`` it decided, and logs the correction. An answer already right, or
one it cannot read (another shape, a caller's own schema), is returned unchanged,
byte for byte.

The bar is the prioritization policy's ``relevance_min_score_threat`` (#2798), read live
at each finalize, ``CTEM_RELEVANCE_MIN_SCORE`` when the profile cannot be read.

The PIR half of the relevance rule (a PIR score at or above the policy's
``pir_min_score_threat``) is not in the answer: a threat the analyst called relevant
while its own score was under the bar was called relevant by that rule, and keeps it.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

TOOL_NAME = "ctem_relevance_score"


def _weights() -> tuple[dict[str, int], int]:
    from app.agents.builtin_agents import CTEM_RELEVANCE_MIN_SCORE, CTEM_RELEVANCE_WEIGHTS

    return CTEM_RELEVANCE_WEIGHTS, CTEM_RELEVANCE_MIN_SCORE


def weighted_score(factors: Any, weights: dict[str, int] | None = None) -> int | None:
    """The weighted total of *factors* (``[{name, score}]``), rounded half up, or ``None``.

    ``None`` when the factors cannot be read as the fixed set: not a list, a name outside
    the set or named twice, a score that is not an integer from 0 to 100. A factor of the
    set left out scores 0, as the persona says of a factor with no evidence.
    """
    weights = weights if weights is not None else _weights()[0]
    if not isinstance(factors, list):
        return None
    seen: dict[str, int] = {}
    for factor in factors:
        if not isinstance(factor, dict):
            return None
        name, score = factor.get("name"), factor.get("score")
        if name not in weights or name in seen:
            return None
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
            return None
        seen[name] = score
    hundredths = sum(score * weights[name] for name, score in seen.items())
    return (hundredths + 50) // 100


def _correct_threat(threat: dict[str, Any], weights: dict[str, int], bar: int) -> dict[str, tuple[Any, Any]] | None:
    """The correction of one threat entry (``{score: (old, new), relevant: (old, new)}``), or ``None``."""
    computed = weighted_score(threat.get("factors"), weights)
    submitted = threat.get("score")
    if computed is None or "score" not in threat:
        return None
    changes: dict[str, tuple[Any, Any]] = {}
    if submitted != computed:
        changes["score"] = (submitted, computed)
        threat["score"] = computed
    claimed = threat.get("relevant")
    if isinstance(claimed, bool):
        # Relevant by the PIR floor: the analyst said relevant with its own score under the bar.
        by_pir_floor = claimed and isinstance(submitted, int) and not isinstance(submitted, bool) and submitted < bar
        relevant = computed >= bar or by_pir_floor
        if relevant != claimed:
            changes["relevant"] = (claimed, relevant)
            threat["relevant"] = relevant
    return changes or None


async def live_relevance_bar() -> int:
    """The policy's ``relevance_min_score_threat`` (#2798), ``CTEM_RELEVANCE_MIN_SCORE`` when unreadable."""
    from app.core.database import async_session
    from app.services import organization_context as org_context

    default = _weights()[1]
    try:
        async with async_session() as db:
            _row, profile = await org_context.current_profile(db)
    except Exception:
        logger.warning("CTEM relevance bar: the organization profile was not read", exc_info=True)
        return default
    value = getattr(getattr(profile, "prioritization_policy", None), "relevance_min_score_threat", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def correct_relevance_answer(content: str, bar: int | None = None) -> str:
    """*content* with every readable threat's ``score`` and ``relevant`` recomputed.

    *bar* is the live relevance bar of the policy (:func:`live_relevance_bar`), the
    default when ``None``. Returned unchanged (the same string) when nothing needed a
    correction or the answer is not a JSON object with a ``threats`` list.
    """
    from app.services.structured_output import extract_json_text

    try:
        payload = json.loads(extract_json_text(content or ""))
    except (ValueError, TypeError):
        return content
    if not isinstance(payload, dict) or not isinstance(payload.get("threats"), list):
        return content
    weights, default = _weights()
    bar = default if bar is None else bar
    corrected = False
    for threat in payload["threats"]:
        if not isinstance(threat, dict):
            continue
        changes = _correct_threat(threat, weights, bar)
        if changes:
            corrected = True
            logger.warning(
                "CTEM relevance score corrected for %s: %s",
                threat.get("stix_id") or threat.get("name") or "a threat",
                ", ".join(f"{field} {old!r} -> {new!r}" for field, (old, new) in changes.items()),
            )
    return json.dumps(payload, ensure_ascii=False) if corrected else content


def build_ctem_relevance_score_tool() -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The analyst's finalize tool: one ``response`` string in, the corrected answer out."""

    async def _score(response: str = "") -> ToolResult:
        return ToolResult(tool_call_id="", content=correct_relevance_answer(response, await live_relevance_bar()))

    executor = CallableToolExecutor()
    executor.register(TOOL_NAME, _score)
    definition = ToolDefinition(
        name=TOOL_NAME,
        description=(
            "Recompute each threat's relevance score as the weighted sum of its six factor "
            "scores and its relevant flag from that score."
        ),
        input_schema={
            "type": "object",
            "properties": {"response": {"type": "string", "description": "The analyst's JSON answer."}},
            "required": ["response"],
        },
        source="builtin:ctem",
    )
    return [definition], executor
