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

"""Fleet as code: export a fleet as a spec, apply one.

A fleet spec is the agent bundle format wrapped in a fleet envelope, never a
second portability layer (``dev-docs/rfc/fleets.md`` section 9)::

    apiVersion: xtm-one/fleet/v1
    kind: Fleet
    metadata: {slug, name, description, icon, color, workspace}
    spec:
      orchestrator: <member key>
      skills: [...]                      # the Fleet Card skills
      accepted_handover_types: [...]     # null: all of them
      autonomy_level: full_gates | medium | low | autonomous   # optional (#379)
      member_defaults: {context_inclusion, collaboration_instruction, organization_scope}
      members:
        - key: <agent slug>
          role_label: ...
          organization_scope?: {mode: primary | monitored | list, organization_ids?}
          parent: <member key> | null    # layout only, as on the fleet page
          exposed: true | false
          handovers: [{to: <member key>, type: delegate | consult | transfer | mandatory_transfer}]
          agent: <native agent bundle> | {ref: {slug | id}}
      connected:
        - {agent: {slug, id}, collaboration_instruction?, context_inclusion?, organization_scope?}
        - {fleet: {slug}, collaboration_instruction?, context_inclusion?, organization_scope?}

* **Defaults and overrides.** ``member_defaults`` carries what an entry
  inherits; an entry names only the settings it overrides. A field absent
  from an entry inherits on apply, so an export round-trips. An
  ``organization_scope`` of ``list`` mode names organization ids of the
  exporting instance: on another instance an id it does not know is
  reported as a warning and the entry keeps inheriting.

* **Keys are agent slugs.** A member key is its agent's slug, unique on the
  instance; apply adopts an agent the caller may manage under that slug, and
  creates one from the member's bundle only when none exists. An adopted agent
  is never edited: its persona, tools and handovers stay as they are, and a
  difference with the spec is reported as a warning.
* **Idempotent.** The fleet is found by slug among the caller's own fleets; a
  second apply of the same spec answers every object ``unchanged``. Home
  members the spec does not name are removed from the fleet (the agents are
  kept), so the spec is the fleet's desired state.
* **Dry run first.** ``dry_run=True`` (the default everywhere) runs the whole
  apply inside a savepoint and rolls it back: the plan is exact, nothing is
  written.
* **Nothing dropped silently.** A reference that cannot be resolved for the
  caller (an agent they cannot manage, one already home in another fleet, an
  unknown connected agent) is listed and blocks the apply unless
  ``allow_partial``.
* **Personal fleets only.** A spec applies to the caller's personal fleet:
  over a company-managed fleet of the same slug it is refused whole (its
  company-managed members could not be adopted, so the plan would strip it).
* **No secrets.** The bundles are the ones ``build_agent_export_bundle``
  writes: secret values never travel. Intra-fleet handovers are carried by
  member key, never by the exporting instance's agent ids.
* **Structure without bundles.** ``export_fleet_spec(bundles=False)``
  (and :func:`structural_spec` on a full spec) carries every member as a
  reference (``agent.ref.slug``) with its role label, parent, exposure and
  handovers: a few kilobytes where the bundles of a fleet of long personas
  weigh megabytes. It is a valid spec: applied, each member is adopted by
  slug, so on the live fleet every member answers ``unchanged``.

The rules a fleet write follows stay in :mod:`app.services.fleets`; this module
calls them, so a refusal reads the same as on the REST routes and the tools.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.fleet import (
    FLEET_CONTEXT_INCLUSIONS,
    FLEET_DEFAULT_ICON,
    FLEET_ROLE_CONNECTED,
    FLEET_ROLE_MEMBER,
    Fleet,
)
from app.models.user import User
from app.models.workspace import Workspace
from app.services.fleet_autonomy import AUTONOMY_LEVELS, effective_level, is_raise
from app.services.fleets import (
    FLEET_HEALTH_ANSWERED_NAME,
    FLEET_HEALTH_CHECK_NAME,
    FLEET_PAST_DUE_NAME,
    MEMBER_SETTING_FIELDS,
    FleetActionRefusedError,
    add_connected,
    add_home_member,
    check_scope_organizations,
    create_fleet,
    ensure_workspace_can_host,
    fleet_members,
    home_fleets_of,
    normalize_accepted_handover_types,
    normalize_organization_scope,
    remove_member,
    resolve_organization_scope,
    set_member_defaults,
    set_orchestrator,
    update_member,
)

FLEET_SPEC_API_VERSION = "xtm-one/fleet/v1"
FLEET_SPEC_KIND = "Fleet"
#: The handover types an agent's ``handover_types`` map may name.
SPEC_HANDOVER_TYPES: tuple[str, ...] = ("delegate", "consult", "transfer", "mandatory_transfer")
#: The most members one spec may carry.
SPEC_MAX_MEMBERS = 100
#: Bundle keys the spec leaves out: not reproducible (a timestamp) or carried by
#: member key in ``handovers`` instead of the exporting instance's agent ids.
_BUNDLE_VOLATILE_KEYS = ("exported_at",)
_AGENT_ID_KEYED_FIELDS = ("handover_types", "handover_conditions")


# ── Export ───────────────────────────────────────────────────────────────────


async def export_fleet_spec(
    db: AsyncSession,
    fleet: Fleet,
    user: User,
    user_group_ids: list[str],
    *,
    bundles: bool = True,
) -> dict[str, Any]:
    """The spec of *fleet* as *user* may read it (the caller checked visibility).

    A member agent the caller may manage travels as its native bundle; one they
    only see travels as a reference (``agent.ref.slug``), so an export never
    hands out the configuration of an agent its reader could not open.
    ``bundles=False`` carries every member as a reference (:func:`structural_spec`).
    """
    from app.api.v1.helpers import can_manage, user_visible_filter
    from app.services.agent_bundle_projection import build_agent_export_bundle

    rows = await fleet_members(db, fleet.id)
    homes = [r for r in rows if r.role == FLEET_ROLE_MEMBER and r.agent_id is not None]
    agent_ids = [r.agent_id for r in rows if r.agent_id is not None]
    agents: dict[uuid.UUID, Agent] = {}
    if agent_ids:
        found = await db.execute(
            select(Agent).where(
                Agent.id.in_(agent_ids),
                user_visible_filter(Agent, user.id, user_group_ids, is_admin=user.is_admin),
            )
        )
        agents = {a.id: a for a in found.scalars().all()}

    key_by_agent: dict[uuid.UUID, str] = {a.id: a.slug for a in agents.values()}
    key_by_member: dict[uuid.UUID, str] = {r.id: key_by_agent[r.agent_id] for r in homes if r.agent_id in key_by_agent}
    home_agent_ids = {r.agent_id for r in homes}

    members: list[dict[str, Any]] = []
    for row in homes:
        agent = agents.get(row.agent_id)  # type: ignore[arg-type]
        if agent is None:
            # A member the reader cannot see: its id only, flagged so an apply
            # lists it as unresolved rather than guessing.
            members.append({"key": str(row.agent_id), "agent": {"ref": {"id": str(row.agent_id)}}, "hidden": True})
            continue
        types = agent.handover_types or {}
        handovers = [
            {"to": key_by_agent[target], "type": types.get(str(target)) or "delegate"}
            for target in _as_uuids(agent.handover_agent_ids)
            if target in home_agent_ids and target in key_by_agent
        ]
        entry: dict[str, Any] = {
            "key": agent.slug,
            "role_label": row.role_label or "",
            "parent": key_by_member.get(row.parent_member_id) if row.parent_member_id else None,
            "exposed": bool(row.is_exposed),
            "handovers": handovers,
        }
        if row.organization_scope is not None:
            # Only an override; a member without one inherits member_defaults.
            entry["organization_scope"] = resolve_organization_scope(None, row.organization_scope)
        if bundles and can_manage(user, agent, user_group_ids):
            entry["agent"] = _portable_bundle(await build_agent_export_bundle(db, agent))
        else:
            entry["agent"] = {"ref": {"slug": agent.slug}}
        members.append(entry)

    connected: list[dict[str, Any]] = []
    target_fleets: dict[uuid.UUID, Fleet] = {}
    fleet_ids = [r.target_fleet_id for r in rows if r.role == FLEET_ROLE_CONNECTED and r.target_fleet_id]
    if fleet_ids:
        found_fleets = await db.execute(select(Fleet).where(Fleet.id.in_(fleet_ids)))
        target_fleets = {f.id: f for f in found_fleets.scalars().all()}
    for row in rows:
        if row.role != FLEET_ROLE_CONNECTED:
            continue
        # Only the settings the row overrides; the others inherit member_defaults.
        link: dict[str, Any] = {
            name: getattr(row, name) for name in MEMBER_SETTING_FIELDS if getattr(row, name) is not None
        }
        if row.agent_id is not None:
            agent = agents.get(row.agent_id)
            link["agent"] = {"slug": agent.slug} if agent is not None else {"id": str(row.agent_id)}
        else:
            target = target_fleets.get(row.target_fleet_id)  # type: ignore[arg-type]
            link["fleet"] = {"slug": target.slug} if target is not None else {"id": str(row.target_fleet_id)}
        connected.append(link)

    workspace = await db.get(Workspace, fleet.workspace_id)
    orchestrator = key_by_agent.get(fleet.orchestrator_agent_id) if fleet.orchestrator_agent_id else None
    return {
        "apiVersion": FLEET_SPEC_API_VERSION,
        "kind": FLEET_SPEC_KIND,
        "metadata": {
            "slug": fleet.slug,
            "name": fleet.name,
            "description": fleet.description or "",
            "icon": fleet.icon or FLEET_DEFAULT_ICON,
            "color": fleet.color or "",
            "workspace": workspace.name if workspace is not None else None,
        },
        "spec": {
            "orchestrator": orchestrator,
            "skills": fleet.skills,
            "accepted_handover_types": fleet.accepted_handover_types,
            "autonomy_level": effective_level(fleet.autonomy_level),
            "member_defaults": {
                "context_inclusion": fleet.default_context_inclusion or "none",
                "collaboration_instruction": fleet.default_collaboration_instruction or "",
                "organization_scope": resolve_organization_scope(fleet.organization_scope, None),
            },
            "members": members,
            "connected": connected,
        },
    }


def structural_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """*spec* with every member's agent bundle replaced by a reference.

    The envelope, the fleet's metadata, the orchestrator, each member's key,
    role label, parent, exposure and handovers, and the connected entries are
    kept as they are; a bundle becomes ``{ref: {slug}}`` (the bundle's slug,
    else the member key) and a reference is kept. *spec* is not modified.
    """
    body = spec.get("spec") if isinstance(spec.get("spec"), dict) else {}
    members: list[Any] = []
    for member in body.get("members") or []:
        if not isinstance(member, dict):
            members.append(member)
            continue
        entry = dict(member)
        agent = member.get("agent")
        if isinstance(agent, dict) and not isinstance(agent.get("ref"), dict):
            inner = agent.get("agent") if isinstance(agent.get("agent"), dict) else agent
            entry["agent"] = {"ref": {"slug": str(inner.get("slug") or member.get("key") or "")}}
        members.append(entry)
    out = dict(spec)
    out["spec"] = {**body, "members": members}
    return out


def _portable_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """The bundle as a spec carries it: reproducible, no exporting-instance agent ids."""
    out = {k: v for k, v in bundle.items() if k not in _BUNDLE_VOLATILE_KEYS}
    agent = dict(out.get("agent") or {})
    for name in _AGENT_ID_KEYED_FIELDS:
        agent.pop(name, None)
    out["agent"] = agent
    if isinstance(out.get("assignments"), list):
        # The weekly fleet health check (#11700) names the exporting fleet's id and
        # is re-created on the orchestrator by the apply: it never travels, nor its
        # answer listener (#1406), nor its overdue items listener (#1662).
        out["assignments"] = [a for a in out["assignments"] if not _is_fleet_health_check_entry(a)]
    return out


def _is_fleet_health_check_entry(entry: object) -> bool:
    """Whether a bundle's assignment entry is a fleet's weekly built-in health check."""
    if not isinstance(entry, dict):
        return False
    if (
        entry.get("name") in (FLEET_HEALTH_ANSWERED_NAME, FLEET_PAST_DUE_NAME)
        and entry.get("trigger_type") == "platform_event"
    ):
        return True
    check = entry.get("activation_check")
    return (
        entry.get("name") == FLEET_HEALTH_CHECK_NAME
        and isinstance(check, dict)
        and check.get("tool") == "get_fleet_health"
    )


def _as_uuids(raw: object) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for item in raw or []:  # type: ignore[union-attr]
        try:
            value = item if isinstance(item, uuid.UUID) else uuid.UUID(str(item))
        except (TypeError, ValueError):
            continue
        if value not in out:
            out.append(value)
    return out


# ── Validation ───────────────────────────────────────────────────────────────


class FleetSpecError(ValueError):
    """A spec that is not a fleet spec; nothing was read from the database."""


def parse_fleet_spec_text(text: str) -> dict[str, Any]:
    """A spec from YAML or JSON text (JSON is YAML); refuses anything else."""
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise FleetSpecError(f"The spec is neither YAML nor JSON: {exc}") from None
    if not isinstance(data, dict):
        raise FleetSpecError("The spec must be a mapping with apiVersion, kind, metadata and spec.")
    return data


def validate_fleet_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Check the envelope and every key reference; answers the normalised spec."""
    if spec.get("apiVersion") != FLEET_SPEC_API_VERSION:
        raise FleetSpecError(f"apiVersion must be {FLEET_SPEC_API_VERSION}.")
    if spec.get("kind") != FLEET_SPEC_KIND:
        raise FleetSpecError(f"kind must be {FLEET_SPEC_KIND}.")
    metadata = spec.get("metadata")
    if not isinstance(metadata, dict) or not str(metadata.get("name") or "").strip():
        raise FleetSpecError("metadata.name is required.")
    body = spec.get("spec")
    if not isinstance(body, dict):
        raise FleetSpecError("spec is required.")
    members = body.get("members") or []
    if not isinstance(members, list):
        raise FleetSpecError("spec.members must be a list.")
    if len(members) > SPEC_MAX_MEMBERS:
        raise FleetSpecError(f"spec.members: at most {SPEC_MAX_MEMBERS} members.")
    keys: list[str] = []
    for member in members:
        if not isinstance(member, dict) or not str(member.get("key") or "").strip():
            raise FleetSpecError("Every member needs a key.")
        key = str(member["key"]).strip()
        if key in keys:
            raise FleetSpecError(f"Member key '{key}' appears twice.")
        keys.append(key)
        if member.get("agent") is not None and not isinstance(member.get("agent"), dict):
            raise FleetSpecError(f"Member '{key}': agent must be a bundle or a ref.")
    for member in members:
        key = str(member["key"]).strip()
        parent = member.get("parent")
        if parent is not None and str(parent) not in keys:
            raise FleetSpecError(f"Member '{key}': parent '{parent}' is not a member key.")
        if parent is not None and str(parent) == key:
            raise FleetSpecError(f"Member '{key}' cannot report to itself.")
        for handover in member.get("handovers") or []:
            if not isinstance(handover, dict) or str(handover.get("to") or "") not in keys:
                raise FleetSpecError(f"Member '{key}': a handover names no member key.")
            kind = str(handover.get("type") or "delegate")
            if kind not in SPEC_HANDOVER_TYPES:
                raise FleetSpecError(f"Member '{key}': handover type '{kind}' is one of {', '.join(SPEC_HANDOVER_TYPES)}.")
    orchestrator = body.get("orchestrator")
    if orchestrator is not None and str(orchestrator) not in keys:
        raise FleetSpecError(f"spec.orchestrator '{orchestrator}' is not a member key.")
    level = body.get("autonomy_level")
    if level is not None and level not in AUTONOMY_LEVELS:
        raise FleetSpecError(f"spec.autonomy_level is one of {', '.join(AUTONOMY_LEVELS)}.")
    for link in body.get("connected") or []:
        if not isinstance(link, dict) or not (isinstance(link.get("agent"), dict) or isinstance(link.get("fleet"), dict)):
            raise FleetSpecError("Every connected entry names an agent or a fleet.")
        inclusion = link.get("context_inclusion")
        if inclusion is not None and str(inclusion) not in FLEET_CONTEXT_INCLUSIONS:
            raise FleetSpecError(f"context_inclusion is one of {', '.join(FLEET_CONTEXT_INCLUSIONS)}.")
    defaults = body.get("member_defaults")
    if defaults is not None:
        if not isinstance(defaults, dict) or set(defaults) - set(MEMBER_SETTING_FIELDS):
            raise FleetSpecError(f"spec.member_defaults takes {', '.join(MEMBER_SETTING_FIELDS)}.")
        inclusion = defaults.get("context_inclusion")
        if inclusion is not None and str(inclusion) not in FLEET_CONTEXT_INCLUSIONS:
            raise FleetSpecError(f"context_inclusion is one of {', '.join(FLEET_CONTEXT_INCLUSIONS)}.")
    scopes = [(defaults or {}).get("organization_scope")]
    scopes += [m.get("organization_scope") for m in members]
    scopes += [link.get("organization_scope") for link in body.get("connected") or []]
    for scope in scopes:
        if scope is None:
            continue
        try:
            normalize_organization_scope(scope)
        except FleetActionRefusedError as exc:
            raise FleetSpecError(exc.detail) from None
    return spec


