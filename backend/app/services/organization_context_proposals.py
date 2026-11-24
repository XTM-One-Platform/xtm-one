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

"""Organization changes proposed from chat, reviewed by an administrator (#9484, S-a2).

The design is ``dev-docs/ctem-fleets.md`` section 6.1, "Chat". The CTEM
Assistant and the Platform Health call ``propose_organization_context_change``
for "add Salesforce to our software" or "we just acquired X, track it";
the tool stores an :class:`OrganizationContextProposal` and NEVER writes a
profile. An administrator accepts or rejects it, field by field, in
Settings > Organization: accepting saves the next profile version with
source ``ai_draft`` and the proposal's basis kept in ``sources``, or creates
the tracked organization with monitoring ``off`` (the multi-org rule).

Rules this module keeps:

- Every proposed value is validated against the contract of the
  organization it changes before it is stored; a bad one is refused with
  the field named, so a pending proposal is always applicable.
- A value a person typed is never replaced silently: a field the current
  version already holds is answered with ``conflict: true`` and is applied
  only when the administrator names it in the accepted fields.
- No model is called here: the agent's own turn already did the thinking.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization_context import OrganizationContextProposal
from app.services import organization_context as org_context

#: Most changes one proposal carries (one chat request, not an import).
MAX_CHANGES = 20
#: Most tracked organizations one proposal creates (a bulk file is S-c's import, not chat).
MAX_NEW_TRACKED = 10
BASIS_MAX_CHARS = 500
#: The source an accepted chat proposal keeps in the version's ``sources``.
CHAT_SOURCE = "chat"


class ProposalError(ValueError):
    """A proposal that cannot be stored or decided; the message names why."""


#: Policy settings of the primary profile a proposal may name besides the contract fields:
#: the CTEM watchlist's "propose to raise the scope" (CTEM-30, #937). An administrator
#: still accepts it field by field; nothing is applied on proposal.
PROPOSABLE_POLICY_PATHS: tuple[str, ...] = ("prioritization_policy.scope_min_criticality",)
#: Outside-lens settings of the primary profile an agent may set or propose (CTEM-26): the
#: owned domains, the address ranges (CIDR, validated by the profile model) and the attack
#: surface pass. They stay exposure fields: out of the always-on block and of missing_fields.
PROPOSABLE_EXPOSURE_PATHS: tuple[str, ...] = ("owned_domains", "address_ranges", "attack_surface")
#: The reference exchange rates the risk agents cite to convert a figure (#1383): an
#: administrator sets them by API or through an agent; not a contract field.
PROPOSABLE_RATE_PATHS: tuple[str, ...] = ("fx_rates",)
#: What the organization ships, with its repositories (#1432): the CTEM scope gate resolves a
#: vulnerability on one of them to its repositories. Not a contract field.
PROPOSABLE_PRODUCT_PATHS: tuple[str, ...] = ("shipped_products",)
#: The knowledge bases of the organization's procedures (#11543), linked by an agent as by the form (#1691).
#: Linking grants nothing: a reader only searches the linked bases it can read already.
PROPOSABLE_KNOWLEDGE_PATHS: tuple[str, ...] = ("enterprise_knowledge",)


def _contract_paths(role: str) -> tuple[str, ...]:
    from app.services.organizations import TRACKED_CONTRACT_FIELDS

    if role == "primary":
        if org_context.identity_only():
            # Copilot mode (#1690): an agent proposes or writes the identity alone.
            return tuple(path for path, _label in org_context.contract_fields())
        return (
            tuple(path for path, _label in org_context.CONTRACT_FIELDS)
            + PROPOSABLE_POLICY_PATHS
            + PROPOSABLE_EXPOSURE_PATHS
            + PROPOSABLE_RATE_PATHS
            + PROPOSABLE_PRODUCT_PATHS
            + PROPOSABLE_KNOWLEDGE_PATHS
        )
    return tuple(path for path, _label in TRACKED_CONTRACT_FIELDS)


def _get(data: dict[str, Any], path: str) -> Any:
    value: Any = data
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _set(data: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    head, _, tail = path.partition(".")
    out = dict(data)
    out[head] = {**(out.get(head) or {}), tail: value} if tail else value
    return out


def _is_empty(path: str, value: Any) -> bool:
    if path == "ticketing":
        return not (isinstance(value, dict) and value.get("integration") and value.get("project"))
    return value is None or value == "" or value == [] or value == {}


def validate_field_changes(organization: Any, current: dict[str, Any], changes: list[dict[str, Any]]) -> list[dict]:
    """*changes* (``[{field, value, basis}]``) checked against *organization*'s contract, values normalised.

    ``ProposalError`` names the first bad change. Each kept change carries
    ``current`` and ``conflict`` (the current version already holds a value).
    """
    from app.services.organizations import parse_for

    if not changes:
        raise ProposalError("Name at least one field to change.")
    if len(changes) > MAX_CHANGES:
        raise ProposalError(f"At most {MAX_CHANGES} fields can be proposed at once.")
    paths = _contract_paths(organization.role)
    try:
        current = parse_for(organization, current).model_dump(mode="json")
    except ValueError as exc:
        raise ProposalError(f"The current profile does not validate: {exc}") from None
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in changes:
        if not isinstance(raw, dict):
            raise ProposalError("Each change is an object with field, value and basis.")
        path = str(raw.get("field") or "").strip()
        if path not in paths:
            raise ProposalError(f"'{path}' is not a field of this organization's profile ({', '.join(paths)}).")
        if path in seen:
            raise ProposalError(f"'{path}' is proposed twice.")
        seen.add(path)
        try:
            normalised = _get(
                parse_for(organization, _set(current, path, raw.get("value"))).model_dump(mode="json"), path
            )
        except ValueError as exc:
            raise ProposalError(f"'{path}': {exc}") from None
        current_value = _get(current, path)
        if _is_empty(path, normalised) or normalised == current_value:
            raise ProposalError(f"'{path}': the proposed value is empty or already the current one.")
        kept.append(
            {
                "field": path,
                "value": normalised,
                "basis": str(raw.get("basis") or "").strip()[:BASIS_MAX_CHARS],
                "current": current_value,
                "conflict": not _is_empty(path, current_value),
            }
        )
    return kept


def validate_new_tracked(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """New tracked organizations (``[{name, relationship, criticality, basis}]``), checked."""
    from app.services.organizations import check_criticality, check_relationship

    if not entries:
        raise ProposalError("Name at least one organization to track.")
    if len(entries) > MAX_NEW_TRACKED:
        raise ProposalError(f"At most {MAX_NEW_TRACKED} organizations can be proposed at once from chat.")
    kept: list[dict[str, Any]] = []
    for raw in entries:
        name = str((raw or {}).get("name") or "").strip()[:200]
        if not name:
            raise ProposalError("Each organization to track needs a name.")
        try:
            relationship = check_relationship(str(raw.get("relationship") or "").strip())
            criticality = check_criticality(str(raw.get("criticality") or "medium").strip())
        except ValueError as exc:
            raise ProposalError(f"{name}: {exc}") from None
        kept.append(
            {
                "name": name,
                "relationship": relationship,
                "criticality": criticality,
                "basis": str(raw.get("basis") or "").strip()[:BASIS_MAX_CHARS],
            }
        )
    return kept


async def propose(
    db: AsyncSession,
    *,
    author_user_id: uuid.UUID | None,
    organization: str = "",
    changes: list[dict[str, Any]] | None = None,
    new_tracked: list[dict[str, Any]] | None = None,
    conversation_id: uuid.UUID | None = None,
) -> OrganizationContextProposal:
    """Store one pending proposal (the caller commits); nothing else is written.

    *changes* change the profile of *organization* (the primary when empty);
    *new_tracked* proposes organizations to track. Exactly one of them.
    """
    from app.services import organizations as orgs

    if bool(changes) == bool(new_tracked):
        raise ProposalError("Propose either field changes or organizations to track, one of the two.")
    if new_tracked:
        row = OrganizationContextProposal(
            organization_id=None,
            kind="new_tracked",
            proposals=validate_new_tracked(new_tracked),
            author_user_id=author_user_id,
            conversation_id=conversation_id,
        )
    else:
        target = await orgs.resolve(db, organization)
        if target is None:
            if organization.strip():
                raise ProposalError(f"No active organization is named {organization!r} (or several are).")
            target = await orgs.ensure_primary(db)
        current = await org_context.current_version(db, target.id)
        current_profile = orgs.parse_for(target, current.profile if current else {}).model_dump(mode="json")
        row = OrganizationContextProposal(
            organization_id=target.id,
            kind="fields",
            proposals=validate_field_changes(target, current_profile, list(changes or [])),
            based_on_version=current.version if current else None,
            author_user_id=author_user_id,
            conversation_id=conversation_id,
        )
    db.add(row)
    await db.flush()
    return row


#: The ``sources`` entry a profile version written by an agent keeps (``update_organization_context``).
AGENT_SOURCE_PREFIX = "agent:"


async def apply_changes(
    db: AsyncSession,
    *,
    author_user_id: uuid.UUID,
    organization: str = "",
    changes: list[dict[str, Any]] | None = None,
    agent_name: str = "",
    based_on_version: int | None = None,
) -> dict[str, Any]:
    """Apply *changes* to *organization*'s profile at once, as a new version (the caller commits).

    The direct path of ``update_organization_context`` (#11594): the caller
    already checked the acting person is an administrator. Every change is
    validated as a proposal is (the contract of the organization, one field
    once, a value that changes something); a field a person filled is
    replaced, the administrator having asked for it. The version is saved
    with source ``ai_draft``, authored by *author_user_id*, the agent and
    the basis kept per field in ``sources``, so Settings > Organization
    shows who changed what and can roll it back. Answers
    ``{organization_id, organization, version, fields}``. *based_on_version*
    given and another version saved since refuses, nothing saved (#1676, the
    guard of the Settings > Organization PUT, #1473).
    """
    from app.services import organizations as orgs

    target = await orgs.resolve(db, organization)
    if target is None:
        if organization.strip():
            raise ProposalError(f"No active organization is named {organization!r} (or several are).")
        target = await orgs.ensure_primary(db)
    current = await org_context.current_version(db, target.id)
    latest = current.version if current is not None else None
    if based_on_version is not None and latest != based_on_version:
        raise ProposalError(
            f"the profile changed since version {based_on_version} (now version {latest}). "
            "Read get_organization_context again and apply your change on the new version."
        )
    data = orgs.parse_for(target, current.profile if current else {}).model_dump(mode="json")
    kept = validate_field_changes(target, data, list(changes or []))
    source = f"{AGENT_SOURCE_PREFIX}{agent_name}".strip()[:300] if agent_name.strip() else CHAT_SOURCE
    sources: dict[str, dict[str, str]] = {}
    for entry in kept:
        data = _set(data, entry["field"], entry["value"])
        sources[entry["field"]] = {"source": source, "basis": entry.get("basis") or ""}
    try:
        profile = orgs.parse_for(target, data)
    except ValueError as exc:
        raise ProposalError(str(exc)) from None
    row = await org_context.save_version(
        db,
        profile,
        source="ai_draft",
        author_user_id=author_user_id,
        organization_id=target.id,
        sources=org_context.accepted_sources(sources) if target.role == "primary" else sources,
    )
    return {
        "organization_id": str(target.id),
        "organization": target.name,
        "version": row.version,
        "fields": [entry["field"] for entry in kept],
    }


#: Most rows one tracked-organizations AI draft stores (section 6.1: 50 names per run).
MAX_TRACKED_DRAFT_ROWS = 50


async def store_tracked_draft(
    db: AsyncSession, *, author_user_id: uuid.UUID | None, rows: list[dict[str, Any]]
) -> OrganizationContextProposal:
    """Store the rows of a tracked-organizations AI draft as one pending ``new_tracked`` proposal.

    The rows come from ``organization_tracked_ai_draft.draft_rows`` (already
    validated); a drafted row's relationship and criticality are checked
    again here so a pending proposal is always applicable. Nothing is created
    (the caller commits).
    """
    from app.services.organizations import check_criticality, check_relationship

    if not rows:
        raise ProposalError("Name at least one organization to draft.")
    if len(rows) > MAX_TRACKED_DRAFT_ROWS:
        raise ProposalError(f"At most {MAX_TRACKED_DRAFT_ROWS} organizations can be drafted at once.")
    for row in rows:
        if row.get("status") == "pending":
            try:
                check_relationship(str(row.get("relationship") or ""))
                check_criticality(str(row.get("criticality") or ""))
            except ValueError as exc:
                raise ProposalError(f"{row.get('name')}: {exc}") from None
    stored = OrganizationContextProposal(
        organization_id=None,
        kind="new_tracked",
        proposals=rows,
        author_user_id=author_user_id,
    )
    db.add(stored)
    await db.flush()
    return stored


async def list_proposals(
    db: AsyncSession, *, status: str = "pending", limit: int = 50
) -> list[OrganizationContextProposal]:
    """Proposals in *status*, newest first."""
    rows = await db.execute(
        select(OrganizationContextProposal)
        .where(OrganizationContextProposal.status == status)
        .order_by(OrganizationContextProposal.created_at.desc())
        .limit(max(1, min(limit, 200)))
    )
    return list(rows.scalars().all())


async def decide(
    db: AsyncSession,
    proposal: OrganizationContextProposal,
    *,
    admin_user_id: uuid.UUID,
    accept: list[str],
    opencti_ids: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Apply the accepted entries of a pending *proposal* and record the decision (the caller commits).

    *accept* names the fields (``fields``) or the organization names
    (``new_tracked``) the administrator accepted; an empty list rejects the
    proposal. *opencti_ids* (``new_tracked``) maps an accepted name to the
    OpenCTI Organization the administrator picked, over the one the draft
    pre-set; a row already tracked is refused, never created twice; a row
    drafted with a sector or regions gets its first profile version (source
    ``ai_draft``). A field is re-validated against the CURRENT version, so a
    profile saved since the proposal is never overwritten by a stale value
    it did not name. Answers ``{status, version, created}``.
    """
    from app.services import organizations as orgs

    if proposal.status != "pending":
        raise ProposalError(f"This proposal is already {proposal.status}.")
    wanted = {str(a).strip() for a in accept if str(a).strip()}
    outcome: dict[str, Any] = {"status": "rejected", "version": None, "created": []}
    if proposal.kind == "fields" and wanted:
        target = await orgs.get_organization(db, proposal.organization_id) if proposal.organization_id else None
        if target is None or target.archived_at is not None:
            raise ProposalError("The organization this proposal changes no longer exists.")
        unknown = wanted - {p["field"] for p in proposal.proposals}
        if unknown:
            raise ProposalError(f"Not in this proposal: {', '.join(sorted(unknown))}.")
        current = await org_context.current_version(db, target.id)
        data = orgs.parse_for(target, current.profile if current else {}).model_dump(mode="json")
        sources: dict[str, dict[str, str]] = {}
        for entry in proposal.proposals:
            if entry["field"] in wanted:
                data = _set(data, entry["field"], entry["value"])
                sources[entry["field"]] = {"source": CHAT_SOURCE, "basis": entry.get("basis") or ""}
        try:
            profile = orgs.parse_for(target, data)
        except ValueError as exc:
            raise ProposalError(str(exc)) from None
        if isinstance(profile, org_context.OrganizationProfile):
            # An accepted PIR is stored with its OpenCTI name, never its id alone (#11589).
            from app.services.organization_context_draft import name_saved_pirs

            profile = await name_saved_pirs(db, user_id=admin_user_id, profile=profile)
        row = await org_context.save_version(
            db,
            profile,
            source="ai_draft",
            author_user_id=admin_user_id,
            organization_id=target.id,
            sources=org_context.accepted_sources(sources) if target.role == "primary" else sources,
        )
        outcome = {"status": "accepted", "version": row.version, "created": []}
    elif proposal.kind == "new_tracked" and wanted:
        unknown = wanted - {p["name"] for p in proposal.proposals}
        if unknown:
            raise ProposalError(f"Not in this proposal: {', '.join(sorted(unknown))}.")
        conflicts = sorted(
            e["name"] for e in proposal.proposals if e["name"] in wanted and e.get("already_tracked_id")
        )
        if conflicts:
            raise ProposalError(f"Already tracked: {', '.join(conflicts)}.")
        failed = sorted(e["name"] for e in proposal.proposals if e["name"] in wanted and e.get("error"))
        if failed:
            raise ProposalError(f"Not drafted (the batch failed): {', '.join(failed)}.")
        picked = {str(k).strip(): str(v or "").strip()[:100] for k, v in (opencti_ids or {}).items()}
        created = []
        for entry in proposal.proposals:
            if entry["name"] not in wanted:
                continue
            organization = await orgs.create_tracked(
                db,
                name=entry["name"],
                relationship=entry["relationship"],
                criticality=entry["criticality"],
                opencti_organization_id=picked.get(entry["name"], entry.get("opencti_organization_id") or None)
                or None,
            )
            created.append(str(organization.id))
            drafted = {k: entry[k] for k in ("sector", "regions") if entry.get(k)}
            if drafted:
                await org_context.save_version(
                    db,
                    orgs.parse_tracked_profile(drafted),
                    source="ai_draft",
                    author_user_id=admin_user_id,
                    organization_id=organization.id,
                    sources={k: {"source": "ai_draft", "basis": entry.get("basis") or ""} for k in drafted},
                )
        # Each row keeps its own decision, so the review page shows what was created.
        proposal.proposals = [
            {**e, "status": "accepted" if e["name"] in wanted else "rejected"}
            if e.get("status", "pending") == "pending"
            else e
            for e in proposal.proposals
        ]
        outcome = {"status": "accepted", "version": None, "created": created}
    proposal.status = outcome["status"]
    proposal.decided_at = datetime.now(UTC)
    proposal.decided_by_user_id = admin_user_id
    await db.flush()
    return outcome


def render_for_agent(proposal: OrganizationContextProposal) -> str:
    """What the chat tool answers: the proposal, its id and where an administrator reviews it."""
    lines = [f"Proposal {proposal.id} recorded, pending review. Nothing is saved until an administrator accepts it."]
    for entry in proposal.proposals:
        if proposal.kind == "fields":
            flag = " (replaces a value already set: accepted only if named explicitly)" if entry.get("conflict") else ""
            lines.append(f"- {entry['field']}: {entry['value']!r}{flag}")
        else:
            lines.append(f"- track {entry['name']} ({entry['relationship']}, {entry['criticality']}), monitoring off")
    lines.append(f"Review it in Settings > Organization: /settings/organization?proposal={proposal.id}")
    return "\n".join(lines)


__all__ = [
    "AGENT_SOURCE_PREFIX",
    "CHAT_SOURCE",
    "MAX_CHANGES",
    "MAX_NEW_TRACKED",
    "MAX_TRACKED_DRAFT_ROWS",
    "PROPOSABLE_EXPOSURE_PATHS",
    "PROPOSABLE_KNOWLEDGE_PATHS",
    "PROPOSABLE_POLICY_PATHS",
    "ProposalError",
    "apply_changes",
    "decide",
    "list_proposals",
    "propose",
    "render_for_agent",
    "store_tracked_draft",
    "validate_field_changes",
    "validate_new_tracked",
]
