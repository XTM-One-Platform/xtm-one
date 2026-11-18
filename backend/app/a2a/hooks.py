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

"""Automatic exposure revocation on agent tier, ownership or sharing changes.

Wired as a session-level ``before_flush`` listener rather than as a call in
the agent-update endpoint. That is deliberate: FR-041 must hold no matter
which code path changes the agent — the REST update handler, an admin tool, a
bulk operation, a future importer. A hook on one endpoint would leave the
others silently exposing a partner to a boundary nobody re-approved.

The listener is intentionally narrow. It looks only at ``Agent`` instances,
checks only the attributes in :data:`_WATCHED` — the ones that can change a
boundary or the authority needed to expose it — and does nothing at all when
none of them moved, so the common flush pays one ``isinstance`` check per
pending or dirty object.

It reads ``session.new`` as well as ``session.dirty``, and the two are handled
differently on purpose. Only an EDITED agent can have a boundary that moved, so
revocation looks at ``dirty`` alone; a NEW agent can only ever become exposed,
and must never be mistaken for one whose tier just changed.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, event, inspect, select
from sqlalchemy.orm import Session, SessionTransaction

from app.a2a import metrics as a2a_metrics
from app.core.session_transactions import enclosing_transaction as _enclosing
from app.core.session_transactions import innermost_transaction
from app.core.session_transactions import transaction_within as _within
from app.core.utils import format_exception_message
from app.models.a2a import (
    EXPOSED_BOUND,
    REVOKED_AGENT_DISABLED,
    REVOKED_OWNER_CHANGE,
    REVOKED_SHARING_CHANGE,
    REVOKED_TIER_CHANGE,
    REVOKED_UNBOUND,
    A2AExposure,
)
from app.models.agent import Agent

logger = logging.getLogger(__name__)

#: Attribute -> revocation reason. ``is_company_managed`` changes the boundary
#: itself; the sharing fields change who may keep the agent exposed; ``enabled``
#: makes it unreachable.
#:
#: ``user_id`` is watched because a personally-owned agent's run-as identity IS
#: its owner: an owner transfer (``helpers.apply_owner_transfer``) touches no
#: other watched attribute, and an unwatched one would leave a live exposure
#: running the partner's work inside the PREVIOUS owner's data boundary — the
#: silent widening FR-041 exists to prevent, the tier flip being merely its
#: more obvious form.
#:
#: The per-user grant columns are watched for the same reason as the
#: group ones: ``can_manage`` honours a direct ``config`` grant, so a per-user
#: grant can be the entire authority under which someone exposed the agent —
#: revoking that grant must revoke the exposure, exactly as revoking the
#: equivalent group grant does.
#:
#: ``access_restricted`` is deliberately NOT here. It narrows a
#: company-managed agent's INTERNAL visibility, and neither thing this module
#: guards moves with it: the exposure's boundary is the run-as user, and the
#: authority to expose a company-managed agent is administrator-only with or
#: without the flag (it is rejected on personal agents, where grants confer
#: authority). Revoking a partner integration because an admin narrowed the
#: internal audience would tear down what remains a deliberate admin decision.
_WATCHED: dict[str, str] = {
    "is_company_managed": REVOKED_TIER_CHANGE,
    "user_id": REVOKED_OWNER_CHANGE,
    "shared_group_ids": REVOKED_SHARING_CHANGE,
    "shared_group_permissions": REVOKED_SHARING_CHANGE,
    "shared_user_ids": REVOKED_SHARING_CHANGE,
    "shared_user_permissions": REVOKED_SHARING_CHANGE,
    "enabled": REVOKED_AGENT_DISABLED,
}


def _normalise(value: Any) -> Any:
    """Compare group lists order-insensitively; leave everything else alone."""
    if isinstance(value, list):
        return frozenset(str(v) for v in value)
    return value


def _really_changed(state: Any, attr: str) -> bool:
    """True only when *attr*'s value actually differs from its loaded value.

    ``History.has_changes()`` is not enough. SQLAlchemy reports a change on
    *assignment*, even of an identical value, and ``agents.update_agent``
    assigns ``shared_group_ids``/``shared_group_permissions`` (and ``user_id``)
    whenever the field is present in the payload - so on assignment alone,
    re-saving an agent without touching its sharing would silently revoke a
    live exposure, dropping a partner integration over an unrelated edit.
    """
    history = state.attrs[attr].history
    if not history.has_changes():
        return False
    old = _normalise(history.deleted[0]) if history.deleted else None
    new = _normalise(history.added[0]) if history.added else None
    return old != new


def _revocation_reason(agent: Agent) -> str | None:
    """Return why *agent*'s exposure must be revoked, or None."""
    state = inspect(agent)
    for attr, reason in _WATCHED.items():
        if not _really_changed(state, attr):
            continue
        if attr == "enabled":
            # Only DISABLING matters. Re-enabling an agent must not tear down
            # an exposure that is live and still correctly bounded.
            history = state.attrs[attr].history
            if history.added and history.added[0] is False:
                return reason
            continue
        if attr == "user_id" and agent.is_company_managed:
            # A company-managed agent's ``user_id`` is provenance — whoever
            # seeded it — and the boundary does not follow it: the run-as
            # identity is resolved once and recorded on the exposure, whether
            # it was nominated or inherited (``exposure.resolve_run_as``).
            # Transferring the agent therefore moves nothing a partner can
            # reach, and revoking here would break a live integration over a
            # change that cannot affect it. Matches
            # ``exposure.exposure_invalidated``, which encodes the same
            # asymmetry and is what confirms it per-exposure below.
            continue
        return reason
    return None


