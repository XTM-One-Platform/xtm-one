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

"""Effective HITL whitelist resolution.

Membership is *computed*, never materialised::

    default   = shipped_default | platform_reads      (both owned by the product)
    effective = (default - admin_removals) | admin_additions

Keeping it computed is what makes the upgrade story safe. Both halves of the
default are code — a curated list plus the read-classified tools of the
registered platforms — so a release replaces them wholesale; the
administrator overlay is a small table that upgrades never touch. Subtracting
removals *after* applying the default is what guarantees a deliberate "re-gate
this tool" survives a later release that puts the tool back in the default.

Resolution is consulted for every tool call, so callers MUST resolve once per
run and reuse the result — see :func:`resolve_whitelist`.
"""

from __future__ import annotations

import contextlib
import logging
from functools import partial

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tool_approval import HitlWhitelistOverride
from app.services.hitl_whitelist_default import SHIPPED_DEFAULT_WHITELIST
from app.services.hitl_whitelist_platform import read_tools_for

logger = logging.getLogger(__name__)

ACTION_ADD = "add"
ACTION_REMOVE = "remove"


class WhitelistUnavailableError(RuntimeError):
    """Raised when effective membership cannot be determined.

    Deliberately distinct from "nothing is whitelisted". A caller that received
    an empty set could not tell a genuinely empty overlay from a failed read,
    and the two demand opposite behaviour: the first is normal, the second must
    gate everything. Raising forces the caller to make that choice explicitly
    rather than inheriting a plausible-looking empty result.
    """


async def resolve_whitelist(
    db: AsyncSession, *, reach: dict[str, set[str]] | None = None
) -> frozenset[str]:
    """Return the effective set of tool names exempt from human approval.

    Resolve this ONCE per run and reuse it. Membership is consulted on every
    tool call, so querying per call turns a constant-time check into a database
    round-trip on the hot path.

    *reach*, when given, receives which tools each exempt name covers — what the
    gate hands ``evaluate_gate`` as ``whitelist_reach``. Membership is by bare
    name, and the name of an MCP or custom tool is not the product's to vouch
    for: without the reach, one taking a whitelisted name ran unattended.

    Raises:
        WhitelistUnavailableError: if the overlay cannot be read. Callers must
            treat this as "gate everything" — never as "nothing is whitelisted".
    """
    try:
        result = await db.execute(
            select(HitlWhitelistOverride.tool_key, HitlWhitelistOverride.action)
        )
        overrides = result.all()
    except Exception as exc:  # deliberately broad — see the docstring below
        # Any failure here is a failure to know the answer. Converting it into
        # an empty set would silently gate everything (annoying but safe) or,
        # worse, a partial read would under-gate. Fail loudly and let the caller
        # apply the fail-closed policy.
        logger.error("hitl_whitelist_resolution_failed", exc_info=exc)
        raise WhitelistUnavailableError(
            "Could not resolve the HITL whitelist"
        ) from exc

    additions = {key for key, action in overrides if action == ACTION_ADD}
    removals = {key for key, action in overrides if action == ACTION_REMOVE}

    # The gate's half of the fail-closed split: unreadable registrations mean we
    # do not know which platforms are connected, and the safe reading of that is
    # "none" — their reads keep asking for approval, which is noisy rather than
    # dangerous. The curated default is still trustworthy, so this is NOT the
    # same as the overlay failing, which raises. The administration endpoints
    # take the opposite branch and refuse; see PlatformReadsUnavailableError.
    try:
        default = await default_membership(db, reach=reach)
    except PlatformReadsUnavailableError:
        logger.error(
            "Platform reads unavailable — gating every platform tool call"
        )
        default = SHIPPED_DEFAULT_WHITELIST
        if reach is not None:
            from app.tools.approval import REACH_PRODUCT

            reach.clear()
            for name in default:
                reach.setdefault(name, set()).add(REACH_PRODUCT)

    if reach is not None:
        from app.tools.approval import REACH_ANY

        for name in additions:
            reach.setdefault(name, set()).add(REACH_ANY)

    # Order matters: subtract removals from the default BEFORE unioning
    # additions. An administrator's explicit removal must outlive a release that
    # re-adds the tool to the default, which is only true if the subtraction is
    # applied to the default rather than to the final set. That now covers the
    # derived platform reads too: re-gating one has to survive the next release
    # exactly like re-gating a shipped entry does.
    return frozenset((default - removals) | additions)


def gate_reach(reach: dict[str, set[str]]) -> dict[str, frozenset[str]] | None:
    """*reach* as ``evaluate_gate`` takes it.

    ``resolve_whitelist`` records a reach for every name it exempts, the shipped
    list included, so an empty one means nothing was recorded — a resolver that
    raised, whose whitelist is then empty — and membership by name stands.
    """
    return {name: frozenset(who) for name, who in reach.items()} or None


