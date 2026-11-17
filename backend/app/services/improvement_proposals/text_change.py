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

"""What every text change of an improvement proposal must be: targeted.

A persona, a skill and an assignment prompt are all text an agent reads on
every call, so a proposed change grows it by half at most, or by 2,000
characters for a short one (the #1044 rule, ``rewrite_length_limit``), and
never past the type's ceiling (invariant 16: no system prompt inflation). The
check is on the stored text, so it holds for a person's request and an agent's
filing alike.
"""

from __future__ import annotations

from app.services.improvement_proposals.lifecycle import ProposalError

_MAX_GROWTH_RATIO = 1.5
_MAX_GROWTH_CHARS = 2_000


def growth_limit(base: str, ceiling: int) -> int:
    """The longest change of *base* a proposal may hold."""
    return min(ceiling, max(int(len(base) * _MAX_GROWTH_RATIO), len(base) + _MAX_GROWTH_CHARS))


def check_text_change(base: str, proposed: str, *, ceiling: int, subject: str) -> None:
    """Raise ``ProposalError`` (422) unless *proposed* is a targeted change of *base*."""
    if not proposed.strip():
        raise ProposalError(422, f"The proposed {subject} is empty.")
    if proposed.strip() == base.strip():
        raise ProposalError(422, f"The proposed {subject} is the current one.")
    if len(base) > ceiling:
        raise ProposalError(422, f"The {subject} is longer than the {ceiling:,} characters a change handles.")
    if len(proposed) > growth_limit(base, ceiling):
        raise ProposalError(
            422,
            f"The proposed {subject} grows it from {len(base):,} to {len(proposed):,} characters; "
            "a change is targeted.",
        )