#: Exposure counter increments waiting for the transaction that carries them.
_PENDING_METRICS = "_a2a_pending_exposure_metrics"


def _queue_exposure_metric(session: Session, action: str, reason: str) -> None:
    """Count an exposure change once it is durable, not when it is flushed.

    Every change is decided inside a flush listener, before COMMIT, and a
    Prometheus counter cannot be corrected afterwards: incrementing here would
    count a change that a failed flush, a rollback or a caller that never
    commits then discards. So the increment waits on ``session.info``, tagged
    with the innermost transaction that can end on its own (the SAVEPOINT in
    progress, else the root), until :func:`_after_commit` counts it on the
    durable commit or :func:`_after_transaction_end` drops it with a
    transaction that ended without committing it.

    Only what goes through the flush is seen: a bulk ``update(Agent)`` or
    ``delete(Agent)`` statement never reaches ``before_flush`` (none exists
    today), so it would be neither enforced nor counted.
    """
    txn = innermost_transaction(session)
    session.info.setdefault(_PENDING_METRICS, []).append((txn, action, reason))


def _after_commit(session: Session) -> None:
    """Count the queued increments once the root transaction commits.

    ``after_commit`` also fires when a SAVEPOINT is released, and that is not
    the durable commit: the savepoint's entries become the enclosing
    transaction's, so they go with it if it rolls back and count when the root
    commits. During a release the session still reports the savepoint as its
    nested transaction (measured, see
    ``persistent_memory._schedule_hook_delivery``).

    Never raises: the COMMIT is durable by now, and raising would report a
    failure for data that did commit.
    """
    try:
        pending = session.info.pop(_PENDING_METRICS, None)
        if not pending:
            return
        released = session.get_nested_transaction()
        if released is not None:
            parent = _enclosing(released.parent)
            session.info[_PENDING_METRICS] = [
                (parent if _within(txn, released) else txn, action, reason)
                for txn, action, reason in pending
            ]
            return
        for _txn, action, reason in pending:
            a2a_metrics.record_exposure_change(action, reason)
    except Exception as exc:
        logger.warning(
            "a2a: exposure changes committed but not counted: %s",
            format_exception_message(exc),
        )