async def resolve_vetoed(db: AsyncSession) -> frozenset[str]:
    """Tools an administrator has explicitly re-gated.

    Separate from :func:`resolve_whitelist` because the two answer different
    questions and only one of them can be expressed as absence. A removal takes
    the tool out of the whitelist, which reads identically to "never
    whitelisted" — so the per-connection exemption below it still ran the tool,
    letting any user with their own connection defeat an administrator.

    Resolve once per run alongside the whitelist. Raises the same error type for
    the same reason: not knowing must never read as "nothing was re-gated".
    """
    try:
        result = await db.execute(
            select(HitlWhitelistOverride.tool_key).where(
                HitlWhitelistOverride.action == ACTION_REMOVE
            )
        )
        return frozenset(key for (key,) in result.all() if key)
    except Exception as exc:
        logger.error("hitl_veto_resolution_failed", exc_info=exc)
        raise WhitelistUnavailableError(
            "Could not resolve the HITL re-gates"
        ) from exc


class PlatformReadsUnavailableError(RuntimeError):
    """Raised when the derived platform-read layer cannot be determined.

    Deliberately distinct from "no platform is registered", for the same reason
    :class:`WhitelistUnavailableError` is distinct from "nothing is
    whitelisted" — and this one was learned the hard way. Returning an empty
    set on failure is right for the *gate*, which then asks a human, but it was
    silently wrong for the *administrator*: a transient read failure made the
    tool look absent from the default, so "require approval for this tool"
    took the already-gated branch, wrote nothing, and answered 204. The
    operator was told the tool now asks while it went on running unattended as
    soon as the database recovered.

    So the two readings are now separated at the source. Callers that gate
    catch this and treat it as "exempt nothing"; callers that *write* an
    administrator's decision must refuse, because a decision recorded against
    membership nobody could read is not a decision.
    """


async def platform_read_default(db: AsyncSession) -> frozenset[str]:
    """Read-classified tools of the platforms this deployment has registered.

    Derived rather than stored, so it tracks the release the way the shipped
    default does — see ``app.services.hitl_whitelist_platform``. Scoped to
    registered platforms so the whitelist screen lists what this deployment
    actually has.

    Raises:
        PlatformReadsUnavailableError: if the registrations cannot be read.
            This does NOT mean "no platform is registered" — see the exception.
            :func:`resolve_whitelist` converts it to "exempt nothing"; the
            administration endpoints refuse instead.
    """
    try:
        governed, _ = await platform_approval_by_provider(db)
    except Exception as exc:
        # Roll back before raising. Postgres aborts the whole transaction on a
        # failed statement, so without this every later query on the caller's
        # session fails too — which is not hypothetical: it turned the refusal
        # into a 500 on the one endpoint that reads the overlay *after* asking
        # us (``POST /hitl-whitelist``, where ``DELETE`` reads it before and so
        # answered a clean 503), and it silently emptied the name suggestions on
        # the listing, because those queries ran on the poisoned session too.
        #
        # Nothing is lost by rolling back. The transaction was already dead when
        # the statement failed; this only makes the session usable again, so the
        # caller's own error handling gets to run.
        with contextlib.suppress(Exception):
            await db.rollback()
        logger.error("hitl_platform_reads_unavailable", exc_info=exc)
        raise PlatformReadsUnavailableError(
            "Could not resolve which platforms are registered"
        ) from exc
    return read_tools_for(governed)


async def default_membership(
    db: AsyncSession, *, reach: dict[str, set[str]] | None = None
) -> frozenset[str]:
    """Everything exempt before this deployment's overrides are applied.

    Three layers, all owned by the product rather than the operator: the curated
    shipped list, the platform reads, and the reads of everything else this
    deployment has connected — integrations and bound MCP servers — all three
    derived by the same classifier. Callers need them as one set, because the
    overlay treats them identically: an administrator re-gating any of them
    records the same durable ``remove``, and a durable ``remove`` is the only
    thing that survives an upgrade.

    Channel bot tools are deliberately absent from the derived layer; see
    ``hitl_whitelist_derived``.

    Propagates :class:`PlatformReadsUnavailableError` on purpose. Every caller
    of this function is answering "is this tool exempt?" for an administrator,
    and a half-known answer to that question must not look like a whole one.

    *reach* is filled as :func:`resolve_whitelist` documents: the shipped list
    and the platform and integration reads cover the product's own tools, an MCP
    read only its server's.
    """
    from app.services.hitl_whitelist_derived import connected_read_tools
    from app.tools.approval import REACH_PRODUCT

    platform_reads = await platform_read_default(db)
    connected = await connected_read_tools(db, reach=reach)
    if reach is not None:
        for name in SHIPPED_DEFAULT_WHITELIST | platform_reads:
            reach.setdefault(name, set()).add(REACH_PRODUCT)
    return SHIPPED_DEFAULT_WHITELIST | platform_reads | connected


async def load_overrides(db: AsyncSession) -> dict[str, str]:
    """Return ``{tool_key: action}`` for every administrator override."""
    result = await db.execute(
        select(HitlWhitelistOverride.tool_key, HitlWhitelistOverride.action)
    )
    return {key: action for key, action in result.all()}


