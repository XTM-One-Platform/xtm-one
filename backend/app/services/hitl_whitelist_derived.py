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

"""Read-classified tools of everything else this deployment has connected.

``hitl_whitelist_platform`` derives the exempt reads of the registered Suite
platforms from the tool classifier. This module applies the same rule to the
rest of what a deployment connects — its integrations and its bound MCP servers
— because the alternative was arbitrary: ``search_opencti`` ran without asking
while ``search_notion`` paused, for no reason a person could state except which
registry the tool happened to live in.

Two boundaries are deliberate.

**Scoped to what is connected.** A provider nobody has connected exempts
nothing. Its tools do not exist here, and listing four hundred inert names would
make the one screen answering "what runs without asking?" unreadable.

**Only company-managed MCP servers count.** A personal server is one user's own
binding and needs no administrator, so honouring it here would let anybody name
a tool into the deployment-wide exempt set.

**Channel bots are excluded.** ``read_slack_bot_messages`` classifies as a read
and is one — but it reads *people's conversations*, not platform data. That is a
different decision from exempting ``list_agents``, and it is not the
classifier's to make. Bot tools therefore keep asking until somebody says
otherwise, per tool, on the record.

The derivation is advisory in the same way the platform one is: a provider whose
builder raises contributes nothing rather than half a tool set, since half would
mean half its tools silently start asking again — a product bug's shape, not an
import failure's.
"""

from __future__ import annotations

import contextlib
import logging
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: Successful introspections, per provider, accumulated across calls. A builder
#: that raised is never stored here — caching a failed pass gates that
#: provider's reads until the pod restarts, decided by whichever request warmed
#: it first, which is the failure the previous "complete passes only" rule
#: existed to prevent. Keeping the successes is what that rule cost: one broken
#: provider re-introspected every other one on every gate resolution — once per
#: turn, per run, twice per ``GET /hitl-whitelist``.
_PROVIDER_READS_CACHE: dict[str, frozenset[str]] = {}

#: ``{provider: monotonic deadline}`` for providers whose builder raised. Before
#: the deadline the provider contributes nothing and is not retried; after it,
#: one call retries it. Bounded staleness in the safe direction: its reads keep
#: asking meanwhile.
_PROVIDER_RETRY_AFTER: dict[str, float] = {}

#: How long a failed provider stays un-retried.
_FAILED_PROVIDER_TTL_SECONDS = 60.0

#: Where the per-session memo of :func:`connected_read_tools` lives. Scoped to
#: one ``AsyncSession`` — i.e. one request or one turn — so it can never serve a
#: stale answer to a later one.
_SESSION_CACHE_KEY = "hitl_connected_read_tools"


#: Channel-bot tool names, introspected once. Only a complete pass is stored:
#: under-excluding is the unsafe direction here.
_CHANNEL_BOT_NAMES_CACHE: frozenset[str] | None = None


def _channel_bot_tool_names() -> frozenset[str]:
    """Every tool name the channel-bot builders declare."""
    global _CHANNEL_BOT_NAMES_CACHE
    if _CHANNEL_BOT_NAMES_CACHE is not None:
        return _CHANNEL_BOT_NAMES_CACHE

    from app.api.v1.integration_catalog import build_dummy_token
    from app.services.integration_tools import CHANNEL_BOT_TOOLS
    from app.tools.base import CallableToolExecutor

    names: set[str] = set()
    complete = True
    for provider, builder in CHANNEL_BOT_TOOLS.items():
        try:
            definitions = builder(
                build_dummy_token(provider),
                CallableToolExecutor(),
                refresh_fn=lambda: None,
            )
        except Exception:
            logger.exception(
                "Could not introspect %s tools — its names are not excluded by "
                "name this pass",
                provider,
            )
            complete = False
            continue
        names.update(d.name for d in definitions if getattr(d, "name", None))
    resolved = frozenset(names)
    if complete:
        _CHANNEL_BOT_NAMES_CACHE = resolved
    return resolved


