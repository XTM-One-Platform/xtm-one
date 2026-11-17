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

"""Request-shaped validators for per-agent extractors.

Only the checks that belong to a request live here. The database accessors are
service-side in ``app/services/evaluation/extractor_repository`` so the engine
can read an extractor without importing the API layer; they are re-exported
below so the routes have one import site.
"""

from __future__ import annotations

from typing import Any

from app.models.evaluation import EvaluationAgent
from app.services.evaluation.extraction import (
    validate_extractor_chain,
    validate_output_kind_for_family,
)
from app.services.evaluation.extractor_repository import (
    apply_agent_extractors,
    load_agent_extractors,
    load_evaluation_agents,
)
from app.services.evaluation.extractor_repository import (
    as_extractor_dict as _as_dict,
)


def validate_agent_extractors(
    agent_extractors: dict[str, Any] | None,
    *,
    family: str | None,
    known_agent_ids: set[str],
) -> str | None:
    """Validate a submitted map. Returns an error message, or ``None``.

    Every extractor is checked *before* any row is written, so a request carrying
    one bad chain among several leaves the stored configuration untouched.
    """
    if not agent_extractors:
        return None
    for agent_id, extractor in agent_extractors.items():
        if agent_id not in known_agent_ids:
            return (
                f"agent_extractors names agent {agent_id!r}, which is not on this "
                "evaluation; add the agent first, or remove the extractor"
            )
        payload = _as_dict(extractor)
        if payload is None:
            # An explicit null clears the extractor; nothing to validate.
            continue
        problem = validate_extractor_chain(payload, family=family)
        if problem:
            return f"agent_extractors[{agent_id!r}]: {problem}"
    return None


def validate_existing_extractors_against_family(
    rows: list[EvaluationAgent], family: str | None
) -> str | None:
    """Re-check stored extractors when the evaluation's family changes.

    The family change is refused rather than left to orphan an extractor: the
    incompatible pair is rejected where it would be created, not discovered on
    the next run as a failure on every entry.
    """
    orphaned: list[str] = []
    for row in rows:
        extractor = row.output_extractor
        if not isinstance(extractor, dict):
            continue
        output_kind = str(extractor.get("output_kind", ""))
        if validate_output_kind_for_family(output_kind, family):
            orphaned.append(str(row.agent_id))
    if orphaned:
        return (
            f"changing the family to {family!r} would leave the output extractors "
            f"of these agents unsupported: {', '.join(sorted(orphaned))}. "
            "Remove or adjust them first."
        )
    return None


__all__ = [
    "apply_agent_extractors",
    "load_agent_extractors",
    "load_evaluation_agents",
    "validate_agent_extractors",
    "validate_existing_extractors_against_family",
]