async def set_override(
    db: AsyncSession,
    *,
    tool_key: str,
    action: str,
    user_id,
    reason: str | None = None,
) -> HitlWhitelistOverride:
    """Record an administrator's decision about one tool.

    Upserts on ``tool_key``: adding a tool that is currently overridden to
    ``remove`` flips that row rather than inserting a second, so a tool never
    ends up with two contradictory decisions and concurrent edits converge.

    A real ``ON CONFLICT``, not select-then-insert. The earlier shape read the
    row first and inserted when it saw none, so two administrators acting at
    once both saw none, both inserted, and one request died on the unique
    constraint — the opposite of the convergence this docstring claimed
    (Principle II). One row per request made that a narrow window; the bulk
    platform-read actions write up to 58 rows per transaction and widen it
    proportionally, which is what turned a theoretical race into a likely one.

    Any tool may be exempted, including a destructive one — consistent with how
    tool allow/deny lists are already administered here. The safeguards are the
    warning shown before saving and the audit record written by the caller, not
    a list the product refuses to change.
    """
    stmt = (
        pg_insert(HitlWhitelistOverride)
        .values(
            tool_key=tool_key,
            action=action,
            created_by_id=user_id,
            reason=reason,
        )
        .on_conflict_do_update(
            index_elements=[HitlWhitelistOverride.tool_key],
            set_={
                "action": action,
                "created_by_id": user_id,
                "reason": reason,
            },
        )
        .returning(HitlWhitelistOverride)
    )
    # ``scalars().first()`` rather than ``scalar_one()``: the caller wants the
    # resulting row, and RETURNING gives exactly one for either branch of the
    # upsert. Nothing downstream depends on knowing which branch it took, and
    # RETURNING carries every column — including the ``created_at`` default —
    # so there is nothing left to re-read afterwards. An earlier version added
    # a ``db.refresh`` here to defeat a stale identity-map entry; no caller can
    # produce one, because they all read the overlay through ``load_overrides``,
    # which selects columns rather than entities. The bulk platform-read
    # actions made the cost of that mistake visible: one wasted SELECT per row,
    # dozens per request.
    result = await db.execute(stmt)
    return result.scalars().first()


async def clear_override(db: AsyncSession, tool_key: str) -> bool:
    """Drop an administrator override, returning the tool to its shipped state."""
    existing = await db.scalar(
        select(HitlWhitelistOverride).where(
            HitlWhitelistOverride.tool_key == tool_key
        )
    )
    if existing is None:
        return False
    await db.delete(existing)
    return True


def install_default_asks(stored: bool | None) -> bool:
    """What the install default says, read from its stored value.

    Approvals are opt-in: only a default an administrator turned on asks. No
    settings row yet reads as off, the value that row is created with.
    """
    return stored is True


def agent_asks(own: bool | None, install_default: bool) -> bool:
    """Whether an agent's tool calls go through the approval gate.

    Its own ``approve_tool_calls`` wins; NULL follows the install default. The
    one rule behind the gate, the security audit and every screen that says
    whether approvals are in use (``services/tool_approval_usage.py``).
    """
    return bool(own) if own is not None else install_default


async def resolve_approve_tool_calls(db: AsyncSession, agent) -> bool:
    """Whether *agent*'s tool calls go through the approval gate at all.

    The agent's own ``approve_tool_calls`` wins; NULL (or no agent, the General
    Assistant) follows the install default, which is off until an administrator
    turns it on. Not ``human_in_the_loop``: that switch keeps its prompt-driven
    meaning.
    """
    if await seeded_fleet_member(db, agent):
        return False
    own = getattr(agent, "approve_tool_calls", None) if agent is not None else None
    if own is not None:
        return bool(own)
    try:
        from app.models.platform_settings import PlatformSettings

        # A savepoint, so a failed read cannot roll back the caller's run.
        async with db.begin_nested():
            default = await db.scalar(select(PlatformSettings.approve_tool_calls_default))
    except Exception:
        logger.exception("Could not read the install's approval default — gating")
        return True
    return agent_asks(None, install_default_asks(default))


async def seeded_fleet_member(db: AsyncSession, agent: object) -> bool:
    """Whether *agent* is a built-in agent of a built-in (seeded) fleet: never an in-run approval card (#1675).

    A seeded fleet's tools are always offered and authorized on every
    deployment; its gates are decision items between steps. ``False`` on a
    failed read, so the agent's own switch decides as before.
    """
    agent_id = getattr(agent, "id", None)
    if agent is None or not getattr(agent, "is_builtin", False) or agent_id is None:
        return False
    try:
        from app.models.fleet import Fleet, FleetMember

        async with db.begin_nested():
            found = await db.scalar(
                select(FleetMember.id)
                .join(Fleet, Fleet.id == FleetMember.fleet_id)
                .where(FleetMember.agent_id == agent_id, Fleet.is_builtin.is_(True))
                .limit(1)
            )
    except Exception:
        logger.exception("Could not read whether agent %s is a seeded fleet member", getattr(agent, "id", None))
        return False
    return found is not None


def gate_off_outcome(agent) -> str | None:
    """Which switch let an ungated call run: the install default when the
    agent sets nothing (FR-020), else ``None`` (the agent's own)."""
    if agent is None or getattr(agent, "approve_tool_calls", None) is None:
        from app.tools.approval import OUTCOME_INSTALL_DEFAULT_OFF

        return OUTCOME_INSTALL_DEFAULT_OFF
    return None


async def _platform_approvals_off(db: AsyncSession) -> bool | None:
    """Whether ``platform_settings.tool_approvals_enabled`` is off; ``None`` when unreadable."""
    try:
        from app.models.platform_settings import PlatformSettings

        enabled = await db.scalar(select(PlatformSettings.tool_approvals_enabled))
    except Exception:
        with contextlib.suppress(Exception):
            await db.rollback()
        logger.exception(
            "Could not read the platform approval switch — gating normally"
        )
        return None
    # No row yet (fresh install, settings never saved) means the default:
    # approvals are on.
    return enabled is False