def _after_transaction_end(session: Session, transaction: SessionTransaction) -> None:
    """Drop the increments of a transaction that ended without committing them.

    A root commit has counted its queue by now and a released SAVEPOINT has
    handed its entries on, so whatever is still tagged with the ended
    transaction, or with one inside it, was discarded: rolled back, lost to a
    failed flush or COMMIT, or closed. ``after_soft_rollback`` cannot decide
    this: a flush that fails reports its own subtransaction, not the savepoint
    it rolled back, and the ``begin_nested()`` block then CLOSES that savepoint
    rather than rolling it back, while ``Session.close()`` fires no rollback
    event at all - and ``session.info`` outlives both. Once the root ends
    nothing can commit the queue any more, so all of it goes.

    Never raises, and fails towards not counting.
    """
    try:
        pending = session.info.get(_PENDING_METRICS)
        if not pending:
            return
        if transaction.parent is None:
            del session.info[_PENDING_METRICS]
            return
        kept = [entry for entry in pending if not _within(entry[0], transaction)]
        if len(kept) != len(pending):
            session.info[_PENDING_METRICS] = kept
    except Exception as exc:
        session.info.pop(_PENDING_METRICS, None)
        logger.warning(
            "a2a: discarded the queued exposure counts: %s",
            format_exception_message(exc),
        )


def _run_as_inactive(session: Session, exposure: A2AExposure) -> bool:
    """Whether *exposure*'s run-as account is deactivated, pending change included.

    ``get`` answers from the identity map first, so an account deactivated
    earlier in the same unit of work reads as such before it is flushed.
    """
    from app.models.user import User

    if exposure.run_as_user_id is None:
        return False
    with session.no_autoflush:
        user = session.get(User, exposure.run_as_user_id)
    return user is not None and user.is_active is False


def _revoke_live_exposures(session: Session, agent: Agent, reason: str) -> bool:
    """Revoke *agent*'s live exposures. True when any actually were.

    The return value is load-bearing, not a convenience. Having a reason is
    not the same as having torn something down: there may be no live
    exposure, and an owner change is confirmed per-exposure below and often
    survives. Callers that narrow behaviour on "a teardown happened" must
    key on THIS, never on the reason.
    """
    from app.a2a.exposure import exposure_invalidated

    agent_id = agent.id
    live = session.execute(
        select(A2AExposure).where(
            A2AExposure.agent_id == agent_id,
            A2AExposure.revoked_at.is_(None),
        )
    ).scalars().all()
    if not live:
        return False

    now = datetime.now(UTC)
    revoked_ids: list[uuid.UUID] = []
    for exposure in live:
        # For an owner change, confirm the identity actually moved before
        # tearing anything down — ``exposure_invalidated`` is the single place
        # that comparison lives. Only this reason is filtered: a disabled agent
        # or a sharing change invalidates an exposure whose run-as user still
        # matches the owner, so gating those on a mismatch would revoke nothing.
        if reason == REVOKED_OWNER_CHANGE and not exposure_invalidated(
            exposure, agent
        ):
            continue
        # A disable whose run-as account is deactivated keeps the exposure:
        # ``resolvable_exposures`` already stops resolving it on ``is_active``
        # (FR-007 says "cease resolving", not "revoke"), and the deactivation
        # is reversible, so revoking would hand the partner a new card URL
        # when the account and its agents come back.
        if reason == REVOKED_AGENT_DISABLED and _run_as_inactive(session, exposure):
            continue
        exposure.revoked_at = now
        exposure.revoked_reason = reason
        revoked_ids.append(exposure.id)
        _queue_exposure_metric(session, "revoke", reason)
        # The UI tells the owner their exposure will be revoked automatically;
        # this is what makes that true. Written through the session already in
        # flush rather than awaited, since a before_flush listener is sync.
        #
        # TWO recipients when they differ: the agent's OWNER, the party FR-041
        # names — it is their agent that just went dark externally — and the
        # run-as user, whose data boundary the exposure was running inside.
        from app.a2a.notifications import ACTIVITY_REVOKED
        from app.models.activity import Activity

        summary = (
            f"Reason: {reason}. Any partner using this agent needs "
            "a new card URL and new credentials before it works again."
        )
        recipients = {exposure.run_as_user_id, agent.user_id} - {None}
        for recipient in recipients:
            session.add(
                Activity(
                    user_id=recipient,
                    activity_type=ACTIVITY_REVOKED,
                    agent_id=agent_id,
                    agent_name=agent.name,
                    title="External exposure revoked",
                    summary=summary,
                    extra_data={"reason": reason},
                )
            )

        # FR-041's other half: the revocation is audit-logged naming the
        # triggering change, so every grant in the trail has its matching
        # revocation (SC-012). Written directly rather than through
        # ``log_audit`` because before_flush is sync. Anchored on the run-as
        # user (the column is NOT NULL and the agent's owner may be NULL); the
        # email slot names the system actor, since no human performed this —
        # a human's revocation goes through the API and is audited there.
        from app.models.audit_log import AuditLog

        session.add(
            AuditLog(
                user_id=exposure.run_as_user_id,
                user_email="system:a2a-auto-revocation",
                action="configure",
                entity_type="a2a_exposure",
                entity_id=exposure.id,
                entity_name=agent.name,
                summary=(
                    f"External exposure of '{agent.name}' was revoked "
                    f"automatically: {reason}"
                ),
                details={"reason": reason, "agent_id": str(agent_id)},
            )
        )
        logger.info(
            "a2a: revoked exposure %s for agent %s (%s)",
            exposure.id,
            agent_id,
            reason,
        )

    # Credentials die with the exposures, in ONE statement rather than one per
    # row: the model permits several live exposures per agent, and a before_flush
    # listener is a bad place to emit N statements for a set we already hold.
    #
    # DELETED, not detached: a row with a nulled a2a_exposure_id is one no
    # cleanup path could find again, and is indistinguishable from a
    # dynamically-registered MCP client — so it would also permanently occupy a
    # slot under the DCR cap. Only a LEGACY exposure has one of these; a partner
    # credential now belongs to the oauth_a2a integration and outlives any
    # single agent's exposure by design.
    if revoked_ids:
        from app.models.oauth_client import OAuthClient

        session.execute(
            delete(OAuthClient).where(
                OAuthClient.a2a_exposure_id.in_(revoked_ids)
            )
        )
    return bool(revoked_ids)


