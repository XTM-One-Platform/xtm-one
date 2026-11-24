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

"""The conversational onboarding of the organization profile (#11224).

After "Draft with AI" saves what it could, a chat with the person's default
assistant interviews an administrator for the rest: one question (or one
natural group, currency then hourly rate) at a time, each answer saved as a
new version, until every contract field is filled or explicitly skipped.

The state is the profile's, never the chat's: the next question is the first
group of :data:`ONBOARDING_GROUPS` with a field that is still missing and not
skipped, so leaving the conversation and coming back (or "Continue
onboarding" on the profile page) resumes at the right field. The skipped
fields are ``OrganizationProfile.onboarding_skipped``, a non-contract field
saved with the profile; a field saved later leaves that list.
"""

from __future__ import annotations

from typing import Any

from app.services import organization_context as org_context

#: The interview order: contract paths, grouped where one question asks both.
ONBOARDING_GROUPS: tuple[tuple[str, ...], ...] = (
    ("name",),
    ("sector",),
    ("regions",),
    ("size",),
    ("crown_jewels",),
    ("tech_stack",),
    ("pir_ids",),
    ("currency", "loaded_hourly_rate"),
    ("risk_appetite.annual_loss_threshold", "risk_appetite.approval_threshold"),
    ("ticketing",),
)

#: What a person is asked for each field, and the shape the tool expects.
FIELD_HINTS: dict[str, str] = {
    "name": "the organization's name (text)",
    "sector": "the sector, e.g. Finance, Energy, Technology (text; sub_sector optional)",
    "regions": 'the countries of operation (list of ISO 3166-1 alpha-2 codes, e.g. ["FR", "US"])',
    "size": "the number of employees (one of 1-50, 51-250, 251-1000, 1001-5000, 5001-20000, 20000+)",
    "crown_jewels": (
        "the crown jewels and the business services they carry (list of {name, business_service, owner, "
        "criticality: low|medium|high|critical})"
    ),
    "tech_stack": "the main products in use (list of {product, vendor})",
    "pir_ids": "the OpenCTI priority intelligence requirements to follow (list of OpenCTI PIR ids)",
    "currency": "the currency of every amount (ISO 4217 code, e.g. EUR)",
    "loaded_hourly_rate": "the loaded cost of one hour of work, in that currency (number)",
    "risk_appetite.annual_loss_threshold": "the annual loss one scenario may reach before it matters (number)",
    "risk_appetite.approval_threshold": "the amount above which a decision is escalated for approval (number)",
    "ticketing": "where agents open tickets ({integration, project})",
}

_LABELS: dict[str, str] = dict(org_context.CONTRACT_FIELDS)


def _paths() -> frozenset[str]:
    """The paths the interview asks and saves: the identity alone in Copilot mode (#1690)."""
    return frozenset(path for path, _label in org_context.contract_fields())


class OnboardingError(ValueError):
    """An answer that cannot be saved; the message names the field and why."""


def remaining_fields(profile: org_context.OrganizationProfile) -> list[str]:
    """The contract fields still missing and not skipped, in contract order."""
    skipped = set(profile.onboarding_skipped)
    return [path for path in org_context.missing_fields(profile) if path not in skipped]


def next_group(profile: org_context.OrganizationProfile) -> list[str]:
    """The fields the next question asks for (``[]`` when the onboarding is complete)."""
    remaining = set(remaining_fields(profile))
    for group in ONBOARDING_GROUPS:
        fields = [path for path in group if path in remaining]
        if fields:
            return fields
    return []


def _set(data: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    head, _, tail = path.partition(".")
    out = dict(data)
    out[head] = {**(out.get(head) or {}), tail: value} if tail else value
    return out


def apply_answers(profile: org_context.OrganizationProfile, answers: dict[str, Any]) -> org_context.OrganizationProfile:
    """*profile* with *answers* (contract path -> value) set and validated; a saved field leaves the skipped list.

    ``sub_sector`` rides with ``sector``. ``OnboardingError`` names the first
    field that is not a contract field, is empty, or does not validate.
    """
    if not answers:
        raise OnboardingError("Name at least one field to save.")
    data = profile.model_dump(mode="json")
    saved: list[str] = []
    for raw_path, value in answers.items():
        path = str(raw_path).strip()
        if path == "sub_sector":
            data["sub_sector"] = value
            continue
        if path not in _paths():
            raise OnboardingError(f"'{path}' is not a field of the organization profile ({', '.join(sorted(_paths()))}).")
        data = _set(data, path, value)
        saved.append(path)
    try:
        updated = org_context.parse_profile(data)
    except ValueError as exc:
        raise OnboardingError(str(exc)) from None
    still_missing = set(org_context.missing_fields(updated))
    empty = [path for path in saved if path in still_missing]
    if empty:
        raise OnboardingError(f"'{empty[0]}': the value is empty or incomplete; ask again or skip it.")
    updated.onboarding_skipped = [path for path in updated.onboarding_skipped if path not in saved]
    return updated


def skip_fields(profile: org_context.OrganizationProfile, fields: list[str]) -> org_context.OrganizationProfile:
    """*profile* with *fields* marked skipped (contract paths only; a filled field is not skipped)."""
    paths = [str(path).strip() for path in fields if str(path).strip()]
    if not paths:
        raise OnboardingError("Name at least one field to skip.")
    unknown = [path for path in paths if path not in _paths()]
    if unknown:
        raise OnboardingError(f"'{unknown[0]}' is not a field of the organization profile.")
    missing = set(org_context.missing_fields(profile))
    skipped = list(profile.onboarding_skipped)
    for path in paths:
        if path in missing and path not in skipped:
            skipped.append(path)
    return profile.model_copy(update={"onboarding_skipped": skipped})


def render_step(
    profile: org_context.OrganizationProfile, *, version: int | None, saved: list[str] | None = None
) -> str:
    """What the assistant reads after each step: what was saved, where the profile stands, what to ask next."""
    total = len(org_context.contract_fields())
    filled = total - len(org_context.missing_fields(profile))
    lines: list[str] = []
    if saved:
        lines.append(f"Saved as version {version}: {', '.join(_LABELS.get(p, p) for p in saved)}.")
    elif version is not None:
        lines.append(f"Current profile: version {version}.")
    else:
        lines.append("No profile is saved yet.")
    lines.append(f"Completeness: {filled} of {total} fields filled.")
    if profile.onboarding_skipped:
        lines.append("Skipped by the user: " + ", ".join(_LABELS.get(p, p) for p in profile.onboarding_skipped) + ".")
    group = next_group(profile)
    if not group:
        lines.append(
            "The onboarding is complete: every field is filled or skipped. End with a one-line summary and the "
            "link to the profile: /settings/organization."
        )
        return "\n".join(lines)
    lines.append("Ask next, in one question: " + "; ".join(f"{path}: {FIELD_HINTS[path]}" for path in group) + ".")
    lines.append(
        "Propose a value when the conversation or the saved profile lets you infer one, and let the user confirm. "
        'Save with action="save"; on "skip" or "not now" call action="skip" for these fields.'
    )
    return "\n".join(lines)


__all__ = [
    "FIELD_HINTS",
    "ONBOARDING_GROUPS",
    "OnboardingError",
    "apply_answers",
    "next_group",
    "remaining_fields",
    "render_step",
    "skip_fields",
]