async def resolve_platform_kill_switch(db: AsyncSession) -> str | None:
    """``OUTCOME_APPROVALS_DISABLED`` when an administrator turned approvals off.

    ``platform_settings.tool_approvals_enabled`` is absolute: off means no tool
    call pauses for anyone, on any surface, a tool that always asks included.
    Approval is opt-in at platform level, and no tool-level flag turns it back
    on. Fails closed: an unreadable switch answers ``None`` (gate normally).
    """
    if await _platform_approvals_off(db):
        from app.tools.approval import OUTCOME_APPROVALS_DISABLED

        return OUTCOME_APPROVALS_DISABLED
    return None


async def resolve_gate_suppression(
    db: AsyncSession, user_id, *, is_company_managed: bool = False
) -> str | None:
    """Whether the approval gate is switched off wholesale, and by whom.

    Two switches sit ABOVE the per-tool decision table and this is their single
    resolution point, shared by chat, public chat, the general assistant and
    the assignment engine so no entry point can honour one and miss the other:

    * ``platform_settings.tool_approvals_enabled`` — an administrator's
      platform-wide kill switch (:func:`resolve_platform_kill_switch`). Off
      means no tool call pauses for anyone, a tool that always asks included.
    * ``users.approve_all_tools`` — one user's own "allow all tools" mode,
      the wholesale form of answering "Yes, always" per tool.

    Returns the audit outcome to record against every call
    (``OUTCOME_APPROVALS_DISABLED`` / ``OUTCOME_USER_ALLOWS_ALL``), or ``None``
    when the gate must run normally. Returning the *outcome* rather than a
    boolean is deliberate: a suppressed gate still records why every call ran
    unattended (FR-020), and the two reasons name different responsible
    parties.

    ``is_company_managed`` withholds the *personal* switch from a run whose
    governing decision is not the run-as user's. They cannot approve such a run
    — ``require_run_access`` refuses a non-admin any interaction with one — so
    letting their own allow-all cancel the question would hand the person who
    may not answer it the power to remove it. The platform kill switch still
    applies: that one is the administrator's own.

    Fails closed: an unreadable switch means we do not know it was turned off,
    so the gate keeps gating. Note what is NOT suppressed here — the
    ``request_human_input`` tool, which is the agent *choosing* to ask a
    question, not the platform forcing a pause.
    """
    off = await _platform_approvals_off(db)
    if off is None:
        return None
    if off:
        from app.tools.approval import OUTCOME_APPROVALS_DISABLED

        return OUTCOME_APPROVALS_DISABLED

    if not user_id or is_company_managed:
        return None

    # Chat passes the user id as a string, runs pass a UUID; anonymous surfaces
    # pass None and can hold no preference.
    if isinstance(user_id, str):
        import uuid as _uuid

        try:
            user_id = _uuid.UUID(user_id)
        except ValueError:
            return None

    try:
        from app.models.user import User

        allows_all = await db.scalar(
            select(User.approve_all_tools).where(User.id == user_id)
        )
        if allows_all:
            from app.tools.approval import OUTCOME_USER_ALLOWS_ALL

            return OUTCOME_USER_ALLOWS_ALL
    except Exception:
        with contextlib.suppress(Exception):
            await db.rollback()
        logger.exception(
            "Could not read the user's allow-all preference — gating normally"
        )
    return None


def make_recording_gate(outcome: str, outcome_sink: dict[str, str] | None):
    """A gate that never pauses but still explains itself.

    Installed when :func:`resolve_gate_suppression` says the gate is switched
    off. Returning ``None`` instead would leave every call's audit outcome
    blank — and a blank is reserved for the one state that must stay legible:
    a gate that failed to install. ``setdefault`` so a verdict a human already
    recorded on a resumed call is never overwritten.
    """

    def _gate(call) -> None:
        if outcome_sink is not None:
            outcome_sink.setdefault(call.id, outcome)

    return _gate


def make_ungated_gate():
    """The gate of an agent that does not ask: nothing pauses, nothing is stamped.

    Installed where the agent's approvals are off (its ``approve_tool_calls``
    false, or NULL under an install default that is off) and the platform
    switch is on. Tool approval is opt-in: an agent nobody set to ask never
    stops for a person, a tool that always asks included. The call's outcome
    is stamped after the turn by :func:`app.tools.approval.resolve_log_outcome`
    with the switch that was off (``OUTCOME_HITL_OFF``,
    ``OUTCOME_INSTALL_DEFAULT_OFF`` or ``OUTCOME_BOT_UNINTERRUPTED``), which is
    why this gate records nothing itself: a stamp here would hide which one.
    """

    def _gate(_call) -> None:
        return None

    return _gate