#: What makes this listener re-evaluate an agent's exposure.
#:
#: ``integration_bindings`` is the obvious one — it IS the opt-in. The other
#: two are here because :func:`_expose` refuses on them, and a refusal that
#: never gets reconsidered is permanent: the agent goes on binding an A2A
#: integration, every read surface goes on deriving "exposed" from that
#: binding, and no exposure row ever appears. Both were reachable.
#:
#: * ``enabled`` — bind an integration to a disabled agent (refused, correctly)
#:   and then enable it. Re-enabling is not a revocation, so nothing else looks
#:   at the agent again.
#: * ``user_id`` — bind to an agent with no owner to run as (refused, fail
#:   closed) and then give it one. The seed pass backfills exactly this way on
#:   a company-managed row, so the recovery has to be automatic.
#:
#: Deliberately NOT every watched attribute. A sharing or tier change is a
#: revocation, and re-minting on the spot would make the teardown FR-041
#: requires into a rotation nobody asked for — so when a revocation fires,
#: only an explicit re-binding in the same request re-exposes (that is the
#: ``revoked`` argument below). These three are the opposite case: nothing was
#: torn down, a refusal was simply lifted.
#:
#: **The invariant, since it is otherwise only inferable from ``_before_flush``:
#: ``enabled`` and ``user_id`` trigger a resync only on flushes where no
#: exposure was actually revoked.** ``_WATCHED`` names both too, and the two
#: sets are read together — a teardown wins, and what "a teardown happened"
#: means is the return of ``_revoke_live_exposures``, never the mere presence
#: of a reason.
_RESYNC_ON = ("integration_bindings", "enabled", "user_id")


def _run_as_or_none(agent: Agent) -> uuid.UUID | None:
    """The identity external work would run as, or ``None`` to refuse.

    Fail closed rather than invent one: an ownerless agent simply does not
    become exposed, whichever entry point asked.
    """
    from app.a2a.exposure import RunAsRequiredError, resolve_run_as

    try:
        return resolve_run_as(agent)
    except RunAsRequiredError:
        logger.warning(
            "a2a: agent %s binds an A2A integration but has no owner to "
            "run as — not exposing",
            agent.id,
        )
        return None