# ── Apply ────────────────────────────────────────────────────────────────────


@dataclass
class FleetSpecPlan:
    """What an apply did (or, in a dry run, would do), object by object."""

    dry_run: bool
    fleet_id: uuid.UUID | None = None
    fleet_slug: str = ""
    applied: bool = False
    changes: list[dict[str, str]] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, kind: str, name: str, action: str, detail: str = "") -> None:
        self.changes.append({"object": kind, "name": name, "action": action, "detail": detail})

    @property
    def created_fleet(self) -> bool:
        """Whether an applied spec created its fleet (its first documentation is then generated, #10316)."""
        return self.applied and any(c["object"] == "fleet" and c["action"] == "create" for c in self.changes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "applied": self.applied,
            "fleet_id": str(self.fleet_id) if self.fleet_id else None,
            "fleet_slug": self.fleet_slug,
            "changes": self.changes,
            "unresolved": self.unresolved,
            "warnings": self.warnings,
        }


async def apply_fleet_spec(
    db: AsyncSession,
    user: User,
    spec: dict[str, Any],
    user_group_ids: list[str],
    *,
    dry_run: bool = True,
    allow_partial: bool = False,
    workspace_id: uuid.UUID | None = None,
) -> FleetSpecPlan:
    """Apply *spec* for *user* inside a savepoint; a dry run rolls it back.

    Raises :class:`FleetSpecError` on a malformed spec and
    :class:`FleetActionRefusedError` on a refusal (the savepoint is rolled
    back, nothing is written). The caller commits.
    """
    validate_fleet_spec(spec)
    plan = FleetSpecPlan(dry_run=dry_run)
    savepoint = await db.begin_nested()
    try:
        await _apply(db, user, spec, user_group_ids, plan, workspace_id=workspace_id)
        if plan.unresolved and not allow_partial and not dry_run:
            raise FleetActionRefusedError(
                409,
                "The spec has references this instance cannot resolve for you: "
                + "; ".join(plan.unresolved)
                + ". Fix them, or apply with allow_partial.",
            )
    except Exception:
        await savepoint.rollback()
        raise
    if dry_run:
        await savepoint.rollback()
    else:
        await savepoint.commit()
        plan.applied = True
    return plan