def make_fail_closed_gate(outcome_sink: dict[str, str] | None = None):
    """A gate that pauses everything, for when the real one could not be built.

    ``build_approval_gate`` fails closed internally — an unreadable whitelist or
    preference lookup gates everything rather than proceeding. A caller that
    catches its exception and installs nothing discards that: the turn then runs
    every tool unattended precisely because the control was unavailable, which
    inverts the rule the feature is built on ("unknown means gated").

    For surfaces where nobody is watching — the anonymous embed — or where the
    product promises a permanent gate, that trade is not available.
    """

    def _gate(call) -> None:
        from app.tools.approval import OUTCOME_GATE_UNAVAILABLE
        from app.tools.base import HumanInputRequestedError

        if outcome_sink is not None:
            outcome_sink.setdefault(call.id, OUTCOME_GATE_UNAVAILABLE)
        raise HumanInputRequestedError(
            question=f"Approval required to run {call.name}",
            tool_call_id=call.id,
            conversation=[],
        )

    return _gate


def make_always_ask_gate(
    composite,
    inner,
    *,
    approved_call_ids: set[str] | None = None,
    approved_shapes: dict[str, str] | None = None,
    outcome_sink: dict[str, str] | None = None,
):
    """*inner*, except that a call of a tool that always asks pauses.

    Such a call sets its tool's ``always_ask_argument``, or its tool declares
    ``always_ask`` (:func:`app.tools.approval.definition_always_asks`).
    Wrapped around every gate a surface installs for an agent that asks:
    what such a call does - a click that saves or deletes, a write through a
    company-wide key - is not something a whitelist entry, exemption,
    standing approval or user switch may clear in place of the person. Only
    a decision on this very call does: one approved on resume runs, while the
    tool is still the one the reviewer was shown. Approval stays opt-in, so
    this wrapper is never installed for an agent that does not ask
    (:func:`make_ungated_gate`), nor where an administrator turned approvals
    off for the platform (:func:`resolve_platform_kill_switch`).
    """
    from app.tools.approval import (
        OUTCOME_PAUSED_TOOL_CHANGED,
        definition_always_asks,
        fingerprint,
    )
    from app.tools.base import HumanInputRequestedError

    approved = approved_call_ids or set()
    shapes = approved_shapes or {}

    def _gate(call) -> None:
        definition = composite.get_tool_definition(call.name) if composite is not None else None
        if not definition_always_asks(definition, call):
            if inner is not None:
                inner(call)
            return
        if call.id in approved:
            shown = shapes.get(call.id)
            if shown is None or shown == fingerprint(definition.input_schema):
                return
            if outcome_sink is not None:
                outcome_sink[call.id] = OUTCOME_PAUSED_TOOL_CHANGED
            raise HumanInputRequestedError(
                question=(
                    f"Approval required to run {call.name}: it changed since "
                    "you were asked"
                ),
                tool_call_id=call.id,
                conversation=[],
            )
        raise HumanInputRequestedError(
            question=f"Approval required to run {call.name}",
            tool_call_id=call.id,
            conversation=[],
        )

    return _gate