def _exposure_values(agent: Agent, run_as: uuid.UUID) -> dict:
    """What an exposure IS, in one place.

    Both entry points build it from here — the ORM add for an edited agent and
    the Core insert for a created one — so the two cannot drift into exposing
    on different terms.
    """
    from app.a2a.exposure import _mint_card_token

    return {
        "agent_id": agent.id,
        "card_token": _mint_card_token(),
        "run_as_user_id": run_as,
    }


def _exposed_notice_values(agent: Agent, run_as: uuid.UUID) -> list[dict]:
    """FR-005: the people whose reach just went external, one dict each.

    The mitigation the exposure-authority decision rests on. Management
    authority was deliberately not narrowed to owners — a config-tier group
    member may bind an integration to somebody else's agent — so the owner is
    TOLD rather than the member being blocked.

    Two recipients when they differ, mirroring the revocation notice: the
    agent's OWNER, whose agent just became reachable, and the run-as user,
    whose data boundary the partner's work will execute inside — on a
    company-managed agent those are frequently not the same person, and the
    second is the one actually exposed.

    The actor is NOT named. A flush listener has no request context to read one
    from, and inventing one would be worse than omitting it; the recipient
    still learns the fact that matters, and the audit trail records who made
    the binding edit.
    """
    from app.a2a.notifications import ACTIVITY_EXPOSED

    return [
        {
            "user_id": recipient,
            "activity_type": ACTIVITY_EXPOSED,
            "agent_id": agent.id,
            "agent_name": agent.name,
            "title": f"'{agent.name}' was exposed to external agents",
            "summary": (
                "An A2A integration was bound to this agent, making it "
                "reachable by external A2A clients. External work runs "
                "inside the run-as user's data boundary."
            ),
        }
        for recipient in {agent.user_id, run_as} - {None}
    ]


def _expose(session: Session, agent: Agent) -> bool:
    """Mint an exposure for an already-persisted *agent*, and notify.

    The edit path and :func:`converge_exposures_sync`. A newly created agent
    goes through :func:`_sync_new_agent_exposure`, which cannot add ORM
    objects — see there for why. Answers whether an exposure was added.
    """
    from app.models.activity import Activity

    # ``is False``, not ``not``: ``enabled`` is a Python-side default applied
    # at INSERT, so it reads ``None`` on an object that has not been flushed.
    # Only persistent agents reach this today, but the truthiness form is what
    # silently refused every newly created agent (see _sync_new_agent_exposure).
    if agent.enabled is False:
        return False
    run_as = _run_as_or_none(agent)
    if run_as is None:
        return False
    session.add(A2AExposure(**_exposure_values(agent, run_as)))
    for values in _exposed_notice_values(agent, run_as):
        session.add(Activity(**values))
    _queue_exposure_metric(session, "expose", EXPOSED_BOUND)
    logger.info("a2a: exposed agent %s (A2A integration bound)", agent.id)
    return True