async def _apply(
    db: AsyncSession,
    user: User,
    spec: dict[str, Any],
    user_group_ids: list[str],
    plan: FleetSpecPlan,
    *,
    workspace_id: uuid.UUID | None,
) -> None:
    metadata = spec["metadata"]
    body = spec["spec"]
    name = str(metadata["name"]).strip()
    slug = str(metadata.get("slug") or "").strip()

    fleet = await _own_fleet(db, user, slug or None, name)
    fleet = await _upsert_fleet(db, user, fleet, metadata, body, plan, workspace_id=workspace_id, user_group_ids=user_group_ids)
    plan.fleet_id = fleet.id
    plan.fleet_slug = fleet.slug

    # Members: resolve, then create what is missing, then place them.
    resolved: dict[str, Agent] = {}
    created: set[str] = set()
    for member in body.get("members") or []:
        key = str(member["key"]).strip()
        agent, how = await _resolve_member(db, user, user_group_ids, key, member, plan)
        if agent is None:
            continue
        resolved[key] = agent
        if how == "create":
            created.add(key)

    rows = await fleet_members(db, fleet.id)
    homes = {r.agent_id: r for r in rows if r.role == FLEET_ROLE_MEMBER}
    wanted_ids = {a.id for a in resolved.values()}

    if fleet.orchestrator_agent_id is not None and fleet.orchestrator_agent_id not in wanted_ids:
        await set_orchestrator(db, fleet, None)
    for agent_id, row in list(homes.items()):
        if agent_id not in wanted_ids:
            await remove_member(db, fleet, row.id)
            plan.add("member", str(agent_id), "remove", "not in the spec; the agent is kept")
            homes.pop(agent_id)

    home_of = await home_fleets_of(db, wanted_ids)
    for key, agent in list(resolved.items()):
        if agent.id in homes:
            continue
        other = home_of.get(agent.id)
        if other is not None and other.fleet_id != fleet.id:
            plan.unresolved.append(f"member '{key}' is already home in the fleet '{other.fleet_name}'")
            resolved.pop(key)
            continue
        homes[agent.id] = await add_home_member(db, fleet, agent)
        if key not in created:
            plan.add("member", key, "add")

    member_id_by_key = {key: homes[agent.id].id for key, agent in resolved.items()}
    for member in body.get("members") or []:
        key = str(member["key"]).strip()
        if key not in resolved:
            continue
        row = homes[resolved[key].id]
        parent_key = member.get("parent")
        fields: dict[str, Any] = {}
        parent_id = member_id_by_key.get(str(parent_key)) if parent_key is not None else None
        if parent_key is not None and parent_id is None:
            plan.warnings.append(f"member '{key}': parent '{parent_key}' is not resolved; placed at top level")
        if key == body.get("orchestrator"):
            # The orchestrator is the root: set_orchestrator clears its parent.
            parent_id = None
        if row.parent_member_id != parent_id:
            fields["parent_member_id"] = parent_id
        exposed = bool(member.get("exposed", False))
        if bool(row.is_exposed) != exposed:
            fields["is_exposed"] = exposed
        label = str(member.get("role_label") or "").strip()
        if (row.role_label or "") != label:
            fields["role_label"] = label
        if await _apply_member_scope(db, fleet, row, member.get("organization_scope"), plan, f"member '{key}'"):
            fields_changed = [*fields, "organization_scope"]
        else:
            fields_changed = list(fields)
        if fields:
            await update_member(db, fleet, row.id, fields=fields)
        if fields_changed:
            if key not in created:
                plan.add("member", key, "update", ", ".join(sorted(fields_changed)))
        elif key not in created and not any(c["name"] == key for c in plan.changes):
            plan.add("member", key, "unchanged")

    orchestrator_key = body.get("orchestrator")
    target = resolved.get(str(orchestrator_key)) if orchestrator_key else None
    if orchestrator_key and target is None:
        plan.warnings.append(f"orchestrator '{orchestrator_key}' is not resolved; the fleet has none")
    wanted_orchestrator = target.id if target is not None else None
    if fleet.orchestrator_agent_id != wanted_orchestrator:
        await set_orchestrator(db, fleet, wanted_orchestrator)
        plan.add("fleet", fleet.slug, "update", "orchestrator")

    defaults = body.get("member_defaults")
    if isinstance(defaults, dict):
        current = {
            "context_inclusion": fleet.default_context_inclusion or "none",
            "collaboration_instruction": fleet.default_collaboration_instruction or "",
            "organization_scope": resolve_organization_scope(fleet.organization_scope, None),
        }
        changed = {k: v for k, v in defaults.items() if v is not None and v != current[k]}
        scope = changed.get("organization_scope")
        if scope is not None and not await _scope_resolves(db, scope, plan, "member_defaults"):
            changed.pop("organization_scope")
        if changed:
            set_member_defaults(fleet, changed)
            plan.add("fleet", fleet.slug, "update", "member_defaults")

    await _apply_handovers(db, body, resolved, created, plan)
    await _apply_connected(db, user, user_group_ids, fleet, body, plan)
    await db.flush()