def excluded_from_derivation() -> frozenset[str]:
    """Names no derivation may exempt, whatever the classifier says.

    Both boundaries the module docstring promises, resolved in one place so the
    two entry points cannot apply different ones — which is exactly how the MCP
    branch came to honour neither.

    * ``HAND_GATED``: a hand-gate outranks the classifier.
    * Channel-bot tools: they read *people's conversations*, not platform data.
      The integration branch excludes them structurally — ``CHANNEL_BOT_TOOLS``
      is a separate registry from ``PROVIDER_TOOLS`` — but that protects only
      that branch. A company-managed MCP server advertises whatever names it
      likes and the whitelist matches bare names, so the boundary has to be
      enforced by name on that path.
    """
    from app.services.hitl_whitelist_platform import HAND_GATED

    return frozenset(HAND_GATED) | _channel_bot_tool_names()


def provider_read_tools() -> dict[str, frozenset[str]]:
    """``{provider: read-classified tool names}`` for every integration provider.

    Built with a dummy credential purely to harvest ``ToolDefinition`` metadata
    — the same introspection the integration catalogue and the platform
    derivation already perform, sharing their token helper so a provider that
    validates its token format does not early-return an empty list.
    """
    from app.api.v1.integration_catalog import build_dummy_token
    from app.services.integration_tools import PROVIDER_TOOLS
    from app.services.tool_classification import infer_tool_type
    from app.tools.base import CallableToolExecutor

    excluded = excluded_from_derivation()

    # Built per provider rather than through ``build_provider_tools``, which
    # catches a failing builder internally and stores ``[]`` — indistinguishable
    # from a provider that genuinely has no tools. The pass then looked complete
    # and got memoised, so one transient introspection failure gated that
    # provider's reads for the life of the process: exactly the failure this
    # cache's "complete passes only" rule exists to prevent.
    derived: dict[str, frozenset[str]] = {}
    now = time.monotonic()
    for provider, builder in PROVIDER_TOOLS.items():
        cached = _PROVIDER_READS_CACHE.get(provider)
        if cached is not None:
            derived[provider] = cached
            continue
        if now < _PROVIDER_RETRY_AFTER.get(provider, 0.0):
            derived[provider] = frozenset()
            continue
        try:
            definitions = builder(
                build_dummy_token(provider),
                CallableToolExecutor(),
                refresh_fn=lambda: None,
            )
        except Exception:
            logger.exception(
                "Could not introspect %s tools — its reads keep asking", provider
            )
            derived[provider] = frozenset()
            _PROVIDER_RETRY_AFTER[provider] = now + _FAILED_PROVIDER_TTL_SECONDS
            continue
        derived[provider] = frozenset(
            d.name
            for d in definitions
            if getattr(d, "name", None)
            and infer_tool_type(d.name) == "read"
            # A hand-gate outranks the classifier, and channel-bot tools are
            # never derived. ``download_opencti_file`` classifies as a read and
            # is one — of an arbitrary third-party document pulled into the
            # model's context, which is why somebody gated it by hand.
            and d.name not in excluded
        )
        _PROVIDER_READS_CACHE[provider] = derived[provider]
        _PROVIDER_RETRY_AFTER.pop(provider, None)
    return derived


def reset_cache() -> None:
    """Drop the memoised providers. For tests, which change the classifier."""
    global _CHANNEL_BOT_NAMES_CACHE
    _PROVIDER_READS_CACHE.clear()
    _PROVIDER_RETRY_AFTER.clear()
    _CHANNEL_BOT_NAMES_CACHE = None