def converge_exposures_sync(session: Session, integration_id: uuid.UUID | None = None) -> int:
    """Expose every agent that binds an inbound A2A integration and has no
    live exposure; answer how many were exposed.

    The flush listener only acts when an agent's bindings, ``enabled`` or
    ``user_id`` move. Direction lives on the integration's settings row, so a
    binding made while that row was missing (an integration created before
    every create path provisioned it, #11778) was neither direction at bind
    time and minted nothing; the row arriving later (a migration backfill,
    :func:`app.a2a.providers.provision_settings`,
    :func:`app.a2a.providers.change_direction`) changes no agent, so nothing
    re-ran. This is the converge step for that order of events: same rule as
    the listener (``binds_a2a`` decides, direction included), same values and
    notices (:func:`_expose`), and the same refusals (a disabled or ownerless
    agent stays unexposed).

    ``integration_id`` narrows to the agents binding that one integration (the
    settings-row paths); ``None`` scans every agent binding an A2A provider
    key (the startup pass). Additive only: it never revokes, so it cannot
    tear down a live partner integration.
    """
    from sqlalchemy import Text, cast, or_

    from app.a2a.providers import ALL_A2A_PROVIDERS, binds_a2a, bound_integration_ids

    bindings_text = cast(Agent.integration_bindings, Text)
    live = select(A2AExposure.id).where(A2AExposure.agent_id == Agent.id, A2AExposure.revoked_at.is_(None)).exists()
    stmt = select(Agent).where(~live)
    if integration_id is not None:
        stmt = stmt.where(bindings_text.contains(str(integration_id)))
    else:
        stmt = stmt.where(or_(*(bindings_text.contains(f'"{key}"') for key in sorted(ALL_A2A_PROVIDERS))))
    with session.no_autoflush:
        candidates = session.execute(stmt).scalars().all()

    exposed = 0
    for agent in candidates:
        if integration_id is not None and integration_id not in bound_integration_ids(agent):
            continue
        if binds_a2a(session, agent) and _expose(session, agent):
            exposed += 1
    if exposed:
        logger.info("a2a: converged %d bound agent(s) with no live exposure", exposed)
    return exposed


async def converge_exposures(db, integration_id: uuid.UUID | None = None) -> int:
    """The async door to :func:`converge_exposures_sync`; the caller commits."""
    return await db.run_sync(converge_exposures_sync, integration_id)


#: Where :func:`_sync_new_agent_exposure` parks work for :func:`_after_flush`.
_PENDING = "_a2a_pending_exposures"


def _sync_new_agent_exposure(session: Session, agent: Agent) -> None:
    """Queue an exposure for an agent being CREATED with A2A bindings set.

    A pending object needs its own entry point rather than a reuse of
    :func:`_sync_implicit_exposure`: ``_really_changed`` reads attribute
    history against a loaded value there is none of, so it reports no change
    and the agent would never be exposed. There is nothing to revoke and no
    existing exposure to find either, so this is the bind half alone.

    Without it the invariant breaks in the one direction nothing self-heals:
    ``POST /agents`` carrying ``integration_bindings`` (``CreateAgentRequest``
    accepts them) would produce an agent every read surface reports as exposed
    — they all derive it from ``binds_a2a`` (which resolves direction) —
    with no exposure row, no card
    token and no notification, until somebody happened to edit the bindings
    again.

    **Queued rather than added, and that is the whole subtlety.** The exposure
    carries ``agent_id`` as a real foreign key, and nothing tells the unit of
    work that ``agents`` must be inserted before ``a2a_exposures``: the two
    models are joined by a ``ForeignKey`` column but by no ``relationship()``,
    and inter-mapper insert order comes from relationships alone. Adding the
    exposure here therefore raced its own agent and failed on the FK about as
    often as not. :func:`_after_flush` writes it once the agent row exists.
    """
    from app.a2a.providers import binds_a2a

    # ``binds_a2a`` takes the session because exposure follows the
    # integration's DIRECTION, not merely the provider key it is bound under.
    # This path serves agent CREATION, which is a separate code path from the
    # edit path below and is covered separately by SC-020 for exactly that
    # reason: a direction bug fixed in one and not the other would leave
    # "create an agent with an outbound binding already set" exposing it.
    if not binds_a2a(session, agent) or agent.enabled is False:
        return
    run_as = _run_as_or_none(agent)
    if run_as is None:
        return
    # The primary key is a Python-side default SQLAlchemy applies at INSERT.
    # Settling it now is what lets the queued exposure name it — and it is
    # exactly the value the default would have produced.
    if agent.id is None:
        agent.id = uuid.uuid4()
    session.info.setdefault(_PENDING, []).append((agent, run_as))


