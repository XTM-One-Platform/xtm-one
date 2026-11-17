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

"""Which integrations make an agent reachable over A2A.

Three providers carry a partner relationship, and binding ANY of them to an
agent is the whole opt-in: there is no separate "expose" switch, because a
partner credential that reaches nothing and an exposed agent no partner can
reach are both states an operator has to reconcile by hand. Binding is the
single act, in the one place every other capability is attached (the agent
configuration's Tools tab).

* :data:`OAUTH_A2A_PROVIDER` — the platform mints a client id / secret; the
  partner drives the OAuth client-credentials flow itself. Works for any
  partner that can re-run a token exchange (Google ADK / Vertex).
* :data:`ENTRA_A2A_PROVIDER` — the partner presents its own Microsoft Entra
  token and this platform verifies it against pinned facts. For Azure-side
  partners that cannot refresh a client-credentials token (Foundry).
* :data:`GOOGLE_A2A_PROVIDER` — the Google-side analogue of the Entra one:
  the partner presents its own Google-issued token, verified against pinned
  facts (``a2a.google_federation``). Outbound, it presents a service-account
  assertion to Vertex, which is the credential family neither sibling has.

None of them contributes agent tools. They are trust configuration, pointing
inbound or outbound according to the direction on their settings row.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.a2a import A2AIntegrationSettings
    from app.models.agent import Agent
    from app.models.integration import Integration

#: Provider keys of the integrations-catalog entries. Mirrored in the
#: frontend (``hooks/api/useA2AApi.ts``) and in ``integration_catalog``.
OAUTH_A2A_PROVIDER = "oauth_a2a"
ENTRA_A2A_PROVIDER = "entra_a2a"

#: The Google-side mechanism. Outbound it presents a service-account
#: assertion to Vertex, which is the credential family neither sibling has —
#: outbound OAuth presents a client-credentials bearer, and Vertex requires a
#: JWT signed with the service account's own key, exchanged at
#: ``cloud-platform`` scope. Inbound it is the Google analogue of the Entra
#: mechanism: the partner presents its OWN Google-issued token and this
#: platform verifies it against pins (see :mod:`app.a2a.google_federation`).
#:
#: The inbound half exists ALONGSIDE ``oauth_a2a`` inbound because the two
#: are not one state: ``oauth_a2a`` requires this platform to mint and the
#: partner to hold a shared secret, while this one holds no secret on either
#: side and lets Google refresh the caller's own credential forever — the
#: same reason ``entra_a2a`` exists beside ``oauth_a2a``.
GOOGLE_A2A_PROVIDER = "google_a2a"

#: Every provider whose binding can expose an agent. Membership is what the
#: implicit-exposure hook, the auth path and the UI all key on, so a fourth
#: partner mechanism joins by being added HERE and nowhere else.
#:
#: With ONE deliberate exception, named so the claim above stays honest: the
#: convergence predicate in migration ``a2a2intmodel1`` spells the keys as SQL
#: literals. A migration must keep meaning what it meant on the day it ran, so
#: it cannot read a constant that later changes underneath it — and a migration
#: that has already run is not re-run for a provider added afterwards, so there
#: is nothing there to update either. The predicate names two keys while this
#: set holds three (``google_a2a`` postdates that migration), and
#: ``test_migration_predicate_matches_binds_a2a`` pins that the predicate
#: never names a key this set lacks.
A2A_PROVIDERS = frozenset(
    {OAUTH_A2A_PROVIDER, ENTRA_A2A_PROVIDER, GOOGLE_A2A_PROVIDER}
)

#: Every provider key that is A2A at all, in either direction. Equal to
#: :data:`A2A_PROVIDERS` today, because every shipped mechanism can serve both
#: — but not the same QUESTION: this one asks "is this key A2A", that one asks
#: "can binding it expose an agent". They diverge again the moment a
#: single-direction mechanism ships, and call sites already say which they
#: mean, so the two names are kept rather than collapsed.
ALL_A2A_PROVIDERS = frozenset(A2A_PROVIDERS)


#: Prefixes of a federated partner's derived registration identity, one per
#: mechanism that admits a token the partner minted itself. The id each one
#: precedes is the integration's own, which is what lets a federated caller be
#: attributed without a lookup — see :func:`partner_client_id`.
#:
#: Distinct per provider rather than one shared "federated" prefix, because
#: the prefix is also how :func:`federated_provider` answers WHICH mechanism
#: admitted a call. One prefix would make every federated submission read as
#: the same partner mechanism wherever origin is displayed.
ENTRA_CLIENT_PREFIX = "a2a-entra-"
GOOGLE_CLIENT_PREFIX = "a2a-google-"

#: Ordered longest-prefix-first so a future prefix that extends another cannot
#: be matched by the shorter one. Neither current pair overlaps; the ordering
#: is what keeps that a property of the data rather than of luck.
_FEDERATED_PREFIXES: tuple[tuple[str, str], ...] = tuple(
    sorted(
        (
            (ENTRA_CLIENT_PREFIX, ENTRA_A2A_PROVIDER),
            (GOOGLE_CLIENT_PREFIX, GOOGLE_A2A_PROVIDER),
        ),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
)


#: The mechanisms whose partners authenticate with a token they minted
#: themselves, and therefore carry a DERIVED registration identity rather than
#: one this platform issued. Derived from the prefix table so the two can
#: never disagree about which providers have a derived namespace.
FEDERATED_A2A_PROVIDERS = frozenset(key for _prefix, key in _FEDERATED_PREFIXES)


def partner_client_id(integration_id: uuid.UUID, provider: str) -> str:
    """The registration identity of a federated partner.

    A federated caller presents no platform-minted credential, yet the task
    surface needs a stable "who is calling" string: it scopes task reads,
    keys the per-registration rate budgets, deduplicates submissions and
    names the actor in the audit trail. Before exposure became implicit that
    string was borrowed from the OAuth registration minted alongside the
    exposure — which no longer exists, because nothing mints one when a
    binding creates the exposure.

    Deriving it from the INTEGRATION is the right identity anyway: the
    partner *is* the integration. One partner therefore keeps one budget and
    one task scope across every agent it reaches, and two partners bound to
    the same agent stay separated — which borrowing a per-agent registration
    could never express.

    Distinct by prefix from minted ids (``a2a-<random>``), so the two
    namespaces cannot collide, and distinct per mechanism so attribution can
    read the mechanism back off the id.

    ``provider`` is required rather than defaulted: a default here would
    silently stamp a partner with another mechanism's identity, and every
    surface that displays origin would then name the wrong mechanism with no
    way to tell.
    """
    for prefix, key in _FEDERATED_PREFIXES:
        if key == provider:
            return f"{prefix}{integration_id}"
    raise ValueError(f"{provider!r} does not admit federated callers")


def federated_provider(client_id: str | None) -> str | None:
    """Which mechanism minted a derived registration id, or ``None``.

    ``None`` for a platform-minted ``a2a-<random>`` id, which belongs to no
    mechanism by its shape — its owner lives in a column.
    """
    if not client_id:
        return None
    for prefix, key in _FEDERATED_PREFIXES:
        if client_id.startswith(prefix):
            return key
    return None


async def registration_integration_id(db, client_id: str | None) -> uuid.UUID | None:
    """Which integration a CALLING registration belongs to, or ``None``.

    The inverse of "which ids can this partner call under"
    (:func:`partner_client_ids`), and the question any surface asks when it has
    a caller and needs the partner: an inbound task records the registration id
    it arrived under, not the integration.

    Two shapes, mirroring the two credential families:

    * a **derived** id (``a2a-entra-…`` / ``a2a-google-…``) carries the
      integration in the id itself — no query;
    * a **minted** id is random, and its owner lives in
      ``OAuthClient.a2a_integration_id``.

    ``None`` for a per-exposure registration from the old explicit toggle,
    whose credential was never owned by an integration. Callers MUST treat that
    as "partner unknown" rather than falling back to a guess: the whole point
    of asking is to scope something to one partner, and a wrong answer would
    scope it to another.
    """
    if not client_id:
        return None
    derived = partner_integration_id(client_id)
    if derived is not None:
        return derived

    from sqlalchemy import select

    from app.models.oauth_client import OAuthClient

    return (
        await db.execute(
            select(OAuthClient.a2a_integration_id).where(
                OAuthClient.client_id == client_id,
                OAuthClient.a2a_integration_id.isnot(None),
            )
        )
    ).scalar_one_or_none()


def bound_integration_ids(agent: object, provider: str | None = None) -> list[uuid.UUID]:
    """The agent's A2A integration bindings, as UUIDs.

    ``agent`` is anything with an ``integration_bindings`` attribute: an
    ``Agent`` row, or the projection the graph read carries
    (``app.a2a.liaisons.LiaisonAgent``).

    ``provider`` narrows to one key; the default spans every A2A provider.
    Bindings are JSON, so a malformed entry is data rather than an error —
    it is skipped, exactly as the auth path skips it.
    """
    bindings = getattr(agent, "integration_bindings", None) or {}
    # ALL keys, not just the inbound-capable ones: an agent binds the
    # outbound-only mechanism to obtain a delegation tool, and a binding this
    # function cannot see is a tool the run never gets.
    keys = [provider] if provider else sorted(ALL_A2A_PROVIDERS)
    out: list[uuid.UUID] = []
    for key in keys:
        for raw in bindings.get(key) or []:
            try:
                out.append(uuid.UUID(str(raw)))
            except ValueError:
                continue
    return out


def directions_for(session, integration_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Map integration id -> direction, for the ids that HAVE a settings row.

    An id absent from the returned mapping has no row, and callers must treat
    it as belonging to NEITHER direction. That is the fail-closed rule this
    whole design rests on: a missing row degrades to "not exposed, no tool",
    which is visible and recoverable, and can never become an unintended
    exposure. The migration backfill and the create path both guarantee a
    row, so an absence means a bug — and a bug here must under-permit.

    Takes a live ``Session`` rather than an ``AsyncSession`` because its first
    caller is the ``before_flush`` listener in :mod:`app.a2a.hooks`, which is
    synchronous and already issues queries on the flushing session.
    """
    if not integration_ids:
        return {}
    from sqlalchemy import select

    from app.models.a2a import A2AIntegrationSettings

    stmt = select(
        A2AIntegrationSettings.integration_id,
        A2AIntegrationSettings.direction,
    ).where(A2AIntegrationSettings.integration_id.in_(integration_ids))

    # ``no_autoflush`` is not decoration. This runs inside ``before_flush``,
    # and an autoflush triggered by our own SELECT would re-enter the flush
    # that is already in progress — the listener would observe a half-written
    # unit of work and could queue an exposure against an agent row that is
    # about to be rolled back.
    with session.no_autoflush:
        rows = session.execute(stmt).all()
    return {row[0]: row[1] for row in rows}


