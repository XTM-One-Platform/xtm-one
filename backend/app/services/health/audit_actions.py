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

"""The audit actions Platform Health writes (#101 S1).

Written ``health.*`` from #101 S1. The rows written before it carry the former
``concierge.*`` names: the audit log is never rewritten, so a reader that
filters on one of these actions matches both names through :func:`action_names`.
"""

from __future__ import annotations

DATASET_BUILD = "health.dataset_build"
EVALUATION_LAUNCH = "health.evaluation_launch"
DRY_RUN_WRITE = "health.dry_run_write"
SANDBOX_RUN = "health.sandbox_run"

#: Each action and the name the rows written before #101 S1 carry.
LEGACY_AUDIT_ACTIONS: dict[str, str] = {
    DATASET_BUILD: "concierge.dataset_build",
    EVALUATION_LAUNCH: "concierge.evaluation_launch",
    DRY_RUN_WRITE: "concierge.dry_run_write",
    SANDBOX_RUN: "concierge.sandbox_run",
}


def action_names(action: str) -> tuple[str, ...]:
    """*action* and its former name, for a reader of rows written on either side of the rename."""
    legacy = LEGACY_AUDIT_ACTIONS.get(action)
    return (action, legacy) if legacy else (action,)