def _after_flush(session: Session, _flush_context) -> None:
    """Write the exposures queued for agents created in this flush.

    Core inserts, not ORM adds: an object added to the session here is not
    picked up by the flush that just ran, and ``session.commit()`` does not run
    another. A Core statement executes immediately on the open transaction, in
    which the agent rows now exist — which is the ordering this exists to get.

    Entries are consumed one at a time and only after their writes land, so a
    statement that raises mid-way leaves the agents behind it queued for the
    next flush rather than dropping them silently. Popping the whole list up
    front would turn one failure into an unbounded, invisible divergence.
    """
    pending = session.info.get(_PENDING)
    if not pending:
        return

    from sqlalchemy import insert

    from app.models.activity import Activity

    while pending:
        agent, run_as = pending[0]
        session.execute(insert(A2AExposure).values(**_exposure_values(agent, run_as)))
        # One statement for the notices: executemany over the value dicts,
        # matching the batching the credential delete already uses.
        notices = _exposed_notice_values(agent, run_as)
        if notices:
            session.execute(insert(Activity), notices)
        pending.pop(0)
        _queue_exposure_metric(session, "expose", EXPOSED_BOUND)
        logger.info("a2a: exposed agent %s (created with an A2A binding)", agent.id)

    session.info.pop(_PENDING, None)


def _after_soft_rollback(session: Session, _previous_transaction) -> None:
    """Discard queued exposures whose transaction no longer exists.

    ``session.info`` is not transaction-scoped and ``rollback()`` does not
    clear it, so a flush that raises after ``before_flush`` ran leaves entries
    behind — ``_after_flush`` never fired to consume them. The next successful
    flush on that same session would then insert an exposure naming an
    ``agent_id`` whose INSERT was rolled back, failing on the foreign key in a
    request with nothing to do with A2A.

    Per-request sessions bound the blast radius, but it is reachable in
    process: ``seed_builtin_agents`` commits several times on one long-lived
    session, and any retry-after-IntegrityError path hits it directly.

    ``after_soft_rollback`` rather than ``after_rollback`` so savepoint and
    subtransaction rollbacks are covered too.

    The queued counter increments are not settled here but in
    :func:`_after_transaction_end`, which sees the ends this event misses.
    """
    session.info.pop(_PENDING, None)


def _sync_implicit_exposure(
    session: Session, agent: Agent, *, revoked: bool = False
) -> None:
    """Make the agent's exposure follow its A2A integration bindings.

    Exposure is not a switch of its own: an agent is reachable exactly while
    it binds at least one A2A integration (``a2a.providers``). Maintaining
    that here rather than in the update endpoint means it holds wherever
    bindings are edited — the agent API, an import, a flow — instead of only
    on the path someone remembered to wire.

    Binding gained -> ensure a live exposure (card token minted, run-as
    resolved) and tell the owner. Last binding removed -> revoke, which
    retires the card URL exactly as the old explicit toggle did.

    ``revoked`` says a revocation already fired for this agent in this flush,
    and it narrows what counts as a trigger. See :data:`_RESYNC_ON`.
    """
    from app.a2a.providers import binds_a2a

    state = inspect(agent)
    triggers = ("integration_bindings",) if revoked else _RESYNC_ON
    if not any(_really_changed(state, attr) for attr in triggers):
        return

    # The DB filter narrows; the Python re-check is what makes this correct
    # when a revocation landed EARLIER IN THIS SAME FLUSH. That revocation is
    # an attribute change not yet written, so the row still reads
    # ``revoked_at IS NULL`` in the database — but the identity map hands back
    # the same object carrying the pending timestamp, and treating it as live
    # would skip re-minting exactly when re-minting is the point.
    live = next(
        (
            e
            for e in session.execute(
                select(A2AExposure).where(
                    A2AExposure.agent_id == agent.id,
                    A2AExposure.revoked_at.is_(None),
                )
            ).scalars()
            if e.revoked_at is None
        ),
        None,
    )

    # THE security-relevant line of this feature: ``binds_a2a`` resolves
    # direction and answers only for INBOUND bindings. Provider-key
    # membership alone would fail in the dangerous direction — an operator
    # binding an integration to obtain a delegation tool would have their
    # agent exposed to a partner nobody approved.
    #
    # The ``elif`` is equally load-bearing: an agent whose last INBOUND
    # binding is removed must be revoked even if it still binds outbound
    # ones, because it is no longer reachable. Testing "binds any A2A
    # integration" here would leave a dead exposure resolving.
    if binds_a2a(session, agent):
        if live is None:
            _expose(session, agent)
    elif live is not None:
        _revoke_live_exposures(session, agent, REVOKED_UNBOUND)