def inbound_integration_ids(session, agent: Agent) -> list[uuid.UUID]:
    """The agent's bindings that actually EXPOSE it.

    The whole reason this function exists: ``bound_integration_ids`` answers
    "does this agent bind an A2A integration", and exposure needs "does it
    bind an INBOUND one". Testing membership alone would create an exposure
    for an agent bound purely to obtain a delegation tool — a fail-open
    regression on a surface that is already live.
    """
    from app.models.a2a import DIRECTION_INBOUND

    ids = bound_integration_ids(agent)
    directions = directions_for(session, ids)
    return [i for i in ids if directions.get(i) == DIRECTION_INBOUND]


#: There is deliberately no ``outbound_integration_ids`` mirroring the above:
#: the only thing that needs the question — the delegation-tool builder in
#: ``services.integration_tools`` — receives ``effective_bindings``, which
#: also carries session-override and template origins, and an agent-row query
#: would silently drop a partner bound by those routes. The asymmetry with
#: the inbound half is real: inbound persists per-agent state (an exposure
#: row) that an agent-level query must serve, while outbound persists none —
#: direction is read per integration, where the tool is built.


def binds_a2a(session, agent: Agent) -> bool:
    """Whether this agent is exposed — i.e. binds an INBOUND A2A integration.

    Takes a session because direction lives on a settings row, not in the
    binding key — and takes it as the FIRST positional argument so a call
    site omitting it fails loudly rather than answering wrongly.
    """
    return bool(inbound_integration_ids(session, agent))


