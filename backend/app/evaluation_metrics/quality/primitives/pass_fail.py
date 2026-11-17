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

"""A pass/fail metric pools one item per entry.

The entry passes (100) or fails (0), so it reports 1 or 0 of 1, and a run reports its share
of passes. Averaged less the spread penalty, a pass/fail value would read
``100 x (p - 0.5 x sqrt(p(1 - p)))`` for a share ``p``: 0 up to 20 %, whatever the
consistency it claims to measure.
"""

from app.evaluation_metrics.core.contracts import POOLED_DENOMINATOR, POOLED_NUMERATOR


def pass_fail_counts(passed: bool) -> dict[str, int]:
    """The counts of one pass/fail entry: 1 of 1 when it passed, else 0 of 1."""
    return {POOLED_NUMERATOR: int(passed), POOLED_DENOMINATOR: 1}


__all__ = ["pass_fail_counts"]