async def connected_read_tools(
    db: AsyncSession, *, reach: dict[str, set[str]] | None = None
) -> frozenset[str]:
    """Read-classified tools of the integrations and MCP servers connected here.

    Deployment-wide, not per user: the whitelist answers "does this tool gate?"
    without inspecting the caller, so scoping it to one person's visible
    connections would make the same tool exempt for one user and gated for
    another — the one thing the whitelist must never be.

    Failure is advisory, unlike the platform derivation: a deployment with an
    unreadable integrations table still has a shipped list and its platform
    reads, and gating a few extra tools is the safe direction.

    Memoised on the session, not globally. ``default_membership`` runs on every
    gate resolution — once per agent turn, per assignment run, and twice per
    ``GET /hitl-whitelist`` — and these two reads answered identically each
    time within one request. A process-wide cache would be wrong instead of
    merely slow: a provider connected a moment ago would keep gating, and one
    disconnected a moment ago would keep running unattended.

    *reach*, when given, receives who each name covers (``whitelist_reaches``):
    an integration read covers the product's own tool of that name, an MCP read
    only the tools of the server it was derived from.
    """
    from app.models.integration import Integration
    from app.models.mcp_server import MCPServer
    from app.tools.approval import REACH_PRODUCT

    cache = getattr(db, "info", None)
    if isinstance(cache, dict) and _SESSION_CACHE_KEY in cache:
        cached_names, cached_reach = cache[_SESSION_CACHE_KEY]
        _merge_reach(reach, cached_reach)
        return cached_names

    names: set[str] = set()
    derived_reach: dict[str, set[str]] = {}

    try:
        rows = await db.execute(select(Integration.provider).distinct())
        connected = {p for (p,) in rows.all() if p}
        by_provider = provider_read_tools()
        for provider in connected:
            for name in by_provider.get(provider, frozenset()):
                names.add(name)
                derived_reach.setdefault(name, set()).add(REACH_PRODUCT)
    except Exception:
        # Roll back before continuing, for the reason ``platform_read_default``
        # documents at length: Postgres aborts the whole transaction on a failed
        # statement, so without this every later query on the caller's session
        # fails too — and this runs on that session, from inside
        # ``default_membership``, with plenty still to do afterwards.
        with contextlib.suppress(Exception):
            await db.rollback()
        logger.exception("Could not derive integration reads — they keep asking")

    try:
        from app.services.tool_classification import infer_tool_type

        excluded = excluded_from_derivation()

        # Company-managed servers only, and enabled ones. This set is exempt
        # DEPLOYMENT-wide, while binding a *personal* MCP server needs no admin
        # (``create_mcp_server`` gates only the company-managed flag) — so an
        # unscoped read here let any user un-gate a name for everybody by
        # declaring a read-shaped tool on a server of their own. The whitelist
        # matches by name, so the name they chose need not be theirs.
        rows = await db.execute(
            select(MCPServer.id, MCPServer.discovered_tools).where(
                MCPServer.is_company_managed.is_(True),
                MCPServer.enabled.is_(True),
            )
        )
        for server_id, discovered in rows.all():
            for tool in discovered or []:
                name = tool.get("name") if isinstance(tool, dict) else None
                # The module's exclusions apply here too, and only by name:
                # ``discovered_tools`` is whatever a remote endpoint advertised,
                # and the whitelist matches bare names, so a company-managed
                # server declaring a hand-gated or channel-bot name would
                # un-gate the real tool deployment-wide. The reach recorded
                # below is what keeps any other name it declares - a gated
                # built-in's, another server's - to this server's own tools.
                if (
                    name
                    and infer_tool_type(name) == "read"
                    and name not in excluded
                ):
                    names.add(name)
                    derived_reach.setdefault(name, set()).add(str(server_id))
    except Exception:
        with contextlib.suppress(Exception):
            await db.rollback()
        logger.exception("Could not derive MCP reads — they keep asking")

    resolved = frozenset(names)
    frozen_reach = {name: frozenset(who) for name, who in derived_reach.items()}
    if isinstance(cache, dict):
        cache[_SESSION_CACHE_KEY] = (resolved, frozen_reach)
    _merge_reach(reach, frozen_reach)
    return resolved


def _merge_reach(
    into: dict[str, set[str]] | None, reach: dict[str, frozenset[str]]
) -> None:
    if into is None:
        return
    for name, who in reach.items():
        into.setdefault(name, set()).update(who)
