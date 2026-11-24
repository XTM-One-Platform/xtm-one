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

"""The live part of a fleet's About page (About page redesign, slice S1 of 8644e340).

:func:`evaluate_prerequisites` answers, for the reader, whether each structured
prerequisite of the fleet's documentation (``fleet_docs.DocPrerequisite``) is
met: a module integration the reader can use, a field of the organization
profile, a tracked organization with monitoring on. It reads rows only: no run
starts and no model is called, so viewing the page costs no quota.

A ``manual`` check is never evaluated (``met`` is ``None``): the page shows it
as a line to verify by hand.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

#: ``profile:<field>`` prerequisite checks of a fleet's documentation, to the profile field they read.
PROFILE_CHECK_FIELDS: dict[str, str] = {
    "profile:sector": "sector",
    "profile:regions": "regions",
    "profile:crown_jewels": "crown_jewels",
    "profile:tech_stack": "tech_stack",
    # The AI security fleet needs AI targets: the assets of category ai_target (#1844).
    "profile:ai_targets": "assets_exposure",
}

#: The prerequisite met by an asset of category ai_target, not by a whole field (#1844).
AI_TARGETS_CHECK = "profile:ai_targets"

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.fleet import Fleet
    from app.models.user import User


@dataclass
class PrerequisiteState:
    """One evaluated line of the checklist."""

    check: str
    label: str
    fix_route: str
    #: True met, False not met, None not evaluated (``manual``).
    met: bool | None


async def _integration_connected(db: AsyncSession, user: User, group_ids: list[str], provider: str) -> bool:
    from app.api.v1.helpers import user_visible_query
    from app.models.integration import Integration
    from app.services.integration_state import INTEGRATION_CONNECTED

    query = (
        user_visible_query(Integration, user.id, group_ids)
        .where(Integration.provider == provider)
        .where(Integration.status == INTEGRATION_CONNECTED)
        .order_by(None)
        .limit(1)
    )
    return (await db.execute(query)).scalars().first() is not None


async def _tracked_monitored(db: AsyncSession) -> bool:
    from app.models.organization import Organization

    row = (
        await db.execute(
            select(Organization.id)
            .where(Organization.role == "tracked")
            .where(Organization.monitoring != "off")
            .limit(1)
        )
    ).first()
    return row is not None


async def _automations_enabled(db: AsyncSession, fleet: Fleet) -> bool:
    from app.services.fleet_views import fleet_automation_counts

    return (await fleet_automation_counts(db, fleet)).enabled > 0


async def evaluate_prerequisites(
    db: AsyncSession,
    user: User,
    prerequisites: list[dict[str, Any]],
    *,
    group_ids: list[str] | None = None,
    fleet: Fleet | None = None,
) -> list[PrerequisiteState]:
    """Each structured prerequisite of a documentation, evaluated for *user*, in order.

    ``automations:enabled`` (#10094) reads *fleet*'s home members' automations:
    met once one is switched on; not evaluated without a fleet.
    """
    if not prerequisites:
        return []
    from app.api.v1.helpers import get_user_group_ids
    from app.services.organization_context import current_version, hydrated_profile, parse_profile

    if group_ids is None:
        group_ids = await get_user_group_ids(db, user.id)
    profile = None
    cache: dict[str, bool] = {}
    states: list[PrerequisiteState] = []
    for item in prerequisites:
        check = str(item.get("check") or "")
        met: bool | None
        if check in cache:
            met = cache[check]
        elif check.startswith("integration:"):
            met = cache[check] = await _integration_connected(db, user, group_ids, check.split(":", 1)[1])
        elif check in PROFILE_CHECK_FIELDS:
            if profile is None:
                row = await current_version(db)
                try:
                    # The asset lists come from the asset records once imported (#1543), not the stored version.
                    profile = await hydrated_profile(db, row)
                except ValueError:
                    profile = parse_profile(None)
            if check == AI_TARGETS_CHECK:
                from app.services.organization_context import ai_target_assets

                met = cache[check] = bool(ai_target_assets(profile))
            else:
                met = cache[check] = bool(getattr(profile, PROFILE_CHECK_FIELDS[check]))
        elif check == "profile:tracked_organizations":
            met = cache[check] = await _tracked_monitored(db)
        elif check == "automations:enabled" and fleet is not None:
            met = cache[check] = await _automations_enabled(db, fleet)
        else:
            met = None
        states.append(
            PrerequisiteState(
                check=check,
                label=str(item.get("label") or ""),
                fix_route=str(item.get("fix_route") or ""),
                met=met,
            )
        )
    return states


# --- The live checklist derived from the configuration (#10258 S1) -------------------

#: The states a derived line takes. ``to_confirm`` only for a documented ``manual`` line.
MET, NOT_MET, UNREADABLE, TO_CONFIRM = "met", "not_met", "unreadable", "to_confirm"
#: Documented ``manual`` lines kept beside the derived ones: what no system can read.
MANUAL_LINES_KEPT = 2
#: A manual line's check id prefix; the rest is :func:`manual_key` of its label (#10469 S2).
MANUAL_PREFIX = "manual:"
#: A confirmation older than this returns the line to ``to_confirm`` (#10469 S2).
CONFIRMATION_DAYS = 90
#: A subject shorter than this never drops a manual line (too common to name one thing).
_SUBJECT_MIN = 4
#: Names too generic to name one thing: an integration's default name and the fallback labels.
_GENERIC_NAMES = frozenset({"default", "integration", "knowledge base", "mcp server", "handover to an agent"})
_AUTOMATION_WORD = re.compile(r"\bautomations?\b")
_INTEGRATIONS_ROUTE = "/integrations"
#: Routes only an administrator opens (the app's ``AdminRoute``): a Fix there is offered to an admin alone.
_ADMIN_ROUTE_PREFIXES = ("/settings/",)

#: Why a line is not met or not readable, as a code the page translates (#10258, UX round 1 B1),
#: to its plain English for the agent tool and the API's ``reason``. ``reason_params`` carries
#: the values a code names: ``date`` (ISO, ``token_expired``), ``detail`` (an account's last error).
_REASON_TEXT = {
    "integration_deleted": "the integration was deleted",
    "integration_disconnected": "disconnected: reconnect it",
    "integration_not_signed_in": "not signed in yet: connect it",
    "integration_error": "in error: reconnect it",
    "integration_not_connected": "not connected: reconnect it",
    "token_expired": "token expired {date}",
    "own_account_missing": "connect your own account",
    "account_switched_off": "switched off",
    "account_integration_deleted": "its integration was deleted",
    "account_paused": "paused",
    "account_error": "in error",
    "account_setup": "setup not finished",
    "mcp_switched_off": "switched off",
    "mcp_sign_in": "sign-in needed",
    "not_shared": "not shared with you",
    "another_person_account": "another person's account",
    "target_deleted": "the target agent was deleted",
    "target_switched_off": "the target agent is switched off",
    "deleted": "it was deleted",
    "automations_off": "every automation is off",
    "platform_health_off": "Platform Health is off in Settings",
    "not_filled": "not filled",
}
REASON_CODES = frozenset(_REASON_TEXT)
#: Integration statuses to their reason code: plain words, never the raw status.
_STATUS_REASON = {
    "disconnected": "integration_disconnected",
    "configured": "integration_not_signed_in",
    "error": "integration_error",
}

#: Labels the platform writes, as catalog keys the page translates (``label_params`` fills them).
LABEL_OWN_ACCOUNT = "{provider} (your own account)"
LABEL_HANDOVER = "Handover to {name}"
LABEL_HANDOVER_UNNAMED = "Handover to an agent"
LABEL_AUTOMATIONS = "At least one automation switched on"
#: The Platform Health fleet's one automation prerequisite (#974): Platform Health itself, on in Settings.
LABEL_PLATFORM_HEALTH_ON = "Platform Health switched on in Settings"
LABEL_INTEGRATION = "Integration"
LABEL_KNOWLEDGE_BASE = "Knowledge base"
LABEL_MCP_SERVER = "MCP server"
#: The organization profile checks, to their canonical label (the documented label is not shown).
PROFILE_LABELS = {
    "profile:sector": "Sector in the organization profile",
    "profile:regions": "Regions in the organization profile",
    "profile:crown_jewels": "Primary assets in the organization profile",
    "profile:tech_stack": "Technology in the organization profile",
    "profile:ai_targets": "AI target assets in the inventory",
    "profile:tracked_organizations": "A tracked organization with monitoring on",
}

#: ``(state, reason_code, reason_params)``: what a check answers.
Check = tuple[str, str, dict[str, str]]
_OK: Check = (MET, "", {})


def reason_text(code: str, params: dict[str, str] | None = None) -> str:
    """The plain English of a reason code, with its detail when one is given."""
    if not code:
        return ""
    params = params or {}
    text = _REASON_TEXT.get(code, code).format(date=params.get("date", ""))
    detail = params.get("detail")
    return f"{text}: {detail}" if detail else text


@dataclass
class LivePrerequisite:
    """One line of the derived checklist: read from the fleet's configuration, never written by a model."""

    check: str
    #: ``integration`` | ``agent_account`` | ``handover`` | ``knowledge_base`` | ``mcp_server`` |
    #: ``automations`` | ``profile`` | ``manual``.
    kind: str
    label: str
    state: str
    #: Why it is not met, in plain words; empty when met.
    reason: str
    #: The home members that need it, as agent id strings.
    member_ids: list[str]
    #: Where it is fixed (a real app path), or empty.
    fix_route: str
    #: The integration provider key, for the logo; empty otherwise.
    provider: str = ""
    #: The reason as a code of :data:`REASON_CODES` and its values, for the page to translate.
    reason_code: str = ""
    reason_params: dict[str, str] = field(default_factory=dict)
    #: The label as a catalog key and its values when the platform wrote it; empty for a name.
    label_key: str = ""
    label_params: dict[str, str] = field(default_factory=dict)
    #: Whether the reader may open :attr:`fix_route` (an admin page is an admin's alone).
    can_fix: bool = False
    #: A ``manual`` line confirmed by a person (#10469 S2): who (display name) and when (ISO). A
    #: confirmation past :data:`CONFIRMATION_DAYS` is kept here with the line back to ``to_confirm``.
    confirmed_by: str = ""
    confirmed_at: str = ""
    #: Whether the reader may confirm or unconfirm this ``manual`` line (a fleet manager).
    can_confirm: bool = False


def covered_by_configuration(label: str, subjects: set[str], *, automations: bool = False) -> bool:
    """Whether a documented manual *label* names something a derived line already checks (#10469).

    *subjects* are the names, emails and ids the derived lines read (an
    integration, its owner's email, an agent account, a handover target, a
    knowledge base, an MCP server); *automations* is whether the automations
    line is derived. A match is the whole subject in the label, never a word of it.
    """
    lowered = label.lower()
    if automations and _AUTOMATION_WORD.search(lowered):
        return True
    for subject in subjects:
        text = subject.strip().lower()
        if len(text) < _SUBJECT_MIN or text in _GENERIC_NAMES:
            continue
        if re.search(rf"(?<![\w@.-]){re.escape(text)}(?![\w@-])", lowered):
            return True
    return False


def manual_key(label: str) -> str:
    """The stable key of a manual line: its label, case and spacing folded (#10469 S2).

    A confirmation is stored under it, so it survives a regenerate that keeps
    the label and lapses when the label changes.
    """
    folded = " ".join(label.lower().split())
    return hashlib.sha256(folded.encode("utf-8")).hexdigest()[:12]


def confirmation_state(entry: object, *, now: datetime) -> tuple[bool, str, str]:
    """``(confirmed and current, by, at)`` of one stored confirmation; expired past :data:`CONFIRMATION_DAYS`."""
    if not isinstance(entry, dict):
        return False, "", ""
    by, at = str(entry.get("name") or ""), str(entry.get("at") or "")
    try:
        when = datetime.fromisoformat(at)
    except ValueError:
        return False, by, at
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return now - when <= timedelta(days=CONFIRMATION_DAYS), by, at


def _uuid(value: object) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _provider_name(provider: str) -> str:
    from app.api.v1.integration_catalog import PROVIDER_CATALOG

    return str(PROVIDER_CATALOG.get(provider, {}).get("name") or provider)


def integration_state(integration: Any | None, *, now: datetime) -> Check:
    """``(state, reason_code, params)`` of a bound integration row: present, connected, token not expired."""
    from app.services.integration_state import INTEGRATION_CONNECTED

    if integration is None:
        return NOT_MET, "integration_deleted", {}
    if integration.status != INTEGRATION_CONNECTED:
        return NOT_MET, _STATUS_REASON.get(integration.status or "", "integration_not_connected"), {}
    expires = integration.token_expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires is not None and expires <= now and not integration.refresh_token_enc:
        return NOT_MET, "token_expired", {"date": expires.date().isoformat()}
    return _OK


def agent_account_state(account: Any) -> Check:
    """``(state, reason_code, params)`` of an agent account bound to a member."""
    if not account.enabled:
        return NOT_MET, "account_switched_off", {}
    if account.integration_id is None:
        return NOT_MET, "account_integration_deleted", {}
    status = account.status or "setup"
    if status == "active":
        return _OK
    detail = {"detail": account.last_error} if account.last_error else {}
    if status == "paused":
        return NOT_MET, "account_paused", detail
    if status == "error":
        return NOT_MET, "account_error", detail
    return NOT_MET, "account_setup", {}


def mcp_state(server: Any) -> Check:
    """``(state, reason_code, params)`` of an MCP server bound to a member."""
    if not server.enabled:
        return NOT_MET, "mcp_switched_off", {}
    if server.auth_mode == "oauth" and server.oauth_status != "connected":
        return NOT_MET, "mcp_sign_in", {}
    return _OK


def can_open(route: str, user: User) -> bool:
    """Whether *user* may open *route*: any path but an administrator's page, which only an admin opens."""
    if not route:
        return False
    return bool(user.is_admin) or not route.startswith(_ADMIN_ROUTE_PREFIXES)


def _members_by(pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    """``{key: [member ids]}`` in first-seen order, each member once."""
    out: dict[str, list[str]] = {}
    for key, member in pairs:
        bucket = out.setdefault(key, [])
        if member not in bucket:
            bucket.append(member)
    return out


async def _visible_ids(db: AsyncSession, model: Any, ids: set[uuid.UUID], user: User, group_ids: list[str]) -> set:
    from app.api.v1.helpers import user_visible_filter

    if not ids:
        return set()
    rows = await db.execute(
        select(model.id).where(
            model.id.in_(ids), user_visible_filter(model, user.id, group_ids, is_admin=bool(user.is_admin))
        )
    )
    return set(rows.scalars().all())


async def _rows(db: AsyncSession, model: Any, ids: set[uuid.UUID]) -> dict[uuid.UUID, Any]:
    if not ids:
        return {}
    return {row.id: row for row in (await db.execute(select(model).where(model.id.in_(ids)))).scalars().all()}


@dataclass
class _Builder:
    """Builds the lines for one reader: the reason in plain English beside its code, the Fix gated."""

    user: Any
    lines: list[LivePrerequisite] = field(default_factory=list)

    def add(
        self,
        check: str,
        kind: str,
        label: str | tuple[str, dict[str, str]],
        state: Check,
        members: list[str],
        fix_route: str,
        provider: str = "",
    ) -> None:
        """*label* is a name as written, or ``(catalog key, params)`` for a label the platform writes."""
        if isinstance(label, tuple):
            key, params = label
            text = key.format(**params)
        else:
            key, params, text = "", {}, label
        code, reason_params = state[1], state[2]
        self.lines.append(
            LivePrerequisite(
                check=check,
                kind=kind,
                label=text,
                state=state[0],
                reason=reason_text(code, reason_params),
                member_ids=members,
                fix_route=fix_route,
                provider=provider,
                reason_code=code,
                reason_params=reason_params,
                label_key=key,
                label_params=params,
                can_fix=can_open(fix_route, self.user),
            )
        )


async def live_prerequisites(
    db: AsyncSession,
    user: User,
    fleet: Fleet,
    documented: list[dict[str, Any]] | None = None,
    *,
    group_ids: list[str] | None = None,
    now: datetime | None = None,
    can_confirm: bool = False,
) -> list[LivePrerequisite]:
    """The fleet's prerequisites derived from its home members' configuration, checked live for *user*.

    One line per bound integration (its members listed), per template binding,
    per agent account, per handover to an agent outside the fleet, per
    knowledge base, per MCP server, plus the automations and the documented
    organization profile lines. A documented ``manual`` line is kept as
    ``to_confirm`` (at most :data:`MANUAL_LINES_KEPT`) unless it names what a
    derived line already checks (:func:`covered_by_configuration`, #10469); a documented
    ``integration:<provider>`` line is replaced by the bindings. What the reader
    may not see is ``unreadable``, never ``met``. Every reason is a code of
    :data:`REASON_CODES` with its plain English beside it; a Fix route the reader
    may not open is ``can_fix`` false. A manual line's check is
    ``manual:<key>`` (:func:`manual_key`); a person's confirmation stored on the
    fleet (``Fleet.prerequisite_confirmations``, #10469 S2) makes it ``met`` with
    who and when, until :data:`CONFIRMATION_DAYS` pass. *can_confirm* is whether
    the reader manages the fleet. Rows only: no run, no model.
    """
    from app.api.v1.helpers import get_user_group_ids
    from app.models.agent import Agent
    from app.models.agent_account import AgentAccount
    from app.models.integration import Integration
    from app.models.knowledge_base import KnowledgeBase
    from app.models.mcp_server import MCPServer
    from app.models.user import User as UserModel
    from app.services.fleet_views import HEALTH_SETTINGS_ROUTE, fleet_automation_counts, home_member_ids
    from app.services.fleets import fleet_members
    from app.services.health.read_only import is_health_fleet

    now = now or datetime.now(UTC)
    if group_ids is None:
        group_ids = await get_user_group_ids(db, user.id)
    home = home_member_ids(await fleet_members(db, fleet.id), fleet.orchestrator_agent_id)
    agents = await _rows(db, Agent, set(home))
    members = [agents[a] for a in home if a in agents]
    home_set = {str(a.id) for a in members}

    bound: list[tuple[str, str]] = []
    templates: list[tuple[str, str]] = []
    handovers: list[tuple[str, str]] = []
    kbs: list[tuple[str, str]] = []
    mcps: list[tuple[str, str]] = []
    for agent in members:
        aid = str(agent.id)
        for ids in (agent.integration_bindings or {}).values():
            bound += [(str(u), aid) for u in map(_uuid, ids or []) if u]
        templates += [(str(p), aid) for p in agent.integration_template_bindings or [] if p]
        handovers += [(str(u), aid) for u in map(_uuid, agent.handover_agent_ids or []) if u and str(u) not in home_set]
        kbs += [(str(u), aid) for u in map(_uuid, agent.knowledge_base_ids or []) if u]
        mcps += [(str(u), aid) for u in map(_uuid, agent.mcp_server_ids or []) if u]

    out = _Builder(user)
    unshared: Check = (UNREADABLE, "not_shared", {})
    by_integration = _members_by(bound)
    ids = {uuid.UUID(k) for k in by_integration}
    rows, visible = await _rows(db, Integration, ids), await _visible_ids(db, Integration, ids, user, group_ids)
    for key, who in by_integration.items():
        row, iid = rows.get(uuid.UUID(key)), uuid.UUID(key)
        if row is not None and iid not in visible:
            out.add(f"binding:{key}", "integration", _provider_name(row.provider), unshared, who, "", row.provider)
            continue
        label = f"{_provider_name(row.provider)} ({row.name})" if row is not None else (LABEL_INTEGRATION, {})
        out.add(
            f"binding:{key}",
            "integration",
            label,
            integration_state(row, now=now),
            who,
            f"{_INTEGRATIONS_ROUTE}/{key}",
            row.provider if row is not None else "",
        )
    for provider, who in _members_by(templates).items():
        met = await _integration_connected(db, user, group_ids, provider)
        out.add(
            f"template:{provider}",
            "integration",
            (LABEL_OWN_ACCOUNT, {"provider": _provider_name(provider)}),
            _OK if met else (NOT_MET, "own_account_missing", {}),
            who,
            _INTEGRATIONS_ROUTE,
            provider,
        )

    accounts = (
        (await db.execute(select(AgentAccount).where(AgentAccount.agent_id.in_([a.id for a in members]))))
        .scalars()
        .all()
        if members
        else []
    )
    for account in accounts:
        mine = bool(user.is_admin) or account.user_id == user.id
        out.add(
            f"agent_account:{account.id}",
            "agent_account",
            account.name,
            agent_account_state(account) if mine else (UNREADABLE, "another_person_account", {}),
            [str(account.agent_id)],
            f"/profile/agent-accounts/{account.id}" if mine else "",
            account.provider,
        )

    by_target = _members_by(handovers)
    target_ids = {uuid.UUID(k) for k in by_target}
    targets, seen = await _rows(db, Agent, target_ids), await _visible_ids(db, Agent, target_ids, user, group_ids)
    for key, who in by_target.items():
        target, tid = targets.get(uuid.UUID(key)), uuid.UUID(key)
        state: Check
        if target is None:
            state = (NOT_MET, "target_deleted", {})
        elif tid not in seen:
            state = unshared
        else:
            state = _OK if target.enabled else (NOT_MET, "target_switched_off", {})
        name = target.name if target is not None and tid in seen else None
        label = (LABEL_HANDOVER, {"name": name}) if name else (LABEL_HANDOVER_UNNAMED, {})
        out.add(f"handover:{key}", "handover", label, state, who, f"/agents/{who[0]}/config")

    for kind, model, pairs, route, fallback in (
        ("knowledge_base", KnowledgeBase, kbs, "/knowledge", LABEL_KNOWLEDGE_BASE),
        ("mcp_server", MCPServer, mcps, "/mcp-servers", LABEL_MCP_SERVER),
    ):
        grouped = _members_by(pairs)
        keys = {uuid.UUID(k) for k in grouped}
        found, readable = await _rows(db, model, keys), await _visible_ids(db, model, keys, user, group_ids)
        prefix = "kb" if kind == "knowledge_base" else "mcp"
        for key, who in grouped.items():
            row, rid = found.get(uuid.UUID(key)), uuid.UUID(key)
            if row is None:
                state = (NOT_MET, "deleted", {})
            elif rid not in readable:
                state = unshared
            else:
                state = mcp_state(row) if kind == "mcp_server" else _OK
            shown = row is not None and rid in readable
            out.add(f"{prefix}:{key}", kind, row.name if shown else (fallback, {}), state, who, f"{route}/{key}")

    counts = await fleet_automation_counts(db, fleet)
    if is_health_fleet(fleet):
        # Its assignments start from the Settings cycle runner (#974): the one
        # prerequisite is Platform Health on, fixed in Settings, never on the fleet.
        out.add(
            "automations:enabled",
            "automations",
            (LABEL_PLATFORM_HEALTH_ON, {}),
            _OK if counts.enabled else (NOT_MET, "platform_health_off", {}),
            [str(a.id) for a in members],
            HEALTH_SETTINGS_ROUTE,
        )
    elif counts.total:
        out.add(
            "automations:enabled",
            "automations",
            (LABEL_AUTOMATIONS, {}),
            _OK if counts.enabled else (NOT_MET, "automations_off", {}),
            [str(a.id) for a in members],
            f"/fleets/{fleet.id}",
        )

    documented = [d for d in documented or [] if not str(d.get("check") or "").startswith("integration:")]
    profile = [d for d in documented if str(d.get("check") or "").startswith("profile:")]
    for item in await evaluate_prerequisites(db, user, profile, group_ids=group_ids, fleet=fleet):
        canonical = PROFILE_LABELS.get(item.check)
        out.add(
            item.check,
            "profile",
            (canonical, {}) if canonical else item.label,
            _OK if item.met else (NOT_MET, "not_filled", {}),
            [],
            item.fix_route,
        )
    # What the derived lines read, so a manual line repeating one of them is dropped (#10469).
    subjects = {line.label for line in out.lines} | {line.check.split(":", 1)[-1] for line in out.lines}
    subjects |= {str(r.name or "") for r in rows.values()} | {str(t.name or "") for t in targets.values()}
    subjects |= {str(getattr(a, "account_email", None) or "") for a in accounts} | {str(a.name or "") for a in accounts}
    owners = [(getattr(r, "user_id", None), getattr(r, "oauth_token_owner_id", None)) for r in rows.values()]
    owner_ids = {o for pair in owners for o in pair if o} | {a.user_id for a in accounts if a.user_id}
    owner_ids |= {a.user_id for a in members if getattr(a, "user_id", None)}
    subjects |= {str(u.email or "") for u in (await _rows(db, UserModel, owner_ids)).values()}
    automated = any(line.kind == "automations" for line in out.lines)
    manual = [
        d
        for d in documented
        if str(d.get("check") or "") == "manual"
        and not covered_by_configuration(str(d.get("label") or ""), subjects, automations=automated)
    ][:MANUAL_LINES_KEPT]
    stored = getattr(fleet, "prerequisite_confirmations", None)
    confirmations = stored if isinstance(stored, dict) else {}
    for d in manual:
        label = str(d.get("label") or "")
        key = manual_key(label)
        current, by, at = confirmation_state(confirmations.get(key), now=now)
        out.add(
            f"{MANUAL_PREFIX}{key}",
            "manual",
            label,
            _OK if current else (TO_CONFIRM, "", {}),
            [],
            str(d.get("fix_route") or ""),
        )
        line = out.lines[-1]
        line.confirmed_by, line.confirmed_at, line.can_confirm = by, at, can_confirm
    return out.lines


async def set_prerequisite_confirmation(
    db: AsyncSession, user: User, fleet: Fleet, check: str, *, confirmed: bool, now: datetime | None = None
) -> LivePrerequisite:
    """Confirm or unconfirm one manual line of *fleet* for a person who manages it (#10469 S2).

    The caller checked the permission. *check* is a manual line the fleet
    shows now (``manual:<key>``), else :class:`ValueError` with the plain reason.
    Stored on the fleet, never in the AI documentation, so a regenerate that
    keeps the label keeps it. Answers the line as it reads after the write.
    """
    now = now or datetime.now(UTC)
    documented = ((fleet.documentation or {}).get("structured") or {}).get("prerequisites") or []
    lines = await live_prerequisites(db, user, fleet, documented, now=now, can_confirm=True)
    line = next((x for x in lines if x.kind == "manual" and x.check == check.strip()), None)
    if line is None:
        shown = ", ".join(x.check for x in lines if x.kind == "manual") or "none"
        raise ValueError(f"No manual prerequisite `{check}` on this fleet (manual lines: {shown}).")
    key = line.check.removeprefix(MANUAL_PREFIX)
    stored = dict(fleet.prerequisite_confirmations or {})
    if confirmed:
        name = str(getattr(user, "full_name", "") or getattr(user, "email", "") or user.id)
        stored[key] = {"by": str(user.id), "name": name, "at": now.isoformat(), "label": line.label}
        line.state, line.confirmed_by, line.confirmed_at = MET, name, stored[key]["at"]
    else:
        stored.pop(key, None)
        line.state, line.confirmed_by, line.confirmed_at = TO_CONFIRM, "", ""
    fleet.prerequisite_confirmations = stored or None
    await db.flush()
    return line