async def _own_fleet(db: AsyncSession, user: User, slug: str | None, name: str) -> Fleet | None:
    if not slug:
        return None
    fleet = (
        await db.execute(select(Fleet).where(Fleet.user_id == user.id, Fleet.slug == slug).with_for_update())
    ).scalar_one_or_none()
    if fleet is not None and fleet.is_builtin:
        # The seed owns a built-in fleet (``app.agents.fleet_seed``): a spec
        # applied over it would be reverted at the next boot.
        raise FleetSpecError(
            f"The fleet '{slug}' is built-in and read-only: apply the spec under another slug."
        )
    if fleet is None:
        # Owned by someone else (an administrator applying the export of a
        # company-managed fleet): the same refusal, never a personal copy whose
        # members all stay unresolved.
        fleet = (
            await db.execute(
                select(Fleet)
                .where(Fleet.slug == slug, Fleet.is_company_managed.is_(True), Fleet.is_archived.is_(False))
                .limit(1)
            )
        ).scalar_one_or_none()
        if fleet is None:
            return None
    if fleet.is_company_managed:
        # A spec fleet is personal and so are the agents it adopts: over a
        # company-managed fleet every company-managed member would be
        # unresolved, and the plan would remove them all and leave it with no
        # orchestrator. Refused whole, before any plan is computed.
        raise FleetSpecError(
            f"The fleet '{slug}' is company-managed: a spec applies to a personal fleet only. "
            "Change it on its fleet page or with the fleet tools, or apply the spec under another slug."
        )
    return fleet