async def build_approval_gate(
    db: AsyncSession,
    composite,
    *,
    user_id,
    hitl_enabled: bool,
    approved_call_ids: set[str] | None = None,
    approved_shapes: dict[str, str] | None = None,
    stale_sink: set[str] | None = None,
    outcome_sink: dict[str, str] | None = None,
):
    """Resolve gate inputs once and return a gate callable.

    Shared by assignment runs and chat so the two cannot drift apart. Gating
    that differs by entry point is the exact defect this exists to close: the
    same agent calling the same tool must behave the same way whether it was
    reached from a schedule or a conversation.

    The platform kill switch comes first: when an administrator turned
    approvals off, a recording gate is returned alone - nothing pauses, a
    tool that always asks included, and every call is stamped
    ``OUTCOME_APPROVALS_DISABLED``. Otherwise, when ``hitl_enabled`` is false
    (the agent does not ask) nothing pauses either, a tool that always asks
    included: approval is opt-in, and :func:`make_ungated_gate` records
    nothing so the trail names the agent's switch. Only an agent that asks
    gets the always-ask wrapper (:func:`make_always_ask_gate`). When the
    user's allow-all mode is on, a recording gate wrapped in the always-ask
    gate is returned: it pauses only the calls of a tool that always asks,
    and stamps why each other call ran, so suppression is visible in the
    trail rather than indistinguishable from a gate that failed to install.

    Both lookups fail closed. If either cannot be read the caller gates
    everything rather than proceeding — and each failure is distinguishable from
    a legitimately empty result, so an outage is never recorded as policy.
    """
    always_ask = partial(
        make_always_ask_gate,
        composite,
        approved_call_ids=approved_call_ids,
        approved_shapes=approved_shapes,
        outcome_sink=outcome_sink,
    )
    from app.tools.approval import OUTCOME_APPROVALS_DISABLED

    if not hitl_enabled:
        off = await resolve_platform_kill_switch(db)
        if off:
            return make_recording_gate(off, outcome_sink)
        return make_ungated_gate()

    suppressed = await resolve_gate_suppression(db, user_id)
    if suppressed == OUTCOME_APPROVALS_DISABLED:
        return make_recording_gate(suppressed, outcome_sink)
    if suppressed:
        return always_ask(make_recording_gate(suppressed, outcome_sink))

    from app.services.tool_approval_service import (
        ApprovalPreferencesUnavailableError,
        list_active_exceptions,
    )
    from app.tools.approval import evaluate_gate, fingerprint

    reach: dict[str, set[str]] = {}
    try:
        whitelist = await resolve_whitelist(db, reach=reach)
        vetoed = await resolve_vetoed(db)
    except WhitelistUnavailableError:
        logger.error("HITL whitelist unavailable — gating every tool call")
        # Empty whitelist gates everything, so an empty veto set adds nothing:
        # both must still be BOUND or the gate raises NameError on its first
        # call and installs nothing at all — fail-open by crash.
        whitelist = frozenset()
        vetoed = frozenset()
    whitelist_reach = gate_reach(reach)

    exception_sources: dict[str, str] = {}
    exception_entity_ids: dict[str, str] = {}
    try:
        exceptions = await list_active_exceptions(
            db,
            user_id,
            sources=exception_sources,
            entity_ids=exception_entity_ids,
        )
    except ApprovalPreferencesUnavailableError:
        logger.error("Standing approvals unavailable — gating every tool call")
        exceptions = {}

    exempt_sources = await resolve_exempt_sources(db)
    approved = approved_call_ids or set()
    shapes = approved_shapes or {}

    def _gate(call) -> None:
        from app.tools.approval import OUTCOME_PAUSED_TOOL_CHANGED
        from app.tools.base import HumanInputRequestedError

        definition = composite.get_tool_definition(call.name)
        live_fingerprint = fingerprint(
            definition.input_schema if definition else None
        )

        if call.id in approved:
            # FR-002c4's second half (spec.md:207): the bypass carries the
            # human's decision, but only while it still describes this tool. A
            # resumable proposal never expires on a clock, so without this a tab
            # reopened days later executed against whatever the tool had since
            # become. ``shapes`` is empty for callers that pass none — the old
            # behaviour, not a silent re-pause of everything.
            shown = shapes.get(call.id)
            if shown is None or shown == live_fingerprint:
                return
            if outcome_sink is not None:
                outcome_sink[call.id] = OUTCOME_PAUSED_TOOL_CHANGED
            raise HumanInputRequestedError(
                question=(
                    f"Approval required to run {call.name}: it changed since "
                    "you were asked"
                ),
                tool_call_id=call.id,
                conversation=[],
            )

        decision = evaluate_gate(
            call.name,
            hitl_enabled=True,
            whitelist=whitelist,
            active_exceptions=exceptions,
            current_fingerprint=live_fingerprint,
            tool_source=definition.source if definition else "",
            tool_entity_id=(
                str(definition.entity_id)
                if definition and definition.entity_id
                else ""
            ),
            exempt_sources=exempt_sources,
            vetoed=vetoed,
            whitelist_reach=whitelist_reach,
            exception_sources=exception_sources,
            exception_entity_ids=exception_entity_ids,
        )
        if decision.invalidated_preference:
            if stale_sink is not None:
                stale_sink.add(call.name)
            exceptions.pop(call.name, None)
        if decision.log_outcome and outcome_sink is not None:
            outcome_sink[call.id] = decision.log_outcome
        if decision.action == "pause":
            raise HumanInputRequestedError(
                question=f"Approval required to run {call.name}",
                tool_call_id=call.id,
                conversation=[],
            )

    return always_ask(_gate)


async def drain_gate_bookkeeping(
    db: AsyncSession,
    messages: list,
    composite,
    *,
    outcomes: dict[str, str],
    stale: set[str],
    user_id,
    hitl_enabled: bool,
    #: Passed through to the recorder: which switch being off let these calls
    #: run. ``None`` means the agent's own.
    hitl_off_outcome: str | None = None,
    conversation_id=None,
    exclude_call_ids: set[str] | None = None,
) -> None:
    """Persist what the gate observed during one chat turn.

    The gate is synchronous by contract — it sits on the hot path of every tool
    call and must stay free of I/O — so it only fills two sinks and leaves the
    writing to whoever installed it. Assignment runs have always drained both;
    chat drained neither, which meant a conversation recorded no gate outcome
    (FR-020) and left a standing approval marked stale in memory only, so the
    user's preference list went on claiming they always allow a tool that had
    just prompted them (FR-019a/b).

    Both halves are best-effort and independent: a failure to record must never
    cost the user their answer, and a stale row is safe either way because it
    re-prompts regardless of whether the revocation was written.
    """
    from app.services.tool_tracking import record_chat_gate_outcomes

    # Chat passes the user id as a string, runs pass a UUID. Normalise once so
    # neither the audit lookup nor the revocation depends on which called.
    if isinstance(user_id, str):
        import uuid as _uuid

        try:
            user_id = _uuid.UUID(user_id)
        except ValueError:
            user_id = None

    try:
        await record_chat_gate_outcomes(
            messages,
            composite,
            outcomes=outcomes,
            user_id=user_id,
            hitl_enabled=hitl_enabled,
            hitl_off_outcome=hitl_off_outcome,
            conversation_id=conversation_id,
            exclude_call_ids=exclude_call_ids,
        )
    except Exception:
        logger.warning("Could not record chat gate outcomes", exc_info=True)

    if not stale or not user_id:
        return

    from app.services.tool_approval_service import (
        REASON_TOOL_CHANGED,
        invalidate_stale_approvals,
    )

    try:
        await invalidate_stale_approvals(
            db, user_id=user_id, tool_keys=stale, reason=REASON_TOOL_CHANGED
        )
        await db.commit()
    except Exception:
        logger.warning(
            "Could not invalidate stale standing approvals after a chat turn",
            exc_info=True,
        )


