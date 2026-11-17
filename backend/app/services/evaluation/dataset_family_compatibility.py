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

"""Dataset <-> evaluation family compatibility check.

One pure function for both API modules that can change a side of the link:
``datasets.py`` (linking, and a linked dataset's family change) and ``evaluations.py``
(an evaluation's family change). One rule keeps the two re-check paths from drifting.
"""

from __future__ import annotations


def check_family_compatibility(
    dataset_family: str | None, evaluation_family: str | None
) -> str | None:
    """Return a conflict message, or ``None`` when the link is allowed.

    A family-less side never conflicts: only two declared, different families do.
    """
    if not dataset_family or not evaluation_family:
        return None
    if dataset_family == evaluation_family:
        return None
    return (
        f"Dataset family '{dataset_family}' conflicts with evaluation family "
        f"'{evaluation_family}'."
    )