async def _upsert_fleet(
    db: AsyncSession,
    user: User,
    fleet: Fleet | None,
    metadata: dict[str, Any],
    body: dict[str, Any],
    plan: FleetSpecPlan,
    *,
    workspace_id: uuid.UUID | None,
    user_group_ids: list[str],
) -> Fleet:
    name = str(metadata["name"]).strip()[:200]
    wanted = {
        "name": name,
        "description": str(metadata.get("description") or ""),
        "icon": str(metadata.get("icon") or FLEET_DEFAULT_ICON),
        "color": str(metadata.get("color") or ""),
        "skills": body.get("skills"),
        "accepted_handover_types": normalize_accepted_handover_types(body.get("accepted_handover_types")),
    }
    level = body.get("autonomy_level")
    if fleet is None:
        workspace = await _workspace_for(db, user, user_group_ids, workspace_id, metadata.get("workspace"), name, plan)
        if level is not None:
            # #379: created at the spec's level, never autonomous first then lowered.
            wanted["autonomy_level"] = level
        fleet = await create_fleet(db, user_id=user.id, name=name, workspace_id=workspace.id, **{
            k: v for k, v in wanted.items() if k != "name"
        })
        slug = str(metadata.get("slug") or "").strip()
        if slug and fleet.slug != slug:
            plan.warnings.append(f"the slug '{slug}' is taken; the fleet is '{fleet.slug}'")
        plan.add("fleet", fleet.slug, "create")
        return fleet
    if fleet.is_archived:
        raise FleetActionRefusedError(409, f"The fleet '{fleet.name}' is archived: restore it before applying a spec.")
    if level is not None and level != effective_level(fleet.autonomy_level):
        if is_raise(fleet.autonomy_level, level):
            # Raising stays a person's choice on the fleet page (#10439): never from a spec.
            plan.warnings.append(
                f"autonomy_level '{level}' is a raise from '{effective_level(fleet.autonomy_level)}': "
                "not applied, an administrator raises it on the fleet page"
            )
        else:
            wanted["autonomy_level"] = level
    changed = [k for k, v in wanted.items() if getattr(fleet, k) != v]
    for k in changed:
        setattr(fleet, k, wanted[k])
    if workspace_id is not None and workspace_id != fleet.workspace_id:
        workspace = await _workspace_for(db, user, user_group_ids, workspace_id, None, name, plan)
        fleet.workspace_id = workspace.id
        changed.append("workspace")
    plan.add("fleet", fleet.slug, "update" if changed else "unchanged", ", ".join(changed))
    return fleet