async def agents_binding(
    db, integration_id: uuid.UUID, provider: str
) -> list[Agent]:
    """Every agent that binds *integration_id* under *provider*'s key.

    The one spelling of that question, shared by the auth path's exposure
    lookup and both providers' connectivity probes. Bindings are JSON, so it
    takes two steps: a text ``contains`` narrows in SQL to rows mentioning
    the id at all, then :func:`bound_integration_ids` confirms it under the
    provider's own key — an id echoed under a different provider is not a
    binding, and only that second step knows the difference.
    """
    from sqlalchemy import Text, cast, select

    from app.models.agent import Agent

    candidates = (
        (
            await db.execute(
                select(Agent).where(
                    cast(Agent.integration_bindings, Text).contains(
                        str(integration_id)
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        a for a in candidates if integration_id in bound_integration_ids(a, provider)
    ]


def partner_integration_id(client_id: str | None) -> uuid.UUID | None:
    """The integration a federated registration id was derived from.

    Only a FEDERATED id carries one, under either mechanism's prefix. A
    platform-minted registration is a random string whose owning integration
    lives in a column, not in the id.
    """
    if not client_id:
        return None
    for prefix, _key in _FEDERATED_PREFIXES:
        if client_id.startswith(prefix):
            try:
                return uuid.UUID(client_id[len(prefix) :])
            except ValueError:
                return None
    return None


async def partner_client_ids(db, integration) -> list[str]:
    """Every registration identity *integration* can have called under.

    The single definition of "which external traffic is this partner's",
    shared by the last-activity read and the run listing so the two can never
    disagree about what an integration has done.

    Attribution is by the calling registration recorded on each task, never by
    "some task on an agent this integration is bound to". Two partners may be
    bound to one agent, and an agent may carry history from before this
    integration existed — counting those would report a partner as active
    before it had ever connected, and would make two A2A integrations on the
    same agent show each other's traffic.

    Three sources, and the third only for federation:

    * **OAuth A2A** — the credentials minted for it, which carry
      ``a2a_integration_id``;
    * **a federated mechanism** (Entra or Google) — its derived identity
      (:func:`partner_client_id`), under that mechanism's own prefix;
    * **legacy exposures**, for federation ONLY. An exposure minted by the old
      explicit toggle carries its own registration, and a federated partner
      authenticating against that exposure still calls under it, so a Foundry
      partner predating the integration model would otherwise lose its whole
      history. Deliberately NOT extended to ``oauth_a2a``: a per-exposure
      credential was never owned by an OAuth A2A integration — those always
      carry ``a2a_integration_id`` — so claiming it would report a freshly
      created partner as already active, exactly the misattribution this
      function exists to avoid. The residual ambiguity is narrow and inherent
      to the older shape: on a legacy exposure that ALSO had an OAuth partner,
      those tasks are attributed here too, because the wire recorded one id for
      both credential forms.
    """
    from sqlalchemy import Text, cast, select

    from app.models.a2a import A2AExposure
    from app.models.agent import Agent
    from app.models.oauth_client import OAuthClient

    client_ids: list[str] = list(
        (
            await db.execute(
                select(OAuthClient.client_id).where(
                    OAuthClient.a2a_integration_id == integration.id
                )
            )
        )
        .scalars()
        .all()
    )
    # Only for a mechanism that admits federated callers. An ``oauth_a2a``
    # partner always carries a minted registration, so it has no derived id —
    # and asking for one would raise rather than invent a namespace.
    if integration.provider in FEDERATED_A2A_PROVIDERS:
        client_ids.append(partner_client_id(integration.id, integration.provider))

    if integration.provider == ENTRA_A2A_PROVIDER:
        legacy = (
            (
                await db.execute(
                    select(OAuthClient.client_id)
                    .join(
                        A2AExposure,
                        A2AExposure.id == OAuthClient.a2a_exposure_id,
                    )
                    .join(Agent, Agent.id == A2AExposure.agent_id)
                    .where(
                        cast(Agent.integration_bindings, Text).contains(
                            str(integration.id)
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        client_ids.extend(legacy)

    return client_ids


async def resolve_submitter_providers(
    db, contexts: dict[object, dict | None]
) -> dict[object, str]:
    """Which A2A provider submitted each run, keyed as *contexts* was.

    Resolved ONCE, server-side, from what each registration is **owned by** —
    never from what its id looks like. That distinction is the whole reason this
    exists. Both credential forms mint ``a2a-<random>`` ids, so a shape-based
    guess is wrong for any federated caller on a legacy exposure, and wrong
    *differently* on different pages: the same run then reads "Foundry A2A" on
    the integration that claims it and "OAuth A2A" in the agent's own activity
    log. One answer, computed here, is what makes every surface agree.

    Four cases, in descending order of certainty:

    1. ``a2a_provider`` stamped on the run — the credential form that actually
       verified, recorded at submission. Exact, and the only exact answer for a
       legacy registration.
    2. A derived federated id (``a2a-entra-…`` / ``a2a-google-…``). Exact:
       nothing else mints those shapes, and the PREFIX names the mechanism —
       which is why the two federated mechanisms do not share one.
    3. A registration owned by an integration (``a2a_integration_id``). Exact —
       that column IS the ownership, so the integration's provider is the answer.
    4. A per-exposure registration (``a2a_exposure_id``, the old explicit
       toggle), or one whose row is gone because its exposure was revoked.
       **Omitted from the result**, and that is the point: a legacy registration
       genuinely served both credential forms, and no column records which was
       presented. A key missing here means "unattributable", which callers
       should show as plain external work rather than resolving to a guess.

    Two batched queries at most, and none at all when every run is stamped —
    which is every run submitted since the stamp existed.
    """
    from sqlalchemy import select

    from app.models.integration import Integration
    from app.models.oauth_client import OAuthClient

    out: dict[object, str] = {}
    pending: dict[object, str] = {}
    for key, context in contexts.items():
        if not context or context.get("source") != "a2a":
            continue
        stamped = context.get("a2a_provider")
        if stamped in A2A_PROVIDERS:
            out[key] = stamped
            continue
        client_id = context.get("client_id")
        if not isinstance(client_id, str):
            continue
        derived = federated_provider(client_id)
        if derived is not None and partner_integration_id(client_id) is not None:
            out[key] = derived
            continue
        pending[key] = client_id

    if not pending:
        return out

    owners = dict(
        (
            await db.execute(
                select(OAuthClient.client_id, OAuthClient.a2a_integration_id).where(
                    OAuthClient.client_id.in_(set(pending.values())),
                    OAuthClient.a2a_integration_id.isnot(None),
                )
            )
        ).all()
    )
    if not owners:
        return out

    providers = dict(
        (
            await db.execute(
                select(Integration.id, Integration.provider).where(
                    Integration.id.in_(set(owners.values()))
                )
            )
        ).all()
    )
    for key, client_id in pending.items():
        provider = providers.get(owners.get(client_id))
        if provider in A2A_PROVIDERS:
            out[key] = provider
    return out


# --- Direction lifecycle --------------------------------------------------
# Creating, reading and changing the direction of an A2A integration. Kept
# beside the direction LOOKUP above rather than in ``a2a.client`` because both
# directions are managed here and the outbound package must not become the
# owner of a value the inbound hook depends on.


class DirectionChangeRefusedError(ValueError):
    """A direction change was refused because the integration is bound.

    Carries the agent names so the caller can tell the operator what to
    unbind. A refusal that only says "it is in use" makes the operator hunt.
    """

    def __init__(self, agent_names: list[str]):
        self.agent_names = agent_names
        joined = ", ".join(agent_names) if agent_names else "one or more agents"
        super().__init__(
            "Unbind this integration before changing its direction. "
            f"Currently bound to: {joined}."
        )


async def create_settings(db, integration_id: uuid.UUID, direction: str):
    """Give a new A2A integration its settings row.

    NOT optional. An A2A integration without a row belongs to neither
    direction, so skipping this leaves an integration that can never expose
    and never delegate — inert in a way an operator would read as a bug in
    the feature rather than a missing row.
    """
    from app.models.a2a import DIRECTION_INBOUND, DIRECTION_OUTBOUND, A2AIntegrationSettings

    if direction not in (DIRECTION_INBOUND, DIRECTION_OUTBOUND):
        raise ValueError(f"direction must be inbound or outbound, got {direction!r}")
    settings = A2AIntegrationSettings(
        integration_id=integration_id, direction=direction
    )
    db.add(settings)
    await db.flush()
    return settings


def requested_direction(provider: str, requested: str | None) -> str | None:
    """The direction a create of *provider* gives its integration.

    ``None`` for a provider that is not A2A (and refused with ``ValueError``
    when a direction is still asked for); for an A2A provider the requested
    direction, inbound when none is given. Every create path asks this one
    function, so the REST API and the agent tools default the same way.
    """
    from app.models.a2a import DIRECTION_INBOUND, DIRECTION_OUTBOUND

    requested = (requested or "").strip() or None
    if provider not in ALL_A2A_PROVIDERS:
        if requested is not None:
            raise ValueError("a2a_direction applies only to A2A integrations.")
        return None
    direction = requested or DIRECTION_INBOUND
    if direction not in (DIRECTION_INBOUND, DIRECTION_OUTBOUND):
        raise ValueError("a2a_direction must be 'inbound' or 'outbound'.")
    return direction


def federation_config_problem(provider: str, direction: str | None, extra_config: dict[str, object]) -> str | None:
    """Why an INBOUND federation config does not validate, or ``None``.

    The SAME parser the A2A auth path applies per request (Entra's three
    pinned facts, Google's audience and allowed caller), so a config every
    create and update path accepts is exactly one whose tokens the task
    surface will consider. Only an inbound registration's config is a set of
    pins: an outbound one holds card URLs and our own credentials.
    """
    from app.models.a2a import DIRECTION_INBOUND

    if direction != DIRECTION_INBOUND:
        return None
    if provider == ENTRA_A2A_PROVIDER:
        from app.a2a.federation import EntraPinsError, parse_entra_pins

        try:
            parse_entra_pins(extra_config)
        except EntraPinsError as exc:
            return str(exc)
    elif provider == GOOGLE_A2A_PROVIDER:
        from app.a2a.google_federation import GooglePinsError, parse_google_pins

        try:
            parse_google_pins(extra_config)
        except GooglePinsError as exc:
            return str(exc)
    return None


async def provision_settings(db: AsyncSession, integration: Integration, direction: str) -> A2AIntegrationSettings:
    """Give a just-created A2A integration its settings row and targets.

    The one step every create path runs right after adding the integration:
    the settings row (:func:`create_settings`) and, outbound, the agents the
    operator's card URLs name (:func:`sync_targets`), so a registration is
    never briefly configured-but-unreachable. ``tests/test_a2a_settings_guard.py``
    fails on a create path of an A2A integration that does not call it.
    """
    from app.models.a2a import DIRECTION_OUTBOUND

    if getattr(integration, "id", None) is None:
        integration.id = uuid.uuid4()
    settings = await create_settings(db, integration.id, direction)
    await _converge_inbound(db, integration.id, direction)
    if direction == DIRECTION_OUTBOUND:
        from app.a2a.client.credentials import configured_card_urls

        await sync_targets(db, integration.id, configured_card_urls(integration))
    return settings


async def _converge_inbound(db, integration_id: uuid.UUID, direction: str) -> None:
    """Expose the agents already bound to a registration that just became inbound.

    A binding made while the integration had no settings row was neither
    direction, so the flush listener minted nothing; creating the row changes
    no agent and would not wake it (#11786).
    """
    from app.models.a2a import DIRECTION_INBOUND

    if direction != DIRECTION_INBOUND:
        return
    from app.a2a.hooks import converge_exposures

    await converge_exposures(db, integration_id)
    await db.flush()


async def directions_of(db: AsyncSession, integration_ids: Iterable[uuid.UUID]) -> dict[str, str]:
    """The direction of each integration that has a settings row, by id."""
    from sqlalchemy import select

    from app.models.a2a import A2AIntegrationSettings

    ids = list(integration_ids)
    if not ids:
        return {}
    rows = await db.execute(
        select(A2AIntegrationSettings.integration_id, A2AIntegrationSettings.direction).where(
            A2AIntegrationSettings.integration_id.in_(ids)
        )
    )
    return {str(integration_id): direction for integration_id, direction in rows.all()}


async def get_settings(db, integration_id: uuid.UUID):
    """The settings row for one integration, or ``None``."""
    from sqlalchemy import select

    from app.models.a2a import A2AIntegrationSettings

    result = await db.execute(
        select(A2AIntegrationSettings).where(
            A2AIntegrationSettings.integration_id == integration_id
        )
    )
    return result.scalar_one_or_none()


async def change_direction(db, integration, new_direction: str) -> None:
    """Change an integration's direction, or refuse.

    **Refused while bound** (FR-057). The alternative — applying immediately —
    would mass-revoke every exposure the integration created, or silently hand
    every bound agent a new outbound tool, from a dropdown. Requiring the
    operator to unbind first makes that consequence explicit and impossible to
    trigger by accident. It also means no exposure or tool is ever live across
    a direction change, which is why this feature needs no ``Integration``
    flush listener: the hook watches agents, and nothing about an agent
    changes here.

    **Credentials are cleared** on a permitted change (FR-058). A partner
    credential kept for a direction the integration no longer serves is a
    stored secret with no purpose and no expiry.
    """
    from app.models.a2a import DIRECTION_INBOUND, DIRECTION_OUTBOUND

    if new_direction not in (DIRECTION_INBOUND, DIRECTION_OUTBOUND):
        raise ValueError(f"direction must be inbound or outbound, got {new_direction!r}")

    settings = await get_settings(db, integration.id)
    if settings is None:
        # No row: treat as a first assignment rather than a change. Reachable
        # only for a row predating the backfill, and creating it is strictly
        # better than leaving the integration inert. The agents that bound it
        # while it had no row were exposed by nothing: converge them now.
        await create_settings(db, integration.id, new_direction)
        await _converge_inbound(db, integration.id, new_direction)
        return
    if settings.direction == new_direction:
        return  # Assignment of an unchanged value is not a change.

    bound = await agents_binding(db, integration.id, integration.provider)
    if bound:
        raise DirectionChangeRefusedError([a.name for a in bound])

    settings.direction = new_direction
    # Everything the previous direction stored is now meaningless, and the
    # verification state belongs to a card this integration no longer fetches.
    integration.client_id_enc = None
    integration.client_secret_enc = None
    integration.api_key_enc = None
    integration.access_token_enc = None
    integration.refresh_token_enc = None
    integration.extra_config_enc = None
    integration.token_expires_at = None
    # Every target belonged to the direction being left: an outbound one
    # described agents this integration will no longer task, and an inbound
    # one has none. Deleted rather than blanked, because a target IS a card —
    # a row with nothing verified about it is not a cleared registration, it
    # is an agent nobody asked to reach.
    for target in await outbound_targets(db, integration.id):
        await db.delete(target)
    # The columns above are what this platform PRESENTS; the ``OAuthClient``
    # row is what the partner presents back, and it is the only one that still
    # authenticates (FR-058). ``_client_covers_exposure`` admits it on binding
    # alone, with no direction check, so a registration flipped and rebound
    # would otherwise still take the old partner's tasks.
    from sqlalchemy import delete as _delete

    from app.models.oauth_client import OAuthClient

    await db.execute(
        _delete(OAuthClient).where(OAuthClient.a2a_integration_id == integration.id)
    )
    await db.flush()


async def outbound_targets(db, integration_id: uuid.UUID) -> list:
    """Every agent this registration can task, oldest first.

    Stable order because it decides the order tools reach a model, and a set
    that reshuffles between runs makes a prompt cache miss for no reason.
    """
    from sqlalchemy import select

    from app.models.a2a import A2AClientTarget

    result = await db.execute(
        select(A2AClientTarget)
        .where(A2AClientTarget.integration_id == integration_id)
        .order_by(A2AClientTarget.created_at, A2AClientTarget.id)
    )
    return list(result.scalars().all())


async def get_target(db, target_id: uuid.UUID):
    """One target by id, or ``None``."""
    from sqlalchemy import select

    from app.models.a2a import A2AClientTarget

    result = await db.execute(
        select(A2AClientTarget).where(A2AClientTarget.id == target_id)
    )
    return result.scalar_one_or_none()


async def reconcile_configured_targets(
    db, integration_id: uuid.UUID, *, previous, current
) -> tuple[int, int]:
    """Apply an edit to the operator's card-URL list, and only that edit.

    :func:`sync_targets` matches the table to one list, which is right on
    creation and wrong on every later save: by then the table also holds the
    agents discovery added, which the operator never typed, and matching their
    list would delete each of them — verdict, pin and accepted fingerprint —
    on a save that merely rotated a secret. So a save removes what is in
    ``previous`` and not in ``current``, adds the reverse, and leaves the rest.
    """
    removed = {str(u).strip() for u in previous} - {str(u).strip() for u in current}
    kept = [
        t.card_url
        for t in await outbound_targets(db, integration_id)
        if t.card_url not in removed
    ]
    return await sync_targets(db, integration_id, [*current, *kept])


async def sync_targets(db, integration_id: uuid.UUID, card_urls) -> tuple[int, int]:
    """Make the stored targets match the operator's list. Returns (added, removed).

    Reconciled rather than replaced, and that distinction is the whole point:
    rewriting the set on every save would discard the verdict, the pinned
    origin and the accepted-card fingerprint of every target an operator did
    not touch, so correcting a typo in one URL would silently re-open the
    card-change gate on all the others.

    A URL that is still present keeps its row untouched. A new one arrives
    unverified — never inheriting a sibling's verdict, which would assert
    something about an agent nobody has looked at. A URL that is gone takes
    its row with it, because a target an operator removed must stop
    contributing a tool immediately rather than at the next sweep.
    """
    from sqlalchemy import select

    from app.models.a2a import A2AClientTarget

    wanted: list[str] = []
    for raw in card_urls or []:
        url = str(raw).strip()
        # De-duplicated here as well as by the unique constraint: an operator
        # pasting the same URL twice is a typo, not an error worth refusing a
        # whole save over.
        if url and url not in wanted:
            wanted.append(url)

    existing = {
        t.card_url: t
        for t in (
            await db.execute(
                select(A2AClientTarget).where(
                    A2AClientTarget.integration_id == integration_id
                )
            )
        )
        .scalars()
        .all()
    }

    added = 0
    for url in wanted:
        if url in existing:
            continue
        db.add(A2AClientTarget(integration_id=integration_id, card_url=url))
        added += 1

    removed = 0
    for url, row in existing.items():
        if url not in wanted:
            await db.delete(row)
            removed += 1

    await db.flush()
    return added, removed
