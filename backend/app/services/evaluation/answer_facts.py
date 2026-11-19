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

"""What the comparison and the bundle review read from one stored answer (#12282).

One owner for three readings both pages share, so they never disagree:

* :func:`metric_counts`: each scored metric's ``tp`` / ``fp`` / ``fn`` as its result
  reports them (precision and recall need the split the ``evaluation_metrics`` rows,
  which keep the pooled numerator and denominator only, do not hold);
* :func:`is_unreadable`: whether the scorer could not read the answer at all, from the
  ``reason`` every unreadable-answer outcome carries (``unreadable_answer``), whatever
  the family;
* :func:`judge_score`: the judge's score of an answer, or ``None`` when there is none.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

#: Anomalies that leave an entry out of the judge's scores, as the run's pages read them.
_JUDGE_EXCLUDED_ANOMALIES = frozenset({"transient_error", "platform_error"})


def _scored(metric_results: object) -> dict[str, Mapping[str, Any]]:
    if not isinstance(metric_results, Mapping):
        return {}
    return {
        str(key): blob
        for key, blob in metric_results.items()
        if isinstance(blob, Mapping) and blob.get("status") == "scored"
    }


def metric_counts(metric_results: object, keys: list[str] | tuple[str, ...]) -> dict[str, list[float]]:
    """``{key: [tp, fp, fn]}`` for the scored metrics of *keys* that report the three."""
    out: dict[str, list[float]] = {}
    for key, blob in _scored(metric_results).items():
        if key not in keys:
            continue
        detail = blob.get("detail")
        if not isinstance(detail, Mapping):
            continue
        values = [detail.get(part) for part in ("tp", "fp", "fn")]
        if all(isinstance(v, int | float) and not isinstance(v, bool) and v >= 0 for v in values):
            out[key] = [float(v) for v in values]  # type: ignore[arg-type]
    return out


def is_unreadable(metric_results: object) -> bool:
    """Whether the scorer could not read the answer: a scored quality metric whose detail
    gives the reason (``unreadable_answer`` always does), never a platform key."""
    for key, blob in _scored(metric_results).items():
        if key.startswith("performance."):
            continue
        detail = blob.get("detail")
        if isinstance(detail, Mapping) and isinstance(detail.get("reason"), str):
            return True
    return False


def judge_score(
    score: float | None,
    *,
    scorer_selection: list[str] | None,
    is_judge_failure: bool,
    anomaly: str | None,
) -> float | None:
    """The judge's score of an answer, or ``None``: the run's judge was off (``score`` is
    NOT NULL and then holds 0.0), its verdict failed, or the entry was left out."""
    judged = scorer_selection is None or "judge" in scorer_selection
    if not judged or is_judge_failure or anomaly in _JUDGE_EXCLUDED_ANOMALIES:
        return None
    return score


def setup_of(run: Any) -> tuple[str, dict[str, Any]]:
    """A run's experimental setup and its fingerprint: two runs with the same key ran the
    same subject version, prompts, skills, settings and execution mode (reruns of one
    setup), whatever the model."""
    setup = {
        "subject_id": str(run.agent_id or run.flow_id) if (run.agent_id or run.flow_id) else None,
        "persona_version": run.persona_version_number,
        "flow_version": run.flow_version_number,
        "prompt_versions": run.prompt_versions or {},
        "agent_config_versions": run.agent_config_versions or {},
        "skill_versions": run.skill_versions or {},
        "execution_mode": run.execution_mode,
    }
    digest = hashlib.sha256(json.dumps(setup, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    return digest, setup


__all__ = ["is_unreadable", "judge_score", "metric_counts", "setup_of"]
