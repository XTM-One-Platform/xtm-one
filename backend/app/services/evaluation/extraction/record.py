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

"""What the extractor actually produced, persisted beside the score.

With an extractor configured, the score is computed from the EXTRACTED prediction,
not the raw agent output. Showing it lets an author tell an agent that answered badly
from a chain that selected the wrong thing, two problems with opposite fixes.

This is the success-path counterpart of ``failures.py``, and it discloses more on
purpose. A failure record carries bounded metadata only, because it is counted into
run aggregates and written to logs. This record lives in one entry's
``execution_details`` and travels exactly where ``agent_output`` already travels (same
row, same visibility gate, same export), as a projection of that raw output, so it
discloses nothing new. Never log it, never aggregate it, never put it in a failure blob.

Stored whole; the renderer bounds what a reader is shown. The extractor's limits
(``extraction_max_values`` 2000 x ``extraction_max_value_chars`` 200) put the worst case
at ~408 kB of JSON per result. That is proportionate: ``EvaluationResult.agent_output``
is an unbounded ``Text`` column (``extraction_max_source_bytes`` bounds what the
extractor reads, not what the row stores), so this record is the only half of the pair
with a ceiling.
"""

from __future__ import annotations

import json
from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Any

EXTRACTED_PREDICTION_KEY = "extracted_prediction"


def item_count(prediction: str) -> int | None:
    """How many items the prediction holds, or ``None`` when that has no meaning.

    Shared with the preview endpoint so the count shown while authoring a chain and
    the count stored beside the score are computed once.
    """
    try:
        parsed = json.loads(prediction)
    except ValueError:
        return None
    return len(parsed) if isinstance(parsed, (list, dict)) else None


@dataclass(frozen=True)
class ExtractedPrediction:
    """The prediction an extractor produced, as persisted.

    Mirrors the preview response's shape, so the stored and previewed panels read one
    structure.
    """

    prediction: str
    output_kind: str | None = None

    def as_detail(self) -> dict[str, Any]:
        return {
            "prediction": self.prediction,
            "output_kind": self.output_kind,
            "item_count": item_count(self.prediction),
        }


def record_extracted_prediction(
    details: MutableMapping[str, Any], extracted: ExtractedPrediction | None
) -> MutableMapping[str, Any]:
    """Write ``extracted`` into ``details``, or REMOVE what is already there.

    The removal is the half that matters: a re-score reads the extractor configured
    now, so after the chain is dropped or starts failing, a value left from the
    previous chain would be shown beside a fresh score as though it produced it. Both
    engine paths go through here so neither can implement only the write half.
    """
    if extracted is None:
        details.pop(EXTRACTED_PREDICTION_KEY, None)
    else:
        details[EXTRACTED_PREDICTION_KEY] = extracted.as_detail()
    return details


__all__ = [
    "EXTRACTED_PREDICTION_KEY",
    "ExtractedPrediction",
    "item_count",
    "record_extracted_prediction",
]