async def resolve_exempt_sources(db: AsyncSession) -> frozenset[str]:
    """Tool sources that skip human approval.

    Two layers answer this and they are NOT peers. For a registered Suite
    platform the platform policy is the whole answer; the older per-integration
    ``requires_approval`` toggle applies only to everything else.

    That precedence is the point. Registration creates an integration row for
    the platform, so honouring both layers would make exemption an OR: a
    platform an operator deliberately set to *require* would stop gating the
    moment anybody flipped the older flag on its integration — while the
    settings card carried on claiming its tools ask before running. One control
    per platform, one truth.

    Resolve this ONCE per run alongside :func:`resolve_whitelist`; it is
    consulted on every tool call.

    Fails closed: an unreadable toggle means we do not know it was turned off,
    so the source keeps gating.
    """
    try:
        governed, platform_exempt = await platform_approval_by_provider(db)

        from app.models.integration import Integration

        rows = (
            await db.execute(
                select(Integration.provider, Integration.id).where(
                    Integration.requires_approval.is_(False)
                )
            )
        ).all()
        # Two shapes, deliberately. A platform policy governs every connection
        # of its provider, so it stays bare. A connection's own switch is keyed
        # by that connection: several Notion integrations can exist, owned by
        # different people, and one operator turning theirs off must not exempt
        # everybody else's — which a bare ``integration:notion`` did.
        #
        # Subtract before unioning: a governed provider takes its answer from
        # the platform policy alone, in both directions.
        sources = {
            f"integration:{provider}:{integ_id}"
            for provider, integ_id in rows
            if provider not in governed
        }
        sources |= {f"integration:{p}" for p in platform_exempt}

        # Channel bots, keyed by bot rather than by provider: two Slack
        # bots can disagree, and a switch shown per configured bot has to mean
        # that bot. Nothing here is derived — a bot's tools are exempt only
        # because somebody turned this off.
        from app.models.channel_bot import ChannelBot

        bots = (
            await db.execute(
                select(ChannelBot.provider, ChannelBot.id).where(
                    ChannelBot.requires_approval.is_(False)
                )
            )
        ).all()
        sources |= {f"channel_bot:{provider}:{bot_id}" for provider, bot_id in bots}
        return frozenset(sources)
    except Exception:
        # Roll back for the same reason ``platform_read_default`` does: Postgres
        # aborts the whole transaction on a failed statement, and this runs on
        # the per-turn gate-build path, so a poisoned session would turn "gate
        # everything" into "the turn dies on its next query".
        with contextlib.suppress(Exception):
            await db.rollback()
        logger.exception("Could not resolve integration approval toggles")
        return frozenset()


async def platform_approval_by_provider(
    db: AsyncSession,
) -> tuple[frozenset[str], frozenset[str]]:
    """Per-platform approval decisions, as ``(governed, exempt)`` providers.

    A registered platform contributes tools XTM One cannot classify - they are
    not on the shipped whitelist and never can be, because we do not know what
    they do. Whether they gate is therefore decided per platform, from the
    capability it reports and the policy an operator set, rather than tool by
    tool.

    Kept separate from the per-integration toggle because a platform is one
    decision, while registration creates an integration per user: the operator
    answers once, not once per person.

    ``governed`` is every provider a registration speaks for, gating or not.
    Callers subtract it from the per-integration toggles so a platform's policy
    cannot be contradicted by the flag that predates it, and read it to tell an
    operator where the control actually lives. One read answers both, so a
    listing does not pay a query per row.
    """
    from app.models.platform_registration import PlatformRegistration
    from app.tools.approval import platform_gates_tools

    # Unregistering keeps the row and only sets the status (``platform.py``
    # ``unregister_platform``), so without this filter a platform that is gone
    # goes on governing its provider forever: its tools keep taking a policy
    # nobody can reach, and the per-integration control stays replaced by a
    # pointer to a platform that is no longer connected.
    #
    # Excluding one known-terminal status rather than requiring "active": an
    # unrecognised status keeps the platform governed, which errs toward the
    # gate rather than toward the older flag.
    rows = (
        await db.execute(
            select(
                PlatformRegistration.platform_identifier,
                PlatformRegistration.supports_approval_prompts,
                PlatformRegistration.approval_policy,
                PlatformRegistration.approval_policy_set_by,
            ).where(PlatformRegistration.status != "unregistered")
        )
    ).all()

    exempt: set[str] = set()
    governed: set[str] = set()
    for identifier, supports, policy, set_by in rows:
        providers = _platform_providers(identifier)
        governed |= providers
        if platform_gates_tools(
            supports_approval_prompts=supports,
            approval_policy=policy,
            approval_policy_set_by=set_by,
        ):
            continue
        exempt |= providers
    return frozenset(governed), frozenset(exempt)


def _platform_providers(identifier: str) -> set[str]:
    """Integration providers one registration speaks for.

    Registration under OpenAEV's legacy ``openbas`` identifier creates the
    integration under the canonical key, so such a registration governs both —
    otherwise it silently keeps gating. The pair itself lives in
    :func:`app.tools.approval.platform_identifiers`, shared with the chat path
    so a platform is resolved the same way wherever it is named.
    """
    from app.tools.approval import platform_identifiers

    return platform_identifiers(identifier)


