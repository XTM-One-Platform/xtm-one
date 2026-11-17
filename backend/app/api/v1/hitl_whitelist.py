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

"""HITL whitelist administration API.

Administrators manage the single platform-wide list of tools exempt from human
approval. The effective list is the product's default with this deployment's
overrides applied on top, so responses distinguish entries that came from the
product from entries an administrator added, and surface default entries an
administrator has deliberately re-gated.

The default has two halves and the responses name which one an entry came from:
the curated shipped list, and the read-classified tools of the registered
platforms. The distinction is not cosmetic — a deployment that decides
it does not want unattended platform reads needs to re-gate dozens of names,
and doing that one chip at a time is not an escape hatch, so the platform half
gets a bulk action the curated half does not need.

Deliberately absent: any per-agent or per-assignment variant. Whether a tool
gates must be answerable from the whitelist alone, without inspecting the
caller.
"""

from __future__ import annotations

import contextlib
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.api.v1.helpers import get_user_group_ids, user_visible_query
from app.core.database import get_db
from app.models.custom_tool import CustomTool
from app.models.user import User
from app.services.audit import log_audit
from app.services.hitl_whitelist import (
    ACTION_ADD,
    ACTION_REMOVE,
    PlatformReadsUnavailableError,
    clear_override,
    default_membership,
    gate_reach,
    known_tool_names,
    load_overrides,
    platform_read_default,
    resolve_exempt_sources,
    resolve_whitelist,
    set_override,
    strip_sanitised_aliases,
    suggest_similar,
)
from app.services.hitl_whitelist_default import (
    SHIPPED_DEFAULT_WHITELIST,
    SHIPPED_WHITELIST_VERSION,
)
from app.services.hitl_whitelist_platform import (
    HAND_GATED,
    PLATFORM_PROVIDERS,
    provider_of,
)

logger = logging.getLogger(__name__)

router = APIRouter()


async def _default_or_503(db: AsyncSession) -> frozenset[str]:
    """Current default membership, or refuse the request.

    Every caller is about to record an administrator's decision about whether
    one tool asks for approval, and that decision is only meaningful against a
    known default. When the derived half cannot be read, the honest answer is
    "ask me again" — not a write against a membership set we guessed at.

    Refusing is the whole point of this helper. The alternative shipped
    briefly: the tool looked absent from the default, "require approval" took
    the already-gated branch, wrote nothing and returned 204, and the operator
    was told a tool now asks while it went on running unattended.

    503 rather than 500: nothing is wrong with the request, and retrying is the
    correct client behaviour.
    """
    try:
        return await default_membership(db)
    except PlatformReadsUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "Cannot determine which tools are currently exempt — the "
                "registered platforms could not be read. Nothing was changed; "
                "please retry."
            ),
        ) from exc


class WhitelistAddRequest(BaseModel):
    tool_key: str
    reason: str | None = None