async def _workspace_for(
    db: AsyncSession,
    user: User,
    user_group_ids: list[str],
    workspace_id: uuid.UUID | None,
    workspace_name: object,
    fleet_name: str,
    plan: FleetSpecPlan,
) -> Workspace:
    """The workspace named by id, else the caller's own one of that name, else a new one."""
    from app.api.v1.helpers import can_manage
    from app.services.workspaces import create_workspace, lock_workspace_for_hosting

    workspace: Workspace | None = None
    if workspace_id is not None:
        workspace = await db.get(Workspace, workspace_id)
        if workspace is None or not can_manage(user, workspace, user_group_ids):
            raise FleetActionRefusedError(404, f"Workspace {workspace_id} not found or you cannot manage it.")
    elif workspace_name:
        workspace = (
            await db.execute(
                select(Workspace)
                .where(
                    Workspace.user_id == user.id,
                    Workspace.name == str(workspace_name),
                    Workspace.is_archived.is_(False),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
    if workspace is not None:
        await lock_workspace_for_hosting(db, workspace)
        ensure_workspace_can_host(False, workspace)
        return workspace
    workspace = await create_workspace(
        db, user_id=user.id, name=fleet_name, description=f"Where the fleet {fleet_name} works.", is_company_managed=False
    )
    plan.add("workspace", workspace.name, "create")
    return workspace


async def _resolve_member(
    db: AsyncSession,
    user: User,
    user_group_ids: list[str],
    key: str,
    member: dict[str, Any],
    plan: FleetSpecPlan,
) -> tuple[Agent | None, str]:
    """The agent behind a member: adopted by slug or id, else created from its bundle."""
    from app.api.v1.helpers import can_manage

    agent_section = member.get("agent") or {}
    ref = agent_section.get("ref") if isinstance(agent_section.get("ref"), dict) else None
    wanted_slug = str((ref or {}).get("slug") or key)
    wanted_id = (ref or {}).get("id")

    existing: Agent | None = None
    if wanted_id:
        try:
            existing = await db.get(Agent, uuid.UUID(str(wanted_id)))
        except ValueError:
            existing = None
    if existing is None:
        existing = (await db.execute(select(Agent).where(Agent.slug == wanted_slug))).scalar_one_or_none()
    if existing is not None:
        if not can_manage(user, existing, user_group_ids):
            plan.unresolved.append(f"member '{key}': the agent '{wanted_slug}' exists and you cannot manage it")
            return None, ""
        if existing.is_company_managed:
            plan.unresolved.append(f"member '{key}': '{existing.name}' is company-managed; a spec fleet is personal")
            return None, ""
        return existing, "adopt"
    if ref is not None or (not agent_section.get("agent") and not agent_section.get("name")):
        plan.unresolved.append(f"member '{key}': no agent '{wanted_slug}' and no bundle to create it from")
        return None, ""
    agent = await _create_from_bundle(db, user, key, agent_section, plan)
    return agent, "create" if agent is not None else ""


async def _create_from_bundle(
    db: AsyncSession, user: User, key: str, section: dict[str, Any], plan: FleetSpecPlan
) -> Agent | None:
    from app.api.v1.agent_import import create_agent_from_bundle

    bundle = dict(section) if "agent" in section else {"version": 3, "export_type": "agent", "agent": dict(section)}
    bundle.setdefault("version", 3)
    bundle.setdefault("export_type", "agent")
    try:
        imported = await create_agent_from_bundle(
            db, user=user, bundle=bundle, audit_details={"via": "fleet_spec", "member_key": key}
        )
    except HTTPException as exc:
        plan.unresolved.append(f"member '{key}': {exc.detail}")
        return None
    agent = imported.agent
    taken = (await db.execute(select(Agent.id).where(Agent.slug == key, Agent.id != agent.id))).first()
    if taken is None:
        agent.slug = key
    else:
        plan.warnings.append(f"member '{key}': the slug is taken; the agent was created as '{agent.slug}'")
    for warning in imported.warnings:
        plan.warnings.append(f"member '{key}': {warning}")
    await db.flush()
    plan.add("agent", key, "create", agent.name)
    return agent


async def _apply_handovers(
    db: AsyncSession, body: dict[str, Any], resolved: dict[str, Agent], created: set[str], plan: FleetSpecPlan
) -> None:
    """Write the intra-fleet handovers on the agents the apply created; report the others."""
    for member in body.get("members") or []:
        key = str(member["key"]).strip()
        agent = resolved.get(key)
        if agent is None:
            continue
        wanted = {
            str(resolved[str(h["to"])].id): str(h.get("type") or "delegate")
            for h in member.get("handovers") or []
            if str(h.get("to")) in resolved
        }
        if key in created:
            if wanted:
                agent.handover_agent_ids = list(wanted)
                agent.handover_types = dict(wanted)
            continue
        current_ids = {str(a) for a in agent.handover_agent_ids or []}
        types = agent.handover_types or {}
        missing = [t for t, kind in wanted.items() if t not in current_ids or (types.get(t) or "delegate") != kind]
        if missing:
            plan.warnings.append(
                f"member '{key}': its handovers differ from the spec; an adopted agent is never edited, "
                "change them on the agent page"
            )
    await db.flush()


async def _scope_resolves(db: AsyncSession, scope: object, plan: FleetSpecPlan, where: str) -> bool:
    """Whether *scope* names only organizations of this instance; a warning names the others."""
    try:
        await check_scope_organizations(db, normalize_organization_scope(scope))
    except FleetActionRefusedError as exc:
        plan.warnings.append(f"{where}: {exc.detail} The scope is not applied.")
        return False
    return True


async def _apply_member_scope(
    db: AsyncSession, fleet: Fleet, row: Any, scope: object, plan: FleetSpecPlan, where: str
) -> bool:
    """Set or clear a row's own organization scope to the spec's; whether it changed.

    Absent from the entry, the row inherits (``None``), so an export of a
    row without an override applies back to none.
    """
    wanted = None
    if scope is not None:
        if not await _scope_resolves(db, scope, plan, where):
            return False
        wanted = normalize_organization_scope(scope)
    current = resolve_organization_scope(None, row.organization_scope) if row.organization_scope is not None else None
    if current == wanted:
        return False
    await update_member(db, fleet, row.id, fields={"organization_scope": wanted})
    return True


async def _apply_connected(
    db: AsyncSession,
    user: User,
    user_group_ids: list[str],
    fleet: Fleet,
    body: dict[str, Any],
    plan: FleetSpecPlan,
) -> None:
    from app.api.v1.helpers import user_visible_filter

    rows = [r for r in await fleet_members(db, fleet.id) if r.role == FLEET_ROLE_CONNECTED]
    by_agent = {r.agent_id: r for r in rows if r.agent_id is not None}
    by_fleet = {r.target_fleet_id: r for r in rows if r.target_fleet_id is not None}
    kept: set[uuid.UUID] = set()
    for link in body.get("connected") or []:
        # A setting absent from the entry inherits the fleet default (None).
        raw_instruction = link.get("collaboration_instruction")
        raw_inclusion = link.get("context_inclusion")
        instruction = str(raw_instruction) if raw_instruction is not None else None
        inclusion = str(raw_inclusion) if raw_inclusion is not None else None
        scope = link.get("organization_scope")
        agent: Agent | None = None
        target: Fleet | None = None
        if isinstance(link.get("agent"), dict):
            ref = link["agent"]
            clause = Agent.slug == str(ref.get("slug")) if ref.get("slug") else Agent.id == _safe_uuid(ref.get("id"))
            agent = (
                await db.execute(
                    select(Agent).where(clause, user_visible_filter(Agent, user.id, user_group_ids, is_admin=user.is_admin))
                )
            ).scalar_one_or_none()
            label = str(ref.get("slug") or ref.get("id"))
        else:
            ref = link["fleet"]
            clause = Fleet.slug == str(ref.get("slug")) if ref.get("slug") else Fleet.id == _safe_uuid(ref.get("id"))
            target = (
                await db.execute(
                    select(Fleet).where(clause, user_visible_filter(Fleet, user.id, user_group_ids, is_admin=user.is_admin)).limit(1)
                )
            ).scalar_one_or_none()
            label = str(ref.get("slug") or ref.get("id"))
        if agent is None and target is None:
            plan.unresolved.append(f"connected '{label}' is not visible to you")
            continue
        row = by_agent.get(agent.id) if agent is not None else by_fleet.get(target.id)  # type: ignore[union-attr]
        if row is None:
            row = await add_connected(
                db, fleet, agent=agent, target_fleet=target, collaboration_instruction=instruction, context_inclusion=inclusion
            )
            plan.add("connected", label, "add")
            await _apply_member_scope(db, fleet, row, scope, plan, f"connected '{label}'")
        else:
            scope_changed = await _apply_member_scope(db, fleet, row, scope, plan, f"connected '{label}'")
            if row.collaboration_instruction != instruction or row.context_inclusion != inclusion:
                await update_member(
                    db, fleet, row.id, fields={"collaboration_instruction": instruction, "context_inclusion": inclusion}
                )
                plan.add("connected", label, "update")
            elif scope_changed:
                plan.add("connected", label, "update")
            else:
                plan.add("connected", label, "unchanged")
        kept.add(row.id)
    for row in rows:
        if row.id not in kept:
            await remove_member(db, fleet, row.id)
            plan.add("connected", str(row.agent_id or row.target_fleet_id), "remove")


def _safe_uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


__all__ = [
    "FLEET_SPEC_API_VERSION",
    "FleetSpecError",
    "FleetSpecPlan",
    "apply_fleet_spec",
    "export_fleet_spec",
    "parse_fleet_spec_text",
    "structural_spec",
    "validate_fleet_spec",
]