def _before_flush(session: Session, _flush_context, _instances) -> None:
    # NEW agents: the bind half only. There is nothing to revoke on an object
    # that has never been persisted, and a pending agent must never look like
    # one whose tier just changed — which is why this is a separate pass with
    # its own entry point rather than a widened iteration below.
    for obj in session.new:
        if isinstance(obj, Agent):
            _sync_new_agent_exposure(session, obj)

    # EDITED agents: revoke what the change invalidated, THEN let the binding
    # state have the last word. The two are not alternatives, and treating them
    # as such (an early `continue` after revoking) dropped a binding added in
    # the same request: one update that changed sharing and bound an A2A
    # integration revoked the exposure and ignored the binding, leaving
    # ``binds_a2a() == True`` with nothing live — the exact divergence the
    # migration's converge step exists to clean up, reintroduced at runtime and
    # self-healing only on some later bindings edit.
    #
    # Running both is safe and is the correct outcome: the revoked exposure is
    # retired for good (its card URL never resolves again) and a fresh one is
    # minted under the NEW boundary, which is what the operator just asked for
    # in that same request. A change that makes the agent unreachable at all —
    # disabling it — still ends with no exposure, because ``_expose`` refuses a
    # disabled agent.
    for obj in session.dirty:
        if not isinstance(obj, Agent):
            continue
        reason = _revocation_reason(obj)
        # Narrow the resync only when a teardown ACTUALLY happened. A reason
        # that revoked nothing — no live exposure, or an owner change the
        # exposure survived — must not suppress the lifted-refusal resync,
        # or the recovery below never runs on a personally-owned agent.
        revoked = (
            _revoke_live_exposures(session, obj, reason)
            if reason is not None
            else False
        )
        _sync_implicit_exposure(session, obj, revoked=revoked)


def register_a2a_hooks() -> None:
    """Install the listener. Idempotent — safe to call from startup twice.

    **Deliberately not gated on chat availability**, unlike every route in
    this feature. The routes gate *reachability*; this listener maintains
    *data integrity*. Exposure rows outlive the gate: a deployment that
    switches AI off, or whose last EE platform disconnects, keeps its rows
    with their boundary frozen. Gated too, an agent flipped between personal
    and company-managed in that window would keep an exposure whose boundary
    no longer matches it, and chat coming back would resolve it — the silent
    widening FR-041 exists to prevent.

    The cost of leaving it on is one ``isinstance`` per pending or dirty object
    on a flush that changes no agent, and a single lookup only when a watched
    attribute actually moved. Agent tier changes are rare; the hazard above
    is not worth trading for that.

    FIVE listeners. ``before_flush`` decides everything and handles edits;
    ``after_flush`` writes the exposures queued for agents created in that same
    flush, which cannot be added earlier without racing their own agent row's
    INSERT; ``after_soft_rollback`` discards that queue when the transaction it
    belonged to is gone. ``after_commit`` counts the exposure changes once they
    are durable (a SAVEPOINT release hands them to the transaction around it),
    and ``after_transaction_end`` drops those of any transaction that ended
    without committing them.
    """
    if not event.contains(Session, "before_flush", _before_flush):
        event.listen(Session, "before_flush", _before_flush)
    if not event.contains(Session, "after_flush", _after_flush):
        event.listen(Session, "after_flush", _after_flush)
    if not event.contains(Session, "after_soft_rollback", _after_soft_rollback):
        event.listen(Session, "after_soft_rollback", _after_soft_rollback)
    if not event.contains(Session, "after_commit", _after_commit):
        event.listen(Session, "after_commit", _after_commit)
    if not event.contains(Session, "after_transaction_end", _after_transaction_end):
        event.listen(Session, "after_transaction_end", _after_transaction_end)