@router.get("")
async def list_whitelist(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """The effective whitelist, with each entry attributed to its layer.

    ``removed_from_default`` is returned alongside the active entries so a
    reader sees the whole picture rather than only what is currently exempt —
    "why does this tool prompt when we ship it whitelisted?" is otherwise
    unanswerable from this endpoint.
    """
    overrides = await load_overrides(db)
    additions = {k for k, a in overrides.items() if a == ACTION_ADD}
    removals = {k for k, a in overrides.items() if a == ACTION_REMOVE}

    # Flag additions we cannot vouch for. A typo is accepted silently otherwise:
    # the entry simply never matches, the tool keeps prompting, and nothing
    # anywhere explains why. Safe (an unknown name can never un-gate a real
    # tool) but invisible, which erodes trust in the one screen that is supposed
    # to answer "what runs without asking?".
    known = await known_tool_names(db)
    # The listing degrades instead of refusing: showing the curated half of a
    # read-only screen beats a blank error page, and `platform_reads_available`
    # lets the UI say the platform section is temporarily unknown rather than
    # imply those tools were re-gated.
    try:
        platform_reads = await platform_read_default(db)
        platform_reads_available = True
    except PlatformReadsUnavailableError:
        logger.error("Platform reads unavailable while listing the whitelist")
        platform_reads = frozenset()
        platform_reads_available = False

    # Reads of the integrations and MCP servers connected here, derived by the
    # same classifier as the platform half. Listed rather than merely
    # applied: a tool that stopped asking and appears on no screen is the one
    # state this page exists to make impossible.
    from app.services.hitl_whitelist_derived import (
        connected_read_tools,
        provider_read_tools,
    )

    connected_reads = await connected_read_tools(db)
    reads_by_provider = provider_read_tools()
    provider_of_read = {
        name: provider
        for provider, names in reads_by_provider.items()
        for name in names
    }

    entries = [
        {"tool_key": key, "source": "shipped_default", "recognised": True}
        for key in sorted(SHIPPED_DEFAULT_WHITELIST - removals)
    ] + [
        # Attributed like the platform half, and for the same reason: nobody
        # chose this name, a classifier did, and the operator is owed both that
        # fact and where it came from. ``provider`` is absent for an MCP tool,
        # whose server is per-deployment rather than a known provider key.
        {
            "tool_key": key,
            "source": "connected_read",
            "provider": provider_of_read.get(key),
            "recognised": True,
        }
        for key in sorted(
            connected_reads - removals - SHIPPED_DEFAULT_WHITELIST - platform_reads
        )
    ] + [
        # Attributed to the platform that contributes them, because "why is
        # this exempt?" has a different answer here than for a curated entry:
        # nobody chose this name, a classifier did, and the operator is owed
        # both that fact and the platform it came from.
        {
            "tool_key": key,
            "source": "platform_read",
            "provider": provider_of(key),
            "recognised": True,
        }
        for key in sorted(platform_reads - removals - SHIPPED_DEFAULT_WHITELIST)
    ] + [
        {
            "tool_key": key,
            "source": "admin_added",
            "recognised": key in known,
            "suggestion": None if key in known else suggest_similar(key, known),
        }
        for key in sorted(additions)
    ]

    return {
        "shipped_default_version": SHIPPED_WHITELIST_VERSION,
        "entries": entries,
        # The CURATED half only. A re-gated platform read is a removal too, but
        # the screen renders this list under "Shipped exempt, but re-gated here"
        # with a "Restore the shipped default" control — so folding the derived
        # half in put 58 platform tools under a heading that misnames where they
        # came from. They belong to their platform's group, in
        # ``platform_reads[provider].regated``.
        # Minus BOTH derived halves. Subtracting only the platform one filed a
        # re-gated connected read under "Shipped exempt, but re-gated here",
        # beside a "Restore the shipped default" control that does not describe
        # it — the same misnaming the platform subtraction was added to fix.
        "removed_from_default": sorted(removals - platform_reads - connected_reads),
        # Per-platform totals, so the screen can offer "re-gate all reads" and
        # say what it would affect without counting chips. Reported even when
        # every read of a platform is currently re-gated: that is exactly the
        # state in which an operator needs the button that puts them back.
        "platform_reads": {
            provider: {
                "exempt": sorted(
                    k for k in platform_reads - removals if provider_of(k) == provider
                ),
                "regated": sorted(
                    k for k in platform_reads & removals if provider_of(k) == provider
                ),
            }
            for provider in sorted(PLATFORM_PROVIDERS)
            if any(provider_of(k) == provider for k in platform_reads)
        },
        # False when the registrations could not be read. Distinguishes "this
        # deployment has no platform reads" from "we could not tell", which the
        # screen must not present as the same thing.
        "platform_reads_available": platform_reads_available,
        # Names this deployment can vouch for, so the field that adds an entry
        # can suggest instead of leaving an administrator to type from memory.
        # Already computed above for the ``recognised`` flag — the screen was
        # using it to explain a mistake after the fact rather than to prevent
        # one. Suggestions only: an unknown name stays legitimate (FR-003), so
        # nothing here may narrow what can be added.
        "known": sorted(known),
    }


async def _attribution(
    db: AsyncSession, user: User
) -> tuple[dict, dict, dict, dict, dict]:
    """Which MCP server, integration provider, or channel bot each tool came from.

    Answered from what this caller can see, like everything else here. Both are
    best-effort: an unattributed tool still appears, in its own group, which is
    the honest rendering of "it has run here and we cannot say where from".
    """
    from app.models.channel_bot import ChannelBot
    from app.models.integration import Integration
    from app.models.mcp_server import MCPServer

    group_ids = await get_user_group_ids(db, user.id)
    is_admin = user.role == "admin"

    mcp: dict[str, str] = {}
    try:
        rows = await db.execute(
            user_visible_query(MCPServer, user.id, group_ids, is_admin=is_admin)
        )
        for server in rows.scalars().all():
            for tool in server.discovered_tools or []:
                name = tool.get("name") if isinstance(tool, dict) else None
                if name:
                    mcp.setdefault(name, server.name)
    except Exception:  # pragma: no cover - advisory
        with contextlib.suppress(Exception):
            await db.rollback()
        logger.debug("MCP servers unavailable for the gate catalogue")

    integrations: dict[str, str] = {}
    # Per connection, not per provider — the shape ``resolve_exempt_sources``
    # emits for a connection's own switch. Matching the bare provider form here
    # made an operator's switch read as "still gates" on this page while the
    # gate honoured it, which is the disagreement this whole helper exists to
    # prevent.
    integration_keys: dict[str, list[str]] = {}
    try:
        from app.api.v1.integration_catalog import build_provider_tools

        rows = await db.execute(
            user_visible_query(Integration, user.id, group_ids, is_admin=is_admin)
        )
        connections = [(i.provider, i.id) for i in rows.scalars().all() if i.provider]
        # Definitions are static per provider and need no credentials — the
        # same build the tool-scoping UI reads. Narrowed to providers actually
        # connected here, so the list is what this deployment can call rather
        # than the whole catalogue.
        catalogue = build_provider_tools()
        for provider, integ_id in sorted(connections, key=lambda c: str(c[0])):
            for tool in catalogue.get(provider, []):
                if tool.name:
                    integrations.setdefault(tool.name, provider)
                    integration_keys.setdefault(tool.name, []).append(
                        f"integration:{provider}:{integ_id}"
                    )
    except Exception:  # pragma: no cover - advisory
        with contextlib.suppress(Exception):
            await db.rollback()
        logger.debug("Integrations unavailable for the gate catalogue")

    # Channel bots are their own model, not integrations, and contribute real
    # tools — sending, editing and deleting messages in somebody's Slack or
    # Telegram. They were reaching this screen only after a call, for the same
    # reason the integration writes were: a separate registry nothing here read.
    channels: dict[str, str] = {}
    # Per bot, not per provider: the switch is keyed by bot id, so a name that
    # two bots both provide has to name both before it can be called exempt.
    channel_bots: dict[str, list[str]] = {}
    try:
        from app.api.v1.channel_bots import _build_catalog_tools

        # Deployment-wide and connected only, unlike everything else here.
        # Whether a bot's switch is off is a fact about the deployment, and the
        # exempt sources it is compared against are deployment-wide too — so
        # scoping this to what the caller can see made a tool read as "gates"
        # for a non-admin while the deployment had switched it off. A
        # disconnected bot provides nothing, so counting it only made the
        # "every bot must agree" rule fail on a bot that cannot run anything.
        rows = await db.execute(
            select(ChannelBot).where(ChannelBot.status == "connected")
        )
        bots = [b for b in rows.scalars().all() if b.provider]
        catalogue = _build_catalog_tools()
        for bot in sorted(bots, key=lambda b: b.provider):
            for tool in catalogue.get(bot.provider, []):
                if tool.name:
                    channels.setdefault(tool.name, bot.provider)
                    # The whole source, so this compares against exactly what
                    # ``resolve_exempt_sources`` emits rather than rebuilding
                    # the shape and drifting from it.
                    channel_bots.setdefault(tool.name, []).append(
                        f"channel_bot:{bot.provider}:{bot.id}"
                    )
    except Exception:  # pragma: no cover - advisory
        with contextlib.suppress(Exception):
            await db.rollback()
        logger.debug("Channel bots unavailable for the gate catalogue")

    return mcp, integrations, channels, channel_bots, integration_keys


async def _tool_names_from_visible_runs(db: AsyncSession, user: User) -> set[str]:
    """Tool names harvested from the runs *user* may see.

    Bounded to recent runs, like ``known_tool_names``: a tool nobody has called
    in the last few hundred runs is not one this screen has to rule on today,
    and the unbounded scan is not worth its cost on a busy deployment.

    Advisory — an unreadable history costs a few rows here, never the screen.
    """
    try:
        from app.api.v1.helpers import run_visible_filter
        from app.models.assignment import AssignmentRun
        from app.services.hitl_whitelist import tool_names_in_actions

        rows = await db.execute(
            select(AssignmentRun.actions_taken)
            .where(
                AssignmentRun.actions_taken.is_not(None),
                run_visible_filter(user.id),
            )
            .order_by(AssignmentRun.created_at.desc())
            .limit(500)
        )
        return tool_names_in_actions(actions for (actions,) in rows.all())
    except Exception:  # pragma: no cover - advisory
        with contextlib.suppress(Exception):
            await db.rollback()
        logger.debug("run history unavailable for the gate catalogue")
        return set()


@router.get("/catalog")
async def tool_gate_catalog(
    # Any authenticated user, unlike the rest of this router: whether a tool
    # pauses is what the tools list shows everyone, and a user grants their own
    # standing approval from that same row (FR-045). Only the *changing* of the
    # platform whitelist stays admin-only, which is the endpoints below.
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Every tool this deployment can name, and whether it pauses.

    Grouped by where the tool comes from, because that is what decides who can
    do anything about it:

    * ``builtin`` — product tools, classified on the shipped list.
    * ``custom`` — this deployment's own sandboxed scripts.
    * ``platform`` — a registered platform's read tools, exempted by
      classification rather than by anyone's choice.
    * ``other`` — MCP and integration tools, which exist only once a server is
      bound or a provider connected. They can never be on the shipped list (we
      do not know what they do), so they are the ones an operator has to rule
      on, and FR-045 gives them their own section.

    Names come from ``known_tool_names``, so the ``other`` group is what has
    actually run here rather than a catalogue we cannot build. A tool nobody has
    called yet is absent — honest, and the same limitation the whitelist's
    "unrecognised" hint already carries.

    The caller's own standing approvals are deliberately NOT here: they come
    from ``/tool-approvals``, which every surface showing them already reads.
    Two sources for one fact would be two things to invalidate.

    ``gates`` is the whitelist decision only. An integration's own
    ``requires_approval`` and a platform's approval policy sit on their own
    surfaces (FR-047, FR-048) and are not folded in here — a single boolean
    covering three mechanisms would say "gates: false" for a reason the reader
    cannot see.
    """
    from app.services.builtin_tool_catalog import (
        build_builtin_tool_catalog,
        withhold_concierge_tools,
        withhold_evaluation_tools,
        withhold_flow_builder_tools,
    )

    # Same withholding the settings catalogue applies, for the same reason and
    # with more at stake: this is the list every user sees under Tools →
    # Approvals, so thirteen sub-tools naming an unannounced feature would be
    # announced here first. Filtered at the call site, not in the builder —
    # that doubles as the registry of valid built-in ids, and the flag is
    # UI-only and must not reject a skill package that references the tool.
    _builtin_catalog = build_builtin_tool_catalog()
    # The flow functions of the platform group.
    withhold_flow_builder_tools(_builtin_catalog)
    withhold_concierge_tools(_builtin_catalog)
    # The evaluation tools, while evaluation is off (#5503).
    withhold_evaluation_tools(_builtin_catalog)

    # Kept as name -> builtin id, not just a set: the tools list shows builtin
    # *groups* ("Work Items"), so a row there covers several names and can only
    # say what it covers by aggregating them.
    builtin_group = {
        d["name"]: group
        for group, defs in _builtin_catalog.items()
        for d in defs
        if d.get("name")
    }
    builtin = set(builtin_group)

    # Scoped to what this caller may see — own, shared with them, and
    # company-managed — exactly as ``/custom-tools`` scopes the list this sits
    # beside. A flat ``SELECT name FROM custom_tools`` named other people's
    # private tools to everyone, which is a disclosure and not merely a
    # confusing row.
    custom: set[str] = set()
    try:
        group_ids = await get_user_group_ids(db, user.id)
        rows = await db.execute(
            user_visible_query(
                CustomTool, user.id, group_ids, is_admin=user.role == "admin"
            )
        )
        custom = {t.name for t in rows.scalars().all() if t.name}
    except Exception:  # pragma: no cover - advisory, as in known_tool_names
        with contextlib.suppress(Exception):
            await db.rollback()
        logger.debug("custom tool names unavailable for the gate catalogue")

    try:
        platform_reads = await platform_read_default(db)
        platform_reads_available = True
    except PlatformReadsUnavailableError:
        logger.error("Platform reads unavailable while listing the gate catalogue")
        platform_reads = frozenset()
        platform_reads_available = False

    reach: dict[str, set[str]] = {}
    exempt = await resolve_whitelist(db, reach=reach)
    overrides = await load_overrides(db)

    # What the default exempts RIGHT NOW, for ``regated``. A ``remove`` override
    # is only written while its tool is in the default, but the default is
    # derived and so outlives it — a platform disconnected, a tool reclassified
    # as a write, a release that drops it. The override then still exists while
    # nothing exempts the tool any more, and calling that "re-gated" made the
    # row offer to restore a shipped default that no longer covers it, which
    # ``add_to_whitelist`` answers by pinning an ``add`` instead.
    #
    # Degrades like the block above rather than refusing: this is the read
    # endpoint every tools list calls, and a missing registration costs a
    # ``regated`` flag rather than the page.
    try:
        default_now = await default_membership(db)
    except PlatformReadsUnavailableError:
        default_now = frozenset(SHIPPED_DEFAULT_WHITELIST)
    # MCP and integration tools exist only once a server is bound or a provider
    # connected, so the honest source is what has actually run — and only in
    # runs this caller may see, for the same reason the custom tools above are
    # scoped. ``known_tool_names`` is the admin-screen variant of this and reads
    # every run, which is why it is not reused here.
    known = await _tool_names_from_visible_runs(db, user)
    (
        mcp_by_name,
        integration_by_name,
        channel_by_name,
        bots_by_name,
        integration_keys_by_name,
    ) = await _attribution(db, user)
    # The per-connection switches — an integration's ``requires_approval``, a
    # platform's policy, a bot's own switch — exempt by SOURCE rather than by
    # name, so the whitelist alone cannot answer "does this pause?". Reading
    # only the whitelist is what made a bot's switch appear to do nothing on
    # this screen while it worked at the gate.
    exempt_sources = await resolve_exempt_sources(db)

    # The platform switch outranks every layer below it, so while it is off
    # nothing pauses and a row claiming otherwise is simply wrong — the same
    # reason Settings takes the whitelist off the screen with it. Read here
    # rather than in each consumer: the badges on the cards, the tables, the
    # tool pages and this section would each have had to remember, and one
    # forgetting is a screen that lies.
    #
    # The user's own allow-all is deliberately NOT folded in: it is per person,
    # and a tool that read as exempt for one reviewer and gated for another
    # would make this list unusable as a description of the deployment. The
    # surfaces say that one in a banner instead.
    platform_off = False
    try:
        from app.models.platform_settings import PlatformSettings

        platform_off = (
            await db.scalar(select(PlatformSettings.tool_approvals_enabled)) is False
        )
    except Exception:
        with contextlib.suppress(Exception):
            await db.rollback()
        logger.debug("Could not read the platform approval switch for the catalogue")
    # History records the spelling the MODEL saw, which is sanitised for the
    # providers; the gate keys off the canonical name (``evaluate_gate``'s
    # ``tool_key``, post-alias-resolution). Without this a custom tool called
    # "Multiple by random" is listed twice — once under its real name, and once
    # as "Multiple_by_random" under a heading saying nothing claims it, where
    # whitelisting it would do nothing at all.
    known = strip_sanitised_aliases(
        known,
        builtin
        | custom
        | set(platform_reads)
        | set(mcp_by_name)
        | set(integration_by_name)
        | set(channel_by_name),
    )

    def _source(key: str) -> str:
        """Where a tool comes from, and so who can do anything about it.

        Platform reads are checked before integrations on purpose: a connected
        OpenCTI contributes the same names its platform registration does, and
        it is the platform layer that exempts them, so that is the answer an
        operator needs to see.
        """
        if key in builtin:
            return "builtin"
        if key in custom:
            return "custom"
        if key in platform_reads:
            return "platform"
        if key in mcp_by_name:
            return "mcp"
        if key in integration_by_name:
            return "integration"
        if key in channel_by_name:
            return "channel"
        return "other"

    def _origin(key: str, source: str) -> str | None:
        """What contributes it — a platform, an MCP server, or a provider."""
        if source == "platform":
            return provider_of(key)
        if source == "mcp":
            return mcp_by_name.get(key)
        if source == "integration":
            return integration_by_name.get(key)
        if source == "channel":
            return channel_by_name.get(key)
        return None

    def _switched_off(key: str, source: str) -> bool:
        """Whether a per-connection switch already exempts this tool.

        The whitelist answers by name; these answer by source, and a screen
        reading only the first told an administrator their bot switch had done
        nothing while it worked perfectly well at the gate.

        A name two bots both provide is exempt only when **both** are switched
        off: at runtime each definition carries its own bot's id, so a mixed
        answer means some calls still pause, and the honest summary of that is
        that it pauses.
        """
        if source == "channel":
            ids = bots_by_name.get(key) or []
            return bool(ids) and all(i in exempt_sources for i in ids)
        if source == "integration":
            # A registered platform's policy exempts its provider PROVIDER-wide,
            # and ``resolve_exempt_sources`` emits the bare form for it — which
            # is what ``evaluate_gate`` matches. Only a governed provider's READ
            # tools resolve as ``platform`` here; its writes fall through to this
            # branch, so without the bare check this screen reported "pauses and
            # asks" for calls the gate ran unattended.
            provider = _origin(key, source)
            if provider and f"integration:{provider}" in exempt_sources:
                return True
            # Otherwise every connection offering this name must be switched off.
            # One of three Notion connections exempted means calls through the
            # other two still pause, and the honest summary is that it pauses.
            keys = integration_keys_by_name.get(key) or []
            return bool(keys) and all(k in exempt_sources for k in keys)
        if source == "platform":
            # A registered platform's policy is provider-wide by design, so the
            # bare form is the right one to look for here.
            provider = _origin(key, source)
            return bool(provider) and f"integration:{provider}" in exempt_sources
        return False

    from app.tools.approval import whitelist_reaches

    row_reach = gate_reach(reach)
    # The ``ToolDefinition.source`` shape of each group, for the reach check the
    # gate applies. MCP and unattributed rows stay keyed by name: a row stands
    # for every server offering the name, and the gate is stricter per server.
    gate_source_of_group = {
        "builtin": "builtin",
        "custom": "custom",
        "platform": "integration:",
        "integration": "integration:",
        "channel": "channel_bot:",
    }

    def _whitelisted(key: str, source: str) -> bool:
        """Whether the whitelist exempts the tool this row describes.

        The whitelist is keyed by bare name and the gate asks who an entry
        covers, so a built-in whose name a company-managed MCP server also
        advertises, or a custom tool named after a derived read, pauses there
        and must not read as exempt here.
        """
        if key not in exempt:
            return False
        gate_source = gate_source_of_group.get(source)
        if gate_source is None:
            return True
        return whitelist_reaches(
            key, tool_source=gate_source, tool_entity_id=None, reach=row_reach
        )

    def _exempt_source(key: str, source: str) -> str | None:
        """Why a tool is exempt — the layer, not merely the fact.

        An administrator's addition is named first: it is the one an audit asks
        about, and the only one somebody here chose.
        """
        if not _whitelisted(key, source):
            return None
        if overrides.get(key) == ACTION_ADD:
            return "admin_added"
        if key in SHIPPED_DEFAULT_WHITELIST:
            return "shipped_default"
        if key in platform_reads:
            return "platform_read"
        # Derived from the classifier for a connected integration or MCP server.
        # Reached only when nothing above claimed it, so the layers
        # keep their precedence.
        return "connected_read"

    # The full tool set of what is connected, not only what has run. A
    # provider's definitions and a bound server's discovered tools are both
    # known up front, and leaving them out hid the half that matters: OpenCTI
    # contributes 63 tools, of which 18 are classified reads — the other 45
    # gate, and they appeared only once somebody had already called one.
    # History is left to contribute the remainder, which is what "not
    # attributed" then means.
    tools = []
    for key in sorted(
        builtin
        | custom
        | set(platform_reads)
        | set(mcp_by_name)
        | set(integration_by_name)
        | set(channel_by_name)
        | known
        # An administrator's own addition, even when nothing here provides the
        # name — whitelisting a tool ahead of connecting its integration is
        # supported, and a typo is the other reason a name matches nothing.
        # Both are invisible if the row is absent, which is the one state this
        # page exists to prevent: an exemption on no screen at all.
        | {k for k, a in overrides.items() if a == ACTION_ADD}
    ):
        source = _source(key)
        switched_off = _switched_off(key, source)
        tools.append(
            {
                "tool_key": key,
                "source": source,
                "group": builtin_group.get(key),
                "provider": _origin(key, source),
                "gates": (
                    not platform_off
                    and not _whitelisted(key, source)
                    and not switched_off
                ),
                # The switch is named ahead of the whitelist layer when both
                # apply: it is the one somebody here flipped, and the one they
                # will flip back.
                # Named outermost-first: while the platform switch is off it
                # is the whole answer, whatever the layers underneath say.
                "exempt_source": (
                    "platform_switch"
                    if platform_off
                    else "connection_switch"
                    if switched_off
                    else _exempt_source(key, source)
                ),
                # Gated because somebody here re-gated it, not because nothing
                # ever exempted it. Without the distinction the row cannot
                # answer "we ship this exempt — why does it prompt?", and the
                # control on it would offer to whitelist a tool whose default
                # already does.
                "regated": (
                    overrides.get(key) == ACTION_REMOVE and key in default_now
                ),
                # Classified a read, gated anyway by a human decision in the
                # product. Without this the row cannot answer the question it
                # provokes — "it says Read, so why does it pause?" — and the
                # answer is not in the whitelist, the switches, or anything an
                # operator here can change.
                "hand_gated": key in HAND_GATED,
            }
        )

    return {
        "tools": tools,
        # False when the registrations could not be read: the platform group is
        # then unknown rather than empty, and the screen must not present the
        # two as the same thing.
        "platform_reads_available": platform_reads_available,
    }


@router.post("", status_code=201)
async def add_to_whitelist(
    body: WhitelistAddRequest,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Exempt a tool from human approval.

    Any tool may be added, destructive ones included. The API does not refuse —
    the safeguards are the warning the UI shows before saving and this audit
    record, not a hard-coded list the product declines to change.
    """
    tool_key = body.tool_key.strip()
    if not tool_key:
        raise HTTPException(status_code=400, detail="tool_key cannot be empty")

    known = await known_tool_names(db)
    recognised = tool_key in known
    suggestion = None if recognised else suggest_similar(tool_key, known)

    overrides = await load_overrides(db)
    in_default = tool_key in await _default_or_503(db)
    if in_default and tool_key not in overrides:
        # Already exempt with nothing overriding it, so there is nothing to do
        # — and pinning an ``add`` would be actively wrong for the same reason
        # the branch below clears rather than pins: a tool frozen exempt by an
        # ``add`` row keeps running unattended after a release that drops it
        # from the default or reclassifies it as a write. It would also list the
        # tool twice, once per layer. Reachable by typing a name that is already
        # exempt, which the suggestions do not offer but the field accepts.
        outcome = "already_exempt"
    elif in_default and overrides.get(tool_key) == ACTION_REMOVE:
        # Undoing a removal, not making an addition. Clearing the override
        # returns the tool to tracking the default — pinning an ``add``
        # instead would exempt it today and then keep exempting it after a
        # release that drops it from the default, which is the overlay
        # outliving the decision it was meant to record. True of a derived
        # platform read as much as of a curated entry: if a later release
        # reclassifies the tool as a write, a pinned ``add`` would keep it
        # running unattended.
        await clear_override(db, tool_key)
        outcome = (
            "restored_shipped_default"
            if tool_key in SHIPPED_DEFAULT_WHITELIST
            else "restored_platform_read"
        )
    else:
        await set_override(
            db,
            tool_key=tool_key,
            action=ACTION_ADD,
            user_id=user.id,
            reason=body.reason,
        )
        outcome = "admin_added"
    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="hitl_whitelist",
        entity_name=tool_key,
        summary=f"Exempted '{tool_key}' from human approval",
        details={
            "tool_key": tool_key,
            "reason": body.reason,
            "was_in_shipped_default": tool_key in SHIPPED_DEFAULT_WHITELIST,
            "was_in_default": in_default,
            "recognised": recognised,
            "outcome": outcome,
        },
        request=request,
    )
    await db.commit()
    # Accepted either way — refusing would break whitelisting a tool ahead of
    # connecting the integration that provides it.
    return {
        "tool_key": tool_key,
        "source": _default_source(tool_key, outcome),
        "changed": outcome != "already_exempt",
        "recognised": recognised,
        "suggestion": suggestion,
    }


def _default_source(tool_key: str, outcome: str) -> str:
    """Which layer an accepted addition now belongs to.

    The screen colours and explains an entry by its layer, so an add that
    merely undid a removal must report the layer the tool went back to rather
    than claiming the administrator introduced it.
    """
    if outcome == "restored_shipped_default":
        return "shipped_default"
    if outcome == "restored_platform_read":
        return "platform_read"
    if outcome == "already_exempt":
        # Report the layer it is already exempt under, so the caller is not told
        # an administrator added something nobody added.
        return (
            "shipped_default"
            if tool_key in SHIPPED_DEFAULT_WHITELIST
            else "platform_read"
        )
    return "admin_added"


class PlatformReadBulk(BaseModel):
    """How many entries a bulk platform-read action moved."""

    provider: str
    changed: int
    tool_keys: list[str]


@router.post("/platform-reads/{provider}/require-approval")
async def require_approval_for_platform_reads(
    provider: str,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> PlatformReadBulk:
    """Re-gate every read tool one platform contributes.

    The counterpart to deriving the list at all. A deployment that does not
    want unattended platform reads has a real objection, and answering it with
    "remove these 62 chips one at a time" is the kind of escape hatch nobody
    uses — which would leave the derived default effectively unopposable.

    Durable ``remove`` rows, exactly as re-gating one tool by hand records, so
    the decision survives upgrades and survives the platform reconnecting.
    """
    if provider not in PLATFORM_PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown platform {provider}")

    overrides = await load_overrides(db)
    targets = sorted(
        key
        for key in await _default_or_503(db)
        if provider_of(key) == provider and overrides.get(key) != ACTION_REMOVE
    )
    for tool_key in targets:
        await set_override(
            db, tool_key=tool_key, action=ACTION_REMOVE, user_id=user.id
        )

    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="hitl_whitelist",
        entity_name=provider,
        summary=f"Required human approval for {len(targets)} {provider} read tools",
        details={
            "provider": provider,
            "outcome": "removed_from_platform_read_bulk",
            "tool_keys": targets,
        },
        request=request,
    )
    await db.commit()
    return PlatformReadBulk(
        provider=provider, changed=len(targets), tool_keys=targets
    )


@router.post("/platform-reads/{provider}/restore-default")
async def restore_platform_reads(
    provider: str,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> PlatformReadBulk:
    """Undo a bulk re-gate, returning this platform's reads to the default.

    Clears the ``remove`` rows rather than pinning ``add`` rows: the tools go
    back to *tracking* the classifier, so one that is reclassified as a write
    in a later release starts gating again instead of being frozen exempt by
    the click that restored it.
    """
    if provider not in PLATFORM_PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown platform {provider}")

    overrides = await load_overrides(db)
    targets = sorted(
        key
        for key in await _default_or_503(db)
        if provider_of(key) == provider and overrides.get(key) == ACTION_REMOVE
    )
    for tool_key in targets:
        await clear_override(db, tool_key)

    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="hitl_whitelist",
        entity_name=provider,
        summary=f"Exempted {len(targets)} {provider} read tools from human approval",
        details={
            "provider": provider,
            "outcome": "restored_platform_read_bulk",
            "tool_keys": targets,
        },
        request=request,
    )
    await db.commit()
    return PlatformReadBulk(
        provider=provider, changed=len(targets), tool_keys=targets
    )


@router.delete("/{tool_key}", status_code=204)
async def remove_from_whitelist(
    tool_key: str,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Re-gate a tool, so it pauses for approval again.

    For a tool in the product's default — curated or a derived platform read —
    this records a durable ``remove`` override, which survives upgrades: a later
    release that re-adds the tool to the default must not silently undo a
    deliberate decision to gate it. For a tool an administrator had added, it
    simply clears that addition.
    """
    overrides = await load_overrides(db)

    if overrides.get(tool_key) == ACTION_ADD:
        await clear_override(db, tool_key)
        outcome = "cleared_admin_addition"
    elif tool_key in await _default_or_503(db):
        await set_override(
            db, tool_key=tool_key, action=ACTION_REMOVE, user_id=user.id
        )
        outcome = (
            "removed_from_shipped_default"
            if tool_key in SHIPPED_DEFAULT_WHITELIST
            else "removed_from_platform_read"
        )
    else:
        # Already gated — nothing to change. Not an error: the caller's intent
        # ("this tool must require approval") is satisfied.
        outcome = "already_gated"

    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="hitl_whitelist",
        entity_name=tool_key,
        summary=f"Required human approval for '{tool_key}'",
        details={"tool_key": tool_key, "outcome": outcome},
        request=request,
    )
    await db.commit()
