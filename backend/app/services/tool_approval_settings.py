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

"""The two platform tool approval switches: how they are read and changed.

``PlatformSettings.tool_approvals_enabled`` (Settings > Access and security > Tool
approvals > Suspend all approvals, off suspends every approval) and
``PlatformSettings.approve_tool_calls_default`` (Every agent asks by default).
An administrator changes them from ``PUT /settings/platform`` or, through an
agent, from ``update_platform_ai_settings``; both go through
:func:`apply_tool_approval_settings` so the validation (strict booleans: a
truthy string must not switch off the control that pauses destructive tools)
and the audit wording cannot drift between the two callers.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Field -> the value an unset column reads as (the model's own defaults).
TOOL_APPROVAL_DEFAULTS: dict[str, bool] = {
    "tool_approvals_enabled": True,
    "approve_tool_calls_default": False,
}

#: Field -> (summary when set true, summary when set false) for the audit log.
_SUMMARIES: dict[str, tuple[str, str]] = {
    "tool_approvals_enabled": (
        "Enabled tool approval pauses platform-wide",
        "Disabled tool approval pauses platform-wide",
    ),
    "approve_tool_calls_default": (
        "Every agent asks before running tools by default",
        "Agents no longer ask before running tools by default",
    ),
}

#: The audit entity name both callers write.
TOOL_APPROVALS_AUDIT_ENTITY = "Tool approvals"


@dataclass(frozen=True)
class ToolApprovalChange:
    """One switch an administrator changed, with its audit summary."""

    field: str
    value: bool
    summary: str


def serialize_tool_approval_settings(ps: object | None) -> dict[str, bool]:
    """The two switches as ``GET /settings/platform`` serves them."""
    return {field: bool(getattr(ps, field, default)) for field, default in TOOL_APPROVAL_DEFAULTS.items()}


def validate_tool_approval_settings(body: dict[str, object]) -> None:
    """Raise :class:`ValueError` naming the first switch in *body* that is not a boolean."""
    for field in TOOL_APPROVAL_DEFAULTS:
        if field in body and not isinstance(body[field], bool):
            raise ValueError(f"{field}: must be true or false")


def apply_tool_approval_settings(ps: object, body: dict[str, object]) -> list[ToolApprovalChange]:
    """Write the switches present in *body* on *ps*; return what changed.

    Every present value is validated before any is written, so a malformed
    one leaves *ps* untouched. Raises :class:`ValueError` naming the field for
    anything but ``true`` / ``false`` (the caller maps it to a 400 or an error
    line). A value equal to the stored one is not a change and is not audited.
    """
    validate_tool_approval_settings(body)
    present = [field for field in TOOL_APPROVAL_DEFAULTS if field in body]
    changes: list[ToolApprovalChange] = []
    for field in present:
        value = bool(body[field])
        if value == bool(getattr(ps, field, TOOL_APPROVAL_DEFAULTS[field])):
            continue
        setattr(ps, field, value)
        on_summary, off_summary = _SUMMARIES[field]
        changes.append(ToolApprovalChange(field=field, value=value, summary=on_summary if value else off_summary))
    return changes
