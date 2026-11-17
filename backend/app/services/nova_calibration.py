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

"""Semantic-threshold calibration of the Nova rules per embedding model.

The community rules' ``semantics:`` thresholds were tuned upstream against
``all-MiniLM-L6-v2``. The platform evaluates that tier with its own embedding
model, and cosine similarities are not comparable across models: a model whose
unrelated texts already score 0.3 fires every rule tuned for one where they
score 0.1, and the other way round.

A calibration maps a model's similarity onto the reference model's scale, so a
rule's threshold means the same thing whatever model computes the score::

    reference_score = REFERENCE_BASELINE + (score - baseline) / span * REFERENCE_SPAN

``baseline`` is a model's median similarity between a pattern and text that
does not match it, ``span`` the gap to its median matching similarity, both
measured on the labelled set (``nova_calibration_set.json``) with
``scripts/calibrate_nova_semantics.py``. The reference model is measured on the
same set: :data:`REFERENCE_BASELINE` and :data:`REFERENCE_SPAN` are its own
numbers, so a calibrated model's baseline lands on the reference's baseline and
its matching median on the reference's matching median.

Nothing here is guessed: every constant below comes from a run of the harness,
and each entry records the labelled-set version it was measured on. A model
without an entry is scored as before and reported as UNCALIBRATED - on the
engine status, against every rule with a ``semantics:`` pattern, and in the
Security Audit - so an operator keeps those rules on ``log`` until it is
measured.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from pathlib import Path

#: The model the upstream community rules' thresholds were calibrated for.
REFERENCE_MODEL = "all-MiniLM-L6-v2"

#: The labelled set the constants below were measured on.
CALIBRATION_SET_PATH = Path(__file__).with_name("nova_calibration_set.json")


@dataclass(frozen=True)
class SemanticCalibration:
    """How one embedding model's cosine maps onto the reference scale.

    ``baseline`` / ``span`` are medians over the labelled set; ``samples`` is
    how many pattern-to-text similarities they were taken from,
    ``set_version`` which version of the set, and ``dimensions`` the length of
    the vectors measured: the same model asked for another length (an OpenAI
    ``dimensions`` setting) scores on another scale, so it is not calibrated.
    """

    model: str
    baseline: float
    span: float
    samples: int
    set_version: str
    dimensions: int


#: The reference model measured on the labelled set: the scale the rules'
#: thresholds are written in. Its own calibration is the identity.
REFERENCE = SemanticCalibration(
    model=REFERENCE_MODEL,
    baseline=0.213,
    span=0.1313,
    samples=216,
    set_version="2026.09",
    dimensions=384,
)
REFERENCE_BASELINE = REFERENCE.baseline
REFERENCE_SPAN = REFERENCE.span

#: Measured calibrations, keyed by the embedding model id the platform is
#: configured with. Add an entry only from a run of
#: ``scripts/calibrate_nova_semantics.py`` against that model, on the current
#: labelled set. ``text-embedding-3-large`` is the OpenAI and Azure OpenAI
#: default; ``nomic-embed-text`` the Ollama default (its Hugging Face weights,
#: nomic-ai/nomic-embed-text-v1.5, raw text as the platform sends it).
CALIBRATIONS: dict[str, SemanticCalibration] = {
    "nomic-embed-text": SemanticCalibration(
        model="nomic-embed-text",
        baseline=0.491,
        span=0.0823,
        samples=216,
        set_version="2026.09",
        dimensions=768,
    ),
    "text-embedding-3-large": SemanticCalibration(
        model="text-embedding-3-large",
        baseline=0.2348,
        span=0.17,
        samples=216,
        set_version="2026.09",
        dimensions=3072,
    ),
}

#: Ollama tags that name the same weights as a measured model (checked against
#: the Ollama library manifests), so a tagged configuration is not reported as
#: uncalibrated. ``all-minilm`` is Ollama's packaging of the reference model.
ALIASES: dict[str, str] = {
    "nomic-embed-text:latest": "nomic-embed-text",
    "nomic-embed-text:v1.5": "nomic-embed-text",
    "nomic-embed-text:137m-v1.5-fp16": "nomic-embed-text",
    "all-minilm": REFERENCE_MODEL,
    "all-minilm:latest": REFERENCE_MODEL,
    "all-minilm:l6": REFERENCE_MODEL,
    "all-minilm:l6-v2": REFERENCE_MODEL,
    "all-minilm:v2": REFERENCE_MODEL,
    "all-minilm:22m": REFERENCE_MODEL,
}


def calibration_set_version(path: Path = CALIBRATION_SET_PATH) -> str:
    """The version of the labelled set at *path*."""
    return str(json.loads(path.read_text(encoding="utf-8"))["set_version"])


def load_calibration_set(path: Path = CALIBRATION_SET_PATH) -> list[dict]:
    """The labelled set's entries: a pattern, texts it must and must not fire on."""
    return json.loads(path.read_text(encoding="utf-8"))["entries"]


