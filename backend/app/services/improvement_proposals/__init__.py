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

"""Improvement proposals (#8256, PRD ``platform-concierge.md`` section 6.10).

A typed, evidenced change to a production target - persona, skill, assignment
prompt, flow - filed by a person (the #1044 persona rewrite) or by the
Platform Health's Experimenter, approved by the people who manage the target,
watched, and rolled back on regression. ``lifecycle`` holds the rules every
type shares, ``registry`` what each type does to its target.
"""

# Imported for its sandbox reference check: the copy a candidate run measures
# outlives its expiry until the run is read.
from app.services.improvement_proposals import candidate as _candidate  # noqa: F401
from app.services.improvement_proposals.dedup import dedup_key, evidence_fingerprint
from app.services.improvement_proposals.lifecycle import (
    AUDIT_ENTITY,
    FiledProposal,
    ProposalError,
    ProposalSpec,
    SettleResult,
    any_surface_enabled,
    approval_error,
    approve,
    blocking_proposal_on,
    can_read,
    file_proposal,
    open_proposal_on,
    reject,
    set_proposal_status,
    settle,
    settle_open_proposals,
    settle_status,
    surface_enabled,
    surface_filter,
    watch_days,
)
from app.services.improvement_proposals.registry import (
    Applied,
    LiveVerdict,
    ProposalHandler,
    TargetInfo,
    handler_for,
    handler_for_target,
    register_live_watcher,
    register_proposal_type,
)

__all__ = [
    "AUDIT_ENTITY",
    "Applied",
    "FiledProposal",
    "LiveVerdict",
    "ProposalError",
    "ProposalHandler",
    "ProposalSpec",
    "SettleResult",
    "TargetInfo",
    "any_surface_enabled",
    "approval_error",
    "approve",
    "blocking_proposal_on",
    "can_read",
    "dedup_key",
    "evidence_fingerprint",
    "file_proposal",
    "handler_for",
    "handler_for_target",
    "open_proposal_on",
    "register_live_watcher",
    "register_proposal_type",
    "reject",
    "set_proposal_status",
    "settle",
    "settle_open_proposals",
    "settle_status",
    "surface_enabled",
    "surface_filter",
    "watch_days",
]
