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

"""In-process scorer registry.

No per-scorer configuration, no sandboxed custom scorers. Deterministic quality comes
from the ``evaluation_metrics`` package through the family-driven quality adapter,
so the registry holds only the LLM judge and ``scorer_selection`` is in effect
a judge on/off toggle (see :func:`judge_enabled`).
"""

from __future__ import annotations

from typing import Final

#: The scorer keys a ``scorer_selection`` may name.
_REGISTERED: Final[frozenset[str]] = frozenset({"judge"})


def invalid_keys(selection: list[str] | None) -> list[str]:
    """Return the subset of ``selection`` that is not registered."""
    if not selection:
        return []
    return [key for key in selection if key not in _REGISTERED]


def judge_enabled(selection: list[str] | None) -> bool:
    """Whether the LLM judge runs for a given ``scorer_selection``.

    ``None`` (unset) → enabled. An explicit selection enables the judge only when it
    contains ``"judge"``, so an empty selection is judge-off. The one judge gate for the
    API and the engine.
    """
    if selection is None:
        return True
    return "judge" in selection