def _normalized(model: str | None) -> str:
    key = (model or "").strip().lower().rsplit("/", 1)[-1]
    return ALIASES.get(key, key).lower()


def _calibration_for(model: str | None, dimensions: int | None = None) -> SemanticCalibration | None:
    """The entry measured for *model* at *dimensions* (``None``: its native length)."""
    key = _normalized(model)
    for name, calibration in CALIBRATIONS.items():
        if _normalized(name) == key:
            if calibration.span <= 0 or dimensions not in (None, calibration.dimensions):
                return None
            return calibration
    return None


def _is_reference(model: str | None, dimensions: int | None) -> bool:
    return _normalized(model) == REFERENCE_MODEL.lower() and dimensions in (None, REFERENCE.dimensions)


def is_calibrated(model: str | None, dimensions: int | None = None) -> bool:
    """Whether scores from *model*, at *dimensions*, are on the scale the rules were written in."""
    return _is_reference(model, dimensions) or _calibration_for(model, dimensions) is not None


def calibrate_score(score: float, model: str | None, dimensions: int | None = None) -> float:
    """*score* from *model*'s vectors of length *dimensions*, on the reference model's scale.

    Unchanged for the reference model and for a model with no measured
    calibration at that length (the latter is reported, not silently re-scaled).
    """
    calibration = _calibration_for(model, dimensions)
    if calibration is None:
        return score
    relative = (score - calibration.baseline) / calibration.span
    return REFERENCE_BASELINE + relative * REFERENCE_SPAN


def calibration_status(model: str | None, dimensions: int | None = None) -> dict:
    """What the engine status, the rules tab and the Security Audit report about *model*."""
    return {
        "calibrated_for": REFERENCE_MODEL,
        "configured_model": model,
        "calibrated": is_calibrated(model, dimensions),
    }


def derive_calibration(
    model: str,
    non_matching: list[float],
    matching: list[float],
    *,
    set_version: str,
    dimensions: int,
) -> SemanticCalibration:
    """A calibration from one model's scores on the labelled set.

    *non_matching* are pattern-to-text similarities for texts a rule must not
    fire on, *matching* for texts it must. The baseline is the median
    non-matching similarity; the span is the gap to the median matching one.
    Medians, not means: one odd sample must not move a platform-wide scale.
    """
    if not non_matching or not matching:
        raise ValueError("both sample sets are needed to calibrate a model")
    baseline = statistics.median(non_matching)
    span = round(statistics.median(matching) - baseline, 4)
    if span <= 0:
        raise ValueError(
            f"{model} does not separate the labelled texts (median matching "
            f"similarity is not above the median non-matching one at 4 decimals)"
        )
    return SemanticCalibration(
        model=model,
        baseline=round(baseline, 4),
        span=span,
        samples=len(non_matching) + len(matching),
        set_version=set_version,
        dimensions=dimensions,
    )