async def known_tool_names(db: AsyncSession) -> set[str]:
    """Tool names this deployment can vouch for.

    Deliberately "known", not "existing". There is no global catalogue to check
    against: integration tools only exist once their provider is connected, MCP
    tools only once a server is bound, and between them that is thousands of
    names that come and go. So this gathers what can be answered cheaply and
    honestly:

    * the shipped default whitelist,
    * the read tools of the registered platforms (they are part of the default,
      so flagging one as unrecognised would contradict the list it appears on),
    * custom tools defined in this deployment,
    * every tool that has actually run here, harvested from run history — which
      is what organically covers integration and MCP tools.

    A name missing from this set is *unrecognised*, not *invalid*. Whitelisting
    a tool before connecting the integration that provides it is legitimate, so
    callers warn rather than refuse.
    """
    # Advisory only: this feeds name suggestions and the "unrecognised" hint, so
    # an unreadable registration table costs a few suggestions rather than the
    # whole screen. The membership *decisions* in this module raise instead.
    try:
        default: set[str] = set(await default_membership(db))
    except PlatformReadsUnavailableError:
        default = set(SHIPPED_DEFAULT_WHITELIST)
    names: set[str] = set(default)

    custom_names: set[str] = set()
    try:
        rows = await db.execute(text("SELECT name FROM custom_tools"))
        custom_names = {n for (n,) in rows.all() if n}
        names.update(custom_names)
    except Exception:  # pragma: no cover - advisory only
        # Advisory, but the session is not: a failed statement aborts the
        # transaction, and the caller reads the overlay after us.
        with contextlib.suppress(Exception):
            await db.rollback()
        logger.debug("custom tool names unavailable for whitelist validation")

    try:
        # Distinct tool names from run history. Bounded to recent runs so this
        # stays cheap — an old tool nobody has used in 500 runs is not a name an
        # administrator is likely to be typing right now.
        from app.models.assignment import AssignmentRun

        rows = await db.execute(
            select(AssignmentRun.actions_taken)
            .where(AssignmentRun.actions_taken.is_not(None))
            .order_by(AssignmentRun.created_at.desc())
            .limit(500)
        )
        names.update(tool_names_in_actions(actions for (actions,) in rows.all()))
    except Exception:  # pragma: no cover - advisory only
        with contextlib.suppress(Exception):
            await db.rollback()
        logger.debug("run history unavailable for whitelist validation")

    # ``registered`` is the set of names a tool is actually REGISTERED under —
    # the product default plus this deployment's custom tools. It must NOT
    # include the run-history names, which are the sanitised forms themselves.
    return strip_sanitised_aliases(names, default | custom_names)


def tool_names_in_actions(runs) -> set[str]:
    """Tool names in runs' ``actions_taken``, read in Python: a raw ``::jsonb``
    cast fails the whole query on one unparseable row."""
    names: set[str] = set()
    for actions in runs:
        for action in actions if isinstance(actions, list) else ():
            tool = action.get("tool") if isinstance(action, dict) else None
            if isinstance(tool, str) and tool:
                names.add(tool)
    return names


def strip_sanitised_aliases(names: set[str], registered: set[str]) -> set[str]:
    """Drop the sanitised spelling of a name that is registered differently.

    Run history records the name the MODEL saw, which is sanitised for the
    providers (``sanitize_tool_name``): a custom tool called "Address search"
    appears there as "Address_search". The gate keys off the registered name,
    so the alias is a name that can never match a whitelist entry — offering it
    would send an administrator to type the one spelling guaranteed to do
    nothing, silently, and would list one tool twice on any screen that reads
    both sources.

    Drop an alias only when it is not itself a registered name. Subtracting
    every sanitised form would remove a genuine snake_case tool that happens to
    collide with the alias of a differently-spelled one — "address_search"
    deleted because "address search" also exists — and the administrator would
    then be told their correct entry has no effect.

    ``registered`` must NOT contain run-history names: with the history folded
    in, an alias is a member of it, the subtraction below removes it from
    ``aliases``, and the very name this exists to strip is kept instead.
    Reproduced with a custom tool "Address search" and a history row
    "Address_search".
    """
    from app.tools.base import sanitize_tool_name

    aliases = {
        sanitized
        for name in registered
        if (sanitized := sanitize_tool_name(name)) != name
    } - registered
    return names - aliases


def suggest_similar(tool_key: str, known: set[str]) -> str | None:
    """Best near-match for a probable typo, or ``None``.

    Reuses the same order-independent token key the executor uses to recover
    from reordered tool names, so ``agent_delete`` finds
    ``delete_agent``. Falls back to a prefix/edit-distance-ish check for simple
    letter transpositions like ``delete_agnet``.
    """
    from app.tools.base import normalize_tool_key

    wanted = normalize_tool_key(tool_key)
    if wanted:
        for candidate in known:
            if normalize_tool_key(candidate) == wanted and candidate != tool_key:
                return candidate

    # Cheap transposition/typo catch: same length, same character multiset.
    target = sorted(tool_key)
    for candidate in known:
        if (
            candidate != tool_key
            and len(candidate) == len(tool_key)
            and sorted(candidate) == target
        ):
            return candidate
    return None
